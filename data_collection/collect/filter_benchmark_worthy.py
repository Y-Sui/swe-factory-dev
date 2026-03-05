#!/usr/bin/env python3
"""
filter_benchmark_worthy.py

Post-process instances_all_*.jsonl to filter instances that are worthy of
being used as SWE-bench benchmark tasks. Uses an LLM to review the code
patch and problem statement, then decides whether to keep or discard.

This runs between build_dataset (step 2) and get_version (step 3).

Usage:
    python3 filter_benchmark_worthy.py <output_dir> [--workers N]
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

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

MODEL_NAME = "anthropic/claude-sonnet-4.5"
MAX_TOKENS = 1024

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

FILTER_SYSTEM = (
    "You are an expert benchmark curator for SWE-bench, a software engineering benchmark. "
    "Your job is to review a code patch (unified diff) and its associated problem statement, "
    "then decide whether this instance is suitable as a benchmark task.\n\n"
    "A good benchmark instance should:\n"
    "1. Involve a meaningful code change — fixing a real bug, adding a non-trivial feature, "
    "or improving correctness/robustness of logic\n"
    "2. Be testable — it should be possible to write a test that fails before the patch and "
    "passes after\n"
    "3. Require understanding of the codebase to solve — not just a mechanical text replacement\n\n"
    "NOTE: The problem statement may be low-quality, vague, or auto-generated from PR titles. "
    "Do NOT reject an instance just because the problem statement is unclear. "
    "Focus primarily on the CODE PATCH itself — if the patch represents a meaningful, testable "
    "code change, it should be KEPT regardless of problem statement quality.\n\n"
    "An instance should be REJECTED if it is:\n"
    "- Pure config/CI/build changes (pyproject.toml, Makefile, Dockerfile, .github/, etc.)\n"
    "- Documentation-only changes (docstrings, comments, README, type hints with no logic change)\n"
    "- Pure dependency version bumps or lock file updates\n"
    "- Trivial string/formatting changes with no behavioral impact\n"
    "- Pure refactoring with no observable behavior change (renaming, moving code, reformatting)\n"
    "- Simple variable/attribute renames (e.g., changing `self.old_name` to `self.new_name`) "
    "with no new logic, no new branching, no new validation — these are mechanical find-and-replace "
    "changes that any developer can do without understanding the codebase\n"
    "- Patches that are too small or too simple to be a meaningful benchmark challenge "
    "(e.g., changing only 1-2 lines with a straightforward substitution). A good benchmark task "
    "should require the solver to reason about code behavior, not just do a text replacement.\n"
    "- Changes that cannot be meaningfully tested (e.g., logging changes, print statement changes)\n"
    "- Merge conflict resolutions or reverts with no new logic\n"
    "- Changes to non-Python files only (unless the task specifically involves those files)\n"
    "Focus your analysis on the Python (.py) file changes in the patch."
)

FILTER_USER = """## Problem Statement
{problem_statement}

## Code Patch (unified diff)
{patch}

## Code Context (affected functions/modules)
{context_str}

Based on the above, decide whether this instance is worthy of being a SWE-bench benchmark task.

Respond in the following JSON format:
```json
{{
  "verdict": "KEEP" or "REJECT",
  "reason": "<1-2 sentence explanation>",
  "category": "<one of: bug_fix, feature, robustness, refactor_only, config_only, docs_only, trivial, untestable>"
}}
```

