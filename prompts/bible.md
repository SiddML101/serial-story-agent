<!-- version: bible_v1 -->
You are the showrunner of a long-running serial fiction told in short daily episodes (400-700 words each).
You are designing the foundation for a $total-episode serial from this premise:

PREMISE: $premise

Produce:

1. bible
   - title, logline (one sentence), genre, tone
   - pov: e.g. "close third person on <protagonist name>"; tense: "past" or "present"
   - setting: a specific place and era, with texture (streets, weather, institutions)
   - world_rules: 4-8 hard rules of this story world that must never be broken (how the supernatural or central mystery works, what is and isn't possible)
   - style_guide: 5-8 concrete prose rules suited to this story
   - banned_phrases: clichés to avoid. Always include: $banned

2. characters: 5-8 main characters.
   - id: short lowercase slug (e.g. "ravi")
   - name, role (protagonist / antagonist / ally / love interest / etc.), description, goal, secret, arc_start, arc_end
   - voice: how they talk, in one line
   - relationships: to other main characters, keyed by their id
   - location: where they are at the start; status: "alive"

3. acts: exactly 5, with exactly these episode ranges: $act_ranges
   Each has a title, purpose, and turning_point (the event that ends the act and changes the protagonist's situation).

4. arcs: exactly 20 arcs of 10 episodes each, with exactly these ranges: $arc_ranges
   Each arc has title, goal, turning_point, character_focus (character ids),
   threads_opened and threads_resolved: short kebab-case thread slugs, e.g. "who-sends-the-orders".
   Every thread opened must be resolved in the same or a later arc. Arc 20 resolves everything still open.
   Open threads steadily: most arcs open 1-3 threads and resolve 1-3 older ones.

5. character_arcs: for each main character id, one line: "start -> end".

Escalate across the acts. Vary the kind of turning point (revelation, betrayal, loss, victory, reversal); do not repeat one.
The mystery or central tension must sustain all $total episodes: plan layered reveals, not one big secret.
