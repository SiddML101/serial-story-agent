"""Typer + Rich command line. `python -m story --help`."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from . import export, feedback, fmt, pipeline, planner, report, retcon, service
from .config import TOTAL_EPISODES, Settings, load_settings
from .context import build_context
from .editor import edit_text
from .llm import LLM, LLMError, QuotaExhausted, RunLogger
from .models import ArcPlan, Episode
from .store import Store

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Agentic serial story writer.")
plan_app = typer.Typer(no_args_is_help=True, help="Show, edit, steer and approve the 200-episode plan.")
app.add_typer(plan_app, name="plan")
console = Console()

StoryOpt = typer.Option(None, "--story-id", help="Story to act on (default: most recent).")


Ctx = service.Session


def open_ctx(story_id: Optional[str] = None) -> Ctx:
    try:
        return service.open_session(story_id)
    except service.ServiceError as e:
        console.print(f"[red]{escape(str(e))}[/] Start one with: python -m story new \"<premise>\"")
        raise typer.Exit(1)


def choose(prompt: str, options: dict[str, str], default: str | None = None) -> str:
    """Single-key menu. options maps key -> label."""
    menu = "  ".join(f"[bold]\\[{k}][/]{escape(v[len(k):]) if v.lower().startswith(k) else ' ' + escape(v)}"
                     for k, v in options.items())
    console.print(menu)
    return Prompt.ask(prompt, choices=list(options), default=default, show_choices=False)


# --- plan display -------------------------------------------------------------

def show_plan_overview(ctx: Ctx, plan: ArcPlan) -> None:
    bible = ctx.store.latest_bible(ctx.story_id)
    console.print(Panel(f"[bold]{escape(bible.title)}[/]\n{escape(bible.logline)}\n\n"
                        f"[dim]{escape(bible.genre)} · {escape(bible.tone)} · {escape(bible.pov)}, "
                        f"{escape(bible.tense)} tense · {escape(bible.setting)}[/]",
                        title=f"Plan v{plan.version}"))
    cast = Table("id", "name", "role", "goal", title="Main cast", show_lines=False)
    for c in ctx.store.state_at(ctx.story_id, 0).characters.values():
        cast.add_row(c.id, c.name, c.role, c.goal)
    console.print(cast)
    t = Table("act", "eps", "arc", "title", "turning point", title="Acts and arcs", show_lines=False)
    for act in plan.acts:
        t.add_row(f"[bold]{act.act_no}[/]", f"{act.ep_start}-{act.ep_end}", "", f"[bold]{escape(act.title)}[/]",
                  escape(act.turning_point))
        for arc in (a for a in plan.arcs if a.act_no == act.act_no):
            t.add_row("", f"{arc.ep_start}-{arc.ep_end}", str(arc.arc_no), escape(arc.title), escape(arc.turning_point))
    console.print(t)


def show_beats(plan: ArcPlan, start: int, end: int, title: str) -> None:
    t = Table("ep", "beat", "characters", "hook", title=title)
    for b in plan.beats:
        if start <= b.ep <= end:
            t.add_row(str(b.ep), escape(b.beat), ", ".join(b.characters), b.hook_type_hint or "")
    console.print(t)


def show_validation(ctx: Ctx, plan: ArcPlan) -> planner.Validation:
    v = planner.validate_plan(plan, list(ctx.store.state_at(ctx.story_id, 0).characters))
    for e in v.errors:
        console.print(f"[red]✗ {escape(e)}[/]")
    for w in v.warnings:
        console.print(f"[yellow]! {escape(w)}[/]")
    if v.ok:
        console.print(f"[green]✓ structure valid[/] ({len(plan.beats)} beats, {len(v.warnings)} warnings)")
    return v


def show_plan_diff(ctx: Ctx, v1: int, v2: int, limit: int = 40) -> list[dict]:
    changes = ctx.store.plan_diff(ctx.story_id, v1, v2)
    if not changes:
        console.print(f"[dim]No changes between v{v1} and v{v2}.[/]")
        return changes
    console.print(f"[bold]Plan diff v{v1} → v{v2}[/] ({len(changes)} changes)")
    for c in changes[:limit]:
        if c["kind"] == "beat":
            console.print(f"  ep {c['ep']}:\n    [red]- {escape(str(c['old']))}[/]\n    [green]+ {escape(str(c['new']))}[/]")
        else:
            old, new = c["old"] or {}, c["new"] or {}
            fields = [k for k in set(old) | set(new) if old.get(k) != new.get(k)]
            console.print(f"  arc {c['arc_no']}: changed {', '.join(sorted(fields))}")
    if len(changes) > limit:
        console.print(f"  [dim]... {len(changes) - limit} more[/]")
    return changes


# --- plan commands --------------------------------------------------------------

@app.command()
def new(premise: str = typer.Argument(..., help="One-line story premise.")):
    """Create a story: bible, cast, 5 acts, 20 arcs, 200 beats. Then review the plan."""
    ctx = service.new_session(premise)
    console.print(f"Story [bold]{ctx.story_id}[/] created.")
    _generate(ctx)
    plan_review(ctx)


def _generate(ctx: Ctx) -> ArcPlan:
    try:
        with console.status("Planning...") as status:
            plan = service.generate_plan(ctx, progress=status.update)
    except Exception as e:  # quota, API errors after retries, unparseable output
        console.print(f"[red]Planning interrupted:[/] {escape(type(e).__name__ + ': ' + str(e)[:300])}\n"
                      f"Progress so far is saved. Resume with: python -m story plan generate")
        raise typer.Exit(2)
    console.print(f"[green]Plan v{plan.version} ready[/] → {ctx.out_dir / 'arc_plan.md'}")
    return plan


@plan_app.command("generate")
def plan_generate(story_id: Optional[str] = StoryOpt):
    """Resume an interrupted plan generation."""
    ctx = open_ctx(story_id)
    _generate(ctx)


@plan_app.command("show")
def plan_show(arc: Optional[int] = typer.Option(None, help="Show this arc's beats."),
              act: Optional[int] = typer.Option(None, help="Show this act's beats."),
              version: Optional[int] = typer.Option(None, help="Plan version (default: latest)."),
              story_id: Optional[str] = StoryOpt):
    """Show the plan overview, or the beats of one arc/act."""
    ctx = open_ctx(story_id)
    plan = ctx.store.get_plan(ctx.story_id, version) if version else ctx.store.latest_plan(ctx.story_id)
    if plan is None:
        console.print("[red]No plan yet.[/] Run: python -m story plan generate")
        raise typer.Exit(1)
    if arc:
        a = next((x for x in plan.arcs if x.arc_no == arc), None)
        if a is None:
            console.print(f"[red]No arc {arc}; arcs are 1-{len(plan.arcs)}.[/]")
            raise typer.Exit(1)
        console.print(escape(fmt.arc(a)))
        show_beats(plan, a.ep_start, a.ep_end, f"Arc {arc} beats")
    elif act:
        a = next((x for x in plan.acts if x.act_no == act), None)
        if a is None:
            console.print(f"[red]No act {act}; acts are 1-{len(plan.acts)}.[/]")
            raise typer.Exit(1)
        show_beats(plan, a.ep_start, a.ep_end, f"Act {act}: {a.title}")
    else:
        show_plan_overview(ctx, plan)
        show_validation(ctx, plan)
        hist = ctx.store.plan_history(ctx.story_id)
        console.print("[dim]Versions: " + ", ".join(
            f"v{h['version']}{'✓' if h['approved'] else ''} ({h['reason']})" for h in hist[-6:]) + "[/]")


def edit_plan(ctx: Ctx) -> ArcPlan | None:
    plan = ctx.store.latest_plan(ctx.story_id)
    text = planner.plan_to_yaml(plan)
    while True:
        edited = edit_text(text, suffix=".yaml")
        if edited is None:
            console.print("[dim]No changes saved.[/]")
            return None
        try:
            new_plan = planner.plan_from_yaml(edited)
        except Exception as e:  # YAML or schema error: show it and reopen
            console.print(f"[red]Invalid plan:[/] {escape(str(e)[:800])}")
            if not typer.confirm("Re-open the editor?", default=True):
                return None
            text = edited
            continue
        try:
            version, _ = service.save_human_plan(ctx, new_plan, "human edit (YAML)")
        except service.ServiceError as e:
            console.print(f"[red]✗ {escape(str(e))}[/]")
            if typer.confirm("Re-open the editor to fix?", default=True):
                text = edited
                continue
            return None
        show_plan_diff(ctx, plan.version, version)
        return ctx.store.latest_plan(ctx.story_id)


@plan_app.command("edit")
def plan_edit(story_id: Optional[str] = StoryOpt):
    """Edit the plan as YAML in your editor; saves a new version and shows the diff."""
    edit_plan(open_ctx(story_id))


def approve_plan(ctx: Ctx) -> bool:
    plan = ctx.store.latest_plan(ctx.story_id)
    if not show_validation(ctx, plan).ok:
        console.print("[red]Fix the errors above before approving.[/]")
        return False
    version = service.approve_plan(ctx)
    console.print(f"[green]Plan v{version} approved.[/] Start writing: python -m story write")
    return True


@plan_app.command("approve")
def plan_approve(story_id: Optional[str] = StoryOpt):
    """Approve the latest plan version so writing can start."""
    if not approve_plan(open_ctx(story_id)):
        raise typer.Exit(1)


def regenerate_act(ctx: Ctx, act_no: int) -> None:
    try:
        with console.status(f"Regenerating act {act_no} beats...") as status:
            version, changes = service.regenerate_act(ctx, act_no, progress=status.update)
    except service.ServiceError as e:
        console.print(f"[red]{escape(str(e))}[/]")
        return
    show_plan_diff(ctx, version - 1, version, limit=10)


@plan_app.command("regen")
def plan_regen(act: int = typer.Option(..., help="Act number to regenerate."), story_id: Optional[str] = StoryOpt):
    """Regenerate one act's 40 beats."""
    regenerate_act(open_ctx(story_id), act)


