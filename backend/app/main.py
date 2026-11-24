"""FastAPI app: /health, /chat, /sessions/{session_id}/messages."""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import Settings, get_settings
from .db import Database
from .llm import AllProvidersUnavailable, LLMGateway, parse_simulation_header
from .schemas import ChatMetadata, ChatRequest, ChatResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or get_settings()
    db = Database(settings.db_path)
    gateway = LLMGateway(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        db.init()  # schema is created automatically on startup
        log.info(
            "Started: %d key(s), cache=%s, db=%s, failure_simulation=%s, mock=%s",
            gateway.keys_configured, gateway.cache_type, settings.db_path,
            settings.enable_failure_simulation, settings.mock_llm,
        )
        yield

    app = FastAPI(title="LLM Chatbot with Key Failover and Caching", version="1.0.0", lifespan=lifespan)
    app.state.settings, app.state.db, app.state.gateway = settings, db, gateway

    origins = settings.allowed_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials="*" not in origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],  # includes Content-Type and X-Simulate-Failure
    )

    # Never leak a stack trace: anything unexpected becomes a JSON 500.
    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception):
        log.exception("Unhandled error: %s", exc)
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    # ---- GET /health ------------------------------------------------------------------
    @app.get("/health")
    async def health():
        try:
            await run_in_threadpool(db.ping)
        except Exception:
            return JSONResponse(
                status_code=503,
                content={"status": "error", "detail": "database unavailable",
                         "keys_configured": gateway.keys_configured, "cache_enabled": gateway.cache_enabled},
            )
        if gateway.keys_configured == 0:
            return JSONResponse(
                status_code=503,
                content={"status": "error", "detail": "no API keys configured",
                         "keys_configured": 0, "cache_enabled": gateway.cache_enabled},
            )
        return {
            "status": "ok",
            "keys_configured": gateway.keys_configured,
            "cache_enabled": gateway.cache_enabled,
        }

    # ---- POST /chat -------------------------------------------------------------------
    @app.post("/chat", response_model=ChatResponse)
    async def chat(
        body: ChatRequest,
        x_simulate_failure: Optional[str] = Header(default=None, alias="X-Simulate-Failure"),
    ):
        started = time.perf_counter()

        session = await run_in_threadpool(db.get_session, body.session_id)
        new_prompt = body.system_prompt or None  # "" counts as "not provided"
        system_prompt = new_prompt or (session["system_prompt"] if session else None) \
            or settings.default_system_prompt

        history = await run_in_threadpool(db.get_history, body.session_id, settings.max_history_messages)
        messages = [{"role": "system", "content": system_prompt}]
        messages += [{"role": m["role"], "content": m["content"]} for m in history]
        messages.append({"role": "user", "content": body.message})

        simulate = parse_simulation_header(x_simulate_failure) if settings.enable_failure_simulation else {}

        try:
            result = await gateway.complete(messages, simulate=simulate)
        except AllProvidersUnavailable:
            # Nothing is written to history for a failed request.
            return JSONResponse(status_code=503, content={"detail": "all providers unavailable"})

        latency_ms = int(round((time.perf_counter() - started) * 1000))
        metadata = ChatMetadata(
            resolution_source="LLM_CACHE" if result.cache_hit else "LIVE_INFERENCE",
            model=result.model,
            latency_ms=latency_ms,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            key_id=result.key_id,
            fallback_attempts=result.fallback_attempts,
        )
        await run_in_threadpool(
            db.save_exchange, body.session_id, new_prompt, body.message, result.reply, metadata.model_dump()
        )
        return ChatResponse(session_id=body.session_id, reply=result.reply, metadata=metadata)

    # ---- GET /sessions/{session_id}/messages ------------------------------------------
    @app.get("/sessions/{session_id}/messages")
    async def session_messages(session_id: str):
        if not await run_in_threadpool(db.session_exists, session_id):
            return JSONResponse(status_code=404, content={"detail": "session not found"})
        history = await run_in_threadpool(db.get_history, session_id)
        return {"session_id": session_id, "messages": history}

    return app


app = create_app()
