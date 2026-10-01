"""The single entry point for LLM calls: rate limiting, retries, JSON parsing, cost, budget, JSONL logging."""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from string import Template
from typing import Any, Callable, Literal
from uuid import uuid4

from openai import APIConnectionError, APIStatusError, InternalServerError, OpenAI, RateLimitError
from pydantic import BaseModel

from .config import PROMPTS_DIR, Settings, price_usd
from .models import utcnow

Tier = Literal["writer", "fast"]
RETRYABLE = (RateLimitError, InternalServerError, APIConnectionError)  # 429, 5xx, network/timeout
# Output tokens assumed when checking the budget before a call. Thinking models spend output tokens on reasoning too.
OUTPUT_ESTIMATE = {"writer": 2500, "fast": 1500}


class LLMError(Exception):
    pass


class BudgetExceeded(LLMError):
    pass


class LLMParseError(LLMError):
    pass


class QuotaExhausted(LLMError):
    pass


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def load_prompt(name: str) -> tuple[str, str]:
    """Load prompts/<name>.md. A first line like `<!-- version: draft_v2 -->` sets the logged version."""
    text = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    m = re.match(r"\s*<!--\s*version:\s*(\S+)\s*-->\s*\n?", text)
    return (text[m.end():], m.group(1)) if m else (text, f"{name}_v0")


def render_prompt(name: str, **values: Any) -> tuple[str, str]:
    """Load a prompt and fill its $placeholders. Returns (text, version)."""
    text, version = load_prompt(name)
    return Template(text).safe_substitute({k: str(v) for k, v in values.items()}), version


@dataclass
class EpisodeBudget:
    """Spend for one episode in list-price USD. Actual spend is $0 on the free tier, so the cap uses list price."""

    cap_usd: float
    spent_usd: float = 0.0

    @property
    def remaining_usd(self) -> float:
        return self.cap_usd - self.spent_usd

    def check(self, estimate_usd: float, step: str) -> None:
        if self.spent_usd + estimate_usd > self.cap_usd:
            raise BudgetExceeded(
                f"{step}: estimated ${estimate_usd:.4f} would exceed the episode cap "
                f"(${self.spent_usd:.4f} of ${self.cap_usd:.2f} spent)"
            )

    def add(self, usd: float) -> None:
        self.spent_usd += usd


@dataclass
class Result:
    text: str
    parsed: BaseModel | None
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    list_price_usd: float
    latency_ms: int
    attempts: int


class RunLogger:
    """Appends one JSON object per line to runs/<story_id>/log.jsonl."""

    def __init__(self, runs_dir: Path, story_id: str | None = None, run_id: str | None = None):
        self.runs_dir = Path(runs_dir)
        self.story_id = story_id
        self.run_id = run_id or uuid4().hex[:8]

    @property
    def path(self) -> Path:
        return self.runs_dir / (self.story_id or "_no_story") / "log.jsonl"

    def log(self, **fields: Any) -> dict:
        record = {"ts": utcnow(), "run_id": self.run_id, "story_id": self.story_id, **fields}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record


def _usage(resp: Any) -> tuple[int, int]:
    u = getattr(resp, "usage", None)
    if u is None:
        return 0, 0
    tokens_in = u.prompt_tokens or 0
    tokens_out = u.completion_tokens or 0
    # Gemini's OpenAI endpoint leaves reasoning tokens out of completion_tokens but counts them in total_tokens.
    # They are billed as output, so count them.
    total = getattr(u, "total_tokens", None) or 0
    if total > tokens_in + tokens_out:
        tokens_out = total - tokens_in
    return tokens_in, tokens_out


def parse_json(text: str, schema: type[BaseModel]) -> BaseModel:
    """Parse model output into `schema`, tolerating ```json fences and text around the object."""
    s = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    elif not s.startswith("{"):
        start, end = s.find("{"), s.rfind("}")
        if start != -1 and end > start:
            s = s[start:end + 1]
    return schema.model_validate_json(s)


def _with_schema(messages: list[dict], schema: type[BaseModel]) -> list[dict]:
    instruction = (
        "Respond with a single JSON object and nothing else. It must validate against this JSON Schema:\n"
        + json.dumps(schema.model_json_schema())
    )
    msgs = [dict(m) for m in messages]
    if msgs and msgs[0]["role"] == "system":
        msgs[0]["content"] = msgs[0]["content"] + "\n\n" + instruction
    else:
        msgs.insert(0, {"role": "system", "content": instruction})
    return msgs


