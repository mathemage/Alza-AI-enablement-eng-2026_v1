import base64
import hashlib
from collections.abc import Mapping
from email import policy
from email.parser import BytesParser

import pytest
from googleapiclient.errors import HttpError  # type: ignore[import-untyped]

import alza_ai.gmail as gmail_module
from alza_ai.gmail import (
    GmailAmbiguousSendError,
    GmailApiGateway,
    GmailGateway,
    GmailMessageMetadata,
    GmailMessageRef,
    GmailRetryableError,
    GmailTerminalError,
    HistoryPage,
    MessagePage,
    OutboundMessage,
    SentMessage,
    ThreadMessage,
    ThreadSnapshot,
    WatchState,
    build_threaded_reply,
)

JsonObject = dict[str, object]

TOPIC = "projects/example/topics/gmail-notifications"
HISTORY_RECORD: JsonObject = {
    "id": "101",
    "messages": [{"id": "message-1", "threadId": "thread-1"}],
}
FULL_MESSAGE: JsonObject = {
    "id": "message-1",
    "threadId": "thread-1",
    "labelIds": ["INBOX", "UNREAD"],
    "payload": {"mimeType": "text/plain", "body": {"data": "cXVlc3Rpb24="}},
}
MESSAGE_METADATA_RESPONSE: JsonObject = {
    "id": "message-1",
    "threadId": "thread-1",
    "internalDate": "1786968000000",
    "labelIds": ["INBOX", "UNREAD"],
}
MESSAGE_METADATA = GmailMessageMetadata(
    message_id="message-1",
    thread_id="thread-1",
    internal_date_ms=1_786_968_000_000,
    label_ids=("INBOX", "UNREAD"),
)
ATTACHMENT = b"attachment-bytes"
THREAD = ThreadSnapshot(
    thread_id="thread-1",
    messages=(
        ThreadMessage(
            message_id="sent-1",
            rfc_message_id="<reply@example.test>",
            source_message_id="message-1",
        ),
    ),
)
OUTBOUND = OutboundMessage(thread_id="thread-1", raw=b"Subject: Status\r\n\r\nDone")


class FakeGmailGateway:
    def __init__(self) -> None:
        self.watch_active = False
        self.labels = {"message-1": {"INBOX", "UNREAD"}}
        self.sent: list[OutboundMessage] = []

    def start_watch(self, topic_name: str) -> WatchState:
        assert topic_name == TOPIC
        self.watch_active = True
        return WatchState(history_id="100", expiration_ms=2_000_000_000_000)

    def stop_watch(self) -> None:
        self.watch_active = False

    def list_history(
        self, start_history_id: str, page_token: str | None = None
    ) -> HistoryPage:
        assert (start_history_id, page_token) == ("100", "history-page")
        return HistoryPage(
            records=(HISTORY_RECORD,),
            history_id="101",
            next_page_token="history-next",
        )

    def list_unread(self, page_token: str | None = None) -> MessagePage:
        assert page_token == "message-page"
        return MessagePage(
            messages=(GmailMessageRef("message-1", "thread-1"),),
            next_page_token="message-next",
        )

    def get_message(self, message_id: str) -> Mapping[str, object]:
        assert message_id == "message-1"
        return FULL_MESSAGE

    def get_message_metadata(self, message_id: str) -> GmailMessageMetadata:
        assert message_id == "message-1"
        return MESSAGE_METADATA

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        assert (message_id, attachment_id) == ("message-1", "attachment-1")
        return ATTACHMENT

    def modify_labels(
        self,
        message_id: str,
        *,
        add: tuple[str, ...] = (),
        remove: tuple[str, ...] = (),
    ) -> None:
        labels = self.labels[message_id]
        labels.update(add)
        labels.difference_update(remove)

    def inspect_thread(self, thread_id: str) -> ThreadSnapshot:
        assert thread_id == "thread-1"
        return THREAD

    def send_message(self, message: OutboundMessage) -> SentMessage:
        self.sent.append(message)
        return SentMessage(message_id="sent-1", thread_id=message.thread_id)


