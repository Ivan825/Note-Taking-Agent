# Teammate tasks: brief for your coding assistant

> **How to use this file:** give this whole document to your AI coding assistant (for example,
> paste it into Claude Code) from the repository root. Do the tasks one at a time, in order, and
> review each diff before committing.

## 0. Ground rules (read first)

### Commit and authorship rules (mandatory)
- Every commit must be authored by **your own git identity**. Check `git config user.name` and
  `git config user.email` before the first commit.
- **Do not** add `Co-Authored-By:` trailers, "Generated with …" lines, emoji signatures or any
  other AI/bot attribution to commit messages, PR descriptions or code comments. The repository
  should show only human contributors.
- Write commit messages in the imperative mood ("Add DLQ replay endpoint"), with one logical
  change per commit.
- Work on one branch per task (`feat/dlq-admin`, `feat/retry-cancel`, …), then open a PR into
  `main` or merge it with a fast-forward after review. Never force-push `main`.
- Never commit `.env`, tokens or keys. `.env` is gitignored, so keep it that way.

### Do-not-break rules
- The REST contract in `docs/spec.pdf` §5 is graded. **Don't change** the shape of `POST /jobs`,
  `GET /jobs/{id}` or `GET /jobs/{id}/results`. You may only add new fields or new endpoints.
- Status names and the state machine (`common/notes_common/states.py`) are graded (§4). Don't add
  or rename statuses.
- Every image on a Notion page must stay a real frame with an LLM caption (Constraint B).
- `make test` must pass before every commit. Add tests for whatever you build.
- The extension must keep loading as an **unpacked extension with no build step**: plain
  JS/HTML/CSS, ES modules only.

### Orientation (what already exists)
| Path | What it is |
|---|---|
| `gateway/app/` | FastAPI gateway; routes in `routes/` (`jobs.py`, `oauth.py`, `health.py`, `debug.py`); the error body helper is in `errors.py` (`ApiError`) |
| `worker/worker/` | Worker process: `main.py` (RabbitMQ consumer), `pipeline.py` (stage runner + retry policy), `stages/*.py`, `notion_blocks.py` |
| `common/notes_common/` | Shared code: `config.py` (env settings), `models.py` (`Job`, `Artifact`, `Frame`), `broker.py` (queues + publish helpers), `ratelimit.py`, `chaos.py`, `notion_api.py` |
| `extension/` | MV3 extension: `content.js` (player button), `background.js` (OAuth, submit, polling), `popup.*`, `options.*`, `lib/common.js` (API helper, friendly errors) |
| `tests/` | pytest unit tests (SQLite + `FakePublisher`; see `tests/conftest.py`) |
| `docs/ARCHITECTURE.md` | How delivery, retries, rate limiting and OAuth work. **Read it before starting.** |

To run the stack: `cp .env.example .env`, fill in the secrets (ask the repo owner), run `make up`,
then load `extension/` unpacked in `chrome://extensions`. To run the tests: `make test`.

---

## Task 1: Dead-letter queue admin (inspect + replay)
**Branch:** `feat/dlq-admin`

Build:
- `GET /admin/dlq`: list dead-lettered jobs: job id, video id/title, error code/message, attempts,
  `finished_at`. Read the `jobs` table (`status == DEAD_LETTERED`) and the message count of
  `jobs.dlq` (see `Publisher.queue_stats()` in `common/notes_common/broker.py`).
- `POST /admin/dlq/{job_id}/replay`: reset the job to `QUEUED`. Set `attempt = 1`, clear
  `error`/`next_retry_at`, and keep existing artifacts so it resumes. Then publish a fresh work
  message.
  - This is the only place a terminal state may be left. Do it with an explicit admin helper; don't
    loosen `can_transition`.
  - The original message body (with the encrypted Notion token) is in `jobs.dlq`. Use
    `basic_get` to find the message whose `job_id` matches, re-publish it with
    `broker.publish_work`, and ack it out of the DLQ. If it isn't found, return
    `409 {"error":{"code":"DLQ_MESSAGE_NOT_FOUND",...}}`.
- `GET /admin`: a small server-rendered HTML page (no build step) that lists the DLQ, with a Replay
  button per job.
- Protect everything under `/admin` with an `X-Admin-Token` header matching a new `ADMIN_TOKEN`
  env var. Return 401 if it's missing or wrong. If `ADMIN_TOKEN` is unset, don't mount the router.
  Document the variable in `.env.example` and the README.

Acceptance:
- `make dlq` produces a `DEAD_LETTERED` job, and it appears in `GET /admin/dlq`.
- Replaying a job whose video now works takes it through to `COMPLETED`.
- Unit tests cover the auth checks, the replay of a non-DLQ job (409), and the listing.

## Task 2: Retry and cancel from the popup
**Branch:** `feat/retry-cancel`

Build:
- `POST /jobs/{id}/retry` is allowed only for `FAILED` or `DEAD_LETTERED` jobs; return 409
  otherwise.
  - The gateway doesn't store the Notion token, so the request body must include `notion_token`
    (the extension has it).
  - Create a **new** job for the same video (new `job_id`) and return `202` with the same body
    shape as `POST /jobs`. Don't mutate the old job.
