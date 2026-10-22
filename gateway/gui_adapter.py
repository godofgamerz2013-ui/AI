from __future__ import annotations

import asyncio
import os
import time
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


class GUIAdapterError(RuntimeError):
    """Raised when an authenticated browser GUI is unavailable or did not respond."""


TARGETS = {
    "chatgpt_gui": {
        "prefix": "CHATGPT_GUI",
        "default_url": "https://chatgpt.com/",
        "default_model": "chatgpt-web",
    },
    "claude_gui": {
        "prefix": "CLAUDE_GUI",
        "default_url": "https://claude.ai/new",
        "default_model": "claude-web",
    },
    "gemini_gui": {
        "prefix": "GEMINI_GUI",
        "default_url": "https://gemini.google.com/app",
        "default_model": "gemini-web",
    },
}

# Serialize UI interactions: most chat websites have one active composer per page.
_ui_lock = asyncio.Lock()


def _messages_to_prompt(messages: list[dict]) -> str:
    chunks = []
    for message in messages:
        role = str(message.get("role", "user")).strip().upper()
        content = message.get("content", "")
        if isinstance(content, list):
            # Plain text blocks only. This adapter intentionally doesn't emulate image/audio uploads.
            text_parts = [str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("type") in ("text", "input_text")]
            content = "\n".join(text_parts)
        if not isinstance(content, str):
            content = str(content)
        if content.strip():
            chunks.append(f"{role}:\n{content.strip()}")
    if not chunks:
        raise GUIAdapterError("No text messages were supplied to the browser GUI adapter.")
    return (
        "The following is a conversation transcript for context. Continue as the assistant and "
        "answer the final user message. Do not repeat this instruction.\n\n"
        + "\n\n".join(chunks)
    )


def _quota_message(text: str) -> bool:
    low = text.lower()
    phrases = (
        "you've reached your limit", "you have reached your limit",
        "usage limit", "message limit", "rate limit", "too many requests",
        "quota exceeded", "try again later", "come back later",
        "daily limit reached", "weekly limit reached",
    )
    return any(phrase in low for phrase in phrases)


async def complete_via_gui(provider_name: str, messages: list[dict]) -> tuple[str, bool]:
    """Use a user-authenticated browser over a private Chrome DevTools Protocol connection.

    Required environment variables per target:
      <PREFIX>_CDP_URL, <PREFIX>_INPUT_SELECTOR, <PREFIX>_SEND_SELECTOR,
      <PREFIX>_RESPONSE_SELECTOR
    Optional:
      <PREFIX>_CHAT_URL, <PREFIX>_TIMEOUT_SECONDS
    """
    target = TARGETS.get(provider_name)
    if not target:
        raise GUIAdapterError(f"Unknown GUI provider '{provider_name}'.")
    prefix = target["prefix"]
    cdp_url = os.getenv(f"{prefix}_CDP_URL", "").strip()
    chat_url = os.getenv(f"{prefix}_CHAT_URL", target["default_url"]).strip()
    input_selector = os.getenv(f"{prefix}_INPUT_SELECTOR", "").strip()
    send_selector = os.getenv(f"{prefix}_SEND_SELECTOR", "").strip()
    response_selector = os.getenv(f"{prefix}_RESPONSE_SELECTOR", "").strip()
    timeout_seconds = max(15, min(int(os.getenv(f"{prefix}_TIMEOUT_SECONDS", "120")), 300))

    missing = [
        env for env, value in (
            (f"{prefix}_CDP_URL", cdp_url),
            (f"{prefix}_INPUT_SELECTOR", input_selector),
            (f"{prefix}_SEND_SELECTOR", send_selector),
            (f"{prefix}_RESPONSE_SELECTOR", response_selector),
        ) if not value
    ]
    if missing:
        raise GUIAdapterError("Browser GUI adapter isn't configured. Missing: " + ", ".join(missing))

    prompt = _messages_to_prompt(messages)
    target_host = urlparse(chat_url).netloc
    async with _ui_lock:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(cdp_url, timeout=15000)
                if not browser.contexts:
                    raise GUIAdapterError("No browser context is available at the CDP endpoint.")
                context = browser.contexts[0]
                page = next(
                    (candidate for candidate in context.pages
                     if urlparse(candidate.url).netloc == target_host),
                    None,
                )
                if page is None:
                    page = await context.new_page()
                    await page.goto(chat_url, wait_until="domcontentloaded", timeout=30000)

                composer = page.locator(input_selector).last
                await composer.wait_for(state="visible", timeout=15000)
                responses = page.locator(response_selector)
                count_before = await responses.count()
                old_text = ""
                if count_before:
                    try:
                        old_text = (await responses.nth(count_before - 1).inner_text(timeout=1500)).strip()
                    except PlaywrightError:
                        old_text = ""

                await composer.fill(prompt, timeout=15000)
                await page.locator(send_selector).last.click(timeout=15000)

                start = time.monotonic()
                last_text, stable_reads = "", 0
                while time.monotonic() - start < timeout_seconds:
                    await asyncio.sleep(0.5)
                    count_now = await responses.count()
                    if not count_now:
                        continue
                    try:
                        text = (await responses.nth(count_now - 1).inner_text(timeout=2000)).strip()
                    except PlaywrightError:
                        continue
                    if not text or (count_now <= count_before and text == old_text):
                        continue
                    if text == last_text:
                        stable_reads += 1
                    else:
                        last_text, stable_reads = text, 0
                    # Require several unchanged observations to avoid returning a partial stream.
                    if stable_reads >= 3:
                        return text, _quota_message(text)

                raise GUIAdapterError(
                    f"No stable assistant response was detected within {timeout_seconds} seconds. "
                    "Check the site's selectors, login state and any quota/consent prompts."
                )
        except GUIAdapterError:
            raise
        except PlaywrightTimeoutError as exc:
            raise GUIAdapterError("Browser GUI timed out. Check login state and configured CSS selectors.") from exc
        except PlaywrightError as exc:
            raise GUIAdapterError(
                "Could not connect to the configured browser GUI. Confirm the CDP endpoint is reachable "
                "on a private network and that Chromium is running with remote debugging enabled."
            ) from exc
