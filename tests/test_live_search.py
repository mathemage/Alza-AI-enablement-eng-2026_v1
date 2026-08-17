import asyncio
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import cast

import httpx
import pytest
from google.genai import types

import alza_ai.reply_providers as providers
from alza_ai.domain import GeneratedReply

GEMINI_MODEL = "gemini-search-model"
OPENROUTER_MODEL = "vendor/search-model"
UNVERIFIED_REPLY = (
    "I couldn't verify the requested current information with live web search."
)


@dataclass(frozen=True, slots=True)
class FakeGeminiUsage:
    prompt_token_count: int = 5
    candidates_token_count: int = 3
    total_token_count: int = 8


@dataclass(frozen=True, slots=True)
class FakeGeminiWeb:
    uri: object
    title: object


@dataclass(frozen=True, slots=True)
class FakeGeminiChunk:
    web: object


@dataclass(frozen=True, slots=True)
class FakeGeminiSearchEntryPoint:
    rendered_content: object


@dataclass(frozen=True, slots=True)
class FakeGeminiGroundingMetadata:
    grounding_chunks: object
    search_entry_point: object = None
    web_search_queries: object = ("provider query",)


@dataclass(frozen=True, slots=True)
class FakeGeminiCandidate:
    grounding_metadata: object


@dataclass(frozen=True, slots=True)
class FakeGeminiResponse:
    text: object
    usage_metadata: object
    candidates: object


class FakeGeminiModels:
    def __init__(
        self,
        response: FakeGeminiResponse,
        failure: BaseException | None,
    ) -> None:
        self.response = response
        self.failure = failure
        self.calls: list[dict[str, object]] = []

    async def generate_content(self, **kwargs: object) -> FakeGeminiResponse:
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        return self.response


@dataclass(frozen=True, slots=True)
class FakeGeminiAio:
    models: FakeGeminiModels


@dataclass(frozen=True, slots=True)
class FakeGeminiClient:
    aio: FakeGeminiAio


class FakeGeminiClientFactory:
    def __init__(self, models: FakeGeminiModels) -> None:
        self.models = models

    def __call__(self, **kwargs: object) -> FakeGeminiClient:
        return FakeGeminiClient(FakeGeminiAio(self.models))


