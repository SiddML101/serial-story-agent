"""Writes the demo/ folder: arc plan, episodes, HITL log, cost report."""
from __future__ import annotations

import shutil
from pathlib import Path

from . import fmt
from .config import ROOT
from .models import ArcPlan
from .store import Store

DEMO_DIR = ROOT / "demo"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def arc_plan_md(store: Store, story_id: str, plan: ArcPlan | None = None) -> str:
    plan = plan or store.latest_plan(story_id)
    bible = store.latest_bible(story_id)
    cast = store.state_at(story_id, 0).characters.values()
    story = store.get_story(story_id)
    out = [f"# {bible.title}: 200-episode arc plan", "",
           f"**Premise:** {story['premise']}  ", f"**Logline:** {bible.logline}  ",
           f"**Plan version:** v{plan.version}", "", "## Story bible", "", fmt.bible(bible), "",
           "## Main cast", ""]
    for c in cast:
        out.append(f"- **{c.name}** (`{c.id}`, {c.role}): {c.description} *Goal:* {c.goal} *Secret:* {c.secret}")
    out += ["", "## Character arcs", ""] + [f"- **{k}**: {v}" for k, v in plan.character_arcs.items()]
    out += ["", "## Acts", ""]
    for a in plan.acts:
        out.append(f"- **Act {a.act_no}: {a.title}** (eps {a.ep_start}-{a.ep_end}). {a.purpose} "
                   f"*Turning point:* {a.turning_point}")
    for act in plan.acts:
        out += ["", f"## Act {act.act_no}: {act.title}", ""]
        for arc in (x for x in plan.arcs if x.act_no == act.act_no):
            out += [f"### Arc {arc.arc_no}: {arc.title} (eps {arc.ep_start}-{arc.ep_end})", "",
                    f"*Goal:* {arc.goal}  ", f"*Turning point:* {arc.turning_point}  ",
                    f"*Threads opened:* {', '.join(arc.threads_opened) or '-'} · "
                    f"*resolved:* {', '.join(arc.threads_resolved) or '-'}", ""]
            for b in (x for x in plan.beats if arc.ep_start <= x.ep <= arc.ep_end):
                chars = f" _({', '.join(b.characters)})_" if b.characters else ""
                out.append(f"{b.ep}. {b.beat}{chars} `{b.hook_type_hint or '-'}`")
    return "\n".join(out) + "\n"


def export_plan(store: Store, story_id: str, demo_dir: Path | None = None) -> Path:
    demo_dir = demo_dir or DEMO_DIR
    return _write(demo_dir / "arc_plan.md", arc_plan_md(store, story_id))


def copy_run(log_path: Path, db_path: Path, story_id: str, demo_dir: Path | None = None) -> list[Path]:
    demo_dir = demo_dir or DEMO_DIR
    """Snapshot the run into demo/ so a fresh clone can inspect it:
    DB_PATH=demo/story.db RUNS_DIR=demo/runs python -m story status"""
    out = []
    if log_path.exists():
        dest = demo_dir / "runs" / story_id / "log.jsonl"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(log_path, dest)
        out.append(dest)
    if db_path.exists() and db_path.resolve() != (demo_dir / "story.db").resolve():
        import sqlite3
        dest = demo_dir / "story.db"
        dest.unlink(missing_ok=True)
        src = sqlite3.connect(str(db_path))
        with sqlite3.connect(str(dest)) as dst:  # online backup: consistent even while the source is open
            src.backup(dst)
        src.close()
        out.append(dest)
    return out


# --- episodes -------------------------------------------------------------------

