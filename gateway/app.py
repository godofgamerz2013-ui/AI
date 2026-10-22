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
app = FastAPI(title=APP_NAME, version="0.4.0", docs_url=None, redoc_url=None)
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
        if not os.getenv(cfg["key_env"], "").strip():
            continue
        # LocalAI can be configured without an API key on private deployments.
        if name == "localai" and not os.getenv("LOCALAI_API_KEY", "").strip():
            if not os.getenv("LOCALAI_BASE_URL", "").strip():
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
    if not model or model == "auto":
        raise HTTPException(status_code=400, detail="Select an explicit model ID from GET /v1/models.")
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
    # Preserve the user's selection first, then continue down the explicit fallback order.
    ordered = sorted(providers, key=provider_priority)
    candidates = [(selected_provider, model)]
    for provider in ordered:
        if provider["name"] != selected_provider["name"]:
            candidates.append((provider, provider["models"][0]))
    return candidates


def can_fail_over(status: int, body: str) -> bool:
    if status in (408, 429, 500, 502, 503, 504):
        return True
    lower = body.lower()
    quota_terms = (
        "quota", "rate limit", "rate_limit", "insufficient_quota",
        "billing hard limit", "credits exhausted", "payment required",
        "insufficient credits", "out of credits", "billing",
    )
    return status in (400, 402, 403) and any(term in lower for term in quota_terms)


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
    data = [{"id": f'{p["name"]}/{model}', "object": "model", "owned_by": p["name"]}
            for p in sorted(configured_providers(), key=provider_priority) for model in p["models"]]
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
        safe_status = status if status in (400, 401, 403, 404, 429) else 502
        raise HTTPException(status_code=safe_status, detail={
            "error": "provider_request_failed",
            "message": "Provider failed. Check provider configuration, model and quota/browser session state.",
            "attempts": attempts,
        })
    raise HTTPException(status_code=502, detail="All configured providers failed.")


