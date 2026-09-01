# 协议转换器（Protocol Converter）

> 犀牛鸟开源实战 · TRACK 05A / 05B 协议转换组
> 在 OpenAI Chat / OpenAI Response 与 Anthropic Messages 之间做**协议转换层**，
> 核心回答「记忆注入 × KV Cache 缓存命中率」的量化取舍。

## 一句话

写一个**翻译层**（网关/代理 + 独立状态层），让按某家格式写的程序能无缝切换到另一家；
最终交付物不是转换器本身，而是一份**量化取舍文档**（`docs/量化取舍文档.md`）。

## 架构

```
OpenAI Chat (05A) ──adapter──┐
OpenAI Response (05B) ──adapter──┼──▶ IR 中间表示（唯一契约）◀──adapter── Anthropic
```

- **IR v0**：`src/ir/schema.py`（L0 内容块 / L1 规范请求 / L2 会话缓存上下文；4 断点上限 + 20-block 回看窗口），契约见 `docs/ir-schema.md`
- **3 对 adapter**：`src/adapters/`（chat / response / anthropic），6 个方向压成 3；含 `ir_to_payload`（IR→上游请求，Anthropic 侧渲染 cache_control 4 断点布局）与 `response_to_ir`
- **命中率埋点**：`src/observability/metrics.py`（北极星指标：cache_read 命中率；方案 3.3 五项可观测性；OpenAI 折扣按模型代次）
- **状态层**：`src/state/store.py`（SQLite 会话表 + TTL + previous_response_id + TRACK04 单键兜底 + 会话级 `memory_cap` 落库）
- **网关**：`src/gateway/server.py`（HTTP 转发 + SSE 透传 + 记忆注入（幂等去重 + `session_memory_cap` 上限：全局默认 0=不限制，会话级可经弹网页覆盖）+ 限流 + 预热请求单独路径 + 拒绝条件校验）
- **实验**：`src/experiments/run_experiments.py`（更新频率 × 位置 × 粒度 × 模型阈值，11 组对照，先预热再计量）
- **编排面板**：`src/webpanel/app.py`（Session init 上下文编排 + 缓存前缀可视化 + **经弹网页链接新建会话** `POST /api/session/init`（可设 team/agent/task 与记忆上限）；绑 127.0.0.1 / 随机端口 / Host 校验 / 一次性 token / 记忆脱敏）

## 快速开始

```bash
pip install -r requirements.txt

# 1) 离线跑对照实验（默认 5 轮 × 10 组）
python -m src.experiments.run_experiments --data-dir data

# 2) 启动网关（mock 演示「外部协议 → IR → 上游 → 回包」链路）
python -m src.gateway.server --mock --port 8096
curl -X POST http://127.0.0.1:8096/v1/messages -H "content-type: application/json" \
  -d '{"model":"claude-opus-5","system":"你是助手","messages":[{"role":"user","content":[{"type":"text","text":"hi"}]}]}'

# 3) 启动编排面板（随机端口 + 一次性 token + Host 校验；启动日志会打印完整 URL）
python -m src.webpanel.app --port 0

# 4) 测试
python tests/test_adapters.py && python tests/test_gateway.py && python tests/test_core.py
#    或用 pytest 统一收集（含 async 测试）
pytest tests/ -q
```

## 真实上游（关闭 mock）

```bash
# 全部外部端点默认转到 Anthropic（共享右侧端点），需配 key
$env:ANTHROPIC_API_KEY="sk-..."   # PowerShell
python -m src.gateway.server --port 8096        # 不加 --mock 即真实模式
# 或转发到 OpenAI：--upstream openai 需 $env:OPENAI_API_KEY
```

## 硬件友好

无需 GPU；本地只做 HTTP 转发 / JSON 映射 / SSE 透传。单进程异步 + SQLite，
并发默认 2（`--concurrency`），日志按天轮转——不影响日常使用。

## 模块分工（M0–M7）

| 模块 | 职责 | 状态 |
|---|---|---|
| M0 契约层 | IR v0 + ir-schema.md | ✅ |
| M1 可观测 | 命中率埋点 + usage 解析 + 5 项可观测性 | ✅ |
| M2 状态层 | 会话表 + TTL + previous_response_id + TRACK04 单键 | ✅ |
| M3 适配层 | 三对 adapter + 丢弃参数/降级记录 | ✅ |
| M4 网关 | HTTP 转发 + SSE 透传 + 限流 + 预热路径 + 拒绝条件 | ✅ |
| M5 实验层 | 11 组对照（先预热再计量 + 4断点 + G2 分档） | ✅ |
| M6 弹网页 | 上下文编排 + 前缀可视化 + 安全骨架（Host/token/脱敏） | ✅ |
| M7 交付层 | 量化文档 ✅ + PR（代码已 push 到 master，评审流程未走） | 🟡 |

## 待办（外部依赖 / 流程）

- [x] TRACK 01 注入策略（L0–L3）已获取，作为实验输入（B/D1=L3 无条件注入，C/D2=L1/L2 按需召回）
- [x] **工具 ID 映射表 + 能力矩阵 v0**（W1/W2 产出物，见 `docs/工具ID映射表与能力矩阵.md`）
- [x] 向 TRACK 04 对齐 Session 边界（方案 3.9.1）：**已做成可切换配置**（`config.py` 三开关 `key_granularity` / `replay_from` / `end_policy` + 默认假设值 task-id 单键 / 全量重放 / ttl+archive）；实验前把配置快照写入 `data/session_boundary_snapshot.json` 与命中率数据绑死。**第三参数「记忆上限」也已做成开关**（`config.session_memory_cap` 全局默认 0=不限制，会话级 `meta.memory_cap` 可经弹网页 Session Init 设置），三参数现已全部可配置、可签字。仅待 04 在参数层面签字确认（当前默认可独立运行）
- [ ] 分工会向导师确认自开仓库 PR 是否计入「开源提交 PR」考核口径；若只认上游 PR → fallback 向上游提最小可用 PR（当前代码已 push 到 master，尚未走 PR 评审）
- [ ] 真实验证：D2 previous_response_id 冲突、20-block 窗口、各模型阈值表、count_tokens RTT、4096 档大块复测（需 API key）

## 文档索引

- `docs/量化取舍文档.md`：核心交付物——记忆注入 × KV Cache 命中率量化取舍
- `docs/ir-schema.md`：IR v0 三层契约（L0 内容块 / L1 规范请求响应 / L2 会话缓存上下文）
- `docs/工具ID映射表与能力矩阵.md`：三协议工具 ID 对齐 + 能力矩阵 v0（W1/W2）
- `docs/完成度评测报告.md`：逐模块完成度评测与待办清单
- `docs/评审综述.md`：功能 / 完成度 / 不足之处 评审综述
- `docs/TRACK04_签字确认稿.md`：TRACK 04 Session 边界三参数（TTL / 最大轮次 / 记忆上限）本地验证证据 + 边界快照 + 待导师签字栏
