# Video Note Taker: a distributed multimodal note-taking agent

Click **Take Notes** on any YouTube video (up to 20 minutes). A few minutes later a Notion page
appears with structured study notes and **real frames from the video**: diagrams and slides
captured at the moments that matter, each with a caption explaining what it shows and why it
was picked.

```
Chrome extension ──▶ API gateway ──▶ RabbitMQ ──▶ worker × N ──▶ YouTube · Claude · Notion
                        │                            │
                    PostgreSQL (job state + checkpoints)    Redis (global rate limits)
```

* **Base URL:** `http://localhost:8000`. Interactive API docs are at `/docs`, the health check at `/healthz`.
* **RabbitMQ UI:** `http://localhost:15672` (user `notes`, password `notes`). It shows `jobs.work`, the delay queues and `jobs.dlq`.
* Deep dive: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Spec: [`docs/spec.pdf`](docs/spec.pdf).

---

## Quick start

**Prerequisites:** Docker Desktop with Compose v2, and Chrome 116 or later. Run it on a
residential network: YouTube blocks most datacenter IPs.

```bash
cp .env.example .env
#   fill in ANTHROPIC_API_KEY, NOTION_CLIENT_ID, NOTION_CLIENT_SECRET, TOKEN_ENC_KEY
#   (TOKEN_ENC_KEY: python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())")
make up            # = docker compose up -d --build, then waits for /healthz
```

Then load the extension:

1. Open `chrome://extensions` and turn on **Developer mode**.
2. Click **Load unpacked** and select the `extension/` folder. There's no build step.
3. Open a YouTube video and click **Take Notes** in the player toolbar. The first click opens
   Notion's consent screen once. After that the token is stored and you won't be asked again.
4. Click the extension icon to follow progress: Queued → Transcribing → Summarizing → Extracting
   Frames → Publishing → Done. Then click **View in Notion**.

The extension's ID is fixed at `cfajbkllcohlnhfahfccoiopccagglon` (by the `key` in
`manifest.json`), so the Notion redirect URI is always
`https://cfajbkllcohlnhfahfccoiopccagglon.chromiumapp.org/notion`.

### Setting up your own Notion connection (only if you're not using the provided credentials)

1. Go to <https://www.notion.so/profile/integrations>, choose **New connection**, then **OAuth**.
2. Add the redirect URI above.
3. Under capabilities, choose read, update and insert content, and *no user information*.
4. Copy the client ID and secret into `.env`.

### Default Notion destination

`notion_parent_id` is optional. If it's omitted, notes go to a private page called **"Video Notes"**
at the top level of the user's workspace. The gateway creates that page (or finds the existing one)
during the one-time OAuth flow, and every video becomes a sub-page under it. If the workspace
doesn't allow a top-level page, the first page the user shared during OAuth is used instead. API
callers can pass any page id, or a database id, as `notion_parent_id`.

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | **required** | Claude API key used by the workers |
| `LLM_MODEL` | `claude-haiku-5-5` | Claude model name (e.g. `claude-sonnet-5-5` for richer notes) |
| `LLM_EFFORT` | `medium` | Claude effort level: `low`, `medium` or `high` |
| `LLM_RPM` | `40` | **Configured global LLM rate limit**: requests/minute shared by all workers |
| `LLM_ITPM` | `40000` | Global input-tokens/minute budget shared by all workers |
| `LLM_CHUNK_TOKENS` | `60000` | Transcripts longer than this are split into chunks and merged in order |
| `LLM_MAX_FRAMES` | `8` | Maximum number of key frames per video |
| `FRAME_VISION_CHECK` | `true` | Claude re-captions each captured frame from its actual pixels |
| `NOTION_CLIENT_ID` | **required** | Notion OAuth client ID (public connection) |
| `NOTION_CLIENT_SECRET` | **required** | Notion OAuth client secret. It stays on the gateway and is never shipped in the extension |
| `NOTION_REDIRECT_URI` | `https://cfajbkllcohlnhfahfccoiopccagglon.chromiumapp.org/notion` | Must match the connection's configured redirect URI |
| `NOTION_RPS` | `2.5` | Global Notion requests/second (Notion allows about 3) |
| `DEFAULT_NOTION_PAGE_TITLE` | `Video Notes` | Title of the default destination page |
| `TOKEN_ENC_KEY` | **required** | Fernet key that encrypts the Notion token inside queued messages |
| `RABBITMQ_URL` | `amqp://notes:notes@rabbitmq:5672/%2F` | **Broker connection string** |
| `DATABASE_URL` | `postgresql+psycopg://notes:notes@postgres:5432/notes` | Job state store |
| `REDIS_URL` | `redis://redis:6379/0` | Shared rate-limit buckets, stream-URL cache, chaos flags |
| `MAX_ATTEMPTS` | `5` | **Retry budget**: attempts before a job is `DEAD_LETTERED` |
| `BACKOFF_SECONDS` | `10,30,90,90` | **Backoff schedule**: delay before retry 1, 2, …; the last value repeats |
| `YOUTUBE_RPS` | `1` | Global YouTube requests/second |
| `MAX_VIDEO_SECONDS` | `1200` | Constraint A: the 20-minute cap |
| `FRAME_TIMEOUT_SECONDS` | `60` | Timeout for one ffmpeg frame capture |
| `ENABLE_DEBUG_ENDPOINTS` | `true` | Exposes `POST /debug/chaos` for fault injection during the demo |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Public URL of the gateway |
| `LOG_LEVEL` | `INFO` | Log verbosity |

