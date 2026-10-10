"""Plex webhook endpoint."""

import json
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from usharr import api

BOUNDARY = "------------------------GmUEPJGOGBBaWryrlxEwbe"


MULTIPART = {"content-type": f"multipart/form-data; boundary={BOUNDARY}"}


def plex_body(event: str) -> bytes:
    return multipart(
        json.dumps({"event": event, "user": True, "owner": True, "Account": {"id": 1}})
    )


def multipart(payload: str, filename: bool = True) -> bytes:
    """A Plex webhook body. Plex attaches a filename to the `payload` part."""
    disposition = 'form-data; name="payload"'
    if filename:
        disposition += '; filename="payload20261010-41-2oh52n.json"'
    return (
        f"--{BOUNDARY}\r\n"
        f"Content-Disposition: {disposition}\r\n"
        "Content-Type: application/json\r\n\r\n"
        f"{payload}\r\n"
        f"--{BOUNDARY}--\r\n"
    ).encode()


@pytest.fixture
def post():
    app = FastAPI()
    app.include_router(api.api)
    with mock.patch.object(api.scanner, "enqueue", mock.AsyncMock()) as enqueue:
        client = TestClient(app)

        def call(**kwargs) -> tuple[int, int]:
            enqueue.reset_mock()
            resp = client.post("/api/webhook", **kwargs)
            return resp.status_code, enqueue.await_count

        yield call


def test_plex_library_new_scans(post):
    assert post(content=plex_body("library.new"), headers=MULTIPART) == (204, 1)


def test_plex_other_event_no_scan(post):
    assert post(content=plex_body("media.play"), headers=MULTIPART) == (204, 0)


def test_payload_without_filename(post):
    body = multipart('{"event":"library.new"}', filename=False)
    assert post(content=body, headers=MULTIPART) == (204, 1)


def test_payload_as_urlencoded_field(post):
    assert post(data={"payload": '{"event":"library.new"}'}) == (204, 1)


def test_missing_payload_part(post):
    assert post(data={"other": "x"}) == (422, 0)


def test_bad_payload_json(post):
    assert post(content=multipart("{"), headers=MULTIPART) == (422, 0)


def test_malformed_multipart(post):
    assert post(content=b"garbage", headers=MULTIPART) == (400, 0)
