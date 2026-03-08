#!/usr/bin/env python3
"""
verify_test_quality.py

Quality verification for generated test cases in SWE-bench results.
Uses LLM majority vote to check multiple dimensions:

1. BREADTH   — Are tests too broad? (would pass with unrelated changes)
2. NARROWNESS — Are tests too narrow / overfitted to the exact gold patch?
3. WEAKNESS  — Are F2P tests too weak? (fail for wrong reasons, e.g. import errors)
4. LEAKAGE   — Do tests encode gold patch details? (variable names, constants, etc.)
5. RELEVANCE — Do F2P test failures actually relate to the bug in problem_statement?

Usage:
    python3 scripts/verify_test_quality.py <results_json> [--workers N] [--num-votes 3] [--output results_verified.json]
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from openai import OpenAI, RateLimitError

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

MODEL_NAME = "anthropic/claude-sonnet-4.5"
MAX_TOKENS_JUDGE = 2048


# ---------------------------------------------------------------------------
# Judge prompt
# ---------------------------------------------------------------------------

JUDGE_SYSTEM = (
    "You are an expert test-quality auditor for SWE-bench, a software engineering benchmark. "
    "You will be given:\n"
    "- A problem statement (describes the bug or feature)\n"
    "- The gold code patch (unified diff of the fix)\n"
    "- The test patch (unified diff of generated test files)\n"
    "- FAIL_TO_PASS test IDs (tests that should fail before fix, pass after)\n"
    "- PASS_TO_PASS test IDs (regression tests that pass both before and after)\n\n"
    "Your job is to evaluate the TEST QUALITY across 5 dimensions.\n\n"
    "1. **BREADTH** — Are the tests too broad?\n"
    "   A test is too broad if it would pass with almost any change to the affected file, "
    "or if the assertions are so loose they don't actually verify the fix. "
    "For example: only checking that a function exists, only checking return type, "
    "or catching overly general exceptions.\n\n"
    "2. **NARROWNESS** — Are the tests too narrow / overfitted to the gold patch?\n"
    "   A test is too narrow if it would ONLY pass with the exact gold patch implementation "
    "and would reject other valid fixes. For example: asserting exact string output that "
    "depends on implementation details, checking internal variable values instead of "
    "observable behavior, or testing the specific implementation approach rather than the result.\n\n"
    "3. **WEAKNESS** — Do F2P tests fail for the RIGHT reason?\n"
    "   You must first determine whether the patch is a BUG FIX or a FEATURE ADDITION.\n"
    "   - For BUG FIXES: A test is weak if it fails pre-patch due to ImportError, SyntaxError, "
    "NameError, or missing module rather than the actual buggy behavior. The failure should be "
    "a meaningful assertion failure or behavioral error related to the bug.\n"
    "   - For FEATURE ADDITIONS (new functions, parameters, modules, classes): It is perfectly "
    "acceptable — and expected — for F2P tests to fail pre-patch with ImportError, AttributeError, "
    "TypeError (missing parameter), or similar errors, because the feature simply does not exist yet. "
    "This is the CORRECT failure mode. Only mark FAIL if the test has broken setup unrelated to "
    "the missing feature (e.g., wrong test fixtures, unrelated missing dependencies).\n\n"
    "4. **LEAKAGE** — Do the tests leak enough gold patch information to reconstruct the fix?\n"
    "   Some overlap between tests and the patch is inevitable and acceptable:\n"
    "   - Tests for new features MUST reference new parameter/function/class names — this is NOT leakage.\n"
    "   - Tests that check expected output values related to the fix are acceptable.\n"
    "   - Tests that verify correct behavior using values from the patch are acceptable.\n"
    "   Leakage means the tests reveal SO MUCH implementation detail that a solver could "
    "fully reconstruct the fix just by reading the tests, WITHOUT understanding the problem. "
    "For example: tests that inspect source code (AST checks), tests that assert exact internal "
    "variable values matching the patch, or tests that encode the complete solution logic step-by-step. "
    "The bar is: could a solver write the COMPLETE fix with only the tests and no problem understanding?\n\n"
    "5. **RELEVANCE** — Are F2P tests actually testing the bug/feature described?\n"
    "   The F2P tests should exercise the specific behavior described in the problem statement. "
    "If the problem is about proxy handling but the tests check unrelated config parsing, "
    "that's irrelevant. The tests should directly validate that the bug is fixed or the "
    "feature works as described."
)

JUDGE_USER = """## Problem Statement
{problem_statement}

