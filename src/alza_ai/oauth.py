import json
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol, cast

from google_auth_oauthlib.flow import (  # type: ignore[import-untyped]
    InstalledAppFlow,
)
from googleapiclient.discovery import build  # type: ignore[import-untyped]

GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"


class OAuthBootstrapError(Exception):
    pass


class OAuthCredentials(Protocol):
    refresh_token: str | None
    scopes: Sequence[str] | None


class OAuthFlow(Protocol):
    def run_local_server(self, **kwargs: object) -> OAuthCredentials: ...


FlowFactory = Callable[[Path, tuple[str, ...]], OAuthFlow]
MailboxLookup = Callable[[OAuthCredentials], str]


def _create_flow(client_secrets: Path, scopes: tuple[str, ...]) -> OAuthFlow:
    return cast(
        OAuthFlow,
        InstalledAppFlow.from_client_secrets_file(
            str(client_secrets), scopes=list(scopes)
        ),
    )


def _lookup_mailbox(credentials: OAuthCredentials) -> str:
    service = build(
        "gmail",
        "v1",
        credentials=credentials,
        cache_discovery=False,
    )
    response = service.users().getProfile(userId="me").execute()
    email_address = response.get("emailAddress")
    if not isinstance(email_address, str) or not email_address:
        raise OAuthBootstrapError("oauth_profile_invalid")
    return email_address


def bootstrap_oauth(
    *,
    client_secrets: Path,
    expected_account: str,
    token_output: Path,
    flow_factory: FlowFactory = _create_flow,
    mailbox_lookup: MailboxLookup = _lookup_mailbox,
) -> None:
    if token_output.exists():
        raise OAuthBootstrapError("oauth_output_exists")
    if not client_secrets.is_file():
        raise OAuthBootstrapError("oauth_client_secrets_missing")
    if not token_output.parent.is_dir():
        raise OAuthBootstrapError("oauth_output_parent_missing")

    try:
        flow = flow_factory(client_secrets, (GMAIL_MODIFY_SCOPE,))
        credentials = flow.run_local_server(
            host="127.0.0.1",
            port=0,
            open_browser=True,
            access_type="offline",
            prompt="consent",
        )
    # This credential boundary must never expose vendor exception text.
    except Exception:  # noqa: BLE001
        raise OAuthBootstrapError("oauth_authorization_failed") from None

    granted_scopes = cast(
        Sequence[str] | None, getattr(credentials, "granted_scopes", None)
    )
    scopes = granted_scopes if granted_scopes is not None else credentials.scopes
    if scopes is None or tuple(scopes) != (GMAIL_MODIFY_SCOPE,):
        raise OAuthBootstrapError("oauth_scope_mismatch")
    if not credentials.refresh_token:
        raise OAuthBootstrapError("oauth_refresh_token_missing")

    try:
        mailbox = mailbox_lookup(credentials)
    except OAuthBootstrapError:
        raise
    # Profile-client failures can include request data and remain sanitized here.
    except Exception:  # noqa: BLE001
        raise OAuthBootstrapError("oauth_profile_failed") from None
    if mailbox.casefold() != expected_account.casefold():
        raise OAuthBootstrapError("oauth_account_mismatch")

    payload = json.dumps(
        {
            "refresh_token": credentials.refresh_token,
            "scopes": [GMAIL_MODIFY_SCOPE],
        },
        sort_keys=True,
    )
    try:
        descriptor = os.open(
            token_output,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(payload + "\n")
    except FileExistsError:
        raise OAuthBootstrapError("oauth_output_exists") from None
    except OSError:
        if token_output.exists():
            token_output.unlink()
        raise OAuthBootstrapError("oauth_output_write_failed") from None
