"""Drafting and revising episodes (writer tier)."""
from __future__ import annotations

import re

from . import fmt
from .config import DEFAULT_BANNED_PHRASES, EPISODE_WORDS, TOTAL_EPISODES
from .context import Context
from .llm import LLM, EpisodeBudget, Result, render_prompt
from .planner import HOOK_TYPES


def pick_hook_type(hint: str | None, recent: list[str]) -> str:
    """The beat's hint, unless it repeats one of the last three hooks."""
    if hint and hint not in recent:
        return hint
    return next(h for h in HOOK_TYPES if h not in recent)


def system_prompt(ctx: Context) -> tuple[str, str]:
    b = ctx.bible
    return render_prompt(
        "draft", title=b.title, bible=fmt.bible(b), word_min=EPISODE_WORDS[0], word_max=EPISODE_WORDS[1],
        pov=b.pov, tense=b.tense, banned="; ".join(dict.fromkeys(DEFAULT_BANNED_PHRASES + b.banned_phrases)),
    )


def clean_prose(text: str) -> str:
    """Strip headings, fences and trailing notes the model sometimes adds."""
    t = text.strip()
    t = re.sub(r"^```[a-z]*\s*|\s*```$", "", t)
    lines = t.splitlines()
    while lines and re.match(r"^\s*(#+\s|\*\*?episode\b|episode\s+\d+\b|title:)", lines[0], re.IGNORECASE):
        lines.pop(0)
    while lines and re.match(r"^\s*[\(\[]?\s*(word count|words?:|\d+\s+words)", lines[-1], re.IGNORECASE):
        lines.pop()
    return "\n".join(lines).strip()


def draft(llm: LLM, ctx: Context, budget: EpisodeBudget | None = None, note: str | None = None) -> Result:
    system, sys_version = system_prompt(ctx)
    hook_type = pick_hook_type(ctx.beat.hook_type_hint if ctx.beat else None, ctx.recent_hook_types)
    task, task_version = render_prompt(
        "draft_task", ep=ctx.ep, total=TOTAL_EPISODES, beat=ctx.beat.beat if ctx.beat else "(continue the story)",
        note=f"\nEDITOR NOTE FOR THIS EPISODE: {note}\n" if note else "", hook_type=hook_type,
        recent_hooks=", ".join(ctx.recent_hook_types) or "(none yet)",
        word_min=EPISODE_WORDS[0], word_max=EPISODE_WORDS[1],
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": ctx.text() + "\n\n" + task}]
    result = llm.call("draft", "writer", messages, ep=ctx.ep, budget=budget,
                      prompt_version=f"{sys_version}+{task_version}")
    result.text = clean_prose(result.text)
    return result


def revise(llm: LLM, ctx: Context, text: str, problems: list[str], budget: EpisodeBudget | None = None) -> Result:
    system, sys_version = system_prompt(ctx)
    task, task_version = render_prompt(
        "revise", ep=ctx.ep, problems=fmt.bullets(problems), draft=text,
        word_min=EPISODE_WORDS[0], word_max=EPISODE_WORDS[1],
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": ctx.text() + "\n\n" + task}]
    result = llm.call("revise", "writer", messages, ep=ctx.ep, budget=budget,
                      prompt_version=f"{sys_version}+{task_version}")
    result.text = clean_prose(result.text)
    return result
