from app.data_structures import MessageThread
from app.agents.write_dockerfile_agent import write_dockerfile_utils
from app.agents.agent import Agent
from app.task import SweTask
import os
import shutil
from loguru import logger
from app.log import print_banner
from os.path import join as pjoin


class WriteDockerfileAgent(Agent):
    """
    LLM-based agent for creating or modifying a Dockerfile via direct chat.
    Always operates in instance-layer mode (FROM pre-built base image).
    """
    api_functions: list[str] = []

    def __init__(self, task: SweTask, output_dir: str, repo_basic_info: str):
        super().__init__(agent_id="WriteDockerfileAgent")
        self.task = task
        self.output_dir = os.path.abspath(output_dir)
        self.run_count = 0
        self.repo_basic_info = repo_basic_info
        self.pending_guidance: str | None = None
        self.init_msg_thread()

    def init_msg_thread(self) -> None:
        self.msg_thread = MessageThread()
        self.add_system_message(write_dockerfile_utils.get_system_prompt_dockerfile(instance_layer=True))
        self.add_user_message(self.repo_basic_info)

    def run_task(self, print_callback=None) -> tuple[str, str, bool]:
        """
        Generate or refine the instance-layer Dockerfile.
        Returns raw_output, summary, success.
        """
        if self.run_count > 0:
            self.init_msg_thread()
            if self.pending_guidance:
                self.add_user_message(self.pending_guidance)
                self.pending_guidance = None

        print_banner(f"Iteration ROUND {self.iteration_num}: Dockerfile Generation")
        prev_dir = self.get_latest_write_dockerfile_output_dir()
        prev_file = os.path.join(prev_dir, "Dockerfile")
        self.run_count += 1
        curr_dir = self.get_latest_write_dockerfile_output_dir()
        os.makedirs(curr_dir, exist_ok=True)

        if os.path.exists(prev_file):
            prev_content = self._read_file(prev_file)
            self.add_user_message(f"Previous dockerfile:\n{prev_content}\n")
            self.add_user_message(write_dockerfile_utils.get_user_prompt_modify_dockerfile())
        else:
            self.add_user_message(write_dockerfile_utils.get_user_prompt_instance_layer_dockerfile(
                base_image=self.task.base_image,
                base_commit=self.task.commit,
            ))

        task_output = write_dockerfile_utils.write_dockerfile_with_retries(
            self.msg_thread,
            curr_dir,
            print_callback=print_callback,
        )

        dockerfile_path = os.path.join(curr_dir, "Dockerfile")
        if not os.path.isfile(dockerfile_path):
            if os.path.exists(prev_file):
                shutil.copy(prev_file, dockerfile_path)
            summary = "Dockerfile generation failed."
            is_ok = False
        else:
            summary = "Dockerfile created/updated successfully."
            is_ok = True

        conversation_file = pjoin(curr_dir, "conversation.json")
        self.msg_thread.save_to_file(conversation_file)
        return task_output, summary, is_ok

    def _read_file(self, path: str) -> str:
        try:
            with open(path, "r") as f:
                return f.read()
        except Exception:
            return ""

    def get_latest_write_dockerfile_output_dir(self) -> str:
        return os.path.join(self.output_dir, f"write_dockerfile_agent_{self.run_count}")

    def get_latest_dockerfile(self) -> str:
        path = os.path.join(self.get_latest_write_dockerfile_output_dir(), "Dockerfile")
        try:
            with open(path, "r") as f:
                return f.read()
        except Exception as e:
            logger.error(f"Failed to read latest Dockerfile at {path}: {e}")
            return ""
