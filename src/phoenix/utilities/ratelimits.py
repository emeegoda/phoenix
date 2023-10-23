import asyncio
import sys
import time
from collections import defaultdict
from functools import wraps
from typing import Any, Awaitable, Callable, Dict, Optional, TypeVar, Union, cast

if sys.version_info < (3, 10):
    from typing_extensions import ParamSpec
else:
    from typing import ParamSpec

Numeric = Union[int, float]
P = ParamSpec("P")
T = TypeVar("T")
A = TypeVar("A", bound=Callable[..., Awaitable[Any]])


class UnavailableTokensError(Exception):
    pass


class TokenLimiter:
    """
    A simple in-memory rate-limiter implemented using the leaky bucket algorithm.
    """

    def __init__(
        self,
        per_minute_rate: Numeric,
        starting_tokens: Numeric,
        max_tokens: Numeric,
        rate_multiplier: Numeric = 1,
    ):
        self.rate_multiplier = rate_multiplier
        self.rate = self._per_second_rate(per_minute_rate)
        self.tokens = starting_tokens
        self.max_tokens = max_tokens
        self.created = time.time()
        self.last_checked = self.created
        self.total_tokens: Numeric = 0

    def _per_second_rate(self, per_minute_rate: Numeric) -> float:
        return round(per_minute_rate / 60, 3) * self.rate_multiplier

    def refresh_limit(self, per_minute_rate: Numeric, max_tokens: Numeric) -> None:
        new_rate = self._per_second_rate(per_minute_rate)
        if self.rate != new_rate:
            self.rate = new_rate
            self.tokens = 0  # reset tokens as conservatively as possible
            self.max_tokens = max_tokens
            self.last_checked = time.time()

    def available_tokens(self) -> float:
        time_since_last_checked = time.time() - self.last_checked
        return min(self.max_tokens, self.rate * time_since_last_checked + self.tokens)

    def spend_tokens_if_available(self, token_cost: Numeric) -> None:
        if token_cost > self.available_tokens():
            raise UnavailableTokensError
        now = time.time()
        current_tokens = min(self.max_tokens, self.tokens + (now - self.last_checked) * self.rate)
        self.tokens = current_tokens - token_cost
        self.last_checked = now
        self.total_tokens += token_cost

    def spend_tokens(self, token_cost: Numeric) -> None:
        self.tokens -= token_cost
        self.total_tokens += token_cost

    def wait_for_then_spend_available_tokens(self, token_cost: Numeric) -> None:
        MAX_WAIT_TIME = 5 * 60  # 5 minutes
        start = time.time()
        while (time.time() - start) < MAX_WAIT_TIME:
            try:
                self.spend_tokens_if_available(token_cost)
                break
            except UnavailableTokensError:
                time.sleep(1 / self.rate)
                continue

    async def async_wait_for_then_spend_available_tokens(self, token_cost: Numeric) -> None:
        MAX_WAIT_TIME = 5 * 60
        start = time.time()
        while (time.time() - start) < MAX_WAIT_TIME:
            try:
                self.spend_tokens_if_available(token_cost)
                break
            except UnavailableTokensError:
                await asyncio.sleep(1 / self.rate)
                continue

    def effective_rate(self) -> Numeric:
        return self.total_tokens / (time.time() - self.created)


class LimitStore:
    """
    A singleton store for collections of rate limits grouped by service key.

    LimitStore is a singleton because we want to share rate limits across all calls controlled by
    phoenix's rate limiting mechanism.
    """

    _singleton = None

    def __new__(cls) -> "LimitStore":
        if not cls._singleton:
            cls._singleton = super().__new__(cls)
            cls._singleton._rate_limits = defaultdict(dict)
        return cls._singleton

    _rate_limits: Dict[str, Dict[str, TokenLimiter]]

    def set_rate_limit(
        self,
        key: str,
        limit_type: str,
        per_minute_rate_limit: Numeric,
        enforcement_window_minutes: Optional[int] = None,
    ) -> None:
        # default to 1 minute enforcement window
        enforcement_window_minutes = (
            enforcement_window_minutes if enforcement_window_minutes is not None else 1
        )
        max_tokens = per_minute_rate_limit * enforcement_window_minutes
        if limits := self._rate_limits[key]:
            if limit := limits.get(limit_type):
                limit.refresh_limit(per_minute_rate_limit, max_tokens)
                return
        limits[limit_type] = TokenLimiter(
            per_minute_rate_limit,
            0,
            max_tokens,
        )

    def get_rate_limits(self, key: str) -> Dict[str, TokenLimiter]:
        return self._rate_limits[key]

    def wait_for_rate_limits(self, key: str, rate_limit_costs: Dict[str, Numeric]) -> None:
        rate_limits = self._rate_limits[key]
        for limit_type, cost in rate_limit_costs.items():
            if limit := rate_limits.get(limit_type):
                limit.wait_for_then_spend_available_tokens(cost)

    async def async_wait_for_rate_limits(
        self, key: str, rate_limit_costs: Dict[str, Numeric]
    ) -> None:
        rate_limits = self._rate_limits[key]
        for limit_type, cost in rate_limit_costs.items():
            if limit := rate_limits.get(limit_type):
                await limit.async_wait_for_then_spend_available_tokens(cost)

    def spend_rate_limits(self, key: str, rate_limit_costs: Dict[str, Numeric]) -> None:
        rate_limits = self._rate_limits[key]
        for limit_type, cost in rate_limit_costs.items():
            if limit := rate_limits.get(limit_type):
                limit.spend_tokens(cost)


class OpenAIRateLimiter:
    def __init__(self, api_key: str) -> None:
        self._store = LimitStore()
        self._api_key = api_key

    def key(self, model_name: str) -> str:
        return f"openai:{self._api_key}:{model_name}"

    def set_rate_limits(
        self, model_name: str, request_rate_limit: Numeric, token_rate_limit: Numeric
    ) -> None:
        self._store.set_rate_limit(self.key(model_name), "requests", request_rate_limit)
        self._store.set_rate_limit(self.key(model_name), "tokens", token_rate_limit)

    def limit(
        self, model_name: str, token_cost: Numeric
    ) -> Callable[[Callable[P, T]], Callable[P, T]]:
        def rate_limit_decorator(fn: Callable[P, T]) -> Callable[P, T]:
            @wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> T:
                self._store.wait_for_rate_limits(
                    self.key(model_name), {"requests": 1, "tokens": token_cost}
                )
                result: T = fn(*args, **kwargs)
                return result

            return wrapper

        return rate_limit_decorator

    def alimit(
        self,
        model_name: str,
        input_cost_fn: Callable[..., Numeric],
        response_cost_fn: Callable[..., Numeric],
    ) -> Callable[[A], A]:
        def rate_limit_decorator(fn: A) -> A:
            @wraps(fn)
            async def wrapper(*args: Any, **kwargs: Any) -> T:
                key = self.key(model_name)
                await self._store.async_wait_for_rate_limits(
                    key, {"requests": 1, "tokens": input_cost_fn(*args, **kwargs)}
                )
                result: T = await fn(*args, **kwargs)
                self._store.spend_rate_limits(key, {"tokens": response_cost_fn(result)})
                return result

            return cast(A, wrapper)

        return rate_limit_decorator
