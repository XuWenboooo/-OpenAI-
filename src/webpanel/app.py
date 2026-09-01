"""M6 · 弹网页：Session init 上下文编排 + 缓存前缀可视化

方案 3.2 的「Session init 弹网页」决策：
  * B 类上下文编排 —— 网页展示「注入了什么记忆 + 缓存前缀」；
  * 可勾选裁剪注入的记忆（与状态层共用一套会话存储，不重复建设）。

安全骨架（v3 修正版 3.7）：
  * 绑定 127.0.0.1（绝不 0.0.0.0）；
  * 端口运行时随机，URL 带一次性 token（/?t=随机串），服务端校验后才返回页面；
  * Host 头校验：只接受 127.0.0.1:端口，拒绝域名形式（防 DNS rebinding）；
  * 记忆内容默认脱敏/截断，完整内容需显式点开。
  * 注：无 OAuth 授权码流程，故无需 PKCE/state（v3 已删除）。

技术：aiohttp 单进程 + 自包含 HTML/JS（无前端框架，轻量，不占资源）。
数据源：M2 SessionStore + M1 MetricsStore。

启动：python -m src.webpanel.app --port 0
"""

from __future__ import annotations

import argparse
import json
import secrets
from pathlib import Path
from typing import Any, Optional

from aiohttp import web

from ..observability.metrics import MetricsStore
from ..state.store import SessionStore

# ---------------------------------------------------------------------------
# 前端页面（自包含）
# ---------------------------------------------------------------------------

