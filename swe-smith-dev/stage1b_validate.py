"""Stage 1b: Validate discovered candidates by reading actual code.

Uses Claude Agent SDK to read each candidate's source code and filter out:
- Glue code (just calls other functions without real logic)
- Trivial wrappers around external APIs
- Config/setup/logging/formatting code
- Functions under 20 lines
- Functions in utils/helpers/compat directories

Runs all repos in parallel.
"""

import asyncio

from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage, AssistantMessage, ToolUseBlock

from config import REPOS, AGENT_MODEL
from stage1_discover import _parse_json_from_text

VALIDATE_PROMPT = """You are filtering candidates for a coding benchmark from the repository `{repo_name}`.

For EACH candidate below, Read the actual source code in the repo. REJECT if the function is clearly unsuitable:
- Does not exist at the specified file_path and lineno
- Under 15 lines of actual implementation (excluding docstring/comments)
- Pure config/setup: only sets attributes or reads config, no logic
- Pure logging/formatting: only formats strings or logs data
- Trivial one-liner wrapper: single return statement delegating to another function
- Glue code: just calls other functions/methods without real logic of its own
- LLM calling patterns: formats prompt and calls an LLM client, but doesn't do real data processing or algorithmic logic

KEEP functions that have any of these qualities:
- Multi-step processing or orchestration logic (even if it calls other functions — orchestration IS real logic)
- Data transformation, parsing, or state management
- Error handling with branching or recovery logic
- Protocol handling or message processing
- Mathematical computation or algorithmic logic

We want 10-20 good candidates per repo. When in doubt, KEEP the candidate.

Candidates:
{candidates_text}

Read each candidate's source code before deciding. Return a JSON array of the INDICES (0-based) of candidates that you KEEP.
Example: [0, 2, 5, 7]

Return ONLY the JSON array."""


async def validate_repo(repo_key: str, candidates: list[dict]) -> list[dict]:
    """Validate candidates for one repo by reading actual code."""
    repo_config = REPOS[repo_key]

    candidates_text = ""
    for i, c in enumerate(candidates):
        qual = f"{c.get('class_name', '')}.{c['func_name']}" if c.get("class_name") else c["func_name"]
        candidates_text += (
            f"[{i}] {c['file_path']}:{c.get('lineno', '?')}  {qual}  "
            f"(~{c.get('num_lines', '?')} lines)\n"
            f"    Reason: {c.get('reason', '')}\n\n"
        )

    prompt = VALIDATE_PROMPT.format(
        repo_name=repo_config.name,
        candidates_text=candidates_text,
    )

    print(f"  Validating {len(candidates)} candidates for {repo_key}...")

    result_text = None
    tool_use_count = 0
    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            allowed_tools=["Read", "Glob", "Grep"],
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
        elif isinstance(message, ResultMessage):
            result_text = message.result
            print(f"  Validator finished {repo_key}: {message.num_turns} turns, {tool_use_count} tool calls")

    if not result_text:
        print(f"  Validation returned no result for {repo_key}, keeping all")
        return candidates

    valid_indices = _parse_json_from_text(result_text)
    if not valid_indices or not isinstance(valid_indices, list):
        print(f"  Failed to parse validation result for {repo_key}, keeping all")
        return candidates

    validated = [candidates[i] for i in valid_indices if isinstance(i, int) and 0 <= i < len(candidates)]
    dropped = len(candidates) - len(validated)
    if dropped:
        print(f"  Validation: {len(validated)} kept, {dropped} dropped for {repo_key}")
    else:
        print(f"  Validation: all {len(validated)} kept for {repo_key}")

    return validated


async def validate_all_repos(discovered: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Validate candidates for all repos concurrently."""
    async def _validate(repo_key: str) -> tuple[str, list[dict]]:
        cands = discovered.get(repo_key, [])
        if not cands:
            return repo_key, []
        return repo_key, await validate_repo(repo_key, cands)

    results = {}
    for key, validated in await asyncio.gather(*[_validate(k) for k in discovered]):
        results[key] = validated
    return results
