# Execution providers and resource presets

Core calls the neutral interfaces in `virgo.execution`. It has an in-process Host provider; the required Sandbox provider loads only after explicit selection. Providers have one owner and bind before Runtime construction. Optional trusted extension failure handling cannot turn required Sandbox failure into Host execution.

A Runtime captures an immutable context containing its source workspace, provider and resource policy. Source binding, argument validation, cancellation and output limits remain Core responsibilities. File editing/search logic remains in Core; file access and process launch belong to the provider. A process handle supports wait, cancellation and cleanup.

`execution.provider` defaults to `host`. Host has no resource approval controls. `permissions.approval=ask|never` only controls requests for additional Sandbox resources; it does not classify commands.

Sandbox presets have this shape:

```json
{
  "execution": {"provider": "sandbox"},
  "sandbox": {"executable": "/absolute/path/to/mxc-exec-mac"},
  "permissions": {
    "preset": "offline",
    "approval": "ask",
    "presets": {
      "offline": {"name": "Offline", "readPaths": ["."], "writePaths": ["."], "networkAllowed": false}
    }
  }
}
```

This config does **not** certify native support. Current shared-path native policies fail closed; see [acceptance](execution-acceptance.md). Broker file protection and process isolation require separate evidence. Runtime dependencies and unmanaged activities are reported separately from user resource grants.

User settings and startup overrides supply authority. Project settings cannot select Host, change resource grants or alter extension trust. The loader preserves source information and rejects explicit legacy isolation requirements without an explicit Sandbox selection. See [migration](../guides/migrating-execution-policy.md) for retired keys and APIs.

A task captures its resource and approval policy together at admission. A queued child takes that context at submission. Its own `approval_policy="never"` disables requests for it and its descendants. A session-wide change to `never` also blocks new requests from already running tasks.

Preset selection validates the candidate against the conversation's workspace before publishing it. An idle conversation adopts it immediately. A busy conversation uses it for its next run. A failed validation or save preserves the earlier selection and outstanding approvals. `/permissions show` reports the current preset, the next preset and native Shell availability.

A resource grant is not a reusable session approval. It is consumed once, after final argument validation and before dispatch. Its identity includes caller/run, tool instance, arguments, cwd, environment hash, policy revision, canonical resource names and existing resource identities. The TTL limits new dispatch, not the lifetime of an already launched process. Audit entries never reconstruct live grants.
