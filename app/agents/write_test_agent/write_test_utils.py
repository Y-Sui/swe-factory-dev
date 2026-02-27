"""
Prompts, patch summarizer, extraction logic, and retry loop for WriteTestAgent.
"""

import json
import os
import re
from collections.abc import Callable
from copy import deepcopy
from os.path import join as pjoin

from loguru import logger

from app.data_structures import MessageThread
from app.log import print_acr, print_patch_generation
from app.model import common
from app.task import Task


SYSTEM_PROMPT_WRITE_TEST = """You are an expert software testing engineer. Your task is to generate Python test files that verify the changes described in a pull request.

You will receive:
- **Problem statement**: The description of the issue or feature being addressed.
- **Patch content**: The code changes (unified diff) made to resolve the issue.
- **Repository info**: Basic information about the target repository.
- **Guidance** (if available): Feedback from a test analysis agent on how to improve previously generated tests.

### Your Responsibilities:
1. Analyze the problem statement and patch to understand what behavior changed.
2. Write pytest-compatible test files that specifically test the changed behavior.
3. Include both positive tests (verifying the fix works) and negative tests (verifying the old broken behavior is gone) where appropriate.
4. Tests should be focused, minimal, and deterministic — avoid testing unrelated functionality.
5. Import paths and module names must match the repository structure shown in the patch.
6. Python module/package names CANNOT contain hyphens (`-`). Directory names with hyphens (e.g., `miroflow-agent`) are not valid Python import paths. Use relative imports from within the package or adjust `sys.path`/`PYTHONPATH` and import from the correct Python package name.

### Output Format:
Return your generated tests as a unified diff that creates new test files. Wrap the diff in `<test_patch>` tags.

The diff must:
- Use `diff --git` format with `/dev/null` as the `a/` side (since these are new files)
- Include proper `---` and `+++` headers
- Include `@@ -0,0 +1,N @@` hunk headers

Example:
<test_patch>
diff --git a/dev/null b/tests/test_fix_issue.py
--- /dev/null
+++ b/tests/test_fix_issue.py
@@ -0,0 +1,25 @@
+import pytest
+from mymodule import my_function
+
+
+def test_my_function_returns_correct_value():
+    result = my_function(42)
+    assert result == expected_value
+
+
+def test_my_function_handles_edge_case():
+    result = my_function(0)
+    assert result is not None
</test_patch>
"""


USER_PROMPT_WRITE_TEST = """Generate test files for the following pull request.

### Repository Info:
{repo_info}

### Problem Statement:
{problem_statement}

### Patch (code changes):
{patch_content}

### Existing Tests (if any):
{existing_tests}

Based on the above, generate pytest-compatible test file(s) as a unified diff wrapped in `<test_patch>` tags.
Focus on testing the specific behavior changes introduced by the patch. If existing tests are provided, generate **additional** complementary tests that cover cases not already tested — do NOT duplicate existing test coverage. Make sure test file paths are reasonable for the repository structure (e.g., `tests/test_*.py` or similar conventions visible in the patch paths).
"""


def summarize_large_patch(patch: str, max_chars: int = 15000) -> str:
    """For patches >max_chars, extract per-file hunk headers + key changed lines."""
    if len(patch) <= max_chars:
        return patch

    chunks = re.split(r'(?m)(?=^diff --git )', patch)
    summarized_parts = []
    total_chars = 0

    for chunk in chunks:
        if not chunk.strip():
            continue
        # Small chunks: include verbatim
        if len(chunk) <= 2000:
            summarized_parts.append(chunk)
            total_chars += len(chunk)
        else:
            # Extract header + hunk headers + first few changed lines per hunk
            lines = chunk.splitlines(keepends=True)
            kept_lines = []
            in_hunk = False
            hunk_lines_kept = 0
            for line in lines:
                if line.startswith('diff --git') or line.startswith('---') or line.startswith('+++'):
                    kept_lines.append(line)
                    in_hunk = False
                elif line.startswith('@@'):
                    kept_lines.append(line)
                    in_hunk = True
                    hunk_lines_kept = 0
                elif in_hunk and (line.startswith('+') or line.startswith('-')):
                    if hunk_lines_kept < 30:
                        kept_lines.append(line)
                        hunk_lines_kept += 1
                    elif hunk_lines_kept == 30:
                        kept_lines.append('... (truncated)\n')
                        hunk_lines_kept += 1

            summary = ''.join(kept_lines)
            summarized_parts.append(summary)
            total_chars += len(summary)

        if total_chars > max_chars:
            break

    return ''.join(summarized_parts)