class MockHttpResponse(dict[str, str]):
    def __init__(self, status: int) -> None:
        super().__init__(status=str(status), reason="failure")
        self.status = status
        self.reason = "failure"


class MockRequest:
    def __init__(self, result: JsonObject | BaseException) -> None:
        self.result = result

    def execute(self) -> JsonObject:
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class MockMethod:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.results: list[JsonObject | BaseException] = []

    def queue(self, *results: JsonObject | BaseException) -> None:
        self.results.extend(results)

    def __call__(self, **kwargs: object) -> MockRequest:
        self.calls.append(kwargs)
        return MockRequest(self.results.pop(0))


class MockAttachmentsResource:
    def __init__(self) -> None:
        self.get = MockMethod()


class MockMessagesResource:
    def __init__(self) -> None:
        self.list = MockMethod()
        self.get = MockMethod()
        self.modify = MockMethod()
        self.send = MockMethod()
        self.attachment_resource = MockAttachmentsResource()

    def attachments(self) -> MockAttachmentsResource:
        return self.attachment_resource


class MockHistoryResource:
    def __init__(self) -> None:
        self.list = MockMethod()


class MockThreadsResource:
    def __init__(self) -> None:
        self.get = MockMethod()


class MockUsersResource:
    def __init__(self) -> None:
        self.watch = MockMethod()
        self.stop = MockMethod()
        self.history_resource = MockHistoryResource()
        self.messages_resource = MockMessagesResource()
        self.threads_resource = MockThreadsResource()

    def history(self) -> MockHistoryResource:
        return self.history_resource

    def messages(self) -> MockMessagesResource:
        return self.messages_resource

    def threads(self) -> MockThreadsResource:
        return self.threads_resource


class MockGmailService:
    def __init__(self) -> None:
        self.users_resource = MockUsersResource()

    def users(self) -> MockUsersResource:
        return self.users_resource


def exercise_gateway_contract(gateway: GmailGateway) -> None:
    assert gateway.start_watch(TOPIC) == WatchState(
        history_id="100", expiration_ms=2_000_000_000_000
    )
    gateway.stop_watch()
    assert gateway.list_history("100", "history-page") == HistoryPage(
        records=(HISTORY_RECORD,),
        history_id="101",
        next_page_token="history-next",
    )
    assert gateway.list_unread("message-page") == MessagePage(
        messages=(GmailMessageRef("message-1", "thread-1"),),
        next_page_token="message-next",
    )
    assert gateway.get_message_metadata("message-1") == MESSAGE_METADATA
    assert gateway.get_message("message-1") == FULL_MESSAGE
    assert gateway.get_attachment("message-1", "attachment-1") == ATTACHMENT
    gateway.modify_labels("message-1", add=("AI/Processed",), remove=("UNREAD",))
    assert gateway.inspect_thread("thread-1") == THREAD
    assert gateway.send_message(OUTBOUND) == SentMessage(
        message_id="sent-1", thread_id="thread-1"
    )


def configured_mock_service() -> MockGmailService:
    service = MockGmailService()
    users = service.users_resource
    messages = users.messages_resource
    users.watch.queue({"historyId": "100", "expiration": "2000000000000"})
    users.stop.queue({})
    users.history_resource.list.queue(
        {
            "history": [HISTORY_RECORD],
            "historyId": "101",
            "nextPageToken": "history-next",
        }
    )
    messages.list.queue(
        {
            "messages": [{"id": "message-1", "threadId": "thread-1"}],
            "nextPageToken": "message-next",
        }
    )
    messages.get.queue(MESSAGE_METADATA_RESPONSE, FULL_MESSAGE)
    messages.attachment_resource.get.queue(
        {"data": base64.urlsafe_b64encode(ATTACHMENT).decode().rstrip("=")}
    )
    messages.modify.queue(FULL_MESSAGE)
    users.threads_resource.get.queue(
        {
            "id": "thread-1",
            "messages": [
                {
                    "id": "sent-1",
                    "payload": {
                        "headers": [
                            {"name": "Message-ID", "value": "<reply@example.test>"},
                            {
                                "name": "X-Alza-AI-Source-Message-ID",
                                "value": "message-1",
                            },
                        ],
                        "body": {"data": "must-not-cross-the-boundary"},
                    },
                }
            ],
        }
    )
    messages.send.queue({"id": "sent-1", "threadId": "thread-1"})
    return service


