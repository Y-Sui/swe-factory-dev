"""Stage 3: Generate pytest tests for candidates using Claude Agent SDK.

For each candidate function, launches a Claude Agent with tools (Read, Glob, Grep, Bash)
so it can explore the repo, understand imports, write a test, and run it in Docker.
Failed tests get retried with error feedback.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

from config import REPOS, RepoConfig, AGENT_MODEL, DOCKER_TEMPLATES, DOCKER_TEST_CONFIG

TEST_GEN_PROMPT = """Write a pytest test file for this Python function.

Repository: {repo_name}
File: {file_path}
Function: {qual_name}

```python
{source}
```

{file_context}

Requirements:
- 3-6 test functions with descriptive names
- Include normal cases and edge cases
- Import the function directly (repo root is on sys.path)
- If a directory in the import path has hyphens, use sys.path.insert to add it
- Do NOT mock the target function — test its real behavior
- Do NOT use external services, network, files, or databases
- Do NOT test functions that require GPU or CUDA-only dependencies (e.g. grouped_gemm, flash_attn, triton kernels). If the function under test has hard imports of such packages, return "SKIP_GPU_DEPENDENCY" instead of a test file.
- Minimize mocking: only mock external I/O (network, filesystem, LLM API calls). Do NOT mock internal classes, methods, or modules from the same repo — construct real objects with test data instead.
- Test observable behavior, not implementation details: assert on return values, side effects, and error types. Do NOT assert exact dict key names, exact string messages, or internal field names that could vary across valid implementations.
- Prefer constructing real input objects over building elaborate mock scaffolding. If a function takes a complex object, read the class definition and instantiate it with minimal test data.

You can use Read/Glob/Grep to explore the repo and understand the import structure before writing the test.

Return ONLY the Python test file content. No markdown fences, no explanation."""

TEST_RETRY_PROMPT = """The previous test file for `{qual_name}` (in {file_path}) failed with this error:

{error_output}

The function source:
```python
{source}
```

You can use Read/Glob/Grep to explore the repo and fix the issue.

Fix the test file. Common issues:
- Wrong import path (check the file path and adjust sys.path if needed)
- Missing dependencies (use only stdlib + what the repo provides)
- Wrong assumptions about return types or behavior

