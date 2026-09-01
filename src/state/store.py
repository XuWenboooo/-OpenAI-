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
    """SQLite 会话存储。

    Session 边界参数（方案 3.9.1 / TRACK 04）由调用方按配置传入，做成可切换：
      * default_ttl_seconds / end_policy / on_end —— 对应 ② 起止定义；
      * get_replay_history 的 mode —— 对应 ③ 重放起点（由网关按 config.session_replay_from 调用）。
    默认假设值对齐 v3(1)：ttl=1800s、on_end=archive（结束后归档可回放）。
    """

    def __init__(
        self,
        path: str | Path = "data/sessions.db",
        default_ttl_seconds: int = 1800,
        end_policy: str = "ttl",
        on_end: str = "archive",
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.default_ttl_seconds = default_ttl_seconds
        self.end_policy = end_policy
        self.on_end = on_end
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
        ttl_seconds: Optional[int] = None,
        session_id: Optional[str] = None,
        memory_cap: Optional[int] = None,
    ) -> Session:
        """创建会话。身份三者齐全可「直接登记」（作业 5.2 踩坑 4）。

        TRACK 04 对齐（方案 3.9 兜底）：若 04 组尚未定 Session 标识粒度，
        先按 task-id 单键假设推进——调用方可用 find_active_by_task 复用同 task 会话，
        避免另造一套 Session 概念（耦合点 3：previous_response_id 状态键复用 04 标识）。

        memory_cap（TRACK 04 第三参数 / 老师方向：经弹网页 Session Init 链接按会话设置）：
          传入则写入会话 meta（0 = 不限制，>0 限制单会话注入记忆块数）；不传则不写入，
          由网关回退到全局 config.session_memory_cap。
        """
        sid = session_id or f"sess_{uuid.uuid4().hex[:12]}"
        now = utcnow_iso()
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds
        expires = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + ttl)
        )
        meta: dict[str, Any] = {}
        if memory_cap is not None:
            meta["memory_cap"] = memory_cap
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO sessions
                   (session_id, team_id, agent_id, task_id, meta, created_at, updated_at, expires_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (sid, team_id, agent_id, task_id, json.dumps(meta, ensure_ascii=False),
                 now, now, expires),
            )
            conn.commit()
        finally:
            conn.close()
        return Session(
            session_id=sid, team_id=team_id, agent_id=agent_id,
            task_id=task_id, meta=meta, created_at=now, updated_at=now, expires_at=expires,
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
        ttl = self.default_ttl_seconds
        expires = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + ttl)
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
        self, session_id: str, mode: str = "full", max_turns: int = 20
    ) -> list[dict[str, Any]]:
        """历史重放起点规则（TRACK 04 耦合点 1：重放起点决定缓存前缀）。

        对齐 v3(1) 3.9.1 的 replay_from 三模式：
          * full             —— 全量重放（默认假设值，最保守、最易被 04 兼容）
          * sliding_window  —— 只重放最近 max_turns 轮（保护 20-block 回看窗口）
          * last_breakpoint —— 从上次缓存断点（replay_cursor）之后重放；无游标等同全量
        """
        turns = self.list_turns(session_id, limit=max_turns + 1)
        # list_turns 是倒序，转正序
        turns = list(reversed(turns))
        if mode in ("full", "last_breakpoint"):
            if mode == "last_breakpoint":
                cursor = self.get_replay_cursor(session_id) or 0
                return turns[cursor:]
            return turns
        return turns[-max_turns:]

    def get_replay_cursor(self, session_id: str) -> Optional[int]:
        """读取上次缓存断点游标（last_breakpoint 模式用；无则 None）。"""
        s = self.get_session(session_id)
        if not s:
            return None
        cur = (s.meta or {}).get("replay_cursor")
        return int(cur) if isinstance(cur, (int, float)) or (isinstance(cur, str) and cur.isdigit()) else None

    def set_replay_cursor(self, session_id: str, cursor: int) -> None:
        """记录上次缓存断点游标（网关在构造前缀时写入）。"""
        s = self.get_session(session_id)
        if not s:
            return
        meta = dict(s.meta or {})
        meta["replay_cursor"] = cursor
        self.update_meta(session_id, meta)

    # ---- TTL 淘汰 ----
    def evict_expired(self) -> int:
        """按 end_policy 淘汰过期会话。

        on_end=drop  → 真正删除，返回删除数；
        on_end=archive → 保留（结束后归档可回放，仅刷新不删），返回 0。
        """
        now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        conn = self._connect()
        try:
            expired = conn.execute(
                "SELECT session_id FROM sessions WHERE expires_at <= ?", (now,)
            ).fetchall()
            ids = [r["session_id"] for r in expired]
            if self.on_end == "drop":
                for sid in ids:
                    conn.execute("DELETE FROM session_turns WHERE session_id = ?", (sid,))
                conn.executemany(
                    "DELETE FROM sessions WHERE session_id = ?",
                    [(sid,) for sid in ids],
                )
                conn.commit()
                return len(ids)
            # archive：保留会话与轮次，仅重置 TTL 以免反复扫描（仍在可回放状态）
            for sid in ids:
                conn.execute(
                    "UPDATE sessions SET expires_at=NULL WHERE session_id=?", (sid,)
                )
            conn.commit()
            return 0
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
