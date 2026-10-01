"""Story operations shared by the CLI and the web UI. No printing, no prompts: callers decide how to show results."""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

from . import export, pipeline, planner
from .config import Settings, load_settings
from .llm import LLM, RunLogger
from .models import ArcPlan, Directive, Episode
from .store import Store

Progress = Callable[[str], None]


def _noop(_: str) -> None:
    pass


class ServiceError(Exception):
    """A user-facing failure (bad input, invalid plan, nothing to do)."""


@dataclass
class Session:
    settings: Settings
    store: Store
    llm: LLM
    story_id: str

    @property
    def out_dir(self) -> Path:
        """Where automatic exports go while working. Only an explicit `story export` writes demo/."""
        return self.settings.runs_dir / self.story_id / "export"

    def autosave(self, *, plan: bool = False, episodes: bool = False, hitl: bool = False) -> None:
        if plan:
            export.export_plan(self.store, self.story_id, self.out_dir)
        if episodes:
            export.export_episodes(self.store, self.story_id, self.out_dir)
        if hitl:
            export.export_hitl_log(self.store, self.story_id, self.out_dir)


def open_session(story_id: str | None = None, settings: Settings | None = None) -> Session:
    settings = settings or load_settings()
    store = Store(settings.db_path)
    story_id = story_id or store.latest_story_id()
    if story_id is None or store.get_story(story_id) is None:
        store.close()
        raise ServiceError("No story found. Start one with a premise.")
    return Session(settings, store, LLM(settings, logger=RunLogger(settings.runs_dir, story_id=story_id)), story_id)


def new_session(premise: str, settings: Settings | None = None) -> Session:
    if not premise.strip():
        raise ServiceError("The premise is empty.")
    settings = settings or load_settings()
    store = Store(settings.db_path)
    llm = LLM(settings, logger=RunLogger(settings.runs_dir))
    story_id = planner.start_story(llm, store, premise.strip())
    return Session(settings, store, llm, story_id)


# --- plan -----------------------------------------------------------------------

def generate_plan(s: Session, progress: Progress = _noop) -> ArcPlan:
    plan = planner.generate_plan(s.llm, s.store, s.story_id, on_progress=progress)
    s.autosave(plan=True)
    return plan


def validate(s: Session, plan: ArcPlan | None = None) -> planner.Validation:
    plan = plan or s.store.latest_plan(s.story_id)
    return planner.validate_plan(plan, list(s.store.state_at(s.story_id, 0).characters))


def save_human_plan(s: Session, new_plan: ArcPlan, reason: str = "human edit") -> tuple[int, list[dict]]:
    """Save a human-edited plan. Changed beats become human_owned (replans will not overwrite them)."""
    v = validate(s, new_plan)
    if not v.ok:
        raise ServiceError("Plan is invalid: " + "; ".join(v.errors[:5]))
    old = s.store.latest_plan(s.story_id)
    old_beats = {b.ep: b for b in old.beats}
    new_plan = new_plan.model_copy(update={"beats": [
        b.model_copy(update={"human_owned": True})
        if (o := old_beats.get(b.ep)) is None or (o.beat, o.characters, o.threads) != (b.beat, b.characters, b.threads)
        else b for b in new_plan.beats]})
    version = s.store.save_plan(s.story_id, new_plan, reason)
    changes = s.store.plan_diff(s.story_id, old.version, version)
    s.store.add_event(s.story_id, s.store.last_approved_ep(s.story_id), "plan_edit",
                      {"from_version": old.version, "to_version": version, "changes": changes})
    s.llm.decision(f"human:plan_edit v{old.version}→v{version} ({len(changes)} changes)")
    s.autosave(plan=True, hitl=True)
    return version, changes


def edit_beat(s: Session, ep: int, text: str, characters: list[str] | None = None) -> tuple[int, list[dict]]:
    plan = s.store.latest_plan(s.story_id)
    if plan.beat(ep) is None:
        raise ServiceError(f"No beat for episode {ep}.")
    if ep <= s.store.last_approved_ep(s.story_id):
        raise ServiceError(f"Episode {ep} is already written; use a retcon to change it.")
    beats = [b.model_copy(update={"beat": text.strip(), **({"characters": characters} if characters is not None else {})})
             if b.ep == ep else b for b in plan.beats]
    return save_human_plan(s, plan.model_copy(update={"beats": beats}), f"human edit of beat {ep}")


