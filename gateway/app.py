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
app = FastAPI(title=APP_NAME, version="0.3.0", docs_url=None, redoc_url=None)
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


PROVIDERS = {
    # Official/server API integrations.
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
    "openrouter": {
        "base_url": os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        "key_env": "OPENROUTER_API_KEY", "models_env": "OPENROUTER_MODELS",
        "default_models": ["openrouter/auto"], "mode": "openai",
    },
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
    "anthropic": {
        "base_url": os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"),
        "key_env": "ANTHROPIC_API_KEY", "models_env": "ANTHROPIC_MODELS",
        "default_models": ["claude-3-5-haiku-latest"], "mode": "anthropic",
    },
    # Optional website GUI adapters. They require a user-signed-in browser over private CDP.
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
        models = [s.strip() for s in os.getenv(cfg["models_env"], "").split(",") if s.strip()]
        out.append({**cfg, "name": name, "models": models or cfg["default_models"]})
    return out


def authenticate(authorization: str | None = Header(default=None)) -> None:
    if not API_KEY:
        raise HTTPException(status_code=503, detail="Gateway not configured: set GATEWAY_API_KEY.")
    if not authorization or not hmac.compare_digest(authorization, f"Bearer {API_KEY}"):
        raise HTTPException(status_code=401, detail="Invalid bearer token.")


def candidate_models(req: ChatRequest) -> list[tuple[dict[str, Any], str]]:
    providers = configured_providers()
    if not providers:
        raise HTTPException(status_code=503, detail="No providers configured; set at least one API key or GUI CDP endpoint.")
    provider_name, model = req.provider, req.model or "auto"
    if not provider_name and "/" in model:
        prefix, remainder = model.split("/", 1)
        if prefix in PROVIDERS:
            provider_name, model = prefix, remainder
    if provider_name:
        providers = [p for p in providers if p["name"] == provider_name]
        if not providers:
            raise HTTPException(status_code=400, detail=f"Provider '{provider_name}' is not configured.")
    candidates = []
    for provider in providers:
        if model == "auto":
            selected = provider["models"][0]
        elif provider_name or model in provider["models"]:
            selected = model
        else:
            continue
        candidates.append((provider, selected))
    if not candidates:
        raise HTTPException(status_code=400, detail="Unknown model. Use 'auto', 'provider/model', or configure provider model lists.")
    return candidates


def can_fail_over(status: int, body: str) -> bool:
    if status in (408, 429, 500, 502, 503, 504):
        return True
    lower = body.lower()
    quota_terms = ("quota", "rate limit", "rate_limit", "insufficient_quota", "billing hard limit", "credits exhausted")
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
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": answer},
                    "finish_reason": "stop",
                }],
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
    return {"ok": True, "service": APP_NAME, "version": "0.3.0"}


@app.get("/v1/models", dependencies=[Depends(authenticate)])
async def list_models():
    data = [{"id": f'{p["name"]}/{model}', "object": "model", "owned_by": p["name"]}
            for p in configured_providers() for model in p["models"]]
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


@app.get("/")
async def root():
    return {"name": APP_NAME, "health": "/health", "openai_compatible_endpoint": "/v1/chat/completions",
            "models": "/v1/models", "note": "API adapters and optional user-controlled GUI browser adapters; no website scraping or private endpoint replay."}