## Gold Patch (unified diff)
{patch}

## Test Patch (generated tests)
{test_patch}

## FAIL_TO_PASS tests
{fail_to_pass}

## PASS_TO_PASS tests
{pass_to_pass}

Evaluate the test quality across all 5 dimensions. For each dimension, give a verdict and brief reason.

Respond in this JSON format:
```json
{{
  "breadth": {{"verdict": "PASS" or "FAIL", "reason": "<1-2 sentences>"}},
  "narrowness": {{"verdict": "PASS" or "FAIL", "reason": "<1-2 sentences>"}},
  "weakness": {{"verdict": "PASS" or "FAIL", "reason": "<1-2 sentences>"}},
  "leakage": {{"verdict": "PASS" or "FAIL", "reason": "<1-2 sentences>"}},
  "relevance": {{"verdict": "PASS" or "FAIL", "reason": "<1-2 sentences>"}},
  "overall": "PASS" or "FAIL",
  "summary": "<1-2 sentence overall assessment>"
}}
```

Rules:
- overall is FAIL if ANY dimension is FAIL
- BREADTH FAIL: assertions are so loose that almost any code change would make them pass
- NARROWNESS FAIL: tests are so tightly coupled to the exact gold patch that valid alternative fixes would fail
- WEAKNESS FAIL: for BUG FIXES, F2P fails due to unrelated errors instead of the bug. For FEATURE ADDITIONS, ImportError/AttributeError for the new feature is EXPECTED and is a PASS — only FAIL if setup is broken for unrelated reasons
- LEAKAGE FAIL: tests reveal enough that a solver could write the COMPLETE fix without understanding the problem. Referencing new parameter/function names for features is NOT leakage. Testing expected behavior/values is NOT leakage. Only FAIL for source code inspection, complete solution logic encoded in tests, or internal implementation details that go far beyond behavioral testing
- RELEVANCE FAIL: F2P tests don't actually exercise the behavior described in the problem statement
- Be strict on relevance — this is the most critical for benchmark quality
- Be moderate on narrowness and leakage — some coupling to the fix is acceptable

