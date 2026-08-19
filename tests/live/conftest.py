from pathlib import Path

import pytest

from tests.live.support import LiveConfig, LiveFailure


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--live-config",
        action="store",
        default=None,
        help="Ignored JSON operator configuration for opt-in live acceptance.",
    )


@pytest.fixture(scope="session")
def live_config(request: pytest.FixtureRequest) -> LiveConfig:
    value = request.config.getoption("--live-config")
    if value is None:
        pytest.skip("live_acceptance_not_requested")
    try:
        return LiveConfig.load(Path(str(value)))
    except LiveFailure as error:
        pytest.fail(error.code, pytrace=False)
