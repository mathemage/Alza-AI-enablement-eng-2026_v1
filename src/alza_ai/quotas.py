from collections.abc import Mapping
from dataclasses import dataclass

MAX_ATTACHMENT_ANALYSIS_CALLS = 5
MAX_REPLY_GENERATION_CALLS = 1
MAX_SEARCH_CALLS = 1
MAX_REPLY_OUTPUT_TOKENS = 2_048


class QuotaConfigurationError(ValueError):
    def __init__(self) -> None:
        self.code = "runtime_quota_invalid"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class RuntimeQuotas:
    attachment_analysis_calls: int = MAX_ATTACHMENT_ANALYSIS_CALLS
    reply_generation_calls: int = MAX_REPLY_GENERATION_CALLS
    search_calls: int = MAX_SEARCH_CALLS
    reply_output_tokens: int = MAX_REPLY_OUTPUT_TOKENS

    @classmethod
    def load(cls, environ: Mapping[str, str]) -> RuntimeQuotas:
        return cls(
            attachment_analysis_calls=_ceiling(
                environ,
                "MAX_ATTACHMENT_ANALYSIS_CALLS",
                1,
                MAX_ATTACHMENT_ANALYSIS_CALLS,
            ),
            reply_generation_calls=_ceiling(
                environ, "MAX_REPLY_GENERATION_CALLS", 0, MAX_REPLY_GENERATION_CALLS
            ),
            search_calls=_ceiling(environ, "MAX_SEARCH_CALLS", 0, MAX_SEARCH_CALLS),
            reply_output_tokens=_ceiling(
                environ, "MAX_REPLY_OUTPUT_TOKENS", 1, MAX_REPLY_OUTPUT_TOKENS
            ),
        )


DEFAULT_QUOTAS = RuntimeQuotas()


def _ceiling(
    environ: Mapping[str, str],
    key: str,
    minimum: int,
    maximum: int,
) -> int:
    raw = environ.get(key)
    if raw is None:
        return maximum
    try:
        value = int(raw.strip())
    except ValueError:
        raise QuotaConfigurationError from None
    if not minimum <= value <= maximum:
        raise QuotaConfigurationError
    return value
