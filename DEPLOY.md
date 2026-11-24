# Deployment checklist (about 30–40 min)

## 0. Get 2 API keys (5 min)
1. Go to https://aistudio.google.com/apikey and sign in.
2. Click **Create API key** and choose **Create in new project**. Copy it.
3. Click **Create API key** again, choose **another new project**, and copy that one too.
   (Gemini free-tier quotas are per project, so these are two independent keys.)
4. Check the model names your key can use (`GET https://generativelanguage.googleapis.com/v1beta/models` with header `x-goog-api-key`). This deployment uses `gemini/gemini-3.5-flash-lite` and `gemini/gemini-3.1-flash-lite`.
   Alternative: one Gemini key + one Groq key (console.groq.com), with
   `LLM_MODELS=gemini/gemini-3.5-flash-lite,groq/llama-3.1-8b-instant`.

## 1. Test locally (5 min)
```
copy .env.example .env      (Windows)   |   cp .env.example .env
# put both keys in LLM_API_KEYS=key1,key2
make up            # or: docker compose up -d --build
```
Open http://localhost:3000 and send a message. No `make` on Windows? Run `docker compose up -d --build` instead.

## 2. Push to GitHub (5 min)
```
git init
git add .
git commit -m "LLM chatbot with key failover and caching"
git branch -M main
git remote add origin https://github.com/<you>/lyftr-chatbot.git
git push -u origin main
```
Make the repo **public**. `.env` is git-ignored, so never commit real keys.

## 3. Backend on Render (10 min)
1. https://render.com → New → **Web Service** → connect the GitHub repo.
2. Root Directory: `backend`. Runtime: **Docker**. Instance type: **Free**.
3. Environment variables:
   - `LLM_API_KEYS` = `key1,key2`
   - `LLM_MODEL` = `gemini/gemini-3.5-flash-lite`
   - `LLM_MODELS` = `gemini/gemini-3.5-flash-lite,gemini/gemini-3.1-flash-lite` (each key on a different model, so one busy model can't take the app down)
   - `DATABASE_URL` = `sqlite:////data/app.db`
   - `ENABLE_FAILURE_SIMULATION` = `true`
   - `ALLOWED_ORIGINS` = `http://localhost:3000` (update in step 5)
4. Health check path: `/health`. Deploy, then open `https://<name>.onrender.com/health`.

Note: Render's free tier has no persistent disk, so the DB resets on redeploy. That's fine for the demo;
locally, the Docker volume keeps history across restarts. Free instances sleep after ~15 min idle:
open /health a minute before recording the demo.
(Railway with a volume mounted at /data also works and keeps data.)

## 4. Frontend on Vercel (5 min)
1. https://vercel.com → Add New → Project → import the repo.
2. Root Directory: `frontend`. Framework: **Vite**.
3. Environment variable: `VITE_API_URL` = `https://<name>.onrender.com` (no trailing slash).
4. Deploy and copy the URL.

## 5. Connect them
On Render, set `ALLOWED_ORIGINS` = `https://<your-app>.vercel.app,http://localhost:3000`, save, and let it redeploy.
Then fill in section 1 (Live URLs) of README.md and push.

## 6. Demo video (2–3 min, with voice). Suggested script
1. (15 s) Repo structure + `make up` mention; open the deployed frontend.
2. (20 s) Open `<backend>/health` → keys_configured 2, cache_enabled true.
3. (30 s) Ask "What is the capital of France?" then "What about Germany?" → it remembers context.
   Point out the chips: LIVE_INFERENCE, model, latency, tokens, key_1.
4. (30 s) Reload (new session), ask "What is the capital of France?" again → **LLM_CACHE**, same reply,
   few-ms latency, key —.
5. (30 s) Dropdown "key_1 → 429", send → answered by **key_2**, failover badge.
   Dropdown "All keys → 429" → readable 503 message.
6. (20 s) Terminal: curl with `X-Simulate-Failure: key_1=500` and `*=429`, then
   `GET /sessions/<id>/messages` (session id is shown under the chat) and an unknown id → 404.
7. (10 s) Mention `pytest`: 32 tests passing.
