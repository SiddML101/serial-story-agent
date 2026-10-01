"""End-to-end episode loop, feedback routing and retcon with a scripted LLM (no network)."""
import pytest

from fakes import ScriptedLLM
from helpers import episode_text, fill_history, make_story
from story import feedback, pipeline, retcon
from story.config import Settings
from story.context import build_context
from story.models import Beat
from story.store import Store


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "t.db")
    sid = make_story(store)
    yield store, sid, Settings(max_revisions=2, max_cost_per_episode_usd=0.15, runs_dir=tmp_path)
    store.close()


def extraction(ep, **kw):
    base = {"summary": f"Ravi reached floor {ep} and found receipt {ep * 7919}.", "hook": f"A knock at {ep}.",
            "hook_type": ["threat", "arrival", "decision", "question", "reversal", "revelation", "cliffhanger"][ep % 7],
            "in_story_time": f"Day 1, {ep}pm",
            "new_facts": [{"text": f"Flat {ep}0{ep} belonged to a woman who died in {1990 + ep}", "entities": ["ravi", f"flat-{ep}"]}],
            "character_updates": [{"id": "ravi", "changes": {"location": f"floor {ep}"}}]}
    base.update(kw)
    return base


def good_critic(*_):
    return {"issues": [], "hook_score": 4, "momentum_score": 4}


def test_episode_passes_first_time_and_commits(env):
    store, sid, settings = env
    llm = ScriptedLLM({"draft": episode_text(1, seed=101), "extract": lambda m, ep: extraction(ep),
                       "critic": good_critic})
    draft = pipeline.produce_episode(llm, store, settings, sid, 1)
    assert draft.status == "draft" and draft.checks["passed"] and draft.checks["revisions"] == 0
    assert [c["step"] for c in llm.calls] == ["draft", "extract", "critic"]
    assert store.last_approved_ep(sid) == 0  # nothing is canon before approval
    ep, boundary = pipeline.commit(llm, store, sid, 1, draft.text, draft.delta)
    assert ep.status == "approved" and boundary is None
    assert store.state_at(sid, 1).characters["ravi"].location == "floor 1"


def test_failing_checks_trigger_revision_then_pass(env):
    store, sid, settings = env
    llm = ScriptedLLM({"draft": episode_text(1, words=250), "revise": episode_text(1, seed=202),
                       "extract": lambda m, ep: extraction(ep), "critic": good_critic})
    draft = pipeline.produce_episode(llm, store, settings, sid, 1)
    assert draft.checks["passed"] and draft.checks["revisions"] == 1
    assert [c["step"] for c in llm.calls] == ["draft", "extract", "critic", "revise", "extract", "critic"]
    revise_prompt = llm.calls[3]["messages"][-1]["content"]
    assert "word_count" in revise_prompt  # the specific failure is passed to the reviser
    assert any(d.startswith("check_failed:word_count→revise") for d in llm.decisions)


def test_revision_cap_marks_needs_human(env):
    store, sid, settings = env
    bad = {"issues": [{"type": "contradiction", "severity": "high", "evidence": "Ravi Kumar climbed. Meera said nothing", "fix": "y"}],
           "hook_score": 4, "momentum_score": 3}
    llm = ScriptedLLM({"draft": episode_text(1, seed=3), "revise": episode_text(1, seed=4),
                       "extract": lambda m, ep: extraction(ep), "critic": bad})
    draft = pipeline.produce_episode(llm, store, settings, sid, 1)
    assert draft.checks["needs_human"] and not draft.checks["passed"]
    assert sum(c["step"] == "revise" for c in llm.calls) == 2  # MAX_REVISIONS
    assert any("needs_human" in d for d in llm.decisions)


def test_budget_cap_stops_revisions(env):
    store, sid, _ = env
    tight = Settings(max_revisions=5, max_cost_per_episode_usd=0.0035)
    llm = ScriptedLLM({"draft": episode_text(1, words=200), "revise": episode_text(1, words=200),
                       "extract": lambda m, ep: extraction(ep), "critic": good_critic})

    real_call = llm.call

    def call(step, tier, messages, **kw):  # the scripted LLM adds $0.001 per call; enforce the cap like the real one
        if kw.get("budget") and kw["budget"].spent_usd + 0.001 > kw["budget"].cap_usd:
            from story.llm import BudgetExceeded
            raise BudgetExceeded("cap")
        return real_call(step, tier, messages, **kw)

    llm.call = call
    draft = pipeline.produce_episode(llm, store, tight, sid, 1)
    assert draft.checks["needs_human"]
    assert any(f.startswith("budget") for f in draft.checks["failures"])


