import asyncio
import html
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from time import perf_counter
from typing import Protocol, cast, runtime_checkable

import httpx
from google import genai
from google.genai import types

from alza_ai.domain import AttachmentInsight, GeneratedReply

DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"
DEFAULT_OPENROUTER_MODEL = "anthropic/claude-opus-5"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_OUTPUT_TOKENS = 2_048
MAX_REPLY_CHARACTERS = 8_000
MAX_USAGE_TOKENS = 1_000_000
MAX_LATENCY_MS = 3_600_000
REQUEST_TIMEOUT_SECONDS = 30.0

REPLY_SYSTEM_INSTRUCTION = (
    "Draft one concise, helpful email reply using only the supplied current email "
    "text and attachment insights. Return plain text only. Do not use live search."
)


class RetryClassification(StrEnum):
    RETRYABLE = "retryable"
    TERMINAL = "terminal"


class ReplyConfigurationError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ReplyProviderError(Exception):
    def __init__(self, code: str, classification: RetryClassification) -> None:
        self.code = code
        self.classification = classification
        super().__init__(f"{code}:{classification.value}")


@runtime_checkable
class ReplyProvider(Protocol):
    async def generate(
        self,
        *,
        current_text: str,
        attachment_insights: Sequence[AttachmentInsight],
    ) -> GeneratedReply: ...


class _Clock(Protocol):
    def __call__(self) -> float: ...


class _GeminiUsage(Protocol):
    prompt_token_count: object
    candidates_token_count: object
    total_token_count: object


class _GeminiResponse(Protocol):
    text: object
    usage_metadata: _GeminiUsage | None


class _GeminiModels(Protocol):
    async def generate_content(self, **kwargs: object) -> _GeminiResponse: ...


class _GeminiAio(Protocol):
    models: _GeminiModels


class _GeminiClient(Protocol):
    aio: _GeminiAio


class _GeminiClientFactory(Protocol):
    def __call__(self, **kwargs: object) -> object: ...


class _OpenRouterClient(Protocol):
    async def post(self, url: str, **kwargs: object) -> httpx.Response: ...


class _OpenRouterClientFactory(Protocol):
    def __call__(self) -> object: ...


@dataclass(frozen=True, slots=True)
class _RawReply:
    text: object
    input_tokens: object
    output_tokens: object
    total_tokens: object


class _BaseReplyProvider:
    provider: str

    def __init__(self, *, model: str, clock: _Clock) -> None:
        self.model = model
        self._clock = clock

    async def generate(
        self,
        *,
        current_text: str,
        attachment_insights: Sequence[AttachmentInsight],
    ) -> GeneratedReply:
        started = self._clock()
        payload = _reply_payload(current_text, attachment_insights)
        try:
            raw_reply = await self._generate_raw(payload)
        except asyncio.CancelledError:
            raise
        except ReplyProviderError:
            raise
        except Exception as error:  # noqa: BLE001 - sanitize provider boundary
            raise _normalized_provider_error(error) from None
        provider_finished = self._clock()
        reply_text, reply_html = _reply_alternatives(raw_reply.text)
        finished = self._clock()
        provider_latency = _latency_ms(provider_finished - started)
        total_latency = _latency_ms(finished - started)
        return GeneratedReply(
            text=reply_text,
            html=reply_html,
            citations=(),
            provider=self.provider,
            model=self.model,
            input_tokens=_bounded_token_count(raw_reply.input_tokens),
            output_tokens=_bounded_token_count(raw_reply.output_tokens),
            total_tokens=_bounded_token_count(raw_reply.total_tokens),
            provider_latency_ms=min(provider_latency, total_latency),
            total_latency_ms=total_latency,
        )

    async def _generate_raw(self, payload: str) -> _RawReply:
        raise NotImplementedError


class GeminiReplyProvider(_BaseReplyProvider):
    provider = "gemini"

    def __init__(
        self,
        *,
        project_id: str | None,
        model: str = DEFAULT_GEMINI_MODEL,
        client_factory: object = genai.Client,
        clock: _Clock = perf_counter,
    ) -> None:
        super().__init__(model=model, clock=clock)
        factory = cast(_GeminiClientFactory, client_factory)
        self._client = cast(
            _GeminiClient,
            factory(
                vertexai=True,
                project=project_id,
                location="global",
                http_options=types.HttpOptions(
                    api_version="v1",
                    timeout=int(REQUEST_TIMEOUT_SECONDS * 1_000),
                ),
            ),
        )

    async def _generate_raw(self, payload: str) -> _RawReply:
        response = await self._client.aio.models.generate_content(
            model=self.model,
            contents=payload,
            config=types.GenerateContentConfig(
                system_instruction=REPLY_SYSTEM_INSTRUCTION,
                max_output_tokens=MAX_OUTPUT_TOKENS,
                temperature=0,
            ),
        )
        usage = response.usage_metadata
        return _RawReply(
            text=response.text,
            input_tokens=getattr(usage, "prompt_token_count", 0),
            output_tokens=getattr(usage, "candidates_token_count", 0),
            total_tokens=getattr(usage, "total_token_count", 0),
        )


