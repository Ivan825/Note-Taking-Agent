# Architecture

## Components

```
┌──────────────────────────┐   POST /jobs (token, url)    ┌──────────────┐  publish (confirm)  ┌────────────────────────┐
│ Chrome extension (MV3)   │ ───────────────────────────▶ │ API gateway  │ ──────────────────▶ │ RabbitMQ               │
│  content.js  "Take Notes"│ ◀─────── GET /jobs/{id} ──── │ (FastAPI)    │                     │  jobs.work  (durable)  │
│  background.js  OAuth    │                              └──────┬───────┘                     │  jobs.delay.{10,30,90}s│
│  popup  progress UI      │                                     │ reads                       │  jobs.dlq              │
└──────────────────────────┘                                     ▼                             └───────────┬────────────┘
                                                         ┌──────────────┐  checkpoints   consume (ack late) │
                                                         │ PostgreSQL   │ ◀──────────────────────────┐      ▼
                                                         │ jobs/artifacts│                ┌──────────────────────────┐
                                                         │ /frames      │                 │ worker × N (same image)  │
                                                         └──────────────┘                 │ transcribe → summarize → │
                                                         ┌──────────────┐  token buckets  │ frames → publish         │
                                                         │ Redis        │ ◀────────────── │                          │
                                                         └──────────────┘                 └────┬───────┬───────┬─────┘
                                                                                               ▼       ▼       ▼
                                                                                          YouTube   Claude   Notion
```

Each box is its own container (`docker compose ps`), so any one of them can be killed or scaled
without touching the others.

## Job lifecycle

```
QUEUED → TRANSCRIBING → SUMMARIZING → EXTRACTING_FRAMES → PUBLISHING → COMPLETED
                                                                      ↘ COMPLETED_WITH_WARNINGS
any non-terminal state → FAILED          (non-retryable error)
any non-terminal state → DEAD_LETTERED   (retry budget exhausted)
```

`common/notes_common/states.py` holds the allowed-transition table. Every status change goes
through `jobstore.transition()`, which locks the row and rejects illegal moves. Re-entering the
current stage is allowed because that is how a redelivered job resumes.

## Delivery semantics

* **At-least-once.** Workers consume with `auto_ack=False` and `prefetch=1`. A message is acked only
  after one of these:
  1. the job reached a terminal state in Postgres, or
  2. its retry copy was published to a delay queue *and confirmed by the broker*, or
  3. its copy was confirmed in `jobs.dlq`.
* **Crash recovery.** The pipeline runs in a worker thread while the main thread keeps the AMQP
  connection alive with heartbeats. When a process dies, even from `SIGKILL`, the TCP connection
  drops and RabbitMQ immediately requeues the unacked message to another consumer.
* **Idempotent stages.** Each stage writes its output to Postgres before the next stage starts:
  `meta`, `transcript`, `chunk:N`, `synthesis`, one `frames` row per timestamp, and the Notion upload
  ids. A redelivered message skips everything that is already done.
* **No duplicate Notion pages.** The page is created in a single API call. Its title carries a
  `#<job8>` tag and its id is saved right away. Before creating a page, the worker first checks
  for a saved id, then scans the parent for the tag.
* **Broker restarts.** RabbitMQ runs with a fixed hostname, so its node name and its data directory
  stay the same across container re-creation, and durable queues keep their persistent messages. A
  worker that loses its broker connection exits. Its unacked job is redelivered, and Docker's
  `restart: unless-stopped` brings the worker back once the broker is reachable. A worker you
  deliberately `docker kill` stays down.
* **Stale copies are dropped.** The message carries `x-attempt` and the job row stores the current
  attempt. A copy with a lower attempt number is acked and ignored.

## Retry policy

| Error class | Examples | Action |
|---|---|---|
| `RateLimited` | HTTP 429 from Claude, Notion or YouTube; our own limiter saturated | Re-queue into `jobs.delay.<N>s`, honouring `Retry-After`. The status stays on the current stage and `status_detail` says "retrying in Ns". |
| `TransientError` | 5xx, network errors, video unavailable or private | Same as `RateLimited`. |
| `PermanentError` | too long, no transcript, bad Notion token | `FAILED` immediately |
| budget exhausted | `attempt > MAX_ATTEMPTS` | publish to `jobs.dlq`, then `DEAD_LETTERED` |

