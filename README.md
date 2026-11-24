# LLM Chatbot with Key Failover and Response Caching

A full-stack chat application: **React** frontend, **FastAPI** backend, **LiteLLM** gateway with a multi-key pool (automatic failover on 429 / quota / 5xx) and LiteLLM's built-in response cache, with **SQLite** session storage.

```
/backend     FastAPI app, LiteLLM gateway, SQLite storage, pytest suite
/frontend    React (Vite) chat UI, served by nginx in Docker
docker-compose.yml   backend :8000 + frontend :3000 + named volume at /data
Makefile             make up / make down / make test
.env.example         every environment variable with placeholder values
```

---

## 1. Live URLs

| Tier | URL |
|------|-----|
| Frontend | https://lyftr-chatbot-chi.vercel.app |
| Backend  | https://lyftr-chatbot-8sad.onrender.com |
| Health   | https://lyftr-chatbot-8sad.onrender.com/health |
| API docs (Swagger) | https://lyftr-chatbot-8sad.onrender.com/docs |
| Repository | https://github.com/AnishBhardwajTech/lyftr-chatbot |
| Demo video | `<add link>` |

> The backend runs on Render's free tier, which sleeps after ~15 minutes of inactivity. The first request after a pause can take ~50 seconds while it wakes up; later requests are fast.

---

## 2. Run locally: with Docker and without

### With Docker (recommended)

```bash
cp .env.example .env          # then put at least 2 real keys in LLM_API_KEYS
make up                       # builds and starts both services
# Frontend: http://localhost:3000   Backend: http://localhost:8000/health
make down
```

The SQLite database lives at `/data/app.db` inside the named volume `app-data`, so history survives `make down && make up`. `make clean` also deletes the volume.

**No keys yet?** Set `MOCK_LLM=true` in `.env`. LiteLLM then returns canned replies offline, and the failover, caching, token and history features all still work, so you can try the whole stack first.

### Without Docker

Backend (Python 3.11+):

```bash
cd backend
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
export LLM_API_KEYS="key-one,key-two" ENABLE_FAILURE_SIMULATION=true \
       DATABASE_URL="sqlite:///./data/app.db" ALLOWED_ORIGINS="http://localhost:3000"
# Windows PowerShell: $env:LLM_API_KEYS="key-one,key-two"  (and so on)
uvicorn app.main:app --reload --port 8000
python -m pytest -q                                   # 32 tests, offline, no keys needed
```

Frontend (Node 18+):

```bash
cd frontend
npm install
echo "VITE_API_URL=http://localhost:8000" > .env
npm run dev                                           # http://localhost:3000
```

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `LLM_API_KEYS` | — | Comma-separated key pool (≥ 2). Ids are `key_1`, `key_2`, … in this order. |
| `LLM_MODEL` | `gemini/gemini-3.5-flash-lite` | LiteLLM model used by every key |
| `LLM_MODELS` | — | Optional: one model per key, aligned with `LLM_API_KEYS` (multi-model pool) |
| `DATABASE_URL` | `sqlite:///./data/app.db` (Docker: `sqlite:////data/app.db`) | SQLite location |
| `ALLOWED_ORIGINS` | `http://localhost:3000,http://localhost:5173` | CORS origins |
| `ENABLE_FAILURE_SIMULATION` | `false` | Turns on the `X-Simulate-Failure` header |
| `CACHE_TYPE` | `disk` | LiteLLM cache: `disk` (stored next to the DB in `llm_cache/`) or `local` (in-memory) |
| `CACHE_TTL_SECONDS` | `86400` | Cache entry lifetime |
| `DEFAULT_SYSTEM_PROMPT` | `You are a helpful, concise assistant.` | Used when a session has no system prompt |
| `LLM_TIMEOUT_SECONDS` | `30` | Per-key request timeout, so a hanging provider fails over too |
| `LLM_TEMPERATURE` / `LLM_MAX_TOKENS` | `0.3` / `0` (no cap) | Sampling parameters |
| `MAX_HISTORY_MESSAGES` | `40` | Most recent stored messages sent as context |
| `MOCK_LLM` | `false` | Offline canned replies for local testing |
| `VITE_API_URL` (frontend, build time) | `http://localhost:8000` | Backend base URL |

---

## 3. Design decisions

### Request flow

