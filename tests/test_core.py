"""补充测试（tests/test_core.py）：状态层 / 预热拒绝 / usage 对账 / 断点布局 / 记忆注入 / SSE / 实验预期。

运行：python tests/test_core.py   或   pytest tests/test_core.py
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.adapters.anthropic import AnthropicAdapter
from src.gateway.config import GatewayConfig
from src.gateway.server import ProtocolGateway
from src.experiments.run_experiments import session_boundary_snapshot
from src.ir.schema import ContentBlock, IRMessage, IRRequest
from src.observability.metrics import usage_from_anthropic, usage_from_openai
from src.state.store import SessionStore


def test_state_store():
    """状态层：创建 / prev_id / task-id 单键兜底 / TTL 淘汰。"""
    tmp = tempfile.mkdtemp()
    s = SessionStore(Path(tmp) / "sessions.db", default_ttl_seconds=1, on_end="drop")
    sess = s.create_session(team_id="t1", agent_id="a1", task_id="task1")
    assert s.get_session(sess.session_id) is not None

    s.set_previous_response_id(sess.session_id, "resp_1")
    assert s.get_session(sess.session_id).previous_response_id == "resp_1"
    assert sess.session_id in s.query_by_prev_id("resp_1")
    assert s.find_active_by_task("task1").session_id == sess.session_id

    # 立即过期的会话应被 TTL 淘汰（on_end=drop 默认删除）
    s2 = s.create_session(task_id="task_expired", ttl_seconds=-1)
    assert s.evict_expired() >= 1
    assert s.get_session(s2.session_id) is None
    print("[OK] state/store 状态层（prev_id / task-id 兜底 / TTL）")


def test_warmup_validation():
    """预热请求五条拒绝条件（方案 3.7，v3 已核实）。"""
    v = ProtocolGateway._validate_warmup
    # 五条都必须拒绝
    assert v({"max_tokens": 0, "stream": True}, "anthropic") is not None
    assert v({"max_tokens": 0, "thinking": {"type": "enabled"}}, "anthropic") is not None
    assert v({"max_tokens": 0, "response_format": {"type": "json_object"}}, "openai_chat") is not None
    assert v({"max_tokens": 0, "text": {"format": {"type": "json_schema"}}}, "openai_response") is not None
    assert v({"max_tokens": 0, "output_config": {"format": {"type": "json_schema"}}}, "openai_response") is not None
    assert v({"max_tokens": 0, "tool_choice": {"type": "tool"}}, "anthropic") is not None
    assert v({"max_tokens": 0, "tool_choice": {"type": "any"}}, "anthropic") is not None
    # 合法预热应放行
    assert v({"max_tokens": 0}, "anthropic") is None
    print("[OK] 预热请求拒绝条件（stream / thinking / structured outputs / tool_choice）")


def test_usage_parsing():
    """usage 对账口径（方案 3.5 点名的坑）。"""
    # OpenAI Chat：prompt_tokens 已含缓存，input_tokens = prompt - cached
    u = usage_from_openai({"usage": {
        "prompt_tokens": 1000, "completion_tokens": 100,
        "prompt_tokens_details": {"cached_tokens": 300},
    }})
    assert u.input_tokens == 700
    assert u.cache_read_input_tokens == 300
    assert u.total_input() == 1000  # 对账一致，不重复计

    # Anthropic：input_tokens 不含缓存，total = input + creation + read
    a = usage_from_anthropic({"usage": {
        "input_tokens": 500, "output_tokens": 50,
        "cache_creation_input_tokens": 200, "cache_read_input_tokens": 100,
    }})
    assert a.input_tokens == 500
    assert a.total_input() == 800
    assert a.cache_hit and a.cache_created and not a.cache_miss
    print("[OK] usage 对账（OpenAI prompt-cached / Anthropic 三字段）")


def test_breakpoint_layout():
    """Anthropic cache_control 断点布局（3 固定 + 1 滚动；预热不打 messages 断点）。"""
    req = IRRequest(
        model="claude-opus-5",
        system=[ContentBlock.text_block("sys")],
        messages=[
            IRMessage.text("user", "q1"),
            IRMessage.text("assistant", "a1"),
            IRMessage.text("user", "q2"),
        ],
    )
    payload = AnthropicAdapter.ir_to_payload(req)
    sys_bp = sum(1 for b in payload["system"] if b.get("cache_control"))
    msg_bp = sum(1 for m in payload["messages"] for b in m["content"] if b.get("cache_control"))
    assert sys_bp == 1, sys_bp
    assert msg_bp == 2, msg_bp  # 断点3（历史末）+ 断点4（滚动尾）

    # 预热：只打 system 断点，不打占位消息（方案 3.7 断点位置陷阱）
    payload_w = AnthropicAdapter.ir_to_payload(req, warmup=True)
    assert all(
        not b.get("cache_control")
        for m in payload_w["messages"] for b in m["content"]
    )
    print("[OK] cache_control 断点布局（含预热语义）")


def test_memory_injection():
    """网关记忆注入 + 文本幂等去重。"""
    tmp = tempfile.mkdtemp()
    gw = ProtocolGateway(GatewayConfig(mock=True, data_dir=tmp))
    sid = "sess_mem"
    gw.sessions.create_session(session_id=sid)
    gw.sessions.update_meta(sid, {"memories": [
        {"id": "m1", "content": "记忆A"},
        {"id": "m2", "content": "记忆B"},
    ]})
    sess = gw.sessions.get_session(sid)

    ir = IRRequest(model="m", system=[], messages=[])
    assert gw._inject_memories(ir, sess) == 2
    assert gw._inject_memories(ir, sess) == 0  # 幂等：再注入 0 新增

    # 外部已注入相同文本 → 只补注入缺失项
    ir2 = IRRequest(model="m", system=[ContentBlock.text_block("记忆A")], messages=[])
    assert gw._inject_memories(ir2, sess) == 1
    print("[OK] 记忆注入幂等去重")


def test_replay_into_prefix():
    """重放起点→缓存前缀：stateful 请求按 prev_id 重建完整前缀。"""
    tmp = tempfile.mkdtemp()
    gw = ProtocolGateway(GatewayConfig(mock=True, data_dir=tmp))
    sid = "sess_replay"
    gw.sessions.create_session(session_id=sid, task_id="t1")

    # 模拟历史轮次：已存一条带完整消息的请求
    hist_ir = IRRequest(
        model="m",
        system=[ContentBlock.text_block("系统设定")],
        messages=[
            IRMessage.text("user", "第一问"),
            IRMessage.text("assistant", "第一答"),
        ],
    )
    gw.sessions.append_turn(
        sid, {"path": "/v1/messages", "request": hist_ir.to_dict()},
        response_id="resp_hist",
    )

    # 新 stateful 请求：只带新轮 + previous_response_id
    new_ir = IRRequest(model="m", messages=[IRMessage.text("user", "第二问")])
    added = gw._apply_replay(new_ir, sid, {"previous_response_id": "resp_hist"})

    assert added == 2, added
    assert len(new_ir.messages) == 3, [m.plain_text() for m in new_ir.messages]
    assert new_ir.messages[0].plain_text() == "第一问"
    assert new_ir.messages[-1].plain_text() == "第二问"
    print("[OK] 重放→缓存前缀（history 2 + 新 1 = 3）")


def test_replay_noop_without_prev_id():
    """无 prev_id 的 stateless 请求不触发重放（避免历史翻倍）。"""
    tmp = tempfile.mkdtemp()
    gw = ProtocolGateway(GatewayConfig(mock=True, data_dir=tmp))
    sid = "sess_noreplay"
    gw.sessions.create_session(session_id=sid)
    hist_ir = IRRequest(model="m", messages=[IRMessage.text("user", "历史")])
    gw.sessions.append_turn(sid, {"path": "/x", "request": hist_ir.to_dict()})
    new_ir = IRRequest(model="m", messages=[IRMessage.text("user", "新")])
    assert gw._apply_replay(new_ir, sid, {}) == 0
    assert len(new_ir.messages) == 1
    print("[OK] stateless 不重放")


def test_sse_stream():
    """网关 SSE 流式透传（mock 上游）。"""
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        tmp = tempfile.mkdtemp()
        gw = ProtocolGateway(GatewayConfig(mock=True, data_dir=tmp))
        client = TestClient(TestServer(gw.build_app()))
        await client.start_server()
        try:
            resp = await client.post("/v1/chat/completions", json={
                "model": "demo-model", "stream": True, "max_tokens": 64,
                "messages": [{"role": "user", "content": "hi"}],
            })
            assert resp.headers["Content-Type"].startswith("text/event-stream")
            body = await resp.text()
            assert "data: [DONE]" in body
            assert body.count("data:") >= 3  # 至少 2 个 chunk + [DONE]
        finally:
            await client.close()

    asyncio.run(run())
    print("[OK] SSE 流式透传")


def test_experiment_expectations():
    """实验组预期断言化：11 组命中率高低必须与 spec.expect_high 一致。"""
    from src.experiments.run_experiments import GROUP_SPECS, ExperimentRunner

    async def run():
        tmp = tempfile.mkdtemp()
        runner = ExperimentRunner(rounds=3, data_dir=tmp, min_cache_tokens=512)
        for spec in GROUP_SPECS:
            r = await runner.run_group(spec)
            hit = r["hit_rate"] > 0.5
            assert hit == spec.expect_high, (
                f"[{spec.name}] 预期 {'高' if spec.expect_high else '低'} 命中，"
                f"实际 {r['hit_rate']:.0%}"
            )

    asyncio.run(run())
    print("[OK] 实验预期断言（11 组全部符合）")


def test_session_boundary_config_defaults():
    """方案 3.9.1：Session 边界做成可切换配置，默认假设值对齐 v3(1) 第 13–14 页。"""
    cfg = GatewayConfig()
    assert cfg.session_key_granularity == "single_task"   # ① 标识粒度 = task-id 单键
    assert cfg.session_replay_from == "full"             # ③ 重放起点 = 全量重放
    assert cfg.session_end_policy == "ttl"               # ② 起止 = 超时淘汰
    assert cfg.session_on_end == "archive"               # ② 结束后归档可回放
    assert cfg.session_ttl_seconds == 1800
    snap = session_boundary_snapshot()
    assert "premise" in snap and "task-id 单键" in snap["premise"]
    print("[OK] Session 边界配置默认假设值（single_task / full / ttl / archive）")


def test_replay_history_modes():
    """重放起点三模式：full / sliding_window / last_breakpoint。"""
    tmp = tempfile.mkdtemp()
    s = SessionStore(Path(tmp) / "s.db", default_ttl_seconds=9999, on_end="drop")
    sid = s.create_session(session_id="r1").session_id
    for i in range(5):
        s.append_turn(sid, {"path": "/x", "request": {"messages": [{"role": "user", "content": f"m{i}"}]}})
    full = s.get_replay_history(sid, mode="full")
    window = s.get_replay_history(sid, mode="sliding_window", max_turns=2)
    bp = s.get_replay_history(sid, mode="last_breakpoint")  # 无游标等同全量
    assert len(full) == 5
    assert len(window) == 2
    assert len(bp) == 5
    # last_breakpoint 带游标后只返回游标之后
    s.set_replay_cursor(sid, 3)
    assert len(s.get_replay_history(sid, mode="last_breakpoint")) == 2
    print("[OK] 重放三模式（full / sliding_window / last_breakpoint）")


def test_evict_archive_keeps_session():
    """on_end=archive 时过期会话保留可回放（不删除）。"""
    tmp = tempfile.mkdtemp()
    s = SessionStore(Path(tmp) / "a.db", default_ttl_seconds=1, on_end="archive")
    sid = s.create_session(task_id="keep", ttl_seconds=-1).session_id
    assert s.evict_expired() == 0            # archive：不删，返回 0
    assert s.get_session(sid) is not None    # 仍可回放
    print("[OK] on_end=archive 保留过期会话")


if __name__ == "__main__":
    test_state_store()
    test_warmup_validation()
    test_usage_parsing()
    test_breakpoint_layout()
    test_memory_injection()
    test_replay_into_prefix()
    test_replay_noop_without_prev_id()
    test_sse_stream()
    test_experiment_expectations()
    test_session_boundary_config_defaults()
    test_replay_history_modes()
    test_evict_archive_keeps_session()
    print("\nALL CORE TESTS PASSED")
