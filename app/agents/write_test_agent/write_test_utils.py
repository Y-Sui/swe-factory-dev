"""
Patch summarizer, extraction logic, reflexion loop, and retry logic
for WriteTestAgent.

All prompts live in app/prompts/prompts.py.
"""

import json
import os
import re
from collections.abc import Callable
from os.path import join as pjoin

from loguru import logger

from app.data_structures import MessageThread
from app.log import print_acr, print_patch_generation
from app.model import common
from app.prompts.prompts import (
    get_test_system_prompt,
    TEST_USER_PROMPT,
    TEST_REFLEXION_CRITIQUE_PROMPT,
    TEST_REFLEXION_REFINE_PROMPT,
)

# Re-exports for callers that reference write_test_utils directly
USER_PROMPT_WRITE_TEST = TEST_USER_PROMPT
REFLEXION_CRITIQUE_PROMPT = TEST_REFLEXION_CRITIQUE_PROMPT
REFLEXION_REFINE_PROMPT = TEST_REFLEXION_REFINE_PROMPT


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
        if len(chunk) <= 2000:
            summarized_parts.append(chunk)
            total_chars += len(chunk)
        else:
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
# Test file extraction and patch generation
# ---------------------------------------------------------------------------

def extract_test_files_from_response(res_text: str) -> dict[str, str]:
    """Extract test files from LLM response using <test_file path="..."> tags.
    Returns a dict mapping relative path -> file content.
    """
    files: dict[str, str] = {}
    for m in re.finditer(r'<test_file\s+path="([^"]+)">([\s\S]*?)</test_file>', res_text):
        path = m.group(1).strip()
        content = m.group(2)
        # Strip a single leading newline if present (tag formatting artefact)
        if content.startswith('\n'):
            content = content[1:]
        if path:
            files[path] = content
    return files


def build_patch_from_files(files: dict[str, str], output_dir: str) -> tuple[str, list[str]]:
    """Write files to output_dir and produce a unified diff using the system diff command.
    Returns (patch_str, list_of_relative_paths).
    """
    import subprocess

    os.makedirs(output_dir, exist_ok=True)
    patch_parts: list[str] = []

    for rel_path, content in files.items():
        full_path = pjoin(output_dir, rel_path)
        parent = os.path.dirname(full_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)

        # Use `diff /dev/null <file>` to produce a standard unified diff for a new file.
        # Exit code 1 = differences found (expected for new files), 2 = error.
        # Do NOT use --label for /dev/null — git apply needs to see `--- /dev/null` to
        # recognise this as a new-file creation. Only fix the +++ line to use b/<rel_path>.
        result = subprocess.run(
            ["diff", "-u", "/dev/null", full_path],
            capture_output=True, text=True,
        )
        if result.returncode == 2:
            logger.warning(f"diff failed for {rel_path}: {result.stderr}")
            continue

        # Replace the absolute path in the +++ line with the canonical b/<rel_path> form.
        stdout = result.stdout.replace(f"+++ {full_path}", f"+++ b/{rel_path}", 1)
        # Prepend the git diff header so git apply recognises it
        diff_block = f"diff --git a/{rel_path} b/{rel_path}\n" + stdout
        patch_parts.append(diff_block)

    patch_str = "\n".join(patch_parts)
    patch_path = pjoin(output_dir, "generated_test_patch.diff")
    with open(patch_path, "w", encoding="utf-8") as f:
        f.write(patch_str)

    return patch_str, list(files.keys())


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
    Call LLM to generate test files, with retries on format extraction failure.
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

        try:
            res_text, *_ = common.SELECTED_MODEL.call(new_thread.to_msg())
        except Exception as e:
            logger.error(f"LLM call failed in test generation try {i}: {e}")
            continue
        new_thread.add_model(res_text, [])

        logger.info(f"Raw test generation output produced in try {i}. Writing to file.")
        with open(raw_output_file, "w") as f:
            f.write(res_text)

        print_patch_generation(res_text, f"test gen try {i} / {retries}", print_callback=print_callback)

        extracted_files = extract_test_files_from_response(res_text)
        if extracted_files:
            patch_content, test_files = build_patch_from_files(extracted_files, output_dir)
            can_stop = True
        else:
            patch_content, test_files = None, []

        if can_stop:
            result_msg = "Successfully generated test files."
            print_acr(result_msg, f"test generation try {i}/{retries}", print_callback=print_callback)
            break
        else:
            feedback = 'Failed to extract test files from your response. Please return each test file wrapped in <test_file path="relative/path/to/test.py"> tags containing the raw file content (no diff syntax).'
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
      1. Critique the current test patch.
      2. If "TESTS_APPROVED" in critique, stop early.
      3. Otherwise, refine based on critique.

    Returns (refined_patch, refined_test_files).
    """
    current_patch = generated_patch
    current_files = re.findall(r'\+\+\+ b/(.*)', current_patch)

    for round_num in range(1, max_rounds + 1):
        round_dir = pjoin(output_dir, f"reflexion_round_{round_num}")
        os.makedirs(round_dir, exist_ok=True)

        critique_prompt = TEST_REFLEXION_CRITIQUE_PROMPT.format(
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

        with open(pjoin(round_dir, "critique.txt"), "w") as f:
            f.write(critique_text)

        logger.info(f"Reflexion round {round_num}: critique completed.")

        if "TESTS_APPROVED" in critique_text:
            logger.info(f"Reflexion round {round_num}: tests approved, stopping early.")
            break

        refine_prompt = TEST_REFLEXION_REFINE_PROMPT.format(
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

        with open(pjoin(round_dir, "refinement_raw.txt"), "w") as f:
            f.write(refined_text)

        extracted_files = extract_test_files_from_response(refined_text)
        if extracted_files:
            refined_patch, refined_files = build_patch_from_files(extracted_files, round_dir)
            current_patch = refined_patch
            current_files = refined_files
            logger.info(f"Reflexion round {round_num}: refined to {len(refined_files)} file(s).")
        else:
            logger.warning(f"Reflexion round {round_num}: failed to extract refined files, keeping previous version.")
            break

    return current_patch, current_files