def test_gmail_01_deterministic_fake_satisfies_the_gateway_contract() -> None:
    fake = FakeGmailGateway()

    assert isinstance(fake, GmailGateway)
    exercise_gateway_contract(fake)

    assert fake.watch_active is False
    assert fake.labels["message-1"] == {"INBOX", "AI/Processed"}
    assert fake.sent == [OUTBOUND]


def test_gmail_01_mocked_adapter_satisfies_the_same_contract() -> None:
    service = configured_mock_service()
    gateway = GmailApiGateway(service)

    exercise_gateway_contract(gateway)

    users = service.users_resource
    messages = users.messages_resource
    assert users.watch.calls == [
        {
            "userId": "me",
            "body": {
                "topicName": TOPIC,
                "labelIds": ["INBOX"],
                "labelFilterBehavior": "include",
            },
        }
    ]
    assert users.stop.calls == [{"userId": "me"}]
    assert users.history_resource.list.calls == [
        {
            "userId": "me",
            "startHistoryId": "100",
            "maxResults": 500,
            "pageToken": "history-page",
        }
    ]
    assert messages.list.calls == [
        {
            "userId": "me",
            "labelIds": ["INBOX", "UNREAD"],
            "maxResults": 500,
            "pageToken": "message-page",
        }
    ]
    assert messages.get.calls == [
        {
            "userId": "me",
            "id": "message-1",
            "format": "metadata",
            "metadataHeaders": [],
        },
        {"userId": "me", "id": "message-1", "format": "full"},
    ]
    assert messages.attachment_resource.get.calls == [
        {
            "userId": "me",
            "messageId": "message-1",
            "id": "attachment-1",
        }
    ]
    assert messages.modify.calls == [
        {
            "userId": "me",
            "id": "message-1",
            "body": {
                "addLabelIds": ["AI/Processed"],
                "removeLabelIds": ["UNREAD"],
            },
        }
    ]
    assert users.threads_resource.get.calls == [
        {
            "userId": "me",
            "id": "thread-1",
            "format": "metadata",
            "metadataHeaders": [
                "Message-ID",
                "X-Alza-AI-Source-Message-ID",
            ],
        }
    ]
    assert messages.send.calls == [
        {
            "userId": "me",
            "body": {
                "threadId": "thread-1",
                "raw": base64.urlsafe_b64encode(OUTBOUND.raw).decode().rstrip("="),
            },
        }
    ]


def test_gmail_01_adapter_factory_uses_the_mocked_gmail_v1_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MockGmailService()
    credentials = object()
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_build(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        return service

    monkeypatch.setattr(gmail_module, "build", fake_build)

    gateway = GmailApiGateway.from_credentials(credentials)

    assert isinstance(gateway, GmailApiGateway)
    assert calls == [
        (
            ("gmail", "v1"),
            {"credentials": credentials, "cache_discovery": False},
        )
    ]


def make_http_error(status: int, content: bytes) -> HttpError:
    return HttpError(MockHttpResponse(status), content)


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (408, GmailRetryableError),
        (429, GmailRetryableError),
        (503, GmailRetryableError),
        (403, GmailTerminalError),
    ],
)
def test_gmail_01_adapter_maps_http_errors_without_response_content(
    status: int, error_type: type[Exception]
) -> None:
    service = MockGmailService()
    service.users_resource.messages_resource.get.queue(
        make_http_error(status, b"private body and token-secret")
    )

    with pytest.raises(error_type) as raised:
        GmailApiGateway(service).get_message("message-1")

    assert "private body" not in str(raised.value)
    assert "token-secret" not in str(raised.value)
    assert raised.value.__cause__ is None


