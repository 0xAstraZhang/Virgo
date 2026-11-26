# Leaf functional requirements

[Documentation](../README.md) · [Axiomatic Design](axiomatic-design.md) · [Acceptance evidence](functional-acceptance.md) · [简体中文](../../zh-CN/development/functional-requirements.md)

The FR1–FR9 groups below decompose into observable outcomes. A leaf ID identifies a requirement, not a Python package or an assertion of independence. Each corresponding DP describes a mechanism or policy that can be selected or varied. File paths locate implementations; the [acceptance map](functional-acceptance.md) identifies representative executable checks and their limits.

Acceptance statements describe required behavior. In particular, structural preservation of a compacted history does not establish semantic recall or task-success rates on arbitrary model responses.

## FR1 — Model interaction

| Leaf FR | Required outcome | Corresponding DP | Acceptance boundary |
| --- | --- | --- | --- |
| **FR1.1** | Receive normalized streamed text, reasoning, tool calls, and terminal outcomes | **DP1.1:** Provider adapters and stream normalization in `ai/providers/*` | Equivalent protocol fixtures produce the expected message and stop/error classifications; cancellation stops consumption |
| **FR1.2** | Use the intended account without borrowing another provider's credentials | **DP1.2:** Credential lookup, OAuth/API authentication, endpoint binding | Account/endpoint selection uses matching credentials; rejected credentials do not silently select another identity |
| **FR1.3** | Select an available model and supported reasoning effort | **DP1.3:** `ModelRegistry`, catalog discovery/cache, effort mapping | The selected model/effort reaches the request; failed selection preserves the active configuration |
| **FR1.4** | Expose model failures in a form the runtime can handle | **DP1.4:** Provider error classification and normalized diagnostic metadata | Retryable, overflow, unsupported-capability, fatal, and cancelled outcomes retain their distinct meanings |

## FR2 — Authorization and resource boundaries

| Leaf FR | Required outcome | Corresponding DP | Acceptance boundary |
| --- | --- | --- | --- |
| **FR2.1** | Permit only resources allowed by the effective policy | **DP2.1:** Deny-first `PermissionPolicy`, execution-mode selection and bound context | Allowed operations succeed; denied file/network operations produce explicit outcomes without widening the mode |
| **FR2.2** | Constrain model-started arbitrary commands on supported hosts | **DP2.2:** Native Shell backend and process-launch policy | Native host suites establish the claimed filesystem/network boundary; Broker fixtures do not |
| **FR2.3** | Obtain and apply resource-specific user authorization | **DP2.3:** Approval/grant scope, persistence and revision binding | Approval covers the requested operation/resources; refusal, changed arguments, and unrelated tasks cannot reuse it |
| **FR2.4** | Require an explicit decision for high-risk commands and unavailable native boundaries | **DP2.4:** Risk classification and one-operation confirmation | Refusal prevents dispatch; one-shot permission does not change session mode or replay a possibly started payload |
| **FR2.5** | Keep project-controlled executable configuration within the user's trust choice | **DP2.5:** Settings filtering, project trust and MCP configuration fingerprints | Untrusted project hooks/providers/extensions cannot override trusted settings or gain executable authority |

## FR3 — Files, commands, and results

| Leaf FR | Required outcome | Corresponding DP | Acceptance boundary |
| --- | --- | --- | --- |
| **FR3.1** | Read, locate, and modify the intended files | **DP3.1:** Broker file tools, path policy, matching and atomic replacement | The intended content changes or is returned; scope violations and ambiguous edits leave files intact |
| **FR3.2** | Run a command and observe its completion, failure, or cancellation | **DP3.2:** Shell selection, execution context, process supervision | The result describes actual execution; cancellation and cleanup follow the selected process contract |
| **FR3.3** | Retrieve large outputs without unbounded model/UI payloads | **DP3.3:** `OutputStore`, byte limits, paging/search and retention | Bounded references are readable by the originating session; pages preserve valid UTF-8 and access scope |

