#!/usr/bin/env python3
"""
Instance analysis script: classify benchmark instances by type and difficulty.
Generates a structured report across miroflow, mirothinker, and sd-torchtune.

Usage:
    python scripts/analysis.py
    python scripts/analysis.py --output scripts/analysis_results.json
"""

import json
import os
import re
import sys
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

INSTANCE_FILES = {
    "miroflow": "internal-swe-bench-data/MiroMindAI__miroflow/instances_selected_36.jsonl",
    "mirothinker": "internal-swe-bench-data/MiroMindAI__MiroThinker/instances_selected_24.jsonl",
    "torchtune": "internal-swe-bench-data/MiroMindAI__sd-torchtune/instances_selected_50.jsonl",
}

DEFAULT_OUTPUT = "scripts/analysis_results.json"

# ---------------------------------------------------------------------------
# Classification schema
# ---------------------------------------------------------------------------

# Type labels (mutually exclusive; use "mixed" only when two major categories
# are clearly and equally represented)
VALID_TYPES = {
    "bug_fix",       # fix a defect / unintended behaviour / error
    "feature",       # add new functionality, model support, benchmark, tool
    "refactor",      # code restructuring without changing external behaviour
    "performance",   # speed / memory / resource optimisation
    "docs_config",   # documentation, CI/CD, config files, lock files only
    "mixed",         # two clearly distinct major categories present
}

# Difficulty labels
VALID_DIFFICULTIES = {"easy", "medium", "hard"}

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PatchStats:
    lines_added: int
    lines_removed: int
    files_changed: int

    @property
    def total_changed(self) -> int:
        return self.lines_added + self.lines_removed


@dataclass
class InstanceAnalysis:
    instance_id: str
    repo: str
    title: str                # first line of problem_statement
    patch_stats: PatchStats
    inst_type: str            # one of VALID_TYPES
    difficulty: str           # one of VALID_DIFFICULTIES
    type_reason: str          # one-sentence justification
    difficulty_reason: str    # one-sentence justification


# ---------------------------------------------------------------------------
# Patch statistics (deterministic, no LLM)
# ---------------------------------------------------------------------------

def compute_patch_stats(patch: str) -> PatchStats:
    lines = patch.splitlines()
    added = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
    files = len([l for l in lines if l.startswith("diff --git")])
    return PatchStats(lines_added=added, lines_removed=removed, files_changed=files)


# ---------------------------------------------------------------------------
# LLM classification
# ---------------------------------------------------------------------------

CLASSIFICATION_SYSTEM = """You are a senior software engineer analysing GitHub pull requests.
For each pull request you receive, output a JSON object with exactly these fields:
  "type"              : one of [bug_fix, feature, refactor, performance, docs_config, mixed]
  "difficulty"        : one of [easy, medium, hard]
  "type_reason"       : one sentence explaining the type choice
  "difficulty_reason" : one sentence explaining the difficulty choice

Definitions:
  bug_fix     – fixes a defect, error, or unintended runtime behaviour
  feature     – adds new functionality, model support, benchmark, tool, or capability
  refactor    – restructures code without changing external/observable behaviour
  performance – optimises speed, memory, or resource usage
  docs_config – changes only documentation, CI/CD pipelines, config files, or lock files
  mixed       – two clearly distinct major categories that cannot be reduced to one

Difficulty rules:
  easy   – total lines changed ≤ 30, OR a very targeted, well-scoped single-purpose fix
  medium – total lines changed 31–300, or moderate complexity / scope
  hard   – total lines changed > 300, OR highly complex / vague description, multi-subsystem impact

Return ONLY valid JSON — no markdown fences, no commentary.
"""

SINGLE_USER_TEMPLATE = """Title: {title}
Patch stats: +{added} added / -{removed} removed lines, {files} file(s) changed
Problem statement:
{problem}
"""


def classify_one(client: OpenAI, inst: dict, model: str) -> dict:
    """Call LLM to classify a single instance. Returns classification dict."""
    prompt = SINGLE_USER_TEMPLATE.format(
        title=inst["title"],
        added=inst["stats"].lines_added,
        removed=inst["stats"].lines_removed,
        files=inst["stats"].files_changed,
        problem=inst["problem"],
    )
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": CLASSIFICATION_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
    )
    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_jsonl(path: str) -> list[dict]:
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def prepare_instances(repo: str, raw: list[dict]) -> list[dict]:
    prepared = []
    for inst in raw:
        stats = compute_patch_stats(inst.get("patch", ""))
        title = inst["problem_statement"].splitlines()[0].strip()
        prepared.append({
            "instance_id": inst["instance_id"],
            "repo": repo,
            "title": title,
            "problem": inst["problem_statement"],
            "stats": stats,
            "raw": inst,
        })
    return prepared


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_report(analyses: list[InstanceAnalysis]) -> None:
    by_repo = defaultdict(list)
    for a in analyses:
        by_repo[a.repo].append(a)

    print("\n" + "=" * 70)
    print("BENCHMARK INSTANCE ANALYSIS")
    print("=" * 70)

    for repo, items in by_repo.items():
        print(f"\n{'─' * 70}")
        print(f"  Repository: {repo}  ({len(items)} instances)")
        print(f"{'─' * 70}")

        type_counts = Counter(a.inst_type for a in items)
        diff_counts = Counter(a.difficulty for a in items)

        print(f"\n  Type distribution:")
        for t, c in sorted(type_counts.items()):
            print(f"    {t:<15} {c:>3}  ({100*c/len(items):.0f}%)")

        print(f"\n  Difficulty distribution:")
        for d in ["easy", "medium", "hard"]:
            c = diff_counts[d]
            print(f"    {d:<10} {c:>3}  ({100*c/len(items):.0f}%)")

        print(f"\n  Instance breakdown:")
        print(f"  {'ID':<40} {'Type':<14} {'Diff':<8} {'±Lines':>7} {'Files':>6}")
        print(f"  {'-'*40} {'-'*14} {'-'*8} {'-'*7} {'-'*6}")
        for a in sorted(items, key=lambda x: x.patch_stats.total_changed):
            print(
                f"  {a.instance_id:<40} {a.inst_type:<14} {a.difficulty:<8} "
                f"{a.patch_stats.total_changed:>7} {a.patch_stats.files_changed:>6}"
            )

    # Global summary
    print(f"\n{'=' * 70}")
    print("GLOBAL SUMMARY")
    print(f"{'=' * 70}")
    total = len(analyses)
    type_counts = Counter(a.inst_type for a in analyses)
    diff_counts = Counter(a.difficulty for a in analyses)

    print(f"\n  Total instances: {total}")
    print(f"\n  By type:")
    for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"    {t:<15} {c:>3}  ({100*c/total:.0f}%)")
    print(f"\n  By difficulty:")
    for d in ["easy", "medium", "hard"]:
        c = diff_counts[d]
        print(f"    {d:<10} {c:>3}  ({100*c/total:.0f}%)")
    print()


