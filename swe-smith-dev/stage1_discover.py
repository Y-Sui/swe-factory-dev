"""Stage 1: Use Claude Agent SDK to discover core functions in repos.

Sends an agent into each repo to explore the codebase and identify the most
core, non-trivial, testable functions. Returns candidate references (file_path,
func_name, class_name, lineno) without extracting full source yet.
"""

import asyncio
import json
from pathlib import Path

from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage, AssistantMessage, ToolUseBlock

from config import REPOS, RepoConfig, CANDIDATES_PER_REPO, AGENT_MODEL, DISCOVER_HINTS

DISCOVER_PROMPT = """Find 60~80 core Python functions in this repo for a coding benchmark.

The benchmark works like this: we will MASK the function body (replace with `raise NotImplementedError`), keep the signature + docstring + surrounding class/module context, and ask an AI agent to re-implement the function. So we need functions where:
- The docstring clearly describes what the function should do
- The implementation requires understanding other parts of the repo (not self-contained)
- The function can be tested by mocking external services (LLM APIs, network calls, etc.)

Repo: {repo_name} at {repo_path}

{repo_hints}

Step 1: Explore the repo structure. Use Glob to find Python files, then Read key files to understand the architecture and where core logic lives.

Step 2: Select functions meeting ALL of these criteria:
- Core to the repo's purpose (not utils, helpers, compat, config, CLI, logging, or tests)
- Has a docstring that describes its behavior
- Between 20-150 lines of implementation
- Non-trivial logic: has branching, state management, or multi-step processing
- Interacts with other classes/modules in the repo (not a standalone algorithm)
- Diverse: spread across different modules, not clustered in one file

Read the actual function bodies before selecting — do not guess from file names alone.

Return a JSON array:
[{{"file_path": "src/core/engine.py", "func_name": "process_batch", "class_name": "Engine", "lineno": 45, "num_lines": 38, "reason": "Core batch processing with error recovery — requires understanding of TaskLog and StreamHandler interfaces"}}]

Return ONLY the JSON array."""


async def discover_repo(repo_key: str, num_candidates: int = CANDIDATES_PER_REPO) -> list[dict]:
    """Use Claude Agent to discover core functions in a repo."""
    repo_config = REPOS[repo_key]

    # Build repo-specific hints
    hints = DISCOVER_HINTS.get(repo_key)
    if hints:
        hint_lines = [f"This repo is: {hints.description}", ""]
        hint_lines.append("LOOK HERE FIRST (priority directories):")
        for d in hints.priority_dirs:
            hint_lines.append(f"  - {d}")
        hint_lines.append("")
        hint_lines.append("DO NOT look in these directories:")
        for d in hints.skip_dirs:
            hint_lines.append(f"  - {d}")
        repo_hints = "\n".join(hint_lines)
    else:
        repo_hints = ""

    prompt = DISCOVER_PROMPT.format(
        repo_path=str(repo_config.path),
        repo_name=repo_config.name,
        src_dirs=", ".join(repo_config.src_dirs),
        num_candidates=num_candidates,
        num_candidates_max=num_candidates + 10,
        repo_hints=repo_hints,
    )

    print(f"  Agent exploring {repo_key}...")

    result_text = None
    tool_use_count = 0
    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            allowed_tools=["Read", "Glob", "Grep", "Bash"],
            permission_mode="bypassPermissions",
            cwd=str(repo_config.path),
            max_turns=30,
            model=AGENT_MODEL,
        ),
    ):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    tool_use_count += 1
                    print(f"    [{repo_key}] tool: {block.name}")
        elif isinstance(message, ResultMessage):
            result_text = message.result
            print(f"  Agent finished {repo_key}: {message.num_turns} turns, {tool_use_count} tool calls")

    if not result_text:
        print(f"  Agent returned no result for {repo_key}")
        return []

    # Extract JSON from result
    candidates = _parse_json_from_text(result_text)
    if not candidates:
        print(f"  Failed to parse candidates from agent response for {repo_key}")
        return []

    # Normalize file_path to be relative to repo root
    repo_prefix = str(repo_config.path) + "/"
    for c in candidates:
        c["repo"] = repo_config.name
        if c.get("file_path", "").startswith(repo_prefix):
            c["file_path"] = c["file_path"][len(repo_prefix):]
        elif c.get("file_path", "").startswith("/"):
            # Try to extract relative path from any absolute path containing repo name
            parts = c["file_path"].split(repo_config.name.replace("__", "/") + "/", 1)
            if len(parts) == 2:
                c["file_path"] = parts[1]

    print(f"  Agent found {len(candidates)} candidates for {repo_key}")
    return candidates


async def discover_all_repos(
    repo_keys: list[str],
    num_candidates: int = CANDIDATES_PER_REPO,
) -> dict[str, list[dict]]:
    """Discover candidates for all repos concurrently."""
    tasks = {
        key: asyncio.create_task(discover_repo(key, num_candidates))
        for key in repo_keys
    }

    results = {}
    for key, task in tasks.items():
        results[key] = await task

    return results


def _parse_json_from_text(text: str) -> list[dict]:
    """Extract a JSON array from text that may contain markdown or extra content."""
    text = text.strip()

    # Try direct parse
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    # Try to find JSON array in text
    start = text.find("[")
    if start == -1:
        return []

    # Find matching closing bracket
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return []

    return []


def save_discovered(candidates: dict[str, list[dict]], output_dir: Path):
    """Save discovered candidates per repo."""
    for repo_key, cands in candidates.items():
        repo_dir = output_dir / repo_key
        repo_dir.mkdir(parents=True, exist_ok=True)
        path = repo_dir / "stage1_discovered.jsonl"
        with open(path, "w") as f:
            for c in cands:
                f.write(json.dumps(c) + "\n")
        print(f"  Saved {len(cands)} discovered candidates to {path}")


def load_discovered(output_dir: Path, repo_key: str) -> list[dict]:
    """Load discovered candidates for a repo."""
    path = output_dir / repo_key / "stage1_discovered.jsonl"
    if not path.exists():
        return []
    candidates = []
    with open(path) as f:
        for line in f:
            if line.strip():
                candidates.append(json.loads(line))
    return candidates
