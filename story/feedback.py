"""Routes human feedback into durable objects: directives, plan edits, state edits (fast tier)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from . import extractor, fmt
from .config import TOTAL_EPISODES
from .llm import LLM, render_prompt
from .models import CharacterUpdate, Directive, EpisodeDelta, Feedback, utcnow
from .store import Store

EPISODE_WINDOW = 20  # beats the router may rewrite during writing


class DirectiveOut(BaseModel):
    text: str
    scope: str = "global"


class PlanEdit(BaseModel):
    ep: int
    new_beat: str
    characters: list[str] | None = None


class StateEdit(BaseModel):
    character_id: str
    changes: dict[str, Any]


class RoutedFeedback(BaseModel):
    directives: list[DirectiveOut] = []
    plan_edits: list[PlanEdit] = []
    state_edits: list[StateEdit] = []
    regenerate_current: bool = False
    explanation: str = ""


@dataclass
class Applied:
    feedback_id: str
    directive_ids: list[str] = field(default_factory=list)
    plan_from: int | None = None
    touched_human_beats: list[int] = field(default_factory=list)
    plan_to: int | None = None
    plan_changes: list[dict] = field(default_factory=list)
    state_edits: list[dict] = field(default_factory=list)


def editable_range(stage: Literal["plan", "episode"], next_ep: int) -> tuple[int, int]:
    if stage == "plan":
        return 1, TOTAL_EPISODES
    return next_ep, min(TOTAL_EPISODES, next_ep + EPISODE_WINDOW - 1)


def route(llm: LLM, store: Store, story_id: str, text: str, stage: Literal["plan", "episode"],
          next_ep: int) -> RoutedFeedback:
    plan = store.latest_plan(story_id)
    state = store.state_at(story_id, next_ep - 1)
    lo, hi = editable_range(stage, next_ep)
    prompt, version = render_prompt(
        "feedback_router", feedback=text, stage=stage, ep=next_ep, from_ep=lo, to_ep=hi,
        characters="\n".join(f"{c.id} | {c.name} | {c.status} | {c.role}" for c in state.characters.values()),
        directives="\n".join(fmt.directive(d) for d in store.directives(story_id)) or "(none)",
        beats=fmt.beats([b for b in plan.beats if lo <= b.ep <= hi]),
    )
    routed = llm.call("feedback_router", "fast", [{"role": "user", "content": prompt}], json_schema=RoutedFeedback,
                      ep=next_ep, prompt_version=version).parsed
    # Never let a routed edit touch episodes outside the editable window (e.g. already-approved canon).
    routed.plan_edits = [e for e in routed.plan_edits if lo <= e.ep <= hi]
    routed.state_edits = [s for s in routed.state_edits if s.character_id in state.characters]
    llm.decision(f"feedback_routed:{len(routed.directives)} directives,{len(routed.plan_edits)} plan edits,"
                 f"{len(routed.state_edits)} state edits", ep=next_ep)
    return routed


def apply(llm: LLM, store: Store, story_id: str, text: str, routed: RoutedFeedback, stage: str,
          next_ep: int) -> Applied:
    fb = Feedback(ep=next_ep, text=text, routed=routed.model_dump(), applied_at=utcnow())
    store.save_feedback(story_id, fb)
    applied = Applied(fb.id)

    for d in routed.directives:
        directive = Directive(text=d.text, scope=d.scope or "global", created_ep=next_ep, source_feedback_id=fb.id)
        store.save_directive(story_id, directive)
        applied.directive_ids.append(directive.id)

    if routed.plan_edits:
        plan = store.latest_plan(story_id)
        edits = {e.ep: e for e in routed.plan_edits}
        applied.touched_human_beats = sorted(b.ep for b in plan.beats if b.ep in edits and b.human_owned)
        beats = []
        for b in plan.beats:
            e = edits.get(b.ep)
            beats.append(b if e is None else b.model_copy(update={
                "beat": e.new_beat, **({"characters": e.characters} if e.characters is not None else {})}))
        version = store.save_plan(story_id, plan.model_copy(update={"beats": beats}), f"feedback: {text[:80]}")
        applied.plan_from, applied.plan_to = plan.version, version
        applied.plan_changes = store.plan_diff(story_id, plan.version, version)

    last = store.last_approved_ep(story_id)
    if routed.state_edits:
        state = store.state_at(story_id, last)
        for s in routed.state_edits:
            probe = EpisodeDelta(summary="", hook="", hook_type="question",
                                 character_updates=[CharacterUpdate(id=s.character_id, changes=s.changes)])
            clean, _ = extractor.sanitize(probe, state, "", last)
            changes = next((u.changes for u in clean.character_updates if u.id == s.character_id and u.changes), None)
            if changes:
                store.add_state_edit(story_id, last, CharacterUpdate(id=s.character_id, changes=changes),
                                     source=f"feedback:{fb.id}")
                applied.state_edits.append({"character_id": s.character_id, "changes": changes})

    store.add_event(story_id, next_ep, "feedback", {
        "feedback_id": fb.id, "stage": stage, "text": text, "explanation": routed.explanation,
        "directives": [d.model_dump() for d in routed.directives], "plan_from": applied.plan_from,
        "plan_to": applied.plan_to, "plan_changes": applied.plan_changes, "state_edits": applied.state_edits,
        "regenerate_current": routed.regenerate_current,
    })
    llm.decision(f"human:feedback_applied:{len(applied.directive_ids)} directives,"
                 f"{len(applied.plan_changes)} plan changes,{len(applied.state_edits)} state edits", ep=next_ep)
    return applied
