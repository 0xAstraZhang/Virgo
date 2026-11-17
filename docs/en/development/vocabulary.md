# Execution vocabulary

| Term | Meaning |
| --- | --- |
| execution provider | `host` or explicitly selected `sandbox`; `execution.provider` / `--execution` |
| resource preset | Stable ID and editable name with `readPaths`, `writePaths`, `networkAllowed` |
| resource increment | One exact additional-resource operation; never a provider switch |
| approval policy | `ask|never` controls whether Sandbox can request increments |
| execution context | Immutable source workspace, provider, resources and policy revision |
| requested boundary | User-selected resource policy |
| effective boundary | Resources actually enforced, with establishment state and evidence |
| plugin | Optional capability selected with `plugins.<id>.enabled` |

Host is the fresh default. `/permissions show` reports execution state. Sandbox owns preset selection and one-use grants; children inherit the captured context. Task instructions express intent and cannot certify isolation. See [migration](../guides/migrating-execution-policy.md) for retired names and [acceptance](execution-acceptance.md) for platform blockers.
