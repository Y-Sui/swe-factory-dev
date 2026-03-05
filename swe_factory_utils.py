"""Shared utilities for SWE-Factory pipeline.

Canonical implementations of token injection, exit-code extraction,
F2P classification, Dockerfile essentials injection, diff parsing,
and repo eval config.  Every module in the project should import from
here instead of maintaining its own copy.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess

# ---------------------------------------------------------------------------
# Fetch missing (dangling) commits
# ---------------------------------------------------------------------------


def fetch_missing_commits(
    repo_dir: str,
    commits: list[str] | set[str],
    repo_url: str | None = None,
) -> list[str]:
    """Fetch commits not reachable in a local clone (e.g. from force-pushed PRs).

    Args:
        repo_dir: Path to the local git repository.
        commits: Commit SHAs to ensure are present.
        repo_url: If provided, update the remote origin URL first
                  (useful when GITHUB_TOKEN has changed).

    Returns:
        List of SHAs that are still missing after fetching.
    """
    if not commits:
        return []

    if repo_url:
        subprocess.run(
            ["git", "remote", "set-url", "origin", repo_url],
            cwd=repo_dir, capture_output=True, check=False,
        )

    # Find which SHAs are missing locally.
    missing = []
    for sha in commits:
        r = subprocess.run(
            ["git", "cat-file", "-t", sha],
            cwd=repo_dir, capture_output=True, check=False,
        )
        if r.returncode != 0:
            missing.append(sha)

    if not missing:
        return []

    print(f"⏳ Fetching {len(missing)} missing commits in {os.path.basename(repo_dir)}...")
    for sha in missing:
        r = subprocess.run(
            ["git", "fetch", "origin", sha],
            cwd=repo_dir, capture_output=True, check=False,
        )
        if r.returncode != 0:
            print(f"  ⚠️  Could not fetch {sha[:12]}")

    # Check which are still missing.
    still_missing = []
    for sha in missing:
        r = subprocess.run(
            ["git", "cat-file", "-t", sha],
            cwd=repo_dir, capture_output=True, check=False,
        )
        if r.returncode != 0:
            still_missing.append(sha)

    fetched = len(missing) - len(still_missing)
    print(
        f"  ✅ Fetched {fetched}/{len(missing)} commits"
        + (f", {len(still_missing)} still missing" if still_missing else "")
    )
    return still_missing


# ---------------------------------------------------------------------------
# Exit-code extraction
# ---------------------------------------------------------------------------

EXIT_CODE_RE = re.compile(r"OMNIGRIL_EXIT_CODE=(\d+)")


def extract_exit_code(output: str) -> int | None:
    """Extract the OMNIGRIL exit code from test output; returns None if not found."""
    m = EXIT_CODE_RE.search(output)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Fail-to-Pass classification
# ---------------------------------------------------------------------------


def classify_f2p(pre_exit: int | None, post_exit: int | None) -> str:
    """Classify Fail-to-Pass result from pre-patch and post-patch exit codes."""
    if pre_exit is None or post_exit is None:
        return "ERROR"
    pre_pass = pre_exit == 0
    post_pass = post_exit == 0
    if not pre_pass and post_pass:
        return "FAIL2PASS"
    elif pre_pass and post_pass:
        return "PASS2PASS"
    elif not pre_pass and not post_pass:
        return "FAIL2FAIL"
    else:  # pre_pass and not post_pass
        return "PASS2FAIL"


# ---------------------------------------------------------------------------
# GitHub token injection
# ---------------------------------------------------------------------------


def inject_github_token(url: str) -> str:
    """Inject GITHUB_TOKEN into a GitHub HTTPS URL for private repo access.

    Returns the original URL unchanged when no token is set or the URL
    already contains credentials.
    """
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token and "github.com" in url and "x-access-token" not in url:
        return url.replace(
            "https://github.com", f"https://x-access-token:{token}@github.com"
        )
    return url


# ---------------------------------------------------------------------------
# Dockerfile essentials injection
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Diff parsing
# ---------------------------------------------------------------------------

DIFF_MODIFIED_FILE_REGEX = r"--- a/(.*)"
DIFF_NEW_FILE_REGEX = r"\+\+\+ b/(.*)"
DIFF_DEVNULL_REGEX = r"--- /dev/null\n\+\+\+ b/(.*)"


def parse_test_files_from_patch(patch: str) -> list[str]:
    """Return deduplicated list of test file paths referenced in a unified diff."""
    modified = [p.split("\t")[0] for p in re.findall(DIFF_MODIFIED_FILE_REGEX, patch)
                if not p.startswith("/dev/null")]
    new_files = [p.split("\t")[0] for p in re.findall(DIFF_NEW_FILE_REGEX, patch)
                 if not p.startswith("/dev/null")]
    return list(dict.fromkeys(modified + new_files))


# ---------------------------------------------------------------------------
# Repo-specific eval config
# ---------------------------------------------------------------------------

REPO_EVAL_CONFIG: dict[str, dict] = {
    "MiroMindAI/miroflow": {
        "workdir": "/testbed",
        "pytest_cmd": ".venv/bin/pytest {files} -xvs",
    },
    "MiroMindAI/MiroThinker": {
        "workdir": "/testbed/apps/miroflow-agent",
        "pytest_cmd": ".venv/bin/pytest {files} -xvs",
    },
    "MiroMindAI/sd-torchtune": {
        "workdir": "/testbed",
        "pytest_cmd": "pytest {files} -xvs --without-integration",
    },
}
DEFAULT_REPO_EVAL_CONFIG = REPO_EVAL_CONFIG["MiroMindAI/miroflow"]


# ---------------------------------------------------------------------------
# JSON extraction from LLM responses
# ---------------------------------------------------------------------------


def extract_json_from_response(res_text: str) -> str:
    """Extract a JSON block from an LLM response.

    Tries ```json ... ``` first, then any ``` ... ``` block that parses as
    valid JSON. Returns the original text if nothing matches.
    """
    import json as _json

    json_matches = re.findall(r"```json([\s\S]*?)```", res_text, re.IGNORECASE)
    if json_matches:
        return json_matches[0].strip()

    for block in re.findall(r"```([\s\S]*?)```", res_text, re.IGNORECASE):
        clean = block.strip()
        try:
            _json.loads(clean)
            return clean
        except _json.JSONDecodeError:
            continue

    return res_text


# ---------------------------------------------------------------------------
# Repo-specific git clean command
# ---------------------------------------------------------------------------


def get_clean_command_for_repo(repo_name: str) -> str:
    """Return a repo-aware git clean command that preserves uv virtualenvs."""
    if repo_name == "MiroMindAI/miroflow":
        return "git clean -fdx -e .venv"
    if repo_name == "MiroMindAI/MiroThinker":
        return "git clean -fdx -e .venv -e apps/miroflow-agent/.venv"
    return "git clean -fdx"


# ---------------------------------------------------------------------------
# Dockerfile essentials injection
# ---------------------------------------------------------------------------

ESSENTIALS_RUN = (
    "RUN apt-get update && apt-get install -y --no-install-recommends "
    "curl git ca-certificates && rm -rf /var/lib/apt/lists/*"
)


def ensure_essentials_in_dockerfile(dockerfile: str) -> str:
    """Inject an early apt-get layer for curl/git/ca-certificates.

    LLMs frequently generate ``RUN curl ...`` before installing curl.
    This inserts the essentials right after the first FROM line so that
    every subsequent RUN can rely on them.  If the Dockerfile already
    installs them, the extra apt-get is a harmless no-op.
    """
    lines = dockerfile.split("\n")
    out: list[str] = []
    inserted = False
    for line in lines:
        out.append(line)
        # Insert right after the first FROM (possibly with --platform)
        if not inserted and line.strip().upper().startswith("FROM "):
            out.append(ESSENTIALS_RUN)
            inserted = True
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Per-test result parsing and filtering
# ---------------------------------------------------------------------------

_PER_TEST_RE = re.compile(r"^(\S+::\S+)\s+(PASSED|FAILED)\s", re.MULTILINE)


def parse_per_test_results(pytest_output: str) -> dict[str, str]:
    """Parse pytest -v output into {test_id: "PASSED"|"FAILED"}.

    Matches lines like: tests/test_foo.py::test_bar PASSED [ 25%]
    """
    return {m.group(1): m.group(2) for m in _PER_TEST_RE.finditer(pytest_output)}


def classify_per_test_f2p(
    pre: dict[str, str], post: dict[str, str]
) -> dict[str, str]:
    """Cross-reference per-test results from pre-patch and post-patch runs.

    Returns {test_id: "FAIL2PASS"|"PASS2PASS"|"FAIL2FAIL"|"PASS2FAIL"} for
    test IDs present in both dicts.
    """
    result: dict[str, str] = {}
    for tid in pre:
        if tid not in post:
            continue
        pre_pass = pre[tid] == "PASSED"
        post_pass = post[tid] == "PASSED"
        if not pre_pass and post_pass:
            result[tid] = "FAIL2PASS"
        elif pre_pass and post_pass:
            result[tid] = "PASS2PASS"
        elif not pre_pass and not post_pass:
            result[tid] = "FAIL2FAIL"
        else:
            result[tid] = "PASS2FAIL"
    return result


def filter_test_file_by_names(source: str, keep_names: set[str]) -> str:
    """Remove top-level test functions not in *keep_names* from *source*.

    Keeps all non-test code (imports, fixtures, constants, classes) intact.
    A "test function" is a top-level ``def test_*`` or ``async def test_*``.
    Returns the filtered source, or empty string if no test functions remain.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source  # can't parse → return as-is

    lines = source.splitlines(keepends=True)

    # Collect line ranges (0-indexed) of test functions to DROP
    drop_ranges: list[tuple[int, int]] = []
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        if node.name in keep_names:
            continue
        # node.lineno is 1-indexed
        start = node.lineno - 1
        end = node.end_lineno  # end_lineno is 1-indexed inclusive → exclusive as index
        if end is None:
            end = start + 1
        drop_ranges.append((start, end))

    if not drop_ranges:
        return source  # nothing to drop

    # Build filtered output by skipping drop ranges
    kept: list[str] = []
    drop_set: set[int] = set()
    for start, end in drop_ranges:
        for i in range(start, end):
            drop_set.add(i)

    for i, line in enumerate(lines):
        if i not in drop_set:
            kept.append(line)

    result = "".join(kept)

    # Check if any test functions remain
    try:
        new_tree = ast.parse(result)
    except SyntaxError:
        return result
    has_tests = any(
        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")
        for n in ast.iter_child_nodes(new_tree)
    )
    return result if has_tests else ""
