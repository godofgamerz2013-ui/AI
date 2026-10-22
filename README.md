# Unified Multi-AI Gateway

OpenAI-compatible model gateway for PC clients, Android apps, and MCP tools that accept an OpenAI-style endpoint. It supports both official provider APIs and optional user-controlled website GUI automation, kept explicitly separate.

> **Hosting note:** GitHub Actions is CI/CD, not an always-on web host. This repository uses Actions to validate the code and publish a Docker image to GitHub Container Registry (GHCR). Run the image on a persistent VPS/container host for a stable endpoint. A signed-in browser session must also live on a persistent machine.

## Endpoint

- `POST /v1/chat/completions`
- `GET /v1/models`
- `GET /health`
- Bearer authentication using `GATEWAY_API_KEY`.
- Non-streaming chat only. `model: "auto"` starts the ordered fallback chain; explicit provider/model IDs start at that provider and can fall back on upstream failures. Default order: GPT → Gemini → DeepSeek → Claude → OpenRouter → LocalAI. Only configured providers are tried.

## Two different connection methods

### 1. Official/server API adapters (recommended for stability)

Implemented API adapters: OpenAI GPT, Google's OpenAI-compatible Gemini endpoint, DeepSeek, Anthropic Claude Messages API (mapped into a Chat Completions-shaped response), OpenRouter, LocalAI (OpenAI-compatible, self-hosted), Groq, Together AI, and Mistral. Configure developer API keys; a consumer chat website subscription is not the same thing as API access.

Fallback is attempted on upstream authentication errors, rate limits, billing/payment failures, missing models, timeouts, and server errors. Provider responses differ, so no fallback system can fix an exhausted quota or invalid model automatically; it only tries the next configured backend. Use `model: "auto"` when you want the gateway to begin at GPT rather than at a specific selected provider.

### 2. Website GUI adapters (experimental; separate from APIs)

The optional Playwright adapter can connect to a browser that the user has already signed into for:
- ChatGPT web (`chatgpt_gui/chatgpt-web`)
- Claude web (`claude_gui/claude-web`)
- Gemini web (`gemini_gui/gemini-web`)

For each site, set its `*_GUI_CDP_URL`, `*_GUI_INPUT_SELECTOR`, `*_GUI_SEND_SELECTOR`, and `*_GUI_RESPONSE_SELECTOR`. CSS selectors can change when the site changes, so they are intentionally not assumed reliable by default. The adapter sends a text transcript into the browser UI and reads the latest assistant element; it does not upload images/audio or emulate website internals. If a quota notice is rendered inside the selected assistant response, common phrases trigger fallback to the next configured provider.

**Browser-GUI limitations and security:** GUI workflows are less reliable than APIs and can fail on UI changes, login/MFA, consent pages, anti-bot checks, queue screens or quota banners that are outside the configured response selector. This adapter does not scrape private endpoints, bypass controls, or store consumer website passwords/cookies. Keep the browser signed-in manually and user-controlled.

The Chrome DevTools Protocol (CDP) endpoint has powerful browser control. Never expose its port to the public internet. Prefer localhost or a private VPN/tunnel between the gateway and browser machine, and use a dedicated browser profile. The gateway host must be able to reach the CDP endpoint.

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

For API-only use, configure one or more provider keys. For GUI-only use, set `GATEWAY_API_KEY`, the selected site CDP URL, and its CSS selectors instead. The app does not auto-load a local `.env`; export variables or use a host secret manager. The browser tester is deployed to GitHub Pages from `index.html` after the branch is merged into `main`. Its default allowed CORS origin is `https://godofgamerz2013-ui.github.io`; set `CORS_ORIGINS` to a comma-separated list if you use a different Pages/custom domain.

## Configure a GUI browser

1. Install Chrome/Chromium on the persistent browser machine and sign into the target website yourself.
2. Start a dedicated browser profile with remote debugging enabled. Example for Windows (update the Chrome executable/path and use a dedicated profile):
   ```powershell
   & "$env:ProgramFiles\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="$env:LOCALAPPDATA\AI-Gateway-Chrome"
   ```
3. Keep port 9222 bound to localhost/private networking only. Do not port-forward it directly to the internet. If the gateway runs elsewhere, use a trusted private network/VPN.
4. Set `CHATGPT_GUI_CDP_URL=http://127.0.0.1:9222` (or the private address reachable from the gateway), a chat URL, and the three site-specific CSS selectors. Repeat with separate ports/profiles for Claude or Gemini. Inspect the page's current DOM in DevTools; selector values are site/version dependent.
5. Restart the gateway after exporting the settings. Use `GET /v1/models` to see configured model IDs.

## Request example

```bash
curl http://127.0.0.1:8080/v1/chat/completions -H "Authorization: Bearer $GATEWAY_API_KEY" -H "Content-Type: application/json" -d '{"model":"auto","messages":[{"role":"user","content":"Say hello in one sentence."}],"stream":false}'
```

Choose one provider explicitly, e.g. `"model":"openai/gpt-4o-mini"`, `"model":"chatgpt_gui/chatgpt-web"`, or `"provider":"gemini_gui","model":"gemini-web"`. The gateway can fail over in its configured order only when `model:"auto"` is used. `stream=true` returns HTTP 400.

## PC, Android and MCP clients

For a client supporting OpenAI-compatible custom endpoints:
- **Base URL:** `https://YOUR-HOST/v1`
- **API key:** your gateway `GATEWAY_API_KEY`, not provider keys.
- **Model:** select an exact ID from `GET /v1/models`; `auto` is disabled.
- **Streaming:** disabled.

