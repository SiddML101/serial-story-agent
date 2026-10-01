# Retcon demo: "a human rewrites episode 3, what happens to the episodes after it?"

Run with `python -m story edit-episode 3 --file ep3_retcon.md` on a **copy** of the database after episode 15, so the
demo canon stays intact. The edit changes one fact: Harlan Reed, the first delivery recipient, no longer died of cardiac
arrest at 02:15 that night; the terminal now says he died **in 1998, of smoke inhalation in the Ashwood Monolith**.

What the system did:
1. Re-extracted episode 3's memory delta from the new text and diffed it against the old one (facts removed and added,
   character knowledge changed, thread links changed).
2. Committed the edit as a new version. State snapshots from episode 3 on are invalidated and replayed.
3. Found the later episodes that mention changed entities. It ignores entities present in most episodes, such as the
   protagonist, which don't discriminate.
4. Ran a cheap conflict check (fast tier) on only those episodes.

Result: **conflicts in eps 4, 6 and 8**, the three places where Leo says Reed died "an hour ago" or "at two-fifteen".
These are the true positives. **Ep 12** was flagged too, which is a false positive (a terminal description). The human
then chooses: accept as-is · regenerate from ep 4 · patch the 4 flagged episodes (a targeted rewrite of each,
re-extracted) · replan the remaining beats.

```
─────────────────────────────────────── Retcon of episode 3 (v3 → v4) ───────────────────────────────────────
  - The tenant of Apartment 2A at 142 Crane Street is Harlan Reed.
  - Harlan Reed was pronounced dead at 02:15 a.m. from cardiac arrest.
  - Leo Vance's handlebar terminal confirmed Shift Zero delivery 1/5 as completed.
  + Apartment 2A at 142 Crane Street is occupied by Harlan Reed, who was pronounced dead on 14 March 1998 due
to smoke inhalation.
  + Touching the tenant of Apartment 2A leaves an ice-cold, greasy, translucent film on Leo's palm that 
smells of stagnant pond mud and does not wipe off.
  ~ leo: {'location': '142 Crane Street, Apartment 2A', 'knows': ['Delivered the first package to Harlan Reed
at Apartment 2A', "Harlan Reed's mortality record states he died at 02:15 a.m."]} → {'location': '142 Crane 
Street, Apartment 2A corridor', 'knows': ['Apartment 2A tenant is Harlan Reed, pronounced dead in 1998', 'The
delivery recipient took both the manila envelope and the rusted key'], 'relationships': {'harlan-reed': 
'Delivered the sealed envelope and rusted key to him in Apartment 2A'}}
Later episodes mentioning changed entities: [4, 5, 6, 7, 8, 9, 10, 11, 12, 14]
                                                  Conflicts                                                  
┌────┬───────────────────────────────────────────────────┬──────────────────────────────────────────────────┐
│ ep │ conflicting text                                  │ fix                                              │
├────┼───────────────────────────────────────────────────┼──────────────────────────────────────────────────┤
│ 4  │ The tenant at Crane Street was dead, Grideon.     │ Change Leo's dialogue to state that Harlan Reed  │
│    │ Pronounced an hour ago.                           │ was pronounced dead in 1998 rather than an hour  │
│    │                                                   │ ago.                                             │
│ 6  │ "Harlan Reed was pronounced dead at two-fifteen," │ Change Leo's dialogue to state that Harlan Reed  │
│    │ Leo says, leaning over his top tube.              │ died in 1998 from smoke inhalation, matching the │
│    │                                                   │ new mortuary record details in Episode 3.        │
│ 8  │ "He was dead an hour before I got there."         │ Update Leo's dialogue to reflect that Harlan     │
│    │                                                   │ Reed died in 1998 (twenty-eight years prior)     │
│    │                                                   │ rather than an hour before the delivery.         │
│ 12 │ The proximity terminal on his grips remains dark, │ Update the description of Leo's terminal to      │
│    │ but the raw metal casing radiates a dry, electric │ reflect that touching the Apartment 2A tenant    │
│    │ heat against his hands.                           │ left an ice-cold, greasy, translucent film on    │
│    │                                                   │ his palm rather than dry electric heat.          │
└────┴───────────────────────────────────────────────────┴──────────────────────────────────────────────────┘
```
