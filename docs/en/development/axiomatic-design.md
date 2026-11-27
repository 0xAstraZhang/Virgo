# Functional requirements and Axiomatic Design

[Documentation](../README.md) · [Architecture](architecture.md) · [简体中文](../../zh-CN/development/axiomatic-design.md)

This overview separates functional requirements (FRs), design parameters (DPs), and constraints (Cs). The [leaf requirement catalog](functional-requirements.md) defines observable outcomes and their implementation choices; [functional acceptance](functional-acceptance.md) links those outcomes and cross-functional effects to executable evidence.

## Scope and definitions

- **CA0 — Customer need:** A lightweight, extensible terminal agent that helps explore and modify software, execute commands, and continue substantial tasks across supported platforms.
- **FR0 — Overall function:** Collaboratively complete software-engineering tasks while respecting the user's authorization and resource limits and retaining the state needed to continue.
- **DP0 — Overall implementation:** The layered coding-agent runtime assembled by `AgentSession`.

An FR states **what result is required**; a DP identifies **how that result is produced and what may be changed**. For example, recovering a conversation is an FR; a locked, append-only JSONL tree is a DP. A constraint bounds acceptable solutions. Permission enforcement is itself a function, while its threat model, supported platforms, and overhead limits constrain its implementation. Safety or performance labels alone do not determine whether an item is an FR or a constraint.

The conceptual design relation is $\{FR\}=[A]\{DP\}$. A nonzero $A_{ij}$ means that varying $DP_j$, within a specified range and operating scenario, affects the required outcome of $FR_i$. A module import, callback, or request/response exchange is evidence to investigate; it is not automatically such a functional effect.

## Product constraints and design invariants

- **C1 — Guardrail threat model:** Bound occasional mistakes by a well-aligned model; malicious-code containment, deliberate evasion, and prompt-injection attack chains are outside this boundary ([ADR 001](../decisions/001-guardrail-threat-model.md)).
- **C2 — Latency and scaling budgets:** Cold start plus first Shell command `<= 5s`, repeated short Shell command `<= 2s` with `O(1)` scaling, and Broker `read` `<= 250ms` with `O(1)` scaling on Tier 1 hosts; unconfigured optional SDKs add no cold-start import cost. These are acceptance targets, not consequences of a dependency check ([execution acceptance](execution-acceptance.md)).
- **C3 — Platforms:** Linux, macOS, unelevated supported Windows 11, and Windows + WSL2 with Python 3.11+; native guarantees require validation on the corresponding host ([ADR 003](../decisions/003-native-sandbox-backends.md)).
- **C4 — Configured work limits:** Model requests and assistant turns must respect configured caps, including shared model-call and Web-call limits where applicable. The configured cap is a constraint; the counting, scope and admission mechanisms are DPs. Independent unlimited auxiliary budgets would change the resource contract.

- **I1 — Execution correctness invariant:** An unavailable native boundary fails closed. An interactive one-shot No-Limit choice does not change the session mode. A payload that may have started is never automatically replayed ([ADR 002](../decisions/002-execution-boundaries-and-broker-tools.md)).
- **I2 — Failure-domain invariant:** Plugin loading, diagnostic reporting, and presentation failures must preserve the original operation outcome and session state. Failure of the authoritative session store intentionally stops further work; it is not treated as an optional logging failure ([ADR 005](../decisions/005-runtime-layering-and-durable-state.md)).
- **I3 — Internal constraint of the current cache design:** Ordinary turns append history; system instructions and retained sections stay frozen between compaction boundaries ([ADR 006](../decisions/006-three-layer-provider-agnostic-compaction.md)).

I1 and I2 are required correctness contracts; their implementations may vary without relaxing those contracts. I3 is conditional on the chosen cache strategy: the product goals are latency, cost and context continuity, not a particular prefix format. Do not treat an internal design constraint as an immutable customer need.

## Functional decomposition

The top-level goal FR0 is realized by the selected model/tool-loop runtime DP0. Its [leaf catalog](functional-requirements.md) organizes 43 observable outcomes into eight functional domains: A user interaction, B model interaction, C operations and information acquisition, D authorization and trust control, E task orchestration and control, F conversation and context management, G observability and diagnostics, and H extensibility.

This is a functional catalog with a top-level design choice, not a claim that the eight headings are independent intermediate FR–DP pairs. Strict further axiomatic decomposition would require an explicit DP at each parent and a justified derivation of its children (zigzagging between the functional and design domains). Do not infer such a hierarchy from the package tree. Model/tool orchestration describes protocol behavior rather than arbitrary end-to-end task success; the latter is evaluated at FR0. Capability installation is separate from the outcomes of installed features. The leaf catalog is the authority for IDs, required outcomes, principal mechanisms and acceptance boundaries.

## Two different matrices

### Static import DSM

[`test_architecture_matrix.py`](../../../tests/test_architecture_matrix.py) extracts AST `Import` and `ImportFrom` statements, including top-level, function-local, and `TYPE_CHECKING` statements. It checks module acyclicity, runtime package acyclicity and layer ordering, selected peer import boundaries, and specific cross-boundary access patterns. Removing self-edges and topologically ordering an acyclic graph yields a strictly lower-triangular DSM.

This scope excludes dynamic loading such as `importlib.import_module`, callbacks, shared mutable state, and competition for request budgets. For example, [`harness/extensions.py`](../../../src/virgo/harness/extensions.py) dynamically loads configured plugins. The DSM gate is an implementation-structure check; it neither covers every runtime dependency nor proves FR independence.

### Sparse leaf FR–DP impact matrix

