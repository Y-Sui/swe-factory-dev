"""
Prompts, patch summarizer, extraction logic, reflexion loop, and retry logic
for WriteTestAgent.

Supports multi-language test generation (Python, JavaScript, Java, TypeScript)
and two test categories:
  - Fail-to-Pass (F2P): tests that FAIL before the gold patch and PASS after.
  - Pass-to-Pass (P2P): regression tests that PASS both before and after the patch.
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


# ---------------------------------------------------------------------------
# Multi-language system prompts
# ---------------------------------------------------------------------------

# Base responsibilities shared by all languages
_SHARED_RESPONSIBILITIES = """
### Your Responsibilities:
1. Analyze the problem statement and patch to understand what behavior changed.
2. Generate **two categories** of tests:
   - **Fail-to-Pass (F2P) tests**: These MUST fail on the codebase BEFORE the gold patch is applied and PASS after the patch. They directly verify the bug fix or new behavior.
   - **Pass-to-Pass (P2P) tests**: These MUST pass both BEFORE and AFTER the gold patch. They are regression tests ensuring that existing related functionality is not broken by the change.
3. All tests must be **directly relevant** to the PR and its related issues — do NOT generate tests targeting unrelated functions or modules not mentioned in the patch.
4. Tests should be focused, minimal, and deterministic.
5. Import paths and module names must match the repository structure shown in the patch.

### Output Format:
Return your generated tests as a unified diff that creates new test files. Wrap the diff in `<test_patch>` tags.
Use comments in the test files to clearly mark F2P vs P2P tests, e.g.:
  # --- F2P: tests that should fail before patch, pass after ---
  # --- P2P: regression tests that should always pass ---

The diff must:
- Use `diff --git` format with `/dev/null` as the `a/` side (since these are new files)
- Include proper `---` and `+++` headers
- Include `@@ -0,0 +1,N @@` hunk headers
"""

SYSTEM_PROMPT_PYTHON = (
    """You are an expert software testing engineer. Your task is to generate Python pytest-compatible test files that verify the changes described in a pull request.

You will receive:
- **Problem statement**: The description of the issue or feature being addressed.
- **Patch content**: The code changes (unified diff) made to resolve the issue.
- **Repository info**: Basic information about the target repository.
- **Guidance** (if available): Feedback from a test analysis agent on how to improve previously generated tests.
"""
    + _SHARED_RESPONSIBILITIES
    + """
Example:
<test_patch>
diff --git a/dev/null b/tests/test_fix_issue.py
--- /dev/null
+++ b/tests/test_fix_issue.py
@@ -0,0 +1,25 @@
+import pytest
+from mymodule import my_function
+
+# --- F2P: tests that should fail before patch, pass after ---
+def test_my_function_returns_correct_value():
+    result = my_function(42)
+    assert result == expected_value
+
+# --- P2P: regression tests that should always pass ---
+def test_my_function_handles_edge_case():
+    result = my_function(0)
+    assert result is not None
</test_patch>
"""
)

SYSTEM_PROMPT_JAVASCRIPT = (
    """You are an expert software testing engineer. Your task is to generate JavaScript test files (using Jest or Mocha) that verify the changes described in a pull request.

You will receive:
- **Problem statement**: The description of the issue or feature being addressed.
- **Patch content**: The code changes (unified diff) made to resolve the issue.
- **Repository info**: Basic information about the target repository.
- **Guidance** (if available): Feedback from a test analysis agent on how to improve previously generated tests.
"""
    + _SHARED_RESPONSIBILITIES
    + """
Example:
<test_patch>
diff --git a/dev/null b/tests/fix_issue.test.js
--- /dev/null
+++ b/tests/fix_issue.test.js
@@ -0,0 +1,20 @@
+const { myFunction } = require('../src/myModule');
+
+// --- F2P: tests that should fail before patch, pass after ---
+describe('myFunction fix', () => {
+  test('returns correct value after fix', () => {
+    expect(myFunction(42)).toBe(expectedValue);
+  });
+});
+
+// --- P2P: regression tests that should always pass ---
+describe('myFunction regression', () => {
+  test('handles edge case', () => {
+    expect(myFunction(0)).not.toBeNull();
+  });
+});
</test_patch>
"""
)

SYSTEM_PROMPT_JAVA = (
    """You are an expert software testing engineer. Your task is to generate Java JUnit test files that verify the changes described in a pull request.

You will receive:
- **Problem statement**: The description of the issue or feature being addressed.
- **Patch content**: The code changes (unified diff) made to resolve the issue.
- **Repository info**: Basic information about the target repository.
- **Guidance** (if available): Feedback from a test analysis agent on how to improve previously generated tests.
"""
    + _SHARED_RESPONSIBILITIES
    + """
