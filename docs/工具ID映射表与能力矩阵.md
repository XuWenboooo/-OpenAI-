# 工具 ID 映射表 与 能力矩阵 v0

> 协议转换课题 · TRACK 05A/05B 交付物（方案点名的 W1 / W2 产出物）
> 说明三协议在**工具调用 ID** 上的对齐关系，以及**每个协议的能力边界**与转换器的支持情况。
> 本文件为 v0（首版），能力边界随真实 API 验证（D2 / 20-block / 4096 档）可能微调。

---

## 1. 工具 ID 映射表（Tool ID Mapping）

三协议对「一次工具调用」的标识与「结果回指」字段命名不同，转换经 IR 中转时必须**保持 ID 不变**，否则工具结果无法正确回指工具调用（roundtrip 失败）。

| 语义角色 | Anthropic Messages | OpenAI Chat | OpenAI Response | IR 字段 |
|---|---|---|---|---|
| 工具调用 id（请求侧） | `content[].tool_use.id`（形如 `toolu_01…`） | `choices[].message.tool_calls[].id`（形如 `call_…`） | `output[].function_call.call_id`（形如 `fc_…` / `call_…`） | `ContentBlock.id` |
| 工具名 | `tool_use.name` | `tool_calls[].function.name` | `function_call.name` | `ContentBlock.name` |
| 工具入参 | `tool_use.input`（object） | `tool_calls[].function.arguments`（JSON 字符串） | `function_call.arguments`（object 或 JSON 字符串） | `ContentBlock.input`（object，统一解析） |
| 结果回指 id | `content[].tool_result.tool_use_id` | `messages[].tool_call_id`（role=tool 消息） | `input[].function_call_output.call_id` | `ContentBlock.tool_use_id` |
| 结果内容 | `tool_result.content`（str / blocks） | `messages[].content`（str） | `function_call_output.output`（str） | `ContentBlock.text` |
| 工具定义 id | `tools[].id`（可选） | `tools[].function`（无独立 id） | `tools[].name`（无独立 id） | `IRTool.id`（可选透传） |

### 1.1 不变式（转换约束）

- **调用侧 id 必须原样透传**：`adapter.to_ir` 提取 `id` → 存入 `ContentBlock.id`；`adapter.from_ir` 还原回各协议对应字段。转换器不重新生成 id，避免工具结果回指失效。
- **结果侧回指 id 必须对齐调用侧 id**：`tool_result.tool_use_id` / `tool_call_id` / `function_call_output.call_id` 全部来自同一份 `ContentBlock.tool_use_id`，与调用侧 `ContentBlock.id` 配对。
- **arguments 格式归一**：OpenAI Chat 的 `arguments` 是 JSON 字符串，进入 IR 时统一 `json.loads` 为 object；出 IR 时统一 `json.dumps` 回字符串（见 `openai_chat.py` / `openai_response.py` 的 `_parse_args` 与渲染逻辑）。

### 1.2 已知差异与取舍

- **OpenAI Response 的 id 策略**：`from_ir` 渲染 `function_call` 时，`id` 使用 `f"fc_{ir.id}"` 作为 item id，`call_id` 仍用 IR 透传的 `b.id`——因为 OpenAI Response 的 `function_call` 同时需要 item 级 id 与回指级 call_id，二者角色不同，不能混用。
- **Anthropic `tools[].id`**：当前 IR 的 `IRTool.id` 可选透传，但三协议的工具定义 id 语义不完全一致（Anthropic 可显式指定、OpenAI 无），故**不以工具定义 id 做跨协议关联**，关联一律以「调用侧 `ContentBlock.id` ↔ 结果侧 `ContentBlock.tool_use_id`」为准。
- **降级**：结构化输出（`response_format` / `text.format`）触发显式降级，工具调用本身不受影响。

---

## 2. 能力矩阵 v0（Capability Matrix）

✅ 已支持 / 🟡 部分支持或依赖条件 / ❌ 明确不做（方案 3.6）

