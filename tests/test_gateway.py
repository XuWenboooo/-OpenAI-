"""M4 · 网关端到端测试（mock 上游，离线）。

运行：python tests/test_gateway.py
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.gateway.config import GatewayConfig
from src.gateway.server import ProtocolGateway


def _chat_payload(text: str = "你好，介绍一下你自己"):
    return {
        # 用不在阈值表的模型名，走 mock 配置阈值（8 token），便于离线演示缓存命中；
        # 真实模型（claude-opus-5 等）会按 MODEL_CACHE_THRESHOLDS 查表（实验语义）。
        "model": "demo-model",
        "max_tokens": 64,
        "messages": [
            {"role": "system", "content": "你是协议转换演示助手"},
            {"role": "user", "content": text},
        ],
    }


async def test_openai_chat_flow():
    tmp = tempfile.mkdtemp()
    cfg = GatewayConfig(mock=True, data_dir=tmp, max_concurrency=2)
    gw = ProtocolGateway(cfg)
    client = TestClient(TestServer(gw.build_app()))
    await client.start_server()
    try:
        # 第一次：创建缓存前缀（creation）
        resp = await client.post("/v1/chat/completions", json=_chat_payload())
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["object"] == "chat.completion"
        assert body["choices"][0]["message"]["content"]
        assert body["usage"]["prompt_tokens"] > 0
        print("[OK] OpenAI Chat 第一次调用（返回 OpenAI 格式）")

        # 第二次：相同前缀应命中缓存（read）
        resp2 = await client.post("/v1/chat/completions", json=_chat_payload())
        body2 = await resp2.json()
        cached = body2["usage"]["prompt_tokens_details"]["cached_tokens"]
        assert cached > 0, f"第二次应命中缓存, cached={cached}"
        print(f"[OK] OpenAI Chat 第二次调用命中缓存 cached_tokens={cached}")

        s = gw.metrics.summary()
        assert s["requests"] >= 2
        assert s["hit_requests"] >= 1
        print(f"[OK] 埋点汇总 requests={s['requests']} hit_rate={s['hit_rate']}")
    finally:
        await client.close()


async def test_anthropic_flow():
    tmp = tempfile.mkdtemp()
    cfg = GatewayConfig(mock=True, data_dir=tmp)
    gw = ProtocolGateway(cfg)
    client = TestClient(TestServer(gw.build_app()))
    await client.start_server()
    try:
        payload = {
            "model": "claude-opus-5",
            "system": "你是演示助手",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        }
        resp = await client.post("/v1/messages", json=payload)
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["type"] == "message"
        assert body["content"][0]["type"] == "text"
        assert "cache_creation_input_tokens" in body["usage"]
        print("[OK] Anthropic Messages 调用（返回 Anthropic 格式）")
    finally:
        await client.close()


async def test_openai_response_flow():
    tmp = tempfile.mkdtemp()
    cfg = GatewayConfig(mock=True, data_dir=tmp)
    gw = ProtocolGateway(cfg)
    client = TestClient(TestServer(gw.build_app()))
    await client.start_server()
    try:
        payload = {
            "model": "gpt-5",
            "instructions": "你是助手",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "你好"}]}],
        }
        resp = await client.post("/v1/responses", json=payload)
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["object"] == "response"
        assert body["output"][0]["type"] == "output_text"
        assert body["previous_response_id"]  # 状态层注入
        print(f"[OK] OpenAI Response 调用 prev_id={body['previous_response_id'][:8]}...")
    finally:
        await client.close()


async def main():
    await test_openai_chat_flow()
    await test_anthropic_flow()
    await test_openai_response_flow()
    print("\nALL GATEWAY TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
