#!/usr/bin/env python3

import argparse
import glob as glob_mod
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from datetime import datetime

# Make app/ importable from the project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from utils import (
    Repo,
    fetch_commit_info,
    fetch_file_at_commit,
    extract_changed_context,
    reconstruct_source_from_patch,
    extract_problem_statement_and_hints,
    extract_problem_statement_and_hints_with_official_github_api,
    extract_problem_statement_from_pr,
    CODE_CHANGE_TITLE_RE,
)

# Commits touching more than DECOMPOSE_THRESHOLD py files are split into chunks of CHUNK_SIZE.
DECOMPOSE_THRESHOLD = 6
CHUNK_SIZE = 4

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def _split_patch_by_file(patch: str) -> dict[str, str]:
    """Split a unified diff string into per-file diffs, keyed by filename."""
    files = {}
    current_file = None
    current_lines = []

    for line in patch.split("\n"):
        if line.startswith("diff --git a/"):
            if current_file is not None:
                files[current_file] = "\n".join(current_lines)
            parts = line.split(" b/")
            current_file = parts[-1].strip() if len(parts) >= 2 else None
            current_lines = [line]
        elif current_file is not None:
            current_lines.append(line)

    if current_file is not None:
        files[current_file] = "\n".join(current_lines)

    return files


def _reassemble_patch(file_diffs: dict[str, str], file_set: set[str]) -> str:
    """Reassemble a patch string from per-file diffs for the given files."""
    parts = [file_diffs[f] for f in file_diffs if f in file_set]
    return "\n".join(parts) + "\n" if parts else ""


def _match_test_to_code_files(test_file: str, code_files: set[str]) -> bool:
    """Check if a test file is related to any of the code files by name."""
    test_base = os.path.basename(test_file).replace(".py", "")
    # strip test_ prefix and _test suffix
    test_base_stripped = test_base.replace("test_", "").replace("_test", "")

    for code_file in code_files:
        code_base = os.path.basename(code_file).replace(".py", "")
        if test_base_stripped == code_base:
            return True
        # check directory overlap
        test_dir = os.path.dirname(test_file)
        code_dir = os.path.dirname(code_file)
        if test_dir and code_dir and (test_dir in code_dir or code_dir in test_dir):
            return True
    return False


def _decompose_instance(instance: dict, context_files: list[str]) -> list[dict]:
    """
    If a commit touches more than DECOMPOSE_THRESHOLD Python files,
    split it into chunks of CHUNK_SIZE sub-instances. Each sub-instance gets
    only the patch, test_patch, and patch_context relevant to its file group.

    Returns the original instance unchanged if under the threshold.
    """
    context = instance["patch_context"]
    if len(context) <= DECOMPOSE_THRESHOLD:
        return [instance]

    # Pre-split patch and test_patch by file
    patch_by_file = _split_patch_by_file(instance.get("patch", ""))
    test_by_file = _split_patch_by_file(instance.get("test_patch", ""))

    # Chunk patch_context and context_files together
    chunks = []
    for i in range(0, len(context), CHUNK_SIZE):
        chunks.append((
            context[i:i + CHUNK_SIZE],
            context_files[i:i + CHUNK_SIZE],
        ))

    sub_instances = []
    assigned_tests = set()
    for idx, (ctx_chunk, files_chunk) in enumerate(chunks):
        file_set = set(files_chunk)
        sub_patch = _reassemble_patch(patch_by_file, file_set)

        # Match test files to this chunk's code files
        chunk_tests = set()
        for test_file in test_by_file:
            if _match_test_to_code_files(test_file, file_set):
                chunk_tests.add(test_file)
                assigned_tests.add(test_file)
        sub_test_patch = _reassemble_patch(test_by_file, chunk_tests)

        sub = {
            **instance,
            "instance_id": f"{instance['instance_id']}-g{idx}",
            "patch": sub_patch,
            "test_patch": sub_test_patch,
            "patch_context": ctx_chunk,
        }
        sub_instances.append(sub)

    # Put unmatched test files into the first sub-instance
    unmatched = set(test_by_file.keys()) - assigned_tests
    if unmatched and sub_instances:
        existing = sub_instances[0].get("test_patch", "")
        extra = _reassemble_patch(test_by_file, unmatched)
        sub_instances[0]["test_patch"] = (existing + extra).strip() + "\n" if (existing + extra).strip() else ""

    logger.info(
        f"Decomposed {instance['instance_id']} into {len(sub_instances)} sub-instances "
        f"({len(context)} context entries > threshold {DECOMPOSE_THRESHOLD}, chunk size {CHUNK_SIZE})"
    )
    return sub_instances


