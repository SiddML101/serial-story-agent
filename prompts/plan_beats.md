<!-- version: beats_v1 -->
You are breaking Act $act_no of a $total-episode serial into one beat per episode.

BIBLE
$bible

MAIN CAST
$cast

ALL ACTS
$acts

ARCS IN THIS ACT
$arcs

PREVIOUS BEATS (for continuity; do not repeat these events)
$prev_beats

Write exactly $n beats, for episodes $ep_start to $ep_end inclusive, one per episode. Each beat has:
- ep, arc_no (the arc containing that episode)
- beat: 1-2 sentences naming the concrete event or turn of THIS episode (who does what, what changes)
- characters: ids of characters present (existing ids above; a new side character may use a new slug)
- threads: thread slugs this episode touches (from the arcs' threads_opened/threads_resolved)
- hook_type_hint: one of cliffhanger, revelation, arrival, reversal, decision, threat, question. Vary them; never use the same type in 3 consecutive episodes.

Rules:
- Every episode must change something. No filler, no two beats describing the same event.
- Each arc reaches its turning_point in its last 1-2 episodes.
- Open each arc's threads early in that arc and resolve its threads_resolved by its end.
- Every main character appears several times in this act.
- Pace reveals so tension builds across the whole act.

Return {"beats": [...]}.
