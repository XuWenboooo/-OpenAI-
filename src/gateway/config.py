"""M4 · 网关配置。

硬件友好约束（方案/分工共识）：
  * max_concurrency 默认 2——实验并发限流，避免 API 堆积与 token 成本失控；
  * 单进程运行，数据落 SQLite，不引入外部 DB；
  * mock=True 时完全不联网（离线可跑通整条转换链路 + 埋点）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GatewayConfig:
    host: str = "127.0.0.1"
    port: int = 8096  # 与犀牛鸟 MemoryProxy 端口一致的口径

    #: 外部端点默认转到的上游协议（方案：Anthropic 为共享右侧端点）
    upstream_protocol: str = "anthropic"  # anthropic | openai

    #: 是否使用内置 mock 上游（离线可演示，不消耗 API 费用）
    mock: bool = True

    #: mock 上游的最小可缓存阈值（token）。
    #: 默认 8 便于离线演示「第二轮命中」；实验 G1 用 512 验证「块太短静默不缓存」。
    mock_min_cache_tokens: int = 8

    #: 并发上限（2–4；批跑实验时再调低）
    max_concurrency: int = 2

    #: 请求体上限（字节）
    max_body_bytes: int = 2 * 1024 * 1024

    # ---------------------------------------------------------------------------
    # Session 边界（方案 3.9.1 / TRACK 04：做成可切换配置，04 口径变化只改配置不返工）
    # 默认假设值（v3(1) 第 13–14 页对照表；实验命中率结论均在此前提下陈述）：
    #   ① 标识粒度 = task-id 单键
    #   ② 起止定义 = 首次请求开始 / 超时或显式关闭结束 / 结束后归档可回放
    #   ③ 重放起点 = 全量重放（最保守、最易被 04 组兼容）
    # 切换链路：config(3 开关) → 状态层读配置取历史 → 重放起点策略 → 缓存前缀(命中率)
    # 纪律：key_granularity 仅启动时读（中途切换会错乱已存键值）；replay_from/end_policy 可热切换
    # ---------------------------------------------------------------------------
    # ① 标识粒度：single_task（默认，task-id 单键）/ three_level（x-team-id+x-agent-id+x-task-id 组合键）
    session_key_granularity: str = "single_task"
    session_key_fields: list[str] = field(default_factory=lambda: ["task-id"])
    # ③ 重放起点：full（全量）/ sliding_window（最近 N 条）/ last_breakpoint（上次断点之后）
    session_replay_from: str = "full"
    session_sliding_window_n: int = 20
    # ② 起止定义：ttl（超时淘汰）/ explicit_close（显式关闭）；on_end：archive（保留可回放）/ drop（删除）
    session_end_policy: str = "ttl"
    session_ttl_seconds: int = 1800
    session_on_end: str = "archive"
    # ③ 记忆上限（TRACK 04 第三参数 / 老师方向：经弹网页 Session Init 链接按会话设置）
    #   0 = 不限制（全部注入，保持现状）；>0 时限制单会话注入记忆块数（与 _inject_memories 幂等去重配合）。
    #   有效上限优先级：会话 meta.memory_cap > 本全局默认 > 0(不限制)。
    session_memory_cap: int = 0

    #: 数据目录（SQLite 埋点 + 会话）
    data_dir: str = "data"

    #: API 凭据（真实模式才需要；从环境变量读取）
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    anthropic_base_url: str = field(
        default_factory=lambda: os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    )
    openai_base_url: str = field(
        default_factory=lambda: os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    )

    @property
    def metrics_db(self) -> str:
        return str(Path(self.data_dir) / "metrics.db")

    @property
    def sessions_db(self) -> str:
        return str(Path(self.data_dir) / "sessions.db")

    def resolve_upstream(self, adapter_protocol: str) -> str:
        """决定某外部端点转去哪个上游协议。

        方案默认：所有外部端点 → Anthropic（共享右侧端点）。
        可配置：openai_chat → openai；openai_response → openai；anthropic → anthropic。
        """
        if self.upstream_protocol == "auto":
            # 同源直通（anthropic 转 anthropic，openai 两类转 openai），跨源走 anthropic
            if adapter_protocol in ("openai_chat", "openai_response"):
                return "openai"
            return "anthropic"
        return self.upstream_protocol
