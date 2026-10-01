# Screen recording script (< 5 min)

**Prep:**
- Use a story whose plan is approved and that has a few episodes written, so no step waits on a long generation. Your own sandbox (`$env:DB_PATH="mytest.db"`) or a copy of the demo both work.
- Run `python -m story ui`, zoom the browser to about 110%, and close other tabs.
- Check **⚙ Settings** first: the quota dots in the top bar should be green.
- If Google is returning 503s that day, record the drafting step in advance and narrate over it.

## UI version (recommended)

| Time | Tab | Do | Say |
|---|---|---|---|
| 0:00–0:35 | **Plan** | Scroll acts → arcs → beats. Click **Edit** on an unwritten beat, change it, **Save beat**. Click a version in *Versions* to show the diff. | "A one-line premise becomes a bible, a cast, 5 acts, 20 arcs and 200 beats. The validator checks the structure. My edit is a new plan version, marked as mine so replans won't overwrite it." |
| 0:35–1:40 | **Write** | **Draft episode N**. While it runs, point at the progress bar. When it lands, point at the checks (✓/✗), the critic scores, the hook, "what memory will record" and the cost. Edit one sentence in the text box, then **Approve with my edits**. | "Draft → structured memory delta → rule checks → critic → up to 2 targeted rewrites within a cost cap. Nothing is canon until I approve, and my edit is re-extracted, so memory matches what I wrote." |
| 1:40–2:40 | **Write** | **Feedback…** → type "slow down the romance" (or something that fits your story) → **Route it**. Show the directive and the old → new beats → **Apply**. | "Feedback isn't a one-off prompt tweak. It becomes a standing directive, checked in every later episode, plus rewrites of the upcoming beats as a new plan version." |
| 2:40–3:20 | **Write** | Draft the next episode. Show that it follows the rewritten beat. Then **Reject & rewrite…** with "too much exposition" → the new draft. | "The next episode follows the change. A rejection reason goes straight into the rewrite." |
| 3:20–3:50 | **Memory** | Show directives (reword one), characters (dead or alive, what they know), threads (overdue in red). | "This is the memory the writer works from at episode 150 as well as episode 5. The context stays bounded, around 6–8k tokens." |
| 3:50–4:20 | **Episodes** | Open an older episode → **Rewrite this episode (retcon)** → change a fact → save → the conflict list. | "Rewriting history: memory is re-extracted and only the later episodes that mention the changed people or places are checked, then I choose patch, rewrite or replan." |
| 4:20–4:50 | **Cost**, **Activity** | The 200-episode projection, then the activity timeline. Close the tab, reopen it, and show the work is still there. | "Every call is logged with tokens, cost and latency. It's resumable: close it any time and it continues where it stopped." |

## CLI version

| Time | Show | Command / action |
|---|---|---|
| 0:00–0:30 | Plan | `python -m story plan show`, then `plan show --arc 1`, then `plan edit` (change one beat) |
| 0:30–1:30 | Episode review | `python -m story write --count 1`: walk through the checks, then `c` for details and `a` to approve |
| 1:30–2:30 | Feedback | `python -m story feedback "slow down the romance"`: show the directive and beat diff, then `y` |
| 2:30–3:15 | Carries forward + reject | `write --count 1`, then `r` with a reason, then approve the rewrite |
| 3:15–3:45 | Resume | `write`, press `q`, run `write` again (resumes at the same episode) |
| 3:45–4:15 | Traceability | `python -m story status`, then `report --project 200` |
| 4:15–4:45 | Retcon | `python -m story edit-episode 3`: change a fact, see the conflicts |

Close on `demo/hitl_log.md`, scrolling past a feedback entry: the directive, the beat diff, and what was written for those beats.