def create_instances_from_pr(repo: Repo, pull: dict, output_dir: str, mode: str = 'swebench') -> list[dict]:
    """
    Create one task instance per non-merge commit in a pull request.

    Each commit becomes an independent instance:
      - base_commit = parent SHA of that commit
      - patch       = diff introduced by that commit only
      - problem_statement / hints = shared from the PR's linked issues

    Merge commits (those with more than one parent) are skipped.
    """
    # Fetch all commits in this PR
    commits = repo.call_github_api(
        call_type='get_commits',
        owner=repo.owner,
        repo=repo.name,
        token=repo.token,
        pull_idx=pull['number'],
    )
    if not commits:
        return []

    # Extract problem statement once — shared across all commits in this PR
    resolved_issues = pull.get("resolved_issues", [])
    if resolved_issues:
        if mode == 'swebench':
            problem_statement, hints = extract_problem_statement_and_hints(pull, repo, commits=commits)
        else:
            problem_statement, hints = extract_problem_statement_and_hints_with_official_github_api(pull, repo, commits=commits)
    else:
        problem_statement, hints = extract_problem_statement_from_pr(pull, repo)

    repo_full_name = repo.repo.full_name
    pr_number = pull["number"]

    instances = []
    for commit in commits:
        parents = commit.get('parents', [])
        # Skip merge commits (more than one parent)
        if len(parents) != 1:
            continue

        commit_sha = commit['sha']
        base_commit = parents[0]['sha']
        commit_data = fetch_commit_info(commit_sha, repo)
        patch = commit_data["patch"]
        test_patch = commit_data["test_patch"]
        request_success = commit_data["success"]

        # For each modified .py file, fetch original source and extract changed function context
        # Parallelize fetch_file_at_commit calls (each is an independent API request)
        def _build_context(item):
            file_path, file_patch = item
            source = fetch_file_at_commit(file_path, base_commit, repo)
            if source is None:
                source = reconstruct_source_from_patch(file_patch)
            if source:
                return extract_changed_context(source, file_patch, file_path)
            return None

        py_items = list(commit_data["py_file_patches"].items())
        patch_context = []
        context_files = []
        with ThreadPoolExecutor(max_workers=min(8, len(py_items) or 1)) as file_executor:
            for (file_path_key, _), ctx in zip(py_items, file_executor.map(_build_context, py_items)):
                if ctx is not None:
                    patch_context.append(ctx)
                    context_files.append(file_path_key)

        instance_id = f"{repo_full_name}-{pr_number}-{commit_sha[:8]}".replace("/", "__")

        instance = {
            "repo": repo_full_name,
            "pull_number": pr_number,
            "pull_url": pull.get("html_url") or pull.get("url"),
            "instance_id": instance_id,
            "commit_sha": commit_sha,
            "issue_numbers": resolved_issues,
            "base_commit": base_commit,
            "patch": patch,
            "test_patch": test_patch,
            "raw_problem_statement": problem_statement,
            "problem_statement": problem_statement,
            "hints_text": hints,
            "created_at": pull["created_at"],
            "patch_context": patch_context,
        }
        instances.extend(_decompose_instance(instance, context_files))

    return instances


