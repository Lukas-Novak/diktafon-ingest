"""Sends one demo memo to a running diktafon-ingest instance — the reference
client for the upload protocol (part order: audio, transcript, metadata-last,
metadata carries the hashes of the streamed parts).

    python simulate_upload.py --url https://example.com/diktafon/upload \
        --token test-token [--audio some.m4a]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import uuid

import httpx


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--audio", help="existing audio file; a synthetic 1 MiB body is generated when omitted")
    p.add_argument("--timeout", type=float, default=120)
    args = p.parse_args()

    memo_id = str(uuid.uuid4())
    if args.audio:
        audio = open(args.audio, "rb").read()
        ext = args.audio.rsplit(".", 1)[-1].lower()
        assert ext in ("m4a", "wav"), "audio must be .m4a or .wav"
    else:
        audio = bytes((i * 13 + 5) % 256 for i in range(1 << 20))
        ext = "wav"

    transcript = {
        "lang": "cs",
        "segments": [{"s": 0, "e": 1500, "w": [
            {"t": "testovací", "s": 0, "e": 700},
            {"t": "nahrávka", "s": 750, "e": 1500},
        ]}],
    }
    transcript_json = json.dumps(transcript, ensure_ascii=False).encode()
    metadata = {
        "memo_id": memo_id,
        "cassette_id": str(uuid.uuid4()),
        "title": "simulate_upload demo",
        "created_at": "2026-01-01T12:00:00.000",
        "duration_ms": 1500,
        "detected_lang": "cs",
        "audio_format": ext,
        "sample_rate": 16000,
        "channels": 1,
        "audio_size_bytes": len(audio),
        "audio_sha256": hashlib.sha256(audio).hexdigest(),
        "transcript_sha256": hashlib.sha256(transcript_json).hexdigest(),
        "app_version": "simulate_upload/1.0",
        "uploaded_at": "2026-01-01T12:05:00.000",
    }

    r = httpx.post(
        args.url,
        headers={"Authorization": f"Bearer {args.token}",
                 "Idempotency-Key": memo_id},
        files=[
            ("audio", (f"audio.{ext}", audio, "application/octet-stream")),
            ("transcript", ("transcript.json", transcript_json, "application/json")),
            ("metadata", ("metadata.json",
                          json.dumps(metadata).encode(), "application/json")),
        ],
        timeout=args.timeout,
    )
    print(r.status_code, r.text)
    print("memo_id:", memo_id)
    return 0 if r.is_success else 1


if __name__ == "__main__":
    raise SystemExit(main())
