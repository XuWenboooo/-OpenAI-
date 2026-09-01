"""M1 · 可观测层：命中率埋点 + usage 解析 + 采集存储

北极星指标：缓存命中率（cache_read 命中率），辅以总 token 成本。
方案 3.3 的判定口径：
  * cache_creation 与 cache_read 同时为 0 → 未命中（miss）
  * cache_read > 0 → 命中（hit）
  * cache_creation > 0 且 cache_read == 0 → 写入新前缀（creation）

设计约束（硬件友好）：单文件 SQLite 存储，I/O 轻、内存小、日志按天轮转，
不影响正常使用。
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..ir.schema import IRUsage, utcnow_iso

# 各档位缓存写入溢价（方案 3.5：5 分钟档 vs 1 小时档，Anthropic 口径）
WRITE_PREMIUM_5MIN = 1.25
WRITE_PREMIUM_1HOUR = 2.0
# Anthropic 读取省 90%（全系一致）
ANTHROPIC_READ_DISCOUNT = 0.1
# OpenAI 读取折扣按模型代次（v3 修正，官方 Cookbook）：GPT-4o/o 系 50% / GPT-4.1 系 75% / GPT-5 系 90%
OPENAI_READ_DISCOUNT_BY_GEN: tuple[tuple[str, float], ...] = (
    ("gpt-5", 0.1),   # GPT-5 系省 90%
    ("gpt-4.1", 0.25),  # GPT-4.1 系省 75%
    ("gpt-4", 0.5),   # GPT-4 系 / o 系省 50%
    ("o1", 0.5),
    ("o3", 0.5),
)


def _openai_read_discount(model: str) -> float:
    for key, discount in OPENAI_READ_DISCOUNT_BY_GEN:
        if key in (model or "").lower():
            return discount
    return 0.5  # 默认省 50%（保守）


# 估算费率（美元 / 百万 token，输入侧；仅用于成本对照的示意，可替换为实际价）
RATE_INPUT_PER_MTOK = 3.0
RATE_OUTPUT_PER_MTOK = 15.0


def estimate_cost_usd(
    u: IRUsage,
    protocol: str = "anthropic",
    write_tier: str = "5min",
    model: str = "",
) -> float:
    """按协议与模型区分费率估算单轮成本（美元，示意）。

    方案 3.5 双重不对称：
      * Anthropic：写入按输入价 ×1.25（5min）/ ×2（1h）；读取 0.1x（省 90%）
      * OpenAI：无写入溢价；读取折扣按模型代次（v3：GPT-4o 50% / 4.1 75% / 5 90%）
    """
    write_premium = WRITE_PREMIUM_5MIN if write_tier == "5min" else WRITE_PREMIUM_1HOUR
    if protocol == "openai":
        read_mult, write_mult = _openai_read_discount(model), 1.0  # 无写入溢价
    else:
        read_mult, write_mult = ANTHROPIC_READ_DISCOUNT, write_premium
    read_cost = u.cache_read_input_tokens / 1e6 * RATE_INPUT_PER_MTOK * read_mult
    write_cost = u.cache_creation_input_tokens / 1e6 * RATE_INPUT_PER_MTOK * write_mult
    input_cost = u.input_tokens / 1e6 * RATE_INPUT_PER_MTOK
    output_cost = u.output_tokens / 1e6 * RATE_OUTPUT_PER_MTOK
    return round(read_cost + write_cost + input_cost + output_cost, 6)

def usage_from_openai(raw: dict[str, Any]) -> IRUsage:
    """从 OpenAI Chat Completions 的 usage 解析。

    ⚠️ 对账口径（方案 3.5）：OpenAI Chat 的 prompt_tokens **已包含**缓存部分
    （prompt_tokens_details.cached_tokens 是其子集）。因此 input_tokens 应为
    「不含缓存的新 token」= prompt_tokens - cached_tokens，保证 total_input()
    恰好等于 prompt_tokens，避免重复计算（对账全错的坑）。
    """
    u = raw.get("usage") or {}
    details = u.get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens", 0) or 0)
    prompt = int(u.get("prompt_tokens", 0) or 0)
    return IRUsage(
        input_tokens=max(0, prompt - cached),
        output_tokens=int(u.get("completion_tokens", 0) or 0),
        cache_creation_input_tokens=0,
        cache_read_input_tokens=cached,
    )


def usage_from_anthropic(raw: dict[str, Any]) -> IRUsage:
    """从 Anthropic Messages 的 usage 解析。

    Anthropic: input_tokens / output_tokens / cache_creation_input_tokens /
               cache_read_input_tokens（北极星指标的权威来源）
    """
    u = raw.get("usage") or {}
    return IRUsage(
        input_tokens=int(u.get("input_tokens", 0) or 0),
        output_tokens=int(u.get("output_tokens", 0) or 0),
        cache_creation_input_tokens=int(u.get("cache_creation_input_tokens", 0) or 0),
        cache_read_input_tokens=int(u.get("cache_read_input_tokens", 0) or 0),
    )


def usage_from_response(raw: dict[str, Any], protocol: str = "anthropic") -> IRUsage:
    """按协议自动分派解析。protocol ∈ {anthropic, openai}。"""
    if protocol == "openai":
        return usage_from_openai(raw)
    return usage_from_anthropic(raw)


# ---------------------------------------------------------------------------
# 埋点记录
# ---------------------------------------------------------------------------

@dataclass
class MetricRecord:
    """一次请求的命中率埋点（北极星：5 项必须暴露，方案 3.3）。

    ① 每轮缓存命中率（cache_read>0）  ② 注入 token 数  ③ 重放 token 数
    ④ 被丢弃的参数清单  ⑤ 走的降级路径（显式降级 / 静默通过）
    """

    timestamp: str = field(default_factory=utcnow_iso)
    session_id: str = ""
    group: str = ""                # 实验组标签（对照组A / B / C / D1 ...）
    protocol: str = ""             # anthropic / openai
    model: str = ""
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # ---- 方案 3.3 的 5 项可观测性 ----
    injected_tokens: int = 0            # ② 注入的记忆 token 数
    replay_tokens: int = 0              # ③ 无状态重放的历史 token 数
    dropped_params: list[str] = field(default_factory=list)   # ④ 被丢弃的参数
    degradation_path: str = "静默通过"   # ⑤ 显式降级 / 静默通过
    cost_usd: float = 0.0
    tags: dict[str, Any] = field(default_factory=dict)

    @property
    def cache_hit(self) -> bool:
        return self.cache_read_input_tokens > 0

    @property
    def cache_miss(self) -> bool:
        return self.cache_creation_input_tokens == 0 and self.cache_read_input_tokens == 0

    @classmethod
    def from_usage(
        cls,
        u: IRUsage,
        *,
        session_id: str = "",
        group: str = "",
        protocol: str = "anthropic",
        model: str = "",
        injected_tokens: int = 0,
        replay_tokens: int = 0,
        dropped_params: Optional[list[str]] = None,
        degradation_path: str = "静默通过",
        tags: Optional[dict[str, Any]] = None,
    ) -> "MetricRecord":
        return cls(
            session_id=session_id,
            group=group,
            protocol=protocol,
            model=model,
            cache_creation_input_tokens=u.cache_creation_input_tokens,
            cache_read_input_tokens=u.cache_read_input_tokens,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            injected_tokens=injected_tokens,
            replay_tokens=replay_tokens,
            dropped_params=list(dropped_params or []),
            degradation_path=degradation_path,
            cost_usd=estimate_cost_usd(u, protocol=protocol, model=model),
            tags=tags or {},
        )


# ---------------------------------------------------------------------------
# 采集存储（SQLite，单文件，轻量）
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_id TEXT,
    group_name TEXT,
    protocol TEXT,
    model TEXT,
    cache_creation_input_tokens INTEGER DEFAULT 0,
    cache_read_input_tokens INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    injected_tokens INTEGER DEFAULT 0,
    replay_tokens INTEGER DEFAULT 0,
    dropped_params TEXT DEFAULT '[]',
    degradation_path TEXT DEFAULT '静默通过',
    cost_usd REAL DEFAULT 0,
    tags TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_metrics_group ON metrics(group_name);
CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(timestamp);
"""


