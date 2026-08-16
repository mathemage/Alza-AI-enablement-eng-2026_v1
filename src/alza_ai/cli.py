import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, cast

from alza_ai.oauth import OAuthBootstrapError, bootstrap_oauth


class OAuthBootstrapRunner(Protocol):
    def __call__(
        self, *, client_secrets: Path, expected_account: str, token_output: Path
    ) -> None: ...


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="alza-ai")
    commands = parser.add_subparsers(dest="command", required=True)
    oauth = commands.add_parser("oauth")
    oauth_commands = oauth.add_subparsers(dest="oauth_command", required=True)
    bootstrap = oauth_commands.add_parser("bootstrap")
    bootstrap.add_argument("--client-secrets", required=True)
    bootstrap.add_argument("--expected-account", required=True)
    bootstrap.add_argument("--token-output", required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    bootstrap_runner: OAuthBootstrapRunner = bootstrap_oauth,
) -> int:
    arguments = _parser().parse_args(argv)
    client_secrets = Path(cast(str, arguments.client_secrets))
    expected_account = cast(str, arguments.expected_account)
    token_output = Path(cast(str, arguments.token_output))
    try:
        bootstrap_runner(
            client_secrets=client_secrets,
            expected_account=expected_account,
            token_output=token_output,
        )
    except OAuthBootstrapError as error:
        print(f"OAuth bootstrap failed: {error}", file=sys.stderr)
        return 1
    print("OAuth bootstrap complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
