"""Builds the writer's context for one episode within a fixed token budget, whatever the episode number."""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field, replace

from . import fmt
from .config import TOTAL_EPISODES
from .llm import estimate_tokens
from .models import ArcPlan, Beat, Bible, Character, Directive, Episode, Fact, StoryState
from .store import Store

CONTEXT_BUDGET_TOKENS = 12_000


@dataclass(frozen=True)
class Knobs:
    """How much of each trimmable section to include. Trimmed in TRIM_ORDER until the context fits."""

    ep_summaries: int = 10
    facts: int = 30
    arc_summaries_full: int = 4  # older arc summaries are cut to their first sentences
    prev_episodes_full: int = 2
    compact_cast: int = 30
    threads: int = 20


TRIM_ORDER = [  # (knob, step, minimum): cheapest losses first
    ("ep_summaries", 1, 3),
    ("facts", 5, 10),
    ("arc_summaries_full", 1, 1),
    ("prev_episodes_full", 1, 1),
    ("compact_cast", 5, 8),
    ("threads", 4, 8),
    ("facts", 2, 4),
]


@dataclass
class Context:
    ep: int
    bible: Bible
    plan: ArcPlan
    beat: Beat | None
    state: StoryState
    directives: list[Directive]
    scene_characters: list[Character]
    facts: list[Fact]  # ranked, all candidates; `selected_facts` is what went into the prompt
    recent_hook_types: list[str]
    previous: list[Episode]
    knobs: Knobs = field(default_factory=Knobs)
    sections: list[tuple[str, str]] = field(default_factory=list)

    @property
    def selected_facts(self) -> list[Fact]:
        return self.facts[: self.knobs.facts]

    def tokens(self) -> dict[str, int]:
        return {name: estimate_tokens(body) for name, body in self.sections}

    @property
    def total_tokens(self) -> int:
        return sum(self.tokens().values())

    def text(self, only: list[str] | None = None) -> str:
        return "\n\n".join(f"## {name}\n{body}" for name, body in self.sections if only is None or name in only)

    def debug_report(self) -> str:
        rows = "\n".join(f"| {name} | {n} |" for name, n in self.tokens().items())
        return (f"# Context for episode {self.ep}\n\nTotal ≈ {self.total_tokens} tokens (budget "
                f"{CONTEXT_BUDGET_TOKENS}); knobs: {self.knobs}\n\n| section | tokens |\n|---|---|\n{rows}\n\n"
                f"---\n\n{self.text()}\n")


# --- selection helpers ------------------------------------------------------------

def name_pattern(c: Character) -> re.Pattern:
    names = {c.name}
    first = c.name.split()[0] if c.name.split() else c.name
    if len(first) >= 3:
        names.add(first)
    return re.compile(r"\b(" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")\b")


def mentioned(characters: dict[str, Character], text: str) -> list[str]:
    return [cid for cid, c in characters.items() if name_pattern(c).search(text)]


def active_directives(directives: list[Directive], ep: int) -> list[Directive]:
    out = []
    for d in directives:
        if not d.active:
            continue
        if d.scope.startswith("until_ep:"):
            try:
                if ep > int(d.scope.split(":", 1)[1]):
                    continue
            except ValueError:
                pass
        out.append(d)
    return out


def rank_facts(facts: list[Fact], entities: set[str], query: str) -> list[Fact]:
    """Rarity-weighted entity overlap + TF-IDF similarity to the beat, world rules boosted, recency as tie-break.

    Entity matches are weighted by inverse frequency: at ep 150 the protagonist is tagged on hundreds of facts, so
    matching "leo" says little, while matching a rare side character or place says a lot. Without this, filler facts
    about the main cast crowd out the one fact the scene needs.
    """
    if not facts:
        return []
    df: Counter = Counter(e.lower() for f in facts for e in set(f.entities))
    n = len(facts)
    sims = [0.0] * len(facts)
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity

        matrix = TfidfVectorizer(stop_words="english").fit_transform([query] + [f.text for f in facts])
        sims = cosine_similarity(matrix[0], matrix[1:])[0].tolist()
    except ValueError:  # empty vocabulary
        pass
    ents = {e.lower() for e in entities}

    def score(i: int) -> float:
        f = facts[i]
        overlap = sum(math.log((n + 1) / (df[e] + 1)) for e in ents & {x.lower() for x in f.entities})
        return overlap + 4.0 * sims[i] + (1.0 if f.kind == "rule" else 0.0) + f.ep / 10_000

    return [facts[i] for i in sorted(range(len(facts)), key=score, reverse=True)]


