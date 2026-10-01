"""Consistency and quality checks: free rule-based checks first, then an LLM critic (fast tier)."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Literal

from pydantic import BaseModel, field_validator

from . import fmt
from .config import DEFAULT_BANNED_PHRASES, EPISODE_WORDS
from .context import Context, name_pattern
from .llm import LLM, EpisodeBudget, render_prompt
from .models import EpisodeDelta
from .planner import HOOK_TYPES

SUMMARY_SIMILARITY_MAX = 0.6  # TF-IDF cosine vs any earlier episode summary
SHARED_5GRAMS_MAX = 12  # distinct 5-word phrases shared with the last 10 episodes
MIN_HOOK_SCORE = 3


@dataclass
class Check:
    name: str
    ok: bool
    level: Literal["fail", "warn"]  # "warn" never fails the episode; it is passed to the critic to judge
    detail: str = ""


def word_count(text: str) -> int:
    return len(text.split())


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def ngrams(text: str, n: int = 5) -> set[tuple[str, ...]]:
    w = _words(text)
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}


def summary_similarity(summary: str, past: list[str]) -> tuple[float, int]:
    """Highest TF-IDF cosine between `summary` and any past summary, and which index it was."""
    if not past or not summary.strip():
        return 0.0, -1
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity

    try:
        m = TfidfVectorizer(stop_words="english").fit_transform([summary] + past)
    except ValueError:
        return 0.0, -1
    sims = cosine_similarity(m[0], m[1:])[0]
    i = int(sims.argmax())
    return float(sims[i]), i


def rule_checks(text: str, delta: EpisodeDelta | None, ctx: Context) -> list[Check]:
    checks: list[Check] = []
    n = word_count(text)
    lo, hi = EPISODE_WORDS
    checks.append(Check("word_count", lo <= n <= hi, "fail", f"{n} words (target {lo}-{hi})"))

    patterns = list(dict.fromkeys(DEFAULT_BANNED_PHRASES + [re.escape(p) for p in ctx.bible.banned_phrases]))
    hits = sorted({m.group(0) for p in patterns for m in re.finditer(p, text, re.IGNORECASE)})
    checks.append(Check("banned_phrases", not hits, "fail", ", ".join(f'"{h}"' for h in hits)))

    if delta is not None:
        repeat = delta.hook_type in ctx.recent_hook_types
        allowed = [h for h in HOOK_TYPES if h not in ctx.recent_hook_types]
        checks.append(Check("hook", bool(delta.hook.strip()) and not repeat, "fail",
                            f"{delta.hook_type}: {delta.hook}" + (
                                f" (repeats one of the last 3 hook types: {', '.join(ctx.recent_hook_types)}; "
                                f"end instead on a {' or '.join(allowed[:3])})" if repeat else "")))
        past = [e for e in ctx.previous if e.delta]
        sim, i = summary_similarity(delta.summary, [e.delta.summary for e in past])
        checks.append(Check("summary_repetition", sim <= SUMMARY_SIMILARITY_MAX, "fail",
                            f"max similarity {sim:.2f}" + (f" with ep {past[i].ep}" if i >= 0 else "")))

    grams = ngrams(text)
    shared: Counter = Counter()
    for e in ctx.previous[-10:]:
        for g in grams & ngrams(e.text):
            shared[" ".join(g)] += 1
    examples = "; ".join(f'"{g}"' for g, _ in shared.most_common(5))
    checks.append(Check("phrase_repetition", len(shared) <= SHARED_5GRAMS_MAX, "fail",
                        f"{len(shared)} 5-word phrases reused from recent episodes" + (f", e.g. {examples}" if shared else "")))

    gone = [c for c in ctx.state.characters.values() if c.status in ("dead", "missing") and name_pattern(c).search(text)]
    checks.append(Check("dead_or_missing_named", not gone, "warn",
                        ", ".join(f"{c.name} ({c.status})" for c in gone)))

    overdue = [t for t in ctx.state.open_threads() if t.due_by_ep is not None and t.due_by_ep < ctx.ep]
    touched = set(delta.threads_advanced + delta.threads_resolved) if delta else set()
    untouched = [t for t in overdue if t.id not in touched]
    checks.append(Check("overdue_threads", not untouched or len(untouched) < len(overdue), "warn",
                        ", ".join(t.id for t in untouched)))

    beat_chars = [ctx.state.characters[c] for c in (ctx.beat.characters if ctx.beat else []) if c in ctx.state.characters]
    absent = [c.name for c in beat_chars if not name_pattern(c).search(text)]
    checks.append(Check("beat_characters_present", not absent, "warn", ", ".join(absent)))
    return checks


# --- LLM critic ---------------------------------------------------------------

class Issue(BaseModel):
    type: Literal["contradiction", "dead_character", "directive_violation", "beat_drift", "repetition", "timeline", "voice"]
    severity: Literal["low", "med", "high"]
    evidence: str = ""
    fix: str = ""

    @field_validator("severity", mode="before")
    @classmethod
    def _sev(cls, v):
        return {"medium": "med", "moderate": "med", "critical": "high", "major": "high", "minor": "low"}.get(
            str(v).lower(), str(v).lower())

    @field_validator("type", mode="before")
    @classmethod
    def _type(cls, v):
        v = str(v).lower().replace(" ", "_").replace("-", "_")
        return {"dead_character_acting": "dead_character", "directive": "directive_violation",
                "continuity": "contradiction", "pov": "voice", "tense": "voice"}.get(v, v)


class CriticReport(BaseModel):
    issues: list[Issue] = []
    hook_score: int
    momentum_score: int


CRITIC_SECTIONS = ["EDITOR DIRECTIVES (must follow; they override the plan)", "CURRENT ARC",
                   "RECENT EPISODES (summaries)", "TIMELINE (in-story time)", "CHARACTERS IN THIS EPISODE",
                   "OPEN THREADS (touch at least one)", "ESTABLISHED FACTS (never contradict)", "THIS EPISODE'S BEAT",
                   "UPCOMING BEATS (do NOT write these yet; only set them up)"]


def critic(llm: LLM, text: str, ctx: Context, flags: list[Check], budget: EpisodeBudget | None = None) -> CriticReport:
    prompt, version = render_prompt(
        "critic", ep=ctx.ep, context=ctx.text(only=CRITIC_SECTIONS), draft=text,
        flags=fmt.bullets([f"{c.name}: {c.detail}" for c in flags]),
    )
    return llm.call("critic", "fast", [{"role": "user", "content": prompt}], json_schema=CriticReport,
                    ep=ctx.ep, budget=budget, prompt_version=version).parsed


def evaluate(rules: list[Check], report: CriticReport | None) -> tuple[bool, list[str]]:
    """Fail on any failed rule check, any high-severity critic issue, or a weak hook."""
    failures = [f"{c.name}: {c.detail}" for c in rules if not c.ok and c.level == "fail"]
    if report:
        failures += [f"{i.type} ({i.severity}): {i.evidence} → fix: {i.fix}" for i in report.issues if i.severity == "high"]
        if report.hook_score < MIN_HOOK_SCORE:
            failures.append(f"weak hook (score {report.hook_score}/5): make the ending more compelling")
    return not failures, failures


def _norm_quote(s: str) -> str:
    s = s.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return " ".join(re.sub(r"[^a-z0-9' ]+", " ", s).split())


def evidence_in_draft(evidence: str, text: str, min_len: int = 12) -> bool:
    """True if the critic's quoted evidence (allowing '...' elisions) really appears in the draft."""
    draft = _norm_quote(text)
    parts = [_norm_quote(p) for p in re.split(r"\.\.\.|…", evidence)]
    parts = [p for p in parts if len(p) >= min_len]
    return bool(parts) and all(p in draft for p in parts)


def verify_critic(report: CriticReport, text: str) -> list[str]:
    """Downgrade high-severity issues whose evidence is not in the draft (e.g. the model quoting a directive back).
    Returns notes on what was downgraded. A hallucinated issue must not trigger a paid rewrite."""
    notes = []
    for i in report.issues:
        if i.severity == "high" and not evidence_in_draft(i.evidence, text):
            i.severity = "low"
            i.fix = f"[unverified: evidence not found in draft] {i.fix}"
            notes.append(f"{i.type}: {i.evidence[:80]}")
    return notes


def run_checks(llm: LLM, text: str, delta: EpisodeDelta | None, ctx: Context,
               budget: EpisodeBudget | None = None) -> dict:
    rules = rule_checks(text, delta, ctx)
    flags = [c for c in rules if not c.ok and c.level == "warn"]
    report = critic(llm, text, ctx, flags, budget)
    downgraded = verify_critic(report, text)
    if downgraded:
        llm.decision(f"critic_unverified:{len(downgraded)} high issues downgraded", ep=ctx.ep, issues=downgraded)
    passed, failures = evaluate(rules, report)
    return {"passed": passed, "failures": failures, "rules": [asdict(c) for c in rules],
            "critic": report.model_dump() if report else None}
