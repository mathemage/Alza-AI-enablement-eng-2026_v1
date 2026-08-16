import asyncio
import html
import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import FrozenInstanceError, dataclass
from typing import cast

import httpx
import pytest
from google.genai import types

import alza_ai.reply_providers as providers
from alza_ai.domain import Attachment, AttachmentInsight, GeneratedReply

PRIVATE_MARKER = "private-provider-marker"
OPENROUTER_KEY = "openrouter-secret-key"
GEMINI_MODEL = "gemini-contract-model"
OPENROUTER_MODEL = "vendor/contract-model"


@dataclass(frozen=True, slots=True)
class FakeGeminiUsage:
    prompt_token_count: object = 11
    candidates_token_count: object = 7
    total_token_count: object = 18


@dataclass(frozen=True, slots=True)
class FakeGeminiResponse:
    text: object = "  Safe reply  "
    usage_metadata: object = FakeGeminiUsage()


class FakeGeminiModels:
    def __init__(
        self,
        *,
        response: FakeGeminiResponse | None = None,
        failure: BaseException | None = None,
    ) -> None:
        self.response = response or FakeGeminiResponse()
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


class RecordingGeminiClientFactory:
    def __init__(self, models: FakeGeminiModels | None = None) -> None:
        self.models = models or FakeGeminiModels()
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> FakeGeminiClient:
        self.calls.append(kwargs)
        return FakeGeminiClient(FakeGeminiAio(self.models))


class FakeOpenRouterClient:
    def __init__(
        self,
        *,
        response_data: object | None = None,
        status_code: int = 200,
        failure: BaseException | None = None,
    ) -> None:
        self.response_data = response_data or {
            "choices": [{"message": {"content": "  Safe reply  "}}],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            },
            "model": "untrusted/response-model",
        }
        self.status_code = status_code
        self.failure = failure
        self.urls: list[str] = []
        self.calls: list[dict[str, object]] = []

    async def post(self, url: str, **kwargs: object) -> httpx.Response:
        self.urls.append(url)
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        request = httpx.Request("POST", url)
        return httpx.Response(
            self.status_code,
            json=self.response_data,
            request=request,
        )


class RecordingOpenRouterClientFactory:
    def __init__(self, client: FakeOpenRouterClient | None = None) -> None:
        self.client = client or FakeOpenRouterClient()
        self.calls = 0

    def __call__(self) -> FakeOpenRouterClient:
        self.calls += 1
        return self.client


class StepClock:
    def __init__(self, *values: float) -> None:
        self._values: Iterator[float] = iter(values)

    def __call__(self) -> float:
        return next(self._values)


class StatusFailure(Exception):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"{PRIVATE_MARKER}:{status_code}")


class TrackingEnvironment(Mapping[str, str]):
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values
        self.accessed: list[str] = []

    def __getitem__(self, key: str) -> str:
        self.accessed.append(key)
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


@dataclass(frozen=True, slots=True)
class ProviderHarness:
    provider: providers.ReplyProvider
    calls: list[dict[str, object]]


def make_insight() -> AttachmentInsight:
    return AttachmentInsight(
        filename="brief.pdf",
        media_type="application/pdf",
        summary="Quarterly brief",
        extracted_text="Revenue grew",
        relevant_facts=("Growth was 12%",),
        warnings=("One table was unreadable",),
    )


def make_provider(
    provider_name: str,
    *,
    text: object = "  Safe reply  ",
    input_tokens: object = 11,
    output_tokens: object = 7,
    total_tokens: object = 18,
    failure: BaseException | None = None,
    clock: Callable[[], float] | None = None,
) -> ProviderHarness:
    selected_clock = clock or StepClock(10.0, 10.125, 10.130)
    provider: providers.ReplyProvider
    if provider_name == "gemini":
        models = FakeGeminiModels(
            response=FakeGeminiResponse(
                text=text,
                usage_metadata=FakeGeminiUsage(
                    prompt_token_count=input_tokens,
                    candidates_token_count=output_tokens,
                    total_token_count=total_tokens,
                ),
            ),
            failure=failure,
        )
        factory = RecordingGeminiClientFactory(models)
        provider = providers.GeminiReplyProvider(
            project_id="test-project",
            model=GEMINI_MODEL,
            client_factory=factory,
            clock=selected_clock,
        )
        return ProviderHarness(provider, models.calls)

    client = FakeOpenRouterClient(
        response_data={
            "choices": [{"message": {"content": text}}],
            "usage": {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": total_tokens,
            },
            "model": "untrusted/response-model",
        },
        failure=failure,
    )
    provider = providers.OpenRouterReplyProvider(
        api_key=OPENROUTER_KEY,
        model=OPENROUTER_MODEL,
        http_client=client,
        clock=selected_clock,
    )
    return ProviderHarness(provider, client.calls)


