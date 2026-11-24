import { useEffect, useRef, useState } from "react";
import { API_URL, fetchHealth, newSessionId, sendChat } from "./api.js";

const FAILURE_PRESETS = [
  { label: "None (normal)", value: "" },
  { label: "key_1 → 429 (rate limit)", value: "key_1=429" },
  { label: "key_1 → 500 (server error)", value: "key_1=500" },
  { label: "key_1 → 503", value: "key_1=503" },
  { label: "All keys → 429", value: "*=429" },
];

function Metadata({ meta }) {
  const isCache = meta.resolution_source === "LLM_CACHE";
  return (
    <div className="meta">
      <span className={`chip ${isCache ? "chip-cache" : "chip-live"}`} title="Resolution source">
        <span data-testid="meta-source">{meta.resolution_source}</span>
      </span>
      <span className="chip" title="Model used">
        <span data-testid="meta-model">{meta.model}</span>
      </span>
      <span className="chip" title="Server-measured latency">
        ⏱ <span data-testid="meta-latency">{meta.latency_ms} ms</span>
      </span>
      <span className="chip" title="Input (prompt) tokens for this request">
        in <span data-testid="meta-input-tokens">{meta.input_tokens}</span>
      </span>
      <span className="chip" title="Output (completion) tokens for this reply">
        out <span data-testid="meta-output-tokens">{meta.output_tokens}</span>
      </span>
      <span className="chip chip-muted" title="Key that answered">
        key {meta.key_id ?? "—"}
      </span>
      {meta.fallback_attempts > 0 && (
        <span className="chip chip-warn" title="Keys that failed before this one">
          ↪ {meta.fallback_attempts} failover{meta.fallback_attempts > 1 ? "s" : ""}
        </span>
      )}
    </div>
  );
}

export default function App() {
  // A new session starts on every page load.
  const [sessionId, setSessionId] = useState(() => newSessionId());
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [simulate, setSimulate] = useState("");
  const [pending, setPending] = useState(false);
  const [health, setHealth] = useState(null);
  const bottomRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    fetchHealth().then(setHealth).catch(() => setHealth({ ok: false }));
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, pending]);

  async function handleSend(e) {
    e?.preventDefault();
    const text = input.trim();
    if (!text || pending) return;

    setInput("");
    setMessages((m) => [...m, { role: "user", content: text }]);
    setPending(true);
    try {
      const data = await sendChat({ sessionId, message: text, systemPrompt, simulateFailure: simulate });
      setMessages((m) => [...m, { role: "assistant", content: data.reply, metadata: data.metadata }]);
    } catch (err) {
      setMessages((m) => [...m, { role: "error", content: err.message }]);
    } finally {
      setPending(false);
      inputRef.current?.focus();
    }
  }

  function handleKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  function newChat() {
    setSessionId(newSessionId());
    setMessages([]);
  }

  return (
    <div className="app">
      <header className="header">
        <div>
          <h1>Lyftr Chat</h1>
          <p className="subtitle">LLM chatbot · key failover · response caching</p>
        </div>
        <div className="header-right">
          <span className={`status ${health?.ok ? "status-ok" : "status-bad"}`}>
            {health === null
              ? "checking…"
              : health.ok
              ? `backend ok · ${health.keys_configured} keys · cache ${health.cache_enabled ? "on" : "off"}`
              : "backend unreachable"}
          </span>
          <button className="btn-secondary" onClick={newChat} type="button">
            New chat
          </button>
        </div>
      </header>

      <section className="settings">
        <label className="field">
          <span>System prompt (optional, applies to this session)</span>
          <input
            data-testid="system-prompt-input"
            type="text"
            value={systemPrompt}
            placeholder="You are a helpful, concise assistant."
            onChange={(e) => setSystemPrompt(e.target.value)}
          />
        </label>
        <label className="field field-small">
          <span>Simulate failure (X-Simulate-Failure)</span>
          <select value={simulate} onChange={(e) => setSimulate(e.target.value)}>
            {FAILURE_PRESETS.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </select>
        </label>
      </section>

      <main className="chat">
        {messages.length === 0 && (
          <div className="empty">
            <p>Start a conversation. Send the same first message in a new chat to see a cache hit.</p>
            <p className="muted">
              session <code>{sessionId}</code>
            </p>
          </div>
        )}
        {messages.map((m, i) =>
          m.role === "assistant" ? (
            <div key={i} className="msg msg-assistant" data-testid="assistant-message">
              <div className="bubble">{m.content}</div>
              <Metadata meta={m.metadata} />
            </div>
          ) : m.role === "user" ? (
            <div key={i} className="msg msg-user">
              <div className="bubble">{m.content}</div>
            </div>
          ) : (
            <div key={i} className="msg msg-error" role="alert">
              <div className="bubble">⚠ {m.content}</div>
            </div>
          )
        )}
        {pending && (
          <div className="msg msg-assistant">
            <div className="bubble typing">
              <span />
              <span />
              <span />
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </main>

      <form className="composer" onSubmit={handleSend}>
        <textarea
          ref={inputRef}
          data-testid="chat-input"
          value={input}
          rows={1}
          maxLength={4000}
          placeholder="Type a message… (Enter to send, Shift+Enter for a new line)"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
        />
        <button data-testid="send-button" type="submit" disabled={pending || !input.trim()}>
          {pending ? "Sending…" : "Send"}
        </button>
      </form>
      <footer className="footer">
        session <code>{sessionId}</code> · API <code>{API_URL}</code>
      </footer>
    </div>
  );
}
