"""
Post-fix failed test generation cases by calling ClaudeOpus4_6 for one round of repair,
then validate the new tests via Docker-based F2P testing.

Pipeline per instance:
  1. Collect context (problem statement, gold patch, previous test files, logs, feedback)
  2. Call ClaudeOpus4_6 to regenerate test files
  3. Generate new eval.sh (LLM + fallback)
  4. Build Docker image & run F2P validation (pre-patch → post-patch)
  5. Report classification result with clear colored output

Usage:
    python scripts/post_fix_failed_cases.py \
        --setup-dir internal-swe-bench-data/MiroMindAI__miroflow/setup_output_2026-03-03 \
        --instances-jsonl internal-swe-bench-data/MiroMindAI__miroflow/instances_selected_36.jsonl \
        --num-processes 5 \
        [--instance INSTANCE_ID]  # optional: run only one instance
        [--skip-docker]           # optional: skip Docker F2P validation
"""

import argparse
import json
import os
import sys
import threading
import tempfile
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from os.path import join as pjoin
from pathlib import Path
from typing import Any

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# Load .env file
from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from loguru import logger

from app.agents.write_test_agent.write_test_utils import (
    build_patch_from_files,
    extract_test_files_from_response,
)
from app.agents.write_eval_script_agent.write_eval_script_utils import (
    extract_eval_script_from_response,
)
from app.prompts.prompts import _REPO_ENV_CONFIG
from swe_factory_utils import classify_f2p, ensure_essentials_in_dockerfile, extract_exit_code

# ---------------------------------------------------------------------------
# Colored output helpers
# ---------------------------------------------------------------------------

