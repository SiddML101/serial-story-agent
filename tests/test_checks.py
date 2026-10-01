import pytest

from helpers import delta_for, episode_text, fill_history, make_story
from story.checks import CriticReport, Issue, evaluate, rule_checks
from story.context import build_context
from story.models import CharacterUpdate
from story.store import Store


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "t.db")
    sid = make_story(store)
    fill_history(store, sid, 12)
    yield build_context(store, sid, 13), store, sid
    store.close()


def by_name(checks):
    return {c.name: c for c in checks}


def good_delta(ctx):
    d = delta_for(13)
    fresh = next(h for h in ("threat", "arrival", "decision", "question", "reversal") if h not in ctx.recent_hook_types)
    return d.model_copy(update={"hook_type": fresh})


def test_clean_episode_passes_rules(env):
    c, _, _ = env
    checks = by_name(rule_checks(episode_text(13, seed=999), good_delta(c), c))
    assert all(x.ok for x in checks.values() if x.level == "fail"), {k: v.detail for k, v in checks.items() if not v.ok}


def test_word_count_limits(env):
    c, _, _ = env
    assert not by_name(rule_checks(episode_text(13, words=300), good_delta(c), c))["word_count"].ok
    assert not by_name(rule_checks(episode_text(13, words=800), good_delta(c), c))["word_count"].ok


def test_banned_phrases_including_regex_and_bible(env):
    c, _, _ = env
    text = episode_text(13, seed=5) + " Little did she know. It was bone-chilling. The TAPESTRY of fate."
    detail = by_name(rule_checks(text, good_delta(c), c))["banned_phrases"].detail.lower()
    assert "little did she know" in detail and "bone-chilling" in detail and "tapestry" in detail


def test_hook_type_repeat_fails(env):
    c, _, _ = env
    d = good_delta(c).model_copy(update={"hook_type": c.recent_hook_types[-1]})
    assert not by_name(rule_checks(episode_text(13, seed=7), d, c))["hook"].ok


def test_summary_repetition_fails(env):
    c, _, _ = env
    d = good_delta(c).model_copy(update={"summary": c.previous[-2].delta.summary})
    check = by_name(rule_checks(episode_text(13, seed=8), d, c))["summary_repetition"]
    assert not check.ok and "ep 11" in check.detail


def test_phrase_repetition_fails_when_copying_previous_text(env):
    c, _, _ = env
    check = by_name(rule_checks(c.previous[-1].text, good_delta(c), c))["phrase_repetition"]
    assert not check.ok


def test_dead_character_named_is_flagged_for_critic(env):
    _, store, sid = env
    store.add_state_edit(sid, 12, CharacterUpdate(id="anil", changes={"status": "dead"}), source="test")
    c2 = build_context(store, sid, 13)
    check = by_name(rule_checks(episode_text(13, seed=9) + " Anil waved.", good_delta(c2), c2))["dead_or_missing_named"]
    assert not check.ok and check.level == "warn" and "Anil" in check.detail


def test_evaluate_fails_on_high_issue_or_weak_hook(env):
    c, _, _ = env
    rules = rule_checks(episode_text(13, seed=999), good_delta(c), c)
    ok_report = CriticReport(issues=[Issue(type="voice", severity="low")], hook_score=4, momentum_score=4)
    assert evaluate(rules, ok_report)[0]
    high = CriticReport(issues=[Issue(type="contradiction", severity="high", evidence="x", fix="y")], hook_score=4,
                        momentum_score=4)
    assert not evaluate(rules, high)[0]
    assert not evaluate(rules, CriticReport(hook_score=2, momentum_score=4))[0]


def test_issue_normalizes_model_variants():
    i = Issue.model_validate({"type": "Directive", "severity": "Medium"})
    assert (i.type, i.severity) == ("directive_violation", "med")


def test_critic_evidence_must_appear_in_draft():
    from story.checks import evidence_in_draft, verify_critic
    draft = "Leo leans as far as his frame leash permits. “Don’t touch it,” Mira snaps."
    assert evidence_in_draft("Leo leans as far as his frame leash permits", draft)
    assert evidence_in_draft("\"Don't touch it,\" Mira snaps", draft)  # curly vs straight quotes
    assert evidence_in_draft("Leo leans as far ... Mira snaps", draft)  # elision
    report = CriticReport(issues=[
        Issue(type="directive_violation", severity="high",
              evidence="The link between the Ashwood Monolith and Leo's mother is strictly reserved", fix="x"),
        Issue(type="contradiction", severity="high", evidence="as far as his frame leash permits", fix="y")],
        hook_score=4, momentum_score=4)
    notes = verify_critic(report, draft)
    assert len(notes) == 1
    assert [i.severity for i in report.issues] == ["low", "high"]  # hallucinated quote downgraded, real one kept