Example:
<test_patch>
diff --git a/dev/null b/src/test/java/com/example/FixIssueTest.java
--- /dev/null
+++ b/src/test/java/com/example/FixIssueTest.java
@@ -0,0 +1,25 @@
+package com.example;
+
+import org.junit.jupiter.api.Test;
+import static org.junit.jupiter.api.Assertions.*;
+
+// --- F2P: tests that should fail before patch, pass after ---
+class FixIssueTest {
+    @Test
+    void testReturnsCorrectValue() {
+        assertEquals(expectedValue, MyClass.myMethod(42));
+    }
+
+    // --- P2P: regression tests that should always pass ---
+    @Test
+    void testHandlesEdgeCase() {
+        assertNotNull(MyClass.myMethod(0));
+    }
+}
</test_patch>
"""
)

SYSTEM_PROMPT_TYPESCRIPT = (
    """You are an expert software testing engineer. Your task is to generate TypeScript test files (using Jest or Vitest) that verify the changes described in a pull request.

You will receive:
- **Problem statement**: The description of the issue or feature being addressed.
- **Patch content**: The code changes (unified diff) made to resolve the issue.
- **Repository info**: Basic information about the target repository.
- **Guidance** (if available): Feedback from a test analysis agent on how to improve previously generated tests.
"""
    + _SHARED_RESPONSIBILITIES
    + """
Example:
<test_patch>
diff --git a/dev/null b/tests/fix_issue.test.ts
--- /dev/null
+++ b/tests/fix_issue.test.ts
@@ -0,0 +1,20 @@
+import { myFunction } from '../src/myModule';
+
+// --- F2P: tests that should fail before patch, pass after ---
+describe('myFunction fix', () => {
+  it('returns correct value after fix', () => {
+    expect(myFunction(42)).toBe(expectedValue);
+  });
+});
+
+// --- P2P: regression tests that should always pass ---
+describe('myFunction regression', () => {
+  it('handles edge case', () => {
+    expect(myFunction(0)).not.toBeNull();
+  });
+});
</test_patch>
"""
)

# Keep backward-compatible alias
SYSTEM_PROMPT_WRITE_TEST = SYSTEM_PROMPT_PYTHON


def get_test_system_prompt(language: str) -> str:
    """Select the language-specific system prompt for test generation."""
    lang = (language or "").lower().strip()
    if lang in ("javascript", "js", "nodejs"):
        return SYSTEM_PROMPT_JAVASCRIPT
    elif lang in ("java",):
        return SYSTEM_PROMPT_JAVA
    elif lang in ("typescript", "ts"):
        return SYSTEM_PROMPT_TYPESCRIPT
    # Default to Python for unknown languages
    return SYSTEM_PROMPT_PYTHON


# ---------------------------------------------------------------------------
# User prompt template (language-agnostic)
# ---------------------------------------------------------------------------

USER_PROMPT_WRITE_TEST = """Generate test files for the following pull request.

### Repository Info:
{repo_info}

### Problem Statement:
{problem_statement}

### Patch (code changes):
{patch_content}

### Existing Tests (if any):
{existing_tests}

Based on the above, generate test file(s) as a unified diff wrapped in `<test_patch>` tags.

**Important**: You MUST generate two categories of tests:
1. **Fail-to-Pass (F2P)**: Tests that specifically verify the bug fix / new behavior. These tests MUST fail on the codebase before the patch and pass after the patch is applied.
2. **Pass-to-Pass (P2P)**: Regression tests for related functionality that should pass both before and after the patch.

All tests must be directly relevant to the PR and its related issues — do NOT test unrelated functions. If existing tests are provided, generate **additional** complementary tests that cover cases not already tested — do NOT duplicate existing test coverage. Make sure test file paths are reasonable for the repository structure.
"""


# ---------------------------------------------------------------------------
# Reflexion prompts for self-critique and refinement
# ---------------------------------------------------------------------------

REFLEXION_CRITIQUE_PROMPT = """You are reviewing auto-generated test files for quality. Analyze the following generated tests and provide a detailed critique.

### Problem Statement:
{problem_statement}

### Gold Patch (code changes):
{patch_content}

### Generated Test Patch:
{test_patch}

### Review Criteria:
1. **F2P correctness**: Would the F2P tests actually FAIL on the code BEFORE the patch? Do they test the exact behavior that the patch changes?
2. **P2P correctness**: Would the P2P tests actually PASS both before and after the patch? Are they testing stable, related behavior?
3. **Relevance**: Are ALL tests directly related to the PR and its issues? Flag any tests targeting unrelated functions.
4. **Import paths**: Do the import paths match the actual repository structure visible in the patch?
5. **Determinism**: Are tests free of randomness, timing dependencies, or external service calls?
6. **Edge cases**: Are important edge cases covered?

Provide your critique as structured text. If the tests are high-quality and no changes are needed, explicitly say "TESTS_APPROVED".
"""

REFLEXION_REFINE_PROMPT = """Based on the following critique of the generated tests, produce an improved version.

