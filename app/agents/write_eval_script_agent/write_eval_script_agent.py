from app.data_structures import MessageThread
from app.agents.write_eval_script_agent import write_eval_script_utils
from app.agents.agent import Agent
from app.task import SweTask
import os
import re
import shutil
from app.log import print_banner
from os.path import join as pjoin
from loguru import logger

DIFF_MODIFIED_FILE_REGEX = r"--- a/(.*)"
DIFF_NEW_FILE_REGEX = r"\+\+\+ b/(.*)"


class WriteEvalScriptAgent(Agent):
    """
    Agent responsible for generating or modifying an evaluation script (`eval.sh`).
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
        self.dockerfile: str | None = None
        self.pending_guidance: str | None = None
        self.init_msg_thread()

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

    def init_msg_thread(self) -> None:
        self.msg_thread = MessageThread()
        self.add_system_message(write_eval_script_utils.get_system_prompt_eval_script())
        self.add_user_message(self.repo_basic_info)

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

    def run_task(self, print_callback=None) -> tuple[str, str, bool]:
        """
        Generate or modify the evaluation script. Returns raw_output, summary, success.
        """
        if self.run_count > 0:
            self.init_msg_thread()
            if self.pending_guidance:
                self.add_user_message(self.pending_guidance)
                self.pending_guidance = None

        print_banner(f"Task {self.task.task_id} Iteration ROUND {self.iteration_num}: Eval Script Generation")

        prev_dir = self.get_latest_write_output_dir()
        self.run_count += 1
        curr_dir = self.get_latest_write_output_dir()
        os.makedirs(curr_dir, exist_ok=True)

        prev_script = os.path.join(prev_dir, "eval.sh")
        dockerfile_msg = f"The dockerfile environment you are running tests on:\n{self.dockerfile}\n\n"

        if os.path.exists(prev_script):
            self.add_user_message(dockerfile_msg)
            msg_prev = (
                f"Previous generated eval script skeleton:\n"
                f"{self.get_latest_eval_script_skeleton()}\n\n"
                f"Test patch that MUST be applied (do NOT modify this content):\n"
                f"{self.test_patch}\n\n"
            )
            self.add_user_message(msg_prev)
            self.add_user_message(
                "Please modify current eval script according to collected information. "
                "You MUST keep the `cat` heredoc blocks that write test files. "
                "Do NOT rewrite the test file writing mechanism. "
                "Return modified eval script in defined format. Wrap results in <script></script>."
            )
        else:
            self.add_user_message(dockerfile_msg)
            self.add_user_message(write_eval_script_utils.get_user_prompt_init_eval_script(self.initial_skeleton))

        task_output = write_eval_script_utils.write_eval_script_with_retries(
            self.msg_thread,
            curr_dir,
            self.test_patch,
            test_files_content=self.test_files_content,
            repo_root=self.task.project_path,
            retries=3,
            print_callback=print_callback,
        )

        script_path = os.path.join(curr_dir, "eval.sh")
        ok = os.path.isfile(script_path)
        if not ok and os.path.exists(prev_script):
            shutil.copy(prev_script, script_path)
            ok = False

        summary = (
            "Evaluation script created/updated successfully." if ok
            else "Evaluation script generation failed."
        )
        conversation_file = pjoin(curr_dir, "conversation.json")
        self.msg_thread.save_to_file(conversation_file)
        return task_output, summary, ok
