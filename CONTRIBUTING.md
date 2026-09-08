# Contributing

Thanks for your interest in improving Protocol Converter.

## Development setup

```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# Linux / macOS
# source .venv/bin/activate

python -m pip install --upgrade pip
pip install -r requirements-dev.txt
pytest tests/ -q
```

## Branch and pull-request workflow

Use short-lived branches and keep the default branch releasable.

Recommended branch prefixes:

- `feat/` — user-visible functionality
- `fix/` — bug fixes
- `docs/` — documentation-only changes
- `test/` — testing changes
- `chore/` — maintenance and repository work
- `research/` — experiment or evaluation changes

Before opening a PR:

1. Run `pytest tests/ -q` locally.
2. Keep provider-specific behavior explicit; do not silently erase unsupported fields.
3. Update documentation when protocol semantics, cost assumptions, cache thresholds, or experiment methodology change.
4. Do not commit API keys, session databases, local `.env` files, or generated experiment output.
5. Describe any behavior degradation or lossy conversion in the PR body.

## Protocol changes

Changes to adapters or the IR should answer all of the following:

- Which source protocol is affected?
- Which target protocol is affected?
- Is the conversion lossless, explicitly degraded, or unsupported?
- Are dropped parameters observable?
- Does the change alter cache, token-accounting, or session semantics?
- Is there a regression test?

## Experiment changes

Experiment PRs should record:

- configuration and model assumptions
- warm-up behavior
- session-boundary configuration
- cache threshold assumptions
- number of measured runs
- whether the result is mock/offline or real-provider validated
- limitations and unresolved external dependencies

## Review philosophy

A successful change should be understandable from the code, testable in a clean environment, and reviewable without relying on hidden local state.
