# Functional requirements and Axiomatic Design

[Documentation](../README.md) · [Architecture](architecture.md) · [简体中文](../../zh-CN/development/axiomatic-design.md)

This overview separates functional requirements (FRs), design parameters (DPs), and constraints (Cs). The [leaf requirement catalog](functional-requirements.md) defines observable outcomes and their implementation choices; [functional acceptance](functional-acceptance.md) links those outcomes and cross-functional effects to executable evidence.

## Scope and definitions

- **CA0 — Customer need:** A lightweight, extensible terminal agent that helps explore and modify software, execute commands, and continue substantial tasks across supported platforms.
- **FR0 — Overall function:** Collaboratively complete software-engineering tasks while respecting the user's authorization and resource limits and retaining the state needed to continue.
- **DP0 — Overall implementation:** The layered coding-agent runtime assembled by `AgentSession`.

An FR states **what result is required**; a DP identifies **how that result is produced and what may be changed**. For example, recovering a conversation is an FR; a locked, append-only JSONL tree is a DP. A constraint bounds acceptable solutions. Permission enforcement is itself a function, while its threat model, supported platforms, and overhead limits constrain its implementation. Safety or performance labels alone do not determine whether an item is an FR or a constraint.

The conceptual design relation is $\{FR\}=[A]\{DP\}$. A nonzero $A_{ij}$ means that varying $DP_j$, within a specified range and operating scenario, affects the required outcome of $FR_i$. A module import, callback, or request/response exchange is evidence to investigate; it is not automatically such a functional effect.

## System constraints

- **C1 — Guardrail threat model:** Bound occasional mistakes by a well-aligned model; malicious-code containment, deliberate evasion, and prompt-injection attack chains are outside this boundary ([ADR 001](../decisions/001-guardrail-threat-model.md)).
- **C2 — Latency and scaling budgets:** Cold start plus first Shell command `<= 5s`, repeated short Shell command `<= 2s` with `O(1)` scaling, and Broker `read` `<= 250ms` with `O(1)` scaling on Tier 1 hosts; unconfigured optional SDKs add no cold-start import cost. These are acceptance targets, not consequences of a dependency check ([execution acceptance](execution-acceptance.md)).
- **C3 — Platforms:** Linux, macOS, unelevated supported Windows 11, and Windows + WSL2 with Python 3.11+; native guarantees require validation on the corresponding host ([ADR 003](../decisions/003-native-sandbox-backends.md)).
- **C4 — Explicit execution failure:** An unavailable native boundary fails closed. An interactive one-shot No-Limit choice does not change the session mode. A payload that may have started is never automatically replayed ([ADR 002](../decisions/002-execution-boundaries-and-broker-tools.md)).
- **C5 — Failure domains:** Plugin loading, diagnostic reporting, and presentation failures must preserve the original operation outcome and session state. Failure of the authoritative session store intentionally stops further work; it is not treated as an optional logging failure ([ADR 005](../decisions/005-runtime-layering-and-durable-state.md)).
- **C6 — Prompt-cache prefix stability:** Ordinary turns append history; system instructions and retained sections stay frozen between compaction boundaries ([ADR 006](../decisions/006-three-layer-provider-agnostic-compaction.md)).

## Level-1 requirement groups

These groups retain the FR1–FR9 numbering used by the macro matrix. They are navigation groups, not a claim that each group is a single independently adjustable function. The leaf catalog separates state recovery from compression, evidence storage from delivery, extension registration from feature behavior, and presentation from decision input.

| Group | Required outcome | Principal implementation group |
| --- | --- | --- |
| **FR1** | Obtain usable, normalized model interaction using the chosen model and account | **DP1:** `ai/*`, `harness/model_registry` |
| **FR2** | Admit operations according to authorization, resource boundaries, and project trust | **DP2:** `execution_policy`, execution/sandbox components, settings and approvals |
| **FR3** | Read and modify files, run commands, and retrieve bounded results | **DP3:** file/Shell tools, process supervision, `OutputStore` |
| **FR4** | Advance multi-step tasks and honor user steering, cancellation, and budgets | **DP4:** `agent/*`, `Runtime`, retry and request-budget policy |
| **FR5** | Preserve recoverable state, correct ownership, usable request context, and accounting | **DP5:** session/origin stores, prompt assembly, compaction, telemetry |
| **FR6** | Preserve, query, and deliver relevant failure evidence without replacing the original outcome | **DP6:** capture/redaction, diagnostic storage, scoped access and delivery |
| **FR7** | Delegate, inspect, cancel, and collect child or background work | **DP7:** child runtimes, `SubagentStore`, `JobManager` |
| **FR8** | Add configured capabilities and use skills, external services, checklists, web access, and clarification | **DP8:** extension registration and the individual feature implementations |
| **FR9** | Enter work, make decisions, and inspect progress through terminal or programmatic interfaces | **DP9:** presentation adapters, input/command contracts, localization |