def repair_hunk_headers(patch: str) -> str:
    """Fix common LLM-generated unified diff issues.

    1. ``diff --git a/dev/null b/<path>`` → proper new-file header with
       ``new file mode 100644``.
    2. Incorrect ``@@ -A,B +C,D @@`` line counts → recounted from actual
       ``+``/``-``/context lines.
    3. Ensures trailing newline so ``git apply`` doesn't choke.
    """
    # --- Pass 1: fix new-file diff headers ---
    raw_lines = patch.splitlines(keepends=True)
    fixed_lines: list[str] = []
    for line in raw_lines:
        stripped = line.rstrip('\n\r')
        # "diff --git a/dev/null b/<path>" → proper new-file header
        m = re.match(r'^diff --git a/dev/null b/(.*)', stripped)
        if m:
            path = m.group(1)
            fixed_lines.append(f'diff --git a/{path} b/{path}\n')
            fixed_lines.append('new file mode 100644\n')
        # "--- dev/null" (missing leading /) → "--- /dev/null"
        elif stripped == '--- dev/null':
            fixed_lines.append('--- /dev/null\n')
        else:
            fixed_lines.append(line)

    # --- Pass 2: recount hunk line numbers ---
    out: list[str] = []
    hunk_start_idx: int | None = None
    old_count = 0
    new_count = 0

    def _flush_hunk():
        nonlocal hunk_start_idx, old_count, new_count
        if hunk_start_idx is None:
            return
        header = out[hunk_start_idx]
        m2 = re.match(r'^(@@ -(\d+),?\d* \+(\d+),?)\d*(.*)', header)
        if m2:
            old_start = m2.group(2)
            new_start = m2.group(3)
            rest = m2.group(4)  # trailing @@ and optional section heading
            out[hunk_start_idx] = f"@@ -{old_start},{old_count} +{new_start},{new_count}{rest}"
            if not out[hunk_start_idx].endswith('\n'):
                out[hunk_start_idx] += '\n'
        hunk_start_idx = None
        old_count = 0
        new_count = 0

    for line in fixed_lines:
        stripped = line.rstrip('\n\r')
        if stripped.startswith('diff --git'):
            _flush_hunk()
            out.append(line)
        elif stripped.startswith('---') or stripped.startswith('+++') or stripped.startswith('new file mode'):
            _flush_hunk()
            out.append(line)
        elif stripped.startswith('@@'):
            _flush_hunk()
            hunk_start_idx = len(out)
            out.append(line)
            old_count = 0
            new_count = 0
        elif hunk_start_idx is not None:
            if stripped.startswith('+'):
                new_count += 1
            elif stripped.startswith('-'):
                old_count += 1
            else:
                # context line (or empty line treated as context)
                old_count += 1
                new_count += 1
            out.append(line)
        else:
            out.append(line)

    _flush_hunk()
    result = ''.join(out)
    # git apply requires a trailing newline
    if result and not result.endswith('\n'):
        result += '\n'
    return result


def extract_test_patch_from_response(res_text: str, output_dir: str) -> tuple[str | None, list[str]]:
    """Extract test patch from LLM response. Returns (patch_str, test_file_list)."""
    patch_content = None

    # Pattern 1: <test_patch> tags
    matches = re.findall(r"<test_patch>([\s\S]*?)</test_patch>", res_text)
    for content in matches:
        clean = content.strip()
        if clean:
            # Strip wrapping ```diff ... ``` if present
            lines = clean.splitlines()
            if len(lines) >= 2 and '```' in lines[0] and '```' in lines[-1]:
                lines = lines[1:-1]
            clean = '\n'.join(lines)
            if 'diff --git' in clean or '---' in clean:
                patch_content = clean
                break

    # Pattern 2: ```diff code blocks
    if not patch_content:
        diff_blocks = re.findall(r"```\s*diff\s*([\s\S]*?)```", res_text, re.IGNORECASE)
        for content in diff_blocks:
            clean = content.strip()
            if clean and ('diff --git' in clean or '---' in clean):
                patch_content = clean
                break

    if not patch_content:
        return None, []

    # Ensure it starts with diff --git
    idx = patch_content.find('diff --git')
    if idx > 0:
        patch_content = patch_content[idx:]
    elif idx < 0:
        return None, []

    # Fix hunk headers that LLMs often get wrong
    patch_content = repair_hunk_headers(patch_content)

    # Extract test file paths from +++ b/... lines
    test_files = re.findall(r'\+\+\+ b/(.*)', patch_content)

    # Save the generated patch
    os.makedirs(output_dir, exist_ok=True)
    patch_path = pjoin(output_dir, "generated_test_patch.diff")
    with open(patch_path, "w") as f:
        f.write(patch_content)

    return patch_content, test_files


def write_test_with_retries(
    msg_thread: MessageThread,
    output_dir: str,
    retries: int = 3,
    print_callback: Callable[[dict], None] | None = None,
) -> tuple[str, str | None, list[str], bool]:
    """
    Call LLM to generate test patch, with retries on failure.
    Returns (result_msg, patch_str, test_file_list, success).
    """
    new_thread = msg_thread
    patch_content = None
    test_files = []
    can_stop = False
    result_msg = ""
    os.makedirs(output_dir, exist_ok=True)

    for i in range(1, retries + 2):
        if i > 1:
            debug_file = pjoin(output_dir, f"debug_agent_write_test_{i - 1}.json")
            with open(debug_file, "w") as f:
                json.dump(new_thread.to_msg(), f, indent=4)

        if can_stop or i > retries:
            break

        logger.info(f"Trying to generate test patch. Try {i} of {retries}.")

        raw_output_file = pjoin(output_dir, f"agent_write_test_raw_{i}")

        # Call the model
        res_text, *_ = common.SELECTED_MODEL.call(new_thread.to_msg())
        new_thread.add_model(res_text, [])

        logger.info(f"Raw test generation output produced in try {i}. Writing to file.")
        with open(raw_output_file, "w") as f:
            f.write(res_text)

        print_patch_generation(
            res_text, f"test gen try {i} / {retries}", print_callback=print_callback
        )

        # Try to extract the test patch
        patch_content, test_files = extract_test_patch_from_response(res_text, output_dir)

        can_stop = patch_content is not None and len(test_files) > 0

        if can_stop:
            result_msg = "Successfully generated test patch."
            print_acr(result_msg, f"test generation try {i}/{retries}", print_callback=print_callback)
            break
        else:
            feedback = "Failed to extract a valid test patch from your response. Please return the test patch as a unified diff wrapped in <test_patch> tags, starting with 'diff --git'."
            new_thread.add_user(feedback)
            print_acr(feedback, f"Retry {i}/{retries}", print_callback=print_callback)

    if result_msg == '':
        result_msg = 'Failed to generate test patch.'

    return result_msg, patch_content, test_files, can_stop
