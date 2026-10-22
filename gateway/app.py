from __future__ import annotations

import hmac
import os
import time
import uuid
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .gui_adapter import GUIAdapterError, complete_via_gui

APP_NAME = "Unified Multi-AI Gateway"
API_KEY = os.getenv("GATEWAY_API_KEY", "")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "90"))
MAX_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "4096"))
FALLBACK_ORDER = [
    item.strip().lower()
    for item in os.getenv(
        "PROVIDER_FALLBACK_ORDER",
        "openai,google,deepseek,anthropic,openrouter,localai",
    ).split(",")
    if item.strip()
]
app = FastAPI(title=APP_NAME, version="0.5.0", docs_url=None, redoc_url=None)
CORS_ORIGINS = [x.strip() for x in os.getenv("CORS_ORIGINS", "https://godofgamerz2013-ui.github.io").split(",") if x.strip()]
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_credentials=False, allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Authorization", "Content-Type", "X-Request-ID"])


class Message(BaseModel):
    role: str
    content: Any


class ChatRequest(BaseModel):
    model: str
    messages: list[Message]
    temperature: float | None = Field(default=0.7, ge=0, le=2)
    max_tokens: int | None = Field(default=1024, ge=1, le=MAX_TOKENS)
    stream: bool = False
    provider: str | None = None


# Official provider APIs, browser adapters, and LocalAI are separate backends.
# LocalAI speaks the OpenAI-compatible API and must run on a persistent host.
PROVIDERS = {
    "openai": {
        "base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "key_env": "OPENAI_API_KEY", "models_env": "OPENAI_MODELS",
        "default_models": ["gpt-4o-mini"], "mode": "openai",
    },
    "google": {
        "base_url": os.getenv("GOOGLE_OPENAI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"),
        "key_env": "GOOGLE_API_KEY", "models_env": "GOOGLE_MODELS",
        "default_models": ["gemini-2.5-flash"], "mode": "openai",
    },
    "deepseek": {
        "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "key_env": "DEEPSEEK_API_KEY", "models_env": "DEEPSEEK_MODELS",
        "default_models": ["deepseek-chat"], "mode": "openai",
    },
    "anthropic": {
        "base_url": os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"),
        "key_env": "ANTHROPIC_API_KEY", "models_env": "ANTHROPIC_MODELS",
        "default_models": ["claude-3-5-haiku-latest"], "mode": "anthropic",
    },
    "openrouter": {
        "base_url": os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        "key_env": "OPENROUTER_API_KEY", "models_env": "OPENROUTER_MODELS",
        "default_models": ["openrouter/auto"], "mode": "openai",
    },
    "localai": {
        "base_url": os.getenv("LOCALAI_BASE_URL", "http://127.0.0.1:8080/v1"),
        "key_env": "LOCALAI_API_KEY", "models_env": "LOCALAI_MODELS",
        "default_models": ["llama-3.2-1b-instruct"], "mode": "openai",
    },
    # Additional supported API providers can be added without changing the fallback engine.
    "groq": {
        "base_url": os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
        "key_env": "GROQ_API_KEY", "models_env": "GROQ_MODELS",
        "default_models": ["llama-3.3-70b-versatile"], "mode": "openai",
    },
    "together": {
        "base_url": os.getenv("TOGETHER_BASE_URL", "https://api.together.xyz/v1"),
        "key_env": "TOGETHER_API_KEY", "models_env": "TOGETHER_MODELS",
        "default_models": ["meta-llama/Llama-3.3-70B-Instruct-Turbo"], "mode": "openai",
    },
    "mistral": {
        "base_url": os.getenv("MISTRAL_BASE_URL", "https://api.mistral.ai/v1"),
        "key_env": "MISTRAL_API_KEY", "models_env": "MISTRAL_MODELS",
        "default_models": ["mistral-small-latest"], "mode": "openai",
    },
    # Optional website GUI adapters require a user-signed-in browser via private CDP.
    "chatgpt_gui": {
        "key_env": "CHATGPT_GUI_CDP_URL", "models_env": "CHATGPT_GUI_MODELS",
        "default_models": ["chatgpt-web"], "mode": "gui",
    },
    "claude_gui": {
        "key_env": "CLAUDE_GUI_CDP_URL", "models_env": "CLAUDE_GUI_MODELS",
        "default_models": ["claude-web"], "mode": "gui",
    },
    "gemini_gui": {
        "key_env": "GEMINI_GUI_CDP_URL", "models_env": "GEMINI_GUI_MODELS",
        "default_models": ["gemini-web"], "mode": "gui",
    },
}


def configured_providers() -> list[dict[str, Any]]:
    out = []
    for name, cfg in PROVIDERS.items():
        configured_key = bool(os.getenv(cfg["key_env"], "").strip())
        # LocalAI may run without an API key on a private network, but it must
        # have an explicit reachable base URL to be considered configured.
        if name == "localai":
            if not os.getenv("LOCALAI_BASE_URL", "").strip():
                continue
        elif not configured_key:
            continue
        models = [s.strip() for s in os.getenv(cfg["models_env"], "").split(",") if s.strip()]
        out.append({**cfg, "name": name, "models": models or cfg["default_models"]})
    return out


