"""M3 · OpenAI Chat Completions ↔ IR adapter（05A 主对象）

映射要点（作业 Table 3）：
  * system 位置：OpenAI 的 messages[].role=system → IR 的 system 字段
  * 消息内容：OpenAI content 为字符串 → IR 单个 text block；tool_calls → tool_use block
  * 工具调用：OpenAI tool_calls + finish_reason=tool_calls ↔ IR tool_use block + stop_reason=tool_use
  * 用量：prompt_tokens / prompt_tokens_details.cached_tokens
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


def _content_text_to_blocks(content: Any) -> list[ContentBlock]:
    """OpenAI Chat 的 content 可能是字符串或（新版本）数组，统一转 block。"""
    if content is None:
        return []
    if isinstance(content, str):
        return [ContentBlock.text_block(content)]
    # 少数情况下为数组（如含多段文本），取其中 text 部分
    blocks: list[ContentBlock] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            blocks.append(ContentBlock.text_block(item.get("text", "")))
        elif isinstance(item, str):
            blocks.append(ContentBlock.text_block(item))
    return blocks


@_register
class OpenAIChatAdapter(BaseAdapter):
    protocol = "openai_chat"
    endpoint_path = "/v1/chat/completions"

    # ---- 外部请求 → IRRequest ----
    @staticmethod
    def to_ir(payload: dict[str, Any]) -> IRRequest:
        system_blocks: list[ContentBlock] = []
        messages: list[IRMessage] = []
        tools: list[IRTool] = []

        for msg in payload.get("messages", []):
            role = msg.get("role", "user")
            if role == "system":
                # OpenAI system 提到 IR 顶层 system（缓存前缀）
                system_blocks.extend(_content_text_to_blocks(msg.get("content")))
                continue
            if role == "tool":
                # 工具结果：tool_call_id 指向 assistant 的 tool_use
                messages.append(
                    IRMessage(
                        role="user",
                        content=[
                            ContentBlock.tool_result_block(
                                tool_use_id=msg.get("tool_call_id", ""),
                                content=str(msg.get("content", "")),
                                is_error=bool(msg.get("is_error")),
                            )
                        ],
                        name=msg.get("name"),
                    )
                )
                continue
            blocks = _content_text_to_blocks(msg.get("content"))
            # assistant 的工具调用（顶层 tool_calls）
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                blocks.append(
                    ContentBlock.tool_use_block(
                        id=tc.get("id", ""), name=fn.get("name", ""), input=args
                    )
                )
            messages.append(IRMessage(role=role, content=blocks, name=msg.get("name")))

        for tool in payload.get("tools") or []:
            if isinstance(tool, dict) and tool.get("type") == "function":
                fn = tool.get("function", {})
                tools.append(
                    IRTool(
                        name=fn.get("name", ""),
                        description=fn.get("description", ""),
                        input_schema=fn.get("parameters", {}),
                    )
                )

        dropped, degradation = collect_dropped_params(payload, "openai_chat")
        extra: dict[str, Any] = {
            k: payload[k]
            for k in ("user", "stop", "n", "seed", "logprobs")
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
            extra=extra,
        )

    # ---- IRResponse → 外部响应 ----
    @staticmethod
    def from_ir(ir: IRResponse) -> dict[str, Any]:
        text_parts = [b.text for b in ir.content if b.type in ("text", "thinking") and b.text]
        tool_calls = [
            {
                "id": b.id,
                "type": "function",
                "function": {
                    "name": b.name,
                    "arguments": json.dumps(b.input or {}, ensure_ascii=False),
                },
            }
            for b in ir.content
            if b.type == "tool_use" and b.id
        ]
        message: dict[str, Any] = {
            "role": ir.role,
            "content": "".join(text_parts) or None,
        }
        if tool_calls:
            message["tool_calls"] = tool_calls

        finish_reason = ir.stop_reason
        if finish_reason == "tool_use":
            finish_reason = "tool_calls"

        usage = ir.usage
        return {
            "id": ir.id,
            "object": "chat.completion",
            "model": ir.model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": usage.total_input(),
                "completion_tokens": usage.output_tokens,
                "total_tokens": usage.total_input() + usage.output_tokens,
                "prompt_tokens_details": {
                    "cached_tokens": usage.cache_read_input_tokens,
                },
            },
        }

    @staticmethod
    def parse_usage(raw: dict[str, Any]) -> IRUsage:
        from ..observability.metrics import usage_from_openai
        return usage_from_openai(raw)

    # ---- IRRequest → 上游请求体 ----
    @staticmethod
    def ir_to_payload(ir: IRRequest) -> dict[str, Any]:
        """IRRequest → OpenAI Chat Completions 请求体。

        缓存语义（方案 3.5）：OpenAI 为自动缓存（prompt ≥ 1024 token 参与），
        无显式断点概念，故这里只需正确还原 messages / tools。
        """
        messages: list[dict[str, Any]] = []
        if ir.system:
            sys_text = "".join(b.text or "" for b in ir.system)
            messages.append({"role": "system", "content": sys_text})
        for m in ir.messages:
            text = "".join(b.text or "" for b in m.content if b.type in ("text", "thinking"))
            msg: dict[str, Any] = {"role": m.role, "content": text or None}
            tool_calls = [
                {
                    "id": b.id,
                    "type": "function",
                    "function": {"name": b.name,
                                 "arguments": json.dumps(b.input or {}, ensure_ascii=False)},
                }
                for b in m.content if b.type == "tool_use" and b.id
            ]
            if tool_calls:
                msg["tool_calls"] = tool_calls
            messages.append(msg)

        payload: dict[str, Any] = {
            "model": ir.model,
            "max_tokens": ir.max_tokens,
            "temperature": ir.temperature,
            "messages": messages,
        }
        if ir.tools:
            payload["tools"] = [
                {"type": "function", "function": {
                    "name": t.name, "description": t.description,
                    "parameters": t.input_schema,
                }}
                for t in ir.tools
            ]
        # 协议私有字段透传
        for k in ("user", "stop", "n", "seed", "logprobs"):
            if k in ir.extra and ir.extra[k] is not None:
                payload[k] = ir.extra[k]
        return payload

    @staticmethod
    def response_to_ir(raw: dict[str, Any]) -> IRResponse:
        """OpenAI Chat 响应 → IRResponse（保留 text 与 tool_calls 块）。"""
        choice = (raw.get("choices") or [{}])[0]
        msg = choice.get("message", {})
        content: list[ContentBlock] = []
        if msg.get("content"):
            content.append(ContentBlock.text_block(msg.get("content", "")))
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            content.append(
                ContentBlock.tool_use_block(
                    id=tc.get("id", ""), name=fn.get("name", ""), input=args
                )
            )
        return IRResponse(
            id=raw.get("id", uuid.uuid4().hex),
            model=raw.get("model", ""),
            content=content,
            stop_reason=choice.get("finish_reason"),
            usage=OpenAIChatAdapter.parse_usage(raw),
            raw=raw,
        )

    # ---- 流式 chunk 渲染 ----
    @staticmethod
    def stream_chunk(ir: IRResponse, index: int) -> Optional[dict[str, Any]]:
        """把增量 IRResponse 转成 OpenAI Chat 的 SSE chunk（chat.completion.chunk）。"""
        if not ir.content:
            return None
        b = ir.content[-1]
        delta: dict[str, Any] = {}
        if b.type in ("text", "thinking") and b.text:
            delta["content"] = b.text
        elif b.type == "tool_use" and b.id:
            delta["tool_calls"] = [{
                "index": 0, "id": b.id, "type": "function",
                "function": {"name": b.name,
                             "arguments": json.dumps(b.input or {}, ensure_ascii=False)},
            }]
        else:
            return None
        finish = ir.stop_reason
        if finish == "tool_use":
            finish = "tool_calls"
        return {
            "id": ir.id, "object": "chat.completion.chunk",
            "model": ir.model, "choices": [{"index": 0, "delta": delta,
                                            "finish_reason": finish}],
        }