@app.get("/", include_in_schema=False)
async def root():
    from fastapi.responses import HTMLResponse
    return HTMLResponse("""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark"><title>Unified Multi-AI Gateway</title>
<style>
:root{font-family:Inter,ui-sans-serif,system-ui,sans-serif;color:#edf1ff;background:#080c17;--muted:#9ba9c4;--line:#28354d;--accent:#98a7ff;--panel:#111a2b}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 20% 0%,#202d59 0,transparent 45%),#080c17;min-height:100vh}main{max-width:1180px;margin:auto;padding:28px 18px 48px}.header{display:flex;justify-content:space-between;align-items:center;gap:18px;flex-wrap:wrap;margin-bottom:24px}h1{font-size:clamp(28px,5vw,44px);letter-spacing:-1.5px;margin:0}.muted{color:var(--muted)}.eyebrow{font-size:12px;color:var(--accent);letter-spacing:2px;font-weight:800;margin-bottom:8px}.layout{display:grid;grid-template-columns:310px minmax(0,1fr);gap:16px}.panel{background:#111a2bec;border:1px solid var(--line);border-radius:18px;padding:19px;box-shadow:0 20px 60px #0003}h2{font-size:16px;margin:0 0 16px}.field{display:grid;gap:7px;margin:14px 0}label{font-size:12px;color:#bdc9df;font-weight:700}input,select,textarea{width:100%;background:#0a1120;color:#f5f7ff;border:1px solid #35445f;border-radius:10px;padding:12px;font:inherit}textarea{resize:vertical;min-height:100px;line-height:1.5}button{cursor:pointer;border:0;border-radius:10px;background:var(--accent);color:#0c1225;font-weight:800;padding:12px 14px;font:inherit}button.secondary{background:#202d45;color:#edf1ff;border:1px solid #35455f}.actions{display:flex;gap:8px;flex-wrap:wrap}.actions button{flex:1}.status{border:1px solid var(--line);background:#0a1120;color:var(--muted);border-radius:10px;padding:11px;font-size:12px;line-height:1.5;overflow-wrap:anywhere;margin-top:12px}.good{color:#7de3b5!important;border-color:#2e6758!important}.bad{color:#ff9fab!important;border-color:#75404a!important}.warning{border:1px solid #695a36;background:#211e15;color:#f0dda2;border-radius:12px;padding:12px;font-size:12px;line-height:1.55;margin-bottom:16px}.chat{display:flex;flex-direction:column;min-height:610px}.chathead{display:flex;align-items:center;justify-content:space-between;gap:12px;border-bottom:1px solid var(--line);padding-bottom:14px}.messages{flex:1;display:flex;flex-direction:column;gap:12px;padding:17px 0}.empty{margin:auto;text-align:center;max-width:350px;color:var(--muted);line-height:1.6;padding:32px}.msg{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.6;font-size:14px;border:1px solid var(--line);border-radius:13px;padding:13px;background:#0a1120}.msg.user{margin-left:10%;background:#1a2641}.msg.assistant{margin-right:5%}.role{font-size:10px;letter-spacing:1.4px;text-transform:uppercase;color:var(--muted);margin-bottom:6px}.composer{border-top:1px solid var(--line);padding-top:14px}.foot{font-size:11px;line-height:1.6;color:#7485a3;margin-top:18px}a{color:var(--accent)}.tag{border:1px solid #31584e;color:#83e5b6;border-radius:99px;padding:7px 11px;font-size:11px;font-weight:800}@media(max-width:780px){.layout{grid-template-columns:1fr}.chat{min-height:500px}main{padding-top:20px}}
</style></head><body><main>
<header class="header"><div><div class="eyebrow">ONE API · MANY MODELS</div><h1>Unified AI <span style="color:var(--accent)">Gateway</span></h1><p class="muted">Connect providers, select a model, and test your OpenAI-compatible endpoint.</p></div><div class="tag">GATEWAY DASHBOARD</div></header>
<div class="warning"><b>Backend setup:</b> this dashboard is running directly on your Vercel gateway. Chat requires <code>GATEWAY_API_KEY</code> plus at least one configured AI provider or a user-controlled browser adapter. Secrets entered here are kept in page memory only.</div>
<div class="layout"><aside class="panel"><h2>Connection & model</h2>
<div class="field"><label for="base">GATEWAY BASE URL</label><input id="base" value="" readonly><div class="muted" style="font-size:12px">This site is also the API host.</div></div>
<div class="field"><label for="key">GATEWAY API KEY</label><input id="key" type="password" autocomplete="off" placeholder="Paste your GATEWAY_API_KEY"></div>
<div class="field"><label for="model">SELECT MODEL</label><select id="model"><option value="">Load models first</option></select></div>
<div class="actions"><button id="load">Connect & load models</button><button class="secondary" id="health">Test health</button></div>
<div id="status" class="status">Checking this gateway…</div>
<div class="foot">API endpoints: <code>/health</code>, <code>/v1/models</code>, <code>/v1/chat/completions</code>. Health is public; model discovery and chat use your bearer key.</div></aside>
<section class="panel chat"><div class="chathead"><div><h2 style="margin:0 0 5px">Chat playground</h2><div id="chosen" class="muted" style="font-size:12px">No model selected</div></div><button class="secondary" id="clear">Clear chat</button></div>
<div class="messages" id="messages"><div class="empty">Load your configured models, choose one, then send a test prompt. Chat history is only shown in this browser tab.</div></div>
<form class="composer" id="form"><div class="field"><label for="prompt">MESSAGE</label><textarea id="prompt" required placeholder="Ask the selected model something…"></textarea></div><div class="actions"><button id="send">Send message</button><span id="request" class="muted" style="align-self:center;font-size:12px">Ready when configured.</span></div></form>
</section></div>
<div class="foot">Unified Multi-AI Gateway · <a href="/health">Health</a> · <a href="/docs">API docs</a> · <a href="https://github.com/godofgamerz2013-ui/AI" target="_blank" rel="noreferrer">Source on GitHub</a></div></main>
<script>
(()=>{const $=id=>document.getElementById(id);$("base").value=location.origin+"/v1";let busy=false;const headers=()=>{const k=$("key").value.trim();if(!k)throw Error("Paste your GATEWAY_API_KEY first.");return {"Authorization":"Bearer "+k,"Content-Type":"application/json"}};const status=(s,kind="")=>{$("status").textContent=s;$("status").className="status "+kind};const reqStatus=s=>$("request").textContent=s;const call=async(path,opts={})=>{const r=await fetch(path,{...opts,headers:{...headers(),...(opts.headers||{})}});const t=await r.text();let d;try{d=JSON.parse(t)}catch{d={detail:t}}if(!r.ok)throw Error("HTTP "+r.status+": "+(typeof d.detail==="string"?d.detail:JSON.stringify(d.detail||d)));return d};const add=(role,text)=>{const e=$("messages").querySelector(".empty");if(e)e.remove();const m=document.createElement("div");m.className="msg "+role;const r=document.createElement("div");r.className="role";r.textContent=role==="user"?"YOU":"ASSISTANT";const b=document.createElement("div");b.textContent=text;m.append(r,b);$("messages").append(m);m.scrollIntoView({block:"nearest"});return b};const health=async()=>{try{const r=await fetch("/health");const d=await r.json();if(!r.ok)throw Error("HTTP "+r.status);status("Online · "+d.service+" · v"+d.version,"good")}catch(e){status("Health check failed: "+e.message,"bad")}};$("health").onclick=health;$("load").onclick=async()=>{if(busy)return;busy=true;$("load").disabled=true;try{const d=await call("/v1/models");const s=$("model");s.replaceChildren(new Option("— Select a model —",""));for(const m of d.data||[])s.add(new Option(m.id,m.id));if(!(d.data||[]).length)throw Error("No configured models. Add at least one provider key or browser adapter in Vercel environment variables.");status("Connected · "+d.data.length+" model(s) loaded","good")}catch(e){status(e.message,"bad")}finally{busy=false;$("load").disabled=false}};$("model").onchange=()=>{$("chosen").textContent=$("model").value||"No model selected"};$("clear").onclick=()=>{$("messages").replaceChildren();const e=document.createElement("div");e.className="empty";e.textContent="Chat cleared."; $("messages").append(e)};$("form").onsubmit=async ev=>{ev.preventDefault();if(busy)return;const model=$("model").value,p=$("prompt").value.trim();if(!model){status("Load models and select one first.","bad");return}if(!p)return;busy=true;$("send").disabled=true;add("user",p);$("prompt").value="";const out=add("assistant","Waiting for response…");reqStatus("Request running…");try{const d=await call("/v1/chat/completions",{method:"POST",body:JSON.stringify({model,messages:[{role:"user",content:p}],stream:false})});out.textContent=d.choices?.[0]?.message?.content||"No text response returned.";reqStatus("Success · "+(d.gateway?.provider||model));status("Chat request completed.","good")}catch(e){out.textContent="Request failed: "+e.message;reqStatus("Failed");status(e.message,"bad")}finally{busy=false;$("send").disabled=false}};health()})();
</script></body></html>""")
