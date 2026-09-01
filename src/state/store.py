"""M2 · 状态层：会话表 + TTL 淘汰 + previous_response_id（SQLite）

职责：
  * 存会话归属（team/agent/task）与对话历史；
  * 管理 previous_response_id（OpenAI Response 有状态字段）；
  * TTL 淘汰过期会话（配合命中率埋点轮转，保持磁盘轻量）。

设计约束（硬件友好）：单文件 SQLite + WAL，连接短生命周期，
不引入外部 DB 服务；内存占用小、I/O 轻。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..ir.schema import IRMessage, IRSessionContext, utcnow_iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    team_id TEXT,
    agent_id TEXT,
    task_id TEXT,
    previous_response_id TEXT,
    meta TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS session_turns (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    prev_response_id TEXT,
    request_json TEXT NOT NULL,
    response_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON session_turns(session_id);
"""


@dataclass
class Session:
    """运行时会话对象。"""

    session_id: str
    team_id: Optional[str] = None
    agent_id: Optional[str] = None
    task_id: Optional[str] = None
    previous_response_id: Optional[str] = None
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)
    expires_at: Optional[str] = None

    def to_context(self) -> IRSessionContext:
        return IRSessionContext(
            session_id=self.session_id,
            team_id=self.team_id,
            agent_id=self.agent_id,
            task_id=self.task_id,
            previous_response_id=self.previous_response_id,
        )


