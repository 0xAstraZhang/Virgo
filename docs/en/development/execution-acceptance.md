# Permission mode acceptance

Accept a revision after reviewing the strict native JUnit results, full-suite comparison and timing artifacts for that same Git commit. `Native sandbox boundaries` covers Linux and macOS. `Windows sandbox boundaries` covers Windows Server 2022 and 2025. Check the executed cases and require zero errors, failures and skips in the strict suites. `Cross-platform CI` compares the complete suites with the PR base.

## Performance

`Permission mode performance` runs a paired workload on each Linux, macOS and Windows runner. It installs each revision's locked environment, runs both sides sequentially, and uploads the raw samples for both sides. The comparison verifies the commit IDs, script digest, platform, Python version, workload, clean tracked sources, approval count and sample count. It reports medians, millisecond differences and ratios for each completed pair.

| Mode | Baseline | Workload |
| --- | --- | --- |
| No-Limit | Host at `98cc711cdb9bbb80a9ad9a2ae11c8d92f94ceec8` | Edit with Virgo, run the test |
| Workspace-Edit | Native Workspace-Edit at `6346255146f11b8024e11fd3f3faae19f41d5862` | Edit with Virgo, run the test |
| Read-Only | Offline, workspace-scoped read-only native mode at `6346255146f11b8024e11fd3f3faae19f41d5862` | An outside editor saves the change, Virgo runs the test |

Each of the three samples starts a new interpreter and an in-memory session in a fresh workspace and user home. Cold start measures interpreter startup, module import and session construction, excluding fixture creation. The first short command is recorded separately. The next five give the total duration for consecutive short commands. The edit-to-test timer includes saving the changed source and running a test that imports and asserts its new value. Shells and file tools run through a normal agent session with a local scripted model. The read-only pair keeps the editor outside Virgo's authority. All successful workloads require zero resource prompts.

Run the head workload in the checkout being measured:

```sh
uv run --no-sync python ci/measure_startup.py --mode workspace-edit --samples 3 --repeat 5 --output timing.json
```

The workflow runs this same script from the head checkout against each baseline's installed package. It checkpoints samples after each worker and retains failures. Unconfirmed cleanup stops further samples. The comparison issues timings only when every sample completed. Runner timing, native isolation acceptance and web or network reachability have separate evidence.

## Evidence scope

Review POSIX live-workspace support against the documented host concurrency assumptions. Track Windows external TCP, inbound, loopback and ordinary desktop-account results individually. CI running on Windows Server establishes the cases exercised under that runner account.

The archived MXC reports, model evaluation and older timing samples describe the revisions recorded in each file. The model and provider-comparison experiments are outside the timing workflow. [Historical execution evidence](../../evidence/execution/README.md)
