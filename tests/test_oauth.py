import json
import stat
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

import alza_ai.oauth as oauth_module
from alza_ai.cli import main
from alza_ai.oauth import (
    GMAIL_MODIFY_SCOPE,
    OAuthBootstrapError,
    OAuthCredentials,
    OAuthFlow,
    bootstrap_oauth,
)

ROOT = Path(__file__).resolve().parents[1]
REFRESH_TOKEN = "refresh-token-secret"
ACCESS_TOKEN = "access-token-secret"
CLIENT_SECRET = "client-secret-value"


class FakeCredentials:
    def __init__(
        self,
        *,
        refresh_token: str | None = REFRESH_TOKEN,
        scopes: Sequence[str] = (GMAIL_MODIFY_SCOPE,),
        granted_scopes: Sequence[str] | None = None,
    ) -> None:
        self.refresh_token: str | None = refresh_token
        self.scopes: Sequence[str] | None = scopes
        self.granted_scopes = granted_scopes
        self.token = ACCESS_TOKEN
        self.client_secret = CLIENT_SECRET


class FakeInstalledAppFlow:
    def __init__(self, credentials: FakeCredentials) -> None:
        self.credentials = credentials
        self.local_server_kwargs: dict[str, object] | None = None

    def run_local_server(self, **kwargs: object) -> OAuthCredentials:
        self.local_server_kwargs = kwargs
        return self.credentials


class FlowFactorySpy:
    def __init__(self, flow: FakeInstalledAppFlow) -> None:
        self.flow = flow
        self.calls: list[tuple[Path, tuple[str, ...]]] = []

    def __call__(self, client_secrets: Path, scopes: tuple[str, ...]) -> OAuthFlow:
        self.calls.append((client_secrets, scopes))
        return self.flow


def client_file(tmp_path: Path) -> Path:
    path = tmp_path / "client-secret.json"
    path.write_text('{"installed": {}}', encoding="utf-8")
    return path


def test_oauth_01_bootstrap_uses_only_modify_and_writes_a_secure_refresh_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    client_secrets = client_file(tmp_path)
    output = tmp_path / "refresh-token.json"
    credentials = FakeCredentials()
    flow = FakeInstalledAppFlow(credentials)
    factory = FlowFactorySpy(flow)
    looked_up: list[OAuthCredentials] = []

    def mailbox_lookup(value: OAuthCredentials) -> str:
        looked_up.append(value)
        return "Dedicated@Example.test"

    bootstrap_oauth(
        client_secrets=client_secrets,
        expected_account="dedicated@example.test",
        token_output=output,
        flow_factory=factory,
        mailbox_lookup=mailbox_lookup,
    )

    assert factory.calls == [(client_secrets, (GMAIL_MODIFY_SCOPE,))]
    assert flow.local_server_kwargs == {
        "host": "127.0.0.1",
        "port": 0,
        "open_browser": True,
        "access_type": "offline",
        "prompt": "consent",
    }
    assert len(looked_up) == 1
    assert looked_up[0] is credentials
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "refresh_token": REFRESH_TOKEN,
        "scopes": [GMAIL_MODIFY_SCOPE],
    }
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert ACCESS_TOKEN not in output.read_text(encoding="utf-8")
    assert CLIENT_SECRET not in output.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("credentials", "mailbox", "code"),
    [
        (
            FakeCredentials(
                granted_scopes=(GMAIL_MODIFY_SCOPE, "openid"),
            ),
            "dedicated@example.test",
            "oauth_scope_mismatch",
        ),
        (
            FakeCredentials(scopes=(GMAIL_MODIFY_SCOPE, "openid")),
            "dedicated@example.test",
            "oauth_scope_mismatch",
        ),
        (
            FakeCredentials(refresh_token=None),
            "dedicated@example.test",
            "oauth_refresh_token_missing",
        ),
        (
            FakeCredentials(),
            "other@example.test",
            "oauth_account_mismatch",
        ),
    ],
)
def test_oauth_01_rejects_unsafe_authorization_without_writing(
    tmp_path: Path,
    credentials: FakeCredentials,
    mailbox: str,
    code: str,
) -> None:
    output = tmp_path / "refresh-token.json"
    flow = FakeInstalledAppFlow(credentials)

    with pytest.raises(OAuthBootstrapError, match=code) as raised:
        bootstrap_oauth(
            client_secrets=client_file(tmp_path),
            expected_account="dedicated@example.test",
            token_output=output,
            flow_factory=FlowFactorySpy(flow),
            mailbox_lookup=lambda _: mailbox,
        )

    assert not output.exists()
    assert REFRESH_TOKEN not in str(raised.value)
    assert ACCESS_TOKEN not in str(raised.value)
    assert CLIENT_SECRET not in str(raised.value)
    assert "@example.test" not in str(raised.value)


