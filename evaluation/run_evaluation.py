"""Evaluation harness for SWE-bench instances.

Two modes:
  benchmark - Run eval before AND after applying gold patch (validates test cases for benchmark creation)
  evaluate  - Apply agent's patch on clean container, run eval (checks if agent solved the instance)

Usage:
  # Benchmark creation: validate gold patches produce F2P
  python run_evaluation.py --dataset_path data.json --mode benchmark --run_id run1 --output_path output/

  # Agent evaluation: test agent predictions
  python run_evaluation.py --dataset_path data.json --predictions_path preds.json --mode evaluate --run_id run1 --output_path output/
"""
from __future__ import annotations

import docker
import json
import re
import resource
import sys
import traceback
from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from swe_factory_utils import extract_exit_code
from docker_utils import (
    remove_image,
    copy_to_container,
    exec_run_with_timeout,
    cleanup_container,
    list_images,
    clean_images,
)
from docker_build import (
    build_setup_container,
    close_logger,
    setup_logger,
)
from test_spec import make_test_spec, TestSpec

APPLY_PATCH_FAIL = ">>>>> Patch Apply Failed"
APPLY_PATCH_PASS = ">>>>> Patch Apply Passed"

# Matches pytest verbose lines like "tests/test_foo.py::test_bar PASSED"
PYTEST_RESULT_RE = re.compile(r"^(\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED)", re.MULTILINE)


def parse_test_results(test_output: str) -> dict[str, str]:
    """Parse pytest -v output into {test_id: "PASSED"|"FAILED"|...}."""
    return {m.group(1): m.group(2) for m in PYTEST_RESULT_RE.finditer(test_output)}


def classify_tests(test_results: dict[str, str], f2p_tests: list[str], p2p_tests: list[str]) -> dict:
    """Classify per-test results against F2P/P2P lists.

    If F2P/P2P lists are empty, infer from test name convention (test_f2p_* / test_p2p_*).
    """
    if not f2p_tests and not p2p_tests and test_results:
        f2p_tests = [t for t in test_results if "test_f2p_" in t]
        p2p_tests = [t for t in test_results if "test_p2p_" in t]

    f2p_passed = sum(1 for t in f2p_tests if test_results.get(t) == "PASSED")
    p2p_passed = sum(1 for t in p2p_tests if test_results.get(t) == "PASSED")
    total_tests = len(test_results)
    total_passed = sum(1 for v in test_results.values() if v == "PASSED")
    return {
        "total_tests": total_tests,
        "total_passed": total_passed,
        "f2p_total": len(f2p_tests),
        "f2p_passed": f2p_passed,
        "p2p_total": len(p2p_tests),
        "p2p_passed": p2p_passed,
        "per_test": test_results,
    }


def load_trajectory_stats(preds_dir: str, instance_id: str) -> dict:
    """Load cost and steps from trajectory file if available."""
    if not preds_dir:
        return {}
    traj_dir = Path(preds_dir) / instance_id
    if not traj_dir.is_dir():
        return {}
    traj_files = list(traj_dir.glob("*.traj.json"))
    if not traj_files:
        return {}
    try:
        traj = json.loads(traj_files[0].read_text())
        stats = traj.get("info", {}).get("model_stats", {})
        msgs = traj.get("messages", [])
        steps = sum(1 for m in msgs if m.get("role") == "assistant")
        return {
            "cost": stats.get("instance_cost", 0),
            "steps": steps,
        }
    except Exception:
        return {}


class EvaluationError(Exception):
    def __init__(self, instance_id, message, logger):
        super().__init__(message)
        self.instance_id = instance_id
        self.log_file = logger.log_file
        self.logger = logger

    def __str__(self):
        self.logger.info(traceback.format_exc())
        return (
            f"{self.instance_id}: {super().__str__()}\n"
            f"Check ({self.log_file}) for more information."
        )


# ---------------------------------------------------------------------------
# Helpers: patch apply + eval run (shared by both modes)
# ---------------------------------------------------------------------------

