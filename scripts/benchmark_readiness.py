#!/usr/bin/env python3
"""
Benchmark readiness checker: actually builds Docker images and runs pre/post-patch
tests for each instance in predictions.json. Reports FAIL2PASS / FAIL2FAIL / PASS2PASS
per instance and produces an overall readiness summary.

Usage:
    python scripts/benchmark_readiness.py
    python scripts/benchmark_readiness.py --repos miroflow
    python scripts/benchmark_readiness.py --workers 2 --timeout 600
"""

import argparse
import io
import json
import os
import sys
import tarfile
import tempfile
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import docker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from swe_factory_utils import (
    extract_exit_code,
    classify_f2p,
    ensure_essentials_in_dockerfile,
    get_clean_command_for_repo,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SETUP_DIRS = {
    "miroflow":    "internal-swe-bench-data/MiroMindAI__miroflow/setup_output_2026-03-03",
    "mirothinker": "internal-swe-bench-data/MiroMindAI__MiroThinker/setup_output_2026-03-03",
}

JSONL_FILES = {
    "miroflow":    "internal-swe-bench-data/MiroMindAI__miroflow/instances_selected_36.jsonl",
    "mirothinker": "internal-swe-bench-data/MiroMindAI__MiroThinker/instances_selected_24.jsonl",
}

REPO_NAMES = {
    "miroflow":    "MiroMindAI/miroflow",
    "mirothinker": "MiroMindAI/MiroThinker",
}

# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------

def _run_in_container(container, cmd: str, workdir: str = "/testbed", timeout: int = 600):
    """Execute a shell command inside a running container with a timeout."""
    result_holder = {}
    exc_holder = {}

    def _run():
        try:
            exec_id = container.client.api.exec_create(
                container.id, ["/bin/bash", "-c", cmd], workdir=workdir
            )["Id"]
            result_holder["out"] = container.client.api.exec_start(exec_id)
        except Exception as e:
            exc_holder["e"] = e

    t = threading.Thread(target=_run)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"Command timed out after {timeout}s: {cmd[:80]}")
    if exc_holder:
        raise exc_holder["e"]
    return result_holder.get("out", b"").decode("utf-8", errors="replace")


def _copy_str_to_container(container, content: str, dst_path: str):
    """Write a string as a file inside the container using a tar archive."""
    buf = io.BytesIO()
    data = content.encode("utf-8")
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=os.path.basename(dst_path))
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    container.put_archive(os.path.dirname(dst_path) or "/", buf.read())


def _build_image(client, dockerfile_content: str, image_tag: str, buildargs: dict | None = None) -> str:
    """Build a Docker image from dockerfile_content string. Returns error string or empty string."""
    with tempfile.TemporaryDirectory() as ctx_dir:
        df_path = os.path.join(ctx_dir, "Dockerfile")
        Path(df_path).write_text(dockerfile_content)
        try:
            for chunk in client.api.build(
                path=ctx_dir,
                tag=image_tag,
                rm=True,
                forcerm=True,
                decode=True,
                platform="linux/x86_64",
                nocache=False,
                buildargs=buildargs or None,
            ):
                if "errorDetail" in chunk:
                    return "BUILD ERROR:\n" + chunk["errorDetail"]["message"]
            return ""
        except Exception as e:
            return f"BUILD ERROR: {e}"


def _remove_image(client, image_tag: str):
    try:
        client.images.remove(image_tag, force=True)
    except Exception:
        pass



# ---------------------------------------------------------------------------
# Per-instance runner
# ---------------------------------------------------------------------------

