#!/usr/bin/env python3
"""
refine_problem_statements.py

Post-process instances_all_*.jsonl to refine problem_statement and hints_text
fields using LLM.

- problem_statement: general issue description (no specific fix, minimal function names)
- hints_text: detailed diagnosis with specific locations and solution hints

Usage:
    python3 refine_problem_statements.py <output_dir> [--workers N]
"""

import argparse
import glob as glob_mod
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from openai import OpenAI, RateLimitError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from utils import refine_problem_statement

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

MODEL_NAME = "anthropic/claude-sonnet-4.5"
MAX_TOKENS_PS = 256
MAX_TOKENS_HINTS = 512

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

PROBLEM_STATEMENT_SYSTEM = (
    "You rewrite raw PR/commit descriptions into realistic GitHub issue descriptions, "
    "matching the style used in SWE-bench and SWE-bench-Pro benchmarks.\n\n"
    "A good problem statement reads like a real bug report or feature request filed by a user "
    "who has encountered the problem but does NOT know the fix. It should:\n"
    "- State the problem from the user's perspective (what they tried, what went wrong)\n"
    "- Include a short reproduction scenario or code snippet when possible\n"
    "- Show expected vs actual behavior for bugs\n"
    "- Describe the desired capability for feature requests\n"
    "- NEVER mention the fix, patch, implementation, or solution\n"
    "- NEVER reference PR numbers, commit SHAs, or that a fix exists\n"
    "- Use natural developer language, not formal spec language\n"
    "- Focus only on Python (.py) files; ignore non-Python changes"
)

PROBLEM_STATEMENT_USER = """## Raw PR/commit description
{source}

## Code context (affected functions/modules)
{context_str}

Rewrite the above into a GitHub issue that a developer would file BEFORE any fix exists.

Format:
<title line — a short summary of the bug or feature request>

<body — 60-120 words describing the problem. For bugs, include: what the user did, what happened, what they expected. For features, include: what capability is missing and a usage scenario. If possible, include a minimal code snippet showing the broken or missing behavior.>

Rules:
- Write as if you do NOT know the solution
- Do NOT say "should be changed to", "needs to be fixed by", or describe any implementation
- Do NOT reference any PR, commit, or that a patch exists
- Keep it natural — this should read like a real GitHub issue

Return only the issue text (title + body), no extra commentary."""

HINTS_SYSTEM = (
    "You provide diagnostic hints that help a developer locate the root cause. "
    "You know the codebase well and can point to specific locations, but you "
    "do not give away the complete solution — only enough to guide investigation.\n"
    "Focus only on Python (.py) files."
)

HINTS_USER = """## Problem description
{source}

## Code context (affected functions/modules)
{context_str}

Write diagnostic hints (100-150 words) that help a developer find and fix this issue:
- Name every specific file, class, and function involved
- Describe what the code currently does at each location and why it's wrong
- Point toward the fix direction without spelling out the complete solution
  (e.g. "the validation in X.validate() doesn't account for Y" or
   "this function needs to propagate Z to its caller")

Return only the hints text, plain prose."""


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def _make_client() -> OpenAI:
    """Create a thread-local OpenAI client. Each worker calls this independently."""
    return OpenAI(
        api_key=os.environ["OPENAI_KEY"],
        base_url=os.environ.get("OPENAI_API_BASE_URL"),
        timeout=300,
    )


def _call_llm(client: OpenAI, messages: list[dict], max_tokens: int = MAX_TOKENS_PS) -> str:
    """Call the LLM with a simple retry (short waits, no 60s tenacity cascade)."""
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=MODEL_NAME,
                messages=messages,
                max_tokens=max_tokens,
            )
            return resp.choices[0].message.content.strip()
        except RateLimitError:
            if attempt == 2:
                raise
            wait = 10 * (attempt + 1)
            logger.warning(f"Rate limited, retrying in {wait}s...")
            time.sleep(wait)
    return ""


# ---------------------------------------------------------------------------
# Per-instance refinement
# ---------------------------------------------------------------------------

def _refine_one(args: tuple) -> tuple[int, str, str]:
    """Returns (idx, problem_statement, hints_text)."""
    idx, inst = args
    raw = inst.get("raw_problem_statement", "")
    existing = inst.get("problem_statement", "")
    # Prefer existing refined problem_statement if it differs from raw; otherwise use raw
    source = existing if (existing and existing != raw) else raw
    if not source:
        return idx, "", ""

    patch_context = inst.get("patch_context", [])
    context_str = "\n\n".join(patch_context) if patch_context else "(no code context available)"

    fmt = {"source": source, "context_str": context_str}

    client = _make_client()
    try:
        # Generate general problem_statement
        ps = _call_llm(client, [
            {"role": "system", "content": PROBLEM_STATEMENT_SYSTEM},
            {"role": "user", "content": PROBLEM_STATEMENT_USER.format(**fmt)},
        ], max_tokens=MAX_TOKENS_PS)

        # Generate detailed hints_text
        hints = _call_llm(client, [
            {"role": "system", "content": HINTS_SYSTEM},
            {"role": "user", "content": HINTS_USER.format(**fmt)},
        ], max_tokens=MAX_TOKENS_HINTS)

        return idx, ps, hints
    except Exception as e:
        logger.warning(f"[{idx}] refine failed: {e}, keeping original")
        return idx, source, inst.get("hints_text", "")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(output_dir: str, workers: int):
    files = sorted(glob_mod.glob(os.path.join(output_dir, "instances_all_*.jsonl")))
    if not files:
        logger.error(f"No instances_all_*.jsonl found in {output_dir}")
        sys.exit(1)

    jsonl_path = files[-1]
    logger.info(f"Refining problem statements in {jsonl_path} (workers={workers})")

    instances = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                instances.append(json.loads(line))

    total = len(instances)
    done = 0
    executor = ThreadPoolExecutor(max_workers=workers)
    futures = {
        executor.submit(_refine_one, (i, inst)): i
        for i, inst in enumerate(instances)
    }
    try:
        for future in as_completed(futures):
            try:
                idx, ps, hints = future.result()
                instances[idx]["problem_statement"] = ps
                instances[idx]["hints_text"] = hints
            except Exception as e:
                logger.warning(f"Unexpected error: {e}")
            done += 1
            if done % 10 == 0 or done == total:
                logger.info(f"Refined {done}/{total}")
    except KeyboardInterrupt:
        logger.info("Interrupted — cancelling pending tasks...")
        for f in futures:
            f.cancel()
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    with open(jsonl_path, "w", encoding="utf-8") as f:
        for inst in instances:
            f.write(json.dumps(inst) + "\n")

    logger.info(f"Done. Updated {total} instances in {jsonl_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", help="Directory containing instances_all_*.jsonl")
    parser.add_argument("--workers", type=int, default=30, help="Number of concurrent LLM calls (default: 4)")
    args = parser.parse_args()
    main(args.output_dir, args.workers)
