# Leaf functional requirements

[Documentation](../README.md) · [Axiomatic Design](axiomatic-design.md) · [Acceptance evidence](functional-acceptance.md) · [简体中文](../../zh-CN/development/functional-requirements.md)

Given the selected layered coding-agent runtime (DP0), the catalog decomposes FR0 into observable outcomes organized by eight functional domains A–H. The domains are navigation groups, not eight independently adjustable FR–DP pairs or a proven axiomatic hierarchy. Leaf IDs identify outcomes, not implementation packages: moving a feature between Core, Plugin and Extension does not change its FR. Each principal DP describes a design mechanism or policy, with implementation names as locators where useful. The [design analysis](axiomatic-design.md) defines constraints and the sparse impact matrix; the [acceptance map](functional-acceptance.md) links requirements to executable checks.

Split outcomes where design choices or acceptance boundaries differ, not down to every API or function. Principal DP labels do not imply independent mechanisms: search and fetch share capability planning and a Web-call cap, and tasks, compaction and children share a model-call cap. Task success on arbitrary model responses belongs to FR0, not the orchestration protocol. Structural preservation of history does not establish semantic recall.

## A — User interaction

| Leaf FR | Required outcome | Principal DP | Acceptance boundary |
| --- | --- | --- | --- |
| **FR-A.1** | Display conversation progress and results in the selected interactive interface | **DP-A.1:** TUI/REPL rendering, transcript projection and event observation | Stream and terminal results remain associated with the correct run; view changes preserve conversation state |
| **FR-A.2** | Return usable results to a noninteractive caller | **DP-A.2:** Print/text/JSON output adapters | Output uses the requested format; concurrent JSON records are complete and parseable |
| **FR-A.3** | Route commands and ordinary user text to the intended operation | **DP-A.3:** Command parsing and session/runtime facade methods | Valid commands reach their handler; steering/follow-up text is not misclassified; invalid commands preserve the active session |
| **FR-A.4** | Collect explicit decisions through an input adapter with declared capabilities | **DP-A.4:** Declared confirmation/input capabilities and adapter selection (`UI`) | With the same request and answer, supported adapters preserve the approval result; missing confirmation capability prevents interactive escalation |
| **FR-A.5** | Present supported interface text in the selected language | **DP-A.5:** Session-scoped translator and locale resources | Tool/help/approval text uses the selected locale with matching placeholders; reply-language guidance still follows the user's request |
| **FR-A.6** | Ask for a decision and distinguish an answer from refusal, timeout or cancellation | **DP-A.6:** `ask_user` options, interaction state and timeout policy | Answers preserve user intent; noninteractive operation reports no answer; cancelled or unattended questions do not fabricate a decision |

## B — Model interaction

| Leaf FR | Required outcome | Principal DP | Acceptance boundary |
| --- | --- | --- | --- |
| **FR-B.1** | Receive normalized streamed text, reasoning, tool calls, and terminal outcomes | **DP-B.1:** Protocol-specific adaptation and stream normalization (`ai/providers/*`) | Equivalent protocol fixtures produce the expected message and stop/error classifications; cancellation stops consumption |
| **FR-B.2** | Use the intended account without borrowing another provider's credentials | **DP-B.2:** Credential lookup, OAuth/API authentication, endpoint binding | Account/endpoint selection uses matching credentials; rejected credentials do not silently select another identity |
| **FR-B.3** | Select an available model and supported reasoning effort | **DP-B.3:** Catalog discovery/cache, model selection and supported-effort mapping (`ModelRegistry`) | The selected model/effort reaches the request; failed selection preserves the active configuration |
| **FR-B.4** | Expose model failures in a form the runtime can handle | **DP-B.4:** Provider error classification and normalized diagnostic metadata | Retryable, overflow, unsupported-capability, fatal, and cancelled outcomes retain their distinct meanings |

## C — Operations and information acquisition

