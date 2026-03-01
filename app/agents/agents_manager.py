from app.data_structures import MessageThread
from app.task import Task
from app.agents.write_dockerfile_agent import WriteDockerfileAgent
from app.agents.write_dockerfile_agent.write_dockerfile_utils import get_repo_env_template
from app.agents.test_analysis_agent import TestAnalysisAgent
from app.agents.write_eval_script_agent import WriteEvalScriptAgent
from app.agents.context_retrieval_agent import ContextRetrievalAgent
from app.agents.write_test_agent import WriteTestAgent
import os
import re
import docker
from datetime import datetime
from app.model import common
from os.path import join as pjoin
from loguru import logger
from packaging import version
import json
import random
from filelock import FileLock
from copy import deepcopy

DIFF_MODIFIED_FILE_REGEX = r"--- a/(.*)"
DIFF_DEVNULL_REGEX = r"--- /dev/null\n\+\+\+ b/(.*)"
def normalize_version(ver_str):
    match = re.search(r"(\d+(?:\.\d+){0,2})", ver_str)
    return match.group(1) if match else ver_str

def get_closest_version_info(records, repo, target_version):
    same_repo = [r for r in records if r.get('repo') == repo]
    if not same_repo:
        return None
    ver_map = {r['version']: normalize_version(r['version']) for r in same_repo}
    try:
        sorted_list = sorted(same_repo, key=lambda r: version.parse(ver_map[r['version']]))
        target_parsed = version.parse(normalize_version(target_version))
    except Exception:
        sorted_list = sorted(same_repo, key=lambda r: r['version'])
        target_parsed = version.parse(normalize_version(target_version))
    exact_matches = [r for r in same_repo if r['version'] == target_version]
    if exact_matches:
        return random.choice(exact_matches)
    candidates = [r for r in sorted_list if version.parse(ver_map[r['version']]) <= target_parsed]
    return random.choice(candidates) if candidates else None

