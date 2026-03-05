#!/usr/bin/env python3
"""
Manual annotation CLI tool for SWE-bench pipeline outputs.

Usage:
    python scripts/annotate.py <applicable_setup_dir> [--filter STATUS] [--output FILE]

Examples:
    python3 scripts/annotate.py internal-swe-bench-data/MiroMindAI__sd-torchtune/setup_output_2026-03-04/applicable_setup/
    python3 scripts/annotate.py internal-swe-bench-data/MiroMindAI__miroflow/setup_output_2026-03-03/applicable_setup/

    python scripts/annotate.py ... --filter FAIL2FAIL
    python scripts/annotate.py ... --output /path/to/annotations.jsonl
"""

import argparse
import ast
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


# ─── helpers ─────────────────────────────────────────────────────────────────

def natural_sort_key(path: Path) -> list:
    """Sort path by embedded integers for natural ordering."""
    parts = re.split(r'(\d+)', path.name)
    return [int(p) if p.isdigit() else p for p in parts]


def get_final_test_source(instance_dir: Path) -> Path | None:
    """Return the directory containing the final test files.

    Priority: highest-numbered post_fix_round_N > highest-numbered write_test_agent_N.
    """
    post_fix_dirs = sorted(
        instance_dir.glob("post_fix_round_*"), key=natural_sort_key
    )
    if post_fix_dirs:
        return post_fix_dirs[-1] / "tests"

    agent_dirs = sorted(
        instance_dir.glob("write_test_agent_*"), key=natural_sort_key
    )
    if agent_dirs:
        return agent_dirs[-1] / "tests"
    return None


def get_final_test_output(instance_dir: Path) -> tuple[Path | None, Path | None]:
    """Return (pre_patch_output, post_patch_output) paths for the final run.

    Priority: highest-numbered post_fix_round_N > highest-numbered test_analysis_agent_N.
    """
    post_fix_dirs = sorted(
        instance_dir.glob("post_fix_round_*"), key=natural_sort_key
    )
    if post_fix_dirs:
        d = post_fix_dirs[-1]
        return d / "test_output_prev_apply.txt", d / "test_output.txt"

    analysis_dirs = sorted(
        instance_dir.glob("test_analysis_agent_*"), key=natural_sort_key
    )
    if analysis_dirs:
        d = analysis_dirs[-1]
        return d / "test_output_prev_apply.txt", d / "test_output.txt"

    return None, None


def read_text(path: Path | None, last_n_lines: int | None = None) -> str:
    """Safely read a text file, optionally returning only the last N lines."""
    if path is None or not path.exists():
        return "(not found)"
    try:
        text = path.read_text(errors="replace")
        if last_n_lines is not None:
            lines = text.splitlines()
            text = "\n".join(lines[-last_n_lines:])
        return text
    except Exception as e:
        return f"(read error: {e})"


def read_json(path: Path) -> dict:
    """Safely read a JSON file."""
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def extract_test_function_names(test_dir: Path) -> dict[str, list[str]]:
    """Extract F2P and P2P test function names from test files using AST."""
    result: dict[str, list[str]] = {}
    if test_dir is None or not test_dir.exists():
        return result

    for py_file in sorted(test_dir.rglob("*.py")):
        try:
            tree = ast.parse(py_file.read_text())
        except SyntaxError:
            continue
        funcs = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name.startswith("test_")
        ]
        if funcs:
            rel = str(py_file.relative_to(test_dir.parent))
            result[rel] = funcs
    return result


def colorize(text: str, code: str) -> str:
    """Wrap text with ANSI color code (if stdout is a tty)."""
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def c_green(t: str)  -> str: return colorize(t, "32")
def c_red(t: str)    -> str: return colorize(t, "31")
def c_yellow(t: str) -> str: return colorize(t, "33")
def c_cyan(t: str)   -> str: return colorize(t, "36")
def c_magenta(t: str)-> str: return colorize(t, "35")
def c_bold(t: str)   -> str: return colorize(t, "1")
def c_dim(t: str)    -> str: return colorize(t, "2")


def classification_badge(cls: str) -> str:
    color_fn = {
        "FAIL2PASS": c_green,
        "FAIL2FAIL": c_red,
        "PASS2PASS": c_yellow,
        "PASS2FAIL": c_magenta,
    }.get(cls, lambda t: t)
    return color_fn(f"[{cls}]")


def section(title: str, width: int = 56) -> str:
    bar_len = max(0, width - len(title) - 4)
    return f"\n{c_cyan('──')} {c_bold(title)} {c_dim('─' * bar_len)}"