| Leaf FR | Required outcome | Principal DP | Acceptance boundary |
| --- | --- | --- | --- |
| **FR-C.1** | Read and locate intended file content | **DP-C.1:** Broker read/search, path scope and matching rules (`tools/fs.py`, `file_access.py`) | The intended content or matches are returned; denied reads and searches do not expose out-of-scope content |
| **FR-C.2** | Modify intended files without unintended changes | **DP-C.2:** Broker write/edit, path admission, unique matching and atomic replacement (`tools/fs.py`, `file_access.py`) | The intended content changes; denied or ambiguous edits leave files intact |
| **FR-C.3** | Run a command and observe its completion, failure, or cancellation | **DP-C.3:** Shell selection, execution context, process supervision | The result describes actual execution; cancellation and cleanup follow the selected process contract |
| **FR-C.4** | Retrieve large outputs without unbounded model/UI payloads | **DP-C.4:** Output byte limits, reference storage, paging/search and retention (`OutputStore`) | Bounded references are readable by the originating session; pages preserve valid UTF-8 and access scope |
| **FR-C.5** | Invoke configured external services and report connection/dispatch outcomes | **DP-C.5:** MCP transport, configuration binding, tool schemas and result projection | Configured services expose valid tools; unavailable services do not stop unrelated startup; results preserve origin and output bounds |
| **FR-C.6** | Search for information using available authorized capabilities | **DP-C.6:** Search capability selection, provider/client planning and rejection-triggered fallback (`plugins/web`, `ai/web.py`, `tools/web.py`) | Search is available or explicitly unavailable; a fallback does not duplicate already-started hosted work; applicable limits are enforced |
| **FR-C.7** | Retrieve content using available authorized capabilities | **DP-C.7:** Fetch capability selection, provider/client planning and rejection-triggered fallback (`plugins/web`, `tools/web_fetch.py`) | Fetch is available or explicitly unavailable; a fallback does not duplicate already-started hosted work; applicable limits are enforced |

## D — Authorization and trust control

| Leaf FR | Required outcome | Principal DP | Acceptance boundary |
| --- | --- | --- | --- |
| **FR-D.1** | Permit only resources allowed by the effective policy | **DP-D.1:** Deny-first resource policy, effective-mode selection and context binding (`PermissionPolicy`) | Allowed operations succeed; denied file/network operations produce explicit outcomes without widening the mode |
| **FR-D.2** | Constrain model-started arbitrary commands on supported hosts | **DP-D.2:** Native Shell backend and process-launch policy | Native host suites establish the claimed filesystem/network boundary; Broker fixtures do not |
| **FR-D.3** | Obtain and apply resource-specific user authorization | **DP-D.3:** Approval/grant scope, persistence and revision binding | Approval covers the requested operation/resources; refusal, changed arguments, and unrelated tasks cannot reuse it |
| **FR-D.4** | Require explicit authorization for high-risk operations when required by policy | **DP-D.4:** Risk classification, policy-controlled confirmation and one-operation scope (`harness/high_risk.py`, `harness/execution.py`) | Refusal prevents dispatch; accepting one operation does not authorize the next |
| **FR-D.5** | Handle an unavailable native execution boundary without silent escalation or unsafe replay | **DP-D.5:** Startup-state classification and explicit one-shot Host fallback policy (`harness/execution.py`) | Unavailable boundary fails closed unless an eligible one-shot choice is accepted; session mode is unchanged; a possibly started payload is not replayed |
| **FR-D.6** | Keep project-controlled executable configuration within the user's trust choice | **DP-D.6:** Settings filtering, project trust and MCP configuration fingerprints | Untrusted project hooks, MCP servers, providers, extensions, and the `shell`, `search` and `fetch` settings cannot override the corresponding user settings or gain executable authority |

## E — Task orchestration and control

| Leaf FR | Required outcome | Principal DP | Acceptance boundary |
| --- | --- | --- | --- |
| **FR-E.1** | Coordinate a multi-step model/tool interaction, carrying valid decisions and results to the next permitted request or explicit terminal outcome | **DP-E.1:** Tool-call validation, scheduling, result ordering and loop termination rules (`agent/loop.py`, `Runtime`) | Invalid calls return errors without dispatch; concurrent completion preserves logical call order; permitted steps reach the next request or explicit terminal outcome. Arbitrary task success belongs to FR0 |
| **FR-E.2** | Deliver steering and follow-up input at the appropriate execution boundary | **DP-E.2:** Steering/follow-up queues and delivery precedence (`agent/`) | Queued user input reaches the next permitted boundary without being misclassified or silently dropped |
| **FR-E.3** | Cancel active and queued work according to the runtime cancellation contract | **DP-E.3:** Cancellation propagation, worker interruption and cleanup coordination (`agent/`, `ChildCancellation`) | Cancellation prevents further dispatch and observes the applicable worker/child lifecycle; child abort does not cancel its parent |
| **FR-E.4** | Enforce configured limits on model requests and assistant turns | **DP-E.4:** Shared request-budget scope and atomic consumption, runtime turn-limit enforcement (`RequestBudget`, Agent loop) | Exhausted limits prevent further work; task, auxiliary and child requests share the configured call cap |
| **FR-E.5** | Retry or switch models only under the declared recovery conditions | **DP-E.5:** Failure classification-driven retry count/wait budget, fallback model and core replay guard (`Runtime`) | Retryable failures respect limits; possible hosted execution blocks unsafe automatic replay |
| **FR-E.6** | Run and reuse a child task with its own conversation and explicit inherited limits | **DP-E.6:** Child conversation isolation, role/tool selection, state reuse and inherited limits (`SubagentStore`, child runtime) | Child history remains isolated, reusable state survives resume, and inherited permission/budget/cancellation limits remain effective |
| **FR-E.7** | Inspect, cancel, and collect asynchronous work in the correct conversation | **DP-E.7:** Job lifecycle, cancellation/result access and origin-routed notifications (`JobManager`) | Jobs reach terminal states; result lookup respects ownership; completion is delivered to the originating session |
| **FR-E.8** | Maintain an accurate task checklist across conversation boundaries | **DP-E.8:** Checklist validation, successful-result journal and compaction-epoch projection (Todo) | Valid updates replace the list; invalid updates leave it intact; incomplete items survive compaction and resume. The checklist does not schedule work |

