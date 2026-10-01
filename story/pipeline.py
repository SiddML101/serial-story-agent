"""Per-episode loop (draft → extract → check → revise, bounded) and the commit path with arc-boundary work."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from pydantic import BaseModel

from . import checks, extractor, fmt, planner, writer
from .config import Settings
from .context import Context, build_context
from .llm import LLM, BudgetExceeded, EpisodeBudget, render_prompt
from .models import Beat, CharacterUpdate, Episode, EpisodeDelta, ThreadUpdate
from .store import Store

Progress = Callable[[str], None]


def _noop(_: str) -> None:
    pass


# --- produce a reviewed draft ----------------------------------------------------

def produce_episode(llm: LLM, store: Store, settings: Settings, story_id: str, ep: int, *,
                    note: str | None = None, on_progress: Progress = _noop) -> Episode:
    """Draft, extract, check and revise up to MAX_REVISIONS within the per-episode cost cap. Saves a draft.

    Nothing touches story state here; the human (or autopilot) approves the draft via `commit`.
    """
    ctx = build_context(store, story_id, ep)
    llm.decision(f"context_built:{ctx.total_tokens}_tokens", ep=ep, knobs=ctx.knobs.__dict__)
    budget = EpisodeBudget(settings.max_cost_per_episode_usd)
    text: str | None = None
    delta: EpisodeDelta | None = None
    result: dict[str, Any] = {"passed": False, "failures": [], "rules": [], "critic": None}
    history: list[dict] = []
    needs_human = False
    try:
        on_progress(f"Ep {ep}: drafting...")
        text = writer.draft(llm, ctx, budget, note=note).text
        for revision in range(settings.max_revisions + 1):
            on_progress(f"Ep {ep}: extracting + checking (pass {revision + 1})...")
            delta = None
            delta, _ = extractor.extract(llm, text, ep, ctx.state, ctx.selected_facts, ctx.plan, budget)
            result = checks.run_checks(llm, text, delta, ctx, budget)
            history.append({"revision": revision, "words": checks.word_count(text), "passed": result["passed"],
                            "failures": result["failures"]})
            if result["passed"]:
                llm.decision("checks_passed" + (f"_after_{revision}_revisions" if revision else ""), ep=ep)
                break
            names = ",".join(sorted({f.split(":")[0].split(" (")[0] for f in result["failures"]}))
            if revision == settings.max_revisions:
                llm.decision(f"check_failed:{names}→max_revisions→needs_human", ep=ep)
                needs_human = True
                break
            llm.decision(f"check_failed:{names}→revise", ep=ep)
            on_progress(f"Ep {ep}: revising ({names})...")
            text = writer.revise(llm, ctx, text, result["failures"], budget).text
            delta = None
            result = {"passed": False, "failures": ["revised text not re-checked yet"], "rules": [], "critic": None}
    except BudgetExceeded as e:
        needs_human = True
        result["failures"] = result.get("failures", []) + [f"budget: {e}"]
        if text is None:
            raise
    except Exception as e:  # a later step failed (quota, API, bad JSON): keep the paid-for draft for the human
        if text is None:
            raise
        needs_human = True
        result["failures"] = result.get("failures", []) + [f"pipeline error: {type(e).__name__}: {str(e)[:200]}"]
        llm.decision(f"step_failed:{type(e).__name__}→needs_human", ep=ep)
    checks_out = {**result, "needs_human": needs_human, "history": history, "revisions": max(0, len(history) - 1),
                  "list_price_usd": round(budget.spent_usd, 6), "context_tokens": ctx.total_tokens,
                  "beat": ctx.beat.beat if ctx.beat else None, "note": note}
    return store.save_draft(story_id, ep, text, delta, checks_out, cost_usd=budget.spent_usd)


# --- commit ------------------------------------------------------------------------

@dataclass
class BoundaryReport:
    arc_no: int
    summary: str
    corrections: list[dict] = field(default_factory=list)
    replan_from: int | None = None
    replan_to: int | None = None
    replan_changes: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def ensure_delta(llm: LLM, store: Store, story_id: str, ep: int, text: str, delta: EpisodeDelta | None,
                 reextract: bool) -> EpisodeDelta:
    """Human-edited text (or a draft whose extraction was cut off) is re-extracted so memory matches canon."""
    if delta is not None and not reextract:
        return delta
    ctx = build_context(store, story_id, ep)
    delta, _ = extractor.extract(llm, text, ep, ctx.state, ctx.selected_facts, ctx.plan)
    return delta


def commit(llm: LLM, store: Store, story_id: str, ep: int, text: str, delta: EpisodeDelta, *,
           human_edited: bool = False, checks_json: dict | None = None, cost_usd: float = 0.0,
           on_progress: Progress = _noop) -> tuple[Episode, BoundaryReport | None]:
    episode = store.commit_episode(story_id, ep, text, delta, human_edited=human_edited, checks=checks_json,
                                   cost_usd=cost_usd)
    plan = store.latest_plan(story_id)
    arc = plan.arc_for(ep)
    report = None
    if arc and ep == arc.ep_end:
        report = arc_boundary(llm, store, story_id, arc.arc_no, on_progress)
    return episode, report


# --- arc boundary: summary, audit, replan -------------------------------------------

class Correction(BaseModel):
    character_id: str
    changes: dict[str, Any]
    reason: str = ""


class ThreadVerdict(BaseModel):
    thread_id: str
    status: Literal["resolved", "abandoned"]
    reason: str = ""


class AuditReport(BaseModel):
    corrections: list[Correction] = []
    thread_updates: list[ThreadVerdict] = []


THREAD_GRACE_EPS = 10  # a planned resolution that slipped is re-dated this far past the arc end, not left "overdue"


class ArcBeats(BaseModel):
    beats: list[Beat]


def summarize_arc(llm: LLM, store: Store, story_id: str, arc_no: int) -> str:
    plan = store.latest_plan(story_id)
    arc = next(a for a in plan.arcs if a.arc_no == arc_no)
    eps = store.approved_episodes(story_id, arc.ep_start, arc.ep_end)
    prompt, version = render_prompt(
        "arc_summary", arc_no=arc_no, arc_title=arc.title, ep_start=arc.ep_start, ep_end=arc.ep_end,
        arc_plan=fmt.arc(arc),
        summaries="\n".join(f"Ep {e.ep}: {e.delta.summary} (ends: {e.delta.hook})" for e in eps if e.delta),
    )
    text = llm.call("arc_summary", "fast", [{"role": "user", "content": prompt}], ep=arc.ep_end,
                    prompt_version=version).text.strip()
    store.save_arc_summary(story_id, arc_no, text)
    return text


def audit_arc(llm: LLM, store: Store, story_id: str, arc_no: int) -> list[dict]:
    """Re-check the character store against the arc's actual text; apply corrections as state edits."""
    plan = store.latest_plan(story_id)
    arc = next(a for a in plan.arcs if a.arc_no == arc_no)
    state = store.state_at(story_id, arc.ep_end)
    eps = store.approved_episodes(story_id, arc.ep_start, arc.ep_end)
    seen = [c for c in state.characters.values() if c.last_seen_ep >= arc.ep_start]
    open_threads = state.open_threads()
    prompt, version = render_prompt(
        "arc_audit", arc_no=arc_no, ep_start=arc.ep_start, ep_end=arc.ep_end,
        characters="\n".join(fmt.character(c) for c in seen),
        threads="\n".join(f"{t.id}: {t.title} — {t.description}" for t in open_threads) or "(none)",
        planned=", ".join(arc.threads_resolved) or "(none)",
        episodes="\n\n".join(f"### Episode {e.ep}\n{e.text}" for e in eps),
    )
    report = llm.call("arc_audit", "fast", [{"role": "user", "content": prompt}], json_schema=AuditReport,
                      ep=arc.ep_end, prompt_version=version).parsed
    applied = []
    for c in report.corrections:
        if c.character_id not in state.characters:
            continue
        upd, _ = extractor.sanitize(
            EpisodeDelta(summary="", hook="", hook_type="question",
                         character_updates=[CharacterUpdate(id=c.character_id, changes=c.changes)]),
            state, "", arc.ep_end)
        changes = next((u.changes for u in upd.character_updates if u.id == c.character_id and u.changes), None)
        if changes:
            store.add_state_edit(story_id, arc.ep_end, CharacterUpdate(id=c.character_id, changes=changes),
                                 source=f"audit:arc{arc_no}")
            applied.append({"character_id": c.character_id, "changes": changes, "reason": c.reason})
    applied += reconcile_threads(llm, store, story_id, arc, state, report.thread_updates)
    llm.decision(f"arc_audit:{len(applied)} corrections", ep=arc.ep_end, corrections=applied)
    return applied


