"""End-to-end API tests.

They run LiteLLM in offline mock mode (MOCK_LLM), so the real failover loop and the real
LiteLLM cache are exercised without network access or API keys.
"""
from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.llm import parse_simulation_header
from app.main import create_app


def _sid() -> str:
    return f"s-{uuid.uuid4().hex[:8]}"


def _msg(text: str = "hello") -> str:
    # Unique text per test so cache entries from other tests never interfere.
    return f"{text} {uuid.uuid4().hex[:6]}"


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        api_keys=["fake-key-a", "fake-key-b"],
        models=["gemini/gemini-3.5-flash-lite"],
        database_url=f"sqlite:///{tmp_path}/app.db",
        allowed_origins=["http://localhost:3000"],
        enable_failure_simulation=True,
        cache_type="local",
        mock_llm=True,
    )
    with TestClient(create_app(settings)) as c:
        yield c


def chat(client, session_id, message, headers=None, **extra):
    return client.post("/chat", json={"session_id": session_id, "message": message, **extra}, headers=headers or {})


def wait_for_cache():
    # LiteLLM writes cache entries in a background task after the response returns.
    time.sleep(0.3)


# ---- health ---------------------------------------------------------------------------
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "keys_configured": 2, "cache_enabled": True}


# ---- contract shape -------------------------------------------------------------------
def test_chat_response_contract(client):
    sid = _sid()
    r = chat(client, sid, _msg("What is the capital of France?"))
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == sid
    assert body["reply"]
    meta = body["metadata"]
    assert meta["resolution_source"] == "LIVE_INFERENCE"
    assert meta["model"] == "gemini/gemini-3.5-flash-lite"
    assert isinstance(meta["latency_ms"], int)
    assert isinstance(meta["input_tokens"], int) and meta["input_tokens"] > 0
    assert isinstance(meta["output_tokens"], int) and meta["output_tokens"] > 0
    assert meta["key_id"] == "key_1"
    assert meta["fallback_attempts"] == 0


# ---- validation -----------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload",
    [
        {"session_id": "s-1"},                              # missing message
        {"message": "hi"},                                  # missing session_id
        {"session_id": "s-1", "message": ""},               # empty message
        {"session_id": "s-1", "message": "    "},           # whitespace only
        {"session_id": "", "message": "hi"},                # empty session id
        {"session_id": "x" * 129, "message": "hi"},         # session id too long
        {"session_id": "s-1", "message": "a" * 4001},       # message too long
        {"session_id": "s-1", "message": 123},              # wrong type
    ],
)
def test_validation_errors(client, payload):
    r = client.post("/chat", json=payload)
    assert r.status_code == 422
    assert "detail" in r.json()


