# Design decisions

## 1. Memory at episode 150

Nothing is memory until approved. A draft proposes a structured **delta** (atomic facts, character changes, threads, ~100-word summary, hook, in-story time). **State at N = replay of approved deltas 1..N plus human/audit corrections**, cached as snapshots. Append-only history makes reject, edit, resume and retcon simple versioning.

The writer's context has a **token budget (12k), not an episode-number dependence**:

| Always in context | Retrieved / summarized |
|---|---|
| Bible + style rules (static prefix) · **directives** at the top · act outline, current arc, beat + next 3 · last 2 episodes in full · **style watch** (descriptions overused lately) | Scene characters in full, rest of the cast in one line each · top-30 facts by **rarity-weighted entity overlap** + TF-IDF · open threads (this beat's, then overdue, then stalest; cap 20) · last 10 summaries · all arc summaries (old ones shortened) · last 5 timeline entries |

Measured: 1.2k tokens at ep 1 → ~6.5k from ep 10 in the real run. Synthetic 150-episode history: 7.7k. A **long-horizon test** plants facts at eps 7, 33 and 90 in a 149-episode history and checks the ep-150 context. Its first run failed: protagonist filler facts crowded out a rare on-topic fact. Entity matches are now weighted by rarity.

## 2. Where the human steps in

**Plan approval** (cheapest point to steer) · **each episode** (approve / edit / reject + reason / feedback; edits are re-extracted so memory matches the human text) · **arc boundaries** (summary, audit, replan; the only stop in `--auto`, ~20 reviews per 200 episodes) · **anytime** (`feedback`, `directives --edit/--off`, `edit-episode` retcons).

Feedback becomes **scoped directives** (injected into every later prompt, checked by the critic) plus **beat rewrites** (a new plan version with a diff), never a one-off prompt tweak. Beats a human wrote are `human_owned`: replans never overwrite them, and the router is told to keep their specifics. That rule was added after the router reworded one in the demo.

## 3. Catching problems before the human

Rule checks first (free): length, banned phrases, hook-type variety, summary similarity, reused 5-grams, dead characters named, overdue threads. Then an LLM critic (fast tier) against facts, states, directives and the beat. **The critic's quoted evidence must actually appear in the draft**, otherwise a "high" issue is downgraded. In the demo the critic quoted directives back as "evidence" 5 times; each would have cost a paid rewrite. At most 2 targeted revisions within a per-episode cost cap, then `needs_human` (3 of 15 episodes). At arc ends an audit corrects character records and **reconciles threads**: it closes answered ones and re-dates planned resolutions that slipped. In the demo run 0 of 6 threads were ever closed, which made "overdue" meaningless.

## 4. What breaks first, and honest limits

1. **Extraction errors compound.** Ep 8's location was mislabeled and facts are sparse (49 in 15 episodes, none superseded). Audits and re-extraction mitigate this; a second extraction pass is the next step.
2. **The cheap critic is the weakest link.** It over-read directives, and its hook score gave every episode 4–5, so that gate never fired. The calibrated prompt (v3) is **untested against real output**.
3. **Planner contradictions.** The arc-2 replan said Kallen vanished "five years ago" (canon: three weeks). A human caught it in the diff.
4. **Prose tics.** "Grease" appears in 10 of 15 episodes, even after a directive against it. The style watch is a frequency heuristic, not a fix.
5. **Free tier.** ~20 requests/day per model, failed attempts seem to count, two keys shared one quota, and 39 of 56 draft attempts failed. Four writer models wrote the 15 episodes, so the voice drifts.

**About the demo:** Claude played the human editor; the interventions are real tool calls, but the judgment was an AI's. Two retcon *test* runs (on a database copy) were removed from the log, and the retcon result is in `demo/retcon_demo.md`. The ep-150 figures use synthetic history. **Cost:** $0.0265 per episode at list price (actual $0) → ~$5.9 for 200 episodes; revisions are ~45% of that. Free-tier quota, not money, is the constraint: ~5.5 days for 200 episodes.
