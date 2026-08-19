import asyncio
import json
from collections.abc import Callable, Mapping

import httpx
import pytest

import alza_ai.runtime as runtime_module
from alza_ai.oauth import GMAIL_MODIFY_SCOPE
from alza_ai.processing import SenderPolicy
from alza_ai.runtime import (
    RuntimeConfigurationError,
    RuntimeSettings,
    build_components,
    create_production_app,
)

PROJECT = "test-project"
MAILBOX = "dedicated@example.test"
MAILBOX_KEY = "opaque-mailbox"
SENDER = "allowed@example.test"
REFRESH_TOKEN = "owned-refresh-token"
CLIENT_SECRET = "owned-client-secret"


def runtime_environment(**overrides: str) -> dict[str, str]:
    client = {
        "installed": {
            "client_id": "client-id",
            "client_secret": CLIENT_SECRET,
            "token_uri": "https://oauth2.googleapis.com/token",
        },
        "mailbox": MAILBOX,
        "mailbox_key": MAILBOX_KEY,
        "allowed_senders": [SENDER],
    }
    token = {"refresh_token": REFRESH_TOKEN, "scopes": [GMAIL_MODIFY_SCOPE]}
    environment = {
        "GOOGLE_CLOUD_PROJECT": PROJECT,
        "SCRATCH_BUCKET": "scratch-bucket",
        "GMAIL_OAUTH_CLIENT_JSON": json.dumps(client),
        "GMAIL_REFRESH_TOKEN_JSON": json.dumps(token),
        "RESPONSE_PROVIDER": "gemini",
        "GEMINI_MODEL": "gemini-3.6-flash",
    }
    environment.update(overrides)
    return environment


def test_runtime_13_loads_only_explicit_valid_secret_backed_configuration() -> None:
    settings = RuntimeSettings.load(runtime_environment())

    assert settings.project_id == PROJECT
    assert settings.scratch_bucket == "scratch-bucket"
    assert settings.mailbox == MAILBOX
    assert settings.mailbox_key == MAILBOX_KEY
    assert settings.allowed_senders == (SENDER,)
    assert settings.gmail_topic == f"projects/{PROJECT}/topics/gmail-notifications"
    assert settings.work_topic == f"projects/{PROJECT}/topics/email-work"
    assert settings.credentials.refresh_token == REFRESH_TOKEN
    assert tuple(settings.credentials.scopes or ()) == (GMAIL_MODIFY_SCOPE,)
    assert REFRESH_TOKEN not in repr(settings)
    assert CLIENT_SECRET not in repr(settings)


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"GOOGLE_CLOUD_PROJECT": ""}, "runtime_project_missing"),
        ({"SCRATCH_BUCKET": ""}, "runtime_scratch_bucket_missing"),
        ({"GMAIL_OAUTH_CLIENT_JSON": "{}"}, "runtime_oauth_client_invalid"),
        (
            {
                "GMAIL_REFRESH_TOKEN_JSON": json.dumps(
                    {"refresh_token": REFRESH_TOKEN, "scopes": ["other"]}
                )
            },
            "runtime_oauth_scope_invalid",
        ),
    ],
)
def test_runtime_13_rejects_missing_or_unsafe_configuration(
    overrides: Mapping[str, str], code: str
) -> None:
    with pytest.raises(RuntimeConfigurationError, match=f"^{code}$"):
        RuntimeSettings.load(runtime_environment(**overrides))


