import subprocess

from app.data_structures import MessageThread
from app.agents.write_test_agent import write_test_utils
from app.agents.agent import Agent
from app.agents.context_retrieval_agent.context_retrieval_utils import RepoBrowseManager
from app.task import SweTask
from app.utils import parse_function_invocation
from app.model import common
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
    api_functions: list[str] = ["read_file", "search_symbol"]

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
        self.generated_test_file_contents: dict[str, str] = {}
        # Select language-specific system prompt
        self._language = getattr(task, "language", "python") or "python"
        self.pending_guidance: str | None = None
        self.repo_browse_manager = RepoBrowseManager(task.project_path)
        self.init_msg_thread()

    def init_msg_thread(self) -> None:
        self.msg_thread = MessageThread()
        # Use language-aware system prompt for proper test framework guidance
        system_prompt = write_test_utils.get_test_system_prompt(self._language)
        self.add_system_message(system_prompt)
        self.add_user_message(self.repo_basic_info)

    def get_latest_write_output_dir(self) -> str:
        return os.path.join(self.output_dir, f"write_test_agent_{self.run_count}")

    def read_file(self, file_path: str) -> tuple[str, str, bool]:
        """Read a source file from the repo. Path is relative to repo root."""
        if os.path.isabs(file_path):
            abs_path = file_path
        else:
            abs_path = os.path.join(self.task.project_path, file_path)
        try:
            content = self.repo_browse_manager.browse_file(abs_path)
            return content, f"Read {file_path}", True
        except (ValueError, FileNotFoundError) as e:
            return str(e), str(e), False

    def search_symbol(self, symbol_name: str) -> tuple[str, str, bool]:
        """Search for a symbol across all Python files in the repo."""
        try:
            result = subprocess.run(
                ["grep", "-rn", "--include=*.py", symbol_name, self.task.project_path],
                capture_output=True, text=True, timeout=10,
            )
            lines = result.stdout.strip().split("\n")
            # Filter out venv/site-packages and limit to 20 matches
            filtered = []
            for line in lines:
                if not line or ".venv" in line or "site-packages" in line:
                    continue
                # Make paths relative
                rel_line = line.replace(self.task.project_path + "/", "", 1)
                filtered.append(rel_line)
                if len(filtered) >= 20:
                    break
            if not filtered:
                return f"No matches found for '{symbol_name}'.", f"No matches for {symbol_name}", True
            output = f"Search results for '{symbol_name}' ({len(filtered)} matches):\n" + "\n".join(filtered)
            return output, f"Found {len(filtered)} matches", True
        except Exception as e:
            return f"Search failed: {e}", str(e), False

    def _run_research_phase(self, print_callback=None) -> None:
        """Run a research phase where the LLM explores the repo to find correct import paths."""
        modified_files = write_test_utils.extract_modified_files_from_patch(self.task.patch or "")
        if not modified_files:
            logger.info("Research phase: no modified files found in patch, skipping.")
            return

        modified_files_str = "\n".join(f"- {f}" for f in modified_files)
        research_prompt = write_test_utils.RESEARCH_PROMPT.format(modified_files=modified_files_str)
        self.add_user_message(research_prompt)

        max_rounds = 3
        max_calls_per_round = 5

        for round_num in range(max_rounds):
            try:
                res_text, *_ = common.SELECTED_MODEL.call(self.msg_thread.to_msg(), max_tokens=1024)
            except Exception as e:
                logger.error(f"Research phase LLM call failed in round {round_num + 1}: {e}")
                break
            self.msg_thread.add_model(res_text, [])

            tool_calls, is_done = write_test_utils.parse_research_response(res_text)
            if is_done or not tool_calls:
                logger.info(f"Research phase completed after {round_num + 1} round(s).")
                break

            # Execute tool calls
            results = []
            for call_str in tool_calls[:max_calls_per_round]:
                try:
                    func_name, args = parse_function_invocation(call_str)
                except ValueError as e:
                    results.append(f"Error parsing '{call_str}': {e}")
                    continue

                if func_name == "read_file" and args:
                    output, _, _ = self.read_file(args[0])
                    results.append(f"## read_file('{args[0]}')\n{output}")
                elif func_name == "search_symbol" and args:
                    output, _, _ = self.search_symbol(args[0])
                    results.append(f"## search_symbol('{args[0]}')\n{output}")
                else:
                    results.append(f"Unknown tool call: {call_str}")

            tool_results = "\n\n".join(results)
            self.add_user_message(f"{tool_results}\n\n{write_test_utils.RESEARCH_CONTINUE_PROMPT}")
            print_acr(
                f"Research round {round_num + 1}: executed {len(results)} tool call(s)",
                "research phase",
                print_callback=print_callback,
            )

        logger.info("Research phase finished.")

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

        # Research phase: let the LLM explore the repo to discover correct import paths
        self._run_research_phase(print_callback)

        self.run_count += 1
        curr_dir = self.get_latest_write_output_dir()
        os.makedirs(curr_dir, exist_ok=True)
        self.generated_test_file_contents = {}

        # Use patch_context (richer context with full function bodies) instead of raw diff
        patch_content = write_test_utils.summarize_large_patch(self.task.patch_context)

        # Include existing test_patch info if available (small but non-empty)
        existing_test_info = ""
        if (self.task.test_patch or "").strip():
            existing_test_info = write_test_utils.summarize_large_patch(self.task.test_patch)

        # Combine problem_statement with hints_text for richer context
        problem_stmt = self.task.problem_statement
        hints = (self.task.hints_text or "").strip()
        if hints:
            problem_stmt = f"{problem_stmt}\n\n## Developer Hints\n{hints}"

        # Build user prompt
        user_prompt = write_test_utils.USER_PROMPT_WRITE_TEST.format(
            instance_id=self.task.task_id,
            base_commit=self.task.commit,
            problem_statement=problem_stmt,
            patch_content=patch_content,
            existing_tests=existing_test_info,
        )
        self.add_user_message(user_prompt)

        # --- Phase 1: Initial test generation with retries ---
        result_msg, patch_str, test_files, test_file_contents, success = write_test_utils.write_test_with_retries(
            self.msg_thread,
            curr_dir,
            repo_root=self.task.project_path,
            retries=3,
            print_callback=print_callback,
        )

        # --- Phase 2: Reflexion loop to improve test quality ---
        # Skip reflexion on first iteration (no real test feedback yet)
        if success and patch_str and self.max_reflexion_rounds > 0 and self.iteration_num > 0:
            logger.info(f"Starting reflexion loop ({self.max_reflexion_rounds} rounds) to refine tests.")
            refined_patch, refined_files, refined_file_contents = write_test_utils.refine_tests_with_reflexion(
                msg_thread=self.msg_thread,
                generated_test_patch=patch_str,
                output_dir=curr_dir,
                repo_root=self.task.project_path,
                test_file_contents=test_file_contents,
                max_rounds=self.max_reflexion_rounds,
                print_callback=print_callback,
            )
            patch_str = refined_patch
            test_files = refined_files
            test_file_contents = refined_file_contents

        if success and patch_str:
            self.generated_test_patch = patch_str
            self.generated_test_files = test_files
            self.generated_test_file_contents = test_file_contents
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

    def get_generated_test_file_contents(self) -> dict[str, str]:
        return self.generated_test_file_contents
