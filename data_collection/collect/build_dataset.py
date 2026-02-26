#!/usr/bin/env python3

import argparse
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


def create_instance(repo: Repo, pull: dict, output_path: str, mode: str ='swebench') -> dict:
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
    # try: 
    patch, test_patch, request_success = extract_patches(pull, repo)
    # except Exception as e:
    #     logger.info(e)
    #     patch = ""
    #     test_patch = ""
    instance_id  = (repo.repo.full_name + "-" + str(pull["number"])).replace("/", "__")
    successful_path = os.path.join(os.path.dirname(output_path), "successful_requests.txt")
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

def main(pr_file: str, output: str, token: Optional[str] = None,mode: Optional[str] = 'swebench',language: Optional[str] = 'python', cutoff_date: Optional[str] = None):
    """
    Main thread for creating task instances from pull requests

    Args:
        pr_file (str): path to pull request JSONL file
        output (str): output file name
        token (str): GitHub token
    """
    logger.info(f'Language: {language}')
    logger.info(f'mode: {mode}')
    cutoff_date = datetime.strptime(cutoff_date, "%Y-%m-%dT%H:%M:%SZ")
    if token is None:
        # Get GitHub token from environment variable if not provided
        token = os.environ["GITHUB_TOKEN"]

    def load_repo(repo_name,language):
        # Return repo object for a given repo name
        owner, repo = repo_name.split("/")
        return Repo(owner, repo, token=token,language=language)

    repos = dict()
    completed = 0
    with_tests = 0
    total_instances = 0
    all_output = output + ".all"
    seen_prs = set()

    successful_path = os.path.join(os.path.dirname(output), "successful_requests.txt")

    if not os.path.exists(successful_path):
        with open(successful_path, "w") as f:
            pass  

    successful_instances = set()
    with open(successful_path, "r") as f:
        for line in f:
            successful_instances.add(line.strip())

    # Continue where we left off if output file already exists
    if os.path.exists(all_output):
        with open(all_output) as f:
            for line in f:
                pr = json.loads(line)
                if "instance_id" not in pr:
                    pr["instance_id"] = (
                        pr["repo"] + "-" + str(pr["pull_number"])
                    ).replace("/", "__")
                instance_id = pr["instance_id"]
                seen_prs.add(instance_id)
                if datetime.strptime(pr["created_at"], "%Y-%m-%dT%H:%M:%SZ") >= cutoff_date:
                    logger.info(f"Instance {instance_id} created_at {pr['created_at']} exceeds cutoff_date {cutoff_date}")
                    continue
                if is_valid_instance(pr):
                    completed += 1
                    if has_test_patch(pr):
                        with_tests += 1
    logger.info(f"{len(seen_prs)} instance_ids previously recorded")
    original_output_path = output
    # Write to .all file for all PRs
    write_mode_all = "w" if not os.path.exists(all_output) else "a"
    with open(all_output, write_mode_all) as all_output:
        # Write to output file for PRs with test suites
        write_mode = "w" if not os.path.exists(output) else "a"
        with open(output, write_mode) as output:
            for ix, line in enumerate(open(pr_file)):
                total_instances += 1
                pull = json.loads(line)
                if ix % 100 == 0:
                    logger.info(
                        f"[{pull['base']['repo']['full_name']}] ( Up to {ix} checked ) {completed} valid, {with_tests} with tests."
                    )
                # Construct instance fields
                instance_id = (
                    pull["base"]["repo"]["full_name"] + "-" + str(pull["number"])
                )
                instance_id = instance_id.replace("/", "__")
                
                if instance_id in seen_prs:
                    seen_prs -= {instance_id}
                    continue

                if instance_id in successful_instances:
                    continue
    
                if not is_valid_pull(pull):
                    # Throw out invalid PRs
                    continue
                # Create task instance
                repo_name = pull["base"]["repo"]["full_name"]
                if repo_name not in repos:
                    repos[repo_name] = load_repo(repo_name,language)
                repo = repos[repo_name]
                instance = create_instance(repo, pull,original_output_path,mode)
                if is_valid_instance(instance):
                    # If valid, write to .all output file
                    print(
                        json.dumps(instance), end="\n", flush=True, file=all_output
                    )  # write all instances to a separate file
                    completed += 1
                    if has_test_patch(instance):
                        # If has test suite, write to output file
                        print(json.dumps(instance), end="\n", flush=True, file=output)
                        with_tests += 1
    logger.info(
        f"Total instances: {total_instances}, completed: {completed}, with tests: {with_tests}"
    )
    logger.info(f"Didn't see {len(seen_prs)} instances previously recorded")
    logger.info("\n".join(sorted(seen_prs)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("pr_file", type=str, help="Path to pull request JSONL file")
    parser.add_argument("output", type=str, help="Output file name")
    parser.add_argument("--token", type=str, help="GitHub token")
    parser.add_argument("--mode", type=str, default='omnigirl',help="collecting mode")
    parser.add_argument("--cutoff_date", type=str, default="2025-03-31T23:59:59Z", help="Cutoff date for filtering PRs in YYYY-MM-DDTHH:MM:SSZ format")
    parser.add_argument("--language", type=str, help="language")
    
    args = parser.parse_args()
    print(">>> reached main()")
    main(**vars(args))