def run_instance(client, inst: dict, base_commit: str, verbose: bool = False) -> dict:
    """
    Build Docker image, run eval.sh pre- and post-patch inside a container.
    Returns a result dict with fields: instance_id, build_ok, classification,
    pre_exit, post_exit, error, pre_output, post_output.
    """
    instance_id = inst["instance_id"]
    dockerfile  = inst["dockerfile"]
    eval_script = inst["eval_script"]
    patch       = inst["patch"]
    repo_name   = inst.get("repo", "")
    clean_cmd   = get_clean_command_for_repo(repo_name)

    # Ensure curl/git/ca-certificates are available before any RUN that needs them
    dockerfile = ensure_essentials_in_dockerfile(dockerfile)

    # Inject GITHUB_TOKEN for private repo support
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    buildargs: dict = {}
    if token and "github.com" in dockerfile:
        lines = dockerfile.split("\n")
        out = []
        arg_inserted = False
        for line in lines:
            out.append(line)
            if not arg_inserted and line.strip().upper().startswith("FROM "):
                out.append("ARG GITHUB_TOKEN")
                arg_inserted = True
        dockerfile = "\n".join(out)
        dockerfile = dockerfile.replace(
            "https://github.com/",
            "https://x-access-token:${GITHUB_TOKEN}@github.com/",
        )
        buildargs["GITHUB_TOKEN"] = token

    result = {
        "instance_id": instance_id,
        "build_ok": False,
        "classification": "ERROR",
        "pre_exit": None,
        "post_exit": None,
        "error": None,
        "pre_output_tail": "",
        "post_output_tail": "",
    }

    image_tag = f"readiness-{instance_id.lower()}:latest"
    container = None

    try:
        # --- Build ---
        build_err = _build_image(client, dockerfile, image_tag, buildargs=buildargs or None)
        if build_err:
            result["error"] = build_err[:2000]
            return result
        result["build_ok"] = True

        # --- Create container ---
        container = client.containers.create(
            image=image_tag,
            name=f"readiness-{instance_id.lower()}-run",
            user="root",
            detach=True,
            command="tail -f /dev/null",
            platform="linux/x86_64",
        )
        container.start()

        # --- Phase 1: pre-patch run ---
        _copy_str_to_container(container, eval_script, "/eval.sh")
        pre_out = _run_in_container(container, "/bin/bash /eval.sh", timeout=600)
        pre_exit = extract_exit_code(pre_out)
        result["pre_exit"] = pre_exit
        result["pre_output_tail"] = pre_out[-3000:] if len(pre_out) > 3000 else pre_out

        # --- Reset container to base commit ---
        container.exec_run(f"git reset --hard {base_commit}", workdir="/testbed")
        container.exec_run(clean_cmd, workdir="/testbed")

        # --- Apply gold patch ---
        _copy_str_to_container(container, patch, "/tmp/patch.diff")
        _apply_r = container.exec_run("git apply -p1 /tmp/patch.diff", workdir="/testbed")
        if _apply_r.exit_code != 0:
            # Fallback to GNU patch when git apply fails
            _patch_r = container.exec_run(
                "patch --batch --fuzz=5 -p1 -i /tmp/patch.diff", workdir="/testbed"
            )
            if _patch_r.exit_code != 0:
                result["error"] = (
                    "Patch apply failed: "
                    + _patch_r.output.decode("utf-8", errors="replace")[:500]
                )
                return result

        # --- Phase 2: post-patch run ---
        _copy_str_to_container(container, eval_script, "/eval.sh")
        post_out = _run_in_container(container, "/bin/bash /eval.sh", timeout=600)
        post_exit = extract_exit_code(post_out)
        result["post_exit"] = post_exit
        result["post_output_tail"] = post_out[-3000:] if len(post_out) > 3000 else post_out

        result["classification"] = classify_f2p(pre_exit, post_exit)

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1000:]}"
    finally:
        if container:
            try:
                container.stop(timeout=5)
                container.remove(force=True)
            except Exception:
                pass
        _remove_image(client, image_tag)

    return result


# ---------------------------------------------------------------------------
# Load predictions and JSONL
# ---------------------------------------------------------------------------

def load_predictions(setup_dir: str) -> list[dict]:
    path = Path(setup_dir) / "predictions.json"
    if not path.exists():
        return []
    with open(path) as f:
        return json.load(f)


def load_base_commits(jsonl_path: str) -> dict[str, str]:
    """Returns {instance_id: base_commit}."""
    commits = {}
    path = Path(jsonl_path)
    if not path.exists():
        return commits
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            commits[row["instance_id"]] = row.get("base_commit", "")
    return commits


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

CLASSIFICATION_ORDER = ["FAIL2PASS", "PASS2PASS", "FAIL2FAIL", "PASS2FAIL", "ERROR"]


def print_summary(repo: str, results: list[dict]):
    total = len(results)
    counts = {c: sum(1 for r in results if r["classification"] == c) for c in CLASSIFICATION_ORDER}
    build_ok = sum(1 for r in results if r["build_ok"])

    print(f"\n{'─' * 70}")
    print(f"  {repo}  ({total} instances tested)")
    print(f"{'─' * 70}")
    print(f"  Docker build success: {build_ok}/{total}")
    print()
    print(f"  Classification:")
    for c in CLASSIFICATION_ORDER:
        n = counts[c]
        pct = 100 * n / total if total else 0
        flag = "  ✓" if c == "FAIL2PASS" else ("  ✗" if c in ("FAIL2FAIL", "PASS2FAIL", "ERROR") else "")
        print(f"    {c:<12} {n:>3}  ({pct:.0f}%){flag}")

    print(f"\n  Per-instance breakdown:")
    print(f"  {'Instance':<42} {'Build':<7} {'Pre':>4} {'Post':>5} {'Result':<12}")
    print(f"  {'-'*42} {'-'*7} {'-'*4} {'-'*5} {'-'*12}")
    for r in sorted(results, key=lambda x: x["instance_id"]):
        build_str = "OK" if r["build_ok"] else "FAIL"
        pre  = str(r["pre_exit"])  if r["pre_exit"]  is not None else "—"
        post = str(r["post_exit"]) if r["post_exit"] is not None else "—"
        print(f"  {r['instance_id']:<42} {build_str:<7} {pre:>4} {post:>5} {r['classification']:<12}")


