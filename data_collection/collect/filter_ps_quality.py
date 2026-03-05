#!/usr/bin/env python3
"""
filter_ps_quality.py

Quality gate for refined problem statements. Uses LLM majority vote to check:
1. No gold patch leakage (fix details, new variable names, added constants)
2. Behavioral coverage — the statement covers all changes in the patch at a
   general/behavioral level (not implementation-specific)
3. Declarative style — statements only, no questions
4. Reads like a code task / GitHub issue

Instances that fail are re-refined using the original refine prompt augmented
with the quality analysis as feedback, then written back in-place.

Usage:
    python3 filter_ps_quality.py <output_dir> [--workers N] [--num-votes 3] [--max-retries 2]
"""

import argparse
import glob as glob_mod
import json
import logging
import os
import re
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
MAX_TOKENS_JUDGE = 1024
MAX_TOKENS_REGEN = 400
MAX_TOKENS_HINTS = 512

# ---------------------------------------------------------------------------
# Quality-check prompts
# ---------------------------------------------------------------------------

JUDGE_SYSTEM = (
    "You are a quality auditor for SWE-bench problem statements. "
    "You will be given a problem statement, the gold code patch (unified diff), "
    "and code context. Your job is to check the problem statement against strict criteria.\n\n"
    "You must evaluate ALL of the following dimensions:\n\n"
    "1. **LEAKAGE** — Does the problem statement reveal information from the gold patch?\n"
    "   Leakage includes: mentioning new variable/parameter/constant names introduced by the fix, "
    "describing the exact code change, referencing added lines, naming the solution approach, "
    "or mentioning that a PR/commit/patch exists.\n\n"
    "2. **COVERAGE** — Does the problem statement cover the general behavioral intent of ALL "
    "the changes in the patch? It should describe at a high level what is broken or missing "
    "that the patch addresses. It does NOT need to name every file or function — it should "
    "capture the overall bug or feature request that motivates all the changes.\n\n"
    "3. **STYLE** — Is the problem statement written entirely in declarative statements? "
    "It must NOT contain any question marks or interrogative sentences. "
    "It should read like a GitHub issue or code task description.\n\n"
    "4. **SPECIFICITY** — Is the problem statement at the right level of abstraction? "
    "It should describe observable behavior or missing capability, NOT internal implementation "
    "details. It should be broad enough that multiple valid fixes could address it, "
    "but specific enough that a developer understands what needs to change."
)

JUDGE_USER = """## Problem Statement (to evaluate)
{problem_statement}

## Gold Patch (unified diff)
{patch}

## Code Context (affected functions/modules)
{context_str}

Evaluate the problem statement against all 4 criteria. For each, give a PASS or FAIL with a brief reason.

Respond in this JSON format:
```json
{{
  "leakage": {{"verdict": "PASS" or "FAIL", "reason": "<1 sentence>"}},
  "coverage": {{"verdict": "PASS" or "FAIL", "reason": "<1 sentence>"}},
  "style": {{"verdict": "PASS" or "FAIL", "reason": "<1 sentence>"}},
  "specificity": {{"verdict": "PASS" or "FAIL", "reason": "<1 sentence>"}},
  "overall": "PASS" or "FAIL"
}}
```

Rules:
- overall is FAIL if ANY dimension is FAIL
- Be strict on leakage — any hint of the fix is a FAIL
- Be reasonable on coverage — it should capture the general intent, not every line
- Be strict on style — any question mark is a FAIL

Return ONLY the JSON block."""


# ---------------------------------------------------------------------------
# Re-generation prompts (refine with feedback)
# ---------------------------------------------------------------------------

