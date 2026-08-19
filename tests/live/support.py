import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast


class LiveFailure(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class LiveConfig:
    account: str
    project_id: str
    project_number: str
    billing_account_id: str
    region: str
    mailbox: str
    service_name: str
    budget_currency: str
    monthly_budget_amount: int
    cost_approved: bool
    oauth_consent_status: str | None
    oauth_testing_risk_approved: bool
    oauth_client_path: Path | None
    oauth_token_path: Path | None
    mailbox_key: str | None
    live_sender: str | None
    live_cases: Mapping[str, str]

    @classmethod
    def load(cls, path: Path) -> LiveConfig:
        if not path.is_file() or path.parent.name != "credentials":
            raise LiveFailure("operator_config_invalid")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            raise LiveFailure("operator_config_invalid") from None
        if not isinstance(value, dict):
            raise LiveFailure("operator_config_invalid")
        required_strings = {
            key: _required_string(value, key)
            for key in (
                "account",
                "project_id",
                "project_number",
                "billing_account_id",
                "region",
                "mailbox",
                "service_name",
                "budget_currency",
            )
        }
        amount = value.get("monthly_budget_amount")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise LiveFailure("operator_config_invalid")
        cost_approved = value.get("cost_approved")
        if cost_approved is not True:
            raise LiveFailure("cost_approval_missing")
        consent_status = _optional_string(value, "oauth_consent_status")
        risk_approved = value.get("oauth_testing_risk_approved", False)
        if not isinstance(risk_approved, bool):
            raise LiveFailure("operator_config_invalid")
        raw_cases = value.get("live_cases", {})
        if not isinstance(raw_cases, dict) or not all(
            isinstance(key, str) and key and isinstance(item, str) and item
            for key, item in raw_cases.items()
        ):
            raise LiveFailure("operator_config_invalid")
        return cls(
            **required_strings,
            monthly_budget_amount=amount,
            cost_approved=cost_approved,
            oauth_consent_status=consent_status,
            oauth_testing_risk_approved=risk_approved,
            oauth_client_path=_optional_path(value, "oauth_client_path", path.parent),
            oauth_token_path=_optional_path(value, "oauth_token_path", path.parent),
            mailbox_key=_optional_string(value, "mailbox_key"),
            live_sender=_optional_string(value, "live_sender"),
            live_cases=cast(dict[str, str], raw_cases),
        )

    def require_gmail(self) -> None:
        if self.oauth_consent_status not in {"testing", "production"}:
            raise LiveFailure("oauth_consent_unconfirmed")
        if (
            self.oauth_consent_status == "testing"
            and not self.oauth_testing_risk_approved
        ):
            raise LiveFailure("oauth_testing_risk_unapproved")
        if (
            self.oauth_client_path is None
            or self.oauth_token_path is None
            or self.mailbox_key is None
            or self.live_sender is None
            or set(self.live_cases) != {"plain", "pdf", "audio", "image", "current"}
        ):
            raise LiveFailure("gmail_operator_config_incomplete")
        if not self.oauth_client_path.is_file() or not self.oauth_token_path.is_file():
            raise LiveFailure("gmail_credentials_missing")


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str


def gcloud(arguments: Sequence[str]) -> CommandResult:
    try:
        result = subprocess.run(
            ["gcloud", *arguments, "--quiet"],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except OSError, subprocess.TimeoutExpired:
        raise LiveFailure("gcloud_unavailable") from None
    return CommandResult(result.returncode, result.stdout)


def gcloud_json(arguments: Sequence[str], failure_code: str) -> object:
    result = gcloud([*arguments, "--format=json"])
    if result.returncode != 0:
        raise LiveFailure(failure_code)
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise LiveFailure("gcloud_response_invalid") from None


def require(condition: bool, code: str) -> None:
    if not condition:
        raise LiveFailure(code)


def mapping(
    value: object, code: str = "gcloud_response_invalid"
) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise LiveFailure(code)
    return value


def sequence(value: object, code: str = "gcloud_response_invalid") -> Sequence[object]:
    if not isinstance(value, list):
        raise LiveFailure(code)
    return value


def _required_string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise LiveFailure("operator_config_invalid")
    return item


def _optional_string(value: Mapping[str, object], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str) or not item:
        raise LiveFailure("operator_config_invalid")
    return item


def _optional_path(
    value: Mapping[str, object], key: str, directory: Path
) -> Path | None:
    item = _optional_string(value, key)
    if item is None:
        return None
    path = Path(item)
    if not path.is_absolute():
        path = directory / path
    return path