def plan_review(ctx: Ctx) -> None:
    """Interactive loop after planning: view, edit, steer, regenerate, approve."""
    while True:
        plan = ctx.store.latest_plan(ctx.story_id)
        show_plan_overview(ctx, plan)
        show_validation(ctx, plan)
        key = choose("Plan", {"a": "approve", "v": "view arc beats", "e": "edit YAML", "f": "feedback",
                              "r": "regenerate an act", "q": "quit"})
        if key == "a" and approve_plan(ctx):
            return
        if key == "v":
            arc_no = typer.prompt("Arc number (1-20)", type=int)
            a = next((x for x in plan.arcs if x.arc_no == arc_no), None)
            if a:
                console.print(escape(fmt.arc(a)))
                show_beats(plan, a.ep_start, a.ep_end, f"Arc {arc_no} beats")
                typer.prompt("Enter to continue", default="", show_default=False)
        elif key == "e":
            edit_plan(ctx)
        elif key == "f":
            feedback_flow(ctx, typer.prompt("Feedback on the plan"), stage="plan")
        elif key == "r":
            regenerate_act(ctx, typer.prompt("Act number (1-5)", type=int))
        elif key == "q":
            console.print("Plan saved. Resume with: python -m story plan show / plan approve")
            return


@plan_app.command("feedback")
def plan_feedback(text: str = typer.Argument(..., help='e.g. "make act 3 darker"'),
                  yes: bool = typer.Option(False, "--yes", "-y", help="Apply without confirming."),
                  story_id: Optional[str] = StoryOpt):
    """Steer the plan in plain language; routed into directives and beat rewrites (new plan version)."""
    ctx = open_ctx(story_id)
    stage = "plan" if ctx.store.last_approved_ep(ctx.story_id) == 0 else "episode"
    feedback_flow(ctx, text, stage=stage, yes=yes)


