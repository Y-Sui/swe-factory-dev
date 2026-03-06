#!/usr/bin/env python3
"""Rebuild results.json from applicable_setup directories.

Reads status.json, meta.json, eval.sh, Dockerfile, and test output files
to reconstruct complete results with FAIL_TO_PASS, PASS_TO_PASS, and test_patch.
"""

import json
import os
import re
import sys
import glob
import tempfile
import subprocess
from pathlib import Path


def parse_pytest_results(output: str) -> dict[str, str]:
    """Parse pytest verbose output to get per-test PASSED/FAILED status."""
    results = {}
    for line in output.splitlines():
        # Match lines like: tests/test_foo.py::test_bar PASSED [ 50%]
        # or: FAILED tests/test_foo.py::test_bar - ...
        m = re.match(r'^([\w/._-]+::[\w:._-]+)\s+(PASSED|FAILED)', line)
        if m:
            results[m.group(1)] = m.group(2)
            continue
        # Short test summary: FAILED tests/test_foo.py::test_bar
        m = re.match(r'^FAILED\s+([\w/._-]+::[\w:._-]+)', line)
        if m:
            results[m.group(1)] = "FAILED"
    return results


def extract_test_files_from_eval(eval_sh: str) -> dict[str, str]:
    """Extract test file contents from eval.sh heredoc blocks."""
    files = {}
    # Pattern: cat <<'EOF_TEST_N' > "path/to/file.py"
    pattern = re.compile(
        r'cat\s+<<\'(EOF_TEST_\d+)\'\s*>\s*"([^"]+)"\s*\n(.*?)\n\1',
        re.DOTALL,
    )
    for m in pattern.finditer(eval_sh):
        filepath = m.group(2)
        content = m.group(3)
        files[filepath] = content
    return files


def build_test_patch(test_files: dict[str, str]) -> str:
    """Build a unified diff (test_patch) from test file contents.

    Simulates adding new files to an empty repo.
    """
    patches = []
    for filepath, content in sorted(test_files.items()):
        lines = content.splitlines(keepends=True)
        # Ensure last line ends with newline
        if lines and not lines[-1].endswith('\n'):
            lines[-1] += '\n'
        header = f"diff --git a/{filepath} b/{filepath}\nnew file mode 100644\n--- /dev/null\n+++ b/{filepath}\n"
        header += f"@@ -0,0 +1,{len(lines)} @@\n"
        body = "".join(f"+{line}" for line in lines)
        patches.append(header + body)
    return "\n".join(patches)


def find_last_analysis_dir(case_dir: str) -> str | None:
    """Find the last test_analysis_agent_N directory."""
    dirs = sorted(glob.glob(os.path.join(case_dir, "test_analysis_agent_*")))
    return dirs[-1] if dirs else None


def process_case(case_dir: str) -> dict | None:
    """Process a single case directory and return a result dict."""
    status_path = os.path.join(case_dir, "status.json")
    meta_path = os.path.join(case_dir, "meta.json")

    if not os.path.exists(status_path) or not os.path.exists(meta_path):
        return None

    status = json.load(open(status_path))
    if status.get("f2p_classification") != "FAIL2PASS":
        return None

    meta = json.load(open(meta_path))
    task_info = meta["task_info"]

    # Find last successful test_analysis dir
    last_ta = find_last_analysis_dir(case_dir)
    if not last_ta:
        return None

    # Parse per-test results from pre/post output
    pre_output_path = os.path.join(last_ta, "test_output_prev_apply.txt")
    post_output_path = os.path.join(last_ta, "test_output.txt")

    fail_to_pass = []
    pass_to_pass = []

    if os.path.exists(pre_output_path) and os.path.exists(post_output_path):
        pre_results = parse_pytest_results(open(pre_output_path).read())
        post_results = parse_pytest_results(open(post_output_path).read())

        all_tests = set(pre_results.keys()) | set(post_results.keys())
        for test_id in sorted(all_tests):
            pre = pre_results.get(test_id)
            post = post_results.get(test_id)
            if pre == "FAILED" and post == "PASSED":
                fail_to_pass.append(test_id)
            elif pre == "PASSED" and post == "PASSED":
                pass_to_pass.append(test_id)

    # If no per-test data but overall is FAIL2PASS, still include
    # (some cases only have exit codes)

    # Get eval.sh and extract test files
    eval_path = os.path.join(case_dir, "eval.sh")
    eval_script = ""
    test_patch = ""
    if os.path.exists(eval_path):
        eval_script = open(eval_path).read()
        test_files = extract_test_files_from_eval(eval_script)
        if test_files:
            test_patch = build_test_patch(test_files)

    # Get Dockerfile
    dockerfile_path = os.path.join(case_dir, "Dockerfile")
    dockerfile = ""
    if os.path.exists(dockerfile_path):
        dockerfile = open(dockerfile_path).read()

    # Build result
    result = {
        "repo": task_info.get("repo", ""),
        "pull_number": task_info.get("pull_number"),
        "pull_url": task_info.get("pull_url", ""),
        "instance_id": task_info.get("instance_id", ""),
        "commit_sha": task_info.get("commit_sha", ""),
        "issue_numbers": task_info.get("issue_numbers", []),
        "base_commit": task_info.get("base_commit", ""),
        "patch": task_info.get("patch", ""),
        "test_patch": test_patch,
        "problem_statement": task_info.get("problem_statement", ""),
        "hints_text": task_info.get("hints_text", ""),
        "created_at": task_info.get("created_at", ""),
        "version": task_info.get("version", ""),
        "FAIL_TO_PASS": json.dumps(fail_to_pass),
        "PASS_TO_PASS": json.dumps(pass_to_pass),
        "environment_setup_commit": task_info.get("base_commit", ""),
        "dockerfile": dockerfile,
        "eval_script": eval_script,
    }

    return result


def main():
    if len(sys.argv) < 2:
        print("Usage: python rebuild_results.py <setup_output_dir> [<setup_output_dir2> ...]")
        sys.exit(1)

    for setup_dir in sys.argv[1:]:
        applicable = os.path.join(setup_dir, "applicable_setup")
        results_dir = os.path.join(setup_dir, "results")

        if not os.path.isdir(applicable):
            print(f"Skipping {setup_dir}: no applicable_setup/")
            continue

        results = []
        seen_ids = set()

        for case_name in sorted(os.listdir(applicable)):
            case_dir = os.path.join(applicable, case_name)
            if not os.path.isdir(case_dir):
                continue

            result = process_case(case_dir)
            if result is None:
                continue

            iid = result["instance_id"]
            if iid in seen_ids:
                print(f"  Skipping duplicate: {iid}")
                continue
            seen_ids.add(iid)
            results.append(result)

            f2p_count = len(json.loads(result["FAIL_TO_PASS"]))
            p2p_count = len(json.loads(result["PASS_TO_PASS"]))
            tp_len = len(result["test_patch"])
            print(f"  {iid}: F2P={f2p_count}, P2P={p2p_count}, test_patch={tp_len} chars")

        # Write results
        os.makedirs(results_dir, exist_ok=True)
        results_path = os.path.join(results_dir, "results.json")

        # Backup old results
        if os.path.exists(results_path):
            backup = results_path + ".bak"
            os.rename(results_path, backup)
            print(f"  Backed up old results to {backup}")

        with open(results_path, "w") as f:
            json.dump(results, f, indent=2)

        print(f"  Wrote {len(results)} results to {results_path}")
        print()


if __name__ == "__main__":
    main()
