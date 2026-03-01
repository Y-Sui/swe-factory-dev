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

# Re-export for any callers that still reference write_test_utils directly
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

    idx = patch_content.find('diff --git')
    if idx > 0:
        patch_content = patch_content[idx:]
    elif idx < 0:
        return None, []

    test_files = re.findall(r'\+\+\+ b/(.*)', patch_content)

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

        refined_patch, refined_files = extract_test_patch_from_response(refined_text, round_dir)

        if refined_patch and len(refined_files) > 0:
            current_patch = refined_patch
            current_files = refined_files
            logger.info(f"Reflexion round {round_num}: refined to {len(refined_files)} file(s).")
        else:
            logger.warning(f"Reflexion round {round_num}: failed to extract refined patch, keeping previous version.")
            break

    return current_patch, current_files