def authenticate(authorization: str | None = Header(default=None)) -> None:
    if not API_KEY:
        raise HTTPException(status_code=503, detail="Gateway not configured: set GATEWAY_API_KEY.")
    if not authorization or not hmac.compare_digest(authorization, f"Bearer {API_KEY}"):
        raise HTTPException(status_code=401, detail="Invalid bearer token.")


def provider_priority(provider: dict[str, Any]) -> tuple[int, str]:
    name = provider["name"]
    # GUI session adapters are not silently substituted for API backends.
    if name.endswith("_gui"):
        return (len(FALLBACK_ORDER) + 100, name)
    try:
        return (FALLBACK_ORDER.index(name), name)
    except ValueError:
        return (len(FALLBACK_ORDER) + 10, name)


def candidate_models(req: ChatRequest) -> list[tuple[dict[str, Any], str]]:
    providers = configured_providers()
    if not providers:
        raise HTTPException(status_code=503, detail="No providers configured; set API keys, LOCALAI_BASE_URL, or a GUI CDP endpoint.")
    provider_name, model = req.provider, req.model.strip()
    ordered = sorted(providers, key=provider_priority)
    # "auto" is the explicit gateway-managed route: GPT -> Gemini -> DeepSeek ->
    # Claude -> OpenRouter -> LocalAI by default, skipping unconfigured providers.
    if model == "auto":
        if provider_name not in (None, "", "gateway", "auto"):
            raise HTTPException(status_code=400, detail="Use model='auto' without a provider to enable ordered fallback.")
        return [(provider, provider["models"][0]) for provider in ordered]
    if not model:
        raise HTTPException(status_code=400, detail="Select a model ID from GET /v1/models.")
    if not provider_name and "/" in model:
        prefix, remainder = model.split("/", 1)
        if prefix in PROVIDERS:
            provider_name, model = prefix, remainder
    selected_provider = next((p for p in providers if p["name"] == provider_name), None) if provider_name else None
    if provider_name and selected_provider is None:
        raise HTTPException(status_code=400, detail=f"Provider '{provider_name}' is not configured.")
    if selected_provider is None:
        selected_provider = next((p for p in providers if model in p["models"]), None)
    if selected_provider is None or model not in selected_provider["models"]:
        raise HTTPException(status_code=400, detail="Unknown model. Choose an exact model ID from GET /v1/models.")
    # Explicit model selection begins with that provider, then falls back in priority order.
    candidates = [(selected_provider, model)]
    for provider in ordered:
        if provider["name"] != selected_provider["name"]:
            candidates.append((provider, provider["models"][0]))
    return candidates


def can_fail_over(status: int, body: str) -> bool:
    # Continue to the next configured provider for upstream credential, billing,
    # model availability, rate-limit, timeout, and server failures.
    if status in (401, 402, 403, 404, 408, 409, 422, 429, 500, 502, 503, 504):
        return True
    lower = body.lower()
    quota_terms = (
        "quota", "rate limit", "rate_limit", "insufficient_quota",
        "billing hard limit", "credits exhausted", "payment required",
        "insufficient credits", "out of credits", "billing",
        "model not found", "model_not_found", "unknown model",
        "not found", "unsupported model", "does not exist",
    )
    return status == 400 and any(term in lower for term in quota_terms)


async def call_provider(provider: dict[str, Any], model: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any] | None, str]:
    if provider["mode"] == "gui":
        try:
            answer, quota_seen = await complete_via_gui(provider["name"], payload["messages"])
            if quota_seen:
                return 429, None, answer[:3000]
            return 200, {
                "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
            }, answer[:3000]
        except GUIAdapterError as exc:
            return 503, None, str(exc)

    key = os.getenv(provider["key_env"], "").strip()
    headers = {"Content-Type": "application/json"}
    if provider["mode"] == "anthropic":
        messages, system_parts = [], []
        for msg in payload["messages"]:
            role, content = msg.get("role", "user"), msg.get("content", "")
            if role == "system":
                system_parts.append(str(content))
            elif role in ("user", "assistant"):
                messages.append({"role": role, "content": content})
        body: dict[str, Any] = {"model": model, "max_tokens": payload.get("max_tokens") or 1024, "messages": messages}
        if system_parts:
            body["system"] = "\n\n".join(system_parts)
        if payload.get("temperature") is not None:
            body["temperature"] = payload["temperature"]
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        url = provider["base_url"].rstrip("/") + "/messages"
    else:
        body = {**payload, "model": model, "stream": False}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        url = provider["base_url"].rstrip("/") + "/chat/completions"
        if provider["name"] == "openrouter":
            referer = os.getenv("OPENROUTER_SITE_URL", "").strip()
            if referer:
                headers["HTTP-Referer"] = referer
            headers["X-Title"] = os.getenv("OPENROUTER_APP_NAME", APP_NAME)
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.post(url, headers=headers, json=body)
        response_text = response.text[:3000]
        if response.status_code >= 400:
            return response.status_code, None, response_text
        data = response.json()
        if provider["mode"] == "anthropic":
            text = "".join(p.get("text", "") for p in data.get("content", []) if p.get("type") == "text")
            usage = data.get("usage", {})
            prompt_tokens, completion_tokens = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
            data = {
                "id": data.get("id", f"chatcmpl-{uuid.uuid4().hex[:20]}"),
                "object": "chat.completion", "created": int(time.time()), "model": data.get("model", model),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                             "finish_reason": data.get("stop_reason", "stop")}],
                "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                          "total_tokens": prompt_tokens + completion_tokens},
            }
        return response.status_code, data, response_text
    except (httpx.TimeoutException, httpx.NetworkError):
        return 504, None, "Provider timed out or network connection failed"
    except (ValueError, TypeError):
        return 502, None, "Provider returned an invalid response"