def box(text: str, cls: str = "", width: int = 58) -> str:
    top    = c_bold("╔" + "═" * (width - 2) + "╗")
    bottom = c_bold("╚" + "═" * (width - 2) + "╝")
    # Replace plain [CLS] in text with colored badge
    if cls:
        text = text.replace(f"[{cls}]", classification_badge(cls))
    inner = c_bold("║") + f"  {text:<{width - 4}}" + c_bold("║")
    return f"{top}\n{inner}\n{bottom}"


def colorize_diff_line(line: str) -> str:
    if line.startswith("diff --git") or line.startswith("index "):
        return c_bold(line)
    if line.startswith("@@"):
        return c_cyan(line)
    if line.startswith("+++") or line.startswith("---"):
        return c_bold(line)
    if line.startswith("+"):
        return c_green(line)
    if line.startswith("-"):
        return c_red(line)
    return line


def colorize_test_output(text: str) -> str:
    """Highlight PASSED/FAILED/ERROR lines in test output."""
    lines = text.splitlines()
    out = []
    for line in lines:
        if re.search(r'\bFAILED\b', line):
            out.append(c_red(line))
        elif re.search(r'\bPASSED\b', line):
            out.append(c_green(line))
        elif re.search(r'\bERROR\b', line):
            out.append(c_red(line))
        elif re.match(r'=+\s+\d+ (failed|passed)', line):
            out.append(c_bold(line))
        else:
            out.append(line)
    return "\n".join(out)


# ─── display ─────────────────────────────────────────────────────────────────

def display_instance(instance_dir: Path, index: int, total: int, meta: dict, status: dict) -> None:
    # Clear terminal before showing new instance
    print("\033[2J\033[H", end="", flush=True)

    instance_id = instance_dir.name
    cls = status.get("f2p_classification", "UNKNOWN")
    task_info = meta.get("task_info", {})

    header_text = f"  {instance_id}  [{index}/{total}]  [{cls}]"
    print(box(header_text, cls=cls))

    pull_url = task_info.get("pull_url", "")
    if pull_url:
        print(f"  {c_cyan(pull_url)}")

    # ISSUE
    print(section("ISSUE"))
    problem = task_info.get("problem_statement", "(no problem statement)")
    lines = problem.strip().splitlines()
    print("\n".join(lines[:10]))
    if len(lines) > 10:
        print(c_dim(f"... ({len(lines) - 10} more lines, press [d] to see full patch)"))

    # PATCH summary
    patch = task_info.get("patch", "")
    patch_file_count = len(re.findall(r'^diff --git', patch, re.MULTILINE))
    print(section(f"PATCH ({patch_file_count} file(s) changed)"))
    condensed = []
    for line in patch.splitlines():
        if line.startswith(("diff --git", "---", "+++", "@@", "+", "-")):
            condensed.append(colorize_diff_line(line))
    if len(condensed) > 40:
        print("\n".join(condensed[:40]))
        print(c_dim(f"... ({len(condensed) - 40} more diff lines)"))
    else:
        print("\n".join(condensed))

    # TEST FILES
    test_dir = get_final_test_source(instance_dir)
    if test_dir and test_dir.exists():
        for py_file in sorted(test_dir.rglob("*.py")):
            rel = str(py_file.relative_to(test_dir.parent))
            print(section(f"TEST FILE ({rel})"))
            content = py_file.read_text(errors="replace")
            lines = content.splitlines()
            if len(lines) > 60:
                print("\n".join(lines[:60]))
                print(c_dim(f"... ({len(lines) - 60} more lines)"))
            else:
                print(content)
    else:
        print(section("TEST FILES"))
        print(c_dim("(no test files found)"))

    # PRE-PATCH output
    pre_path, post_path = get_final_test_output(instance_dir)
    print(section("PRE-PATCH OUTPUT (last 20 lines)"))
    print(colorize_test_output(read_text(pre_path, last_n_lines=20)))

    # POST-PATCH output
    print(section("POST-PATCH OUTPUT (last 20 lines)"))
    print(colorize_test_output(read_text(post_path, last_n_lines=20)))

    print("\n" + c_dim("─" * 56))


def display_full_patch(meta: dict) -> None:
    patch = meta.get("task_info", {}).get("patch", "(no patch)")
    print(section("FULL PATCH"))
    for line in patch.splitlines():
        print(colorize_diff_line(line))
    print(c_dim("─" * 56))


def display_full_tests(instance_dir: Path) -> None:
    test_dir = get_final_test_source(instance_dir)
    if not test_dir or not test_dir.exists():
        print(c_dim("(no test files found)"))
        return
    for py_file in sorted(test_dir.rglob("*.py")):
        rel = str(py_file.relative_to(test_dir.parent))
        print(section(f"FULL TEST FILE ({rel})"))
        print(py_file.read_text(errors="replace"))
    print(c_dim("─" * 56))


