import asyncio
import html
import ipaddress
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from time import perf_counter
from typing import Protocol, cast, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

import httpx
from google import genai
from google.genai import types

from alza_ai.domain import AttachmentInsight, Citation, GeneratedReply

DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"
DEFAULT_OPENROUTER_MODEL = "anthropic/claude-opus-5"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_OUTPUT_TOKENS = 2_048
MAX_REPLY_CHARACTERS = 8_000
MAX_USAGE_TOKENS = 1_000_000
MAX_LATENCY_MS = 3_600_000
MAX_CITATIONS = 5
MAX_CITATION_URL_CHARACTERS = 2_048
MAX_CITATION_TITLE_CHARACTERS = 200
REQUEST_TIMEOUT_SECONDS = 30.0
UNVERIFIED_CURRENT_REPLY = (
    "I couldn't verify the requested current information with live web search."
)

REPLY_SYSTEM_INSTRUCTION = (
    "Draft one concise, helpful email reply using only the supplied current email "
    "text and attachment insights. Return plain text only. Do not use live search."
)
SEARCH_PERMITTED_SYSTEM_INSTRUCTION = (
    "Draft one concise, helpful email reply using only the supplied current email "
    "text, attachment insights, and the provided live search tool when useful. "
    "Return plain text only. Never claim information is current unless search "
    "grounding supports it."
)
FORCED_CURRENT_SYSTEM_INSTRUCTION = (
    "Draft one concise, helpful email reply using only the supplied current email "
    "text, attachment insights, and the provided live search tool. The request needs "
    "current information, so use search and state no current fact without grounding. "
    "Return plain text only."
)

_FORCED_FRESHNESS_PATTERN = re.compile(
    r"\b(?:latest|today|tonight|tomorrow|yesterday|newest|recent|up-to-date)\b"
    r"|\bas\s+of\b"
)
_CURRENT_PATTERN = re.compile(
    r"\bcurrent(?:ly)?\b(?!\s+(?:message|email|attachment|text|request)\b)"
)
_FORCED_TOPIC_PATTERN = re.compile(
    r"\b(?:price|prices|cost|schedule|timetable|news|weather|forecast|score|scores|"
    r"standings|availability|exchange\s+rate|current\s+events)\b"
)
_OFFICE_HOLDER_PATTERN = re.compile(
    r"\bwho\s+(?:is|are)\s+(?:the\s+)?(?:president|prime\s+minister|chancellor|"
    r"governor|mayor|minister|ceo|chief\s+executive)\b"
)
_STABLE_TASK_PATTERN = re.compile(
    r"\b(?:summari[sz]e|rewrite|proofread|translate|extract|draft|classify)\b"
    r"|\bcurrent\s+(?:message|email|attachment|text|request)\b"
)
_URL_FORBIDDEN_PATTERN = re.compile(r"[\x00-\x20\x7f]")
_DNS_LABEL_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class RetryClassification(StrEnum):
    RETRYABLE = "retryable"
    TERMINAL = "terminal"


class SearchPolicy(StrEnum):
    STABLE = "stable"
    SEARCH_PERMITTED = "search_permitted"
    FORCED_CURRENT = "forced_current"


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
class _RawCitation:
    url: object
    title: object