def episode_md(store: Store, story_id: str, ep: int) -> str:
    e = store.approved_episode(story_id, ep)
    plan = store.latest_plan(story_id)
    beat = (e.checks or {}).get("beat") or (plan.beat(ep).beat if plan.beat(ep) else "")
    d = e.delta
    crit = (e.checks or {}).get("critic") or {}
    meta = [f"**Words:** {e.word_count}", f"**Version:** v{e.version}",
            f"**Human edited:** {'yes' if e.human_edited else 'no'}",
            f"**Revisions:** {(e.checks or {}).get('revisions', 0)}"]
    if crit:
        meta.append(f"**Critic:** hook {crit.get('hook_score')}/5, momentum {crit.get('momentum_score')}/5")
    out = [f"# Episode {ep}", "", f"> **Planned beat:** {beat}", "", " · ".join(meta)]
    if d:
        out += ["", f"**Hook ({d.hook_type}):** {d.hook}  ", f"**In-story time:** {d.in_story_time or '-'}"]
    out += ["", "---", "", e.text, "", "---", ""]
    if d:
        out += [f"*Summary (memory):* {d.summary}"]
    return "\n".join(out) + "\n"


def export_episodes(store: Store, story_id: str, demo_dir: Path | None = None) -> list[Path]:
    demo_dir = demo_dir or DEMO_DIR
    return [_write(demo_dir / "episodes" / f"ep_{e.ep:03d}.md", episode_md(store, story_id, e.ep))
            for e in store.approved_episodes(story_id)]


# --- HITL log --------------------------------------------------------------------

def _diff_lines(changes: list[dict], limit: int = 12) -> list[str]:
    out = []
    for c in changes[:limit]:
        if c["kind"] == "beat":
            out += [f"- **ep {c['ep']}**", f"  - before: {c['old']}", f"  - after: {c['new']}"]
        else:
            out.append(f"- arc {c['arc_no']} changed")
    if len(changes) > limit:
        out.append(f"- … {len(changes) - limit} more changes")
    return out