def reconcile_threads(llm: LLM, store: Store, story_id: str, arc, state, verdicts: list) -> list[dict]:
    """Close threads the audit saw answered; re-date planned resolutions that slipped, so OVERDUE stays meaningful."""
    applied = []
    open_ids = {t.id for t in state.open_threads()}
    closed = set()
    for v in verdicts:
        if v.thread_id in open_ids:
            store.add_state_edit(story_id, arc.ep_end, ThreadUpdate(thread_id=v.thread_id, status=v.status,
                                                                    reason=v.reason), source=f"audit:arc{arc.arc_no}")
            closed.add(v.thread_id)
            applied.append({"character_id": f"thread:{v.thread_id}", "changes": {"status": v.status},
                            "reason": v.reason})
    plan = store.latest_plan(story_id)
    later_resolution = {t: a.ep_end for a in plan.arcs if a.arc_no > arc.arc_no for t in a.threads_resolved}
    for t in state.open_threads():
        overdue = t.due_by_ep is not None and t.due_by_ep <= arc.ep_end
        if t.id in closed or not overdue:
            continue
        new_due = later_resolution.get(t.id, arc.ep_end + THREAD_GRACE_EPS)
        store.add_state_edit(story_id, arc.ep_end, ThreadUpdate(thread_id=t.id, due_by_ep=new_due,
                                                                reason="planned resolution slipped"),
                             source=f"audit:arc{arc.arc_no}")
        llm.decision(f"thread_deferred:{t.id}:{t.due_by_ep}→{new_due}", ep=arc.ep_end)
        applied.append({"character_id": f"thread:{t.id}", "changes": {"due_by_ep": new_due},
                        "reason": f"was due by ep {t.due_by_ep}; not resolved on the page"})
    return applied


