# IR v0 契约文档（ir-schema.md）

> 犀牛鸟开源实战 · TRACK 05A / 05B 协议转换组 · M0 契约层
> 版本：v0（待两队共同签字） · 生成日期：2026-09-01
> 配套实现：`src/ir/schema.py`（本契约的可执行版本，以代码为准）

## 0. 一句话契约

**三个协议（OpenAI Chat / OpenAI Response / Anthropic Messages）只与 IR 对话，
不直接两两互转。每个协议只写「to_ir / from_ir」两个方向（3 对 adapter），
把 6 个转换方向压缩为 3。**

```
  OpenAI Chat (05A)  ──adapter──┐
  OpenAI Response (05B) ──adapter──┼──▶ IR 中间表示（唯一契约）◀──adapter── Anthropic
```

IR 一冻结，05A 与 05B 即可并行开发，第三周才能合得起来。

---

## 1. IR 分层

| 层 | 名称 | 承载内容 | 代码位置 |
|---|---|---|---|
| L0 | 内容块模型 | 消息内容载体（text / tool_use / tool_result / thinking） | `ContentBlock` |
| L1 | 规范请求/响应 | 统一请求、工具、用量、响应结构 | `IRRequest` / `IRResponse` / `IRUsage` / `IRTool` |
| L2 | 会话与缓存上下文 | 会话归属（team/agent/task）、previous_response_id、缓存断点布局 | `IRSessionContext` |

---

## 2. L0 · 内容块模型

每条 IR 消息的 `content` 统一为 **ContentBlock 数组**。

| type | 关键字段 | 说明 |
|---|---|---|
| `text` | `text` | 普通文本 |
| `tool_use` | `id`, `name`, `input` | 模型发起的工具调用 |
| `tool_result` | `tool_use_id`, `text`, `is_error` | 工具执行结果，回填给模型 |
| `thinking` | `thinking` | 思考块（往返透传） |

**明确不做**：多模态（image / document / audio）内容块 —— 见执行方案 3.6。

### 各协议到 IR 的对应规则

| 协议 | 原结构 | IR 映射 |
|---|---|---|
| Anthropic | `content` 已是 block 数组 | 1:1 直接映射 |
| OpenAI Chat | `content` 为字符串；工具在顶层 `tool_calls` | 字符串 → 单个 text 块；`tool_calls` → tool_use 块 |
| OpenAI Response | `content` 为数组（output_text / function_call） | 逐项映射为 text / tool_use 块 |

---

## 3. L1 · 规范请求 / 响应

### 3.1 IRRequest 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `model` | str | 目标模型名 |
| `system` | list[ContentBlock] | 系统提示（缓存前缀一部分） |
| `messages` | list[IRMessage] | 对话消息 |
| `tools` | list[IRTool] | 工具定义 |
| `stream` | bool | 是否流式 |
| `max_tokens` / `temperature` | int / float | 采样参数 |
| `cache_control` | list[int] | 需要插缓存断点的消息下标 |
| `extra` | dict | 协议私有字段透传 |

### 3.2 IRUsage（北极星指标的数据来源）

| 字段 | 对应 Anthropic | 对应 OpenAI |
|---|---|---|
| `input_tokens` | `input_tokens` | `prompt_tokens` |
| `output_tokens` | `output_tokens` | `completion_tokens` |
| `cache_creation_input_tokens` | 同名 | `prompt_tokens_details.cached_tokens`（写入侧） |
| `cache_read_input_tokens` | 同名 | 由 OpenAI 缓存命中侧换算 |

**命中判定（方案 3.3 口径）**：
- `cache_read > 0` → **命中**（hit）
- `cache_creation > 0 且 cache_read == 0` → 写入新前缀（creation）
- 两者同时为 0 → **未命中**（miss，每轮重新付费）

### 3.3 IRResponse 字段

`id` / `model` / `role` / `content`（block 数组）/ `stop_reason` / `usage` /
`previous_response_id`（OpenAI Response 有状态字段，状态层管理）/ `raw`（原始响应）。

---

## 4. L2 · 会话与缓存上下文

### 4.1 会话归属

Session Init 的二级/三级身份绑定：`team_id` / `agent_id` / `task_id`。
三者齐全才能「直接登记」跳过表单（来自作业 5.2 踩坑 4）。

### 4.2 previous_response_id（唯一有状态点）

OpenAI Response 依赖服务端保存上一轮内容；转换层必须支持该字段。
实现上由 **M2 状态层** 统一存储（会话表 + TTL），是转换层落点锁定
「网关/代理 + 独立状态层」的根本原因。

### 4.3 缓存断点布局（核心约定）

**关键规则：断点之前任意字节变了，之后全部失效。**
渲染顺序**必须固定**为：

```
tools → system → messages（易变内容放最后）
```

**缓存回看窗口：单轮新增 content block 超过 20 个 → 尾部断点失效、全部重算，且不报错。**
对策：主动分层插断点（工具后 / 系统后 / 历史每 N 块）。

**缓存阈值非单调**（不能外推，必须逐模型查表）：

| 模型 | 最小可缓存阈值（token） |
|---|---|
| claude-opus-4.5 / 4.6 | 4096 |
| claude-opus-4.7 | 2048 |
| claude-opus-4.8 | 1024 |
| claude-opus-5 | 512 |

---

## 5. 冻结签字

- [ ] 05A（OpenAI Chat 主对象）签字
- [ ] 05B（OpenAI Response 主对象）签字
- [ ] Anthropic 共享端点确认

> 冻结后如需修改，走变更流程：更新 `src/ir/schema.py` + 本文档 + 通知双方，
> 禁止静默改动导致两队在 W2 合并时对不上。
