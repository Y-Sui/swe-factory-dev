"""
Dockerfile extraction logic and retry wrapper for WriteDockerfileAgent.
All prompts live in app/prompts/prompts.py.
"""

import json
from collections.abc import Callable
from os.path import join as pjoin
import os
from loguru import logger
from app.data_structures import MessageThread
from app.log import print_acr, print_patch_generation
from app.model import common
from app.prompts.prompts import (
    get_dockerfile_system_prompt,
    get_dockerfile_user_prompt_modify,
    get_dockerfile_instance_layer_user_prompt,
    get_repo_env_template,
)
import re


# Maps GitHub repo name → pre-built base image tag (built from docker/Dockerfile.<name>)
REPO_BASE_IMAGES: dict[str, str] = {
    "MiroMindAI/miroflow":      "swe-factory/miroflow:base",
    "MiroMindAI/MiroThinker":   "swe-factory/mirothinker:base",
    "MiroMindAI/sd-torchtune":  "swe-factory/sd-torchtune:base",
}


def get_base_image_for_repo(repo_name: str) -> str | None:
    """Return the pre-built base image tag for the given repo, or None if not mapped."""
    return REPO_BASE_IMAGES.get(repo_name)


def get_system_prompt_dockerfile(instance_layer: bool = False) -> str:
    return get_dockerfile_system_prompt(instance_layer=instance_layer)


def get_user_prompt_modify_dockerfile() -> str:
    return get_dockerfile_user_prompt_modify()


def get_user_prompt_instance_layer_dockerfile(base_image: str, base_commit: str) -> str:
    return get_dockerfile_instance_layer_user_prompt(
        base_image=base_image,
        base_commit=base_commit,
    )


def write_dockerfile_with_retries(
    message_thread: MessageThread,
    output_dir: str,
    retries: int = 3,
    print_callback: Callable[[dict], None] | None = None,
) -> str:
    """
    Call the LLM to produce a Dockerfile, retrying up to `retries` times if
    extraction fails.  Returns a result message string.
    """
    new_thread = message_thread
    can_stop = False
    result_msg = ""
    os.makedirs(output_dir, exist_ok=True)

    for i in range(1, retries + 2):
        if i > 1:
            debug_file = pjoin(output_dir, f"debug_agent_write_dockerfile_{i - 1}.json")
            with open(debug_file, "w") as f:
                json.dump(new_thread.to_msg(), f, indent=4)

        if can_stop or i > retries:
            break

        logger.info(f"Trying to extract a dockerfile. Try {i} of {retries}.")

        raw_dockerfile_file = pjoin(output_dir, f"agent_dockerfile_raw_{i}")

        try:
            res_text, *_ = common.SELECTED_MODEL.call(new_thread.to_msg())
        except Exception as e:
            logger.error(f"LLM call failed in dockerfile generation try {i}: {e}")
            continue

        new_thread.add_model(res_text, [])

        logger.info(f"Raw dockerfile produced in try {i}. Writing to file.")
        with open(raw_dockerfile_file, "w") as f:
            f.write(res_text)

        print_patch_generation(res_text, f"try {i} / {retries}", print_callback=print_callback)

        dockerfile_extracted = extract_dockerfile_from_response(res_text, output_dir)
        can_stop = dockerfile_extracted

        if can_stop:
            result_msg = "Successfully extracted Dockerfile."
            print_acr(result_msg, f"dockerfile generation try {i}/{retries}", print_callback=print_callback)
        else:
            feedback = "Failed to extract Dockerfile. Please return result in defined format."
            new_thread.add_user(feedback)
            print_acr(feedback, f"Retry {i}/{retries}", print_callback=print_callback)

    if result_msg == "":
        result_msg = "Failed to extract Dockerfile."

    return result_msg


def extract_dockerfile_from_response(res_text: str, output_dir: str) -> bool:
    """Extract Dockerfile content from the LLM response and write it to output_dir/Dockerfile."""
    dockerfile_path = pjoin(output_dir, "Dockerfile")
    dockerfile_extracted = False

    # Pattern 1: <dockerfile> tags
    docker_matches = re.findall(r"<dockerfile>([\s\S]*?)</dockerfile>", res_text)
    for content in docker_matches:
        clean_content = content.strip()
        if clean_content:
            lines = clean_content.splitlines()
            if len(lines) >= 2 and "```" in lines[0] and "```" in lines[-1]:
                lines = lines[1:-1]
            filtered_content = "\n".join(lines)
            with open(dockerfile_path, "w") as f:
                f.write(filtered_content)
            dockerfile_extracted = True
            break

    # Pattern 2: ```dockerfile code block
    if not dockerfile_extracted:
        docker_code_blocks = re.findall(r"```\s*dockerfile\s*([\s\S]*?)```", res_text, re.IGNORECASE)
        for content in docker_code_blocks:
            clean_content = content.strip()
            if clean_content:
                lines = clean_content.splitlines()
                if len(lines) >= 2 and "```" in lines[0] and "```" in lines[-1]:
                    lines = lines[1:-1]
                filtered_content = "\n".join(lines)
                with open(dockerfile_path, "w") as f:
                    f.write(filtered_content)
                dockerfile_extracted = True
                break

    return dockerfile_extracted
