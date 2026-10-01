"""The web API end to end (FastAPI TestClient + scripted LLM, background jobs polled to completion)."""
import random
import time

import pytest
from fastapi.testclient import TestClient

from fakes import ScriptedLLM
from helpers import VOCAB, episode_text, fill_history, make_story
from story import service, web
from story.config import Settings
from story.llm import RunLogger
from story.store import Store


def extraction(messages, ep):
    rng = random.Random(ep * 131 + len(messages[0]["content"]))
    return {"summary": " ".join(rng.choice(VOCAB) + str(rng.randint(0, 999)) for _ in range(40)),
            "hook": f"Knock {ep}", "hook_type": ["threat", "arrival", "decision", "question", "reversal",
                                                  "revelation", "cliffhanger"][ep % 7]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    setup = Store(db)
    sid = make_story(setup)
    setup.close()
    settings = Settings(runs_dir=tmp_path / "runs", db_path=db)
    seeds = iter(range(5000, 9000))
    llm = ScriptedLLM({
        "draft": lambda m, ep: episode_text(ep, seed=next(seeds)),
        "revise": lambda m, ep: episode_text(ep, seed=next(seeds)),
        "extract": extraction,
        "critic": {"issues": [], "hook_score": 4, "momentum_score": 4},
        "feedback_router": {"directives": [{"text": "Keep the romance slow", "scope": "global"}],
                            "plan_edits": [{"ep": 2, "new_beat": "Ravi and Meera barely speak."}],
                            "explanation": "slowed down"},
        "retcon_check": {"conflict": False},
        "arc_summary": "Arc one happened.",
        "arc_audit": {"corrections": [], "thread_updates": []},
    }, logger=RunLogger(settings.runs_dir, story_id=sid))

    def open_session(story_id=None, settings_=None):
        return service.Session(settings, Store(db), llm, story_id or sid)

    monkeypatch.setattr(service, "open_session", open_session)
    monkeypatch.setattr(service, "load_settings", lambda: settings)
    web.PENDING_BOUNDARY.clear()
    web.PENDING_RETCON.clear()
    return TestClient(web.app), sid, llm, db


def wait(client, resp):
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job"]
    for _ in range(300):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] != "running":
            assert j["status"] == "done", j.get("error")
            return j["result"]
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_page_and_read_endpoints(env):
    c, sid, _, _ = env
    assert "Serial Story Agent" in c.get("/").text
    assert [s["id"] for s in c.get("/api/stories").json()] == [sid]
    st = c.get(f"/api/stories/{sid}/status").json()
    assert st["next_ep"] == 1 and st["approved_plan_version"] == 1 and st["pending_draft"] is None
    plan = c.get(f"/api/stories/{sid}/plan").json()
    assert len(plan["plan"]["beats"]) == 200 and plan["validation"]["errors"] == []
    assert c.get(f"/api/stories/{sid}/context/1").json()["total_tokens"] > 0
    assert c.get(f"/api/stories/{sid}/context/999").status_code == 400


def test_draft_edit_approve_flow(env):
    c, sid, llm, db = env
    draft = wait(c, c.post(f"/api/stories/{sid}/draft", json={}))
    assert draft["ep"] == 1 and draft["status"] == "draft"
    again = wait(c, c.post(f"/api/stories/{sid}/draft", json={}))
    assert again["version"] == draft["version"]  # pending draft reused, not re-paid
    assert sum(x["step"] == "draft" for x in llm.calls) == 1
    out = wait(c, c.post(f"/api/stories/{sid}/episodes/1/approve", json={"text": draft["text"] + "\n\nMY LINE."}))
    assert out["episode"]["human_edited"] and out["episode"]["text"].endswith("MY LINE.")
    eps = c.get(f"/api/stories/{sid}/episodes").json()
    assert [e["ep"] for e in eps] == [1] and eps[0]["human_edited"]
    kinds = [e["kind"] for e in c.get(f"/api/stories/{sid}/events").json()]
    assert "human_edit" in kinds