def test_arc_boundary_summarizes_audits_and_replans(env):
    store, sid, settings = env
    fill_history(store, sid, 9)
    new_beats = [Beat(ep=e, arc_no=2, beat=f"Replanned beat {e}", characters=["ravi"]).model_dump() for e in range(11, 21)]
    llm = ScriptedLLM({"arc_summary": "Arc 1 happened.",
                       "arc_audit": {"corrections": [{"character_id": "meera", "changes": {"location": "control room"},
                                                      "reason": "ep 10 ends there"}]},
                       "replan_arc": {"beats": new_beats}})
    ep, boundary = pipeline.commit(llm, store, sid, 10, episode_text(10), extraction_delta(10))
    assert boundary.summary == "Arc 1 happened." and store.arc_summaries(sid)[1] == "Arc 1 happened."
    assert store.state_at(sid, 10).characters["meera"].location == "control room"
    plan = store.latest_plan(sid)
    assert plan.beat(11).beat == "Replanned beat 11" and plan.beat(10).beat != "Replanned beat 10"
    assert len(boundary.replan_changes) == 10
    reverted = pipeline.revert_replan(store, sid, boundary)
    assert store.get_plan(sid, reverted).beat(11).beat == "Ravi delivers parcel 11 to Tower B."


def test_replan_never_overwrites_human_owned_beats(env):
    store, sid, _ = env
    fill_history(store, sid, 10)
    plan = store.latest_plan(sid)
    mine = "Mira, a print-shop archivist, meets Leo at the tram depot."
    beats = [b.model_copy(update={"beat": mine, "human_owned": True}) if b.ep == 13 else b for b in plan.beats]
    store.save_plan(sid, plan.model_copy(update={"beats": beats}), "human edit (YAML)")
    new_beats = [Beat(ep=e, arc_no=2, beat=f"Replanned beat {e}").model_dump() for e in range(11, 21)]
    llm = ScriptedLLM({"replan_arc": {"beats": new_beats}})
    pipeline.replan_next_arc(llm, store, sid, 1)
    after = store.latest_plan(sid)
    assert after.beat(13).beat == mine and after.beat(13).human_owned
    assert after.beat(12).beat == "Replanned beat 12"
    assert "HUMAN-WRITTEN" in llm.calls[0]["messages"][0]["content"]  # the planner is told, too


def test_arc_audit_closes_answered_threads_and_redates_slipped_ones(env):
    store, sid, _ = env
    from story.models import EpisodeDelta, Thread
    fill_history(store, sid, 9)  # opens t3, t6, t9 along the way
    d10 = EpisodeDelta(summary="Ravi learns who sent the orders.", hook="h", hook_type="question",
                       threads_opened=[Thread(id="slipped", title="A planned answer that never came", due_by_ep=8)])
    llm = ScriptedLLM({"arc_summary": "Arc 1.",
                       "arc_audit": {"corrections": [], "thread_updates": [
                           {"thread_id": "t3", "status": "resolved", "reason": "ep 7 names the sender"},
                           {"thread_id": "not-a-thread", "status": "resolved"}]},
                       "replan_arc": {"beats": []}})
    pipeline.commit(llm, store, sid, 10, episode_text(10), d10)
    threads = store.state_at(sid, 10).threads
    assert threads["t3"].status == "resolved" and threads["t3"].resolved_ep == 10
    assert threads["slipped"].status == "open" and threads["slipped"].due_by_ep == 20  # re-dated, not left overdue
    assert threads["t6"].status == "open" and threads["t6"].due_by_ep == 36  # not due yet: untouched
    assert any(d.startswith("thread_deferred:slipped") for d in llm.decisions)


def extraction_delta(ep):
    from story.models import EpisodeDelta
    return EpisodeDelta.model_validate(extraction(ep))


