from app.data_structures import MessageThread
from app.agents.write_test_agent import write_test_utils
from app.agents.agent import Agent
from app.task import SweTask
import os
from app.log import (
    print_acr,
    print_banner,
)
from os.path import join as pjoin
from loguru import logger


class WriteTestAgent(Agent):
    """
    Agent responsible for generating test files when test_patch is empty or insufficient.

    Generates two categories of tests:
      - Fail-to-Pass (F2P): tests that fail before the gold patch and pass after.
      - Pass-to-Pass (P2P): regression tests that pass both before and after.

    Uses multi-round reflexion (self-critique + refinement) to improve test quality.
    Supports multiple languages via task.language field.
    """
    api_functions: list[str] = []

    def __init__(
        self,
        task: SweTask,
        output_dir: str,
        repo_basic_info: str,
        max_reflexion_rounds: int = 2,
    ):
        super().__init__(agent_id="WriteTestAgent")
        self.task = task
        self.output_dir = os.path.abspath(output_dir)
        self.repo_basic_info = repo_basic_info
        self.max_reflexion_rounds = max_reflexion_rounds
        self.run_count = 0
        self.generated_test_patch: str | None = None
        self.generated_test_files = []
        # Select language-specific system prompt
        self._language = getattr(task, "language", "python") or "python"
        self.pending_guidance: str | None = None
        self.init_msg_thread()

    def init_msg_thread(self) -> None:
        self.msg_thread = MessageThread()
        # Use language-aware system prompt for proper test framework guidance
        system_prompt = write_test_utils.get_test_system_prompt(self._language)
        self.add_system_message(system_prompt)
        self.add_user_message(self.repo_basic_info)

    def get_latest_write_output_dir(self) -> str:
        return os.path.join(self.output_dir, f"write_test_agent_{self.run_count}")

    def run_task(
        self,
        print_callback=None
    ) -> tuple[str, str, bool]:
        """
        Generate test files based on problem_statement and patch.
        After initial generation, runs reflexion rounds to improve F2P/P2P quality.
        Returns raw_output, summary, success.
        """
        # Reset thread on subsequent runs to prevent unbounded growth
        if self.run_count > 0:
            self.init_msg_thread()
            if self.pending_guidance:
                self.add_user_message(self.pending_guidance)
                self.pending_guidance = None
        print_banner(f"Task {self.task.task_id} Iteration ROUND {self.iteration_num}: Test Generation")

        self.run_count += 1
        curr_dir = self.get_latest_write_output_dir()
        os.makedirs(curr_dir, exist_ok=True)

        # Summarize patch if too large
        patch_content = write_test_utils.summarize_large_patch(self.task.patch)

        # Include existing test_patch info if available (small but non-empty)
        existing_test_info = ""
        if (self.task.test_patch or "").strip():
            existing_test_info = write_test_utils.summarize_large_patch(self.task.test_patch)

        # Build user prompt
        user_prompt = write_test_utils.USER_PROMPT_WRITE_TEST.format(
            repo_info=self.repo_basic_info,
            instance_id=self.task.task_id,
            base_commit=self.task.commit,
            problem_statement=self.task.problem_statement,
            patch_content=patch_content,
            existing_tests=existing_test_info,
        )
        self.add_user_message(user_prompt)

        # --- Phase 1: Initial test generation with retries ---
        result_msg, patch_str, test_files, success = write_test_utils.write_test_with_retries(
            self.msg_thread,
            curr_dir,
            retries=3,
            print_callback=print_callback,
        )

        # --- Phase 2: Reflexion loop to improve test quality ---
        if success and patch_str and self.max_reflexion_rounds > 0:
            logger.info(f"Starting reflexion loop ({self.max_reflexion_rounds} rounds) to refine tests.")
            refined_patch, refined_files = write_test_utils.refine_tests_with_reflexion(
                msg_thread=self.msg_thread,
                generated_patch=patch_str,
                problem_statement=self.task.problem_statement,
                code_patch=self.task.patch,
                output_dir=curr_dir,
                max_rounds=self.max_reflexion_rounds,
                print_callback=print_callback,
            )
            patch_str = refined_patch
            test_files = refined_files

        if success and patch_str:
            self.generated_test_patch = patch_str
            self.generated_test_files = test_files
            logger.info(f"Generated test patch with {len(test_files)} test file(s): {test_files}")

        summary = (
            f"Test generation succeeded: {len(test_files)} file(s)." if success
            else "Test generation failed."
        )

        # Save conversation
        conversation_file = pjoin(curr_dir, "conversation.json")
        self.msg_thread.save_to_file(conversation_file)

        return result_msg, summary, success

    def get_generated_test_patch(self) -> str:
        return self.generated_test_patch or ""

    def get_generated_test_files(self) -> list[str]:
        return self.generated_test_files