## Two different matrices

### Static import DSM

[`test_architecture_matrix.py`](../../../tests/test_architecture_matrix.py) extracts AST `Import` and `ImportFrom` statements, including top-level, function-local, and `TYPE_CHECKING` statements. It checks module acyclicity, runtime package acyclicity and layer ordering, selected peer import boundaries, and specific cross-boundary access patterns. Removing self-edges and topologically ordering an acyclic graph yields a strictly lower-triangular DSM.

This scope excludes dynamic loading such as `importlib.import_module`, callbacks, shared mutable state, and competition for request budgets. For example, [`harness/extensions.py`](../../../src/virgo/harness/extensions.py) dynamically loads configured plugins. The DSM gate is an implementation-structure check; it neither covers every runtime dependency nor proves FR independence.

### Macro relationship table

The table below records the nine implementation groups' declared relationships. It is a **candidate functional-impact model**, not a measured sensitivity matrix: some entries describe interactions whose functional effect still needs a scenario and acceptance criterion. `X` marks the principal implementation; `x` and `⊗` mark relationships below and above the diagonal. `·` means no relationship is marked here, **not a behaviorally established zero**. The [acceptance evidence register](functional-acceptance.md) distinguishes supported effects, bounded invariance, and unverified claims.

| FR \ DP | DP1 | DP2 | DP3 | DP4 | DP5 | DP6 | DP7 | DP8 | DP9 |
| --- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **FR1** Model interaction | X | ⊗ | · | ⊗ | · | · | · | · | · |
| **FR2** Authorization and trust | · | X | ⊗ | · | ⊗ | · | · | ⊗ | ⊗ |
| **FR3** Files and commands | · | x | X | · | · | · | · | · | · |
| **FR4** Task progression | x | x | x | X | ⊗ | ⊗ | · | ⊗ | · |
| **FR5** State and context | x | x | x | x | X | ⊗ | · | ⊗ | · |
| **FR6** Failure evidence | x | x | x | x | x | X | · | · | · |
| **FR7** Delegation and jobs | x | x | x | x | x | x | X | · | · |
| **FR8** Added capabilities | x | x | x | x | x | · | x | X | ⊗ |
| **FR9** User interaction | x | x | x | x | x | x | x | x | X |

Treating every marked relationship as nonzero gives **54 entries: 9 diagonal, 33 below, and 12 above**. This table is not lower-triangular, and reordering cannot make it triangular: every row has at least two marked entries, whereas the first row of a lower-triangular matrix can contain at most one. With each FR–DP pair as a node and each off-diagonal $A_{ij}$ as an edge $j\rightarrow i$, all nine nodes form one strongly connected component.

If all those entries are accepted as functional effects, the resulting design is **coupled**, not decoupled. Calling an interaction bounded does not make its matrix entry zero. Grouping a coupled component into a larger diagonal block also does not establish independence inside the block. Conversely, this coarse table alone does not prove that every leaf function is coupled: refine the FRs and allowed DP variations before drawing that conclusion.

## Independence and information axioms

A diagonal FR–DP matrix is **uncoupled**. A matrix that can be reordered into triangular form is **decoupled**: the DPs must be set in the corresponding order. Neither property follows from tool execution order or the absence of circular imports. Functional independence concerns outcomes, not physical separation of components.

The information axiom compares the probability of meeting specified functional ranges: $I=-\log_2 P(\text{all required FR ranges are met})$. Code size, number of interfaces, and number of matrix entries are not this information content. Joint success probabilities cannot be calculated by multiplying marginal rates without justified independence. The current structural and deterministic behavior checks do not estimate this probability or establish a numeric Axiomatic Design score.

References: [MIT Axiomatic Design Group definitions](https://web.mit.edu/axiom/www/introduction.shtml), [Suh's introduction](https://web.mit.edu/2.882/www/chapter1/chapter1.htm), and [MIT research on leaf-level decomposition and re-sequencing](https://web.mit.edu/pccs/pub/2003/melvin-manufacturing.pdf).

See also: [Leaf requirements](functional-requirements.md) · [Functional acceptance](functional-acceptance.md) · [Architecture](architecture.md).
