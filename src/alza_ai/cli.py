import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, cast

import google.cloud.firestore as firestore  # noqa: PLR0402

from alza_ai.oauth import OAuthBootstrapError, bootstrap_oauth
from alza_ai.processing import SenderPolicyStore, normalize_sender_entry


class OAuthBootstrapRunner(Protocol):
    def __call__(
        self, *, client_secrets: Path, expected_account: str, token_output: Path
    ) -> None: ...


class SenderPolicyAdmin(Protocol):
    def allowed_senders(self) -> tuple[str, ...]: ...

    def set_allowed_senders(self, entries: Sequence[str]) -> None: ...


class SenderPolicyStoreFactory(Protocol):
    def __call__(self, project: str) -> SenderPolicyAdmin: ...


def open_sender_policy_store(project: str) -> SenderPolicyAdmin:
    return SenderPolicyStore(firestore.Client(project=project))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="alza-ai")
    commands = parser.add_subparsers(dest="command", required=True)
    oauth = commands.add_parser("oauth")
    oauth_commands = oauth.add_subparsers(dest="oauth_command", required=True)
    bootstrap = oauth_commands.add_parser("bootstrap")
    bootstrap.add_argument("--client-secrets", required=True)
    bootstrap.add_argument("--expected-account", required=True)
    bootstrap.add_argument("--token-output", required=True)
    allowlist = commands.add_parser("allowlist")
    allowlist_commands = allowlist.add_subparsers(
        dest="allowlist_command", required=True
    )
    for name in ("list", "add", "remove"):
        command = allowlist_commands.add_parser(name)
        command.add_argument("--project", required=True)
        if name != "list":
            command.add_argument("entry")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    bootstrap_runner: OAuthBootstrapRunner = bootstrap_oauth,
    sender_policy_store_factory: SenderPolicyStoreFactory = open_sender_policy_store,
) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "allowlist":
        return _allowlist(arguments, sender_policy_store_factory)
    return _oauth_bootstrap(arguments, bootstrap_runner)


def _oauth_bootstrap(
    arguments: argparse.Namespace, bootstrap_runner: OAuthBootstrapRunner
) -> int:
    try:
        bootstrap_runner(
            client_secrets=Path(cast(str, arguments.client_secrets)),
            expected_account=cast(str, arguments.expected_account),
            token_output=Path(cast(str, arguments.token_output)),
        )
    except OAuthBootstrapError as error:
        print(f"OAuth bootstrap failed: {error}", file=sys.stderr)
        return 1
    print("OAuth bootstrap complete.")
    return 0


def _allowlist(arguments: argparse.Namespace, factory: SenderPolicyStoreFactory) -> int:
    store = factory(cast(str, arguments.project))
    entries = dict.fromkeys(
        entry
        for value in store.allowed_senders()
        if (entry := normalize_sender_entry(value)) is not None
    )
    command = cast(str, arguments.allowlist_command)
    if command != "list":
        requested = cast(str, arguments.entry)
        entry = normalize_sender_entry(requested)
        if entry is None:
            print(f"Invalid allowlist entry: {requested}", file=sys.stderr)
            return 1
        if command == "remove" and entry not in entries:
            print(f"Allowlist entry not present: {requested}", file=sys.stderr)
            return 1
        if command == "add":
            entries[entry] = None
        else:
            del entries[entry]
        store.set_allowed_senders(tuple(entries))
    for entry in entries:
        print(entry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