@dataclass(frozen=True, slots=True)
class _RawReply:
    text: object
    input_tokens: object
    output_tokens: object
    total_tokens: object
    citations: tuple[_RawCitation, ...] = ()
    grounding_attempted: bool = False
    search_entry_point_html: str | None = None


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
        search_policy = classify_search_policy(current_text)
        try:
            raw_reply = await self._generate_raw(payload, search_policy)
        except asyncio.CancelledError:
            raise
        except ReplyProviderError:
            raise
        except Exception as error:  # noqa: BLE001 - sanitize provider boundary
            raise _normalized_provider_error(error) from None
        provider_finished = self._clock()
        citations = _normalize_citations(raw_reply.citations, self.provider)
        reply_value = raw_reply.text
        if not citations and (
            search_policy is SearchPolicy.FORCED_CURRENT
            or raw_reply.grounding_attempted
        ):
            reply_value = UNVERIFIED_CURRENT_REPLY
        reply_text, reply_html, citations = _reply_alternatives(
            reply_value,
            citations,
        )
        finished = self._clock()
        provider_latency = _latency_ms(provider_finished - started)
        total_latency = _latency_ms(finished - started)
        return GeneratedReply(
            text=reply_text,
            html=reply_html,
            citations=citations,
            search_entry_point_html=raw_reply.search_entry_point_html,
            provider=self.provider,
            model=self.model,
            input_tokens=_bounded_token_count(raw_reply.input_tokens),
            output_tokens=_bounded_token_count(raw_reply.output_tokens),
            total_tokens=_bounded_token_count(raw_reply.total_tokens),
            provider_latency_ms=min(provider_latency, total_latency),
            total_latency_ms=total_latency,
        )

    async def _generate_raw(
        self,
        payload: str,
        search_policy: SearchPolicy,
    ) -> _RawReply:
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

    async def _generate_raw(
        self,
        payload: str,
        search_policy: SearchPolicy,
    ) -> _RawReply:
        search_enabled = search_policy is not SearchPolicy.STABLE
        response = await self._client.aio.models.generate_content(
            model=self.model,
            contents=payload,
            config=types.GenerateContentConfig(
                system_instruction=_system_instruction(search_policy),
                max_output_tokens=MAX_OUTPUT_TOKENS,
                temperature=0,
                tools=(
                    [types.Tool(google_search=types.GoogleSearch())]
                    if search_enabled
                    else None
                ),
            ),
        )
        usage = response.usage_metadata
        citations, grounding_attempted, search_entry_point_html = (
            _gemini_grounding(response) if search_enabled else ((), False, None)
        )
        return _RawReply(
            text=response.text,
            input_tokens=getattr(usage, "prompt_token_count", 0),
            output_tokens=getattr(usage, "candidates_token_count", 0),
            total_tokens=getattr(usage, "total_token_count", 0),
            citations=citations,
            grounding_attempted=grounding_attempted,
            search_entry_point_html=search_entry_point_html,
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

    async def _generate_raw(
        self,
        payload: str,
        search_policy: SearchPolicy,
    ) -> _RawReply:
        search_enabled = search_policy is not SearchPolicy.STABLE
        request_body: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _system_instruction(search_policy)},
                {"role": "user", "content": payload},
            ],
            "max_tokens": MAX_OUTPUT_TOKENS,
            "temperature": 0,
            "stream": False,
        }
        if search_enabled:
            request_body["tools"] = [{"type": "openrouter:web_search"}]
        response = await self._http_client.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json=request_body,
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
        citations, grounding_attempted = (
            _openrouter_grounding(message, usage) if search_enabled else ((), False)
        )
        return _RawReply(
            text=text,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            citations=citations,
            grounding_attempted=grounding_attempted,
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


def classify_search_policy(current_text: str) -> SearchPolicy:
    normalized = current_text.casefold()
    if (
        _FORCED_FRESHNESS_PATTERN.search(normalized)
        or _CURRENT_PATTERN.search(normalized)
        or _FORCED_TOPIC_PATTERN.search(normalized)
        or _OFFICE_HOLDER_PATTERN.search(normalized)
    ):
        return SearchPolicy.FORCED_CURRENT
    if _STABLE_TASK_PATTERN.search(normalized):
        return SearchPolicy.STABLE
    return SearchPolicy.SEARCH_PERMITTED


def _system_instruction(search_policy: SearchPolicy) -> str:
    if search_policy is SearchPolicy.STABLE:
        return REPLY_SYSTEM_INSTRUCTION
    if search_policy is SearchPolicy.FORCED_CURRENT:
        return FORCED_CURRENT_SYSTEM_INSTRUCTION
    return SEARCH_PERMITTED_SYSTEM_INSTRUCTION


def _gemini_grounding(
    response: _GeminiResponse,
) -> tuple[tuple[_RawCitation, ...], bool, str | None]:
    candidates = _object_sequence(getattr(response, "candidates", None))
    if not candidates:
        return (), False, None
    metadata = getattr(candidates[0], "grounding_metadata", None)
    if metadata is None:
        return (), False, None

    citations: list[_RawCitation] = []
    for chunk in _object_sequence(getattr(metadata, "grounding_chunks", None)):
        web = getattr(chunk, "web", None)
        if web is not None:
            citations.append(
                _RawCitation(
                    url=getattr(web, "uri", None),
                    title=getattr(web, "title", None),
                )
            )

    entry_point = getattr(metadata, "search_entry_point", None)
    rendered_content = getattr(entry_point, "rendered_content", None)
    search_entry_point_html = (
        rendered_content
        if isinstance(rendered_content, str) and rendered_content.strip()
        else None
    )
    return tuple(citations), True, search_entry_point_html


def _openrouter_grounding(
    message: dict[str, object],
    usage: dict[str, object],
) -> tuple[tuple[_RawCitation, ...], bool]:
    annotations_value = message.get("annotations")
    annotations = _object_sequence(annotations_value)
    grounding_attempted = "annotations" in message

    server_tool_use = usage.get("server_tool_use")
    if isinstance(server_tool_use, dict):
        searches = server_tool_use.get("web_search_requests")
        grounding_attempted = grounding_attempted or (
            isinstance(searches, int)
            and not isinstance(searches, bool)
            and searches > 0
        )

    citations: list[_RawCitation] = []
    for annotation in annotations:
        if not isinstance(annotation, dict) or annotation.get("type") != "url_citation":
            continue
        url_citation = annotation.get("url_citation")
        if not isinstance(url_citation, dict):
            continue
        citations.append(
            _RawCitation(
                url=url_citation.get("url"),
                title=url_citation.get("title"),
            )
        )
    return tuple(citations), grounding_attempted


def _object_sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return cast(Sequence[object], value)
    return ()


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


def _normalize_citations(
    raw_citations: Sequence[_RawCitation],
    provider: str,
) -> tuple[Citation, ...]:
    citations: list[Citation] = []
    seen: set[str] = set()
    for raw_citation in raw_citations:
        url = _canonical_citation_url(raw_citation.url)
        if url is None or url in seen:
            continue
        title = _citation_title(raw_citation.title, url)
        citations.append(Citation(url=url, title=title, provider=provider))
        seen.add(url)
        if len(citations) == MAX_CITATIONS:
            break
    return tuple(citations)


def _canonical_citation_url(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_CITATION_URL_CHARACTERS
        or _URL_FORBIDDEN_PATTERN.search(value)
    ):
        return None
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if (
        scheme not in {"http", "https"}
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None

    canonical_host = _canonical_public_host(hostname)
    if canonical_host is None:
        return None
    if ":" in canonical_host:
        canonical_host = f"[{canonical_host}]"
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        canonical_host = f"{canonical_host}:{port}"

    canonical = urlunsplit(
        (
            scheme,
            canonical_host,
            parsed.path or "/",
            parsed.query,
            "",
        )
    )
    if len(canonical) > MAX_CITATION_URL_CHARACTERS:
        return None
    return canonical


def _canonical_public_host(hostname: str) -> str | None:
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            host = hostname.encode("idna").decode("ascii").lower().rstrip(".")
        except UnicodeError:
            return None
        labels = host.split(".")
        if (
            len(host) > 253
            or len(labels) < 2
            or labels[-1].isdigit()
            or labels[-1] in {"internal", "local", "localhost"}
            or any(_DNS_LABEL_PATTERN.fullmatch(label) is None for label in labels)
        ):
            return None
        return host
    if not address.is_global:
        return None
    return address.compressed


def _citation_title(value: object, url: str) -> str:
    title = " ".join(value.split()) if isinstance(value, str) else ""
    if not title:
        title = urlsplit(url).hostname or url
    return title[:MAX_CITATION_TITLE_CHARACTERS]


def _reply_alternatives(
    value: object,
    citations: Sequence[Citation],
) -> tuple[str, str, tuple[Citation, ...]]:
    if not isinstance(value, str):
        raise _invalid_response()
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise _invalid_response()

    retained = tuple(citations[:MAX_CITATIONS])
    while True:
        text_suffix, html_suffix = _citation_suffixes(retained)
        text, safe_html = _bounded_prose(
            normalized,
            MAX_REPLY_CHARACTERS - len(text_suffix),
            MAX_REPLY_CHARACTERS - len(html_suffix),
        )
        if text:
            return text + text_suffix, safe_html + html_suffix, retained
        if not retained:
            raise _invalid_response()
        retained = retained[:-1]


def _citation_suffixes(citations: Sequence[Citation]) -> tuple[str, str]:
    if not citations:
        return "", ""
    text_lines = ["\n\nSources:"]
    html_lines = ["<br><br>Sources:"]
    for index, citation in enumerate(citations, start=1):
        text_lines.append(f"[{index}] {citation.title}: {citation.url}")
        html_lines.append(
            f'<a href="{html.escape(citation.url, quote=True)}">'
            f"[{index}] {html.escape(citation.title)}</a>"
        )
    return "\n".join(text_lines), "<br>".join(html_lines)


def _bounded_prose(
    normalized: str,
    text_limit: int,
    html_limit: int,
) -> tuple[str, str]:
    text_characters: list[str] = []
    html_fragments: list[str] = []
    html_length = 0
    for character in normalized:
        escaped = "<br>" if character == "\n" else html.escape(character)
        if (
            len(text_characters) == text_limit
            or html_length + len(escaped) > html_limit
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
