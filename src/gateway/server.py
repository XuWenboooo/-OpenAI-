"""M4 · 协议转换网关/代理（独立进程，aiohttp 单进程异步）

职责：
  * 暴露三个外部端点（OpenAI Chat / OpenAI Response / Anthropic Messages）；
  * 每个请求：外部协议 → IR → 上游协议 → 解析 usage 埋点 → 转回外部协议；
  * 状态层注入：会话归属（team/agent/task）+ previous_response_id；
  * 限流：asyncio.Semaphore（默认 2，硬件友好）；
  * 鉴权从简：检查 key 头存在（mock 模式跳过）。

启动：python -m src.gateway.server --mock --port 8096
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from typing import Any, Optional

from aiohttp import web

from ..adapters.base import AdapterRegistry, registry
from ..ir.schema import ContentBlock, IRResponse, IRUsage, message_from_json
from ..observability.metrics import MetricRecord, MetricsStore
from ..state.store import SessionStore
from .config import GatewayConfig
from .upstream import make_upstream


class ProtocolGateway:
    def __init__(self, config: Optional[GatewayConfig] = None):
        self.config = config or GatewayConfig()
        self.registry: AdapterRegistry = registry
        self.metrics = MetricsStore(self.config.metrics_db)
        self.sessions = SessionStore(
            self.config.sessions_db,
            default_ttl_seconds=self.config.session_ttl_seconds,
            end_policy=self.config.session_end_policy,
            on_end=self.config.session_on_end,
        )
        self.semaphore = asyncio.Semaphore(self.config.max_concurrency)
        # 上游按外部 adapter 协议惰性创建（每个协议一个实例）
        self._upstreams: dict[str, Any] = {}

    def upstream_for(self, protocol: str) -> Any:
        up = self.config.resolve_upstream(protocol)
        if up not in self._upstreams:
            self._upstreams[up] = make_upstream(up, self.config)
        return self._upstreams[up]

    # ---- 鉴权（从简） ----
    def _check_auth(self, request: web.Request, adapter_protocol: str) -> bool:
        if self.config.mock:
            return True
        key = request.headers.get("x-api-key") or request.headers.get("Authorization")
        if not key:
            return False
        if adapter_protocol == "anthropic":
            return bool(self.config.anthropic_api_key)
        return bool(self.config.openai_api_key)

    # ---- 会话状态 ----
    def _resolve_session(self, request: web.Request, payload: dict) -> Any:
        """从请求头解析会话归属；OpenAI Response 用 previous_response_id 关联。

        key_granularity=three_level 时（方案 3.9.1 ①），按配置的 key_fields 从
        x-<field> 头组合出会话键；single_task 时沿用既有 task-id 单键逻辑。
        """
        team = request.headers.get("x-team-id")
        agent = request.headers.get("x-agent-id")
        task = request.headers.get("x-task-id")
        session_id = request.headers.get("x-session-id")

        if self.config.session_key_granularity == "three_level":
            return self._derive_three_level_key(request)

        prev_id = payload.get("previous_response_id") or payload.get("extra", {}).get(
            "previous_response_id"
        )
        if not session_id:
            # 通过 previous_response_id 反查会话（Response 有状态设计）
            for s in self.sessions.query_by_prev_id(prev_id) if prev_id else []:
                session_id = s
                break
        if not session_id:
            s = self.sessions.create_session(team_id=team, agent_id=agent, task_id=task)
            session_id = s.session_id
        return session_id

    def _derive_three_level_key(self, request: web.Request) -> str:
        """three_level 标识粒度：按 session_key_fields 组合 x-<field> 头生成会话键。"""
        parts = []
        for field_name in self.config.session_key_fields:
            val = request.headers.get(f"x-{field_name}") or request.headers.get(field_name)
            parts.append(val or "_")
        return "sess_" + "__".join(parts)

    # ---- 单请求处理 ----
    async def handle(self, request: web.Request) -> web.Response:
        adapter_cls = self.registry.get(request.path)
        if adapter_cls is None:
            return web.json_response(
                {"error": {"message": f"unsupported path {request.path}"}},
                status=404,
            )
        adapter = adapter_cls
        if not self._check_auth(request, adapter.protocol):
            return web.json_response(
                {"error": {"message": "missing api key"}}, status=401
            )

        raw_body = await request.read()
        if len(raw_body) > self.config.max_body_bytes:
            return web.json_response(
                {"error": {"message": "body too large"}}, status=413
            )
        try:
            payload = json.loads(raw_body or b"{}")
        except json.JSONDecodeError:
            return web.json_response(
                {"error": {"message": "invalid json"}}, status=400
            )

        session_id = self._resolve_session(request, payload)

        # 预热请求识别（方案 3.7：max_tokens:0 走单独语义，响应畸形但不得当错误）
        warmup = self._is_warmup(payload)
        if warmup:
            werr = self._validate_warmup(payload, adapter.protocol)
            if werr:
                return web.json_response(
                    {"error": {"message": werr, "type": "invalid_request_error"}},
                    status=400,
                )

        # 限流（硬件友好：并发 2，避免占满 + token 成本失控）
        async with self.semaphore:
            try:
                ir_req = adapter.to_ir(payload)
                session = self.sessions.get_session(session_id)

                # previous_response_id 注入到 IR extra
                if session and session.previous_response_id and not ir_req.extra.get("previous_response_id"):
                    ir_req.extra["previous_response_id"] = session.previous_response_id

                # 重放起点→缓存前缀（方案 3.9 耦合点 1 / TRACK 04）：stateful 请求按
                # previous_response_id 反查，从存储的历史轮次重建完整前缀；task-id 单键
                # 兜底已保证可独立运行，此步仅对带 prev_id 的请求生效（避免 stateless 历史翻倍）。
                self._apply_replay(ir_req, session_id, payload)

                # 记忆注入（方案 3.9 / TRACK01）：session meta → IR system 尾部，文本幂等去重
                self._inject_memories(ir_req, session)

                upstream = self.upstream_for(adapter.protocol)

                # 流式透传分支（方案 M4 / README「SSE 透传」）
                if ir_req.stream:
                    return await self._handle_stream(
                        request, adapter, ir_req, session_id, warmup
                    )

                ir_resp = await upstream.complete(ir_req)
                self._finish_turn(
                    request, adapter, ir_req, session_id, ir_resp, warmup,
                    group=payload.get("x-experiment-group") or "",
                )
                ir_resp.previous_response_id = (
                    self.sessions.get_session(session_id).previous_response_id
                )
                out = adapter.from_ir(ir_resp)
                return web.json_response(out)
            except Exception as exc:  # noqa: BLE001 - 统一转 502
                return web.json_response(
                    {"error": {"message": f"upstream error: {exc}"}}, status=502
                )

    @staticmethod
    def _is_warmup(payload: dict) -> bool:
        """预热请求：max_tokens/max_output_tokens == 0（方案 3.7，官方支持用法）。"""
        mt = payload.get("max_tokens", payload.get("max_output_tokens", 1))
        try:
            return int(mt) == 0
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _validate_warmup(payload: dict, protocol: str) -> Optional[str]:
        """预热请求的拒绝条件（v3 已核实官方行为）：

        max_tokens:0 不能同时带 stream / extended thinking / structured outputs /
        tool_choice={type:tool|any}，否则上游返回 invalid_request_error。
        """
        if payload.get("stream"):
            return "warmup(max_tokens:0) 不能同时带 stream:true"
        if protocol == "anthropic":
            thinking = payload.get("thinking")
            if isinstance(thinking, dict) and thinking.get("type") == "enabled":
                return "warmup(max_tokens:0) 不能同时带 extended thinking"
        else:
            # OpenAI 系：structured outputs（Chat 用 response_format；Responses 用 output_config.format / text.format）
            if (
                payload.get("response_format")
                or (payload.get("output_config") or {}).get("format")
                or (payload.get("text") or {}).get("format")
            ):
                return "warmup(max_tokens:0) 不能同时带 structured outputs"
        tc = payload.get("tool_choice")
        if isinstance(tc, dict) and tc.get("type") in ("tool", "any"):
            return "warmup(max_tokens:0) 不能同时带 tool_choice={type:tool/any}"
        return None

    @staticmethod
    def _estimate_injected_tokens(request: web.Request, ir_req: Any) -> int:
        """② 注入 token 数：优先取 x-injected-tokens 头，否则按 system 记忆块字符数估算。

        ⚠️ 口径：这里是「字符数」估算，非真实 token 计数（中文 1 字 ≈ 1 token 的示意口径）。
        真实口径需接 count_tokens（每轮多一次 RTT，方案 3.5 已点名成本评估）。
        """
        h = request.headers.get("x-injected-tokens")
        if h and h.isdigit():
            return int(h)
        n = 0
        for b in ir_req.system:
            if getattr(b, "type", "") == "text" and b.text:
                n += len(b.text)
        return n

    def _estimate_replay_tokens(self, session_id: str) -> int:
        """③ 重放 token 数：本次请求被无状态重放的历史轮次 token 估算。

        ⚠️ 口径：按「轮数 × 120」的示意值占位，非真实重放 token 计数。
        """
        try:
            turns = self.sessions.list_turns(session_id)
            # 最后一条是当前轮，前面都是重放历史
            return max(0, len(turns) - 1) * 120
        except Exception:  # noqa: BLE001
            return 0

    def _apply_replay(self, ir_req: Any, session_id: str, payload: dict[str, Any]) -> int:
        """重放起点→缓存前缀（TRACK 04 耦合点 1）。

        仅对 stateful 请求（携带 previous_response_id）生效：按 prev_id 反查会话后，
        从最近一次历史轮次存储的 IR 重建完整消息前缀，与当前新轮拼接。
        这样缓存断点（断点3 = 历史静态段末 messages[-2]）才有意义的落点，且重复前缀稳定可命中。

        stateless 客户端（OpenAI Chat）自行重放完整历史、不传 prev_id，网关不重复拼接。
        """
        prev_id = payload.get("previous_response_id") or (
            payload.get("extra", {}) or {}
        ).get("previous_response_id")
        if not prev_id:
            return 0
        turns = self.sessions.get_replay_history(
            session_id,
            mode=self.config.session_replay_from,
            max_turns=self.config.session_sliding_window_n,
        )
        if not turns:
            return 0
        last = turns[-1]
        try:
            stored = json.loads(last.get("request_json") or "{}").get("request", {})
            hist = stored.get("messages", [])
        except (json.JSONDecodeError, ValueError):
            return 0
        if not hist:
            return 0
        history_msgs = [message_from_json(m) for m in hist]
        ir_req.messages = history_msgs + ir_req.messages
        return len(history_msgs)

    def _inject_memories(self, ir_req: Any, session: Any) -> int:
        """记忆注入（方案 3.9 / TRACK01）：把 session meta 的记忆块注入 IR system 尾部。

        幂等去重：按文本去重——外部请求（如 MemoryProxy）若已注入相同文本则跳过，
        避免同一段记忆重复注入、重复计费（方案 3.9 耦合点 2）。
        L3 无条件注入语义：每轮注入相同稳定内容 → 前缀稳定 → 应命中缓存。
        """
        memories = (session.meta or {}).get("memories") if session else []
        if not memories:
            return 0
        seen = {b.text for b in ir_req.system
                if getattr(b, "type", "") == "text" and b.text}
        added = 0
        for m in memories:
            if not isinstance(m, dict):
                continue
            text = (m.get("content") or m.get("preview") or "").strip()
            if text and text not in seen:
                ir_req.system.append(ContentBlock.text_block(text))
                seen.add(text)
                added += 1
        return added

    def _finish_turn(
        self,
        request: web.Request,
        adapter: Any,
        ir_req: Any,
        session_id: str,
        ir_resp: IRResponse,
        warmup: bool,
        group: str = "",
    ) -> None:
        """状态层 previous_response_id 更新 + 北极星埋点（流式/非流式共用）。"""
        if ir_resp.id:
            self.sessions.set_previous_response_id(session_id, ir_resp.id)
            self.sessions.append_turn(
                session_id,
                {"path": request.path, "request": ir_req.to_dict()},
                response_id=ir_resp.id,
            )
        else:
            self.sessions.append_turn(
                session_id, {"path": request.path, "request": ir_req.to_dict()}
            )
        dropped, degradation = (
            ir_req.extra.get("_dropped_params", []),
            ir_req.extra.get("_degradation", "静默通过"),
        )
        injected_tokens = self._estimate_injected_tokens(request, ir_req)
        replay_tokens = self._estimate_replay_tokens(session_id)
        tags: dict[str, Any] = {
            "upstream": self.config.resolve_upstream(adapter.protocol),
            "mock": self.config.mock,
            "warmup": warmup,
        }
        self.metrics.record(
            MetricRecord.from_usage(
                ir_resp.usage,
                session_id=session_id,
                group=group,
                protocol=adapter.protocol,
                model=ir_req.model,
                injected_tokens=injected_tokens,
                replay_tokens=replay_tokens,
                dropped_params=dropped,
                degradation_path=degradation,
                tags=tags,
            )
        )

    async def _handle_stream(
        self,
        request: web.Request,
        adapter: Any,
        ir_req: Any,
        session_id: str,
        warmup: bool,
    ) -> web.StreamResponse:
        """SSE 流式透传：上游逐块 → 协议 chunk → SSE data 事件。"""
        upstream = self.upstream_for(adapter.protocol)
        resp = web.StreamResponse()
        resp.headers["Content-Type"] = "text/event-stream"
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["Connection"] = "keep-alive"
        await resp.prepare(request)

        idx = 0
        acc = IRUsage()
        try:
            async for delta in upstream.stream(ir_req):
                idx += 1
                # usage 散落在不同事件（input 侧 / output 侧），用 max 合并避免重复
                if delta.usage:
                    acc = IRUsage(
                        input_tokens=max(acc.input_tokens, delta.usage.input_tokens),
                        output_tokens=max(acc.output_tokens, delta.usage.output_tokens),
                        cache_creation_input_tokens=max(
                            acc.cache_creation_input_tokens,
                            delta.usage.cache_creation_input_tokens,
                        ),
                        cache_read_input_tokens=max(
                            acc.cache_read_input_tokens,
                            delta.usage.cache_read_input_tokens,
                        ),
                    )
                chunk = adapter.stream_chunk(delta, idx)
                if chunk:
                    await resp.write(
                        f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
                    )
            await resp.write(b"data: [DONE]\n\n")
        except Exception as exc:  # noqa: BLE001
            await resp.write(
                f"data: {json.dumps({'error': {'message': f'upstream error: {exc}'}})}\n\n".encode()
            )
        await resp.write_eof()

        # 状态层 + 埋点（用合并后的 usage）
        ir_resp = IRResponse(
            id=f"stream_{uuid.uuid4().hex[:8]}", model=ir_req.model, usage=acc
        )
        self._finish_turn(request, adapter, ir_req, session_id, ir_resp, warmup)
        return resp

    # ---- aiohttp app ----
    def build_app(self) -> web.Application:
        app = web.Application(client_max_size=self.config.max_body_bytes)
        app.router.add_post("/v1/chat/completions", self.handle)
        app.router.add_post("/v1/responses", self.handle)
        app.router.add_post("/v1/messages", self.handle)
        app.router.add_get("/health", self.health)
        app.router.add_get("/", self.index)
        return app

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"status": "ok", "mock": self.config.mock, "paths": self.registry.paths()}
        )

    async def index(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "service": "protocol-gateway (M4)",
                "endpoints": self.registry.paths(),
                "usage": "POST 原始协议请求到对应端点，返回同协议格式（内部经 IR 转换）",
            }
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="协议转换网关 (M4)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8096)
    parser.add_argument("--mock", action="store_true", help="使用内置 mock 上游（离线）")
    parser.add_argument("--upstream", default="anthropic", help="anthropic|openai|auto")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args()

    config = GatewayConfig(
        host=args.host,
        port=args.port,
        mock=args.mock,
        upstream_protocol=args.upstream,
        max_concurrency=args.concurrency,
        data_dir=args.data_dir,
    )
    gw = ProtocolGateway(config)
    web.run_app(gw.build_app(), host=config.host, port=config.port)


if __name__ == "__main__":
    main()
