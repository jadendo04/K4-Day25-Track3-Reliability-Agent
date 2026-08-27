from __future__ import annotations

from dataclasses import dataclass

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.providers import FakeLLMProvider, ProviderError


@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None = None


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers."""

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, CircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
    ):
        self.providers = providers
        self.breakers = breakers
        self.cache = cache

    def complete(self, prompt: str) -> GatewayResponse:
        """Return a reliable response or a static fallback.

        TODO(student): Implement the full request routing pipeline:

        1. CACHE CHECK — if self.cache is not None:
           - Call self.cache.get(prompt) → (cached_text, score)
           - If cached_text is not None, return GatewayResponse with:
             route=f"cache_hit:{score:.2f}", cache_hit=True, latency=0, cost=0

        2. PROVIDER FALLBACK CHAIN — iterate self.providers in order:
           - Get the circuit breaker: self.breakers[provider.name]
           - Try breaker.call(provider.complete, prompt)
           - On success:
             a. Store in cache: self.cache.set(prompt, response.text, {"provider": provider.name})
             b. Determine route: "primary" if first provider, else "fallback"
             c. Return GatewayResponse with provider info, latency, cost
           - On ProviderError or CircuitOpenError: save error, continue to next provider

        3. STATIC FALLBACK — if all providers fail:
           - Return GatewayResponse with:
             text="The service is temporarily degraded. Please try again soon."
             route="static_fallback", error=last_error

        BONUS TODO: Add cost budget tracking — if cumulative cost exceeds a threshold,
        skip expensive providers and route to cache or cheaper fallback.
        """
        if self.cache is not None:
            cached_text, score = self.cache.get(prompt)
            if cached_text is not None:
                return GatewayResponse(
                    text=cached_text,
                    route=f"cache_hit:{score:.2f}",
                    provider=None,
                    cache_hit=True,
                    latency_ms=0.0,
                    estimated_cost=0.0,
                )

        last_error: str | None = None
        for i, provider in enumerate(self.providers):
            breaker = self.breakers[provider.name]
            try:
                response = breaker.call(provider.complete, prompt)
            except (ProviderError, CircuitOpenError) as exc:
                last_error = str(exc)
                continue

            if self.cache is not None:
                self.cache.set(prompt, response.text, {"provider": provider.name})

            route = "primary" if i == 0 else "fallback"
            return GatewayResponse(
                text=response.text,
                route=route,
                provider=provider.name,
                cache_hit=False,
                latency_ms=response.latency_ms,
                estimated_cost=response.estimated_cost,
            )

        return GatewayResponse(
            text="The service is temporarily degraded. Please try again soon.",
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=0.0,
            estimated_cost=0.0,
            error=last_error,
        )