def is_valid_pull(pull: dict) -> bool:
    """
    Check whether PR is a candidate for task-instance creation.

    Tier 1: merged + has linked issues  (original strict path)
    Tier 2: merged + PR title indicates a code change  (fallback for repos
            that don't use "fixes #N" conventions)

    Args:
        pull (dict): pull request object
    Returns:
        bool: whether PR is valid
    """
    if pull["merged_at"] is None:
        return False
    # Tier 1: has linked issues (original)
    if pull.get("resolved_issues") and len(pull["resolved_issues"]) >= 1:
        return True
    # Tier 2: PR title indicates code change (fallback)
    title = pull.get("title", "")
    if CODE_CHANGE_TITLE_RE.search(title):
        return True
    return False


def is_valid_instance(instance: dict) -> bool:
    """Return True if the instance has a non-empty patch, problem statement, and patch context."""
    if not instance.get("patch"):
        logger.info(f"Instance {instance['instance_id']} no patch")
        return False
    if not instance.get("problem_statement"):
        logger.info(f"Instance {instance['instance_id']} no problem statement, will generate later")
    if not instance.get("patch_context"):
        logger.info(f"Instance {instance['instance_id']} skipped: no changes in target language files")
        return False
    return True


def has_test_patch(instance: dict, threshold: int = 4) -> bool:
    """Return True if the instance has a non-trivial test patch (> threshold changed lines)."""
    test_patch = instance.get("test_patch", "").strip()
    if not test_patch:
        logger.info(f"Instance {instance['instance_id']} no test patch")
        return False
    if is_trivial_patch(test_patch, threshold):
        logger.info(f"Instance {instance['instance_id']} trivial test patch (<={threshold} lines)")
        return False
    return True


def is_readme_only_patch(patch: str) -> bool:
    """Return True if every changed file in the patch is a README."""
    if not patch or not patch.strip():
        return False
    found_diff = False
    for line in patch.split("\n"):
        if line.startswith("diff --git a/"):
            found_diff = True
            parts = line.split(" b/")
            if len(parts) >= 2:
                filepath = parts[-1].strip()
                basename = os.path.basename(filepath).lower()
                if not basename.startswith("readme"):
                    return False
    return found_diff


def is_trivial_patch(patch: str, threshold: int = 2) -> bool:
    """Return True if the total changed lines (added + removed) is <= threshold."""
    if not patch or not patch.strip():
        return True
    changed = 0
    for line in patch.split("\n"):
        if (line.startswith("+") and not line.startswith("+++")) or \
           (line.startswith("-") and not line.startswith("---")):
            changed += 1
    return changed <= threshold


