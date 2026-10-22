import importlib

from fastapi.testclient import TestClient

gateway = importlib.import_module("gateway.app")


def clear_provider_env(monkeypatch):
    for provider in gateway.PROVIDERS.values():
        monkeypatch.setenv(provider["key_env"], "")
        monkeypatch.setenv(provider["models_env"], "")


def test_health_is_public(monkeypatch):
    monkeypatch.setattr(gateway, "API_KEY", "test-gateway-key")
    with TestClient(gateway.app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_models_requires_gateway_key(monkeypatch):
    clear_provider_env(monkeypatch)
    monkeypatch.setattr(gateway, "API_KEY", "test-gateway-key")
    with TestClient(gateway.app) as client:
        response = client.get("/v1/models")
    assert response.status_code == 401


def test_auto_falls_back_after_quota(monkeypatch):
    clear_provider_env(monkeypatch)
    monkeypatch.setattr(gateway, "API_KEY", "test-gateway-key")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-openai-key")
    monkeypatch.setenv("OPENAI_MODELS", "model-a")
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy-google-key")
    monkeypatch.setenv("GOOGLE_MODELS", "model-b")
    calls = []

    async def fake_call(provider, model, payload):
        calls.append((provider["name"], model))
        if provider["name"] == "openai":
            return 429, None, "insufficient_quota"
        return 200, {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "fallback worked"}, "finish_reason": "stop"}],
        }, "ok"

    monkeypatch.setattr(gateway, "call_provider", fake_call)
    with TestClient(gateway.app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-gateway-key"},
            json={"model": "auto", "messages": [{"role": "user", "content": "hello"}], "stream": False},
        )
    assert response.status_code == 200, response.text
    assert calls == [("openai", "model-a"), ("google", "model-b")]
    assert response.json()["choices"][0]["message"]["content"] == "fallback worked"
    assert response.json()["gateway"]["fallback_count"] == 1


def test_streaming_is_rejected(monkeypatch):
    monkeypatch.setattr(gateway, "API_KEY", "test-gateway-key")
    with TestClient(gateway.app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-gateway-key"},
            json={"model": "auto", "messages": [{"role": "user", "content": "hello"}], "stream": True},
        )
    assert response.status_code == 400
