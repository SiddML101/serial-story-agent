"""Pydantic data models for the story bible, plan, memory stores and episodes."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

HookType = Literal["cliffhanger", "revelation", "arrival", "reversal", "decision", "threat", "question"]
CharacterStatus = Literal["alive", "dead", "missing", "unknown"]
EpisodeStatus = Literal["draft", "approved", "rejected", "superseded"]


def new_id() -> str:
    return uuid4().hex[:8]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- bible & plan ---------------------------------------------------------

class Bible(BaseModel):
    title: str
    logline: str
    genre: str
    tone: str
    pov: str
    tense: str
    setting: str
    world_rules: list[str] = []
    style_guide: list[str] = []
    banned_phrases: list[str] = []


class Character(BaseModel):
    id: str  # slug, e.g. "ravi"
    name: str
    role: str = ""
    description: str = ""
    goal: str = ""
    secret: str = ""
    arc_start: str = ""
    arc_end: str = ""
    status: CharacterStatus = "alive"
    location: str = ""
    knows: list[str] = []
    relationships: dict[str, str] = {}
    voice: str = ""
    first_ep: int = 0
    last_seen_ep: int = 0


class Act(BaseModel):
    act_no: int
    title: str
    ep_start: int
    ep_end: int
    purpose: str
    turning_point: str


class Arc(BaseModel):
    arc_no: int
    act_no: int
    title: str
    ep_start: int
    ep_end: int
    goal: str
    turning_point: str
    character_focus: list[str] = []
    threads_opened: list[str] = []
    threads_resolved: list[str] = []


class Beat(BaseModel):
    ep: int
    arc_no: int
    beat: str
    characters: list[str] = []
    threads: list[str] = []
    hook_type_hint: HookType | None = None
    human_owned: bool = False  # written or edited by a human: automation (replans) must not overwrite it


class ArcPlan(BaseModel):
    acts: list[Act]
    arcs: list[Arc]
    beats: list[Beat]  # validated to exactly TOTAL_EPISODES by the planner
    character_arcs: dict[str, str] = {}
    version: int = 0

    def beat(self, ep: int) -> Beat | None:
        return next((b for b in self.beats if b.ep == ep), None)

    def arc_for(self, ep: int) -> Arc | None:
        return next((a for a in self.arcs if a.ep_start <= ep <= a.ep_end), None)


# --- memory stores ----------------------------------------------------------

class Fact(BaseModel):
    id: str = Field(default_factory=new_id)
    text: str
    entities: list[str] = []
    ep: int = 0
    kind: Literal["event", "attribute", "rule", "relationship"] = "event"
    superseded_by: str | None = None
    supersedes: str | None = None  # id of an older fact this one replaces


class Thread(BaseModel):
    id: str = Field(default_factory=new_id)
    title: str
    description: str = ""
    opened_ep: int = 0
    due_by_ep: int | None = None
    status: Literal["open", "resolved", "abandoned"] = "open"
    last_touched_ep: int = 0
    resolved_ep: int | None = None


class Directive(BaseModel):
    id: str = Field(default_factory=new_id)
    text: str
    scope: str = "global"  # "global" | "character:<id>" | "until_ep:<n>"
    created_ep: int = 0
    active: bool = True
    source_feedback_id: str | None = None


class TimelineEntry(BaseModel):
    ep: int
    in_story_time: str


# --- episodes -------------------------------------------------------------

class CharacterUpdate(BaseModel):
    id: str
    changes: dict[str, Any]


class ThreadUpdate(BaseModel):
    """A correction to a thread (from an arc audit): close it, or move its due date."""

    thread_id: str
    status: Literal["open", "resolved", "abandoned"] | None = None
    due_by_ep: int | None = None
    reason: str = ""


StateEdit = CharacterUpdate | ThreadUpdate


class EpisodeDelta(BaseModel):
    ep: int = 0
    summary: str
    hook: str
    hook_type: HookType
    in_story_time: str = ""
    new_facts: list[Fact] = []
    new_characters: list[Character] = []
    character_updates: list[CharacterUpdate] = []
    threads_opened: list[Thread] = []
    threads_advanced: list[str] = []
    threads_resolved: list[str] = []


class Episode(BaseModel):
    ep: int
    version: int
    status: EpisodeStatus
    text: str
    word_count: int
    delta: EpisodeDelta | None = None
    human_edited: bool = False
    checks: dict[str, Any] = {}
    cost_usd: float = 0.0
    created_at: str = Field(default_factory=utcnow)


class Feedback(BaseModel):
    id: str = Field(default_factory=new_id)
    ep: int
    text: str
    routed: dict[str, Any] = {}
    applied_at: str | None = None


# --- derived story state ---------------------------------------------------

_MERGE_LIST_FIELDS = {"knows"}
_MERGE_DICT_FIELDS = {"relationships"}
_IMMUTABLE_FIELDS = {"id", "first_ep"}


class StoryState(BaseModel):
    """Story memory at a point in time. Built by replaying approved deltas in order."""

    characters: dict[str, Character] = {}
    facts: list[Fact] = []
    threads: dict[str, Thread] = {}
    timeline: list[TimelineEntry] = []

    def apply(self, delta: EpisodeDelta) -> list[str]:
        """Apply one approved delta in place. Returns warnings for references that could not be resolved."""
        ep = delta.ep
        warnings: list[str] = []

        for ch in delta.new_characters:
            if ch.id in self.characters:
                warnings.append(f"new character '{ch.id}' already exists; treated as update")
                self._update_character(ch.id, ch.model_dump(exclude_defaults=True), ep)
            else:
                self.characters[ch.id] = ch.model_copy(update={"first_ep": ep, "last_seen_ep": ep})

        for upd in delta.character_updates:
            if upd.id not in self.characters:
                warnings.append(f"update for unknown character '{upd.id}'")
                continue
            self._update_character(upd.id, upd.changes, ep)

        by_id = {f.id: f for f in self.facts}
        for fact in delta.new_facts:
            fact = fact.model_copy(update={"ep": ep})
            if fact.supersedes:
                old = by_id.get(fact.supersedes)
                if old is None:
                    warnings.append(f"fact supersedes unknown fact '{fact.supersedes}'")
                else:
                    old.superseded_by = fact.id
            self.facts.append(fact)
            by_id[fact.id] = fact

        for th in delta.threads_opened:
            self.threads[th.id] = th.model_copy(update={"opened_ep": ep, "last_touched_ep": ep, "status": "open"})
        for tid in delta.threads_advanced:
            if tid in self.threads:
                self.threads[tid].last_touched_ep = ep
            else:
                warnings.append(f"advanced unknown thread '{tid}'")
        for tid in delta.threads_resolved:
            if tid in self.threads:
                th = self.threads[tid]
                th.status, th.resolved_ep, th.last_touched_ep = "resolved", ep, ep
            else:
                warnings.append(f"resolved unknown thread '{tid}'")

        if delta.in_story_time:
            self.timeline.append(TimelineEntry(ep=ep, in_story_time=delta.in_story_time))
        return warnings

    def apply_edit(self, update: "StateEdit", ep: int) -> None:
        """A human/audit correction. Unlike episode deltas it does not count as the character appearing."""
        if isinstance(update, ThreadUpdate):
            t = self.threads.get(update.thread_id)
            if t is not None:
                if update.status:
                    t.status = update.status
                    t.resolved_ep = ep if update.status in ("resolved", "abandoned") else None
                if update.due_by_ep is not None:
                    t.due_by_ep = update.due_by_ep
            return
        if update.id in self.characters:
            seen = self.characters[update.id].last_seen_ep
            self._update_character(update.id, update.changes, ep)
            self.characters[update.id].last_seen_ep = seen

    def _update_character(self, cid: str, changes: dict[str, Any], ep: int) -> None:
        data = self.characters[cid].model_dump()
        for key, value in changes.items():
            if key not in data or key in _IMMUTABLE_FIELDS:
                continue
            if key in _MERGE_LIST_FIELDS:
                items = value if isinstance(value, list) else [value]
                data[key] = data[key] + [x for x in items if x not in data[key]]
            elif key in _MERGE_DICT_FIELDS and isinstance(value, dict):
                data[key] = {**data[key], **value}
            else:
                data[key] = value
        data["last_seen_ep"] = ep
        self.characters[cid] = Character.model_validate(data)  # raises on invalid values, e.g. bad status

    def active_facts(self) -> list[Fact]:
        return [f for f in self.facts if f.superseded_by is None]

    def open_threads(self) -> list[Thread]:
        return [t for t in self.threads.values() if t.status == "open"]
