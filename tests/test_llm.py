import json

import pytest
from pydantic import BaseModel

from fakes import FakeClient, FakeClock, rate_limit_error, response, server_error
from story.config import Settings, price_usd
from story.llm import LLM, BudgetExceeded, EpisodeBudget, LLMParseError, RunLogger, parse_json


class Answer(BaseModel):
    word: str
    score: int


@pytest.fixture
def settings(tmp_path):
    return Settings(writer_model="gemini-3.8-flash", fast_model="gemini-3.8-flash",
                    min_seconds_between_calls=4, runs_dir=tmp_path / "runs")


def make_llm(settings, outputs):
    clock = FakeClock()
    client = FakeClient(outputs)
    llm = LLM(settings, client=client, logger=RunLogger(settings.runs_dir, story_id="s1"),
              sleep=clock.sleep, clock=clock)
    return llm, client, clock


def read_log(llm):
    return [json.loads(line) for line in llm.logger.path.read_text(encoding="utf-8").splitlines()]


def test_plain_call_logs_tokens_cost_and_latency(settings):
    llm, _, _ = make_llm(settings, ["hello"])
    r = llm.call("draft", "writer", [{"role": "user", "content": "hi"}], ep=3, prompt_version="draft_v1")
    assert r.text == "hello" and r.cost_usd == 0.0
    assert r.list_price_usd == pytest.approx(price_usd("gemini-3.8-flash", 1000, 200))
    [line] = read_log(llm)
    assert line["step"] == "draft" and line["ep"] == 3 and line["status"] == "ok"
    assert line["tokens_in"] == 1000 and line["tokens_out"] == 200
    assert line["cost_usd"] == 0.0 and line["list_price_usd"] > 0
    assert line["prompt_version"] == "draft_v1" and line["story_id"] == "s1"
    assert {"ts", "run_id", "model", "tier", "latency_ms", "attempt", "decision"} <= line.keys()


def test_reasoning_tokens_counted_as_output(settings):
    llm, _, _ = make_llm(settings, [response("x", tokens_in=100, tokens_out=10, total=610)])
    r = llm.call("draft", "writer", [{"role": "user", "content": "hi"}])
    assert r.tokens_out == 510


def test_retries_on_429_and_503_with_backoff(settings):
    llm, client, clock = make_llm(settings, [rate_limit_error(), server_error(), "ok"])
    r = llm.call("draft", "writer", [{"role": "user", "content": "hi"}])
    assert r.text == "ok" and r.attempts == 3 and len(client.calls) == 3
    assert clock.sleeps == [4.0, 8.0]  # exponential backoff, no extra throttle wait on top
    statuses = [l["status"] for l in read_log(llm)]
    assert statuses == ["retry", "retry", "ok"]


def test_gives_up_after_max_attempts(settings):
    llm, client, _ = make_llm(settings, [server_error()] * 8)
    with pytest.raises(Exception):
        llm.call("draft", "writer", [{"role": "user", "content": "hi"}])
    assert len(client.calls) == 8
    assert read_log(llm)[-1]["status"] == "error"


def test_min_interval_between_calls(settings):
    llm, _, clock = make_llm(settings, ["a", "b"])
    llm.call("s", "fast", [{"role": "user", "content": "hi"}])
    llm.call("s", "fast", [{"role": "user", "content": "hi"}])
    assert clock.sleeps == [4.0]


def test_json_parsed_into_schema(settings):
    llm, client, _ = make_llm(settings, ['```json\n{"word": "hook", "score": 4}\n```'])
    r = llm.call("critic", "fast", [{"role": "user", "content": "rate"}], json_schema=Answer)
    assert r.parsed == Answer(word="hook", score=4)
    assert client.calls[0]["response_format"] == {"type": "json_object"}
    assert "JSON Schema" in client.calls[0]["messages"][0]["content"]


def test_json_retry_appends_error_then_succeeds(settings):
    llm, client, _ = make_llm(settings, ['{"word": "hook"}', '{"word": "hook", "score": 5}'])
    r = llm.call("critic", "fast", [{"role": "user", "content": "rate"}], json_schema=Answer)
    assert r.parsed.score == 5 and r.tokens_in == 2000  # both attempts are counted
    retry_msgs = client.calls[1]["messages"]
    assert retry_msgs[-2] == {"role": "assistant", "content": '{"word": "hook"}'}
    assert "score" in retry_msgs[-1]["content"]  # validation error is fed back
    assert [l["status"] for l in read_log(llm)] == ["parse_error", "ok"]


def test_json_fails_after_one_retry(settings):
    llm, _, _ = make_llm(settings, ["not json", "still not json"])
    with pytest.raises(LLMParseError):
        llm.call("critic", "fast", [{"role": "user", "content": "rate"}], json_schema=Answer)


def test_budget_exceeded_blocks_call_before_request(settings):
    llm, client, _ = make_llm(settings, ["never sent"])
    budget = EpisodeBudget(cap_usd=0.15, spent_usd=0.149)
    with pytest.raises(BudgetExceeded):
        llm.call("revise", "writer", [{"role": "user", "content": "x" * 40_000}], ep=7, budget=budget)
    assert client.calls == []
    [line] = read_log(llm)
    assert line["status"] == "budget_exceeded" and line["decision"] == "budget_exceeded→needs_human"


