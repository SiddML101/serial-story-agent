<!-- version: arc_audit_v2 -->
You audit a serial story's continuity database at the end of arc $arc_no. Compare the RECORDS with what the EPISODES actually show. Report only records that are wrong or out of date. Do not invent anything not shown in the text.

CHARACTER RECORDS
$characters

OPEN THREADS (id: title — description)
$threads

THREADS THE PLAN EXPECTED THIS ARC TO RESOLVE
$planned

EPISODES $ep_start-$ep_end
$episodes

Return JSON:
- corrections: [{"character_id": ..., "changes": {field: value}, "reason": "..."}] for character records the text contradicts or that are missing something. Allowed fields: status (alive|dead|missing|unknown), location, knows (list of things they learned), relationships ({other_id: description}), goal.
- thread_updates: [{"thread_id": ..., "status": "resolved" | "abandoned", "reason": "..."}] for open threads whose question the episodes clearly ANSWERED (resolved) or made moot (abandoned). Quote or paraphrase the answering moment in reason. Leave a thread out if it is still genuinely open.
Return empty lists if the records are accurate.