def test_oauth_01_existing_destination_fails_before_authorization(
    tmp_path: Path,
) -> None:
    output = tmp_path / "refresh-token.json"
    output.write_text("keep", encoding="utf-8")
    factory = FlowFactorySpy(FakeInstalledAppFlow(FakeCredentials()))

    with pytest.raises(OAuthBootstrapError, match="oauth_output_exists"):
        bootstrap_oauth(
            client_secrets=client_file(tmp_path),
            expected_account="dedicated@example.test",
            token_output=output,
            flow_factory=factory,
            mailbox_lookup=lambda _: "dedicated@example.test",
        )

    assert output.read_text(encoding="utf-8") == "keep"
    assert factory.calls == []


def test_oauth_01_flow_failure_is_sanitized(
    tmp_path: Path,
) -> None:
    class FailingFlow(FakeInstalledAppFlow):
        def run_local_server(self, **kwargs: object) -> OAuthCredentials:
            raise RuntimeError(f"authorization failed with {REFRESH_TOKEN}")

    with pytest.raises(
        OAuthBootstrapError, match="oauth_authorization_failed"
    ) as raised:
        bootstrap_oauth(
            client_secrets=client_file(tmp_path),
            expected_account="dedicated@example.test",
            token_output=tmp_path / "refresh-token.json",
            flow_factory=FlowFactorySpy(FailingFlow(FakeCredentials())),
            mailbox_lookup=lambda _: "dedicated@example.test",
        )

    assert REFRESH_TOKEN not in str(raised.value)
    assert raised.value.__cause__ is None


def test_oauth_01_mailbox_verification_uses_a_mocked_gmail_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = FakeCredentials()
    profile_calls: list[dict[str, object]] = []
    build_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class ProfileRequest:
        def execute(self) -> dict[str, object]:
            return {"emailAddress": "dedicated@example.test"}

    class ProfileUsers:
        def getProfile(self, **kwargs: object) -> ProfileRequest:
            profile_calls.append(kwargs)
            return ProfileRequest()

    class ProfileService:
        def users(self) -> ProfileUsers:
            return ProfileUsers()

    def fake_build(*args: object, **kwargs: object) -> object:
        build_calls.append((args, kwargs))
        return ProfileService()

    monkeypatch.setattr(oauth_module, "build", fake_build)

    assert oauth_module._lookup_mailbox(credentials) == "dedicated@example.test"
    assert build_calls == [
        (
            ("gmail", "v1"),
            {"credentials": credentials, "cache_discovery": False},
        )
    ]
    assert profile_calls == [{"userId": "me"}]


def test_oauth_01_cli_passes_only_explicit_destinations_and_prints_no_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    client_secrets = client_file(tmp_path)
    output = tmp_path / "refresh-token.json"
    calls: list[tuple[Path, str, Path]] = []

    def runner(
        *, client_secrets: Path, expected_account: str, token_output: Path
    ) -> None:
        calls.append((client_secrets, expected_account, token_output))

    result = main(
        [
            "oauth",
            "bootstrap",
            "--client-secrets",
            str(client_secrets),
            "--expected-account",
            "dedicated@example.test",
            "--token-output",
            str(output),
        ],
        bootstrap_runner=runner,
    )

    assert result == 0
    assert calls == [(client_secrets, "dedicated@example.test", output)]
    captured = capsys.readouterr()
    assert captured.out == "OAuth bootstrap complete.\n"
    assert captured.err == ""
    assert REFRESH_TOKEN not in captured.out


def test_oauth_01_installed_command_help_makes_no_live_call() -> None:
    result = subprocess.run(
        ["uv", "run", "--offline", "alza-ai", "oauth", "bootstrap", "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "--client-secrets" in result.stdout
    assert "--expected-account" in result.stdout
    assert "--token-output" in result.stdout
    assert result.stderr == ""