| 能力维度 | Anthropic Messages | OpenAI Chat | OpenAI Response | 转换器说明 |
|---|---|---|---|---|
| 文本对话 | ✅ | ✅ | ✅ | IR `text` block 三向互通 |
| 工具调用（tool_use） | ✅ | ✅ | ✅ | 见 §1 映射，ID 原样透传 |
| 工具结果（tool_result） | ✅ | ✅（role=tool 消息） | ✅（function_call_output） | 回指 id 配对 |
| 流式 SSE 透传 | ✅ | ✅ | ✅ | 三协议均实现 `stream_chunk`，网关 `_handle_stream` 转发 |
| 显式缓存断点（cache_control） | ✅ 4 断点布局 | ❌ 自动缓存 | ❌ 自动缓存 | 仅 Anthropic 渲染断点；OpenAI 自动前缀缓存无断点概念 |
| `previous_response_id` 有状态 | 🟡 靠状态层兜底 | ❌ 无状态（客户端重放） | ✅ 原生字段 | 网关 `_apply_replay` 按 prev_id 反查重建前缀（stateful 触发） |
| 思考链（thinking / reasoning） | ✅ `thinking` block | ❌ | 🟡 `reasoning` 以 extra 透传 | IR 保留 `thinking` 块，OpenAI Chat 端无对应字段则丢弃 |
| 多模态（image / document / audio） | ❌ | ❌ | ❌ | 方案 3.6 明确不做，`UNSUPPORTED_KEYS` 静默丢弃 |
| 结构化输出（JSON mode） | ❌ | 🟡 `response_format` 显式降级 | 🟡 `text.format` 显式降级 | 降级时仍走规则化输出，非报错 |
| usage 缓存字段解析 | ✅ `cache_creation` / `cache_read` | ✅ `cached_tokens` | ✅ `cached_tokens` | IR `IRUsage` 统一，埋点取 `cache_read` 算命中率 |
| 请求体上限 / 参数裁剪 | ✅ | ✅ | ✅ | `UNSUPPORTED_KEYS` 记录并降级/丢弃，埋点上报 `dropped_params` |
| 记忆注入（幂等去重） | ✅ | ✅ | ✅ | 网关注入 IR system 尾部，按文本去重 |
| 限流 / 预热路径 / 拒绝条件 | ✅（网关统一） | ✅ | ✅ | 与协议无关，网关层处理 |

### 2.1 关键取舍说明

- **缓存断点只在 Anthropic 侧「可见」**：OpenAI 两协议为自动前缀缓存（prompt ≥ 阈值即参与），无显式断点。因此「4 断点布局」是 IR→Anthropic 的渲染产物，对 OpenAI 侧是透明的。
- **有状态依赖状态层兜底**：Anthropic 与 OpenAI Chat 没有原生的 `previous_response_id`，转换器用 **task-id 单键**（session_id 主键）在 `SessionStore` 维护上一轮 id 与历史，模拟有状态省 token 效果；OpenAI Response 则直接透传其原生字段。
- **多模态一律不做**：三协议的多模态字段均进入 `UNSUPPORTED_KEYS` 静默丢弃清单，且**不在 README / 文档中宣称支持**，避免声明与实现不符。
- **结构化输出降级而非报错**：用户传入 `response_format` / `text.format` 时，转换器记录并显式降级（仍返回规则化文本），不抛 5xx。

---

## 3. 与本仓库其他文档的关系

- `docs/ir-schema.md`：IR v0 三层契约（L0 内容块 / L1 规范请求响应 / L2 会话缓存上下文）
- `docs/量化取舍文档.md`：核心交付物——记忆注入 × KV Cache 命中率的量化取舍
- `docs/完成度评测报告.md`：逐模块完成度评测与待办清单
- `docs/评审综述.md`：功能 / 完成度 / 不足之处 三部分评审综述

> 本文件属于 M7 交付层的 W1/W2 产出物；能力矩阵 v0 的边界以真实 API 验证（见评测报告「剩余待办·真实 API 验证」）为准，验证后升级为 v1。
