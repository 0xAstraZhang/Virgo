#!/usr/bin/env bash
# WSL2 (Ubuntu) acceptance script for Tier 1 Windows support.
# Run inside a WSL2 Linux filesystem checkout (e.g. ~/src/Virgo, NOT /mnt/c/...).
set -euo pipefail

allow_native_linux=0
for arg in "$@"; do
  case "$arg" in
    --allow-native-linux)
      allow_native_linux=1
      ;;
    *)
      echo "ERROR: Unknown argument: $arg (supported: --allow-native-linux)" >&2
      exit 2
      ;;
  esac
done

if ! grep -qi microsoft /proc/version 2>/dev/null; then
  if [ "$allow_native_linux" -ne 1 ]; then
    echo "ERROR: /proc/version does not indicate WSL2. Re-run with --allow-native-linux on native Linux." >&2
    exit 2
  fi
  echo "Warning: /proc/version does not indicate WSL2; continuing with --allow-native-linux." >&2
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

junit_xml="$(mktemp -t virgo-wsl2-acceptance-XXXXXX.xml)"
trap 'rm -f "$junit_xml"' EXIT

echo "==> Installing locked dev dependencies"
uv sync --locked --dev

echo "==> Running required Linux native sandbox & policy acceptance suites"
VIRGO_REQUIRE_SANDBOX_TESTS=1 uv run --no-sync pytest -q \
  --junitxml="$junit_xml" \
  tests/harness/test_sandbox_e2e.py \
  tests/harness/test_policy_boundaries.py \
  tests/harness/test_guardrails.py \
  tests/harness/test_output_store.py

uv run --no-sync python ci/check_native_report.py "$junit_xml"

echo "==> Running realistic-scale cold-start and Read budget check in Workspace-Edit"
uv run --no-sync python ci/measure_permission_modes.py \
  --mode workspace-edit --samples 2 --repeat 2 --read-repeats 10 \
  --realistic-files 1000 --check-budgets

echo "==> WSL2 acceptance passed"
