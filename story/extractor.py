"""Turns episode text into a structured EpisodeDelta (fast tier), then sanitizes it against current state."""
from __future__ import annotations

from typing import Any

from . import fmt
from .context import mentioned
from .llm import LLM, EpisodeBudget, render_prompt
from .models import Character, CharacterUpdate, EpisodeDelta, Fact, StoryState, Thread, new_id
from .planner import slug

STATUS_SYNONYMS = {"deceased": "dead", "killed": "dead", "died": "dead", "murdered": "dead",
                   "disappeared": "missing", "gone": "missing", "vanished": "missing", "living": "alive"}
UPDATABLE = {"status", "location", "goal", "knows", "relationships", "description", "secret", "role", "voice"}
DEFAULT_THREAD_WINDOW = 40  # improvised threads without a planned resolution are due this many episodes later


def planned_thread_due(plan) -> dict[str, int]:
    """thread slug -> last episode of the arc that is planned to resolve it."""
    due: dict[str, int] = {}
    for arc in plan.arcs:
        for t in arc.threads_resolved:
            due.setdefault(t, arc.ep_end)
    return due


def sanitize(delta: EpisodeDelta, state: StoryState, text: str, ep: int,
             thread_due: dict[str, int] | None = None) -> tuple[EpisodeDelta, list[str]]:
    """Fix ids and values the model got wrong so the delta always applies cleanly. Returns (delta, warnings)."""
    warnings: list[str] = []
    thread_due = thread_due or {}
    by_name = {c.name.lower(): cid for cid, c in state.characters.items()}

    new_chars: list[Character] = []
    for c in delta.new_characters:
        cid = slug(c.id or c.name)
        if cid in state.characters or c.name.lower() in by_name:
            warnings.append(f"'{c.name}' is not new; ignored as a new character")
            continue
        new_chars.append(c.model_copy(update={"id": cid}))
    known = set(state.characters) | {c.id for c in new_chars}
    new_by_name = {c.name.lower(): c.id for c in new_chars}

    updates: dict[str, dict[str, Any]] = {}
    for u in delta.character_updates:
        cid = u.id if u.id in known else by_name.get(u.id.lower()) or new_by_name.get(u.id.lower()) or slug(u.id)
        if cid not in known:
            warnings.append(f"update for unknown character '{u.id}' dropped")
            continue
        changes = {}
        for key, value in u.changes.items():
            if key not in UPDATABLE:
                continue
            if key == "status":
                value = STATUS_SYNONYMS.get(str(value).lower(), str(value).lower())
                if value not in ("alive", "dead", "missing", "unknown"):
                    warnings.append(f"invalid status '{value}' for {cid} dropped")
                    continue
            if key == "relationships":
                if not isinstance(value, dict):
                    continue
                value = {str(k): str(v) for k, v in value.items() if v is not None and str(v).strip()}
                if not value:
                    continue
            if key == "knows":
                if isinstance(value, dict):
                    value = [f"{k}: {v}" for k, v in value.items()]
                elif not isinstance(value, list):
                    value = [value]
                value = [str(x) for x in value if x is not None and str(x).strip()]
                if not value:
                    continue
            if key in ("location", "goal", "description", "secret", "role", "voice") and not isinstance(value, str):
                value = str(value)
            changes[key] = value
        updates.setdefault(cid, {}).update(changes)
    # Everyone named in the text counts as appearing (updates last_seen_ep), even with no other change.
    for cid in mentioned(state.characters, text):
        updates.setdefault(cid, {})

    opened: list[Thread] = []
    advanced = list(delta.threads_advanced)
    for t in delta.threads_opened:
        tid = slug(t.id or t.title)
        if tid in state.threads:
            advanced.append(tid)
            continue
        opened.append(t.model_copy(update={"id": tid, "due_by_ep": t.due_by_ep or thread_due.get(tid, ep + DEFAULT_THREAD_WINDOW)}))
    live = {t.id for t in state.open_threads()} | {t.id for t in opened}
    clean_adv, clean_res = [], []
    for label, ids, out in (("advanced", advanced, clean_adv), ("resolved", delta.threads_resolved, clean_res)):
        for tid in ids:
            tid = tid if tid in live else slug(tid)
            if tid in live:
                if tid not in out:
                    out.append(tid)
            else:
                warnings.append(f"{label} unknown thread '{tid}' dropped")

    fact_ids = {f.id for f in state.facts}
    facts = []
    for f in delta.new_facts:
        supersedes = f.supersedes if f.supersedes in fact_ids else None
        if f.supersedes and not supersedes:
            warnings.append(f"fact supersedes unknown id '{f.supersedes}'")
        facts.append(f.model_copy(update={"entities": [e.lower() for e in f.entities], "supersedes": supersedes,
                                          "ep": ep, "id": f.id if f.id not in fact_ids else new_id()}))

    clean = delta.model_copy(update={
        "ep": ep, "new_characters": new_chars, "new_facts": facts, "threads_opened": opened,
        "threads_advanced": [t for t in clean_adv if t not in clean_res], "threads_resolved": clean_res,
        "character_updates": [CharacterUpdate(id=cid, changes=ch) for cid, ch in updates.items()],
    })
    return clean, warnings


def extract(llm: LLM, text: str, ep: int, state: StoryState, facts: list[Fact], plan,
            budget: EpisodeBudget | None = None) -> tuple[EpisodeDelta, list[str]]:
    arc = plan.arc_for(ep)
    planned = sorted({t for a in plan.arcs if arc and a.arc_no <= arc.arc_no + 1
                      for t in a.threads_opened + a.threads_resolved})
    prompt, version = render_prompt(
        "extract", ep=ep, text=text,
        characters="\n".join(f"{c.id} | {c.name} | {c.status} | {c.location}" for c in state.characters.values()),
        threads="\n".join(f"{t.id}: {t.title}" for t in state.open_threads()) or "(none)",
        planned_threads=", ".join(planned) or "(none)",
        planned_resolutions=", ".join(arc.threads_resolved) if arc and arc.threads_resolved else "(none)",
        facts="\n".join(fmt.fact(f) for f in facts[:30]) or "(none)",
    )
    result = llm.call("extract", "fast", [{"role": "user", "content": prompt}], json_schema=EpisodeDelta,
                      ep=ep, budget=budget, prompt_version=version)
    delta, warnings = sanitize(result.parsed, state, text, ep, planned_thread_due(plan))
    if warnings:
        llm.decision(f"extract_sanitized:{len(warnings)} fixes", ep=ep, warnings=warnings[:10])
    return delta, warnings