def apply_patch(container, patch_content: str, log_dir: Path, logger) -> bool:
    """Copy patch into container and apply it. Returns True if successful."""
    patch_file = log_dir / "patch.diff"
    patch_file.write_text(patch_content or "")
    copy_to_container(container, patch_file, Path("/tmp/patch.diff"))

    # Try git apply first
    val = container.exec_run(
        "git apply -p1 -v /tmp/patch.diff", workdir="/testbed", user="root",
    )
    if val.exit_code == 0:
        logger.info(f"{APPLY_PATCH_PASS}:\n{val.output.decode('utf-8')}")
        return True

    # Fallback to patch command
    logger.info("git apply failed, trying patch command...")
    val = container.exec_run(
        "patch --batch --fuzz=5 -p1 -i /tmp/patch.diff", workdir="/testbed", user="root",
    )
    if val.exit_code == 0:
        logger.info(f"{APPLY_PATCH_PASS}:\n{val.output.decode('utf-8')}")
        return True

    logger.info(f"{APPLY_PATCH_FAIL}:\n{val.output.decode('utf-8')}")
    return False


def run_eval(container, eval_script: str, log_dir: Path, output_name: str, logger, timeout):
    """Copy eval script into container, run it, save output. Returns (output_text, exit_code)."""
    eval_file = log_dir / "eval.sh"
    eval_file.write_text(eval_script)
    copy_to_container(container, eval_file, Path("/eval.sh"))

    result = exec_run_with_timeout(container, "/bin/bash /eval.sh", timeout=timeout)
    test_output = result.decode("utf-8") if result else ""

    output_path = log_dir / output_name
    output_path.write_text(test_output)
    logger.info(f"Test output written to {output_path}")

    exit_code = extract_exit_code(test_output)
    return test_output, exit_code


# ---------------------------------------------------------------------------
# Mode 1: Benchmark creation — pre-patch + post-patch in one container
# ---------------------------------------------------------------------------

def run_instance_benchmark(
    test_spec: TestSpec, pred: dict, client: docker.DockerClient,
    run_id: str, output_path: str, rm_image: bool, force_rebuild: bool,
    timeout: int | None = None,
):
    instance_id = test_spec.instance_id
    model_name = pred.get("model_name_or_path", "gold").replace("/", "__")
    log_dir = Path(output_path) / run_id / model_name / instance_id
    log_dir.mkdir(parents=True, exist_ok=True)

    report_path = log_dir / "report.json"
    if report_path.exists():
        return instance_id, json.loads(report_path.read_text())

    log_file = log_dir / "run_instance.log"
    logger = setup_logger(instance_id, log_file)
    container = None
    try:
        container = build_setup_container(
            test_spec, client, run_id, logger, rm_image, log_dir, force_rebuild, "benchmark",
        )
        container.start()
        logger.info(f"Container started: {container.id}")

        # Pre-patch: run eval on unpatched code
        pre_output, pre_exit = run_eval(
            container, test_spec.eval_script, log_dir, "test_output_prev_apply.txt", logger, timeout,
        )
        logger.info(f"Pre-patch exit code: {pre_exit}")

        # Apply gold patch
        patch_ok = apply_patch(container, test_spec.patch, log_dir, logger)
        if not patch_ok:
            raise EvaluationError(instance_id, "Failed to apply gold patch", logger)

        # Post-patch: run eval on patched code
        post_output, post_exit = run_eval(
            container, test_spec.eval_script, log_dir, "test_output_after_apply.txt", logger, timeout,
        )
        logger.info(f"Post-patch exit code: {post_exit}")

        # Parse per-test results
        pre_results = parse_test_results(pre_output)
        post_results = parse_test_results(post_output)
        f2p_tests = test_spec.FAIL_TO_PASS
        p2p_tests = test_spec.PASS_TO_PASS

        report = {
            instance_id: {
                "patch_successfully_applied": True,
                "pre_patch_exit_code": pre_exit,
                "post_patch_exit_code": post_exit,
                "resolved": post_exit == 0,
                "tests": classify_tests(post_results, f2p_tests, p2p_tests),
                "pre_patch_tests": classify_tests(pre_results, f2p_tests, p2p_tests),
            }
        }
        report_path.write_text(json.dumps(report, indent=4))
        return instance_id, report

    except EvaluationError as e:
        logger.info(f"EvaluationError: {e}\n{traceback.format_exc()}")
        print(f"EvaluationError {instance_id}: {e}")
    except Exception as e:
        logger.info(f"Error: {e}\n{traceback.format_exc()}")
        print(f"Error {instance_id}: {e}")
    finally:
        cleanup_container(client, container, logger)
        if rm_image:
            remove_image(client, test_spec.instance_image_key, logger)
        close_logger(logger)
    return instance_id, None