# Narrative glue that legitimately recurs in every episode; never worth flagging as a tic.
NARRATIVE_COMMON = set("""says said asks like just back pulls looks hand hands eyes face head voice door doesn't don't
can't it's that's there's what's isn't won't he's she's i'm you're they're leans steps turns moves stands holds""".split())


def overused_words(previous: list[Episode], exclude: set[str], window: int = 10, share: float = 0.7,
                   top: int = 8) -> list[str]:
    """Descriptive words used in most recent episodes (a prose tic), excluding the story's own plot vocabulary.

    Heuristic: a word in >= `share` of the last `window` episodes that is not a stopword, a name, narrative glue, or a
    word from the plan/bible (those recur by design: "terminal", "courier"). What remains is mostly description that
    has gone stale: in the demo run, "grease", "heavy", "sharp", "cold", "iron".
    """
    from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS

    recent = previous[-window:]
    if len(recent) < 5:
        return []
    df: Counter = Counter()
    total: Counter = Counter()
    for e in recent:
        words = [w for w in re.findall(r"[a-z']+", e.text.lower()) if len(w) >= 4]
        total.update(words)
        df.update(set(words))
    need = math.ceil(share * len(recent))
    bad = ENGLISH_STOP_WORDS | NARRATIVE_COMMON | exclude
    hits = [w for w, n in df.items() if n >= need and w not in bad and w.rstrip("s") not in bad]
    return sorted(hits, key=lambda w: (-df[w], -total[w]))[:top]


def plot_vocabulary(ctx: "Context") -> set[str]:
    arc = ctx.plan.arc_for(ctx.ep)
    near = [b.beat for b in ctx.plan.beats if (arc and arc.ep_start <= b.ep <= arc.ep_end) or ctx.ep <= b.ep <= ctx.ep + 3]
    b = ctx.bible
    text = " ".join(near + ([arc.title, arc.goal, arc.turning_point] if arc else []) +
                    [b.title, b.logline, b.setting, *b.world_rules] +
                    [c.name + " " + c.role for c in ctx.state.characters.values()])
    words = set(re.findall(r"[a-z']+", text.lower()))
    return words | {w.rstrip("s") for w in words}


def first_sentences(text: str, max_words: int = 45) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    cut = " ".join(words[:max_words])
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[: end + 1] if end > 40 else cut) + " …"


# --- build ------------------------------------------------------------------------

def gather(store: Store, story_id: str, ep: int) -> Context:
    """Collect everything the builder could include for `ep` (state is as of the end of ep-1)."""
    plan = store.latest_plan(story_id)
    bible = store.latest_bible(story_id)
    state = store.state_at(story_id, ep - 1)
    beat = plan.beat(ep)
    previous = store.approved_episodes(story_id, max(1, ep - 10), ep - 1)
    last_text = previous[-1].text[-2500:] if previous else ""

    scene_ids = [c for c in (beat.characters if beat else []) if c in state.characters]
    for cid in mentioned(state.characters, (beat.beat if beat else "") + "\n" + last_text):
        if cid not in scene_ids:
            scene_ids.append(cid)
    scene = [state.characters[c] for c in scene_ids]

    entities = set(scene_ids) | set(beat.threads if beat else [])
    entities |= {w.lower() for c in scene for w in c.name.split()}
    query = (beat.beat if beat else "") + " " + " ".join(c.name for c in scene)
    facts = rank_facts(state.active_facts(), entities, query)

    hooks = [e.delta.hook_type for e in previous[-3:] if e.delta]
    return Context(ep=ep, bible=bible, plan=plan, beat=beat, state=state,
                   directives=active_directives(store.directives(story_id), ep), scene_characters=scene,
                   facts=facts, recent_hook_types=hooks, previous=previous)


