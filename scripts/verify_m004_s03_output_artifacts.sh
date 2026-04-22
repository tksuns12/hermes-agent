#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

PHASE_TIMEOUT_SECONDS="${VERIFY_PHASE_TIMEOUT_SECONDS:-900}"
TIMEOUT_BIN="$(command -v timeout || true)"

run_phase() {
  local phase="$1"
  shift

  local start_ts=$SECONDS
  echo "==> phase=${phase}"
  echo "    command: $*"

  set +e
  if [[ -n "$TIMEOUT_BIN" ]]; then
    "$TIMEOUT_BIN" --foreground "${PHASE_TIMEOUT_SECONDS}" "$@"
  else
    "$@"
  fi
  local exit_code=$?
  set -e

  local duration=$((SECONDS - start_ts))
  if [[ $exit_code -eq 0 ]]; then
    echo "✅ phase=${phase} status=pass duration=${duration}s"
    return 0
  fi

  if [[ $exit_code -eq 124 ]]; then
    echo "❌ phase=${phase} status=timeout duration=${duration}s" >&2
    echo "   hint: verification phase exceeded timeout budget" >&2
  else
    echo "❌ phase=${phase} status=fail exit_code=${exit_code} duration=${duration}s" >&2
  fi

  return "$exit_code"
}

run_phase "frontend.outcome-contract-test" \
  npm --prefix web run test -- --run \
  web/src/features/end-user/documentOutcomeContract.test.ts

run_phase "frontend.runtime-output-artifacts-test" \
  npm --prefix web run test -- --run \
  web/src/pages/EndUserWorkspacePage.runtime.test.tsx

run_phase "gateway.output-provenance-download-regressions" \
  bash -lc "source venv/bin/activate && env HERMES_TEST_WORKERS=1 scripts/run_tests.sh tests/gateway/test_api_server.py -k 'responses_stage_explicit_output_files_and_download or output_file_download_is_tenant_isolated or responses_upload_to_download_flow or chat_completions_upload_to_download_flow or run_events_stage_output_files_with_source_run_id'"

run_phase "frontend.production-build" npm --prefix web run build

echo "✅ verification bundle complete: m004/s03 output artifacts + boundary states"
