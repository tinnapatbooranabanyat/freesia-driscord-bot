"""Minimal async client for any OpenAI-compatible chat completions API (Groq, Gemini, OpenRouter, ...)."""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field

import aiohttp

log = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_CITATION_RE = re.compile(r"【[^】]*】")  # gpt-oss browser citations like 【2†L6-L10】


_TRY_AGAIN_RE = re.compile(r"try again in (?:(\d+)m)?([\d.]+)(ms|s)")
MAX_RETRY_WAIT = 20  # seconds we're willing to make a user wait for a rate limit to reset


class LLMError(Exception):
    pass


def _retry_after(header: str | None, body: str) -> float | None:
    """Seconds until a 429 rate limit resets, from the Retry-After header or Groq's error message."""
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    m = _TRY_AGAIN_RE.search(body)
    if not m:
        return None
    minutes, value, unit = m.groups()
    seconds = float(value) / 1000 if unit == "ms" else float(value)
    return seconds + 60 * int(minutes or 0)


@dataclass
class LLMReply:
    text: str
    sources: list[tuple[str, str]] = field(default_factory=list)  # (title, url) found by web search


class LLMClient:
    def __init__(self, base_url: str, api_key: str, models: list[str], max_tokens: int = 1500, web_search: bool = True):
        if not models:
            raise ValueError("LLM_MODEL must contain at least one model")
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.models = models
        self.max_tokens = max_tokens
        self.web_search = web_search
        self.whisper_model = os.getenv("WHISPER_MODEL", "whisper-large-v3-turbo")
        self._session: aiohttp.ClientSession | None = None

    def supports_search(self, model: str) -> bool:
        # Groq runs web search server-side for gpt-oss models via the built-in "browser_search" tool
        return self.web_search and "groq.com" in self.url and model.startswith("openai/gpt-oss")

    @property
    def search_available(self) -> bool:
        return any(self.supports_search(m) for m in self.models)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def chat(self, messages: list[dict], allow_search: bool = True) -> LLMReply:
        """Try every model (with web search first where supported). If all are only briefly rate limited
        (free tiers limit tokens per minute), wait a few seconds and try once more."""
        errors: list[str] = []
        for attempt in range(2):
            reply, retry_after = await self._try_models(messages, errors, allow_search)
            if reply:
                return reply
            if attempt == 0 and retry_after is not None and retry_after <= MAX_RETRY_WAIT:
                log.info("All models rate limited; retrying in %.1fs", retry_after)
                await asyncio.sleep(retry_after + 0.5)
                continue
            break
        raise LLMError("; ".join(errors) or "no models available")

    async def _try_models(
        self, messages: list[dict], errors: list[str], allow_search: bool
    ) -> tuple[LLMReply | None, float | None]:
        session = await self._get_session()
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        shortest_wait: float | None = None

        attempts = []
        for model in self.models:
            if allow_search and self.supports_search(model):
                attempts.append((model, True))
            attempts.append((model, False))

        for model, search in attempts:
            label = f"{model}{' +search' if search else ''}"
            payload = {"model": model, "messages": messages, "max_tokens": self.max_tokens, "temperature": 0.7}
            if search:
                payload.update(tools=[{"type": "browser_search"}], tool_choice="auto", reasoning_effort="low")
            try:
                async with session.post(self.url, json=payload, headers=headers) as resp:
                    if resp.status != 200:
                        body = (await resp.text())[:300]
                        errors.append(f"{label}: HTTP {resp.status} {body}")
                        log.warning("LLM %s failed with %s: %s", label, resp.status, body)
                        # 401/403 = bad key; no point trying other models
                        if resp.status in (401, 403):
                            return None, None
                        if resp.status == 429:
                            wait = _retry_after(resp.headers.get("retry-after"), body)
                            if wait is not None:
                                shortest_wait = wait if shortest_wait is None else min(shortest_wait, wait)
                        continue
                    data = await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                errors.append(f"{label}: {e!r}")
                log.warning("LLM %s request error: %r", label, e)
                continue

            try:
                message = data["choices"][0]["message"]
                content = message.get("content") or ""
            except (KeyError, IndexError, TypeError):
                errors.append(f"{label}: unexpected response {str(data)[:200]}")
                continue

            content = _CITATION_RE.sub("", _THINK_RE.sub("", content)).strip()
            if content:
                return LLMReply(content, self._sources(message)), None
            errors.append(f"{label}: empty response")

        return None, shortest_wait

    async def transcribe(self, audio: bytes, filename: str, content_type: str = "audio/ogg") -> str:
        """Speech to text with the provider's Whisper endpoint (Groq offers it on the free tier)."""
        session = await self._get_session()
        form = aiohttp.FormData()
        form.add_field("file", audio, filename=filename, content_type=content_type)
        form.add_field("model", self.whisper_model)
        try:
            async with session.post(self.url.replace("/chat/completions", "/audio/transcriptions"), data=form,
                                    headers={"Authorization": f"Bearer {self.api_key}"}) as resp:
                if resp.status != 200:
                    raise LLMError(f"transcription HTTP {resp.status}: {(await resp.text())[:200]}")
                return ((await resp.json()).get("text") or "").strip()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise LLMError(f"transcription failed: {e!r}") from e

    @staticmethod
    def _sources(message: dict) -> list[tuple[str, str]]:
        sources: list[tuple[str, str]] = []
        for tool in message.get("executed_tools") or []:
            results = (tool.get("search_results") or {}).get("results") or []
            for r in results:
                url = r.get("url")
                if url and url not in (u for _, u in sources):
                    sources.append((r.get("title") or url, url))
        return sources