def test_feedback_creates_directives_and_plan_edits_that_reach_later_drafts(env):
    store, sid, settings = env
    fill_history(store, sid, 5)
    routed = {"directives": [{"text": "Romance advances at most one small step every 3 episodes", "scope": "global"}],
              "plan_edits": [{"ep": 6, "new_beat": "Ravi and Meera share a silence, nothing more."},
                             {"ep": 3, "new_beat": "cannot touch canon"},
                             {"ep": 40, "new_beat": "outside the window"}],
              "state_edits": [], "regenerate_current": False, "explanation": "slowed it down"}
    llm = ScriptedLLM({"feedback_router": routed})
    r = feedback.route(llm, store, sid, "slow down the romance", "episode", 6)
    assert [e.ep for e in r.plan_edits] == [6]  # approved ep 3 and out-of-window ep 40 are dropped
    applied = feedback.apply(llm, store, sid, "slow down the romance", r, "episode", 6)
    assert applied.plan_to == applied.plan_from + 1
    assert applied.plan_changes == [{"kind": "beat", "ep": 6, "old": "Ravi delivers parcel 6 to Tower B.",
                                     "new": "Ravi and Meera share a silence, nothing more."}]
    ctx = build_context(store, sid, 6)
    assert "Romance advances at most one small step" in ctx.sections[0][1]
    assert "share a silence" in ctx.text()
    assert "Romance advances" in build_context(store, sid, 9).text()  # carries forward
    assert store.events(sid)[-1]["kind"] == "feedback"


def test_resume_reuses_pending_draft(env):
    store, sid, settings = env
    fill_history(store, sid, 7)
    store.save_draft(sid, 8, episode_text(8), extraction_delta(8), {"passed": True})
    assert store.last_approved_ep(sid) + 1 == 8
    assert store.get_draft(sid, 8).text == episode_text(8)


def test_retcon_flags_later_episodes_that_relied_on_changed_fact(env):
    store, sid, _ = env
    from story.models import EpisodeDelta, Fact
    fill_history(store, sid, 2)
    d3 = EpisodeDelta(summary="Ravi learns Mrs. Iyer died in the fire of 1998.", hook="h", hook_type="threat",
                      new_facts=[Fact(text="Mrs. Iyer died in the 1998 fire", entities=["iyer", "fire"])])
    store.commit_episode(sid, 3, episode_text(3) + " Mrs. Iyer burned.", d3)
    d4 = EpisodeDelta(summary="Ravi visits the fire memorial for Iyer.", hook="h", hook_type="arrival",
                      new_facts=[Fact(text="The memorial lists Iyer among fire victims", entities=["iyer", "memorial"])])
    store.commit_episode(sid, 4, episode_text(4) + " The fire memorial had her name.", d4)
    store.commit_episode(sid, 5, episode_text(5), extraction_delta(5))

    new_extract = {"summary": "Ravi learns Mrs. Iyer drowned in 1998.", "hook": "h", "hook_type": "threat",
                   "new_facts": [{"text": "Mrs. Iyer drowned in the building's tank in 1998", "entities": ["iyer", "tank"]}]}
    llm = ScriptedLLM({"extract": new_extract,
                       "retcon_check": lambda m, ep: {"conflict": ep == 4, "evidence": "fire memorial had her name",
                                                      "fix": "make it a drowning memorial"}})
    rep = retcon.retcon(llm, store, sid, 3, "Mrs. Iyer drowned. " + episode_text(3))
    assert rep.removed_facts == ["Mrs. Iyer died in the 1998 fire"]
    assert "iyer" in rep.entities
    assert 4 in rep.affected and 5 not in rep.affected
    assert rep.conflicts == [{"ep": 4, "evidence": "fire memorial had her name", "fix": "make it a drowning memorial"}]
    assert store.approved_episode(sid, 3).version == 2 and store.approved_episode(sid, 3).human_edited
    facts = [f.text for f in store.state_at(sid, 5).active_facts()]
    assert "Mrs. Iyer drowned in the building's tank in 1998" in facts and "Mrs. Iyer died in the 1998 fire" not in facts

    patch = ScriptedLLM({"revise": episode_text(4, seed=44) + " A memorial for the drowned.",
                         "extract": {"summary": "Ravi visits the drowning memorial.", "hook": "h", "hook_type": "arrival"}})
    assert retcon.patch_episode(patch, store, sid, 4, ["make it a drowning memorial"]) == 2
    assert "drowned" in store.approved_episode(sid, 4).text

    assert store.rollback_to(sid, 3) == [4, 5]
    assert store.last_approved_ep(sid) == 3