def test_reject_regenerates_with_reason(env):
    c, sid, llm, _ = env
    wait(c, c.post(f"/api/stories/{sid}/draft", json={}))
    new = wait(c, c.post(f"/api/stories/{sid}/episodes/1/reject", json={"reason": "too much exposition"}))
    assert new["version"] == 2
    assert "too much exposition" in [x for x in llm.calls if x["step"] == "draft"][-1]["messages"][-1]["content"]


def test_feedback_route_then_apply(env):
    c, sid, _, _ = env
    r = wait(c, c.post(f"/api/stories/{sid}/feedback/route", json={"text": "slow down the romance"}))
    assert r["routed"]["directives"][0]["text"] == "Keep the romance slow" and r["old_beats"]["2"]["beat"]
    out = c.post(f"/api/stories/{sid}/feedback/apply", json={"text": "slow down the romance", "stage": r["stage"],
                                                             "next_ep": r["next_ep"], "routed": r["routed"]}).json()
    assert out["applied"]["plan_to"] == 2
    mem = c.get(f"/api/stories/{sid}/memory").json()
    assert [d["text"] for d in mem["directives"]] == ["Keep the romance slow"]
    d = mem["directives"][0]
    assert c.put(f"/api/stories/{sid}/directives/{d['id']}", json={"active": False}).json()["active"] is False


def test_beat_edit_marks_human_owned_and_blocks_written_eps(env):
    c, sid, _, db = env
    r = c.put(f"/api/stories/{sid}/plan/beats/5", json={"text": "Mira arrives early.", "characters": ["meera"]}).json()
    assert r["changes"][0]["new"] == "Mira arrives early."
    beat = next(b for b in c.get(f"/api/stories/{sid}/plan").json()["plan"]["beats"] if b["ep"] == 5)
    assert beat["human_owned"] and beat["characters"] == ["meera"]
    s = Store(db)
    s.commit_episode(sid, 1, episode_text(1), service.pipeline.EpisodeDelta(summary="s", hook="h", hook_type="threat"))
    s.close()
    assert c.put(f"/api/stories/{sid}/plan/beats/1", json={"text": "x"}).status_code == 400


def test_arc_boundary_waits_for_human_decision(env):
    c, sid, llm, db = env
    s = Store(db)
    fill_history(s, sid, 9)
    s.close()
    from story.models import Beat
    llm.handlers["replan_arc"] = {"beats": [Beat(ep=e, arc_no=2, beat=f"New {e}").model_dump() for e in range(11, 21)]}
    wait(c, c.post(f"/api/stories/{sid}/draft", json={}))
    out = wait(c, c.post(f"/api/stories/{sid}/episodes/10/approve", json={}))
    assert out["boundary"]["arc_no"] == 1 and out["boundary"]["replan_to"]
    assert c.get(f"/api/stories/{sid}/status").json()["pending_boundary"]["arc_no"] == 1
    assert c.post(f"/api/stories/{sid}/boundary/decision", json={"decision": "reverted"}).json() == {"ok": True}
    beats = c.get(f"/api/stories/{sid}/plan").json()["plan"]["beats"]
    assert beats[10]["beat"] == "Ravi delivers parcel 11 to Tower B."  # replan reverted
    assert c.get(f"/api/stories/{sid}/status").json()["pending_boundary"] is None


def test_retcon_without_conflicts(env):
    c, sid, _, db = env
    s = Store(db)
    fill_history(s, sid, 3)
    s.close()
    rep = wait(c, c.post(f"/api/stories/{sid}/episodes/2/retcon", json={"text": episode_text(2, seed=77)}))
    assert rep["ep"] == 2 and rep["conflicts"] == []
    assert any(e["kind"] == "retcon" for e in c.get(f"/api/stories/{sid}/events").json())
    assert c.post(f"/api/stories/{sid}/retcon/resolve", json={"resolution": "accept"}).status_code == 404


