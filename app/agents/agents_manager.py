from pathlib import Path
import ast

from app.task import SweTask
from app.agents.write_dockerfile_agent import WriteDockerfileAgent
from app.agents.write_dockerfile_agent.write_dockerfile_utils import get_base_image_for_repo
from app.prompts.prompts import get_repo_env_template
from app.agents.test_analysis_agent import TestAnalysisAgent
from app.agents.write_eval_script_agent import WriteEvalScriptAgent
from app.agents.context_retrieval_agent import ContextRetrievalAgent
from app.agents.write_test_agent import WriteTestAgent
from app.agents.write_eval_script_agent.write_eval_script_utils import _generate_cat_heredoc_block
from app.agents.test_analysis_agent.docker_utils import (
    build_container,
    copy_to_container,
    exec_run_with_timeout,
    cleanup_container,
)
from app.log import setup_logger, close_logger
from swe_factory_utils import extract_exit_code, classify_f2p
import os
import re
import traceback
import docker
from datetime import datetime
from app.model import common
from os.path import join as pjoin
from loguru import logger
import json
from copy import deepcopy
from filelock import FileLock

DIFF_MODIFIED_FILE_REGEX = r"--- a/(.*)"
DIFF_NEW_FILE_REGEX = r"\+\+\+ b/(.*)"
DIFF_DEVNULL_REGEX = r"--- /dev/null\n\+\+\+ b/(.*)"

# Repo-specific workdir and pytest command for quick F2P validation.
# {files} is replaced with space-separated test file paths.
_QUICK_TEST_CMD = {
    "MiroMindAI/miroflow": ("/testbed", ".venv/bin/pytest {files} -xvs --override-ini=\"addopts=\""),
    "MiroMindAI/MiroThinker": (
        "/testbed/apps/miroflow-agent",
        ".venv/bin/pytest {files} -xvs --override-ini=\"addopts=\"",
    ),
    "MiroMindAI/sd-torchtune": ("/testbed", "pytest {files} -xvs --without-integration --override-ini=\"addopts=\""),
}