- `DELETE /jobs/{id}` cancels a job that is still `QUEUED`. Mark it `FAILED` with
  `error.code = "CANCELLED"`. The worker must then drop the message; it already drops messages for
  terminal jobs (see the top of `pipeline.run`). Return 409 if the job has already started.
- In the popup (`extension/popup.js`):
  - Add a **Retry** button on failed or dead-lettered job cards. It sends a `RETRY_JOB` message to
    `background.js`, which calls the endpoint with the stored token and puts the new job in the list.
  - Add a **Cancel** button on queued job cards.
  - Add `CANCELLED` to `ERROR_TEXT` in `extension/lib/common.js` so the user sees friendly text.

Acceptance: both actions work from the popup, the old job card stays readable, and there are
tests for 202/409/404.

## Task 3: Job history, notifications and badge polish
**Branch:** `feat/history-notifications`

Build:
- `extension/history.html` (+ `history.js`), opened from a "History" link in the popup footer. It
  shows every job stored in `chrome.storage.local.jobs`, newest first, with a search box (title)
  and a status filter (All / Done / Warnings / Failed). It also has a "Clear history" button that
  removes only terminal jobs. Raise `MAX_JOBS` in `background.js` from 25 to 100.
- Desktop notifications: add the `"notifications"` permission. When `pollJobs()` sees a job become
  terminal, show `chrome.notifications.create` with the video title and the outcome. Clicking a
  completed job's notification opens its Notion page.
- Badge: blue with the active count while jobs run, then a green ✓ for about 10 seconds after a
  job completes (red ! on failure).

Acceptance: a notification appears when a job finishes even with the popup closed, the history
filters work, and no innerHTML is used with server-provided strings.

## Task 4: Options page (Notion destination picker and sign-out)
**Branch:** `feat/options-notion`

Build:
- On the options page, show the connected workspace and add a **Disconnect Notion** button that
  sends the existing `DISCONNECT_NOTION` message.
- Add a **Destination page** picker:
  - Add a gateway endpoint `POST /notion/pages/search` with body `{notion_token, query}`. It
    proxies Notion `POST /v1/search` (pages only) through `NotionClient` and returns
    `[{id, title, icon}]`. The token is never stored.
  - The options page lists the results. Choosing one saves `notion.default_parent_id` in
    `chrome.storage.local`; new jobs then use it as `notion_parent_id`.
  - Show the currently selected page and a "Use default (Video Notes)" reset.

Acceptance: picking a page makes the next job publish under it, and disconnecting makes the next
**Take Notes** start OAuth again.

## Task 5: Integration tests and CI
**Branch:** `feat/ci`

Build:
- `.github/workflows/ci.yml`, run on push and PR. Jobs:
  1. `ruff check .`. Add a `ruff.toml`, line length 120, and fix any findings.
  2. Unit tests: `pip install -r requirements-dev.txt && pytest -q tests`.
  3. Integration: `docker compose up -d --build` with a CI `.env` (dummy `ANTHROPIC_API_KEY`, a
     generated `TOKEN_ENC_KEY`, `ENABLE_DEBUG_ENDPOINTS=true`), then run `tests/integration/` and
     `docker compose down -v`.
- `tests/integration/` (pytest, using `httpx` against `http://localhost:8000`). It must not call
  real external APIs:
  - `/healthz` is 200 with every component ok.
  - `POST /jobs` with an unavailable video id and `MAX_ATTEMPTS=2`, `BACKOFF_SECONDS=2` (set in the
    CI `.env`) reaches `DEAD_LETTERED` within 30 seconds.
  - `GET /jobs/{id}/results` returns 409 while the job is running.
  - Killing a worker container (`docker kill`) while a job is queued still lets the job reach a
    terminal state.
- Add a CI status badge and a coverage badge (`pytest-cov`) to the top of `README.md`.

Acceptance: CI is green on `main`.

## Task 6: Observability
**Branch:** `feat/metrics`

Build:
- `GET /metrics` on the gateway in Prometheus text format, using `prometheus_client`. Expose:
  - `jobs_total{status}`: a count by status from Postgres.
  - `queue_depth{queue}`: the depth of `jobs.work`, the delay queues and `jobs.dlq`.
  - `stage_duration_seconds` (a histogram per stage). For this, the worker writes
    `stage_started_at`/`stage_finished_at` into a new `stage_timings` JSON column on `Job`; add it in
    `models.py`. `create_all` won't alter an existing table, so document
    `docker compose down -v` or add a one-line `ALTER TABLE … ADD COLUMN IF NOT EXISTS` at startup.
  - `ratelimit_wait_seconds_total{bucket}`. Increment a Redis counter in `ratelimit.acquire()`.
- An optional compose **profile** `monitoring` with Prometheus and Grafana, plus a provisioned
  dashboard JSON in `monitoring/`. `docker compose --profile monitoring up` must work; the default
  `docker compose up` must not start them.

Acceptance: the dashboard shows jobs per status, queue depth and p50/p95 stage durations during
`make burst`.

---

### Definition of done (every task)
- [ ] The feature works end to end in the running stack.
- [ ] `make test` passes, and new tests are added.
- [ ] README updated: new env vars, endpoints and how to use them.
- [ ] Commits are authored by you only, with no AI/bot attribution lines.