def test_cost_report(env):
    c, sid, _, _ = env
    wait(c, c.post(f"/api/stories/{sid}/draft", json={}))
    r = c.get(f"/api/stories/{sid}/report").json()
    assert "projection" in r and r["writer_models"] == ["fake-model"]


@pytest.fixture
def keyenv(tmp_path, monkeypatch):
    from story import config
    env = tmp_path / ".env"
    env.write_text("LLM_BASE_URL=https://example.test/v1/\nLLM_API_KEY=old-key-0000000000000000\nWRITER_MODEL=a,b\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", env)
    monkeypatch.setenv("LLM_API_KEY", "old-key-0000000000000000")
    monkeypatch.setattr(service, "load_settings", lambda: config.Settings(
        base_url="https://example.test/v1/", api_key=__import__("os").environ["LLM_API_KEY"], writer_model="a,b", fast_model="c"))
    return TestClient(web.app), env


NEW_KEY = "AQ.TestKey_abcdefghijklmnop1234"


def test_settings_never_returns_the_key(keyenv):
    c, _ = keyenv
    st = c.get("/api/settings").json()
    assert st["key_set"] and st["key_hint"] == "••••0000" and "old-key" not in str(st)
    assert st["writer_models"] == ["a", "b"]


def test_good_key_is_checked_then_saved(keyenv, monkeypatch):
    c, env = keyenv
    monkeypatch.setattr(web, "check_api_key", lambda url, key: (key == NEW_KEY, "Key works: 3 models available."))
    r = c.post("/api/settings/key", json={"api_key": f"  {NEW_KEY}  "})
    assert r.status_code == 200 and NEW_KEY not in r.text and r.json()["key_hint"] == "••••1234"
    text = env.read_text(encoding="utf-8")
    assert f"LLM_API_KEY={NEW_KEY}" in text and "WRITER_MODEL=a,b" in text and "old-key" not in text
    assert c.get("/api/settings").json()["key_hint"] == "••••1234"  # takes effect without restart


def test_bad_key_is_not_saved(keyenv, monkeypatch):
    c, env = keyenv
    monkeypatch.setattr(web, "check_api_key", lambda url, key: (False, "The provider rejected this key (400)."))
    r = c.post("/api/settings/key", json={"api_key": NEW_KEY})
    assert r.status_code == 400 and "Nothing was saved" in r.json()["detail"]
    assert "old-key" in env.read_text(encoding="utf-8")


def test_malformed_key_and_env_injection_refused(keyenv):
    c, env = keyenv
    for bad in ("short", NEW_KEY + "\nDB_PATH=evil.db", "has spaces in it 12345678901234"):
        assert c.post("/api/settings/key", json={"api_key": bad}).status_code == 400
    assert "evil" not in env.read_text(encoding="utf-8")


def test_cross_site_requests_are_refused(keyenv):
    c, _ = keyenv
    r = c.post("/api/settings/key", json={"api_key": NEW_KEY}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert c.post("/api/settings/key", json={"api_key": "short"}, headers={"Origin": "http://127.0.0.1:8765"}).status_code == 400


def test_failed_planning_still_leaves_a_resumable_story(env):
    c, sid, llm, _ = env  # the scripted LLM has no plan_foundation output, so planning fails
    r = c.post("/api/stories", json={"premise": "A serial killer leaves letters in library books."})
    new_id = r.json()["story_id"]
    for _ in range(300):
        j = c.get(f"/api/jobs/{r.json()['job']}").json()
        if j["status"] != "running":
            break
        time.sleep(0.02)
    assert j["status"] == "error"
    assert new_id in [s["id"] for s in c.get("/api/stories").json()]  # visible in the story picker
    assert c.get(f"/api/stories/{new_id}/plan").json()["plan"] is None  # Plan tab offers "Resume planning"
