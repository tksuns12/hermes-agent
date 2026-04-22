#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

PHASE_TIMEOUT_SECONDS="${VERIFY_PHASE_TIMEOUT_SECONDS:-900}"
TIMEOUT_BIN="$(command -v timeout || true)"

FIXTURE_DIR_REL="${M004_S04_FIXTURE_DIR:-tmp/m004-s04-office-samples}"
FIXTURE_DIR="$REPO_ROOT/$FIXTURE_DIR_REL"
DOCX_PATH="$FIXTURE_DIR/m004-s04-representative.docx"
XLSX_PATH="$FIXTURE_DIR/m004-s04-representative.xlsx"

API_E2E_BASE_URL="${API_E2E_BASE_URL:-}"
API_E2E_TENANT="${API_E2E_TENANT:-e2e-office-live}"
LIVE_BASE_DISPLAY="${API_E2E_BASE_URL:-<skipped; set API_E2E_BASE_URL for live preflight>}"

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
    echo "   hint: timed out while verifying live browser preflight prerequisites" >&2
  else
    echo "❌ phase=${phase} status=fail exit_code=${exit_code} duration=${duration}s" >&2
  fi

  return "$exit_code"
}

run_phase "fixtures.generate" \
  "$REPO_ROOT/venv/bin/python" scripts/generate_m004_s04_office_samples.py --output-dir "$FIXTURE_DIR_REL"

run_phase "fixtures.validate" \
  "$REPO_ROOT/venv/bin/python" -c '
import sys
from pathlib import Path

docx = Path(sys.argv[1])
xlsx = Path(sys.argv[2])
for path in (docx, xlsx):
    if not path.exists():
        raise SystemExit(f"missing fixture: {path}")
    data = path.read_bytes()
    if len(data) == 0:
        raise SystemExit(f"empty fixture: {path}")
    if not data.startswith(b"PK"):
        raise SystemExit(f"not zip/ooxml fixture: {path}")
' "$DOCX_PATH" "$XLSX_PATH"

run_phase "frontend.guided-task-contract" \
  npm --prefix web run test -- --run \
  web/src/features/end-user/guidedDocumentTasks.test.ts

run_phase "frontend.outcome-contract" \
  npm --prefix web run test -- --run \
  web/src/features/end-user/documentOutcomeContract.test.ts

run_phase "frontend.runtime-regression" \
  npm --prefix web run test -- --run \
  web/src/pages/EndUserWorkspacePage.runtime.test.tsx

run_phase "python.same-origin-proxy-regressions" \
  env PATH="$REPO_ROOT/venv/bin:$PATH" HERMES_TEST_WORKERS=1 \
  scripts/run_tests.sh tests/hermes_cli/test_web_server.py -k \
  "ignores_gateway_health_url_for_workbench_proxy or workbench_files_list_reports_incompatible_upstream_route or workbench_files_metadata_and_content_proxy or workbench_file_content_proxy_handles_upstream_denial"

if [[ -n "$API_E2E_BASE_URL" ]]; then
  run_phase "runtime.live-api-route-preflight" \
    "$REPO_ROOT/venv/bin/python" -c '
import json
import sys
import urllib.error
import urllib.request

base = sys.argv[1].rstrip("/")
probe_urls = [
    ("health", f"{base}/health", {200, 401, 403}),
    ("models", f"{base}/v1/models", {200, 401, 403}),
    ("files", f"{base}/v1/files?user_id=live-proof-probe", {200, 400, 401, 403, 422, 429}),
]
for name, url, allowed in probe_urls:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            status = resp.status
            body = resp.read(200).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = (exc.read(200) if hasattr(exc, "read") else b"").decode("utf-8", errors="replace")
    except Exception as exc:
        raise SystemExit(f"[phase:runtime.live-api-route-preflight] unreachable url={url} details={exc}")

    if status == 404:
        raise SystemExit(
            "[phase:runtime.live-api-route-preflight] incompatible upstream route "
            + json.dumps({"probe": name, "url": url, "status": status, "body": body})
        )
    if status not in allowed:
        raise SystemExit(
            "[phase:runtime.live-api-route-preflight] unexpected status "
            + json.dumps({"probe": name, "url": url, "status": status, "body": body})
        )
' "$API_E2E_BASE_URL"

  run_phase "python.live-office-api-preflight" \
    env PATH="$REPO_ROOT/venv/bin:$PATH" HERMES_TEST_WORKERS=1 API_E2E_BASE_URL="$API_E2E_BASE_URL" API_E2E_TENANT="$API_E2E_TENANT" \
    scripts/run_tests.sh tests/e2e/test_document_workspace_live_api.py
else
  echo "⚠️ phase=runtime.live-api-route-preflight status=skipped reason=API_E2E_BASE_URL not set"
  echo "⚠️ phase=python.live-office-api-preflight status=skipped reason=API_E2E_BASE_URL not set"
  echo "   hint: export API_E2E_BASE_URL=http://127.0.0.1:8642 before browser UAT to enforce live runtime checks"
fi

run_phase "frontend.production-build" npm --prefix web run build

echo
printf 'Generated fixtures:\n  DOCX: %s\n  XLSX: %s\n' "$FIXTURE_DIR_REL/m004-s04-representative.docx" "$FIXTURE_DIR_REL/m004-s04-representative.xlsx"
printf 'Live endpoints preflight target: %s\n' "$LIVE_BASE_DISPLAY"

echo
cat <<'EOF'
Browser UAT handoff (same-origin live proof):

1) Start/confirm runtime processes:
   - API server (port 8642):
     source venv/bin/activate && API_SERVER_ENABLED=true API_SERVER_HOST=127.0.0.1 API_SERVER_PORT=8642 python -m hermes_cli.main gateway
   - Dashboard (port 9119):
     source venv/bin/activate && API_SERVER_HOST=127.0.0.1 API_SERVER_PORT=8642 python -m hermes_cli.main dashboard --port 9119 --no-open

2) Open http://127.0.0.1:9119/ and confirm:
   - End-user shell renders at '/' (not operator-only '/workbench').
   - Guided-first tasks are visible and freeform fallback stays visible.
   - Upload both generated fixtures and run representative DOCX + XLSX guided tasks.

3) For each representative run, capture one of:
   - Output card with filename + source_run_id badge + successful download, OR
   - Explicit honest boundary panel (partial_success|unsupported|no_output) with explanation.

4) If output exists, verify browser/network evidence includes:
   - Download activity row, and
   - Successful same-origin request to /api/workbench/files/{id}/content.

5) Record request IDs from runtime surfaces:
   - files_request_id, upload_request_id, run_request_id, stream_request_id.
EOF

echo "✅ verification bundle complete: m004/s04 same-origin live browser proof"
