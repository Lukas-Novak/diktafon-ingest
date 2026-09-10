"""Outbound webhook (DIKTAFON_INGEST_NOTIFY_URL):

  * disabled by default (empty URL) — no HTTP attempted at all
  * a fresh store (201) notifies with updated=False
  * an idempotent re-delivery / edited transcript (200) notifies updated=True
  * a failing/unreachable webhook never affects the upload result — the memo
    is stored regardless (fire-and-forget)

The httpx module reference in app.main is patched (not httpx itself), so the
sync TestClient keeps working.
"""
import time
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from app import config
from app import main as main_module
from app.main import app
from test_ingest import make_memo, post_memo


class _FakeResponse:
    status_code = 200


class _RecorderClient:
    """Drop-in stand-in for httpx.AsyncClient that records calls instead."""
    calls: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json):
        type(self).calls.append((url, json))
        return _FakeResponse()


class _ExplodingClient(_RecorderClient):
    async def post(self, url, json):
        type(self).calls.append((url, json))
        raise OSError("connection refused")


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def recorder(monkeypatch):
    """Patch app.main.httpx with the recorder and reset it per test."""
    _RecorderClient.calls = []
    monkeypatch.setattr(main_module, "httpx",
                        SimpleNamespace(AsyncClient=_RecorderClient))
    yield _RecorderClient


def _wait_for_calls(n: int, timeout_s: float = 2.0) -> list:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if len(_RecorderClient.calls) >= n:
            break
        time.sleep(0.05)
    return _RecorderClient.calls


def test_notify_disabled_by_default_sends_nothing(client, monkeypatch, recorder):
    monkeypatch.setattr(config, "NOTIFY_URL", "")
    memo_id, audio, tr, meta = make_memo()
    r = post_memo(client, memo_id, audio, tr, meta)
    assert r.status_code == 201
    time.sleep(0.3)
    assert _RecorderClient.calls == []


def test_notify_fires_on_fresh_store(client, monkeypatch, recorder):
    monkeypatch.setattr(config, "NOTIFY_URL", "http://notify.example/hook")
    memo_id, audio, tr, meta = make_memo()
    r = post_memo(client, memo_id, audio, tr, meta)
    assert r.status_code == 201
    assert _wait_for_calls(1) == [
        ("http://notify.example/hook", {"memo_id": memo_id, "updated": False}),
    ]


def test_notify_fires_updated_true_on_redelivery(client, monkeypatch, recorder):
    monkeypatch.setattr(config, "NOTIFY_URL", "http://notify.example/hook")
    memo_id, audio, tr, meta = make_memo()
    assert post_memo(client, memo_id, audio, tr, meta).status_code == 201
    r2 = post_memo(client, memo_id, audio, tr, meta)  # idempotent retry
    assert r2.status_code == 200 and r2.json()["updated"] is True
    calls = _wait_for_calls(2)
    assert [c[0] for c in calls] == ["http://notify.example/hook"] * 2
    assert calls[0][1] == {"memo_id": memo_id, "updated": False}
    assert calls[1][1] == {"memo_id": memo_id, "updated": True}


def test_notify_failure_never_blocks_the_upload(client, monkeypatch):
    """The webhook target refusing connections must not affect storing."""
    _ExplodingClient.calls = []
    monkeypatch.setattr(config, "NOTIFY_URL", "http://127.0.0.1:9/unreachable")
    monkeypatch.setattr(main_module, "httpx",
                        SimpleNamespace(AsyncClient=_ExplodingClient))
    memo_id, audio, tr, meta = make_memo()
    r = post_memo(client, memo_id, audio, tr, meta)
    assert r.status_code == 201
    time.sleep(0.3)
    assert _ExplodingClient.calls  # an attempt was made (and logged) anyway
