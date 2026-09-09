"""Runtime configuration from environment variables (never from the repo)."""
from __future__ import annotations

import os
from pathlib import Path

TOKEN = os.environ.get("DIKTAFON_INGEST_TOKEN", "")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8378"))

# Structural size caps, so a bogus client cannot wedge the service in memory.
# Audio itself is unbounded here — recordings can be hours long and are
# streamed/spooled, never buffered. Bound it at the reverse proxy instead
# (e.g. nginx client_max_body_size 4g).
MAX_METADATA_BYTES = 1 << 20          # 1 MiB of JSON is already generous
MAX_TRANSCRIPT_BYTES = 64 << 20       # word-level timings for very long meets
MAX_UPLOADED_FILE_BYTES = 8 << 30     # 8 GiB hard sanity cap per audio file