@app.get("/health")
async def health():
    return {"ok": True, "service": APP_NAME, "version": "0.4.0"}


@app.get("/v1/models", dependencies=[Depends(authenticate)])
async def list_models():
    providers = sorted(configured_providers(), key=provider_priority)
    # Include an explicit auto-route that walks the configured priority chain.
    data = [{"id": "auto", "object": "model", "owned_by": "gateway"}]
    data.extend({"id": f'{p["name"]}/{model}', "object": "model", "owned_by": p["name"]}
                for p in providers for model in p["models"])
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions", dependencies=[Depends(authenticate)])
async def chat_completions(req: ChatRequest, request: Request):
    if req.stream:
        raise HTTPException(status_code=400, detail="Streaming is not enabled; set stream=false.")
    payload = {
        "messages": [message.model_dump() for message in req.messages],
        "temperature": req.temperature,
        "max_tokens": min(req.max_tokens or 1024, MAX_TOKENS),
    }
    candidates, attempts = candidate_models(req), []
    for index, (provider, model) in enumerate(candidates):
        status, result, upstream_text = await call_provider(provider, model, payload)
        attempts.append({"provider": provider["name"], "status": status})
        if result is not None and status < 400:
            result.setdefault("object", "chat.completion")
            result["gateway"] = {
                "provider": provider["name"], "fallback_count": index,
                "request_id": request.headers.get("x-request-id", str(uuid.uuid4())),
            }
            return result
        if index < len(candidates) - 1 and can_fail_over(status, upstream_text):
            continue
        if index < len(candidates) - 1:
            # A provider-specific rejection must not hide the remaining fallback chain.
            continue
        safe_status = status if status in (400, 401, 403, 404, 429) else 502
        raise HTTPException(status_code=safe_status, detail={
            "error": "provider_request_failed",
            "message": "All configured providers were tried and failed; inspect the status recorded for each provider.",
            "attempts": attempts,
        })
    raise HTTPException(status_code=502, detail="All configured providers failed.")