def test_malformed_json(client):
    r = client.post("/chat", content="{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    assert "detail" in r.json()


def test_message_trimmed_and_4000_chars_allowed(client):
    r = chat(client, _sid(), "  " + "a" * 4000 + "  ")
    assert r.status_code == 200


def test_validation_failure_not_stored(client):
    sid = _sid()
    chat(client, sid, "")
    assert client.get(f"/sessions/{sid}/messages").status_code == 404


# ---- caching --------------------------------------------------------------------------
def test_same_first_message_in_new_session_hits_cache(client):
    text = _msg("Tell me a fact")
    first = chat(client, _sid(), text).json()
    wait_for_cache()
    second = chat(client, _sid(), text).json()

    assert first["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    assert second["metadata"]["resolution_source"] == "LLM_CACHE"
    assert second["reply"] == first["reply"]
    assert second["metadata"]["key_id"] is None
    assert second["metadata"]["fallback_attempts"] == 0
    assert second["metadata"]["input_tokens"] == first["metadata"]["input_tokens"]
    assert second["metadata"]["output_tokens"] == first["metadata"]["output_tokens"]
    assert second["metadata"]["latency_ms"] < first["metadata"]["latency_ms"]


def test_different_message_never_from_cache(client):
    chat(client, _sid(), _msg("one"))
    wait_for_cache()
    r = chat(client, _sid(), _msg("two")).json()
    assert r["metadata"]["resolution_source"] == "LIVE_INFERENCE"


def test_different_system_prompt_is_a_different_cache_key(client):
    text = _msg("same text")
    chat(client, _sid(), text)
    wait_for_cache()
    r = chat(client, _sid(), text, system_prompt="Answer like a pirate.").json()
    assert r["metadata"]["resolution_source"] == "LIVE_INFERENCE"


# ---- multi-turn + history -------------------------------------------------------------
def test_history_is_stored_per_session(client):
    a, b = _sid(), _sid()
    chat(client, a, "first in A")
    chat(client, a, "second in A")
    chat(client, b, "only in B")

    ha = client.get(f"/sessions/{a}/messages").json()
    hb = client.get(f"/sessions/{b}/messages").json()
    assert ha["session_id"] == a
    assert [m["role"] for m in ha["messages"]] == ["user", "assistant", "user", "assistant"]
    assert ha["messages"][0]["content"] == "first in A"
    assert ha["messages"][2]["content"] == "second in A"
    assert "metadata" not in ha["messages"][0]
    assert ha["messages"][1]["metadata"]["resolution_source"] in {"LIVE_INFERENCE", "LLM_CACHE"}
    assert len(hb["messages"]) == 2
    assert all(m["role"] != "system" for m in ha["messages"])


def test_history_is_sent_to_llm(client):
    sid = _sid()
    first = chat(client, sid, _msg("turn one")).json()
    second = chat(client, sid, _msg("turn two")).json()
    # Second request carries system + 2 history messages + new message -> more prompt tokens.
    assert second["metadata"]["input_tokens"] > first["metadata"]["input_tokens"]


def test_tokens_are_per_request_not_cumulative(client):
    a = chat(client, _sid(), _msg("equal length A")).json()
    b = chat(client, _sid(), _msg("equal length B")).json()
    assert abs(a["metadata"]["input_tokens"] - b["metadata"]["input_tokens"]) <= 2


def test_unknown_session_404(client):
    r = client.get("/sessions/does-not-exist/messages")
    assert r.status_code == 404
    assert r.json() == {"detail": "session not found"}


# ---- failover -------------------------------------------------------------------------
@pytest.mark.parametrize("header", ["key_1=429", "key_1=500", "key_1=502", "key_1=503"])
def test_failover_to_second_key(client, header):
    r = chat(client, _sid(), _msg(), headers={"X-Simulate-Failure": header})
    assert r.status_code == 200
    meta = r.json()["metadata"]
    assert meta["key_id"] == "key_2"
    assert meta["fallback_attempts"] == 1
    assert meta["resolution_source"] == "LIVE_INFERENCE"
    assert r.json()["reply"]


@pytest.mark.parametrize("header", ["*=429", "key_1=429,key_2=500"])
def test_all_keys_fail_returns_503_and_nothing_stored(client, header):
    sid = _sid()
    r = chat(client, sid, _msg(), headers={"X-Simulate-Failure": header})
    assert r.status_code == 503
    assert r.json() == {"detail": "all providers unavailable"}
    assert client.get(f"/sessions/{sid}/messages").status_code == 404


def test_failed_turn_not_added_to_existing_session(client):
    sid = _sid()
    chat(client, sid, "kept")
    chat(client, sid, _msg("dropped"), headers={"X-Simulate-Failure": "*=503"})
    history = client.get(f"/sessions/{sid}/messages").json()["messages"]
    assert len(history) == 2


def test_simulation_bypasses_cache_read(client):
    text = _msg("cached question")
    chat(client, _sid(), text)
    wait_for_cache()
    r = chat(client, _sid(), text, headers={"X-Simulate-Failure": "key_1=429"}).json()
    assert r["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    assert r["metadata"]["key_id"] == "key_2"


def test_simulation_ignored_when_disabled(tmp_path):
    settings = Settings(
        api_keys=["a", "b"], database_url=f"sqlite:///{tmp_path}/x.db",
        enable_failure_simulation=False, cache_type="local", mock_llm=True,
    )
    with TestClient(create_app(settings)) as c:
        r = chat(c, _sid(), _msg(), headers={"X-Simulate-Failure": "*=429"})
        assert r.status_code == 200
        assert r.json()["metadata"]["key_id"] == "key_1"


def test_real_provider_error_fails_over(client, monkeypatch):
    """A genuine LiteLLM exception (not the test hook) on key_1 also fails over."""
    import litellm

    real = litellm.acompletion

    async def flaky(**kwargs):
        if kwargs["api_key"] == "fake-key-a":
            raise litellm.RateLimitError(message="quota exceeded", llm_provider="gemini", model=kwargs["model"])
        return await real(**kwargs)

    monkeypatch.setattr("app.llm.litellm.acompletion", flaky)
    r = chat(client, _sid(), _msg())
    assert r.status_code == 200
    assert r.json()["metadata"]["key_id"] == "key_2"


def test_cors_allows_frontend_origin_and_header(client):
    r = client.options(
        "/chat",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-simulate-failure",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:3000"


# ---- unit -----------------------------------------------------------------------------
def test_parse_simulation_header():
    assert parse_simulation_header("key_1=429") == {"key_1": 429}
    assert parse_simulation_header("key_1=429, key_2=500") == {"key_1": 429, "key_2": 500}
    assert parse_simulation_header("*=503") == {"*": 503}
    assert parse_simulation_header("key_1=404,garbage,=500") == {}
    assert parse_simulation_header(None) == {}
