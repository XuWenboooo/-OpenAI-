# Reproducibility Guide

This document separates three kinds of evidence in the repository:

1. **unit / integration tests** — deterministic local behavior
2. **offline / mock experiments** — controlled cache and protocol experiments
3. **real-provider validation** — behavior that depends on current external APIs, credentials, pricing, and provider semantics

These evidence types should not be mixed.

## 1. Environment

Recommended Python versions are the versions covered by CI:

- Python 3.11
- Python 3.12

Create a clean environment:

```bash
python -m venv .venv
```

PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Linux / macOS:

```bash
source .venv/bin/activate
```

Install development dependencies:

```bash
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
```

## 2. Reproduce the automated test suite

```bash
pytest tests/ -q
```

Expected behavior: adapter, core, and mock gateway tests should pass without external API credentials.

The CI workflow repeats this check on Python 3.11 and 3.12.

## 3. Reproduce the offline experiment layer

Run:

```bash
python -m src.experiments.run_experiments --data-dir data
```

The experiment design studies combinations of:

- memory update frequency
- memory placement
- chunk / granularity behavior
- cache-threshold assumptions
- session-boundary configuration

Important methodological constraint: cache experiments use a warm-up phase before measured requests. Warm-up requests are not counted as measured samples.

Generated data files are intentionally excluded from version control. The authoritative narrative and summarized results live in `docs/量化取舍文档.md`.

## 4. Session-boundary assumptions

Cache / memory conclusions depend on session semantics.

Before interpreting results, record or inspect:

- key granularity
- replay policy
- end policy
- session memory cap

The implementation can emit a session-boundary snapshot into the data directory. Treat this configuration as part of the experiment, not incidental runtime state.

## 5. Mock vs real-provider evidence

The result table in `docs/量化取舍文档.md` is explicitly described as a **mock / offline controlled experiment**.

Do not cite those numbers as current OpenAI or Anthropic production measurements without real-provider replication.

Real-provider validation may require:

```powershell
$env:ANTHROPIC_API_KEY="..."
$env:OPENAI_API_KEY="..."
```

or equivalent environment variables on Linux / macOS.

Never commit those credentials.

## 6. Real-provider validation checklist

When credentials and quota are available, a validation run should record:

- date and time
- provider
- model identifier
- relevant API / SDK version
- request mode
- cache TTL / threshold assumptions
- warm-up procedure
- number of measured requests
- token accounting returned by the provider
- any provider errors or rate-limit effects

External behavior can change. A dated result is more scientifically useful than an undated claim that a provider "works."

## 7. Reproducibility limitations

Several properties cannot be guaranteed by this repository alone:

- provider pricing may change
- cache threshold rules may change
- undocumented provider behavior may change
- model aliases can point to updated backends
- network and rate-limit behavior are external
- real-provider cache state is not fully controlled locally

For this reason, provider-specific claims should always carry a validation date and evidence level.

## 8. Evidence levels

A useful convention for future reports:

| Level | Meaning |
|---|---|
| `TESTED_OFFLINE` | deterministic local test / mock behavior |
| `CONTROLLED_EXPERIMENT` | repeated experiment under documented local assumptions |
| `PROVIDER_VALIDATED` | reproduced against a real external provider on a stated date |
| `EXTERNAL_CLAIM` | based on provider documentation or other external source, not independently reproduced |

This distinction helps keep engineering evidence and research conclusions auditable.
