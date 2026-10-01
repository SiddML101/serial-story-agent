<!-- version: retcon_check_v1 -->
A human editor rewrote episode $ep of a serial story. Later episodes were written against the OLD version. Decide whether episode $later_ep now contradicts the NEW version, or depends on something that is no longer true.

WHAT CHANGED IN EPISODE $ep
Facts no longer true (old version):
$removed
Facts now true (new version):
$added
Character changes:
$characters

NEW SUMMARY OF EPISODE $ep
$new_summary

EPISODE $later_ep TEXT
$text

Return JSON {"conflict": true|false, "evidence": "short quote from episode $later_ep that conflicts (empty if none)", "fix": "one concrete instruction to make episode $later_ep consistent (empty if none)"}. Flag only real contradictions, not stylistic differences.
