# Experiment Results Summary

This page gives a compact portfolio-oriented summary of the controlled experiment already documented in detail in `docs/量化取舍文档.md`.

## Evidence level

**CONTROLLED_EXPERIMENT / MOCK**

The values below come from the repository's offline mock experiment. They are useful for validating the experiment logic and comparing strategies under controlled assumptions. They are **not** presented as current production measurements from OpenAI or Anthropic.

## Research question

How do memory-injection frequency, placement, and granularity affect cache hit behavior and estimated cost in a protocol-conversion gateway?

## Controlled result snapshot

| Group | Strategy | Cache hit rate | Estimated measured cost | Interpretation |
|---|---|---:|---:|---|
| A | no injected memory baseline | 100% | $0.0200 | stable prefix after warm-up |
| B | inject once at session start and freeze | 100% | $0.0233 | preferred memory strategy under tested assumptions |
| C | dynamically inject every turn | 0% | $0.0422 | prefix instability destroys cache reuse |
| D1 | stable memory near prefix / system area | 100% | $0.0233 | stable and semantically preferred |
| D2 | memory appended near message tail | 100% | $0.0206 | cache-layer hit survives, but state semantics need care |
| G1 | sub-threshold small block | 0% | $0.0067 | silently below the configured cache threshold |
| G2 | four 512-token blocks, 512-threshold model | 100% | $0.0360 | cumulative prefix reaches threshold |
| G2-1024 | same blocks, 1024 threshold | 100% | $0.0360 | cumulative threshold reached at later breakpoint |
| G2-4096 | same blocks, 4096 threshold | 0% | $0.0329 | cumulative prefix remains below threshold |
| G3 | single ~2048-token block | 100% | $0.0360 | stable in tested lower-threshold configurations |
| G4 | many fine-grained blocks with rolling breakpoint | 0% | $0.0081 | exceeds modeled 20-block lookback behavior |

## Main observations

### 1. Update frequency dominates

Under the controlled experiment, stable session-start memory (`B`) keeps the cacheable prefix fixed and retains the same post-warm-up hit behavior as the baseline.

Dynamic per-turn memory (`C`) changes the prefix every round and eliminates reuse under the modeled cache semantics.

### 2. Placement is secondary to stability

A stable prefix location (`D1`) preserves the intended memory semantics and cache behavior.

Tail placement (`D2`) can still hit at the cache layer in the mock model, but may conflict with stateful semantics such as `previous_response_id`; therefore cache hit rate alone is not enough to select the placement.

### 3. Thresholds are cumulative-prefix properties

The experiment corrected an earlier block-level interpretation: splitting content into blocks does not reset the modeled threshold. The threshold is evaluated on cumulative prefix length at a breakpoint.

### 4. Warm-up is part of the experimental protocol

Measured requests are run only after a serial warm-up phase. Without that discipline, first-batch misses would contaminate the comparison.

## Recommended interpretation

The controlled result supports the design preference:

> **Inject stable session-level memory once, serialize it deterministically, and keep it in the stable prefix when possible.**

This is a repository-level engineering conclusion under the stated mock assumptions, not a universal provider guarantee.

## Limitations

- The summarized table is based on mock / offline semantics.
- Current provider pricing and cache behavior require dated real-provider validation.
- Some provider-specific state interactions remain external-validation tasks.
- Generated experiment CSV / JSON files are not committed; they are reproducible from the experiment runner.

For full methodology, assumptions, cache-accounting details, and provider-document references, see `docs/量化取舍文档.md` and `docs/REPRODUCIBILITY.md`.
