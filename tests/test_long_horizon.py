"""Does memory hold at episode 150? Plant facts early in a 149-episode history and check the ep-150 context.

The history is synthetic (random filler facts about the main cast), so this tests retrieval and state replay at
scale, not prose. Each planted item is something a writer at ep 150 must not forget.
"""
import pytest

from helpers import delta_for, episode_text, make_story
from story.context import CONTEXT_BUDGET_TOKENS, build_context
from story.models import Character, CharacterUpdate, Directive, Fact, Thread
from story.store import Store


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("lh") / "t.db")
    sid = make_story(store)
    store.save_directive(sid, Directive(text="Kallen never speaks again after he dies", created_ep=60))
    store.save_directive(sid, Directive(text="No mention of the mother before ep 40", scope="until_ep:40", created_ep=3))
    for ep in range(1, 150):
        d = delta_for(ep)
        extra_facts, extra_chars, updates, threads = [], [], [], []
        if ep == 5:
            threads.append(Thread(id="who-sends-orders", title="Who sends the orders?", due_by_ep=120))
        if ep == 7:
            extra_chars.append(Character(id="kallen", name="Kallen", role="missing courier"))
            extra_facts.append(Fact(id="watch", text="Kallen's pocket watch stopped at 3:14 am", entities=["kallen", "watch"]))
        if ep == 20:
            extra_facts.append(Fact(id="kflat", text="Kallen lived in flat 3F", entities=["kallen", "flat-3f"]))
        if ep == 33:
            # Relevant by text, not by entity tag: retrieval must still find it when the beat is about the roof tank.
            extra_facts.append(Fact(id="tank", text="The roof water tank of Tower B is where the drowned tenants were found",
                                    entities=["tower-b"]))
        if ep == 40:
            extra_facts.append(Fact(id="mother", text="Leo's mother died in the Ashwood fire of 1998", entities=["ravi", "ashwood"]))
        if ep == 60:
            updates.append(CharacterUpdate(id="kallen", changes={"status": "dead", "location": "depot basement"}))
        if ep == 90:
            extra_facts.append(Fact(text="Kallen had secretly moved to flat 4B a week before he vanished",
                                    entities=["kallen", "flat-4b"], supersedes="kflat"))
        d = d.model_copy(update={
            "new_facts": d.new_facts + extra_facts, "new_characters": d.new_characters + extra_chars,
            "character_updates": d.character_updates + updates, "threads_opened": d.threads_opened + threads,
            "threads_advanced": [t for t in d.threads_advanced]})
        store.commit_episode(sid, ep, episode_text(ep), d)
        if ep % 10 == 0:
            store.save_arc_summary(sid, ep // 10, " ".join(f"Arc {ep // 10} sentence {i}." for i in range(40)))
    plan = store.latest_plan(sid)
    beats = [b if b.ep != 150 else b.model_copy(update={
        "beat": "Ravi finds Kallen's pocket watch floating in the roof water tank of Tower B.",
        "characters": ["ravi", "kallen"], "threads": ["who-sends-orders"]}) for b in plan.beats]
    store.save_plan(sid, plan.model_copy(update={"beats": beats}), "test: ep 150 beat")
    yield store, sid, build_context(store, sid, 150)
    store.close()


def facts(ctx):
    return [f.text for f in ctx.selected_facts]


def test_stays_within_budget(world):
    _, _, ctx = world
    assert ctx.total_tokens <= CONTEXT_BUDGET_TOKENS


def test_entity_fact_from_ep_7_is_retrieved_at_ep_150(world):
    assert "Kallen's pocket watch stopped at 3:14 am" in facts(world[2])


def test_text_relevant_fact_without_entity_overlap_is_retrieved(world):
    # The protagonist appears in hundreds of filler facts; they must not crowd out a rare, on-topic fact.
    assert "The roof water tank of Tower B is where the drowned tenants were found" in facts(world[2])


def test_superseded_fact_is_replaced(world):
    f = facts(world[2])
    assert "Kallen had secretly moved to flat 4B a week before he vanished" in f
    assert "Kallen lived in flat 3F" not in f


def test_dead_character_is_marked_dead_in_scene(world):
    ctx = world[2]
    scene = {c.id: c for c in ctx.scene_characters}
    assert scene["kallen"].status == "dead"
    assert "Kallen [kallen] (missing courier) STATUS: DEAD" in ctx.text()


def test_scoped_directives(world):
    text = world[2].sections[0][1]
    assert "Kallen never speaks again" in text
    assert "mother before ep 40" not in text  # expired at ep 40


def test_old_thread_for_this_beat_is_shown(world):
    assert "[who-sends-orders]" in world[2].text()
