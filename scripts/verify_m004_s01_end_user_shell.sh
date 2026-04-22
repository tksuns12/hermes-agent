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
    echo "   hint: possible hang in same-origin route/proxy regression path" >&2
  else
    echo "❌ phase=${phase} status=fail exit_code=${exit_code} duration=${duration}s" >&2
  fi

  return "$exit_code"
}

run_phase "frontend.runtime-regressions" \
  npm --prefix web run test -- --run \
  web/src/App.end-user-shell.test.tsx \
  web/src/pages/EndUserWorkspacePage.runtime.test.tsx

run_phase "frontend.production-build" npm --prefix web run build

run_phase "python.same-origin-proxy-regressions" \
  env HERMES_TEST_WORKERS=1 scripts/run_tests.sh tests/hermes_cli/test_web_server.py -k \
  "root_and_workbench_paths_serve_spa_shell or unknown_spa_route_serves_index_html or generic_unknown_api_route_fails_closed_with_json_detail or workbench_unknown_api_route_fails_closed_with_correlation or workbench_unknown_api_route_never_serves_spa_html or workbench_runs_proxy_translates_upstream_5xx or workbench_files_proxy_handles_unreachable_and_malformed_upstream"

echo "✅ verification bundle complete: m004/s01 end-user shell + runtime path"