The backoff comes from `BACKOFF_SECONDS` (default `10,30,90,90`). One durable delay queue is
declared per step, with `x-message-ttl` and dead-lettering back to `jobs.work`, so waiting jobs
don't occupy a worker.

## Global rate limiting

`common/notes_common/ratelimit.py` keeps one token bucket per external API in Redis. A Lua script
updates each bucket atomically using Redis' clock, and every worker must take tokens before calling
the API:

| Bucket | Default | Purpose |
|---|---|---|
| `llm_requests` | `LLM_RPM`=40/min, burst `RPM/6` | stay under Claude's requests-per-minute limit |
| `llm_input_tokens` | `LLM_ITPM`=40k/min, cost = estimated prompt tokens | stay under Claude's input-tokens-per-minute limit |
| `notion` | `NOTION_RPS`=2.5/s | Notion allows about 3 requests/second per integration |
| `youtube` | `YOUTUBE_RPS`=1/s | stay polite to YouTube |

A wait of up to about 20 seconds is spent sleeping. A longer wait turns into a `RateLimited` re-queue.
The budgets are set below the provider limits, so a burst of jobs drains through the queue without
Claude ever returning a 429.

## Pipeline details

1. **Transcribe.** `yt-dlp` reads the video's metadata: its true duration, title, and a ≤720p stream
   URL that is cached in Redis. Videos over `MAX_VIDEO_SECONDS` are rejected with `VIDEO_TOO_LONG`.
   This is the second check; the gateway already rejects them when the extension supplies the
   duration. `youtube-transcript-api` then fetches the timestamped captions, preferring English and
   otherwise translating.
2. **Summarize.** Caption snippets are grouped into lines of about 15 seconds, each prefixed with
   `[m:ss]`. A transcript longer than `LLM_CHUNK_TOKENS` is split on line boundaries, and each chunk
   gets one Claude call constrained by a JSON schema (`output_config.format`). Partial results are
   merged in time order, plus one small call that writes the overall overview and takeaways. Flagged
   frames are clamped to the video's length, de-duplicated and capped at `LLM_MAX_FRAMES`.
3. **Extract frames.** For each timestamp,
   `ffmpeg -ss T -i <stream> -frames:v 1` decodes exactly one frame from the remote stream, three
   frames at a time. If capture fails, the worker retries once with a fresh stream URL. A blank
   transition frame is nudged forward 3 seconds. Each captured frame is then shown to Claude
   (vision) together with the nearby narration, so the caption describes what is actually visible.
   A frame that can't be captured is marked `failed` and later omitted, never substituted.
4. **Publish.** Frames are uploaded with Notion's File Upload API, and the page is created in one
   request: a source callout, overview, key takeaways, timestamped sections with bullets, and images
   placed under the section they belong to. Every image caption reads
   `[m:ss] <what it shows>. Why it matters: <reason>`. When a frame was omitted, a callout says so.

## Notion OAuth

* A public connection. The extension runs
  `chrome.identity.launchWebAuthFlow(https://api.notion.com/v1/oauth/authorize…)` with the redirect
  URI `https://<extension-id>.chromiumapp.org/notion`. The extension id is pinned with `key` in
  `manifest.json`.
* The code is exchanged by the gateway (`POST /oauth/notion/exchange`), so the client secret never
  ships inside the extension. The resulting token goes back to the extension, is stored in
  `chrome.storage.local`, and is sent with every job. The gateway never stores it. It travels inside
  the queued message encrypted with Fernet (`TOKEN_ENC_KEY`).
* **Default destination.** During the exchange the gateway finds or creates a private
  workspace-level page called **"Video Notes"**, which public connections are allowed to create. Its
  id is returned as `default_parent_id`, and each video's notes become a sub-page of it. If a
  workspace-level page can't be created, the first page the user shared during OAuth is used
  instead. A `notion_parent_id` supplied by an API caller overrides the default and may be a page
  or a database.