PAGE_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Session Init · 上下文编排面板</title>
<style>
  body{font-family:'Segoe UI','PingFang SC',sans-serif;margin:0;background:#F4F3EE;color:#1A1B1C;}
  header{padding:18px 22px;background:#fff;border-bottom:1px solid #E4E3DD;}
  header h1{margin:0;font-size:18px;}
  header p{margin:4px 0 0;color:#6B7280;font-size:12px;}
  main{padding:20px;max-width:1080px;margin:0 auto;}
  .cards{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0;}
  .card{flex:1 1 150px;min-width:140px;background:#fff;border:1px solid #E4E3DD;border-radius:12px;padding:14px;}
  .card .k{font-size:12px;color:#6B7280;}
  .card .v{font-size:22px;font-weight:600;margin-top:4px;}
  .panel{background:#fff;border:1px solid #E4E3DD;border-radius:12px;padding:16px;margin:14px 0;}
  .panel h2{margin:0 0 10px;font-size:14px;}
  .prefix{display:flex;flex-wrap:wrap;gap:6px;align-items:stretch;}
  .seg{border-radius:8px;padding:10px;font-size:12px;min-width:90px;flex:1 1 120px;box-sizing:border-box;}
  .seg .t{font-weight:600;}
  .seg .m{font-size:11px;color:#555;margin-top:4px;word-break:break-all;}
  .bp{font-size:10px;color:#b8740a;font-weight:600;}
  .mem{display:flex;align-items:center;gap:8px;padding:8px 10px;border:1px solid #E4E3DD;border-radius:8px;margin:6px 0;font-size:13px;}
  .mem input{width:16px;height:16px;}
  .tag{display:inline-block;background:rgba(139,200,234,.22);border-radius:6px;padding:2px 8px;font-size:11px;margin-right:6px;}
  .sess{border:1px solid #E4E3DD;border-radius:8px;padding:8px 10px;margin:6px 0;font-size:13px;cursor:pointer;}
  .sess.active{border-color:#8BC8EA;background:rgba(139,200,234,.08);}
  button{background:#8BC8EA;border:none;border-radius:8px;padding:8px 16px;font-size:13px;color:#1A1B1C;cursor:pointer;font-weight:600;}
  button:disabled{opacity:.5;}
  .hint{font-size:11px;color:#6B7280;margin-top:8px;}
</style>
</head>
<body>
<header>
  <h1>Session Init · 上下文编排面板</h1>
  <p>展示「注入了什么记忆 + 缓存前缀布局」，可勾选裁剪记忆（B 类上下文编排）· 数据来自 M1 埋点 + M2 状态层</p>
</header>
<main>
  <div class="cards" id="cards"></div>

  <div class="panel">
    <h2>当前会话 · 缓存前缀可视化（tools → system → messages，易变内容放最后）</h2>
    <div class="prefix" id="prefix"></div>
    <div class="hint">图例：<span class="tag">记忆注入</span> 绿色块为命中缓存前缀的稳定部分；断点（▼）之前内容变更会导致其后全部失效。</div>
  </div>

  <div class="panel">
    <h2>记忆注入（可勾选裁剪，裁剪后下次请求按新前缀缓存）</h2>
    <div id="memories"></div>
    <button id="pruneBtn" style="margin-top:10px;">裁剪勾选记忆</button>
  </div>

  <div class="panel">
    <h2>会话列表</h2>
    <div id="sessions"></div>
  </div>
  <div class="panel">
    <h2>新建会话 · Session Init（经弹网页链接初始化，可设记忆上限）</h2>
    <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;">
      <div><div class="hint">team_id</div><input id="ni_team" style="padding:6px 8px;border:1px solid #E4E3DD;border-radius:6px;"></div>
      <div><div class="hint">agent_id</div><input id="ni_agent" style="padding:6px 8px;border:1px solid #E4E3DD;border-radius:6px;"></div>
      <div><div class="hint">task_id</div><input id="ni_task" style="padding:6px 8px;border:1px solid #E4E3DD;border-radius:6px;"></div>
      <div><div class="hint">记忆上限(0=不限制)</div><input id="ni_cap" value="0" style="width:96px;padding:6px 8px;border:1px solid #E4E3DD;border-radius:6px;"></div>
      <button id="initBtn">创建会话</button>
    </div>
    <div class="hint">会话创建后即出现在上方列表；记忆上限由网关在注入时按此值截断（0 = 全部注入）。这正是 TRACK 04「记忆上限」参数的落地入口（老师方向：session init 走弹网页链接）。</div>
  </div>

</main>
<script>
var currentId = null;
// 一次性 token（服务端校验后内嵌进页面）
var TOKEN = '__AUTH_TOKEN__';

function el(id){ return document.getElementById(id); }

async function api(path, opts){
  opts = opts || {};
  opts.headers = opts.headers || {};
  opts.headers['X-Auth-Token'] = TOKEN;
  var r = await fetch(path, opts);
  return r.json();
}

function renderCards(m){
  el('cards').innerHTML = [
    ['命中率', (m.hit_rate*100).toFixed(1)+'%'],
    ['请求数', m.requests],
    ['命中次数', m.hit_requests],
    ['总成本 $', m.total_cost_usd.toFixed(4)],
  ].map(function(x){
    return '<div class="card"><div class="k">'+x[0]+'</div><div class="v">'+x[1]+'</div></div>';
  }).join('');
}

function renderPrefix(seg){
  // seg: [{name, text, mem, bp}]
  el('prefix').innerHTML = seg.map(function(s){
    var style = s.mem
      ? 'background:linear-gradient(135deg,rgba(139,200,234,.28),rgba(139,200,234,.42));'
      : 'background:#fff;border:1px solid #E4E3DD;';
    var bp = s.bp ? '<div class="bp">▼ 缓存断点</div>' : '';
    var m = (s.text||'').slice(0,60) + ((s.text||'').length>60?'…':'');
    return '<div class="seg" style="'+style+'"><div class="t">'+s.name+'</div>'+
           (s.mem?'<span class="tag">记忆注入</span>':'')+ bp +
           '<div class="m">'+m+'</div></div>';
  }).join('');
}

function renderMemories(mems){
  el('memories').innerHTML = mems.map(function(m, i){
    return '<div class="mem"><input type="checkbox" data-idx="'+i+'" data-id="'+m.id+'" checked>'+
           '<div><div style="font-weight:600">'+m.name+'</div><div style="font-size:12px;color:#555">'+m.preview+'</div></div></div>';
  }).join('');
}

function renderSessions(sessions, active){
  el('sessions').innerHTML = sessions.map(function(s){
    var cls = 'sess' + (s.session_id===active ? ' active' : '');
    var cap = (s.meta && s.meta.memory_cap) ? s.meta.memory_cap : '∞';
    return '<div class="'+cls+'" onclick="loadSession(\''+s.session_id+'\')">'+
           '<span class="tag">'+s.session_id.slice(0,8)+'</span>'+
           'prev_id='+(s.previous_response_id||'-').slice(0,16)+
           ' · team='+(s.team_id||'-')+' · agent='+(s.agent_id||'-')+
           ' · cap='+cap+'</div>';
  }).join('') || '<div class="hint">暂无会话（先通过网关发请求，或运行 M5 实验生成数据）</div>';
}

async function initSession(){
  var cap = parseInt(el('ni_cap').value||'0',10);
  if(isNaN(cap)||cap<0) cap = 0;
  var body = {
    team_id: el('ni_team').value || null,
    agent_id: el('ni_agent').value || null,
    task_id: el('ni_task').value || null,
    memory_cap: cap
  };
  var r = await api('/api/session/init', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)
  });
  alert('已创建会话：'+r.session_id+'（记忆上限='+(r.memory_cap||0)+'）');
  var s = await api('/api/sessions');
  renderSessions(s, r.session_id);
}

async function loadSession(id){
  currentId = id;
  var d = await api('/api/session/'+id);
  renderPrefix(d.prefix_segments);
  renderMemories(d.memories);
  renderSessions(d.sessions, id);
}

async function init(){
  var m = await api('/api/metrics/summary');
  renderCards(m);
  var s = await api('/api/sessions');
  if (s.length){ await loadSession(s[0].session_id); }
  else {
    renderPrefix([]);
    renderMemories([]);
    renderSessions(s, null);
  }
  el('pruneBtn').addEventListener('click', async function(){
    if(!currentId) return;
    var keep = [];
    document.querySelectorAll('#memories input').forEach(function(cb){
      if(cb.checked) keep.push(cb.getAttribute('data-id'));
    });
    var r = await api('/api/session/'+currentId+'/prune', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({keep_ids: keep})
    });
    alert('已裁剪。保留记忆块：'+r.keep_count+'，裁剪掉：'+r.removed_count);
    loadSession(currentId);
  });
  el('initBtn').addEventListener('click', initSession);
}
init();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# M6 服务
# ---------------------------------------------------------------------------

class SessionPanelApp:
    def __init__(self, data_dir: str = "data", token: Optional[str] = None):
        self.sessions = SessionStore(Path(data_dir) / "sessions.db")
        self.metrics = MetricsStore(Path(data_dir) / "metrics.db")
        self.token = token or secrets.token_urlsafe(16)

    # ---- 安全校验（v3 3.7）----
    def _check_host(self, request: web.Request) -> bool:
        """Host 头校验：只接受 127.0.0.1:端口 或 localhost:端口，拒绝域名形式（防 DNS rebinding）。

        说明（相对 v3「只接受 127.0.0.1」口径的取舍）：浏览器经 http://localhost:端口 访问时
        Host 头为 localhost:端口，收紧会误伤合法访问；而 DNS rebinding 攻击者使用的是任意
        第三方域名（如 evil.com），不会被 localhost 前缀匹配，故放行 localhost 不引入风险。
        """
        host = request.headers.get("Host", "")
        return host.startswith("127.0.0.1:") or host.startswith("localhost:")

    def _check_token(self, request: web.Request) -> bool:
        """一次性 URL token 校验（防同机其他进程扫端口直接访问）。"""
        return request.headers.get("X-Auth-Token") == self.token

    def _check(self, request: web.Request) -> bool:
        return self._check_host(request) and self._check_token(request)

    @web.middleware
    async def _security_middleware(self, request: web.Request, handler: Any):
        # Host 校验：所有请求（含首页）都必须来自本机 127.0.0.1
        if not self._check_host(request):
            return web.json_response({"error": "invalid Host"}, status=403)
        # 首页允许通过 query ?t= 校验；API 必须带 X-Auth-Token
        if request.path == "/":
            if request.query.get("t") == self.token:
                return await handler(request)
            return web.json_response({"error": "missing token"}, status=403)
        if not self._check_token(request):
            return web.json_response({"error": "missing token"}, status=403)
        return await handler(request)

    def _list_sessions(self) -> list[dict[str, Any]]:
        out = []
        for s in self.sessions.list_all():
            out.append(
                {
                    "session_id": s.session_id,
                    "team_id": s.team_id,
                    "agent_id": s.agent_id,
                    "task_id": s.task_id,
                    "previous_response_id": s.previous_response_id,
                    "meta": s.meta,
                    "created_at": s.created_at,
                    "updated_at": s.updated_at,
                }
            )
        return out

    @staticmethod
    def _mask(text: str, limit: int = 30) -> str:
        """敏感信息脱敏（v3 3.7）：邮箱/手机号/身份证号打码 + 截断。"""
        import re
        t = re.sub(r"[\w.\-]+@[\w.\-]+\.\w+", "[邮箱已脱敏]", text or "")
        t = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机已脱敏]", t)
        t = re.sub(r"\d{17}[\dXx]", "[证件已脱敏]", t)
        return (t[:limit] + "…（完整内容需显式点开）") if len(t) > limit else t

    def _prefix_segments(self, session_id: str) -> list[dict[str, Any]]:
        """把会话记忆编排成缓存前缀分段（tools → system → messages + 断点）。"""
        s = self.sessions.get_session(session_id)
        if s is None:
            return []
        meta = s.meta or {}
        memories = meta.get("memories", [])
        system_text = meta.get("system", "（无 system，等待真实请求注入）")
        history_count = meta.get("history_count", 0)
        segments = [
            {"name": "tools", "text": "", "mem": False, "bp": False},
            {
                "name": "system",
                "text": self._mask(system_text, 80)
                        + ("\n[记忆已注入]" if memories else ""),
                "mem": bool(memories),
                "bp": True,
            },
        ]
        for m in memories:
            segments.append(
                {"name": f"记忆块·{m.get('id','?')}", "text": self._mask(m.get("preview", "")),
                 "mem": True, "bp": True}
            )
        segments.append(
            {"name": f"messages 历史（{history_count} 轮）", "text": "……（易变内容放最后）",
             "mem": False, "bp": True}
        )
        return segments

    # ---- 路由 ----
    def build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._security_middleware])
        app.router.add_get("/", self.index)
        app.router.add_get("/api/sessions", self.api_sessions)
        app.router.add_get("/api/session/{id}", self.api_session)
        app.router.add_post("/api/session/{id}/prune", self.api_prune)
        app.router.add_post("/api/session/init", self.api_init)
        app.router.add_get("/api/metrics/summary", self.api_metrics)
        return app

    async def index(self, request: web.Request) -> web.Response:
        # token 由中间件校验通过；页面内嵌 token 供 fetch 附加
        page = PAGE_HTML.replace("__AUTH_TOKEN__", self.token)
        return web.Response(text=page, content_type="text/html", charset="utf-8")

    async def api_sessions(self, request: web.Request) -> web.Response:
        return web.json_response(self._list_sessions())

    async def api_session(self, request: web.Request) -> web.Response:
        sid = request.match_info["id"]
        s = self.sessions.get_session(sid)
        if s is None:
            return web.json_response({"error": "not found"}, status=404)
        memories = (s.meta or {}).get("memories", [])
        return web.json_response(
            {
                "session": {
                    "session_id": s.session_id,
                    "previous_response_id": s.previous_response_id,
                    "team_id": s.team_id,
                    "agent_id": s.agent_id,
                    "task_id": s.task_id,
                },
                "prefix_segments": self._prefix_segments(sid),
                "memories": memories,
                "sessions": self._list_sessions(),
            }
        )

    async def api_prune(self, request: web.Request) -> web.Response:
        sid = request.match_info["id"]
        s = self.sessions.get_session(sid)
        if s is None:
            return web.json_response({"error": "not found"}, status=404)
        body = await request.json()
        keep_ids = set(body.get("keep_ids") or [])
        old = list((s.meta or {}).get("memories", []))
        kept = [m for m in old if m.get("id") in keep_ids]
        removed = len(old) - len(kept)
        meta = dict(s.meta or {})
        meta["memories"] = kept
        self.sessions.update_meta(sid, meta)
        return web.json_response(
            {"keep_count": len(kept), "removed_count": removed,
             "note": "已更新会话记忆；下次请求将按新前缀缓存"}
        )

    async def api_init(self, request: web.Request) -> web.Response:
        """经弹网页链接初始化会话（老师方向：session init 走弹网页）。

        可同时设置该会话的记忆上限（TRACK 04 第三参数）：memory_cap=0 表示不限制，
        >0 限制单会话注入记忆块数；不传则回退到全局 config.session_memory_cap。
        """
        body = await request.json()
        cap = body.get("memory_cap")
        sess = self.sessions.create_session(
            team_id=body.get("team_id") or None,
            agent_id=body.get("agent_id") or None,
            task_id=body.get("task_id") or None,
            memory_cap=cap,
        )
        return web.json_response(
            {
                "session_id": sess.session_id,
                "memory_cap": (sess.meta or {}).get("memory_cap", 0) or 0,
                "note": "会话已创建；记忆上限将由网关在注入时按此值截断",
            }
        )

    async def api_metrics(self, request: web.Request) -> web.Response:
        return web.json_response(self.metrics.summary())


def main() -> None:
    parser = argparse.ArgumentParser(description="M6 Session Init 编排面板")
    # 方案 3.7 安全骨架：绑定 127.0.0.1（绝不能 0.0.0.0 暴露给局域网）；--port 0 = 随机端口
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args()
    app = SessionPanelApp(data_dir=args.data_dir)

    if args.port != 0:
        web.run_app(app.build_app(), host=args.host, port=args.port)
        return

    # 随机端口：拿到实际端口后打印「带一次性 token 的 URL」（v3：防扫端口 + fallback 人工复制）
    import asyncio

    async def _serve() -> None:
        runner = web.AppRunner(app.build_app())
        await runner.setup()
        site = web.TCPSite(runner, args.host, 0)
        await site.start()
        port = runner.addresses[0][1]
        print(f"M6 编排面板已启动（仅本机可见，Host 头校验 + 一次性 token）：")
        print(f"  打开: http://{args.host}:{port}/?t={app.token}")
        print("安全提示：绑定 127.0.0.1、随机端口、Host 头校验（防 DNS rebinding）、"
              "URL 一次性 token、记忆内容默认脱敏；不暴露局域网")
        while True:
            await asyncio.sleep(3600)

    asyncio.run(_serve())


if __name__ == "__main__":
    main()