# --- feedback ----------------------------------------------------------------------

def show_routed(ctx: Ctx, routed: feedback.RoutedFeedback) -> None:
    plan = ctx.store.latest_plan(ctx.story_id)
    console.print(Panel(escape(routed.explanation or "(no explanation)"), title="How the feedback was routed"))
    for d in routed.directives:
        console.print(f"  [bold magenta]directive[/] {escape(d.text)} [dim]\\[{d.scope}][/]")
    for e in sorted(routed.plan_edits, key=lambda e: e.ep):
        old = plan.beat(e.ep)
        owned = " [bold yellow](rewrites a beat you wrote by hand)[/]" if old and old.human_owned else ""
        console.print(f"  [bold cyan]beat {e.ep}[/]{owned}\n    [red]- {escape(old.beat if old else '')}[/]\n"
                      f"    [green]+ {escape(e.new_beat)}[/]")
    for s in routed.state_edits:
        console.print(f"  [bold yellow]state[/] {s.character_id}: {escape(str(s.changes))}")
    if routed.regenerate_current:
        console.print("  [bold]→ suggests regenerating the current episode[/]")


def feedback_flow(ctx: Ctx, text: str, stage: str, next_ep: int | None = None,
                  yes: bool = False) -> feedback.RoutedFeedback | None:
    next_ep = next_ep or ctx.store.last_approved_ep(ctx.story_id) + 1
    try:
        with console.status("Routing feedback..."):
            routed = feedback.route(ctx.llm, ctx.store, ctx.story_id, text, stage, next_ep)
    except LLMError as e:
        console.print(f"[red]Could not route feedback:[/] {escape(str(e))}")
        return None
    show_routed(ctx, routed)
    if not (routed.directives or routed.plan_edits or routed.state_edits):
        console.print("[yellow]The router found nothing to change.[/]")
    if not yes and not typer.confirm("Apply these changes?", default=True):
        ctx.llm.decision("human:feedback_discarded", ep=next_ep)
        return None
    applied = feedback.apply(ctx.llm, ctx.store, ctx.story_id, text, routed, stage, next_ep)
    if applied.plan_to:
        console.print(f"[green]Plan v{applied.plan_from} → v{applied.plan_to}[/] "
                      f"({len(applied.plan_changes)} beats rewritten)")
    console.print(f"[green]Applied:[/] {len(applied.directive_ids)} directives, {len(applied.plan_changes)} plan "
                  f"changes, {len(applied.state_edits)} state edits")
    ctx.autosave(plan=True, hitl=True)
    return routed


@app.command("feedback")
def feedback_cmd(text: str = typer.Argument(..., help='e.g. "slow down the romance"'),
                 yes: bool = typer.Option(False, "--yes", "-y", help="Apply without confirming."),
                 story_id: Optional[str] = StoryOpt):
    """Give feedback at any time; it becomes directives + plan edits that carry forward."""
    ctx = open_ctx(story_id)
    next_ep = ctx.store.last_approved_ep(ctx.story_id) + 1
    routed = feedback_flow(ctx, text, stage="episode", next_ep=next_ep, yes=yes)
    draft = ctx.store.get_draft(ctx.story_id, next_ep)
    if routed and routed.regenerate_current and draft:
        ctx.store.set_episode_status(ctx.story_id, next_ep, draft.version, "rejected")
        ctx.store.add_event(ctx.story_id, next_ep, "reject", {"version": draft.version, "reason": f"feedback: {text}",
                                                              "summary": draft.delta.summary if draft.delta else None})
        console.print(f"Pending draft of episode {next_ep} discarded; `write` will regenerate it with the feedback.")


