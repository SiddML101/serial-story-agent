import pytest

from story import planner
from story.models import Act, Arc, ArcPlan, Beat

HOOKS = list(planner.HOOK_TYPES)
WORDS = ("lantern bridge ledger stairwell rooftop parcel siren elevator ledger courier tram monsoon "
         "basement ferry market archive clinic temple garage radio").split()


def make_plan() -> ArcPlan:
    acts = [Act(act_no=i, title=f"A{i}", ep_start=planner.act_range(i)[0], ep_end=planner.act_range(i)[1],
                purpose="p", turning_point="t") for i in range(1, 6)]
    arcs = []
    for i in range(1, 21):
        s, e = planner.arc_range(i)
        arcs.append(Arc(arc_no=i, act_no=(s - 1) // 40 + 1, title=f"Arc {i}", ep_start=s, ep_end=e, goal="g",
                        turning_point="t", threads_opened=[f"t{i}"], threads_resolved=[f"t{i}"]))
    beats = [Beat(ep=ep, arc_no=planner.arc_no_for(ep),
                  beat=f"Ravi finds place{ep} and object{ep} near {WORDS[ep % len(WORDS)]}",
                  characters=["ravi", "meera"], hook_type_hint=HOOKS[ep % len(HOOKS)])
             for ep in range(1, 201)]
    return ArcPlan(acts=acts, arcs=arcs, beats=beats)


def test_valid_plan_passes():
    v = planner.validate_plan(make_plan(), ["ravi", "meera"])
    assert v.ok, v.errors
    assert v.warnings == []


def test_missing_and_duplicate_beats_are_errors():
    p = make_plan()
    p.beats = p.beats[:150] + [p.beats[0]]
    v = planner.validate_plan(p, ["ravi"])
    assert not v.ok
    assert any("151 beats" in e for e in v.errors)
    assert any("contiguous" in e for e in v.errors)


def test_arc_range_gap_is_error():
    p = make_plan()
    p.arcs[3].ep_start = 33
    assert any("arc 4" in e for e in planner.validate_plan(p, []).errors)


def test_wrong_arc_number_on_beat_is_error():
    p = make_plan()
    p.beats[14].arc_no = 1
    assert any("ep 15" in e for e in planner.validate_plan(p, []).errors)


def test_thread_and_cast_warnings():
    p = make_plan()
    p.arcs[0].threads_resolved = []  # t1 never resolved
    p.arcs[5].threads_resolved.append("ghost-thread")  # resolved, never opened
    v = planner.validate_plan(p, ["ravi", "meera", "anil"])
    assert v.ok
    assert any("'t1'" in w and "never resolved" in w for w in v.warnings)
    assert any("ghost-thread" in w for w in v.warnings)
    assert sum("anil" in w for w in v.warnings) == 5  # missing from every act


def test_near_duplicate_and_hook_run_warnings():
    p = make_plan()
    p.beats[50].beat = p.beats[10].beat
    for i in (20, 21, 22):
        p.beats[i].hook_type_hint = "threat"
    v = planner.validate_plan(p, [])
    assert any("eps 11 and 51" in w for w in v.warnings)
    assert any("three times in a row" in w for w in v.warnings)


def test_normalize_foundation_forces_structure():
    from story.models import Bible, Character
    f = planner.Foundation(
        bible=Bible(title="t", logline="l", genre="g", tone="t", pov="p", tense="past", setting="s"),
        characters=[Character(id="Ravi Kumar", name="Ravi", status="dead")],
        acts=[Act(act_no=9, title="x", ep_start=0, ep_end=0, purpose="p", turning_point="t") for _ in range(5)],
        arcs=[Arc(arc_no=0, act_no=0, title="x", ep_start=0, ep_end=0, goal="g", turning_point="t",
                  threads_opened=["Who Sends It?"]) for _ in range(20)],
    )
    n = planner.normalize_foundation(f)
    assert [(a.ep_start, a.ep_end) for a in n.acts][2] == (81, 120)
    assert (n.arcs[19].ep_start, n.arcs[19].ep_end, n.arcs[19].act_no) == (191, 200, 5)
    assert n.arcs[0].threads_opened == ["who-sends-it"]
    assert n.characters[0].id == "ravi-kumar" and n.characters[0].status == "alive"
    with pytest.raises(planner.PlanError):
        planner.normalize_foundation(f.model_copy(update={"acts": f.acts[:4]}))


def test_yaml_round_trip():
    p = make_plan()
    back = planner.plan_from_yaml(planner.plan_to_yaml(p))
    assert back.beats == p.beats and back.arcs == p.arcs
