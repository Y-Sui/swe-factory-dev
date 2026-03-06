#!/usr/bin/env python3
"""Merge results.json from all repos into a single file.

Usage:
    python scripts/merge_results.py [--model-slug gpt-5.2] [--version v1]

Output:
    /home/yuansui/swe-factory-dev/internal-swe-bench-data/results_<version>_<model_slug>_<timestamp>.json
"""

import json
import os
import sys
from datetime import datetime

DATA_DIR = "/home/yuansui/swe-factory-dev/internal-swe-bench-data"
REPOS = ["MiroMindAI__miroflow", "MiroMindAI__MiroThinker", "MiroMindAI__sd-torchtune"]


def main():
    model_slug = "gpt-5.2"
    version = "v1"

    args = sys.argv[1:]
    while args:
        if args[0] == "--model-slug" and len(args) > 1:
            model_slug = args[1]; args = args[2:]
        elif args[0] == "--version" and len(args) > 1:
            version = args[1]; args = args[2:]
        else:
            args = args[1:]

    merged = []
    for repo in REPOS:
        results_path = os.path.join(DATA_DIR, repo, f"setup_output_{model_slug}", "results", "results.json")
        if not os.path.exists(results_path):
            print(f"  SKIP {repo}: {results_path} not found")
            continue
        data = json.load(open(results_path))
        print(f"  {repo}: {len(data)} cases")
        merged.extend(data)

    timestamp = datetime.now().strftime("%Y%m%d")
    model_tag = model_slug.replace("-", "_").replace(".", "_")
    out_name = f"results_{version}_{model_tag}_{len(merged)}_{timestamp}.json"
    out_path = os.path.join(DATA_DIR, out_name)

    with open(out_path, "w") as f:
        json.dump(merged, f, indent=2)

    print(f"\nMerged {len(merged)} cases -> {out_path}")


if __name__ == "__main__":
    main()