Return ONLY the JSON block."""


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
    logger.warning(f"Could not parse judge response: {response[:200]}")
    return {"overall": "UNKNOWN", "summary": "parse failure"}


# ---------------------------------------------------------------------------
# Judging logic
# ---------------------------------------------------------------------------

DIMENSIONS = ("breadth", "narrowness", "weakness", "leakage", "relevance")


def _build_judge_messages(inst: dict) -> list[dict] | None:
    test_patch = inst.get("test_patch", "")
    patch = inst.get("patch", "")
    if not test_patch.strip() or not patch.strip():
        return None

    fmt = {
        "problem_statement": inst.get("problem_statement", "(none)"),
        "patch": patch,
        "test_patch": test_patch,
        "fail_to_pass": inst.get("FAIL_TO_PASS", "[]"),
        "pass_to_pass": inst.get("PASS_TO_PASS", "[]"),
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
        logger.warning(f"[{idx}] vote {vote_idx} failed: {e}")
        return idx, vote_idx, {"overall": "UNKNOWN", "summary": f"error: {e}"}


def _majority_vote(verdicts: list[dict]) -> dict:
    fail_count = sum(1 for v in verdicts if v.get("overall", "PASS").upper() == "FAIL")
    pass_count = sum(1 for v in verdicts if v.get("overall", "PASS").upper() == "PASS")
    winner = "FAIL" if fail_count > pass_count else "PASS"

    merged = {
        "overall": winner,
        "votes": {"pass": pass_count, "fail": fail_count, "total": len(verdicts)},
    }

    for dim in DIMENSIONS:
        dim_verdicts = []
        dim_reasons = []
        for v in verdicts:
            if isinstance(v.get(dim), dict):
                verd = v[dim].get("verdict", "PASS").upper()
                dim_verdicts.append(verd)
                if verd == "FAIL":
                    dim_reasons.append(v[dim].get("reason", ""))
        dim_fail_count = sum(1 for d in dim_verdicts if d == "FAIL")
        merged[dim] = {
            "verdict": "FAIL" if dim_fail_count > len(verdicts) // 2 else "PASS",
            "fail_reasons": dim_reasons,
        }

    # Pick best summary from a FAIL vote if overall is FAIL, else from PASS vote
    for v in verdicts:
        if v.get("overall", "").upper() == winner and v.get("summary"):
            merged["summary"] = v["summary"]
            break

    return merged


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(results_path: str, workers: int, num_votes: int):
    logger.info(f"Loading results from {results_path}")
    with open(results_path, encoding="utf-8") as f:
        instances = json.load(f)

    total = len(instances)
    logger.info(f"Total instances: {total}")

    # Build vote tasks
    vote_tasks = []
    skip_indices = set()
    for i, inst in enumerate(instances):
        messages = _build_judge_messages(inst)
        if messages is None:
            skip_indices.add(i)
            instances[i]["test_quality"] = {"overall": "SKIP", "reason": "empty test_patch or patch"}
            continue
        for v in range(num_votes):
            vote_tasks.append((i, v, messages))

    logger.info(f"Submitting {len(vote_tasks)} vote tasks for {total - len(skip_indices)} instances ({len(skip_indices)} skipped)")

    # Collect votes
    all_votes: dict[int, list[dict]] = {i: [] for i in range(total) if i not in skip_indices}
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
            if done_votes % 20 == 0 or done_votes == total_votes:
                logger.info(f"Votes completed: {done_votes}/{total_votes}")
    except KeyboardInterrupt:
        logger.info("Interrupted -- cancelling...")
        for f in futures:
            f.cancel()
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    # Aggregate votes
    for idx, votes in all_votes.items():
        if not votes:
            continue
        verdict = _majority_vote(votes)
        verdict["instance_id"] = instances[idx].get("instance_id", "")
        instances[idx]["test_quality"] = verdict

    # Summary counts
    pass_count = sum(1 for inst in instances if inst.get("test_quality", {}).get("overall") == "PASS")
    fail_count = sum(1 for inst in instances if inst.get("test_quality", {}).get("overall") == "FAIL")
    skip_count = sum(1 for inst in instances if inst.get("test_quality", {}).get("overall") == "SKIP")
    unknown_count = total - pass_count - fail_count - skip_count

    # Write verified-pass subset with pass_count in filename
    output_dir = os.path.dirname(results_path) or "."
    base_name = os.path.basename(results_path)
    # Replace the instance count in the filename (e.g. _80_ -> _<pass_count>_)
    # Pattern: results_v1_gpt_5_2_80_20260306.json -> results_v1_gpt_5_2_<pass>_20260306_verified.json
    name_no_ext, ext = os.path.splitext(base_name)
    passed_name = re.sub(r'_(\d+)_(\d{8})', f'_{pass_count}_\\2_verified', name_no_ext)
    passed_path = os.path.join(output_dir, f"{passed_name}{ext}")
    passed_instances = [inst for inst in instances if inst.get("test_quality", {}).get("overall") == "PASS"]
    with open(passed_path, "w", encoding="utf-8") as f:
        json.dump(passed_instances, f, indent=2, ensure_ascii=False)
    logger.info(f"Wrote {pass_count} verified-pass instances to {passed_path}")

    logger.info(f"Results: {pass_count} PASS, {fail_count} FAIL, {skip_count} SKIP, {unknown_count} UNKNOWN out of {total}")

    # Per-dimension breakdown
    for dim in DIMENSIONS:
        dim_fail = sum(
            1 for inst in instances
            if isinstance(inst.get("test_quality", {}).get(dim), dict)
            and inst["test_quality"][dim].get("verdict") == "FAIL"
        )
        logger.info(f"  {dim.upper():12s}: {dim_fail} FAIL")

    # Print failing instances for quick review
    if fail_count > 0:
        logger.info("--- Failing instances ---")
        for inst in instances:
            tq = inst.get("test_quality", {})
            if tq.get("overall") != "FAIL":
                continue
            iid = inst.get("instance_id", "?")
            failed_dims = [d for d in DIMENSIONS
                          if isinstance(tq.get(d), dict) and tq[d].get("verdict") == "FAIL"]
            summary = tq.get("summary", "")
            logger.info(f"  {iid}: dims={failed_dims} | {summary}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify test case quality in SWE-bench results using LLM judges")
    parser.add_argument("results_json", help="Path to results JSON file")
    parser.add_argument("--workers", type=int, default=20, help="Number of concurrent LLM calls (default: 20)")
    parser.add_argument("--num-votes", type=int, default=3, help="Number of LLM judges per instance (default: 3)")
    args = parser.parse_args()

    main(args.results_json, args.workers, args.num_votes)
