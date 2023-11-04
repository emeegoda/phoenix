import asyncio
import time
from functools import wraps
from math import exp
from typing import Any, Awaitable, Callable, Type, TypeVar, cast

from typing_extensions import ParamSpec

ParameterSpec = ParamSpec("ParameterSpec")
GenericType = TypeVar("GenericType")
AsyncCallable = TypeVar("AsyncCallable", bound=Callable[..., Awaitable[Any]])


class UnavailableTokensError(Exception):
    pass


class AdaptiveTokenBucket:
    """
    An adaptive rate-limiter that adjusts the rate based on the number of rate limit errors.

    This rate limiter does not need to know the exact rate limit. Instead, it starts with a high
    rate and reduces it whenever a rate limit error occurs. The rate is increased slowly over time
    if no further errors occur.

    Args:
    initial_per_second_request_rate (float): The allowed request rate.
    maximum_per_second_request_rate (float): The maximum allowed request rate.
    enforcement_window_minutes (float): The time window over which the rate limit is enforced.
    rate_reduction_factor (float): Multiplier used to reduce the rate limit after an error.
    rate_increase_factor (float): Exponential factor increasing the rate limit over time.
    cooldown_seconds (float): The minimum time before allowing the rate limit to decrease again.
    """

    def __init__(
        self,
        initial_per_second_request_rate: float,
        maximum_per_second_request_rate: float = 1000,
        enforcement_window_minutes: float = 1,
        rate_reduction_factor: float = 0.5,
        rate_increase_factor: float = 0.01,
        cooldown_seconds: float = 5,
    ):
        now = time.time()
        self.rate = initial_per_second_request_rate
        self.maximum_rate = maximum_per_second_request_rate
        self.rate_reduction_factor = rate_reduction_factor
        self.enforcement_window = enforcement_window_minutes * 60
        self.rate_increase_factor = rate_increase_factor
        self.cooldown = cooldown_seconds
        self.last_rate_update = now
        self.last_checked = now
        self.last_error = now - self.cooldown
        self.tokens = 0.0

    def _increase_rate(self) -> None:
        time_since_last_update = time.time() - self.last_rate_update
        self.rate *= exp(self.rate_increase_factor * time_since_last_update)
        self.rate = min(self.rate, self.maximum_rate)
        self.last_rate_update = time.time()

    def on_rate_limit_error(self) -> None:
        now = time.time()
        if self.cooldown > (now - self.last_error):
            # don't reduce the rate if we just had a rate limit error
            return
        self.rate *= self.rate_reduction_factor
        self.rate = max(self.rate, 1 / self.enforcement_window)
        self.last_rate_update = now
        self.last_error = now

    def max_tokens(self) -> float:
        return self.rate * self.enforcement_window

    def available_requests(self) -> float:
        now = time.time()
        time_since_last_checked = time.time() - self.last_checked
        self.tokens = min(self.max_tokens(), self.rate * time_since_last_checked + self.tokens)
        self.last_checked = now
        return self.tokens

    def make_request_if_ready(self) -> None:
        if self.available_requests() <= 1:
            raise UnavailableTokensError
        self.tokens -= 1

    def wait_until_ready(
        self,
        max_wait_time: float = 300,
    ) -> None:
        start = time.time()
        while (time.time() - start) < max_wait_time:
            try:
                self._increase_rate()
                self.make_request_if_ready()
                break
            except UnavailableTokensError:
                time.sleep(0.1 / self.rate)
                continue

    async def async_wait_until_ready(
        self,
        max_wait_time: float = 300,
    ) -> None:
        start = time.time()
        while (time.time() - start) < max_wait_time:
            try:
                self._increase_rate()
                self.make_request_if_ready()
                break
            except UnavailableTokensError:
                await asyncio.sleep(0.1 / self.rate)
                continue


class RateLimiter:
    def __init__(
        self,
        rate_limit_error: Type[BaseException],
        max_rate_limit_retries: int = 10,
        initial_per_second_request_rate: float = 200,
        maximum_per_second_request_rate: float = 1000,
        enforcement_window_minutes: float = 1,
        rate_reduction_factor: float = 0.5,
        rate_increase_factor: float = 0.01,
        cooldown_seconds: float = 5,
    ) -> None:
        self._rate_limit_error = rate_limit_error
        self._max_rate_limit_retries = max_rate_limit_retries
        self._throttler = AdaptiveTokenBucket(
            initial_per_second_request_rate=initial_per_second_request_rate,
            maximum_per_second_request_rate=maximum_per_second_request_rate,
            enforcement_window_minutes=enforcement_window_minutes,
            rate_reduction_factor=rate_reduction_factor,
            rate_increase_factor=rate_increase_factor,
            cooldown_seconds=cooldown_seconds,
        )

    def limit(
        self, fn: Callable[ParameterSpec, GenericType]
    ) -> Callable[ParameterSpec, GenericType]:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> GenericType:
            for _attempt in range(self._max_rate_limit_retries):
                try:
                    self._throttler.wait_until_ready()
                    result: GenericType = fn(*args, **kwargs)
                    return result
                except self._rate_limit_error:
                    self._throttler.on_rate_limit_error()
                    continue

        return wrapper

    def alimit(self, fn: AsyncCallable) -> AsyncCallable:
        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> GenericType:
            for _attempt in range(self._max_rate_limit_retries)
                try:
                    await self._throttler.async_wait_until_ready()
                    result: GenericType = await fn(*args, **kwargs)
                    return result
                except self._rate_limit_error:
                    self._throttler.on_rate_limit_error()
                    continue

        return cast(AsyncCallable, wrapper)