# ---------------------------------------------------------------------------
# Mode 2: Agent evaluation — apply agent patch, run eval once
# ---------------------------------------------------------------------------

def run_instance_evaluate(
    test_spec: TestSpec, pred: dict, client: docker.DockerClient,
    run_id: str, output_path: str, rm_image: bool, force_rebuild: bool,
    timeout: int | None = None,
):
    instance_id = test_spec.instance_id
    model_name = pred.get("model_name_or_path", "unknown").replace("/", "__")
    log_dir = Path(output_path) / run_id / model_name / instance_id
    log_dir.mkdir(parents=True, exist_ok=True)

    report_path = log_dir / "report.json"
    if report_path.exists():
        return instance_id, json.loads(report_path.read_text())

    log_file = log_dir / "run_instance.log"
    logger = setup_logger(instance_id, log_file)
    container = None
    try:
        container = build_setup_container(
            test_spec, client, run_id, logger, rm_image, log_dir, force_rebuild, "evaluate",
        )
        container.start()
        logger.info(f"Container started: {container.id}")

        # Apply agent's patch
        patch_ok = apply_patch(container, test_spec.patch, log_dir, logger)

        # Run eval
        test_output, exit_code = run_eval(
            container, test_spec.eval_script, log_dir, "test_output.txt", logger, timeout,
        )
        logger.info(f"Exit code: {exit_code}")

        # Parse per-test results
        test_results = parse_test_results(test_output)
        tests = classify_tests(test_results, test_spec.FAIL_TO_PASS, test_spec.PASS_TO_PASS)

        report = {
            instance_id: {
                "patch_successfully_applied": patch_ok,
                "exit_code": exit_code,
                "resolved": patch_ok and exit_code == 0,
                "tests": {k: v for k, v in tests.items() if k != "per_test"},
                "per_test": tests["per_test"],
            }
        }
        report_path.write_text(json.dumps(report, indent=4))
        return instance_id, report

    except EvaluationError as e:
        logger.info(f"EvaluationError: {e}\n{traceback.format_exc()}")
        print(f"EvaluationError {instance_id}: {e}")
    except Exception as e:
        logger.info(f"Error: {e}\n{traceback.format_exc()}")
        print(f"Error {instance_id}: {e}")
    finally:
        cleanup_container(client, container, logger)
        if rm_image:
            remove_image(client, test_spec.instance_image_key, logger)
        close_logger(logger)
    return instance_id, None


# ---------------------------------------------------------------------------
# Parallel executor
# ---------------------------------------------------------------------------

def run_instances(instances, predictions, mode, client, run_id, output_path,
                  rm_image, force_rebuild, max_workers, timeout):
    test_specs = [make_test_spec(inst, predictions[inst["instance_id"]]) for inst in instances]
    test_specs = [ts for ts in test_specs if ts is not None]

    run_fn = run_instance_benchmark if mode == "benchmark" else run_instance_evaluate

    print(f"Running {len(test_specs)} instances...")
    with tqdm(total=len(test_specs), smoothing=0) as pbar:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    run_fn, ts, predictions[ts.instance_id], client,
                    run_id, output_path, rm_image, force_rebuild, timeout,
                ): ts.instance_id
                for ts in test_specs
            }
            for future in as_completed(futures):
                pbar.update(1)
                try:
                    future.result()
                except Exception:
                    traceback.print_exc()
    print("All instances done.")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_data(path: str) -> list[dict]:
    """Load instances from a local JSON or JSONL file."""
    with open(path, "r", encoding="utf-8") as f:
        if path.endswith(".jsonl"):
            return [json.loads(line) for line in f if line.strip()]
        data = json.load(f)
        return data if isinstance(data, list) else list(data.values())