def run_generation(
    provider: providers.ReplyProvider,
    *,
    current_text: str = "Please summarize the attachment.",
    attachment_insights: tuple[AttachmentInsight, ...] = (),
) -> GeneratedReply:
    return asyncio.run(
        provider.generate(
            current_text=current_text,
            attachment_insights=attachment_insights,
        )
    )


@pytest.mark.parametrize("provider_name", ("gemini", "openrouter"))
def test_port_01_shared_reply_provider_contract_builds_safe_generated_reply(
    provider_name: str,
) -> None:
    harness = make_provider(
        provider_name,
        text="  Hello\r\n<script>alert('x')</script>\rGoodbye  ",
    )

    reply = run_generation(
        harness.provider,
        current_text="Please use the current message only.",
        attachment_insights=(make_insight(),),
    )

    assert isinstance(harness.provider, providers.ReplyProvider)
    assert isinstance(reply, GeneratedReply)
    assert reply.text == "Hello\n<script>alert('x')</script>\nGoodbye"
    assert reply.html == html.escape(reply.text).replace("\n", "<br>")
    assert "<script>" not in reply.html
    assert reply.citations == ()
    assert reply.provider == provider_name
    assert reply.model == (
        GEMINI_MODEL if provider_name == "gemini" else OPENROUTER_MODEL
    )
    assert reply.input_tokens == 11
    assert reply.output_tokens == 7
    assert reply.total_tokens == 18
    assert reply.provider_latency_ms == 125
    assert reply.total_latency_ms == 130
    assert PRIVATE_MARKER not in repr(reply)
    frozen_field = "text"
    with pytest.raises(FrozenInstanceError):
        setattr(reply, frozen_field, "changed")


@pytest.mark.parametrize("provider_name", ("gemini", "openrouter"))
def test_provider_02_shared_contract_bounds_usage_text_html_and_latency(
    provider_name: str,
) -> None:
    harness = make_provider(
        provider_name,
        text="<&\r\n" * 4_000,
        input_tokens=-1,
        output_tokens=1_000_001,
        total_tokens="invalid",
        clock=StepClock(0.0, 7_200.0, 9_000.0),
    )

    reply = run_generation(harness.provider)

    assert 0 < len(reply.text) <= 8_000
    assert len(reply.html) <= 8_000
    assert reply.html == html.escape(reply.text).replace("\n", "<br>")
    assert reply.input_tokens == 0
    assert reply.output_tokens == 1_000_000
    assert reply.total_tokens == 0
    assert reply.provider_latency_ms == 3_600_000
    assert reply.total_latency_ms == 3_600_000