class Colors:
    BOLD = "\033[1m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    RESET = "\033[0m"
    DIM = "\033[2m"


# Lock for thread-safe printing
_print_lock = threading.Lock()


def _tprint(*args, **kwargs):
    """Thread-safe print."""
    with _print_lock:
        print(*args, **kwargs)


def print_header(msg: str):
    _tprint(f"\n{Colors.BOLD}{Colors.CYAN}{'=' * 70}{Colors.RESET}")
    _tprint(f"{Colors.BOLD}{Colors.CYAN}  {msg}{Colors.RESET}")
    _tprint(f"{Colors.BOLD}{Colors.CYAN}{'=' * 70}{Colors.RESET}")


def print_step(instance_id: str, step: int, msg: str):
    _tprint(f"  {Colors.BLUE}[{instance_id}]{Colors.RESET} Step {step}: {msg}")


def print_success(msg: str):
    _tprint(f"  {Colors.GREEN}[PASS]{Colors.RESET} {msg}")


def print_fail(msg: str):
    _tprint(f"  {Colors.RED}[FAIL]{Colors.RESET} {msg}")


def print_warn(msg: str):
    _tprint(f"  {Colors.YELLOW}[WARN]{Colors.RESET} {msg}")


def print_f2p_result(instance_id: str, classification: str, pre_exit: int | None, post_exit: int | None):
    """Print F2P classification result with color coding."""
    color = {
        "FAIL2PASS": Colors.GREEN,
        "PASS2PASS": Colors.YELLOW,
        "FAIL2FAIL": Colors.RED,
        "PASS2FAIL": Colors.MAGENTA,
        "ERROR": Colors.RED,
    }.get(classification, Colors.RED)

    symbol = {
        "FAIL2PASS": "++",
        "PASS2PASS": "--",
        "FAIL2FAIL": "XX",
        "PASS2FAIL": "!!",
        "ERROR": "??",
    }.get(classification, "??")

    pre_str = f"exit={pre_exit}" if pre_exit is not None else "N/A"
    post_str = f"exit={post_exit}" if post_exit is not None else "N/A"

    _tprint(f"  {color}{Colors.BOLD}[{symbol} {classification}]{Colors.RESET} "
            f"{Colors.BOLD}{instance_id}{Colors.RESET}  "
            f"pre-patch: {pre_str}, post-patch: {post_str}")


# ---------------------------------------------------------------------------
# Constants & Prompts
# ---------------------------------------------------------------------------

POST_FIX_DIR_NAME = "post_fix_round_1"
F2P_TEST_TIMEOUT = 300  # 5 minutes per test phase

POST_FIX_SYSTEM_PROMPT = """You are a senior Test-Repair Agent. A previous test generation attempt for a SWE-bench instance FAILED to achieve FAIL2PASS classification. Your job is to diagnose the failure and produce corrected test files in ONE shot.

You will receive:
1. The problem statement (issue description)
2. The gold patch (the code fix)
3. The PREVIOUSLY generated test file(s) that failed
4. The Dockerfile used to build the test environment
5. The eval.sh script used to run the tests
6. The pre-patch test output (tests run WITHOUT the gold patch applied)
7. The post-patch test output (tests run WITH the gold patch applied)
8. The analysis agent's diagnosis of what went wrong
9. The F2P classification result

## Your goal
Produce NEW test files that achieve FAIL2PASS:
- Tests FAIL at base_commit (before the gold patch is applied)
- Tests PASS after the gold patch is applied

## Common failure patterns and how to fix them

### FAIL2FAIL (tests fail both before and after patch)
Root causes:
- **Import errors**: wrong module paths (e.g. `from src.X` instead of `from X` for `packages=["src"]` repos)
- **Missing dependencies**: test uses a package not installed in the Docker environment
- **Wrong test setup**: incorrect mock configuration, wrong config structure, missing required env vars
- **Syntax errors**: malformed test code
- **Testing wrong behavior**: assertions don't match what the gold patch actually does

Fix strategy: Read the error in test_output.txt carefully. Fix the specific error. Don't rewrite everything — targeted fixes.

### PASS2PASS (tests pass both before and after patch)
Root causes:
- **Self-contained simulation**: test defines its own version of the function instead of importing the real one
- **Over-mocking**: the function being tested is mocked, so the real code path is never exercised
- **Weak assertions**: `assert result is not None` passes regardless of the bug
- **Testing unrelated code**: test exercises code that isn't affected by the patch

Fix strategy: Ensure tests import and call the ACTUAL repository code. Use concrete assertions with specific expected values.

### ERROR (couldn't extract exit codes)
Root causes:
- eval.sh script issues (missing OMNIGRIL_EXIT_CODE marker)
- Docker build failures

Fix strategy: Focus on test file quality; the eval.sh will be regenerated.

{repo_env_guidance}

## Rules
- **Classify first**: is the patch a bug fix (wrong output) or new feature (missing attribute/param)?
  - Bug fix → assert the EXACT correct value the fixed code returns
  - New feature → call the new API; test naturally fails pre-patch (AttributeError/TypeError) and passes post-patch
- **No over-mocking**: never mock the function/class the patch is fixing; only mock external I/O
- **Concrete assertions**: `assert result == specific_value`, not `assert result` or `assert result is not None`
- **Import paths**: use the module path relative to the package root, not to the repo root
- **`packages = ["src"]` rule**: if the repo uses this, strip `src/` from import paths
  - Example: file `.../src/core/pipeline.py` → import `from core.pipeline import ...`
  - Never import with `from src...` and never prefix imports with the repo name
- **NEVER simulate patch logic inline**: Do NOT create mock/simulated versions of the patched function inside the test file. Your test MUST exercise the ACTUAL source code from the repository.
- **Relevance**: only test code mentioned in the patch
- Apply any guidance from the analysis agent precisely

### Output Format
Return each test file as its **complete raw content** wrapped in `<test_file path="...">` tags.
Use the `path` attribute to specify where the file should be placed relative to the repo root.
Use comments to clearly mark F2P vs P2P tests.

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
```
"""

POST_FIX_USER_PROMPT = """## Instance Info
- **Instance ID**: `{instance_id}`
- **Base commit**: `{base_commit}`
- **F2P Classification**: `{f2p_classification}` (FAILED — needs repair)

## Problem Statement
```
{problem_statement}
```

## Gold Patch
```diff
{patch}
```

## Previously Generated Test File(s) (FAILED)
{previous_test_files}

## Current Dockerfile
```dockerfile
{dockerfile}
```

## Current eval.sh
```bash
{eval_script}
```

## Pre-Patch Test Output (tests run WITHOUT gold patch)
```
{test_output_pre}
```

## Post-Patch Test Output (tests run WITH gold patch)
```
{test_output_post}
```

## Analysis Agent Diagnosis
```
{analysis_feedback}
```

---

**Instructions**: Based on the failure diagnosis above, generate CORRECTED test files.
- Fix the specific issues identified in the analysis
- Read the test output carefully — the error messages tell you exactly what's wrong
- Keep working test logic; only fix what's broken
- Produce 3-4 F2P tests and 3-4 P2P tests
- Use `<test_file path="...">` tags for each file

Think step by step:
1. What is the F2P classification and what does it mean?
2. What specific errors appear in the test output?
3. What does the analysis agent say is wrong?
4. What minimal changes fix these issues?
"""

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
# Model helper
# ---------------------------------------------------------------------------

def create_model():
    """Create and setup the ClaudeOpus4_6 model instance."""
    from app.model.gpt import ClaudeOpus4_6
    from app.model import common

    model = ClaudeOpus4_6()
    common.MODEL_TEMP = 0.2
    model.setup()
    return model


def call_model(model, messages: list[dict], temperature: float = 0.2) -> str:
    """Call ClaudeOpus4_6 and return the response content string."""
    content, _tool_calls, _func_calls, cost, in_tok, out_tok = model.call(
        messages, temperature=temperature
    )
    logger.info(f"LLM call: input={in_tok}, output={out_tok}, cost=${cost:.4f}")
    return content


# ---------------------------------------------------------------------------
# Docker F2P validation
# ---------------------------------------------------------------------------

def run_f2p_validation(
    output_dir: str,
    instance_id: str,
    base_commit: str,
    gold_patch: str,
    repo: str,
    dockerfile_content: str,
    eval_script_path: str,
) -> dict[str, Any]:
    """Build Docker image and run pre-patch/post-patch F2P validation.

    Returns dict with:
      classification, pre_exit_code, post_exit_code,
      pre_test_output, post_test_output, error
    """
    import docker
    from app.agents.test_analysis_agent.docker_utils import (
        build_container,
        cleanup_container,
        copy_to_container,
        exec_run_with_timeout,
        remove_image,
        BuildImageError,
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
    image_name = f"postfix-{instance_id}:latest".lower().replace("__", "-")
    container_name = f"postfix-{instance_id}-test".lower().replace("__", "-")
    container = None

    try:
        # --- Step 1: Build Docker image ---
        print_step(instance_id, 4, "Building Docker image...")

        # Prepare Dockerfile with essentials
        processed_dockerfile = ensure_essentials_in_dockerfile(dockerfile_content)

        # Handle GITHUB_TOKEN for private repos
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

        # Write Dockerfile to temp dir for build context
        build_ctx = pjoin(output_dir, "docker_build")
        os.makedirs(build_ctx, exist_ok=True)
        with open(pjoin(build_ctx, "Dockerfile"), "w") as f:
            f.write(processed_dockerfile)

        # Build image
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
                raise docker.errors.BuildError(err_msg, build_log_lines)

        # Save build log
        with open(pjoin(output_dir, "build_image.log"), "w") as f:
            f.write("\n".join(build_log_lines))

        print_success(f"Image built: {image_name}")

        # --- Step 2: Create and start container ---
        print_step(instance_id, 5, "Running F2P tests in Docker...")

        # We use a simple logger-like object for build_container
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

        try:
            container = build_container(
                client, image_name, container_name, instance_id, run_logger
            )
            container.start()

            # Copy eval.sh into container
            eval_local = Path(eval_script_path)
            copy_to_container(container, eval_local, Path("/eval.sh"))

            # --- Phase 1: Pre-patch (no gold patch) ---
            # Reset to base commit
            container.exec_run(
                f"git reset --hard {base_commit}",
                workdir="/testbed",
                user="root",
            )
            clean_cmd = _get_clean_command(repo)
            container.exec_run(clean_cmd, workdir="/testbed", user="root")

            # Run eval.sh (pre-patch)
            pre_output_raw = exec_run_with_timeout(
                container, "/bin/bash /eval.sh", timeout=F2P_TEST_TIMEOUT
            )
            pre_output = pre_output_raw.decode("utf-8") if pre_output_raw else ""
            result["pre_test_output"] = pre_output
            result["pre_exit_code"] = extract_exit_code(pre_output)

            # Save pre-patch output
            with open(pjoin(output_dir, "test_output_prev_apply.txt"), "w") as f:
                f.write(pre_output)

            # --- Phase 2: Post-patch (apply gold patch) ---
            # Reset again
            container.exec_run(
                f"git reset --hard {base_commit}",
                workdir="/testbed",
                user="root",
            )
            container.exec_run(clean_cmd, workdir="/testbed", user="root")

            # Apply gold patch
            patch_local = Path(pjoin(output_dir, "gold_patch.diff"))
            patch_local.write_text(gold_patch)
            copy_to_container(container, patch_local, Path("/tmp/patch.diff"))

            apply_result = container.exec_run(
                "git apply -p1 -v /tmp/patch.diff",
                workdir="/testbed",
                user="root",
            )
            if apply_result.exit_code != 0:
                # Fallback to patch command
                apply_result = container.exec_run(
                    "patch --batch --fuzz=5 -p1 -i /tmp/patch.diff",
                    workdir="/testbed",
                    user="root",
                )
                if apply_result.exit_code != 0:
                    result["error"] = "Failed to apply gold patch"
                    print_fail(f"Could not apply gold patch: {apply_result.output.decode('utf-8')[:200]}")
                    return result

            # Re-copy eval.sh (in case pre-patch run modified something)
            copy_to_container(container, eval_local, Path("/eval.sh"))

            # Run eval.sh (post-patch)
            post_output_raw = exec_run_with_timeout(
                container, "/bin/bash /eval.sh", timeout=F2P_TEST_TIMEOUT
            )
            post_output = post_output_raw.decode("utf-8") if post_output_raw else ""
            result["post_test_output"] = post_output
            result["post_exit_code"] = extract_exit_code(post_output)

            # Save post-patch output
            with open(pjoin(output_dir, "test_output.txt"), "w") as f:
                f.write(post_output)

            # --- Classify ---
            result["classification"] = classify_f2p(
                result["pre_exit_code"], result["post_exit_code"]
            )

        finally:
            # Cleanup
            if container:
                try:
                    cleanup_container(client, container, run_logger)
                except Exception:
                    pass
            run_logger.close()

        # Remove image to save disk
        try:
            remove_image(client, image_name, "quiet")
        except Exception:
            pass

    except docker.errors.BuildError as e:
        result["error"] = f"Docker build failed: {e}"
        print_fail(f"Docker build failed")
        # Save build log
        with open(pjoin(output_dir, "build_image.log"), "a") as f:
            f.write(f"\nBUILD ERROR: {e}\n")
    except Exception as e:
        result["error"] = f"Docker validation error: {e}"
        print_fail(f"Docker error: {e}")
        logger.debug(traceback.format_exc())

    return result


def _get_clean_command(repo: str) -> str:
    """Return a repo-aware git clean command that preserves virtualenvs."""
    if repo == "MiroMindAI/miroflow":
        return "git clean -fdx -e .venv"
    if repo == "MiroMindAI/MiroThinker":
        return "git clean -fdx -e .venv -e apps/miroflow-agent/.venv"
    return "git clean -fdx"


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------

def load_instances_map(jsonl_path: str) -> dict[str, dict]:
    """Load instances from JSONL into a dict keyed by instance_id."""
    instances = {}
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            instances[item["instance_id"]] = item
    return instances


def get_failed_instances(setup_dir: str) -> list[tuple[str, str]]:
    """Return list of (instance_id, f2p_classification) for failed instances."""
    applicable_dir = pjoin(setup_dir, "applicable_setup")
    failed = []
    for d in sorted(os.listdir(applicable_dir)):
        status_path = pjoin(applicable_dir, d, "status.json")
        if not os.path.exists(status_path):
            continue
        with open(status_path) as f:
            status = json.load(f)
        if status.get("f2p_classification") != "FAIL2PASS":
            classification = status.get("f2p_classification", "unknown")
            failed.append((d, classification))
    return failed


def get_latest_agent_dir(inst_dir: str, prefix: str) -> str | None:
    """Find the highest-numbered agent directory with given prefix."""
    dirs = [d for d in os.listdir(inst_dir) if d.startswith(prefix)]
    if not dirs:
        return None
    dirs.sort(key=lambda x: int(x.rsplit("_", 1)[-1]))
    return pjoin(inst_dir, dirs[-1])


def read_file_safe(path: str, max_chars: int = 30000) -> str:
    """Read file content, truncating if too large."""
    if not os.path.exists(path):
        return "(file not found)"
    with open(path) as f:
        content = f.read()
    if len(content) > max_chars:
        half = max_chars // 2
        content = (
            content[:half]
            + f"\n\n... [TRUNCATED {len(content) - max_chars} chars] ...\n\n"
            + content[-half:]
        )
    return content


def collect_instance_context(inst_dir: str, instance_data: dict) -> dict[str, Any]:
    """Collect all context needed for post-fixing from an instance directory."""
    ctx = {}

    ctx["instance_id"] = instance_data["instance_id"]
    ctx["repo"] = instance_data["repo"]
    ctx["base_commit"] = instance_data["base_commit"]
    ctx["problem_statement"] = instance_data.get("problem_statement", "")
    ctx["patch"] = instance_data.get("patch", "")

    status_path = pjoin(inst_dir, "status.json")
    with open(status_path) as f:
        status = json.load(f)
    ctx["f2p_classification"] = status.get("f2p_classification", "unknown")

    ctx["dockerfile"] = read_file_safe(pjoin(inst_dir, "Dockerfile"))
    ctx["eval_script"] = read_file_safe(pjoin(inst_dir, "eval.sh"))

    # Latest test files
    latest_test_dir = get_latest_agent_dir(inst_dir, "write_test_agent_")
    ctx["previous_test_files"] = ""
    ctx["previous_test_files_content"] = {}
    if latest_test_dir:
        tests_dir = pjoin(latest_test_dir, "tests")
        if os.path.exists(tests_dir):
            parts = []
            for fname in sorted(os.listdir(tests_dir)):
                fpath = pjoin(tests_dir, fname)
                if os.path.isfile(fpath):
                    content = read_file_safe(fpath)
                    rel_path = f"tests/{fname}"
                    parts.append(f'<test_file path="{rel_path}">\n{content}\n</test_file>')
                    ctx["previous_test_files_content"][rel_path] = content
            ctx["previous_test_files"] = "\n\n".join(parts) if parts else "(no test files found)"

    # Latest test analysis output
    latest_analysis_dir = get_latest_agent_dir(inst_dir, "test_analysis_agent_")
    ctx["test_output_pre"] = "(not available)"
    ctx["test_output_post"] = "(not available)"
    ctx["analysis_feedback"] = "(not available)"

    if latest_analysis_dir:
        ctx["test_output_pre"] = read_file_safe(
            pjoin(latest_analysis_dir, "test_output_prev_apply.txt"), max_chars=15000
        )
        ctx["test_output_post"] = read_file_safe(
            pjoin(latest_analysis_dir, "test_output.txt"), max_chars=15000
        )
        analysis_path = pjoin(latest_analysis_dir, "analysis.json")
        if os.path.exists(analysis_path):
            with open(analysis_path) as f:
                analysis = json.load(f)
            ctx["analysis_feedback"] = analysis.get(
                "guidance_for_write_test_agent", "(no guidance)"
            )

    return ctx


def get_repo_env_guidance(repo: str) -> str:
    """Get repo-specific environment guidance for the prompt."""
    config = _REPO_ENV_CONFIG.get(repo)
    if config:
        return f"\n## Repository Environment\n{config[2]}"
    return ""


# ---------------------------------------------------------------------------
# Eval script generation
# ---------------------------------------------------------------------------

def _generate_fallback_eval_script(
    output_dir: str, repo: str, test_files: dict[str, str]
):
    """Generate a simple fallback eval.sh when LLM extraction fails."""
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
    with open(pjoin(output_dir, "eval.sh"), "w") as f:
        f.write(script_content)
    with open(pjoin(output_dir, "eval_skeleton.sh"), "w") as f:
        f.write(script_content)


# ---------------------------------------------------------------------------
# Write-back: promote successful post-fix results into the parent outputs
# ---------------------------------------------------------------------------

def write_back_instance(setup_dir: str, inst_dir: str, output_dir: str, instance_id: str):
    """Copy post-fix artifacts into the parent instance directory when FAIL2PASS.

    Updates:
      - applicable_setup/<id>/status.json  (overwrite)
      - applicable_setup/<id>/eval.sh      (overwrite)
    """
    import shutil

    # Overwrite status.json
    src_status = pjoin(output_dir, "status.json")
    dst_status = pjoin(inst_dir, "status.json")
    if os.path.exists(src_status):
        shutil.copy2(src_status, dst_status)

    # Overwrite eval.sh
    src_eval = pjoin(output_dir, "eval.sh")
    dst_eval = pjoin(inst_dir, "eval.sh")
    if os.path.exists(src_eval):
        shutil.copy2(src_eval, dst_eval)

    # Copy eval_skeleton.sh if present
    src_skel = pjoin(output_dir, "eval_skeleton.sh")
    dst_skel = pjoin(inst_dir, "eval_skeleton.sh")
    if os.path.exists(src_skel):
        shutil.copy2(src_skel, dst_skel)


def write_back_json_files(
    setup_dir: str,
    instances_jsonl_path: str,
    improved_results: list[dict[str, Any]],
):
    """Update the three top-level JSON files with newly passing instances.

    Updates:
      - raw_predictions.json   — flip status to True, update eval_script
      - predictions.json       — add newly passing instances
      - results/results.json   — add newly passing instances with full schema
    """
    if not improved_results:
        return

    improved_ids = {r["instance_id"] for r in improved_results}

    # Build a map of instance_id -> new eval.sh content
    eval_contents: dict[str, str] = {}
    eval_skeleton_contents: dict[str, str] = {}
    for r in improved_results:
        out_dir = r["output_dir"]
        iid = r["instance_id"]
        eval_path = pjoin(out_dir, "eval.sh")
        if os.path.exists(eval_path):
            with open(eval_path) as f:
                eval_contents[iid] = f.read()
        skel_path = pjoin(out_dir, "eval_skeleton.sh")
        if os.path.exists(skel_path):
            with open(skel_path) as f:
                eval_skeleton_contents[iid] = f.read()

    # --- 1. Update raw_predictions.json ---
    raw_path = pjoin(setup_dir, "raw_predictions.json")
    if os.path.exists(raw_path):
        with open(raw_path) as f:
            raw_data = json.load(f)
        for item in raw_data:
            if item["instance_id"] in improved_ids:
                item["status"] = True
                if item["instance_id"] in eval_contents:
                    item["eval_script"] = eval_contents[item["instance_id"]]
        with open(raw_path, "w") as f:
            json.dump(raw_data, f, indent=2)
        print_success(f"Updated raw_predictions.json ({len(improved_ids)} flipped to True)")

    # --- 2. Update predictions.json ---
    pred_path = pjoin(setup_dir, "predictions.json")
    if os.path.exists(pred_path):
        with open(pred_path) as f:
            pred_data = json.load(f)
        existing_ids = {p["instance_id"] for p in pred_data}

        # Build new entries from raw_predictions for improved instances
        if os.path.exists(raw_path):
            with open(raw_path) as f:
                raw_data = json.load(f)
            for item in raw_data:
                if item["instance_id"] in improved_ids and item["instance_id"] not in existing_ids:
                    pred_data.append(item)

        with open(pred_path, "w") as f:
            json.dump(pred_data, f, indent=2)
        print_success(f"Updated predictions.json (now {len(pred_data)} entries)")

    # --- 3. Update results/results.json ---
    results_path = pjoin(setup_dir, "results", "results.json")
    if os.path.exists(results_path):
        with open(results_path) as f:
            results_data = json.load(f)
        existing_ids = {r["instance_id"] for r in results_data}

        # Load full instance data from JSONL for the improved ones
        instances_map = load_instances_map(instances_jsonl_path)
        for iid in improved_ids:
            if iid in existing_ids:
                # Already in results — update eval_script
                for r in results_data:
                    if r["instance_id"] == iid:
                        if iid in eval_contents:
                            r["eval_script"] = eval_contents[iid]
                        if iid in eval_skeleton_contents:
                            r["eval_script_skeleton"] = eval_skeleton_contents[iid]
                        break
            elif iid in instances_map:
                # New entry — build from JSONL data + new artifacts
                inst = instances_map[iid]
                inst_dir = pjoin(setup_dir, "applicable_setup", iid)
                dockerfile_path = pjoin(inst_dir, "Dockerfile")
                dockerfile = ""
                if os.path.exists(dockerfile_path):
                    with open(dockerfile_path) as f:
                        dockerfile = f.read()

                new_entry = {
                    "repo": inst.get("repo", ""),
                    "pull_number": inst.get("pull_number"),
                    "pull_url": inst.get("pull_url", ""),
                    "instance_id": iid,
                    "issue_numbers": inst.get("issue_numbers", []),
                    "base_commit": inst.get("base_commit", ""),
                    "patch": inst.get("patch", ""),
                    "test_patch": inst.get("test_patch", ""),
                    "problem_statement": inst.get("problem_statement", ""),
                    "hints_text": inst.get("hints_text", ""),
                    "created_at": inst.get("created_at", ""),
                    "problem_statement_source": inst.get("problem_statement_source", ""),
                    "version": inst.get("version", ""),
                    "dockerfile": dockerfile,
                    "eval_script": eval_contents.get(iid, ""),
                    "eval_script_skeleton": eval_skeleton_contents.get(iid, ""),
                }
                results_data.append(new_entry)

        with open(results_path, "w") as f:
            json.dump(results_data, f, indent=2)
        print_success(f"Updated results/results.json (now {len(results_data)} entries)")


# ---------------------------------------------------------------------------
# Core post-fix logic
# ---------------------------------------------------------------------------

def _run_one_round(
    setup_dir: str,
    inst_dir: str,
    instance_data: dict,
    model,
    round_num: int,
    ctx: dict[str, Any],
    repo_env_guidance: str,
    skip_docker: bool,
) -> dict[str, Any]:
    """Execute a single generate-and-validate round.

    Returns a dict with: success, output_dir, new_classification, pre/post exit codes,
    new_test_files (dict path->content), f2p_result, error.
    """
    instance_id = instance_data["instance_id"]
    output_dir = pjoin(inst_dir, f"post_fix_round_{round_num}")
    os.makedirs(output_dir, exist_ok=True)

    round_result: dict[str, Any] = {
        "success": False,
        "output_dir": output_dir,
        "new_classification": None,
        "pre_exit_code": None,
        "post_exit_code": None,
        "new_test_files": {},
        "error": None,
    }

    # 1. Call LLM to regenerate test files
    print_step(instance_id, 1, f"Calling ClaudeOpus4_6 for test repair (round {round_num})...")
    system_prompt = POST_FIX_SYSTEM_PROMPT.format(repo_env_guidance=repo_env_guidance)
    user_prompt = POST_FIX_USER_PROMPT.format(**ctx)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    response_text = call_model(model, messages)

    with open(pjoin(output_dir, "llm_response.txt"), "w") as f:
        f.write(response_text)

    # 2. Extract test files
    new_test_files = extract_test_files_from_response(response_text)
    if not new_test_files:
        round_result["error"] = "No test files extracted from LLM response"
        print_fail(f"No test files extracted")
        return round_result

    print_success(
        f"Extracted {len(new_test_files)} test file(s): {list(new_test_files.keys())}"
    )
    round_result["new_test_files"] = new_test_files

    # Build patch
    test_patch, _ = build_patch_from_files(new_test_files, output_dir)
    with open(pjoin(output_dir, "generated_test_patch.diff"), "w") as f:
        f.write(test_patch)

    # 3. Generate eval.sh
    print_step(instance_id, 2, "Generating eval.sh...")
    eval_system = (
        "You are an expert at writing evaluation scripts for SWE-bench. "
        "Generate a bash eval.sh script that runs the given test files in a Docker container."
    )
    test_file_list = "\n".join(f"- `{p}`" for p in new_test_files.keys())
    test_contents_str = "\n\n".join(
        f"### `{path}`\n```python\n{content}\n```"
        for path, content in new_test_files.items()
    )
    eval_user = EVAL_SCRIPT_REGEN_PROMPT.format(
        repo=ctx["repo"],
        instance_id=instance_id,
        test_file_list=test_file_list,
        test_file_contents=test_contents_str,
        repo_env_guidance=repo_env_guidance,
    )
    eval_messages = [
        {"role": "system", "content": eval_system},
        {"role": "user", "content": eval_user},
    ]
    eval_response = call_model(model, eval_messages)

    with open(pjoin(output_dir, "eval_llm_response.txt"), "w") as f:
        f.write(eval_response)

    eval_extracted = extract_eval_script_from_response(
        eval_response, output_dir, test_patch,
        test_files_content=new_test_files,
    )
    if not eval_extracted:
        print_warn("Could not extract eval.sh from LLM, using fallback")
        _generate_fallback_eval_script(output_dir, ctx["repo"], new_test_files)

    print_success("eval.sh generated")

    # Copy Dockerfile
    dockerfile_src = pjoin(inst_dir, "Dockerfile")
    dockerfile_dst = pjoin(output_dir, "Dockerfile")
    if os.path.exists(dockerfile_src):
        with open(dockerfile_src) as f:
            df_content = f.read()
        with open(dockerfile_dst, "w") as f:
            f.write(df_content)

    # Save metadata
    with open(pjoin(output_dir, "post_fix_context.json"), "w") as f:
        json.dump(
            {
                "instance_id": instance_id,
                "model": "ClaudeOpus4_6",
                "round": round_num,
                "old_classification": ctx["f2p_classification"],
                "new_test_files": list(new_test_files.keys()),
            },
            f,
            indent=2,
        )

    round_result["success"] = True

    # 4. Docker F2P validation
    if not skip_docker:
        f2p_result = run_f2p_validation(
            output_dir=output_dir,
            instance_id=instance_id,
            base_commit=ctx["base_commit"],
            gold_patch=ctx["patch"],
            repo=ctx["repo"],
            dockerfile_content=ctx["dockerfile"],
            eval_script_path=pjoin(output_dir, "eval.sh"),
        )

        round_result["new_classification"] = f2p_result["classification"]
        round_result["pre_exit_code"] = f2p_result["pre_exit_code"]
        round_result["post_exit_code"] = f2p_result["post_exit_code"]

        if f2p_result["error"]:
            round_result["docker_error"] = f2p_result["error"]

        # Print F2P result
        print_f2p_result(
            instance_id,
            f2p_result["classification"],
            f2p_result["pre_exit_code"],
            f2p_result["post_exit_code"],
        )

        # Save status.json in round dir
        with open(pjoin(output_dir, "status.json"), "w") as f:
            json.dump(
                {
                    "is_finish": f2p_result["classification"] == "FAIL2PASS",
                    "f2p_classification": f2p_result["classification"],
                    "pre_exit_code": f2p_result["pre_exit_code"],
                    "post_exit_code": f2p_result["post_exit_code"],
                },
                f,
                indent=2,
            )
    else:
        print_warn("Docker F2P validation skipped (--skip-docker)")

    return round_result


def post_fix_instance(
    setup_dir: str,
    instance_data: dict,
    model,
    skip_docker: bool = False,
    max_rounds: int = 3,
) -> dict[str, Any]:
    """Run up to max_rounds of post-fixing for a single failed instance.

    Each round feeds its test output back as context for the next round.
    Stops early when FAIL2PASS is achieved.

    Returns a result dict with instance_id, success, f2p classification, etc.
    """
    instance_id = instance_data["instance_id"]
    inst_dir = pjoin(setup_dir, "applicable_setup", instance_id)

    result: dict[str, Any] = {
        "instance_id": instance_id,
        "success": False,
        "output_dir": None,
        "old_classification": None,
        "new_classification": None,
        "pre_exit_code": None,
        "post_exit_code": None,
        "rounds_used": 0,
        "error": None,
    }

    try:
        # Collect initial context from the original pipeline run
        print_step(instance_id, 0, "Collecting context...")
        ctx = collect_instance_context(inst_dir, instance_data)
        result["old_classification"] = ctx["f2p_classification"]
        repo_env_guidance = get_repo_env_guidance(ctx["repo"])

        for round_num in range(1, max_rounds + 1):
            if round_num > 1:
                _tprint(f"  {Colors.CYAN}--- Round {round_num}/{max_rounds} ---{Colors.RESET}")

            rr = _run_one_round(
                setup_dir, inst_dir, instance_data, model,
                round_num, ctx, repo_env_guidance, skip_docker,
            )

            result["output_dir"] = rr["output_dir"]
            result["rounds_used"] = round_num
            result["success"] = rr["success"]
            result["new_classification"] = rr.get("new_classification")
            result["pre_exit_code"] = rr.get("pre_exit_code")
            result["post_exit_code"] = rr.get("post_exit_code")
            if rr.get("new_test_files"):
                result["new_test_files"] = list(rr["new_test_files"].keys())
            if rr.get("error"):
                result["error"] = rr["error"]
            if rr.get("docker_error"):
                result["docker_error"] = rr["docker_error"]

            # --- Check if we should stop ---
            classification = rr.get("new_classification")

            # Extraction failed — no point retrying with the same context
            if not rr["success"]:
                print_fail(f"Round {round_num} failed to generate files, stopping")
                break

            # Docker skipped — can't evaluate, stop
            if skip_docker:
                break

            # FAIL2PASS achieved — success!
            if classification == "FAIL2PASS":
                write_back_instance(setup_dir, inst_dir, rr["output_dir"], instance_id)
                print_success(
                    f"FAIL2PASS achieved in round {round_num}! "
                    f"Wrote back to {instance_id}/"
                )
                break

            # Last round — don't prepare next iteration
            if round_num == max_rounds:
                print_warn(
                    f"Exhausted {max_rounds} rounds, best result: "
                    f"{classification or 'N/A'}"
                )
                break

            # --- Prepare context for next round ---
            # Feed the NEW test outputs and a diagnosis back into the context
            print_warn(f"Round {round_num} result: {classification}, retrying...")

            # Update previous_test_files with this round's generated tests
            new_files = rr.get("new_test_files", {})
            if new_files:
                parts = []
                for path, content in new_files.items():
                    parts.append(f'<test_file path="{path}">\n{content}\n</test_file>')
                ctx["previous_test_files"] = "\n\n".join(parts)

            # Update test outputs from this round's Docker run
            out_dir = rr["output_dir"]
            pre_path = pjoin(out_dir, "test_output_prev_apply.txt")
            post_path = pjoin(out_dir, "test_output.txt")
            if os.path.exists(pre_path):
                ctx["test_output_pre"] = read_file_safe(pre_path, max_chars=15000)
            if os.path.exists(post_path):
                ctx["test_output_post"] = read_file_safe(post_path, max_chars=15000)

            # Update classification and build a self-diagnosis for the next round
            ctx["f2p_classification"] = classification or "unknown"
            pre_exit = rr.get("pre_exit_code")
            post_exit = rr.get("post_exit_code")
            ctx["analysis_feedback"] = (
                f"Post-fix round {round_num} result: {classification} "
                f"(pre-patch exit={pre_exit}, post-patch exit={post_exit}). "
                f"The previous repair attempt did NOT achieve FAIL2PASS. "
                f"Review the test output above carefully and try a different approach. "
                f"Do not repeat the same mistakes."
            )

    except Exception as e:
        result["error"] = str(e)
        print_fail(f"[{instance_id}] Exception: {e}")
        logger.debug(traceback.format_exc())

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Post-fix failed SWE-bench test generation cases with Docker F2P validation"
    )
    parser.add_argument(
        "--setup-dir", required=True,
        help="Path to setup_output directory",
    )
    parser.add_argument(
        "--instances-jsonl", required=True,
        help="Path to instances JSONL file",
    )
    parser.add_argument(
        "--num-processes", type=int, default=5,
        help="Number of parallel workers (default: 5)",
    )
    parser.add_argument(
        "--instance", default=None,
        help="Run only this instance ID (optional)",
    )
    parser.add_argument(
        "--max-rounds", type=int, default=3,
        help="Max repair rounds per instance (default: 3)",
    )
    parser.add_argument(
        "--skip-docker", action="store_true",
        help="Skip Docker F2P validation (generate files only)",
    )
    parser.add_argument(
        "--temperature", type=float, default=0.2,
        help="LLM temperature (default: 0.2)",
    )
    args = parser.parse_args()

    print_header("SWE-Factory Post-Fix Pipeline")

    # Load instance data
    instances_map = load_instances_map(args.instances_jsonl)
    print(f"  Loaded {Colors.BOLD}{len(instances_map)}{Colors.RESET} instances from JSONL")

    # Find failed instances
    failed = get_failed_instances(args.setup_dir)
    print(f"  Found {Colors.BOLD}{Colors.RED}{len(failed)}{Colors.RESET} failed instances")

    if args.instance:
        failed = [(iid, cls) for iid, cls in failed if iid == args.instance]
        if not failed:
            print_fail(f"Instance {args.instance} not found in failed set")
            sys.exit(1)

    # Show breakdown
    from collections import Counter
    cls_counts = Counter(cls for _, cls in failed)
    for cls, count in cls_counts.most_common():
        color = Colors.RED if cls == "FAIL2FAIL" else Colors.YELLOW
        print(f"    {color}{cls}{Colors.RESET}: {count}")

    # Initialize model (singleton, shared across threads)
    print(f"\n  Initializing ClaudeOpus4_6 model...")
    model = create_model()
    print_success("Model ready")

    # Filter to instances in JSONL
    work_items = []
    for instance_id, classification in failed:
        if instance_id not in instances_map:
            print_warn(f"Skipping {instance_id}: not in instances JSONL")
            continue
        work_items.append(instances_map[instance_id])

    num_workers = min(args.num_processes, len(work_items)) if work_items else 1
    print(f"\n  Processing {Colors.BOLD}{len(work_items)}{Colors.RESET} instances "
          f"(max {args.max_rounds} rounds each, {num_workers} parallel workers)")
    if args.skip_docker:
        print(f"  {Colors.YELLOW}Docker F2P validation: SKIPPED{Colors.RESET}")
    print()

    # Process instances (parallel or sequential)
    results: list[dict[str, Any]] = []

    def _worker(idx_and_data: tuple[int, dict]) -> dict[str, Any]:
        idx, inst_data = idx_and_data
        iid = inst_data["instance_id"]
        _tprint(f"{Colors.BOLD}[{idx}/{len(work_items)}] {iid}{Colors.RESET}")
        r = post_fix_instance(
            args.setup_dir, inst_data, model,
            skip_docker=args.skip_docker,
            max_rounds=args.max_rounds,
        )
        _tprint()
        return r

    if num_workers <= 1:
        for i, inst_data in enumerate(work_items, 1):
            results.append(_worker((i, inst_data)))
    else:
        indexed_items = list(enumerate(work_items, 1))
        with ThreadPoolExecutor(max_workers=num_workers) as pool:
            futures = {
                pool.submit(_worker, item): item[1]["instance_id"]
                for item in indexed_items
            }
            for future in as_completed(futures):
                iid = futures[future]
                try:
                    results.append(future.result())
                except Exception as e:
                    logger.error(f"[{iid}] Worker exception: {e}")
                    results.append({
                        "instance_id": iid,
                        "success": False,
                        "output_dir": None,
                        "old_classification": None,
                        "new_classification": None,
                        "rounds_used": 0,
                        "error": str(e),
                    })

    # =========================================================================
    # Summary
    # =========================================================================
    print_header("POST-FIX RESULTS SUMMARY")

    gen_success = sum(1 for r in results if r["success"])
    gen_fail = len(results) - gen_success
    total_rounds = sum(r.get("rounds_used", 0) for r in results)
    print(f"  Generation: {Colors.GREEN}{gen_success} OK{Colors.RESET}, "
          f"{Colors.RED}{gen_fail} failed{Colors.RESET}, "
          f"{total_rounds} total rounds")

    if not args.skip_docker:
        # F2P classification summary
        f2p_counts: dict[str, int] = Counter()
        improved = []
        unchanged = []
        degraded = []

        for r in results:
            new_cls = r.get("new_classification")
            old_cls = r.get("old_classification")
            if new_cls:
                f2p_counts[new_cls] += 1
                if new_cls == "FAIL2PASS" and old_cls != "FAIL2PASS":
                    improved.append(r)
                elif new_cls == old_cls:
                    unchanged.append(r)
                else:
                    degraded.append(r)

        print(f"\n  {Colors.BOLD}F2P Classification:{Colors.RESET}")
        for cls in ["FAIL2PASS", "PASS2PASS", "FAIL2FAIL", "PASS2FAIL", "ERROR"]:
            count = f2p_counts.get(cls, 0)
            if count == 0:
                continue
            color = {
                "FAIL2PASS": Colors.GREEN,
                "PASS2PASS": Colors.YELLOW,
                "FAIL2FAIL": Colors.RED,
                "PASS2FAIL": Colors.MAGENTA,
                "ERROR": Colors.RED,
            }[cls]
            bar = "#" * count
            print(f"    {color}{cls:12s}{Colors.RESET} {bar} ({count})")

        if improved:
            print(f"\n  {Colors.GREEN}{Colors.BOLD}IMPROVED ({len(improved)}):{Colors.RESET}")
            for r in improved:
                rounds = r.get('rounds_used', '?')
                print(f"    {Colors.GREEN}+{Colors.RESET} {r['instance_id']}: "
                      f"{r['old_classification']} -> {Colors.GREEN}{r['new_classification']}{Colors.RESET}"
                      f"  {Colors.DIM}(round {rounds}){Colors.RESET}")

        if unchanged:
            print(f"\n  {Colors.YELLOW}UNCHANGED ({len(unchanged)}):{Colors.RESET}")
            for r in unchanged:
                print(f"    {Colors.DIM}= {r['instance_id']}: {r.get('new_classification', 'N/A')}{Colors.RESET}")

        if degraded:
            print(f"\n  {Colors.RED}CHANGED (not improved) ({len(degraded)}):{Colors.RESET}")
            for r in degraded:
                print(f"    {Colors.RED}!{Colors.RESET} {r['instance_id']}: "
                      f"{r['old_classification']} -> {r.get('new_classification', 'N/A')}")

        # Key metric
        f2p_count = f2p_counts.get("FAIL2PASS", 0)
        total = len(results)
        pct = (f2p_count / total * 100) if total else 0
        print(f"\n  {Colors.BOLD}FAIL2PASS rate: {f2p_count}/{total} ({pct:.0f}%){Colors.RESET}")

        # Write back improved results to top-level JSON files
        if improved:
            print_header("WRITING BACK RESULTS")
            write_back_json_files(args.setup_dir, args.instances_jsonl, improved)
        else:
            print(f"\n  {Colors.DIM}No improvements to write back.{Colors.RESET}")

    # Save summary
    summary_path = pjoin(args.setup_dir, "post_fix_summary.json")
    with open(summary_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n  Summary saved to {Colors.DIM}{summary_path}{Colors.RESET}")

    # Print generation failures
    gen_failures = [r for r in results if not r["success"]]
    if gen_failures:
        print(f"\n  {Colors.RED}Generation failures:{Colors.RESET}")
        for r in gen_failures:
            print(f"    {r['instance_id']}: {r.get('error', 'unknown')}")

    print()


if __name__ == "__main__":
    main()
