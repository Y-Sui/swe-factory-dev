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

MODEL_NAME = "google/gemini-2.5-flash"
MAX_TOKENS_PS = 256
MAX_TOKENS_HINTS = 512

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

PROBLEM_STATEMENT_SYSTEM = (
    "You are a developer filing a bug report or feature request. "
    "Write a general issue description that describes the problem or missing functionality. "
    "Keep it high-level — mention at most 1-2 file or function names to orient the reader, "
    "but do NOT enumerate every specific function, variable, or line involved. "
    "Focus on the observable symptoms: what goes wrong, what error occurs, or what behavior is missing. "
    "CRITICAL: Do NOT suggest or describe any solution, fix, or implementation approach. "
    "Only describe WHAT is wrong or missing, not HOW to fix it. "
    "End with a natural question a developer would ask, e.g. "
    "'Can someone look into this?' or 'Is this the expected behavior?' "
    "Keep it under 100 words. "
    "Focus ONLY on Python code changes (.py files). Ignore non-Python files."
)

PROBLEM_STATEMENT_USER = """## Source issue description
{source}

## Code context (changed functions and module-level changes)
{context_str}

Write an issue description in approximately 80 words. It must:
1. Describe the observable problem or missing feature at a high level
2. Mention at most 1-2 file/function names for orientation — do not list every affected location
3. Do NOT describe any solution or what the correct behavior "should be" — only describe the problem
4. End with a question

Write in plain prose (no bullet points, no headers). Return only the issue description text."""

HINTS_SYSTEM = (
    "You are a senior developer providing diagnostic hints for a bug report. "
    "Write a detailed technical analysis that helps another developer locate and fix the issue. "
    "Name every specific function, class, and file involved. "
    "Point out the exact location(s) where the bug manifests or where changes are needed. "
    "Describe what the code currently does wrong and hint at the direction of the fix "
    "(e.g. 'the condition should also check X' or 'this value needs to be passed through'). "
    "Keep it under 200 words. "
    "Focus ONLY on Python code changes (.py files). Ignore non-Python files."
)

HINTS_USER = """## Source issue description
{source}

## Code context (changed functions and module-level changes)
{context_str}

Write detailed diagnostic hints in approximately 150 words. It must:
1. Name every specific function, class, and file involved
2. Describe what the code currently does wrong at each location
3. Hint at the direction of the fix without giving the full solution
4. Focus ONLY on Python (.py) file changes

Write in plain prose. Return only the hints text."""


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
    parser.add_argument("--workers", type=int, default=4, help="Number of concurrent LLM calls (default: 4)")
    args = parser.parse_args()
    main(args.output_dir, args.workers)