class FakeOpenRouterClient:
    def __init__(
        self,
        response_data: object,
        failure: BaseException | None,
    ) -> None:
        self.response_data = response_data
        self.failure = failure
        self.calls: list[dict[str, object]] = []

    async def post(self, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        return httpx.Response(
            200,
            json=self.response_data,
            request=httpx.Request("POST", url),
        )


class StepClock:
    def __init__(self) -> None:
        self._values: Iterator[float] = iter((1.0, 1.1, 1.2))

    def __call__(self) -> float:
        return next(self._values)


@dataclass(frozen=True, slots=True)
class SearchHarness:
    provider: providers.ReplyProvider
    calls: list[dict[str, object]]


def make_harness(
    provider_name: str,
    *,
    text: str = "Grounded answer",
    sources: Sequence[tuple[object, object]] = (),
    metadata_present: bool = False,
    entry_point_html: object = None,
    failure: BaseException | None = None,
) -> SearchHarness:
    provider: providers.ReplyProvider
    if provider_name == "gemini":
        metadata: object = None
        if metadata_present:
            metadata = FakeGeminiGroundingMetadata(
                grounding_chunks=tuple(
                    FakeGeminiChunk(FakeGeminiWeb(url, title)) for url, title in sources
                ),
                search_entry_point=(
                    FakeGeminiSearchEntryPoint(entry_point_html)
                    if entry_point_html is not None
                    else None
                ),
            )
        response = FakeGeminiResponse(
            text=text,
            usage_metadata=FakeGeminiUsage(),
            candidates=(FakeGeminiCandidate(metadata),),
        )
        models = FakeGeminiModels(response, failure)
        provider = providers.GeminiReplyProvider(
            project_id="test-project",
            model=GEMINI_MODEL,
            client_factory=FakeGeminiClientFactory(models),
            clock=StepClock(),
        )
        return SearchHarness(provider, models.calls)

    annotations: list[dict[str, object]] = []
    if metadata_present:
        annotations = [
            {
                "type": "url_citation",
                "url_citation": {"url": url, "title": title},
            }
            for url, title in sources
        ]
    response_data = {
        "choices": [
            {
                "message": {
                    "content": text,
                    **({"annotations": annotations} if metadata_present else {}),
                }
            }
        ],
        "usage": {
            "prompt_tokens": 5,
            "completion_tokens": 3,
            "total_tokens": 8,
            **(
                {"server_tool_use": {"web_search_requests": 1}}
                if metadata_present
                else {}
            ),
        },
    }
    client = FakeOpenRouterClient(response_data, failure)
    provider = providers.OpenRouterReplyProvider(
        api_key="test-openrouter-key",
        model=OPENROUTER_MODEL,
        http_client=client,
        clock=StepClock(),
    )
    return SearchHarness(provider, client.calls)


def generate(harness: SearchHarness, current_text: str) -> GeneratedReply:
    return asyncio.run(
        harness.provider.generate(
            current_text=current_text,
            attachment_insights=(),
        )
    )


def policy_value(current_text: str) -> str:
    return providers.classify_search_policy(current_text).value


def assert_search_tool(provider_name: str, call: dict[str, object]) -> None:
    if provider_name == "gemini":
        config = cast(types.GenerateContentConfig, call["config"])
        assert config.tools is not None
        assert len(config.tools) == 1
        tool = cast(types.Tool, config.tools[0])
        assert tool.google_search is not None
        return

    body = cast(dict[str, object], call["json"])
    assert body["tools"] == [{"type": "openrouter:web_search"}]
    assert "plugins" not in body
    assert not cast(str, body["model"]).endswith(":online")


def assert_no_search_tool(provider_name: str, call: dict[str, object]) -> None:
    if provider_name == "gemini":
        config = cast(types.GenerateContentConfig, call["config"])
        assert config.tools is None
        return

    body = cast(dict[str, object], call["json"])
    assert "tools" not in body
    assert "plugins" not in body


@pytest.mark.parametrize(
    ("current_text", "expected"),
    (
        ("Summarize the supplied attachment.", "stable"),
        ("Would a standing desk suit my office?", "search_permitted"),
        ("What is the current price of Bitcoin?", "forced_current"),
        ("What is tomorrow's train schedule?", "forced_current"),
        ("Summarize today's technology news.", "forced_current"),
        ("Who is the president of France?", "forced_current"),
    ),
)
def test_search_01_classifies_stable_ordinary_and_forced_current_queries(
    current_text: str,
    expected: str,
) -> None:
    assert policy_value(current_text) == expected


@pytest.mark.parametrize("provider_name", ("gemini", "openrouter"))
def test_search_01_stable_query_keeps_native_search_disabled(
    provider_name: str,
) -> None:
    harness = make_harness(provider_name)

    generate(harness, "Summarize the supplied attachment.")

    assert len(harness.calls) == 1
    assert_no_search_tool(provider_name, harness.calls[0])


@pytest.mark.parametrize("provider_name", ("gemini", "openrouter"))
@pytest.mark.parametrize(
    "current_text",
    (
        "Would a standing desk suit my office?",
        "What is the current price of Bitcoin?",
    ),
)
def test_search_01_ordinary_and_forced_queries_enable_only_native_tool(
    provider_name: str,
    current_text: str,
) -> None:
    harness = make_harness(provider_name)

    generate(harness, current_text)

    assert len(harness.calls) == 1
    assert_search_tool(provider_name, harness.calls[0])


SAFE_AND_UNSAFE_SOURCES: tuple[tuple[object, object], ...] = (
    ("HTTPS://BÜCHER.de:443#first", " <b>Primary</b> "),
    ("https://xn--bcher-kva.de/#duplicate", "Duplicate"),
    ("javascript:alert(1)", "Script"),
    ("https://user:pass@example.com/private", "Credentials"),
    ("http://127.0.0.1/internal", "Loopback"),
    ("https://bad host.example/path", "Whitespace"),
    ("https://localhost/internal", "Single-label host"),
    ("https://example.com:99999/path", "Invalid port"),
    ("https://[::1]/internal", "IPv6 loopback"),
    ("https://example.com/" + "x" * 2_100, "Too long"),
    ("https://www.python.org/a", "X" * 220),
    ("https://docs.python.org/b", "Documentation"),
    ("https://openai.com/c", "OpenAI"),
    ("https://www.google.com/d", "Google"),
    ("https://www.alza.cz/e", "Over the five-citation limit"),
)


@pytest.mark.parametrize("provider_name", ("gemini", "openrouter"))
def test_cite_01_normalizes_deduplicates_rejects_and_renders_five_citations(
    provider_name: str,
) -> None:
    harness = make_harness(
        provider_name,
        sources=SAFE_AND_UNSAFE_SOURCES,
        metadata_present=True,
    )

    reply = generate(harness, "What is the current price of Bitcoin?")

    assert len(harness.calls) == 1
    assert [citation.url for citation in reply.citations] == [
        "https://xn--bcher-kva.de/",
        "https://www.python.org/a",
        "https://docs.python.org/b",
        "https://openai.com/c",
        "https://www.google.com/d",
    ]
    assert [citation.provider for citation in reply.citations] == [provider_name] * 5
    assert reply.citations[0].title == "<b>Primary</b>"
    assert reply.citations[1].title == "X" * 200
    assert "\n\nSources:\n[1] <b>Primary</b>: https://xn--bcher-kva.de/" in reply.text
    assert (
        '<br><br>Sources:<br><a href="https://xn--bcher-kva.de/">'
        "[1] &lt;b&gt;Primary&lt;/b&gt;</a>"
    ) in reply.html
    assert "<b>Primary</b>" not in reply.html
    assert "javascript:" not in reply.text
    assert "user:pass" not in reply.html
    assert "127.0.0.1" not in reply.text


def test_cite_01_preserves_gemini_search_entry_point_separately() -> None:
    entry_point = '<style>.chip{color:blue}</style><a class="chip">Search</a>'
    harness = make_harness(
        "gemini",
        sources=(("https://www.google.com/search?q=test", "Google Search"),),
        metadata_present=True,
        entry_point_html=entry_point,
    )

    reply = generate(harness, "What is today's technology news?")

    assert reply.search_entry_point_html == entry_point
    assert entry_point not in reply.html
    assert len(harness.calls) == 1


@pytest.mark.parametrize("provider_name", ("gemini", "openrouter"))
def test_cite_02_forced_current_without_grounding_discards_provider_claim(
    provider_name: str,
) -> None:
    provider_claim = "The current price is definitely 123."
    harness = make_harness(provider_name, text=provider_claim)

    reply = generate(harness, "What is the current price of Bitcoin?")

    assert reply.text == UNVERIFIED_REPLY
    assert reply.html == UNVERIFIED_REPLY.replace("'", "&#x27;")
    assert reply.citations == ()
    assert provider_claim not in reply.text
    assert len(harness.calls) == 1


@pytest.mark.parametrize("provider_name", ("gemini", "openrouter"))
def test_search_01_ordinary_query_may_remain_ungrounded_when_search_not_attempted(
    provider_name: str,
) -> None:
    harness = make_harness(provider_name, text="An ordinary answer")

    reply = generate(harness, "Would a standing desk suit my office?")

    assert reply.text == "An ordinary answer"
    assert reply.citations == ()
    assert len(harness.calls) == 1
    assert_search_tool(provider_name, harness.calls[0])


@pytest.mark.parametrize("provider_name", ("gemini", "openrouter"))
def test_cite_02_failed_grounding_makes_no_second_response_call(
    provider_name: str,
) -> None:
    harness = make_harness(
        provider_name,
        failure=TimeoutError("private-grounding-failure"),
    )

    with pytest.raises(providers.ReplyProviderError) as raised:
        generate(harness, "What is the current price of Bitcoin?")

    assert raised.value.classification is providers.RetryClassification.RETRYABLE
    assert str(raised.value) == "reply_provider_unavailable:retryable"
    assert len(harness.calls) == 1
    assert_search_tool(provider_name, harness.calls[0])