@pytest.mark.parametrize("provider_name", ("gemini", "openrouter"))
def test_provider_02_shared_contract_sends_current_text_and_ordered_insights(
    provider_name: str,
) -> None:
    harness = make_provider(provider_name)
    insights = (
        make_insight(),
        AttachmentInsight(
            filename="recording.wav",
            media_type="audio/wav",
            summary="Customer call",
            extracted_text="Please ship tomorrow",
            relevant_facts=("Priority customer", "Prague delivery"),
            warnings=(),
        ),
    )

    run_generation(
        harness.provider,
        current_text="Can we deliver tomorrow?",
        attachment_insights=insights,
    )

    assert len(harness.calls) == 1
    call = harness.calls[0]
    if provider_name == "gemini":
        payload = json.loads(cast(str, call["contents"]))
    else:
        body = cast(dict[str, object], call["json"])
        messages = cast(list[dict[str, str]], body["messages"])
        payload = json.loads(messages[1]["content"])
    assert payload == {
        "current_email_text": "Can we deliver tomorrow?",
        "attachment_insights": [
            {
                "filename": "brief.pdf",
                "media_type": "application/pdf",
                "summary": "Quarterly brief",
                "extracted_text": "Revenue grew",
                "relevant_facts": ["Growth was 12%"],
                "warnings": ["One table was unreadable"],
            },
            {
                "filename": "recording.wav",
                "media_type": "audio/wav",
                "summary": "Customer call",
                "extracted_text": "Please ship tomorrow",
                "relevant_facts": ["Priority customer", "Prague delivery"],
                "warnings": [],
            },
        ],
    }


@pytest.mark.parametrize("provider_name", ("gemini", "openrouter"))
@pytest.mark.parametrize("text", (" \r\n ", None))
def test_provider_02_shared_contract_rejects_empty_or_malformed_prose(
    provider_name: str,
    text: object,
) -> None:
    harness = make_provider(provider_name, text=text)

    with pytest.raises(providers.ReplyProviderError) as raised:
        run_generation(harness.provider)

    assert raised.value.code == "reply_provider_invalid_response"
    assert raised.value.classification is providers.RetryClassification.TERMINAL
    assert str(raised.value) == "reply_provider_invalid_response:terminal"


@pytest.mark.parametrize("provider_name", ("gemini", "openrouter"))
@pytest.mark.parametrize(
    "failure",
    (
        TimeoutError(PRIVATE_MARKER),
        ConnectionError(PRIVATE_MARKER),
        StatusFailure(408),
        StatusFailure(429),
        StatusFailure(503),
    ),
)
def test_provider_02_shared_contract_classifies_retryable_failures(
    provider_name: str,
    failure: BaseException,
) -> None:
    harness = make_provider(provider_name, failure=failure)

    with pytest.raises(providers.ReplyProviderError) as raised:
        run_generation(harness.provider)

    assert raised.value.code == "reply_provider_unavailable"
    assert raised.value.classification is providers.RetryClassification.RETRYABLE
    assert str(raised.value) == "reply_provider_unavailable:retryable"
    assert PRIVATE_MARKER not in str(raised.value)
    assert len(harness.calls) == 1


@pytest.mark.parametrize("provider_name", ("gemini", "openrouter"))
def test_provider_02_shared_contract_classifies_non_retryable_provider_failure(
    provider_name: str,
) -> None:
    harness = make_provider(provider_name, failure=StatusFailure(400))

    with pytest.raises(providers.ReplyProviderError) as raised:
        run_generation(harness.provider)

    assert raised.value.code == "reply_provider_unavailable"
    assert raised.value.classification is providers.RetryClassification.TERMINAL
    assert str(raised.value) == "reply_provider_unavailable:terminal"
    assert PRIVATE_MARKER not in str(raised.value)


def test_provider_01_default_gemini_needs_no_openrouter_key_or_client() -> None:
    environment = TrackingEnvironment(
        {
            "RESPONSE_PROVIDER": "gemini",
            "GEMINI_MODEL": providers.DEFAULT_GEMINI_MODEL,
            "GOOGLE_CLOUD_PROJECT": "test-project",
        }
    )
    gemini_factory = RecordingGeminiClientFactory()
    openrouter_factory = RecordingOpenRouterClientFactory()

    selected = providers.load_reply_provider(
        environment,
        gemini_client_factory=gemini_factory,
        openrouter_client_factory=openrouter_factory,
    )

    assert isinstance(selected, providers.GeminiReplyProvider)
    assert selected.model == "gemini-3.6-flash"
    assert run_generation(selected).provider == "gemini"
    assert len(gemini_factory.calls) == 1
    assert len(gemini_factory.models.calls) == 1
    assert openrouter_factory.calls == 0
    assert "OPENROUTER_API_KEY" not in environment.accessed
    assert "OPENROUTER_MODEL" not in environment.accessed