def hitl_log_md(store: Store, story_id: str) -> str:
    approved = {e.ep: e for e in store.approved_episodes(story_id)}
    out = ["# Human-in-the-loop log", "",
           "Every human intervention, what the system turned it into, and which later episodes it changed.", ""]
    for i, ev in enumerate(store.events(story_id), 1):
        kind, ep = ev["kind"], ev["ep"]
        when = ev["created_at"].replace("T", " ")[:16]
        if kind == "plan_edit":
            out += [f"## {i}. Plan edited by hand (v{ev['from_version']} → v{ev['to_version']})", f"*{when}*", ""]
            out += _diff_lines(ev["changes"]) + [""]
        elif kind == "plan_regenerate":
            out += [f"## {i}. Act {ev['act']} beats regenerated (v{ev['from_version']} → v{ev['to_version']})",
                    f"*{when}*, {len(ev['changes'])} beats changed", ""]
        elif kind == "plan_approved":
            out += [f"## {i}. Plan v{ev['version']} approved", f"*{when}*", ""]
        elif kind == "feedback":
            out += [f"## {i}. Feedback before episode {ep} ({ev['stage']} stage)", f"*{when}*", "",
                    f"> \"{ev['text']}\"", "", f"**Router's interpretation:** {ev['explanation']}", ""]
            if ev["directives"]:
                out += ["**Standing directives created** (injected into every later draft and checked by the critic):", ""]
                out += [f"- {d['text']} `[{d['scope']}]`" for d in ev["directives"]] + [""]
            if ev["plan_changes"]:
                out += [f"**Plan v{ev['plan_from']} → v{ev['plan_to']}**: {len(ev['plan_changes'])} beats rewritten", ""]
                out += _diff_lines(ev["plan_changes"]) + [""]
            if ev["state_edits"]:
                out += ["**State edits:**", ""] + [f"- {s['character_id']}: {s['changes']}" for s in ev["state_edits"]] + [""]
            later = [approved[c["ep"]] for c in ev["plan_changes"] if c["kind"] == "beat" and c["ep"] in approved]
            if later:
                out += ["**What was actually written for the changed beats:**", ""]
                for e in later[:6]:
                    out.append(f"- **Ep {e.ep}** ([text](episodes/ep_{e.ep:03d}.md)): {e.delta.summary if e.delta else ''}")
                out.append("")
            written = sorted(n for n in approved if n >= ep)
            if written and ev["directives"]:
                out += [f"Episodes written under these directives: {written[0]}–{written[-1]}.", ""]
        elif kind == "reject":
            final = approved.get(ep)
            out += [f"## {i}. Episode {ep} draft v{ev['version']} rejected", f"*{when}*", "",
                    f"> Reason: \"{ev['reason']}\"", "", f"- Rejected draft: {ev.get('summary') or '(no summary)'}"]
            if final and final.delta:
                out.append(f"- Approved replacement (v{final.version}): {final.delta.summary}")
            out.append("")
        elif kind == "human_edit":
            out += [f"## {i}. Episode {ep} edited by hand before approval", f"*{when}*", "",
                    f"- {ev['words_before']} → {ev['words_after']} words, text similarity {ev['similarity']:.0%}",
                    "- Memory re-extracted from the edited text, so later episodes build on the human version.", ""]
        elif kind == "arc_boundary":
            out += [f"## {i}. Arc {ev['arc_no']} boundary review", f"*{when}*", "",
                    f"**Arc summary:** {ev['summary']}", ""]
            if ev.get("corrections"):
                out += ["**Audit corrections:**", ""] + [f"- {c['character_id']}: {c['changes']} ({c['reason']})"
                                                        for c in ev["corrections"]] + [""]
            if ev.get("replan_to"):
                out += [f"**Next arc replanned** v{ev['replan_from']} → v{ev['replan_to']} "
                        f"({ev.get('changes_count', 0)} beats changed). Human decision: **{ev.get('decision')}**", ""]
        elif kind == "retcon":
            out += [f"## {i}. Retcon: episode {ep} rewritten (v{ev['old_version']} → v{ev['new_version']})", f"*{when}*", ""]
            out += ["**Facts no longer true:**"] + [f"- {t}" for t in ev["removed_facts"] or ["(none)"]]
            out += ["", "**New facts:**"] + [f"- {t}" for t in ev["added_facts"] or ["(none)"]]
            out += ["", f"Later episodes mentioning changed entities: {ev['affected'] or 'none'}; "
                        f"conflicts found: {len(ev['conflicts'])}", ""]
            out += [f"- ep {c['ep']}: \"{c['evidence']}\" → {c['fix']}" for c in ev["conflicts"]] + [""]
        elif kind == "directive_edit":
            out += [f"## {i}. Directive reworded before episode {ep}", f"*{when}*", "",
                    f"- before: {ev['old']}", f"- after: {ev['new']}", ""]
        elif kind == "directive_off":
            out += [f"## {i}. Directive {ev['id']} deactivated before episode {ep}", f"*{when}*", ""]
        elif kind == "retcon_resolution":
            detail = {k: v for k, v in ev.items() if k not in ("id", "ep", "kind", "created_at", "resolution")}
            out += [f"## {i}. Retcon of episode {ep} resolved: **{ev['resolution']}**", f"*{when}* {detail or ''}", ""]
    return "\n".join(out) + "\n"


def export_hitl_log(store: Store, story_id: str, demo_dir: Path | None = None) -> Path:
    demo_dir = demo_dir or DEMO_DIR
    return _write(demo_dir / "hitl_log.md", hitl_log_md(store, story_id))


def export_all(store: Store, story_id: str, log_path: Path, n_writer_models: int,
               demo_dir: Path | None = None, db_path: Path | None = None) -> list[Path]:
    demo_dir = demo_dir or DEMO_DIR
    from . import report
    summary = report.summarize(report.load_log(log_path))
    paths = [export_plan(store, story_id, demo_dir), export_hitl_log(store, story_id, demo_dir),
             _write(demo_dir / "cost_report.md",
                    report.cost_report_md(summary, report.project(summary), n_writer_models))]
    paths += export_episodes(store, story_id, demo_dir)
    return paths + copy_run(log_path, db_path or Path("__none__"), story_id, demo_dir)