class AgentsManager:
    """
    Orchestrates the agent workflow: ContextRetrieval → Dockerfile → WriteTest → EvalScript → TestAnalysis.
    """
    def __init__(
        self,
        task: SweTask,
        output_dir: str,
        client: docker.DockerClient,
        start_time: datetime,
        max_iteration_num: int,
        results_path: str,
        disable_run_test: bool,
        quick_f2p_rounds: int = 2,
        env_recovery_rounds: int = 2,
    ):
        self.task = task
        self.output_dir = os.path.abspath(output_dir)
        self.client = client
        self.max_iteration_num = max_iteration_num
        self.start_time = start_time
        self.workflow_finish_status = False
        self.quick_f2p_rounds = quick_f2p_rounds
        self.disable_quick_f2p = quick_f2p_rounds <= 0 or disable_run_test
        self.env_recovery_rounds = max(env_recovery_rounds, 0)

        # Auto-populate base_image from repo name mapping if not already set
        if not getattr(task, "base_image", None):
            task.base_image = get_base_image_for_repo(task.repo_name)

        self.test_files = self.get_test_files()
        self.repo_basic_info = self.get_repository_basic_info()

        self.agents_dict = {
            "write_docker_agent": WriteDockerfileAgent(task, output_dir, self.repo_basic_info),
            "write_eval_script_agent": WriteEvalScriptAgent(task, output_dir, self.repo_basic_info),
            "test_analysis_agent": TestAnalysisAgent(task, output_dir, self.repo_basic_info, client),
            "context_retrieval_agent": ContextRetrievalAgent(task, output_dir, self.repo_basic_info),
        }
        self.set_agent_status("all", False)

        # Always generate tests for our repos (test_patch is empty or has fewer than 3 files)
        self.needs_test_generation = (
            not (self.task.test_patch or "").strip() or len(self.test_files) < 3
        )
        if self.needs_test_generation:
            self.agents_dict["write_test_agent"] = WriteTestAgent(task, output_dir, self.repo_basic_info)
            self.set_agent_status("write_test_agent", False)

        self.agents_dict["test_analysis_agent"].disable_run_test = disable_run_test

        self.results_file = f"{results_path}/results.json"
        lock_path = self.results_file + ".lock"
        self.lock = FileLock(lock_path, timeout=30)
        with self.lock:
            if not os.path.exists(self.results_file):
                with open(self.results_file, "w") as f:
                    json.dump([], f, indent=2)

    def set_agent_status(self, agent_name: str, status: bool):
        if agent_name == "all":
            for agent in self.agents_dict.values():
                agent.finish_status = status
        elif agent_name in self.agents_dict:
            self.agents_dict[agent_name].finish_status = status
        else:
            logger.error(f"Agent {agent_name} not found!")

    def get_agent_status(self, agent_name: str) -> bool:
        if agent_name in self.agents_dict:
            return self.agents_dict[agent_name].finish_status
        logger.error(f"Agent {agent_name} not found!")
        return False

    def set_agents_iteration_num(self, iteration_num: int) -> None:
        for agent in self.agents_dict.values():
            agent.iteration_num = iteration_num

    def get_test_files(self) -> list[str]:
        patch = self.task.test_patch or ""
        # Match modified files (--- a/path), excluding /dev/null
        modified = [p.split("\t")[0] for p in re.findall(DIFF_MODIFIED_FILE_REGEX, patch)
                    if not p.startswith("/dev/null")]
        # Match all target files (+++ b/path), excluding /dev/null
        new_files = [p.split("\t")[0] for p in re.findall(DIFF_NEW_FILE_REGEX, patch)
                     if not p.startswith("/dev/null")]
        # Deduplicate while preserving order
        return list(dict.fromkeys(modified + new_files))

    def get_repository_basic_info(self) -> str:
        base = (
            f"Target repository name: {self.task.repo_name}\n"
            f"Commit SHA: {self.task.commit}\n"
            f"Version: {self.task.version}\n"
            "Target test files:\n"
            + "\n".join(self.test_files)
            + "\n"
        )
        template = get_repo_env_template(self.task.repo_name)
        if template:
            base += f"\n{template}"
        return base

    def dump_cost(self):
        end_time = datetime.now()
        stats = {
            "start_epoch": self.start_time.timestamp(),
            "end_epoch": end_time.timestamp(),
            "elapsed_seconds": (end_time - self.start_time).total_seconds(),
        }
        stats.update(common.SELECTED_MODEL.get_overall_exec_stats())
        with open(pjoin(self.output_dir, "cost.json"), "w") as f:
            json.dump(stats, f, indent=4)

    def _read_results(self) -> list:
        with self.lock:
            with open(self.results_file, "r") as f:
                return json.load(f)

    def _write_results(self, records: list) -> None:
        tmp = self.results_file + ".tmp"
        with self.lock:
            with open(tmp, "w") as f:
                json.dump(records, f, indent=2)
            os.replace(tmp, self.results_file)

    def _get_clean_command(self) -> str:
        """Return a repo-aware git clean command that preserves uv virtualenvs."""
        if self.task.repo_name == "MiroMindAI/miroflow":
            return "git clean -fdx -e .venv"
        if self.task.repo_name == "MiroMindAI/MiroThinker":
            return "git clean -fdx -e .venv -e apps/miroflow-agent/.venv"
        return "git clean -fdx"

    def _get_pytest_bootstrap(self) -> str:
        """Install pytest into uv-managed venv if it was removed by a reset."""
        if self.task.repo_name == "MiroMindAI/miroflow":
            return (
                'if [ ! -x "/testbed/.venv/bin/pytest" ]; then\n'
                '  cd /testbed\n'
                '  uv pip install pytest pytest-asyncio\n'
                "fi\n"
            )
        if self.task.repo_name == "MiroMindAI/MiroThinker":
            return (
                'if [ ! -x "/testbed/apps/miroflow-agent/.venv/bin/pytest" ]; then\n'
                '  cd /testbed/apps/miroflow-agent\n'
                '  uv pip install pytest pytest-asyncio\n'
                "fi\n"
            )
        return ""

    def _python_bin_for_repo(self) -> str:
        if self.task.repo_name == "MiroMindAI/miroflow":
            return "/testbed/.venv/bin/python"
        if self.task.repo_name == "MiroMindAI/MiroThinker":
            return "/testbed/apps/miroflow-agent/.venv/bin/python"
        return "python"

    def _should_preflight_import(self, module_name: str) -> bool:
        root = module_name.split(".", 1)[0]
        if self.task.repo_name in {"MiroMindAI/miroflow", "MiroMindAI/MiroThinker"}:
            local_roots = {
                "core", "llm", "utils", "workflow", "agents", "config",
                "src", "miroflow", "miroflow_agent",
            }
            return root in local_roots or module_name.startswith("src.")
        if self.task.repo_name == "MiroMindAI/sd-torchtune":
            return root in {"torchtune", "recipes", "src"}
        return False

    def _collect_import_modules(self, test_file_contents: dict[str, str]) -> list[str]:
        modules: set[str] = set()
        for path, content in test_file_contents.items():
            if not path.endswith(".py"):
                continue
            try:
                tree = ast.parse(content)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        name = (alias.name or "").strip()
                        if name and self._should_preflight_import(name):
                            modules.add(name)
                elif isinstance(node, ast.ImportFrom):
                    if node.level and node.level > 0:
                        continue
                    name = (node.module or "").strip()
                    if name and self._should_preflight_import(name):
                        modules.add(name)
        return sorted(modules)

    def _build_import_preflight_block(self, modules: list[str]) -> str:
        if not modules:
            return ""
        python_bin = self._python_bin_for_repo()
        lines = [f'PYTHON_BIN="{python_bin}"']
        lines.extend(
            [
                f"""$PYTHON_BIN -c "import importlib; importlib.import_module('{m}')" || {{ echo "OMNIGRIL_IMPORT_PREFLIGHT_FAILED={m}"; rc=97; echo "OMNIGRIL_EXIT_CODE=$rc"; exit 0; }}"""
                for m in modules
            ]
        )
        return "\n".join(lines) + "\n"

    def _looks_like_infra_failure(self, analysis: dict, f2p_classification: str | None) -> bool:
        if f2p_classification in {"FAIL2FAIL", "ERROR"}:
            return True
        lower = " ".join(
            str(analysis.get(k, "")).lower()
            for k in (
                "guidance_for_write_dockerfile_agent",
                "guidance_for_write_eval_script_agent",
                "guidance_for_context_retrieval_agent",
                "guidance_for_write_test_agent",
            )
        )
        infra_keywords = (
            "no such file or directory",
            "command not found",
            "modulenotfounderror",
            "importerror",
            "import error",
            "pytest",
            ".venv",
            "xdist",
            "--cov",
            "dependency",
            "pip install",
            "uv sync",
            "git apply",
            "patch failed",
            "environment",
            "docker",
        )
        return any(k in lower for k in infra_keywords)

    def _run_quick_f2p(
        self,
        dockerfile: str,
        test_files: list[str],
        test_file_contents: dict[str, str],
    ) -> tuple[str, str, str]:
        """Quick F2P check in Docker. Returns (classification, pre_output, post_output).

        Builds an image (reusing TestAnalysisAgent's cache), creates a
        container, writes test files, runs pytest, then applies the gold
        patch and re-runs.  No LLM calls — purely mechanical.
        """
        repo = self.task.repo_name
        workdir, cmd_template = _QUICK_TEST_CMD.get(
            repo, ("/testbed", "pytest {files} -xvs --override-ini=\"addopts=\"")
        )
        if not test_files:
            return "ERROR", "No generated test files were provided for quick F2P.", ""
        selected_files = {p: test_file_contents[p] for p in test_files if p in test_file_contents}
        if not selected_files:
            return "ERROR", "Generated test files have no materialized content.", ""
        files_str = " ".join(selected_files.keys())
        test_cmd = cmd_template.format(files=files_str)
        pytest_bootstrap = self._get_pytest_bootstrap()
        cat_block = _generate_cat_heredoc_block(selected_files)
        import_preflight = self._build_import_preflight_block(self._collect_import_modules(selected_files))
        eval_sh = (
            "#!/bin/bash\nset -e\n"
            "cd /testbed\n"
            f"{cat_block}\n"
            f"cd {workdir}\n"
            f"{pytest_bootstrap}"
            f"cd {workdir}\n"
            f"{import_preflight}"
            # Disable set -e so a failing test still prints the exit code
            f"set +e\n"
            f"{test_cmd}; rc=$?; echo \"OMNIGRIL_EXIT_CODE=$rc\"\n"
        )

        # Reuse TestAnalysisAgent's image build logic + cache
        analysis_agent: TestAnalysisAgent = self.agents_dict["test_analysis_agent"]  # type: ignore[assignment]
        analysis_agent.setup_dockerfile_num += 1
        quick_dir = os.path.join(self.output_dir, f"quick_f2p_{analysis_agent.setup_dockerfile_num}")
        os.makedirs(quick_dir, exist_ok=True)
        qf2p_logger = setup_logger(self.task.task_id.lower(), Path(f"{quick_dir}/quick_f2p.log"))

        image_name = f"{self.task.task_id.lower()}-dockerfile{analysis_agent.setup_dockerfile_num}:latest"

        # Build or reuse cached image
        dockerfile_changed = (dockerfile != analysis_agent._cached_dockerfile)
        if not dockerfile_changed and analysis_agent._cached_image_name:
            image_name = analysis_agent._cached_image_name
            qf2p_logger.info(f"Reusing cached image {image_name}")
        else:
            try:
                analysis_agent.build_docker_image(
                    dockerfile, quick_dir, self.task.task_id.lower(),
                    image_name, qf2p_logger, self.client,
                )
                analysis_agent._cached_image_name = image_name
                analysis_agent._cached_dockerfile = dockerfile
            except Exception as e:
                qf2p_logger.error(f"Quick F2P image build failed: {e}")
                close_logger(qf2p_logger)
                return "ERROR", str(e), ""

        container = None
        pre_output = ""
        post_output = ""
        classification = "ERROR"
        container_name = f"{self.task.task_id.lower()}-quickf2p-{analysis_agent.setup_dockerfile_num}"

        try:
            container = build_container(
                self.client, image_name, container_name,
                self.task.task_id.lower(), qf2p_logger,
            )
            container.start()

            # Write eval.sh locally, copy into container
            eval_file = Path(f"{quick_dir}/eval.sh")
            eval_file.write_text(eval_sh)
            copy_to_container(container, eval_file, Path("/eval.sh"))

            # Phase 1: pre-patch (no gold patch)
            pre_result = exec_run_with_timeout(container, "/bin/bash /eval.sh", timeout=300)
            pre_output = pre_result.decode("utf-8") if pre_result else ""
            pre_exit = extract_exit_code(pre_output)
            qf2p_logger.info(f"Pre-patch exit code: {pre_exit}")

            # Reset container state
            container.exec_run(f"git reset --hard {self.task.commit}", workdir="/testbed", user="root")
            container.exec_run(self._get_clean_command(), workdir="/testbed", user="root")

            # Apply gold patch
            patch_file = Path(f"{quick_dir}/gold_patch.diff")
            patch_file.write_text(self.task.patch or "")
            if not self.task.patch or not self.task.patch.strip():
                raise RuntimeError("Quick F2P cannot apply an empty gold patch.")
            copy_to_container(container, patch_file, Path("/tmp/patch.diff"))
            apply_result = container.exec_run("git apply -p1 -v /tmp/patch.diff", workdir="/testbed", user="root")
            if apply_result.exit_code != 0:
                # Fallback
                fallback = container.exec_run("patch --batch --fuzz=5 -p1 -i /tmp/patch.diff", workdir="/testbed", user="root")
                if fallback.exit_code != 0:
                    apply_log = apply_result.output.decode("utf-8", errors="replace")
                    fallback_log = fallback.output.decode("utf-8", errors="replace")
                    raise RuntimeError(
                        "Quick F2P failed to apply gold patch.\n"
                        f"git apply output:\n{apply_log}\n\n"
                        f"patch fallback output:\n{fallback_log}"
                    )

            # Re-copy eval.sh and run phase 2
            copy_to_container(container, eval_file, Path("/eval.sh"))
            post_result = exec_run_with_timeout(container, "/bin/bash /eval.sh", timeout=300)
            post_output = post_result.decode("utf-8") if post_result else ""
            post_exit = extract_exit_code(post_output)
            qf2p_logger.info(f"Post-patch exit code: {post_exit}")

            classification = classify_f2p(pre_exit, post_exit)
            qf2p_logger.info(f"Quick F2P classification: {classification}")

        except Exception as e:
            qf2p_logger.error(f"Quick F2P error: {e}\n{traceback.format_exc()}")
            classification = "ERROR"
        finally:
            cleanup_container(self.client, container, qf2p_logger)
            close_logger(qf2p_logger)

        # Save outputs
        for fname, content in [("pre_output.txt", pre_output), ("post_output.txt", post_output)]:
            with open(os.path.join(quick_dir, fname), "w") as f:
                f.write(content)

        return classification, pre_output, post_output

    def _format_f2p_feedback(self, classification: str, pre_output: str, post_output: str) -> str:
        """Map quick F2P classification to actionable feedback for WriteTestAgent."""
        max_output = 3000  # truncate to keep prompt manageable
        import_fail_markers = re.findall(
            r"OMNIGRIL_IMPORT_PREFLIGHT_FAILED=([A-Za-z0-9_\.]+)",
            pre_output + "\n" + post_output,
        )
        if import_fail_markers:
            failing_modules = ", ".join(sorted(set(import_fail_markers)))
            return (
                "Quick F2P validation found import preflight failures in generated tests.\n"
                f"Failing imports: {failing_modules}\n\n"
                "Fix import paths to match the repository package layout. For repos with "
                "`packages = [\"src\"]`, never import using `from src...`; strip `src/` and "
                "import from package-root modules instead."
            )
        if classification == "PASS2PASS":
            snippet = pre_output[-max_output:] if len(pre_output) > max_output else pre_output
            return (
                "Quick F2P validation: PASS2PASS — the generated tests pass BOTH before and "
                "after the gold patch.  They do not capture the bug.\n\n"
                "Pre-patch test output (tests should FAIL here but they PASS):\n"
                f"```\n{snippet}\n```\n\n"
                "Please rewrite the tests so that at least one test FAILS at the base commit "
                "(before the gold patch is applied) and PASSES after the gold patch."
            )
        elif classification == "FAIL2FAIL":
            snippet = pre_output[-max_output:] if len(pre_output) > max_output else pre_output
            return (
                "Quick F2P validation: FAIL2FAIL — the generated tests fail BOTH before and "
                "after the gold patch.  The tests likely have import errors, syntax errors, "
                "or environment issues unrelated to the bug.\n\n"
                "Pre-patch error output:\n"
                f"```\n{snippet}\n```\n\n"
                "Please fix the test errors.  Ensure the tests can actually run in the "
                "container environment and that failures are caused by the bug, not broken imports."
            )
        elif classification == "PASS2FAIL":
            snippet = post_output[-max_output:] if len(post_output) > max_output else post_output
            return (
                "Quick F2P validation: PASS2FAIL — the tests pass before the gold patch but "
                "FAIL after it.  The assertions are likely backwards.\n\n"
                "Post-patch failure output:\n"
                f"```\n{snippet}\n```\n\n"
                "Please invert the assertions or rewrite the tests so they test the correct "
                "expected behavior after the fix."
            )
        else:
            return (
                f"Quick F2P validation: {classification} — could not determine exit codes.  "
                "The eval script may have failed to run.  Check that test file paths are correct "
                "and the test framework is available in the container."
            )

    def run_workflow(self) -> None:
        iteration_num = 0
        allowed_iterations = self.max_iteration_num
        remaining_recovery_rounds = self.env_recovery_rounds
        exhausted_rounds = True
        while iteration_num < allowed_iterations:
            self.set_agents_iteration_num(iteration_num)
            infra_failure_this_round = False

            # Step 1: Context retrieval
            if not self.get_agent_status("context_retrieval_agent"):
                collected_information, _, _ = self.agents_dict["context_retrieval_agent"].run_task()
                self.dump_cost()
                if collected_information is not None:
                    self.set_agent_status("context_retrieval_agent", True)
                    self.agents_dict["write_eval_script_agent"].add_user_message(collected_information)
                    self.agents_dict["write_docker_agent"].add_user_message(collected_information)
                    if self.needs_test_generation:
                        self.agents_dict["write_test_agent"].add_user_message(collected_information)

            # Step 2: Dockerfile generation
            if self.get_agent_status("context_retrieval_agent") and not self.get_agent_status("write_docker_agent"):
                _, _, success = self.agents_dict["write_docker_agent"].run_task()
                self.dump_cost()
                if success:
                    self.set_agent_status("write_docker_agent", True)

            # Step 3: Test generation (after Dockerfile is ready)
            if (self.needs_test_generation
                    and self.get_agent_status("context_retrieval_agent")
                    and self.get_agent_status("write_docker_agent")
                    and not self.get_agent_status("write_test_agent")):
                _, _, success = self.agents_dict["write_test_agent"].run_task()
                self.dump_cost()
                if success:
                    write_test_agent: WriteTestAgent = self.agents_dict["write_test_agent"]  # type: ignore[assignment]
                    gen_patch = write_test_agent.get_generated_test_patch()
                    gen_files = write_test_agent.get_generated_test_files()
                    gen_file_contents = write_test_agent.get_generated_test_file_contents()

                    # Step 3.5: Quick F2P validation loop
                    if gen_patch and not self.disable_quick_f2p:
                        docker_agent_for_f2p: WriteDockerfileAgent = self.agents_dict["write_docker_agent"]  # type: ignore[assignment]
                        dockerfile_for_f2p = docker_agent_for_f2p.get_latest_dockerfile()
                        for f2p_round in range(self.quick_f2p_rounds):
                            logger.info(f"Quick F2P round {f2p_round + 1}/{self.quick_f2p_rounds}")
                            classification, pre_out, post_out = self._run_quick_f2p(
                                dockerfile_for_f2p, gen_files, gen_file_contents,
                            )
                            logger.info(f"Quick F2P result: {classification}")
                            if classification == "FAIL2PASS":
                                break
                            if f2p_round < self.quick_f2p_rounds - 1:
                                feedback = self._format_f2p_feedback(classification, pre_out, post_out)
                                write_test_agent.pending_guidance = feedback
                                write_test_agent.finish_status = False
                                _, _, retry_ok = write_test_agent.run_task()
                                self.dump_cost()
                                if not retry_ok:
                                    break
                                gen_patch = write_test_agent.get_generated_test_patch()
                                gen_files = write_test_agent.get_generated_test_files()
                                gen_file_contents = write_test_agent.get_generated_test_file_contents()

                    self.set_agent_status("write_test_agent", True)
                    eval_agent: WriteEvalScriptAgent = self.agents_dict["write_eval_script_agent"]  # type: ignore[assignment]
                    existing_patch = eval_agent.test_patch.strip()
                    if existing_patch:
                        eval_agent.test_patch = existing_patch + "\n" + gen_patch
                    else:
                        eval_agent.test_patch = gen_patch
                    eval_agent.generated_test_files = gen_files
                    eval_agent.test_files_content.update(gen_file_contents)
                    # Re-derive test_files from the updated patch to stay consistent
                    eval_agent.test_files = eval_agent.get_test_files()
                    eval_agent.initial_skeleton = eval_agent.get_initial_eval_script_skeleton()

            # Step 4: Eval script generation
            test_gen_ready = not self.needs_test_generation or self.get_agent_status("write_test_agent")
            if (self.get_agent_status("context_retrieval_agent")
                    and self.get_agent_status("write_docker_agent")
                    and test_gen_ready
                    and not self.get_agent_status("write_eval_script_agent")):
                _docker_agent: WriteDockerfileAgent = self.agents_dict["write_docker_agent"]  # type: ignore[assignment]
                _eval_agent: WriteEvalScriptAgent = self.agents_dict["write_eval_script_agent"]  # type: ignore[assignment]
                _eval_agent.dockerfile = _docker_agent.get_latest_dockerfile()
                _, _, success = _eval_agent.run_task()
                self.dump_cost()
                if success:
                    self.set_agent_status("write_eval_script_agent", True)

            # Step 5: Test analysis
            if (self.get_agent_status("context_retrieval_agent")
                    and self.get_agent_status("write_docker_agent")
                    and self.get_agent_status("write_eval_script_agent")):
                _docker_agent2: WriteDockerfileAgent = self.agents_dict["write_docker_agent"]  # type: ignore[assignment]
                _eval_agent2: WriteEvalScriptAgent = self.agents_dict["write_eval_script_agent"]  # type: ignore[assignment]
                _analysis_agent: TestAnalysisAgent = self.agents_dict["test_analysis_agent"]  # type: ignore[assignment]
                _analysis_agent.dockerfile = _docker_agent2.get_latest_dockerfile()
                _analysis_agent.eval_script_skeleton = _eval_agent2.get_latest_eval_script_skeleton()
                _analysis_agent.eval_script = _eval_agent2.get_latest_eval_script() or ""

                analysis, _, success = _analysis_agent.run_task()
                self.dump_cost()

                if isinstance(analysis, str):
                    try:
                        analysis = json.loads(analysis)
                    except (json.JSONDecodeError, ValueError, TypeError):
                        analysis = {}
                elif not isinstance(analysis, dict):
                    analysis = {}

                if analysis.get("is_finish"):
                    self.workflow_finish_status = True
                    exhausted_rounds = False
                    break
                infra_failure_this_round = self._looks_like_infra_failure(
                    analysis,
                    getattr(_analysis_agent, "f2p_classification", None),
                )

                # Route feedback to agents
                guidance = analysis.get("guidance_for_context_retrieval_agent")
                if guidance:
                    self.set_agent_status("context_retrieval_agent", False)
                    self.agents_dict["context_retrieval_agent"].add_user_message(
                        f"The test analysis agent found additional context is needed:\n{guidance}\n\n"
                    )

                guidance = analysis.get("guidance_for_write_dockerfile_agent")
                if guidance:
                    self.set_agent_status("write_docker_agent", False)
                    dockerfile_agent: WriteDockerfileAgent = self.agents_dict["write_docker_agent"]  # type: ignore[assignment]
                    dockerfile_agent.pending_guidance = (dockerfile_agent.pending_guidance or "") + (
                        f"The test analysis agent found a problem with the Dockerfile:\n{guidance}\n\n"
                    )

                guidance = analysis.get("guidance_for_write_eval_script_agent")
                if guidance:
                    self.set_agent_status("write_eval_script_agent", False)
                    eval_agent: WriteEvalScriptAgent = self.agents_dict["write_eval_script_agent"]  # type: ignore[assignment]
                    eval_agent.pending_guidance = (eval_agent.pending_guidance or "") + (
                        f"The test analysis agent found a problem with the eval script:\n{guidance}\n\n"
                    )

                guidance = analysis.get("guidance_for_write_test_agent")
                if guidance:
                    if "write_test_agent" not in self.agents_dict:
                        self.needs_test_generation = True
                        self.agents_dict["write_test_agent"] = WriteTestAgent(
                            self.task, self.output_dir, self.repo_basic_info
                        )
                    if self.needs_test_generation:
                        self.set_agent_status("write_test_agent", False)
                        self.set_agent_status("write_eval_script_agent", False)
                        test_agent: WriteTestAgent = self.agents_dict["write_test_agent"]  # type: ignore[assignment]
                        test_agent.pending_guidance = (test_agent.pending_guidance or "") + (
                            f"The generated tests need improvement:\n{guidance}\n\n"
                        )
            if (
                not self.workflow_finish_status
                and iteration_num + 1 >= allowed_iterations
                and remaining_recovery_rounds > 0
                and infra_failure_this_round
            ):
                allowed_iterations += 1
                remaining_recovery_rounds -= 1
                logger.info(
                    "Granting one extra environment recovery round "
                    f"({remaining_recovery_rounds} recovery rounds left)."
                )
            iteration_num += 1

        if exhausted_rounds and not self.workflow_finish_status:
            logger.info("Too many rounds. Exceeded iteration limit (including recovery rounds).")

        # Save final outputs
        _final_docker: WriteDockerfileAgent = self.agents_dict["write_docker_agent"]  # type: ignore[assignment]
        _final_eval: WriteEvalScriptAgent = self.agents_dict["write_eval_script_agent"]  # type: ignore[assignment]
        dockerfile_content = _final_docker.get_latest_dockerfile()
        eval_script_content = _final_eval.get_latest_eval_script() or ""
        eval_script_skeleton_content = _final_eval.get_latest_eval_script_skeleton()

        if dockerfile_content and eval_script_content:
            with open(os.path.join(self.output_dir, "Dockerfile"), "w") as f:
                f.write(dockerfile_content)
            with open(os.path.join(self.output_dir, "eval.sh"), "w") as f:
                f.write(eval_script_content)

        f2p_result = getattr(self.agents_dict.get("test_analysis_agent"), "f2p_classification", None)
        status_data = {"is_finish": self.workflow_finish_status}
        if f2p_result:
            status_data["f2p_classification"] = f2p_result
        with open(os.path.join(self.output_dir, "status.json"), "w") as f:
            json.dump(status_data, f)

        if self.workflow_finish_status:
            recs = self._read_results()
            info = deepcopy(self.task.task_info)
            info.update({
                "dockerfile": dockerfile_content,
                "eval_script": eval_script_content,
                "eval_script_skeleton": eval_script_skeleton_content,
            })
            recs.append(info)
            self._write_results(recs)