def render(ctx: Context, arc_summaries: dict[int, str]) -> list[tuple[str, str]]:
    k, ep, plan, state = ctx.knobs, ctx.ep, ctx.plan, ctx.state
    arc = plan.arc_for(ep)
    sections: list[tuple[str, str]] = []

    sections.append(("EDITOR DIRECTIVES (must follow; they override the plan)",
                     "\n".join(fmt.directive(d) for d in ctx.directives) or "(none)"))
    tics = overused_words(ctx.previous, plot_vocabulary(ctx))
    if tics:
        sections.append(("STYLE WATCH (descriptions used in most recent episodes; vary them or drop them)",
                         ", ".join(tics)))
    sections.append(("ACT OUTLINE", fmt.acts(plan.acts)))
    if arc:
        sections.append(("CURRENT ARC", fmt.arc(arc)))

    past_arcs = sorted((n, t) for n, t in arc_summaries.items() if arc is None or n < arc.arc_no)
    if past_arcs:
        full_from = len(past_arcs) - k.arc_summaries_full
        sections.append(("STORY SO FAR (arc summaries)", "\n".join(
            f"Arc {n}: {t if i >= full_from else first_sentences(t)}" for i, (n, t) in enumerate(past_arcs))))

    summaries = [e for e in ctx.previous if e.delta][-k.ep_summaries:]
    if summaries:
        sections.append(("RECENT EPISODES (summaries)", "\n".join(
            f"Ep {e.ep}: {e.delta.summary} [hook: {e.delta.hook_type}]" for e in summaries)))

    if state.timeline:
        sections.append(("TIMELINE (in-story time)", "\n".join(
            f"Ep {t.ep}: {t.in_story_time}" for t in state.timeline[-5:])
            + f"\nEpisode {ep} continues from {state.timeline[-1].in_story_time}."))

    new_ids = [c for c in (ctx.beat.characters if ctx.beat else []) if c not in state.characters]
    sections.append(("CHARACTERS IN THIS EPISODE", "\n".join(fmt.character(c) for c in ctx.scene_characters)
                     + ("\nNew characters this beat introduces: " + ", ".join(new_ids) if new_ids else "")
                     or "(none listed)"))

    scene_ids = {c.id for c in ctx.scene_characters}
    others = sorted((c for c in state.characters.values() if c.id not in scene_ids),
                    key=lambda c: (c.first_ep != 0, -c.last_seen_ep))[: k.compact_cast]
    if others:
        sections.append(("OTHER CAST (not necessarily in this episode)", "\n".join(fmt.character(c, full=False)
                                                                                  for c in others)))

    beat_threads = set(ctx.beat.threads if ctx.beat else [])
    threads = sorted(state.open_threads(),  # this beat's threads, then overdue, then longest untouched
                     key=lambda t: (t.id not in beat_threads, not (t.due_by_ep is not None and t.due_by_ep < ep),
                                    t.last_touched_ep))
    shown = threads[: k.threads]
    more = f"\n(+{len(threads) - len(shown)} more open threads not shown)" if len(threads) > len(shown) else ""
    sections.append(("OPEN THREADS (touch at least one)",
                     ("\n".join(fmt.thread(t, ep) for t in shown) + more) or "(none yet)"))

    if ctx.selected_facts:
        sections.append(("ESTABLISHED FACTS (never contradict)", "\n".join(fmt.fact(f) for f in ctx.selected_facts)))

    full = ctx.previous[-k.prev_episodes_full:] if ctx.previous else []
    if full:
        sections.append(("PREVIOUS EPISODES (full text, for voice and continuity)", "\n\n".join(
            f"### Episode {e.ep}\n{e.text}" for e in full)))

    upcoming = [b for b in plan.beats if ep < b.ep <= ep + 3]
    sections.append(("THIS EPISODE'S BEAT", fmt.beat(ctx.beat) if ctx.beat else "(no beat planned)"))
    sections.append(("UPCOMING BEATS (do NOT write these yet; only set them up)", fmt.beats(upcoming)))
    return sections


def build_context(store: Store, story_id: str, ep: int, budget_tokens: int = CONTEXT_BUDGET_TOKENS) -> Context:
    if not 1 <= ep <= TOTAL_EPISODES:
        raise ValueError(f"episode must be 1-{TOTAL_EPISODES}")
    ctx = gather(store, story_id, ep)
    arc_summaries = store.arc_summaries(story_id)
    ctx.sections = render(ctx, arc_summaries)
    for knob, step, minimum in TRIM_ORDER:
        while ctx.total_tokens > budget_tokens and getattr(ctx.knobs, knob) > minimum:
            ctx.knobs = replace(ctx.knobs, **{knob: max(minimum, getattr(ctx.knobs, knob) - step)})
            ctx.sections = render(ctx, arc_summaries)
    return ctx
