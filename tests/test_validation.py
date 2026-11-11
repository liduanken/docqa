import json

from tests.conftest import upload


def _error_code(resp) -> str:  # type: ignore[no-untyped-def]
    return resp.json()["error"]["code"]


def test_empty_text_rejected(make_client):
    resp = make_client().post("/v1/documents", json={"text": ""})
    assert resp.status_code == 422
    assert _error_code(resp) == "invalid_request"


def test_whitespace_only_rejected(make_client):
    resp = make_client().post("/v1/documents", json={"text": "   \n\t "})
    assert resp.status_code == 422


def test_document_over_limit_rejected(make_client):
    resp = make_client().post("/v1/documents", json={"text": "x" * 2001})
    assert resp.status_code == 413
    assert _error_code(resp) == "payload_too_large"


def test_limit_counts_utf8_bytes_not_characters(make_client):
    resp = make_client().post("/v1/documents", json={"text": "é" * 1001})  # 2002 bytes
    assert resp.status_code == 413


def test_oversized_body_rejected_before_parsing(make_client):
    # Above 6 * max_document_bytes + 4096, so rejected from Content-Length without reading the body.
    resp = make_client().post(
        "/v1/documents", content=b"x" * 20_000, headers={"content-type": "application/json"}
    )
    assert resp.status_code == 413
    assert _error_code(resp) == "payload_too_large"


def test_escaped_non_ascii_document_under_limit_accepted(make_client):
    # 19 998 UTF-8 bytes, but about 40 KB on the wire once json.dumps escapes each char as \uXXXX.
    body = json.dumps({"text": "文" * 6666})
    resp = make_client(max_document_bytes=20_000).post(
        "/v1/documents", content=body, headers={"content-type": "application/json"}
    )
    assert resp.status_code == 201
    assert resp.json()["size_bytes"] == 19_998


def test_document_at_limit_accepted(make_client):
    resp = make_client().post("/v1/documents", json={"text": "x" * 2000})
    assert resp.status_code == 201
    assert resp.json()["size_bytes"] == 2000


def test_unknown_document_is_404(make_client):
    resp = make_client().post("/v1/documents/does-not-exist/questions", json={"question": "hi"})
    assert resp.status_code == 404
    assert _error_code(resp) == "not_found"


def test_question_over_limit_rejected(make_client):
    client = make_client(max_question_chars=10)
    doc_id = upload(client)
    resp = client.post(f"/v1/documents/{doc_id}/questions", json={"question": "x" * 11})
    assert resp.status_code == 422
    assert _error_code(resp) == "invalid_request"


def test_validation_error_does_not_echo_input(make_client):
    resp = make_client().post("/v1/documents", json={"text": 12345, "secret": "s3cr3t"})
    assert resp.status_code == 422
    assert "s3cr3t" not in resp.text
    assert "12345" not in resp.text


def test_request_id_is_echoed(make_client):
    resp = make_client().get("/healthz", headers={"x-request-id": "abc123"})
    assert resp.headers["x-request-id"] == "abc123"
