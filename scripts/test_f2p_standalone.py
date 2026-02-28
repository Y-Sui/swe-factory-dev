#!/usr/bin/env python3
"""
Standalone F2P (Fail-to-Pass) tester for already-generated artifacts.

Reads Dockerfile + eval.sh + meta.json from each task directory, builds the
Docker image, runs eval.sh twice (without / with gold patch), and reports
whether each task achieves FAIL2PASS.

Usage:
    python scripts/test_f2p_standalone.py --output-dir <dir> [--timeout 1800] [--num-workers 4]

The <dir> should be the setup_output directory (or setup_output_small) that
contains per-task subdirectories with Dockerfile, eval.sh, and meta.json.
If the directory contains an applicable_setup/ subdirectory, tasks are read
from there; otherwise task dirs are found directly.
"""

import argparse
import json
import os
import sys
import tarfile
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Ensure project root is on sys.path so swe_factory_utils is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import docker

from swe_factory_utils import (
    extract_exit_code,
    classify_f2p,
    ensure_essentials_in_dockerfile as _ensure_essentials,
)


def copy_to_container(container, src: Path, dst: Path):
    tar_path = src.with_suffix(".tar")
    with tarfile.open(tar_path, "w") as tar:
        tar.add(src, arcname=src.name)
    with open(tar_path, "rb") as f:
        data = f.read()
    container.exec_run(f"mkdir -p {dst.parent}")
    container.put_archive(os.path.dirname(dst), data)
    tar_path.unlink()


def exec_run_with_timeout(container, cmd, timeout=1800):
    exec_result = None
    exception = None

    def run():
        nonlocal exec_result, exception
        try:
            eid = container.client.api.exec_create(container.id, cmd)["Id"]
            exec_result = container.client.api.exec_start(eid)
        except Exception as e:
            exception = e

    t = threading.Thread(target=run)
    t.start()
    t.join(timeout)
    if exception:
        raise exception
    if t.is_alive():
        raise TimeoutError(f"Command timed out after {timeout}s")
    return exec_result


def run_f2p_for_task(task_dir: str, client: docker.DockerClient, timeout: int) -> dict:
    """Run F2P validation for a single task directory. Returns result dict."""
    task_id = os.path.basename(task_dir)
    result = {"task_id": task_id, "f2p": "ERROR", "detail": ""}

    dockerfile_path = os.path.join(task_dir, "Dockerfile")
    eval_path = os.path.join(task_dir, "eval.sh")
    meta_path = os.path.join(task_dir, "meta.json")

    for required in (dockerfile_path, eval_path, meta_path):
        if not os.path.isfile(required):
            result["detail"] = f"Missing {os.path.basename(required)}"
            return result

    with open(meta_path) as f:
        meta = json.load(f)

    task_info = meta.get("task_info", meta)
    gold_patch = task_info.get("patch", "")
    commit = task_info.get("base_commit", task_info.get("commit", ""))

    with open(dockerfile_path) as f:
        dockerfile_content = f.read()
    with open(eval_path) as f:
        eval_content = f.read()

    # Ensure curl/git/ca-certificates are available before any RUN that needs them
    dockerfile_content = _ensure_essentials(dockerfile_content)

    # Inject ARG GITHUB_TOKEN for private repo support via --build-arg
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token and "github.com" in dockerfile_content:
        lines = dockerfile_content.split("\n")
        out = []
        arg_inserted = False
        for line in lines:
            out.append(line)
            if not arg_inserted and line.strip().upper().startswith("FROM "):
                out.append("ARG GITHUB_TOKEN")
                arg_inserted = True
        dockerfile_content = "\n".join(out)
        dockerfile_content = dockerfile_content.replace(
            "https://github.com/",
            "https://x-access-token:${GITHUB_TOKEN}@github.com/",
        )

    buildargs = {}
    if token:
        buildargs["GITHUB_TOKEN"] = token

    image_name = f"f2p-test-{task_id.lower()}:latest"
    container_name = f"f2p-test-{task_id.lower()}"
    container = None

    with tempfile.TemporaryDirectory() as build_dir:
        # Write Dockerfile
        df_path = os.path.join(build_dir, "Dockerfile")
        with open(df_path, "w") as f:
            f.write(dockerfile_content)

        # Build image
        try:
            print(f"  [{task_id}] Building Docker image...")
            for chunk in client.api.build(
                path=build_dir,
                tag=image_name,
                rm=True,
                forcerm=True,
                decode=True,
                platform="linux/x86_64",
                nocache=False,
                buildargs=buildargs or None,
            ):
                if "errorDetail" in chunk:
                    err = chunk["errorDetail"]["message"]
                    result["detail"] = f"Build failed: {err}"
                    return result
        except Exception as e:
            result["detail"] = f"Build exception: {e}"
            return result

        try:
            # Create and start container
            container = client.containers.create(
                image=image_name,
                name=container_name,
                user="root",
                detach=True,
                command="tail -f /dev/null",
                platform="linux/x86_64",
            )
            container.start()

            # Write eval.sh locally for copying
            eval_file = Path(build_dir) / "eval.sh"
            eval_file.write_text(eval_content)

            # === Phase 1: Pre-patch (no gold patch) ===
            print(f"  [{task_id}] Phase 1: Running tests WITHOUT gold patch...")
            copy_to_container(container, eval_file, Path("/eval.sh"))
            pre_output = exec_run_with_timeout(
                container, "/bin/bash /eval.sh", timeout=timeout
            ).decode("utf-8", errors="replace")
            pre_exit = extract_exit_code(pre_output)

            # === Reset for Phase 2 ===
            if commit:
                container.exec_run(f"git reset --hard {commit}", workdir="/testbed", user="root")
                container.exec_run("git clean -fdx", workdir="/testbed", user="root")

            # === Phase 2: Post-patch (with gold patch) ===
            print(f"  [{task_id}] Phase 2: Running tests WITH gold patch...")
            patch_file = Path(build_dir) / "patch.diff"
            patch_file.write_text(gold_patch or "")
            copy_to_container(container, patch_file, Path("/tmp/patch.diff"))

            val = container.exec_run(
                "git apply -p1 -v /tmp/patch.diff", workdir="/testbed", user="root"
            )
            if val.exit_code != 0:
                val = container.exec_run(
                    "patch --batch --fuzz=5 -p1 -i /tmp/patch.diff",
                    workdir="/testbed", user="root",
                )
                if val.exit_code != 0:
                    result["detail"] = f"Patch apply failed: {val.output.decode('utf-8', errors='replace')[:500]}"
                    result["f2p"] = "ERROR"
                    return result

            copy_to_container(container, eval_file, Path("/eval.sh"))
            post_output = exec_run_with_timeout(
                container, "/bin/bash /eval.sh", timeout=timeout
            ).decode("utf-8", errors="replace")
            post_exit = extract_exit_code(post_output)

            classification = classify_f2p(pre_exit, post_exit)
            result["f2p"] = classification
            result["pre_exit"] = pre_exit
            result["post_exit"] = post_exit
            result["detail"] = f"pre={pre_exit} post={post_exit}"
            print(f"  [{task_id}] F2P={classification} (pre={pre_exit}, post={post_exit})")

        except Exception as e:
            result["detail"] = f"Runtime error: {e}"
            print(f"  [{task_id}] Error: {e}")
        finally:
            if container:
                try:
                    container.stop(timeout=10)
                except Exception:
                    pass
                try:
                    container.remove(force=True)
                except Exception:
                    pass
            try:
                client.images.remove(image_name, force=True)
            except Exception:
                pass

    return result


