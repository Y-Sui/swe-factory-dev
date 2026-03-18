"""LLM-assisted fix for bench-inspector instances via OpenRouter."""

import json
import os
import re
from difflib import unified_diff

from openai import OpenAI

SYSTEM_PROMPTS = {
    "problem_statement": (
        "You are improving a SWE-bench problem statement. "
        "Make it clear, specific, and actionable. Include: "
        "what the expected behavior is, what the actual behavior is, "
        "and steps to reproduce if applicable.\n\n"
        "Return ONLY the improved problem statement, no explanation."
    ),
    "test_files": (
        "You are improving test files for a SWE-bench instance. "
        "The tests should have FAIL_TO_PASS tests that catch the bug, "
        "PASS_TO_PASS regression tests, clear assertions with meaningful "
        "failure messages, and test behavior not implementation.\n\n"
        "Return ONLY the improved test file as valid Python code, no explanation."
    ),
}

DEFAULT_MODEL = "anthropic/claude-opus-4.6"


def _build_context(instance: dict, fix_type: str) -> str:
    parts = [
        f"## Instance: {instance.get('instance_id', '')}",
        f"## Repo: {instance.get('repo', '')}",
    ]

    if fix_type == "problem_statement":
        parts.append(f"## Current Problem Statement\n{instance.get('problem_statement', '')}")
        parts.append(f"## Gold Patch\n```diff\n{instance.get('patch', '')}\n```")
    elif fix_type == "test_files":
        parts.append(f"## Current Test Patch\n```diff\n{instance.get('test_patch', '')}\n```")
        parts.append(f"## Gold Patch\n```diff\n{instance.get('patch', '')}\n```")
        parts.append(f"## Problem Statement\n{instance.get('problem_statement', '')}")

    fm = instance.get("function_metadata")
    if fm:
        parts.append(f"## Function: `{fm.get('func_name', '')}` in `{fm.get('file_path', '')}`")
        if fm.get("generated_docstring"):
            parts.append(f"## Docstring\n{fm['generated_docstring']}")

    return "\n\n".join(parts)


def chat_fix(
    messages: list[dict],
    instance: dict,
    fix_type: str,
    model: str = DEFAULT_MODEL,
) -> str:
    """Send multi-turn conversation to OpenRouter. Returns assistant response."""
    client = OpenAI(
        api_key=os.environ.get("OPENAI_KEY", ""),
        base_url=os.environ.get("OPENAI_API_BASE_URL", "https://openrouter.ai/api/v1"),
    )

    full_messages = [{"role": "system", "content": SYSTEM_PROMPTS[fix_type]}]

    for i, msg in enumerate(messages):
        if i == 0 and msg["role"] == "user":
            context = _build_context(instance, fix_type)
            full_messages.append({
                "role": "user",
                "content": f"{context}\n\n## Your Instruction\n{msg['content']}",
            })
        else:
            full_messages.append(msg)

    resp = client.chat.completions.create(model=model, messages=full_messages)
    return resp.choices[0].message.content


def make_diff(old: str, new: str, label: str = "content") -> str:
    diff = unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{label}",
        tofile=f"b/{label}",
    )
    return "".join(diff)


def strip_code_fences(text: str) -> str:
    """Strip markdown code fences if present."""
    lines = text.strip().splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


# ── Test patch helpers ────────────────────────────────────────────────

def extract_test_content(test_patch: str) -> tuple[str, str]:
    """Extract test file content and filename from a creation diff.

    Returns (content, filename). Empty strings if not a simple creation diff.
    """
    if not test_patch.strip():
        return "", ""

    filename = ""
    content_lines = []
    has_deletions = False

    for line in test_patch.splitlines():
        if line.startswith("+++ b/"):
            filename = line[6:]
        elif line.startswith("-") and not line.startswith("---"):
            has_deletions = True
        elif line.startswith("+") and not line.startswith("+++"):
            content_lines.append(line[1:])

    if has_deletions or not content_lines:
        return "", ""

    return "\n".join(content_lines), filename


def test_content_to_patch(content: str, filename: str) -> str:
    """Convert test content to a creation diff."""
    lines = content.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    diff = unified_diff([], lines, fromfile=f"a/{filename}", tofile=f"b/{filename}")
    return "".join(diff)


def extract_test_ids(content: str, filename: str) -> list[str]:
    """Extract pytest test IDs from test content."""
    module = filename.replace("/", ".").replace(".py", "")
    ids = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("def test_"):
            name = stripped.split("(")[0].replace("def ", "")
            ids.append(f"{module}::{name}")
    return ids
