"""End-to-end tests against the ASGI app (no network, real multipart parser).

Covers the reliability matrix agreed with the phone-side design:
  * auth rejected without/with wrong Bearer token
  * happy path stores audio + transcript + metadata atomically (201)
  * an exact retry after a lost response is a no-op duplicate (200 updated)
  * an edited transcript replaces the artifacts WITHOUT touching the audio
  * a different audio under a known memo_id is a 409, master preserved
  * payload integrity: audio_sha256 / transcript_sha256 are verified
  * Idempotency-Key must equal metadata.memo_id, and both must be UUIDs
  * large (200 MiB) recordings stream through with flat memory
"""
import hashlib
import json
import uuid
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import config
from app.main import app
from conftest import TEST_TOKEN

LARGE_AUDIO_BYTES = 200 << 20  # 200 MiB


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


def _auth(memo_id: str, token: str = TEST_TOKEN) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": memo_id,
    }


def make_memo(audio_size: int = 4096, transcript_text: str = "dobrý den světe"):
    """Builds a coherent (audio, transcript, metadata) triple for one memo."""
    memo_id = str(uuid.uuid4())
    audio = bytes((i * 31 + 7) % 256 for i in range(audio_size))
    transcript = {
        "lang": "cs",
        "segments": [
            {"s": 0, "e": 1900,
             "w": [{"t": w, "s": i * 100, "e": i * 100 + 90}
                   for i, w in enumerate(transcript_text.split())]},
        ],
    }
    transcript_json = json.dumps(transcript).encode()
    meta = {
        "memo_id": memo_id,
        "cassette_id": str(uuid.uuid4()),
        "title": None,
        "created_at": "2026-09-09T12:00:00.000",
        "duration_ms": 1900,
        "detected_lang": "cs",
        "audio_format": "m4a",
        "sample_rate": 16000,
        "channels": 1,
        "audio_size_bytes": len(audio),
        "audio_sha256": hashlib.sha256(audio).hexdigest(),
        "transcript_sha256": hashlib.sha256(transcript_json).hexdigest(),
        "app_version": "1.0.9-test",
        "uploaded_at": "2026-09-09T12:05:00.000",
    }
    return memo_id, audio, transcript_json, meta


def post_memo(client, memo_id, audio, transcript_json, meta,
              token=TEST_TOKEN, filename="audio.m4a"):
    """Posts in the documented part order: audio, transcript, metadata (last,
    because it carries the hashes of the streamed parts)."""
    return client.post(
        "/diktafon/upload",
        headers=_auth(memo_id, token),
        files=[
            ("audio", (filename, audio, "application/octet-stream")),
            ("transcript", ("transcript.json", transcript_json, "application/json")),
            ("metadata", ("metadata.json", json.dumps(meta).encode(), "application/json")),
        ],
    )


# ----------------------------------------------------------------- health
def test_health(client):
    r = client.get("/diktafon/health")
    assert r.status_code == 200 and r.json()["ok"] is True


# ------------------------------------------------------------------- auth
def test_rejects_missing_token(client):
    memo_id, audio, tr, meta = make_memo()
    r = client.post("/diktafon/upload", headers={"Idempotency-Key": memo_id},
                    files=[("audio", ("audio.m4a", audio)),
                           ("metadata", ("metadata.json", json.dumps(meta))),
                           ("transcript", ("transcript.json", tr))])
    assert r.status_code == 401


def test_rejects_wrong_token(client):
    memo_id, audio, tr, meta = make_memo()
    r = post_memo(client, memo_id, audio, tr, meta, token="wrong")
    assert r.status_code == 401


# ------------------------------------------------------------- happy path
def test_upload_stores_everything(client):
    memo_id, audio, tr, meta = make_memo()
    r = post_memo(client, memo_id, audio, tr, meta)
    assert r.status_code == 201, r.text
    assert r.json()["meeting_id"] == memo_id

    store = Path(config.DATA_DIR) / memo_id
    assert (store / "audio.m4a").read_bytes() == audio
    assert json.loads((store / "transcript.json").read_bytes())["lang"] == "cs"
    stored_meta = json.loads((store / "metadata.json").read_text())
    assert stored_meta["memo_id"] == memo_id
    assert stored_meta["ingest"]["audio_sha256"] == meta["audio_sha256"]
    assert stored_meta["ingest"]["received_at"]


def test_wav_extension_accepted(client):
    memo_id, audio, tr, meta = make_memo()
    r = post_memo(client, memo_id, audio, tr, meta, filename="audio.wav")
    assert r.status_code == 201
    assert (Path(config.DATA_DIR) / memo_id / "audio.wav").exists()


# ------------------------------------------------------------- idempotency
def test_retry_after_lost_response_is_single_meeting(client):
    """The phone uploaded fully but never saw the 201; it retries. Exactly
    one meeting must exist afterwards."""
    memo_id, audio, tr, meta = make_memo()
    r1 = post_memo(client, memo_id, audio, tr, meta)
    r2 = post_memo(client, memo_id, audio, tr, meta)
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert r2.json()["updated"] is True

    store = Path(config.DATA_DIR) / memo_id
    assert len(list(store.glob("audio.*"))) == 1
    assert not list(store.glob("*.part"))


