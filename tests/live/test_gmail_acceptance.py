import base64
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parseaddr
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import pytest
from google.cloud import firestore
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build  # type: ignore[import-untyped]

from alza_ai.oauth import GMAIL_MODIFY_SCOPE
from alza_ai.processing import PROCESSING_COLLECTION
from alza_ai.synchronization import MAILBOX_COLLECTION
from tests.live.support import LiveConfig, LiveFailure, require

_CASE_COUNTS = {"plain": 0, "pdf": 1, "audio": 2, "image": 2, "current": 0}
_URL = re.compile(r"https?://[^\s<>\"]+")


def test_live_13_gmail_watch_and_five_cases(live_config: LiveConfig) -> None:
    try:
        live_config.require_gmail()
        assert live_config.oauth_client_path is not None
        assert live_config.oauth_token_path is not None
        assert live_config.mailbox_key is not None
        assert live_config.live_sender is not None
        credentials = _credentials(
            live_config.oauth_client_path, live_config.oauth_token_path
        )
        gmail = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        profile = _gmail_execute(gmail.users().getProfile(userId="me"))
        require(
            _string(profile, "emailAddress").casefold()
            == live_config.mailbox.casefold(),
            "gmail_profile_mismatch",
        )
        database = firestore.Client(project=live_config.project_id)
        try:
            mailbox_document = (
                database.collection(MAILBOX_COLLECTION)
                .document(hashlib.sha256(live_config.mailbox_key.encode()).hexdigest())
                .get()
            )
        except Exception:  # noqa: BLE001 - live output must remain sanitized
            raise LiveFailure("gmail_watch_absent") from None
        mailbox_state = mailbox_document.to_dict() if mailbox_document.exists else None
        if not isinstance(mailbox_state, dict):
            raise LiveFailure("gmail_watch_absent")
        expiration = mailbox_state.get("watch_expiration_ms")
        activated_at = mailbox_state.get("activated_at")
        require(
            isinstance(expiration, int)
            and expiration > int(datetime.now(UTC).timestamp() * 1000),
            "gmail_watch_expired",
        )
        require(
            isinstance(activated_at, datetime) and activated_at.tzinfo is not None,
            "gmail_watch_activation_invalid",
        )

        labels_response = _gmail_execute(gmail.users().labels().list(userId="me"))
        labels = {
            _string(item, "name"): _string(item, "id")
            for item in _mappings(labels_response.get("labels", []))
        }
        require(
            "AI/Processed" in labels and "AI/Error" in labels, "gmail_labels_absent"
        )

        for case, expected_attachments in _CASE_COUNTS.items():
            _verify_case(
                gmail=gmail,
                database=database,
                config=live_config,
                case=case,
                query=live_config.live_cases[case],
                expected_attachments=expected_attachments,
                processed_label=labels["AI/Processed"],
                error_label=labels["AI/Error"],
            )
    except LiveFailure as error:
        pytest.fail(error.code, pytrace=False)


def _verify_case(
    *,
    gmail: object,
    database: firestore.Client,
    config: LiveConfig,
    case: str,
    query: str,
    expected_attachments: int,
    processed_label: str,
    error_label: str,
) -> None:
    assert config.mailbox_key is not None
    assert config.live_sender is not None
    users = gmail.users()  # type: ignore[attr-defined]
    listed = _gmail_execute(users.messages().list(userId="me", q=query, maxResults=10))
    references = _mappings(listed.get("messages", []))
    candidates = [
        _gmail_execute(
            users.messages().get(
                userId="me", id=_string(reference, "id"), format="full"
            )
        )
        for reference in references
    ]
    sources = [
        message
        for message in candidates
        if parseaddr(_headers(_mapping(message.get("payload"))).get("from", ""))[
            1
        ].casefold()
        == config.live_sender.casefold()
    ]
    require(len(sources) == 1, f"live_{case}_source_count_invalid")
    source = sources[0]
    source_id = _string(source, "id")
    source_headers = _headers(_mapping(source.get("payload")))
    sender = parseaddr(source_headers.get("from", ""))[1].casefold()
    require(sender == config.live_sender.casefold(), f"live_{case}_sender_mismatch")
    original_rfc_id = source_headers.get("message-id")
    require(bool(original_rfc_id), f"live_{case}_source_headers_invalid")
    thread_id = _string(source, "threadId")
    thread = _gmail_execute(
        users.threads().get(userId="me", id=thread_id, format="full")
    )
    messages = _mappings(thread.get("messages", []))
    replies = [
        message
        for message in messages
        if _headers(_mapping(message.get("payload"))).get("x-alza-ai-source-message-id")
        == source_id
    ]
    require(len(replies) == 1, f"live_{case}_reply_count_invalid")
    reply = replies[0]
    reply_headers = _headers(_mapping(reply.get("payload")))
    headers_ok = (
        bool(reply_headers.get("message-id"))
        and reply_headers.get("in-reply-to") == original_rfc_id
        and original_rfc_id in reply_headers.get("references", "").split()
    )
    require(headers_ok, f"live_{case}_thread_headers_invalid")

    source_time = int(_string(source, "internalDate"))
    reply_time = int(_string(reply, "internalDate"))
    latency_ms = reply_time - source_time
    require(0 <= latency_ms < 120_000, f"live_{case}_latency_invalid")
    label_ids = set(_strings(source.get("labelIds", [])))
    labels_ok = (
        processed_label in label_ids
        and error_label not in label_ids
        and "UNREAD" not in label_ids
    )
    require(labels_ok, f"live_{case}_labels_invalid")

    attachment_count = _attachment_count(_mapping(source.get("payload")))
    require(
        attachment_count == expected_attachments,
        f"live_{case}_attachment_count_invalid",
    )
    reply_text = _message_text(_mapping(reply.get("payload")))
    citations = {url for url in _URL.findall(reply_text) if _public_url(url)}
    if case == "current":
        require(bool(citations), "live_current_citations_absent")

    record_id = hashlib.sha256(f"{config.mailbox_key}:{source_id}".encode()).hexdigest()
    record = database.collection(PROCESSING_COLLECTION).document(record_id).get()
    state = record.to_dict() if record.exists else None
    require(
        isinstance(state, dict) and state.get("state") == "completed",
        f"live_{case}_state_invalid",
    )
    print(
        f"LIVE-01-{case} pass=true latency_ms={latency_ms} reply_count=1 "
        f"attachment_count={attachment_count} citation_count={len(citations)} "
        "state=completed thread=true headers=true labels=true"
    )