# --- episode review -----------------------------------------------------------------

def _mark(ok: bool, level: str = "fail") -> str:
    return "[green]✓[/]" if ok else ("[red]✗[/]" if level == "fail" else "[yellow]![/]")


def show_episode(ctx: Ctx, ep: Episode, full_text: bool = True) -> None:
    c = ep.checks or {}
    console.rule(f"[bold]Episode {ep.ep}[/] · {ep.status} v{ep.version}")
    if c.get("beat"):
        console.print(Panel(escape(c["beat"]), title="Planned beat", border_style="dim"))
    if c.get("note"):
        console.print(f"[magenta]Editor note applied:[/] {escape(c['note'])}")
    if full_text:
        console.print(Panel(escape(ep.text), title=f"Episode {ep.ep}", padding=(1, 2)))
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_row("words", str(ep.word_count))
    if ep.delta:
        t.add_row("hook", f"[bold]{ep.delta.hook_type}[/]: {escape(ep.delta.hook)}")
    crit = c.get("critic") or {}
    if crit:
        t.add_row("critic", f"hook {crit.get('hook_score')}/5 · momentum {crit.get('momentum_score')}/5 · "
                            f"{len(crit.get('issues', []))} issues")
    rules = c.get("rules") or []
    if rules:
        t.add_row("checks", "  ".join(f"{_mark(r['ok'], r['level'])} {r['name']}" for r in rules))
    t.add_row("revisions", str(c.get("revisions", 0)))
    t.add_row("cost", f"$0.00 actual · ${ep.cost_usd:.4f} list price · context {c.get('context_tokens', '?')} tokens")
    console.print(t)
    if c.get("needs_human"):
        console.print("[bold yellow]Checks still failing after the revision limit or budget cap. Your call:[/]")
        for f in c.get("failures", []):
            console.print(f"  [yellow]• {escape(f)}[/]")


def show_checks(ep: Episode) -> None:
    c = ep.checks or {}
    t = Table("check", "", "level", "detail", title="Rule checks")
    for r in c.get("rules", []):
        t.add_row(r["name"], _mark(r["ok"], r["level"]), r["level"], escape(r["detail"]))
    console.print(t)
    crit = c.get("critic") or {}
    if crit.get("issues"):
        it = Table("type", "severity", "evidence", "fix", title="Critic issues")
        for i in crit["issues"]:
            it.add_row(i["type"], i["severity"], escape(i["evidence"]), escape(i["fix"]))
        console.print(it)
    for h in c.get("history", []):
        console.print(f"[dim]pass {h['revision'] + 1}: {h['words']} words, "
                      f"{'passed' if h['passed'] else 'failed: ' + escape('; '.join(h['failures'])[:300])}[/]")


def produce(ctx: Ctx, ep: int, note: str | None = None) -> Episode:
    try:
        with console.status(f"Writing episode {ep}...") as status:
            return pipeline.produce_episode(ctx.llm, ctx.store, ctx.settings, ctx.story_id, ep, note=note,
                                            on_progress=status.update)
    except QuotaExhausted as e:
        console.print(f"[red]Free-tier quota used up:[/] {escape(str(e))}\nEverything approved is saved; "
                      f"run `python -m story write` after the reset to continue.")
        raise typer.Exit(2)
    except LLMError as e:
        console.print(f"[red]LLM step failed:[/] {escape(str(e))}")
        raise typer.Exit(2)
    except Exception as e:  # API errors after all retries
        console.print(f"[red]Episode {ep} failed:[/] {escape(type(e).__name__ + ': ' + str(e)[:300])}\n"
                      f"Nothing was committed; run `python -m story write` to retry.")
        raise typer.Exit(2)


def boundary_review(ctx: Ctx, rep: pipeline.BoundaryReport, auto: bool) -> None:
    console.rule(f"[bold]Arc {rep.arc_no} complete[/]")
    for err in rep.errors:
        console.print(f"[red]Arc-boundary step failed:[/] {escape(err)} · retry with `python -m story boundary {rep.arc_no}`")
    if rep.summary:
        console.print(Panel(escape(rep.summary), title=f"Arc {rep.arc_no} summary (replaces its episodes in memory)"))
    for c in rep.corrections:
        console.print(f"  [yellow]audit fix[/] {c['character_id']}: {escape(str(c['changes']))} ({escape(c['reason'])})")
    decision = "none"
    if rep.replan_to:
        show_plan_diff(ctx, rep.replan_from, rep.replan_to, limit=10)
        key = choose("Next-arc replan", {"a": "accept", "r": "revert to previous beats", "e": "edit plan YAML"}, "a")
        decision = {"r": "reverted", "a": "accepted"}.get(key) or ("accepted+edited" if edit_plan(ctx) else "accepted")
    service.record_boundary(ctx, rep, decision, auto)