Use the 43 leaf IDs from the catalog as row and column labels. Each matching FR–DP pair is marked `X` (principal design relationship, not a measured derivative); the following table is the sparse representation of investigated off-diagonal entries. It records observable outcomes asserted by existing tests, not proof of a numerical sensitivity matrix or a claim that a particular revision has passed. A change of setting, replacement within a contract, or injected failure is bounded to its stated scenario. This also distinguishes changes in an allowed outcome (such as stopping at a resource cap) from violations of the required protocol.

- `N`: evidence-supported nonzero effect required by an existing constraint or functional contract.
- `U`: evidence-supported nonzero, unnecessary effect that violates or needlessly constrains another requirement. None is established by the controlled checks in this table.
- `0*`: bounded invariance for the stated substitution or fault; not a universal zero.
- `?`: unknown. Every off-diagonal entry not listed is unknown; a possible mechanism or mere data/control interaction is not proof of an effect.

See [functional acceptance](functional-acceptance.md) for the test scenarios and limits of their assertions.

| Affected FR | Varied DP | Entry | Bounded variation / result | Existing test |
| --- | --- | :---: | --- | --- |
| FR-E.1 | DP-E.4 | N | At the low compaction threshold, varying the call cap changes whether the next ordinary request is dispatched. | `test_auto_compaction_and_task_share_request_budget` |
| FR-E.1 | DP-F.4 | N | With a fixed cap of two, varying the compaction trigger changes whether the task or summary consumes the remaining call. | `test_auto_compaction_and_task_share_request_budget` |
| FR-F.4 | DP-E.4 | N | With a summary due, varying the cap changes whether a summary checkpoint is produced. | `test_auto_compaction_and_task_share_request_budget` |
| FR-E.1 | DP-F.1 | N | Injected authoritative history write failure blocks further requests while preserving unsaved history (fault scenario). | `test_failed_save_stops_requests_and_keeps_history` |
| FR-F.3 | DP-D.1 | N | Changing the mode and restoring it changes the write boundary in the same request context. | `test_permission_switch_updates_requests_and_restores_the_original_prefix` |
| FR-H.1 | DP-D.6 | N | The same project extension executes only with the explicit external trust choice. | `test_project_extensions_require_trust_outside_the_project` |
| FR-D.3 | DP-A.4 | 0* | Fix the request, workspace, policy and y/n answers; replace the recording input adapter with TerminalUI. | `test_direct_file_approval_persists_in_session_and_preserves_the_mode` |
| FR-G.5 | DP-H.1 | 0* | Rename the registered and declared diagnostic query tool; the new reference is followed successfully. | `test_renamed_query_reference_can_be_followed_by_the_model` |
| FR-G.6 | DP-H.1 | 0* | Rename the declared query tool; pending and checkpoint references use the new name. | `test_pending_evidence_uses_the_declared_query_name`; `test_compaction_checkpoint_uses_the_declared_query_name` |
| FR-G.4 | DP-H.1 | 0* | Disable the lookup plugin; the diagnostic log remains available. | `test_model_diagnostics_feature_can_be_disabled_without_disabling_logs` |
| FR-G.4 | DP-A.1 | 0* | Inject failure in UI printing of a diagnostic; the persisted original error remains identifiable and deduplicated. | `test_failing_ui_does_not_destroy_original_error_or_recurse` |

For reordering, orient a nonzero off-diagonal entry from the matched DP node to the affected FR node. Both `N` and `U` count as edges; do not discard required effects to obtain a diagonal. The six supported nonzero entries above form the edges E.4 → E.1, F.4 → E.1, E.4 → F.4, F.1 → E.1, D.1 → F.3, and D.6 → H.1. All other entries are omitted from this **known-edge graph**, not assumed zero in the full matrix.

The known graph has no directed cycle (all strongly connected components are singletons). With the matched diagonal fixed, one valid simultaneous FR/DP ordering is all leaf IDs in catalog order except E.1, F.3, F.4 and H.1, followed by F.3, F.4, H.1, and E.1. The known nonzero support is then lower-triangular, not diagonal. This does not establish that the real 43×43 matrix is triangular: unknown entries may add cycles, and the principal mechanisms may share design choices. Bounded invariance does not fill unknown cells with zeros. Nor does an intentional change in an allowed outcome imply a violation of the corresponding FR.

Coarse domain aggregation would merge distinct mechanisms and can produce apparent reciprocal dependencies, for example task progression and compaction under a shared budget. It is not a substitute for leaf-level variation, so no domain-level functional matrix is assigned. Preserve required shared budgets, explicit authorization, origin binding and fail-closed authoritative persistence. Investigate unknown or unnecessary effects using concrete acceptance boundaries before changing implementation; do not create abstractions, independent budgets or additional stores merely to remove entries.

## Independence and information axioms

A diagonal FR–DP matrix is **uncoupled**. A matrix that can be reordered into triangular form is **decoupled**: the DPs must be set in the corresponding order. Neither property follows from tool execution order or the absence of circular imports. Functional independence concerns outcomes, not physical separation of components.

The information axiom compares the probability of meeting specified functional ranges: $I=-\log_2 P(\text{all required FR ranges are met})$. Code size, number of interfaces, and number of matrix entries are not this information content. Joint success probabilities cannot be calculated by multiplying marginal rates without justified independence. The current structural and deterministic behavior checks do not estimate this probability or establish a numeric Axiomatic Design score.

References: [MIT Axiomatic Design Group definitions](https://web.mit.edu/axiom/www/introduction.shtml), [Suh's introduction](https://web.mit.edu/2.882/www/chapter1/chapter1.htm), and [MIT research on leaf-level decomposition and re-sequencing](https://web.mit.edu/pccs/pub/2003/melvin-manufacturing.pdf).

See also: [Leaf requirements](functional-requirements.md) · [Functional acceptance](functional-acceptance.md) · [Architecture](architecture.md).
