# Permission profiles and prompt workflows

This change separates task instructions from execution permissions. Plan and Code Review use
the normal Runtime and registered tools; their prompts request planning/review without implementing.
Only execution permissions establish an enforced filesystem/network boundary.

## Permission contract

User-owned presets contain a stable ID, editable name, and three booleans: `readOnly`,
`workspaceOnly`, `networkAllowed`. Read-only protects host file data, not remote services.
Private scratch and broker session/output records remain writable. Workspace scope includes
user-data reads and writes, with documented system/runtime dependencies. Model transport and
authentication are broker communication, not task network access. Trusted in-process Python
extensions and MCP service startup are outside the OS sandbox. Offline invocation gating does
not prevent an already trusted MCP service from connecting autonomously. Unsupported backend
combinations fail before launching.

Each run captures a policy; children and background work keep their source workspace and policy.
Selecting/editing a mode affects subsequent work, never claims to revoke a running process.
A permission request may grant only one exact operation in its originating run. It cannot change
the selected preset, override a user deny, authorize another agent, survive policy edits/revocation,
or be restored from historical messages. Only managed file, Shell, Web and MCP operations can
receive an exception; spawning a child or starting a workflow cannot grant authority to its
future operations. Approvals are rechecked before execution. Failed or
partially executed commands are never automatically replayed.

## Failure modes and acceptance

- A tool visible to Plan/Review may still fail under the execution policy. Check identical normal
  tool schemas under each workflow and prove write denial through the execution path.
- A workspace escape through a direct file tool, link, subprocess, background job or child must
  receive the source policy; changing cwd or selected conversation cannot expand access.
- Offline policy must cover managed Shell, Web/native provider search and remote MCP invocation;
  no-limit filesystem plus offline must not accidentally disable the network boundary.
- An approval for one operation cannot authorize changed arguments, another call/runtime, or
  a second execution. Explicit deny, policy revision, revocation and cancellation invalidate it.
- Distinguish preflight rejection, unavailable isolation, process-control failure and an ordinary
  command error. Model-visible results and TUI must share the same bounded evidence, including
  whether the payload started and whether side effects are known.
- Keep exact current-head cross-platform/native gates. A simulated failure or skipped test is
  not evidence of native enforcement. Existing E2E scenarios are updated for changed contracts;
  focused new scenarios cover newly expressible combinations and single-operation exceptions.
- In compact terminals and approval panels all three permission values remain visible; replay
  preserves actual event order. A cancelled/incomplete review never appears as a successful
  review with zero findings.