@app.get("/", include_in_schema=False)
async def root():
    from fastapi.responses import HTMLResponse
    return HTMLResponse("""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark"><title>OmniRoute Control Center</title>
<style>
:root{font:14px/1.45 Inter,ui-sans-serif,system-ui,sans-serif;color:#e7edf9;background:#0a0c11;--muted:#8e99ab;--line:#2a303b;--panel:#11151c;--green:#65dc9a;--blue:#8ea9ff;--amber:#eac77e;--red:#ff8790}
*{box-sizing:border-box}body{margin:0;background:#0a0c11}button,input,select,textarea{font:inherit}button{cursor:pointer;color:#ecf1fa;background:#252c38;border:1px solid #384353;border-radius:8px;padding:9px 12px;font-weight:650}button:hover{filter:brightness(1.12)}button.primary{background:#d8e1ff;color:#101727;border-color:#d8e1ff}button.ghost{background:transparent}button:disabled{opacity:.45;cursor:wait}input,select,textarea{background:#0c1016;border:1px solid #323a47;color:#eef2fa;border-radius:8px;padding:10px;width:100%;outline:none}input:focus,select:focus,textarea:focus{border-color:#7f9bfa}textarea{min-height:100px;resize:vertical}.app{min-height:100vh;display:grid;grid-template-columns:220px minmax(0,1fr)}aside{background:#0e1117;border-right:1px solid var(--line);padding:18px 12px;display:flex;flex-direction:column;gap:22px}.brand{display:flex;align-items:center;gap:10px;padding:5px 7px}.logo{width:34px;height:34px;border-radius:10px;display:grid;place-items:center;background:linear-gradient(140deg,#dbe4ff,#7e95ed);color:#101625;font-weight:900}.brand b{display:block;font-size:14px}.brand small{color:var(--muted);font-size:10px}.navlabel{font-size:10px;letter-spacing:1.4px;color:#677386;padding:0 10px 7px}.nav{width:100%;text-align:left;background:transparent;border-color:transparent;color:#9da9bb;font-weight:550;display:flex;gap:11px;align-items:center;padding:10px}.nav.active{background:#1b2432;border-color:#303c51;color:#eef3ff}.nav .glyph{width:18px;text-align:center;color:#aab8d5}.sidebottom{margin-top:auto}.mini{border:1px solid var(--line);border-radius:10px;padding:11px;background:#121821}.mini span{display:block;color:var(--muted);font-size:11px;margin-top:4px}.main{min-width:0}.top{height:64px;display:flex;align-items:center;justify-content:space-between;gap:12px;padding:0 26px;border-bottom:1px solid var(--line);background:#0d1016}.crumb{color:var(--muted);font-size:12px}.online{font-size:11px;display:flex;gap:8px;align-items:center;color:#9eb4ab}.dot{width:7px;height:7px;border-radius:50%;background:var(--green)}.content{max-width:1450px;margin:auto;padding:25px 26px 42px}.pagehead{display:flex;align-items:end;justify-content:space-between;gap:15px;flex-wrap:wrap;margin-bottom:20px}.overline{font-size:10px;color:#93aaff;letter-spacing:1.9px;font-weight:800;margin-bottom:7px}h1{font-size:29px;line-height:1.12;letter-spacing:-.8px;margin:0}h2{font-size:14px;margin:0;font-weight:700}.sub{color:var(--muted);margin:9px 0 0;font-size:12px}.actions{display:flex;gap:8px;flex-wrap:wrap}.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:14px}.stat,.card{border:1px solid var(--line);background:var(--panel);border-radius:12px}.stat{padding:15px}.statlabel{color:var(--muted);font-size:11px;display:flex;justify-content:space-between;gap:8px}.statnum{font-size:25px;margin:12px 0 4px;font-weight:750}.statfoot{font-size:11px;color:#8290a4}.grid2{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(290px,.85fr);gap:13px;margin-top:13px}.cardhead{padding:15px 16px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;gap:12px}.cardbody{padding:15px 16px}.muted{color:var(--muted)}.tiny{font-size:11px}.pill{display:inline-flex;align-items:center;gap:6px;border:1px solid #343d4a;border-radius:99px;padding:4px 8px;color:#b7c0ce;font-size:10px;white-space:nowrap}.pill.green{border-color:#315b48;color:#85e2ad;background:#14241d}.pill.amber{border-color:#665332;color:#e6cb8d;background:#272115}.pill.red{border-color:#673942;color:#ff9ca5;background:#29171b}.providers{display:grid;gap:8px}.provider{display:grid;grid-template-columns:34px minmax(0,1fr) auto;align-items:center;gap:11px;padding:10px;border:1px solid #262e3a;background:#0e1219;border-radius:10px}.picon{width:33px;height:33px;border-radius:9px;background:#202838;display:grid;place-items:center;font-weight:850;color:#d6dfff}.pname{font-weight:680;font-size:12px}.pmeta{font-size:10px;color:var(--muted);margin-top:3px;overflow-wrap:anywhere}.sequence{display:flex;flex-direction:column}.seqitem{display:grid;grid-template-columns:26px minmax(0,1fr) auto;align-items:center;gap:9px;padding:10px 3px;border-bottom:1px solid #252b35}.seqitem:last-child{border-bottom:0}.num{width:23px;height:23px;display:grid;place-items:center;border-radius:7px;background:#202838;color:#adbfef;font-size:10px;font-weight:800}.seqname{font-size:12px;font-weight:650}.seqdesc{font-size:10px;color:var(--muted);margin-top:2px}.tag{font-size:10px;color:#c0c9d6;background:#1b222d;border-radius:5px;padding:4px 6px}.tag.enabled{color:#83e2ad;background:#14291f}.banner{border:1px solid #665536;background:#211d15;color:#e8d29b;border-radius:10px;padding:12px 13px;font-size:11px;line-height:1.6;margin-bottom:14px}.banner b{color:#f8e4aa}.log{background:#0b0f15;border:1px solid #252c37;border-radius:9px;min-height:100px;max-height:230px;overflow:auto;padding:10px;font:11px/1.65 ui-monospace,monospace;color:#adb9cb;white-space:pre-wrap}.table{width:100%;border-collapse:collapse;font-size:11px}.table th,.table td{text-align:left;padding:10px 8px;border-bottom:1px solid #252c36}.table th{color:#8592a6;font-weight:600}.section{display:none}.section.active{display:block}.settings{display:grid;grid-template-columns:1fr 1fr;gap:13px}.field{display:grid;gap:6px;margin-bottom:12px}.field label{font-size:11px;color:#aab5c7}.field small{font-size:10px;color:#78859a}.split{display:flex;gap:8px}.split>*{flex:1}.chatlayout{display:grid;grid-template-columns:270px minmax(0,1fr);gap:13px}.chatbox{min-height:440px;display:flex;flex-direction:column}.chatmessages{flex:1;padding:14px;display:flex;flex-direction:column;gap:10px;max-height:480px;overflow:auto}.bubble{border:1px solid #2d3543;border-radius:10px;background:#0c1119;padding:12px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;line-height:1.6}.bubble.user{background:#192337;margin-left:8%}.bubble.assistant{margin-right:5%}.bubble label{display:block;font-size:9px;color:#8ea9ff;letter-spacing:1px;text-transform:uppercase;margin-bottom:5px}.composer{padding:14px;border-top:1px solid var(--line);display:grid;gap:10px}.toast{font-size:11px;color:#adb9cb;min-height:18px}.hide{display:none!important}a{color:#a8bbff;text-decoration:none}a:hover{text-decoration:underline}.footer{color:#657187;font-size:10px;margin-top:20px;line-height:1.7}@media(max-width:980px){.app{grid-template-columns:68px minmax(0,1fr)}aside{padding:16px 7px}.brand{justify-content:center}.brandtext,.navlabel,.navtext,.mini{display:none}.nav{justify-content:center;padding:11px 0}.nav .glyph{width:auto}.grid2{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:700px){.content{padding:18px 12px}.top{padding:0 13px}.settings,.chatlayout{grid-template-columns:1fr}h1{font-size:25px}.stats{gap:8px}.stat{padding:11px}.statnum{font-size:21px}}
</style></head><body><div class="app">
<aside><div class="brand"><div class="logo">O</div><div class="brandtext"><b>OmniRoute</b><small>AI CONTROL PLANE</small></div></div>
<div><div class="navlabel">WORKSPACE</div><button class="nav active" data-page="overview"><span class="glyph">▦</span><span class="navtext">Overview</span></button><button class="nav" data-page="providers"><span class="glyph">◈</span><span class="navtext">Providers</span></button><button class="nav" data-page="models"><span class="glyph">▤</span><span class="navtext">Models</span></button><button class="nav" data-page="routing"><span class="glyph">⇄</span><span class="navtext">Routing & fallback</span></button><button class="nav" data-page="playground"><span class="glyph">✳</span><span class="navtext">Playground</span></button><button class="nav" data-page="settings"><span class="glyph">⚙</span><span class="navtext">Connection settings</span></button></div>
<div class="sidebottom"><div class="mini"><b>Gateway API</b><span>OpenAI-compatible</span><span id="sidehealth">Checking…</span></div><div class="navlabel" style="margin-top:15px">RESOURCES</div><button class="nav" id="docs"><span class="glyph">↗</span><span class="navtext">API endpoints</span></button></div></aside>
<div class="main"><header class="top"><div class="crumb">Workspace / <b id="crumbpage" style="color:#e3e9f4">Overview</b></div><div class="online"><span class="dot"></span><span id="tophealth">Checking gateway</span></div></header><div class="content">
<section id="page-overview" class="section active"><div class="pagehead"><div><div class="overline">GATEWAY CONTROL CENTER</div><h1>Overview</h1><p class="sub">Providers, model catalogue, fallback chain, and LocalAI readiness.</p></div><div class="actions"><button class="ghost" id="refresh">↻ Refresh</button><button class="primary" data-goto="playground">Open playground ↗</button></div></div>
<div class="banner"><b>Provider status:</b> configured models are detected from the gateway inventory; this does not verify credits or guarantee a provider request will succeed. LocalAI runs as a separate persistent service and must be reachable from the gateway.</div>
<div class="stats"><div class="stat"><div class="statlabel">Configured providers <span>◈</span></div><div class="statnum" id="stat-providers">—</div><div class="statfoot" id="stat-providers-foot">Connect to load inventory</div></div><div class="stat"><div class="statlabel">Model IDs <span>▤</span></div><div class="statnum" id="stat-models">—</div><div class="statfoot">Reported by /v1/models</div></div><div class="stat"><div class="statlabel">Fallback stages <span>⇄</span></div><div class="statnum">6</div><div class="statfoot">GPT → Gemini → DeepSeek → Claude → OpenRouter → LocalAI</div></div><div class="stat"><div class="statlabel">Local inference <span>⌂</span></div><div class="statnum" id="local-count">—</div><div class="statfoot" id="local-foot">Needs a running LocalAI host</div></div></div>
<div class="grid2"><div class="card"><div class="cardhead"><div><h2>Provider inventory</h2><div class="tiny muted" style="margin-top:4px">Status reflects model-list configuration</div></div><span class="pill" id="prov-pill">Not loaded</span></div><div class="cardbody"><div class="providers" id="providers"><div class="muted tiny">Enter the gateway key in Playground to load.</div></div></div></div>
<div class="card"><div class="cardhead"><div><h2>Automatic fallback priority</h2><span class="pill green">Configured order</span></div><button class="ghost" data-goto="routing">Details →</button></div><div class="cardbody"><div class="sequence" id="sequence"></div><div class="tiny muted" style="margin-top:11px">Only configured providers are tried. On quota, authentication, model, network or server failures, routing continues to the next available stage.</div></div></div>
<div class="card"><div class="cardhead"><h2>Last request</h2><span class="pill" id="last-pill">None yet</span></div><div class="cardbody"><div class="log" id="last-result">No request made in this browser session.</div></div></div>
<div class="card"><div class="cardhead"><h2>LocalAI final fallback</h2><span class="pill amber" id="local-pill">Not verified</span></div><div class="cardbody tiny muted" style="line-height:1.8">Runs its own OpenAI-compatible inference server. Configure <code>LOCALAI_BASE_URL</code> and an exact installed ID in <code>LOCALAI_MODELS</code> on the gateway runtime. Vercel cannot reach another computer's localhost; use a VPS/private network or run gateway and LocalAI together.</div><div class="cardbody" style="padding-top:0"><a href="https://github.com/mudler/LocalAI" target="_blank" rel="noreferrer">LocalAI GitHub ↗</a> · <a href="https://localai.io/" target="_blank" rel="noreferrer">Docs ↗</a></div></div></div>
</section>
<section id="page-providers" class="section"><div class="pagehead"><div><div class="overline">BACKENDS</div><h1>Providers</h1><p class="sub">Keys remain in Vercel variables or your host's secret environment.</p></div><button class="primary" id="providers-refresh">↻ Reload</button></div><div class="card"><div class="cardhead"><h2>Provider inventory</h2><span class="pill" id="prov-pill-2">Not loaded</span></div><div class="cardbody"><div class="providers" id="providers-full"></div></div></div><div class="footer">A provider appearing here doesn't confirm billing or model access; only an actual chat call can test those.</div></section>
<section id="page-models" class="section"><div class="pagehead"><div><div class="overline">CATALOGUE</div><h1>Models</h1><p class="sub">Exact model IDs exposed by this runtime. Select <code>auto</code> to invoke your failover chain.</p></div><button class="primary" id="models-refresh">↻ Reload models</button></div><div class="card"><div class="cardhead"><h2>Available models</h2><span class="pill" id="models-pill">Not loaded</span></div><div class="cardbody" style="overflow:auto"><table class="table"><thead><tr><th>MODEL ID</th><th>PROVIDER</th><th>OWNER</th><th>ACTION</th></tr></thead><tbody id="model-rows"><tr><td colspan="4" class="muted">Connect to load models.</td></tr></tbody></table></div></div></section>
<section id="page-routing" class="section"><div class="pagehead"><div><div class="overline">RESILIENCY</div><h1>Routing & fallback</h1><p class="sub">GPT → Gemini → DeepSeek → Claude → OpenRouter → LocalAI.</p></div></div><div class="grid2"><div class="card"><div class="cardhead"><h2>Fallback chain</h2><span class="pill green">Default order</span></div><div class="cardbody"><div class="sequence" id="sequence-full"></div></div></div><div class="card"><div class="cardhead"><h2>How failover works</h2></div><div class="cardbody tiny muted" style="line-height:1.9"><p>1. Select <code>auto</code> to begin at GPT.</p><p>2. If a provider is unconfigured it is skipped.</p><p>3. If a configured provider returns a supported upstream failure, the next provider is tried.</p><p>4. HTTP errors, unavailable models and depleted quotas vary by provider.</p><p>5. LocalAI is only available after its server is running, reachable and has an installed model.</p></div></div></div></section>
<section id="page-playground" class="section"><div class="pagehead"><div><div class="overline">TEST INFERENCE</div><h1>Chat playground</h1><p class="sub">Use an explicit model or select <code>auto</code> to enable the full fallback cascade.</p></div></div><div class="chatlayout"><div class="card"><div class="cardhead"><h2>Request setup</h2></div><div class="cardbody"><div class="field"><label>Gateway base URL</label><input id="base" readonly></div><div class="field"><label>Gateway API key</label><input id="key" type="password" autocomplete="off" placeholder="Paste GATEWAY_API_KEY"></div><div class="field"><label>Model / routing</label><select id="model"><option value="">Load models first</option></select><small>Select <code>auto</code> for the ordered cascade.</small></div><div class="split"><button class="primary" id="load">Connect & load</button><button class="ghost" id="health">Health</button></div><div id="play-status" class="tiny muted" style="margin-top:12px">Key remains in this tab only.</div></div></div><div class="card chatbox"><div class="cardhead"><div><h2>Messages</h2><div class="tiny muted">This tab only</div></div><button id="clear" class="ghost">Clear chat</button></div><div class="chatmessages" id="messages"><div class="muted tiny">Connect, select auto and send a test message.</div></div><form class="composer" id="form"><textarea id="prompt" required placeholder="Type your prompt…"></textarea><div class="split"><button class="primary" id="send">Send request</button><span class="toast" id="request-status" style="align-self:center"></span></div></form></div></div></section>
<section id="page-settings" class="section"><div class="pagehead"><div><div class="overline">RUNTIME CONFIGURATION</div><h1>Connection settings</h1><p class="sub">Configure cloud keys separately from the persistent LocalAI machine.</p></div></div><div class="banner"><b>LocalAI is not hosted on Vercel.</b> To use it as a final fallback, run LocalAI and the gateway together on a persistent VPS/PC, or configure a secure reachable LocalAI URL. Never expose an unauthenticated LocalAI server to the public internet.</div><div class="settings"><div class="card"><div class="cardhead"><h2>Cloud provider variables</h2></div><div class="cardbody tiny muted" style="line-height:1.9"><a href="https://vercel.com/godofgamerz2013s-projects/omniroute-gateway/settings/environment-variables" target="_blank" rel="noreferrer">Open Vercel Environment Variables ↗</a><p><code>OPENAI_API_KEY</code> — GPT</p><p><code>GOOGLE_API_KEY</code> — Gemini</p><p><code>DEEPSEEK_API_KEY</code> — DeepSeek</p><p><code>ANTHROPIC_API_KEY</code> — Claude API</p><p><code>OPENROUTER_API_KEY</code> — OpenRouter</p><p>Save to Production and redeploy after edits.</p></div></div><div class="card"><div class="cardhead"><h2>LocalAI setup</h2></div><div class="cardbody tiny muted" style="line-height:1.8"><p>Run the repository's <code>docker-compose.localai.yml</code> on a persistent Linux host with Docker Compose. It starts LocalAI and the gateway on the same private network.</p><p>Install model <code>qwen3-4b</code> at LocalAI startup; on first boot model download may take time and requires enough RAM/disk.</p><p>The combined gateway endpoint is bound to <code>127.0.0.1:8081</code>; put a TLS reverse proxy in front if using it remotely.</p><a href="https://github.com/mudler/LocalAI" target="_blank" rel="noreferrer">LocalAI repository ↗</a> · <a href="https://localai.io/installation/containers/" target="_blank" rel="noreferrer">Official container guide ↗</a></div></div></div></section>
<div class="footer">OmniRoute-inspired Control Center · <a href="/health">Health</a> · <a href="https://github.com/godofgamerz2013-ui/AI" target="_blank" rel="noreferrer">Source on GitHub</a></div>
</div></div></div>
<script>
(()=>{const $=id=>document.getElementById(id);const chain=[["openai","OpenAI GPT","GPT API"],["google","Google Gemini","Gemini API"],["deepseek","DeepSeek","DeepSeek API"],["anthropic","Anthropic Claude","Claude API"],["openrouter","OpenRouter","Multi-provider router"],["localai","LocalAI","Self-hosted final fallback"]];const icons={openai:"G",google:"✦",deepseek:"D",anthropic:"C",openrouter:"↗",localai:"⌂"};let busy=false,models=[],requestCount=0;
$("base").value=location.origin+"/v1";
const cls=(id,msg,kind="")=>{const e=$(id);if(e)e.className=e.className.replace(/\b(good|bad|amber|green|red)\b/g,"").trim()+" "+kind,e.textContent=msg};
const hdr=()=>{const k=$("key").value.trim();if(!k)throw Error("Enter your GATEWAY_API_KEY in Playground.");return {"Authorization":"Bearer "+k,"Content-Type":"application/json"}};
async function api(path,opts={}){const r=await fetch(path,{...opts,headers:{...hdr(),...(opts.headers||{})}});const raw=await r.text();let d;try{d=JSON.parse(raw)}catch{d={detail:raw}}if(!r.ok)throw Error("HTTP "+r.status+": "+JSON.stringify(d.detail||d));return d}
function showPage(name){document.querySelectorAll(".section").forEach(x=>x.classList.toggle("active",x.id==="page-"+name));document.querySelectorAll(".nav[data-page]").forEach(x=>x.classList.toggle("active",x.dataset.page===name));$("crumbpage").textContent=name.charAt(0).toUpperCase()+name.slice(1).replace("-"," ");if(name==="playground")$("prompt").focus()}
document.querySelectorAll(".nav[data-page]").forEach(x=>x.onclick=()=>showPage(x.dataset.page));document.querySelectorAll("[data-goto]").forEach(x=>x.onclick=()=>showPage(x.dataset.goto));$("docs").onclick=()=>window.open("https://github.com/godofgamerz2013-ui/AI#endpoint","_blank","noopener");
function renderSequence(id){const target=$(id);if(!target)return;target.replaceChildren();chain.forEach((p,i)=>{const available=models.some(m=>m.id.startsWith(p[0]+"/"));const item=document.createElement("div");item.className="seqitem";item.innerHTML='<span class="num">'+(i+1)+'</span><div><div class="seqname"></div><div class="seqdesc"></div></div><span class="tag"></span>';item.querySelector(".seqname").textContent=p[1];item.querySelector(".seqdesc").textContent=p[2];const tag=item.querySelector(".tag");tag.textContent=available?"Configured":"Not configured";if(available)tag.classList.add("enabled");target.append(item)})}
function renderProviders(id){const el=$(id);if(!el)return;el.replaceChildren();chain.forEach(p=>{const matches=models.filter(m=>m.id.startsWith(p[0]+"/"));const item=document.createElement("div");item.className="provider";item.innerHTML='<div class="picon"></div><div><div class="pname"></div><div class="pmeta"></div></div><span class="pill"></span>';item.querySelector(".picon").textContent=icons[p[0]];item.querySelector(".pname").textContent=p[1];item.querySelector(".pmeta").textContent=matches.length?matches.length+" model ID(s) · "+matches.map(m=>m.id).join(", "):p[2];const pill=item.querySelector(".pill");pill.textContent=matches.length?"Configured":"Not configured";pill.className="pill "+(matches.length?"green":"amber");el.append(item)})}
function renderModels(){const rows=$("model-rows");rows.replaceChildren();models.forEach(m=>{const tr=document.createElement("tr");[m.id,m.id==="auto"?"Gateway":(m.id.split("/")[0]||"unknown"),m.owned_by||"unknown"].forEach(v=>{const td=document.createElement("td");td.textContent=v;tr.append(td)});const td=document.createElement("td"),b=document.createElement("button");b.textContent="Use in playground";b.onclick=()=>{$("model").value=m.id;showPage("playground")};td.append(b);tr.append(td);rows.append(tr)});$("models-pill").textContent=models.length+" model(s)"}
async function loadModels(){if(busy)return;busy=true;$("load").disabled=true;try{const d=await api("/v1/models");models=d.data||[];if(!models.length)throw Error("No configured models returned.");const sel=$("model");sel.replaceChildren();models.forEach(m=>sel.add(new Option(m.id,m.id)));sel.value=models.some(m=>m.id==="auto")?"auto":models[0].id;$("stat-providers").textContent=new Set(models.map(m=>m.id==="auto"?"gateway":m.id.split("/")[0])).size-1;$("stat-providers-foot").textContent="Based on configured model IDs";$("stat-models").textContent=models.length-1;$("local-count").textContent=models.some(m=>m.id.startsWith("localai/"))?"Ready*":"Not configured";$("local-foot").textContent=models.some(m=>m.id.startsWith("localai/"))?"Configured, reachability not probed":"Requires LocalAI URL and installed model";$("prov-pill").textContent="Inventory loaded";$("prov-pill-2").textContent="Inventory loaded";$("stat-models").textContent=models.filter(m=>m.id!=="auto").length;renderProviders("providers");renderProviders("providers-full");renderSequence("sequence");renderSequence("sequence-full");renderModels();$("play-status").textContent="Connected · "+models.length+" models including auto route";cls("play-status","Connected · "+models.length+" model entries","good")}catch(e){$("play-status").textContent=e.message;const p=$("providers");if(p)p.textContent=e.message;renderSequence("sequence");renderSequence("sequence-full")}finally{busy=false;$("load").disabled=false}}
async function health(){try{const r=await fetch("/health");const d=await r.json();if(!r.ok)throw Error("HTTP "+r.status);$("tophealth").textContent="Gateway online · v"+d.version;$("sidehealth").textContent="Online"}catch(e){$("tophealth").textContent="Health check failed";$("sidehealth").textContent="Unavailable"}}
$("load").onclick=loadModels;$("providers-refresh").onclick=loadModels;$("models-refresh").onclick=loadModels;$("refresh").onclick=()=>{health();if($("key").value.trim())loadModels()};$("health").onclick=health;
$("model").onchange=()=>{$("play-status").textContent="Selected route: "+$("model").value};
function bubble(role,text){const el=$("messages");const e=el.querySelector(".muted");if(e)e.remove();const b=document.createElement("div");b.className="bubble "+role;const l=document.createElement("label");l.textContent=role==="user"?"You":"Assistant";const t=document.createElement("div");t.textContent=text;b.append(l,t);el.append(b);el.scrollTop=el.scrollHeight;return t}
$("form").onsubmit=async ev=>{ev.preventDefault();if(busy)return;const model=$("model").value,p=$("prompt").value.trim();if(!model||!p){$("play-status").textContent="Load models and enter a message.";return}busy=true;$("send").disabled=true;requestCount++;bubble("user",p);$("prompt").value="";const out=bubble("assistant","Running fallback chain…");$("request-status").textContent="Request #"+requestCount;try{const d=await api("/v1/chat/completions",{method:"POST",body:JSON.stringify({model,messages:[{role:"user",content:p}],stream:false})});const provider=d.gateway?.provider||"unknown";out.textContent=d.choices?.[0]?.message?.content||"No content returned.";cls("last-pill","Success · "+provider,"green");$("last-result").textContent="Provider: "+provider+"\nFallback count: "+(d.gateway?.fallback_count??0)+"\nRequest ID: "+(d.gateway?.request_id||"n/a");$("request-status").textContent="Success · "+provider;$("play-status").textContent="Success via "+provider;$("local-pill").textContent=provider==="localai"?"Used for this response":"Fallback not needed";$("local-pill").className="pill "+(provider==="localai"?"green":"amber")}catch(e){out.textContent="Request failed: "+e.message;$("last-result").textContent=e.message;cls("last-pill","All attempts failed","red");$("request-status").textContent="Failed";$("play-status").textContent=e.message}finally{busy=false;$("send").disabled=false}};
$("clear").onclick=()=>{$("messages").replaceChildren();const m=document.createElement("div");m.className="muted tiny";m.textContent="Chat cleared."; $("messages").append(m);$("request-status").textContent=""};
health();renderSequence("sequence");renderSequence("sequence-full");
})();
</script></body></html>""")
