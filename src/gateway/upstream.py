"""M4 · 上游客户端（mock + 真实 Anthropic / OpenAI）。

统一接口：async complete(ir_request) -> IRResponse，以及 stream 版本。

MockUpstream 内置「前缀缓存」模拟，用于离线演示和 M5 实验：
  * 相同前缀第二次调用返回 cache_read（命中）；
  * 前缀变化返回 cache_creation（写入新前缀）；
  * 前缀很短（低于最小可缓存阈值）时静默不缓存（模拟 20-block / 阈值失效）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from typing import Any, AsyncIterator, Optional

from ..ir.schema import (
    CACHE_LOOKBACK_WINDOW_BLOCKS,
    DEFAULT_MIN_CACHE_THRESHOLD,
    MODEL_CACHE_THRESHOLDS,
    ContentBlock,
    IRRequest,
    IRResponse,
    IRUsage,
)

# ---------------------------------------------------------------------------
# 真实上游（httpx 异步客户端）
# ---------------------------------------------------------------------------

class AnthropicUpstream:
    """Anthropic Messages 上游。请求为 Anthropic 协议格式。"""

    def __init__(self, api_key: str, base_url: str = "https://api.anthropic.com"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    async def complete(self, ir: IRRequest) -> IRResponse:
        import httpx

        from ..adapters.anthropic import AnthropicAdapter

        payload = AnthropicAdapter.ir_to_payload(ir, warmup=(ir.max_tokens == 0))
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self.base_url}/v1/messages", json=payload, headers=self._headers()
            )
            resp.raise_for_status()
            raw = resp.json()
        return AnthropicAdapter.response_to_ir(raw)

    async def stream(self, ir: IRRequest) -> AsyncIterator[IRResponse]:
        """流式透传：解析 Anthropic SSE（content_block_delta 增量 + message_delta 用量）。"""
        import httpx

        from ..adapters.anthropic import AnthropicAdapter

        payload = AnthropicAdapter.ir_to_payload(ir, warmup=(ir.max_tokens == 0))
        payload["stream"] = True
        rid = uuid.uuid4().hex
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST", f"{self.base_url}/v1/messages",
                json=payload, headers=self._headers(),
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        evt = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    etype = evt.get("type")
                    if etype == "message_start":
                        # input 侧 usage（含 cache_creation / cache_read）在 message_start 事件
                        mu = (evt.get("message") or {}).get("usage") or {}
                        yield IRResponse(
                            id=rid, model=ir.model, content=[],
                            usage=IRUsage(
                                input_tokens=int(mu.get("input_tokens", 0) or 0),
                                cache_creation_input_tokens=int(mu.get("cache_creation_input_tokens", 0) or 0),
                                cache_read_input_tokens=int(mu.get("cache_read_input_tokens", 0) or 0),
                            ),
                        )
                    elif etype == "content_block_delta":
                        d = evt.get("delta") or {}
                        if d.get("type") == "text_delta":
                            yield IRResponse(id=rid, model=ir.model,
                                             content=[ContentBlock.text_block(d.get("text", ""))])
                        elif d.get("type") == "input_json_delta":
                            yield IRResponse(id=rid, model=ir.model,
                                             content=[ContentBlock(type="tool_use",
                                                                  text=d.get("partial_json", ""))])
                    elif etype == "message_delta":
                        # output 侧 usage 在 message_delta 事件
                        usage = IRUsage(output_tokens=int(
                            (evt.get("usage") or {}).get("output_tokens", 0) or 0))
                        yield IRResponse(id=rid, model=ir.model, content=[],
                                         stop_reason=evt.get("delta", {}).get("stop_reason"),
                                         usage=usage)


class OpenAIUpstream:
    """OpenAI Chat Completions 上游（转发外部 OpenAI 请求的落地侧）。"""

    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}",
                "content-type": "application/json"}

    async def complete(self, ir: IRRequest) -> IRResponse:
        import httpx

        from ..adapters.openai_chat import OpenAIChatAdapter

        payload = OpenAIChatAdapter.ir_to_payload(ir)
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions", json=payload, headers=self._headers()
            )
            resp.raise_for_status()
            raw = resp.json()
        return OpenAIChatAdapter.response_to_ir(raw)

    async def stream(self, ir: IRRequest) -> AsyncIterator[IRResponse]:
        """流式透传：解析 OpenAI SSE（delta.content / delta.tool_calls）。"""
        import httpx

        from ..adapters.openai_chat import OpenAIChatAdapter

        payload = OpenAIChatAdapter.ir_to_payload(ir)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}  # 末尾 usage chunk（含 cached_tokens）
        rid = uuid.uuid4().hex
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST", f"{self.base_url}/chat/completions",
                json=payload, headers=self._headers(),
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        evt = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if not evt.get("choices"):
                        # 末尾 usage chunk：带出完整 usage（含 cached_tokens）供埋点
                        yield IRResponse(id=rid, model=ir.model, content=[],
                                         usage=OpenAIChatAdapter.parse_usage(
                                             {"usage": evt.get("usage") or {}}))
                        continue
                    delta = (evt.get("choices") or [{}])[0].get("delta") or {}
                    if delta.get("content"):
                        yield IRResponse(id=rid, model=ir.model,
                                         content=[ContentBlock.text_block(delta["content"])])
                    for tc in delta.get("tool_calls") or []:
                        fn = tc.get("function") or {}
                        yield IRResponse(id=rid, model=ir.model,
                                         content=[ContentBlock.tool_use_block(
                                             id=tc.get("id", ""), name=fn.get("name", ""),
                                             input={"arguments": fn.get("arguments", "")})])


# ---------------------------------------------------------------------------
# Mock 上游（离线演示 + 实验）
# ---------------------------------------------------------------------------

class MockUpstream:
    """确定性 mock：模拟 Anthropic 前缀缓存行为。

    规则（对齐方案 1.3 / 图 2 / 洞察 3、4）：
      * 前缀 = system + 前 N 条消息的序列化；
      * 前缀长度 < min_cache_tokens → 静默不缓存（cache 全 0）；
      * 前缀块数 > 20 → 尾部断点失效（本次视为 miss）；
      * 相同前缀 → cache_read = 前缀 token 数；新前缀 → cache_creation = 前缀 token 数。
    """

    def __init__(
        self,
        min_cache_tokens: int = DEFAULT_MIN_CACHE_THRESHOLD,
        lookback_blocks: int = CACHE_LOOKBACK_WINDOW_BLOCKS,
        echo_text: Optional[str] = None,
    ):
        self.min_cache_tokens = min_cache_tokens
        self.lookback_blocks = lookback_blocks
        self.echo_text = echo_text or "（mock 上游回复：链路已打通）"
        self._seen: dict[str, int] = {}  # prefix_hash -> 前缀 token 数

    def _threshold_for(self, model: str) -> int:
        """按模型查最小可缓存阈值（方案洞察 4：非单调，必须逐模型查表）。

        精确匹配 MODEL_CACHE_THRESHOLDS；带前缀（如 /v1/ 或 vendor 前缀）做子串匹配；
        查不到回退到配置的 min_cache_tokens。
        """
        if model in MODEL_CACHE_THRESHOLDS:
            return MODEL_CACHE_THRESHOLDS[model]
        for key, val in MODEL_CACHE_THRESHOLDS.items():
            if key in model:
                return val
        return self.min_cache_tokens

    def _prefix_tokens(
        self, ir: IRRequest
    ) -> tuple[str, int, list[int], int]:
        """计算「稳定缓存前缀」的哈希 / 累计 token / 记忆块 token / 历史轮数。

        对齐方案图 2：渲染顺序 tools → system → messages，易变内容放最后。
        稳定前缀 = system + messages[:-1]（最后一条是易变的新问题，不参与前缀）。

        记忆块约定（实验层）：system 第 1 块是提示词，第 2 块及以后是记忆注入块。
        """
        parts = [
            "".join(b.text or "" for b in ir.system),
        ] + [
            f"{m.role}:" + "".join(b.text or "" for b in m.content)
            for m in ir.messages[:-1]
        ]
        prefix_text = "\n".join(parts)
        # token 估算：中文约 1 token/字（示意口径）
        tokens = max(1, len(prefix_text))
        memory_block_tokens = [max(1, len(b.text or "")) for b in ir.system[1:]]
        history_rounds = max(0, len(ir.messages) - 1)
        return (
            hashlib.sha1(prefix_text.encode("utf-8")).hexdigest()[:16],
            tokens,
            memory_block_tokens,
            history_rounds,
        )

    async def complete(self, ir: IRRequest) -> IRResponse:
        await asyncio.sleep(0.01)  # 模拟网络延迟（很小，不占资源）

        # 预热请求（max_tokens:0）：响应畸形——content 空数组、stop_reason=max_tokens、
        # usage 完整（output_tokens=0）。v3 核实为官方支持用法；在断点处写缓存条目，
        # 建立后续请求可命中的缓存（方案 3.6 并发约束：先串行预热再计量）。
        if ir.max_tokens == 0:
            prefix_hash, prefix_tokens, _, _ = self._prefix_tokens(ir)
            self._seen[prefix_hash] = prefix_tokens  # 写入缓存条目（预热）
            return IRResponse(
                id=f"mock_{uuid.uuid4().hex[:8]}",
                model=ir.model,
                content=[],
                stop_reason="max_tokens",
                usage=IRUsage(
                    input_tokens=prefix_tokens, output_tokens=0,
                    cache_creation_input_tokens=prefix_tokens,
                    cache_read_input_tokens=0,
                ),
            )

        prefix_hash, prefix_tokens, _, history_rounds = self._prefix_tokens(ir)

        threshold = self._threshold_for(ir.model)
        total_blocks = sum(len(m.content) for m in ir.messages) + len(ir.system)
        cache_creation = cache_read = 0
        # v3 语义：最小可缓存长度按「断点处累计前缀」判定（v2 按单块是错的）。
        # 前缀累加：分块不重置阈值，但会推迟达标时点。
        # 判定对象 = 最后断点（滚动尾部）处累计前缀 = 稳定前缀累计 token。
        if prefix_tokens >= threshold and total_blocks <= self.lookback_blocks:
            if prefix_hash in self._seen:
                cache_read = prefix_tokens  # 命中
            else:
                cache_creation = prefix_tokens  # 写入新前缀
                self._seen[prefix_hash] = prefix_tokens

        usage = IRUsage(
            input_tokens=prefix_tokens + 40,
            output_tokens=20,
            cache_creation_input_tokens=cache_creation,
            cache_read_input_tokens=cache_read,
        )
        return IRResponse(
            id=f"mock_{uuid.uuid4().hex[:8]}",
            model=ir.model,
            content=[ContentBlock.text_block(self.echo_text)],
            stop_reason="end_turn",
            usage=usage,
        )

    async def stream(self, ir: IRRequest) -> AsyncIterator[IRResponse]:
        """流式（离线）：复用 complete 的缓存计算，把回包切成 2 块增量 yield。

        最后一块携带完整 usage（供埋点），其余块 usage 为空。
        """
        resp = await self.complete(ir)
        text = (resp.content[0].text if resp.content else "") or ""
        if not text:
            # 预热请求（max_tokens:0）无输出：直接带 usage 返回
            yield IRResponse(id=resp.id, model=ir.model, content=[],
                             stop_reason=resp.stop_reason, usage=resp.usage)
            return
        half = max(1, len(text) // 2)
        parts = [text[:half], text[half:]]
        for i, p in enumerate(parts):
            is_last = i == len(parts) - 1
            yield IRResponse(
                id=resp.id, model=ir.model,
                content=[ContentBlock.text_block(p)],
                stop_reason=resp.stop_reason if is_last else None,
                usage=resp.usage if is_last else IRUsage(),
            )


def make_upstream(protocol: str, config) -> Any:
    """按协议与配置构造上游。mock=True 恒返回 MockUpstream。"""
    if config.mock:
        return MockUpstream(min_cache_tokens=config.mock_min_cache_tokens)
    if protocol == "openai":
        return OpenAIUpstream(config.openai_api_key, config.openai_base_url)
    return AnthropicUpstream(config.anthropic_api_key, config.anthropic_base_url)
