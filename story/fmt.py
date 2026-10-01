"""Compact plain-text renderings of story objects, shared by prompts and the CLI."""
from __future__ import annotations

from .models import Act, Arc, Beat, Bible, Character, Directive, Fact, Thread


def bullets(items: list[str], empty: str = "(none)") -> str:
    return "\n".join(f"- {x}" for x in items) if items else empty


def bible(b: Bible) -> str:
    return (
        f"Title: {b.title}\nLogline: {b.logline}\nGenre: {b.genre} | Tone: {b.tone}\n"
        f"POV: {b.pov} | Tense: {b.tense}\nSetting: {b.setting}\n"
        f"World rules:\n{bullets(b.world_rules)}\nStyle guide:\n{bullets(b.style_guide)}"
    )


def character(c: Character, full: bool = True) -> str:
    status = c.status.upper() if c.status != "alive" else "alive"
    if not full:
        return f"- {c.name} [{c.id}] {c.role}; {status}; at {c.location or '?'}; last seen ep {c.last_seen_ep}"
    rel = "; ".join(f"{k}: {v}" for k, v in c.relationships.items()) or "none"
    knows = "; ".join(c.knows[-8:]) or "nothing notable yet"
    return (
        f"- {c.name} [{c.id}] ({c.role}) STATUS: {status} | location: {c.location or '?'}\n"
        f"  {c.description}\n  goal: {c.goal} | secret: {c.secret}\n  voice: {c.voice}\n"
        f"  knows: {knows}\n  relationships: {rel}\n  first ep {c.first_ep}, last seen ep {c.last_seen_ep}"
    )


def acts(items: list[Act]) -> str:
    return "\n".join(
        f"Act {a.act_no} (eps {a.ep_start}-{a.ep_end}) \"{a.title}\": {a.purpose} Turning point: {a.turning_point}"
        for a in items
    )


def arc(a: Arc) -> str:
    return (
        f"Arc {a.arc_no} (act {a.act_no}, eps {a.ep_start}-{a.ep_end}) \"{a.title}\"\n"
        f"  goal: {a.goal}\n  turning point: {a.turning_point}\n  focus: {', '.join(a.character_focus) or '-'}\n"
        f"  opens: {', '.join(a.threads_opened) or '-'} | resolves: {', '.join(a.threads_resolved) or '-'}"
    )


def beat(b: Beat) -> str:
    extras = []
    if b.characters:
        extras.append("chars: " + ", ".join(b.characters))
    if b.threads:
        extras.append("threads: " + ", ".join(b.threads))
    if b.hook_type_hint:
        extras.append(f"hook: {b.hook_type_hint}")
    owned = " (HUMAN-WRITTEN: keep its specifics)" if b.human_owned else ""
    return f"Ep {b.ep}: {b.beat}{owned}" + (f"  [{'; '.join(extras)}]" if extras else "")


def beats(items: list[Beat]) -> str:
    return "\n".join(beat(b) for b in items) or "(none)"


def directive(d: Directive) -> str:
    scope = "" if d.scope == "global" else f" [{d.scope}]"
    return f"- {d.text}{scope} (since ep {d.created_ep})"


def fact(f: Fact) -> str:
    return f"- (ep {f.ep}) {f.text} [id {f.id}]"


def thread(t: Thread, ep: int) -> str:
    flags = []
    if t.due_by_ep is not None and t.due_by_ep < ep:
        flags.append("OVERDUE")
    if ep - t.last_touched_ep > 15:
        flags.append("STALE")
    due = f", due by ep {t.due_by_ep}" if t.due_by_ep else ""
    flag = f" **{' '.join(flags)}**" if flags else ""
    return f"- [{t.id}] {t.title}: {t.description} (opened ep {t.opened_ep}, last touched ep {t.last_touched_ep}{due}){flag}"