def find_task_dirs(output_dir: str) -> list[str]:
    """Find task directories containing Dockerfile + eval.sh."""
    # Check applicable_setup/ first
    applicable = os.path.join(output_dir, "applicable_setup")
    search_dir = applicable if os.path.isdir(applicable) else output_dir

    dirs = []
    for name in os.listdir(search_dir):
        d = os.path.join(search_dir, name)
        if os.path.isdir(d) and os.path.isfile(os.path.join(d, "Dockerfile")):
            dirs.append(d)
    return sorted(dirs)


def main():
    parser = argparse.ArgumentParser(description="Standalone F2P tester for generated artifacts")
    parser.add_argument("--output-dir", required=True, help="Output directory with task subdirs")
    parser.add_argument("--timeout", type=int, default=1800, help="Timeout per test run (seconds)")
    parser.add_argument("--num-workers", type=int, default=3, help="Parallel workers")
    args = parser.parse_args()

    task_dirs = find_task_dirs(args.output_dir)
    if not task_dirs:
        print(f"No task directories with Dockerfile found in {args.output_dir}")
        sys.exit(1)

    print(f"Found {len(task_dirs)} tasks to test")
    client = docker.from_env()
    results = []

    if args.num_workers <= 1:
        for td in task_dirs:
            results.append(run_f2p_for_task(td, client, args.timeout))
    else:
        with ThreadPoolExecutor(max_workers=args.num_workers) as executor:
            futures = {
                executor.submit(run_f2p_for_task, td, client, args.timeout): td
                for td in task_dirs
            }
            for future in as_completed(futures):
                results.append(future.result())

    # Summary
    counts = {"FAIL2PASS": 0, "PASS2PASS": 0, "FAIL2FAIL": 0, "PASS2FAIL": 0, "ERROR": 0}
    for r in results:
        counts[r["f2p"]] = counts.get(r["f2p"], 0) + 1

    print("\n" + "=" * 60)
    print("F2P RESULTS SUMMARY")
    print("=" * 60)
    for cat in ("FAIL2PASS", "PASS2PASS", "FAIL2FAIL", "PASS2FAIL", "ERROR"):
        marker = " <<<" if cat == "FAIL2PASS" and counts[cat] > 0 else ""
        print(f"  {cat:12s}: {counts[cat]}{marker}")
    print(f"  {'TOTAL':12s}: {len(results)}")
    print()

    for r in sorted(results, key=lambda x: x["f2p"]):
        print(f"  {r['f2p']:12s}  {r['task_id']}  ({r['detail']})")

    # Write JSON report
    report_path = os.path.join(args.output_dir, "f2p_report.json")
    with open(report_path, "w") as f:
        json.dump({"summary": counts, "results": results}, f, indent=2)
    print(f"\nReport written to {report_path}")

    # Exit with failure if no FAIL2PASS
    if counts["FAIL2PASS"] == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