# ─── annotation collection ───────────────────────────────────────────────────

REASON_MAP = {
    "e": "environment",
    "t": "test-logic",
    "g": "gold-patch",
    "w": "wrong-behavior",
    "q": "quality-low",
    "o": "other",
}

PRIORITY_MAP = {
    "h": "high",
    "m": "medium",
    "l": "low",
    "x": "delete",
}


def _key(k: str) -> str:
    return c_bold(c_cyan(f"[{k}]"))


def prompt_verdict() -> str | None:
    """Return 'accept', 'reject', 'skip', or None for quit."""
    while True:
        print(
            f"\n{_key('a')} accept   {_key('r')} reject   "
            f"{_key('s')} skip   {_key('q')} quit"
        )
        print(f"{_key('p')} prev     {_key('d')} show full patch   {_key('t')} show full tests")
        key = input("> ").strip().lower()
        if key in ("a", "accept"):
            return "accept"
        if key in ("r", "reject"):
            return "reject"
        if key in ("s", "skip"):
            return "skip"
        if key in ("q", "quit"):
            return None
        if key in ("p", "prev"):
            return "prev"
        if key in ("d",):
            return "full_patch"
        if key in ("t",):
            return "full_tests"
        print(c_yellow("Unknown key. Use a/r/s/q/p/d/t."))


def prompt_reason() -> str:
    print(f"\n{c_bold('Reason for rejection:')}")
    print(f"  {_key('e')} environment/setup broken    {_key('t')} test logic error")
    print(f"  {_key('g')} gold patch issue            {_key('w')} tests wrong behavior")
    print(f"  {_key('q')} test quality too low        {_key('o')} other")
    while True:
        key = input("> ").strip().lower()
        if key in REASON_MAP:
            return REASON_MAP[key]
        print(c_yellow("Unknown key. Use e/t/g/w/q/o."))


def prompt_priority() -> str:
    print(f"\n{c_bold('Fix priority:')}")
    print(
        f"  {_key('h')} high   {_key('m')} medium   "
        f"{_key('l')} low   {_key('x')} delete (not worth fixing)"
    )
    while True:
        key = input("> ").strip().lower()
        if key in PRIORITY_MAP:
            return PRIORITY_MAP[key]
        print(c_yellow("Unknown key. Use h/m/l/x."))


def prompt_note() -> str:
    note = input("\nNote (press Enter to skip): ").strip()
    return note


def collect_annotation(instance_id: str, run_dir: str, cls: str) -> dict | None:
    """Interactive annotation collection. Returns annotation dict or None if quit."""
    verdict = None
    while verdict in (None, "full_patch", "full_tests"):
        verdict = prompt_verdict()
        if verdict in ("full_patch", "full_tests"):
            return {"_action": verdict}
        if verdict == "prev":
            return {"_action": "prev"}
        if verdict is None:
            return None

    failure_reason = None
    fix_priority = None
    if verdict == "reject":
        failure_reason = prompt_reason()
        fix_priority = prompt_priority()

    note = prompt_note()

    include_in_benchmark = verdict == "accept"
    mark_for_deletion = verdict == "reject" and fix_priority == "delete"

    return {
        "instance_id": instance_id,
        "run_dir": run_dir,
        "auto_classification": cls,
        "verdict": verdict,
        "failure_reason": failure_reason,
        "fix_priority": fix_priority,
        "include_in_benchmark": include_in_benchmark,
        "mark_for_deletion": mark_for_deletion,
        "note": note,
        "annotated_at": datetime.now(timezone.utc).isoformat(),
    }


# ─── persistence ─────────────────────────────────────────────────────────────

def load_existing_annotations(output_path: Path) -> set[str]:
    """Return set of already-annotated instance_ids."""
    if not output_path.exists():
        return set()
    annotated = set()
    for line in output_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            annotated.add(rec["instance_id"])
        except Exception:
            pass
    return annotated


def append_annotation(output_path: Path, annotation: dict) -> None:
    with output_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(annotation, ensure_ascii=False) + "\n")


# ─── summary ─────────────────────────────────────────────────────────────────