class QuotaTracker:
    """Remembers which models used up their free daily quota, until the next reset.

    Google resets free-tier daily quotas at midnight Pacific; 08:00 UTC is that or an hour after, so it is safe.
    Persisted so a new process does not waste a request rediscovering it. Quotas belong to the API key's project,
    so entries are kept per key (under a short SHA-256 fingerprint, never the key itself).
    """

    def __init__(self, path: Path | None, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 key_id: str = "default"):
        self.path, self.now, self.key_id = path, now, key_id
        self._all: dict[str, dict[str, str]] = {}
        if path and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                # Files from before per-key tracking were flat {model: until}; their key is unknown, so drop them.
                self._all = {k: v for k, v in data.items() if isinstance(v, dict)}
            except (ValueError, OSError):
                self._all = {}
        self.until: dict[str, str] = self._all.setdefault(key_id, {})

    def next_reset(self) -> datetime:
        now = self.now()
        reset = now.replace(hour=8, minute=0, second=0, microsecond=0)
        return reset if reset > now else reset + timedelta(days=1)

    def exhausted(self, model: str) -> bool:
        until = self.until.get(model)
        return bool(until) and datetime.fromisoformat(until) > self.now()

    def mark(self, model: str) -> datetime:
        reset = self.next_reset()
        self.until[model] = reset.isoformat(timespec="seconds")
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._all, indent=2), encoding="utf-8")
        return reset


def _is_daily_quota(e: Exception) -> bool:
    return isinstance(e, RateLimitError) and "PerDay" in str(e)


def _retry_delay(e: Exception) -> float:
    m = re.search(r"retry in ([\d.]+)s", str(e), re.IGNORECASE) or re.search(r"retryDelay'?:\s*'(\d+)s", str(e))
    return float(m.group(1)) if m else 0.0


