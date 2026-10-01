"""Retroactive edits: rewrite an approved episode, then find and fix the later episodes that relied on it."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher

from pydantic import BaseModel

from . import extractor, writer
from .context import build_context, name_pattern
from .llm import LLM, render_prompt
from .models import EpisodeDelta, Fact, StoryState
from .store import Store

FACT_MATCH = 0.75  # facts this similar across versions are "the same fact"
MAX_EPISODES_CHECKED = 20


class ConflictCheck(BaseModel):
    conflict: bool
    evidence: str = ""
    fix: str = ""


@dataclass
class RetconReport:
    ep: int
    old_version: int
    new_version: int
    removed_facts: list[str] = field(default_factory=list)
    added_facts: list[str] = field(default_factory=list)
    character_changes: dict[str, dict] = field(default_factory=dict)
    thread_changes: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    affected: list[int] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)  # {ep, evidence, fix}
    check_errors: list[dict] = field(default_factory=list)  # {ep, error}: checks that failed; re-run edit-episode

    @property
    def first_conflict(self) -> int | None:
        return min((c["ep"] for c in self.conflicts), default=None)


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _unmatched(a: list[Fact], b: list[Fact]) -> list[Fact]:
    return [f for f in a if not any(SequenceMatcher(None, _norm(f.text), _norm(g.text)).ratio() >= FACT_MATCH for g in b)]


def delta_diff(old: EpisodeDelta, new: EpisodeDelta) -> dict:
    removed, added = _unmatched(old.new_facts, new.new_facts), _unmatched(new.new_facts, old.new_facts)
    old_upd = {u.id: u.changes for u in old.character_updates}
    new_upd = {u.id: u.changes for u in new.character_updates}
    chars = {cid: {"old": old_upd.get(cid, {}), "new": new_upd.get(cid, {})}
             for cid in old_upd.keys() | new_upd.keys() if old_upd.get(cid, {}) != new_upd.get(cid, {})}
    for c in old.new_characters:
        if c.id not in {x.id for x in new.new_characters}:
            chars[c.id] = {"old": {"introduced": True}, "new": {"introduced": False}}
    for c in new.new_characters:
        if c.id not in {x.id for x in old.new_characters}:
            chars[c.id] = {"old": {"introduced": False}, "new": {"introduced": True}}
    threads = []
    for label, o, n in (("opened", {t.id for t in old.threads_opened}, {t.id for t in new.threads_opened}),
                        ("advanced", set(old.threads_advanced), set(new.threads_advanced)),
                        ("resolved", set(old.threads_resolved), set(new.threads_resolved))):
        threads += [f"no longer {label}: {t}" for t in o - n] + [f"now {label}: {t}" for t in n - o]
    entities = {e for f in removed + added for e in f.entities} | set(chars) | {t.split(": ", 1)[1] for t in threads}
    return {"removed": removed, "added": added, "characters": chars, "threads": threads, "entities": sorted(entities)}


def affected_episodes(store: Store, story_id: str, ep: int, entities: list[str], state: StoryState) -> list[int]:
    """Later approved episodes whose delta or text mentions a changed entity.

    Entities that appear in most later episodes (the protagonist, the main setting) don't discriminate, so they are
    ignored when rarer changed entities exist; otherwise one edit to the hero's record would re-check the whole story.
    """
    later = store.approved_episodes(story_id, ep + 1)

    def hits(entity: str) -> set[int]:
        e_low = entity.lower()
        pat = name_pattern(state.characters[entity]) if entity in state.characters else None
        word = e_low.replace("-", " ") if entity not in state.characters and len(entity) >= 4 else None
        out = set()
        for e in later:
            d = e.delta
            in_delta = d is not None and (
                e_low in {x.lower() for f in d.new_facts for x in f.entities}
                or e_low in {u.id for u in d.character_updates if u.changes}
                or e_low in set(d.threads_advanced + d.threads_resolved))
            if in_delta or (pat and pat.search(e.text)) or (word and word in e.text.lower()):
                out.add(e.ep)
        return out

    found = {e: hits(e) for e in entities}
    everything = sorted(set().union(*found.values())) if found else []
    if len(later) < 6:
        return everything
    common = {e for e, eps in found.items() if len(eps) > len(later) / 2}
    specific_hits = set().union(*(found[e] for e in entities if e not in common)) if len(common) < len(entities) else set()
    return sorted(specific_hits) if specific_hits else everything


def retcon(llm: LLM, store: Store, story_id: str, ep: int, new_text: str) -> RetconReport:
    old = store.approved_episode(story_id, ep)
    if old is None:
        raise ValueError(f"episode {ep} is not approved; use review instead")
    # Characters as they were before the edit, so a removed or renamed character is still searched for.
    before = store.state_at(story_id, store.last_approved_ep(story_id))
    ctx = build_context(store, story_id, ep)
    new_delta, _ = extractor.extract(llm, new_text, ep, ctx.state, ctx.selected_facts, ctx.plan)
    diff = delta_diff(old.delta or EpisodeDelta(summary="", hook="", hook_type="question"), new_delta)
    committed = store.commit_episode(story_id, ep, new_text, new_delta, human_edited=True,
                                     checks={"retcon_of_version": old.version})
    report = RetconReport(ep=ep, old_version=old.version, new_version=committed.version,
                          removed_facts=[f.text for f in diff["removed"]], added_facts=[f.text for f in diff["added"]],
                          character_changes=diff["characters"], thread_changes=diff["threads"],
                          entities=diff["entities"])
    llm.decision(f"human:retcon ep{ep} v{old.version}→v{committed.version} "
                 f"({len(report.removed_facts)} facts removed, {len(report.added_facts)} added)", ep=ep)

    state = store.state_at(story_id, store.last_approved_ep(story_id))
    state.characters = {**before.characters, **state.characters}
    report.affected = affected_episodes(store, story_id, ep, report.entities, state)
    for later_ep in report.affected[:MAX_EPISODES_CHECKED]:
        later = store.approved_episode(story_id, later_ep)
        prompt, version = render_prompt(
            "retcon_check", ep=ep, later_ep=later_ep, text=later.text, new_summary=new_delta.summary,
            removed="\n".join(f"- {t}" for t in report.removed_facts) or "(none)",
            added="\n".join(f"- {t}" for t in report.added_facts) or "(none)",
            characters="\n".join(f"- {k}: {v['old']} → {v['new']}" for k, v in report.character_changes.items()) or "(none)",
        )
        try:
            check = llm.call("retcon_check", "fast", [{"role": "user", "content": prompt}], json_schema=ConflictCheck,
                             ep=later_ep, prompt_version=version).parsed
        except Exception as e:  # quota / API / bad JSON: keep going, record which episodes were not checked
            report.check_errors.append({"ep": later_ep, "error": f"{type(e).__name__}: {str(e)[:160]}"})
            continue
        if check.conflict:
            report.conflicts.append({"ep": later_ep, "evidence": check.evidence, "fix": check.fix})
    llm.decision(f"retcon_checked:{len(report.affected)} affected,{len(report.conflicts)} conflicts", ep=ep)
    store.add_event(story_id, ep, "retcon", {**asdict(report), "resolution": None})
    return report


def patch_episode(llm: LLM, store: Store, story_id: str, ep: int, problems: list[str]) -> int:
    """Targeted rewrite of one later episode to remove the conflicts; re-extracted and committed as a new version."""
    current = store.approved_episode(story_id, ep)
    ctx = build_context(store, story_id, ep)
    text = writer.revise(llm, ctx, current.text, problems).text
    delta, _ = extractor.extract(llm, text, ep, ctx.state, ctx.selected_facts, ctx.plan)
    return store.commit_episode(story_id, ep, text, delta, checks={"patched_for_retcon": problems}).version


def resolve(store: Store, story_id: str, report: RetconReport, resolution: str, detail: dict | None = None) -> None:
    store.add_event(story_id, report.ep, "retcon_resolution", {"resolution": resolution, **(detail or {})})
