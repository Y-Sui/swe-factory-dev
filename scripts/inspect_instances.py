#!/usr/bin/env python3
"""
Extract problem_statement and patch_context from instance JSONL files
for quick quality inspection. Saves top N instances per repo as plain text.

Usage:
    python scripts/inspect_instances.py [--top 50] [--output-dir inspection_output]
"""

import argparse
import json
import os
import textwrap

DATA_ROOT = os.path.join(os.path.dirname(__file__), "..", "internal-swe-bench-data")

REPOS = [
    ("MiroMindAI__MiroThinker", "instances_all_130.jsonl"),
    ("MiroMindAI__miroflow", "instances_all_140.jsonl"),
    ("MiroMindAI__sd-torchtune", "instances_all_341.jsonl"),
]


def load_instances(jsonl_path: str, top_n: int) -> list[dict]:
    instances = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            instances.append(json.loads(line))
            if len(instances) >= top_n:
                break
    return instances


def format_instance(inst: dict) -> str:
    iid = inst.get("instance_id", "unknown")
    ps = inst.get("problem_statement", "") or "(empty)"
    ctx_list = inst.get("patch_context", [])

    parts = [
        f"{'=' * 80}",
        f"Instance ID: {iid}",
        f"Repo: {inst.get('repo', '')}",
        f"PR: #{inst.get('pull_number', '?')}",
        f"Base Commit: {inst.get('base_commit', '')[:12]}",
        f"{'=' * 80}",
        "",
        "## Problem Statement",
        "-" * 40,
        ps.strip() if ps else "(empty)",
        "",
        "## Patch Context",
        "-" * 40,
    ]

    if not ctx_list:
        parts.append("(no patch context)")
    else:
        for ctx in ctx_list:
            if isinstance(ctx, str):
                parts.append(ctx)
            elif isinstance(ctx, dict):
                for key in ["file", "functions", "classes", "imports"]:
                    if key in ctx:
                        val = ctx[key]
                        val_str = json.dumps(val, indent=2) if isinstance(val, (dict, list)) else str(val)
                        parts.append(f"  {key}: {val_str}")
            else:
                parts.append(str(ctx))
            parts.append("")

    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=50, help="Number of instances per repo")
    parser.add_argument("--output-dir", default="inspection_output", help="Output directory")
    args = parser.parse_args()

    data_root = os.path.abspath(DATA_ROOT)
    out_root = os.path.abspath(args.output_dir)
    os.makedirs(out_root, exist_ok=True)

    for repo_dir, jsonl_name in REPOS:
        jsonl_path = os.path.join(data_root, repo_dir, jsonl_name)
        if not os.path.exists(jsonl_path):
            print(f"SKIP: {jsonl_path} not found")
            continue

        repo_out = os.path.join(out_root, repo_dir)
        os.makedirs(repo_out, exist_ok=True)

        instances = load_instances(jsonl_path, args.top)
        print(f"{repo_dir}: loaded {len(instances)} instances -> {repo_out}/")

        for inst in instances:
            iid = inst.get("instance_id", "unknown")
            # Sanitize filename
            safe_name = iid.replace("/", "__").replace(" ", "_")
            txt_path = os.path.join(repo_out, f"{safe_name}.txt")

            content = format_instance(inst)
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(content)

        # Also write a summary index
        summary_path = os.path.join(repo_out, "_summary.txt")
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(f"Repo: {repo_dir}\n")
            f.write(f"Total instances inspected: {len(instances)}\n\n")
            for i, inst in enumerate(instances):
                iid = inst.get("instance_id", "unknown")
                ps = (inst.get("problem_statement") or "").strip()
                ps_preview = ps[:120].replace("\n", " ") + ("..." if len(ps) > 120 else "")
                ctx_count = len(inst.get("patch_context", []))
                has_ps = "OK" if ps else "MISSING"
                f.write(f"{i+1:3d}. [{has_ps:7s}] ctx={ctx_count} | {iid}\n")
                if ps_preview:
                    f.write(f"     {ps_preview}\n")

    print(f"\nDone. Output at: {out_root}/")


if __name__ == "__main__":
    main()