def replan_next_arc(llm: LLM, store: Store, story_id: str, done_arc: int,
                    keep_upto: int = 0) -> tuple[int, int, list[dict]] | None:
    """Rewrite the next arc's beats from what actually happened. Saved as a new plan version.

    Beats for episodes <= keep_upto (already written) and human-owned beats are never changed.
    """
    plan = store.latest_plan(story_id)
    nxt = next((a for a in plan.arcs if a.arc_no == done_arc + 1), None)
    if nxt is None:
        return None
    after = next((a for a in plan.arcs if a.arc_no == done_arc + 2), None)
    state = store.state_at(story_id, nxt.ep_start - 1)
    current = [b for b in plan.beats if nxt.ep_start <= b.ep <= nxt.ep_end]
    n = nxt.ep_end - nxt.ep_start + 1
    prompt, version = render_prompt(
        "replan_arc", total=len(plan.beats), done_arc=done_arc, arc_no=nxt.arc_no, ep_start=nxt.ep_start,
        ep_end=nxt.ep_end, n=n, bible=fmt.bible(store.latest_bible(story_id)), acts=fmt.acts(plan.acts),
        arc_summaries="\n".join(f"Arc {k}: {v}" for k, v in store.arc_summaries(story_id).items()),
        characters="\n".join(fmt.character(c, full=False) for c in state.characters.values()),
        threads="\n".join(fmt.thread(t, nxt.ep_start) for t in state.open_threads()) or "(none)",
        directives="\n".join(fmt.directive(d) for d in store.directives(story_id)) or "(none)",
        arc=fmt.arc(nxt), beats=fmt.beats(current), after=fmt.arc(after) if after else "(final arc)",
    )
    result = llm.call("replan_arc", "writer", [{"role": "user", "content": prompt}], json_schema=ArcBeats,
                      ep=nxt.ep_start - 1, prompt_version=version)
    beats = sorted(result.parsed.beats, key=lambda b: b.ep)
    if len(beats) != n:
        llm.decision(f"replan_arc{nxt.arc_no}:wrong_count:{len(beats)}→kept_old_beats", ep=nxt.ep_start - 1)
        return None
    owned = {b.ep: b for b in current if b.human_owned or b.ep <= keep_upto}
    beats = [owned.get(nxt.ep_start + i) or b.model_copy(update={"ep": nxt.ep_start + i, "arc_no": nxt.arc_no})
             for i, b in enumerate(beats)]  # human-written beats survive the replan verbatim
    if owned:
        llm.decision(f"replan_arc{nxt.arc_no}:kept {len(owned)} human-owned beats", ep=nxt.ep_start - 1)
    new_plan = plan.model_copy(update={"beats": [b for b in plan.beats if not nxt.ep_start <= b.ep <= nxt.ep_end] + beats})
    new_plan.beats.sort(key=lambda b: b.ep)
    version_no = store.save_plan(story_id, new_plan, f"replan arc {nxt.arc_no} after arc {done_arc}")
    changes = store.plan_diff(story_id, plan.version, version_no)
    llm.decision(f"replan_arc{nxt.arc_no}:v{plan.version}→v{version_no} ({len(changes)} beats changed)",
                 ep=nxt.ep_start - 1)
    return plan.version, version_no, changes


