"""Local web UI: FastAPI over the same service layer as the CLI. Run with `python -m story ui`.

Binds to 127.0.0.1 only; the API key never leaves the machine. Long LLM steps run as background jobs (one at a time,
to respect free-tier rate limits and SQLite) and report progress the page polls for.
"""
from __future__ import annotations

import threading
import traceback
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import config, export, feedback, pipeline, report, retcon, service
from .config import TOTAL_EPISODES
from .llm import check_api_key
from .context import build_context
from .llm import QuotaExhausted
from .models import utcnow
from .planner import plan_from_yaml, plan_to_yaml

PAGE = Path(__file__).parent / "web" / "index.html"
app = FastAPI(title="Serial Story Agent", docs_url="/api/docs")
LOCAL_HOSTS = {"127.0.0.1", "localhost"}


@app.middleware("http")
async def local_origin_only(request: Request, call_next):
    """Requests that change anything must come from this UI, not from another site open in the same browser."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin and urlparse(origin).hostname not in LOCAL_HOSTS:
            return JSONResponse({"detail": "Cross-site request refused."}, status_code=403)
    return await call_next(request)


# --- sessions & jobs ------------------------------------------------------------

@contextmanager
def session(story_id: str) -> Iterator[service.Session]:
    """One SQLite connection per request/job thread (sqlite3 connections are not shared across threads)."""
    try:
        s = service.open_session(story_id)
    except service.ServiceError as e:
        raise HTTPException(404, str(e))
    try:
        yield s
    finally:
        s.store.close()


def to_json(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump()
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, (list, tuple)):
        return [to_json(x) for x in obj]
    if isinstance(obj, dict):
        return {k: to_json(v) for k, v in obj.items()}
    return obj


class Jobs:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.jobs: dict[str, dict] = {}
        self.running: str | None = None
        self.seq = 0

    def start(self, kind: str, story_id: str | None, fn: Callable[[Callable[[str], None]], Any]) -> dict:
        with self.lock:
            if self.running and self.jobs[self.running]["status"] == "running":
                busy = self.jobs[self.running]
                raise HTTPException(409, f"Busy: '{busy['kind']}' is still running ({busy['progress'][-1:] or ['...']}).")
            self.seq += 1
            job_id = f"job{self.seq}"
            job = {"id": job_id, "kind": kind, "story_id": story_id, "status": "running", "progress": [],
                   "result": None, "error": None, "started": utcnow(), "finished": None}
            self.jobs[job_id] = job
            self.running = job_id

        def progress(msg: str) -> None:
            job["progress"].append(msg)

        def run() -> None:
            try:
                job["result"] = to_json(fn(progress))
                job["status"] = "done"
            except HTTPException as e:
                job["status"], job["error"] = "error", e.detail
            except QuotaExhausted as e:
                job["status"], job["error"] = "error", f"Free-tier quota used up. {e}"
            except service.ServiceError as e:
                job["status"], job["error"] = "error", str(e)
            except Exception as e:  # API failures after retries, bad model output, ...
                job["status"], job["error"] = "error", f"{type(e).__name__}: {str(e)[:400]}"
                job["trace"] = traceback.format_exc()[-2000:]
            finally:
                job["finished"] = utcnow()

        threading.Thread(target=run, daemon=True).start()
        return job

    def get(self, job_id: str) -> dict:
        if job_id not in self.jobs:
            raise HTTPException(404, "Unknown job.")
        return self.jobs[job_id]

    def current(self) -> dict | None:
        return self.jobs.get(self.running) if self.running else None


JOBS = Jobs()
PENDING_BOUNDARY: dict[str, pipeline.BoundaryReport] = {}  # story_id -> boundary awaiting a human decision
PENDING_RETCON: dict[str, retcon.RetconReport] = {}


def job(kind: str, story_id: str | None, fn: Callable[[Callable[[str], None]], Any]) -> dict:
    return {"job": JOBS.start(kind, story_id, fn)["id"]}


def guard(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except service.ServiceError as e:
        raise HTTPException(400, str(e))


# --- request bodies ---------------------------------------------------------------

class PremiseIn(BaseModel):
    premise: str


class NoteIn(BaseModel):
    note: Optional[str] = None


class ApproveIn(BaseModel):
    text: Optional[str] = None


class RejectIn(BaseModel):
    reason: str


class FeedbackIn(BaseModel):
    text: str
    stage: Optional[str] = None


class ApplyFeedbackIn(BaseModel):
    text: str
    stage: str
    next_ep: int
    routed: dict
    regenerate: bool = False


class BeatIn(BaseModel):
    text: str
    characters: Optional[list[str]] = None


class YamlIn(BaseModel):
    yaml: str


class ActIn(BaseModel):
    act: int


class DirectiveIn(BaseModel):
    text: Optional[str] = None
    active: Optional[bool] = None


class DecisionIn(BaseModel):
    decision: str


class RetconIn(BaseModel):
    text: str


class ResolveIn(BaseModel):
    resolution: str  # accept | regenerate | patch | replan


class ExportIn(BaseModel):
    out: Optional[str] = None


class KeyIn(BaseModel):
    api_key: str


# --- page & jobs ------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def page() -> str:
    return PAGE.read_text(encoding="utf-8")


@app.get("/api/jobs/current")
def current_job() -> dict | None:
    return JOBS.current()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    return JOBS.get(job_id)


# --- settings (API key) -----------------------------------------------------------------

@app.get("/api/settings")
def get_settings() -> dict:
    """Never returns the key itself: only whether one is set and its last 4 characters."""
    st = service.load_settings()
    return {"key_set": bool(st.api_key), "key_hint": config.key_hint(st.api_key), "base_url": st.base_url,
            "writer_models": [m.strip() for m in st.writer_model.split(",") if m.strip()],
            "fast_models": [m.strip() for m in st.fast_model.split(",") if m.strip()],
            "env_file": str(config.ENV_FILE)}


@app.post("/api/settings/key")
def set_api_key(body: KeyIn) -> dict:
    key = body.api_key.strip()
    if not config.API_KEY_RE.match(key):
        raise HTTPException(400, "That doesn't look like an API key (letters, digits, '.', '_' or '-'; 20-200 characters).")
    st = service.load_settings()
    ok, message = check_api_key(st.base_url, key)
    if not ok:
        raise HTTPException(400, message + " Nothing was saved.")
    config.set_env_value("LLM_API_KEY", key)
    return {"ok": True, "message": message + " Saved to .env; used from the next step on.",
            "key_hint": config.key_hint(key)}


# --- stories ------------------------------------------------------------------------

@app.get("/api/stories")
def stories() -> list[dict]:
    s = service.load_settings()
    from .store import Store
    store = Store(s.db_path)
    try:
        return store.list_stories()
    finally:
        store.close()


@app.post("/api/stories")
def create_story(body: PremiseIn) -> dict:
    """The story row is created right away, so a planning failure leaves a resumable story, not nothing."""
    created = guard(lambda: service.new_session(body.premise))
    sid = created.story_id
    created.store.close()

    def run(progress):
        with session(sid) as s:
            progress("Designing bible, cast, acts and arcs...")
            service.generate_plan(s, progress)
            return {"story_id": sid}
    return {**job("new_story", sid, run), "story_id": sid}


@app.get("/api/stories/{sid}/status")
def status(sid: str) -> dict:
    with session(sid) as s:
        st, store = s.story_id, s.store
        plan, approved = store.latest_plan(st), store.latest_plan(st, approved_only=True)
        last = store.last_approved_ep(st)
        bible = store.latest_bible(st)
        draft = store.get_draft(st, last + 1)
        summ = report.summarize(report.load_log(s.llm.logger.path))
        return {
            "story": store.get_story(st), "title": bible.title if bible else None,
            "logline": bible.logline if bible else None, "total_episodes": TOTAL_EPISODES,
            "plan_version": plan.version if plan else None, "plan_beats": len(plan.beats) if plan else 0,
            "approved_plan_version": approved.version if approved else None,
            "last_approved_ep": last, "next_ep": last + 1,
            "next_beat": plan.beat(last + 1).model_dump() if plan and plan.beat(last + 1) else None,
            "pending_draft": {"version": draft.version, "words": draft.word_count,
                              "needs_human": (draft.checks or {}).get("needs_human")} if draft else None,
            "pending_boundary": to_json(PENDING_BOUNDARY.get(st)),
            "pending_retcon": to_json(PENDING_RETCON.get(st)),
            "spend_list_usd": round(sum(b["list_price_usd"] for b in summ["by_step"].values()), 4),
            "calls": sum(b["calls"] for b in summ["by_step"].values()),
            "quota": {m: not s.llm.quota.exhausted(m) for m in s.llm.chain("writer") + s.llm.chain("fast")},
            "out_dir": str(s.out_dir),
        }


@app.get("/api/stories/{sid}/memory")
def memory(sid: str) -> dict:
    with session(sid) as s:
        last = s.store.last_approved_ep(s.story_id)
        state = s.store.state_at(s.story_id, last)
        return {
            "characters": [c.model_dump() for c in state.characters.values()],
            "threads": [t.model_dump() for t in state.threads.values()],
            "facts": [f.model_dump() for f in state.active_facts()[-80:]][::-1],
            "fact_count": len(state.active_facts()),
            "timeline": [t.model_dump() for t in state.timeline[-10:]],
            "directives": [d.model_dump() for d in s.store.directives(s.story_id, active_only=False)],
            "arc_summaries": s.store.arc_summaries(s.story_id),
        }


@app.get("/api/stories/{sid}/events")
def events(sid: str) -> list[dict]:
    with session(sid) as s:
        return s.store.events(s.story_id)[::-1]


@app.get("/api/stories/{sid}/report")
def cost_report(sid: str) -> dict:
    with session(sid) as s:
        summ = report.summarize(report.load_log(s.llm.logger.path))
        return {"summary": summ, "projection": report.project(summ),
                "writer_models": s.llm.chain("writer"), "free_requests_per_day": report.FREE_WRITER_REQUESTS_PER_DAY}


@app.get("/api/stories/{sid}/context/{ep}")
def context(sid: str, ep: int) -> dict:
    if not 1 <= ep <= TOTAL_EPISODES:
        raise HTTPException(400, f"Episodes are 1-{TOTAL_EPISODES}.")
    with session(sid) as s:
        c = build_context(s.store, s.story_id, ep)
        return {"total_tokens": c.total_tokens, "sections": [{"name": n, "tokens": t, "text": b}
                                                              for (n, b), t in zip(c.sections, c.tokens().values())]}


@app.post("/api/stories/{sid}/export")
def do_export(sid: str, body: ExportIn) -> dict:
    with session(sid) as s:
        target = Path(body.out) if body.out else s.out_dir
        paths = export.export_all(s.store, s.story_id, s.llm.logger.path, len(s.llm.chain("writer")),
                                  demo_dir=target, db_path=s.settings.db_path if body.out else None)
        return {"files": len(paths), "folder": str(target)}


# --- plan ---------------------------------------------------------------------------

@app.get("/api/stories/{sid}/plan")
def get_plan(sid: str) -> dict:
    with session(sid) as s:
        plan = s.store.latest_plan(s.story_id)
        if plan is None:
            return {"plan": None, "history": [], "validation": None}
        v = service.validate(s, plan)
        return {"plan": plan.model_dump(), "history": s.store.plan_history(s.story_id),
                "validation": {"errors": v.errors, "warnings": v.warnings},
                "cast": [c.model_dump() for c in s.store.state_at(s.story_id, 0).characters.values()],
                "bible": s.store.latest_bible(s.story_id).model_dump() if s.store.latest_bible(s.story_id) else None,
                "last_approved_ep": s.store.last_approved_ep(s.story_id)}


@app.get("/api/stories/{sid}/plan/diff")
def plan_diff(sid: str, v1: int, v2: int) -> list[dict]:
    with session(sid) as s:
        return guard(lambda: s.store.plan_diff(s.story_id, v1, v2))


@app.get("/api/stories/{sid}/plan/yaml")
def plan_yaml(sid: str) -> dict:
    with session(sid) as s:
        return {"yaml": plan_to_yaml(s.store.latest_plan(s.story_id))}


@app.put("/api/stories/{sid}/plan/yaml")
def save_plan_yaml(sid: str, body: YamlIn) -> dict:
    with session(sid) as s:
        try:
            new_plan = plan_from_yaml(body.yaml)
        except Exception as e:
            raise HTTPException(400, f"Invalid YAML/plan: {str(e)[:600]}")
        version, changes = guard(lambda: service.save_human_plan(s, new_plan, "human edit (YAML, web)"))
        return {"version": version, "changes": changes}


@app.put("/api/stories/{sid}/plan/beats/{ep}")
def save_beat(sid: str, ep: int, body: BeatIn) -> dict:
    with session(sid) as s:
        version, changes = guard(lambda: service.edit_beat(s, ep, body.text, body.characters))
        return {"version": version, "changes": changes}


@app.post("/api/stories/{sid}/plan/approve")
def approve_plan(sid: str) -> dict:
    with session(sid) as s:
        return {"version": guard(lambda: service.approve_plan(s))}


@app.post("/api/stories/{sid}/plan/generate")
def resume_plan(sid: str) -> dict:
    def run(progress):
        with session(sid) as s:
            return {"version": service.generate_plan(s, progress).version}
    return job("plan_generate", sid, run)


@app.post("/api/stories/{sid}/plan/regen")
def regen_act(sid: str, body: ActIn) -> dict:
    def run(progress):
        with session(sid) as s:
            version, changes = service.regenerate_act(s, body.act, progress)
            return {"version": version, "changes": changes}
    return job("plan_regen", sid, run)


# --- episodes -------------------------------------------------------------------------

def episode_view(e) -> dict | None:
    if e is None:
        return None
    d = e.model_dump()
    d["word_count"] = e.word_count
    return d


@app.get("/api/stories/{sid}/episodes")
def episodes(sid: str) -> list[dict]:
    with session(sid) as s:
        out = []
        for e in s.store.approved_episodes(s.story_id):
            crit = (e.checks or {}).get("critic") or {}
            out.append({"ep": e.ep, "version": e.version, "words": e.word_count, "human_edited": e.human_edited,
                        "summary": e.delta.summary if e.delta else "", "hook": e.delta.hook if e.delta else "",
                        "hook_type": e.delta.hook_type if e.delta else "", "hook_score": crit.get("hook_score"),
                        "momentum_score": crit.get("momentum_score"), "revisions": (e.checks or {}).get("revisions", 0),
                        "cost_usd": e.cost_usd})
        return out


@app.get("/api/stories/{sid}/episodes/{ep}")
def episode(sid: str, ep: int) -> dict:
    with session(sid) as s:
        plan = s.store.latest_plan(s.story_id)
        beat = plan.beat(ep) if plan else None
        return {"ep": ep, "approved": episode_view(s.store.approved_episode(s.story_id, ep)),
                "draft": episode_view(s.store.get_draft(s.story_id, ep)),
                "beat": beat.model_dump() if beat else None,
                "is_next": ep == s.store.last_approved_ep(s.story_id) + 1}


@app.post("/api/stories/{sid}/draft")
def make_draft(sid: str, body: NoteIn) -> dict:
    def run(progress):
        with session(sid) as s:
            return episode_view(service.produce(s, note=body.note, progress=progress))
    return job("draft", sid, run)


@app.post("/api/stories/{sid}/episodes/{ep}/approve")
def approve_episode(sid: str, ep: int, body: ApproveIn) -> dict:
    def run(progress):
        with session(sid) as s:
            draft = s.store.get_draft(s.story_id, ep)
            if draft is None:
                raise service.ServiceError(f"No pending draft for episode {ep}.")
            if body.text is not None and body.text.strip() != draft.text.strip():
                draft = service.save_human_edit(s, draft, body.text)  # saved before any LLM call
            episode, boundary = service.approve(s, draft, progress=progress)
            if boundary:
                PENDING_BOUNDARY[s.story_id] = boundary
            return {"episode": episode_view(episode), "boundary": boundary}
    return job("approve", sid, run)


@app.post("/api/stories/{sid}/episodes/{ep}/reject")
def reject_episode(sid: str, ep: int, body: RejectIn) -> dict:
    def run(progress):
        with session(sid) as s:
            draft = s.store.get_draft(s.story_id, ep)
            if draft is None:
                raise service.ServiceError(f"No pending draft for episode {ep}.")
            return episode_view(service.reject(s, draft, body.reason, progress))
    return job("reject", sid, run)


@app.post("/api/stories/{sid}/boundary/decision")
def boundary_decision(sid: str, body: DecisionIn) -> dict:
    if body.decision not in ("accepted", "reverted"):
        raise HTTPException(400, "decision must be accepted or reverted")
    rep = PENDING_BOUNDARY.pop(sid, None)
    if rep is None:
        raise HTTPException(404, "No arc boundary is waiting for a decision.")
    with session(sid) as s:
        service.record_boundary(s, rep, body.decision if rep.replan_to else "none")
    return {"ok": True}


@app.post("/api/stories/{sid}/boundary/{arc}")
def rerun_boundary(sid: str, arc: int, only: Optional[str] = None) -> dict:
    def run(progress):
        with session(sid) as s:
            rep = pipeline.arc_boundary(s.llm, s.store, s.story_id, arc, on_progress=progress,
                                        only=set(only.split(",")) if only else None)
            PENDING_BOUNDARY[s.story_id] = rep
            return rep
    return job("boundary", sid, run)


# --- feedback & directives -------------------------------------------------------------

@app.post("/api/stories/{sid}/feedback/route")
def route_feedback(sid: str, body: FeedbackIn) -> dict:
    def run(progress):
        with session(sid) as s:
            next_ep = service.next_episode(s)
            stage = body.stage or ("plan" if next_ep == 1 and not s.store.latest_plan(s.story_id, approved_only=True)
                                   else "episode")
            progress("Routing feedback...")
            routed = feedback.route(s.llm, s.store, s.story_id, body.text, stage, next_ep)
            plan = s.store.latest_plan(s.story_id)
            old = {e.ep: {"beat": plan.beat(e.ep).beat if plan.beat(e.ep) else "",
                          "human_owned": bool(plan.beat(e.ep) and plan.beat(e.ep).human_owned)} for e in routed.plan_edits}
            return {"routed": routed, "old_beats": old, "stage": stage, "next_ep": next_ep}
    return job("feedback_route", sid, run)


@app.post("/api/stories/{sid}/feedback/apply")
def apply_feedback(sid: str, body: ApplyFeedbackIn) -> dict:
    with session(sid) as s:
        routed = feedback.RoutedFeedback.model_validate(body.routed)
        applied = feedback.apply(s.llm, s.store, s.story_id, body.text, routed, body.stage, body.next_ep)
        s.autosave(plan=True, hitl=True)
        out = {"applied": to_json(applied)}
    if body.regenerate:
        with session(sid) as s:
            has_draft = s.store.get_draft(s.story_id, body.next_ep) is not None
        if has_draft:
            out.update(reject_episode(sid, body.next_ep, RejectIn(reason=f"editor feedback: {body.text}")))
    return out


@app.put("/api/stories/{sid}/directives/{directive_id}")
def update_directive(sid: str, directive_id: str, body: DirectiveIn) -> dict:
    with session(sid) as s:
        return guard(lambda: service.update_directive(s, directive_id, text=body.text, active=body.active)).model_dump()


# --- retcon -------------------------------------------------------------------------------

@app.post("/api/stories/{sid}/episodes/{ep}/retcon")
def start_retcon(sid: str, ep: int, body: RetconIn) -> dict:
    def run(progress):
        with session(sid) as s:
            old = s.store.approved_episode(s.story_id, ep)
            if old is None:
                raise service.ServiceError(f"Episode {ep} is not approved.")
            if body.text.strip() == old.text.strip():
                raise service.ServiceError("No changes.")
            progress(f"Re-extracting episode {ep} and checking later episodes...")
            rep = retcon.retcon(s.llm, s.store, s.story_id, ep, body.text.strip())
            s.autosave(episodes=True, hitl=True)
            if rep.conflicts:
                PENDING_RETCON[s.story_id] = rep
            else:
                retcon.resolve(s.store, s.story_id, rep, "accepted (no conflicts)")
            return rep
    return job("retcon", sid, run)


@app.post("/api/stories/{sid}/retcon/resolve")
def resolve_retcon(sid: str, body: ResolveIn) -> dict:
    rep = PENDING_RETCON.get(sid)
    if rep is None:
        raise HTTPException(404, "No retcon is waiting for a decision.")
    if body.resolution not in ("accept", "regenerate", "patch", "replan"):
        raise HTTPException(400, "resolution must be accept, regenerate, patch or replan")

    def run(progress):
        with session(sid) as s:
            PENDING_RETCON.pop(sid, None)
            if body.resolution == "accept":
                retcon.resolve(s.store, s.story_id, rep, "accepted as-is")
                result = {"resolution": "accepted as-is"}
            elif body.resolution == "regenerate":
                dropped = s.store.rollback_to(s.story_id, rep.first_conflict - 1)
                retcon.resolve(s.store, s.story_id, rep, f"regenerate from ep {rep.first_conflict}", {"superseded": dropped})
                result = {"resolution": "regenerate", "superseded": dropped}
            elif body.resolution == "patch":
                patched = {}
                for c in sorted(rep.conflicts, key=lambda c: c["ep"]):
                    progress(f"Patching episode {c['ep']}...")
                    patched[c["ep"]] = retcon.patch_episode(s.llm, s.store, s.story_id, c["ep"], [
                        f"Conflicts with the rewritten episode {rep.ep}: \"{c['evidence']}\". Fix: {c['fix']}"])
                retcon.resolve(s.store, s.story_id, rep, "patched flagged episodes", {"patched_versions": patched})
                result = {"resolution": "patched", "patched": patched}
            else:
                last = s.store.last_approved_ep(s.story_id)
                res = None
                if last < TOTAL_EPISODES:
                    from .planner import arc_no_for
                    progress("Replanning the remaining beats of this arc...")
                    res = pipeline.replan_next_arc(s.llm, s.store, s.story_id, arc_no_for(last + 1) - 1, keep_upto=last)
                retcon.resolve(s.store, s.story_id, rep, "replanned remaining beats", {"plan_to": res[1] if res else None})
                result = {"resolution": "replanned", "plan": res[:2] if res else None}
            s.autosave(plan=True, episodes=True, hitl=True)
            return result
    return job("retcon_resolve", sid, run)


def serve(port: int = 8765, open_browser: bool = True) -> None:
    import webbrowser

    import uvicorn

    url = f"http://127.0.0.1:{port}/"
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"Serial Story Agent UI on {url}  (Ctrl+C to stop)")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