class LLM:
    """Model tiers come from WRITER_MODEL / FAST_MODEL; each may be a comma-separated fallback chain."""

    def __init__(self, settings: Settings, *, client: Any = None, logger: RunLogger | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 quota: QuotaTracker | None = None, max_attempts: int = 8, backoff_base_s: float = 4.0,
                 switch_after_transient: int = 2):
        self.settings = settings
        self._client = client  # created on first use, so a session can open (e.g. to browse) without a key
        self.logger = logger or RunLogger(settings.runs_dir)
        self.quota = quota or QuotaTracker(Path(settings.runs_dir) / "quota.json",
                                           key_id=hashlib.sha256(settings.api_key.encode()).hexdigest()[:12])
        self.sleep, self.clock = sleep, clock
        self.max_attempts, self.backoff_base_s = max_attempts, backoff_base_s
        self.switch_after_transient = switch_after_transient
        self._last_call_at: float | None = None

    @property
    def client(self) -> Any:
        if self._client is None:
            if not self.settings.api_key:
                raise LLMError("LLM_API_KEY is not set: add it in the web UI (⚙ Settings) or in .env")
            self._client = OpenAI(base_url=self.settings.base_url, api_key=self.settings.api_key,
                                  max_retries=0, timeout=240)
        return self._client

    def chain(self, tier: Tier) -> list[str]:
        raw = self.settings.writer_model if tier == "writer" else self.settings.fast_model
        models = [m.strip() for m in (raw or "").split(",") if m.strip()]
        if not models:
            raise LLMError(f"{tier.upper()}_MODEL is not set")
        return models

    def model_for(self, tier: Tier, skip: set[str] = frozenset()) -> str | None:
        """First model in the tier's chain that still has quota today (and is not skipped for this call)."""
        return next((m for m in self.chain(tier) if m not in skip and not self.quota.exhausted(m)), None)

    def decision(self, decision: str, ep: int | None = None, **extra: Any) -> None:
        self.logger.log(ep=ep, step="decision", decision=decision, **extra)

    def call(self, step: str, tier: Tier, messages: list[dict], *, json_schema: type[BaseModel] | None = None,
             ep: int | None = None, budget: EpisodeBudget | None = None, prompt_version: str | None = None,
             max_tokens: int | None = None, temperature: float | None = None) -> Result:
        msgs = _with_schema(messages, json_schema) if json_schema else list(messages)
        base_log = {"ep": ep, "step": step, "tier": tier, "prompt_version": prompt_version}
        totals = {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "list_price_usd": 0.0, "latency_ms": 0, "attempts": 0}

        for parse_try in (1, 2):
            resp, latency_ms, attempt, model = self._request(msgs, tier, base_log, budget, bool(json_schema),
                                                             max_tokens, temperature)
            text = resp.choices[0].message.content or ""
            tokens_in, tokens_out = _usage(resp)
            list_price = price_usd(model, tokens_in, tokens_out)
            cost = 0.0  # free tier only
            if budget:
                budget.add(list_price)
            for k, v in (("tokens_in", tokens_in), ("tokens_out", tokens_out), ("cost_usd", cost),
                         ("list_price_usd", list_price), ("latency_ms", latency_ms), ("attempts", attempt)):
                totals[k] += v
            line = {**base_log, "model": model, "tokens_in": tokens_in, "tokens_out": tokens_out,
                    "cost_usd": round(cost, 6), "list_price_usd": round(list_price, 6), "latency_ms": latency_ms,
                    "attempt": attempt, "decision": None}

            if json_schema is None:
                self.logger.log(**line, status="ok")
                return Result(text=text, parsed=None, model=model, **totals)
            try:
                parsed = parse_json(text, json_schema)
            except ValueError as e:  # JSONDecodeError and pydantic ValidationError are both ValueErrors
                err = str(e)[:500]
                self.logger.log(**line, status="parse_error", error=err)
                if parse_try == 2:
                    raise LLMParseError(f"{step}: output did not match {json_schema.__name__}: {err}") from e
                msgs = msgs + [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": f"That reply did not validate. Error:\n{err}\n"
                                                "Reply with only the corrected JSON object."},
                ]
                continue
            self.logger.log(**line, status="ok")
            return Result(text=text, parsed=parsed, model=model, **totals)
        raise AssertionError("unreachable")

    def _request(self, msgs: list[dict], tier: Tier, base_log: dict, budget: EpisodeBudget | None, json_mode: bool,
                 max_tokens: int | None, temperature: float | None) -> tuple[Any, int, int, str]:
        kwargs: dict[str, Any] = {"messages": msgs}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if temperature is not None:
            kwargs["temperature"] = temperature

        overloaded: set[str] = set()  # models skipped for this call after repeated 5xx
        model = self._pick(tier, overloaded, base_log)
        attempt = transient = on_model = 0
        while True:
            attempt += 1
            if budget:
                estimate = price_usd(model, estimate_tokens("".join(m["content"] for m in msgs)),
                                     max_tokens or OUTPUT_ESTIMATE[tier])
                try:
                    budget.check(estimate, base_log["step"])
                except BudgetExceeded as e:
                    self.logger.log(**base_log, model=model, attempt=attempt, status="budget_exceeded",
                                    decision="budget_exceeded→needs_human", error=str(e))
                    raise
            self._throttle()
            start = self.clock()
            try:
                resp = self.client.chat.completions.create(model=model, **kwargs)
                self._last_call_at = self.clock()
                return resp, int((self._last_call_at - start) * 1000), attempt, model
            except RETRYABLE as e:
                self._last_call_at = self.clock()  # before any backoff sleep, so the throttle doesn't add to it
                latency_ms = int((self._last_call_at - start) * 1000)
                log = {**base_log, "model": model, "attempt": attempt, "latency_ms": latency_ms, "error": _describe(e)}
                if _is_daily_quota(e):
                    reset = self.quota.mark(model)
                    nxt = self.model_for(tier, overloaded) or self.model_for(tier)
                    self.logger.log(**log, status="quota_exhausted",
                                    decision=f"quota_exhausted_until_{reset:%Y-%m-%dT%H:%MZ}→{nxt or 'stop'}")
                    if nxt is None:
                        raise QuotaExhausted(
                            f"Every {tier} model ({', '.join(self.chain(tier))}) has used its free daily quota. "
                            f"Quotas reset around {reset:%Y-%m-%d %H:%M} UTC.") from e
                    model, on_model = nxt, 0
                    continue
                transient += 1
                on_model += 1
                if transient >= self.max_attempts:
                    self.logger.log(**log, status="error", decision=None)
                    raise
                decision = ""
                if on_model >= self.switch_after_transient:
                    alt = self.model_for(tier, overloaded | {model})
                    if alt:
                        overloaded.add(model)
                        decision = f"fallback:{model}→{alt};"
                        model, on_model = alt, 0
                wait = min(60.0, max(self.backoff_base_s * 2 ** (transient - 1), _retry_delay(e)))
                self.logger.log(**log, status="retry", decision=f"{decision}retry_in_{wait:.0f}s")
                self.sleep(wait)
            except APIStatusError as e:  # 400/401/404 etc: retrying will not help
                self._last_call_at = self.clock()
                self.logger.log(**base_log, model=model, attempt=attempt,
                                latency_ms=int((self._last_call_at - start) * 1000), status="error", error=_describe(e))
                raise

    def _pick(self, tier: Tier, skip: set[str], base_log: dict) -> str:
        model = self.model_for(tier, skip)
        if model is None:
            reset = self.quota.next_reset()
            self.logger.log(**base_log, status="quota_exhausted", decision="all_models_exhausted→stop")
            raise QuotaExhausted(f"Every {tier} model ({', '.join(self.chain(tier))}) has used its free daily quota. "
                                 f"Quotas reset around {reset:%Y-%m-%d %H:%M} UTC.")
        return model

    def _throttle(self) -> None:
        if self._last_call_at is None:
            return
        wait = self.settings.min_seconds_between_calls - (self.clock() - self._last_call_at)
        if wait > 0:
            self.sleep(wait)


def check_api_key(base_url: str, api_key: str, client_factory: Callable[..., Any] = OpenAI) -> tuple[bool, str]:
    """Validate a key with a model-list call (no generation quota used). Returns (ok, message); never echoes the key."""
    try:
        models = list(client_factory(base_url=base_url, api_key=api_key, max_retries=1, timeout=30).models.list())
    except APIStatusError as e:
        return False, f"The provider rejected this key ({e.status_code})."
    except APIConnectionError:
        return False, "Could not reach the provider; check your connection."
    return True, f"Key works: {len(models)} models available."


def _describe(e: Exception) -> str:
    code = getattr(e, "status_code", None)
    return f"{type(e).__name__}{f' {code}' if code else ''}: {str(e)[:200]}"