def approve_draft(ctx: Ctx, draft: Episode, *, auto: bool = False, original: Episode | None = None) -> Episode | None:
    """Commit a draft. Returns None (and keeps the draft) if re-extraction or the commit fails."""
    try:
        with console.status("Committing...") as status:
            episode, boundary = service.approve(ctx, draft, auto=auto, original=original, progress=status.update)
    except Exception as e:  # quota, API error, invalid delta: the draft (and any human edit) stays saved
        console.print(f"[red]Approval failed:[/] {escape(type(e).__name__ + ': ' + str(e)[:300])}\n"
                      f"Draft v{draft.version} of episode {draft.ep} is saved; approve it later with "
                      f"`python -m story review {draft.ep}`.")
        return None
    console.print(f"[green]Episode {draft.ep} approved[/] ({'auto' if auto else 'human'}) → "
                  f"{ctx.out_dir / 'episodes' / f'ep_{draft.ep:03d}.md'}")
    if boundary:
        boundary_review(ctx, boundary, auto)
    return episode


def reject_and_regenerate(ctx: Ctx, draft: Episode, reason: str) -> Episode:
    try:
        with console.status(f"Rewriting episode {draft.ep}...") as status:
            return service.reject(ctx, draft, reason, progress=status.update)
    except QuotaExhausted as e:
        console.print(f"[red]Free-tier quota used up:[/] {escape(str(e))}")
        raise typer.Exit(2)
    except Exception as e:
        console.print(f"[red]Rewrite failed:[/] {escape(type(e).__name__ + ': ' + str(e)[:300])}\n"
                      f"The rejection is recorded; `python -m story write` will draft episode {draft.ep} again.")
        raise typer.Exit(2)


def review_loop(ctx: Ctx, draft: Episode) -> str:
    """Returns "approved" or "quit"."""
    show = True
    while True:
        if show:
            show_episode(ctx, draft)
        show = True
        key = choose(f"Episode {draft.ep}", {"a": "approve", "e": "edit", "r": "reject", "f": "feedback",
                                             "c": "checks", "q": "quit"})
        if key == "a":
            return "approved" if approve_draft(ctx, draft) else "quit"
        if key == "e":
            edited = edit_text(draft.text, suffix=".md")
            if edited is None or edited.strip() == draft.text.strip():
                console.print("[dim]No changes.[/]")
                show = False
                continue
            original = draft
            draft = service.save_human_edit(ctx, draft, edited)
            return "approved" if approve_draft(ctx, draft, original=original) else "quit"
        if key == "r":
            reason = typer.prompt("Why reject? (goes into the rewrite)")
            if typer.confirm("Also carry this forward as feedback for future episodes?", default=False):
                feedback_flow(ctx, reason, stage="episode", next_ep=draft.ep)
            draft = reject_and_regenerate(ctx, draft, reason)
        elif key == "f":
            text = typer.prompt("Feedback")
            routed = feedback_flow(ctx, text, stage="episode", next_ep=draft.ep)
            if routed and typer.confirm("Regenerate this episode now with the feedback?",
                                        default=routed.regenerate_current):
                draft = reject_and_regenerate(ctx, draft, f"editor feedback: {text}")
        elif key == "c":
            show_checks(draft)
            show = False
        elif key == "q":
            return "quit"


# --- writing ----------------------------------------------------------------------

@app.command()
def write(count: Optional[int] = typer.Option(None, "--count", "-n", help="Write this many episodes."),
          until: Optional[int] = typer.Option(None, help="Stop after this episode."),
          auto: bool = typer.Option(False, "--auto", help="Autopilot: approve episodes that pass all checks; "
                                                          "review only failures and arc boundaries."),
          debug_context: bool = typer.Option(False, "--debug-context", help="Dump each assembled prompt context."),
          story_id: Optional[str] = StoryOpt):
    """Write episodes, resuming after the last approved one. Unreviewed drafts are reused, never re-paid."""
    ctx = open_ctx(story_id)
    if ctx.store.latest_plan(ctx.story_id, approved_only=True) is None:
        console.print("[red]Approve the plan first:[/] python -m story plan show / plan approve")
        raise typer.Exit(1)
    start = ctx.store.last_approved_ep(ctx.story_id) + 1
    end = min(TOTAL_EPISODES, until or (start + count - 1 if count else TOTAL_EPISODES))
    if start > TOTAL_EPISODES:
        console.print("[green]All episodes are written.[/]")
        return
    console.print(f"{'Resuming' if start > 1 else 'Starting'} at episode {start}"
                  + (f", through episode {end}" if end < TOTAL_EPISODES else "") + (" [autopilot]" if auto else ""))
    for ep in range(start, end + 1):
        if debug_context:
            dump_context(ctx, ep)
        draft = ctx.store.get_draft(ctx.story_id, ep)
        if draft:
            console.print(f"[cyan]Found an unreviewed draft of episode {ep} (v{draft.version}); reusing it.[/]")
            ctx.llm.decision("resume:reuse_draft", ep=ep)
        else:
            draft = produce(ctx, ep)
        c = draft.checks or {}
        if auto and c.get("passed") and not c.get("needs_human"):
            show_episode(ctx, draft, full_text=False)
            if approve_draft(ctx, draft, auto=True) is None:
                return
            continue
        if review_loop(ctx, draft) == "quit":
            nxt = ctx.store.last_approved_ep(ctx.story_id) + 1
            console.print(f"Stopped. All approved work is saved. `python -m story write` resumes at episode {nxt}.")
            return
    console.print(f"[green]Done through episode {ctx.store.last_approved_ep(ctx.story_id)}.[/]")