def test_gmail_01_stale_history_cursor_is_a_terminal_error() -> None:
    service = MockGmailService()
    service.users_resource.history_resource.list.queue(
        make_http_error(404, b"private history response")
    )

    with pytest.raises(GmailTerminalError) as raised:
        GmailApiGateway(service).list_history("stale-history")

    assert raised.value.status == 404
    assert "private history response" not in str(raised.value)
    assert service.users_resource.history_resource.list.calls == [
        {
            "userId": "me",
            "startHistoryId": "stale-history",
            "maxResults": 500,
        }
    ]


def test_gmail_01_transport_failures_are_retryable_except_for_send() -> None:
    read_service = MockGmailService()
    read_service.users_resource.messages_resource.get.queue(
        TimeoutError("private body")
    )
    with pytest.raises(GmailRetryableError, match="gmail_transport_error"):
        GmailApiGateway(read_service).get_message("message-1")

    send_service = MockGmailService()
    send_service.users_resource.messages_resource.send.queue(
        TimeoutError("token-secret")
    )
    with pytest.raises(GmailAmbiguousSendError) as raised:
        GmailApiGateway(send_service).send_message(OUTBOUND)

    assert "token-secret" not in str(raised.value)
    assert raised.value.__cause__ is None


def test_gmail_01_send_server_failure_is_ambiguous() -> None:
    service = MockGmailService()
    service.users_resource.messages_resource.send.queue(
        make_http_error(503, b"private reply")
    )

    with pytest.raises(GmailAmbiguousSendError):
        GmailApiGateway(service).send_message(OUTBOUND)


def test_gmail_01_malformed_send_success_is_ambiguous() -> None:
    service = MockGmailService()
    service.users_resource.messages_resource.send.queue({"id": "sent-1"})

    with pytest.raises(GmailAmbiguousSendError, match="gmail_invalid_response"):
        GmailApiGateway(service).send_message(OUTBOUND)


def test_gmail_01_malformed_success_is_a_sanitized_terminal_error() -> None:
    service = MockGmailService()
    service.users_resource.watch.queue(
        {"historyId": "private-history", "expiration": "not-an-integer"}
    )

    with pytest.raises(GmailTerminalError, match="gmail_invalid_response") as raised:
        GmailApiGateway(service).start_watch(TOPIC)

    assert "private-history" not in str(raised.value)


def test_proc_02_threaded_reply_has_deterministic_headers() -> None:
    def build_reply() -> OutboundMessage:
        return build_threaded_reply(
            mailbox_key="mailbox-key",
            source_message_id="message-1",
            thread_id="thread-1",
            recipient="recipient@example.test",
            subject="Re: Přehled",
            source_rfc_message_id="<source@example.test>",
            references=(
                "<root@example.test>",
                "<root@example.test>",
                "<source@example.test>",
            ),
            text="Hotovo.",
        )

    first = build_reply()
    second = build_reply()
    parsed = BytesParser(policy=policy.default).parsebytes(first.raw)
    digest = hashlib.sha256(b"mailbox-key:message-1").hexdigest()

    assert first == second
    assert first.thread_id == "thread-1"
    assert parsed["To"] == "recipient@example.test"
    assert parsed["Subject"] == "Re: Přehled"
    assert parsed["Message-ID"] == f"<alza-ai-{digest}@reply.invalid>"
    assert parsed["X-Alza-AI-Source-Message-ID"] == "message-1"
    assert parsed["In-Reply-To"] == "<source@example.test>"
    assert parsed["References"] == ("<root@example.test> <source@example.test>")
    body = parsed.get_body(preferencelist=("plain",))
    assert body is not None
    assert body.get_content() == "Hotovo.\r\n"


def test_proc_02_threaded_reply_appends_source_reference_once() -> None:
    reply = build_threaded_reply(
        mailbox_key="mailbox-key",
        source_message_id="message-1",
        thread_id="thread-1",
        recipient="recipient@example.test",
        subject="Status",
        source_rfc_message_id="<source@example.test>",
        references=("<root@example.test>",),
        text="Done",
    )
    parsed = BytesParser(policy=policy.default).parsebytes(reply.raw)

    assert parsed["References"] == ("<root@example.test> <source@example.test>")
