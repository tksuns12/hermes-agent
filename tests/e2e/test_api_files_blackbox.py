"""Black-box file I/O smoke tests for a running API server.

Requires a live API server. Set API_E2E_BASE_URL (default http://localhost:8642)
if your server runs elsewhere. Skips if the server is unreachable.

File-backed conversation tests also require a configured inference provider. If the
server is reachable but no model/provider is configured, those tests skip rather
than failing the suite for an environment issue.
"""

from __future__ import annotations

from collections.abc import Generator
import os
import uuid
from typing import Final, TypeAlias, cast

import httpx
import pytest

BASE_URL: Final = os.getenv("API_E2E_BASE_URL", "http://localhost:8642")
TENANT: Final = os.getenv("API_E2E_TENANT", "e2e-tenant")
API_KEY: Final = os.getenv("API_E2E_API_KEY", "")
MODEL: Final = os.getenv("API_E2E_MODEL", "hermes-agent")
JSONObject: TypeAlias = dict[str, object]


def _headers(tenant: str | None = None) -> dict[str, str]:
    headers = {"X-Hermes-User-Id": tenant or TENANT}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    return headers


def _server_reachable() -> bool:
    try:
        resp = httpx.get(f"{BASE_URL}/health", timeout=2.0, headers=_headers())
        return resp.status_code < 500
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _server_reachable(),
    reason="API server not reachable; set API_E2E_BASE_URL or start the server to run this test",
)


@pytest.fixture
def client() -> Generator[httpx.Client, None, None]:
    with httpx.Client(base_url=BASE_URL, timeout=60.0) as live_client:
        yield live_client


def _json_object(resp: httpx.Response) -> JSONObject:
    return cast(JSONObject, resp.json())


def _upload_file(client: httpx.Client, *, tenant: str, filename: str, content: bytes) -> str:
    upload = client.post(
        "/v1/files",
        headers=_headers(tenant),
        files={"file": (filename, content, "text/plain")},
        data={"purpose": "user_uploads"},
    )
    assert upload.status_code == 201, upload.text
    body = _json_object(upload)
    assert body["filename"] == filename
    return cast(str, body["id"])


def _skip_if_no_provider(resp: httpx.Response) -> None:
    if resp.status_code != 500:
        return
    try:
        payload = _json_object(resp)
    except Exception:
        return

    error_obj = payload.get("error")
    if not isinstance(error_obj, dict):
        return
    error = cast(JSONObject, error_obj)

    message_obj = error.get("message", "")
    message = message_obj if isinstance(message_obj, str) else str(message_obj)
    if "No inference provider configured" in message:
        pytest.skip("API server reachable, but no inference provider is configured for live conversation E2E")


def _responses_output_text(body: JSONObject) -> str:
    output_items_obj = body.get("output", [])
    if not isinstance(output_items_obj, list):
        return ""

    for item_obj in cast(list[object], output_items_obj):
        if not isinstance(item_obj, dict):
            continue
        item = cast(JSONObject, item_obj)
        if item.get("type") != "message":
            continue

        content_items_obj = item.get("content", [])
        if not isinstance(content_items_obj, list):
            continue
        for content_obj in cast(list[object], content_items_obj):
            if not isinstance(content_obj, dict):
                continue
            content = cast(JSONObject, content_obj)
            if content.get("type") != "output_text":
                continue
            text = content.get("text", "")
            return text if isinstance(text, str) else ""
    return ""


def _chat_message_text(body: JSONObject) -> str:
    choices_obj = body.get("choices", [])
    if not isinstance(choices_obj, list) or not choices_obj:
        return ""

    first_choice_obj = cast(list[object], choices_obj)[0]
    if not isinstance(first_choice_obj, dict):
        return ""
    first_choice = cast(JSONObject, first_choice_obj)

    message_obj = first_choice.get("message", {})
    if not isinstance(message_obj, dict):
        return ""
    message = cast(JSONObject, message_obj)

    content = message.get("content", "")
    return content if isinstance(content, str) else ""


def test_file_upload_and_download_roundtrip(client: httpx.Client):
    tenant = f"{TENANT}-files"
    content = b"hello e2e file"
    file_id = _upload_file(client, tenant=tenant, filename="hello.txt", content=content)

    download = client.get(f"/v1/files/{file_id}/content", headers=_headers(tenant))
    assert download.status_code == 200, download.text
    assert download.content == content

    meta = client.get(f"/v1/files/{file_id}", headers=_headers(tenant))
    assert meta.status_code == 200, meta.text
    meta_body = _json_object(meta)
    assert meta_body["filename"] == "hello.txt"
    assert meta_body["bytes"] == len(content)


def test_responses_accept_uploaded_file_reference(client: httpx.Client):
    tenant = f"{TENANT}-responses"
    token = f"responses-e2e-{uuid.uuid4().hex[:12]}"
    file_id = _upload_file(client, tenant=tenant, filename="responses.txt", content=token.encode())

    resp = client.post(
        "/v1/responses",
        headers=_headers(tenant),
        json={
            "model": MODEL,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Repeat the file contents exactly."},
                        {"type": "input_file", "file_id": file_id},
                    ],
                }
            ],
        },
    )
    _skip_if_no_provider(resp)
    assert resp.status_code == 200, resp.text

    body = _json_object(resp)
    assert body["object"] == "response"
    output_text = _responses_output_text(body)
    assert token in output_text


def test_chat_completions_accept_uploaded_file_reference(client: httpx.Client):
    tenant = f"{TENANT}-chat"
    token = f"chat-e2e-{uuid.uuid4().hex[:12]}"
    file_id = _upload_file(client, tenant=tenant, filename="chat.txt", content=token.encode())

    resp = client.post(
        "/v1/chat/completions",
        headers=_headers(tenant),
        json={
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Repeat the file contents exactly."},
                        {"type": "input_file", "file_id": file_id},
                    ],
                }
            ],
        },
    )
    _skip_if_no_provider(resp)
    assert resp.status_code == 200, resp.text

    body = _json_object(resp)
    assert body["object"] == "chat.completion"
    message_text = _chat_message_text(body)
    assert token in message_text
