"""M3 · 协议适配层 —— Adapter 基类与注册表

每个协议实现「to_ir（外部请求 → IRRequest）」与「from_ir（IRResponse → 外部响应）」，
加上 usage 解析。3 个协议 = 3 对 adapter，6 个方向压成 3。
"""

from __future__ import annotations

from typing import Any, Optional, Type

from ..ir.schema import IRRequest, IRResponse, IRUsage


class BaseAdapter:
    """协议适配器基类。子类实现四个静态方法。"""

    #: 协议标识（gateway 路由用）
    protocol: str = "base"
    #: 端点路径（网关据此选择 adapter，如 /v1/chat/completions）
    endpoint_path: str = "/"

    @staticmethod
    def to_ir(payload: dict[str, Any]) -> IRRequest:
        raise NotImplementedError

    @staticmethod
    def from_ir(ir: IRResponse) -> dict[str, Any]:
        raise NotImplementedError

    @staticmethod
    def parse_usage(raw: dict[str, Any]) -> IRUsage:
        raise NotImplementedError

    @staticmethod
    def stream_chunks_from_ir(ir: IRResponse) -> list[dict[str, Any]]:
        """将 IR 响应转成该协议的流式 chunk 列表（非流式调用时可省略）。"""
        return []

    @staticmethod
    def stream_chunk(ir: IRResponse, index: int) -> Optional[dict[str, Any]]:
        """把增量 IRResponse 转成该协议的 SSE 事件对象（流式透传用）。

        返回 None 表示该协议不支持流式（或该增量无可渲染内容）。
        """
        return None

    @staticmethod
    def _max_tokens(v: Any) -> int:
        """max_tokens 解析：保留预热请求的 0（方案 3.7），None 时才回退 1024。"""
        if v is None:
            return 1024
        try:
            return int(v)
        except (TypeError, ValueError):
            return 1024


class AdapterRegistry:
    """按端点路径注册 / 选择 adapter。"""

    def __init__(self) -> None:
        self._by_path: dict[str, Type[BaseAdapter]] = {}

    def register(self, adapter: Type[BaseAdapter]) -> None:
        self._by_path[adapter.endpoint_path] = adapter

    def get(self, path: str) -> Optional[Type[BaseAdapter]]:
        # 支持前缀匹配（如 /v1/chat/completions 带 query）
        if path in self._by_path:
            return self._by_path[path]
        base = path.split("?")[0]
        return self._by_path.get(base)

    def paths(self) -> list[str]:
        return sorted(self._by_path.keys())


# 全局注册表（模块导入时填充）
registry = AdapterRegistry()


def _register(adapter: Type[BaseAdapter]) -> Type[BaseAdapter]:
    registry.register(adapter)
    return adapter


# ---------------------------------------------------------------------------
# 丢弃参数与降级路径记录（方案 3.3 ④⑤ / 3.6）
# ---------------------------------------------------------------------------

#: 明确不做、若用户传入则必须记录并降级的字段（方案 3.6）
UNSUPPORTED_KEYS = {
    "openai_chat": ("response_format", "modalities", "audio", "input_audio", "image_url"),
    "openai_response": ("modalities", "audio", "include", "stream_options"),
    "anthropic": ("metadata",),  # 大多数透传，此处为占位
}


def collect_dropped_params(
    payload: dict[str, Any], protocol: str
) -> tuple[list[str], str]:
    """识别请求中被静默丢弃/降级的字段。

    返回 (dropped_params, degradation_path)。
      * 结构化输出（response_format / 严格 tool_choice）→ 显式降级
      * 多模态字段 → 静默丢弃（明确不做）
    """
    dropped: list[str] = []
    degradation = "静默通过"
    for key in UNSUPPORTED_KEYS.get(protocol, ()):
        if key in payload and payload.get(key) is not None:
            dropped.append(key)
            if key in ("response_format",):
                degradation = "显式降级"
    # OpenAI Response 严格结构化输出（text.format=json_schema）
    if protocol == "openai_response" and payload.get("text", {}).get("format"):
        dropped.append("text.format")
        degradation = "显式降级"
    return dropped, degradation