def approve_plan(s: Session) -> int:
    plan = s.store.latest_plan(s.story_id)
    v = validate(s, plan)
    if not v.ok:
        raise ServiceError("Fix the plan errors before approving: " + "; ".join(v.errors[:5]))
    s.store.approve_plan(s.story_id, plan.version)
    s.store.set_story_status(s.story_id, "writing")
    s.store.add_event(s.story_id, 0, "plan_approved", {"version": plan.version})
    s.llm.decision(f"human:plan_approve v{plan.version}")
    s.autosave(plan=True, hitl=True)
    return plan.version


def regenerate_act(s: Session, act_no: int, progress: Progress = _noop) -> tuple[int, list[dict]]:
    if not 1 <= act_no <= planner.N_ACTS:
        raise ServiceError(f"Acts are 1-{planner.N_ACTS}.")
    plan = s.store.latest_plan(s.story_id)
    start, end = planner.act_range(act_no)
    if s.store.last_approved_ep(s.story_id) >= start:
        raise ServiceError(f"Act {act_no} already has written episodes; edit beats instead.")
    progress(f"Regenerating act {act_no} beats...")
    base = plan.model_copy(update={"beats": [b for b in plan.beats if b.ep < start]})
    beats = planner.generate_act_beats(s.llm, s.store.latest_bible(s.story_id),
                                       list(s.store.state_at(s.story_id, 0).characters.values()), base, act_no)
    new_plan = plan.model_copy(update={"beats": sorted([b for b in plan.beats if not start <= b.ep <= end] + beats,
                                                       key=lambda b: b.ep)})
    version = s.store.save_plan(s.story_id, new_plan, f"regenerated act {act_no} beats")
    changes = s.store.plan_diff(s.story_id, plan.version, version)
    s.store.add_event(s.story_id, 0, "plan_regenerate", {"act": act_no, "from_version": plan.version,
                                                         "to_version": version, "changes": changes})
    s.autosave(plan=True, hitl=True)
    return version, changes


# --- episodes -----------------------------------------------------------------------

def next_episode(s: Session) -> int:
    return s.store.last_approved_ep(s.story_id) + 1


def produce(s: Session, ep: int | None = None, note: str | None = None, progress: Progress = _noop) -> Episode:
    """The pending draft for the next episode, generating one only if none exists (never pay twice)."""
    ep = ep or next_episode(s)
    if s.store.latest_plan(s.story_id, approved_only=True) is None:
        raise ServiceError("Approve the plan first.")
    if ep != next_episode(s):
        raise ServiceError(f"The next episode to write is {next_episode(s)}.")
    draft = s.store.get_draft(s.story_id, ep)
    if draft and note is None:
        s.llm.decision("resume:reuse_draft", ep=ep)
        return draft
    return pipeline.produce_episode(s.llm, s.store, s.settings, s.story_id, ep, note=note, on_progress=progress)


def save_human_edit(s: Session, draft: Episode, text: str) -> Episode:
    """Persist a human edit as a new draft before any LLM call, so a failed approval never loses it."""
    new = s.store.save_draft(s.story_id, draft.ep, text.strip(), None,
                             {**(draft.checks or {}), "human_edited_pending": True,
                              "edited_from_version": draft.version}, draft.cost_usd)
    s.store.set_episode_status(s.story_id, draft.ep, draft.version, "superseded")
    return new


