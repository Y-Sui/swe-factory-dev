#!/usr/bin/env python3
"""
verify_and_fix_test_quality.py

Quality verification + repair loop for generated test cases in SWE-bench results.
Modeled after filter_ps_quality.py: uses LLM majority vote to judge test quality,
then regenerates failing tests with quality feedback, re-validates via Docker F2P,
and repeats up to N rounds.

Pipeline per round:
  1. JUDGE: LLM majority vote on 5 quality dimensions (breadth, narrowness, weakness, leakage, relevance)
  2. Identify FAILing instances
  3. REPAIR: For each failing instance:
     a. Format quality feedback + F2P context
     b. Call LLM to regenerate test files
     c. Generate new eval.sh
     d. Run Docker F2P validation (pre-patch + post-patch)
     e. If F2P passes → update instance with new test_patch, FAIL_TO_PASS, PASS_TO_PASS
  4. Re-judge repaired instances (next round)

Usage: python3 scripts/verify_and_fix_test_quality.py --results_json internal-swe-bench-data/results_v1_gpt_5_2_90_20260307.json --data-dir internal-swe-bench-data --setup-name setup_output_gpt-5.2 --workers 20 --num-votes 3 --max-retries 3
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from os.path import join as pjoin
from pathlib import Path
from typing import Any

from openai import OpenAI, RateLimitError

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from app.agents.write_test_agent.write_test_utils import (
    build_patch_from_files,
    extract_test_files_from_response,
)
from app.agents.write_eval_script_agent.write_eval_script_utils import (
    extract_eval_script_from_response,
)
from app.prompts.prompts import _REPO_ENV_CONFIG
from swe_factory_utils import classify_f2p, ensure_essentials_in_dockerfile, extract_exit_code

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

MODEL_NAME = "anthropic/claude-sonnet-4.5"
REPAIR_MODEL_NAME = "anthropic/claude-opus-4.6"
MAX_TOKENS_JUDGE = 2048
MAX_TOKENS_REPAIR = 32768
F2P_TEST_TIMEOUT = 300

DIMENSIONS = ("breadth", "narrowness", "weakness", "leakage", "relevance")

# Process-level Docker image cache: key = "repo__version" or "repo__instance_id",
# value = {"image_name": str, "dockerfile_hash": str}
# Avoids rebuilding the same image for instances that share the same Dockerfile.
import hashlib
import threading
_image_cache: dict[str, dict] = {}
_image_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Judge prompts (from verify_test_quality.py)
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
# Repair prompts
# ---------------------------------------------------------------------------

REPAIR_SYSTEM = """You are a senior Test-Repair Agent for SWE-bench. A previous test generation passed F2P validation (tests fail pre-patch, pass post-patch) but FAILED a quality review. Your job is to fix the quality issues while STRICTLY maintaining F2P and P2P behavior.

You will receive:
1. The problem statement (issue description)
2. The gold patch (the code fix)
3. The PREVIOUSLY generated test file(s) that failed quality review
4. The quality review feedback (which dimensions failed and why)
5. The Dockerfile and eval.sh used to build/run the test environment

## Your goal
Produce NEW test files that satisfy BOTH requirements:

### Requirement 1: F2P/P2P behavior (NON-NEGOTIABLE)
- **FAIL_TO_PASS (F2P)** tests MUST FAIL at base_commit (before gold patch) and PASS after the gold patch is applied. This is the most critical requirement. If repaired tests no longer fail pre-patch, the entire instance becomes worthless.
- **PASS_TO_PASS (P2P)** tests MUST PASS both before and after the gold patch, serving as regression guards.
- Think carefully about WHY each F2P test will fail before the patch. For bug fixes, the failure should be an assertion error from the buggy behavior. For feature additions, the failure should be ImportError/AttributeError because the new API doesn't exist yet.

### Requirement 2: Quality improvements
- Fix ALL quality issues identified in the review feedback

## Quality dimensions to fix

### BREADTH (tests too loose)
Fix: Write specific assertions that verify the exact behavior the patch changes. Use concrete expected values, not just "is not None" or type checks.

### NARROWNESS (tests overfitted to gold patch)
Fix: Test observable behavior and outputs, not internal implementation. Allow alternative valid fixes to also pass.