def main(pr_file: str, output_dir: str, token: Optional[str] = None, mode: str = 'swebench', language: str = 'python', cutoff_date: str = "2025-03-31T23:59:59Z", max_instances: Optional[int] = None, workers: int = 8):
    """
    Create task instances from pull requests.

    Outputs (written to output_dir):
        instances_all_{N}.jsonl       — all valid instances (N = count)
        instances_ori_test_{N}.jsonl  — instances with original test patches

    Filters applied:
        - Skip PRs that only modify README files
        - Skip PRs with trivial code changes (<=2 lines)
    """
    logger.info(f'Language: {language}')
    logger.info(f'mode: {mode}')
    cutoff_dt = datetime.strptime(cutoff_date, "%Y-%m-%dT%H:%M:%SZ")

    if token is None:
        token = os.environ["GITHUB_TOKEN"]

    def load_repo(repo_name, language):
        owner, repo = repo_name.split("/")
        return Repo(owner, repo, token=token, language=language)

    os.makedirs(output_dir, exist_ok=True)

    # Resume: load existing instances and track which PRs have already been processed
    raw_instances = []
    seen_pr_ids = set()  # PR-level IDs: "{repo}-{pr_number}"
    for fpath in sorted(glob_mod.glob(os.path.join(output_dir, "instances_all_*.jsonl"))):
        with open(fpath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                inst = json.loads(line)
                if datetime.strptime(inst["created_at"], "%Y-%m-%dT%H:%M:%SZ") >= cutoff_dt:
                    continue
                raw_instances.append(inst)
                pr_id = (inst["repo"] + "-" + str(inst["pull_number"])).replace("/", "__")
                seen_pr_ids.add(pr_id)
    logger.info(f"{len(seen_pr_ids)} PR IDs loaded from checkpoint")

    # Filter valid PRs and pre-populate repo objects (no API calls)
    valid_pulls = []
    repos = {}
    total_prs = 0
    new_count = 0

    for ix, line in enumerate(open(pr_file, encoding="utf-8")):
        total_prs += 1
        pull = json.loads(line)
        pr_id = (pull["base"]["repo"]["full_name"] + "-" + str(pull["number"])).replace("/", "__")
        if pr_id in seen_pr_ids:
            continue
        if not is_valid_pull(pull):
            continue
        repo_name = pull["base"]["repo"]["full_name"]
        if repo_name not in repos:
            repos[repo_name] = load_repo(repo_name, language)
        valid_pulls.append(pull)

    logger.info(f"{len(valid_pulls)} valid PRs to process (workers={workers})")

    # Process PRs in parallel — each PR makes several GitHub API calls (I/O-bound)
    reached_limit = False
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_pull = {
            executor.submit(
                create_instances_from_pr,
                repos[pull["base"]["repo"]["full_name"]],
                pull, output_dir, mode,
            ): pull
            for pull in valid_pulls
        }
        for future in as_completed(future_to_pull):
            if reached_limit:
                future.cancel()
                continue
            for instance in future.result():
                if datetime.strptime(instance["created_at"], "%Y-%m-%dT%H:%M:%SZ") >= cutoff_dt:
                    continue
                if is_valid_instance(instance):
                    raw_instances.append(instance)
                    new_count += 1
            if max_instances is not None and len(raw_instances) >= max_instances:
                reached_limit = True

    # Apply filters to ALL instances (including resumed ones, so new filters take effect)
    all_instances = []
    filtered_readme = 0
    filtered_trivial = 0
    for inst in raw_instances:
        if is_readme_only_patch(inst.get("patch", "")):
            logger.info(f"Filtering {inst['instance_id']}: README-only changes")
            filtered_readme += 1
            continue
        if is_trivial_patch(inst.get("patch", "")):
            logger.info(f"Filtering {inst['instance_id']}: trivial changes (<=2 lines)")
            filtered_trivial += 1
            continue
        all_instances.append(inst)

    test_instances = [i for i in all_instances if has_test_patch(i)]

    # Clean up old output files
    for pattern in ["instances_all_*.jsonl", "instances_ori_test_*.jsonl"]:
        for f in glob_mod.glob(os.path.join(output_dir, pattern)):
            os.remove(f)

    # Write new output files with count in filename
    all_path = os.path.join(output_dir, f"instances_all_{len(all_instances)}.jsonl")
    test_path = os.path.join(output_dir, f"instances_ori_test_{len(test_instances)}.jsonl")

    with open(all_path, "w", encoding="utf-8") as f:
        for inst in all_instances:
            f.write(json.dumps(inst) + "\n")

    with open(test_path, "w", encoding="utf-8") as f:
        for inst in test_instances:
            f.write(json.dumps(inst) + "\n")

    logger.info(f"Total PRs scanned: {total_prs}, new instances: {new_count}")
    logger.info(f"Filtered (README-only): {filtered_readme}, filtered (trivial): {filtered_trivial}")
    logger.info(f"All valid instances: {len(all_instances)} -> {all_path}")
    logger.info(f"Instances with tests: {len(test_instances)} -> {test_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("pr_file", type=str, help="Path to pull request JSONL file")
    parser.add_argument("output_dir", type=str, help="Output directory for instance files")
    parser.add_argument("--token", type=str, help="GitHub token")
    parser.add_argument("--mode", type=str, default='omnigirl', help="collecting mode")
    parser.add_argument("--cutoff_date", type=str, default="2025-03-31T23:59:59Z", help="Cutoff date for filtering PRs in YYYY-MM-DDTHH:MM:SSZ format")
    parser.add_argument("--language", type=str, help="language")
    parser.add_argument("--max-instances", type=int, default=None, help="Stop after collecting this many instances")
    parser.add_argument("--workers", type=int, default=8, help="Number of parallel PR processing threads (default: 8)")

    args = parser.parse_args()
    main(**vars(args))