def dump_context(ctx: Ctx, ep: int) -> Path:
    c = build_context(ctx.store, ctx.story_id, ep)
    path = ctx.settings.runs_dir / ctx.story_id / f"context_ep{ep:03d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(c.debug_report(), encoding="utf-8")
    t = Table("section", "tokens", title=f"Context for episode {ep}: {c.total_tokens} tokens")
    for name, n in c.tokens().items():
        t.add_row(name, str(n))
    console.print(t)
    console.print(f"[dim]Full prompt context → {path}[/]")
    return path


@app.command("directives")
def directives_cmd(edit: Optional[str] = typer.Option(None, "--edit", help="Directive id to reword."),
                   text: Optional[str] = typer.Option(None, "--text", help="New wording (with --edit)."),
                   off: Optional[str] = typer.Option(None, "--off", help="Directive id to deactivate."),
                   story_id: Optional[str] = StoryOpt):
    """List standing directives; reword (--edit ID --text ...) or deactivate (--off ID) one."""
    ctx = open_ctx(story_id)
    try:
        if edit:
            if not text:
                raise service.ServiceError("Give the new wording with --text.")
            service.update_directive(ctx, edit, text=text)
            console.print(f"[green]Directive {edit} reworded.[/]")
        if off:
            service.update_directive(ctx, off, active=False)
            console.print(f"[green]Directive {off} deactivated.[/]")
    except service.ServiceError as e:
        console.print(f"[red]{escape(str(e))}[/]")
        raise typer.Exit(1)
    t = Table("id", "active", "scope", "since", "text", title="Directives")
    for d in ctx.store.directives(ctx.story_id, active_only=False):
        t.add_row(d.id, "✓" if d.active else "-", d.scope, str(d.created_ep), escape(d.text))
    console.print(t)


@app.command("boundary")
def boundary_cmd(arc: int = typer.Argument(..., help="Completed arc number."),
                 only: Optional[str] = typer.Option(None, "--only", help="Comma list of steps: summary,audit,replan."),
                 story_id: Optional[str] = StoryOpt):
    """(Re)run the arc-boundary work: arc summary, character audit, next-arc replan."""
    ctx = open_ctx(story_id)
    plan = ctx.store.latest_plan(ctx.story_id)
    a = next((x for x in plan.arcs if x.arc_no == arc), None)
    if a is None or ctx.store.last_approved_ep(ctx.story_id) < a.ep_end:
        console.print(f"[red]Arc {arc} is not complete yet.[/]")
        raise typer.Exit(1)
    with console.status("Arc boundary...") as status:
        rep = pipeline.arc_boundary(ctx.llm, ctx.store, ctx.story_id, arc, on_progress=status.update,
                                    only=set(only.split(",")) if only else None)
    if not rep.summary:
        rep.summary = ctx.store.arc_summaries(ctx.story_id).get(arc, "")
    boundary_review(ctx, rep, auto=False)


@app.command("context")
def context_cmd(ep: int = typer.Argument(..., help="Episode number."), story_id: Optional[str] = StoryOpt):
    """Show what the writer would see for an episode (token counts per section)."""
    if not 1 <= ep <= TOTAL_EPISODES:
        console.print(f"[red]Episodes are 1-{TOTAL_EPISODES}.[/]")
        raise typer.Exit(1)
    dump_context(open_ctx(story_id), ep)


@app.command()
def review(ep: int = typer.Argument(..., help="Episode number."), story_id: Optional[str] = StoryOpt):
    """Re-open an episode: review a pending draft, or read an approved one (and retcon it)."""
    ctx = open_ctx(story_id)
    draft = ctx.store.get_draft(ctx.story_id, ep)
    if draft and ep == ctx.store.last_approved_ep(ctx.story_id) + 1:
        review_loop(ctx, draft)
        return
    approved = ctx.store.approved_episode(ctx.story_id, ep)
    if approved is None:
        console.print(f"[red]Episode {ep} has no approved version or pending draft.[/]")
        raise typer.Exit(1)
    show_episode(ctx, approved)
    key = choose(f"Episode {ep}", {"e": "edit (retcon)", "f": "feedback", "c": "checks", "q": "quit"}, "q")
    if key == "e":
        edit_episode(ep, file=None, story_id=ctx.story_id)
    elif key == "f":
        feedback_flow(ctx, typer.prompt("Feedback"), stage="episode")
    elif key == "c":
        show_checks(approved)


# --- retcon ---------------------------------------------------------------------------