def make_gold_predictions(dataset: list[dict]) -> dict:
    """For benchmark mode: use the gold patch from each instance as the prediction."""
    return {
        inst["instance_id"]: {
            "instance_id": inst["instance_id"],
            "model_patch": inst["patch"],
            "model_name_or_path": "gold",
        }
        for inst in dataset
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def get_completed_ids(dataset, predictions, run_id, output_path) -> set:
    """Find instance IDs that already have report.json."""
    completed = set()
    for inst in dataset:
        iid = inst["instance_id"]
        if iid not in predictions:
            continue
        pred = predictions[iid]
        report = (
            Path(output_path) / run_id
            / pred["model_name_or_path"].replace("/", "__")
            / iid / "report.json"
        )
        if report.exists():
            completed.add(iid)
    return completed


def _get_test_stats_from_output(log_dir: Path, inst: dict) -> dict:
    """Parse test output files on disk to get per-test stats (fallback for old reports)."""
    # Try test_output.txt (evaluate mode) or test_output_after_apply.txt (benchmark mode)
    for name in ("test_output.txt", "test_output_after_apply.txt"):
        path = log_dir / name
        if path.exists():
            test_output = path.read_text()
            test_results = parse_test_results(test_output)
            f2p = json.loads(inst["FAIL_TO_PASS"]) if isinstance(inst["FAIL_TO_PASS"], str) else inst.get("FAIL_TO_PASS", [])
            p2p = json.loads(inst["PASS_TO_PASS"]) if isinstance(inst["PASS_TO_PASS"], str) else inst.get("PASS_TO_PASS", [])
            return classify_tests(test_results, f2p, p2p)
    return {}


def make_run_report(predictions, dataset, run_id, output_path, reports_dir, preds_dir=None):
    """Build and print a detailed summary of all instance results."""
    resolved_ids = []
    unresolved_ids = []
    error_ids = []
    instance_details = []

    # Per-test aggregates
    total_f2p = 0
    total_f2p_passed = 0
    total_p2p = 0
    total_p2p_passed = 0
    total_tests_all = 0
    total_tests_passed = 0

    # Agent stats
    total_cost = 0.0
    total_steps = 0
    instances_with_stats = 0

    for inst in dataset:
        iid = inst["instance_id"]
        if iid not in predictions:
            continue
        pred = predictions[iid]
        log_dir = (
            Path(output_path) / run_id
            / pred["model_name_or_path"].replace("/", "__")
            / iid
        )
        report_file = log_dir / "report.json"
        if not report_file.exists():
            error_ids.append(iid)
            continue

        report = json.loads(report_file.read_text())
        inst_report = report.get(iid, {})
        is_resolved = inst_report.get("resolved", False)

        if is_resolved:
            resolved_ids.append(iid)
        else:
            unresolved_ids.append(iid)

        # Per-test stats: use report if available, otherwise parse from test output files
        tests = inst_report.get("tests", {})
        if not tests.get("total_tests"):
            tests = _get_test_stats_from_output(log_dir, inst)

        total_f2p += tests.get("f2p_total", 0)
        total_f2p_passed += tests.get("f2p_passed", 0)
        total_p2p += tests.get("p2p_total", 0)
        total_p2p_passed += tests.get("p2p_passed", 0)
        total_tests_all += tests.get("total_tests", 0)
        total_tests_passed += tests.get("total_passed", 0)

        # Agent trajectory stats
        traj = load_trajectory_stats(preds_dir, iid) if preds_dir else {}
        if traj:
            total_cost += traj.get("cost", 0)
            total_steps += traj.get("steps", 0)
            instances_with_stats += 1

        instance_details.append({
            "instance_id": iid,
            "resolved": is_resolved,
            "patch_applied": inst_report.get("patch_successfully_applied", False),
            "tests_passed": f"{tests.get('total_passed', '?')}/{tests.get('total_tests', '?')}",
            "f2p_passed": f"{tests.get('f2p_passed', '?')}/{tests.get('f2p_total', '?')}",
            "p2p_passed": f"{tests.get('p2p_passed', '?')}/{tests.get('p2p_total', '?')}",
            **traj,
        })

    total = len(resolved_ids) + len(unresolved_ids) + len(error_ids)
    n_completed = len(resolved_ids) + len(unresolved_ids)

    summary = {
        "total": total,
        "completed": n_completed,
        "resolved": len(resolved_ids),
        "unresolved": len(unresolved_ids),
        "error": len(error_ids),
        "resolve_rate": f"{len(resolved_ids)/total*100:.1f}%" if total else "N/A",
        "test_pass_rate": f"{total_tests_passed}/{total_tests_all}" if total_tests_all else "N/A",
        "f2p_pass_rate": f"{total_f2p_passed}/{total_f2p}" if total_f2p else "N/A",
        "p2p_pass_rate": f"{total_p2p_passed}/{total_p2p}" if total_p2p else "N/A",
        "avg_cost": round(total_cost / instances_with_stats, 2) if instances_with_stats else "N/A",
        "avg_steps": round(total_steps / instances_with_stats, 1) if instances_with_stats else "N/A",
        "total_cost": round(total_cost, 2) if instances_with_stats else "N/A",
        "resolved_ids": sorted(resolved_ids),
        "unresolved_ids": sorted(unresolved_ids),
        "error_ids": sorted(error_ids),
        "instances": instance_details,
    }

    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_file = reports_dir / f"{run_id}.json"
    report_file.write_text(json.dumps(summary, indent=2))

    # Print summary
    print(f"\n{'='*60}")
    print(f"  Run: {run_id}")
    print(f"{'='*60}")
    print(f"  Instances:  {n_completed}/{total} completed, {len(error_ids)} errors")
    print(f"  Resolved:   {len(resolved_ids)}/{total} ({summary['resolve_rate']})")
    print(f"  Tests:      {summary['test_pass_rate']} passed")
    print(f"  F2P:        {summary['f2p_pass_rate']} passed")
    print(f"  P2P:        {summary['p2p_pass_rate']} passed")
    if instances_with_stats:
        print(f"  Avg cost:   ${summary['avg_cost']} / instance")
        print(f"  Avg steps:  {summary['avg_steps']} steps / instance")
        print(f"  Total cost: ${summary['total_cost']}")
    print(f"{'='*60}")
    print(f"  Report: {report_file}")

    return summary


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(
    dataset_path: str,
    mode: str,
    run_id: str,
    output_path: str,
    preds_dir: str | None = None,
    instance_ids: list | None = None,
    max_workers: int = 4,
    force_rebuild: bool = False,
    rm_image: bool = True,
    timeout: int = 1800,
    open_file_limit: int = 4096,
    reports_dir: str = "reports",
):
    assert run_id, "Run ID must be provided"

    # Load dataset
    full_dataset = load_data(dataset_path)
    if instance_ids:
        id_set = set(instance_ids)
        full_dataset = [i for i in full_dataset if i["instance_id"] in id_set]

    # Build predictions
    if mode == "benchmark":
        predictions = make_gold_predictions(full_dataset)
    else:
        if not preds_dir:
            raise ValueError("--preds_dir is required for evaluate mode")
        predictions_path = str(Path(preds_dir) / "preds.json")
        pred_data = load_data(predictions_path)
        predictions = {p["instance_id"]: p for p in pred_data}

    # Skip empty patches
    predictions = {k: v for k, v in predictions.items() if v.get("model_patch")}

    # Find instances that still need evaluation
    dataset = list(full_dataset)
    completed = get_completed_ids(dataset, predictions, run_id, output_path)
    if completed:
        print(f"Skipping {len(completed)} already completed instances")
        dataset = [i for i in dataset if i["instance_id"] not in completed]
    dataset = [i for i in dataset if i["instance_id"] in predictions]

    if dataset:
        resource.setrlimit(resource.RLIMIT_NOFILE, (open_file_limit, open_file_limit))
        try:
            client = docker.from_env()
        except Exception as e:
            print(f"Docker not available: {e}")
            return
        run_instances(
            dataset, predictions, mode, client, run_id, output_path,
            rm_image, force_rebuild, max_workers, timeout,
        )
    else:
        print("No instances to run.")

    # Final report (always runs — parses existing results from disk)
    make_run_report(predictions, full_dataset, run_id, output_path, reports_dir, preds_dir)


if __name__ == "__main__":
    parser = ArgumentParser(description="SWE-bench evaluation harness")
    parser.add_argument("--dataset_path", required=True, help="Path to dataset JSON/JSONL file")
    parser.add_argument("--mode", required=True, choices=["benchmark", "evaluate"],
                        help="benchmark: validate test cases with gold patch; evaluate: test agent patches")
    parser.add_argument("--run_id", required=True, help="Unique run identifier")
    parser.add_argument("--output_path", required=True, help="Output directory for logs and reports")
    parser.add_argument("--preds_dir", help="Agent results directory containing preds.json and per-instance trajectories (required for evaluate mode)")
    parser.add_argument("--instance_ids", nargs="+", help="Only run these instance IDs")
    parser.add_argument("--max_workers", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--force_rebuild", action="store_true", default=False)
    parser.add_argument("--rm_image", action="store_true", default=True)
    parser.add_argument("--open_file_limit", type=int, default=4096)
    parser.add_argument("--reports_dir", default="reports")
    args = parser.parse_args()
    main(**vars(args))
