import asyncio
from collections.abc import Awaitable, Callable
from random import random as random_unit

MAX_ATTEMPTS = 2
FULL_JITTER_CAP_SECONDS = 0.25


class RetryBudgetExceeded(Exception):
    pass


class BoundedRetry:
    def __init__(
        self,
        *,
        random: Callable[[], float] = random_unit,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._random = random
        self._sleeper = sleeper

    async def run[T](
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        retry_if: Callable[[Exception], bool],
        remaining_seconds: Callable[[], float] | None = None,
    ) -> T:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            _require_budget(remaining_seconds)
            try:
                return await operation()
            except Exception as error:
                if attempt == MAX_ATTEMPTS or not retry_if(error):
                    raise

            delay = FULL_JITTER_CAP_SECONDS * self._random()
            if not 0 <= delay <= FULL_JITTER_CAP_SECONDS:
                raise ValueError("retry_random_invalid")
            if remaining_seconds is not None and delay >= remaining_seconds():
                raise RetryBudgetExceeded
            await self._sleeper(delay)

        raise AssertionError("unreachable")


def _require_budget(remaining_seconds: Callable[[], float] | None) -> None:
    if remaining_seconds is not None and remaining_seconds() <= 0:
        raise RetryBudgetExceeded