@app.command("edit-episode")
def edit_episode(ep: int = typer.Argument(..., help="Approved episode to rewrite."),
                 file: Optional[Path] = typer.Option(None, "--file", help="Take the new text from this file "
                                                                           "instead of opening the editor."),
                 story_id: Optional[str] = StoryOpt):
    """Rewrite an approved episode; memory is re-extracted and later episodes are checked for conflicts."""
    ctx = open_ctx(story_id)
    old = ctx.store.approved_episode(ctx.story_id, ep)
    if old is None:
        console.print(f"[red]Episode {ep} is not approved.[/]")
        raise typer.Exit(1)
    new_text = file.read_text(encoding="utf-8-sig") if file else edit_text(old.text, suffix=".md")
    if not new_text or new_text.strip() == old.text.strip():
        console.print("[dim]No changes.[/]")
        return
    pending = ctx.settings.runs_dir / ctx.story_id / f"pending_retcon_ep{ep:03d}.md"
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text(new_text, encoding="utf-8")
    try:
        with console.status(f"Re-extracting episode {ep} and checking later episodes..."):
            rep = retcon.retcon(ctx.llm, ctx.store, ctx.story_id, ep, new_text.strip())
    except Exception as e:
        console.print(f"[red]Retcon failed:[/] {escape(type(e).__name__ + ': ' + str(e)[:300])}\n"
                      f"Your text is saved at {pending}; retry with "
                      f"`python -m story edit-episode {ep} --file \"{pending}\"`.")
        raise typer.Exit(2)
    pending.unlink(missing_ok=True)
    for c in rep.check_errors:
        console.print(f"[yellow]Episode {c['ep']} could not be checked:[/] {escape(c['error'])}")
    console.rule(f"[bold]Retcon of episode {ep}[/] (v{rep.old_version} → v{rep.new_version})")
    for t in rep.removed_facts:
        console.print(f"  [red]- {escape(t)}[/]")
    for t in rep.added_facts:
        console.print(f"  [green]+ {escape(t)}[/]")
    for cid, ch in rep.character_changes.items():
        console.print(f"  [yellow]~ {cid}[/]: {escape(str(ch['old']))} → {escape(str(ch['new']))}")
    for t in rep.thread_changes:
        console.print(f"  [cyan]~ thread {escape(t)}[/]")
    console.print(f"Later episodes mentioning changed entities: {rep.affected or 'none'}")
    if not rep.conflicts:
        console.print("[green]No conflicts found in later episodes.[/]")
        retcon.resolve(ctx.store, ctx.story_id, rep, "accepted (no conflicts)")
        ctx.autosave(plan=True, episodes=True, hitl=True)
        return
    t = Table("ep", "conflicting text", "fix", title="Conflicts")
    for c in rep.conflicts:
        t.add_row(str(c["ep"]), escape(c["evidence"]), escape(c["fix"]))
    console.print(t)
    first = rep.first_conflict
    key = choose("Resolve", {"a": "accept as-is", "g": f"regenerate from ep {first}",
                             "p": f"patch {len(rep.conflicts)} flagged episodes", "r": "replan remaining beats"}, "p")
    if key == "a":
        retcon.resolve(ctx.store, ctx.story_id, rep, "accepted as-is")
    elif key == "g":
        dropped = ctx.store.rollback_to(ctx.story_id, first - 1)
        retcon.resolve(ctx.store, ctx.story_id, rep, f"regenerate from ep {first}", {"superseded": dropped})
        console.print(f"Episodes {dropped} are no longer canon. `python -m story write` continues at episode {first}.")
    elif key == "p":
        patched = {}
        with console.status("Patching flagged episodes...") as status:
            for c in sorted(rep.conflicts, key=lambda c: c["ep"]):
                status.update(f"Patching episode {c['ep']}...")
                patched[c["ep"]] = retcon.patch_episode(ctx.llm, ctx.store, ctx.story_id, c["ep"],
                                                        [f"Conflicts with the rewritten episode {ep}: \"{c['evidence']}\". "
                                                         f"Fix: {c['fix']}"])
        retcon.resolve(ctx.store, ctx.story_id, rep, "patched flagged episodes", {"patched_versions": patched})
        console.print(f"[green]Patched episodes {sorted(patched)}.[/]")
    elif key == "r":
        last = ctx.store.last_approved_ep(ctx.story_id)
        res = None
        if last >= TOTAL_EPISODES:
            console.print("[dim]Every episode is written; nothing left to replan.[/]")
        else:
            with console.status("Replanning..."):
                res = pipeline.replan_next_arc(ctx.llm, ctx.store, ctx.story_id, planner.arc_no_for(last + 1) - 1,
                                               keep_upto=last)
        if res:
            show_plan_diff(ctx, res[0], res[1], limit=10)
        retcon.resolve(ctx.store, ctx.story_id, rep, "replanned remaining beats",
                       {"plan_to": res[1] if res else None})
    ctx.autosave(plan=True, episodes=True, hitl=True)


# --- status, report, export -------------------------------------------------------------

