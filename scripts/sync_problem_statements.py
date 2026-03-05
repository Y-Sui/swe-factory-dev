#!/usr/bin/env python3
"""
Sync refined problem_statement from instances_all_*.jsonl into setup_output directories.

Updates:
  - <instance_id>/problem_statement.txt
  - <instance_id>/meta.json  (task_info.problem_statement)
  - results/results.json     (problem_statement per entry)

Usage:
    python3 scripts/sync_problem_statements.py
    python3 scripts/sync_problem_statements.py --model-slug gpt-5.2
    python3 scripts/sync_problem_statements.py --repos MiroMindAI__miroflow
"""

import argparse
import glob
import json
import os
import sys

DATA_DIR = "/data/yuansui/internal-swe-bench-data"
ALL_REPOS = ["MiroMindAI__MiroThinker", "MiroMindAI__miroflow", "MiroMindAI__sd-torchtune"]


def find_instances_file(repo_dir: str) -> str | None:
    files = sorted(glob.glob(os.path.join(repo_dir, "instances_all_*.jsonl")))
    for f in files:
        base = os.path.basename(f)
        if "_failures" not in base and "_versions" not in base:
            return f
    return None


def load_problem_statements(jsonl_path: str) -> dict[str, str]:
    """Load {instance_id: problem_statement} from JSONL."""
    mapping = {}
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            iid = obj.get("instance_id", "")
            ps = obj.get("problem_statement", "")
            if iid and ps:
                mapping[iid] = ps
    return mapping


def sync_repo(repo: str, model_slug: str) -> tuple[int, int]:
    repo_dir = os.path.join(DATA_DIR, repo)
    jsonl_path = find_instances_file(repo_dir)
    if not jsonl_path:
        print(f"  {repo}: no instances file found, skipping")
        return 0, 0

    ps_map = load_problem_statements(jsonl_path)
    if not ps_map:
        print(f"  {repo}: no problem statements in {jsonl_path}")
        return 0, 0

    out_dir = os.path.join(repo_dir, f"setup_output_{model_slug}")
    if not os.path.isdir(out_dir):
        print(f"  {repo}: no setup_output dir for {model_slug}")
        return 0, 0

    updated = 0
    total = 0

    # Update per-instance dirs
    for iid, ps in ps_map.items():
        inst_dir = os.path.join(out_dir, iid)
        if not os.path.isdir(inst_dir):
            continue
        total += 1

        # problem_statement.txt
        ps_file = os.path.join(inst_dir, "problem_statement.txt")
        with open(ps_file, "w", encoding="utf-8") as f:
            f.write(ps)

        # meta.json
        meta_file = os.path.join(inst_dir, "meta.json")
        if os.path.exists(meta_file):
            with open(meta_file, encoding="utf-8") as f:
                meta = json.load(f)
            if "task_info" in meta:
                meta["task_info"]["problem_statement"] = ps
                with open(meta_file, "w", encoding="utf-8") as f:
                    json.dump(meta, f, indent=2, ensure_ascii=False)

        updated += 1

    # Update results/results.json
    results_file = os.path.join(out_dir, "results", "results.json")
    if os.path.exists(results_file):
        with open(results_file, encoding="utf-8") as f:
            results = json.load(f)
        changed = 0
        for entry in results:
            iid = entry.get("instance_id", "")
            if iid in ps_map:
                entry["problem_statement"] = ps_map[iid]
                changed += 1
        if changed:
            with open(results_file, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, ensure_ascii=False)
            print(f"  {repo}: updated {changed} entries in results.json")

    return updated, total


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-slug", default="gpt-5.2")
    parser.add_argument("--repos", nargs="+", default=ALL_REPOS)
    args = parser.parse_args()

    print(f"Syncing problem statements -> setup_output_{args.model_slug}\n")
    total_updated = 0
    total_found = 0
    for repo in args.repos:
        updated, found = sync_repo(repo, args.model_slug)
        print(f"  {repo}: {updated}/{found} instances updated")
        total_updated += updated
        total_found += found

    print(f"\nDone. {total_updated}/{total_found} total instances updated.")


if __name__ == "__main__":
    main()
