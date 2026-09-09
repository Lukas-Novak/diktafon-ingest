# diktafon-ingest

Receives finished memos from [Diktafon](https://github.com/jaromiru/diktafon)
(Android voice-memo app) — audio + local Whisper transcript + metadata — and
stores them idempotently. One POST per memo; a retry is always safe.

```
phone (Diktafon app)
    │  POST /diktafon/upload   (multipart, Bearer token, Idempotency-Key)
    ▼
TLS reverse proxy (nginx)
    ▼
diktafon-ingest  (FastAPI, Docker, localhost:8378)
    ▼
data/<memo_id>/{audio.(m4a|wav), transcript.json, metadata.json}
```

## Protocol

`POST /diktafon/upload` — `multipart/form-data`, parts **in this order**:

1. `audio` — the recording (`audio.m4a` or `audio.wav`), streamed body
2. `transcript` — `application/json`, the Diktafon transcript document
3. `metadata` — `application/json` manifest, sent **last** because it carries
   the SHA-256 of the streamed parts (`audio_sha256`, `transcript_sha256`),
   along with `memo_id`, cassette id, timestamps, duration, format info,
   app version…

Headers: `Authorization: Bearer <DIKTAFON_INGEST_TOKEN>`,
`Idempotency-Key: <memo UUID>` (must equal `metadata.memo_id`).

**Upsert semantics** (safe to retry after a lost response):

| server state                              | result |
|-------------------------------------------|--------|
| unknown memo_id                           | `201 {ok, meeting_id}` — stored |
| known, same `audio_sha256`                | `200 {ok, updated}` — audio kept, transcript+metadata atomically replaced (also the edited-transcript path) |
| known, different `audio_sha256`           | `409` — divergent master, never overwritten silently |
| hash of received bytes ≠ declared sha256  | `400` — corrupted in transit |

`GET /diktafon/health` → `{"ok": true}`.

## Run (Docker)

```bash
cp .env.example .env
$EDITOR .env                      # set DIKTAFON_INGEST_TOKEN (openssl rand -hex 32)
docker compose up -d --build
curl -s http://127.0.0.1:8378/diktafon/health
```

The container binds `127.0.0.1` only. Front it with your TLS proxy, e.g. nginx
inside an existing HTTPS server block:

```nginx
location /diktafon/ {
    client_max_body_size 4g;      # long meetings are large
    proxy_read_timeout 300s;
    proxy_connect_timeout 10s;
    proxy_redirect off;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto https;
    proxy_pass http://127.0.0.1:8378;
}
```

So the phone-side upload URL becomes
`https://example.com/diktafon/upload`.

## Run (dev, no Docker)

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -r requirements-test.txt
DIKTAFON_INGEST_TOKEN=test-token DATA_DIR=$(pwd)/data \
    .venv/bin/python -m app.main
```

## Test

```bash
.venv/bin/python -m pytest tests/ -q
# against a live instance:
.venv/bin/python simulate_upload.py --url http://127.0.0.1:8378/diktafon/upload \
    --token test-token
```

## Security notes

- The Bearer token is the only auth; use HTTPS (production) and a long random
  token from the environment — never commit `.env`.
- `memo_id` is validated as a UUID before it becomes a path component.
- All writes are write-then-rename; a crash mid-upload never corrupts a memo
  that was already stored.
- The service never deletes `audio.*` on its own; phone-side settings decide
  whether the local copy is kept (Diktafon's upload feature never deletes).
