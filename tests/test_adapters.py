"""M3 · 三对 adapter 双向转换测试（tests/test_adapters.py）

运行：python tests/test_adapters.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.adapters import anthropic, openai_chat, openai_response  # noqa: E402
from src.adapters.base import registry  # noqa: E402
from src.ir.schema import ContentBlock, IRResponse, IRUsage  # noqa: E402


def test_registry():
    paths = registry.paths()
    assert "/v1/chat/completions" in paths
    assert "/v1/responses" in paths
    assert "/v1/messages" in paths
    print("[OK] registry:", paths)


def test_openai_chat_roundtrip():
    req = {
        "model": "gpt-4o", "stream": False, "max_tokens": 256, "temperature": 0.5,
        "messages": [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "北京天气？"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "get_weather", "arguments": '{"city":"北京"}'}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "晴 25度"},
        ],
        "tools": [{"type": "function", "function": {
            "name": "get_weather", "description": "查天气",
            "parameters": {"type": "object"}}}],
    }
    ir = openai_chat.OpenAIChatAdapter.to_ir(req)
    assert [b.text for b in ir.system] == ["你是助手"]
    assert len(ir.messages) == 3  # user + assistant(tool_use) + tool result
    assert ir.messages[-1].content[0].type == "tool_result"
    assert ir.messages[-2].content[0].type == "tool_use"
    assert ir.tools[0].name == "get_weather"

    usage = IRUsage(input_tokens=100, output_tokens=50, cache_read_input_tokens=40)
    ir_resp = IRResponse(
        id="c1", model="gpt-4o",
        content=[ContentBlock.text_block("北京今天晴"),
                 ContentBlock.tool_use_block("call_x", "get_weather", {"city": "上海"})],
        stop_reason="tool_use", usage=usage,
    )
    out = openai_chat.OpenAIChatAdapter.from_ir(ir_resp)
    assert out["choices"][0]["finish_reason"] == "tool_calls"
    assert len(out["choices"][0]["message"]["tool_calls"]) == 1
    assert out["usage"]["prompt_tokens_details"]["cached_tokens"] == 40
    print("[OK] OpenAI Chat roundtrip")


def test_openai_response_roundtrip():
    req = {
        "model": "gpt-5", "instructions": "助手", "previous_response_id": "pr_9",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "你好"}]}],
        "tools": [{"type": "function", "name": "f", "description": "d",
                   "parameters": {"type": "object"}}],
    }
    ir = openai_response.OpenAIResponseAdapter.to_ir(req)
    assert [b.text for b in ir.system] == ["助手"]
    assert ir.extra["previous_response_id"] == "pr_9"
    assert ir.tools[0].name == "f"

    ir_resp = IRResponse(
        id="r1", model="gpt-5",
        content=[ContentBlock.text_block("收到")],
        previous_response_id="pr_9",
        usage=IRUsage(input_tokens=90, cache_read_input_tokens=70),
    )
    out = openai_response.OpenAIResponseAdapter.from_ir(ir_resp)
    assert out["output"][0]["text"] == "收到"
    assert out["previous_response_id"] == "pr_9"
    assert out["usage"]["input_tokens_details"]["cached_tokens"] == 70
    print("[OK] OpenAI Response roundtrip")


def test_anthropic_roundtrip():
    req = {
        "model": "claude-opus-5", "system": "你是助手", "max_tokens": 512, "stream": False,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        "tools": [{"name": "get_weather", "description": "查天气",
                   "input_schema": {"type": "object"}}],
    }
    ir = anthropic.AnthropicAdapter.to_ir(req)
    assert [b.text for b in ir.system] == ["你是助手"]
    assert ir.messages[0].content[0].type == "text"
    assert ir.tools[0].name == "get_weather"

    usage = IRUsage(input_tokens=200, output_tokens=30,
                    cache_creation_input_tokens=1500, cache_read_input_tokens=0)
    ir_resp = IRResponse(
        id="m1", model="claude-opus-5",
        content=[ContentBlock.text_block("你好"),
                 ContentBlock.tool_use_block("tu_1", "get_weather", {"city": "天津"})],
        stop_reason="tool_use", usage=usage,
    )
    out = anthropic.AnthropicAdapter.from_ir(ir_resp)
    assert out["content"][1]["type"] == "tool_use"
    assert out["stop_reason"] == "tool_use"
    assert out["usage"]["cache_creation_input_tokens"] == 1500
    print("[OK] Anthropic roundtrip")


if __name__ == "__main__":
    test_registry()
    test_openai_chat_roundtrip()
    test_openai_response_roundtrip()
    test_anthropic_roundtrip()
    print("\nALL ADAPTER TESTS PASSED")