def test_budget_accumulates_list_price(settings):
    llm, _, _ = make_llm(settings, ["a", "b"])
    budget = EpisodeBudget(cap_usd=0.15)
    llm.call("draft", "writer", [{"role": "user", "content": "hi"}], budget=budget)
    llm.call("extract", "fast", [{"role": "user", "content": "hi"}], budget=budget)
    assert budget.spent_usd == pytest.approx(2 * price_usd("gemini-3.8-flash", 1000, 200))


def test_parse_json_extracts_object_from_prose():
    assert parse_json('Sure! {"word": "a", "score": 1} hope that helps', Answer).word == "a"


def chain_llm(tmp_path, outputs, now=None):
    from datetime import datetime, timezone
    from story.llm import QuotaTracker
    s = Settings(writer_model="m1, m2,m3", fast_model="lite", runs_dir=tmp_path / "runs", min_seconds_between_calls=0)
    clock = FakeClock()
    client = FakeClient(outputs)
    quota = QuotaTracker(tmp_path / "quota.json", now=now or (lambda: datetime(2026, 10, 1, 12, tzinfo=timezone.utc)))
    llm = LLM(s, client=client, logger=RunLogger(s.runs_dir, story_id="s1"), sleep=clock.sleep, clock=clock, quota=quota)
    return llm, client, clock, quota


def test_daily_quota_switches_model_and_persists(tmp_path):
    from fakes import daily_quota_error
    from story.llm import QuotaTracker
    llm, client, clock, quota = chain_llm(tmp_path, [daily_quota_error(), "from m2"])
    r = llm.call("draft", "writer", [{"role": "user", "content": "hi"}])
    assert r.text == "from m2" and r.model == "m2"
    assert [c["model"] for c in client.calls] == ["m1", "m2"]
    assert clock.sleeps == []  # no backoff for a daily quota; move on immediately
    assert read_log(llm)[0]["status"] == "quota_exhausted"
    assert "→m2" in read_log(llm)[0]["decision"]
    # A new process starts on m2 without re-hitting m1.
    again = QuotaTracker(tmp_path / "quota.json", now=quota.now)
    assert again.exhausted("m1") and not again.exhausted("m2")
    assert quota.until["m1"].startswith("2026-10-02T08:00")


def test_quota_expires_after_reset(tmp_path):
    from datetime import datetime, timezone
    from story.llm import QuotaTracker
    t = {"now": datetime(2026, 10, 1, 12, tzinfo=timezone.utc)}
    q = QuotaTracker(tmp_path / "q.json", now=lambda: t["now"])
    q.mark("m1")
    assert q.exhausted("m1")
    t["now"] = datetime(2026, 10, 2, 8, 1, tzinfo=timezone.utc)
    assert not q.exhausted("m1")


def test_all_models_exhausted_raises(tmp_path):
    from fakes import daily_quota_error
    from story.llm import QuotaExhausted
    llm, client, _, _ = chain_llm(tmp_path, [daily_quota_error()] * 3)
    with pytest.raises(QuotaExhausted, match="reset"):
        llm.call("draft", "writer", [{"role": "user", "content": "hi"}])
    assert len(client.calls) == 3
    with pytest.raises(QuotaExhausted):  # later calls fail fast without a request
        llm.call("draft", "writer", [{"role": "user", "content": "hi"}])
    assert len(client.calls) == 3


def test_overloaded_model_is_bypassed_for_the_call(tmp_path):
    llm, client, _, quota = chain_llm(tmp_path, [server_error(), server_error(), "from m2"])
    r = llm.call("draft", "writer", [{"role": "user", "content": "hi"}])
    assert [c["model"] for c in client.calls] == ["m1", "m1", "m2"] and r.model == "m2"
    assert "fallback:m1→m2" in read_log(llm)[1]["decision"]
    assert not quota.exhausted("m1")  # only skipped for this call
    assert llm.model_for("writer") == "m1"


def test_per_minute_429_waits_server_delay(tmp_path):
    from fakes import per_minute_error
    llm, _, clock, _ = chain_llm(tmp_path, [per_minute_error(30), "ok"])
    assert llm.call("draft", "writer", [{"role": "user", "content": "hi"}]).text == "ok"
    assert clock.sleeps == [30.5]


def test_quota_is_tracked_per_api_key(tmp_path):
    from datetime import datetime, timezone
    from story.llm import QuotaTracker
    now = lambda: datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    QuotaTracker(tmp_path / "q.json", now=now, key_id="old").mark("m1")
    assert QuotaTracker(tmp_path / "q.json", now=now, key_id="old").exhausted("m1")
    fresh = QuotaTracker(tmp_path / "q.json", now=now, key_id="new")
    assert not fresh.exhausted("m1")  # a new key's project has its own quota
    fresh.mark("m2")
    assert QuotaTracker(tmp_path / "q.json", now=now, key_id="old").exhausted("m1")  # old entries kept