REGEN_SYSTEM = (
    "You rewrite problem statements for SWE-bench benchmark tasks. "
    "A previous version of this problem statement failed quality review. "
    "You must fix the identified issues while preserving the core intent.\n\n"
    "The problem statement must:\n"
    "- Use declarative statements ONLY — no question marks, no interrogative sentences\n"
    "- Read like a real GitHub issue or code task filed by a developer who encountered the problem\n"
    "- Describe the observable problem or missing behavior, NOT the fix or implementation\n"
    "- Cover the general behavioral intent of all changes in the patch\n"
    "- NEVER leak gold patch information: no new variable names, no added parameters, "
    "no specific code changes, no solution hints\n"
    "- Be broad enough for multiple valid fixes, but specific enough to be actionable\n\n"
    "IMPORTANT: The code context and patch are provided ONLY so you understand the bug/feature. "
    "Do NOT let any fix details appear in the problem statement."
)

REGEN_USER = """## Previous Problem Statement (failed quality review)
{previous_ps}

## Quality Review Feedback
{feedback}

## Raw PR/commit description
{source}

## Code context (affected functions/modules)
{context_str}

Rewrite the problem statement to fix ALL the issues identified in the quality review.

Format:
<title line — a short, declarative summary of the bug or feature request>

<body — 60-150 words describing the problem at a behavioral level. Use only declarative statements. For bugs: state what the user did, what happened, and what the expected behavior is. For features: state what capability is missing and describe a usage scenario.>

Rules:
- Fix every issue from the quality review feedback
- Use ONLY declarative statements. No questions. No "?" anywhere.
- Do NOT leak any patch/fix information
- Keep it broad enough that multiple valid fixes could address the issue

Return only the issue text (title + body), no extra commentary."""

REGEN_HINTS_SYSTEM = (
    "You provide diagnostic hints that help a developer locate the root cause. "
    "You know the codebase well and can point to specific locations, but you "
    "do not give away the complete solution — only enough to guide investigation.\n"
    "Focus only on Python (.py) files.\n\n"
    "IMPORTANT: The code context may contain diff markers showing the fix. "
    "Do NOT reveal exact code changes, new variable names, or added parameters. "
    "Only describe what is currently wrong or missing in the pre-patch code."
)

REGEN_HINTS_USER = """## Problem description
{source}

## Code context (affected functions/modules)
{context_str}

Write diagnostic hints (100-150 words) that help a developer find and fix this issue:
- Name the specific file(s), class(es), and function(s) involved
- Describe what the code currently does at each location and why it is wrong or incomplete
- Point toward the fix direction without spelling out the exact code changes
- Do NOT reveal specific variable names, parameters, or constants that were ADDED by the fix
- Use only declarative statements, no questions

Return only the hints text, plain prose."""


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def _make_client() -> OpenAI:
    return OpenAI(
        api_key=os.environ["OPENAI_KEY"],
        base_url=os.environ.get("OPENAI_API_BASE_URL"),
        timeout=300,
    )


def _call_llm(client: OpenAI, messages: list[dict], max_tokens: int) -> str:
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


