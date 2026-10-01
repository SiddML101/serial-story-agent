"""Check the API key and models: one call per tier; prints reply, token usage, and latency.

Usage: python scripts/smoke_test.py
WRITER_MODEL / FAST_MODEL may be comma-separated fallback chains; each tier tries its models in order and reports
the first that answers (a model out of daily quota or overloaded is reported and skipped).
"""
import os
import sys
import time

from dotenv import load_dotenv
from openai import APIStatusError, OpenAI

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def chain(value: str | None) -> list[str]:
    return [m.strip() for m in (value or "").split(",") if m.strip()]


def ping(client: OpenAI, model: str) -> bool:
    start = time.perf_counter()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with one short sentence: what is a cliffhanger?"}],
        )
    except APIStatusError as e:
        reason = "daily quota used up" if "PerDay" in str(e) else str(e).split("'message': '")[-1][:90]
        print(f"    {model}: {e.status_code} {reason}")
        return False
    usage = resp.usage
    print(f"    {model}: OK in {(time.perf_counter() - start) * 1000:.0f} ms, "
          f"tokens in={usage.prompt_tokens if usage else '?'} out={usage.completion_tokens if usage else '?'}")
    print(f"      reply: {(resp.choices[0].message.content or '').strip()[:120]}")
    return True


def main() -> None:
    load_dotenv(os.path.join(ROOT, ".env"))
    api_key = os.getenv("LLM_API_KEY")
    if not api_key:
        sys.exit("LLM_API_KEY is not set in .env (or set it in the web UI under Settings)")
    tiers = {"writer": chain(os.getenv("WRITER_MODEL")), "fast": chain(os.getenv("FAST_MODEL"))}
    missing = [t for t, models in tiers.items() if not models]
    if missing:
        sys.exit(f"Set {', '.join(t.upper() + '_MODEL' for t in missing)} in .env (see scripts/list_models.py)")

    client = OpenAI(base_url=os.getenv("LLM_BASE_URL"), api_key=api_key, max_retries=1, timeout=120)
    gap = float(os.getenv("MIN_SECONDS_BETWEEN_CALLS", "4"))
    failed = []
    for tier, models in tiers.items():
        print(f"[{tier}] chain: {', '.join(models)}")
        for i, model in enumerate(models):
            if i:
                time.sleep(gap)  # free-tier RPM
            if ping(client, model):
                break
        else:
            failed.append(tier)
        time.sleep(gap)
    if failed:
        sys.exit(f"No model answered for: {', '.join(failed)}")
    print("\nAll tiers OK.")


if __name__ == "__main__":
    main()