def arc_boundary(llm: LLM, store: Store, story_id: str, arc_no: int, on_progress: Progress = _noop,
                 only: set[str] | None = None) -> BoundaryReport:
    """Each step is independent: a failure is recorded (and can be retried with `story boundary N`), never fatal."""
    report = BoundaryReport(arc_no, summary="")
    steps = (("summary", "summarizing", lambda: summarize_arc(llm, store, story_id, arc_no)),
             ("audit", "auditing character records", lambda: audit_arc(llm, store, story_id, arc_no)),
             ("replan", f"replanning arc {arc_no + 1} from what actually happened",
              lambda: replan_next_arc(llm, store, story_id, arc_no)))
    for name, label, fn in steps:
        if only and name not in only:
            continue
        on_progress(f"Arc {arc_no}: {label}...")
        try:
            out = fn()
        except Exception as e:
            report.errors.append(f"{name}: {type(e).__name__}: {str(e)[:200]}")
            llm.decision(f"arc_boundary_{name}_failed:{type(e).__name__}", ep=arc_no * 10)
            continue
        if name == "summary":
            report.summary = out
        elif name == "audit":
            report.corrections = out
        elif out:
            report.replan_from, report.replan_to, report.replan_changes = out
    return report


def revert_replan(store: Store, story_id: str, report: BoundaryReport) -> int | None:
    """Human rejected the replan: restore the previous beats as a new version (history stays append-only)."""
    if report.replan_from is None:
        return None
    old = store.get_plan(story_id, report.replan_from)
    return store.save_plan(story_id, old, f"human rejected replan v{report.replan_to}; restored v{report.replan_from} beats")
