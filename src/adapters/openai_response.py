"""M3 · OpenAI Responses API ↔ IR adapter（05B 主对象）

映射要点：
  * system：OpenAI Responses 顶层 instructions → IR system 字段
  * input：数组 → IR messages；content 项含 input_text / output_text / function_call /
    function_call_output（来自历史输出回填）
  * previous_response_id：有状态字段，由 M2 状态层在会话上管理，请求经网关注入
  * 用量：input_tokens / output_tokens，命中侧 input_tokens_details.cached_tokens
"""

from __future__ import annotations

import json
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
class OpenAIResponseAdapter(BaseAdapter):
    protocol = "openai_response"
    endpoint_path = "/v1/responses"

    # ---- 外部请求 → IRRequest ----
    @staticmethod
    def to_ir(payload: dict[str, Any]) -> IRRequest:
        system_blocks: list[ContentBlock] = []
        instrs = payload.get("instructions")
        if isinstance(instrs, str):
            if instrs:
                system_blocks.append(ContentBlock.text_block(instrs))
        elif isinstance(instrs, list):
            for instr in instrs:
                if isinstance(instr, str):
                    system_blocks.append(ContentBlock.text_block(instr))
                elif isinstance(instr, dict) and instr.get("type") == "input_text":
                    system_blocks.append(ContentBlock.text_block(instr.get("text", "")))

        messages: list[IRMessage] = []
        for item in payload.get("input") or []:
            if isinstance(item, str):
                messages.append(IRMessage.text("user", item))
                continue
            role = item.get("role", "user")
            content = item.get("content", [])
            blocks: list[ContentBlock] = []
            for part in content:
                if isinstance(part, str):
                    blocks.append(ContentBlock.text_block(part))
                    continue
                ptype = part.get("type")
                if ptype in ("input_text", "output_text", "text", "message"):
                    blocks.append(ContentBlock.text_block(part.get("text", "")))
                elif ptype == "function_call":
                    blocks.append(
                        ContentBlock.tool_use_block(
                            id=part.get("call_id") or part.get("id", ""),
                            name=part.get("name", ""),
                            input=_parse_args(part.get("arguments")),
                        )
                    )
                elif ptype == "function_call_output":
                    blocks.append(
                        ContentBlock.tool_result_block(
                            tool_use_id=part.get("call_id", ""),
                            content=part.get("output", ""),
                        )
                    )
            messages.append(IRMessage(role=role, content=blocks))

        tools: list[IRTool] = []
        for t in payload.get("tools") or []:
            tools.append(
                IRTool(
                    name=t.get("name", ""),
                    description=t.get("description", ""),
                    input_schema=t.get("parameters", {}),
                )
            )

        dropped, degradation = collect_dropped_params(payload, "openai_response")
        extra: dict[str, Any] = {
            "previous_response_id": payload.get("previous_response_id"),
            "reasoning": payload.get("reasoning"),
            "store": payload.get("store"),
            "_dropped_params": dropped,
            "_degradation": degradation,
        }
        return IRRequest(
            model=payload.get("model", ""),
            system=system_blocks,
            messages=messages,
            tools=tools,
            stream=bool(payload.get("stream", False)),
            max_tokens=BaseAdapter._max_tokens(payload.get("max_output_tokens")),
            temperature=float(payload.get("temperature", 1.0) or 1.0),
            extra=extra,
        )

    # ---- IRResponse → 外部响应 ----
    @staticmethod
    def from_ir(ir: IRResponse) -> dict[str, Any]:
        output: list[dict[str, Any]] = []
        for b in ir.content:
            if b.type in ("text", "thinking") and b.text:
                output.append({"type": "output_text", "text": b.text})
            elif b.type == "tool_use" and b.name:
                output.append(
                    {
                        "type": "function_call",
                        "id": f"fc_{ir.id}",
                        "call_id": b.id,
                        "name": b.name,
                        "arguments": json.dumps(b.input or {}, ensure_ascii=False),
                    }
                )

        usage = ir.usage
        return {
            "id": ir.id,
            "object": "response",
            "model": ir.model,
            "status": "completed" if not ir.stop_reason or ir.stop_reason != "tool_use" else "incomplete",
            "output": output,
            "previous_response_id": ir.previous_response_id,
            "usage": {
                "input_tokens": usage.total_input(),
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.total_input() + usage.output_tokens,
                "input_tokens_details": {
                    "cached_tokens": usage.cache_read_input_tokens,
                },
            },
        }

    @staticmethod
    def parse_usage(raw: dict[str, Any]) -> IRUsage:
        u = raw.get("usage") or {}
        details = u.get("input_tokens_details") or {}
        return IRUsage(
            input_tokens=int(u.get("input_tokens", 0) or 0),
            output_tokens=int(u.get("output_tokens", 0) or 0),
            cache_creation_input_tokens=0,
            cache_read_input_tokens=int(details.get("cached_tokens", 0) or 0),
        )

    # ---- 流式 chunk 渲染 ----
    @staticmethod
    def stream_chunk(ir: IRResponse, index: int) -> Optional[dict[str, Any]]:
        """把增量 IRResponse 转成 OpenAI Response 的 SSE 事件（简化版）。"""
        if not ir.content:
            return None
        b = ir.content[-1]
        if b.type in ("text", "thinking") and b.text:
            return {"type": "response.output_text.delta", "item_id": ir.id,
                    "output_index": 0, "content_index": 0, "delta": b.text}
        if b.type == "tool_use" and b.name:
            return {"type": "response.function_call_arguments.delta", "item_id": ir.id,
                    "output_index": 0, "delta": json.dumps(b.input or {}, ensure_ascii=False)}
        return None


def _parse_args(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    try:
        return json.loads(arguments or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
