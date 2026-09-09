"""diktafon-ingest — receives finished Diktafon memos (audio + transcript
+ metadata) from phones, stores them idempotently.

Protocol (one POST per memo, retry-safe):

    POST /diktafon/upload
    Authorization: Bearer <DIKTAFON_INGEST_TOKEN>
    Idempotency-Key: <memo UUID>
    Content-Type: multipart/form-data

      part 1  "audio"      — the recording (streamed; audio.m4a / audio.wav)
      part 2  "transcript" — application/json body (Diktafon transcript JSON)
      part 3  "metadata"   — application/json manifest; comes LAST because
                             it carries the SHA-256 of the streamed parts:
                             {memo_id, audio_sha256, transcript_sha256, ...}

Upsert semantics (a retry after a lost response is always safe):

  * unknown memo_id                    -> store all,          201 {ok, meeting_id}
  * known, same audio_sha256           -> audio kept, transcript+metadata
                                          atomically replaced, 200 {ok, updated}
  * known, different audio_sha256      -> 409 (never overwrite a divergent
                                          master silently)
  * client sha != received-bytes sha   -> 400 (corrupted in transit)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from . import config
from .storage import Storage, atomic_write_bytes, sha256_file, store_metadata

log = logging.getLogger("diktafon-ingest")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
CHUNK = 1 << 20

app = FastAPI(title="diktafon-ingest")
storage = Storage(config.DATA_DIR)

# Serialize concurrent uploads of the same memo (a retried client may overlap
# with its own in-flight attempt after a timeout). Locks are process-local;
# the service runs as a single worker.
_locks: dict[str, asyncio.Lock] = {}


def _lock_for(memo_id: str) -> asyncio.Lock:
    lock = _locks.get(memo_id)
    if lock is None:
        lock = _locks[memo_id] = asyncio.Lock()
    return lock


def _err(status: int, error: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": error, **extra})


def _check_auth(authorization: str | None) -> bool:
    if not authorization or not authorization.startswith("Bearer "):
        return False
    return hmac.compare_digest(authorization[len("Bearer "):], config.TOKEN)


async def _read_json_part(value, cap: int, name: str) -> tuple[dict | list | None, bytes | None, JSONResponse | None]:
    """Accepts the JSON payloads as either file parts or plain form fields,
    enforcing a size cap before parsing."""
    if isinstance(value, UploadFile):
        raw = await value.read(cap + 1)
    elif isinstance(value, str):
        raw = value.encode("utf-8")
    else:
        return None, None, _err(400, f"missing part: {name}")
    if len(raw) > cap:
        return None, None, _err(413, f"part too large: {name}")
    try:
        return json.loads(raw), raw, None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, None, _err(400, f"invalid JSON in part: {name}")


async def _spool_audio(upload: UploadFile, dest_tmp: Path) -> tuple[str, int]:
    """Re-read the already-spooled UploadFile in bounded chunks, hashing as we
    go; memory stays flat for arbitrarily large recordings."""
    digest = hashlib.sha256()
    total = 0
    with open(dest_tmp, "wb") as fh:
        while chunk := await upload.read(CHUNK):
            total += len(chunk)
            if total > config.MAX_UPLOADED_FILE_BYTES:
                raise ValueError("audio exceeds server size cap")
            digest.update(chunk)
            fh.write(chunk)
    if total == 0:
        raise ValueError("empty audio body")
    return digest.hexdigest(), total


async def _form(request: Request):
    """starlette gained max_part_size in 0.38-ish; degrade gracefully."""
    try:
        return await request.form(max_part_size=config.MAX_TRANSCRIPT_BYTES)
    except TypeError:
        return await request.form()


@app.get("/diktafon/health")
async def health():
    return {"ok": True}


@app.post("/diktafon/upload")
async def upload(
    request: Request,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
):
    if not _check_auth(authorization):
        # Never echo which part was wrong; 401 for anything auth-shaped.
        return _err(401, "unauthorized")

    if not idempotency_key or not UUID_RE.match(idempotency_key):
        return _err(400, "Idempotency-Key must be the memo UUID")
    memo_id = idempotency_key.lower()

    try:
        form = await _form(request)
    except Exception:
        return _err(400, "malformed multipart body")

    audio = form.get("audio")
    if not isinstance(audio, UploadFile):
        return _err(400, "missing part: audio")
    ext = (os.path.splitext(audio.filename or "")[1].lstrip(".")).lower()
    if ext not in ("m4a", "wav"):
        return _err(400, f"unsupported audio extension: {ext!r}")

    metadata, _raw_meta, err = await _read_json_part(
        form.get("metadata"), config.MAX_METADATA_BYTES, "metadata")
    if err:
        return err
    transcript, raw_transcript, err = await _read_json_part(
        form.get("transcript"), config.MAX_TRANSCRIPT_BYTES, "transcript")
    if err:
        return err
    assert isinstance(metadata, dict) and raw_transcript is not None

    if metadata.get("memo_id", "").lower() != memo_id:
        return _err(400, "metadata.memo_id does not match Idempotency-Key")
    client_sha = metadata.get("audio_sha256")
    if not isinstance(client_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", client_sha):
        return _err(400, "metadata.audio_sha256 must be a lowercase sha256 hex")
    # Optional but verified when present (cheap to send, catches truncated JSON).
    transcript_sha = metadata.get("transcript_sha256")
    if transcript_sha is not None:
        if hashlib.sha256(raw_transcript).hexdigest() != transcript_sha:
            return _err(400, "transcript_sha256 mismatch")

    started = time.monotonic()
    async with _lock_for(memo_id):
        memo_dir = storage.memo_dir(memo_id)
        memo_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=memo_dir, prefix=".audio.", suffix=".part")
        os.close(fd)
        tmp_path = Path(tmp)
        try:
            try:
                actual_sha, audio_bytes = await _spool_audio(audio, tmp_path)
            except ValueError as exc:
                return _err(400, str(exc))
            if actual_sha != client_sha:
                return _err(400, "audio_sha256 mismatch (payload corrupted in transit)")

            stored_audio = storage.audio_file(memo_id)
            if stored_audio is not None:
                stored_sha = sha256_file(stored_audio)
                if stored_sha != client_sha:
                    log.warning("sha conflict for memo %s: stored=%s incoming=%s",
                                memo_id, stored_sha[:12], client_sha[:12])
                    return _err(409, "different audio already stored for this memo_id")
                # Same master already here — replace only the small artifacts.
                # This is also the edited-transcript path.
                tmp_path.unlink(missing_ok=True)
                atomic_write_bytes(memo_dir / "transcript.json", raw_transcript)
                store_metadata(memo_dir / "metadata.json", metadata)
                log.info("memo %s updated in place (audio unchanged, %d bytes)",
                         memo_id, audio_bytes)
                return JSONResponse(status_code=200,
                                    content={"ok": True, "updated": True,
                                             "meeting_id": memo_id})

            # Fresh memo: commit audio + artifacts atomically.
            final_audio = memo_dir / f"audio.{ext}"
            os.replace(tmp_path, final_audio)
            atomic_write_bytes(memo_dir / "transcript.json", raw_transcript)
            stored_meta = store_metadata(memo_dir / "metadata.json", metadata)
            stored_meta["ingest"]["audio_sha256"] = actual_sha
            atomic_write_bytes(
                memo_dir / "metadata.json",
                json.dumps(stored_meta, ensure_ascii=False, indent=2).encode("utf-8"))
            log.info("memo %s stored: %d bytes audio.%s in %.1fs",
                     memo_id, audio_bytes, ext, time.monotonic() - started)
            return JSONResponse(status_code=201,
                                content={"ok": True, "meeting_id": memo_id,
                                         "audio_bytes": audio_bytes})
        finally:
            tmp_path.unlink(missing_ok=True)


def main() -> None:
    if not config.TOKEN:
        raise SystemExit(
            "DIKTAFON_INGEST_TOKEN is not set — refusing to start without "
            "an authentication secret (see .env.example).")
    import uvicorn

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT,
                log_level="info")


if __name__ == "__main__":
    main()