This is a model gateway, not an MCP server itself. If an MCP client requires MCP protocol tools and cannot accept an OpenAI-style endpoint, use an MCP-to-OpenAI adapter or a separate MCP server that calls the gateway.

## GitHub Actions and GHCR

On pushes to `main`, Actions publishes `ghcr.io/godofgamerz2013-ui/unified-ai-gateway:latest`. Ensure Actions are enabled and the workflow token can write packages. Actions does not keep this image running; deploy it on a persistent container host.

Example command on a persistent Linux Docker host:

```bash
docker pull ghcr.io/godofgamerz2013-ui/unified-ai-gateway:latest
docker run -d --name unified-ai-gateway --restart unless-stopped -p 127.0.0.1:8080:8080 -e GATEWAY_API_KEY='YOUR_LONG_RANDOM_GATEWAY_KEY' -e OPENAI_API_KEY='YOUR_PROVIDER_API_KEY' ghcr.io/godofgamerz2013-ui/unified-ai-gateway:latest
```

Place a TLS reverse proxy (Caddy/Nginx or your host's HTTPS proxy) in front. For GUI mode, the running gateway additionally needs private-network access to the signed-in browser's CDP endpoint.

## Security checklist

- Generate a long random gateway bearer key, distinct from provider API keys.
- Store all secret keys in your host's secret manager/environment. Never commit real secrets or put keys in client apps.
- Keep the CDP endpoint on localhost/private networking only; a CDP endpoint effectively grants control of the signed-in browser.
- Use HTTPS, rate limits, request-size limits and separate per-user keys before sharing the gateway with others.
- Keep Actions permissions minimal; use environment secrets/OIDC for deployment where supported.
- Rotate secrets if exposed in chats, commits or logs. Avoid logging prompt/response bodies.
- This starter has no multi-user access controls, persistent quota accounting, rate limiting or spend limits. Do not expose it to untrusted users until those protections are added.

## Limitations

Non-streaming text only. Images/audio, tool/function calling, streaming, persistent usage tracking, dashboards, and exponential backoff are not implemented. Website GUI automation is experimental and selector-dependent. Verify current provider model names/API behavior before production use.


## Browser HTML tester

The static tester lives in [index.html](./index.html). After the pull request is merged to `main` and GitHub Pages is enabled with **GitHub Actions** as its build/deployment source, the Pages workflow deploys it at:

`https://godofgamerz2013-ui.github.io/AI/`

The tester requires you to enter your separately hosted gateway HTTPS URL, gateway key and an explicit model choice. It fetches `/v1/models` and calls `/v1/chat/completions`; the API service must be running on a persistent host and allow the Pages origin through `CORS_ORIGINS`. GitHub Actions builds/deploys the static HTML, but does not run the API server continuously.


## Ordered fallback: GPT → Gemini → DeepSeek → Claude → OpenRouter → LocalAI

The dashboard includes an `auto` model option. Send a request with `"model":"auto"` and the gateway tries configured providers in this order:

1. `openai` — GPT, using `OPENAI_API_KEY`
2. `google` — Gemini, using `GOOGLE_API_KEY`
3. `deepseek` — DeepSeek, using `DEEPSEEK_API_KEY`
4. `anthropic` — Claude API, using `ANTHROPIC_API_KEY`
5. `openrouter` — OpenRouter, using `OPENROUTER_API_KEY`
6. `localai` — LocalAI, using a local/self-hosted OpenAI-compatible endpoint

The order is configurable with `PROVIDER_FALLBACK_ORDER`. A provider with no configured key is skipped. Error status and provider attempts are included in the final error response if all candidates fail. Provider presence in the dashboard does not mean credits are available; GPT's 429 usually indicates rate/quota exhaustion, and HTTP 402 from paid APIs generally indicates billing/credits/payment. A 404 can indicate an unavailable model ID or endpoint version.

## Install LocalAI as the final fallback

LocalAI is **a separate model server**, not a package that can run inside the Vercel serverless function. The repo now includes `docker-compose.localai.yml` and `.env.localai.example` for a persistent Linux VPS/PC. Official project: [mudler/LocalAI](https://github.com/mudler/LocalAI), [container install docs](https://localai.io/installation/containers/), [quickstart](https://localai.io/basics/getting_started/).

On the persistent host:

```bash
git clone https://github.com/godofgamerz2013-ui/AI.git
cd AI
cp .env.localai.example .env.localai
# Edit .env.localai: set a long random GATEWAY_API_KEY and LOCALAI_API_KEY,
# and add the real cloud API keys you want the VPS gateway to use.
docker compose --env-file .env.localai -f docker-compose.localai.yml up -d --build
```

The compose stack starts LocalAI with the `qwen3-4b` gallery model and starts the gateway on `127.0.0.1:8081`. First startup downloads model/backend files and may take several minutes and substantial disk/RAM; wait for health checks to finish. The LocalAI API stays inside the Docker network and is protected with `LOCALAI_API_KEY`. Check installed model IDs from LocalAI's `/v1/models` endpoint (reachable from inside the container network) and adjust `LOCALAI_MODELS` to the exact name if needed.

For remote clients, put a TLS reverse proxy (such as Caddy/Nginx) in front of port 8081 or use a private VPN. Do not directly expose unauthenticated LocalAI. If keeping the gateway API on Vercel instead of the combined compose stack, `LOCALAI_BASE_URL` must be an HTTPS/private-network endpoint reachable from Vercel; `http://127.0.0.1:8080` in Vercel would refer to the Vercel function sandbox, not your PC or VPS. I cannot start this container on your VPS without access to that host, so the code and reproducible runtime stack are committed, but LocalAI must be launched on a machine you control.
