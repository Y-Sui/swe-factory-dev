from pathlib import Path

from app.task import SweTask
from app.agents.write_dockerfile_agent import WriteDockerfileAgent
from app.agents.write_dockerfile_agent.write_dockerfile_utils import get_base_image_for_repo
from app.prompts.prompts import get_repo_env_template
from app.agents.test_analysis_agent import TestAnalysisAgent
from app.agents.context_retrieval_agent import ContextRetrievalAgent
from app.agents.write_test_agent import WriteTestAgent
from app.agents.write_eval_script_agent import WriteEvalScriptAgent
from app.log import setup_logger, close_logger
from swe_factory_utils import parse_test_files_from_patch
import os
import docker
from datetime import datetime
from app.model import common
from os.path import join as pjoin
from loguru import logger
import json
from copy import deepcopy
from filelock import FileLock


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
        env_recovery_rounds: int = 2,
    ):
        self.task = task
        self.output_dir = os.path.abspath(output_dir)
        self.client = client
        self.max_iteration_num = max_iteration_num
        self.start_time = start_time
        self.workflow_finish_status = False
        self.env_recovery_rounds = max(env_recovery_rounds, 0)
        self._pre_build_count = 0  # counter exclusively for _try_build_dockerfile builds

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
        return parse_test_files_from_patch(self.task.test_patch or "")

    def _get_import_examples_for_patch(self) -> str:
        """Scan the local repo clone for how existing code imports the modules modified by the patch.

        Returns a formatted string to inject into WriteTestAgent's context, or "" if nothing found.
        """
        import subprocess

        patch = self.task.patch or ""
        repo_path = self.task.project_path

        # Extract non-test source files modified by the patch
        modified_src_files: list[str] = []
        for line in patch.split("\n"):
            if not line.startswith("diff --git "):
                continue
            parts = line.split()
            if len(parts) < 4:
                continue
            path = parts[3][2:]  # strip "b/" prefix
            if any(seg in path for seg in ("test_", "_test.", "/tests/", "/test/")):
                continue
            if path.endswith(".py"):
                modified_src_files.append(path)

        if not modified_src_files:
            return ""

        import_examples: list[str] = []
        seen_stmts: set[str] = set()
        for src_file in modified_src_files[:5]:
            module_base = os.path.basename(src_file).replace(".py", "")
            if not module_base:
                continue
            # Search for both "import <module>" and "from ... import ... <module>" patterns
            for grep_pattern in [f"import {module_base}", f"from .* import.*{module_base}"]:
                try:
                    result = subprocess.run(
                        ["grep", "-r", "--include=*.py", "-n", "-E", grep_pattern, repo_path],
                        capture_output=True, text=True, timeout=10,
                    )
                    for raw in result.stdout.strip().split("\n"):
                        if not raw or ".venv" in raw or "site-packages" in raw:
                            continue
                        # raw format: "/path/to/file.py:lineno:import statement"
                        parts_raw = raw.split(":", 2)
                        if len(parts_raw) < 3:
                            continue
                        file_path_raw, _, stmt = parts_raw
                        stmt = stmt.strip()
                        if not stmt or stmt in seen_stmts:
                            continue
                        seen_stmts.add(stmt)
                        rel = os.path.relpath(file_path_raw, repo_path)
                        import_examples.append(f"  {stmt}  # from {rel}")
                        if len(import_examples) >= 8:
                            break
                except Exception:
                    pass
                if len(import_examples) >= 8:
                    break
            if len(import_examples) >= 8:
                break

        if not import_examples:
            return ""

        return (
            "## Import examples from existing code\n"
            "These are actual import lines found in the repo for the modules modified by the patch.\n"
            "Use this EXACT import style in your tests — do NOT guess:\n"
            + "\n".join(import_examples)
            + "\n"
        )

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

    def _try_build_dockerfile(self, dockerfile: str) -> str | None:
        """Attempt to build the Dockerfile. Returns error string on failure, None on success."""
        analysis_agent: TestAnalysisAgent = self.agents_dict["test_analysis_agent"]  # type: ignore[assignment]

        # Reuse cached image when Dockerfile hasn't changed
        if dockerfile == analysis_agent._cached_dockerfile and analysis_agent._cached_image_name:
            return None

        self._pre_build_count += 1
        build_dir = os.path.join(self.output_dir, f"dockerfile_build_{self._pre_build_count}")
        os.makedirs(build_dir, exist_ok=True)
        build_logger = setup_logger(self.task.task_id.lower(), Path(f"{build_dir}/build.log"))
        image_name = f"{self.task.task_id.lower()}-prebuild{self._pre_build_count}:latest"

        try:
            analysis_agent.build_docker_image(
                dockerfile, build_dir, self.task.task_id.lower(),
                image_name, build_logger, self.client,
            )
            analysis_agent._cached_image_name = image_name
            analysis_agent._cached_dockerfile = dockerfile
            return None
        except Exception as e:
            build_logger.error(f"Dockerfile build failed: {e}")
            return str(e)
        finally:
            close_logger(build_logger)

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
                    self.agents_dict["write_docker_agent"].add_user_message(collected_information)
                    self.agents_dict["write_eval_script_agent"].add_user_message(collected_information)
                    if self.needs_test_generation:
                        self.agents_dict["write_test_agent"].add_user_message(collected_information)
                        import_examples = self._get_import_examples_for_patch()
                        if import_examples:
                            self.agents_dict["write_test_agent"].add_user_message(import_examples)

            # Step 2: Dockerfile generation + build validation
            # Inner self-reflection loop: generate → build → feed error back → repeat (at least 2 rounds).
            # Only advances to WriteTestAgent once Docker build succeeds.
            if self.get_agent_status("context_retrieval_agent") and not self.get_agent_status("write_docker_agent"):
                dockerfile_agent: WriteDockerfileAgent = self.agents_dict["write_docker_agent"]  # type: ignore[assignment]
                for _ in range(2):
                    _, _, agent_success = dockerfile_agent.run_task()
                    self.dump_cost()
                    if not agent_success:
                        break
                    build_error = self._try_build_dockerfile(dockerfile_agent.get_latest_dockerfile())
                    if build_error is None:
                        self.set_agent_status("write_docker_agent", True)
                        break
                    dockerfile_agent.pending_guidance = (
                        f"Docker build failed with the following error:\n{build_error}\n\n"
                        "Please fix the Dockerfile so it builds successfully."
                    )

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

                    self.set_agent_status("write_test_agent", True)
                    eval_agent: WriteEvalScriptAgent = self.agents_dict["write_eval_script_agent"]  # type: ignore[assignment]
                    original_patch = (self.task.test_patch or "").strip()
                    if original_patch:
                        eval_agent.test_patch = original_patch + "\n" + gen_patch
                    else:
                        eval_agent.test_patch = gen_patch
                    eval_agent.generated_test_files = gen_files
                    eval_agent.test_files_content.update(gen_file_contents)
                    eval_agent.test_files = eval_agent.get_test_files()
                    eval_agent.initial_skeleton = eval_agent.get_initial_eval_script_skeleton()
                    self.set_agent_status("write_eval_script_agent", False)

            # Step 4: Eval script generation (LLM-based)
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
                    self.set_agent_status("write_eval_script_agent", False)
                    dockerfile_agent: WriteDockerfileAgent = self.agents_dict["write_docker_agent"]  # type: ignore[assignment]
                    dockerfile_agent.pending_guidance = (dockerfile_agent.pending_guidance or "") + (
                        f"The test analysis agent found a problem with the Dockerfile:\n{guidance}\n\n"
                    )

                guidance = analysis.get("guidance_for_write_eval_script_agent")
                if guidance:
                    self.set_agent_status("write_eval_script_agent", False)
                    eval_script_agent: WriteEvalScriptAgent = self.agents_dict["write_eval_script_agent"]  # type: ignore[assignment]
                    eval_script_agent.pending_guidance = (eval_script_agent.pending_guidance or "") + (
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
