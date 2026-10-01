<!-- version: extract_v2 -->
You maintain the continuity database for a serial story. Read episode $ep and record exactly what changed. Record only what the text actually shows or states; do not guess or plan ahead.

KNOWN CHARACTERS (id | name | status | location)
$characters

OPEN THREADS (id: title)
$threads

PLANNED THREAD SLUGS FOR THIS PART OF THE STORY (reuse these ids when a matching thread opens)
$planned_threads

THREADS THE PLAN EXPECTS THIS ARC TO RESOLVE (if this episode answers one, list it in threads_resolved)
$planned_resolutions

RELEVANT EXISTING FACTS (with ids)
$facts

EPISODE $ep TEXT
$text

Return JSON with:
- summary: ~100 words, past tense, what happened and what changed. Include names.
- hook: the final hook in one sentence. hook_type: one of cliffhanger, revelation, arrival, reversal, decision, threat, question.
- in_story_time: when this episode happens in story time, e.g. "Day 2, 9:40 pm". Continue from the timeline.
- new_facts: 3-8 atomic, durable facts established in this episode (one claim each), e.g. "Flat 4B's tenant, Mrs. Iyer, died in 2019". entities: character ids, thread ids and lowercase place/object names involved. kind: event | attribute | rule | relationship. If a fact replaces an existing one, set supersedes to that fact's id.
- new_characters: characters who appear for the first time (not in the known list). id = lowercase slug of their name. Fill what the text shows; status alive unless shown otherwise.
- character_updates: for known characters whose state changed: {"id": ..., "changes": {...}}. Allowed change keys: status (alive|dead|missing|unknown), location, goal, knows (list of NEW things they learned), relationships ({other_id: description}), description, secret. Only mark someone dead if the text shows the death.
- threads_opened: new story questions raised: {"id": slug, "title": ..., "description": ...}.
- threads_advanced: ids of open threads this episode moved forward.
- threads_resolved: ids of open threads this episode answered or closed.
