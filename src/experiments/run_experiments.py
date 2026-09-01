"""M5 · 实验层：记忆注入 × 缓存命中率三组对照（方案 3.5）

三组实验（每个都是独立维度）：
  * 实验一 · 更新频率：A 基线（不注入）/ B 锁死（会话内一次并固定）/ C 每轮动态变化
  * 实验二 · 位置：D1 记忆放前缀头部（system 后）/ D2 记忆放消息流尾部（append）
  * 实验三 · 粒度：G1 单块 300 token（低于阈值）/ G2 4×512 小块 / G3 单块 2048 大块 /
    G4 30 条细粒度（触发 20-block 回看窗口失效）

衡量指标：cache_read 命中率（核心）、每轮总 token 成本、盈亏平衡（5 分钟档 vs 1 小时档）。

mock 口径（示意）：
  * 稳定前缀 = system + messages[:-1]（对齐图 2：易变内容放最后）；
  * 前缀 token ≥ 最小可缓存阈值且单轮新增块 ≤ 20 才可缓存；
  * 前缀相同 → cache_read（命中）；新前缀 → cache_creation；否则不缓存。

运行：python -m src.experiments.run_experiments --rounds 10 --data-dir data
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from ..gateway.upstream import MockUpstream
from ..gateway.config import GatewayConfig
from ..ir.schema import CACHE_LOOKBACK_WINDOW_BLOCKS, ContentBlock, IRMessage, IRRequest
from ..observability.metrics import (
    WRITE_PREMIUM_1HOUR,
    WRITE_PREMIUM_5MIN,
    MetricRecord,
    MetricsStore,
)


def session_boundary_snapshot() -> dict[str, Any]:
    """方案 3.9.1 落地纪律①：实验前把 Session 边界配置完整快照，与命中率数据绑死。

    保证「这组数据是在什么边界下测的」可回溯；04 组口径变化只改配置、重跑即可对比。
    """
    cfg = GatewayConfig()
    return {
        "session_key_granularity": cfg.session_key_granularity,
        "session_key_fields": cfg.session_key_fields,
        "session_replay_from": cfg.session_replay_from,
        "session_sliding_window_n": cfg.session_sliding_window_n,
        "session_end_policy": cfg.session_end_policy,
        "session_ttl_seconds": cfg.session_ttl_seconds,
        "session_on_end": cfg.session_on_end,
        "premise": "命中率结论均在 Session 边界 = {task-id 单键，全量重放} 前提下陈述；"
                   "边界口径以 TRACK 04 最终设计为准",
    }


def memory_text(n_tokens: int, seed: str) -> str:
    """中文 1 字 ≈ 1 token 的示意生成器。"""
    return (seed * (n_tokens // len(seed) + 1))[:n_tokens]


# ---- 会话基座 ----
# 稳定前缀（persona + system prompt）：长 system 是命中缓存的基础（作业 1.4）
LONG_SYSTEM = memory_text(
    300, "稳定系统提示：你是协议转换实验助手，负责量化记忆注入与缓存命中率之间的关系。"
)
SHORT_SYSTEM = "你是实验助手。"
# 预置长会话历史（8 轮，约 720 token），使稳定前缀超过最小可缓存阈值
_HISTORY = [
    IRMessage.text("user", memory_text(90, f"历史第{idx}轮对话内容"))
    if idx % 2 == 0
    else IRMessage.text("assistant", memory_text(90, f"历史第{idx}轮回答内容"))
    for idx in range(8)
]


def _request(
    system_blocks: list[ContentBlock],
    extra_messages: Optional[list[IRMessage]] = None,
    with_history: bool = True,
    model: str = "claude-opus-5",
) -> IRRequest:
    req = IRRequest(
        model=model,
        system=system_blocks,
        messages=list(_HISTORY) if with_history else [],
        stream=False,
        max_tokens=32,
    )
    if extra_messages:
        req.messages.extend(extra_messages)
    return req


def _ask(round_i: int) -> IRMessage:
    return IRMessage.text("user", f"第{round_i}轮的新问题（易变内容放最后）")


# ---------------------------------------------------------------------------
# 实验组
# ---------------------------------------------------------------------------

# --- 实验一 · 更新频率 ---
def build_A(round_i: int, total: int, ctx: dict) -> IRRequest:
    """对照组：不注入记忆（基线）。稳定前缀 = 长 system + 历史 → 命中率高。"""
    return _request([ContentBlock.text_block(LONG_SYSTEM)], [_ask(round_i)])


def build_B(round_i: int, total: int, ctx: dict) -> IRRequest:
    """组 B：会话开始注入一次并锁死（确定性序列化，推荐策略候选）。

    TRACK 01 口径：对应 L3 人格画像（persona.md）无条件注入——稳定前缀应命中缓存。
    """
    mem = [ContentBlock.text_block(memory_text(200, "用户记忆：喜欢篮球、偏好简洁、中文交流。"))]
    return _request([ContentBlock.text_block(LONG_SYSTEM)] + mem, [_ask(round_i)])


def build_C(round_i: int, total: int, ctx: dict) -> IRRequest:
    """组 C：每轮动态注入（内容/顺序变化 → 前缀每轮变 → 缓存失效）。

    TRACK 01 口径：对应 L1/L2（原子事实 / 场景块）按需检索注入——动态内容命中不稳定。
    """
    mem = [ContentBlock.text_block(
        f"第{round_i}轮动态记忆：会话步数={round_i}，最近行为=查看实验脚本，时间戳={round_i}。" * 3
    )]
    return _request([ContentBlock.text_block(LONG_SYSTEM)] + mem, [_ask(round_i)])


# --- 实验二 · 位置 ---
def build_D1(round_i: int, total: int, ctx: dict) -> IRRequest:
    """组 D1：记忆放前缀头部（system 后）——命中稳定但占用缓存前缀。

    TRACK 01 口径：L3 人格画像「稳定前缀（persona + system prompt）命中缓存」的最佳落点。
    """
    mem = [ContentBlock.text_block(memory_text(200, "长期记忆：用户是大学生，偏好中文。"))]
    return _request([ContentBlock.text_block(LONG_SYSTEM)] + mem, [_ask(round_i)])


def build_D2(round_i: int, total: int, ctx: dict) -> IRRequest:
    """组 D2：记忆放消息流尾部（append，内容固定）。

    TRACK 01 口径：L1/L2 按需召回结果若放消息尾部 → 缓存层面前缀仍稳定，
    但 Responses 语义上与 previous_response_id 冲突、且改变模型注意力位置
    ——需在真实协议验证（本 mock 只演示缓存层面）。
    """
    mem = IRMessage.text("user", "长期记忆（尾部append）：用户是大学生，偏好中文。")
    return _request([ContentBlock.text_block(LONG_SYSTEM)], [mem, _ask(round_i)])


# --- 实验三 · 粒度（隔离：短 system + 无历史，只测记忆块本身）---
def build_G1(round_i: int, total: int, ctx: dict) -> IRRequest:
    """组 G1：单块约 300 token（低于 512 阈值）→ 静默不缓存。"""
    mem = [ContentBlock.text_block(memory_text(300, "细节记忆"))]
    return _request([ContentBlock.text_block(SHORT_SYSTEM)] + mem, [_ask(round_i)], with_history=False)


def build_G2(round_i: int, total: int, ctx: dict) -> IRRequest:
    """组 G2（v3 重写）：4 条 × 512 token，每条后各插一个断点（用满 4 个）。

    v3 语义：最小可缓存长度按「断点处累计前缀」判定，不是单块。
    断点1=512（512 档达标）/ 断点2=1024（1024 档达标）/ 断点3=1536（<2048/4096）/
    断点4=2048（2048 档达标）。本组用 Opus5（512 档）→ 全部断点达标，命中。
    """
    mem = [ContentBlock.text_block(memory_text(512, "块甲")) for _ in range(4)]
    return _request([ContentBlock.text_block(SHORT_SYSTEM)] + mem, [_ask(round_i)], with_history=False)


def build_G2_1024(round_i: int, total: int, ctx: dict) -> IRRequest:
    """组 G2-1024：同 4×512 小块，换 claude-opus-4.8（1024 档阈值）。

    v3 修正（推翻 v2「每条低于阈值全部不缓存」的错误预期）：
    断点2 累计前缀 = 1024 已达 1024 档阈值 → 后续断点可缓存 → 命中。
    前缀累加：分块不重置阈值，但会推迟达标时点。
    """
    mem = [ContentBlock.text_block(memory_text(512, "块甲")) for _ in range(4)]
    return _request(
        [ContentBlock.text_block(SHORT_SYSTEM)] + mem,
        [_ask(round_i)],
        with_history=False,
        model="claude-opus-4.8",
    )


def build_G2_4096(round_i: int, total: int, ctx: dict) -> IRRequest:
    """组 G2-4096：同 4×512 小块，换 claude-opus-4.5（4096 档阈值）。

    断点4 累计前缀 = 2048 < 4096 → 全程静默不缓存（4096 档需 4096+ 大块复测）。
    """
    mem = [ContentBlock.text_block(memory_text(512, "块甲")) for _ in range(4)]
    return _request(
        [ContentBlock.text_block(SHORT_SYSTEM)] + mem,
        [_ask(round_i)],
        with_history=False,
        model="claude-opus-4.5",
    )


def build_G3(round_i: int, total: int, ctx: dict) -> IRRequest:
    """组 G3：单块 2048 token 大块 → 超阈值稳定命中，token 成本最高。"""
    mem = [ContentBlock.text_block(memory_text(2048, "大块记忆"))]
    return _request([ContentBlock.text_block(SHORT_SYSTEM)] + mem, [_ask(round_i)], with_history=False)


def build_G4(round_i: int, total: int, ctx: dict) -> IRRequest:
    """组 G4：30 条细粒度条目 → 单轮新增 >20 block，触发回看窗口失效。"""
    mem = [ContentBlock.text_block(f"条目{i:02d}: 记忆内容 {i:02d}") for i in range(30)]
    return _request([ContentBlock.text_block(SHORT_SYSTEM)] + mem, [_ask(round_i)], with_history=False)


@dataclass
class GroupSpec:
    name: str
    label: str
    build: Callable[[int, int, dict], IRRequest]
    expect_high: bool  # True=预期命中率高（>50%），False=预期命中率低（<50%）
    note: str = ""


GROUP_SPECS: list[GroupSpec] = [
    GroupSpec("A",  "更新频率·基线(不注入)",   build_A,   True,  "命中率最高，但记不住偏好（无记忆）"),
    GroupSpec("B",  "更新频率·锁死注入(L3)",   build_B,   True,  "命中率高（推荐候选；TRACK01 L3 无条件注入）"),
    GroupSpec("C",  "更新频率·每轮动态(L1/L2)", build_C,   False, "命中率断崖→接近 0（TRACK01 L1/L2 按需召回）"),
    GroupSpec("D1", "位置·前缀头部(system后)",  build_D1,  True,  "命中稳定，占缓存前缀（L3 最佳落点）"),
    GroupSpec("D2", "位置·消息流尾部(append)",  build_D2,  True,  "缓存层命中，但 previous_response_id 语义冲突"),
    GroupSpec("G1", "粒度·单块300tok(<512)",    build_G1,      False, "静默不缓存"),
    GroupSpec("G2", "粒度·4×512(Opus5/512档)",  build_G2,      True,  "断点1即达标，全断点可缓存"),
    GroupSpec("G2-1024", "粒度·4×512(Opus4.8/1024档)", build_G2_1024, True,
              "v3修正：断点2累计1024达标→命中（推翻v2块级错误）"),
    GroupSpec("G2-4096", "粒度·4×512(Opus4.5/4096档)", build_G2_4096, False,
              "断点4累计2048<4096，全程静默不缓存"),
    GroupSpec("G3", "粒度·单块2048tok大块",    build_G3,      True,  "稳定命中，成本最高"),
    GroupSpec("G4", "粒度·30条(>20block)",     build_G4,      False, "尾部断点失效，全部重算"),
]


# ---------------------------------------------------------------------------
# 运行器
# ---------------------------------------------------------------------------

class ExperimentRunner:
    def __init__(
        self,
        rounds: int = 10,
        data_dir: str = "data",
        min_cache_tokens: int = 512,
        lookback_blocks: int = CACHE_LOOKBACK_WINDOW_BLOCKS,
    ):
        self.rounds = rounds
        self.metrics = MetricsStore(Path(data_dir) / "metrics.db")
        self.mock = MockUpstream(
            min_cache_tokens=min_cache_tokens, lookback_blocks=lookback_blocks
        )

    async def run_group(self, spec: GroupSpec) -> dict[str, Any]:
        # 清空该组历史记录，避免跨运行累积污染统计
        self.metrics.delete_group(spec.name)
        # v3 方法学前置（官方并发约束）：缓存条目在首个响应开始后才可用，
        # 必须先串行预热（max_tokens:0 建立缓存条目）再计量；预热不计入统计。
        warm_req = spec.build(0, self.rounds, {})
        warm_req.max_tokens = 0
        await self.mock.complete(warm_req)
        for i in range(1, self.rounds + 1):
            req = spec.build(i, self.rounds, {})
            resp = await self.mock.complete(req)
            self.metrics.record(
                MetricRecord.from_usage(
                    resp.usage, group=spec.name, protocol="anthropic",
                    model=req.model, tags={"label": spec.label},
                )
            )
        return self.metrics.summary(group=spec.name)

    async def run_all(self) -> list[dict[str, Any]]:
        results = []
        for spec in GROUP_SPECS:
            results.append(await self.run_group(spec))
            r = results[-1]
            print(f"  [{spec.name}] 命中率={r['hit_rate']:.2%} "
                  f"请求={r['requests']} 命中={r['hit_requests']} "
                  f"成本=${r['total_cost_usd']:.4f}")
        return results


def break_even_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """写入溢价与盈亏平衡（示意费率）。"""
    rows = []
    for r in results:
        creation_cost = r["total_creation_tokens"] / 1e6 * 3.0
        hit_saving_5min = r["total_read_tokens"] / 1e6 * (3.0 * WRITE_PREMIUM_5MIN - 0.3)
        hit_saving_1h = r["total_read_tokens"] / 1e6 * (3.0 * WRITE_PREMIUM_1HOUR - 0.3)

        # 无缓存活动（写成本与节省同时为 0，如 G1/G2-4096/G4 静默不缓存）：
        # 不存在「回本」概念，输出「—」避免 0>=0 误判为回本
        if creation_cost == 0 and hit_saving_5min == 0 and hit_saving_1h == 0:
            be_5min = be_1h = "—（无可缓存内容）"
        else:
            be_5min = "回本" if hit_saving_5min >= creation_cost else "未回本"
            be_1h = "回本" if hit_saving_1h >= creation_cost else "未回本"

        rows.append(
            {
                "group": r["group"],
                "requests": r["requests"],
                "hit_rate": r["hit_rate"],
                "creation_cost_usd": round(creation_cost, 5),
                "saving_5min_usd": round(hit_saving_5min, 5),
                "saving_1h_usd": round(hit_saving_1h, 5),
                "break_even_5min": be_5min,
                "break_even_1h": be_1h,
            }
        )
    return rows


def write_csv(rows: list[dict[str, Any]], path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


async def main() -> None:
    parser = argparse.ArgumentParser(description="M5 记忆注入×缓存命中率对照实验")
    # 方案 v2 时间校准：剩余约 2 周，实验量按天数砍半起步 → 默认 5 轮
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--mock-min-cache", type=int, default=512)
    parser.add_argument("--out", default="data/experiment_results.csv")
    args = parser.parse_args()

    print(f"=== M5 对照实验（mock 上游，每组 {args.rounds} 轮）===")
    snapshot = session_boundary_snapshot()
    # 落地纪律①：把 Session 边界配置快照与命中率数据绑死（保证可回溯）
    snap_path = str(Path(args.data_dir) / "session_boundary_snapshot.json")
    Path(snap_path).parent.mkdir(parents=True, exist_ok=True)
    with open(snap_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    print(f"[Session 边界快照] {snap_path}")
    print(f"  {json.dumps(snapshot, ensure_ascii=False)}")
    runner = ExperimentRunner(
        rounds=args.rounds, data_dir=args.data_dir, min_cache_tokens=args.mock_min_cache
    )
    results = await runner.run_all()
    be = break_even_rows(results)
    write_csv(be, args.out)
    print(f"\n结果已写入 {args.out}")

    print("\n=== 盈亏平衡对照（5 分钟档 vs 1 小时档，示意费率）===")
    for row in be:
        print(f"  {row['group']:>4} hit={row['hit_rate']:.0%} 写成本=${row['creation_cost_usd']:.4f} "
              f"5min={row['break_even_5min']} 1h={row['break_even_1h']}")

    print("\n=== 结论（对照预期）===")
    for spec in GROUP_SPECS:
        r = next(x for x in results if x["group"] == spec.name)
        hit = r["hit_rate"] > 0.5
        ok = hit == spec.expect_high
        mark = "✔符合" if ok else "✘偏离(需人工核对)"
        print(f"  [{spec.name}] {spec.label} → 命中率 {r['hit_rate']:.2%} "
              f"（预期：{spec.note}）{mark}")


if __name__ == "__main__":
    asyncio.run(main())