def save_report(all_results: dict[str, list[dict]], output_path: str):
    flat = []
    for repo, results in all_results.items():
        for r in results:
            flat.append({**r, "repo": repo})
    with open(output_path, "w") as f:
        json.dump(flat, f, indent=2)
    print(f"\nDetailed results saved to: {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Benchmark readiness: live Docker build + F2P test.")
    p.add_argument(
        "--repos", nargs="+", default=["miroflow", "mirothinker"],
        choices=list(SETUP_DIRS.keys()),
        help="Which repos to test",
    )
    p.add_argument("--workers", type=int, default=15, help="Parallel Docker workers (default 1 to avoid resource contention)")
    p.add_argument("--timeout", type=int, default=600, help="Per-phase eval.sh timeout in seconds")
    p.add_argument("--output", default="scripts/benchmark_readiness_results.json", help="Output JSON path")
    p.add_argument("--verbose", action="store_true", help="Print pre/post output tails on failure")
    p.add_argument("--instance", help="Run a single instance ID only")
    return p.parse_args()


def main():
    args = parse_args()
    client = docker.from_env()

    all_results: dict[str, list[dict]] = {}

    for repo in args.repos:
        setup_dir = SETUP_DIRS[repo]
        jsonl_path = JSONL_FILES[repo]

        predictions = load_predictions(setup_dir)
        if not predictions:
            print(f"[{repo}] No predictions.json found at {setup_dir}/predictions.json — skipping.")
            continue

        base_commits = load_base_commits(jsonl_path)

        if args.instance:
            predictions = [p for p in predictions if p["instance_id"] == args.instance]
            if not predictions:
                print(f"[{repo}] Instance {args.instance} not found — skipping.")
                continue

        print(f"\n[{repo}] {len(predictions)} instances to test (workers={args.workers})")

        repo_results = []
        done = 0
        total = len(predictions)

        def _task(inst):
            iid = inst["instance_id"]
            base_commit = base_commits.get(iid, "HEAD")
            return run_instance(client, inst, base_commit, verbose=args.verbose)

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(_task, inst): inst["instance_id"] for inst in predictions}
            for future in as_completed(futures):
                iid = futures[future]
                done += 1
                try:
                    r = future.result()
                except Exception as e:
                    r = {
                        "instance_id": iid,
                        "build_ok": False,
                        "classification": "ERROR",
                        "pre_exit": None,
                        "post_exit": None,
                        "error": str(e),
                        "pre_output_tail": "",
                        "post_output_tail": "",
                    }
                repo_results.append(r)

                status = r["classification"]
                build = "build-ok" if r["build_ok"] else "build-fail"
                print(f"  [{done}/{total}] {iid}  →  {build}  {status}")

                if args.verbose and r.get("error"):
                    print(f"    ERROR: {r['error'][:400]}")
                if args.verbose and status != "FAIL2PASS" and r["build_ok"]:
                    if r.get("pre_output_tail"):
                        print(f"    PRE-PATCH output (tail):\n{r['pre_output_tail'][-800:]}")
                    if r.get("post_output_tail"):
                        print(f"    POST-PATCH output (tail):\n{r['post_output_tail'][-800:]}")

        all_results[repo] = repo_results

    # Print summary per repo
    for repo, results in all_results.items():
        print_summary(repo, results)

    # Global summary
    all_flat = [r for results in all_results.values() for r in results]
    total = len(all_flat)
    if total > 0:
        print(f"\n{'=' * 70}")
        print("GLOBAL SUMMARY")
        print(f"{'=' * 70}")
        print(f"  Total instances tested: {total}")
        for c in CLASSIFICATION_ORDER:
            n = sum(1 for r in all_flat if r["classification"] == c)
            pct = 100 * n / total
            print(f"    {c:<12} {n:>3}  ({pct:.0f}%)")
        f2p = sum(1 for r in all_flat if r["classification"] == "FAIL2PASS")
        print(f"\n  Benchmark readiness rate (FAIL2PASS): {f2p}/{total} = {100*f2p/total:.1f}%")

    save_report(all_results, args.output)


if __name__ == "__main__":
    main()
