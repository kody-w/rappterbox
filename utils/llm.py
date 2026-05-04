#!/usr/bin/env python3
"""
utils/llm.py — stdlib-only LLM dispatch shared by Tier 1 (brainstem)
and Tier 2 (swarm).

Provider precedence (first one with credentials wins):
    1. Copilot      (Tier 1 only — registered by the brainstem at startup
                     so single-file rapps can reuse the same engine that
                     powers the host process; no env vars required)
    2. Azure OpenAI (AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_API_KEY [+ AZURE_OPENAI_DEPLOYMENT])
    3. OpenAI       (OPENAI_API_KEY)
    4. Anthropic    (ANTHROPIC_API_KEY)
    5. Fake mode    (LLM_FAKE=1) — deterministic stub for tests

Uses urllib only — no openai / anthropic SDK dependency, so this works
under the stdlib-only swarm server AND under Azure Functions without
extra installs.

The wire format we accept and emit is OpenAI-compatible:
    chat({"messages":[…], "tools":[…], "model":…}) →
    {"role":"assistant", "content":…, "tool_calls":[…]}

Single-file rapps can also use the higher-level convenience
    call_llm(messages, model=None) -> str
which returns just the assistant's text content. This is the contract
BookFactory and friends use instead of inlining their own _llm_call.
"""

from __future__ import annotations
import json
import os
import urllib.error
import urllib.request


# ─── Copilot provider (Tier 1 — registered by the brainstem at startup) ─

# A callable returning (token, endpoint) for the brainstem's live Copilot
# session. The brainstem injects this via register_copilot_provider() so
# rapps loaded into its agents/ directory transparently route through the
# same LLM that's powering the engine — no AZURE_/OPENAI_ keys needed for
# local Tier 1 use. Tier 2 (swarm) never registers one and falls through
# to env-configured providers.
_copilot_token_provider = None
_copilot_default_model = "gpt-4o"


def register_copilot_provider(token_getter, default_model: str = "gpt-4o") -> None:
    """Brainstem hook. `token_getter()` must return (bearer_token, endpoint)."""
    global _copilot_token_provider, _copilot_default_model
    _copilot_token_provider = token_getter
    _copilot_default_model = default_model or "gpt-4o"


# ─── Provider detection ─────────────────────────────────────────────────

def detect_provider() -> str:
    """Returns one of: 'copilot', 'azure-openai', 'openai', 'anthropic', 'fake'."""
    if os.environ.get("LLM_FAKE") == "1":
        return "fake"
    if _copilot_token_provider is not None:
        return "copilot"
    if os.environ.get("AZURE_OPENAI_ENDPOINT") and os.environ.get("AZURE_OPENAI_API_KEY"):
        return "azure-openai"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "fake"


def provider_status() -> dict:
    """Diagnostic — what creds are present, what provider would be used."""
    return {
        "provider": detect_provider(),
        "copilot_registered": _copilot_token_provider is not None,
        "azure_openai_configured": bool(
            os.environ.get("AZURE_OPENAI_ENDPOINT") and os.environ.get("AZURE_OPENAI_API_KEY")
        ),
        "openai_configured": bool(os.environ.get("OPENAI_API_KEY")),
        "anthropic_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "azure_endpoint": os.environ.get("AZURE_OPENAI_ENDPOINT", ""),
        "azure_deployment": os.environ.get("AZURE_OPENAI_DEPLOYMENT", ""),
        "fake_mode": os.environ.get("LLM_FAKE") == "1",
    }


# ─── Common HTTP helper ─────────────────────────────────────────────────