Return ONLY the fixed Python test file content. No markdown fences, no explanation."""


def _get_file_context(repo_config: RepoConfig, file_path: str, lineno: int) -> str:
    """Get surrounding file context (imports + nearby code)."""
    full_path = repo_config.path / file_path
    try:
        lines = full_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except FileNotFoundError:
        return ""

    imports = [l for l in lines[:50] if l.startswith(("import ", "from "))]
    result = ""
    if imports:
        result += "File imports:\n```python\n" + "\n".join(imports) + "\n```\n"
    return result


async def generate_test(
    candidate: dict,
    repo_config: RepoConfig,
    prev_error: str = "",
    session_id: str | None = None,
) -> tuple[str | None, str | None]:
    """Generate a test file for one function. Agent can use tools to explore the repo.

    Returns (test_source, session_id) — session_id can be used to resume on retry.
    """
    qual_name = (
        f"{candidate['class_name']}.{candidate['func_name']}"
        if candidate.get("class_name")
        else candidate["func_name"]
    )

    if prev_error:
        prompt = TEST_RETRY_PROMPT.format(
            qual_name=qual_name,
            file_path=candidate["file_path"],
            source=candidate["source"],
            error_output=prev_error[:2000],
        )
    else:
        file_context = _get_file_context(repo_config, candidate["file_path"], candidate["lineno"])
        prompt = TEST_GEN_PROMPT.format(
            repo_name=repo_config.name,
            file_path=candidate["file_path"],
            qual_name=qual_name,
            source=candidate["source"],
            file_context=file_context,
        )

    result_text = None
    new_session_id = None
    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            allowed_tools=["Read", "Glob", "Grep"],
            permission_mode="bypassPermissions",
            cwd=str(repo_config.path),
            max_turns=10,
            model=AGENT_MODEL,
            resume=session_id,
        ),
    ):
        if isinstance(message, ResultMessage):
            result_text = message.result
            new_session_id = message.session_id

    if not result_text:
        return None, new_session_id

    response = result_text.strip()
    if response.startswith("```python"):
        response = response[len("```python"):].strip()
    if response.startswith("```"):
        response = response[3:].strip()
    if response.endswith("```"):
        response = response[:-3].strip()

    if not response or "import" not in response:
        return None, new_session_id

    if "SKIP_GPU_DEPENDENCY" in response:
        return "SKIP_GPU", new_session_id

    return response, new_session_id


# ── Docker-based test validation ──────────────────────────────────────

def _get_repo_head_commit(repo_key: str) -> str:
    """Get HEAD commit SHA of the cached repo."""
    repo_path = REPOS[repo_key].path
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, cwd=str(repo_path),
    )
    return result.stdout.strip()


# Tracks the pinned image tag per repo (set after build)
_pinned_image_tags: dict[str, str] = {}


def _build_base_image(repo_key: str) -> str:
    """Build the base Docker image from docker/Dockerfile.<repo>. Returns base tag."""
    config = DOCKER_TEST_CONFIG[repo_key]
    base_tag = config["image_tag"]
    dockerfile = DOCKER_TEMPLATES[repo_key]

    result = subprocess.run(
        ["docker", "image", "inspect", base_tag],
        capture_output=True, timeout=10,
    )
    if result.returncode == 0:
        print(f"  Base image {base_tag} already exists")
        return base_tag

    print(f"  Building base image {base_tag}...")
    build_cmd = [
        "docker", "build",
        "-t", base_tag,
        "-f", str(dockerfile),
    ]

    github_token = os.environ.get("GITHUB_TOKEN", "")
    if github_token:
        build_cmd.extend(["--build-arg", f"GITHUB_TOKEN={github_token}"])

    build_cmd.append(str(dockerfile.parent))

    result = subprocess.run(
        build_cmd,
        capture_output=True, text=True,
        timeout=600,
    )
    if result.returncode != 0:
        print(f"  Docker build FAILED:\n{result.stderr[-500:]}")
        raise RuntimeError(f"Docker build failed for {repo_key}")

    print(f"  Base image {base_tag} built successfully")
    return base_tag


def build_docker_image(repo_key: str) -> str:
    """Build base image, then a pinned image at HEAD commit. Returns pinned tag."""
    base_tag = _build_base_image(repo_key)

    commit_sha = _get_repo_head_commit(repo_key)
    short_sha = commit_sha[:12]
    pinned_tag = f"internal-swe-bench-swe-smith-{repo_key}:{short_sha}"

    result = subprocess.run(
        ["docker", "image", "inspect", pinned_tag],
        capture_output=True, timeout=10,
    )
    if result.returncode == 0:
        print(f"  Pinned image {pinned_tag} already exists")
    else:
        print(f"  Building pinned image {pinned_tag}...")
        pinned_dockerfile = f"FROM {base_tag}\nWORKDIR /testbed\nRUN git checkout {commit_sha}\n"
        result = subprocess.run(
            ["docker", "build", "-t", pinned_tag, "-"],
            input=pinned_dockerfile,
            capture_output=True, text=True,
            timeout=300,
        )
        if result.returncode != 0:
            print(f"  Pinned image build FAILED:\n{result.stderr[-500:]}")
            raise RuntimeError(f"Pinned image build failed for {repo_key}")
        print(f"  Pinned image {pinned_tag} built successfully")

    _pinned_image_tags[repo_key] = pinned_tag
    return pinned_tag


def validate_test_docker(test_source: str, repo_key: str) -> tuple[bool, str]:
    """Run a test file inside Docker. Returns (passed, output)."""
    config = DOCKER_TEST_CONFIG[repo_key]
    image_tag = _pinned_image_tags.get(repo_key, config["image_tag"])
    test_dir = config["test_dir"]
    pytest_cmd = config["pytest_cmd"]

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", prefix="test_smith_", delete=False,
    ) as f:
        f.write(test_source)
        host_test_path = f.name

    container_test_path = f"{test_dir}/test_smith_tmp.py"

    try:
        result = subprocess.run(
            [
                "docker", "run", "--rm",
                "-v", f"{host_test_path}:{container_test_path}:ro",
                "-w", test_dir,
                image_tag,
                "bash", "-c",
                f"{pytest_cmd} {container_test_path} -x -v --tb=short --no-header 2>&1",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stdout + result.stderr
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"
    except Exception as e:
        return False, str(e)
    finally:
        Path(host_test_path).unlink(missing_ok=True)




# ── Main flow ─────────────────────────────────────────────────────────

import asyncio

# Max concurrent agent calls per repo
_CONCURRENCY = 5


async def _generate_and_validate_one(
    cand: dict,
    index: int,
    total: int,
    repo_config: RepoConfig,
    repo_key: str,
    max_retries: int,
    semaphore: asyncio.Semaphore,
) -> dict | None:
    """Generate and validate test for one function with retry. Returns entry or None."""
    func_name = cand["func_name"]
    qual = (
        f"{cand['class_name']}.{func_name}"
        if cand.get("class_name")
        else func_name
    )

    async with semaphore:
        print(f"  [{index+1}/{total}] {qual}")

        prev_error = ""
        session_id = None
        for attempt in range(max_retries):
            test_source, session_id = await generate_test(
                cand, repo_config, prev_error, session_id,
            )
            if test_source is None:
                print(f"    [{qual}] Attempt {attempt+1}: agent returned empty")
                continue
            if test_source == "SKIP_GPU":
                print(f"    [{qual}] SKIPPED (agent detected GPU-only dependency)")
                return None

            ok, output = validate_test_docker(test_source, repo_key)
            if ok:
                entry = cand.copy()
                entry["test_source"] = test_source
                entry["test_output"] = output[:2000]
                print(f"    [{qual}] PASS (attempt {attempt+1})")
                return entry
            else:
                prev_error = output[:2000]
                print(f"    [{qual}] FAIL (attempt {attempt+1}): {output[:150]}")

        print(f"    [{qual}] SKIPPED")
        return None


async def generate_and_validate_all(
    candidates: list[dict],
    repo_key: str,
    output_path: Path | None = None,
    max_retries: int = 4,
) -> list[dict]:
    """Generate and validate tests in parallel within a repo."""
    repo_config = REPOS[repo_key]

    try:
        build_docker_image(repo_key)
    except RuntimeError as e:
        print(f"  {e} — skipping for {repo_key}")
        return []

    # Load existing results to skip already-passed functions
    existing: dict[str, dict] = {}
    if output_path and output_path.exists():
        for entry in load_tested(output_path):
            existing[entry["func_name"]] = entry

    results = list(existing.values())
    if existing:
        print(f"  Loaded {len(existing)} already-tested, skipping them")

    # Filter to only candidates that need processing
    todo = [
        (i, cand) for i, cand in enumerate(candidates)
        if cand["func_name"] not in existing
    ]
    skipped = len(candidates) - len(todo)
    if skipped:
        print(f"  Skipping {skipped} already-passed functions")

    semaphore = asyncio.Semaphore(_CONCURRENCY)
    tasks = [
        asyncio.create_task(
            _generate_and_validate_one(
                cand, i, len(candidates), repo_config, repo_key, max_retries, semaphore,
            )
        )
        for i, cand in todo
    ]

    for coro in asyncio.as_completed(tasks):
        entry = await coro
        if entry is not None:
            results.append(entry)
            if output_path:
                save_tested(results, output_path)

    return results


def save_tested(tested: list[dict], output_path: Path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        for c in tested:
            f.write(json.dumps(c) + "\n")
    print(f"  Saved {len(tested)} tested candidates to {output_path}")


def load_tested(path: Path) -> list[dict]:
    if not path.exists():
        return []
    candidates = []
    with open(path) as f:
        for line in f:
            if line.strip():
                candidates.append(json.loads(line))
    return candidates
