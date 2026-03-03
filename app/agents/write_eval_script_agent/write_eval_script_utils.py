"""
Eval script extraction logic and retry wrapper for WriteEvalScriptAgent.
All prompts live in app/prompts/prompts.py.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from os.path import join as pjoin

from loguru import logger

from app.data_structures import MessageThread
from app.log import print_acr, print_patch_generation
from app.model import common
from app.prompts.prompts import (
    get_eval_script_system_prompt,
    get_eval_script_user_prompt_init,
)


def get_system_prompt_eval_script() -> str:
    return get_eval_script_system_prompt()


def get_user_prompt_init_eval_script(eval_script_skeleton: str) -> str:
    return get_eval_script_user_prompt_init(eval_script_skeleton, with_downloads=False)


def write_eval_script_with_retries(
    message_thread: MessageThread,
    output_dir: str,
    test_patch: str,
    test_files_content: dict[str, str] | None = None,
    repo_root: str | None = None,
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

        script_extracted = extract_eval_script_from_response(
            res_text,
            output_dir,
            test_patch,
            test_files_content=test_files_content,
            repo_root=repo_root,
        )
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

def parse_patch_to_files(patch: str) -> dict[str, str]:
    """Parse only new-file diff blocks into {relative_path: file_content}.

    Modified-file reconstruction from unified diff hunks is lossy. For modified
    files, use `_materialize_files_from_patch` instead.
    """
    if not patch or not patch.strip():
        return {}

    files: dict[str, str] = {}
    chunks = re.split(r'(?m)(?=^diff --git )', patch)

    for chunk in chunks:
        if not chunk.strip():
            continue
        m = re.search(r'^\+\+\+ b/(.+?)(?:\t.*)?$', chunk, re.MULTILINE)
        if not m:
            continue
        path = m.group(1).strip()
        if not re.search(r'^--- /dev/null$', chunk, re.MULTILINE):
            continue

        content_lines: list[str] = []
        in_hunk = False
        for line in chunk.splitlines():
            if line.startswith('@@'):
                in_hunk = True
                continue
            if not in_hunk:
                continue
            if line.startswith('+'):
                content_lines.append(line[1:])
            elif line.startswith(' '):
                content_lines.append(line[1:])
            elif line.startswith('\\ No newline at end of file'):
                pass
            elif line.startswith('-'):
                pass  # removed lines (modified files only)

        if content_lines:
            files[path] = '\n'.join(content_lines)

    return files


def _extract_target_files_from_patch(patch: str) -> list[str]:
    files = [p.split("\t")[0] for p in re.findall(r"^\+\+\+ b/(.*)$", patch, re.MULTILINE)]
    return list(dict.fromkeys([p for p in files if p and p != "/dev/null"]))


def _materialize_files_from_patch(
    patch: str,
    repo_root: str | None,
    scratch_dir: str | None = None,
) -> dict[str, str]:
    """Apply patch in a temporary git worktree and read resulting file contents."""
    if not patch or not patch.strip() or not repo_root or not os.path.isdir(repo_root):
        return {}

    target_files = _extract_target_files_from_patch(patch)
    if not target_files:
        return {}

    parent_tmp = tempfile.mkdtemp(prefix="eval_patch_", dir=scratch_dir)
    worktree_dir = os.path.join(parent_tmp, "repo")
    patch_path = os.path.join(parent_tmp, "test_patch.diff")
    with open(patch_path, "w", encoding="utf-8") as f:
        f.write(patch)

    try:
        add_worktree = subprocess.run(
            ["git", "-C", repo_root, "worktree", "add", "--detach", worktree_dir, "HEAD"],
            capture_output=True,
            text=True,
        )
        if add_worktree.returncode != 0:
            logger.warning(f"Failed to create temporary worktree: {add_worktree.stderr}")
            return {}

        apply_res = subprocess.run(
            ["git", "-C", worktree_dir, "apply", "-p1", patch_path],
            capture_output=True,
            text=True,
        )
        if apply_res.returncode != 0:
            fallback = subprocess.run(
                ["patch", "--batch", "--fuzz=5", "-p1", "-i", patch_path],
                cwd=worktree_dir,
                capture_output=True,
                text=True,
            )
            if fallback.returncode != 0:
                logger.warning(
                    "Failed to materialize test patch in temporary worktree.\n"
                    f"git apply stderr:\n{apply_res.stderr}\n"
                    f"patch stderr:\n{fallback.stderr}"
                )
                return {}

        files: dict[str, str] = {}
        for rel_path in target_files:
            abs_path = os.path.join(worktree_dir, rel_path)
            if os.path.isfile(abs_path):
                with open(abs_path, "r", encoding="utf-8") as f:
                    files[rel_path] = f.read()
        return files
    finally:
        subprocess.run(
            ["git", "-C", repo_root, "worktree", "remove", "--force", worktree_dir],
            capture_output=True,
            text=True,
        )
        shutil.rmtree(parent_tmp, ignore_errors=True)


def _generate_cat_heredoc_block(files: dict[str, str]) -> str:
    """Generate mkdir + cat heredoc commands for writing test files directly."""
    lines: list[str] = []
    dirs = sorted({os.path.dirname(path) for path in files if os.path.dirname(path)})
    if dirs:
        lines.append("mkdir -p " + " ".join(f'"{d}"' for d in dirs))

    for i, (path, content) in enumerate(files.items()):
        delim = f"EOF_TEST_{i}"
        lines.append(f"cat <<'{delim}' > \"{path}\"")
        lines.append(content)
        lines.append(delim)

    return '\n'.join(lines)


def replace_heredoc_content(
    original_content: str,
    test_patch: str,
    test_files_content: dict[str, str] | None = None,
    repo_root: str | None = None,
    scratch_dir: str | None = None,
) -> str:
    """Replace cat heredoc placeholders with actual test file contents.

    Parses the test_patch (unified diff) into {path: content}, then finds
    cat heredoc blocks in the eval script and injects the real file content.
    """
    files = dict(test_files_content or {})
    if not files:
        files = _materialize_files_from_patch(test_patch, repo_root, scratch_dir)
    if not files:
        files = parse_patch_to_files(test_patch)
    if not files:
        return original_content

    # Replace cat heredoc blocks for matching test file paths
    cat_heredoc_re = re.compile(
        r"^(cat\s+<<['\"]?(\w+)['\"]?\s*>\s*[\"']?([^\"'\n]+?)[\"']?\s*)\n[\s\S]*?\n(\2)\s*$",
        re.MULTILINE,
    )
    result = original_content
    for m in reversed(list(cat_heredoc_re.finditer(result))):
        header = m.group(1)
        delim = m.group(2)
        path = m.group(3).strip()
        if path in files:
            replacement = f"{header}\n{files[path]}\n{delim}"
            result = result[:m.start()] + replacement + result[m.end():]

    return result


def _sanitize_eval_script(content: str) -> str:
    """Remove unsafe git reset/checkout/clean lines that can undo env hotfixes."""
    out_lines: list[str] = []
    in_heredoc: str | None = None

    for line in content.splitlines():
        stripped = line.strip()
        if in_heredoc:
            out_lines.append(line)
            if stripped == in_heredoc:
                in_heredoc = None
            continue

        heredoc_start = re.search(r"cat\s+<<['\"]?([A-Za-z0-9_]+)['\"]?", line)
        if heredoc_start:
            in_heredoc = heredoc_start.group(1)
            out_lines.append(line)
            continue

        lower = stripped.lower()
        if not stripped.startswith("#"):
            if "git reset --hard" in lower:
                out_lines.append(f"# [sanitized] removed unsafe command: {stripped}")
                continue
            if "git clean -fdx" in lower:
                out_lines.append(f"# [sanitized] removed unsafe command: {stripped}")
                continue
            if "git checkout" in lower:
                # Keep data-only checkout patterns used for external test fixtures.
                if "test-data/" in lower or "tests/data/" in lower or "test_resource" in lower:
                    out_lines.append(line)
                else:
                    out_lines.append(f"# [sanitized] removed unsafe command: {stripped}")
                continue

        out_lines.append(line)

    return "\n".join(out_lines) + ("\n" if content.endswith("\n") else "")


def _ensure_pytest_addopts_override(content: str) -> str:
    """Ensure pytest commands neutralize repo-level addopts from pyproject/pytest.ini."""
    out_lines: list[str] = []
    in_heredoc: str | None = None
    pytest_cmd = re.compile(r"(^|\s)(?:\.venv/bin/pytest|pytest)\b")
    for line in content.splitlines():
        stripped = line.strip()
        # Track heredoc state — never modify lines inside heredocs.
        if in_heredoc:
            out_lines.append(line)
            if stripped == in_heredoc:
                in_heredoc = None
            continue
        heredoc_start = re.search(r"cat\s+<<['\"]?([A-Za-z0-9_]+)['\"]?", line)
        if heredoc_start:
            in_heredoc = heredoc_start.group(1)
            out_lines.append(line)
            continue
        if stripped.startswith("#"):
            out_lines.append(line)
            continue
        if pytest_cmd.search(line) and "pip install" not in line and "--override-ini=" not in line:
            out_lines.append(f'{line} --override-ini="addopts="')
        else:
            out_lines.append(line)
    return "\n".join(out_lines) + ("\n" if content.endswith("\n") else "")


def _resolve_target_test_files(
    test_patch: str,
    test_files_content: dict[str, str] | None,
) -> list[str]:
    if test_files_content:
        files = [p for p in test_files_content.keys() if p]
        return list(dict.fromkeys(files))
    return _extract_target_files_from_patch(test_patch)


def _ensure_pytest_targets_generated_files(content: str, target_files: list[str]) -> str:
    """Rewrite broad pytest invocations so they run generated test files only."""
    if not target_files:
        return content

    target_args = " ".join(f'"{p}"' for p in target_files)
    out_lines: list[str] = []
    in_heredoc: str | None = None
    pytest_cmd = re.compile(r"(?:^|\s)(?:\.venv/bin/pytest|pytest)\b")

    for line in content.splitlines():
        stripped = line.strip()
        # Track heredoc state — never modify lines inside heredocs.
        if in_heredoc:
            out_lines.append(line)
            if stripped == in_heredoc:
                in_heredoc = None
            continue
        heredoc_start = re.search(r"cat\s+<<['\"]?([A-Za-z0-9_]+)['\"]?", line)
        if heredoc_start:
            in_heredoc = heredoc_start.group(1)
            out_lines.append(line)
            continue
        if stripped.startswith("#") or "pip install" in stripped:
            out_lines.append(line)
            continue
        if not pytest_cmd.search(line):
            out_lines.append(line)
            continue

        # If the command already targets generated files, keep it.
        if any(f'"{p}"' in line or f"'{p}'" in line or f" {p}" in line for p in target_files):
            out_lines.append(line)
            continue

        match = re.search(r"(?P<prefix>.*?(?:\.venv/bin/pytest|pytest)\b)(?P<rest>.*)", line)
        if not match:
            out_lines.append(line)
            continue
        prefix = match.group("prefix")
        rest = match.group("rest")

        # Replace common broad selectors (`tests`, `tests/`) if present; otherwise prepend targets.
        replaced = re.sub(r'(?<!\S)["\']?tests/?["\']?(?=\s|$)', target_args, rest, count=1)
        if replaced == rest:
            replaced = f" {target_args}{rest}"

        out_lines.append(prefix + replaced)

    return "\n".join(out_lines) + ("\n" if content.endswith("\n") else "")


def extract_eval_script_from_response(
    res_text: str,
    output_dir: str,
    test_patch: str,
    test_files_content: dict[str, str] | None = None,
    repo_root: str | None = None,
) -> bool:
    script_path = pjoin(output_dir, "eval.sh")
    script_skeleton_path = pjoin(output_dir, "eval_skeleton.sh")
    script_extracted = False

    target_test_files = _resolve_target_test_files(test_patch, test_files_content)

    def _write(content: str) -> None:
        content = _sanitize_eval_script(content)
        fixed = replace_heredoc_content(
            content,
            test_patch,
            test_files_content=test_files_content,
            repo_root=repo_root,
            scratch_dir=output_dir,
        )
        fixed = _sanitize_eval_script(fixed)
        fixed = _ensure_pytest_targets_generated_files(fixed, target_test_files)
        fixed = _ensure_pytest_addopts_override(fixed)
        content = _ensure_pytest_targets_generated_files(content, target_test_files)
        content = _ensure_pytest_addopts_override(content)
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


def write_eval_script_from_content(
    content: str,
    output_dir: str,
    test_patch: str,
    test_files_content: dict[str, str] | None = None,
    repo_root: str | None = None,
) -> None:
    """Write eval.sh and eval_skeleton.sh from pre-built content, applying full post-processing."""
    target_test_files = _resolve_target_test_files(test_patch, test_files_content)

    skeleton = _sanitize_eval_script(content)
    skeleton = _ensure_pytest_targets_generated_files(skeleton, target_test_files)
    skeleton = _ensure_pytest_addopts_override(skeleton)

    fixed = replace_heredoc_content(
        content, test_patch,
        test_files_content=test_files_content,
        repo_root=repo_root,
        scratch_dir=output_dir,
    )
    fixed = _sanitize_eval_script(fixed)
    fixed = _ensure_pytest_targets_generated_files(fixed, target_test_files)
    fixed = _ensure_pytest_addopts_override(fixed)

    def _finalize(s: str) -> str:
        if "OMNIGRIL_EXIT_CODE" not in s:
            s += '\nrc=$?\necho "OMNIGRIL_EXIT_CODE=$rc"\n'
        return s

    with open(pjoin(output_dir, "eval_skeleton.sh"), "w") as f:
        f.write(_finalize(skeleton))
    with open(pjoin(output_dir, "eval.sh"), "w") as f:
        f.write(_finalize(fixed))
