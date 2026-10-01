"""Fake OpenAI client and clock so tests never touch the network or really sleep."""
from __future__ import annotations

from types import SimpleNamespace

import httpx2 as httpx  # the openai SDK's HTTP library
from openai import InternalServerError, RateLimitError

_REQ = httpx.Request("POST", "https://fake.local/v1/chat/completions")


def rate_limit_error() -> RateLimitError:
    return RateLimitError("quota", response=httpx.Response(429, request=_REQ), body=None)


def server_error() -> InternalServerError:
    return InternalServerError("overloaded", response=httpx.Response(503, request=_REQ), body=None)


def response(text: str, tokens_in: int = 1000, tokens_out: int = 200, total: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=tokens_in, completion_tokens=tokens_out,
                              total_tokens=total if total is not None else tokens_in + tokens_out),
    )


class FakeClient:
    """Returns (or raises) the queued outputs in order. Strings become responses."""

    def __init__(self, outputs: list):
        self.outputs = list(outputs)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return response(out) if isinstance(out, str) else out


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def daily_quota_error() -> RateLimitError:
    msg = ("Quota exceeded for metric: generate_content_free_tier_requests, limit: 20 "
           "'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'")
    return RateLimitError(msg, response=httpx.Response(429, request=_REQ), body=None)


def per_minute_error(delay_s: int = 30) -> RateLimitError:
    msg = f"'quotaId': 'GenerateRequestsPerMinutePerProjectPerModel-FreeTier' Please retry in {delay_s}.5s."
    return RateLimitError(msg, response=httpx.Response(429, request=_REQ), body=None)


class ScriptedLLM:
    """Stands in for story.llm.LLM: canned outputs per step (prefix match). Values, lists (consumed) or callables."""

    def __init__(self, handlers: dict, logger=None):
        from story.llm import QuotaTracker
        self.handlers = handlers
        self.calls: list[dict] = []
        self.decisions: list[str] = []
        self.logger = logger or SimpleNamespace(story_id=None, log=lambda **kw: None)
        self.quota = QuotaTracker(None)

    def chain(self, tier):
        return ["fake-model"]

    def decision(self, decision: str, ep=None, **extra):
        self.decisions.append(decision)
        self.logger.log(ep=ep, step="decision", decision=decision)

    def call(self, step, tier, messages, *, json_schema=None, ep=None, budget=None, prompt_version=None, **_):
        from story.llm import Result
        self.calls.append({"step": step, "tier": tier, "messages": messages, "ep": ep})
        key = next((k for k in self.handlers if step.startswith(k)), None)
        if key is None:
            raise AssertionError(f"no scripted output for step {step!r}")
        h = self.handlers[key]
        out = h.pop(0) if isinstance(h, list) else (h(messages, ep) if callable(h) else h)
        if budget:
            budget.add(0.001)
        parsed = None
        if json_schema is not None:
            parsed = out if isinstance(out, json_schema) else json_schema.model_validate(out)
            out = parsed.model_dump_json()
        return Result(text=out, parsed=parsed, model="fake", tokens_in=100, tokens_out=50, cost_usd=0.0,
                      list_price_usd=0.001, latency_ms=1, attempts=1)
