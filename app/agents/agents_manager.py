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
from swe_factory_utils import (
    parse_test_files_from_patch,
    parse_per_test_results,
    classify_per_test_f2p,
    filter_test_file_by_names,
)
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

    # Class-level Docker image cache shared across instances in the same process.
    # Key: "repo__version", Value: {"image_name": str, "dockerfile": str}
    # For cross-process sharing, persisted to docker_image_cache.json in the parent output dir.
    _docker_image_cache: dict[str, dict] = {}
    _docker_cache_lock = None  # initialized per-instance from FileLock

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

        # Docker image cache file (shared across all instances for this repo)
        parent_dir = os.path.dirname(self.output_dir)
        self._docker_cache_file = os.path.join(parent_dir, "docker_image_cache.json")
        self._docker_cache_file_lock = FileLock(self._docker_cache_file + ".lock", timeout=30)

        # Auto-populate base_image from repo name mapping if not already set
        if not getattr(task, "base_image", None):
            task.base_image = get_base_image_for_repo(task.repo_name)

        self.test_files = self.get_test_files()
        self.repo_basic_info = self.get_repository_basic_info(include_env_template=True)
        self.repo_basic_info_slim = self.get_repository_basic_info(include_env_template=False)

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
            self.agents_dict["write_test_agent"] = WriteTestAgent(task, output_dir, self.repo_basic_info_slim)
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

    def get_repository_basic_info(self, include_env_template: bool = True) -> str:
        base = (
            f"Target repository name: {self.task.repo_name}\n"
            f"Commit SHA: {self.task.commit}\n"
            f"Version: {self.task.version}\n"
            "Target test files:\n"
            + "\n".join(self.test_files)
            + "\n"
        )
        if include_env_template:
            template = get_repo_env_template(self.task.repo_name)
            if template:
                base += f"\n{template}"
        return base

    def _get_version_cache_key(self) -> str:
        """Cache key for Docker image reuse: repo + version."""
        repo = self.task.repo_name.replace("/", "__")
        version = self.task.version or "unknown"
        return f"{repo}__{version}"

    def _load_docker_image_cache(self) -> dict:
        """Load the shared Docker image cache from disk."""
        with self._docker_cache_file_lock:
            if os.path.exists(self._docker_cache_file):
                with open(self._docker_cache_file, "r") as f:
                    return json.load(f)
        return {}

    def _save_docker_image_cache(self, cache: dict) -> None:
        """Save the shared Docker image cache to disk."""
        with self._docker_cache_file_lock:
            tmp = self._docker_cache_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(cache, f, indent=2)
            os.replace(tmp, self._docker_cache_file)

    def _try_reuse_cached_image(self) -> bool:
        """Check if a Docker image for this repo+version is already cached.

        If found, sets the TestAnalysisAgent cached image and marks
        the Dockerfile + context retrieval agents as done.
        Returns True if reuse succeeded.
        """
        cache_key = self._get_version_cache_key()
        cache = self._load_docker_image_cache()
        entry = cache.get(cache_key)
        if not entry:
            return False

        image_name = entry["image_name"]
        dockerfile = entry["dockerfile"]

        # Verify the image still exists in Docker
        try:
            self.client.images.get(image_name)
        except docker.errors.ImageNotFound:
            logger.info(f"Cached image {image_name} no longer exists, will rebuild.")
            return False

        logger.info(f"Reusing cached Docker image {image_name} for {cache_key}")

        # Set up agents as if Dockerfile was already built
        analysis_agent: TestAnalysisAgent = self.agents_dict["test_analysis_agent"]
        analysis_agent._cached_image_name = image_name
        analysis_agent._cached_dockerfile = dockerfile
        # Store the commit that was baked into this image so run_test can re-checkout
        analysis_agent._cached_image_commit = entry.get("commit")

        # Save Dockerfile to output dir for reference
        docker_agent: WriteDockerfileAgent = self.agents_dict["write_docker_agent"]
        docker_dir = docker_agent.get_latest_write_dockerfile_output_dir()
        os.makedirs(docker_dir, exist_ok=True)
        with open(os.path.join(docker_dir, "Dockerfile"), "w") as f:
            f.write(dockerfile)

        self.set_agent_status("context_retrieval_agent", True)
        self.set_agent_status("write_docker_agent", True)
        return True

    def _cache_docker_image(self, image_name: str, dockerfile: str) -> None:
        """Save a successfully built Docker image to the shared cache."""
        cache_key = self._get_version_cache_key()
        cache = self._load_docker_image_cache()
        cache[cache_key] = {
            "image_name": image_name,
            "dockerfile": dockerfile,
            "commit": self.task.commit,
        }
        self._save_docker_image_cache(cache)
        logger.info(f"Cached Docker image {image_name} for {cache_key}")

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

    def _try_per_test_filtering(self) -> bool:
        """Post-process FAIL2FAIL: keep only per-test FAIL2PASS + PASS2PASS tests.

        Parses pytest -v output from the last test run to identify individual
        test results.  If some tests achieved FAIL→PASS, filters out the
        failing tests and rebuilds test_patch + eval.sh without re-running
        Docker.  Returns True if filtering salvaged at least one FAIL2PASS
        test, False otherwise.
        """
        from app.agents.write_test_agent.write_test_utils import build_patch_from_files
        from app.agents.write_eval_script_agent.write_eval_script_utils import _generate_cat_heredoc_block

        _ta: TestAnalysisAgent = self.agents_dict["test_analysis_agent"]  # type: ignore[assignment]
        pre_output = _ta.get_latest_prev_test_log()
        post_output = _ta.get_latest_test_log()
        if not pre_output or not post_output:
            return False

        pre_results = parse_per_test_results(pre_output)
        post_results = parse_per_test_results(post_output)
        if not pre_results or not post_results:
            return False

        per_test = classify_per_test_f2p(pre_results, post_results)
        if not per_test:
            return False

        f2p_tests = {tid for tid, cls in per_test.items() if cls == "FAIL2PASS"}
        if not f2p_tests:
            return False

        # Collect test names to keep (FAIL2PASS + PASS2PASS), grouped by file
        keep_by_file: dict[str, set[str]] = {}
        for tid, cls in per_test.items():
            if cls in ("FAIL2PASS", "PASS2PASS"):
                # tid format: "tests/file.py::test_name" or "tests/file.py::Class::test_name"
                parts = tid.split("::")
                filepath = parts[0]
                test_name = parts[-1]  # last component is the function name
                keep_by_file.setdefault(filepath, set()).add(test_name)

        # Filter test file contents
        wt_agent: WriteTestAgent | None = self.agents_dict.get("write_test_agent")  # type: ignore[assignment]
        if wt_agent is None:
            return False
        original_contents = wt_agent.get_generated_test_file_contents()
        if not original_contents:
            return False

        filtered_contents: dict[str, str] = {}
        for filepath, source in original_contents.items():
            if filepath in keep_by_file:
                filtered = filter_test_file_by_names(source, keep_by_file[filepath])
                if filtered:
                    filtered_contents[filepath] = filtered
            else:
                # File has no tests in per-test results (maybe never ran);
                # keep it only if all its tests were PASS2PASS or unknown
                drop_names = set()
                for tid, cls in per_test.items():
                    if tid.startswith(filepath + "::") and cls in ("FAIL2FAIL", "PASS2FAIL"):
                        drop_names.add(tid.split("::")[-1])
                if not drop_names:
                    filtered_contents[filepath] = source
                else:
                    keep_names = set()
                    for tid, cls in per_test.items():
                        if tid.startswith(filepath + "::") and cls in ("FAIL2PASS", "PASS2PASS"):
                            keep_names.add(tid.split("::")[-1])
                    if keep_names:
                        filtered = filter_test_file_by_names(source, keep_names)
                        if filtered:
                            filtered_contents[filepath] = filtered

        if not filtered_contents:
            return False

        # Rebuild test_patch
        filter_dir = os.path.join(self.output_dir, "per_test_filtered")
        patch_str, file_list = build_patch_from_files(filtered_contents, filter_dir)
        if not patch_str:
            return False

        # Rebuild eval.sh from skeleton pattern
        _eval_agent: WriteEvalScriptAgent = self.agents_dict["write_eval_script_agent"]  # type: ignore[assignment]
        heredoc_block = _generate_cat_heredoc_block(filtered_contents)

        from swe_factory_utils import REPO_EVAL_CONFIG, DEFAULT_REPO_EVAL_CONFIG
        repo_cfg = REPO_EVAL_CONFIG.get(self.task.repo_name, DEFAULT_REPO_EVAL_CONFIG)
        test_files_str = " ".join(f'"{f}"' for f in filtered_contents.keys())
        pytest_cmd = repo_cfg["pytest_cmd"].replace("{files}", test_files_str)

        eval_lines = [
            "#!/bin/bash",
            "set -uxo pipefail",
            f"cd {repo_cfg['workdir']}",
            'export PYTEST_ADDOPTS="--override-ini=addopts="',
            heredoc_block,
            f"{pytest_cmd}",
        ]
        new_eval_script = "\n".join(eval_lines) + "\n"

        # Update agent states
        wt_agent.generated_test_file_contents = filtered_contents
        _eval_agent.test_patch = patch_str
        _eval_agent.test_files_content = filtered_contents

        # Write filtered eval.sh to the eval agent's output dir so get_latest_eval_script works
        eval_dir = _eval_agent.get_latest_write_output_dir()
        os.makedirs(eval_dir, exist_ok=True)
        with open(os.path.join(eval_dir, "eval.sh"), "w") as f:
            f.write(new_eval_script)

        # Update task info
        self.task.task_info["test_patch"] = patch_str

        # Build FAIL_TO_PASS / PASS_TO_PASS lists
        fail_to_pass = sorted(tid for tid, cls in per_test.items() if cls == "FAIL2PASS")
        pass_to_pass = sorted(tid for tid, cls in per_test.items() if cls == "PASS2PASS")
        self.task.task_info["FAIL_TO_PASS"] = json.dumps(fail_to_pass)
        self.task.task_info["PASS_TO_PASS"] = json.dumps(pass_to_pass)

        # Mark success
        _ta.f2p_classification = "FAIL2PASS"
        self.workflow_finish_status = True

        logger.info(
            f"Per-test filtering: kept {len(f2p_tests)} FAIL2PASS + "
            f"{sum(1 for c in per_test.values() if c == 'PASS2PASS')} PASS2PASS tests, "
            f"dropped {sum(1 for c in per_test.values() if c in ('FAIL2FAIL', 'PASS2FAIL'))} tests."
        )
        return True

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
            # Save to shared cache so other instances with same repo+version can reuse
            self._cache_docker_image(image_name, dockerfile)
            return None
        except Exception as e:
            build_logger.error(f"Dockerfile build failed: {e}")
            return str(e)
        finally:
            close_logger(build_logger)

    def run_workflow(self) -> None:
        # Try reusing a cached Docker image for this repo+version before entering the loop
        self._try_reuse_cached_image()

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
                # Pass test file source code so TestAnalysisAgent can diagnose assertion quality
                if "write_test_agent" in self.agents_dict:
                    _wt: WriteTestAgent = self.agents_dict["write_test_agent"]  # type: ignore[assignment]
                    _analysis_agent.test_file_contents = _wt.get_generated_test_file_contents()

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
                            self.task, self.output_dir, self.repo_basic_info_slim
                        )
                    if self.needs_test_generation:
                        self.set_agent_status("write_test_agent", False)
                        self.set_agent_status("write_eval_script_agent", False)
                        test_agent: WriteTestAgent = self.agents_dict["write_test_agent"]  # type: ignore[assignment]

                        # Build richer context for retry: previous test code + pre-patch log
                        prev_test_files = ""
                        if hasattr(test_agent, "generated_test_file_contents") and test_agent.generated_test_file_contents:
                            prev_test_files = "\n\n".join(
                                f"### {path}\n```python\n{content}\n```"
                                for path, content in test_agent.generated_test_file_contents.items()
                            )

                        prev_test_log = ""
                        _ta: TestAnalysisAgent = self.agents_dict["test_analysis_agent"]  # type: ignore[assignment]
                        raw_log = _ta.get_latest_prev_test_log()
                        if raw_log:
                            lines = raw_log.splitlines()[:100]
                            prev_test_log = "\n".join(lines)

                        test_agent.pending_guidance = (test_agent.pending_guidance or "") + (
                            f"The generated tests need improvement:\n{guidance}\n\n"
                            + (f"## Previous test files\n{prev_test_files}\n\n" if prev_test_files else "")
                            + (f"## Pre-patch test output (first 100 lines)\n```\n{prev_test_log}\n```\n\n" if prev_test_log else "")
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

        # Per-test filtering: salvage FAIL2PASS tests from overall FAIL2FAIL
        if not self.workflow_finish_status:
            f2p = getattr(self.agents_dict.get("test_analysis_agent"), "f2p_classification", None)
            if f2p == "FAIL2FAIL" and self._try_per_test_filtering():
                logger.info("Per-test filtering salvaged FAIL2PASS tests from FAIL2FAIL run.")

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
