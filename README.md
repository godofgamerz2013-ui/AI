# Unified Multi-AI Gateway

OpenAI-style gateway for PC clients, Android apps and tools that accept an OpenAI-compatible endpoint. It supports server-side provider APIs and can fail over on common quota/rate-limit and transient service errors.

> **Hosting note:** GitHub Actions is CI/CD, not an always-on web host. The workflows test the code and publish a Docker image to GitHub Container Registry (GHCR). Run that image on a persistent VPS/container host for a stable endpoint.

## Implemented

- `POST /v1/chat/completions` — non-streaming OpenAI-compatible chat.
- `GET /v1/models` — models from configured providers.
- Bearer authentication with a separate gateway key.
- Fallback for HTTP 429, common 5xx/timeouts and selected quota-like messages.
- Anthropic Messages API mapped into a Chat Completions-shaped response.
- GitHub Actions CI and GHCR publishing.

## API integrations vs website GUI automation

**API integrations implemented:** OpenAI, Google's OpenAI-compatible Gemini API endpoint, OpenRouter, Groq, Together AI, Mistral, and Anthropic. Configure actual API keys from provider developer consoles. Model names, availability and quota policies depend on your account and can change.

Fallback is heuristic: HTTP 429, common temporary service errors/timeouts, and quota-like messages in selected 400/402/403 responses trigger attempts to the next provider. Provider error formats differ, so this cannot identify every billing/quota condition.

**Website GUI automation is not implemented as a provider.** Automating chatgpt.com, claude.ai, gemini.google.com and similar websites can break on UI/login/MFA/consent changes, and is not equivalent to a supported API. Consumer website subscriptions do not automatically provide API access. This repo does not scrape sites, replay private endpoints, bypass access controls, or store website passwords/cookies. Keep any separately approved browser automation experimental and on a persistent machine with user-controlled browser sessions, not public GitHub Actions jobs.

## Run locally (Windows PowerShell)

Requires Python 3.11+.

```powershell
git clone https://github.com/godofgamerz2013-ui/AI.git
cd AI
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:GATEWAY_API_KEY = "replace-with-a-long-random-key"
$env:OPENAI_API_KEY = "your-provider-api-key"
$env:OPENAI_MODELS = "gpt-4o-mini,gpt-4.1-mini"
uvicorn gateway.app:app --host 127.0.0.1 --port 8080
```

macOS/Linux: create a virtual environment, run `pip install -r requirements.txt`, export `GATEWAY_API_KEY` and at least one provider API key, then run `uvicorn gateway.app:app --host 127.0.0.1 --port 8080`. The app does not automatically load local `.env` files; export variables or use the host's secret manager.

## Request examples

Health: `GET http://127.0.0.1:8080/health`

```bash
curl http://127.0.0.1:8080/v1/chat/completions -H "Authorization: Bearer $GATEWAY_API_KEY" -H "Content-Type: application/json" -d '{"model":"auto","messages":[{"role":"user","content":"Say hello in one sentence."}],"stream":false}'
```

Choose a provider/model directly with `"model":"openai/gpt-4o-mini"` or `"provider":"google","model":"gemini-2.5-flash"`. `model:"auto"` tries configured providers in code order. `stream=true` is not supported yet and returns HTTP 400.

## PC, Android and MCP clients

For a client supporting OpenAI-compatible custom endpoints:
- **Base URL:** `https://YOUR-HOST/v1`
- **API key:** your gateway `GATEWAY_API_KEY`, not a vendor key.
- **Model:** `auto` or an ID returned by `GET /v1/models`.
- **Streaming:** disabled; set `stream=false`.

This is a model gateway, not an MCP server itself. If an MCP client requires the MCP protocol and cannot accept an OpenAI-compatible model endpoint, use an MCP-to-OpenAI adapter or a separate MCP server that calls this gateway.

## GitHub Actions and GHCR

On pushes to `main`, the workflow publishes `ghcr.io/godofgamerz2013-ui/unified-ai-gateway:latest`. Ensure Actions are enabled and `GITHUB_TOKEN` can write packages. Decide whether to make the GHCR image public or private.

Run the image on a persistent Linux Docker host:

```bash
docker pull ghcr.io/godofgamerz2013-ui/unified-ai-gateway:latest
docker run -d --name unified-ai-gateway --restart unless-stopped -p 127.0.0.1:8080:8080 -e GATEWAY_API_KEY='YOUR_LONG_RANDOM_GATEWAY_KEY' -e OPENAI_API_KEY='YOUR_PROVIDER_API_KEY' -e OPENAI_MODELS='gpt-4o-mini,gpt-4.1-mini' ghcr.io/godofgamerz2013-ui/unified-ai-gateway:latest
```

Place a TLS reverse proxy (such as Caddy/Nginx) or your hosting provider's HTTPS proxy in front. Do not expose the raw app port publicly without TLS.

## Security checklist

- Generate a long random gateway key, distinct from vendor API keys.
- Store provider keys in the persistent host's secret manager/environment. Never commit real values, bake them into the image, or ship them in PC/Android apps.
- Use HTTPS, rate limits, request-size limits and per-user keys before serving other people.
- Keep Actions permissions minimal; use environment secrets/OIDC for deployment when supported.
- Rotate secrets if exposed in chats, commits or logs. Avoid logging prompts and responses.
- This starter does not include multi-user access controls, persistent usage metering, rate limiting or spend limits. Do not expose it to untrusted users until added.

## Limitations

Non-streaming text chat only. Image/audio inputs, tool calling, streaming, usage accounting, dashboards, exponential backoff and persistent quota tracking are not implemented. Verify provider model names and API support against your account before production use.
