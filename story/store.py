"""SQLite persistence. Append-only and versioned; story state is a replay of approved deltas."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import (
    ArcPlan,
    Bible,
    CharacterUpdate,
    StateEdit,
    ThreadUpdate,
    Directive,
    Episode,
    EpisodeDelta,
    EpisodeStatus,
    Feedback,
    StoryState,
    new_id,
    utcnow,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS stories (
    id TEXT PRIMARY KEY, premise TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bible (
    story_id TEXT NOT NULL, version INTEGER NOT NULL, json TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (story_id, version)
);
CREATE TABLE IF NOT EXISTS plans (
    story_id TEXT NOT NULL, version INTEGER NOT NULL, json TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (story_id, version)
);
CREATE TABLE IF NOT EXISTS episodes (
    story_id TEXT NOT NULL, ep INTEGER NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL,
    text TEXT NOT NULL, delta_json TEXT, checks_json TEXT NOT NULL DEFAULT '{}',
    human_edited INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
    PRIMARY KEY (story_id, ep, version)
);
CREATE TABLE IF NOT EXISTS directives (
    story_id TEXT NOT NULL, id TEXT NOT NULL, json TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (story_id, id)
);
CREATE TABLE IF NOT EXISTS feedback (
    story_id TEXT NOT NULL, id TEXT NOT NULL, ep INTEGER NOT NULL, text TEXT NOT NULL,
    routed_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
    PRIMARY KEY (story_id, id)
);
CREATE TABLE IF NOT EXISTS arc_summaries (
    story_id TEXT NOT NULL, arc_no INTEGER NOT NULL, version INTEGER NOT NULL, text TEXT NOT NULL,
    PRIMARY KEY (story_id, arc_no, version)
);
CREATE TABLE IF NOT EXISTS state_edits (
    story_id TEXT NOT NULL, id TEXT NOT NULL, ep INTEGER NOT NULL, json TEXT NOT NULL, source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (story_id, id)
);
CREATE TABLE IF NOT EXISTS hitl_events (
    story_id TEXT NOT NULL, id TEXT NOT NULL, ep INTEGER NOT NULL, kind TEXT NOT NULL, json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (story_id, id)
);
CREATE TABLE IF NOT EXISTS state_snapshots (
    story_id TEXT NOT NULL, ep INTEGER NOT NULL, json TEXT NOT NULL,
    PRIMARY KEY (story_id, ep)
);
"""


