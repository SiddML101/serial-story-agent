# serial-story-agent

An agentic system that writes a **200-episode serial story** from a one-line premise, with a human in the loop.
It plans the whole arc (5 acts → 20 arcs → 200 beats), then writes 400–700-word episodes that each end on a hook. The story stays consistent at episode 150 as well as episode 5, because memory is layered and the context window is bounded, and every draft is checked before a human sees it.

Plain Python: a state machine, SQLite, Pydantic, and a Typer/Rich CLI. No agent framework.

- **Demo output:** [`demo/arc_plan.md`](demo/arc_plan.md) (full 200-episode plan) · [`demo/episodes/`](demo/episodes/) (15 episodes) · [`demo/hitl_log.md`](demo/hitl_log.md) (every human intervention and what it changed) · [`demo/retcon_demo.md`](demo/retcon_demo.md) (rewriting ep 3 → conflicts flagged in later episodes) · [`demo/cost_report.md`](demo/cost_report.md) · raw log [`demo/runs/982e0b44/log.jsonl`](demo/runs/982e0b44/log.jsonl)
- **Design write-up:** [`DECISIONS.md`](DECISIONS.md)

## Setup (< 5 minutes)

You need Python 3.11+ and a free Google AI Studio API key (https://aistudio.google.com/apikey).

**Windows (PowerShell)**
```powershell
git clone https://github.com/SiddML101/serial-story-agent.git; cd serial-story-agent
python -m venv .venv
.venv\Scripts\Activate.ps1        # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements.txt
copy .env.example .env            # then put your key in LLM_API_KEY
```

**macOS / Linux**
```bash
git clone https://github.com/SiddML101/serial-story-agent.git && cd serial-story-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # then put your key in LLM_API_KEY
```

Then pick models and check the connection:
```bash
python scripts/list_models.py     # see what your key can use
# in .env:
#   WRITER_MODEL=gemini-3.8-flash,gemini-3.7-flash,gemini-3.5-flash,gemini-3.6-flash   (fallback chain)
#   FAST_MODEL=gemini-3.5-flash-lite,gemini-3.1-flash-lite                               (fallback chain)
python scripts/smoke_test.py      # one call per tier: reply, tokens, latency
python -m pytest                  # 99 tests, fake LLM, no network (also run in CI on Ubuntu + Windows)
```

Any OpenAI-compatible endpoint works. To switch provider, change `LLM_BASE_URL`, `LLM_API_KEY` and the model names in `.env`.

## Web UI

```bash
python -m story ui                    # opens http://127.0.0.1:8765 (localhost only; your key never leaves the machine)
```

The UI does everything the CLI does:

| Tab | What you can do |
|---|---|
| **Write** | See the planned beat and next beats, draft the next episode, **edit the text in place**, approve (your edit is re-extracted into memory), reject with a reason (it goes into the rewrite), or give feedback. Shows checks, critic issues, hook, cost and revision history. At arc ends: review the summary, audit fixes and next-arc replan, then accept or revert it. |
| **Plan** | Browse acts, arcs and all 200 beats. **Edit any unwritten beat** (it becomes yours, and replans won't overwrite it), steer the plan in plain language, regenerate an act, edit the whole plan as YAML, compare versions, approve. |
| **Episodes** | Read approved episodes. **Rewrite one (retcon)**, see which later episodes conflict, then patch them, un-canon from the first conflict, replan, or accept. |
| **Memory** | Standing directives (reword or turn off), characters (status, location, knows, relationships), threads (overdue flagged), facts, arc summaries. |
| **Activity / Cost** | Every human intervention; per-step cost and latency, plus the 200-episode projection. |

Long LLM steps run as background jobs with live progress, one at a time. You can close the tab: work continues, and the page picks the job back up when reopened. The UI and CLI share one service layer (`story/service.py`) and one database, so you can switch between them freely.

## Inspect the demo run without an API key

The demo story's database and log are committed under `demo/`, so after setup (an empty `LLM_API_KEY` is fine):

```bash
# PowerShell: $env:DB_PATH="demo/story.db"; $env:RUNS_DIR="demo/runs"
export DB_PATH=demo/story.db RUNS_DIR=demo/runs
python -m story status                # 15/200 episodes, open threads, directives, spend
python -m story report --project 200  # per-step cost/latency from the real log + 200-episode projection
python -m story plan show --arc 2     # the replanned arc 2
python -m story review 15             # read an approved episode with its checks
python -m story context 16            # exactly what the writer would see next, with token counts
```

## Using it

```bash
python -m story new "A delivery rider realizes every address on today's route belongs to someone who died in the same building."
#   → bible + cast + 5 acts + 20 arcs + 200 beats, then a review loop:
#     [a]pprove [v]iew arc beats [e]dit YAML [f]eedback [r]egenerate an act [q]uit

python -m story write                 # interactive: review every episode
python -m story write --auto          # autopilot: only failures and arc boundaries need you
python -m story write --count 5       # or --until 15
python -m story feedback "slow down the romance"    # any time; becomes directives + plan edits
python -m story status                # last episode, open/overdue threads, directives, spend, quota
python -m story report --project 200  # cost/latency per step and per episode, projected to 200
python -m story export                # writes demo/
```

| Command | What it does |
|---|---|
| `plan show [--arc N] [--act N]` | Overview plus validation, or the beats of one arc/act |
| `plan edit` | The plan as YAML in your editor ($EDITOR, Notepad on Windows). Validated, saved as a new version, diff shown |
| `plan feedback "<text>"` | Plain-language steering ("darker act 3"), routed into beat rewrites and directives |
| `plan regen --act N` / `plan approve` / `plan generate` | Regenerate one act / approve / resume interrupted planning |
| `review <ep>` | Re-open a pending draft, or read an approved episode (then retcon it) |
| `edit-episode <ep> [--file f]` | **Retcon**: rewrite an approved episode; memory is re-extracted and later episodes are checked for conflicts |
| `context <ep>` / `write --debug-context` | Show exactly what the writer sees, with token counts per section |
| `directives [--edit ID --text ...] [--off ID]` | List standing directives; reword or retire one that is too broad |
| `boundary <arc> [--only replan]` | Re-run arc-boundary work (summary, audit, replan) |
| `ui [--port 8765]` | The local web UI (see above) |

All commands take `--story-id` (default: the most recent story).

**The episode review screen** shows the planned beat, the full text, word count, every check (✓/✗), the hook and its type, critic scores, revisions and cost, then:
`[a]pprove [e]dit [r]eject [f]eedback [c]hecks [q]uit`

- **Edit** opens the text in your editor. The memory delta is **re-extracted from your edited text**, so canon is what you wrote.
- **Reject** asks for a reason, which goes into the rewrite (and optionally becomes standing feedback).
- **Quit** any time. `write` resumes after the last approved episode, and an unreviewed draft is shown again instead of being regenerated.

## Architecture

```
premise ─▶ planner (writer tier) ─▶ Bible + cast + 5 acts + 20 arcs ─▶ 40 beats × 5 acts ─▶ validator
                                                                                              │
                                                         HUMAN: approve / edit YAML / feedback / regen act
                                                                                              │
     ┌──────────────────────────────── per episode ───────────────────────────────────────────┘
     │  build_context(ep)  ── bounded (~12k tokens budget, same at ep 5 and ep 150)
     │  draft (writer) ─▶ extract delta (fast, JSON) ─▶ rule checks ─▶ critic (fast, JSON)
     │        ▲                                                           │ fail?
     │        └──────── revise with the specific failures (≤ MAX_REVISIONS, within $ cap) ◀┘
     │  HUMAN (or autopilot if all checks pass): approve · edit · reject · feedback
     │  COMMIT: append episode + delta (nothing is canon before this)
     │  arc end: arc summary (fast) → character audit (fast) → replan next arc (writer) → HUMAN accept/revert
     └──────────────────────────────────────────────────────────────────────────────────────────
```

| Module | Role |
|---|---|
| `story/llm.py` | The only way to call a model: model fallback chain, free-tier quota tracking, retries/backoff, JSON parsing with one error-feedback retry, per-episode budget, JSONL logging |
| `story/store.py` | SQLite, append-only and versioned. `state_at(ep)` = snapshot + replay of approved deltas, which is what makes retcons possible |
| `story/planner.py` | Hierarchical plan, validator (200 contiguous beats, arc/act ranges, thread resolution, cast coverage, near-duplicate beats, hook runs), YAML round trip |
| `story/context.py` | Budgeted context builder; trims oldest summaries → extra facts → old arc summaries → 2nd-last episode text |
| `story/writer.py` · `extractor.py` · `checks.py` | Draft/revise · structured delta + sanitization · rule checks + LLM critic |
| `story/pipeline.py` | The bounded episode loop, commit, arc-boundary summary/audit/replan |
| `story/feedback.py` | Feedback router → directives, plan edits, state edits |
| `story/retcon.py` | Rewrite history: delta diff, affected-episode search, conflict check, patch / regenerate / replan |
| `story/export.py` · `report.py` | `demo/` files, cost report and 200-episode projection |
| `prompts/*.md` | Every prompt, with a version tag logged on each call |

**Traceability.** Every call and decision goes to `runs/<story_id>/log.jsonl`: step, model, tier, tokens in/out, `cost_usd`, `list_price_usd`, latency, attempt, status and prompt version. Decisions are logged too: `check_failed:word_count→revise`, `budget_exceeded→needs_human`, `fallback:gemini-3.7-flash→gemini-3.5-flash`, `quota_exhausted…`, `human:approve`, `auto:approve`, `human:reject→regenerate`, `feedback_routed:2 directives,6 plan edits`, `replan_arc2:v4→v5`.

**Bounded.** Each episode gets at most `MAX_REVISIONS` (2) revisions and a per-episode cap of `MAX_COST_PER_EPISODE_USD` (0.15, list price), checked *before* each call. Beyond that the episode goes to the human, marked `needs_human` with the remaining issues listed. API calls retry at most 8 times with exponential backoff, and a model that keeps returning 503s is bypassed for that call.

## Cost & time

From the real demo log (15 episodes, `python -m story report`, full table in [`demo/cost_report.md`](demo/cost_report.md)):

| | per episode | 200 episodes |
|---|---:|---:|
| list price | $0.0265 | **≈ $5.92** (incl. planning $0.34 + 20 arc boundaries) |
| actual spend (free tier) | $0 | $0 |
| model time | ~60 s | ≈ 3.6 h + human review + rate-limit waits |
| tokens | ~39k (context 1.2k at ep 1 → ~6.5k from ep 10) | |
| writer calls | 2.07 (draft + revisions) | ~440 requests ≈ 5.5 days of free quota |

Method:

```
200-episode cost = avg list-price per episode (from logs) × 200 + planning + arc-boundary avg × 20
time             = avg model seconds per episode × 200 + planning + boundaries   (+ human review)
```

This project ran entirely on the **free Gemini tier**, so actual spend is $0. Costs are reported at list price (`PRICING` in `story/config.py`; the values there are placeholders to confirm against the provider's pricing page). **On the free tier the real constraint is requests, not money:** about 20 requests per day per Flash model. That's why `WRITER_MODEL` is a fallback chain and why the run tracks quota (`runs/quota.json`) and never re-hits a model that's out for the day.

**How to cut cost and time:**
0. **Fewer false-positive failures.** Revisions are ~45% of episode cost. Verifying critic evidence already prevented 5 needless rewrites in 15 episodes.
1. **Prompt caching.** The system prompt (bible + rules) is a static prefix by design, so cache it.
2. **Cheap model for everything except prose.** Extraction, critic, summaries, audit and feedback routing already run on `FAST_MODEL`.
3. **Skip the critic when every rule check passes.** That saves one call per clean episode.
4. **Lower thinking budgets.** Reasoning tokens are counted as output and dominate writer-call cost.
5. **Batch API in autopilot.** Cheaper, and latency doesn't matter there.
6. **Shorter summaries for old arcs.** Already partly done: older arc summaries are cut to their first sentences.

## Known limitations

See [`DECISIONS.md`](DECISIONS.md) §4 for the full list. In short:

- **The demo's "human" was Claude** acting as editor through the real CLI. The interventions and their effects are real, but the editorial judgment was an AI's. Two retcon *test* runs (on a database copy) were removed from the committed log; their result is in `demo/retcon_demo.md`.
- **Episode-150 behaviour is measured on synthetic history** (`tests/test_long_horizon.py`, `tests/test_context.py`), not on a real 150-episode story.
- **Four different writer models wrote the 15 demo episodes** (free-tier fallback), so the voice drifts.
- **The critic's hook score did not discriminate** (every episode got 4–5). The calibrated prompt (critic v3) is untested against real output.
- **Extraction quality bounds memory quality.** A fact the extractor misses is forgotten. Arc-boundary audits and human review of edits mitigate this but don't eliminate it.
- **Retrieval is TF-IDF plus rarity-weighted entity tags,** not embeddings. That's fine for 200 episodes, but it will miss paraphrases.
- **Check thresholds are heuristics** (summary similarity 0.6, 12 shared 5-grams). They're tuned on a small run.
- **Free-tier availability is the practical bottleneck.** In the demo, 39 of 56 draft attempts failed (503s or quota), and failed attempts appear to count against the ~20/day/model quota. Two API keys turned out to share one project quota.
- **The fast-tier critic over-reads directives.** Evidence verification and a calibrated prompt reduce this; it is not eliminated. The retcon checker had 1 false positive out of 4 flags in the demo.
- **Scripted demo editor:** `scripts/demo_editor.py` stands in for `$EDITOR` so plan and episode edits are reproducible. Normal use opens your real editor.
- **Single user, local SQLite, no concurrent writers.**
