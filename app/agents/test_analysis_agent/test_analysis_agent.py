from pathlib import Path
import os
from loguru import logger
from app.agents.agent import Agent
from app.data_structures import FunctionCallIntent, MessageThread
from app.task import SweTask
from app.agents.test_analysis_agent import test_analysis_utils
from app.agents.test_analysis_agent.docker_utils  import (
    cleanup_container,
    remove_image,
    copy_to_container,
    exec_run_with_timeout,
    BuildImageError,
    build_container,
    EvaluationError)
import docker
import re
from app.log import log_exception,setup_logger,close_logger
from app.log import (
    print_acr,
    print_banner,
    print_retrieval,
)
import json
from os.path import join as pjoin
import traceback
from swe_factory_utils import (
    extract_exit_code as _extract_exit_code,
    classify_f2p,
    ensure_essentials_in_dockerfile as _ensure_essentials_in_dockerfile,
)
MAX_LINE_NUM = 600
ansi_escape = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
class TestAnalysisAgent(Agent):
    """
    Agent responsible for:
      1. Loading the latest test_output.txt
      2. Formatting it with line numbers and truncation
      3. Sending it to the test-log-analysis utility (agent_analyze_test_log)
    """
    api_functions = ["setup_docker_and_run_test"]

    def __init__(self, task: SweTask, output_dir: str, repo_basic_info: str, client:docker.DockerClient):
        super().__init__(agent_id=self.__class__.__name__)
        self.msg_thread  = MessageThread()
        self.task = task
        self.output_dir = os.path.abspath(output_dir)
        self.analysis_count = 0
        self.run_test_num = 0
        self.setup_dockerfile_num = 0
        self.repo_basic_info = repo_basic_info
        self.task_id = task.task_id.lower()
        self.client = client
        self.test_analysis_dir = os.path.join(self.output_dir, "test_analysis_agent") 
        # self.build_image_dir = os.path.join(self.output_dir, "build_image") 
        # self.run_test_dir = os.path.join(self.output_dir, "run_test") 
        self.eval_script_skeleton: str | None = None
        self.dockerfile: str | None = None
        self.eval_script: str | None = None
        self.timeout = 3600
        self.disable_run_test = False
        self.f2p_classification: str | None = None
        self._cached_image_name: str | None = None   # image tag of last successfully built image
        self._cached_dockerfile: str | None = None   # dockerfile content used for that build
        # self.init_msg_thread()



    def init_msg_thread(self) -> None:
        """
        Reset the message thread and inject the base prompt before each run_task.
        """
        self.msg_thread = MessageThread()
        self.add_system_message(test_analysis_utils.SYSTEM_PROMPT)
        # Inject repository basic information
        self.add_user_message(self.repo_basic_info)
        self.add_user_message(f'The current dockerfile used to setup environemnt:\n{self.dockerfile}')
        self.add_user_message(f'The current eval script (omit test patch to decrease length) used to run tests:\n{self.eval_script_skeleton}')

    def get_latest_test_analysis_output_dir(self):
        output_dir = f'{self.test_analysis_dir}_{self.analysis_count}'
        return output_dir

    def get_latest_test_log(self) -> str:
        """Read the latest test_output.txt produced by run_test."""
        test_dir = self.get_latest_test_analysis_output_dir()
        path = os.path.join(test_dir, "test_output.txt")
        try:
            return Path(path).read_text()
        except FileNotFoundError:
            return ""
    


    def get_test_log_with_line_numbers(self) -> str:
        test_log = self.get_latest_test_log()
        lines = test_log.splitlines()
        
       
        width = len(str(len(lines)))
        full_formatted = [f"{i + 1:>{width}}   {line}" for i, line in enumerate(lines)]
        
        if len(full_formatted) <= MAX_LINE_NUM:
            log_body = "\n".join(full_formatted)
            return f'Test log:\n{log_body}\n\n'

        
        head_size = MAX_LINE_NUM // 2
        tail_size = MAX_LINE_NUM - head_size
        
        head = full_formatted[:head_size]
        tail = full_formatted[-tail_size:]
        
       
        omission = " " * width + "   [..., {} lines omitted ...]".format(
            len(full_formatted) - head_size - tail_size)
        
        truncated_log = "\n".join(head + [omission] + tail)
        
        return f'Test log (showing first {head_size} & last {tail_size} lines):\n{truncated_log}\n\n'

    def get_latest_prev_test_log(self) -> str:
        """Read the latest test_output_prev_apply.txt produced by pre-patch run."""
        test_dir = self.get_latest_test_analysis_output_dir()
        path = os.path.join(test_dir, "test_output_prev_apply.txt")
        try:
            return Path(path).read_text()
        except FileNotFoundError:
            return ""

    def get_prev_test_log_with_line_numbers(self) -> str:
        test_log = self.get_latest_prev_test_log()
        if not test_log:
            return ""
        lines = test_log.splitlines()
        width = len(str(len(lines)))
        full_formatted = [f"{i + 1:>{width}}   {line}" for i, line in enumerate(lines)]

        if len(full_formatted) <= MAX_LINE_NUM:
            log_body = "\n".join(full_formatted)
            return f'Pre-patch test log (without gold patch applied):\n{log_body}\n\n'

        head_size = MAX_LINE_NUM // 2
        tail_size = MAX_LINE_NUM - head_size
        head = full_formatted[:head_size]
        tail = full_formatted[-tail_size:]
        omission = " " * width + "   [..., {} lines omitted ...]".format(
            len(full_formatted) - head_size - tail_size)
        truncated_log = "\n".join(head + [omission] + tail)
        return f'Pre-patch test log (showing first {head_size} & last {tail_size} lines):\n{truncated_log}\n\n'

    def run_task(self, print_callback=None) -> tuple[str, str, bool]:
        self.init_msg_thread()
        print_banner(f"Task {self.task.task_id} Iteration ROUND {self.iteration_num} "
                     f"Analyzing evaluation environment")

        self.analysis_count += 1
        test_log_output_dir = self.get_latest_test_analysis_output_dir()
        os.makedirs(test_log_output_dir, exist_ok=True)

        build_image_status = False

        # --- Optional: Docker build + test execution ---
        if not self.disable_run_test:
            intent = FunctionCallIntent("setup_docker_and_run_test", {}, None)
            tool_output, _, docker_success = self.dispatch_intent(intent)

            if 'Image built successfully!' not in tool_output:
                print_acr('Build Image Failure!',
                          f"Task {self.task.task_id} Iteration ROUND {self.iteration_num}",
                          print_callback=print_callback)
                self.add_user_message(
                    f'Docker image build failed with these errors:\n{tool_output}\n\n')
            elif docker_success:
                build_image_status = True
                print_acr('Build Image Successfully!',
                          f"Task {self.task.task_id} Iteration ROUND {self.iteration_num}",
                          print_callback=print_callback)
                self.add_user_message(self.get_test_log_with_line_numbers())
                prev_log = self.get_prev_test_log_with_line_numbers()
                if prev_log:
                    self.add_user_message(prev_log)
                if self.f2p_classification:
                    self.add_user_message(
                        f"F2P Validation Result: {self.f2p_classification}\n"
                        "- FAIL2PASS: Tests fail without gold patch and pass with it. Desired outcome.\n"
                        "- PASS2PASS: Tests pass both times. Tests are too weak.\n"
                        "- FAIL2FAIL: Tests fail both times. Environment/setup issue.\n"
                        "- PASS2FAIL: Tests pass without but fail with patch. Tests broken/inverted.\n"
                        "- ERROR: Could not determine exit codes.\n")
            else:
                logger.error(tool_output)
                return None, f'{self.agent_id} fails, Docker error', False

        # --- LLM analysis ---
        print_acr(f'Task {self.task.task_id} Iteration ROUND {self.iteration_num} Analyzing',
                  f"Task {self.task.task_id} Iteration ROUND {self.iteration_num} analysis",
                  print_callback=print_callback)

        analysis = test_analysis_utils.run_with_retries(
            self.msg_thread, print_callback=print_callback)
        task_output = analysis

        # --- Save ---
        analysis_file = Path(f"{test_log_output_dir}/analysis.json")
        to_save = {}
        if isinstance(analysis, dict):
            to_save = analysis
        elif isinstance(analysis, str):
            try:
                to_save = json.loads(analysis)
            except Exception:
                to_save = {}

        to_save['build_image_status'] = build_image_status
        if self.f2p_classification:
            to_save['f2p_classification'] = self.f2p_classification

        success = task_output is not None
        summary = ("Analysis completed." if success
                   else "Analysis returned nothing.")

        with analysis_file.open("w", encoding="utf-8") as f:
            json.dump(to_save, f, ensure_ascii=False, indent=2)
        self.msg_thread.save_to_file(pjoin(test_log_output_dir, "conversation.json"))

        return task_output, summary, success
       

    def build_docker_image(
        self,
        dockerfile,
        cur_build_image_dir,
        task_id,
        image_name,
        build_image_logger,
        client
    ):
        """Build Docker image with detailed logging and error handling."""
        build_image_logger.info(
            f"Building image {task_id}\n"
            f"Using dockerfile:\n{dockerfile}\n"
        )

    

        if self.setup_dockerfile_num > 1:
            prev_image_name = f"{self.task_id}-dockerfile{self.setup_dockerfile_num-1}:latest"
            # Don't delete the cached image — it may be reused in future rounds
            if prev_image_name != self._cached_image_name:
                try:
                    client.images.remove(prev_image_name, force=True)
                    build_image_logger.info(f"Deleted previous image: {prev_image_name}")
                except docker.errors.ImageNotFound:
                    build_image_logger.info(f"Do not find previous image, images list is clean.")
                except Exception as e:
                    build_image_logger.error(f"Failed to delete previous image {prev_image_name}: {str(e)}")
        
        

        dockerfile_path = f'{cur_build_image_dir}/Dockerfile'
        # Ensure essential tools (curl, git, ca-certificates) are installed
        # before any command that needs them. LLMs often generate
        # `RUN curl ...` before `apt-get install curl`.
        dockerfile = _ensure_essentials_in_dockerfile(dockerfile)

        # Inject ARG GITHUB_TOKEN so the build can authenticate for private repos.
        # The actual token is passed via buildargs (not written to the Dockerfile on disk).
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if token and "github.com" in dockerfile:
            lines = dockerfile.split("\n")
            out: list[str] = []
            arg_inserted = False
            for line in lines:
                out.append(line)
                if not arg_inserted and line.strip().upper().startswith("FROM "):
                    out.append("ARG GITHUB_TOKEN")
                    arg_inserted = True
            dockerfile = "\n".join(out)
            # Rewrite clone URLs to use the build arg
            dockerfile = dockerfile.replace(
                "https://github.com/",
                "https://x-access-token:${GITHUB_TOKEN}@github.com/",
            )

        with open(dockerfile_path, "w") as f:
            f.write(dockerfile)

        buildargs = {}
        if token:
            buildargs["GITHUB_TOKEN"] = token

        command_output = []
        capturing = False
        response = client.api.build(
            path=cur_build_image_dir,
            tag=image_name,
            rm=True,
            forcerm=True,
            decode=True,
            platform="linux/x86_64",
            nocache=True,
            buildargs=buildargs or None,
        )

        buffer = ""

       
        for chunk in response:
            if "stream" in chunk:
              
                buffer += ansi_escape.sub("", chunk["stream"]).replace("\r\n", "\n").replace("\r", "\n")
                
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if not line.strip():
                        continue

                    
                    if line.startswith("Step "):
                        last_command = line
                        command_output = [line]
                        capturing = True
                    elif capturing:
                        command_output.append(line)

                 
                    build_image_logger.info(line)

            elif "errorDetail" in chunk and capturing:
               
                if buffer.strip():
                    command_output.append(buffer.strip())
                    build_image_logger.info(buffer.strip())
                    buffer = ""

             
                error_msg = ansi_escape.sub("", chunk["errorDetail"]["message"])
                build_image_logger.error(f"Error: {error_msg}")
                command_output.append(f"Error: {error_msg}")

             
                raise docker.errors.BuildError(error_msg, build_log=command_output)

      
        if buffer.strip():
            build_image_logger.info(buffer.strip())

        build_image_logger.info("Image built successfully!")
    def setup_docker_and_run_test(
        self
    ) -> tuple[str, str, bool]:
        dockerfile = self.dockerfile
        eval_script = self.eval_script
        tool_output = ""
        summary = ""
        success = False
        self.setup_dockerfile_num += 1
        cur_build_image_dir = self.get_latest_test_analysis_output_dir()
        os.makedirs(cur_build_image_dir, exist_ok=True)
        build_image_logger = setup_logger(self.task_id, Path(f'{cur_build_image_dir}/build_image.log'))
        image_name = f"{self.task_id}-dockerfile{self.setup_dockerfile_num}:latest"

        # Reuse cached image when the Dockerfile hasn't changed
        dockerfile_changed = (dockerfile != self._cached_dockerfile)
        if not dockerfile_changed and self._cached_image_name:
            build_image_logger.info(
                f"Dockerfile unchanged — reusing cached image {self._cached_image_name}"
            )
            image_name = self._cached_image_name
            tool_output += "Image built successfully!\n"
            summary += f"Docker image {image_name} reused (Dockerfile unchanged).\n"
            close_logger(build_image_logger)
        else:
            try:
                self.build_docker_image(dockerfile,
                                        cur_build_image_dir,
                                        self.task_id,
                                        image_name,
                                        build_image_logger,
                                        self.client)
                self._cached_image_name = image_name
                self._cached_dockerfile = dockerfile
                tool_output += "Image built successfully!\n"
                summary += f"Docker image {image_name} built successfully.\n"
            except docker.errors.BuildError as e:
                build_log = e.build_log
                if len(build_log) > MAX_LINE_NUM:
                    half = MAX_LINE_NUM // 2
                    skipped = len(build_log) - MAX_LINE_NUM
                    build_log = (
                        build_log[:half]
                        + [f"...skipped {skipped} lines..."]
                        + build_log[-half:]
                    )
                tool_output += "\n".join(build_log)
                build_image_logger.error(e)
                summary += "Failed to build Docker image."
                success = False
                return tool_output, summary, success
            except Exception as e:
                build_image_logger.error(f"Unexpected error: {str(e)}")
                tool_output += f'{str(e)}\n'
                summary += "Unexpected error when building images."
                success = False
                return tool_output, summary, success
            finally:
                close_logger(build_image_logger)

        test_output, test_summary, test_success = self.run_test(eval_script, image_name)
        tool_output += test_output
        summary += test_summary
        success = test_success

        return tool_output, summary, success

    def run_test(self, eval_script: str, image_name: str) -> (str, str, bool):
        tool_output = ""
        summary = ""
        success = False
        patch = self.task.patch
        self.run_test_num += 1
        self.reset_tool_sequence()
        cur_test_dir = self.get_latest_test_analysis_output_dir()
        os.makedirs(cur_test_dir, exist_ok=True)
        run_test_logger = setup_logger(self.task_id, Path(f'{cur_test_dir}/run_test.log'))
        test_image_name = image_name
        test_container_name = f"{self.task_id}-test{self.run_test_num}"
        instance_id = self.task_id
        container = None
        test_output_path = f'{cur_test_dir}/test_output.txt'
        prev_test_output_path = f'{cur_test_dir}/test_output_prev_apply.txt'
        pre_exit_code = None
        post_exit_code = None
        try:
            container = build_container(self.client,test_image_name,test_container_name,instance_id,run_test_logger)

            container.start()
            run_test_logger.info(f"Container for {instance_id} started: {container.id}")
            tool_output += f"Container {container.id} started.\n"
            summary += "Container started.\n"

            # === Phase 1: Pre-patch run (without gold patch) ===
            try:
                run_test_logger.info("=== F2P Phase 1: Running tests WITHOUT gold patch ===")
                eval_file = Path(f"{self.get_latest_test_analysis_output_dir()}/eval.sh")
                eval_file.write_text(eval_script)
                copy_to_container(container, eval_file, Path("/eval.sh"))

                pre_result = exec_run_with_timeout(container, "/bin/bash /eval.sh", timeout=self.timeout)
                pre_test_output = pre_result.decode("utf-8")

                with open(prev_test_output_path, "w") as f:
                    f.write(pre_test_output)
                run_test_logger.info(f"Pre-patch test output written to {prev_test_output_path}")

                pre_exit_code = _extract_exit_code(pre_test_output)
                run_test_logger.info(f"Pre-patch exit code: {pre_exit_code}")
                tool_output += f"Pre-patch test run completed (exit code: {pre_exit_code}).\n"
            except Exception as e:
                run_test_logger.warning(f"Pre-patch test run failed: {e}. Continuing with post-patch run.")
                tool_output += f"Pre-patch test run failed: {e}. Continuing.\n"

            # === Reset container state for post-patch run ===
            run_test_logger.info("Resetting container state for post-patch run...")
            container.exec_run(
                f"git reset --hard {self.task.commit}",
                workdir="/testbed",
                user="root",
            )
            container.exec_run(
                "git clean -fdx",
                workdir="/testbed",
                user="root",
            )
            run_test_logger.info("Container state reset complete.")

            # === Phase 2: Post-patch run (with gold patch) ===
            run_test_logger.info("=== F2P Phase 2: Running tests WITH gold patch ===")
            # Copy model prediction as patch file to container
            patch_file = Path(f"{cur_test_dir}/patch.diff")
            patch_file.write_text(patch or "")
            run_test_logger.info(
                f"Intermediate patch for {instance_id} written to {patch_file}, now applying to container..."
            )
            copy_to_container(container, patch_file, Path("/tmp/patch.diff"))


            # Attempt to apply patch to container
            val = container.exec_run(
                "git apply -p1 -v /tmp/patch.diff",
                workdir="/testbed",
                user="root",
            )
            exit_code = val.exit_code
            output = val.output.decode("utf-8", errors="replace")

            if exit_code != 0:
                run_test_logger.info("Failed to apply patch to container, trying again...")
                run_test_logger.error(f"git apply returned exit_code={exit_code}. Output:\n{output}")
                # try "patch --batch --fuzz=5 -p1 -i {patch_path}" to try again
                val = container.exec_run(
                    "patch --batch --fuzz=5 -p1 -i /tmp/patch.diff",
                    workdir="/testbed",
                    user="root",
                )
                if val.exit_code != 0:
                    run_test_logger.info(f"Apply patch fail:\n{val.output.decode('utf-8')}")
                    raise EvaluationError(
                        instance_id,
                        f"Apply patch fail:\n{val.output.decode('utf-8')}. Check if you apply patch in incorrect directories.",
                        run_test_logger,
                    )
                else:
                    run_test_logger.info(f"Apply patch success:\n{val.output.decode('utf-8')}")
            else:
                run_test_logger.info(f"Apply patch success:\n{val.output.decode('utf-8')}")
            tool_output += "Patch applied successfully.\n"
            summary += "Patch applied.\n"
                    # Get git diff before running eval script
            git_diff_output_before = (
                container.exec_run("git diff", workdir="/testbed").output.decode("utf-8").strip()
            )
            run_test_logger.info(f"Git diff before:\n{git_diff_output_before}")

            # Re-copy eval.sh (in case pre-patch run modified it)
            copy_to_container(container, eval_file, Path("/eval.sh"))

            # Run eval script, write output to logs
            result = exec_run_with_timeout(container, "/bin/bash /eval.sh", timeout=self.timeout)
            test_output = result.decode("utf-8")

            with open(test_output_path, "w") as f:
                f.write(test_output)
            run_test_logger.info(f"Test output for {instance_id} written to {test_output_path}")

            post_exit_code = _extract_exit_code(test_output)
            run_test_logger.info(f"Post-patch exit code: {post_exit_code}")

            # Get git diff after running eval script
            git_diff_output_after = (
                container.exec_run("git diff", workdir="/testbed").output.decode("utf-8").strip()
            )

            # Check if git diff changed after running eval script
            run_test_logger.info(f"Git diff after:\n{git_diff_output_after}")
            if git_diff_output_after != git_diff_output_before:
                run_test_logger.info(f"Git diff changed after running eval script")
                tool_output += "Note: Git diff changed after test execution.\n"
                summary += "Git diff changed.\n"

            # === F2P Classification ===
            self.f2p_classification = classify_f2p(pre_exit_code, post_exit_code)
            run_test_logger.info(f"F2P classification: {self.f2p_classification} (pre={pre_exit_code}, post={post_exit_code})")
            tool_output += f"F2P classification: {self.f2p_classification}\n"

        except EvaluationError as e:
            error_msg = (f"EvaluationError {instance_id}: {e}\n"
                        f"{traceback.format_exc()}\n"
                        f"Check ({run_test_logger.log_file}) for more information.")
            run_test_logger.info(error_msg)
            tool_output += error_msg + "\n"
            summary += "Evaluation error occurred.\n"
            success = False

        except Exception as e:
            error_msg = (f"Error in evaluating model for {instance_id}: {e}\n"
                        f"{traceback.format_exc()}\n"
                        f"Check ({run_test_logger.log_file}) for more information.")
            run_test_logger.info(error_msg)
            tool_output += error_msg + "\n"
            summary += "Unexpected error occurred.\n"
            success = False
        else:
            if not os.path.exists(test_output_path):
                tool_output += "Do not generate test_output.txt. Please check the correctness of dockerfile and eval script.\n"
                summary += 'Fail to obtain test results.'
                success = False
            else:
                tool_output += f"Find test_output.txt! Waiting for analysis. "
                summary += 'Obtain test results successfully.'
                success = True

        finally:
            # Always remove the container, but only remove the image when it
            # won't be reused — i.e. when a new build would replace it next round.
            # We keep the cached image alive so the next round can skip rebuilding.
            cleanup_container(self.client, container, run_test_logger)

            if test_image_name != self._cached_image_name:
                remove_image(self.client, test_image_name, run_test_logger)
            close_logger(run_test_logger)
        self.dump_tool_sequence(self.get_latest_test_analysis_output_dir())
        return tool_output, summary, success