class AgentsManager:
    """
    Simple manager to orchestrate LLM-based agents.
    """
    def __init__(self,
                task: Task,
                output_dir: str,
                client: docker.DockerClient,
                start_time: datetime,
                max_iteration_num: int,
                results_path:str,
                disable_memory_pool:bool,
                disable_run_test:bool,
                disable_download_test_resources:bool,
                using_ubuntu_only:bool,
                ):
        self.task = task
        self.output_dir = os.path.abspath(output_dir)
        self.run_count = 0
        self.client = client
        self.max_iteration_num  = max_iteration_num
        self.start_time = start_time
        
        self.test_files = self.get_test_files()
        self.repo_basic_info = self.get_repository_basic_info()
        self.workflow_finish_status  = False
        # Initialize agents
        self.agents_dict = {
            "write_docker_agent": WriteDockerfileAgent(task, output_dir, self.repo_basic_info,using_ubuntu_only),
            "write_eval_script_agent": WriteEvalScriptAgent(task, output_dir, self.repo_basic_info,disable_download_test_resources),
            "test_analysis_agent": TestAnalysisAgent(task, output_dir, self.repo_basic_info, client),
            "context_retrieval_agent": ContextRetrievalAgent(task, output_dir, self.repo_basic_info),
        }
        self.set_agent_status('all',False)
        # Trigger test generation when test_patch is empty/missing OR fewer than 3 test files
        self.needs_test_generation = (
            not (self.task.test_patch or "").strip() or len(self.test_files) < 3
        )
        if self.needs_test_generation:
            self.agents_dict["write_test_agent"] = WriteTestAgent(task, output_dir, self.repo_basic_info)
            self.set_agent_status("write_test_agent", False)
        self.disable_memory_pool = disable_memory_pool
        self.disable_run_test = disable_run_test
        self.disable_download_test_resources = disable_download_test_resources
        self.agents_dict['test_analysis_agent'].disable_run_test = disable_run_test
        self.results_file = f'{results_path}/results.json'
        lock_path = self.results_file + '.lock'
        self.lock = FileLock(lock_path, timeout=30)
        with self.lock:
            if not os.path.exists(self.results_file):
                with open(self.results_file, 'w') as f:
                    json.dump([], f, indent=2)

    def set_agent_status(self, agent_name: str, status: bool):
        """Set the status of an agent to control if it's active or inactive."""
        if agent_name == 'all':
            for agent_key, agent_value in self.agents_dict.items():
                agent_value.finish_status = status  

        elif agent_name in self.agents_dict:
            agent = self.agents_dict[agent_name]
            agent.finish_status = status  
        else:
            logger.error(f"Agent {agent_name} not found!")

    def get_agent_status(self, agent_name: str) -> bool:
        """Get the current status of an agent."""
        if agent_name in self.agents_dict:
            return self.agents_dict[agent_name].finish_status
        else:
            logger.error(f"Agent {agent_name} not found!")
            return False

    def set_agents_iteration_num(self, iteration_num: int) -> None:
        """Get the current status of an agent."""
        for agent_key, agent_value in self.agents_dict.items():
            
            agent_value.iteration_num = iteration_num 
            
    def get_test_files(self) -> list[str]:
        """
        1) Extract modified/deleted files via '--- a/...'
        2) Extract newly added files via the '/dev/null' pattern
        3) Return combined list in patch order (no dedup)
        """
        patch = self.task.test_patch or ""

        old_paths = re.findall(DIFF_MODIFIED_FILE_REGEX, patch)
        new_paths = re.findall(DIFF_DEVNULL_REGEX,   patch)

        # simply concatenate; if duplicates truly don't happen, this is fine
        return old_paths + new_paths
    
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

    def dump_cost(
        self
    ):
        start_time = self.start_time
        end_time = datetime.now()
        task_output_dir = self.output_dir
        project_path  = self.task.project_path
      
        model_stats = common.SELECTED_MODEL.get_overall_exec_stats()
        stats = {
            # "commit": commit_hash,
            "start_epoch": start_time.timestamp(),
            "end_epoch": end_time.timestamp(),
            "elapsed_seconds": (end_time - start_time).total_seconds(),
        }
        stats.update(model_stats)

        with open(pjoin(task_output_dir, "cost.json"), "w") as f:
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

    def get_latest_reference_setup_for_repo(self):
        records = self._read_results()
        return get_closest_version_info(records, self.task.repo_name, self.task.version)

    def run_workflow(self) -> None:
        for iteration_num in range(self.max_iteration_num):
            self.set_agents_iteration_num(iteration_num)

            if not self.get_agent_status("context_retrieval_agent"):
                collected_information, summary, success =  self.agents_dict['context_retrieval_agent'].run_task()
                self.dump_cost()
                if collected_information != None:
                    self.set_agent_status("context_retrieval_agent",True)
                    self.agents_dict['write_eval_script_agent'].add_user_message(collected_information)
                    self.agents_dict['write_docker_agent'].add_user_message(collected_information)
                    if self.needs_test_generation:
                        self.agents_dict['write_test_agent'].add_user_message(collected_information)

            # Run WriteTestAgent if test_patch is empty and context retrieval is done
            if self.needs_test_generation and self.get_agent_status("context_retrieval_agent") \
                    and not self.get_agent_status("write_test_agent"):
                _, _, success = self.agents_dict['write_test_agent'].run_task()
                self.dump_cost()
                if success:
                    self.set_agent_status("write_test_agent", True)
                    # Inject generated test_patch into WriteEvalScriptAgent
                    gen_patch = self.agents_dict['write_test_agent'].get_generated_test_patch()
                    gen_files = self.agents_dict['write_test_agent'].get_generated_test_files()
                    eval_agent = self.agents_dict['write_eval_script_agent']
                    # Combine existing test_patch with generated patch
                    existing_patch = eval_agent.test_patch.strip()
                    if existing_patch:
                        eval_agent.test_patch = existing_patch + "\n" + gen_patch
                        eval_agent.test_files = eval_agent.test_files + gen_files
                    else:
                        eval_agent.test_patch = gen_patch
                        eval_agent.test_files = gen_files
                    eval_agent.generated_test_files = gen_files
                    eval_agent.initial_skeleton = eval_agent.get_initial_eval_script_skeleton()

            if self.disable_memory_pool == False:        
                reference_setup = self.get_latest_reference_setup_for_repo()
                if reference_setup:
                    self.agents_dict['write_docker_agent'].reference_setup = reference_setup
                    
                    self.agents_dict['write_eval_script_agent'].reference_setup = reference_setup

            if self.get_agent_status("context_retrieval_agent") and not self.get_agent_status("write_docker_agent"):
                _, _, success =  self.agents_dict['write_docker_agent'].run_task()
                self.dump_cost()
                if success:
                    self.set_agent_status("write_docker_agent",True)
          

            test_gen_ready = (not self.needs_test_generation or self.get_agent_status("write_test_agent"))
            if self.get_agent_status("context_retrieval_agent") and self.get_agent_status("write_docker_agent") \
                    and test_gen_ready and not self.get_agent_status("write_eval_script_agent"):
                self.agents_dict['write_eval_script_agent'].dockerfile =  self.agents_dict['write_docker_agent'].get_latest_dockerfile()
                _, _, success =  self.agents_dict['write_eval_script_agent'].run_task()
                self.dump_cost()
                if success:
                    self.set_agent_status("write_eval_script_agent",True)
                
            if self.get_agent_status("context_retrieval_agent") and self.get_agent_status("write_docker_agent") and self.get_agent_status("write_eval_script_agent"):
                dockerfile = self.agents_dict['write_docker_agent'].get_latest_dockerfile()
                eval_script_skeleton = self.agents_dict['write_eval_script_agent'].get_latest_eval_script_skeleton()
                eval_script= self.agents_dict['write_eval_script_agent'].get_latest_eval_script()
                self.agents_dict['test_analysis_agent'].dockerfile = dockerfile
                self.agents_dict['test_analysis_agent'].eval_script_skeleton = eval_script_skeleton
                self.agents_dict['test_analysis_agent'].eval_script = eval_script
                # analysis, _, success =  self.agents_dict['test_analysis_agent'].run_task()
              
                analysis, _, success = self.agents_dict['test_analysis_agent'].run_task()
                self.dump_cost()
                if isinstance(analysis, str):
                    try:
                        analysis = json.loads(analysis)
                    except (json.JSONDecodeError, ValueError, TypeError):
                        analysis = {}
                elif not isinstance(analysis, dict):
                    analysis = {}


                is_finish = analysis.get("is_finish", None)

                if is_finish:
                    self.workflow_finish_status = True
                    break

                # is_finish is False/None — route guidance to agents
                guidance = analysis.get("guidance_for_context_retrieval_agent")
                if guidance:
                    self.set_agent_status("context_retrieval_agent", False)
                    self.agents_dict['context_retrieval_agent'].add_user_message(
                        f"The test analysis agent found additional context is needed:\n{guidance}\n\n")

                guidance = analysis.get("guidance_for_write_dockerfile_agent")
                if guidance:
                    self.set_agent_status("write_docker_agent", False)
                    agent = self.agents_dict['write_docker_agent']
                    text = f"The test analysis agent found a problem with the Dockerfile:\n{guidance}\n\n"
                    agent.pending_guidance = (agent.pending_guidance or "") + text

                guidance = analysis.get("guidance_for_write_eval_script_agent")
                if guidance:
                    self.set_agent_status("write_eval_script_agent", False)
                    agent = self.agents_dict['write_eval_script_agent']
                    text = f"The test analysis agent found a problem with the eval script:\n{guidance}\n\n"
                    agent.pending_guidance = (agent.pending_guidance or "") + text

                guidance = analysis.get("guidance_for_write_test_agent")
                if guidance:
                    if "write_test_agent" not in self.agents_dict:
                        self.needs_test_generation = True
                        self.agents_dict["write_test_agent"] = WriteTestAgent(
                            self.task, self.output_dir, self.repo_basic_info)
                    if self.needs_test_generation:
                        self.set_agent_status("write_test_agent", False)
                        self.set_agent_status("write_eval_script_agent", False)
                        agent = self.agents_dict['write_test_agent']
                        text = f"The generated tests need improvement:\n{guidance}\n\n"
                        agent.pending_guidance = (agent.pending_guidance or "") + text

        else:
            log_msg = "Exceed largest number of tries.."
            logger.info(f"Too many rounds. {log_msg}")

        dockerfile_content = self.agents_dict['write_docker_agent'].get_latest_dockerfile()
        eval_script_content = self.agents_dict['write_eval_script_agent'].get_latest_eval_script()
        eval_script_skeleton_content = self.agents_dict['write_eval_script_agent'].get_latest_eval_script_skeleton()
        if dockerfile_content and eval_script_content:
            with open(os.path.join(self.output_dir, "Dockerfile"), "w") as dockerfile_f:
                dockerfile_f.write(dockerfile_content)

        
            with open(os.path.join(self.output_dir, "eval.sh"), "w") as eval_script_f:
                eval_script_f.write(eval_script_content)


        f2p_result = getattr(self.agents_dict.get('test_analysis_agent'), 'f2p_classification', None)
        status_data = {"is_finish": self.workflow_finish_status}
        if f2p_result:
            status_data["f2p_classification"] = f2p_result
        with open(os.path.join(self.output_dir, "status.json"), "w") as status_file_f:
                json.dump(status_data, status_file_f)

        if self.workflow_finish_status:
            recs = self._read_results()
            info = deepcopy(self.task.task_info)

            # merge in your new fields
            info.update({
                "dockerfile": dockerfile_content,
                "eval_script": eval_script_content,
                "eval_script_skeleton": eval_script_skeleton_content,
                # keep any other existing keys from task_info
            })

            recs.append(info)
            self._write_results(recs)
        
