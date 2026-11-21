#!/usr/bin/env bash
# S6-04 / VER-12: WSL2 (Ubuntu) acceptance script for Tier 1 Windows support.
# Run inside a WSL2 Linux filesystem checkout (e.g. ~/src/Virgo, NOT /mnt/c/...).
set -euo pipefail

if ! grep -qi microsoft /proc/version 2>/dev/null; then
  echo "Warning: /proc/version does not indicate WSL2; continuing as native Linux check." >&2
fi

case "$(pwd -P)" in
  /mnt/[a-z]/*)
    echo "ERROR: Workspace is under $(pwd -P) (DrvFS / Windows mount)." >&2
    echo "Move the repository inside the WSL2 Linux filesystem (e.g. ~/src/Virgo) before running acceptance." >&2
    exit 2
    ;;
esac

command -v bwrap >/dev/null || { echo "ERROR: bubblewrap (bwrap) is required: sudo apt-get install -y bubblewrap" >&2; exit 2; }
command -v uv >/dev/null || { echo "ERROR: uv is required in PATH" >&2; exit 2; }

echo "==> Installing locked dev dependencies"
uv sync --locked --dev

echo "==> Running required Linux native sandbox & policy acceptance suites (VER-01, VER-06, VER-12)"
VIRGO_REQUIRE_SANDBOX_TESTS=1 uv run --no-sync pytest -q \
  tests/harness/test_sandbox_e2e.py \
  tests/harness/test_policy_v2.py \
  tests/harness/test_safety_v2.py \
  tests/harness/test_output_store.py

echo "==> Running realistic-scale cold-start and Read budget check in Workspace-Edit (VER-11 / S5-04)"
uv run --no-sync python ci/measure_permission_modes.py \
  --mode workspace-edit --samples 2 --repeat 2 --read-repeats 10 \
  --realistic-files 1000 --check-budgets

echo "==> WSL2 acceptance passed"
