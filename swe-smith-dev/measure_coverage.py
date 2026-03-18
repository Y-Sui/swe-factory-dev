#!/usr/bin/env python3
"""Measure branch coverage of gold patch functions by their generated tests.

For each instance, runs the test file inside Docker with pytest-cov,
measuring branch coverage on the specific source file containing the
gold function. Parses coverage JSON to extract per-file branch coverage.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import DOCKER_TEST_CONFIG, REPOS


def measure_one(instance: dict, repo_key: str) -> dict:
    """Run test with branch coverage for one instance. Returns coverage info."""
    config = DOCKER_TEST_CONFIG[repo_key]
    image_tag = instance.get("docker_image") or config["image_tag"]
    test_dir = config["test_dir"]

    meta = instance["function_metadata"]
    func_name = meta["func_name"]
    class_name = meta.get("class_name")
    file_path = meta["file_path"]
    lineno = meta["lineno"]
    end_lineno = meta.get("end_lineno")
    qual = f"{class_name}.{func_name}" if class_name else func_name

    # Get test source from stage3_tested.jsonl (has test_source field)
    test_source = instance.get("test_source", "")
    if not test_source:
        return {"instance_id": instance["instance_id"], "qual": qual, "error": "no test_source"}

    # Determine the source file path relative to test_dir inside container
    # For miroflow: file_path is like "miroflow/llm/openai_client.py", test_dir is "/testbed"
    # For mirothinker: file_path may be "apps/visualize-trace/...", test_dir is "/testbed/apps/miroflow-agent"
    # For sd-torchtune: file_path is like "torchtune/generation/...", test_dir is "/testbed"

    # Install pytest-cov first, then run test with branch coverage
    # --cov needs path relative to /testbed (the repo root), and we always cd /testbed
    if "uv run" in config["pytest_cmd"]:
        install_cmd = "uv pip install pytest-cov"
        pytest_base = "uv run pytest"
    else:
        install_cmd = "pip install -q pytest-cov"
        pytest_base = "pytest"

    # Source file relative to /testbed
    # For mirothinker, test_dir is /testbed/apps/miroflow-agent but source
    # could be apps/visualize-trace/... — always run from /testbed
    cov_source = file_path  # relative to /testbed

    cov_cmd = (
        f"{install_cmd} && "
        f"cd /testbed && "
        f"{pytest_base} /tmp/test_cov.py -x -v --tb=short --no-header "
        f"--cov={cov_source} --cov-branch --cov-report=json:/tmp/cov.json 2>&1; "
        f"cat /tmp/cov.json 2>/dev/null"
    )

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", prefix="test_cov_", delete=False) as f:
        f.write(test_source)
        host_test_path = f.name

    try:
        result = subprocess.run(
            [
                "docker", "run", "--rm",
                "-v", f"{host_test_path}:/tmp/test_cov.py:ro",
                image_tag,
                "bash", "-c", cov_cmd,
            ],
            capture_output=True, text=True, timeout=180,
        )
        output = result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return {"instance_id": instance["instance_id"], "qual": qual, "error": "timeout"}
    except Exception as e:
        return {"instance_id": instance["instance_id"], "qual": qual, "error": str(e)}
    finally:
        Path(host_test_path).unlink(missing_ok=True)

    # Parse JSON coverage from output — it's after the pytest output
    cov_json = None
    # Find the JSON blob (starts with '{' on its own line after pytest output)
    for i, line in enumerate(output.split("\n")):
        line = line.strip()
        if line.startswith('{"meta"'):
            try:
                cov_json = json.loads(line)
            except json.JSONDecodeError:
                # Maybe it spans multiple lines — try joining rest
                rest = "\n".join(output.split("\n")[i:])
                try:
                    cov_json = json.loads(rest)
                except json.JSONDecodeError:
                    pass
            break

    if not cov_json:
        # Try to find any JSON object in output
        for i, line in enumerate(output.split("\n")):
            if '{"meta"' in line:
                start = line.index('{"meta"')
                try:
                    cov_json = json.loads(line[start:])
                except json.JSONDecodeError:
                    pass
                break

    if not cov_json:
        return {
            "instance_id": instance["instance_id"],
            "qual": qual,
            "error": "no coverage JSON",
            "output_tail": output[-500:],
        }

    # Extract coverage for the target file
    files_cov = cov_json.get("files", {})
    # Find matching file
    target_cov = None
    for fpath, data in files_cov.items():
        if file_path in fpath or fpath.endswith(file_path):
            target_cov = data
            break

    if not target_cov:
        return {
            "instance_id": instance["instance_id"],
            "qual": qual,
            "error": f"file not in coverage (files: {list(files_cov.keys())})",
            "totals": cov_json.get("totals", {}),
        }

    summary = target_cov.get("summary", {})

    # Filter to function lines only
    func_line_cov = _compute_function_coverage(target_cov, lineno, end_lineno)

    return {
        "instance_id": instance["instance_id"],
        "qual": qual,
        "file_path": file_path,
        "lineno": lineno,
        "end_lineno": end_lineno,
        "file_branch_coverage": summary.get("covered_branches", 0) / max(summary.get("num_branches", 1), 1) * 100,
        "file_line_coverage": summary.get("percent_covered", 0),
        "func_branch_coverage": func_line_cov.get("branch_coverage"),
        "func_line_coverage": func_line_cov.get("line_coverage"),
        "func_lines_covered": func_line_cov.get("lines_covered"),
        "func_lines_total": func_line_cov.get("lines_total"),
        "func_branches_covered": func_line_cov.get("branches_covered"),
        "func_branches_total": func_line_cov.get("branches_total"),
    }


def _compute_function_coverage(file_cov: dict, start: int, end: int | None) -> dict:
    """Compute line and branch coverage restricted to function line range."""
    if not end:
        return {"line_coverage": None, "branch_coverage": None}

    executed = set(file_cov.get("executed_lines", []))
    missing = set(file_cov.get("missing_lines", []))
    all_lines = executed | missing

    # Filter to function range
    func_lines = {l for l in all_lines if start <= l <= end}
    func_executed = {l for l in executed if start <= l <= end}

    lines_total = len(func_lines)
    lines_covered = len(func_executed)
    line_coverage = (lines_covered / lines_total * 100) if lines_total else None

    # Branch coverage: missing_branches is list of [line, branch_id] pairs
    missing_branches = file_cov.get("missing_branches", [])
    executed_branches = file_cov.get("executed_branches", [])

    func_missing_br = [b for b in missing_branches if start <= b[0] <= end]
    func_executed_br = [b for b in executed_branches if start <= b[0] <= end]

    branches_total = len(func_missing_br) + len(func_executed_br)
    branches_covered = len(func_executed_br)
    branch_coverage = (branches_covered / branches_total * 100) if branches_total else None

    return {
        "line_coverage": round(line_coverage, 1) if line_coverage is not None else None,
        "branch_coverage": round(branch_coverage, 1) if branch_coverage is not None else None,
        "lines_covered": lines_covered,
        "lines_total": lines_total,
        "branches_covered": branches_covered,
        "branches_total": branches_total,
    }


def main():
    base = Path(__file__).parent / "output"
    repo_keys = ["miroflow", "mirothinker", "sd-torchtune"]

    all_results = []

    for repo_key in repo_keys:
        # Load from stage3 (has test_source) and instances (has metadata)
        s3_path = base / repo_key / "stage3_tested.jsonl"
        inst_path = base / repo_key / "instances.jsonl"

        if not s3_path.exists() or not inst_path.exists():
            print(f"Skipping {repo_key}: missing files")
            continue

        # Build lookup: func_name -> test_source from stage3
        test_sources = {}
        with open(s3_path) as f:
            for line in f:
                c = json.loads(line)
                test_sources[c["func_name"]] = c.get("test_source", "")

        # Load instances
        instances = []
        with open(inst_path) as f:
            for line in f:
                instances.append(json.loads(line))

        print(f"\n{'='*60}")
        print(f"Measuring branch coverage for {repo_key} ({len(instances)} instances)")
        print(f"{'='*60}")

        for inst in instances:
            fn = inst["function_metadata"]["func_name"]
            inst["test_source"] = test_sources.get(fn, "")
            qual = inst["function_metadata"].get("class_name", "")
            qual = f"{qual}.{fn}" if qual else fn
            print(f"  Measuring: {qual} ...", end=" ", flush=True)
            res = measure_one(inst, repo_key)
            all_results.append({"repo": repo_key, **res})

            if "error" in res:
                print(f"ERROR: {res['error'][:80]}")
            else:
                bc = res.get("func_branch_coverage")
                lc = res.get("func_line_coverage")
                bc_str = f"{bc:.1f}%" if bc is not None else "N/A"
                lc_str = f"{lc:.1f}%" if lc is not None else "N/A"
                print(f"branch={bc_str}  line={lc_str}")

    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"{'Repo':<15} {'Function':<45} {'Branch':>8} {'Line':>8}")
    print("-" * 80)

    repo_stats = {}
    for r in all_results:
        repo = r["repo"]
        if repo not in repo_stats:
            repo_stats[repo] = {"branch": [], "line": []}

        bc = r.get("func_branch_coverage")
        lc = r.get("func_line_coverage")
        bc_str = f"{bc:.1f}%" if bc is not None else "N/A"
        lc_str = f"{lc:.1f}%" if lc is not None else "N/A"
        print(f"{repo:<15} {r.get('qual', '?'):<45} {bc_str:>8} {lc_str:>8}")

        if bc is not None:
            repo_stats[repo]["branch"].append(bc)
        if lc is not None:
            repo_stats[repo]["line"].append(lc)

    print("-" * 80)
    all_branch = []
    all_line = []
    for repo, stats in repo_stats.items():
        avg_b = sum(stats["branch"]) / len(stats["branch"]) if stats["branch"] else 0
        avg_l = sum(stats["line"]) / len(stats["line"]) if stats["line"] else 0
        print(f"{repo:<15} {'AVG':<45} {avg_b:>7.1f}% {avg_l:>7.1f}%")
        all_branch.extend(stats["branch"])
        all_line.extend(stats["line"])

    if all_branch:
        print("-" * 80)
        avg_b = sum(all_branch) / len(all_branch)
        avg_l = sum(all_line) / len(all_line)
        print(f"{'OVERALL':<15} {'AVG':<45} {avg_b:>7.1f}% {avg_l:>7.1f}%")

    # Save detailed results
    out_path = base / "coverage_results.json"
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nDetailed results saved to {out_path}")


if __name__ == "__main__":
    main()
