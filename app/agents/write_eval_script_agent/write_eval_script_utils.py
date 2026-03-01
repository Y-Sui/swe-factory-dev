"""
Eval script extraction logic and retry wrapper for WriteEvalScriptAgent.
All prompts live in app/prompts/prompts.py.
"""

import json
from collections.abc import Callable
from os.path import join as pjoin
import os
from loguru import logger

from app.data_structures import MessageThread
from app.log import print_acr, print_patch_generation
from app.model import common
from app.prompts.prompts import (
    get_eval_script_system_prompt,
    get_eval_script_user_prompt_init,
)
import re


def get_system_prompt_eval_script() -> str:
    return get_eval_script_system_prompt()


def get_user_prompt_init_eval_script(eval_script_skeleton: str) -> str:
    return get_eval_script_user_prompt_init(eval_script_skeleton, with_downloads=False)


def write_eval_script_with_retries(
    message_thread: MessageThread,
    output_dir: str,
    test_patch: str,
    retries: int = 3,
    print_callback: Callable[[dict], None] | None = None,
) -> str:
    """Call the LLM to produce an eval.sh, retrying up to `retries` times. Returns a result message."""
    new_thread = message_thread
    script_extracted = None
    can_stop = False
    result_msg = ""
    os.makedirs(output_dir, exist_ok=True)
    for i in range(1, retries + 2):
        if i > 1:
            debug_file = pjoin(output_dir, f"debug_agent_write_eval_script_{i - 1}.json")
            with open(debug_file, "w") as f:
                json.dump(new_thread.to_msg(), f, indent=4)

        if can_stop or i > retries:
            break

        logger.info(f"Trying to extract a eval script. Try {i} of {retries}.")

        raw_script_file = pjoin(output_dir, f"agent_eval_script_raw_{i}")

        try:
            res_text, *_ = common.SELECTED_MODEL.call(new_thread.to_msg())
        except Exception as e:
            logger.error(f"LLM call failed in eval script generation try {i}: {e}")
            continue

        new_thread.add_model(res_text, [])

        logger.info(f"Raw eval script produced in try {i}. Writing to file.")

        with open(raw_script_file, "w") as f:
            f.write(res_text)

        print_patch_generation(
            res_text, f"try {i} / {retries}", print_callback=print_callback
        )

        script_extracted = extract_eval_script_from_response(res_text, output_dir, test_patch)
        can_stop = script_extracted

        if can_stop:
            result_msg = "Successfully extracted eval_script."
            print_acr(result_msg, f"eval script generation try {i}/{retries}", print_callback=print_callback)
            break
        else:
            feedback = "Failed to extract script. Please return result in defined format."
            new_thread.add_user(feedback)
            print_acr(feedback, f"Retry {i}/{retries}", print_callback=print_callback)

    if result_msg == "":
        result_msg = "Failed to extract"
    return result_msg

def _fix_new_file_patch_headers(patch: str) -> str:
    """Rewrite new-file diff headers so git apply --no-index handles them correctly.

    git apply --no-index still resolves 'a/dev/null' as a relative path and
    fails with 'error: dev/null: No such file or directory'.  The fix is to
    rewrite the header line from:
        diff --git a/dev/null b/<path>
    to:
        diff --git a//dev/null b/<path>
    The double-slash makes git treat it as an absolute /dev/null reference,
    which is what --no-index expects for new-file creation.
    """
    return re.sub(
        r'^(diff --git )a/dev/null( b/)',
        r'\1a//dev/null\2',
        patch,
        flags=re.MULTILINE,
    )


def replace_heredoc_content(original_content, test_patch):
    """Replace heredoc placeholder with actual test_patch content.
    Also ensures git apply uses --no-index and fixes new-file patch headers
    so patches adding new files (--- /dev/null) apply cleanly.
    """
    # Fix new-file diff headers before embedding the patch
    test_patch = _fix_new_file_patch_headers(test_patch)

    lines = original_content.splitlines()
    output_lines = []
    in_heredoc = False
    heredoc_delimiter = "EOF_114329324912"

    for line in lines:
        if f" - <<'{heredoc_delimiter}'" in line or f" - <<\"{heredoc_delimiter}\"" in line:
            # Ensure --no-index is present so new-file patches apply cleanly
            if "git apply" in line and "--no-index" not in line:
                line = line.replace("git apply", "git apply --no-index", 1)
            output_lines.append(line)
            in_heredoc = True
            output_lines.extend(test_patch.splitlines())
        elif in_heredoc and line.strip() == heredoc_delimiter:
            output_lines.append(line)
            in_heredoc = False
        elif not in_heredoc:
            output_lines.append(line)

    return '\n'.join(output_lines)


def extract_eval_script_from_response(res_text: str, output_dir: str, test_patch: str) -> bool:
    script_path = pjoin(output_dir, "eval.sh")
    script_skeleton_path = pjoin(output_dir, "eval_skeleton.sh")
    script_extracted = False

    def _write(content: str) -> None:
        fixed = replace_heredoc_content(content, test_patch)
        with open(script_skeleton_path, "w") as f:
            f.write(content)
        with open(script_path, "w") as f:
            f.write(fixed)

    def _clean(content: str) -> str:
        lines = content.strip().splitlines()
        if len(lines) >= 2 and "```" in lines[0] and "```" in lines[-1]:
            lines = lines[1:-1]
        return "\n".join(lines)

    # Pattern 1: <script> tags
    for content in re.findall(r"<script>([\s\S]*?)</script>", res_text):
        cleaned = _clean(content)
        if cleaned:
            _write(cleaned)
            script_extracted = True
            break

    # Pattern 2: ```script code block
    if not script_extracted:
        for content in re.findall(r"```\s*script\s*([\s\S]*?)```", res_text, re.IGNORECASE):
            cleaned = _clean(content)
            if cleaned:
                _write(cleaned)
                script_extracted = True
                break

    # Pattern 3: ```bash code block
    if not script_extracted:
        for content in re.findall(r"```\s*bash.*([\s\S]*?)```", res_text, re.IGNORECASE):
            cleaned = _clean(content)
            if cleaned:
                _write(cleaned)
                script_extracted = True
                break

    # Safety net: ensure OMNIGRIL_EXIT_CODE is present
    if script_extracted:
        for fpath in (script_path, script_skeleton_path):
            try:
                with open(fpath, "r") as f:
                    content = f.read()
            except FileNotFoundError:
                continue
            if "OMNIGRIL_EXIT_CODE" not in content:
                content += '\nrc=$?\necho "OMNIGRIL_EXIT_CODE=$rc"\n'
                with open(fpath, "w") as f:
                    f.write(content)

    return script_extracted
