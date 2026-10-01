"""Regression tests for bugs found in code review (each reproduced before the fix)."""
import pytest
from pydantic import ValidationError

from helpers import delta_for, episode_text, fill_history, make_story
from story import extractor, retcon
from story.models import CharacterUpdate, EpisodeDelta, StoryState
from story.store import Store


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "t.db")
    sid = make_story(store)
    yield store, sid
    store.close()


def test_malformed_relationships_and_knows_are_coerced(env):
    store, sid = env
    state = store.state_at(sid, 0)
    bad = EpisodeDelta(summary="", hook="", hook_type="question", character_updates=[
        CharacterUpdate(id="ravi", changes={"relationships": {"meera": None, "anil": {"type": "ally"}},
                                            "knows": {"secret": "the tank"}})])
    clean, _ = extractor.sanitize(bad, state, "", 1)
    changes = next(u.changes for u in clean.character_updates if u.id == "ravi")
    assert changes["relationships"] == {"anil": "{'type': 'ally'}"}
    assert changes["knows"] == ["secret: the tank"]
    StoryState.model_validate(state.model_dump()).apply(clean.model_copy(update={"ep": 1}))  # applies cleanly


def test_invalid_state_edit_is_rejected_not_stored(env):
    store, sid = env
    fill_history(store, sid, 3)
    with pytest.raises(ValidationError):
        store.add_state_edit(sid, 3, CharacterUpdate(id="ravi", changes={"relationships": {"meera": None}}), "test")
    assert store.state_at(sid, 3).characters["ravi"]  # story still readable


def test_state_edit_at_episode_zero_is_applied(env):
    store, sid = env
    store.add_state_edit(sid, 0, CharacterUpdate(id="anil", changes={"location": "the roof"}), "feedback:plan")
    assert store.state_at(sid, 0).characters["anil"].location == "the roof"
    store.commit_episode(sid, 1, episode_text(1), delta_for(1))
    assert store.state_at(sid, 1).characters["anil"].location == "the roof"
    assert store.state_at(sid, 0).characters["anil"].location == "the roof"  # ep-0 snapshot not polluted twice


def test_rollback_drops_corrections_for_rolled_back_episodes(env):
    store, sid = env
    fill_history(store, sid, 5)
    store.add_state_edit(sid, 5, CharacterUpdate(id="anil", changes={"status": "dead"}), "audit:arc1")
    store.rollback_to(sid, 3)
    for ep in (4, 5):
        store.commit_episode(sid, ep, episode_text(ep, seed=ep * 11), delta_for(ep))
    assert store.state_at(sid, 5).characters["anil"].status == "alive"


def test_retcon_search_keeps_common_entity_when_specific_ones_find_nothing(env):
    store, sid = env
    fill_history(store, sid, 10)
    state = store.state_at(sid, 10)
    store.commit_episode(sid, 11, episode_text(11) + " Anil waved.", delta_for(11))
    assert retcon.affected_episodes(store, sid, 10, ["anil", "zzzz-unused"], state) == [11]


def test_corrupt_log_line_is_skipped(tmp_path):
    from story.report import load_log
    p = tmp_path / "log.jsonl"
    p.write_text('{"step": "draft", "status": "ok"}\n{"step": "dra', encoding="utf-8")
    assert load_log(p) == [{"step": "draft", "status": "ok"}]


def test_replan_after_retcon_keeps_written_beats(env):
    from fakes import ScriptedLLM
    from story import pipeline
    from story.models import Beat
    store, sid = env
    fill_history(store, sid, 14)
    llm = ScriptedLLM({"replan_arc": {"beats": [Beat(ep=e, arc_no=2, beat=f"New {e}").model_dump() for e in range(11, 21)]}})
    pipeline.replan_next_arc(llm, store, sid, 1, keep_upto=14)
    plan = store.latest_plan(sid)
    assert plan.beat(14).beat == "Ravi delivers parcel 14 to Tower B." and plan.beat(15).beat == "New 15"


def test_quoted_windows_editor_path(monkeypatch):
    from story import editor
    monkeypatch.setattr(editor.os, "name", "nt")
    monkeypatch.setenv("EDITOR", '"C:/Program Files/Notepad++/notepad++.exe" -multiInst')
    monkeypatch.setattr(editor.shutil, "which", lambda x: None)
    assert editor._editor_cmd() == ["C:/Program Files/Notepad++/notepad++.exe", "-multiInst"]
