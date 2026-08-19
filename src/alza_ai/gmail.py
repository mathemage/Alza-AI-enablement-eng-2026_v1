import base64
import binascii
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP
from typing import Protocol, cast, runtime_checkable

from google.auth.exceptions import TransportError
from googleapiclient.discovery import build  # type: ignore[import-untyped]
from googleapiclient.errors import HttpError  # type: ignore[import-untyped]


@dataclass(frozen=True, slots=True)
class WatchState:
    history_id: str
    expiration_ms: int


@dataclass(frozen=True, slots=True)
class GmailMessageRef:
    message_id: str
    thread_id: str


@dataclass(frozen=True, slots=True)
class HistoryPage:
    records: tuple[Mapping[str, object], ...]
    history_id: str
    next_page_token: str | None


@dataclass(frozen=True, slots=True)
class MessagePage:
    messages: tuple[GmailMessageRef, ...]
    next_page_token: str | None


@dataclass(frozen=True, slots=True)
class GmailMessageMetadata:
    message_id: str
    thread_id: str
    internal_date_ms: int
    label_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ThreadMessage:
    message_id: str
    rfc_message_id: str | None
    source_message_id: str | None


@dataclass(frozen=True, slots=True)
class ThreadSnapshot:
    thread_id: str
    messages: tuple[ThreadMessage, ...]


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    thread_id: str
    raw: bytes


@dataclass(frozen=True, slots=True)
class SentMessage:
    message_id: str
    thread_id: str


class GmailGatewayError(Exception):
    def __init__(self, code: str, status: int | None = None) -> None:
        self.code = code
        self.status = status
        message = code if status is None else f"{code} (status={status})"
        super().__init__(message)


class GmailRetryableError(GmailGatewayError):
    pass


class GmailTerminalError(GmailGatewayError):
    pass


class GmailAmbiguousSendError(GmailGatewayError):
    pass


@runtime_checkable
class GmailGateway(Protocol):
    def start_watch(self, topic_name: str) -> WatchState: ...

    def stop_watch(self) -> None: ...

    def list_history(
        self, start_history_id: str, page_token: str | None = None
    ) -> HistoryPage: ...

    def list_unread(self, page_token: str | None = None) -> MessagePage: ...

    def get_message_metadata(self, message_id: str) -> GmailMessageMetadata: ...

    def get_message(self, message_id: str) -> Mapping[str, object]: ...

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes: ...

    def modify_labels(
        self,
        message_id: str,
        *,
        add: tuple[str, ...] = (),
        remove: tuple[str, ...] = (),
    ) -> None: ...

    def inspect_thread(self, thread_id: str) -> ThreadSnapshot: ...

    def send_message(self, message: OutboundMessage) -> SentMessage: ...


class _Request(Protocol):
    def execute(self) -> Mapping[str, object]: ...


class _AttachmentsResource(Protocol):
    def get(self, **kwargs: object) -> _Request: ...


class _MessagesResource(Protocol):
    def list(self, **kwargs: object) -> _Request: ...

    def get(self, **kwargs: object) -> _Request: ...

    def modify(self, **kwargs: object) -> _Request: ...

    def send(self, **kwargs: object) -> _Request: ...

    def attachments(self) -> _AttachmentsResource: ...


class _HistoryResource(Protocol):
    def list(self, **kwargs: object) -> _Request: ...


class _ThreadsResource(Protocol):
    def get(self, **kwargs: object) -> _Request: ...


class _LabelsResource(Protocol):
    def list(self, **kwargs: object) -> _Request: ...

    def create(self, **kwargs: object) -> _Request: ...


class _UsersResource(Protocol):
    def getProfile(self, **kwargs: object) -> _Request: ...

    def watch(self, **kwargs: object) -> _Request: ...

    def stop(self, **kwargs: object) -> _Request: ...

    def history(self) -> _HistoryResource: ...

    def messages(self) -> _MessagesResource: ...

    def threads(self) -> _ThreadsResource: ...

    def labels(self) -> _LabelsResource: ...


class _GmailService(Protocol):
    def users(self) -> _UsersResource: ...


