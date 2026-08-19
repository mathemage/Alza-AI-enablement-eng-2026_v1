from collections.abc import Callable
from typing import cast

import pytest
from google.genai import types

import alza_ai.reply_providers as providers
import alza_ai.runtime as runtime_module
from alza_ai.domain import InboundEmail
from alza_ai.mime import MimeParseError
from alza_ai.runtime import RuntimeConfigurationError, RuntimeSettings, build_components
from tests.test_mime import MAILBOX_KEY, external_attachment_message, synthetic_file
from tests.test_reply_providers import (
    RecordingGeminiClientFactory,
    RecordingOpenRouterClientFactory,
    run_generation,
)
from tests.test_runtime import MAILBOX, runtime_environment

FORCED_CURRENT_TEXT = "What is the latest price today?"
UNVERIFIED_REPLY = (
    "I couldn't verify the requested current information with live web search."
)
PATCHED_COMPONENTS = (
    "ProcessingStore",
    "SynchronizationStore",
    "CloudStorageScratchStorage",
    "GeminiMultimodalModel",
    "AttachmentAnalyzer",
    "load_reply_provider",
    "PubSubWorkPublisher",
    "MessageCoordinator",
    "MailboxSynchronizer",
)


class _FakeGmail:
    def get_profile(self) -> str:
        return MAILBOX

    def ensure_labels(self, labels: tuple[str, ...]) -> None:
        return None


def composed_arguments(
    monkeypatch: pytest.MonkeyPatch,
    **overrides: str,
) -> dict[str, dict[str, object]]:
    captured: dict[str, dict[str, object]] = {}

    def record(name: str) -> Callable[..., object]:
        def factory(*args: object, **kwargs: object) -> object:
            captured[name] = kwargs
            return object()

        return factory

    for name in PATCHED_COMPONENTS:
        monkeypatch.setattr(runtime_module, name, record(name))
    monkeypatch.setattr("alza_ai.runtime.firestore.Client", record("firestore"))
    monkeypatch.setattr(
        "alza_ai.runtime.pubsub_v1.PublisherClient", record("pubsub_client")
    )

    settings = RuntimeSettings.load(runtime_environment(**overrides))
    build_components(settings, gmail_factory=lambda _: _FakeGmail())
    return captured


def gemini_provider(
    **overrides: str,
) -> tuple[
    providers.ReplyProvider,
    RecordingGeminiClientFactory,
]:
    factory = RecordingGeminiClientFactory()
    values = {
        "RESPONSE_PROVIDER": "gemini",
        "GOOGLE_CLOUD_PROJECT": "test-project",
        **overrides,
    }
    selected = providers.load_reply_provider(
        values,
        gemini_client_factory=factory,
        openrouter_client_factory=RecordingOpenRouterClientFactory(),
    )
    return selected, factory


def generate_config(
    factory: RecordingGeminiClientFactory,
) -> types.GenerateContentConfig:
    return cast(types.GenerateContentConfig, factory.models.calls[0]["config"])


def test_quota_01_lowered_attachment_ceiling_rejects_the_extra_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = composed_arguments(monkeypatch, MAX_ATTACHMENT_ANALYSIS_CALLS="3")

    assert "parser" in arguments["MessageCoordinator"]
    parser = cast(
        Callable[..., InboundEmail], arguments["MessageCoordinator"]["parser"]
    )

    accepted, accepted_external = external_attachment_message(
        [synthetic_file("pdf") for _ in range(3)]
    )
    assert len(parser(MAILBOX_KEY, accepted, accepted_external).attachments) == 3

    rejected, rejected_external = external_attachment_message(
        [synthetic_file("pdf") for _ in range(4)]
    )
    with pytest.raises(MimeParseError) as raised:
        parser(MAILBOX_KEY, rejected, rejected_external)

    assert raised.value.code == "mime_too_many_attachments"


def test_quota_01_absent_variables_keep_the_design_maximum_attachment_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = composed_arguments(monkeypatch)

    assert "parser" in arguments["MessageCoordinator"]
    parser = cast(
        Callable[..., InboundEmail], arguments["MessageCoordinator"]["parser"]
    )

    accepted, accepted_external = external_attachment_message(
        [synthetic_file("pdf") for _ in range(5)]
    )
    assert len(parser(MAILBOX_KEY, accepted, accepted_external).attachments) == 5

    rejected, rejected_external = external_attachment_message(
        [synthetic_file("pdf") for _ in range(6)]
    )
    with pytest.raises(MimeParseError) as raised:
        parser(MAILBOX_KEY, rejected, rejected_external)

    assert raised.value.code == "mime_too_many_attachments"


def test_quota_02_lowered_output_ceiling_reaches_the_gemini_request() -> None:
    selected, factory = gemini_provider(MAX_REPLY_OUTPUT_TOKENS="256")

    run_generation(selected)

    assert generate_config(factory).max_output_tokens == 256


def test_quota_02_lowered_output_ceiling_reaches_the_openrouter_request() -> None:
    client_factory = RecordingOpenRouterClientFactory()
    selected = providers.load_reply_provider(
        {
            "RESPONSE_PROVIDER": "openrouter",
            "OPENROUTER_API_KEY": "openrouter-secret-key",
            "MAX_REPLY_OUTPUT_TOKENS": "256",
        },
        gemini_client_factory=RecordingGeminiClientFactory(),
        openrouter_client_factory=client_factory,
    )

    run_generation(selected)

    body = cast(dict[str, object], client_factory.client.calls[0]["json"])
    assert body["max_tokens"] == 256


def test_quota_03_zero_search_ceiling_sends_no_tool_and_claims_nothing() -> None:
    selected, factory = gemini_provider(MAX_SEARCH_CALLS="0")

    reply = run_generation(selected, current_text=FORCED_CURRENT_TEXT)

    assert generate_config(factory).tools is None
    assert reply.citations == ()
    assert reply.text == UNVERIFIED_REPLY


def test_quota_04_zero_generation_ceiling_calls_no_model() -> None:
    selected, factory = gemini_provider(MAX_REPLY_GENERATION_CALLS="0")

    with pytest.raises(providers.ReplyProviderError) as raised:
        run_generation(selected)

    assert raised.value.code == "reply_generation_quota_exhausted"
    assert raised.value.classification is providers.RetryClassification.TERMINAL
    assert factory.models.calls == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"MAX_ATTACHMENT_ANALYSIS_CALLS": "0"},
        {"MAX_ATTACHMENT_ANALYSIS_CALLS": "6"},
        {"MAX_ATTACHMENT_ANALYSIS_CALLS": "three"},
        {"MAX_REPLY_GENERATION_CALLS": "-1"},
        {"MAX_REPLY_GENERATION_CALLS": "2"},
        {"MAX_SEARCH_CALLS": "2"},
        {"MAX_REPLY_OUTPUT_TOKENS": "0"},
        {"MAX_REPLY_OUTPUT_TOKENS": "2049"},
        {"MAX_REPLY_OUTPUT_TOKENS": " "},
    ],
)
def test_quota_05_unusable_ceilings_fail_startup(overrides: dict[str, str]) -> None:
    with pytest.raises(RuntimeConfigurationError, match="^runtime_quota_invalid$"):
        RuntimeSettings.load(runtime_environment(**overrides))
