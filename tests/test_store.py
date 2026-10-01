import pytest
from pydantic import ValidationError

from story.models import (
    Act, Arc, ArcPlan, Beat, Bible, Character, CharacterUpdate, Directive, EpisodeDelta, Fact, Feedback,
    StoryState, Thread,
)
from story.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


@pytest.fixture
def story(store):
    sid = store.create_story("A rider's route is all dead people's addresses.")
    store.set_initial_state(sid, StoryState(characters={
        "ravi": Character(id="ravi", name="Ravi", role="protagonist"),
        "meera": Character(id="meera", name="Meera", role="dispatcher"),
    }))
    return sid


def plan(beat_text: str = "Ravi gets the route") -> ArcPlan:
    return ArcPlan(
        acts=[Act(act_no=1, title="Route", ep_start=1, ep_end=2, purpose="p", turning_point="t")],
        arcs=[Arc(arc_no=1, act_no=1, title="Arc", ep_start=1, ep_end=2, goal="g", turning_point="t")],
        beats=[Beat(ep=1, arc_no=1, beat=beat_text), Beat(ep=2, arc_no=1, beat="He knocks")],
    )


def delta(**kw) -> EpisodeDelta:
    return EpisodeDelta(summary=kw.pop("summary", "s"), hook="h", hook_type="question", **kw)


def test_bible_round_trip(store, story):
    b = Bible(title="Last Drop", logline="l", genre="horror", tone="dread", pov="close third", tense="past",
              setting="Mumbai", banned_phrases=["tapestry"])
    assert store.save_bible(story, b) == 1
    assert store.latest_bible(story) == b


def test_plan_versions_approval_and_diff(store, story):
    assert store.save_plan(story, plan(), "initial") == 1
    assert store.latest_plan(story, approved_only=True) is None
    store.approve_plan(story, 1)
    assert store.save_plan(story, plan("Ravi refuses the route"), "human edit") == 2
    assert store.latest_plan(story).version == 2
    assert store.latest_plan(story, approved_only=True).version == 1
    assert store.plan_diff(story, 1, 2) == [
        {"kind": "beat", "ep": 1, "old": "Ravi gets the route", "new": "Ravi refuses the route"}
    ]
    assert [h["reason"] for h in store.plan_history(story)] == ["initial", "human edit"]


def test_episode_draft_commit_and_supersede(store, story):
    draft = store.save_draft(story, 1, "one two three", delta(), checks={"word_count": False}, cost_usd=0.01)
    assert draft.version == 1 and draft.word_count == 3
    assert store.get_draft(story, 1).text == "one two three"

    ep = store.commit_episode(story, 1, "edited text here now", delta(), human_edited=True)
    assert ep.version == 2 and ep.status == "approved" and ep.human_edited
    assert store.get_episode(story, 1, 1).status == "superseded"
    assert store.get_draft(story, 1) is None
    assert store.last_approved_ep(story) == 1
    assert [e.ep for e in store.approved_episodes(story)] == [1]


def test_cannot_skip_episodes(store, story):
    with pytest.raises(ValueError):
        store.commit_episode(story, 2, "x", delta())


def test_invalid_delta_is_never_committed(store, story):
    bad = delta(character_updates=[CharacterUpdate(id="ravi", changes={"status": "deceased"})])
    with pytest.raises(ValidationError):
        store.commit_episode(story, 1, "x", bad)
    assert store.last_approved_ep(story) == 0


def test_state_replays_deltas(store, story):
    store.commit_episode(story, 1, "t", delta(
        in_story_time="Mon 08:00",
        new_facts=[Fact(id="f1", text="Route has 9 stops", entities=["route"])],
        threads_opened=[Thread(id="route", title="Who made the route?", due_by_ep=20)],
        new_characters=[Character(id="guard", name="Old Guard")],
        character_updates=[CharacterUpdate(id="ravi", changes={"location": "Tower B", "knows": ["route is odd"]})],
    ))
    store.commit_episode(story, 2, "t", delta(
        in_story_time="Mon 09:30",
        new_facts=[Fact(id="f2", text="Route has 12 stops", entities=["route"], supersedes="f1")],
        threads_advanced=["route"],
        character_updates=[CharacterUpdate(id="ravi", changes={"knows": ["guard lied"],
                                                               "relationships": {"guard": "distrusts"}}),
                           CharacterUpdate(id="guard", changes={"status": "dead"})],
    ))
    s1, s2 = store.state_at(story, 1), store.state_at(story, 2)

    assert s1.characters["ravi"].location == "Tower B" and "guard" in s1.characters
    assert s1.characters["guard"].first_ep == 1
    assert s2.characters["ravi"].knows == ["route is odd", "guard lied"]
    assert s2.characters["ravi"].relationships == {"guard": "distrusts"}
    assert s2.characters["ravi"].last_seen_ep == 2
    assert s2.characters["guard"].status == "dead" and s1.characters["guard"].status == "alive"
    assert [f.text for f in s2.active_facts()] == ["Route has 12 stops"]
    assert s2.threads["route"].last_touched_ep == 2 and s2.threads["route"].opened_ep == 1
    assert [t.in_story_time for t in s2.timeline] == ["Mon 08:00", "Mon 09:30"]
    assert store.state_at(story, 0).characters.keys() == {"ravi", "meera"}  # initial state untouched


def test_retcon_invalidates_snapshots_and_replays(store, story):
    store.commit_episode(story, 1, "t", delta(character_updates=[CharacterUpdate(id="ravi", changes={"location": "A"})]))
    store.commit_episode(story, 2, "t", delta(threads_opened=[Thread(id="x", title="X")]))
    assert store.state_at(story, 2).characters["ravi"].location == "A"  # snapshot cached at ep 2

    store.commit_episode(story, 1, "rewritten", delta(
        character_updates=[CharacterUpdate(id="ravi", changes={"location": "B"})]))
    s2 = store.state_at(story, 2)
    assert s2.characters["ravi"].location == "B"  # new ep 1, old ep 2 still applied on top
    assert "x" in s2.threads
    assert store.approved_episode(story, 1).text == "rewritten"


def test_unresolved_references_return_warnings():
    state = StoryState()
    warnings = state.apply(delta(ep=1, threads_resolved=["nope"],
                                 character_updates=[CharacterUpdate(id="ghost", changes={"location": "x"})]))
    assert len(warnings) == 2


def test_directives_feedback_and_arc_summaries(store, story):
    d = Directive(text="Romance moves slowly", created_ep=5)
    store.save_directive(story, d)
    assert [x.text for x in store.directives(story)] == ["Romance moves slowly"]
    store.set_directive_active(story, d.id, False)
    assert store.directives(story) == [] and len(store.directives(story, active_only=False)) == 1

    store.save_feedback(story, Feedback(ep=5, text="slow down the romance", routed={"directives": 1}))
    assert store.feedback(story)[0].routed == {"directives": 1}

    store.save_arc_summary(story, 1, "old")
    store.save_arc_summary(story, 1, "new")
    store.save_arc_summary(story, 2, "two")
    assert store.arc_summaries(story) == {1: "new", 2: "two"}


def test_latest_story(store):
    assert store.latest_story_id() is None
    store.create_story("a")
    b = store.create_story("b")
    assert store.latest_story_id() == b
