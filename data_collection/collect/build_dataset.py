#!/usr/bin/env python3

import argparse
import glob as glob_mod
import json
import logging
import os
from typing import Optional
from datetime import datetime
from utils import (
    Repo,
    extract_patches,
    extract_problem_statement_and_hints,
    extract_problem_statement_and_hints_with_official_github_api,
    extract_problem_statement_from_pr,
    CODE_CHANGE_TITLE_RE,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def create_instance(repo: Repo, pull: dict, output_dir: str, mode: str ='swebench') -> dict:
    """
    Create a single task instance from a pull request, where task instance is:

    {
        repo (str): owner/repo this task instance is from,
        pull_number (int): number of PR this task instance is from,
        base_commit (str): SHA of the base commit PR is based on,
        patch (str): reference solution as .patch (apply to base commit),
        test_patch (str): test suite as .patch (apply to base commit),
    }
    """
    patch, test_patch, request_success = extract_patches(pull, repo)
    instance_id  = (repo.repo.full_name + "-" + str(pull["number"])).replace("/", "__")
    successful_path = os.path.join(output_dir, "successful_requests.txt")
    if request_success:
        with open(successful_path, "a") as f:
            f.write(instance_id + "\n")

    problem_statement_source = "issue"
    resolved_issues = pull.get("resolved_issues", [])

    if resolved_issues:
        # Standard path: fetch from linked issues
        if mode == 'swebench':
            problem_statement, hints = extract_problem_statement_and_hints(pull, repo)
        else:
            problem_statement, hints = extract_problem_statement_and_hints_with_official_github_api(pull, repo)
    else:
        # Fallback: use PR title + body as problem statement
        problem_statement, hints = extract_problem_statement_from_pr(pull, repo)
        problem_statement_source = "pr_body"

    return {
        "repo": repo.repo.full_name,
        "pull_number": pull["number"],
        "pull_url": pull.get("html_url") or pull.get("url"),
        "instance_id": instance_id,
        "issue_numbers": resolved_issues,
        "base_commit": pull["base"]["sha"],
        "patch": patch,
        "test_patch": test_patch,
        "problem_statement": problem_statement,
        "hints_text": hints,
        "created_at": pull["created_at"],
        "problem_statement_source": problem_statement_source,
    }


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
    """
    Check whether task instance has all required fields for task instance creation

    Args:
        instance (dict): task instance object
    Returns:
        bool: whether task instance is valid
    """
    if instance["patch"] is None or instance["patch"] == "":
        logger.info(f"Instance {instance['pull_number']} no patch")
        return False
    if instance["problem_statement"] is None or instance["problem_statement"] == "":
        logger.info(f"Instance {instance['pull_number']} no problem statement")
        return False
    return True


def has_test_patch(instance: dict) -> bool:
    """
    Check whether task instance has a test suite

    Args:
        instance (dict): task instance object
    Returns:
        bool: whether task instance has a test suite
    """
    if instance["test_patch"] is None or instance["test_patch"].strip() == "":
        logger.info(f"Instance {instance['pull_number']} no test patch")
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


def main(pr_file: str, output_dir: str, token: Optional[str] = None, mode: Optional[str] = 'swebench', language: Optional[str] = 'python', cutoff_date: Optional[str] = None):
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

    # Load successful requests for resume
    successful_path = os.path.join(output_dir, "successful_requests.txt")
    if not os.path.exists(successful_path):
        with open(successful_path, "w") as f:
            pass
    successful_instances = set()
    with open(successful_path, "r") as f:
        for line in f:
            successful_instances.add(line.strip())

    # Resume: load existing instances from instances_all_*.jsonl
    raw_instances = []
    seen_prs = set()
    for fpath in sorted(glob_mod.glob(os.path.join(output_dir, "instances_all_*.jsonl"))):
        with open(fpath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                inst = json.loads(line)
                if "instance_id" not in inst:
                    inst["instance_id"] = (inst["repo"] + "-" + str(inst["pull_number"])).replace("/", "__")
                if datetime.strptime(inst["created_at"], "%Y-%m-%dT%H:%M:%SZ") >= cutoff_dt:
                    continue
                raw_instances.append(inst)
                seen_prs.add(inst["instance_id"])
    logger.info(f"{len(seen_prs)} instance_ids loaded from checkpoint")

    # Process new PRs
    repos = dict()
    total_prs = 0
    new_count = 0

    for ix, line in enumerate(open(pr_file)):
        total_prs += 1
        pull = json.loads(line)
        if ix % 100 == 0:
            logger.info(
                f"[{pull['base']['repo']['full_name']}] Checked {ix} PRs, {len(raw_instances)} valid so far"
            )
        instance_id = (pull["base"]["repo"]["full_name"] + "-" + str(pull["number"])).replace("/", "__")

        if instance_id in seen_prs or instance_id in successful_instances:
            continue
        if not is_valid_pull(pull):
            continue

        repo_name = pull["base"]["repo"]["full_name"]
        if repo_name not in repos:
            repos[repo_name] = load_repo(repo_name, language)
        repo = repos[repo_name]

        instance = create_instance(repo, pull, output_dir, mode)

        if datetime.strptime(instance["created_at"], "%Y-%m-%dT%H:%M:%SZ") >= cutoff_dt:
            continue
        if is_valid_instance(instance):
            raw_instances.append(instance)
            new_count += 1

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

    args = parser.parse_args()
    main(**vars(args))