class GmailApiGateway:
    def __init__(self, service: object) -> None:
        self._service = cast(_GmailService, service)
        self._label_ids: dict[str, str] = {}

    @classmethod
    def from_credentials(cls, credentials: object) -> GmailApiGateway:
        service = build(
            "gmail",
            "v1",
            credentials=credentials,
            cache_discovery=False,
        )
        return cls(service)

    def start_watch(self, topic_name: str) -> WatchState:
        response = self._execute(
            self._service.users().watch(
                userId="me",
                body={
                    "topicName": topic_name,
                    "labelIds": ["INBOX"],
                    "labelFilterBehavior": "include",
                },
            )
        )
        try:
            return WatchState(
                history_id=self._required_string(response, "historyId"),
                expiration_ms=int(self._required_string(response, "expiration")),
            )
        except TypeError, ValueError:
            raise GmailTerminalError("gmail_invalid_response") from None

    def get_profile(self) -> str:
        response = self._execute(self._service.users().getProfile(userId="me"))
        return self._required_string(response, "emailAddress")

    def ensure_labels(self, labels: tuple[str, ...]) -> None:
        response = self._execute(self._service.users().labels().list(userId="me"))
        known = {
            self._required_string(item, "name"): self._required_string(item, "id")
            for item in self._mapping_tuple(response, "labels")
        }
        for label in labels:
            if label in known:
                continue
            created = self._execute(
                self._service.users()
                .labels()
                .create(
                    userId="me",
                    body={
                        "name": label,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                )
            )
            known[label] = self._required_string(created, "id")
        self._label_ids = {label: known[label] for label in labels}

    def stop_watch(self) -> None:
        self._execute(self._service.users().stop(userId="me"))

    def list_history(
        self, start_history_id: str, page_token: str | None = None
    ) -> HistoryPage:
        arguments: dict[str, object] = {
            "userId": "me",
            "startHistoryId": start_history_id,
            "maxResults": 500,
        }
        if page_token is not None:
            arguments["pageToken"] = page_token
        response = self._execute(self._service.users().history().list(**arguments))
        return HistoryPage(
            records=self._mapping_tuple(response, "history"),
            history_id=self._required_string(response, "historyId"),
            next_page_token=self._optional_string(response, "nextPageToken"),
        )

    def list_unread(self, page_token: str | None = None) -> MessagePage:
        arguments: dict[str, object] = {
            "userId": "me",
            "labelIds": ["INBOX", "UNREAD"],
            "maxResults": 500,
        }
        if page_token is not None:
            arguments["pageToken"] = page_token
        response = self._execute(self._service.users().messages().list(**arguments))
        messages = tuple(
            GmailMessageRef(
                message_id=self._required_string(item, "id"),
                thread_id=self._required_string(item, "threadId"),
            )
            for item in self._mapping_tuple(response, "messages")
        )
        return MessagePage(
            messages=messages,
            next_page_token=self._optional_string(response, "nextPageToken"),
        )

    def get_message(self, message_id: str) -> Mapping[str, object]:
        return self._execute(
            self._service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
        )

    def get_message_metadata(self, message_id: str) -> GmailMessageMetadata:
        response = self._execute(
            self._service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=[],
            )
        )
        try:
            internal_date_ms = int(self._required_string(response, "internalDate"))
        except ValueError:
            raise GmailTerminalError("gmail_invalid_response") from None
        if internal_date_ms < 0:
            raise GmailTerminalError("gmail_invalid_response")
        return GmailMessageMetadata(
            message_id=self._required_string(response, "id"),
            thread_id=self._required_string(response, "threadId"),
            internal_date_ms=internal_date_ms,
            label_ids=self._string_tuple(response, "labelIds"),
        )

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        response = self._execute(
            self._service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
        )
        data = self._required_string(response, "data")
        try:
            return base64.b64decode(
                data + "=" * (-len(data) % 4), altchars=b"-_", validate=True
            )
        except binascii.Error, ValueError:
            raise GmailTerminalError("gmail_invalid_response") from None

    def modify_labels(
        self,
        message_id: str,
        *,
        add: tuple[str, ...] = (),
        remove: tuple[str, ...] = (),
    ) -> None:
        self._execute(
            self._service.users()
            .messages()
            .modify(
                userId="me",
                id=message_id,
                body={
                    "addLabelIds": [self._label_ids.get(label, label) for label in add],
                    "removeLabelIds": [
                        self._label_ids.get(label, label) for label in remove
                    ],
                },
            )
        )

    def inspect_thread(self, thread_id: str) -> ThreadSnapshot:
        response = self._execute(
            self._service.users()
            .threads()
            .get(
                userId="me",
                id=thread_id,
                format="metadata",
                metadataHeaders=[
                    "Message-ID",
                    "X-Alza-AI-Source-Message-ID",
                ],
            )
        )
        messages = tuple(
            self._thread_message(item)
            for item in self._mapping_tuple(response, "messages")
        )
        return ThreadSnapshot(
            thread_id=self._required_string(response, "id"), messages=messages
        )

    def send_message(self, message: OutboundMessage) -> SentMessage:
        raw = base64.urlsafe_b64encode(message.raw).decode("ascii").rstrip("=")
        response = self._execute(
            self._service.users()
            .messages()
            .send(
                userId="me",
                body={"threadId": message.thread_id, "raw": raw},
            ),
            ambiguous_send=True,
        )
        try:
            return SentMessage(
                message_id=self._required_string(response, "id"),
                thread_id=self._required_string(response, "threadId"),
            )
        except GmailTerminalError:
            raise GmailAmbiguousSendError("gmail_invalid_response") from None

    def _execute(
        self, request: _Request, *, ambiguous_send: bool = False
    ) -> Mapping[str, object]:
        try:
            response = request.execute()
        except HttpError as error:
            status = int(getattr(error.resp, "status", 0))
            if ambiguous_send and status >= 500:
                raise GmailAmbiguousSendError("gmail_http_error", status) from None
            if status in {408, 429} or status >= 500:
                raise GmailRetryableError("gmail_http_error", status) from None
            raise GmailTerminalError("gmail_http_error", status) from None
        except TimeoutError, OSError, TransportError:
            error_type = (
                GmailAmbiguousSendError if ambiguous_send else GmailRetryableError
            )
            raise error_type("gmail_transport_error") from None
        if not isinstance(response, Mapping):
            raise GmailTerminalError("gmail_invalid_response")
        return response

    @classmethod
    def _thread_message(cls, value: Mapping[str, object]) -> ThreadMessage:
        payload = value.get("payload", {})
        if not isinstance(payload, Mapping):
            raise GmailTerminalError("gmail_invalid_response")
        headers = cls._mapping_tuple(payload, "headers")
        selected: dict[str, str] = {}
        for header in headers:
            name = header.get("name")
            header_value = header.get("value")
            if isinstance(name, str) and isinstance(header_value, str):
                selected[name.casefold()] = header_value
        return ThreadMessage(
            message_id=cls._required_string(value, "id"),
            rfc_message_id=selected.get("message-id"),
            source_message_id=selected.get("x-alza-ai-source-message-id"),
        )

    @staticmethod
    def _required_string(value: Mapping[str, object], key: str) -> str:
        item = value.get(key)
        if not isinstance(item, str) or not item:
            raise GmailTerminalError("gmail_invalid_response")
        return item

    @staticmethod
    def _optional_string(value: Mapping[str, object], key: str) -> str | None:
        item = value.get(key)
        if item is None:
            return None
        if not isinstance(item, str) or not item:
            raise GmailTerminalError("gmail_invalid_response")
        return item

    @staticmethod
    def _mapping_tuple(
        value: Mapping[str, object], key: str
    ) -> tuple[Mapping[str, object], ...]:
        items = value.get(key, [])
        if not isinstance(items, list) or not all(
            isinstance(item, Mapping) for item in items
        ):
            raise GmailTerminalError("gmail_invalid_response")
        return tuple(cast(Mapping[str, object], item) for item in items)

    @staticmethod
    def _string_tuple(value: Mapping[str, object], key: str) -> tuple[str, ...]:
        items = value.get(key, [])
        if not isinstance(items, list) or not all(
            isinstance(item, str) and item for item in items
        ):
            raise GmailTerminalError("gmail_invalid_response")
        return tuple(cast(str, item) for item in items)


def build_threaded_reply(
    *,
    mailbox_key: str,
    source_message_id: str,
    thread_id: str,
    recipient: str,
    subject: str,
    source_rfc_message_id: str,
    references: tuple[str, ...],
    text: str,
    html: str | None = None,
) -> OutboundMessage:
    ordered_references = tuple(dict.fromkeys((*references, source_rfc_message_id)))

    message = EmailMessage(policy=SMTP)
    message["To"] = recipient
    message["Subject"] = subject
    message["Message-ID"] = deterministic_outbound_message_id(
        mailbox_key, source_message_id
    )
    message["X-Alza-AI-Source-Message-ID"] = source_message_id
    message["In-Reply-To"] = source_rfc_message_id
    message["References"] = " ".join(ordered_references)
    message.set_content(text, charset="utf-8")
    if html is not None:
        message.add_alternative(html, subtype="html", charset="utf-8")
    return OutboundMessage(thread_id=thread_id, raw=message.as_bytes(policy=SMTP))


def deterministic_outbound_message_id(mailbox_key: str, source_message_id: str) -> str:
    digest = hashlib.sha256(f"{mailbox_key}:{source_message_id}".encode()).hexdigest()
    return f"<alza-ai-{digest}@reply.invalid>"
