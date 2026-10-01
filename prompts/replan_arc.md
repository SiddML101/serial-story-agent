<!-- version: replan_arc_v1 -->
You are the showrunner of a $total-episode serial. Arc $done_arc just finished. Revise the beats for the NEXT arc (arc $arc_no, episodes $ep_start-$ep_end) so they follow from what actually happened, not just from the original plan.

BIBLE
$bible

ACT OUTLINE
$acts

WHAT HAS HAPPENED (arc summaries)
$arc_summaries

CURRENT STATE
Characters:
$characters
Open threads:
$threads

EDITOR DIRECTIVES (must be respected)
$directives

NEXT ARC PLAN
$arc

CURRENT BEATS FOR THE NEXT ARC (some may already include the editor's requested changes; keep those intent-for-intent)
$beats

THE ARC AFTER (keep the next arc heading toward it)
$after

Return JSON {"beats": [...]} with exactly $n beats for episodes $ep_start-$ep_end. Each: ep, arc_no, beat (1-2 sentences), characters (ids), threads (slugs), hook_type_hint (cliffhanger|revelation|arrival|reversal|decision|threat|question; no type 3 times in a row). Keep the arc's goal and turning point unless events made them impossible. Dead characters cannot act. Change only what needs changing.