### Critique:
{critique}

### Original Generated Test Patch:
{test_patch}

### Problem Statement:
{problem_statement}

### Gold Patch:
{patch_content}

Generate improved test files as a unified diff wrapped in `<test_patch>` tags. Address every issue raised in the critique. Ensure:
- F2P tests truly fail before the patch and pass after
- P2P tests truly pass both before and after
- All tests are relevant to the PR
- Import paths are correct
"""


# ---------------------------------------------------------------------------
# Patch summarizer
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Test patch extraction
# ---------------------------------------------------------------------------

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

    # Extract test file paths from +++ b/... lines
    test_files = re.findall(r'\+\+\+ b/(.*)', patch_content)

    # Save the generated patch
    os.makedirs(output_dir, exist_ok=True)
    patch_path = pjoin(output_dir, "generated_test_patch.diff")
    with open(patch_path, "w") as f:
        f.write(patch_content)

    return patch_content, test_files


# ---------------------------------------------------------------------------
# Initial test generation with retries
# ---------------------------------------------------------------------------

def write_test_with_retries(
    msg_thread: MessageThread,
    output_dir: str,
    retries: int = 3,
    print_callback: Callable[[dict], None] | None = None,
) -> tuple[str, str | None, list[str], bool]:
    """
    Call LLM to generate test patch, with retries on format extraction failure.
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
        try:
            res_text, *_ = common.SELECTED_MODEL.call(new_thread.to_msg())
        except Exception as e:
            logger.error(f"LLM call failed in test generation try {i}: {e}")
            continue
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


# ---------------------------------------------------------------------------
# Reflexion: multi-round self-critique and refinement
# ---------------------------------------------------------------------------

def refine_tests_with_reflexion(
    msg_thread: MessageThread,
    generated_patch: str,
    problem_statement: str,
    code_patch: str,
    output_dir: str,
    max_rounds: int = 2,
    print_callback: Callable[[dict], None] | None = None,
) -> tuple[str, list[str]]:
    """
    Run multi-round reflexion to improve generated tests.

    Each round:
      1. Ask LLM to critique the current test patch (F2P/P2P correctness, relevance, imports).
      2. If critique says tests are approved, stop early.
      3. Otherwise, ask LLM to refine based on critique.

    Returns (refined_patch, refined_test_files).
    """
    current_patch = generated_patch
    current_files = re.findall(r'\+\+\+ b/(.*)', current_patch)

    for round_num in range(1, max_rounds + 1):
        round_dir = pjoin(output_dir, f"reflexion_round_{round_num}")
        os.makedirs(round_dir, exist_ok=True)

        # --- Step 1: Self-critique ---
        critique_prompt = REFLEXION_CRITIQUE_PROMPT.format(
            problem_statement=summarize_large_patch(problem_statement, 5000),
            patch_content=summarize_large_patch(code_patch),
            test_patch=current_patch,
        )
        msg_thread.add_user(critique_prompt)

        try:
            critique_text, *_ = common.SELECTED_MODEL.call(msg_thread.to_msg())
        except Exception as e:
            logger.error(f"LLM call failed in reflexion critique round {round_num}: {e}")
            break
        msg_thread.add_model(critique_text, [])

        # Save critique
        with open(pjoin(round_dir, "critique.txt"), "w") as f:
            f.write(critique_text)

        logger.info(f"Reflexion round {round_num}: critique completed.")

        # Early exit if tests are approved
        if "TESTS_APPROVED" in critique_text:
            logger.info(f"Reflexion round {round_num}: tests approved, stopping early.")
            break

        # --- Step 2: Refine based on critique ---
        refine_prompt = REFLEXION_REFINE_PROMPT.format(
            critique=critique_text,
            test_patch=current_patch,
            problem_statement=summarize_large_patch(problem_statement, 5000),
            patch_content=summarize_large_patch(code_patch),
        )
        msg_thread.add_user(refine_prompt)

        try:
            refined_text, *_ = common.SELECTED_MODEL.call(msg_thread.to_msg())
        except Exception as e:
            logger.error(f"LLM call failed in reflexion refine round {round_num}: {e}")
            break
        msg_thread.add_model(refined_text, [])

        # Save raw refinement output
        with open(pjoin(round_dir, "refinement_raw.txt"), "w") as f:
            f.write(refined_text)

        # Extract refined test patch
        refined_patch, refined_files = extract_test_patch_from_response(refined_text, round_dir)

        if refined_patch and len(refined_files) > 0:
            current_patch = refined_patch
            current_files = refined_files
            logger.info(f"Reflexion round {round_num}: refined to {len(refined_files)} file(s).")
        else:
            logger.warning(f"Reflexion round {round_num}: failed to extract refined patch, keeping previous version.")
            break

    return current_patch, current_files
