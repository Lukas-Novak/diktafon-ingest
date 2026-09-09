"""On-disk meeting store.

Layout:

    <DATA_DIR>/<memo_id>/audio.<ext>   # the authoritative recording
    <DATA_DIR>/<memo_id>/transcript.json
    <DATA_DIR>/<memo_id>/metadata.json

Every write goes through a `.part` sibling followed by an atomic rename, so a
crash mid-write can never truncate a previously good artifact. A memo_id is
validated as a UUID upstream, which also makes it a safe path component.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

CHUNK = 1 << 20  # 1 MiB streaming granularity

AUDIO_EXTENSIONS = {"m4a", "wav"}


class Storage:
    def __init__(self, root: Path):
        self.root = Path(root)

    def memo_dir(self, memo_id: str) -> Path:
        return self.root / memo_id

    def _audio_candidates(self, memo_id: str) -> list[Path]:
        return [self.memo_dir(memo_id) / f"audio.{ext}" for ext in AUDIO_EXTENSIONS]

    def audio_file(self, memo_id: str) -> Path | None:
        """The stored audio for a memo, if any (extension-agnostic)."""
        for candidate in self._audio_candidates(memo_id):
            if candidate.exists():
                return candidate
        return None

    def existing_audio_sha256(self, memo_id: str) -> str | None:
        audio = self.audio_file(memo_id)
        if audio is None:
            return None
        return sha256_file(audio)

    def has_memo(self, memo_id: str) -> bool:
        return (self.memo_dir(memo_id) / "metadata.json").exists() or self.audio_file(memo_id) is not None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write-then-rename: readers never observe a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def atomic_write_json(path: Path, payload: dict) -> None:
    atomic_write_bytes(path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))


def store_metadata(path: Path, metadata: dict, *, received_at: float | None = None) -> dict:
    """Persist the client manifest, enriched with server-side bookkeeping,
    without ever mutating the client's own fields."""
    stored = dict(metadata)
    stored["ingest"] = {
        "schema": 1,
        "received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(received_at or time.time())),
    }
    atomic_write_json(path, stored)
    return stored