class MetricsStore:
    """命中率埋点存储。

    线程安全：每个操作使用短生命周期连接（连接不跨线程复用），
    WAL 模式降低读写冲突。
    """

    def __init__(self, path: str | Path = "data/metrics.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL")
            self._migrate(conn)
        finally:
            conn.close()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """对旧库自动补列（方案 v2 新增 5 项可观测性字段）。"""
        existing = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(metrics)").fetchall()
        }
        add = {
            "injected_tokens": "INTEGER DEFAULT 0",
            "replay_tokens": "INTEGER DEFAULT 0",
            "dropped_params": "TEXT DEFAULT '[]'",
            "degradation_path": "TEXT DEFAULT '静默通过'",
        }
        for col, ddl in add.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE metrics ADD COLUMN {col} {ddl}")
        conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # ---- 写入 ----
    def record(self, rec: MetricRecord) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                """INSERT INTO metrics
                   (timestamp, session_id, group_name, protocol, model,
                    cache_creation_input_tokens, cache_read_input_tokens,
                    input_tokens, output_tokens, injected_tokens, replay_tokens,
                    dropped_params, degradation_path, cost_usd, tags)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rec.timestamp, rec.session_id, rec.group, rec.protocol, rec.model,
                    rec.cache_creation_input_tokens, rec.cache_read_input_tokens,
                    rec.input_tokens, rec.output_tokens, rec.injected_tokens,
                    rec.replay_tokens, json.dumps(rec.dropped_params, ensure_ascii=False),
                    rec.degradation_path, rec.cost_usd,
                    json.dumps(rec.tags, ensure_ascii=False),
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    # ---- 查询 ----
    def query(
        self,
        *,
        group: Optional[str] = None,
        protocol: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM metrics WHERE 1=1"
        params: list[Any] = []
        if group:
            sql += " AND group_name = ?"
            params.append(group)
        if protocol:
            sql += " AND protocol = ?"
            params.append(protocol)
        if since:
            sql += " AND timestamp >= ?"
            params.append(since)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def summary(self, *, group: Optional[str] = None) -> dict[str, Any]:
        """按 group（或全部）聚合：请求数 / 命中率 / token / 成本。"""
        conn = self._connect()
        try:
            where, params = "", []
            if group:
                where, params = "WHERE group_name = ?", [group]
            row = conn.execute(
                f"""SELECT
                       COUNT(*) AS n,
                       SUM(CASE WHEN cache_read_input_tokens > 0 THEN 1 ELSE 0 END) AS hits,
                       SUM(CASE WHEN cache_creation_input_tokens > 0
                                AND cache_read_input_tokens = 0 THEN 1 ELSE 0 END) AS creations,
                       SUM(CASE WHEN cache_creation_input_tokens = 0
                                AND cache_read_input_tokens = 0 THEN 1 ELSE 0 END) AS misses,
                       SUM(cache_creation_input_tokens) AS total_creation,
                       SUM(cache_read_input_tokens) AS total_read,
                       SUM(input_tokens) AS total_input,
                       SUM(output_tokens) AS total_output,
                       SUM(injected_tokens) AS total_injected,
                       SUM(replay_tokens) AS total_replay,
                       SUM(cost_usd) AS total_cost
                   FROM metrics {where}""",
                params,
            ).fetchone()
        finally:
            conn.close()

        n = int(row["n"] or 0)
        hits = int(row["hits"] or 0)
        return {
            "group": group or "ALL",
            "requests": n,
            "hit_requests": hits,
            "creation_requests": int(row["creations"] or 0),
            "miss_requests": int(row["misses"] or 0),
            "hit_rate": round(hits / n, 4) if n else 0.0,
            "total_creation_tokens": int(row["total_creation"] or 0),
            "total_read_tokens": int(row["total_read"] or 0),
            "total_input_tokens": int(row["total_input"] or 0),
            "total_output_tokens": int(row["total_output"] or 0),
            "total_injected_tokens": int(row["total_injected"] or 0),
            "total_replay_tokens": int(row["total_replay"] or 0),
            "total_cost_usd": round(float(row["total_cost"] or 0.0), 6),
        }

    # ---- 维护 ----
    def delete_group(self, group: str) -> int:
        """删除某实验组的全部记录（实验重跑前清空，避免跨运行统计污染）。"""
        conn = self._connect()
        try:
            cur = conn.execute("DELETE FROM metrics WHERE group_name = ?", (group,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def prune(self, keep_days: int = 7) -> int:
        """按时间清理过期埋点（日志轮转），返回删除行数。"""
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - keep_days * 86400)
        )
        conn = self._connect()
        try:
            cur = conn.execute("DELETE FROM metrics WHERE timestamp < ?", (cutoff,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def demo_record() -> MetricRecord:
    """示例：一条命中 vs 一条未命中的埋点（供单元测试/演示）。"""
    hit = IRUsage(
        input_tokens=1200,
        output_tokens=300,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=5120,
    )
    miss = IRUsage(
        input_tokens=1200,
        output_tokens=300,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    return MetricRecord.from_usage(hit, group="demo-hit", protocol="anthropic", model="claude-opus-5")