### WEAKNESS (F2P fails for wrong reason)
Fix: For bug fixes, ensure pre-patch failure is a meaningful assertion error related to the bug, not an ImportError or setup issue. For features, ImportError/AttributeError for missing feature is fine.

### LEAKAGE (tests reveal too much about the fix)
Fix: Remove any assertions that encode the complete solution logic. Don't inspect source code or AST. Test behavior, not implementation details. For new features, referencing new API names is acceptable.

### RELEVANCE (tests don't match problem statement)
Fix: Ensure tests exercise the specific behavior described in the problem statement, not unrelated functionality.

{repo_env_guidance}

## Critical: Maintain F2P and P2P behavior
The repaired tests MUST still satisfy the F2P/P2P pattern:
- **FAIL_TO_PASS (F2P)**: These tests MUST FAIL at base_commit (before the gold patch) and PASS after the gold patch is applied. This is the primary signal that validates the fix. If your repaired tests no longer fail pre-patch, they are useless regardless of quality improvements.
  - For BUG FIXES: F2P tests should trigger a meaningful assertion failure related to the bug (e.g., wrong return value, incorrect behavior).
  - For FEATURE ADDITIONS: F2P tests should fail with ImportError, AttributeError, TypeError, etc. because the feature does not exist yet.
- **PASS_TO_PASS (P2P)**: Regression tests that PASS both before and after the gold patch, verifying the fix doesn't break existing behavior.

When fixing quality issues, do NOT weaken the tests to the point where they pass pre-patch. The F2P signal is non-negotiable.

## Rules
- Classify first: is the patch a bug fix or new feature?
- No over-mocking: never mock the function/class the patch is fixing; only mock external I/O
- Concrete assertions: `assert result == specific_value`, not `assert result` or `assert result is not None`
- Import paths: use the module path relative to the package root
- NEVER simulate patch logic inline — your test MUST exercise the ACTUAL source code from the repository
- Produce 3-4 F2P tests and 3-4 P2P tests
- Clearly separate F2P and P2P tests with comments in the output

### Output Format
Return each test file wrapped in `<test_file path="...">` tags.

Example:
```
<test_file path="tests/test_my_fix.py">
import pytest
from mymodule import my_function

# --- F2P: tests that should fail before patch, pass after ---
def test_f2p_returns_correct_value():
    result = my_function(42)
    assert result == expected_value

# --- P2P: regression tests that should always pass ---
def test_p2p_basic_behavior():
    result = my_function(0)
    assert result == 0
</test_file>
```"""

REPAIR_USER = """## Instance Info
- **Instance ID**: `{instance_id}`
- **Base commit**: `{base_commit}`

## Problem Statement
```
{problem_statement}
```

## Gold Patch
```diff
{patch}
```

## Previously Generated Test Files (FAILED quality review but PASSED F2P)
{previous_test_files}

## Quality Review Feedback (what needs fixing)
{quality_feedback}

## Current Dockerfile
```dockerfile
{dockerfile}
```

## Current eval.sh
```bash
{eval_script}
```

---

**Your task**: Fix the quality issues identified above while strictly maintaining F2P/P2P behavior.

Work through these steps:
1. **Quality issues**: Read the feedback carefully. Which dimensions failed and why?
2. **Previous tests**: What specifically is wrong with them according to the quality review?
3. **Patch type**: Is this a bug fix or feature addition? This determines how F2P tests should fail pre-patch.
4. **Fix strategy**: For each failed dimension, what concrete changes will fix it WITHOUT breaking the F2P pattern?
5. **F2P preservation check**: For each F2P test you write, explain WHY it will fail at base_commit (before patch) and pass after. If you cannot articulate this clearly, the test is wrong.
6. **P2P check**: For each P2P test, confirm it tests existing behavior that is NOT changed by the patch.

Then generate CORRECTED test files using `<test_file path="...">` tags.
Mark each test function with a comment: `# F2P` or `# P2P`."""

EVAL_SCRIPT_REGEN_PROMPT = """Generate an eval.sh script for the following test files in a SWE-bench evaluation environment.

## Repository
- **Repo**: {repo}
- **Instance ID**: {instance_id}

## Test Files to Run
{test_file_list}

## Test File Contents
{test_file_contents}

{repo_env_guidance}