def print_summary(session_annotations: list[dict], output_path: Path) -> None:
    total = len(session_annotations)
    if total == 0:
        print("\nNo annotations made this session.")
        return

    accepted = [a for a in session_annotations if a.get("verdict") == "accept"]
    rejected = [a for a in session_annotations if a.get("verdict") == "reject"]
    high = [a for a in rejected if a.get("fix_priority") == "high"]
    medium = [a for a in rejected if a.get("fix_priority") == "medium"]
    low = [a for a in rejected if a.get("fix_priority") == "low"]
    deleted = [a for a in rejected if a.get("mark_for_deletion")]

    width = 56
    divider = c_bold("═" * width)
    print("\n" + divider)
    print(c_bold(" Annotation Summary ".center(width, "═")))
    print(divider)
    print(f"Total annotated this session: {c_bold(str(total))}")
    print(f"  {c_green('accept')}:  {len(accepted)}  ({100 * len(accepted) // total}%)")
    print(f"  {c_red('reject')}:  {len(rejected)}  ({100 * len(rejected) // total}%)")
    if rejected:
        print(f"    → {c_red('high')} priority fix:   {len(high)}")
        print(f"    → {c_yellow('medium')} priority fix: {len(medium)}")
        print(f"    → low priority fix:    {len(low)}")
        print(f"    → mark for deletion:   {c_red(str(len(deleted)))}")
    print(f"Saved to: {c_cyan(str(output_path))}")
    print(divider)


# ─── main ────────────────────────────────────────────────────────────────────

def collect_instances(setup_dir: Path, filter_cls: str | None) -> list[Path]:
    """Collect and optionally filter instance directories."""
    dirs = sorted(
        [d for d in setup_dir.iterdir() if d.is_dir()],
        key=natural_sort_key,
    )
    if filter_cls:
        filtered = []
        for d in dirs:
            status = read_json(d / "status.json")
            if status.get("f2p_classification") == filter_cls:
                filtered.append(d)
        return filtered
    return dirs


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manual annotation CLI for SWE-bench pipeline outputs."
    )
    parser.add_argument("setup_dir", help="Path to applicable_setup/ directory")
    parser.add_argument(
        "--filter",
        metavar="STATUS",
        help="Only show instances with this classification (e.g. FAIL2FAIL)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output JSONL file (default: <project_root>/annotations.jsonl)",
    )
    args = parser.parse_args()

    setup_dir = Path(args.setup_dir).resolve()
    if not setup_dir.is_dir():
        print(f"Error: '{setup_dir}' is not a directory.", file=sys.stderr)
        sys.exit(1)

    # Determine output path
    if args.output:
        output_path = Path(args.output).resolve()
    else:
        # Walk up from script location to project root
        project_root = Path(__file__).resolve().parent.parent
        output_path = project_root / "annotations.jsonl"

    output_path.parent.mkdir(parents=True, exist_ok=True)

    already_annotated = load_existing_annotations(output_path)

    instances = collect_instances(setup_dir, args.filter)
    total = len(instances)

    if total == 0:
        print("No instances found" + (f" matching --filter {args.filter}" if args.filter else "") + ".")
        sys.exit(0)

    print(f"Found {c_bold(str(total))} instance(s). Already annotated: {len(already_annotated)}.")
    print(f"Output: {c_cyan(str(output_path))}")
    print(c_dim("Press Ctrl-C at any time to stop and see summary.\n"))

    session_annotations: list[dict] = []
    idx = 0  # current position in instances list

    try:
        while idx < len(instances):
            instance_dir = instances[idx]
            instance_id = instance_dir.name

            if instance_id in already_annotated:
                print(f"[{idx + 1}/{total}] Skipping {instance_id} (already annotated)")
                idx += 1
                continue

            meta = read_json(instance_dir / "meta.json")
            status = read_json(instance_dir / "status.json")
            cls = status.get("f2p_classification", "UNKNOWN")

            display_instance(instance_dir, idx + 1, total, meta, status)

            # Annotation loop for this instance (allows "show full patch" re-display)
            while True:
                annotation = collect_annotation(
                    instance_id=instance_id,
                    run_dir=str(setup_dir),
                    cls=cls,
                )

                if annotation is None:
                    # Quit
                    print_summary(session_annotations, output_path)
                    sys.exit(0)

                action = annotation.get("_action")
                if action == "full_patch":
                    display_full_patch(meta)
                    continue
                if action == "full_tests":
                    display_full_tests(instance_dir)
                    continue
                if action == "prev":
                    if idx > 0:
                        idx -= 1
                    else:
                        print("Already at the first instance.")
                    break  # re-enter outer loop at new idx

                if annotation.get("verdict") == "skip":
                    idx += 1
                    break

                # Record annotation
                append_annotation(output_path, annotation)
                session_annotations.append(annotation)
                already_annotated.add(instance_id)
                v = annotation["verdict"].upper()
                label = c_green(v) if v == "ACCEPT" else c_red(v)
                print(f"  {c_bold('Saved:')} {label}")
                idx += 1
                break

    except KeyboardInterrupt:
        print("\nInterrupted.")

    print_summary(session_annotations, output_path)


if __name__ == "__main__":
    main()
