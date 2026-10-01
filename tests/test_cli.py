"""Drives the real CLI (review loop, feedback, resume, retcon, export) with typed input and a scripted LLM."""
import pytest
from typer.testing import CliRunner

from fakes import ScriptedLLM
from helpers import episode_text, make_story
from story import cli, export
from story.config import Settings
from story.llm import RunLogger
from story.store import Store

runner = CliRunner()


def extraction(messages, ep):
    import random
    from helpers import VOCAB
    rng = random.Random(ep * 97)
    return {"summary": " ".join(rng.choice(VOCAB) + str(rng.randint(0, 999)) for _ in range(40)),
            "hook": f"Knock {ep}", "hook_type": ["threat", "arrival", "decision", "question", "reversal",
                                                  "revelation", "cliffhanger"][ep % 7],
            "new_facts": [{"text": f"Floor {ep} smells of jasmine {ep * 31}", "entities": ["ravi", f"floor-{ep}"]}]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = Store(tmp_path / "t.db")
    sid = make_story(store)
    settings = Settings(runs_dir=tmp_path / "runs", db_path=tmp_path / "t.db")
    drafts = iter(range(1000, 2000))
    llm = ScriptedLLM({
        "draft": lambda m, ep: episode_text(ep, seed=next(drafts)),
        "revise": lambda m, ep: episode_text(ep, seed=next(drafts)),
        "extract": extraction,
        "critic": {"issues": [], "hook_score": 4, "momentum_score": 4},
        "feedback_router": {"directives": [{"text": "Keep the romance slow", "scope": "global"}],
                            "plan_edits": [{"ep": 3, "new_beat": "Ravi and Meera barely speak."}],
                            "regenerate_current": False, "explanation": "slowed down"},
    }, logger=RunLogger(settings.runs_dir, story_id=sid))
    ctx = cli.Ctx(settings, store, llm, sid)
    monkeypatch.setattr(cli, "open_ctx", lambda story_id=None: ctx)
    monkeypatch.setattr(export, "DEMO_DIR", tmp_path / "demo")
    monkeypatch.setattr(export, "export_plan", lambda s, i, d=tmp_path / "demo": d)
    monkeypatch.setattr(export, "export_hitl_log", lambda s, i, d=tmp_path / "demo": d)
    monkeypatch.setattr(export, "export_episodes", lambda s, i, d=tmp_path / "demo": [])
    yield ctx
    store.close()


def test_write_approve_then_quit_then_resume(env):
    r = runner.invoke(cli.app, ["write", "--count", "3"], input="a\na\nq\n")
    assert r.exit_code == 0, r.output
    assert env.store.last_approved_ep(env.story_id) == 2
    assert "resumes at episode 3" in " ".join(r.output.split())
    drafted = sum(c["step"] == "draft" for c in env.llm.calls)

    r = runner.invoke(cli.app, ["write", "--count", "1"], input="a\n")
    assert r.exit_code == 0, r.output
    assert "reusing it" in r.output  # the ep-3 draft from before the quit, not a new call
    assert sum(c["step"] == "draft" for c in env.llm.calls) == drafted
    assert env.store.last_approved_ep(env.story_id) == 3


def test_reject_regenerates_with_reason(env):
    r = runner.invoke(cli.app, ["write", "--count", "1"], input="r\ntoo much exposition\nn\na\n")
    assert r.exit_code == 0, r.output
    drafts = [c for c in env.llm.calls if c["step"] == "draft"]
    assert len(drafts) == 2
    assert "too much exposition" in drafts[1]["messages"][-1]["content"]
    ev = [e for e in env.store.events(env.story_id) if e["kind"] == "reject"]
    assert ev and ev[0]["reason"] == "too much exposition"


def test_feedback_during_review_changes_plan_and_directives(env):
    r = runner.invoke(cli.app, ["write", "--count", "1"], input="f\nslow down the romance\ny\nn\na\n")
    assert r.exit_code == 0, r.output
    assert [d.text for d in env.store.directives(env.story_id)] == ["Keep the romance slow"]
    assert env.store.latest_plan(env.story_id).beat(3).beat == "Ravi and Meera barely speak."
    r = runner.invoke(cli.app, ["write", "--count", "2"], input="a\na\n")
    assert r.exit_code == 0, r.output
    last_draft = [c for c in env.llm.calls if c["step"] == "draft"][-1]
    prompt = last_draft["messages"][-1]["content"]
    assert "Keep the romance slow" in prompt and "barely speak" in prompt


def test_edit_marks_human_edited_and_reextracts(env, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "edit_text", lambda text, suffix=".txt": text + "\n\nMeera finally laughed.")
    r = runner.invoke(cli.app, ["write", "--count", "1"], input="e\n")
    assert r.exit_code == 0, r.output
    ep = env.store.approved_episode(env.story_id, 1)
    assert ep.human_edited and ep.text.endswith("Meera finally laughed.")
    assert sum(c["step"] == "extract" for c in env.llm.calls) == 2  # draft + re-extract of the edited text


def test_auto_mode_and_status_report_export(env, tmp_path):
    r = runner.invoke(cli.app, ["write", "--auto", "--until", "2"])
    assert r.exit_code == 0, r.output
    assert env.store.last_approved_ep(env.story_id) == 2
    assert "auto:approve" in env.llm.decisions
    assert runner.invoke(cli.app, ["status"]).exit_code == 0
    r = runner.invoke(cli.app, ["report", "--project", "200"])
    assert r.exit_code == 0 and "Projection to 200" in r.output


def test_edit_episode_retcon_from_file(env, tmp_path):
    runner.invoke(cli.app, ["write", "--auto", "--until", "2"])
    before = env.store.approved_episode(env.story_id, 1).version
    new_text = tmp_path / "ep1.md"
    new_text.write_text(episode_text(1, seed=4242), encoding="utf-8")
    env.llm.handlers["retcon_check"] = {"conflict": False}
    r = runner.invoke(cli.app, ["edit-episode", "1", "--file", str(new_text)])
    assert r.exit_code == 0, r.output
    assert env.store.approved_episode(env.story_id, 1).version == before + 1
    assert any(e["kind"] == "retcon" for e in env.store.events(env.story_id))


def test_failed_approval_keeps_the_human_edit(env, monkeypatch):
    monkeypatch.setattr(cli, "edit_text", lambda text, suffix=".txt": text + "\n\nMY EDIT.")
    runner.invoke(cli.app, ["write", "--count", "1"], input="q\n")  # draft only
    env.llm.handlers["extract"] = lambda m, ep: (_ for _ in ()).throw(RuntimeError("quota"))
    r = runner.invoke(cli.app, ["review", "1"], input="e\n")
    assert r.exit_code == 0 and "Approval failed" in r.output
    assert env.store.last_approved_ep(env.story_id) == 0
    assert env.store.get_draft(env.story_id, 1).text.endswith("MY EDIT.")  # not lost


def test_bad_arc_and_act_numbers_fail_cleanly(env):
    assert runner.invoke(cli.app, ["plan", "show", "--arc", "99"]).exit_code == 1
    assert runner.invoke(cli.app, ["plan", "show", "--act", "9"]).exit_code == 1
    r = runner.invoke(cli.app, ["plan", "regen", "--act", "6"])
    assert "Acts are 1-5" in r.output and not any(c["step"].startswith("plan_beats") for c in env.llm.calls)
    assert runner.invoke(cli.app, ["context", "201"]).exit_code == 1