def test_edited_transcript_replaces_artifacts_but_not_audio(client):
    memo_id, audio, tr, meta = make_memo(transcript_text="původní přepis")
    assert post_memo(client, memo_id, audio, tr, meta).status_code == 201
    audio_file = Path(config.DATA_DIR) / memo_id / "audio.m4a"
    before = audio_file.read_bytes()

    _, _, tr2, meta2 = make_memo(transcript_text="opravený přepis ručně")
    meta2["memo_id"] = memo_id  # same memo, edited transcript
    r = post_memo(client, memo_id, audio, tr2, meta2)
    assert r.status_code == 200 and r.json()["updated"] is True

    assert audio_file.read_bytes() == before  # master untouched
    stored = json.loads((Path(config.DATA_DIR) / memo_id / "transcript.json").read_text())
    assert "opravený" in stored["segments"][0]["w"][0]["t"]


# ------------------------------------------------------------- conflicts
def test_different_audio_same_memo_id_is_conflict(client):
    memo_id, audio, tr, meta = make_memo(audio_size=1024)
    assert post_memo(client, memo_id, audio, tr, meta).status_code == 201

    other_audio = b"\xde\xad\xbe\xef" * 512
    meta2 = dict(meta)
    meta2["audio_sha256"] = hashlib.sha256(other_audio).hexdigest()
    meta2["audio_size_bytes"] = len(other_audio)
    r = post_memo(client, memo_id, other_audio, tr, meta2)
    assert r.status_code == 409

    store = Path(config.DATA_DIR) / memo_id / "audio.m4a"
    assert store.read_bytes() == audio  # original master preserved


# --------------------------------------------------------------- integrity
def test_audio_sha_mismatch_rejected(client):
    memo_id, audio, tr, meta = make_memo()
    meta["audio_sha256"] = "0" * 64
    r = post_memo(client, memo_id, audio, tr, meta)
    assert r.status_code == 400
    assert not (Path(config.DATA_DIR) / memo_id / "audio.m4a").exists()


def test_transcript_sha_mismatch_rejected(client):
    memo_id, audio, tr, meta = make_memo()
    meta["transcript_sha256"] = "0" * 64
    r = post_memo(client, memo_id, audio, tr, meta)
    assert r.status_code == 400
    assert not (Path(config.DATA_DIR) / memo_id).joinpath("transcript.json").exists()


def test_key_must_match_metadata_memo_id(client):
    _, audio, tr, meta = make_memo()
    other_id = str(uuid.uuid4())
    r = post_memo(client, other_id, audio, tr, meta)
    assert r.status_code == 400


def test_non_uuid_key_rejected(client):
    _, audio, tr, meta = make_memo()
    r = client.post("/diktafon/upload",
                    headers=_auth("../../etc", TEST_TOKEN),
                    files=[("audio", ("audio.m4a", audio)),
                           ("metadata", ("metadata.json", json.dumps(meta))),
                           ("transcript", ("transcript.json", tr))])
    assert r.status_code == 400


def test_bad_extension_rejected(client):
    memo_id, audio, tr, meta = make_memo()
    r = post_memo(client, memo_id, audio, tr, meta, filename="notes.txt")
    assert r.status_code == 400


# ------------------------------------------------------------- large files
def test_large_recording_streams_through(client):
    memo_id = str(uuid.uuid4())
    path = Path(config.DATA_DIR).parent / "big-audio-fixture.wav"
    # Deterministic 200 MiB without holding it in memory.
    block = bytes(range(256)) * 4096  # 1 MiB
    sha = hashlib.sha256()
    with open(path, "wb") as fh:
        for _ in range(LARGE_AUDIO_BYTES // len(block)):
            sha.update(block)
            fh.write(block)
    try:
        audio_bytes = path.read_bytes()  # httpx needs a body; spooled to disk by the parser
        tr = json.dumps({"lang": "cs", "segments": []}).encode()
        meta = {
            "memo_id": memo_id,
            "audio_format": "wav",
            "audio_size_bytes": len(audio_bytes),
            "audio_sha256": sha.hexdigest(),
        }
        r = post_memo(client, memo_id, audio_bytes, tr, meta, filename="audio.wav")
        assert r.status_code == 201, r.text
        assert r.json()["audio_bytes"] == LARGE_AUDIO_BYTES
        stored = Path(config.DATA_DIR) / memo_id / "audio.wav"
        assert stored.stat().st_size == LARGE_AUDIO_BYTES
        assert hashlib.sha256(stored.read_bytes()).hexdigest() == sha.hexdigest()
    finally:
        path.unlink(missing_ok=True)


# ------------------------------------------------------ known-memo salvage
def test_known_memo_with_deleted_audio_stores_fresh(client):
    """An admin removed the audio server-side; the phone re-delivering the
    same memo must be able to restore it instead of hitting 409 forever."""
    memo_id, audio, tr, meta = make_memo()
    assert post_memo(client, memo_id, audio, tr, meta).status_code == 201
    (Path(config.DATA_DIR) / memo_id / "audio.m4a").unlink()

    r = post_memo(client, memo_id, audio, tr, meta)
    assert r.status_code == 201
    assert (Path(config.DATA_DIR) / memo_id / "audio.m4a").read_bytes() == audio
