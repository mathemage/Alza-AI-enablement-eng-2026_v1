import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import Protocol, cast

import google.cloud.firestore as firestore  # noqa: PLR0402
import google.cloud.pubsub_v1 as pubsub_v1  # type: ignore[import-untyped]  # noqa: PLR0402
from fastapi import FastAPI
from google.oauth2.credentials import Credentials

from alza_ai.attachments import (
    AttachmentAnalyzer,
    CloudStorageScratchStorage,
    GeminiMultimodalModel,
)
from alza_ai.gmail import GmailApiGateway
from alza_ai.main import create_app
from alza_ai.mime import parse_inbound_email
from alza_ai.oauth import GMAIL_MODIFY_SCOPE
from alza_ai.processing import MessageCoordinator, ProcessingStore, SenderPolicy
from alza_ai.quotas import QuotaConfigurationError, RuntimeQuotas
from alza_ai.reply_providers import load_reply_provider
from alza_ai.synchronization import (
    MailboxSynchronizer,
    PubSubWorkPublisher,
    SynchronizationStore,
)


class RuntimeConfigurationError(Exception):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class RuntimeSettings:
    project_id: str
    scratch_bucket: str
    mailbox: str
    mailbox_key: str
    allowed_senders: tuple[str, ...]
    gmail_topic: str
    work_topic: str
    quotas: RuntimeQuotas
    credentials: Credentials = field(repr=False)
    environment: Mapping[str, str] = field(repr=False)

    @classmethod
    def load(cls, environ: Mapping[str, str] | None = None) -> RuntimeSettings:
        source = os.environ if environ is None else environ
        project_id = _required_setting(
            source, "GOOGLE_CLOUD_PROJECT", "runtime_project_missing"
        )
        scratch_bucket = _required_setting(
            source, "SCRATCH_BUCKET", "runtime_scratch_bucket_missing"
        )
        try:
            quotas = RuntimeQuotas.load(source)
        except QuotaConfigurationError as error:
            raise RuntimeConfigurationError(error.code) from None
        client_document = _json_object(
            source.get("GMAIL_OAUTH_CLIENT_JSON"), "runtime_oauth_client_invalid"
        )
        token_document = _json_object(
            source.get("GMAIL_REFRESH_TOKEN_JSON"), "runtime_oauth_token_invalid"
        )
        client = client_document.get("installed", client_document.get("web"))
        if not isinstance(client, dict):
            raise RuntimeConfigurationError("runtime_oauth_client_invalid")
        mailbox = _document_string(
            client_document, "mailbox", "runtime_oauth_client_invalid"
        )
        mailbox_key = _document_string(
            client_document, "mailbox_key", "runtime_oauth_client_invalid"
        )
        allowed_value = client_document.get("allowed_senders")
        if not isinstance(allowed_value, list) or not all(
            isinstance(item, str) and item.strip() for item in allowed_value
        ):
            raise RuntimeConfigurationError("runtime_sender_policy_invalid")
        allowed_senders = tuple(cast(str, item).strip() for item in allowed_value)
        if not allowed_senders:
            raise RuntimeConfigurationError("runtime_sender_policy_invalid")

        scopes = token_document.get("scopes")
        if scopes != [GMAIL_MODIFY_SCOPE]:
            raise RuntimeConfigurationError("runtime_oauth_scope_invalid")
        refresh_token = _document_string(
            token_document, "refresh_token", "runtime_oauth_token_invalid"
        )
        try:
            credentials = Credentials(  # type: ignore[no-untyped-call]
                token=None,
                refresh_token=refresh_token,
                token_uri=_document_string(
                    client, "token_uri", "runtime_oauth_client_invalid"
                ),
                client_id=_document_string(
                    client, "client_id", "runtime_oauth_client_invalid"
                ),
                client_secret=_document_string(
                    client, "client_secret", "runtime_oauth_client_invalid"
                ),
                scopes=[GMAIL_MODIFY_SCOPE],
            )
        except RuntimeConfigurationError:
            raise
        except Exception:  # noqa: BLE001 - never expose credential values
            raise RuntimeConfigurationError("runtime_oauth_client_invalid") from None

        provider_environment = {
            "GOOGLE_CLOUD_PROJECT": project_id,
            "RESPONSE_PROVIDER": source.get("RESPONSE_PROVIDER", "gemini"),
            "GEMINI_MODEL": source.get("GEMINI_MODEL", "gemini-3.6-flash"),
        }
        if openrouter_key := source.get("OPENROUTER_API_KEY"):
            provider_environment["OPENROUTER_API_KEY"] = openrouter_key
        if openrouter_model := source.get("OPENROUTER_MODEL"):
            provider_environment["OPENROUTER_MODEL"] = openrouter_model
        return cls(
            project_id=project_id,
            scratch_bucket=scratch_bucket,
            mailbox=mailbox,
            mailbox_key=mailbox_key,
            allowed_senders=allowed_senders,
            gmail_topic=f"projects/{project_id}/topics/gmail-notifications",
            work_topic=f"projects/{project_id}/topics/email-work",
            quotas=quotas,
            credentials=credentials,
            environment=provider_environment,
        )


