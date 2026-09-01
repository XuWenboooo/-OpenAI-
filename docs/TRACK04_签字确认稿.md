# TRACK 04 Session 边界三参数 · 签字确认稿（本地验证版）

> 本文档是 TRACK 04「Session 边界三耦合点（TTL / 最大轮次 / 记忆上限）」签字确认的**前置本地验证稿**。
> **签名动作本身需导师拍板，本文档不可替代签字**；本文档证明：默认假设值已可独立运行，且三耦合点行为经本地测试 + 实验验证正确。
>
> - 验证时间：2026-09-01
> - 代码基准：`d724a3b`（已推 GitHub `master`，对齐方案 v3(1) 3.9.1）
> - 结论前提：所有命中率结论均在 `Session 边界 = {task-id 单键，全量重放}` 前提下陈述（见 §3 快照）

---

## 一、三参数现状与待确认项

| # | 参数 | 对应配置项 | 当前默认值 | 是否已成开关 | 本地验证 | 待导师确认 |
|---|------|-----------|-----------|------------|---------|-----------|
| 1 | TTL（会话超时淘汰） | `session_ttl_seconds` | **1800 s（30 min）** | ✅ 是 | ✅ `evict_expired` 按 ttl 触发 | ☐ 确认 1800s |
| 2 | 最大轮次（重放窗口上限 / 20-block 回看保护） | `session_sliding_window_n` | **20** | ✅ 是 | ✅ `sliding_window` 模式截断到最近 20 轮 | ☐ 确认 20 |
| 3 | 记忆上限（单会话注入记忆的块数上限） | `session_memory_cap`（全局默认） / 会话 `meta.memory_cap`（经弹网页 Session Init 按会话设） | **0 = 不限制（默认，全部注入）**；>0 限制单会话注入记忆块数 | ✅ 是（全局默认 + 会话级覆盖） | ✅ `_inject_memories` 应用上限，三档测试通过（全局 2 / 会话级 1 / 默认不限制） | ☐ 确认默认值 **0 = 不限制**（或指定具体上限值） |

### 关于第 3 项（记忆上限）的说明（已实现，按老师方向「session init 走弹网页链接」）
- 全局默认：`config.session_memory_cap`（默认 **0 = 不限制**，保持原行为）；>0 限制单会话注入记忆块数。
- 会话级覆盖：**经弹网页 M6 面板「新建会话 · Session Init」** 初始化会话时可填 `memory_cap`，存入会话 `meta.memory_cap`，优先于全局默认（老师方向落点）。
- 有效上限优先级：会话 `meta.memory_cap` > 全局 `config.session_memory_cap` > 0（不限制）；达到上限即停止注入（保留已注入的稳定缓存前缀）。
- 验证：`test_memory_cap_limits_injection`（三档全过）、`test_create_session_memory_cap_persist`、`test_webpanel_session_init`（弹网页创建会话并落库记忆上限）。
- 故三参数**现全部可配置、可签字**；签字仅确认默认值取值。

---

## 二、三耦合点本地验证证据

| 耦合点 | 代码落点 | 验证方式 | 结果 |
|--------|---------|---------|------|
| ① 重放起点 → 缓存前缀 | `gateway._apply_replay`（按 prev_id 反查 + 重建历史前缀拼入 `IR.messages`，仅 stateful 请求触发） | `test_replay_into_prefix` + M5 全量重放下命中率 | ✅ `full` 模式 11 组符合预期 |
| ② 记忆回流幂等 | `gateway._inject_memories`（文本去重，避免重复注入/计费） | `test_memory_injection` | ✅ 幂等去重生效 |
| ③ prev_id 状态键 | `state/store.set_previous_response_id` / `find_active_by_task` | `test_state_store` | ✅ task-id 单键兜底正确 |

---

## 三、边界配置快照（与命中率数据绑死，方案 3.9.1 纪律①）

- 文件：`data/session_boundary_snapshot.json`（每次实验前自动重写）
- 当前内容：
  ```json
  {
    "session_key_granularity": "single_task",
    "session_key_fields": ["task-id"],
    "session_replay_from": "full",
    "session_sliding_window_n": 20,
    "session_end_policy": "ttl",
    "session_ttl_seconds": 1800,
    "session_on_end": "archive",
    "session_memory_cap": 0,
    "premise": "命中率结论均在 Session 边界 = {task-id 单键，全量重放} 前提下陈述；边界口径以 TRACK 04 最终设计为准"
  }
  ```
- 含义：本稿所有命中率结论均在 `{task-id 单键，全量重放}` 前提下陈述；边界口径以 TRACK 04 最终设计为准。04 组口径一变，改配置重跑即可对比。

---

## 四、本地运行记录（2026-09-01，含记忆上限开关实现）

| 命令 | 结果 |
|------|------|
| `pytest tests/test_core.py` | **15 passed**（含 3.9.1 边界 + 记忆上限三档 + 弹网页 Session Init 路由） |
| `python -m src.experiments.run_experiments --rounds 5` | M5 对照 11 组 **✔ 全部符合预期**；快照已写入 `data/session_boundary_snapshot.json`（含 `session_memory_cap`） |
| 默认假设值断言 | `test_session_boundary_config_defaults` 确认与 v3(1) 第 13–14 页对照表一致 |

**三开关 + 记忆上限默认假设值经测试断言确认**：`single_task` / `full` / `ttl`+`archive` / `memory_cap=0`（不限制）。

---

## 五、签字栏（待导师）

- [ ] TRACK 04 负责人 / 导师确认：Session 边界三参数
  - TTL = **1800 s**
  - 最大轮次 = **20**
  - 记忆上限 = **0（不限制，默认）** / 或具体 cap 值：__________
  作为默认假设值，可据此推进后续真实 API 验证。
- 签字： __________ 　 日期： __________
