# Permission mode timing

The `Permission mode performance` workflow compares each PR head with main commit [`04e94ed`](https://github.com/0xAstraZhang/Virgo/commit/04e94ed45992d8e2cbf2178c8c6b562704092a83) on Linux, macOS and Windows. Both revisions run sequentially on one runner with their locked dependencies. A local scripted model calls Virgo's real tools.

| Mode | Configuration at the main baseline | Source edit |
| --- | --- | --- |
| No-Limit | Original No-Limit preset | Virgo's file tool |
| Workspace-Edit | Original Workspace-Edit preset | Virgo's file tool |
| Read-Only | Original Read-Only preset with networking disabled | External editor |

Each revision runs three samples. Each starts a new interpreter with a fresh project and a user directory that does not exist yet. The project contains an `AGENTS.md` marker which must appear in the constructed session's system prompt before the sample proceeds. A rejected or omitted automatic-context read therefore cannot count as a successful cold start. The raw sample records this check as `project_context_loaded`, and the comparison requires it for both revisions.

The report gives median cold start, first Shell command, five consecutive short commands, and elapsed time from editing a source file to running a test that imports the new value. Every completed sample must have zero approval prompts. Samples with failures retain their diagnostics. Unconfirmed cleanup stops further samples. The comparison checks source revisions, tracked changes, script digest, platform, Python version and workload before producing ratios. The gate validates comparable successful work; it does not impose a latency threshold or establish the absence of a performance regression.

Run the workload from the checkout being measured after `uv sync --locked --dev`.

```sh
uv run --no-sync python ci/measure_permission_modes.py --mode workspace-edit --samples 3 --repeat 5 --output timing.json
```

When measuring the pinned main checkout, invoke the same script from the PR checkout with `--contract original`. Each Read-Only job also uses the PR's web tool to retrieve a pinned HTTPS file and checks its content digest while the Shell network policy is disabled. The native acceptance workflows separately exercise actual Shell socket denial. The native acceptance workflows separately test the process boundary.
