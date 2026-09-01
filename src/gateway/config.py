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