def test_runtime_13_builds_the_existing_concrete_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}
    gmail = _FakeGmail()

    class FakeGmailGateway:
        @staticmethod
        def from_credentials(credentials: object) -> object:
            calls["credentials"] = credentials
            return gmail

    def record(name: str, result: object) -> Callable[..., object]:
        def factory(*args: object, **kwargs: object) -> object:
            calls[name] = (args, kwargs)
            return result

        return factory

    firestore_client = object()
    pubsub_client = object()
    processing_store = object()
    synchronization_store = object()
    storage = object()
    model = object()
    analyzer = object()
    provider = object()
    publisher = object()
    coordinator = object()
    synchronizer = object()

    monkeypatch.setattr(runtime_module, "GmailApiGateway", FakeGmailGateway)
    monkeypatch.setattr(
        "alza_ai.runtime.firestore.Client", record("firestore", firestore_client)
    )
    monkeypatch.setattr(
        "alza_ai.runtime.pubsub_v1.PublisherClient",
        record("pubsub_client", pubsub_client),
    )
    monkeypatch.setattr(
        runtime_module, "ProcessingStore", record("processing_store", processing_store)
    )
    monkeypatch.setattr(
        runtime_module,
        "SynchronizationStore",
        record("synchronization_store", synchronization_store),
    )
    monkeypatch.setattr(
        runtime_module, "CloudStorageScratchStorage", record("storage", storage)
    )
    monkeypatch.setattr(runtime_module, "GeminiMultimodalModel", record("model", model))
    monkeypatch.setattr(
        runtime_module, "AttachmentAnalyzer", record("analyzer", analyzer)
    )
    monkeypatch.setattr(
        runtime_module, "load_reply_provider", record("provider", provider)
    )
    monkeypatch.setattr(
        runtime_module, "PubSubWorkPublisher", record("publisher", publisher)
    )
    monkeypatch.setattr(
        runtime_module, "MessageCoordinator", record("coordinator", coordinator)
    )
    monkeypatch.setattr(
        runtime_module, "MailboxSynchronizer", record("synchronizer", synchronizer)
    )

    settings = RuntimeSettings.load(runtime_environment())
    result = build_components(settings)

    assert result.processing_coordinator is coordinator
    assert result.mailbox_synchronizer is synchronizer
    assert gmail.labels == ("AI/Processed", "AI/Error")
    assert calls["firestore"] == ((), {"project": PROJECT})
    assert calls["processing_store"] == ((firestore_client,), {})
    assert calls["synchronization_store"] == ((firestore_client,), {})
    assert calls["storage"] == ((), {"bucket_name": "scratch-bucket"})
    assert calls["model"] == ((), {"project_id": PROJECT, "model": "gemini-3.6-flash"})
    assert calls["analyzer"] == ((storage, model), {})
    assert calls["provider"] == ((settings.environment,), {})
    assert calls["publisher"] == ((pubsub_client,), {"topic": settings.work_topic})
    coordinator_call = calls["coordinator"]
    synchronizer_call = calls["synchronizer"]
    assert isinstance(coordinator_call, tuple)
    assert isinstance(synchronizer_call, tuple)
    coordinator_kwargs = coordinator_call[1]
    synchronizer_kwargs = synchronizer_call[1]
    assert isinstance(coordinator_kwargs, dict)
    assert isinstance(synchronizer_kwargs, dict)
    sender_policy = coordinator_kwargs["sender_policy"]
    assert isinstance(sender_policy, SenderPolicy)
    assert sender_policy.mailbox_address == MAILBOX
    assert synchronizer_kwargs["topic_name"] == settings.gmail_topic


def test_runtime_13_rejects_authenticated_mailbox_mismatch() -> None:
    settings = RuntimeSettings.load(runtime_environment())
    gmail = _FakeGmail(profile="wrong@example.test")

    with pytest.raises(RuntimeConfigurationError, match="^runtime_mailbox_mismatch$"):
        build_components(settings, gmail_factory=lambda _: gmail)


def test_runtime_13_production_app_injects_both_coordinators() -> None:
    components = runtime_module.RuntimeComponents(object(), object())
    app = create_production_app(
        runtime_environment(), component_builder=lambda _: components
    )

    async def exercise() -> tuple[int, bytes, int]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            health = await client.get("/health")
            invalid = await client.post("/events/gmail", json={})
        return health.status_code, health.content, invalid.status_code

    assert asyncio.run(exercise()) == (200, b'{"status":"ok"}', 204)


class _FakeGmail:
    def __init__(self, profile: str = MAILBOX) -> None:
        self.profile = profile
        self.labels: tuple[str, ...] = ()

    def get_profile(self) -> str:
        return self.profile

    def ensure_labels(self, labels: tuple[str, ...]) -> None:
        self.labels = labels
