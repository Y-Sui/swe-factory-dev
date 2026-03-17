#!/usr/bin/env python3
"""SWE-Smith pipeline: generate SWE-bench instances from function masking.

Stages:
  1  — Discover: Claude Agent explores repos, identifies core functions
  1b — Validate: Claude Agent reads code, filters out glue/trivial functions
  2  — Extract: AST extracts source/signature/docstring/call count, LLM reviews docstrings
  3  — Test gen: Claude Agent generates pytest tests, validates they PASS on original
  4  — Mask: LibCST masks functions, strips leaking tests, outputs JSONL

Usage:
    python swe-smith-dev/run_pipeline.py --repos miroflow mirothinker sd-torchtune
    python swe-smith-dev/run_pipeline.py --stages 1b          # just validate existing stage1 output
    python3 swe-smith-dev/run_pipeline.py --stages 3,4  # full pipeline
    python3 swe-smith-dev/run_pipeline.py --repos miroflow mirothinker sd-torchtune --stages 1b
    python3 swe-smith-dev/run_pipeline.py --repos miroflow --stages 3

"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Allow Claude Agent SDK to run inside a Claude Code session
os.environ.pop("CLAUDECODE", None)

sys.path.insert(0, str(Path(__file__).parent))

from config import REPOS, CANDIDATES_PER_REPO, TOTAL_SAMPLES
from stage1_discover import discover_all_repos, save_discovered, load_discovered
from stage1b_validate import validate_all_repos
from stage2_extract_rank import extract_all, review_docstrings, save_extracted, load_extracted
from stage3_test_gen import generate_and_validate_all, save_tested, load_tested
from stage4_mask import mask_and_format_all


async def main():
    parser = argparse.ArgumentParser(description="SWE-Smith pipeline")
    parser.add_argument("--repos", nargs="+", default=list(REPOS.keys()),
                        choices=list(REPOS.keys()), help="Repos to process")
    parser.add_argument("--output-dir", default="swe-smith-dev/output", help="Output directory")
    parser.add_argument("--stages", default="1,1b,2,3,4",
                        help="Comma-separated stages to run: 1, 1b, 2, 3, 4")
    parser.add_argument("--total-samples", type=int, default=TOTAL_SAMPLES)
    parser.add_argument("--candidates-per-repo", type=int, default=CANDIDATES_PER_REPO)
    args = parser.parse_args()

    stages = set(s.strip() for s in args.stages.split(","))
    output_base = Path(args.output_dir)
    all_instances = []

    # ── Stage 1: Discover ─────────────────────────────────────────────
    if "1" in stages:
        print(f"\n{'='*60}")
        print(f"Stage 1: Claude Agent discovering core functions")
        print(f"{'='*60}")
        discovered = await discover_all_repos(args.repos, args.candidates_per_repo)
        save_discovered(discovered, output_base)
    else:
        discovered = {}
        for repo_key in args.repos:
            cands = load_discovered(output_base, repo_key)
            if cands:
                discovered[repo_key] = cands
                print(f"Loaded {len(cands)} discovered for {repo_key}")

    # ── Stage 1b: Validate ────────────────────────────────────────────
    if "1b" in stages:
        print(f"\n{'='*60}")
        print(f"Stage 1b: Validating discovered candidates")
        print(f"{'='*60}")
        if not discovered:
            for repo_key in args.repos:
                cands = load_discovered(output_base, repo_key)
                if cands:
                    discovered[repo_key] = cands
                    print(f"Loaded {len(cands)} discovered for {repo_key}")
        discovered = await validate_all_repos(discovered)
        save_discovered(discovered, output_base)

    # ── Stage 2: Extract + Docstring review ───────────────────────────
    extracted_per_repo: dict[str, list[dict]] = {}
    if "2" in stages:
        async def _s2(repo_key: str) -> tuple[str, list[dict]]:
            repo_dir = output_base / repo_key
            repo_dir.mkdir(parents=True, exist_ok=True)
            cands = discovered.get(repo_key, []) or load_discovered(output_base, repo_key)
            if not cands:
                print(f"No discovered candidates for {repo_key}, skipping")
                return repo_key, []
            print(f"\n{'='*60}")
            print(f"Stage 2: Extract for {repo_key} ({len(cands)} discovered)")
            print(f"{'='*60}")
            extracted = extract_all(repo_key, cands)
            print(f"  Extracted {len(extracted)} valid functions from AST")
            if not extracted:
                return repo_key, []
            reviewed = await review_docstrings(repo_key, extracted)
            save_extracted(reviewed, repo_dir / "stage2_extracted.jsonl")
            return repo_key, reviewed

        for key, extracted in await asyncio.gather(*[_s2(k) for k in args.repos]):
            if extracted:
                extracted_per_repo[key] = extracted
    else:
        for repo_key in args.repos:
            s2_path = output_base / repo_key / "stage2_extracted.jsonl"
            if s2_path.exists():
                extracted_per_repo[repo_key] = load_extracted(s2_path)
                print(f"Loaded {len(extracted_per_repo[repo_key])} extracted from {s2_path}")

    # ── Stage 3: Test generation ──────────────────────────────────────
    tested_per_repo: dict[str, list[dict]] = {}
    if "3" in stages:
        async def _s3(repo_key: str) -> tuple[str, list[dict]]:
            repo_dir = output_base / repo_key
            repo_dir.mkdir(parents=True, exist_ok=True)
            cands = extracted_per_repo.get(repo_key, [])
            if not cands:
                cands = load_extracted(output_base / repo_key / "stage2_extracted.jsonl")
            if not cands:
                print(f"No extracted candidates for {repo_key}, skipping")
                return repo_key, []
            print(f"\n{'='*60}")
            print(f"Stage 3: Test generation for {repo_key} ({len(cands)} candidates)")
            print(f"{'='*60}")
            s3_path = repo_dir / "stage3_tested.jsonl"
            tested = await generate_and_validate_all(cands, repo_key, output_path=s3_path)
            save_tested(tested, s3_path)
            return repo_key, tested

        for key, tested in await asyncio.gather(*[_s3(k) for k in args.repos]):
            if tested:
                tested_per_repo[key] = tested
    else:
        for repo_key in args.repos:
            s3_path = output_base / repo_key / "stage3_tested.jsonl"
            if s3_path.exists():
                tested_per_repo[repo_key] = load_tested(s3_path)
                print(f"Loaded {len(tested_per_repo[repo_key])} tested from {s3_path}")

    # ── Stage 4: Mask + Format ────────────────────────────────────────
    if "4" in stages:
        for repo_key in args.repos:
            if repo_key not in tested_per_repo:
                continue
            repo_dir = output_base / repo_key
            repo_dir.mkdir(parents=True, exist_ok=True)
            print(f"\n{'='*60}")
            print(f"Stage 4: Mask + Format for {repo_key} ({len(tested_per_repo[repo_key])} candidates)")
            print(f"{'='*60}")
            instances = mask_and_format_all(tested_per_repo[repo_key], repo_key, repo_dir / "instances.jsonl")
            all_instances.extend(instances)

    # ── Final combined output ─────────────────────────────────────────
    if "4" in stages and all_instances:
        final = all_instances[:args.total_samples]
        final_path = output_base / "swe_smith_instances.jsonl"
        with open(final_path, "w") as f:
            for inst in final:
                f.write(json.dumps(inst) + "\n")
        print(f"\n{'='*60}")
        print(f"DONE: {len(final)} instances written to {final_path}")
        print(f"{'='*60}")


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent.parent / ".env")
    except ImportError:
        pass
    asyncio.run(main())
