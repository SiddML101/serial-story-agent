"""Cost / latency report from the JSONL log, with a projection to all 200 episodes."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from .config import TOTAL_EPISODES

EPISODE_STEPS = {"draft", "extract", "critic", "revise"}
BOUNDARY_STEPS = {"arc_summary", "arc_audit", "replan_arc"}
N_ARCS = 20
FREE_WRITER_REQUESTS_PER_DAY = 20  # per model, observed on gemini-3.8-flash


def load_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                records.append(json.loads(line))
            except ValueError:  # e.g. a line cut off when the process was killed mid-write
                continue
    return records


def _bucket() -> dict:
    return {"calls": 0, "failed_attempts": 0, "tokens_in": 0, "tokens_out": 0, "list_price_usd": 0.0,
            "latency_ms": 0, "wait_ms": 0}


def summarize(records: list[dict]) -> dict:
    by_step: dict[str, dict] = defaultdict(_bucket)
    by_ep: dict[int, dict] = defaultdict(_bucket)
    by_model: dict[str, dict] = defaultdict(_bucket)
    decisions: dict[str, int] = defaultdict(int)
    for r in records:
        step = r.get("step")
        if step == "decision":
            key = (r.get("decision") or "").split(":")[0].split("→")[0]
            decisions[key] += 1
            continue
        b = by_step[step]
        status = r.get("status")
        if status in ("retry", "error", "quota_exhausted", "parse_error"):
            b["failed_attempts"] += 1
            by_model[r.get("model") or "?"]["failed_attempts"] += 1
            if r.get("ep") is not None and step in EPISODE_STEPS:
                by_ep[r["ep"]]["failed_attempts"] += 1
            wait = r.get("decision") or ""
            if "retry_in_" in wait:
                b["wait_ms"] += int(float(wait.rsplit("retry_in_", 1)[1].rstrip("s")) * 1000)
        if status in ("ok", "parse_error"):
            targets = [b, by_model[r.get("model") or "?"]]
            if r.get("ep") is not None and step in EPISODE_STEPS:
                targets.append(by_ep[r["ep"]])
            for t in targets:
                t["calls"] += status == "ok"
                t["tokens_in"] += r.get("tokens_in") or 0
                t["tokens_out"] += r.get("tokens_out") or 0
                t["list_price_usd"] += r.get("list_price_usd") or 0.0
                t["latency_ms"] += r.get("latency_ms") or 0
    return {"by_step": dict(by_step), "by_ep": dict(sorted(by_ep.items())), "by_model": dict(by_model),
            "decisions": dict(decisions)}


def project(summary: dict, n: int = TOTAL_EPISODES) -> dict:
    eps = summary["by_ep"]
    steps = summary["by_step"]
    k = max(1, len(eps))
    per_ep_cost = sum(e["list_price_usd"] for e in eps.values()) / k
    per_ep_ms = sum(e["latency_ms"] for e in eps.values()) / k
    per_ep_tokens = sum(e["tokens_in"] + e["tokens_out"] for e in eps.values()) / k
    planning = [v for s, v in steps.items() if s and s.startswith("plan_")]
    plan_cost = sum(v["list_price_usd"] for v in planning)
    plan_ms = sum(v["latency_ms"] for v in planning)
    boundary = [v for s, v in steps.items() if s in BOUNDARY_STEPS]
    arcs_done = max(1, steps.get("arc_summary", {}).get("calls", 0))
    boundary_cost = sum(v["list_price_usd"] for v in boundary) / arcs_done if boundary else 0.0
    boundary_ms = sum(v["latency_ms"] for v in boundary) / arcs_done if boundary else 0.0
    writer_calls = sum(steps.get(s, {}).get("calls", 0) for s in ("draft", "revise")) / k
    total_cost = per_ep_cost * n + plan_cost + boundary_cost * N_ARCS
    total_hours = (per_ep_ms * n + plan_ms + boundary_ms * N_ARCS) / 3_600_000
    return {"episodes_measured": len(eps), "per_episode_list_usd": per_ep_cost, "per_episode_seconds": per_ep_ms / 1000,
            "per_episode_tokens": per_ep_tokens, "writer_calls_per_episode": writer_calls,
            "planning_list_usd": plan_cost, "boundary_list_usd": boundary_cost,
            "projected_list_usd": total_cost, "projected_hours_model_time": total_hours,
            "projected_writer_requests": writer_calls * n + N_ARCS + 6}


def cost_report_md(summary: dict, proj: dict, n_writer_models: int) -> str:
    lines = ["# Cost & latency report", "",
             "All calls ran on the **free Gemini tier**: actual spend is **$0**. Prices below are list prices "
             "(what the same tokens would cost on a paid tier, from `story/config.py` PRICING), so the estimate is "
             "realistic.", "", "## By step", "",
             "| step | calls | failed attempts | tokens in | tokens out | list $ | avg latency s |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for step, b in sorted(summary["by_step"].items(), key=lambda kv: -kv[1]["list_price_usd"]):
        avg = b["latency_ms"] / max(1, b["calls"]) / 1000
        lines.append(f"| {step} | {b['calls']} | {b['failed_attempts']} | {b['tokens_in']:,} | {b['tokens_out']:,} "
                     f"| {b['list_price_usd']:.4f} | {avg:.1f} |")
    lines += ["", "## By model", "", "| model | calls | failed attempts | tokens out | list $ |", "|---|---:|---:|---:|---:|"]
    for m, b in summary["by_model"].items():
        lines.append(f"| {m} | {b['calls']} | {b['failed_attempts']} | {b['tokens_out']:,} | {b['list_price_usd']:.4f} |")
    lines += ["", "## Per episode", "", "| ep | calls | failed attempts | tokens | list $ | model time s |",
              "|---:|---:|---:|---:|---:|---:|"]
    for ep, b in summary["by_ep"].items():
        lines.append(f"| {ep} | {b['calls']} | {b['failed_attempts']} | {b['tokens_in'] + b['tokens_out']:,} "
                     f"| {b['list_price_usd']:.4f} | {b['latency_ms'] / 1000:.0f} |")
    days = proj["projected_writer_requests"] / (FREE_WRITER_REQUESTS_PER_DAY * max(1, n_writer_models))
    lines += ["", f"## Projection to {TOTAL_EPISODES} episodes", "",
              f"Measured over {proj['episodes_measured']} episodes.", "",
              "```",
              f"total = per_episode × {TOTAL_EPISODES} + planning + arc_boundary × {N_ARCS}",
              f"      = ${proj['per_episode_list_usd']:.4f} × {TOTAL_EPISODES} + ${proj['planning_list_usd']:.4f} "
              f"+ ${proj['boundary_list_usd']:.4f} × {N_ARCS}",
              f"      ≈ ${proj['projected_list_usd']:.2f} at list price (actual: $0 on the free tier)",
              f"model time ≈ {proj['per_episode_seconds']:.0f}s × {TOTAL_EPISODES} + planning + boundaries "
              f"≈ {proj['projected_hours_model_time']:.1f} h (excluding human review and rate-limit waits)",
              "```", "",
              f"- Tokens per episode: ~{proj['per_episode_tokens']:,.0f}; writer calls per episode: "
              f"{proj['writer_calls_per_episode']:.2f} (draft + revisions).",
              f"- **Free-tier bottleneck is requests, not dollars:** ~{proj['projected_writer_requests']:.0f} writer "
              f"requests at {FREE_WRITER_REQUESTS_PER_DAY}/day/model × {n_writer_models} models "
              f"≈ **{days:.1f} days** of quota for all {TOTAL_EPISODES} episodes.",
              "", "## Decisions logged", ""]
    lines += [f"- {k}: {v}" for k, v in sorted(summary["decisions"].items(), key=lambda kv: -kv[1])]
    return "\n".join(lines) + "\n"