---

## REST API

| Method & path | Purpose |
|---|---|
| `POST /jobs` | Submit `{youtube_url, notion_token, notion_parent_id?}` → `202` + `Location: /jobs/{id}`, body `{job_id, status: "QUEUED", video_id}`. Invalid input → `4xx {"error":{"code","message"}}`. No pipeline work happens inside the request. Optional `duration_sec` / `video_title` hints let it reject videos over 20 minutes immediately (`422 VIDEO_TOO_LONG`). |
| `GET /jobs/{id}` | Read-only status: `{job_id, status, progress, video_title, duration_sec, warnings, error, result}`, plus UI extras (`status_detail`, `attempt`, `next_retry_at`, `worker_id`, `recoveries`). Unknown id → `404`. |
| `GET /jobs/{id}/results` | `409` until the job is terminal. Then `{status, result: {notion_page_url, frames_requested, frames_published}, warnings, error}`. |
| `GET /healthz` | Checks that the gateway, RabbitMQ, PostgreSQL and Redis are all reachable, plus queue depth and consumer count. |
| `GET /oauth/notion/config`, `POST /oauth/notion/exchange` | Used by the extension for the one-time OAuth. The exchange returns the token to the extension and doesn't store it. |
| `GET/POST/DELETE /debug/chaos` | Fault injection for the demo (see below). |

Example:

```bash
curl -s -XPOST localhost:8000/jobs -H 'content-type: application/json' \
  -d '{"youtube_url":"https://www.youtube.com/watch?v=aircAruvnKk","notion_token":"<token>"}'
```

---

## Failure handling: live demo script

Each scenario has a one-line command. `scripts/demo/watch_job.sh <job_id>` prints status changes as
they happen.

| Scenario | How to trigger | What you see |
|---|---|---|
| **HTTP 429** from Claude, Notion or YouTube | `make chaos-429 API=llm N=2` (or `notion`, `youtube`), then submit a job | The job stays in its stage. The popup shows *"Rate-limited by Claude. Retrying in 10s (attempt 2 of 5)"*, then the job continues. A real 429 goes through the same code path and honours `Retry-After`. |
| **Worker crash** | Submit a job. While it's extracting frames, run `make kill-worker JOB=<id>` (`docker kill -s KILL`) | RabbitMQ requeues the unacked message at once and another worker resumes from the last checkpoint. Exactly one Notion page is created. `GET /jobs/{id}` shows `recoveries: 1` and the new `worker_id`. |
| **Graceful degradation** | `make frame-fail SPEC=first:2` (or `SPEC=all`), then submit | The page is published with the frames that did succeed and the status is `COMPLETED_WITH_WARNINGS`, with one `FRAME_EXTRACTION_FAILED` warning per omitted frame. With `all`, the page is text only. Reset with `make frame-fail SPEC=off`. |
| **Dead-letter queue** | `make dlq` submits an unavailable video | It retries after 10s, 30s, 90s and 90s, then goes to `DEAD_LETTERED` (about 3.7 minutes). The message is visible in `jobs.dlq` in the RabbitMQ UI. Lower `BACKOFF_SECONDS`/`MAX_ATTEMPTS` for a faster demo. |
| **Scaling** | `make scale WORKERS=4` (= `docker compose up -d --scale worker=4`) | `/healthz` reports 4 consumers. Jobs spread across them. |
| **Burst** | Click **Take Notes** on several videos in a row, or `NOTION_TOKEN=… make burst` | Every job gets its own card. The shared Redis token bucket paces the Claude calls, so the queue drains without a single 429 from Claude. |
| **Too long** | Open a video over 20 minutes and click **Take Notes** | It's rejected immediately and no job runs. If a client skips the duration hint, the worker rejects it with `FAILED / VIDEO_TOO_LONG` before doing any work. |

---

## Design decisions (spec §10)

* **Broker: RabbitMQ.** It gives durable queues, manual acks with automatic redelivery when a
  consumer's connection dies, publisher confirms, and dead-lettering. Per-step delay queues
  (TTL + dead-letter back into `jobs.work`) implement exponential backoff without blocking a worker.
* **Job state: PostgreSQL.** The `jobs` table holds status, progress and results. `job_artifacts`
  holds per-stage checkpoints (metadata, transcript, per-chunk LLM output, the final synthesis).
  `frames` holds one row per flagged timestamp, with the captured JPEG and its Notion upload id. An
  explicit transition table (`states.py`) is enforced on every status change, under a row lock.
* **Global LLM rate limit: Redis token buckets.** A Lua script updates them atomically with Redis'
  clock. There are separate buckets for Claude requests/min and input tokens/min, Notion
  requests/sec and YouTube requests/sec. The limits are set below the providers' limits, so 429s
  are avoided rather than handled. Any 429 that still happens is re-queued with backoff.
