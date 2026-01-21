# repo_env.py
import os
import time
from dataclasses import dataclass, field
from typing import Dict, Tuple, Any, Optional

import gym
import logging

from inference.agenthub.action import Action
from inference.agenthub.utils.log import get_logger
from inference.agenthub.observation import Observation
from inference.agenthub.runtime.docker import DockerRuntime
from inference.agenthub.agent.commands import ParseCommandBash

cmd_parser = ParseCommandBash()


@dataclass(frozen=True)
class EnvArgs:
    """Configure data sources and setup instructions for the environment in which we solve the tasks."""

    ds: Dict
    repo_path: Optional[str] = None
    docker_image: Optional[str] = None
    root_mode: bool = True
    tool_repo_path: Optional[str] = None


class RepoEnv(gym.Env):
    def __init__(self,
                 args: EnvArgs,
                 logger=None,
                 backend: str = "docker",
                 root_mode: Optional[bool] = None,
                 tool_repo_path: Optional[str] = None,
                 verbose: bool = True,
                 step_timeout: int = 90,
                 reward_timeout: int = 300,
                 scaffold: Optional[str] = None):
        # Get the logger
        if logger is None:
            self.logger = get_logger("RepoEnv")  # Pass the module name for clarity
        else:
            self.logger = logger

        if not verbose:
            self.logger.setLevel(logging.CRITICAL)  # Disable all possible logging
            #logging.getLogger().setLevel(logging.CRITICAL)  # Disable root logger
            #logging.disable(logging.CRITICAL)  # Disable all logging

        self.root_mode = root_mode if root_mode is not None else args.root_mode
        self.tool_repo_path = (
            tool_repo_path if tool_repo_path is not None else args.tool_repo_path
        )
        self.scaffold = scaffold
        self.runtime = DockerRuntime(
            ds=args.ds,
            command=["/bin/bash", "-l"],
            logger=self.logger,
            backend=backend,
            root_mode=self.root_mode,
            tool_repo_path=self.tool_repo_path,
            scaffold=self.scaffold,
        )

        self.args = args
        self.done = False
        self.observation = None
        self.state = None
        self.cmd_parser = ParseCommandBash()
        self.backend = backend
        self.step_timeout = step_timeout
        self.reward_timeout = reward_timeout
        self.logger.info(
            f"Initialized Env: {self.runtime.repo_name} with image: {self.runtime.docker_image}"
        )

    def reset(self) -> Dict[str, Any]:
        """
        Resets the environment and returns an initial observation.
        """
        self.logger.info(f"Resetting RepoEnv ...")
        # close the runtime
        self.runtime.close()
        self.observation = "Environment reset"
        self.state = None
        self.done = False
        # also just recreate env again with the same args
        self.runtime = DockerRuntime(
            ds=self.args.ds,
            command=["/bin/bash", "-l"],
            logger=self.logger,
            backend=self.backend,
            root_mode=self.root_mode,
            tool_repo_path=self.tool_repo_path,
            scaffold=self.scaffold,
        )
        return self.observation  # self.get_observation()

    def add_commands(self, cmd_files: list[str]):
        """
        Adds command files to the environment by parsing them,
        copying them to the Docker container, and making them executable or sourced.

        Args:
            cmd_files: List of paths to command files.
        """
        cmds = []
        for cmd_file in cmd_files:
            # Parse commands from file
            parsed_commands = self.cmd_parser.parse_command_file(cmd_file)
            cmds.extend(parsed_commands)

            # Determine the file extension
            _, ext = os.path.splitext(cmd_file)

            # Get the base name of the command file
            cmd_name = os.path.basename(cmd_file)
            if not self.runtime.swefactory:
                if ext == ".py" or self._is_shebang_script(cmd_file):
                    # Python script or shebang script: copy, strip .py extension if applicable
                    if ext == ".py":
                        container_cmd_name = cmd_name[:-3]  # Remove .py extension
                    else:
                        container_cmd_name = cmd_name
                    container_path = f"/usr/local/bin/{container_cmd_name}"
                    self.runtime.copy_to_container(cmd_file, container_path)
                    self.runtime.run(f"chmod +x {container_path}")

                elif ext == ".sh":
                    # Bash script ending with .sh: copy, chmod, and source it
                    container_cmd_name = cmd_name
                    container_path = f"/usr/local/bin/{container_cmd_name}"
                    self.runtime.copy_to_container(cmd_file, container_path)
                    # self.runtime.run(f"chmod +x {container_path}")
                    # Source the script inside the container
                    self.runtime.run(f"bash -c 'source {container_path}'")

                else:
                    # Bash script without shebang: copy, chmod, and source it
                    container_cmd_name = cmd_name
                    container_path = f"/usr/local/bin/{container_cmd_name}"
                    self.runtime.copy_to_container(cmd_file, container_path)
                    self.runtime.run(f"chmod +x {container_path}")
                    # Source the script inside the container
                    self.runtime.run(f"bash -c 'source {container_path}'")

        # Store the parsed commands for reference
        self.commands = cmds
        self.logger.info(f"Added {len(cmds)} commands to the environment.")

    def _is_shebang_script(self, cmd_file: str) -> bool:
        """
        Checks if the given file starts with a shebang (#!).

        Args:
            cmd_file: Path to the command file.

        Returns:
            True if the file starts with a shebang, False otherwise.
        """
        with open(cmd_file, "r") as file:
            first_line = file.readline().strip()
        return first_line.startswith("#!")

    def run_action(self, action: Action, timeout: int):
        # mini_swe_agent/live_swe_agent: allow raw bash command even if function_name is empty or a virtual "bash"
        if self.scaffold in ["mini_swe_agent", "live_swe_agent"] and (not action.function_name or action.function_name == "bash"):
            start_time = time.time()
            try:
                bash_cmd = action.parameters.get("command", "")
                bash_output, error_code = self.runtime.run(bash_cmd, timeout=timeout, use_timeout_wrapper=True)
            except Exception as e:
                obs = str(e)
                self.logger.error(f"Exception occurred: {obs}")
                error_code = -1
                bash_output = ""
            end_time = time.time()
            return bash_output, error_code, end_time - start_time

        # check for empty or no function call / action (non-mini)
        if not action.function_name:
            return "", 0, 0

        start_time = time.time()
        try:
            # Check if action is in allowed actions/commands
            action_name = action.function_name
            allowed_cmds = [x.name for x in self.commands]
            assert (
                action_name in allowed_cmds
            ), f"Invalid Action: input action must be one of allowed actions \n Allowed actions: {allowed_cmds} \n Input action: {action_name}\t"

            # Run action and return
            bash_cmd = action.to_bashcmd()
            bash_output, error_code = self.runtime.run(bash_cmd, timeout=timeout)
        except Exception as e:
            # Capture the error message as observation
            obs = str(e)
            error = f"Exception occurred: {obs}"
            self.logger.error(error)
            error_code = -1
            bash_output = ""
        end_time = time.time()
        total_time = end_time - start_time
        return bash_output, error_code, total_time

    def step(
        self, action: Action, timeout: int = None,
    ) -> Tuple[Observation, int, bool, Dict[str, Any]]:
        """
        Executes an action (command) in the Docker container.
        Runs an action proposed by the agent in the environment and returns the corresponding output.

        Args:
            action: command to run in bash shell

        Returns:
            observation:  output from container
            reward: Always set to 0
            done: whether task is over
            info: additional information (e.g. debugging information)
        """
        if not timeout:
            timeout = self.step_timeout
        bash_output, error_code, total_time = self.run_action(action, timeout=timeout)
        self.observation = Observation(bash_output, error_code, action, docker_image = self.args.ds['docker_image'])
        reward = self.calculate_reward(self.observation)

        # Mini mode: detect completion tokens in stdout first line.
        if self.scaffold in ["mini_swe_agent", "live_swe_agent"]:
            lines = (bash_output or "").lstrip().splitlines()
            if lines and lines[0].strip() in ["MINI_SWE_AGENT_FINAL_OUTPUT", "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"]:
                self.done = True

        if "finish" in action.function_name.lower() or "submit" in action.function_name.lower():
            self.done = True
        info = {"total_time": total_time}
        return self.observation, reward, self.done, info

    def get_task_instruction(self) -> str:
        """
        Returns the task instructions for the environment.
        """
        return self.runtime.get_task_instruction()

    @property
    def _observation(self) -> Dict[str, Any]:
        return {"output": self.observation}

    @property
    def _state(self) -> Dict[str, Any]:
        return {"state": self.state}

    def setup_action_space(self):
        """add different allowed actions"""
        pass

    def add_actions(self, actions: list[dict]) -> None:
        """add different tools from the agent here"""
        pass
    
    def compute_reward(self, timeout: int = None) -> float:
        """
        Compute the reward for the current state.
        """
        if not timeout:
            timeout = self.reward_timeout
        return self.runtime._calculate_reward(timeout=timeout)

    def calculate_reward(self, obs: Observation) -> int:
        """
        Basic reward calculation based on command success.
        """
        return 0  # TODO

    def check_done(self) -> bool:
        return self.done  # Customize to set completion condition

    def close(self):
        self.runtime.close()

    def get_stats(self) -> Dict[str, Any]:
        """
        Returns the statistics of the environment.
        """
        return self.runtime.ds
