# Video Note Taker

[![CI](https://github.com/Ivan825/Note-Taking-Agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Ivan825/Note-Taking-Agent/actions/workflows/ci.yml)

Click **Take Notes** on a YouTube video (up to 20 minutes) and a Notion page appears with structured notes
and **real frames from the video**, each with a caption saying what it shows and why it was picked.
Videos without captions are transcribed locally with Whisper.

```
Chrome extension ─▶ API gateway ─▶ RabbitMQ ─▶ worker × N ─▶ YouTube · Claude · Notion
                        └─ PostgreSQL (job state, checkpoints)      └─ Redis (global rate limits)
```

* **Base URL:** `http://localhost:8000` (API docs at `/docs`, health check at `/healthz`)
* **RabbitMQ UI:** `http://localhost:15672` (`notes` / `notes`)

## Run it

Needs Docker Desktop (Compose v2) and Chrome 116+, on a residential network (YouTube blocks most datacenter IPs).

```bash
# 1. get the code and configure it
git clone https://github.com/Ivan825/Note-Taking-Agent.git && cd Note-Taking-Agent
cp .env.example .env
python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"   # paste as TOKEN_ENC_KEY
#   then edit .env: ANTHROPIC_API_KEY, NOTION_CLIENT_ID, NOTION_CLIENT_SECRET, TOKEN_ENC_KEY

# 2. start gateway, RabbitMQ, PostgreSQL, Redis and 2 workers
docker compose up -d --build          # same as: make up (which also waits for /healthz)

# 3. check it is healthy (gateway, broker, database and Redis should all say "ok")
curl -s localhost:8000/healthz
docker compose ps

# 4. scale the workers up or down at any time
docker compose up -d --scale worker=4          # same as: make scale WORKERS=4

# 5. watch it work, then stop
docker compose logs -f gateway worker          # same as: make logs
docker compose down                            # keeps data;  docker compose down -v  wipes it
```

Submit a test job without the extension (a Notion token comes from the extension's OAuth flow):

```bash
curl -s -XPOST localhost:8000/jobs -H 'content-type: application/json' \
  -d '{"youtube_url":"https://www.youtube.com/watch?v=aircAruvnKk","notion_token":"<token>"}'
curl -s localhost:8000/jobs/<job_id>           # poll status
curl -s localhost:8000/jobs/<job_id>/results   # 409 until finished, then the Notion page URL
```

**Extension (no build step):** open `chrome://extensions`, enable *Developer mode*, **Load unpacked**, pick `extension/`.
Open a video and click **Take Notes**; the first click opens Notion's consent screen once. The popup shows
Queued → Transcribing → Summarizing → Extracting Frames → Publishing → Done, then **View in Notion**.

The extension ID is fixed, so the Notion redirect URI is always
`https://cfajbkllcohlnhfahfccoiopccagglon.chromiumapp.org/notion`. To use your own Notion connection, create a
public (OAuth) connection at <https://www.notion.so/profile/integrations> with that redirect URI and
read/update/insert-content capabilities, then put its client ID and secret in `.env`.

## Default Notion destination

`notion_parent_id` is optional. When omitted, notes go to a private page called **"Video Notes"** at the top level of the
user's workspace (created or found during the OAuth step), and each video becomes a sub-page of it. If the workspace
doesn't allow that, the first page shared during OAuth is used. The Settings page can pick another page.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | **required** | Claude API key |
| `LLM_MODEL` | `claude-haiku-5-5` | Claude model (e.g. `claude-sonnet-5-5` for richer notes) |
| `LLM_RPM` / `LLM_ITPM` | `40` / `40000` | **Global LLM rate limit** (requests and input tokens per minute), shared by all workers |
| `NOTION_CLIENT_ID`, `NOTION_CLIENT_SECRET` | **required** | Notion OAuth credentials (the secret stays on the gateway) |
| `NOTION_REDIRECT_URI` | see above | Must match the Notion connection |
| `NOTION_RPS`, `YOUTUBE_RPS` | `2.5`, `1` | Global requests/second to Notion and YouTube |
| `TOKEN_ENC_KEY` | **required** | Fernet key; encrypts the Notion token inside queued messages |
| `RABBITMQ_URL` | `amqp://notes:notes@rabbitmq:5672/%2F` | **Broker connection string** |
| `DATABASE_URL`, `REDIS_URL` | compose defaults | Job state store; rate-limit buckets and chaos flags |
| `MAX_ATTEMPTS` | `5` | **Retry budget** before a job is `DEAD_LETTERED` |
| `BACKOFF_SECONDS` | `10,30,90,90` | **Backoff schedule** (last value repeats) |
| `MAX_VIDEO_SECONDS` | `1200` | The 20-minute cap |
| `LLM_MAX_FRAMES`, `FRAME_VISION_CHECK` | `8`, `true` | Key-frame cap; re-caption each frame from its real pixels |
| `ASR_FALLBACK`, `WHISPER_MODEL` | `true`, `base` | Local speech-to-text for videos without captions |
| `ENABLE_DEBUG_ENDPOINTS` | `true` | Exposes `POST /debug/chaos` (fault injection for the demo) |
| `ADMIN_TOKEN` | unset | Enables `/admin` (inspect and replay the dead-letter queue); unset = not mounted |

Everything else (`LLM_EFFORT`, `LLM_CHUNK_TOKENS`, `WHISPER_THREADS`, `YOUTUBE_COOKIES_FILE`, …) is listed in `.env.example`.

## REST API

| Endpoint | Purpose |
|---|---|
| `POST /jobs` | `{youtube_url, notion_token, notion_parent_id?}` → `202` + `Location: /jobs/{id}`, body `{job_id, status:"QUEUED", video_id}`. Bad input → `4xx {"error":{"code","message"}}`. |
| `GET /jobs/{id}` | Read-only status: `status, progress, video_title, duration_sec, warnings, error, result`. Unknown id → `404`. |
| `GET /jobs/{id}/results` | `409` until the job is finished, then `{status, result:{notion_page_url, frames_requested, frames_published}, warnings}`. |
| `GET /healthz` | Checks gateway, RabbitMQ, PostgreSQL and Redis. |
| `POST /jobs/{id}/retry`, `DELETE /jobs/{id}` | Retry a failed job as a new job; cancel a job that is still `QUEUED`. |
| `GET /metrics` | Prometheus metrics. `docker compose --profile monitoring up -d` adds Prometheus (`:9090`) and Grafana (`:3000`). |

Job states: `QUEUED → TRANSCRIBING → SUMMARIZING → EXTRACTING_FRAMES → PUBLISHING → COMPLETED | COMPLETED_WITH_WARNINGS`,
or `FAILED` (not retryable) / `DEAD_LETTERED` (retry budget used up).

## Failure handling (live demo)

| Scenario | Trigger | Result |
|---|---|---|
| HTTP 429 from Claude/Notion/YouTube | `make chaos-429 API=llm N=2`, then submit | Job waits and retries with 10s/30s/90s backoff; it is never shown as failed |
| Worker crash | `make kill-worker JOB=<id>` (SIGKILL) | RabbitMQ redelivers the unacked message; another worker resumes from the last checkpoint; one Notion page |
| Frame extraction fails | `make frame-fail SPEC=first:2` (or `all`) | Page published with the remaining frames, status `COMPLETED_WITH_WARNINGS` (text-only if all fail) |
| Retries exhausted | `make dlq` (unavailable video) | After the backoff schedule, job is `DEAD_LETTERED` and the message sits in `jobs.dlq` |
| Scaling / burst | `make scale WORKERS=4`, `make burst` | Jobs spread across workers; a shared Redis token bucket keeps Claude from ever returning a 429 |

## How it works (design choices)

* **Broker: RabbitMQ.** Durable queues, manual acks with `prefetch=1`, publisher confirms. A message is acked only after the job
  is finished, or after its retry/DLQ copy is confirmed by the broker, so a killed worker's job is redelivered (at-least-once).
  Retries use per-delay queues (TTL + dead-letter back to `jobs.work`), so waiting jobs never occupy a worker.
* **State: PostgreSQL.** A `jobs` table, per-stage checkpoints (`job_artifacts`) and one row per frame. Each stage is
  idempotent, and the Notion page carries a short job tag, so a redelivered job can't create a duplicate page.
* **Global rate limit: Redis token buckets** (one atomic Lua script per API), set below the providers' limits so 429s are
  avoided rather than handled. Waiting for our own limiter re-queues a job without using up a retry attempt.
* **Workers: one generic image runs all four stages** with a checkpoint after each, and scales by adding replicas.
* **LLM: Claude** with JSON-schema outputs, chunking for long transcripts, and a vision pass that re-captions every frame
  from its real pixels. **Frames** are grabbed straight from the stream with ffmpeg; a failed frame is omitted, never replaced.
* **Constraints:** videos over 20 minutes are rejected; every image on the page is a real frame with a caption.

## Development

```bash
make test        # unit tests (no network)        ruff check .    # lint
INTEGRATION=1 pytest tests/integration            # against a running stack
make logs | make health | make down
```

Layout: `common/` shared code, `gateway/` FastAPI, `worker/` pipeline, `extension/` Chrome MV3, `monitoring/`, `scripts/demo/`, `tests/`.

**Troubleshooting:** "Server unreachable" → run `make up` and `make health`. YouTube sign-in/bot checks → wait, change
network, or save a `cookies.txt` as `./cookies/youtube.txt` and restart the workers. "Notion page not shared" → reconnect
Notion in the extension and keep the destination page shared with the connection.