def approve(s: Session, draft: Episode, *, auto: bool = False, original: Episode | None = None,
            progress: Progress = _noop) -> tuple[Episode, pipeline.BoundaryReport | None]:
    """Commit a draft (re-extracting memory if a human edited it). Raises on failure; the draft stays saved."""
    human_edited = bool((draft.checks or {}).get("human_edited_pending"))
    if human_edited:
        progress("Re-extracting memory from your edited text...")
        if original is None and (draft.checks or {}).get("edited_from_version"):
            original = s.store.get_episode(s.story_id, draft.ep, draft.checks["edited_from_version"])
    try:
        delta = pipeline.ensure_delta(s.llm, s.store, s.story_id, draft.ep, draft.text, draft.delta,
                                      reextract=human_edited)
        episode, boundary = pipeline.commit(s.llm, s.store, s.story_id, draft.ep, draft.text, delta,
                                            human_edited=human_edited, checks_json=draft.checks,
                                            cost_usd=draft.cost_usd, on_progress=progress)
    except Exception as e:
        s.llm.decision(f"approve_failed:{type(e).__name__}", ep=draft.ep)
        raise
    s.llm.decision("auto:approve" if auto else ("human:edit+approve" if human_edited else "human:approve"), ep=draft.ep)
    if human_edited:
        before = original or draft
        s.store.add_event(s.story_id, draft.ep, "human_edit", {
            "version": episode.version, "words_before": before.word_count, "words_after": episode.word_count,
            "similarity": SequenceMatcher(None, before.text, draft.text).ratio()})
    s.autosave(episodes=True, hitl=True)
    return episode, boundary


def reject(s: Session, draft: Episode, reason: str, progress: Progress = _noop) -> Episode:
    if not reason.strip():
        raise ServiceError("Give a reason; it goes into the rewrite.")
    s.store.set_episode_status(s.story_id, draft.ep, draft.version, "rejected")
    s.store.add_event(s.story_id, draft.ep, "reject", {"version": draft.version, "reason": reason,
                                                       "summary": draft.delta.summary if draft.delta else None})
    s.llm.decision("human:reject→regenerate", ep=draft.ep, reason=reason)
    s.autosave(hitl=True)
    return pipeline.produce_episode(s.llm, s.store, s.settings, s.story_id, draft.ep, on_progress=progress,
                                    note=f"A previous draft was rejected by the editor. Reason: {reason}")


def record_boundary(s: Session, rep: pipeline.BoundaryReport, decision: str, auto: bool = False) -> None:
    """decision: accepted | reverted | accepted+edited | none. Reverting restores the previous beats."""
    if decision == "reverted":
        pipeline.revert_replan(s.store, s.story_id, rep)
    if rep.replan_to:
        s.llm.decision(f"human:replan_{decision}", ep=rep.arc_no * 10)
    s.store.add_event(s.story_id, s.store.last_approved_ep(s.story_id), "arc_boundary", {
        "arc_no": rep.arc_no, "summary": rep.summary, "corrections": rep.corrections, "replan_from": rep.replan_from,
        "replan_to": rep.replan_to, "changes_count": len(rep.replan_changes), "decision": decision, "auto_mode": auto})
    s.autosave(plan=True, hitl=True)


# --- directives -----------------------------------------------------------------------

def update_directive(s: Session, directive_id: str, *, text: str | None = None, active: bool | None = None) -> Directive:
    d = next((x for x in s.store.directives(s.story_id, active_only=False) if x.id == directive_id), None)
    if d is None:
        raise ServiceError(f"Unknown directive {directive_id}.")
    ep = next_episode(s)
    if text is not None and text.strip() and text.strip() != d.text:
        s.store.save_directive(s.story_id, d.model_copy(update={"text": text.strip()}))
        s.store.add_event(s.story_id, ep, "directive_edit", {"id": d.id, "old": d.text, "new": text.strip()})
        s.llm.decision(f"human:directive_edit {d.id}", ep=ep)
        d = d.model_copy(update={"text": text.strip()})
    if active is not None and active != d.active:
        s.store.set_directive_active(s.story_id, d.id, active)
        s.store.add_event(s.story_id, ep, "directive_off" if not active else "directive_on", {"id": d.id})
        s.llm.decision(f"human:directive_{'off' if not active else 'on'} {d.id}", ep=ep)
        d = d.model_copy(update={"active": active})
    s.autosave(hitl=True)
    return d
