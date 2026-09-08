# Architecture

## Purpose

Protocol Converter provides a translation layer between OpenAI Chat, OpenAI Responses, and Anthropic Messages while keeping state, cache semantics, observability, and degradation behavior explicit.

The design avoids pairwise protocol-to-protocol conversion logic. Instead, each protocol maps to and from a shared intermediate representation (IR).

```text
OpenAI Chat ---------┐
                     │
OpenAI Responses ----+----> Intermediate Representation ----> target adapter ----> upstream
                     │                │
Anthropic Messages --┘                ├── state
                                      ├── memory injection
                                      ├── observability
                                      └── cache / experiment metadata
```

## Major components

### 1. Intermediate representation

Location: `src/ir/schema.py`

The IR is the central contract. It separates protocol-specific syntax from conversation semantics and allows conversion behavior to be tested without requiring live provider calls.

See `docs/ir-schema.md` for the detailed schema.

### 2. Protocol adapters

Location: `src/adapters/`

Adapters are responsible for:

- parsing source-provider requests into IR
- rendering IR into target-provider payloads
- converting provider responses back into the expected caller protocol
- recording unsupported or dropped parameters
- preserving token and cache-accounting semantics where possible

A key design rule is that lossy behavior should be observable rather than silently hidden.

### 3. State layer

Location: `src/state/store.py`

The SQLite-backed state layer tracks conversation state such as:

- session metadata
- TTL-related state
- `previous_response_id`
- memory-cap configuration
- replay / session-boundary state

Local generated databases are runtime state and are excluded from version control.

### 4. Gateway

Location: `src/gateway/server.py`

The gateway is the request path that combines adapters, state, memory injection, and upstream forwarding.

Responsibilities include:

- HTTP request handling
- provider translation
- streaming / SSE pass-through
- concurrency limits
- warm-up requests
- explicit rejection of unsupported warm-up combinations
- memory injection and deduplication
- provider-specific error mapping

### 5. Memory injection

Memory is treated as a stateful prefix-management problem rather than an unconditional string append.

Important constraints include:

- deterministic serialization where cache stability matters
- bounded per-session memory
- explicit session-boundary configuration
- deduplication
- observability of injected tokens

### 6. Cache observability

Location: `src/observability/metrics.py`

The project exposes measurements intended to make cross-provider cache behavior inspectable:

- cache hit / read behavior
- injected tokens
- replay tokens
- dropped parameters
- degradation path
- cost-estimation inputs

Because OpenAI and Anthropic report cached tokens differently, accounting is normalized before comparison.

### 7. Experiments

Location: `src/experiments/run_experiments.py`

The experiment layer studies how memory placement, update frequency, chunking, and provider cache thresholds affect hit behavior and estimated cost.

The current repository includes controlled offline/mock experiments. Real-provider validation is tracked separately and should not be conflated with mock results.

## Request lifecycle

```text
Client Request
     │
     ▼
Source Adapter
     │
     ▼
Intermediate Representation
     │
     ├── session lookup
     ├── memory policy
     ├── replay policy
     └── observability context
     │
     ▼
Target Adapter
     │
     ▼
Provider / Mock Upstream
     │
     ▼
Provider Response
     │
     ▼
Response Adapter
     │
     ├── metrics update
     ├── state update
     └── degradation metadata
     │
     ▼
Caller-compatible Response
```

## Design invariants

### IR is the central contract

Provider-specific logic should not leak across adapters when it can be represented in the IR or explicit metadata.

### Lossy conversion must be visible

If a target protocol cannot express a source feature, the implementation should record a dropped parameter or degradation path where practical.

### Session semantics are part of correctness

Protocol conversion is not only JSON field mapping. Stateful semantics such as `previous_response_id`, replay boundaries, and memory lifetime affect correctness.

### Cache semantics are provider-specific

Cache thresholds, TTL behavior, accounting, and breakpoints differ by provider. The project keeps these differences explicit rather than pretending there is one universal cache model.

### Offline and real-provider evidence are distinct

Mock experiments are useful for controlled behavioral validation, but they are not presented as proof of current production-provider behavior.

## Testing boundaries

The automated suite currently covers:

- adapter conversion behavior
- core state / metrics behavior
- gateway flows in mock mode

CI runs the test suite on Python 3.11 and 3.12.

Provider-contract drift, live rate limits, pricing changes, and undocumented behavior require separate real-provider validation.
