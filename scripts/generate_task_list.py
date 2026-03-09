"""Generate a task list for the multi-agent pipeline.

Reads instance IDs from a JSONL file, applies a max-instances limit,
skips already-completed instances (is_finish=True in status.json),
and writes the pending IDs to a task list file.

With --run, orchestrates the full Stage II pipeline: generates task lists,
launches app/main.py for each repo in parallel, and rebuilds results.
"""

import argparse
import glob
import json
import os
import subprocess
import sys

DATA_DIR = "/data/yuansui/internal-swe-bench-data"
ALL_REPOS = ["MiroMindAI__MiroThinker", "MiroMindAI__miroflow", "MiroMindAI__sd-torchtune"]


def find_instances_file(data_dir: str, repo: str) -> str | None:
    """Prefer *_subset.jsonl, fall back to instances_filter_*.jsonl."""
    matches = sorted(glob.glob(f"{data_dir}/{repo}/instances_filter_*_subset.jsonl"))
    if matches:
        return matches[0]
    matches = sorted(glob.glob(f"{data_dir}/{repo}/instances_filter_*.jsonl"))
    return matches[0] if matches else None


def generate_task_list(tasks_map: str, task_list: str, max_instances: int, out_dir: str):
    """Read instances JSONL, filter done ones, write pending IDs to task_list file."""
    all_ids = []
    with open(tasks_map, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if "instance_id" in obj:
                all_ids.append(obj["instance_id"])

    total = len(all_ids)

    if max_instances > 0:
        selected = all_ids[:max_instances]
    else:
        selected = all_ids

    pending = []
    already_done = 0
    for iid in selected:
        found_done = False
        for subdir in [iid, os.path.join("applicable_setup", iid)]:
            status_path = os.path.join(out_dir, subdir, "status.json")
            if os.path.exists(status_path):
                try:
                    with open(status_path) as sf:
                        status = json.load(sf)
                    if status.get("is_finish", False):
                        found_done = True
                        break
                except (json.JSONDecodeError, OSError):
                    pass
        if found_done:
            already_done += 1
        else:
            pending.append(iid)

    print(f"  Total instances: {total}, selected: {len(selected)}, "
          f"already done: {already_done}, to run: {len(pending)}")

    with open(task_list, "w", encoding="utf-8") as f:
        f.write("\n".join(pending))

    return pending


def run_pipeline(args):
    """Orchestrate Stage II: task list generation + app/main.py per repo + rebuild results."""
    repos = args.repos if args.repos else ALL_REPOS
    data_dir = args.data_dir

    # Launch app/main.py for each repo in parallel
    procs: list[tuple[str, subprocess.Popen]] = []
    for repo in repos:
        tasks_map = find_instances_file(data_dir, repo)
        if not tasks_map:
            print(f"=== No instances file found for {repo}, skipping ===")
            continue

        out_dir = os.path.join(data_dir, repo, f"setup_output_{args.model_slug}")
        task_list = os.path.join(out_dir, "task_list.txt")
        os.makedirs(out_dir, exist_ok=True)
        os.makedirs(os.path.join(out_dir, "results"), exist_ok=True)

        generate_task_list(tasks_map, task_list, args.max_instances, out_dir)

        print(f"=== Running Stage II for {repo} | model={args.model} | "
              f"max={args.max_instances} | rounds={args.round} ===")

        cmd = [
            sys.executable, "app/main.py", "swe-bench",
            "--model", args.model,
            "--tasks-map", tasks_map,
            "--task-list-file", task_list,
            "--num-processes", str(args.num_procs),
            "--model-temperature", "0.2",
            "--conv-round-limit", str(args.round),
            "--output-dir", out_dir,
            "--setup-dir", args.setup_dir,
            "--results-path", os.path.join(out_dir, "results"),
        ]
        proc = subprocess.Popen(cmd)
        procs.append((repo, proc))

    # Wait for all
    failed = False
    for repo, proc in procs:
        rc = proc.wait()
        if rc != 0:
            print(f"=== ERROR: Stage II failed for {repo} (exit {rc}) ===")
            failed = True

    # Rebuild results
    print("\n=== Rebuilding results.json ===")
    for repo in repos:
        out_dir = os.path.join(data_dir, repo, f"setup_output_{args.model_slug}")
        if not os.path.isdir(os.path.join(out_dir, "applicable_setup")):
            continue
        subprocess.run([sys.executable, "scripts/rebuild_results.py", out_dir], check=False)

    print("\n=== Done ===")
    if failed:
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Generate task list from instances JSONL")
    sub = parser.add_subparsers(dest="command")

    # Default (no subcommand): single-repo task list generation
    parser.add_argument("--tasks-map", help="Path to instances JSONL file")
    parser.add_argument("--task-list", help="Output task list file path")
    parser.add_argument("--max-instances", type=int, default=0, help="Max instances (0 = all)")
    parser.add_argument("--out-dir", help="Output directory to check for status.json")

    # 'run' subcommand: full pipeline orchestration
    run_parser = sub.add_parser("run", help="Run full Stage II pipeline")
    run_parser.add_argument("--model", default="openai/gpt-5.2-codex", help="Model name")
    run_parser.add_argument("--model-slug", default="gpt-5.2", help="Short model name for dir naming")
    run_parser.add_argument("--repos", nargs="*", help="Repos to process (default: all)")
    run_parser.add_argument("--round", type=int, default=3, help="Conv round limit (default: 3)")
    run_parser.add_argument("--num-procs", type=int, default=15, help="Parallel processes (default: 15)")
    run_parser.add_argument("--max-instances", type=int, default=0, help="Max instances (0 = all)")
    run_parser.add_argument("--setup-dir", default="testbed", help="Setup directory (default: testbed)")
    run_parser.add_argument("--data-dir", default=DATA_DIR, help="Data directory")

    args = parser.parse_args()

    if args.command == "run":
        run_pipeline(args)
    else:
        # Backwards-compatible single-repo mode
        if not args.tasks_map or not args.task_list or not args.out_dir:
            parser.error("--tasks-map, --task-list, and --out-dir are required (or use 'run' subcommand)")
        generate_task_list(args.tasks_map, args.task_list, args.max_instances, args.out_dir)


if __name__ == "__main__":
    main()