## FR4 — Task progression and control

| Leaf FR | Required outcome | Corresponding DP | Acceptance boundary |
| --- | --- | --- | --- |
| **FR4.1** | Advance from model decisions through tool results to a final response | **DP4.1:** Agent loop, schema validation and tool scheduling | Invalid calls return errors without dispatch; concurrent completion does not corrupt logical call order |
| **FR4.2** | Honor steering, cancellation and configured work limits | **DP4.2:** Steering/follow-up queues, turn limits, shared `RequestBudget` | User input reaches the next allowed boundary; cancellation and exhausted budgets prevent further work |
| **FR4.3** | Retry or switch models only under the declared recovery conditions | **DP4.3:** Retry count/wait budget, fallback model and replay guards | Retryable failures respect limits; possible hosted execution blocks unsafe automatic replay |

## FR5 — State, ownership, context, and accounting

These outcomes have different acceptance criteria and are not one adjustable “session/compaction” function.

| Leaf FR | Required outcome | Corresponding DP | Acceptance boundary |
| --- | --- | --- | --- |
| **FR5.1** | Recover saved conversation history and branches without silent loss | **DP5.1:** `SessionStore` record format, locking and tail-repair rules | Resume restores the active branch; incomplete-tail repair preserves valid records; authoritative save failure blocks continuation and retains unsaved history |
| **FR5.2** | Attribute asynchronous work and evidence to the originating conversation | **DP5.2:** `ExecutionOrigin`/`BranchSource` capture and routing | Switching the visible session does not transfer a child's results, permissions, or evidence to another owner |
| **FR5.3** | Supply the applicable instructions and current tool/context contract to the model | **DP5.3:** Prompt composition, project-instruction selection and request preparers | Role-specific instructions reach requests; ordinary turns retain append-only prefixes; permission changes update the applicable boundary |
| **FR5.4** | Keep long conversations usable within the model's context limits | **DP5.4:** Compaction trigger/target budgets, tail selection, pruning and summary strategy | Tool pairs and retained tail remain valid; successful checkpoints survive resume; cancellation/failure does not replace history with an incomplete summary. Fact retention and task quality require separate model evaluations |
| **FR5.5** | Trace recorded request configuration and aggregate reported usage correctly | **DP5.5:** Request headers, auxiliary-call records and branch usage aggregation | Headers reflect effective request settings; retry/auxiliary/child usage is counted once in its intended accounting scope. This is not billing reconciliation |

## FR6 — Failure evidence

Storing evidence, authorizing its retrieval, and delivering it as model input are separate functions. Evidence delivery may intentionally affect later decisions; recording failures must not manufacture new execution results.

| Leaf FR | Required outcome | Corresponding DP | Acceptance boundary |
| --- | --- | --- | --- |
| **FR6.1** | Capture useful failure identity and causes while limiting private data | **DP6.1:** Capture, redaction, field allowlists and size limits | Error IDs/causes survive; credentials and excluded payloads do not enter projected evidence |
| **FR6.2** | Retain inspectable diagnostics independently of session persistence | **DP6.2:** Diagnostic database, deduplication and emergency reporting | Saved records remain queryable; diagnostic write/report failures retain the original failure through the fallback path rather than corrupting session history |
| **FR6.3** | Retrieve only evidence granted to the current runtime through an available query tool | **DP6.3:** `DiagnosticAccess` scope/paging and declared query-tool selection | Foreign error IDs remain unavailable; every advertised query uses a declared tool currently exposed to that runtime; absent query capability is reported without inventing a tool |
| **FR6.4** | Provide relevant evidence at existing request boundaries and after compaction | **DP6.4:** Pending evidence, checkpoint projection and delivery timing | Evidence reaches the owning runtime; a reported fault alone does not start another model turn; checkpoints retain usable query references |

## FR7 — Delegation and background work