def save_results(analyses: list[InstanceAnalysis], output_path: str) -> None:
    results = []
    for a in analyses:
        results.append({
            "instance_id": a.instance_id,
            "repo": a.repo,
            "title": a.title,
            "patch_stats": asdict(a.patch_stats),
            "type": a.inst_type,
            "difficulty": a.difficulty,
            "type_reason": a.type_reason,
            "difficulty_reason": a.difficulty_reason,
        })
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"  Saved detailed results to: {output_path}")


def write_back_to_jsonl(analyses: list[InstanceAnalysis]) -> None:
    """Merge classification fields back into the original JSONL files in-place."""
    clf_by_id = {
        a.instance_id: {
            "analysis_type": a.inst_type,
            "analysis_difficulty": a.difficulty,
            "analysis_type_reason": a.type_reason,
            "analysis_difficulty_reason": a.difficulty_reason,
        }
        for a in analyses
    }

    for repo, rel_path in INSTANCE_FILES.items():
        path = Path(rel_path)
        if not path.exists():
            continue
        rows = load_jsonl(str(path))
        updated = 0
        for row in rows:
            clf = clf_by_id.get(row["instance_id"])
            if clf:
                row.update(clf)
                updated += 1
        with open(path, "w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        print(f"  Written back {updated}/{len(rows)} instances to {path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="Analyse and classify benchmark instances.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Output JSON path")
    parser.add_argument(
        "--model",
        default="openai/gpt-4.1",
        help="LLM model for classification (OpenRouter format)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=20,
        help="Number of parallel LLM calls",
    )
    return parser.parse_args()


def _classify_task(args_tuple):
    """Worker function for ThreadPoolExecutor."""
    client, inst, model = args_tuple
    clf = classify_one(client, inst, model)
    return inst, clf


def main():
    args = parse_args()

    api_key = os.getenv("OPENAI_KEY")
    base_url = os.getenv("OPENAI_API_BASE_URL", "https://openrouter.ai/api/v1")
    if not api_key:
        print("ERROR: OPENAI_KEY not set.", file=sys.stderr)
        sys.exit(1)

    client = OpenAI(api_key=api_key, base_url=base_url)

    # Load and prepare all instances
    all_instances: list[dict] = []
    for repo, rel_path in INSTANCE_FILES.items():
        path = Path(rel_path)
        if not path.exists():
            print(f"WARNING: {path} not found, skipping {repo}", file=sys.stderr)
            continue
        raw = load_jsonl(str(path))
        prepared = prepare_instances(repo, raw)
        all_instances.extend(prepared)
        print(f"  Loaded {len(prepared)} instances from {repo}")

    total = len(all_instances)
    print(f"\n  Total: {total} instances to classify")
    print(f"  Model: {args.model}  |  Workers: {args.workers}\n")

    # Classify in parallel — one LLM call per instance
    all_analyses: list[InstanceAnalysis] = []
    done = 0
    tasks = [(client, inst, args.model) for inst in all_instances]

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(_classify_task, t): t[1] for t in tasks}
        for future in as_completed(futures):
            inst = futures[future]
            done += 1
            try:
                _, clf = future.result()
            except Exception as e:
                print(f"  ERROR classifying {inst['instance_id']}: {e}", file=sys.stderr)
                clf = {}

            inst_type = clf.get("type", "mixed")
            difficulty = clf.get("difficulty", "medium")

            if inst_type not in VALID_TYPES:
                print(f"  WARNING: unknown type '{inst_type}' for {inst['instance_id']}, using 'mixed'")
                inst_type = "mixed"
            if difficulty not in VALID_DIFFICULTIES:
                print(f"  WARNING: unknown difficulty '{difficulty}' for {inst['instance_id']}, using 'medium'")
                difficulty = "medium"

            all_analyses.append(
                InstanceAnalysis(
                    instance_id=inst["instance_id"],
                    repo=inst["repo"],
                    title=inst["title"],
                    patch_stats=inst["stats"],
                    inst_type=inst_type,
                    difficulty=difficulty,
                    type_reason=clf.get("type_reason", ""),
                    difficulty_reason=clf.get("difficulty_reason", ""),
                )
            )
            print(f"  [{done}/{total}] {inst['instance_id']} → {inst_type} / {difficulty}")

    # Report and save
    print_report(all_analyses)
    save_results(all_analyses, args.output)
    write_back_to_jsonl(all_analyses)


if __name__ == "__main__":
    main()