class OpenRouterReplyProvider(_BaseReplyProvider):
    provider = "openrouter"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_OPENROUTER_MODEL,
        http_client: object,
        clock: _Clock = perf_counter,
    ) -> None:
        super().__init__(model=model, clock=clock)
        self._api_key = api_key
        self._http_client = cast(_OpenRouterClient, http_client)

    async def _generate_raw(self, payload: str) -> _RawReply:
        response = await self._http_client.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": REPLY_SYSTEM_INSTRUCTION},
                    {"role": "user", "content": payload},
                ],
                "max_tokens": MAX_OUTPUT_TOKENS,
                "temperature": 0,
                "stream": False,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        try:
            value = response.json()
            if not isinstance(value, dict):
                raise TypeError
            choices = value["choices"]
            if not isinstance(choices, list) or not choices:
                raise TypeError
            first_choice = choices[0]
            if not isinstance(first_choice, dict):
                raise TypeError
            message = first_choice["message"]
            if not isinstance(message, dict):
                raise TypeError
            text = message["content"]
            usage = value.get("usage", {})
            if not isinstance(usage, dict):
                usage = {}
        except KeyError, TypeError, ValueError:
            raise _invalid_response() from None
        return _RawReply(
            text=text,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
        )


def load_reply_provider(
    environ: Mapping[str, str] | None = None,
    *,
    gemini_client_factory: object = genai.Client,
    openrouter_client_factory: object = httpx.AsyncClient,
    clock: _Clock = perf_counter,
) -> ReplyProvider:
    settings = os.environ if environ is None else environ
    configured_provider = settings.get("RESPONSE_PROVIDER")
    if configured_provider is None:
        selected_provider = "gemini"
    else:
        selected_provider = configured_provider.strip()
        if selected_provider not in {"gemini", "openrouter"}:
            raise ReplyConfigurationError("reply_provider_invalid")

    if selected_provider == "gemini":
        model = _selected_model(settings, "GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
        return GeminiReplyProvider(
            project_id=settings.get("GOOGLE_CLOUD_PROJECT"),
            model=model,
            client_factory=gemini_client_factory,
            clock=clock,
        )

    api_key = settings.get("OPENROUTER_API_KEY")
    if api_key is None or not api_key.strip():
        raise ReplyConfigurationError("openrouter_api_key_missing")
    model = _selected_model(
        settings,
        "OPENROUTER_MODEL",
        DEFAULT_OPENROUTER_MODEL,
    )
    client_factory = cast(_OpenRouterClientFactory, openrouter_client_factory)
    return OpenRouterReplyProvider(
        api_key=api_key,
        model=model,
        http_client=client_factory(),
        clock=clock,
    )


def _selected_model(
    settings: Mapping[str, str],
    key: str,
    default: str,
) -> str:
    configured_model = settings.get(key)
    if configured_model is None:
        return default
    model = configured_model.strip()
    if not model:
        raise ReplyConfigurationError("reply_provider_model_invalid")
    return model


def _reply_payload(
    current_text: str,
    attachment_insights: Sequence[AttachmentInsight],
) -> str:
    return json.dumps(
        {
            "current_email_text": current_text,
            "attachment_insights": [
                {
                    "filename": insight.filename,
                    "media_type": insight.media_type,
                    "summary": insight.summary,
                    "extracted_text": insight.extracted_text,
                    "relevant_facts": list(insight.relevant_facts),
                    "warnings": list(insight.warnings),
                }
                for insight in attachment_insights
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _reply_alternatives(value: object) -> tuple[str, str]:
    if not isinstance(value, str):
        raise _invalid_response()
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise _invalid_response()

    text_characters: list[str] = []
    html_fragments: list[str] = []
    html_length = 0
    for character in normalized:
        escaped = "<br>" if character == "\n" else html.escape(character)
        if (
            len(text_characters) == MAX_REPLY_CHARACTERS
            or html_length + len(escaped) > MAX_REPLY_CHARACTERS
        ):
            break
        text_characters.append(character)
        html_fragments.append(escaped)
        html_length += len(escaped)
    return "".join(text_characters), "".join(html_fragments)


def _bounded_token_count(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return 0
    return min(max(value, 0), MAX_USAGE_TOKENS)


def _latency_ms(seconds: float) -> int:
    return min(max(round(seconds * 1_000), 0), MAX_LATENCY_MS)


def _invalid_response() -> ReplyProviderError:
    return ReplyProviderError(
        "reply_provider_invalid_response",
        RetryClassification.TERMINAL,
    )


def _normalized_provider_error(error: Exception) -> ReplyProviderError:
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(error, "code", None)
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
    retryable = isinstance(
        error,
        (TimeoutError, ConnectionError, httpx.TimeoutException, httpx.NetworkError),
    ) or (isinstance(status, int) and (status in {408, 429} or status >= 500))
    return ReplyProviderError(
        "reply_provider_unavailable",
        (RetryClassification.RETRYABLE if retryable else RetryClassification.TERMINAL),
    )