## F — Conversation and context management

| Leaf FR | Required outcome | Principal DP | Acceptance boundary |
| --- | --- | --- | --- |
| **FR-F.1** | Recover saved conversation history and branches without silent loss | **DP-F.1:** Locked append-only JSONL branch journal, commit and tail-repair rules (`SessionStore`) | Resume restores the active branch; incomplete-tail repair preserves valid records; authoritative save failure blocks continuation and retains unsaved history |
| **FR-F.2** | Attribute asynchronous work and evidence to the originating conversation | **DP-F.2:** Identity and branch capture at task start, origin propagation and routing (`ExecutionOrigin`, `BranchSource`) | Switching the visible session does not transfer a child's results, permissions, or evidence to another owner |
| **FR-F.3** | Supply the applicable instructions and current tool/context contract to the model | **DP-F.3:** Role-based prompt composition, project-instruction selection and request preparation | Role-specific instructions reach requests; ordinary turns retain append-only prefixes; permission changes update the applicable boundary |
| **FR-F.4** | Keep long conversations usable within the model's context limits | **DP-F.4:** Trigger/target budgets, tail selection, pruning and portable summary strategy (`CompactionController`) | Tool pairs and retained tail remain valid; successful checkpoints survive resume; cancellation/failure does not replace history with an incomplete summary. Fact retention and task quality require separate model evaluations |
| **FR-F.5** | Discover and apply the intended skill instructions | **DP-F.5:** Skill discovery, precedence and explicit invocation | Selected instructions reach the request without reading resources outside the task's allowed scope |

## G — Observability and diagnostics

| Leaf FR | Required outcome | Principal DP | Acceptance boundary |
| --- | --- | --- | --- |
| **FR-G.1** | Trace the effective configuration of recorded model requests | **DP-G.1:** Pre-dispatch request headers and auxiliary request metadata (`Runtime`, `harness/telemetry.py`) | Headers and auxiliary records reflect the effective model, protocol, effort and applicable output limit |
| **FR-G.2** | Aggregate provider-reported usage in the intended accounting scope | **DP-G.2:** Per-call usage records, attribution and branch aggregation (`harness/telemetry.py`, `SessionStore`) | Retry/auxiliary/child usage is counted once in the intended scope; this is not billing reconciliation |
| **FR-G.3** | Capture useful failure identity and causes while limiting private data | **DP-G.3:** Capture, redaction, field allowlists and size limits | Error IDs/causes survive; credentials and excluded payloads do not enter projected evidence |
| **FR-G.4** | Retain inspectable diagnostics independently of session persistence | **DP-G.4:** Diagnostic database, deduplication and emergency reporting | Saved records remain queryable; diagnostic write/report failures retain the original failure through the fallback path rather than corrupting session history |
| **FR-G.5** | Retrieve only evidence granted to the current runtime through an available query tool | **DP-G.5:** Runtime-scoped grants, paging and selection from declared query capabilities (`DiagnosticAccess`) | Foreign error IDs remain unavailable; new query references use a declared tool in the runtime tool snapshot; absent query capability is reported without inventing a tool. Arbitrary later request mutation is outside this guarantee |
| **FR-G.6** | Provide relevant evidence at existing request boundaries and after compaction | **DP-G.6:** Pending evidence, checkpoint projection and delivery timing | Evidence reaches the owning runtime; a reported fault alone does not start another model turn; checkpoints retain usable query references |

## H — Extensibility

| Leaf FR | Required outcome | Principal DP | Acceptance boundary |
| --- | --- | --- | --- |
| **FR-H.1** | Add configured tools and event behavior with isolated loading failures | **DP-H.1:** Registration ownership, precedence and setup rollback (`ExtensionAPI`) | Failed setup retracts owned registrations; other features and core startup remain usable. This does not assert isolation of every runtime callback |

## Using the catalog

For a functional-impact claim, name the leaf FR, the changed DP, the fixed conditions, and the observable acceptance result. Tests of one fixture establish that bounded case, not independence from every possible DP. Shared budgets remain explicit constraints on concurrent requirements; this catalog does not allocate a separate budget merely to remove a matrix entry.

See [functional acceptance](functional-acceptance.md) for representative tests, concrete controlled variations, and limits on the resulting claims.