def test_provider_01_missing_response_provider_defaults_to_gemini() -> None:
    environment = TrackingEnvironment({"GOOGLE_CLOUD_PROJECT": "test-project"})
    gemini_factory = RecordingGeminiClientFactory()

    selected = providers.load_reply_provider(
        environment,
        gemini_client_factory=gemini_factory,
        openrouter_client_factory=RecordingOpenRouterClientFactory(),
    )

    assert isinstance(selected, providers.GeminiReplyProvider)
    assert selected.model == providers.DEFAULT_GEMINI_MODEL


@pytest.mark.parametrize("key", (None, "", "   "))
def test_provider_01_selected_openrouter_requires_its_own_key(
    key: str | None,
) -> None:
    values = {"RESPONSE_PROVIDER": "openrouter"}
    if key is not None:
        values["OPENROUTER_API_KEY"] = key
    client_factory = RecordingOpenRouterClientFactory()

    with pytest.raises(providers.ReplyConfigurationError) as raised:
        providers.load_reply_provider(
            values,
            gemini_client_factory=RecordingGeminiClientFactory(),
            openrouter_client_factory=client_factory,
        )

    assert raised.value.code == "openrouter_api_key_missing"
    assert str(raised.value) == "openrouter_api_key_missing"
    assert client_factory.calls == 0


@pytest.mark.parametrize(
    ("model_setting", "expected_model"),
    (
        (None, "anthropic/claude-opus-5"),
        ("vendor/custom-model", "vendor/custom-model"),
    ),
)
def test_provider_01_selected_openrouter_uses_default_or_configured_model(
    model_setting: str | None,
    expected_model: str,
) -> None:
    values = {
        "RESPONSE_PROVIDER": "openrouter",
        "OPENROUTER_API_KEY": OPENROUTER_KEY,
    }
    if model_setting is not None:
        values["OPENROUTER_MODEL"] = model_setting
    environment = TrackingEnvironment(values)
    gemini_factory = RecordingGeminiClientFactory()
    openrouter_factory = RecordingOpenRouterClientFactory()

    selected = providers.load_reply_provider(
        environment,
        gemini_client_factory=gemini_factory,
        openrouter_client_factory=openrouter_factory,
    )

    assert isinstance(selected, providers.OpenRouterReplyProvider)
    assert selected.model == expected_model
    assert gemini_factory.calls == []
    assert openrouter_factory.calls == 1
    assert "GEMINI_MODEL" not in environment.accessed
    assert "GOOGLE_CLOUD_PROJECT" not in environment.accessed


@pytest.mark.parametrize(
    ("values", "expected_code"),
    (
        ({"RESPONSE_PROVIDER": ""}, "reply_provider_invalid"),
        ({"RESPONSE_PROVIDER": "other"}, "reply_provider_invalid"),
        (
            {"RESPONSE_PROVIDER": "gemini", "GEMINI_MODEL": " "},
            "reply_provider_model_invalid",
        ),
        (
            {
                "RESPONSE_PROVIDER": "openrouter",
                "OPENROUTER_API_KEY": OPENROUTER_KEY,
                "OPENROUTER_MODEL": " ",
            },
            "reply_provider_model_invalid",
        ),
    ),
)
def test_provider_01_invalid_selected_provider_configuration_is_sanitized(
    values: dict[str, str],
    expected_code: str,
) -> None:
    with pytest.raises(providers.ReplyConfigurationError) as raised:
        providers.load_reply_provider(
            values,
            gemini_client_factory=RecordingGeminiClientFactory(),
            openrouter_client_factory=RecordingOpenRouterClientFactory(),
        )

    assert raised.value.code == expected_code
    assert str(raised.value) == expected_code