Return ONLY the JSON block, no extra text."""


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def _make_client() -> OpenAI:
    return OpenAI(
        api_key=os.environ["OPENAI_KEY"],
        base_url=os.environ.get("OPENAI_API_BASE_URL"),
        timeout=300,
    )


def _call_llm(client: OpenAI, messages: list[dict], max_tokens: int = MAX_TOKENS) -> str:
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


def _parse_verdict(response: str) -> dict:
    """Extract JSON verdict from LLM response."""
    import re
    # Try to find JSON block between ```json ... ``` or bare { ... }
    # Use a greedy match for nested braces
    for pattern in [r"```json\s*(\{.*?\})\s*```", r"(\{.*\})"]:
        match = re.search(pattern, response, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
    # Fallback: keep the instance if we can't parse
    logger.warning(f"Could not parse LLM response, defaulting to KEEP: {response[:200]}")
    return {"verdict": "KEEP", "reason": "parse failure", "category": "unknown"}


# ---------------------------------------------------------------------------
# Per-instance filtering
# ---------------------------------------------------------------------------

def _majority_vote(verdicts: list[dict]) -> dict:
    """Pick the majority verdict from N judge results."""
    keep_count = sum(1 for v in verdicts if v.get("verdict", "KEEP").upper() == "KEEP")
    reject_count = len(verdicts) - keep_count
    winner = "KEEP" if keep_count >= reject_count else "REJECT"

    # Pick the first verdict matching the winner as the representative
    for v in verdicts:
        if v.get("verdict", "KEEP").upper() == winner:
            return {
                **v,
                "verdict": winner,
                "votes": {"keep": keep_count, "reject": reject_count, "total": len(verdicts)},
            }
    return verdicts[0]


def _build_messages(inst: dict) -> list[dict] | None:
    """Build LLM messages for an instance. Returns None if patch is empty."""
    patch = inst.get("patch", "")
    if not patch.strip():
        return None
    problem_statement = inst.get("problem_statement", "") or inst.get("raw_problem_statement", "")
    patch_context = inst.get("patch_context", [])
    context_str = "\n\n".join(patch_context) if patch_context else "(no code context available)"
    fmt = {
        "problem_statement": problem_statement or "(no problem statement)",
        "patch": patch,
        "context_str": context_str,
    }
    return [
        {"role": "system", "content": FILTER_SYSTEM},
        {"role": "user", "content": FILTER_USER.format(**fmt)},
    ]


def _single_vote(args: tuple) -> tuple[int, int, dict]:
    """Run a single vote for instance idx. Returns (instance_idx, vote_idx, verdict)."""
    idx, vote_idx, messages = args
    client = _make_client()
    try:
        response = _call_llm(client, messages)
        return idx, vote_idx, _parse_verdict(response)
    except Exception as e:
        logger.warning(f"[{idx}] vote {vote_idx} failed: {e}, defaulting to KEEP")
        return idx, vote_idx, {"verdict": "KEEP", "reason": f"error: {e}", "category": "unknown"}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(output_dir: str, workers: int, num_votes: int):
    files = sorted(glob_mod.glob(os.path.join(output_dir, "instances_all_*.jsonl")))
    if not files:
        logger.error(f"No instances_all_*.jsonl found in {output_dir}")
        sys.exit(1)

    jsonl_path = files[-1]
    logger.info(f"Filtering benchmark-worthy instances in {jsonl_path} (workers={workers}, votes={num_votes})")

    instances = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                instances.append(json.loads(line))

    total = len(instances)

    # Pre-build messages and collect all (instance_idx, vote_idx) tasks
    vote_tasks = []  # (idx, vote_idx, messages)
    skip_verdicts = {}  # idx -> verdict for empty-patch instances
    for i, inst in enumerate(instances):
        messages = _build_messages(inst)
        if messages is None:
            skip_verdicts[i] = {"verdict": "REJECT", "reason": "empty patch", "category": "trivial",
                                "votes": {"keep": 0, "reject": 1, "total": 1}}
            continue
        for v in range(num_votes):
            vote_tasks.append((i, v, messages))

    logger.info(f"Submitting {len(vote_tasks)} vote tasks ({total} instances x {num_votes} votes, {len(skip_verdicts)} skipped)")

    # Collect all votes into per-instance lists
    all_votes: dict[int, list[dict]] = {i: [] for i in range(total) if i not in skip_verdicts}
    done_votes = 0
    total_votes = len(vote_tasks)

    executor = ThreadPoolExecutor(max_workers=workers)
    futures = {executor.submit(_single_vote, task): task[0] for task in vote_tasks}
    try:
        for future in as_completed(futures):
            try:
                idx, vote_idx, verdict = future.result()
                all_votes[idx].append(verdict)
            except Exception as e:
                logger.warning(f"Unexpected error: {e}")
            done_votes += 1
            if done_votes % 50 == 0 or done_votes == total_votes:
                logger.info(f"Votes completed: {done_votes}/{total_votes}")
    except KeyboardInterrupt:
        logger.info("Interrupted -- cancelling pending tasks...")
        for f in futures:
            f.cancel()
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    # Aggregate votes per instance
    verdicts = [None] * total
    for idx, v in skip_verdicts.items():
        verdicts[idx] = v
    for idx, votes in all_votes.items():
        if votes:
            verdict = _majority_vote(votes)
            verdict["instance_id"] = instances[idx].get("instance_id", "")
            verdicts[idx] = verdict

    # Partition instances
    kept = []
    rejected = []
    stats = {}
    for inst, verdict in zip(instances, verdicts):
        if verdict is None:
            kept.append(inst)
            continue
        cat = verdict.get("category", "unknown")
        stats[cat] = stats.get(cat, 0) + 1
        if verdict.get("verdict", "KEEP").upper() == "KEEP":
            inst["benchmark_filter"] = verdict
            kept.append(inst)
        else:
            inst["benchmark_filter"] = verdict
            rejected.append(inst)

    # Clean up old filter/rejected files
    for pattern in ["instances_filter_*.jsonl", "instances_rejected_*.jsonl"]:
        for f in glob_mod.glob(os.path.join(output_dir, pattern)):
            os.remove(f)

    kept_path = os.path.join(output_dir, f"instances_filter_{len(kept)}.jsonl")
    with open(kept_path, "w", encoding="utf-8") as f:
        for inst in kept:
            f.write(json.dumps(inst) + "\n")

    rejected_path = os.path.join(output_dir, f"instances_rejected_{len(rejected)}.jsonl")
    with open(rejected_path, "w", encoding="utf-8") as f:
        for inst in rejected:
            f.write(json.dumps(inst) + "\n")

    logger.info(f"Category breakdown: {json.dumps(stats, indent=2)}")
    logger.info(f"Kept: {len(kept)} -> {kept_path}")
    logger.info(f"Rejected: {len(rejected)} -> {rejected_path}")
    logger.info(f"Done. {len(kept)}/{total} instances passed benchmark filter.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", help="Directory containing instances_all_*.jsonl")
    parser.add_argument("--workers", type=int, default=30, help="Number of concurrent LLM calls")
    parser.add_argument("--num-votes", type=int, default=3, help="Number of LLM judges per instance for majority vote (default: 3)")
    args = parser.parse_args()
    main(args.output_dir, args.workers, args.num_votes)
