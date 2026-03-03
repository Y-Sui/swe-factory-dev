from app.agents.write_eval_script_agent import write_eval_script_utils
from app.agents.agent import Agent
from app.task import SweTask
import os
import re
from app.log import print_banner
from loguru import logger

DIFF_MODIFIED_FILE_REGEX = r"--- a/(.*)"
DIFF_NEW_FILE_REGEX = r"\+\+\+ b/(.*)"

# Repo-specific workdir and pytest command. {files} is replaced with space-separated test paths.
# Note: --override-ini="addopts=" is omitted — _ensure_pytest_addopts_override() adds it in post-processing.
_REPO_EVAL_CONFIG = {
    "MiroMindAI/miroflow": {
        "workdir": "/testbed",
        "pytest_cmd": ".venv/bin/pytest {files} -xvs",
    },
    "MiroMindAI/MiroThinker": {
        "workdir": "/testbed/apps/miroflow-agent",
        "pytest_cmd": ".venv/bin/pytest {files} -xvs",
    },
    "MiroMindAI/sd-torchtune": {
        "workdir": "/testbed",
        "pytest_cmd": "pytest {files} -xvs --without-integration",
    },
}
_DEFAULT_EVAL_CONFIG = _REPO_EVAL_CONFIG["MiroMindAI/miroflow"]


class WriteEvalScriptAgent(Agent):
    """
    Agent responsible for generating an evaluation script (`eval.sh`) deterministically
    from a repo-specific template, without LLM calls.
    """
    api_functions: list[str] = []

    def __init__(self, task: SweTask, output_dir: str, repo_basic_info: str):
        super().__init__(agent_id="WriteEvalScriptAgent")
        self.task = task
        self.output_dir = os.path.abspath(output_dir)
        self.test_patch = self.task.test_patch
        self.test_files = self.get_test_files()
        self.generated_test_files = []
        self.test_files_content: dict[str, str] = {}
        self.initial_skeleton = self.get_initial_eval_script_skeleton()
        self.run_count = 0
        self.repo_basic_info = repo_basic_info
        # Keep these attributes so agents_manager.py can set them without errors.
        self.dockerfile: str | None = None
        self.pending_guidance: str | None = None

    def get_test_files(self):
        patch = self.test_patch or ""
        # Match modified files (--- a/path) — excludes /dev/null for new files
        modified = [p.split("\t")[0] for p in re.findall(DIFF_MODIFIED_FILE_REGEX, patch)
                    if not p.startswith("/dev/null")]
        # Match new files (+++ b/path) — this captures files added from /dev/null
        new_files = [p.split("\t")[0] for p in re.findall(DIFF_NEW_FILE_REGEX, patch)
                     if not p.startswith("/dev/null")]
        # Deduplicate while preserving order (modified files appear in both --- and +++)
        return list(dict.fromkeys(modified + new_files))

    def get_latest_write_output_dir(self) -> str:
        return os.path.join(self.output_dir, f"write_eval_script_agent_{self.run_count}")

    def get_initial_eval_script_skeleton(self):
        test_files = list(dict.fromkeys(list(self.test_files_content.keys()) + self.test_files))

        # Collect directories needed for all test files
        all_dirs = sorted({os.path.dirname(f) for f in test_files if os.path.dirname(f)})
        quoted_dirs = ['"' + d + '"' for d in all_dirs]

        eval_commands = ["cd /testbed"]

        if quoted_dirs:
            eval_commands.append("mkdir -p " + " ".join(quoted_dirs))

        # Write test files via cat heredocs (content injected by post-processor)
        if self.test_patch and self.test_patch.strip():
            for i, f in enumerate(test_files):
                delim = f"EOF_TEST_{i}"
                eval_commands.append(f"cat <<'{delim}' > \"{f}\"")
                eval_commands.append("[TEST FILE CONTENT]")
                eval_commands.append(delim)

        return "\n".join(["#!/bin/bash", "set -uxo pipefail"] + eval_commands) + "\n"

    def get_latest_eval_script_skeleton(self) -> str:
        skel_path = os.path.join(self.get_latest_write_output_dir(), "eval_skeleton.sh")
        try:
            with open(skel_path, "r") as f:
                return f.read()
        except Exception:
            return self.initial_skeleton

    def get_latest_eval_script(self) -> str | None:
        try:
            path = os.path.join(self.get_latest_write_output_dir(), "eval.sh")
            with open(path, "r") as f:
                return f.read()
        except Exception as e:
            logger.error(e)
            return None

    def _build_eval_script(self) -> str:
        config = _REPO_EVAL_CONFIG.get(self.task.repo, _DEFAULT_EVAL_CONFIG)
        workdir = config["workdir"]

        test_files = list(dict.fromkeys(list(self.test_files_content.keys()) + self.test_files))
        file_args = " ".join(f'"{f}"' for f in test_files) if test_files else "tests/"
        pytest_cmd = config["pytest_cmd"].format(files=file_args)

        lines = ["#!/bin/bash", "set -uxo pipefail"]

        all_dirs = sorted({os.path.dirname(f) for f in test_files if os.path.dirname(f)})
        if all_dirs:
            lines.append("mkdir -p " + " ".join(f'"{d}"' for d in all_dirs))

        if self.test_patch and self.test_patch.strip():
            for i, f in enumerate(test_files):
                delim = f"EOF_TEST_{i}"
                lines += [f"cat <<'{delim}' > \"{f}\"", "[TEST FILE CONTENT]", delim]

        lines += [f"cd {workdir}", pytest_cmd]
        return "\n".join(lines) + "\n"

    def run_task(self, print_callback=None) -> tuple[str, str, bool]:
        print_banner(f"Task {self.task.task_id} Iteration ROUND {self.iteration_num}: Eval Script Generation")

        self.run_count += 1
        curr_dir = self.get_latest_write_output_dir()
        os.makedirs(curr_dir, exist_ok=True)

        content = self._build_eval_script()
        write_eval_script_utils.write_eval_script_from_content(
            content, curr_dir, self.test_patch,
            test_files_content=self.test_files_content,
            repo_root=self.task.project_path,
        )

        ok = os.path.isfile(os.path.join(curr_dir, "eval.sh"))
        summary = "Evaluation script created." if ok else "Evaluation script generation failed."
        return "", summary, ok
