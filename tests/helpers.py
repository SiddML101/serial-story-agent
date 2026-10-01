"""Builds a story in a temp DB with an approved plan and (optionally) a long fake history."""
from __future__ import annotations

import random

from story import planner
from story.models import (Act, Arc, ArcPlan, Beat, Bible, Character, CharacterUpdate, EpisodeDelta, Fact,
                          StoryState, Thread)
from story.store import Store

VOCAB = ("rain stairwell ledger parcel siren tram ferry market archive clinic temple garage radio lantern bridge "
         "courier monsoon rooftop basement elevator tower flat keys receipt scooter helmet dispatcher shrine").split()


def bible() -> Bible:
    return Bible(title="Last Drop", logline="A rider delivers to the dead.", genre="horror", tone="dread",
                 pov="close third on Ravi", tense="past", setting="Mumbai, monsoon", world_rules=["The dead sign."],
                 style_guide=["Short sentences."], banned_phrases=["bone-chilling"])


def plan() -> ArcPlan:
    acts = [Act(act_no=i, title=f"Act {i}", ep_start=planner.act_range(i)[0], ep_end=planner.act_range(i)[1],
                purpose="p", turning_point="t") for i in range(1, 6)]
    arcs = [Arc(arc_no=i, act_no=(planner.arc_range(i)[0] - 1) // 40 + 1, title=f"Arc {i}",
                ep_start=planner.arc_range(i)[0], ep_end=planner.arc_range(i)[1], goal="g", turning_point="t",
                threads_opened=[f"thread-{i}"], threads_resolved=[f"thread-{i}"]) for i in range(1, 21)]
    beats = [Beat(ep=ep, arc_no=planner.arc_no_for(ep), beat=f"Ravi delivers parcel {ep} to Tower B.",
                  characters=["ravi", "meera"], threads=[f"thread-{planner.arc_no_for(ep)}"],
                  hook_type_hint=planner.HOOK_TYPES[ep % 7]) for ep in range(1, 201)]
    return ArcPlan(acts=acts, arcs=arcs, beats=beats)


def cast() -> dict[str, Character]:
    return {
        "ravi": Character(id="ravi", name="Ravi Kumar", role="protagonist", goal="finish the route"),
        "meera": Character(id="meera", name="Meera", role="dispatcher", goal="protect Ravi"),
        "anil": Character(id="anil", name="Anil", role="guard", goal="hide the truth"),
    }


def make_story(store: Store) -> str:
    sid = store.create_story("A rider's route is all dead people's addresses.")
    store.save_bible(sid, bible())
    store.set_initial_state(sid, StoryState(characters=cast()))
    v = store.save_plan(sid, plan(), "test plan")
    store.approve_plan(sid, v)
    return sid


def episode_text(ep: int, words: int = 600, seed: int | None = None) -> str:
    rng = random.Random(seed if seed is not None else ep)
    body = " ".join(rng.choice(VOCAB) + str(rng.randint(0, 999)) for _ in range(words - 12))
    return f"Ravi Kumar climbed. Meera said nothing. {body} The door opened by itself."


def delta_for(ep: int) -> EpisodeDelta:
    rng = random.Random(ep)
    words = " ".join(rng.choice(VOCAB) + str(rng.randint(0, 99)) for _ in range(60))
    return EpisodeDelta(
        summary=f"Ep {ep}: Ravi {words}", hook=f"Hook {ep}", hook_type=planner.HOOK_TYPES[ep % 7],
        in_story_time=f"Day {ep}, night",
        new_facts=[Fact(text=f"Fact {ep}.{i}: {rng.choice(VOCAB)} {rng.choice(VOCAB)} {ep}",
                        entities=[rng.choice(["ravi", "meera", "anil"]), rng.choice(VOCAB)]) for i in range(6)],
        new_characters=[Character(id=f"side{ep}", name=f"Sider{ep}", role="tenant")] if ep % 5 == 0 else [],
        character_updates=[CharacterUpdate(id="ravi", changes={"location": f"floor {ep}", "knows": [f"clue {ep}"]})],
        threads_opened=[Thread(id=f"t{ep}", title=f"Question {ep}", due_by_ep=ep + 30)] if ep % 3 == 0 else [],
        threads_advanced=[f"t{ep - 3}"] if ep % 3 == 0 and ep > 3 else [],
    )


def fill_history(store: Store, sid: str, upto: int) -> None:
    for ep in range(1, upto + 1):
        store.commit_episode(sid, ep, episode_text(ep), delta_for(ep))
        arc = planner.arc_no_for(ep)
        if ep == planner.arc_range(arc)[1]:
            store.save_arc_summary(sid, arc, " ".join([f"Arc {arc} summary sentence {i}." for i in range(40)]))