def _parse_judge(response: str) -> dict:
    for pattern in [r"```json\s*(\{.*?\})\s*```", r"(\{.*\})"]:
        match = re.search(pattern, response, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
    logger.warning(f"Could not parse judge response, defaulting to PASS: {response[:200]}")
    return {"overall": "PASS", "leakage": {"verdict": "PASS"}, "coverage": {"verdict": "PASS"},
            "style": {"verdict": "PASS"}, "specificity": {"verdict": "PASS"}}


# ---------------------------------------------------------------------------
# Judging logic
# ---------------------------------------------------------------------------

def _build_judge_messages(inst: dict) -> list[dict] | None:
    ps = inst.get("problem_statement", "")
    patch = inst.get("patch", "")
    if not ps or not patch.strip():
        return None
    patch_context = inst.get("patch_context", [])
    context_str = "\n\n".join(patch_context) if patch_context else "(no code context available)"
    fmt = {
        "problem_statement": ps,
        "patch": patch,
        "context_str": context_str,
    }
    return [
        {"role": "system", "content": JUDGE_SYSTEM},
        {"role": "user", "content": JUDGE_USER.format(**fmt)},
    ]


def _single_vote(args: tuple) -> tuple[int, int, dict]:
    idx, vote_idx, messages = args
    client = _make_client()
    try:
        response = _call_llm(client, messages, MAX_TOKENS_JUDGE)
        return idx, vote_idx, _parse_judge(response)
    except Exception as e:
        logger.warning(f"[{idx}] vote {vote_idx} failed: {e}, defaulting to PASS")
        return idx, vote_idx, {"overall": "PASS"}


def _majority_vote(verdicts: list[dict]) -> dict:
    fail_count = sum(1 for v in verdicts if v.get("overall", "PASS").upper() == "FAIL")
    pass_count = len(verdicts) - fail_count
    winner = "FAIL" if fail_count > pass_count else "PASS"

    # Merge all failure reasons across votes for richer feedback
    merged = {"overall": winner, "votes": {"pass": pass_count, "fail": fail_count, "total": len(verdicts)}}
    for dim in ("leakage", "coverage", "style", "specificity"):
        dim_fails = [v[dim]["reason"] for v in verdicts
                     if isinstance(v.get(dim), dict) and v[dim].get("verdict", "PASS").upper() == "FAIL"]
        dim_fail_count = len(dim_fails)
        merged[dim] = {
            "verdict": "FAIL" if dim_fail_count > len(verdicts) // 2 else "PASS",
            "reasons": dim_fails if dim_fails else [],
        }
    return merged


def _format_feedback(verdict: dict) -> str:
    """Format the quality verdict into human-readable feedback for the re-generation prompt."""
    lines = []
    for dim in ("leakage", "coverage", "style", "specificity"):
        info = verdict.get(dim, {})
        if info.get("verdict", "PASS").upper() == "FAIL":
            reasons = info.get("reasons", [])
            reason_str = "; ".join(reasons) if reasons else "no detail"
            lines.append(f"- {dim.upper()}: FAIL — {reason_str}")
    return "\n".join(lines) if lines else "No specific failures identified."


# ---------------------------------------------------------------------------
# Re-generation logic
# ---------------------------------------------------------------------------

def _regen_one(args: tuple) -> tuple[int, str, str]:
    """Re-generate problem_statement and hints_text using quality feedback."""
    idx, inst, feedback = args
    raw = inst.get("raw_problem_statement", "")
    existing = inst.get("problem_statement", "")
    source = existing if (existing and existing != raw) else raw
    if not source:
        return idx, existing, inst.get("hints_text", "")

    patch_context = inst.get("patch_context", [])
    context_str = "\n\n".join(patch_context) if patch_context else "(no code context available)"

    fmt = {
        "previous_ps": existing,
        "feedback": feedback,
        "source": source,
        "context_str": context_str,
    }

    client = _make_client()
    try:
        ps = _call_llm(client, [
            {"role": "system", "content": REGEN_SYSTEM},
            {"role": "user", "content": REGEN_USER.format(**fmt)},
        ], MAX_TOKENS_REGEN)

        hints = _call_llm(client, [
            {"role": "system", "content": REGEN_HINTS_SYSTEM},
            {"role": "user", "content": REGEN_HINTS_USER.format(**fmt)},
        ], MAX_TOKENS_HINTS)

        return idx, ps, hints
    except Exception as e:
        logger.warning(f"[{idx}] regen failed: {e}, keeping previous")
        return idx, existing, inst.get("hints_text", "")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(output_dir: str, workers: int, num_votes: int, max_retries: int):
    subset_files = sorted(glob_mod.glob(os.path.join(output_dir, "instances_filter_*_subset.jsonl")))
    filter_files = sorted(glob_mod.glob(os.path.join(output_dir, "instances_filter_*.jsonl")))
    # Prefer subset files over full filter files
    files = subset_files or [f for f in filter_files if "_subset" not in f]
    if not files:
        logger.error(f"No instances_filter_*.jsonl found in {output_dir}")
        sys.exit(1)

    jsonl_path = files[-1]
    logger.info(f"Quality-checking problem statements in {jsonl_path} (workers={workers}, votes={num_votes}, retries={max_retries})")

    instances = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                instances.append(json.loads(line))

    total = len(instances)

    for retry_round in range(max_retries + 1):
        label = "Initial check" if retry_round == 0 else f"Re-check round {retry_round}"
        logger.info(f"--- {label} ---")

        # Determine which instances need checking
        if retry_round == 0:
            check_indices = list(range(total))
        else:
            check_indices = [i for i in range(total)
                            if instances[i].get("ps_quality", {}).get("overall", "PASS").upper() == "FAIL"]

        if not check_indices:
            logger.info("All instances passed quality check.")
            break

        # Build vote tasks
        vote_tasks = []
        skip_indices = set()
        for i in check_indices:
            messages = _build_judge_messages(instances[i])
            if messages is None:
                skip_indices.add(i)
                instances[i]["ps_quality"] = {"overall": "SKIP", "reason": "empty ps or patch"}
                continue
            for v in range(num_votes):
                vote_tasks.append((i, v, messages))

        logger.info(f"Submitting {len(vote_tasks)} vote tasks for {len(check_indices)} instances ({len(skip_indices)} skipped)")

        # Collect votes
        all_votes: dict[int, list[dict]] = {i: [] for i in check_indices if i not in skip_indices}
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
            logger.info("Interrupted -- cancelling...")
            for f in futures:
                f.cancel()
            break
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        # Aggregate votes and mark instances
        fail_indices = []
        for idx, votes in all_votes.items():
            if not votes:
                continue
            verdict = _majority_vote(votes)
            verdict["instance_id"] = instances[idx].get("instance_id", "")
            instances[idx]["ps_quality"] = verdict
            if verdict["overall"] == "FAIL":
                fail_indices.append(idx)

        pass_count = len(check_indices) - len(fail_indices) - len(skip_indices)
        logger.info(f"Quality results: {pass_count} PASS, {len(fail_indices)} FAIL, {len(skip_indices)} SKIP")

        if not fail_indices or retry_round >= max_retries:
            if fail_indices:
                logger.warning(f"{len(fail_indices)} instances still failing after {max_retries} retries")
            break

        # Re-generate problem statements for failing instances
        logger.info(f"Re-generating {len(fail_indices)} failing problem statements...")
        regen_tasks = []
        for idx in fail_indices:
            feedback = _format_feedback(instances[idx]["ps_quality"])
            regen_tasks.append((idx, instances[idx], feedback))

        regen_done = 0
        executor = ThreadPoolExecutor(max_workers=workers)
        futures = {executor.submit(_regen_one, task): task[0] for task in regen_tasks}
        try:
            for future in as_completed(futures):
                try:
                    idx, ps, hints = future.result()
                    instances[idx]["problem_statement"] = ps
                    instances[idx]["hints_text"] = hints
                except Exception as e:
                    logger.warning(f"Regen error: {e}")
                regen_done += 1
                if regen_done % 10 == 0 or regen_done == len(regen_tasks):
                    logger.info(f"Re-generated {regen_done}/{len(regen_tasks)}")
        except KeyboardInterrupt:
            logger.info("Interrupted -- cancelling...")
            for f in futures:
                f.cancel()
            break
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    # Write back
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for inst in instances:
            f.write(json.dumps(inst) + "\n")

    # Summary
    final_pass = sum(1 for inst in instances if inst.get("ps_quality", {}).get("overall", "PASS").upper() == "PASS")
    final_fail = sum(1 for inst in instances if inst.get("ps_quality", {}).get("overall", "").upper() == "FAIL")
    final_skip = sum(1 for inst in instances if inst.get("ps_quality", {}).get("overall", "").upper() == "SKIP")
    logger.info(f"Done. Final: {final_pass} PASS, {final_fail} FAIL, {final_skip} SKIP out of {total}. Updated {jsonl_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", help="Directory containing instances_all_*.jsonl")
    parser.add_argument("--workers", type=int, default=30, help="Number of concurrent LLM calls")
    parser.add_argument("--num-votes", type=int, default=3, help="Number of LLM judges per instance (default: 3)")
    parser.add_argument("--max-retries", type=int, default=2, help="Max re-generation rounds for failing instances (default: 2)")
    args = parser.parse_args()
    main(args.output_dir, args.workers, args.num_votes, args.max_retries)
