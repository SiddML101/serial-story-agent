import pytest

from helpers import fill_history, make_story
from story.context import CONTEXT_BUDGET_TOKENS, active_directives, build_context
from story.models import Directive
from story.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def test_context_at_ep_150_stays_within_budget(store):
    sid = make_story(store)
    fill_history(store, sid, 149)
    ctx = build_context(store, sid, 150)
    assert ctx.total_tokens <= CONTEXT_BUDGET_TOKENS
    names = [n for n, _ in ctx.sections]
    for required in ("EDITOR DIRECTIVES (must follow; they override the plan)", "THIS EPISODE'S BEAT",
                     "PREVIOUS EPISODES (full text, for voice and continuity)", "OPEN THREADS (touch at least one)",
                     "STORY SO FAR (arc summaries)", "ESTABLISHED FACTS (never contradict)"):
        assert required in names
    assert "Episode 149" in ctx.text()  # most recent episode always in full
    assert "Arc 14:" in ctx.text() and "Arc 1:" in ctx.text()  # all arc summaries, older ones shortened


def test_context_size_is_bounded_regardless_of_episode(store):
    sid = make_story(store)
    fill_history(store, sid, 149)
    sizes = {ep: build_context(store, sid, ep).total_tokens for ep in (5, 15, 60, 150)}
    assert max(sizes.values()) <= CONTEXT_BUDGET_TOKENS
    assert sizes[150] < 2.5 * sizes[15]  # grows with history at first, then is capped


def test_trim_order_drops_old_summaries_first(store):
    sid = make_story(store)
    fill_history(store, sid, 149)
    tight = build_context(store, sid, 150, budget_tokens=5000)
    assert tight.knobs.ep_summaries == 3
    assert tight.knobs.facts < 30  # facts trimmed only after summaries hit their minimum
    assert tight.total_tokens <= 5000


def test_scene_characters_and_facts_are_relevant(store):
    sid = make_story(store)
    fill_history(store, sid, 20)
    ctx = build_context(store, sid, 21)
    assert {c.id for c in ctx.scene_characters} >= {"ravi", "meera"}
    top = ctx.selected_facts[:5]
    assert all({"ravi", "meera"} & set(f.entities) for f in top)


def test_directive_scopes():
    d = [Directive(text="a"), Directive(text="b", scope="until_ep:10"), Directive(text="c", active=False),
         Directive(text="d", scope="character:ravi")]
    assert [x.text for x in active_directives(d, 5)] == ["a", "b", "d"]
    assert [x.text for x in active_directives(d, 11)] == ["a", "d"]


def test_directives_appear_in_context(store):
    sid = make_story(store)
    store.save_directive(sid, Directive(text="Romance moves slowly", created_ep=1))
    ctx = build_context(store, sid, 1)
    assert "Romance moves slowly" in ctx.sections[0][1]


def test_overused_words_flags_tics_not_plot_vocabulary():
    from story.context import overused_words
    from story.models import Episode
    eps = [Episode(ep=i, version=1, status="approved", word_count=0,
                   text=f"The terminal hummed. Greasy rain, heavy and greasy, on the courier's hands. Event{i} happened.")
           for i in range(1, 11)]
    flagged = overused_words(eps, exclude={"terminal", "courier", "courier's"})
    assert {"greasy", "heavy", "hummed"} <= set(flagged)
    assert not {"terminal", "courier", "the", "hands"} & set(flagged)  # plot words, stopwords, narrative glue
    assert overused_words(eps[:3], exclude=set()) == []  # needs a few episodes of history first