class Store:
    def __init__(self, db_path: Path | str):
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def _one(self, sql: str, *args) -> sqlite3.Row | None:
        return self.conn.execute(sql, args).fetchone()

    def _next_version(self, table: str, where: str, *args) -> int:
        row = self._one(f"SELECT COALESCE(MAX(version), 0) AS v FROM {table} WHERE {where}", *args)
        return row["v"] + 1

    # --- stories ------------------------------------------------------------

    def create_story(self, premise: str) -> str:
        story_id = new_id()
        with self.conn:
            self.conn.execute(
                "INSERT INTO stories VALUES (?, ?, ?, ?)", (story_id, premise, utcnow(), "planning")
            )
        return story_id

    def list_stories(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM stories ORDER BY created_at DESC, rowid DESC")
        out = []
        for r in rows:
            bible = self.latest_bible(r["id"])
            out.append({**dict(r), "title": bible.title if bible else None,
                        "last_approved_ep": self.last_approved_ep(r["id"])})
        return out

    def latest_story_id(self) -> str | None:
        row = self._one("SELECT id FROM stories ORDER BY created_at DESC, rowid DESC LIMIT 1")
        return row["id"] if row else None

    def get_story(self, story_id: str) -> dict | None:
        row = self._one("SELECT * FROM stories WHERE id = ?", story_id)
        return dict(row) if row else None

    def set_story_status(self, story_id: str, status: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE stories SET status = ? WHERE id = ?", (status, story_id))

    # --- bible --------------------------------------------------------------

    def save_bible(self, story_id: str, bible: Bible) -> int:
        version = self._next_version("bible", "story_id = ?", story_id)
        with self.conn:
            self.conn.execute(
                "INSERT INTO bible VALUES (?, ?, ?, ?)", (story_id, version, bible.model_dump_json(), utcnow())
            )
        return version

    def latest_bible(self, story_id: str) -> Bible | None:
        row = self._one("SELECT json FROM bible WHERE story_id = ? ORDER BY version DESC LIMIT 1", story_id)
        return Bible.model_validate_json(row["json"]) if row else None

    # --- plans (every edit is a new version) ---------------------------------

    def save_plan(self, story_id: str, plan: ArcPlan, reason: str) -> int:
        version = self._next_version("plans", "story_id = ?", story_id)
        plan = plan.model_copy(update={"version": version})
        with self.conn:
            self.conn.execute(
                "INSERT INTO plans VALUES (?, ?, ?, 0, ?, ?)",
                (story_id, version, plan.model_dump_json(), utcnow(), reason),
            )
        return version

    def approve_plan(self, story_id: str, version: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE plans SET approved = 1 WHERE story_id = ? AND version = ?", (story_id, version)
            )

    def get_plan(self, story_id: str, version: int) -> ArcPlan | None:
        row = self._one("SELECT json FROM plans WHERE story_id = ? AND version = ?", story_id, version)
        return ArcPlan.model_validate_json(row["json"]) if row else None

    def latest_plan(self, story_id: str, approved_only: bool = False) -> ArcPlan | None:
        sql = "SELECT json FROM plans WHERE story_id = ?" + (" AND approved = 1" if approved_only else "")
        row = self._one(sql + " ORDER BY version DESC LIMIT 1", story_id)
        return ArcPlan.model_validate_json(row["json"]) if row else None

    def plan_history(self, story_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT version, approved, created_at, reason FROM plans WHERE story_id = ? ORDER BY version",
            (story_id,),
        )
        return [dict(r) for r in rows]

    def plan_diff(self, story_id: str, v1: int, v2: int) -> list[dict]:
        """Beat- and arc-level changes between two plan versions."""
        a, b = self.get_plan(story_id, v1), self.get_plan(story_id, v2)
        if a is None or b is None:
            raise ValueError(f"unknown plan version: {v1 if a is None else v2}")
        changes: list[dict] = []
        old_arcs, new_arcs = {x.arc_no: x for x in a.arcs}, {x.arc_no: x for x in b.arcs}
        for no in sorted(old_arcs.keys() | new_arcs.keys()):
            old, new = old_arcs.get(no), new_arcs.get(no)
            if old != new:
                changes.append({"kind": "arc", "arc_no": no,
                                "old": old.model_dump() if old else None, "new": new.model_dump() if new else None})
        old_beats, new_beats = {x.ep: x for x in a.beats}, {x.ep: x for x in b.beats}
        for ep in sorted(old_beats.keys() | new_beats.keys()):
            old, new = old_beats.get(ep), new_beats.get(ep)
            if old != new:
                changes.append({"kind": "beat", "ep": ep,
                                "old": old.beat if old else None, "new": new.beat if new else None})
        return changes

    # --- episodes -----------------------------------------------------------

    def _episode(self, row: sqlite3.Row) -> Episode:
        return Episode(
            ep=row["ep"], version=row["version"], status=row["status"], text=row["text"],
            word_count=len(row["text"].split()),
            delta=EpisodeDelta.model_validate_json(row["delta_json"]) if row["delta_json"] else None,
            human_edited=bool(row["human_edited"]), checks=json.loads(row["checks_json"]),
            cost_usd=row["cost_usd"], created_at=row["created_at"],
        )

    def _insert_episode(self, story_id: str, ep: int, status: EpisodeStatus, text: str,
                        delta: EpisodeDelta | None, checks: dict | None, human_edited: bool,
                        cost_usd: float) -> int:
        version = self._next_version("episodes", "story_id = ? AND ep = ?", story_id, ep)
        self.conn.execute(
            "INSERT INTO episodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (story_id, ep, version, status, text, delta.model_dump_json() if delta else None,
             json.dumps(checks or {}), int(human_edited), cost_usd, utcnow()),
        )
        return version

    def save_draft(self, story_id: str, ep: int, text: str, delta: EpisodeDelta | None = None,
                   checks: dict | None = None, cost_usd: float = 0.0) -> Episode:
        if delta is not None:
            delta = delta.model_copy(update={"ep": ep})
        with self.conn:
            version = self._insert_episode(story_id, ep, "draft", text, delta, checks, False, cost_usd)
        return self.get_episode(story_id, ep, version)

    def get_episode(self, story_id: str, ep: int, version: int | None = None) -> Episode | None:
        if version is None:
            row = self._one(
                "SELECT * FROM episodes WHERE story_id = ? AND ep = ? ORDER BY version DESC LIMIT 1", story_id, ep
            )
        else:
            row = self._one(
                "SELECT * FROM episodes WHERE story_id = ? AND ep = ? AND version = ?", story_id, ep, version
            )
        return self._episode(row) if row else None

    def get_draft(self, story_id: str, ep: int) -> Episode | None:
        """Latest unreviewed draft for this episode, so resume never re-pays for it."""
        row = self._one(
            "SELECT * FROM episodes WHERE story_id = ? AND ep = ? AND status = 'draft' ORDER BY version DESC LIMIT 1",
            story_id, ep,
        )
        return self._episode(row) if row else None

    def set_episode_status(self, story_id: str, ep: int, version: int, status: EpisodeStatus) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE episodes SET status = ? WHERE story_id = ? AND ep = ? AND version = ?",
                (status, story_id, ep, version),
            )

    def approved_episode(self, story_id: str, ep: int) -> Episode | None:
        row = self._one("SELECT * FROM episodes WHERE story_id = ? AND ep = ? AND status = 'approved'", story_id, ep)
        return self._episode(row) if row else None

    def approved_episodes(self, story_id: str, start: int = 1, end: int | None = None) -> list[Episode]:
        rows = self.conn.execute(
            "SELECT * FROM episodes WHERE story_id = ? AND status = 'approved' AND ep >= ? AND ep <= ? ORDER BY ep",
            (story_id, start, end if end is not None else 10**9),
        )
        return [self._episode(r) for r in rows]

    def last_approved_ep(self, story_id: str) -> int:
        row = self._one("SELECT COALESCE(MAX(ep), 0) AS ep FROM episodes WHERE story_id = ? AND status = 'approved'",
                        story_id)
        return row["ep"]

    def commit_episode(self, story_id: str, ep: int, text: str, delta: EpisodeDelta, *,
                       human_edited: bool = False, checks: dict | None = None, cost_usd: float = 0.0) -> Episode:
        """Make this text canon for `ep`. Earlier versions become superseded; cached state from `ep` on is dropped.

        Committing an already-approved episode is a retcon: later episodes are kept, and state after
        `ep` is recomputed from the new delta on the next `state_at` call.
        """
        last = self.last_approved_ep(story_id)
        if ep > last + 1:
            raise ValueError(f"cannot commit episode {ep}: last approved is {last}")
        delta = delta.model_copy(update={"ep": ep})
        # Validate against the state it will be applied to, so a bad delta never becomes canon.
        self.state_at(story_id, ep - 1).apply(delta)
        with self.conn:
            self.conn.execute(
                "UPDATE episodes SET status = 'superseded' WHERE story_id = ? AND ep = ? AND status IN ('approved','draft')",
                (story_id, ep),
            )
            version = self._insert_episode(story_id, ep, "approved", text, delta, checks, human_edited, cost_usd)
            self._invalidate_snapshots(story_id, ep)
        return self.get_episode(story_id, ep, version)

    # --- state --------------------------------------------------------------

    def set_initial_state(self, story_id: str, state: StoryState) -> None:
        """State before episode 1 (the planned main cast). Stored as the ep-0 snapshot."""
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO state_snapshots VALUES (?, 0, ?)", (story_id, state.model_dump_json())
            )
            self._invalidate_snapshots(story_id, 1)

    def _invalidate_snapshots(self, story_id: str, from_ep: int) -> None:
        # The ep-0 snapshot is the initial state, not a cache, so it is never invalidated.
        self.conn.execute("DELETE FROM state_snapshots WHERE story_id = ? AND ep >= ?", (story_id, max(from_ep, 1)))

    def state_at(self, story_id: str, ep: int) -> StoryState:
        """Story state after episode `ep`: nearest snapshot + replay of approved deltas since."""
        row = self._one(
            "SELECT ep, json FROM state_snapshots WHERE story_id = ? AND ep <= ? ORDER BY ep DESC LIMIT 1", story_id, ep
        )
        base, state = (row["ep"], StoryState.model_validate_json(row["json"])) if row else (0, StoryState())
        episodes = {e.ep: e for e in self.approved_episodes(story_id, base + 1, ep) if e.delta}
        # The ep-0 snapshot is the pristine initial cast; edits made before episode 1 are replayed on top of it.
        edits = self.state_edits(story_id, base if base == 0 else base + 1, ep)
        for n in sorted(episodes.keys() | edits.keys()):
            if n in episodes:
                state.apply(episodes[n].delta)
            for upd in edits.get(n, []):
                state.apply_edit(upd, n)
        if (episodes or edits) and ep > 0:
            with self.conn:
                self.conn.execute(
                    "INSERT OR REPLACE INTO state_snapshots VALUES (?, ?, ?)", (story_id, ep, state.model_dump_json())
                )
        return state

    def add_state_edit(self, story_id: str, ep: int, update: StateEdit, source: str) -> None:
        """A correction to story state applied right after episode `ep` (from feedback or an arc audit).

        Validated against the state it applies to first, so a malformed edit raises here instead of making every
        later `state_at` fail.
        """
        self.state_at(story_id, ep).apply_edit(update, ep)
        with self.conn:
            self.conn.execute(
                "INSERT INTO state_edits VALUES (?, ?, ?, ?, ?, ?)",
                (story_id, new_id(), ep, update.model_dump_json(), source, utcnow()),
            )
            self._invalidate_snapshots(story_id, ep)

    def state_edits(self, story_id: str, start: int = 1, end: int | None = None) -> dict[int, list[StateEdit]]:
        rows = self.conn.execute(
            "SELECT ep, json FROM state_edits WHERE story_id = ? AND ep >= ? AND ep <= ? ORDER BY ep, rowid",
            (story_id, start, end if end is not None else 10**9),
        )
        out: dict[int, list[StateEdit]] = {}
        for r in rows:
            data = json.loads(r["json"])
            model = ThreadUpdate if "thread_id" in data else CharacterUpdate
            out.setdefault(r["ep"], []).append(model.model_validate(data))
        return out

    def rollback_to(self, story_id: str, ep: int) -> list[int]:
        """Un-canon every approved episode after `ep` (they become superseded). Returns the affected episodes."""
        eps = [e.ep for e in self.approved_episodes(story_id, ep + 1)]
        with self.conn:
            self.conn.execute(
                "UPDATE episodes SET status = 'superseded' WHERE story_id = ? AND ep > ? AND status IN ('approved','draft')",
                (story_id, ep),
            )
            # Corrections made after `ep` described episodes that are no longer canon.
            self.conn.execute("DELETE FROM state_edits WHERE story_id = ? AND ep > ?", (story_id, ep))
            self._invalidate_snapshots(story_id, ep + 1)
        return eps

    # --- HITL events (plan edits, rejections, human edits, retcons) ----------

    def add_event(self, story_id: str, ep: int, kind: str, data: dict) -> str:
        event_id = new_id()
        with self.conn:
            self.conn.execute(
                "INSERT INTO hitl_events VALUES (?, ?, ?, ?, ?, ?)",
                (story_id, event_id, ep, kind, json.dumps(data, ensure_ascii=False), utcnow()),
            )
        return event_id

    def events(self, story_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, ep, kind, json, created_at FROM hitl_events WHERE story_id = ? ORDER BY created_at, rowid",
            (story_id,),
        )
        return [{"id": r["id"], "ep": r["ep"], "kind": r["kind"], "created_at": r["created_at"],
                 **json.loads(r["json"])} for r in rows]

    # --- directives, feedback, arc summaries ----------------------------------

    def save_directive(self, story_id: str, directive: Directive) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO directives VALUES (?, ?, ?, ?)",
                (story_id, directive.id, directive.model_dump_json(), int(directive.active)),
            )

    def directives(self, story_id: str, active_only: bool = True) -> list[Directive]:
        sql = "SELECT json FROM directives WHERE story_id = ?" + (" AND active = 1" if active_only else "")
        return [Directive.model_validate_json(r["json"]) for r in self.conn.execute(sql + " ORDER BY rowid", (story_id,))]

    def set_directive_active(self, story_id: str, directive_id: str, active: bool) -> None:
        row = self._one("SELECT json FROM directives WHERE story_id = ? AND id = ?", story_id, directive_id)
        if row is None:
            raise ValueError(f"unknown directive {directive_id}")
        self.save_directive(story_id, Directive.model_validate_json(row["json"]).model_copy(update={"active": active}))

    def save_feedback(self, story_id: str, fb: Feedback) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO feedback VALUES (?, ?, ?, ?, ?, ?)",
                (story_id, fb.id, fb.ep, fb.text, json.dumps(fb.routed), fb.applied_at or utcnow()),
            )

    def feedback(self, story_id: str) -> list[Feedback]:
        rows = self.conn.execute("SELECT * FROM feedback WHERE story_id = ? ORDER BY created_at, rowid", (story_id,))
        return [Feedback(id=r["id"], ep=r["ep"], text=r["text"], routed=json.loads(r["routed_json"]),
                         applied_at=r["created_at"]) for r in rows]

    def save_arc_summary(self, story_id: str, arc_no: int, text: str) -> int:
        version = self._next_version("arc_summaries", "story_id = ? AND arc_no = ?", story_id, arc_no)
        with self.conn:
            self.conn.execute("INSERT INTO arc_summaries VALUES (?, ?, ?, ?)", (story_id, arc_no, version, text))
        return version

    def arc_summaries(self, story_id: str) -> dict[int, str]:
        """Latest summary per arc."""
        rows = self.conn.execute(
            "SELECT arc_no, text FROM arc_summaries a WHERE story_id = ? AND version = "
            "(SELECT MAX(version) FROM arc_summaries b WHERE b.story_id = a.story_id AND b.arc_no = a.arc_no) "
            "ORDER BY arc_no",
            (story_id,),
        )
        return {r["arc_no"]: r["text"] for r in rows}
