"""List models available at the configured OpenAI-compatible endpoint.

Usage: python scripts/list_models.py
Then set WRITER_MODEL (strong) and FAST_MODEL (cheap) in .env.
"""
import os
import sys

from dotenv import load_dotenv
from openai import OpenAI


def main() -> None:
    load_dotenv()
    base_url = os.getenv("LLM_BASE_URL")
    api_key = os.getenv("LLM_API_KEY")
    if not api_key:
        sys.exit("LLM_API_KEY is not set in .env")

    client = OpenAI(base_url=base_url, api_key=api_key)
    ids = sorted(m.id for m in client.models.list())
    print(f"{len(ids)} models at {base_url}\n")
    for model_id in ids:
        print(" ", model_id)


if __name__ == "__main__":
    main()
