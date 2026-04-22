"""Live Office API preflight for representative guided DOCX/XLSX flows.

This suite is intentionally black-box and talks to a running API server. It
uses generated Office fixtures (no committed binaries) and fails with explicit
phase labels so browser UAT can quickly distinguish environment blockers from
contract regressions.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import time
from typing import Final, NoReturn, TypeAlias, cast
from zipfile import ZipFile

import httpx
import pytest

from .office_fixture_builder import build_representative_office_fixtures

API_E2E_BASE_URL = os.getenv("API_E2E_BASE_URL")
BASE_URL: Final = API_E2E_BASE_URL or "http://127.0.0.1:8642"
TENANT_PREFIX: Final = os.getenv("API_E2E_TENANT", "e2e-office-live")
API_KEY: Final = os.getenv("API_E2E_API_KEY", "")
MODEL: Final = os.getenv("API_E2E_MODEL", "hermes-agent")

RUN_EVENTS_TIMEOUT_SECONDS: Final = 150.0

DOCX_MIME: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

HONEST_OFFICE_BOUNDARY_INSTRUCTION: Final = (
    "Inspect only the attached Office files, never invent missing content, and clearly state uncertainty "
    "when evidence is incomplete."
)
OFFICE_OUTCOME_ENVELOPE_INSTRUCTION: Final = (
    "Always append exactly one machine-readable outcome envelope at the end of your response using this "
    "exact wrapper: <hermes_office_outcome>{\"status\":\"success|partial_success|unsupported|no_output\","
    "\"explanation\":\"<one-sentence reason>\",\"next_steps\":[\"<optional next step>\"]}"
    "</hermes_office_outcome> Only use the listed status values; choose unsupported or no_output whenever "
    "you cannot safely produce a complete Office artifact."
)
OUTCOME_START: Final = "<hermes_office_outcome>"
OUTCOME_END: Final = "</hermes_office_outcome>"
NON_OUTPUT_BOUNDARY_STATUSES: Final = {"partial_success", "unsupported", "no_output"}

LIVE_ONLY = pytest.mark.skipif(
    not API_E2E_BASE_URL,
    reason="Set API_E2E_BASE_URL to run live Office API preflight tests.",
)

JSONObject: TypeAlias = dict[str, object]


@dataclass(frozen=True)
class LiveOfficeCase:
    id: str
    fixture_key: str
    upload_name: str
    mime_type: str
    task_label: str
    task_instruction: str
    detail: str


LIVE_CASES: tuple[LiveOfficeCase, ...] = (
    LiveOfficeCase(
        id="docx-summary",
        fixture_key="docx",
        upload_name="representative-docx-summary.docx",
        mime_type=DOCX_MIME,
        task_label="Summarize DOCX",
        task_instruction=(
            "Create a clear summary with key points, decisions, and next steps from the attached DOCX file. "
            "Also generate a downloadable markdown artifact named docx-summary-report.md containing that summary. "
            "Write the artifact to /tmp/hermes-docx-summary-report.md, overwrite any previous file at that "
            "path with the current report, append a standalone line exactly FILE: /tmp/hermes-docx-summary-report.md, "
            "and do not mention local filesystem paths anywhere else in the response. If you cannot both produce "
            "the summary and save the markdown artifact, do not claim success; instead use partial_success, "
            "unsupported, or no_output in the required outcome envelope and explain why."
        ),
        detail="Highlight risks, owners, and due dates.",
    ),
    LiveOfficeCase(
        id="xlsx-anomalies",
        fixture_key="xlsx",
        upload_name="representative-xlsx-anomalies.xlsx",
        mime_type=XLSX_MIME,
        task_label="Find XLSX anomalies",
        task_instruction=(
            "Identify anomalies, outliers, and suspicious value patterns in the attached XLSX files, then "
            "suggest likely root causes. Also generate a downloadable CSV artifact named "
            "xlsx-anomalies-export.csv listing anomaly rows and reason codes. Even if you find no anomalies, "
            "still create the CSV artifact with a header row and a single no_anomalies_found row so a "
            "successful run always includes a download. Write the artifact to "
            "/tmp/hermes-xlsx-anomalies-export.csv, overwrite any previous file at that path with the current "
            "CSV export, append a standalone line exactly FILE: /tmp/hermes-xlsx-anomalies-export.csv, and do "
            "not mention local filesystem paths anywhere else in the response. If you cannot both produce the "
            "anomaly analysis and save the CSV artifact, do not claim success; instead use partial_success, "
            "unsupported, or no_output in the required outcome envelope and explain why."
        ),
        detail="Prioritize abrupt week-over-week swings and duplicate outliers.",
    ),
)


def _headers(tenant: str) -> dict[str, str]:
    headers = {"X-Hermes-User-Id": tenant}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    return headers


def _phase_fail(phase: str, message: str) -> NoReturn:
    raise AssertionError(f"[phase:{phase}] {message}")


def _json_object(resp: httpx.Response, *, phase: str) -> JSONObject:
    try:
        payload = resp.json()
    except Exception as exc:
        _phase_fail(
            phase,
            f"Malformed JSON response (status={resp.status_code}): {resp.text[:400]!r} ({exc})",
        )

    if not isinstance(payload, dict):
        _phase_fail(phase, f"Malformed JSON payload type {type(payload)!r}: {payload!r}")
    return cast(JSONObject, payload)


def _compose_guided_prompt(case: LiveOfficeCase, upload_name: str) -> str:
    parts = [
        f'Run the guided task "{case.task_label}" on the attached Office document set.',
        f"Attached files: {upload_name}.",
        case.task_instruction,
        f"User focus detail: {case.detail}",
        HONEST_OFFICE_BOUNDARY_INSTRUCTION,
        OFFICE_OUTCOME_ENVELOPE_INSTRUCTION,
        "Return concise, actionable output and reference which attached file supports each conclusion.",
    ]
    return "\n\n".join(parts)


def _ensure_server_ready(client: httpx.Client, tenant: str) -> None:
    try:
        health = client.get("/health", headers=_headers(tenant), timeout=httpx.Timeout(4.0, connect=2.0))
    except Exception as exc:
        _phase_fail(
            "environment_not_ready",
            f"API server at {BASE_URL} is unreachable. Start the current API server before preflight. details={exc}",
        )

    if health.status_code >= 500:
        _phase_fail(
            "environment_not_ready",
            f"Health endpoint returned status {health.status_code}; server is unhealthy. body={health.text[:300]!r}",
        )

    models = client.get("/v1/models", headers=_headers(tenant), timeout=httpx.Timeout(6.0, connect=2.0))
    if models.status_code != 200:
        _phase_fail(
            "environment_not_ready",
            f"Model catalog check failed with status {models.status_code}. body={models.text[:400]!r}",
        )

    payload = _json_object(models, phase="environment_not_ready")
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        _phase_fail("environment_not_ready", f"Model catalog payload missing data list: {payload}")


def _upload_fixture(
    client: httpx.Client,
    *,
    tenant: str,
    upload_name: str,
    mime_type: str,
    content: bytes,
) -> str:
    response = client.post(
        "/v1/files",
        headers=_headers(tenant),
        files={"file": (upload_name, content, mime_type)},
        data={"purpose": "user_uploads"},
        timeout=httpx.Timeout(25.0, connect=5.0),
    )

    if response.status_code != 201:
        payload = _json_object(response, phase="upload_contract")
        _phase_fail(
            "upload_contract",
            f"Upload failed for {upload_name} (status={response.status_code}). payload={payload}",
        )

    payload = _json_object(response, phase="upload_contract")
    file_id = payload.get("id")
    filename = payload.get("filename")
    if not isinstance(file_id, str) or not file_id:
        _phase_fail("upload_contract", f"Upload response missing file id: {payload}")
    if filename != upload_name:
        _phase_fail(
            "upload_contract",
            f"Upload filename mismatch: expected {upload_name!r}, got {filename!r}. payload={payload}",
        )

    return file_id


def _start_run(
    client: httpx.Client,
    *,
    tenant: str,
    prompt: str,
    file_id: str,
) -> str:
    response = client.post(
        "/v1/runs",
        headers=_headers(tenant),
        json={
            "model": MODEL,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_file", "file_id": file_id},
                    ],
                }
            ],
        },
        timeout=httpx.Timeout(30.0, connect=5.0),
    )

    if response.status_code != 202:
        payload = _json_object(response, phase="run_start")
        _phase_fail(
            "run_start",
            f"Run start failed (status={response.status_code}). payload={payload}",
        )

    payload = _json_object(response, phase="run_start")
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        _phase_fail("run_start", f"Run start response missing run_id: {payload}")
    return run_id


def _parse_sse_events(raw_sse: str) -> list[JSONObject]:
    events: list[JSONObject] = []
    for line in raw_sse.splitlines():
        stripped = line.strip()
        if not stripped.startswith("data:"):
            continue
        payload_text = stripped[len("data:") :].strip()
        if not payload_text:
            continue

        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            _phase_fail(
                "stream_malformed",
                f"Could not parse SSE data line as JSON: {payload_text[:320]!r} ({exc})",
            )

        if not isinstance(payload, dict):
            _phase_fail(
                "stream_malformed",
                f"SSE data payload must be an object, got {type(payload)!r}: {payload!r}",
            )

        events.append(cast(JSONObject, payload))

    return events


def _await_terminal_event(client: httpx.Client, *, tenant: str, run_id: str) -> JSONObject:
    timeout = httpx.Timeout(
        connect=5.0,
        read=RUN_EVENTS_TIMEOUT_SECONDS,
        write=RUN_EVENTS_TIMEOUT_SECONDS,
        pool=RUN_EVENTS_TIMEOUT_SECONDS,
    )

    try:
        with client.stream(
            "GET",
            f"/v1/runs/{run_id}/events",
            headers=_headers(tenant),
            timeout=timeout,
        ) as stream:
            if stream.status_code != 200:
                payload = _json_object(stream, phase="stream_open")
                _phase_fail(
                    "stream_open",
                    f"Run events endpoint returned status={stream.status_code} for run_id={run_id}. payload={payload}",
                )

            saw_data_event = False
            for line in stream.iter_lines():
                stripped = line.strip()
                if not stripped.startswith("data:"):
                    continue
                saw_data_event = True
                payload_text = stripped[len("data:") :].strip()
                if not payload_text:
                    continue

                try:
                    payload = json.loads(payload_text)
                except json.JSONDecodeError as exc:
                    _phase_fail(
                        "stream_malformed",
                        f"Could not parse SSE data line as JSON: {payload_text[:320]!r} ({exc})",
                    )

                if not isinstance(payload, dict):
                    _phase_fail(
                        "stream_malformed",
                        f"SSE data payload must be an object, got {type(payload)!r}: {payload!r}",
                    )

                event = cast(JSONObject, payload)
                if event.get("event") in {"run.completed", "run.failed"}:
                    return event

            if not saw_data_event:
                _phase_fail(
                    "stream_malformed",
                    f"Run events stream contained no data events for run_id={run_id}.",
                )
    except httpx.TimeoutException:
        _phase_fail(
            "stream_timeout",
            f"Timed out waiting for run events for run_id={run_id}.",
        )
    except Exception as exc:
        _phase_fail("stream_open", f"Could not open run events stream for run_id={run_id}: {exc}")

    _phase_fail(
        "stream_malformed",
        f"Run events stream had no terminal event (run.completed/run.failed) for run_id={run_id}.",
    )


def _extract_outcome_boundary(output: str) -> tuple[JSONObject | None, str | None]:
    if OUTCOME_START not in output or OUTCOME_END not in output:
        return None, "missing_envelope"

    start = output.rfind(OUTCOME_START)
    end = output.find(OUTCOME_END, start)
    if start < 0 or end < 0:
        return None, "missing_envelope"

    payload_text = output[start + len(OUTCOME_START) : end].strip()
    if not payload_text:
        return None, "invalid_json"

    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return None, "invalid_json"

    if not isinstance(payload, dict):
        return None, "invalid_json"

    normalized = cast(JSONObject, payload)
    status = normalized.get("status")
    explanation = normalized.get("explanation")

    if not isinstance(status, str) or not status.strip():
        return None, "invalid_status"

    if not isinstance(explanation, str) or not explanation.strip():
        return None, "missing_explanation"

    return normalized, None


def _verify_output_download(
    client: httpx.Client,
    *,
    tenant: str,
    run_id: str,
    output_file: JSONObject,
) -> tuple[str, int]:
    file_id = output_file.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        _phase_fail("run_completed_malformed", f"output_file missing file_id: {output_file}")

    filename = output_file.get("filename")
    if isinstance(filename, str) and ("/" in filename or "\\" in filename):
        _phase_fail(
            "redaction_violation",
            f"Output filename leaked a path-like value: {filename!r}",
        )

    source_run_id = output_file.get("source_run_id")
    if source_run_id != run_id:
        _phase_fail(
            "run_completed_malformed",
            f"output_file source_run_id mismatch: expected {run_id!r}, got {source_run_id!r}",
        )

    metadata = client.get(f"/v1/files/{file_id}", headers=_headers(tenant), timeout=httpx.Timeout(20.0, connect=5.0))
    if metadata.status_code != 200:
        _phase_fail(
            "download_metadata",
            f"Metadata lookup failed for file_id={file_id} status={metadata.status_code} body={metadata.text[:300]!r}",
        )

    metadata_payload = _json_object(metadata, phase="download_metadata")
    if metadata_payload.get("source_run_id") != run_id:
        _phase_fail(
            "download_metadata",
            f"Metadata source_run_id mismatch for file_id={file_id}: {metadata_payload}",
        )

    download = client.get(
        f"/v1/files/{file_id}/content",
        headers=_headers(tenant),
        timeout=httpx.Timeout(30.0, connect=5.0),
    )
    if download.status_code != 200:
        _phase_fail(
            "download_content",
            f"Download failed for file_id={file_id}: status={download.status_code} body={download.text[:300]!r}",
        )
    if not download.content:
        _phase_fail("download_content", f"Download returned empty content for file_id={file_id}")

    return file_id, len(download.content)


@pytest.fixture
def office_fixtures(tmp_path: Path) -> dict[str, Path]:
    return build_representative_office_fixtures(tmp_path / "office-fixtures")


def test_office_fixture_builder_generates_zip_members(office_fixtures: dict[str, Path]) -> None:
    docx_path = office_fixtures["docx"]
    xlsx_path = office_fixtures["xlsx"]

    assert docx_path.exists()
    assert xlsx_path.exists()
    assert docx_path.read_bytes().startswith(b"PK")
    assert xlsx_path.read_bytes().startswith(b"PK")

    with ZipFile(docx_path) as docx_zip:
        docx_members = set(docx_zip.namelist())
    assert "[Content_Types].xml" in docx_members
    assert "_rels/.rels" in docx_members
    assert "word/document.xml" in docx_members

    with ZipFile(xlsx_path) as xlsx_zip:
        xlsx_members = set(xlsx_zip.namelist())
    assert "[Content_Types].xml" in xlsx_members
    assert "_rels/.rels" in xlsx_members
    assert "xl/workbook.xml" in xlsx_members
    assert "xl/worksheets/sheet1.xml" in xlsx_members


@LIVE_ONLY
@pytest.mark.parametrize("case", LIVE_CASES, ids=[case.id for case in LIVE_CASES])
def test_live_preflight_runs_representative_guided_office_cases(
    office_fixtures: dict[str, Path],
    case: LiveOfficeCase,
) -> None:
    tenant = f"{TENANT_PREFIX}-{case.id}-{int(time.time())}"

    with httpx.Client(base_url=BASE_URL, timeout=httpx.Timeout(60.0, connect=5.0)) as client:
        _ensure_server_ready(client, tenant)

        fixture_path = office_fixtures[case.fixture_key]
        upload_file_id = _upload_fixture(
            client,
            tenant=tenant,
            upload_name=case.upload_name,
            mime_type=case.mime_type,
            content=fixture_path.read_bytes(),
        )

        prompt = _compose_guided_prompt(case, case.upload_name)
        run_id = _start_run(client, tenant=tenant, prompt=prompt, file_id=upload_file_id)
        terminal = _await_terminal_event(client, tenant=tenant, run_id=run_id)

        event_name = terminal.get("event")
        if event_name == "run.failed":
            err = str(terminal.get("error") or "")
            lowered = err.lower()
            if "no inference provider configured" in lowered or "provider" in lowered and "configured" in lowered:
                _phase_fail(
                    "provider_misconfigured",
                    f"Run failed due to provider/runtime setup for case={case.id}, run_id={run_id}: {err}",
                )
            _phase_fail("run_failed", f"Run failed for case={case.id}, run_id={run_id}: {err}")

        if event_name != "run.completed":
            _phase_fail("stream_malformed", f"Unexpected terminal event for run_id={run_id}: {terminal}")

        files_obj = terminal.get("files")
        files = cast(list[object], files_obj) if isinstance(files_obj, list) else []
        output_files = [
            cast(JSONObject, item)
            for item in files
            if isinstance(item, dict) and item.get("type") == "output_file"
        ]

        output_text_obj = terminal.get("output")
        output_text = output_text_obj if isinstance(output_text_obj, str) else ""

        if output_files:
            output_file_id, download_size = _verify_output_download(
                client,
                tenant=tenant,
                run_id=run_id,
                output_file=output_files[0],
            )
            print(
                f"[preflight:{case.id}] upload_file_id={upload_file_id} run_id={run_id} "
                + f"output_file_id={output_file_id} download_bytes={download_size}"
            )
            return

        boundary, boundary_error = _extract_outcome_boundary(output_text)
        if boundary_error or boundary is None:
            _phase_fail(
                "no_output_without_boundary",
                (
                    "Run completed without output files and without a valid outcome boundary "
                    + f"(case={case.id}, run_id={run_id}, parse_error={boundary_error}, "
                    + f"output={output_text[:500]!r})"
                ),
            )

        boundary_obj = boundary
        status = boundary_obj.get("status")
        if status not in NON_OUTPUT_BOUNDARY_STATUSES:
            _phase_fail(
                "ambiguous_no_output",
                (
                    "Run completed without output files but boundary status was not an honest "
                    + f"non-output status (case={case.id}, run_id={run_id}, "
                    + f"status={status!r}, boundary={boundary_obj})"
                ),
            )

        print(
            f"[preflight:{case.id}] upload_file_id={upload_file_id} run_id={run_id} "
            + f"boundary_status={status}"
        )


@LIVE_ONLY
def test_live_preflight_rejects_malformed_office_upload_contract(
    office_fixtures: dict[str, Path],
) -> None:
    """Negative contract check: Office bytes with drifted name/MIME must fail clearly."""
    tenant = f"{TENANT_PREFIX}-malformed-upload-{int(time.time())}"

    with httpx.Client(base_url=BASE_URL, timeout=httpx.Timeout(40.0, connect=5.0)) as client:
        _ensure_server_ready(client, tenant)

        response = client.post(
            "/v1/files",
            headers=_headers(tenant),
            files={"file": ("representative-office.bin", office_fixtures["docx"].read_bytes(), "application/octet-stream")},
            data={"purpose": "user_uploads"},
            timeout=httpx.Timeout(20.0, connect=5.0),
        )

    if response.status_code != 400:
        _phase_fail(
            "malformed_upload_contract",
            f"Expected unsupported upload type to return 400, got {response.status_code}. body={response.text[:300]!r}",
        )

    payload = _json_object(response, phase="malformed_upload_contract")
    error_obj = payload.get("error")
    error = cast(JSONObject, error_obj) if isinstance(error_obj, dict) else {}
    if error.get("code") != "unsupported_file_type":
        _phase_fail(
            "malformed_upload_contract",
            f"Expected error code unsupported_file_type, got payload={payload}",
        )