class SessionStore:
    """SQLite 会话存储。"""

    def __init__(self, path: str | Path = "data/sessions.db", default_ttl_hours: int = 24):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.default_ttl_hours = default_ttl_hours
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL")
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # ---- 生命周期 ----
    def create_session(
        self,
        *,
        team_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        task_id: Optional[str] = None,
        ttl_hours: Optional[int] = None,
        session_id: Optional[str] = None,
    ) -> Session:
        """创建会话。身份三者齐全可「直接登记」（作业 5.2 踩坑 4）。

        TRACK 04 对齐（方案 3.9 兜底）：若 04 组尚未定 Session 标识粒度，
        先按 task-id 单键假设推进——调用方可用 find_active_by_task 复用同 task 会话，
        避免另造一套 Session 概念（耦合点 3：previous_response_id 状态键复用 04 标识）。
        """
        sid = session_id or f"sess_{uuid.uuid4().hex[:12]}"
        now = utcnow_iso()
        ttl = ttl_hours if ttl_hours is not None else self.default_ttl_hours
        expires = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + ttl * 3600)
        )
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO sessions
                   (session_id, team_id, agent_id, task_id, created_at, updated_at, expires_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (sid, team_id, agent_id, task_id, now, now, expires),
            )
            conn.commit()
        finally:
            conn.close()
        return Session(
            session_id=sid, team_id=team_id, agent_id=agent_id,
            task_id=task_id, created_at=now, updated_at=now, expires_at=expires,
        )

    def get_session(self, session_id: str) -> Optional[Session]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return Session(
            session_id=row["session_id"],
            team_id=row["team_id"],
            agent_id=row["agent_id"],
            task_id=row["task_id"],
            previous_response_id=row["previous_response_id"],
            meta=json.loads(row["meta"] or "{}"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
        )

    def touch(self, session_id: str) -> None:
        """刷新 updated_at 与 TTL。"""
        now = utcnow_iso()
        ttl = self.default_ttl_hours
        expires = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + ttl * 3600)
        )
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE sessions SET updated_at=?, expires_at=? WHERE session_id=?",
                (now, expires, session_id),
            )
            conn.commit()
        finally:
            conn.close()

    # ---- previous_response_id 状态 ----
    def set_previous_response_id(self, session_id: str, response_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE sessions SET previous_response_id=?, updated_at=? WHERE session_id=?",
                (response_id, utcnow_iso(), session_id),
            )
            conn.commit()
        finally:
            conn.close()

    def query_by_prev_id(self, previous_response_id: str) -> list[str]:
        """按 previous_response_id 反查所属会话 id（支持并发分叉的多个会话）。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT session_id FROM sessions WHERE previous_response_id = ?",
                (previous_response_id,),
            ).fetchall()
            return [r["session_id"] for r in rows]
        finally:
            conn.close()

    def find_active_by_task(self, task_id: str) -> Optional[Session]:
        """按 task-id 单键找活动会话（TRACK 04 兜底：状态键复用 04 的标识体系）。

        未过期即视为活动；多个则取最近更新的一个。
        """
        if not task_id:
            return None
        now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        conn = self._connect()
        try:
            row = conn.execute(
                """SELECT * FROM sessions
                   WHERE task_id = ? AND (expires_at IS NULL OR expires_at > ?)
                   ORDER BY updated_at DESC LIMIT 1""",
                (task_id, now),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return Session(
            session_id=row["session_id"],
            team_id=row["team_id"],
            agent_id=row["agent_id"],
            task_id=row["task_id"],
            previous_response_id=row["previous_response_id"],
            meta=json.loads(row["meta"] or "{}"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
        )

    # ---- 对话轮次 ----
    def append_turn(
        self,
        session_id: str,
        request: dict[str, Any],
        response_id: Optional[str] = None,
    ) -> str:
        """记录一轮请求（用于状态重建与审计）。返回 turn_id。"""
        turn_id = f"turn_{uuid.uuid4().hex[:12]}"
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO session_turns
                   (turn_id, session_id, prev_response_id, request_json, response_id, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    turn_id, session_id,
                    self._prev_of(session_id),
                    json.dumps(request, ensure_ascii=False),
                    response_id, utcnow_iso(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        if response_id:
            self.set_previous_response_id(session_id, response_id)
        return turn_id

    def _prev_of(self, session_id: str) -> Optional[str]:
        s = self.get_session(session_id)
        return s.previous_response_id if s else None

    def list_turns(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT * FROM session_turns WHERE session_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (session_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_replay_history(
        self, session_id: str, mode: str = "window", max_turns: int = 20
    ) -> list[dict[str, Any]]:
        """历史重放起点规则（TRACK 04 耦合点 1：重放起点决定缓存前缀）。

        mode：
          * window  —— 截断窗口：只重放最近 max_turns 轮（保护 20-block 回看窗口）
          * full    —— 全量重放（会迅速突破缓存回看窗口）
        """
        turns = self.list_turns(session_id, limit=max_turns + 1)
        # list_turns 是倒序，转正序
        turns = list(reversed(turns))
        if mode == "full":
            return turns
        return turns[-max_turns:]

    # ---- TTL 淘汰 ----
    def evict_expired(self) -> int:
        """删除过期会话及其轮次，返回删除的会话数。"""
        now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        conn = self._connect()
        try:
            expired = conn.execute(
                "SELECT session_id FROM sessions WHERE expires_at <= ?", (now,)
            ).fetchall()
            ids = [r["session_id"] for r in expired]
            for sid in ids:
                conn.execute("DELETE FROM session_turns WHERE session_id = ?", (sid,))
            conn.executemany(
                "DELETE FROM sessions WHERE session_id = ?",
                [(sid,) for sid in ids],
            )
            conn.commit()
            return len(ids)
        finally:
            conn.close()

    def count(self) -> int:
        conn = self._connect()
        try:
            return int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
        finally:
            conn.close()

    def list_all(self, limit: int = 100) -> list[Session]:
        """列出会话（M6 面板用）。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        finally:
            conn.close()
        out = []
        for row in rows:
            out.append(
                Session(
                    session_id=row["session_id"],
                    team_id=row["team_id"],
                    agent_id=row["agent_id"],
                    task_id=row["task_id"],
                    previous_response_id=row["previous_response_id"],
                    meta=json.loads(row["meta"] or "{}"),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    expires_at=row["expires_at"],
                )
            )
        return out

    def update_meta(self, session_id: str, meta: dict[str, Any]) -> None:
        """更新会话 meta（M6 记忆裁剪写入）。"""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE sessions SET meta=?, updated_at=? WHERE session_id=?",
                (json.dumps(meta, ensure_ascii=False), utcnow_iso(), session_id),
            )
            conn.commit()
        finally:
            conn.close()
