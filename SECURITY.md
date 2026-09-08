# Security

Protocol Converter sits between clients and model-provider APIs, so protocol translation, secrets, session state, and local web tooling are security-sensitive surfaces.

## Supported security assumptions

This repository is a research / engineering prototype. It is not presented as a hardened multi-tenant production gateway.

The current design includes safeguards such as:

- local web-panel binding to `127.0.0.1`
- randomized local port support
- Host validation
- one-time token use for local session initialization
- memory redaction / sanitization paths
- API keys sourced from environment variables rather than committed configuration
- local `.env` files excluded from version control
- generated SQLite session state excluded from version control

## Sensitive data

Do not commit:

- OpenAI / Anthropic API keys
- `.env` files
- local session databases
- raw private conversation memory
- private provider responses containing personal or confidential data
- generated traces that contain credentials or authorization headers

Use `.env.example` only as a template.

## Threat model notes

Important areas for review include:

1. **Protocol confusion** — unsupported fields must not silently change meaning.
2. **Tool / identifier mapping** — source and target tool IDs must remain correctly associated.
3. **Session mix-up** — conversation state must not cross session boundaries.
4. **Memory injection** — injected memory should be bounded, deduplicated, and treated as untrusted context when appropriate.
5. **Local web panel** — local-only assumptions should not be interpreted as internet-facing security guarantees.
6. **Logging** — secrets and sensitive content should not be written to logs by default.
7. **Upstream error handling** — provider errors should not leak authorization headers or internal state.
8. **Cache semantics** — cache keys and prefixes may contain sensitive prompt content even when the provider abstracts storage details.

## Reporting a vulnerability

Please avoid publishing exploitable details in a public issue before a fix is available. Contact the repository owner through GitHub first with:

- affected component
- reproduction conditions
- security impact
- minimal proof of concept
- suggested mitigation, if known

## Scope disclaimer

A passing test suite demonstrates expected behavior for covered cases; it does not constitute a security audit or production-readiness certification.