* **Worker topology: one generic worker image runs all four stages for a job,** checkpointing
  after each one. This keeps a job's data local, avoids stage-to-stage queue hops, and scales by
  adding replicas. Crash recovery resumes at the exact stage, and for frames at the exact frame.
* **LLM: Claude** (`claude-haiku-5-5` by default, configurable). It uses JSON-schema structured
  outputs (`output_config.format`), with long-transcript chunking and an in-order merge. A second
  vision pass re-captions every captured frame from its real pixels, so the captions match the
  images (Constraint B).
* **Transcripts:** `youtube-transcript-api`, preferring English and translating otherwise. Duration
  and metadata come from `yt-dlp`, which needs Deno (bundled in the worker image).
* **Frames:** `ffmpeg -ss T -i <≤720p stream URL from yt-dlp> -frames:v 1` grabs one real frame
  straight from the stream, with no download. A failed frame is retried with a fresh stream URL,
  and a blank transition frame is nudged forward 3 seconds.
* **Extension:** plain Manifest V3 with ES modules and no build step. The toolbar button is
  re-injected on YouTube's SPA navigation events and DOM mutations. Errors are mapped to readable
  messages, and raw JSON is never shown.

---

## Spec compliance checklist

**Components (§1.1)**
- [x] Browser extension, gateway (no pipeline work), RabbitMQ (durable, at-least-once, redelivery) and ≥ 2 workers, each a separate container

**Client (§2)**
- [x] Content script injects **Take Notes** into the player toolbar and sends the video ID to the background script
- [x] The button keeps working while navigating between videos (SPA events + MutationObserver)
- [x] One-time Notion OAuth; the token is stored in `chrome.storage.local`, so there's no re-prompt
- [x] The background script submits jobs and relays status to the popup
- [x] The popup shows Queued → Transcribing → Summarizing → Extracting Frames → Publishing → Done, plus a one-click **View in Notion** button
- [x] Failures, "completed with warnings" and "waiting to retry" are shown in plain language, never as raw JSON

**Pipeline (§3)**
- [x] Timestamped transcript, the true duration, and Constraint A enforced
- [x] Claude summary bullets plus flagged timestamps, each with a caption and a reason; chunking with an in-order merge
- [x] One real frame per timestamp, captured from the video stream
- [x] Notion page with headings, bullets, and uploaded images with captions

**Lifecycle (§4):** [x] all 9 statuses, with the transition table enforced

**REST (§5)**
- [x] `POST /jobs`: validation, 202 + `Location`, 4xx error body, optional `notion_parent_id` with a documented default
- [x] `GET /jobs/{id}`: read-only and idempotent; 404 for an unknown id
- [x] `GET /jobs/{id}/results`: 409 before terminal; an absolute Notion URL plus frame counts after

**Failure handling (§6)**
- [x] 429 from any of the 3 APIs is re-queued with exponential backoff (10/30/90 s), shown as "waiting"
- [x] Worker SIGKILL leads to redelivery and a resume from the checkpoint, with no lost job and no duplicate page
- [x] Graceful degradation leads to `COMPLETED_WITH_WARNINGS`, or a text-only page if every frame fails
- [x] Bounded, configurable retries lead to `jobs.dlq` and `DEAD_LETTERED`

**Constraints (§7)**
- [x] A: videos ≤ 20 minutes are supported; longer ones are rejected at submission and again in the worker
- [x] B: only real frames; each one is paired with an LLM caption explaining what it shows and why; failed frames are omitted, never substituted

**Deployment (§8)**
- [x] An unpacked extension with no build step
- [x] One `docker-compose.yml`; `docker compose up --scale worker=N`
- [x] This README: base URL, default destination, every environment variable
- [x] `GET /healthz` checks the gateway, broker, database and Redis

---

## Repository layout

```
common/notes_common/   shared: settings, models, state machine, broker topology, rate limiter, Notion client, chaos hooks
gateway/app/           FastAPI gateway (jobs, oauth, healthz, debug routes)
worker/worker/         consumer + pipeline (stages/transcribe, synthesize, frames, publish)
extension/             Chrome MV3 extension (content script, background worker, popup, options)
scripts/demo/          one-command failure demos
tests/                 unit tests (make test)
docs/                  architecture, spec, teammate task brief
```

## Development

```bash
make test          # unit tests (SQLite + fake broker, no network)
make logs          # follow gateway + worker logs
make health        # pretty-print /healthz
make down          # stop (keeps data); make clean also removes volumes
```

## Troubleshooting

* **"Server unreachable" in the popup.** Run `make up`, then check `make health`. If the gateway
  isn't on `localhost:8000`, change the URL in the extension's Settings.
* **Jobs keep retrying with "YouTube asked for a sign-in check".** YouTube is rate-limiting your IP.
  Wait a few minutes, or use a different network.
* **"Notion page not shared".** Reconnect Notion from the popup's chip, and keep the "Video Notes"
  page (or the page you chose) shared with the connection.