def _credentials(client_path: Path, token_path: Path) -> Credentials:
    try:
        client_document = json.loads(client_path.read_text(encoding="utf-8"))
        token_document = json.loads(token_path.read_text(encoding="utf-8"))
        client = client_document.get("installed", client_document.get("web"))
        if not isinstance(client, dict) or not isinstance(token_document, dict):
            raise TypeError
        scopes = token_document.get("scopes")
        refresh_token = token_document.get("refresh_token")
        if scopes != [GMAIL_MODIFY_SCOPE] or not isinstance(refresh_token, str):
            raise ValueError
        return Credentials(  # type: ignore[no-untyped-call]
            token=None,
            refresh_token=refresh_token,
            token_uri=_string(client, "token_uri"),
            client_id=_string(client, "client_id"),
            client_secret=_string(client, "client_secret"),
            scopes=[GMAIL_MODIFY_SCOPE],
        )
    except OSError, TypeError, ValueError, LiveFailure:
        raise LiveFailure("gmail_credentials_invalid") from None


def _gmail_execute(request: object) -> Mapping[str, object]:
    try:
        result = request.execute()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - live output must remain sanitized
        raise LiveFailure("gmail_api_unavailable") from None
    return _mapping(result)


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise LiveFailure("gmail_response_invalid")
    return value


def _mappings(value: object) -> Sequence[Mapping[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise LiveFailure("gmail_response_invalid")
    return cast(list[Mapping[str, object]], value)


def _strings(value: object) -> Sequence[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise LiveFailure("gmail_response_invalid")
    return cast(list[str], value)


def _string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise LiveFailure("gmail_response_invalid")
    return item


def _headers(payload: Mapping[str, object]) -> dict[str, str]:
    return {
        _string(header, "name").casefold(): _string(header, "value")
        for header in _mappings(payload.get("headers", []))
    }


def _attachment_count(payload: Mapping[str, object]) -> int:
    count = 0
    media_type = payload.get("mimeType")
    filename = payload.get("filename")
    if (
        isinstance(filename, str)
        and filename
        and media_type
        in {
            "application/pdf",
            "audio/mpeg",
            "audio/wav",
            "audio/x-wav",
            "audio/vnd.wave",
            "image/jpeg",
            "image/png",
        }
    ):
        count = 1
    return count + sum(
        _attachment_count(part) for part in _mappings(payload.get("parts", []))
    )


def _message_text(payload: Mapping[str, object]) -> str:
    texts: list[str] = []
    media_type = payload.get("mimeType")
    body = payload.get("body")
    if media_type in {"text/plain", "text/html"} and isinstance(body, dict):
        data = body.get("data")
        if isinstance(data, str) and data:
            try:
                texts.append(
                    base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
                        "utf-8"
                    )
                )
            except ValueError, UnicodeDecodeError:
                raise LiveFailure("gmail_response_invalid") from None
    texts.extend(_message_text(part) for part in _mappings(payload.get("parts", [])))
    return "\n".join(texts)


def _public_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)