Generate the eval.sh script wrapped in `<script>` tags. The script MUST:
1. Use `set -uxo pipefail`
2. cd to the correct working directory
3. Write each test file using `cat <<'EOF_TEST_N' > "path"` heredocs
4. Run pytest with `--override-ini="addopts="`
5. Capture exit code and echo `OMNIGRIL_EXIT_CODE=$rc`
"""


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def _make_client() -> OpenAI:
    return OpenAI(
        api_key=os.environ["OPENAI_KEY"],
        base_url=os.environ.get("OPENAI_API_BASE_URL"),
        timeout=300,
    )


def _call_llm(client: OpenAI, messages: list[dict], max_tokens: int, model: str = MODEL_NAME) -> str:
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=model,
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

    for v in verdicts:
        if v.get("overall", "").upper() == winner and v.get("summary"):
            merged["summary"] = v["summary"]
            break

    return merged


def _format_quality_feedback(verdict: dict) -> str:
    """Format quality verdict into structured feedback for repair prompt."""
    lines = []
    for dim in DIMENSIONS:
        info = verdict.get(dim, {})
        if info.get("verdict", "PASS").upper() == "FAIL":
            reasons = info.get("fail_reasons", [])
            reason_str = "; ".join(reasons) if reasons else "no detail"
            lines.append(f"- **{dim.upper()}**: FAIL — {reason_str}")
    summary = verdict.get("summary", "")
    if summary:
        lines.append(f"\nOverall: {summary}")
    return "\n".join(lines) if lines else "No specific failures identified."


# ---------------------------------------------------------------------------
# Docker F2P validation (adapted from post_fix_failed_cases.py)
# ---------------------------------------------------------------------------

def _run_f2p_validation(
    output_dir: str,
    instance_id: str,
    base_commit: str,
    gold_patch: str,
    repo: str,
    dockerfile_content: str,
    eval_script_path: str,
) -> dict[str, Any]:
    """Build Docker image and run pre-patch/post-patch F2P validation."""
    import docker
    from docker import errors as docker_errors
    from app.agents.test_analysis_agent.docker_utils import (
        build_container,
        cleanup_container,
        copy_to_container,
        exec_run_with_timeout,
        remove_image,
    )

    result = {
        "classification": "ERROR",
        "pre_exit_code": None,
        "post_exit_code": None,
        "pre_test_output": "",
        "post_test_output": "",
        "error": None,
    }

    client = docker.from_env()
    container_name = f"tqfix-{instance_id}-test".lower().replace("__", "-")
    container = None

    try:
        processed_dockerfile = ensure_essentials_in_dockerfile(dockerfile_content)

        buildargs = {}
        github_token = os.environ.get("GITHUB_TOKEN", "").strip()
        if github_token and "github.com" in processed_dockerfile:
            if "ARG GITHUB_TOKEN" not in processed_dockerfile:
                lines = processed_dockerfile.split("\n")
                new_lines = []
                for line in lines:
                    new_lines.append(line)
                    if line.strip().upper().startswith("FROM "):
                        new_lines.append("ARG GITHUB_TOKEN")
                processed_dockerfile = "\n".join(new_lines)
            processed_dockerfile = processed_dockerfile.replace(
                "https://github.com/",
                "https://x-access-token:${GITHUB_TOKEN}@github.com/"
            )
            buildargs["GITHUB_TOKEN"] = github_token

        # Cache key: hash of the processed Dockerfile content (same Dockerfile → same image)
        df_hash = hashlib.md5(processed_dockerfile.encode()).hexdigest()[:12]
        cache_key = f"{repo}__{df_hash}"
        image_name = None

        # Check cache: reuse existing image if Dockerfile hasn't changed
        with _image_cache_lock:
            cached = _image_cache.get(cache_key)
            if cached:
                cached_image_name = cached["image_name"]
                try:
                    client.images.get(cached_image_name)
                    image_name = cached_image_name
                    logger.info(f"[{instance_id}] Reusing cached Docker image {image_name}")
                except docker.errors.ImageNotFound:
                    logger.info(f"[{instance_id}] Cached image {cached_image_name} gone, rebuilding")

        # Build if not cached
        if image_name is None:
            image_name = f"tqfix-{instance_id}:latest".lower().replace("__", "-")

            build_ctx = pjoin(output_dir, "docker_build")
            os.makedirs(build_ctx, exist_ok=True)
            with open(pjoin(build_ctx, "Dockerfile"), "w") as f:
                f.write(processed_dockerfile)

            build_log_lines = []
            response = client.api.build(
                path=build_ctx,
                tag=image_name,
                rm=True,
                forcerm=True,
                decode=True,
                platform="linux/x86_64",
                nocache=False,
                buildargs=buildargs or None,
            )
            for chunk in response:
                if "stream" in chunk:
                    line = chunk["stream"].strip()
                    if line:
                        build_log_lines.append(line)
                if "error" in chunk:
                    err_msg = chunk["error"].strip()
                    build_log_lines.append(f"ERROR: {err_msg}")
                    raise docker_errors.BuildError(err_msg, iter(build_log_lines))

            with open(pjoin(output_dir, "build_image.log"), "w") as f:
                f.write("\n".join(build_log_lines))

            # Save to cache
            with _image_cache_lock:
                _image_cache[cache_key] = {
                    "image_name": image_name,
                    "dockerfile_hash": df_hash,
                }
            logger.info(f"[{instance_id}] Built and cached Docker image {image_name}")

        class SimpleLogger:
            def __init__(self, path):
                self.log_file = path
                self._f = open(path, "w")
            def info(self, msg):
                self._f.write(msg + "\n")
                self._f.flush()
            def error(self, msg):
                self._f.write(f"ERROR: {msg}\n")
                self._f.flush()
            def close(self):
                self._f.close()

        run_log_path = pjoin(output_dir, "run_test.log")
        run_logger = SimpleLogger(run_log_path)

        def _get_clean_command(repo: str) -> str:
            if repo == "MiroMindAI/miroflow":
                return "git clean -fdx -e .venv"
            if repo == "MiroMindAI/MiroThinker":
                return "git clean -fdx -e .venv -e apps/miroflow-agent/.venv"
            return "git clean -fdx"

        try:
            container = build_container(
                client, image_name, container_name, instance_id, run_logger
            )
            container.start()

            eval_local = Path(eval_script_path)
            copy_to_container(container, eval_local, Path("/eval.sh"))

            # Pre-patch
            container.exec_run(f"git reset --hard {base_commit}", workdir="/testbed", user="root")
            clean_cmd = _get_clean_command(repo)
            container.exec_run(clean_cmd, workdir="/testbed", user="root")

            pre_output_raw = exec_run_with_timeout(container, "/bin/bash /eval.sh", timeout=F2P_TEST_TIMEOUT)
            pre_output = pre_output_raw.decode("utf-8") if pre_output_raw else ""
            result["pre_test_output"] = pre_output
            result["pre_exit_code"] = extract_exit_code(pre_output)

            with open(pjoin(output_dir, "test_output_prev_apply.txt"), "w") as f:
                f.write(pre_output)

            # Post-patch
            container.exec_run(f"git reset --hard {base_commit}", workdir="/testbed", user="root")
            container.exec_run(clean_cmd, workdir="/testbed", user="root")

            patch_local = Path(pjoin(output_dir, "gold_patch.diff"))
            patch_local.write_text(gold_patch)
            copy_to_container(container, patch_local, Path("/tmp/patch.diff"))

            apply_result = container.exec_run(
                "git apply -p1 -v /tmp/patch.diff", workdir="/testbed", user="root"
            )
            if apply_result.exit_code != 0:
                apply_result = container.exec_run(
                    "patch --batch --fuzz=5 -p1 -i /tmp/patch.diff", workdir="/testbed", user="root"
                )
                if apply_result.exit_code != 0:
                    result["error"] = "Failed to apply gold patch"
                    return result

            copy_to_container(container, eval_local, Path("/eval.sh"))

            post_output_raw = exec_run_with_timeout(container, "/bin/bash /eval.sh", timeout=F2P_TEST_TIMEOUT)
            post_output = post_output_raw.decode("utf-8") if post_output_raw else ""
            result["post_test_output"] = post_output
            result["post_exit_code"] = extract_exit_code(post_output)

            with open(pjoin(output_dir, "test_output.txt"), "w") as f:
                f.write(post_output)

            result["classification"] = classify_f2p(result["pre_exit_code"], result["post_exit_code"])

        finally:
            if container:
                try:
                    cleanup_container(client, container, run_logger)
                except Exception:
                    pass
            run_logger.close()

        # Don't remove cached images — they will be reused by other instances

    except Exception as e:
        result["error"] = f"Docker validation error: {e}"
        logger.warning(f"[{instance_id}] Docker error: {e}")
        logger.debug(traceback.format_exc())

    return result


# ---------------------------------------------------------------------------
# Eval script generation helpers
# ---------------------------------------------------------------------------

def _get_repo_env_guidance(repo: str) -> str:
    config = _REPO_ENV_CONFIG.get(repo)
    if config:
        return f"\n## Repository Environment\n{config[2]}"
    return ""


def _generate_fallback_eval_script(
    output_dir: str, repo: str, test_files: dict[str, str]
) -> str:
    if repo == "MiroMindAI/miroflow":
        workdir = "/testbed"
        pytest_bin = ".venv/bin/pytest"
    elif repo == "MiroMindAI/MiroThinker":
        workdir = "/testbed/apps/miroflow-agent"
        pytest_bin = ".venv/bin/pytest"
    else:
        workdir = "/testbed"
        pytest_bin = "pytest"

    lines = [
        "#!/bin/bash",
        "set -uxo pipefail",
        f"cd {workdir}",
    ]

    dirs_created: set[str] = set()
    for i, (path, content) in enumerate(test_files.items()):
        parent = os.path.dirname(path)
        if parent and parent not in dirs_created:
            lines.append(f"mkdir -p {parent}")
            dirs_created.add(parent)
        lines.append(f"cat <<'EOF_TEST_{i}' > \"{path}\"")
        lines.append(content)
        lines.append(f"EOF_TEST_{i}")

    if "venv" in pytest_bin:
        lines.append(f'if [ ! -x "{pytest_bin}" ]; then')
        lines.append("  uv pip install pytest pytest-asyncio")
        lines.append("fi")

    test_paths = " ".join(f'"{p}"' for p in test_files.keys())
    lines.append(f'{pytest_bin} {test_paths} -v --override-ini="addopts="')
    lines.append("rc=$?")
    lines.append('echo "OMNIGRIL_EXIT_CODE=$rc"')

    script_content = "\n".join(lines) + "\n"
    eval_path = pjoin(output_dir, "eval.sh")
    with open(eval_path, "w") as f:
        f.write(script_content)
    return eval_path


# ---------------------------------------------------------------------------
# Repair logic for a single instance
# ---------------------------------------------------------------------------

def _repair_one(args: tuple) -> tuple[int, dict]:
    """Repair a single instance that failed quality review.

    Returns (idx, repair_result) where repair_result contains:
      - success: bool
      - new_test_files: dict[str, str]
      - new_test_patch: str
      - f2p_result: dict (from Docker validation) or None
      - quality_feedback: str (what was wrong)
    """
    idx, inst, quality_feedback, setup_dir, skip_docker = args
    instance_id = inst.get("instance_id", f"idx_{idx}")
    repo = inst.get("repo", "")

    repair_result: dict[str, Any] = {
        "success": False,
        "new_test_files": {},
        "new_test_patch": "",
        "new_eval_script": "",
        "f2p_result": None,
        "quality_feedback": quality_feedback,
    }

    # Collect context for repair
    # Find Dockerfile and eval.sh from setup dir
    inst_dir = pjoin(setup_dir, "applicable_setup", instance_id) if setup_dir else ""
    dockerfile = inst.get("dockerfile", "")
    eval_script = inst.get("eval_script", "")

    if inst_dir and os.path.isdir(inst_dir):
        df_path = pjoin(inst_dir, "Dockerfile")
        if os.path.exists(df_path) and not dockerfile:
            with open(df_path) as f:
                dockerfile = f.read()
        es_path = pjoin(inst_dir, "eval.sh")
        if os.path.exists(es_path) and not eval_script:
            with open(es_path) as f:
                eval_script = f.read()

    if not dockerfile:
        logger.warning(f"[{idx}] No Dockerfile found for {instance_id}, skipping repair")
        return idx, repair_result

    # Format previous test files from test_patch
    test_patch = inst.get("test_patch", "")
    previous_test_files_str = f"```diff\n{test_patch}\n```" if test_patch else "(no previous test files)"

    repo_env_guidance = _get_repo_env_guidance(repo)

    fmt = {
        "instance_id": instance_id,
        "base_commit": inst.get("base_commit", ""),
        "problem_statement": inst.get("problem_statement", ""),
        "patch": inst.get("patch", ""),
        "previous_test_files": previous_test_files_str,
        "quality_feedback": quality_feedback,
        "dockerfile": dockerfile,
        "eval_script": eval_script,
    }

    client = _make_client()
    try:
        # 1. Call LLM to regenerate test files
        system_prompt = REPAIR_SYSTEM.format(repo_env_guidance=repo_env_guidance)
        response_text = _call_llm(client, [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": REPAIR_USER.format(**fmt)},
        ], MAX_TOKENS_REPAIR, model=REPAIR_MODEL_NAME)

        # 2. Extract test files
        new_test_files = extract_test_files_from_response(response_text)
        if not new_test_files:
            logger.warning(f"[{idx}] No test files extracted from repair response")
            return idx, repair_result

        repair_result["new_test_files"] = new_test_files
        logger.info(f"[{idx}] Extracted {len(new_test_files)} test file(s): {list(new_test_files.keys())}")

        # 3. Build test_patch
        output_dir = pjoin(inst_dir, "quality_repair") if inst_dir else ""
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            # Save LLM response
            with open(pjoin(output_dir, "repair_llm_response.txt"), "w") as f:
                f.write(response_text)

        new_test_patch, _ = build_patch_from_files(new_test_files, output_dir or "/tmp")
        repair_result["new_test_patch"] = new_test_patch

        # 4. Generate eval.sh
        test_file_list = "\n".join(f"- `{p}`" for p in new_test_files.keys())
        test_contents_str = "\n\n".join(
            f"### `{path}`\n```python\n{content}\n```"
            for path, content in new_test_files.items()
        )
        eval_user = EVAL_SCRIPT_REGEN_PROMPT.format(
            repo=repo,
            instance_id=instance_id,
            test_file_list=test_file_list,
            test_file_contents=test_contents_str,
            repo_env_guidance=repo_env_guidance,
        )
        eval_response = _call_llm(client, [
            {"role": "system", "content": "You are an expert at writing evaluation scripts for SWE-bench."},
            {"role": "user", "content": eval_user},
        ], MAX_TOKENS_REPAIR, model=REPAIR_MODEL_NAME)

        eval_script_path = ""
        if output_dir:
            with open(pjoin(output_dir, "eval_llm_response.txt"), "w") as f:
                f.write(eval_response)

            eval_extracted = extract_eval_script_from_response(
                eval_response, output_dir, new_test_patch,
                test_files_content=new_test_files,
            )
            if not eval_extracted:
                eval_script_path = _generate_fallback_eval_script(output_dir, repo, new_test_files)
            else:
                eval_script_path = pjoin(output_dir, "eval.sh")

            with open(eval_script_path) as f:
                repair_result["new_eval_script"] = f.read()

        # 5. Docker F2P validation
        if not skip_docker and output_dir and eval_script_path:
            base_commit = inst.get("base_commit", "")
            gold_patch = inst.get("patch", "")

            if base_commit and gold_patch and dockerfile:
                logger.info(f"[{idx}] Running Docker F2P validation for {instance_id}...")
                f2p_result = _run_f2p_validation(
                    output_dir=output_dir,
                    instance_id=instance_id,
                    base_commit=base_commit,
                    gold_patch=gold_patch,
                    repo=repo,
                    dockerfile_content=dockerfile,
                    eval_script_path=eval_script_path,
                )
                repair_result["f2p_result"] = f2p_result
                logger.info(
                    f"[{idx}] F2P result: {f2p_result['classification']} "
                    f"(pre={f2p_result['pre_exit_code']}, post={f2p_result['post_exit_code']})"
                )

                if f2p_result["classification"] != "FAIL2PASS":
                    logger.warning(
                        f"[{idx}] Repaired tests did not achieve F2P: {f2p_result['classification']}"
                    )
                    return idx, repair_result
            else:
                logger.warning(f"[{idx}] Missing base_commit/patch/dockerfile, skipping Docker validation")

        repair_result["success"] = True
        return idx, repair_result

    except Exception as e:
        logger.warning(f"[{idx}] Repair failed: {e}")
        logger.debug(traceback.format_exc())
        return idx, repair_result


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def main(
    results_path: str,
    data_dir: str,
    setup_name: str,
    workers: int,
    num_votes: int,
    max_retries: int,
    skip_docker: bool,
):
    logger.info(f"Loading results from {results_path}")
    with open(results_path, encoding="utf-8") as f:
        instances = json.load(f)

    total = len(instances)
    logger.info(f"Total instances: {total}")

    for retry_round in range(max_retries + 1):
        label = "Initial quality check" if retry_round == 0 else f"Re-check round {retry_round}"
        logger.info(f"--- {label} ---")

        # Determine which instances need checking
        if retry_round == 0:
            check_indices = list(range(total))
        else:
            check_indices = [
                i for i in range(total)
                if instances[i].get("test_quality", {}).get("overall", "PASS").upper() == "FAIL"
            ]

        if not check_indices:
            logger.info("All instances passed quality check.")
            break

        # ---- JUDGE PHASE ----
        vote_tasks = []
        skip_indices = set()
        for i in check_indices:
            messages = _build_judge_messages(instances[i])
            if messages is None:
                skip_indices.add(i)
                instances[i]["test_quality"] = {"overall": "SKIP", "reason": "empty test_patch or patch"}
                continue
            for v in range(num_votes):
                vote_tasks.append((i, v, messages))

        logger.info(
            f"Submitting {len(vote_tasks)} vote tasks for "
            f"{len(check_indices)} instances ({len(skip_indices)} skipped)"
        )

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
                    logger.warning(f"Unexpected vote error: {e}")
                done_votes += 1
                if done_votes % 20 == 0 or done_votes == total_votes:
                    logger.info(f"Votes completed: {done_votes}/{total_votes}")
        except KeyboardInterrupt:
            logger.info("Interrupted -- cancelling...")
            for f in futures:
                f.cancel()
            break
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        # Aggregate votes
        fail_indices = []
        for idx, votes in all_votes.items():
            if not votes:
                continue
            verdict = _majority_vote(votes)
            verdict["instance_id"] = instances[idx].get("instance_id", "")
            instances[idx]["test_quality"] = verdict
            if verdict["overall"] == "FAIL":
                fail_indices.append(idx)

        pass_count = len(check_indices) - len(fail_indices) - len(skip_indices)
        logger.info(f"Quality results: {pass_count} PASS, {len(fail_indices)} FAIL, {len(skip_indices)} SKIP")

        # Per-dimension breakdown
        for dim in DIMENSIONS:
            dim_fail = sum(
                1 for idx in check_indices
                if isinstance(instances[idx].get("test_quality", {}).get(dim), dict)
                and instances[idx]["test_quality"][dim].get("verdict") == "FAIL"
            )
            if dim_fail > 0:
                logger.info(f"  {dim.upper():12s}: {dim_fail} FAIL")

        if not fail_indices or retry_round >= max_retries:
            if fail_indices:
                logger.warning(f"{len(fail_indices)} instances still failing after {max_retries} retries")
            break

        # ---- REPAIR PHASE ----
        logger.info(f"Repairing {len(fail_indices)} failing instances...")
        repair_tasks = []
        for idx in fail_indices:
            feedback = _format_quality_feedback(instances[idx]["test_quality"])
            repo = instances[idx].get("repo", "")
            setup_dir = _resolve_setup_dir(data_dir, setup_name, repo) if repo else ""
            repair_tasks.append((idx, instances[idx], feedback, setup_dir, skip_docker))

        # With image caching, parallel Docker runs are safe
        repair_workers = workers
        repair_done = 0
        repair_success = 0

        executor = ThreadPoolExecutor(max_workers=repair_workers)
        futures = {executor.submit(_repair_one, task): task[0] for task in repair_tasks}
        try:
            for future in as_completed(futures):
                try:
                    idx, rr = future.result()
                    if rr["success"]:
                        # Check F2P result
                        f2p_ok = True
                        if rr["f2p_result"]:
                            f2p_ok = rr["f2p_result"]["classification"] == "FAIL2PASS"

                        if f2p_ok and rr["new_test_patch"]:
                            # Update instance with repaired test data
                            instances[idx]["test_patch"] = rr["new_test_patch"]
                            if rr.get("new_eval_script"):
                                instances[idx]["eval_script"] = rr["new_eval_script"]
                            # Clear old quality verdict so it gets re-judged next round
                            instances[idx]["test_quality"] = {"overall": "FAIL", "note": "repaired, pending re-judge"}
                            repair_success += 1
                            logger.info(f"[{idx}] Repair successful, pending re-judge")
                        else:
                            f2p_cls = rr["f2p_result"]["classification"] if rr["f2p_result"] else "N/A"
                            logger.warning(f"[{idx}] Repair failed F2P: {f2p_cls}")
                except Exception as e:
                    logger.warning(f"Repair error: {e}")
                repair_done += 1
                if repair_done % 5 == 0 or repair_done == len(repair_tasks):
                    logger.info(f"Repaired {repair_done}/{len(repair_tasks)} ({repair_success} successful)")
        except KeyboardInterrupt:
            logger.info("Interrupted -- cancelling...")
            for f in futures:
                f.cancel()
            break
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        logger.info(f"Repair round {retry_round + 1}: {repair_success}/{len(fail_indices)} repaired successfully")

    # ---- Write output ----
    # Write verified-pass subset
    output_dir = os.path.dirname(results_path) or "."
    base_name = os.path.basename(results_path)
    name_no_ext, ext = os.path.splitext(base_name)

    pass_count = sum(1 for inst in instances if inst.get("test_quality", {}).get("overall") == "PASS")
    fail_count = sum(1 for inst in instances if inst.get("test_quality", {}).get("overall") == "FAIL")
    skip_count = sum(1 for inst in instances if inst.get("test_quality", {}).get("overall") == "SKIP")

    # Write full results with quality annotations
    full_output_path = os.path.join(output_dir, f"{name_no_ext}_quality_checked{ext}")
    with open(full_output_path, "w", encoding="utf-8") as f:
        json.dump(instances, f, indent=2, ensure_ascii=False)
    logger.info(f"Wrote full results to {full_output_path}")

    # Write verified-pass subset
    passed_name = re.sub(r'_(\d+)_(\d{8})', f'_{pass_count}_\\2_verified', name_no_ext)
    passed_path = os.path.join(output_dir, f"{passed_name}{ext}")
    passed_instances = [inst for inst in instances if inst.get("test_quality", {}).get("overall") == "PASS"]
    with open(passed_path, "w", encoding="utf-8") as f:
        json.dump(passed_instances, f, indent=2, ensure_ascii=False)
    logger.info(f"Wrote {pass_count} verified-pass instances to {passed_path}")

    logger.info(f"Final: {pass_count} PASS, {fail_count} FAIL, {skip_count} SKIP out of {total}")

    # Per-dimension breakdown
    for dim in DIMENSIONS:
        dim_fail = sum(
            1 for inst in instances
            if isinstance(inst.get("test_quality", {}).get(dim), dict)
            and inst["test_quality"][dim].get("verdict") == "FAIL"
        )
        logger.info(f"  {dim.upper():12s}: {dim_fail} FAIL")

    # Print failing instances
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


def _resolve_setup_dir(data_dir: str, setup_name: str, repo: str) -> str:
    """Resolve setup_output directory for a given repo.

    Convention: <data_dir>/<repo_with__>/<setup_name>/
    e.g. internal-swe-bench-data/MiroMindAI__miroflow/setup_output_gpt-5.2/
    """
    repo_dir_name = repo.replace("/", "__")
    return pjoin(data_dir, repo_dir_name, setup_name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Verify test quality and repair failing tests with Docker F2P validation"
    )
    parser.add_argument("--results_json", help="Path to results JSON file")
    parser.add_argument(
        "--data-dir", required=True,
        help="Root data directory containing repo subdirs (e.g. internal-swe-bench-data)"
    )
    parser.add_argument(
        "--setup-name", default="setup_output_gpt-5.2",
        help="Name of setup_output subdirectory within each repo dir (default: setup_output_gpt-5.2)"
    )
    parser.add_argument("--workers", type=int, default=10, help="Concurrent LLM calls (default: 10)")
    parser.add_argument("--num-votes", type=int, default=3, help="LLM judges per instance (default: 3)")
    parser.add_argument("--max-retries", type=int, default=3, help="Max repair rounds (default: 3)")
    parser.add_argument("--skip-docker", action="store_true", help="Skip Docker F2P validation during repair")
    args = parser.parse_args()

    main(
        args.results_json,
        args.data_dir,
        args.setup_name,
        args.workers,
        args.num_votes,
        args.max_retries,
        args.skip_docker,
    )
