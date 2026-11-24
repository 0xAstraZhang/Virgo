# Architectural decisions

[Documentation](../README.md) · [Project home](../../../README.md) · [简体中文](../../zh-CN/decisions/README.md)

This directory records **landed architectural decisions** that impose lasting constraints or tradeoffs on future work in Virgo. Process notes, transient milestones, and discarded proposals do not belong here.

| ID | Decision | Core constraint established |
| --- | --- | --- |
| [001](001-guardrail-threat-model.md) | Guardrail-not-fortress threat model | Bound benign model mistakes with `<= 5s` cold start and O(1) tool latency; confirm `.virgo` and high-risk commands instead of chasing adversarial containment |
| [002](002-execution-boundaries-and-broker-tools.md) | Layered execution boundaries and Broker file tools | Enforce `PermissionPolicy` directly in Broker file tools, OS-sandbox Shell only, run trusted integrations on Host, capture immutable task contexts, and fail closed without replay |
| [003](003-native-sandbox-backends.md) | Tier 1 native sandbox backends and platform tradeoffs | Use simple native mechanisms (`bubblewrap`, direct Seatbelt + `HostProcess`, unelevated Windows Standard AppContainer, WSL2) and document honest platform limits |
| [004](004-untrusted-project-config-isolation.md) | Untrusted project configuration isolation and approval binding | Ban project execution overrides, strip untrusted project active configs before merge, reject symlinked project prompt files, and bind MCP approvals to config fingerprints |
| [005](005-runtime-layering-and-durable-state.md) | Runtime layering, extensibility boundaries, and durable state | Keep unidirectional package layers, separate Core tools / plugins / lazy built-in extensions, own job supervision in Core, and isolate durable local stores |