| Leaf FR | Required outcome | Corresponding DP | Acceptance boundary |
| --- | --- | --- | --- |
| **FR7.1** | Run and reuse a child task with its own conversation and explicit inherited limits | **DP7.1:** Child runtime, `SubagentStore`, role/tool selection, turn/effort settings | Child history remains isolated, reusable state survives resume, and inherited permission/budget/cancellation limits remain effective |
| **FR7.2** | Inspect, cancel, and collect asynchronous work in the correct conversation | **DP7.2:** `JobManager` lifecycle, job tools and origin-routed notification | Jobs reach terminal states; result lookup respects ownership; completion is delivered to the originating session |

## FR8 — Added capabilities

Extension registration is the means of adding behavior. The features it installs have their own functional outcomes.

| Leaf FR | Required outcome | Corresponding DP | Acceptance boundary |
| --- | --- | --- | --- |
| **FR8.1** | Add configured tools and event behavior with isolated loading failures | **DP8.1:** `ExtensionAPI`, registry ownership, precedence and setup rollback | Failed setup retracts owned registrations; other features and core startup remain usable |
| **FR8.2** | Discover and apply the intended skill instructions | **DP8.2:** Skill discovery, precedence and explicit invocation | Selected instructions reach the request without reading resources outside the task's allowed scope |
| **FR8.3** | Invoke configured external services and report connection/dispatch outcomes | **DP8.3:** MCP transport, configuration binding, tool schemas and result projection | Configured services expose valid tools; unavailable services do not stop unrelated startup; results preserve origin and output bounds |
| **FR8.4** | Maintain an accurate task checklist across conversation boundaries | **DP8.4:** Todo validation, stored updates and compaction-epoch projection | Valid updates replace the list; invalid updates leave it intact; incomplete items survive compaction and resume. The checklist does not schedule work |
| **FR8.5** | Search or fetch information using available authorized capabilities | **DP8.5:** Hosted/client capability planning, provider limits and fallback policy | Requested capability is available or explicitly unavailable; a fallback does not duplicate already-started hosted work |
| **FR8.6** | Ask for a decision and distinguish an answer from refusal, timeout or cancellation | **DP8.6:** `ask_user` options, interaction state and timeout policy | Answers preserve user intent; noninteractive operation reports no answer; cancelled or unattended questions do not fabricate a decision |

## FR9 — User and programmatic interfaces

Rendering is distinct from collecting decisions. A replacement adapter can preserve an input contract even when its layout changes; removing the ability to answer changes the available functionality.

| Leaf FR | Required outcome | Corresponding DP | Acceptance boundary |
| --- | --- | --- | --- |
| **FR9.1** | Display conversation progress and results in the selected interactive interface | **DP9.1:** TUI/REPL rendering, transcript projection and event observation | Stream and terminal results remain associated with the correct run; view changes preserve conversation state |
| **FR9.2** | Return usable results to a noninteractive caller | **DP9.2:** Print/text/JSON output adapters | Output uses the requested format; concurrent JSON records are complete and parseable |
| **FR9.3** | Route commands and ordinary user text to the intended operation | **DP9.3:** Command parsing and session/runtime facade methods | Valid commands reach their handler; steering/follow-up text is not misclassified; invalid commands preserve the active session |
| **FR9.4** | Collect explicit decisions through an input adapter with declared capabilities | **DP9.4:** `UI` confirmation/input contract and adapter selection | With the same request and answer, supported adapters preserve the approval result; missing confirmation capability prevents interactive escalation |
| **FR9.5** | Present supported interface text in the selected language | **DP9.5:** Session-scoped translator and locale resources | Tool/help/approval text uses the selected locale with matching placeholders; reply-language guidance still follows the user's request |

## Using the catalog

For a functional-impact claim, name the leaf FR, the changed DP, the fixed conditions, and the observable acceptance result. Tests of one fixture establish that bounded case, not independence from every possible DP. Shared budgets remain explicit constraints on concurrent requirements; this catalog does not allocate a separate budget merely to remove a matrix entry.

See [functional acceptance](functional-acceptance.md) for representative tests, concrete controlled variations, and limits on the resulting claims.