```
React ──POST /chat──▶ FastAPI ──▶ validate (Pydantic: trim, 1–4000 chars)
                                  │
                                  ├─ load session system prompt + history from SQLite
                                  ├─ build messages = [system] + history + [new user message]
                                  ▼
                       LLMGateway.complete(messages)
                         for key in pool (key_1, key_2, …):
                           litellm.acompletion(model, messages, api_key=key,
                                               caching=True, num_retries=0)
                             ├─ cache hit  → LLM_CACHE (key_id=null, fallback_attempts=0)
                             ├─ success    → LIVE_INFERENCE (key_id, fallback_attempts=i)
                             └─ exception  → log, try next key
                         all keys failed → 503 {"detail": "all providers unavailable"}
                                  │
                                  ▼
                  save user + assistant message in ONE SQLite transaction
                  (failed requests are never written)
```

### Key fallback

* **The pool is ordered.** `LLM_API_KEYS` becomes `key_1`, `key_2`, … Each attempt is a `litellm.acompletion` call with that key and `num_retries=0`, so LiteLLM never quietly retries a dead key. That retrying would hide the failover and add latency.
* **LiteLLM gives one error model for every provider.** Gemini, OpenAI, Groq and others all raise the same exception types (`RateLimitError` for 429 and quota errors, `InternalServerError`, `ServiceUnavailableError`, `APIError(502)`, `Timeout`, `APIConnectionError`, `AuthenticationError`). Any of them moves the request to the next key. Auth errors fail over too, because a revoked key should never break the app.
* **The failover loop is explicit, not LiteLLM's Router shuffle.** This makes the order deterministic. It also lets each response report exactly which key answered (`key_id`) and how many failed first (`fallback_attempts`), which the contract requires. The key itself never leaves the server, and logs show key ids only.
* **`X-Simulate-Failure`** (only when `ENABLE_FAILURE_SIMULATION=true`) raises the *same LiteLLM exception* the provider would raise, for the listed keys and for that request only. The real failover path is exercised, not a separate code path. Malformed pairs and unsupported statuses are ignored.
* **The hook bypasses cache reads.** While the header is present the request skips the cache *lookup* (`cache={"no-cache": True}`) but still writes the fresh answer. Otherwise a repeated question would be a cache hit, no key would be called, and the failover could not be shown.
* **Every error is JSON.** Validation errors are FastAPI's 422, unknown sessions are 404, an exhausted pool is 503, and a catch-all handler returns a JSON 500. The API never returns a stack trace or an HTML error page.

### Caching

* **LiteLLM's built-in cache** (`litellm.cache = Cache(type="disk")`) is used through `caching=True` on every call. The disk cache sits next to the SQLite file in the Docker volume, so it survives restarts. If the disk cache is unavailable it falls back to the in-memory cache.
* **What the cache key covers.** LiteLLM hashes the model, the full message list (system prompt + history + new message) and the sampling parameters. It does **not** include the API key, so an answer produced by `key_1` is reused even when `key_1` is later rate-limited. As a result:
  * The same first message in a new session returns `LLM_CACHE` the second time, because the messages are identical.
  * A different message, a different history or a different system prompt always produces a new key, so it is never served from cache.
* **Hit detection.** LiteLLM sets `response._hidden_params["cache_hit"]`, which maps to `LLM_CACHE`. A cached response carries its stored `usage`, so the token counts on a hit equal those of the original live call. The reply text is identical, and latency is a few milliseconds instead of hundreds.

### Token tracking

`input_tokens` and `output_tokens` come from the provider's `usage` for **this request only** (prompt = system + history + new message). They are never summed across the conversation. If a provider omits usage, `litellm.token_counter` is the fallback. Each assistant message stores its metadata as JSON in SQLite, so history also shows per-message usage.

### SQLite session storage

* The contract's `messages` table, plus a `sessions` table (holding the session's system prompt) and an index on `(session_id, id)`.
* The schema is created automatically on startup (`CREATE TABLE IF NOT EXISTS`). WAL mode is on, and each operation uses a short-lived connection, so there is no cross-thread connection sharing.
* The user turn and the assistant turn are inserted **in one transaction after the LLM succeeds**. A 422 or 503 therefore never leaves a half-written turn in the history.
* `system_prompt` sent with any request overrides the session's prompt and is stored with the session. Later requests reuse it without resending. `GET /sessions/{id}/messages` never returns the system prompt.
* The file lives at `DATABASE_URL` (`/data/app.db` in Docker, on the named volume).

---

## 4. Verification

Set `API=http://localhost:8000` (or your deployed backend URL).