@dataclass(frozen=True, slots=True)
class RuntimeComponents:
    processing_coordinator: object
    mailbox_synchronizer: object


class _RuntimeGmail(Protocol):
    def get_profile(self) -> str: ...

    def ensure_labels(self, labels: tuple[str, ...]) -> None: ...


GmailFactory = Callable[[object], object]
ComponentBuilder = Callable[[RuntimeSettings], RuntimeComponents]


def build_components(
    settings: RuntimeSettings,
    *,
    gmail_factory: GmailFactory | None = None,
) -> RuntimeComponents:
    try:
        factory = gmail_factory or GmailApiGateway.from_credentials
        gmail = cast(_RuntimeGmail, factory(settings.credentials))
        if gmail.get_profile().casefold() != settings.mailbox.casefold():
            raise RuntimeConfigurationError("runtime_mailbox_mismatch")
        gmail.ensure_labels(("AI/Processed", "AI/Error"))
        firestore_client = firestore.Client(project=settings.project_id)
        processing_store = ProcessingStore(firestore_client)
        synchronization_store = SynchronizationStore(firestore_client)
        storage = CloudStorageScratchStorage(bucket_name=settings.scratch_bucket)
        model = GeminiMultimodalModel(
            project_id=settings.project_id,
            model=settings.environment["GEMINI_MODEL"],
        )
        analyzer = AttachmentAnalyzer(storage, model)
        provider = load_reply_provider(settings.environment, quotas=settings.quotas)
        publisher_client = pubsub_v1.PublisherClient()
        publisher = PubSubWorkPublisher(publisher_client, topic=settings.work_topic)
        sender_policy = SenderPolicy(settings.mailbox, settings.allowed_senders)
        coordinator = MessageCoordinator(
            store=processing_store,
            gmail=gmail,
            analyzer=analyzer,
            provider=provider,
            parser=partial(
                parse_inbound_email,
                max_attachments=settings.quotas.attachment_analysis_calls,
            ),
            sender_policy=sender_policy,
        )
        synchronizer = MailboxSynchronizer(
            mailbox_key=settings.mailbox_key,
            mailbox_address=settings.mailbox,
            topic_name=settings.gmail_topic,
            store=synchronization_store,
            gmail=gmail,
            publisher=publisher,
        )
    except RuntimeConfigurationError:
        raise
    except Exception:  # noqa: BLE001 - startup errors must remain sanitized
        raise RuntimeConfigurationError("runtime_dependency_unavailable") from None
    return RuntimeComponents(coordinator, synchronizer)


def create_production_app(
    environ: Mapping[str, str] | None = None,
    *,
    component_builder: ComponentBuilder = build_components,
) -> FastAPI:
    settings = RuntimeSettings.load(environ)
    components = component_builder(settings)
    return create_app(
        processing_coordinator=components.processing_coordinator,
        mailbox_synchronizer=components.mailbox_synchronizer,
    )


def _required_setting(source: Mapping[str, str], key: str, code: str) -> str:
    value = source.get(key, "").strip()
    if not value:
        raise RuntimeConfigurationError(code)
    return value


def _json_object(value: str | None, code: str) -> Mapping[str, object]:
    if value is None:
        raise RuntimeConfigurationError(code)
    try:
        document = json.loads(value)
    except ValueError:
        raise RuntimeConfigurationError(code) from None
    if not isinstance(document, dict):
        raise RuntimeConfigurationError(code)
    return document


def _document_string(document: Mapping[str, object], key: str, code: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeConfigurationError(code)
    return value.strip()