@app.command()
def status(story_id: Optional[str] = StoryOpt):
    """Where the story is: last episode, open threads, directives, spend, quota."""
    ctx = open_ctx(story_id)
    s, sid = ctx.store, ctx.story_id
    plan, approved_plan = s.latest_plan(sid), s.latest_plan(sid, approved_only=True)
    last = s.last_approved_ep(sid)
    bible = s.latest_bible(sid)
    state = s.state_at(sid, last)
    draft = s.get_draft(sid, last + 1)
    console.print(Panel(
        f"[bold]{escape(bible.title if bible else '(planning)')}[/]  [dim]story {sid}[/]\n"
        f"Plan: v{plan.version if plan else '-'} ({'approved v' + str(approved_plan.version) if approved_plan else 'not approved'})\n"
        f"Approved episodes: {last}/{TOTAL_EPISODES}"
        + (f" · pending draft of ep {last + 1} (v{draft.version})" if draft else "")
        + (f"\nNext beat (ep {last + 1}): {escape(plan.beat(last + 1).beat)}" if plan and plan.beat(last + 1) else ""),
        title="Status"))
    threads = state.open_threads()
    if threads:
        t = Table("thread", "title", "opened", "last touched", "due", title=f"Open threads ({len(threads)})")
        for th in sorted(threads, key=lambda x: x.due_by_ep or 10**6):
            late = th.due_by_ep is not None and th.due_by_ep <= last
            t.add_row(th.id, escape(th.title), str(th.opened_ep), str(th.last_touched_ep),
                      f"[red]{th.due_by_ep} OVERDUE[/]" if late else str(th.due_by_ep or "-"))
        console.print(t)
    ds = s.directives(sid)
    if ds:
        console.print("[bold]Active directives[/]")
        for d in ds:
            console.print(escape(fmt.directive(d)))
    gone = [c for c in state.characters.values() if c.status != "alive"]
    console.print(f"Characters: {len(state.characters)} ({len(gone)} not alive"
                  + (": " + ", ".join(f"{c.name} [{c.status}]" for c in gone) if gone else "") + ")")
    summ = report.summarize(report.load_log(ctx.llm.logger.path))
    total = sum(b["list_price_usd"] for b in summ["by_step"].values())
    calls = sum(b["calls"] for b in summ["by_step"].values())
    console.print(f"Spend: $0.00 actual (free tier) · ${total:.4f} list price over {calls} calls")
    q = ctx.llm.quota
    console.print("Quota: " + ", ".join(
        f"{m} {'[red]used up[/]' if q.exhausted(m) else '[green]ok[/]'}" for m in ctx.llm.chain("writer") + ctx.llm.chain("fast")))


@app.command("report")
def report_cmd(project: int = typer.Option(TOTAL_EPISODES, "--project", help="Project cost/time to N episodes."),
               story_id: Optional[str] = StoryOpt):
    """Cost and latency per step/episode from the logs, projected to N episodes."""
    ctx = open_ctx(story_id)
    summ = report.summarize(report.load_log(ctx.llm.logger.path))
    t = Table("step", "calls", "failed", "tokens in", "tokens out", "list $", "avg s", title="By step")
    for step, b in sorted(summ["by_step"].items(), key=lambda kv: -kv[1]["list_price_usd"]):
        t.add_row(step, str(b["calls"]), str(b["failed_attempts"]), f"{b['tokens_in']:,}", f"{b['tokens_out']:,}",
                  f"{b['list_price_usd']:.4f}", f"{b['latency_ms'] / max(1, b['calls']) / 1000:.1f}")
    console.print(t)
    p = report.project(summ, project)
    n_models = len(ctx.llm.chain("writer"))
    days = p["projected_writer_requests"] / (report.FREE_WRITER_REQUESTS_PER_DAY * n_models)
    console.print(Panel(
        f"Measured episodes: {p['episodes_measured']}\n"
        f"Per episode: ${p['per_episode_list_usd']:.4f} list · {p['per_episode_seconds']:.0f}s model time · "
        f"{p['per_episode_tokens']:,.0f} tokens · {p['writer_calls_per_episode']:.2f} writer calls\n"
        f"{project} episodes ≈ [bold]${p['projected_list_usd']:.2f}[/] list price (actual $0, free tier), "
        f"≈ {p['projected_hours_model_time']:.1f} h model time\n"
        f"Free-tier limit: ~{p['projected_writer_requests']:.0f} writer requests ≈ {days:.1f} days at "
        f"{report.FREE_WRITER_REQUESTS_PER_DAY}/day × {n_models} models",
        title=f"Projection to {project} episodes"))


@app.command("ui")
def ui_cmd(port: int = typer.Option(8765, help="Local port."),
           no_browser: bool = typer.Option(False, "--no-browser", help="Don't open a browser tab.")):
    """Open the local web UI (plan, write/review, feedback, memory, retcon, cost). Localhost only."""
    from .web import serve
    serve(port=port, open_browser=not no_browser)


@app.command("export")
def export_cmd(out: Optional[Path] = typer.Option(None, "--out", help="Folder to write (default: demo/)."),
               story_id: Optional[str] = StoryOpt):
    """Write the deliverable folder: arc_plan.md, episodes/, hitl_log.md, cost_report.md, story.db, runs/."""
    ctx = open_ctx(story_id)
    target = out or export.DEMO_DIR
    paths = export.export_all(ctx.store, ctx.story_id, ctx.llm.logger.path, len(ctx.llm.chain("writer")),
                              demo_dir=target, db_path=ctx.settings.db_path)
    console.print(f"[green]Exported {len(paths)} files[/] to {target}")