**Health**
```bash
curl $API/health
# {"status":"ok","keys_configured":2,"cache_enabled":true}
```

**Cache hit.** Send the same first message in two new sessions:
```bash
curl -s -X POST $API/chat -H 'Content-Type: application/json' \
  -d '{"session_id":"demo-1","message":"What is the capital of France?"}'
# resolution_source: LIVE_INFERENCE, key_id: key_1, latency_ms: ~800
curl -s -X POST $API/chat -H 'Content-Type: application/json' \
  -d '{"session_id":"demo-2","message":"What is the capital of France?"}'
# resolution_source: LLM_CACHE, key_id: null, same reply, same tokens, latency_ms: a few ms
```

**Failover**
```bash
curl -s -X POST $API/chat -H 'Content-Type: application/json' -H 'X-Simulate-Failure: key_1=429' \
  -d '{"session_id":"demo-1","message":"And of Germany?"}'
# 200, key_id: key_2, fallback_attempts: 1

curl -s -w '\n%{http_code}\n' -X POST $API/chat -H 'Content-Type: application/json' \
  -H 'X-Simulate-Failure: *=429' -d '{"session_id":"demo-1","message":"anything"}'
# 503 {"detail":"all providers unavailable"}  (nothing added to history)
```

**Multi-turn and history**
```bash
curl -s $API/sessions/demo-1/messages      # user/assistant turns, oldest first, with metadata
curl -s $API/sessions/unknown/messages     # 404 {"detail":"session not found"}
```

**Validation**
```bash
curl -s -X POST $API/chat -H 'Content-Type: application/json' -d '{"session_id":"x","message":"   "}'  # 422
```

**In the UI:**
* Every assistant reply shows chips for the source (green LIVE_INFERENCE, amber LLM_CACHE), model, latency, input and output tokens, the answering key, and a red failover badge when keys failed.
* Use the **Simulate failure** dropdown to send `X-Simulate-Failure` from the browser. Pick "key_1 → 429" to see `key_2` answer, or "All keys → 429" to see the readable 503 message.
* Reload the page (a new session) and send the same first message to see a cache hit.

**Automated tests:** `make test` (or `cd backend && pytest -q`). The 32 tests run offline with LiteLLM's mock mode and cover:
* the response contract
* every 422 case and malformed JSON
* cache hit and miss rules, identical replies, and cache hits being faster
* per-request tokens and multi-turn context
* history and 404s
* failover on 429, 500, 502 and 503, the 503 when every key fails, and failed turns never being stored
* a real (non-simulated) LiteLLM `RateLimitError`
* CORS

---

## 5. Setup Used

* **Editor:** VS Code
* **AI tools:** Claude (Anthropic), used for scaffolding, code review and the test suite
* **Stack:** Python 3.12, FastAPI, LiteLLM 1.77, SQLite, React 18 + Vite, Docker Compose
* **LLM provider:** Google Gemini (two free-tier Google AI Studio API keys)
* **Hosting:** Render (backend, Docker) and Vercel (frontend)

---

## 6. Bonus features

* **Failure simulator in the UI.** A dropdown sends `X-Simulate-Failure`, so failover can be shown live from the browser, not only with curl.
* **Persistent disk cache.** The LiteLLM cache lives in the same Docker volume as the database, so cache hits survive restarts and redeploys on hosts with a volume.
* **Multi-model pool (used in the live deployment).** key_1 runs `gemini-3.5-flash-lite` and key_2 runs `gemini-3.1-flash-lite`. During deployment a single model returned 503 "high demand" on both keys, so the pool was spread across two models to give failover a genuinely independent path. `LLM_MODELS` lets each key use a different model or provider (for example a Gemini key plus a Groq key), and failover works across providers because LiteLLM normalises their errors.
* **Per-session system prompt** (`system-prompt-input`), stored in SQLite and reused for the rest of the session.
* **Offline mock mode** (`MOCK_LLM=true`). The whole stack and the test suite run with no keys or network.
* **Extra metadata in the UI:** answering `key_id`, a failover badge, and backend health in the header (key count and cache status).
* **Production details:**
  * non-root container user and a Docker `HEALTHCHECK`
  * the backend honours `$PORT` for PaaS hosts
  * a per-key timeout, so a hanging provider also fails over
  * a bounded history window (`MAX_HISTORY_MESSAGES`)
  * a CI workflow that runs the test suite and builds both images
