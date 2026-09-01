"""M3 · Anthropic Messages ↔ IR adapter（共享右侧端点）

映射要点（作业 Table 3）：
  * system：Anthropic 顶层 system 字段 → IR system blocks（缓存前缀）
  * 消息内容：Anthropic content 已是 block 数组 → IR 1:1
  * 工具调用：tool_use block + stop_reason=tool_use ↔ IR tool_use block
  * 用量：input_tokens / cache_creation_input_tokens / cache_read_input_tokens（权威来源）
  * 缓存断点：Anthropic 在 block 上带 cache_control；IR 用 cache_control 记录断点位置
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from ..ir.schema import (
    ContentBlock,
    IRMessage,
    IRRequest,
    IRResponse,
    IRTool,
    IRUsage,
)
from .base import BaseAdapter, _register, collect_dropped_params


@_register
class AnthropicAdapter(BaseAdapter):
    protocol = "anthropic"
    endpoint_path = "/v1/messages"

    # ---- 外部请求 → IRRequest ----
    @staticmethod
    def to_ir(payload: dict[str, Any]) -> IRRequest:
        system_blocks: list[ContentBlock] = []
        sys_raw = payload.get("system")
        if sys_raw is None:
            pass
        elif isinstance(sys_raw, str):
            system_blocks.append(ContentBlock.text_block(sys_raw))
        elif isinstance(sys_raw, list):
            for b in sys_raw:
                if isinstance(b, str):
                    system_blocks.append(ContentBlock.text_block(b))
                elif isinstance(b, dict):
                    if b.get("type") == "text":
                        system_blocks.append(
                            ContentBlock.text_block(b.get("text", ""))
                        )
                    # 其他 system block 类型暂不支持

        messages: list[IRMessage] = []
        cache_control: list[int] = []
        for idx, msg in enumerate(payload.get("messages") or []):
            blocks: list[ContentBlock] = []
            has_breakpoint = False
            for b in msg.get("content") or []:
                if isinstance(b, str):
                    blocks.append(ContentBlock.text_block(b))
                    continue
                btype = b.get("type")
                if btype == "text":
                    blocks.append(ContentBlock.text_block(b.get("text", "")))
                elif btype == "tool_use":
                    blocks.append(
                        ContentBlock.tool_use_block(
                            id=b.get("id", ""),
                            name=b.get("name", ""),
                            input=b.get("input", {}) or {},
                        )
                    )
                elif btype == "tool_result":
                    content = b.get("content")
                    text = ""
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        text = "".join(
                            c.get("text", "") for c in content if isinstance(c, dict)
                        )
                    blocks.append(
                        ContentBlock.tool_result_block(
                            tool_use_id=b.get("tool_use_id", ""),
                            content=text,
                            is_error=bool(b.get("is_error")),
                        )
                    )
                elif btype == "thinking":
                    blocks.append(
                        ContentBlock(
                            type="thinking", thinking=b.get("thinking", ""),
                            extra={"signature": b.get("signature")},
                        )
                    )
                if b.get("cache_control"):
                    has_breakpoint = True
            messages.append(IRMessage(role=msg.get("role", "user"), content=blocks))
            if has_breakpoint:
                cache_control.append(idx)

        tools: list[IRTool] = []
        for t in payload.get("tools") or []:
            tools.append(
                IRTool(
                    name=t.get("name", ""),
                    description=t.get("description", ""),
                    input_schema=t.get("input_schema", {}),
                )
            )

        dropped, degradation = collect_dropped_params(payload, "anthropic")
        extra: dict[str, Any] = {
            k: payload[k]
            for k in ("metadata", "stop_sequences", "top_p")
            if k in payload
        }
        extra["_dropped_params"] = dropped
        extra["_degradation"] = degradation
        return IRRequest(
            model=payload.get("model", ""),
            system=system_blocks,
            messages=messages,
            tools=tools,
            stream=bool(payload.get("stream", False)),
            max_tokens=BaseAdapter._max_tokens(payload.get("max_tokens")),
            temperature=float(payload.get("temperature", 1.0) or 1.0),
            cache_control=cache_control,
            extra=extra,
        )

    # ---- IRResponse → 外部响应 ----
    @staticmethod
    def from_ir(ir: IRResponse) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        for b in ir.content:
            if b.type == "text" and b.text is not None:
                content.append({"type": "text", "text": b.text})
            elif b.type == "thinking" and b.thinking is not None:
                content.append({"type": "thinking", "thinking": b.thinking})
            elif b.type == "tool_use" and b.name:
                content.append(
                    {
                        "type": "tool_use",
                        "id": b.id,
                        "name": b.name,
                        "input": b.input or {},
                    }
                )
            elif b.type == "tool_result" and b.tool_use_id:
                content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": b.tool_use_id,
                        "content": b.text or "",
                    }
                )

        usage = ir.usage
        return {
            "id": ir.id,
            "type": "message",
            "role": ir.role,
            "model": ir.model,
            "content": content,
            "stop_reason": ir.stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_creation_input_tokens": usage.cache_creation_input_tokens,
                "cache_read_input_tokens": usage.cache_read_input_tokens,
            },
        }

    @staticmethod
    def parse_usage(raw: dict[str, Any]) -> IRUsage:
        from ..observability.metrics import usage_from_anthropic
        return usage_from_anthropic(raw)

    # ---- IRRequest → 上游请求体（含 cache_control 断点布局）----
    @staticmethod
    def ir_to_payload(ir: IRRequest, warmup: bool = False) -> dict[str, Any]:
        """IRRequest → Anthropic Messages 请求体。

        缓存断点布局（方案 1.3 图 2 / 3.7）——3 固定 + 1 滚动，最多 4 个（官方硬上限）：
          断点1 = tools 后   → 落在 system[0]（tools 渲染在 system 前，见渲染顺序）
          断点2 = system 后  → 落在 system[-1]
          断点3 = 历史静态段后 → 落在 messages[-2] 最后一块
          断点4 = 滚动尾部    → 落在 messages[-1] 最后一块

        预热请求（warmup=True，方案 3.7）：cache_control 必须打在与后续请求共享的
        前缀末尾（system prompt），不能打在占位 user 消息上——因此只打 system 断点，
        跳过 messages 断点（否则缓存条目以占位消息为键，后续永不命中）。
        """
        system: list[dict[str, Any]] = []
        for i, b in enumerate(ir.system):
            d = b.to_dict()
            # 断点1（system 首块）与断点2（system 末块）；单块时二者重合，只打一次
            if i == 0 or i == len(ir.system) - 1:
                d["cache_control"] = {"type": "ephemeral"}
            system.append(d)

        messages: list[dict[str, Any]] = []
        n = len(ir.messages)
        for mi, m in enumerate(ir.messages):
            blocks = [b.to_dict() for b in m.content]
            if blocks and not warmup:
                is_tail = mi == n - 1                       # 断点4 滚动尾部
                is_history_end = n >= 2 and mi == n - 2     # 断点3 历史静态段末
                if is_tail or is_history_end:
                    blocks[-1]["cache_control"] = {"type": "ephemeral"}
            messages.append({"role": m.role, "content": blocks})

        payload: dict[str, Any] = {
            "model": ir.model,
            "max_tokens": ir.max_tokens,
            "temperature": ir.temperature,
            "system": system or None,
            "messages": messages,
            "tools": [t.to_dict() for t in ir.tools] or None,
        }
        # 协议私有字段透传
        for k in ("top_p", "stop_sequences", "metadata"):
            if k in ir.extra and ir.extra[k] is not None:
                payload[k] = ir.extra[k]
        return payload

    @staticmethod
    def response_to_ir(raw: dict[str, Any]) -> IRResponse:
        """Anthropic Messages 响应 → IRResponse（保留 text / tool_use / thinking 块）。"""
        content: list[ContentBlock] = []
        for b in raw.get("content") or []:
            btype = b.get("type")
            if btype == "text":
                content.append(ContentBlock.text_block(b.get("text", "")))
            elif btype == "tool_use":
                content.append(
                    ContentBlock.tool_use_block(
                        id=b.get("id", ""), name=b.get("name", ""),
                        input=b.get("input", {}) or {},
                    )
                )
            elif btype == "thinking":
                content.append(
                    ContentBlock(type="thinking", thinking=b.get("thinking", ""),
                                 extra={"signature": b.get("signature")})
                )
        return IRResponse(
            id=raw.get("id", uuid.uuid4().hex),
            model=raw.get("model", ""),
            content=content,
            stop_reason=raw.get("stop_reason"),
            usage=AnthropicAdapter.parse_usage(raw),
            raw=raw,
        )

    # ---- 流式 chunk 渲染 ----
    @staticmethod
    def stream_chunk(ir: IRResponse, index: int) -> Optional[dict[str, Any]]:
        """把增量 IRResponse 转成 Anthropic SSE 事件（content_block_delta 简化版）。

        约定：stream 上游每个 yield 的 IRResponse.content 只含 1 个增量块。
        """
        if not ir.content:
            return None
        b = ir.content[-1]
        if b.type == "text" and b.text:
            return {"type": "content_block_delta", "index": index - 1,
                    "delta": {"type": "text_delta", "text": b.text}}
        if b.type == "tool_use" and b.name:
            return {"type": "content_block_delta", "index": index - 1,
                    "delta": {"type": "input_json_delta",
                              "partial_json": json.dumps(b.input or {}, ensure_ascii=False)}}
        if b.type == "thinking" and b.thinking:
            return {"type": "content_block_delta", "index": index - 1,
                    "delta": {"type": "thinking_delta", "thinking": b.thinking}}
        return None