def test_provider_02_gemini_uses_global_v1_text_only_generation() -> None:
    models = FakeGeminiModels()
    factory = RecordingGeminiClientFactory(models)
    provider = providers.GeminiReplyProvider(
        project_id="test-project",
        model=GEMINI_MODEL,
        client_factory=factory,
        clock=StepClock(1.0, 1.1, 1.2),
    )

    run_generation(provider, attachment_insights=(make_insight(),))

    assert len(factory.calls) == 1
    client_call = factory.calls[0]
    assert client_call["vertexai"] is True
    assert client_call["project"] == "test-project"
    assert client_call["location"] == "global"
    http_options = cast(types.HttpOptions, client_call["http_options"])
    assert http_options.api_version == "v1"
    assert len(models.calls) == 1
    generation_call = models.calls[0]
    assert generation_call["model"] == GEMINI_MODEL
    assert isinstance(generation_call["contents"], str)
    config = cast(types.GenerateContentConfig, generation_call["config"])
    assert config.system_instruction == providers.REPLY_SYSTEM_INSTRUCTION
    assert config.max_output_tokens == 2_048
    assert config.temperature == 0
    assert config.tools is None


def test_provider_02_openrouter_request_excludes_raw_attachment() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Reply"}}],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 1,
                    "total_tokens": 4,
                },
            },
        )

    attachment = Attachment(
        part_id="gmail-part-id",
        filename="private.pdf",
        media_family="document",
        media_type="application/pdf",
        disposition="attachment",
        content_id=None,
        size=len(PRIVATE_MARKER.encode()),
        data=PRIVATE_MARKER.encode(),
    )
    scratch_uri = "gs://private-bucket/private-object"
    unrelated_gmail_metadata = "gmail-thread-private-marker"
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = providers.OpenRouterReplyProvider(
        api_key=OPENROUTER_KEY,
        model=OPENROUTER_MODEL,
        http_client=client,
        clock=StepClock(1.0, 1.1, 1.2),
    )

    try:
        run_generation(
            provider,
            current_text="Current message",
            attachment_insights=(make_insight(),),
        )
    finally:
        asyncio.run(client.aclose())

    assert attachment.data == PRIVATE_MARKER.encode()
    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST"
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {OPENROUTER_KEY}"
    body = json.loads(request.content)
    assert set(body) == {"model", "messages", "max_tokens", "temperature", "stream"}
    assert body["model"] == OPENROUTER_MODEL
    assert body["max_tokens"] == 2_048
    assert body["temperature"] == 0
    assert body["stream"] is False
    assert body["messages"][0] == {
        "role": "system",
        "content": providers.REPLY_SYSTEM_INSTRUCTION,
    }
    serialized = request.content.decode()
    assert OPENROUTER_KEY not in serialized
    assert PRIVATE_MARKER not in serialized
    assert scratch_uri not in serialized
    assert unrelated_gmail_metadata not in serialized
    assert "tools" not in body
    assert "plugins" not in body
    assert "models" not in body


@pytest.mark.parametrize("selected_provider", ("gemini", "openrouter"))
def test_provider_02_selected_failure_never_constructs_or_calls_fallback(
    selected_provider: str,
) -> None:
    gemini_models = FakeGeminiModels(failure=TimeoutError(PRIVATE_MARKER))
    gemini_factory = RecordingGeminiClientFactory(gemini_models)
    openrouter_client = FakeOpenRouterClient(failure=TimeoutError(PRIVATE_MARKER))
    openrouter_factory = RecordingOpenRouterClientFactory(openrouter_client)
    values = {
        "RESPONSE_PROVIDER": selected_provider,
        "GOOGLE_CLOUD_PROJECT": "test-project",
        "OPENROUTER_API_KEY": OPENROUTER_KEY,
    }
    selected = providers.load_reply_provider(
        values,
        gemini_client_factory=gemini_factory,
        openrouter_client_factory=openrouter_factory,
    )

    with pytest.raises(providers.ReplyProviderError) as raised:
        run_generation(selected)

    assert raised.value.classification is providers.RetryClassification.RETRYABLE
    if selected_provider == "gemini":
        assert len(gemini_factory.calls) == 1
        assert len(gemini_models.calls) == 1
        assert openrouter_factory.calls == 0
        assert openrouter_client.calls == []
    else:
        assert gemini_factory.calls == []
        assert gemini_models.calls == []
        assert openrouter_factory.calls == 1
        assert len(openrouter_client.calls) == 1
