#!/usr/bin/env python3
"""Parallel runner for agent_root.TransferAgent."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

import sys

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from build_image import TransferAgent


def _read_text_or_warn(path: Path, logger: logging.Logger) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("missing artifact: %s", path)
        return ""
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.error("error reading %s: %s", path, exc)
        return ""


def _embed_artifacts(
    entries: list[dict[str, Any]],
    output_root: Path,
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    embedded: list[dict[str, Any]] = []
    for entry in entries:
        inst_id = entry.get("instance_id")
        if not inst_id:
            logger.warning("entry missing instance_id, skip embedding")
            embedded.append(dict(entry))
            continue

        inst_dir = output_root / inst_id
        run_tests_path = inst_dir / "run_tests.sh"
        eval_path = inst_dir / "eval_script.sh"
        dockerfile_path = inst_dir / "Dockerfile"

        script_source = run_tests_path if run_tests_path.exists() else eval_path

        embedded_entry = dict(entry)
        embedded_entry["eval_script"] = _read_text_or_warn(script_source, logger)
        embedded_entry["dockerfile"] = _read_text_or_warn(dockerfile_path, logger)
        embedded_entry.setdefault("docker_image", embedded_entry.get("docker_name"))
        embedded.append(embedded_entry)

    return embedded


def _collect_status(
    instance_ids: list[str],
    output_root: Path,
    logger: logging.Logger,
) -> dict[str, dict[str, Any]]:
    status_map: dict[str, dict[str, Any]] = {}
    for inst_id in instance_ids:
        status_path = output_root / inst_id / "status.json"
        status = "missing"
        reason = None
        if status_path.exists():
            try:
                data = json.loads(status_path.read_text(encoding="utf-8"))
                status = data.get("status", "unknown") or "unknown"
                reason = data.get("reason")
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.warning("unable to parse %s: %s", status_path, exc)
        status_map[inst_id] = {"status": status, "reason": reason}
    return status_map


def load_instances(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError("input file must contain a list")
    return data


def run_instance(
    instance: dict[str, Any],
    output_dir: Path,
    max_iterations: int,
    eval_timeout: int,
) -> dict[str, Any]:
    inst_id = instance.get("instance_id")
    if not inst_id:
        raise ValueError("each instance must have an 'instance_id'")

    inst_output = output_dir / inst_id
    if inst_output.exists():
        shutil.rmtree(inst_output)
    inst_output.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(inst_id)
    handler = logging.FileHandler(inst_output / "instance.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    try:
        agent = TransferAgent(
            task_dict=instance,
            max_iteration_num=max_iterations,
            output_path=str(inst_output),
            eval_timeout=eval_timeout,
        )
        logger.info("starting instance %s", inst_id)
        ok = agent.run_task()
        if ok:
            logger.info("instance %s succeeded", inst_id)
            return {"instance_id": inst_id, "success": True, "cost": agent.cost}
        logger.error("instance %s failed: run_task returned False", inst_id)
        return {"instance_id": inst_id, "success": False, "reason": "run_task returned False", "cost": agent.cost}
    except Exception as exc:  # noqa: BLE001
        logger.exception("instance %s raised exception", inst_id)
        return {"instance_id": inst_id, "success": False, "reason": str(exc)}
    finally:
        logger.removeHandler(handler)
        handler.close()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run agent_v2 TransferAgent in parallel")
    parser.add_argument("--input", required=True, help="path to JSON list of instances")
    parser.add_argument("--output", required=True, help="directory to store run artifacts")
    parser.add_argument("--max-iterations", type=int, default=5, help="maximum iterations per instance")
    parser.add_argument("--eval-timeout", type=int, default=300, help="eval script timeout (seconds)")
    parser.add_argument("--max-workers", type=int, default=2, help="parallel workers")
    parser.add_argument("--skip-existing", action="store_true", help="skip instances with summary.json already present")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    input_path = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    instances = load_instances(input_path)
    pending: list[dict[str, Any]] = []
    for inst in instances:
        inst_dir = output_dir / inst.get("instance_id", "")
        if args.skip_existing and (inst_dir / "summary.json").exists():
            logging.info("skipping %s: summary.json present", inst.get("instance_id"))
            continue
        pending.append(inst)

    results: list[dict[str, Any]] = []
    if not pending:
        logging.info("no instances to process; consolidating existing outputs")
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_workers) as pool:
            future_map = {
                pool.submit(
                    run_instance,
                    inst,
                    output_dir,
                    args.max_iterations,
                    args.eval_timeout,
                ): inst["instance_id"]
                for inst in pending
            }
            for future in concurrent.futures.as_completed(future_map):
                inst_id = future_map[future]
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001
                    logging.exception("instance %s raised during future", inst_id)
                    result = {"instance_id": inst_id, "success": False, "reason": str(exc)}
                results.append(result)
                logging.info(
                    "instance %s completed: %s",
                    inst_id,
                    "success" if result.get("success") else "failed",
                )

    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    logging.info("wrote summary to %s", summary_path)

    # Consolidate latest artifacts into transferred/failed datasets
    logger = logging.getLogger("main-consolidate")
    status_map = _collect_status([inst.get("instance_id", "") for inst in instances], output_dir, logger)

    def _is_success(inst_id: str) -> bool:
        return status_map.get(inst_id, {}).get("status") == "success"

    success_entries = [inst for inst in instances if _is_success(inst.get("instance_id", ""))]
    failed_entries = [inst for inst in instances if not _is_success(inst.get("instance_id", ""))]

    success_embedded = _embed_artifacts(success_entries, output_dir, logger)
    for entry in success_embedded:
        if entry.get("docker_image") is None and entry.get("instance_id"):
            entry["docker_image"] = f"{entry['instance_id'].lower()}_swefactory_root"

    results_by_id = {item.get("instance_id"): item for item in results}
    failed_with_reason: list[dict[str, Any]] = []
    for inst in failed_entries:
        inst_id = inst.get("instance_id")
        entry = dict(inst)
        reason = status_map.get(inst_id, {}).get("reason")
        if reason is None and inst_id in results_by_id:
            reason = results_by_id[inst_id].get("reason")
        entry["failure_reason"] = reason
        entry.setdefault(
            "docker_image",
            entry.get("docker_name") or (inst_id.lower() + "_swefactory_root" if inst_id else None),
        )
        failed_with_reason.append(entry)

    input_basename = input_path.stem
    transferred_path = output_dir / f"{input_basename}_transferred.json"
    failed_path = output_dir / f"{input_basename}_failed.json"

    transferred_path.write_text(
        json.dumps(success_embedded, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logging.info(
        "wrote %d successful entries with updated artifacts to %s",
        len(success_embedded),
        transferred_path,
    )

    failed_path.write_text(
        json.dumps(failed_with_reason, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logging.info(
        "wrote %d failed entries (original content) to %s",
        len(failed_with_reason),
        failed_path,
    )

    total_effective = len(success_entries) + len(failed_entries)
    succeeded_count = len(success_entries)
    failed_count = len(failed_entries)
    success_rate = succeeded_count / total_effective if total_effective else 0.0

    summary_main = {
        "total": total_effective,
        "succeeded": succeeded_count,
        "failed": failed_count,
        "success_rate": success_rate,
        "failed_instances": [
            {
                "instance_id": entry.get("instance_id"),
                "reason": entry.get("failure_reason"),
            }
            for entry in failed_with_reason
        ],
    }
    summary_main_path = output_dir / "summary_main.json"
    summary_main_path.write_text(
        json.dumps(summary_main, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logging.info("wrote summary_main.json to %s", summary_main_path)

    history_path = output_dir / "summary_history.jsonl"
    history_entry = dict(summary_main)
    history_entry["input_file"] = str(input_path)
    with history_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(history_entry, ensure_ascii=False) + "\n")
    logging.info("appended summary entry to %s", history_path)


if __name__ == "__main__":  # pragma: no cover
    main()
