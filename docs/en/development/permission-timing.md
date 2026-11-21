# Permission mode timing

The `Permission mode performance` workflow runs on Linux (`ubuntu-latest`), macOS (`macos-latest`), and native Windows (`windows-latest`) across `no-limit`, `read-only`, and `workspace-edit`. On **all three platforms**, it enforces realistic-scale (`0` vs `1000` files) cold-start and first-command (`<= 5s`), O(1) repeat-command (`<= 2s` and bounded scaling), and `Read` (`<= 250ms` and O(1) scaling) budgets via `--check-budgets`. A local scripted model drives Virgo's real tools without any external network calls.

Each sample starts a fresh interpreter with an isolated user directory and workspace, confirms zero approval prompts, and records import time, session construction time, `Read` latency, first Shell command latency, repeated short Shell command latency, and edit-to-test latency. Failures retain diagnostics; unconfirmed worker cleanup stops remaining samples immediately.

Run the budget check locally after `uv sync --locked --dev`:

```sh
uv run --no-sync python ci/measure_permission_modes.py \
  --mode workspace-edit --samples 2 --repeat 2 --read-repeats 10 \
  --realistic-files 1000 --check-budgets --output budget.json
```

Native process and network boundary guarantees are enforced separately by the native sandbox acceptance suites (`native-sandbox.yml`, `windows-sandbox.yml`, and `ci/wsl2_acceptance.sh`).
