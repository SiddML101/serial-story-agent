"""Hierarchical planning: bible + cast + 5 acts + 20 arcs in one call, then 40 beats per act."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml
from pydantic import BaseModel

from . import fmt
from .config import DEFAULT_BANNED_PHRASES, TOTAL_EPISODES
from .llm import LLM, render_prompt
from .models import Act, Arc, ArcPlan, Beat, Bible, Character, StoryState
from .store import Store

N_ACTS, N_ARCS = 5, 20
EPS_PER_ACT, EPS_PER_ARC = TOTAL_EPISODES // N_ACTS, TOTAL_EPISODES // N_ARCS
HOOK_TYPES = ("cliffhanger", "revelation", "arrival", "reversal", "decision", "threat", "question")


class PlanError(Exception):
    pass


class Foundation(BaseModel):
    bible: Bible
    characters: list[Character]
    acts: list[Act]
    arcs: list[Arc]
    character_arcs: dict[str, str] = {}


class ActBeats(BaseModel):
    beats: list[Beat]


def act_range(act_no: int) -> tuple[int, int]:
    return (act_no - 1) * EPS_PER_ACT + 1, act_no * EPS_PER_ACT


def arc_range(arc_no: int) -> tuple[int, int]:
    return (arc_no - 1) * EPS_PER_ARC + 1, arc_no * EPS_PER_ARC


def arc_no_for(ep: int) -> int:
    return (ep - 1) // EPS_PER_ARC + 1


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"


# --- generation -------------------------------------------------------------

def normalize_foundation(f: Foundation) -> Foundation:
    """Enforce the fixed 5x40 / 20x10 structure so later steps can rely on it."""
    if len(f.acts) != N_ACTS or len(f.arcs) != N_ARCS:
        raise PlanError(f"expected {N_ACTS} acts and {N_ARCS} arcs, got {len(f.acts)} and {len(f.arcs)}")
    acts = [a.model_copy(update={"act_no": i + 1, "ep_start": act_range(i + 1)[0], "ep_end": act_range(i + 1)[1]})
            for i, a in enumerate(f.acts)]
    arcs = []
    for i, a in enumerate(f.arcs):
        start, end = arc_range(i + 1)
        arcs.append(a.model_copy(update={
            "arc_no": i + 1, "act_no": (start - 1) // EPS_PER_ACT + 1, "ep_start": start, "ep_end": end,
            "threads_opened": [slug(t) for t in a.threads_opened],
            "threads_resolved": [slug(t) for t in a.threads_resolved],
        }))
    chars = [c.model_copy(update={"id": slug(c.id), "status": "alive", "first_ep": 0, "last_seen_ep": 0})
             for c in f.characters]
    return f.model_copy(update={"acts": acts, "arcs": arcs, "characters": chars})


def generate_foundation(llm: LLM, premise: str) -> Foundation:
    prompt, version = render_prompt(
        "bible", premise=premise, total=TOTAL_EPISODES,
        banned="; ".join(DEFAULT_BANNED_PHRASES),
        act_ranges=", ".join(f"act {i}: {'-'.join(map(str, act_range(i)))}" for i in range(1, N_ACTS + 1)),
        arc_ranges=", ".join(f"arc {i}: {'-'.join(map(str, arc_range(i)))}" for i in range(1, N_ARCS + 1)),
    )
    result = llm.call("plan_foundation", "writer", [{"role": "user", "content": prompt}],
                      json_schema=Foundation, prompt_version=version)
    return normalize_foundation(result.parsed)


def generate_act_beats(llm: LLM, bible: Bible, cast: list[Character], plan: ArcPlan, act_no: int) -> list[Beat]:
    start, end = act_range(act_no)
    n = end - start + 1
    prev = [b for b in plan.beats if b.ep < start][-EPS_PER_ACT:]
    prompt, version = render_prompt(
        "plan_beats", act_no=act_no, total=TOTAL_EPISODES, bible=fmt.bible(bible),
        cast="\n".join(fmt.character(c, full=False) + f" | goal: {c.goal}" for c in cast),
        acts=fmt.acts(plan.acts), arcs="\n".join(fmt.arc(a) for a in plan.arcs if a.act_no == act_no),
        prev_beats=fmt.beats(prev), n=n, ep_start=start, ep_end=end,
    )
    messages = [{"role": "user", "content": prompt}]
    for attempt in (1, 2):
        result = llm.call(f"plan_beats_act{act_no}", "writer", messages, json_schema=ActBeats, prompt_version=version)
        beats = sorted(result.parsed.beats, key=lambda b: b.ep)
        if len(beats) == n:
            # Trust the order, not the model's numbering.
            return [b.model_copy(update={"ep": start + i, "arc_no": arc_no_for(start + i)}) for i, b in enumerate(beats)]
        llm.decision(f"plan_beats_act{act_no}:wrong_count:{len(beats)}→retry")
        messages = messages + [
            {"role": "assistant", "content": result.text},
            {"role": "user", "content": f"You returned {len(beats)} beats; exactly {n} are required "
                                        f"(episodes {start}-{end}). Return the full corrected JSON."},
        ]
    raise PlanError(f"act {act_no}: could not get exactly {n} beats")


def start_story(llm: LLM, store: Store, premise: str) -> str:
    story_id = store.create_story(premise)
    llm.logger.story_id = story_id
    return story_id


def generate_plan(llm: LLM, store: Store, story_id: str, on_progress=lambda msg: None) -> ArcPlan:
    """Generate (or resume generating) the full plan. Each step is saved as a plan version, so a crash loses nothing."""
    plan = store.latest_plan(story_id)
    bible = store.latest_bible(story_id)
    if plan is None or bible is None:
        on_progress("Designing bible, cast, acts and arcs...")
        f = generate_foundation(llm, store.get_story(story_id)["premise"])
        store.save_bible(story_id, f.bible)
        store.set_initial_state(story_id, StoryState(characters={c.id: c for c in f.characters}))
        plan = ArcPlan(acts=f.acts, arcs=f.arcs, beats=[], character_arcs=f.character_arcs)
        plan.version = store.save_plan(story_id, plan, "draft: foundation (acts + arcs)")
        bible = f.bible
    cast = list(store.state_at(story_id, 0).characters.values())
    for act_no in range(1, N_ACTS + 1):
        start, _ = act_range(act_no)
        if any(b.ep >= start for b in plan.beats):
            continue
        on_progress(f"Writing beats for act {act_no} of {N_ACTS}...")
        beats = generate_act_beats(llm, bible, cast, plan, act_no)
        plan = plan.model_copy(update={"beats": plan.beats + beats})
        plan.version = store.save_plan(story_id, plan, f"draft: act {act_no} beats")
    store.set_story_status(story_id, "plan_review")
    return plan


# --- validation -------------------------------------------------------------

@dataclass
class Validation:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def near_duplicates(texts: list[str], threshold: float) -> list[tuple[int, int, float]]:
    """Index pairs whose TF-IDF cosine similarity is above `threshold`."""
    if len(texts) < 2:
        return []
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity

    try:
        matrix = TfidfVectorizer(stop_words="english").fit_transform(texts)
    except ValueError:  # empty vocabulary
        return []
    sims = cosine_similarity(matrix)
    return [(i, j, float(sims[i, j])) for i in range(len(texts)) for j in range(i + 1, len(texts))
            if sims[i, j] > threshold]


def validate_plan(plan: ArcPlan, main_cast: list[str]) -> Validation:
    v = Validation()
    eps = [b.ep for b in plan.beats]
    if len(plan.beats) != TOTAL_EPISODES:
        v.errors.append(f"plan has {len(plan.beats)} beats; exactly {TOTAL_EPISODES} required")
    if sorted(eps) != list(range(1, len(eps) + 1)):
        missing = sorted(set(range(1, TOTAL_EPISODES + 1)) - set(eps))[:10]
        dupes = sorted({e for e in eps if eps.count(e) > 1})[:10]
        v.errors.append(f"beats are not contiguous from 1 (missing {missing}, duplicated {dupes})")

    for label, items, key in (("act", plan.acts, "act_no"), ("arc", plan.arcs, "arc_no")):
        items = sorted(items, key=lambda x: x.ep_start)
        expected = 1
        for x in items:
            if x.ep_start != expected or x.ep_end < x.ep_start:
                v.errors.append(f"{label} {getattr(x, key)} range {x.ep_start}-{x.ep_end} breaks contiguity "
                                f"(expected start {expected})")
            expected = x.ep_end + 1
        if items and expected - 1 != TOTAL_EPISODES:
            v.errors.append(f"{label}s end at ep {expected - 1}, not {TOTAL_EPISODES}")

    acts = {a.act_no: a for a in plan.acts}
    for a in plan.arcs:
        act = acts.get(a.act_no)
        if act is None or not (act.ep_start <= a.ep_start and a.ep_end <= act.ep_end):
            v.errors.append(f"arc {a.arc_no} (eps {a.ep_start}-{a.ep_end}) is not inside act {a.act_no}")
    for b in plan.beats:
        arc = plan.arc_for(b.ep)
        if arc and b.arc_no != arc.arc_no:
            v.errors.append(f"ep {b.ep} says arc {b.arc_no} but falls in arc {arc.arc_no}")

    opened = {t: a.arc_no for a in plan.arcs for t in a.threads_opened}
    resolved: dict[str, int] = {}
    for a in plan.arcs:
        for t in a.threads_resolved:
            resolved.setdefault(t, a.arc_no)
    for t, arc_no in opened.items():
        if t not in resolved:
            v.warnings.append(f"thread '{t}' (opened arc {arc_no}) is never resolved")
        elif resolved[t] < arc_no:
            v.warnings.append(f"thread '{t}' is resolved in arc {resolved[t]} before it opens in arc {arc_no}")
    for t, arc_no in resolved.items():
        if t not in opened:
            v.warnings.append(f"thread '{t}' is resolved in arc {arc_no} but never opened")

    for act in plan.acts:
        present = {c for b in plan.beats if act.ep_start <= b.ep <= act.ep_end for c in b.characters}
        absent = [c for c in main_cast if c not in present]
        if absent:
            v.warnings.append(f"act {act.act_no}: main character(s) never appear: {', '.join(absent)}")

    for i, j, sim in near_duplicates([b.beat for b in plan.beats], 0.6)[:10]:
        v.warnings.append(f"eps {plan.beats[i].ep} and {plan.beats[j].ep} look like near-duplicate beats ({sim:.2f})")

    hooks = [b.hook_type_hint for b in sorted(plan.beats, key=lambda b: b.ep)]
    for i in range(2, len(hooks)):
        if hooks[i] and hooks[i] == hooks[i - 1] == hooks[i - 2]:
            v.warnings.append(f"eps {i - 1}-{i + 1}: hook type '{hooks[i]}' three times in a row")
    return v


# --- YAML round trip for human edits -----------------------------------------

YAML_HEADER = """\
# Edit the plan below and save. Keep 5 acts, 20 arcs and exactly 200 beats.
# Beats are the per-episode plan; change the text, characters, threads or hook_type_hint freely.
# Allowed hook types: cliffhanger, revelation, arrival, reversal, decision, threat, question
"""


def plan_to_yaml(plan: ArcPlan) -> str:
    data = plan.model_dump(exclude={"version"})
    return YAML_HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110, default_flow_style=None)


def plan_from_yaml(text: str) -> ArcPlan:
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise PlanError("plan YAML must be a mapping with acts, arcs, beats")
    return ArcPlan.model_validate(data)