def _http_post(url: str, headers: dict, body: dict, timeout: int = 60) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8")
        except Exception:
            err_body = str(e)
        raise RuntimeError(f"LLM HTTP {e.code}: {err_body[:400]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"LLM network error: {e}")


# ─── Azure OpenAI ───────────────────────────────────────────────────────

def chat_azure_openai(messages: list, tools: list | None = None,
                      tool_choice: str = "auto", model: str | None = None) -> dict:
    """OpenAI-format chat completion against an Azure OpenAI deployment."""
    endpoint = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
    key = os.environ["AZURE_OPENAI_API_KEY"]
    deployment = model or os.environ.get("AZURE_OPENAI_DEPLOYMENT") \
                 or os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", "")
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")

    # Endpoint variants we accept:
    #   1. Full v1 URL:    https://…/openai/v1/chat/completions
    #      → use as-is, NO api-version query (v1 doesn't accept it)
    #   2. Full legacy URL: https://…/openai/deployments/<d>/chat/completions
    #      → append ?api-version=…
    #   3. Bare resource:  https://…
    #      → build the legacy deployment URL ourselves
    is_v1 = "/openai/v1/" in endpoint
    if "/chat/completions" in endpoint:
        url = endpoint
        if not is_v1 and "?" not in url:
            url += f"?api-version={api_version}"
    else:
        url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version={api_version}"

    body = {"messages": messages}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = tool_choice
    # v1 + legacy chat-completions both want a model in the body
    if "/chat/completions" in endpoint:
        body["model"] = model or deployment

    resp = _http_post(url, {
        "Content-Type": "application/json",
        "api-key": key,
    }, body)
    return _normalize_openai_response(resp)


# ─── OpenAI ─────────────────────────────────────────────────────────────

def chat_openai(messages: list, tools: list | None = None,
                tool_choice: str = "auto", model: str | None = None) -> dict:
    body = {
        "model": model or os.environ.get("OPENAI_MODEL", "gpt-4o"),
        "messages": messages,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = tool_choice
    resp = _http_post("https://api.openai.com/v1/chat/completions", {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
    }, body)
    return _normalize_openai_response(resp)


# ─── Anthropic ──────────────────────────────────────────────────────────

def chat_anthropic(messages: list, tools: list | None = None,
                   tool_choice: str = "auto", model: str | None = None) -> dict:
    """Anthropic Messages API. Translates OpenAI-style tools → Anthropic tools."""
    # Pull system prompt out of messages array (Anthropic puts it at top level)
    sys_prompt = ""
    msgs_clean = []
    for m in messages:
        if m.get("role") == "system":
            sys_prompt = (sys_prompt + "\n" + (m.get("content") or "")).strip()
        else:
            msgs_clean.append(m)

    body = {
        "model": model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        "max_tokens": 4096,
        "messages": msgs_clean,
    }
    if sys_prompt:
        body["system"] = sys_prompt
    if tools:
        body["tools"] = [{
            "name": t["function"]["name"],
            "description": t["function"].get("description", ""),
            "input_schema": t["function"].get("parameters", {"type": "object", "properties": {}}),
        } for t in tools if t.get("type") == "function"]

    resp = _http_post("https://api.anthropic.com/v1/messages", {
        "Content-Type": "application/json",
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
    }, body)

    # Translate Anthropic response → OpenAI shape
    out_text = ""
    tool_calls = []
    for blk in resp.get("content", []):
        if blk.get("type") == "text":
            out_text += blk.get("text", "")
        elif blk.get("type") == "tool_use":
            tool_calls.append({
                "id": blk.get("id", ""),
                "type": "function",
                "function": {
                    "name": blk.get("name", ""),
                    "arguments": json.dumps(blk.get("input", {})),
                },
            })
    msg = {"role": "assistant", "content": out_text}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


# ─── Fake (for tests) ───────────────────────────────────────────────────

def chat_fake(messages: list, tools: list | None = None,
              tool_choice: str = "auto", model: str | None = None) -> dict:
    """Deterministic stub. If a tool is available, calls the FIRST tool with
    empty args (one round) — otherwise echoes back the last user message
    with a 'fake-llm:' prefix."""
    if tools:
        t = tools[0]
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "fake-call-0",
                "type": "function",
                "function": {
                    "name": t["function"]["name"],
                    "arguments": "{}",
                },
            }],
        }
    last_user = next((m.get("content", "") for m in reversed(messages)
                      if m.get("role") == "user"), "")
    return {"role": "assistant", "content": f"fake-llm: {last_user}"}


# ─── Normalize ──────────────────────────────────────────────────────────

def _normalize_openai_response(resp: dict) -> dict:
    """Pull the message off a chat-completions response. Returns the assistant
    message dict (role + content + optional tool_calls)."""
    choices = resp.get("choices") or []
    if not choices:
        return {"role": "assistant", "content": resp.get("error", {}).get("message", "")}
    msg = choices[0].get("message", {}) or {}
    out = {"role": "assistant", "content": msg.get("content") or ""}
    if msg.get("tool_calls"):
        out["tool_calls"] = msg["tool_calls"]
    return out


# ─── Copilot (Tier 1 — uses the brainstem's live session) ───────────────

def chat_copilot(messages: list, tools: list | None = None,
                 tool_choice: str = "auto", model: str | None = None) -> dict:
    """Chat via the host brainstem's already-authenticated Copilot session."""
    if _copilot_token_provider is None:
        raise RuntimeError("copilot provider not registered")
    token, endpoint = _copilot_token_provider()
    if not token or not endpoint:
        raise RuntimeError("copilot session unavailable")
    body = {"model": model or _copilot_default_model, "messages": messages}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = tool_choice
    resp = _http_post(endpoint.rstrip("/") + "/chat/completions", {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "Editor-Version": "vscode/1.95.0",
        "Copilot-Integration-Id": "vscode-chat",
    }, body)
    return _normalize_openai_response(resp)


# ─── Top-level dispatch ─────────────────────────────────────────────────

def chat(messages: list, tools: list | None = None,
         tool_choice: str = "auto", model: str | None = None) -> dict:
    """Dispatch to the configured provider. Returns an OpenAI-shape
    assistant message dict."""
    p = detect_provider()
    if p == "copilot":
        return chat_copilot(messages, tools, tool_choice, model)
    if p == "azure-openai":
        return chat_azure_openai(messages, tools, tool_choice, model)
    if p == "openai":
        return chat_openai(messages, tools, tool_choice, model)
    if p == "anthropic":
        return chat_anthropic(messages, tools, tool_choice, model)
    return chat_fake(messages, tools, tool_choice, model)


def call_llm(messages: list, model: str | None = None) -> str:
    """Convenience for single-file rapps: send messages, get just the
    assistant's text content back. Same provider precedence as chat()."""
    try:
        msg = chat(messages, model=model)
    except Exception as e:
        return f"(LLM dispatch error: {e})"
    return msg.get("content") or ""


if __name__ == "__main__":
    print(json.dumps(provider_status(), indent=2))
