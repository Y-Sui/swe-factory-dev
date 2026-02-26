"""
A proxy agent. Process raw response into json format.
"""

import inspect
from typing import Any
import re
from loguru import logger
from collections.abc import Callable, Iterable
from app.data_structures import MessageThread
from app.model import common
from app.post_process import ExtractStatus, is_valid_json
import json

SYSTEM_PROMPT_WITH_WEB_SEARCH = """You are an expert in analyzing test logs and debugging test execution.  
Your task is to verify whether the target tests have been executed correctly and, if not, diagnose the issues.
Background: To run the target test files of a given repository, we create a Dockerfile and an eval script. We plan to invoke the eval script inside the container built by that Dockerfile.
Important: In this workflow, the WriteTestAgent may generate new test files. It is expected that their parent directories may not exist in the original repo. Do NOT treat missing test directories as a blocking error. Instead, ensure the eval script creates directories (e.g., `mkdir -p`) before applying the test patch, and only flag issues if the patch fails to apply or tests fail.

You will:
1. Analyze the test log to check if the target tests were executed and whether they passed.
1. If the tests did not run correctly, determine whether the issue is related to:
   - The **Dockerfile** (environment setup issues)
   - The **evaluation script** (test execution issues)
   - Missing information that needs to be collected
3. Based on your analysis, provide **clear guidance** to the appropriate agent:
   - `write_dockerfile_agent`
   - `write_eval_script_agent`
   - `context_retrieval_agent`
   - `web_search_agent`

Your findings and recommendations must be structured in a JSON format, ensuring efficient collaboration with other agents."""

SYSTEM_PROMPT = """You are an expert in analyzing logs of test execution script and dockerfile construction.
Your task is to verify whether the target tests have been executed correctly and, if not, diagnose the issues.
Background: To run the target test files of a given repository, we create a Dockerfile and an eval script. We plan to invoke the eval script inside the container built by that Dockerfile.
Important: In this workflow, the WriteTestAgent may generate new test files. It is expected that their parent directories may not exist in the original repo. Do NOT treat missing test directories as a blocking error. Instead, ensure the eval script creates directories (e.g., `mkdir -p`) before applying the test patch, and only flag issues if the patch fails to apply or tests fail.

We perform **Fail-to-Pass (F2P) validation**: tests are run twice — once WITHOUT the gold patch (should fail) and once WITH the gold patch (should pass). The F2P classification tells you whether the generated tests properly capture the bug:
- **FAIL2PASS**: Tests fail without patch, pass with patch — this is the desired outcome.
- **PASS2PASS**: Tests pass both times — tests are too weak and do not detect the bug.
- **FAIL2FAIL**: Tests fail both times — likely an environment or test setup issue.
- **PASS2FAIL**: Tests pass without patch but fail with it — tests are broken or inverted.
- **ERROR**: Could not determine exit codes from one or both runs.

You will:
1. Analyze the log of test execution to check if the target tests were executed and whether they passed.
2. Review the F2P validation result to assess test quality.
3. If the tests did not run correctly, determine whether the issue is related to:
   - The **Dockerfile** (environment setup issues)
   - The **evaluation script** (test execution issues)
   - The **generated test files** (test content issues)
   - Missing information that needs to be collected

4. Based on your analysis, provide **clear guidance** to the appropriate agent:
   - `write_dockerfile_agent`
   - `write_eval_script_agent`
   - `write_test_agent` (if the test files themselves need improvement)
   - `context_retrieval_agent`

Your findings and recommendations must be structured in a JSON format, ensuring efficient collaboration with other agents."""

SYSTEM_PROMPT_WITHOUT_CONTEXT_RETRIEVAL = """You are an expert in analyzing logs of test execution script and dockerfile construction.
Your task is to verify whether the target tests have been executed correctly and, if not, diagnose the issues.
Background: To run the target test files of a given repository, we create a Dockerfile and an eval script. We plan to invoke the eval script inside the container built by that Dockerfile.
Important: In this workflow, the WriteTestAgent may generate new test files. It is expected that their parent directories may not exist in the original repo. Do NOT treat missing test directories as a blocking error. Instead, ensure the eval script creates directories (e.g., `mkdir -p`) before applying the test patch, and only flag issues if the patch fails to apply or tests fail.

We perform **Fail-to-Pass (F2P) validation**: tests are run twice — once WITHOUT the gold patch (should fail) and once WITH the gold patch (should pass). The F2P classification tells you whether the generated tests properly capture the bug:
- **FAIL2PASS**: Tests fail without patch, pass with patch — this is the desired outcome.
- **PASS2PASS**: Tests pass both times — tests are too weak and do not detect the bug.
- **FAIL2FAIL**: Tests fail both times — likely an environment or test setup issue.
- **PASS2FAIL**: Tests pass without patch but fail with it — tests are broken or inverted.
- **ERROR**: Could not determine exit codes from one or both runs.

Your task is:

1. Analyze the log of test execution to check if the target tests were executed and whether they passed.

2. Review the F2P validation result to assess test quality.

3. If the tests did not run correctly, determine whether the issue is related to:
   - The **Dockerfile** (environment setup issues)
   - The **evaluation script** (test execution issues)
   - The **generated test files** (test content issues)

4. Based on your analysis, provide **clear guidance** to the appropriate agent:
   - `write_dockerfile_agent`
   - `write_eval_script_agent`
   - `write_test_agent` (if the test files themselves need improvement)

Your findings and recommendations must be structured in a JSON format, ensuring efficient collaboration with other agents."""


SYSTEM_PROMPT_WITHOUT_RUN_TEST = """You are an expert in optimizing dockerfile and eval script.

Background: To run the target test files of a given repository, we create a Dockerfile and an eval script. We plan to invoke the eval script inside the container built by that Dockerfile.

You will:
1. Identify any issues that would prevent the tests from running, including syntax errors, missing dependencies, incorrect commands, or misconfigurations. Classify each issue as one of:
   - The **Dockerfile** (environment setup issues)
   - The **evaluation script** (test execution issues)
   - The **generated test files** (test content issues, e.g., wrong imports, incorrect assertions, missing test coverage)
   - Missing information that needs to be collected

3. Based on your analysis, provide **clear guidance** to the appropriate agent:
   - `write_dockerfile_agent`
   - `write_eval_script_agent`
   - `write_test_agent` (if the test files themselves need improvement, e.g., wrong imports, incorrect test logic, missing edge cases)
   - `context_retrieval_agent`

Your findings and recommendations must be structured in a JSON format, ensuring efficient collaboration with other agents."""


ANALYZE_PROMPT_WITH_WEB_SEARCH = """
Given the test log and the target tests, analyze the results and determine the next steps.

### **Step 1: Verify Test Execution**
1. Were the expected test files executed? Specifically, which tests were modified or added by the `eval script`?
2. Did all tests pass? If not, what errors or failures occurred?
   - If all tests pass, then the process is considered finished.
   - If some tests in the test log did not pass, but the tests that were newly added or modified by the developer (the tests you specifically built the environment and script for) have passed, the process can still be considered finished. This is because the focus is on the new or modified tests that are being run.


### **Step 2: Identify Problems**
- If the tests failed due to **environment setup issues**, analyze whether the problem comes from:
  - The **Dockerfile** (e.g., incorrect dependencies, wrong OS, missing configurations).
  - The **evaluation script** (e.g., incorrect test commands, wrong paths, missing environment activation).
- Sometimes, tests may fail due to incorrect versions of specific dependencies. Be sure to check the versions of critical dependencies to ensure compatibility.
- If there are missing dependencies or unknown errors, consider whether additional context retrieval is required.

### **Step 3: Plan Corrective Actions**
- If a fix is needed in the **Dockerfile**, provide guidance to `write_dockerfile_agent` on how to fix it, always include the original error message and a brief description of what is missing or suspected to be the cause.
- If a fix is needed in the **evaluation script**, provide guidance to `write_eval_script_agent` on how to fix it, always include the original error message and a brief description of what is missing or suspected to be the cause.
- If more information from the target repository is needed, provide guidance to `context_retrieval_agent` on what to collect. Here are some instructions:
    1. Always include the original error message and a brief description of what is missing or suspected to be the cause.
    2. Clearly specify what information or files should be searched for. For environment or dependency issues, recommend files such as requirements.txt, environment.yml, Dockerfile, setup.py, pyproject.toml, etc. For test or evaluation issues, suggest looking for files such as  eval*.sh, pytest.ini, .github/workflows/*, etc. 
    3. Additionally, encourage reviewing documentation files like README.md, CONTRIBUTING.md, or any docs in the root or docs/ directory for relevant setup or testing instructions (Contributing file often contains some testing instruction).
    4. If file locations are unclear, request a directory tree or file listing from the project root and key subdirectories to help identify relevant files or missing context.
- If more information from the web is needed, provide guidance to `web_search_agent` on what to collect.

### **Output Example**
Provide your answer in JSON format:
```json
{
    "analysis": "<Provide your analysis here>".
    "is_finish": true/false,  # Set to true only if all tests have passed successfully. If the target tests were not executed, set to false.
    "guidance_for_write_dockerfile_agent": "<Provide detailed guidance if modifications are needed>",
    "guidance_for_write_eval_script_agent": "<Provide detailed guidance if modifications are needed>",
    "guidance_for_context_retrieval_agent": "<Specify what additional information from the target repository is needed, if any>",
    "guidance_for_web_search_agent": "<Specify what additional information from the web is needed, if any>",
}
```

**Important Notes:**
- If `is_finish` is `true`, all guidance fields can be empty.
- Be specific in your guidance, providing detailed steps for the necessary fixes. Only provide guidance to the relevant agent based on the actual issue.
- Provide detailed error information, including the original error messages or log snippets, to help the agent understand what went wrong.
"""

ANALYZE_PROMPT = """
Given the test log and the target tests, analyze the results and determine the next steps. But if the dockerfile is not built successfully, you should analyze what issues happen.

### **Step 1: Verify Test Execution**
- Identify which test files were added or modified by the eval script.
- Confirm that those tests were actually executed (they appear in the test log).
- Check their pass/fail status:
   - If all tests passed, report success.
- Ensure there is at least some test output in the log:
   - If no test output is found, set `is_finish = false` and include an instruction for write_eval_script_agent to revise the eval script so that tests actually run.

### **Step 1.5: F2P Validation**
- Check the F2P (Fail-to-Pass) classification result provided above.
- **CRITICAL RULE**: You may ONLY set `is_finish = true` if the F2P classification is `FAIL2PASS`. Any other classification means the tests are not ready.
- For each F2P classification, take the following action:
  - **FAIL2PASS**: Tests correctly detect the bug. You may set `is_finish = true` if tests also passed in the post-patch run.
  - **PASS2PASS**: Tests pass even without the gold patch — they do not capture the bug. Provide guidance to `write_test_agent` to strengthen the tests so they fail on the unfixed code.
  - **FAIL2FAIL**: Tests fail both with and without the patch — likely an environment/setup issue. Provide guidance to `write_dockerfile_agent` and/or `write_eval_script_agent`.
  - **PASS2FAIL**: Tests pass without the patch but fail with it — the tests are broken or inverted. Provide guidance to `write_test_agent` to fix the test logic.
  - **ERROR**: Could not determine exit codes. Ensure the eval script echoes "OMNIGRIL_EXIT_CODE=$rc" properly.

### **Step 2: Identify Problems**
- If the tests failed due to **environment setup issues**, analyze whether the problem comes from:
  - The **Dockerfile** (e.g., incorrect dependencies, wrong OS, missing configurations).
  - The **evaluation script** (e.g., incorrect test commands, wrong paths, missing environment activation).
- Sometimes, tests may fail due to incorrect versions of specific dependencies. Be sure to check the versions of critical dependencies to ensure compatibility.
- If there are missing dependencies or unknown errors, consider whether additional context retrieval is required.
- Tests should not be run in the Dockerfile**; skip tests during environment setup and run them in the evaluation script.
- Note that the eval script MUST catch exit code after running tests, and echo "OMNIGRIL_EXIT_CODE=$rc". This is important for judge whether tests are run successfully.

### **Step 3: Plan Corrective Actions**
- If a fix is needed in the **Dockerfile**, provide guidance to `write_dockerfile_agent` on how to fix it, always include the original error message and a brief description of what is missing or suspected to be the cause.
- If a fix is needed in the **evaluation script**, provide guidance to `write_eval_script_agent` on how to fix it, always include the original error message and a brief description of what is missing or suspected to be the cause.
- If the **generated test files** need improvement (e.g., tests are too weak for PASS2PASS, or broken for PASS2FAIL), provide guidance to `write_test_agent` on how to improve them.
- If more information from the target repository is needed, provide guidance to `context_retrieval_agent` on what to collect. Here are some instructions:
    1. Always include the original error message and a brief description of what is missing or suspected to be the cause.
    2. Clearly specify what information or files should be searched for. For environment or dependency issues, recommend files such as requirements.txt, environment.yml, Dockerfile, setup.py, pyproject.toml, etc. For test or evaluation issues, suggest looking for files such as  eval*.sh, pytest.ini, .github/workflows/*, etc.
    3. Additionally, encourage reviewing documentation files like README.md, CONTRIBUTING.md, or any docs in the root or docs/ directory for relevant setup or testing instructions (Contributing file often contains some testing instruction).

### **Output Example**
Provide your answer in JSON format:
```json
{
    "is_finish": true/false,  # ONLY set true if F2P is FAIL2PASS and tests passed.
    "f2p_classification": "<FAIL2PASS/PASS2PASS/FAIL2FAIL/PASS2FAIL/ERROR>",
    "guidance_for_write_dockerfile_agent": "<Provide detailed guidance if modifications are needed>",
    "guidance_for_write_eval_script_agent": "<Provide detailed guidance if modifications are needed>",
    "guidance_for_write_test_agent": "<Provide detailed guidance if generated tests need improvement>",
    "guidance_for_context_retrieval_agent": "<Specify what additional information from the target repository is needed, if any>",
}
```

**Important Notes:**
- If `is_finish` is `true`, all guidance fields can be empty.
- **You MUST NOT set `is_finish = true` unless F2P classification is `FAIL2PASS`.**
- Be specific in your guidance, providing detailed steps for the necessary fixes. Only provide guidance to the relevant agent based on the actual issue. For any agent not called, its guidance field must be empty.
- Calling context_retrieval_agent is expensive. Only suggest using it when there is clearly missing information that is necessary to fix the Dockerfile or evaluation script. Be precise and specific in what to retrieve (e.g., particular files or configuration scripts) to avoid repeated or vague searches.
- Provide detailed error information to tell agent what errors happen.
"""

ANALYZE_PROMPT_WITHOUT_CONTEXT_RETRIEVAL = """
Given the test log and the target tests, analyze the results and determine the next steps. But if the dockerfile is not built successfully, you should analyze what issues happen.

### **Step 1: Verify Test Execution**
- Identify which test files were added or modified by the eval script.
- Confirm that those tests were actually executed (they appear in the test log).
- Check their pass/fail status:
   - If all tests passed, report success.
- Ensure there is at least some test output in the log:
   - If no test output is found, set `is_finish = false` and include an instruction for write_eval_script_agent to revise the eval script so that tests actually run.

### **Step 1.5: F2P Validation**
- Check the F2P (Fail-to-Pass) classification result provided above.
- **CRITICAL RULE**: You may ONLY set `is_finish = true` if the F2P classification is `FAIL2PASS`. Any other classification means the tests are not ready.
- For each F2P classification, take the following action:
  - **FAIL2PASS**: Tests correctly detect the bug. You may set `is_finish = true` if tests also passed in the post-patch run.
  - **PASS2PASS**: Tests pass even without the gold patch — they do not capture the bug. Provide guidance to `write_test_agent` to strengthen the tests so they fail on the unfixed code.
  - **FAIL2FAIL**: Tests fail both with and without the patch — likely an environment/setup issue. Provide guidance to `write_dockerfile_agent` and/or `write_eval_script_agent`.
  - **PASS2FAIL**: Tests pass without the patch but fail with it — the tests are broken or inverted. Provide guidance to `write_test_agent` to fix the test logic.
  - **ERROR**: Could not determine exit codes. Ensure the eval script echoes "OMNIGRIL_EXIT_CODE=$rc" properly.

### **Step 2: Identify Problems**
- If the tests failed due to **environment setup issues**, analyze whether the problem comes from:
  - The **Dockerfile** (e.g., incorrect dependencies, wrong OS, missing configurations).
  - The **evaluation script** (e.g., incorrect test commands, wrong paths, missing environment activation).
- Sometimes, tests may fail due to incorrect versions of specific dependencies. Be sure to check the versions of critical dependencies to ensure compatibility.
- If there are missing dependencies or unknown errors, consider whether additional context retrieval is required.
- Tests should not be run in the Dockerfile**; skip tests during environment setup and run them in the evaluation script.
- Note that the eval script MUST catch exit code after running tests, and echo "OMNIGRIL_EXIT_CODE=$rc". This is important for judge whether tests are run successfully.

### **Step 3: Plan Corrective Actions**
- If a fix is needed in the **Dockerfile**, provide guidance to `write_dockerfile_agent` on how to fix it, always include the original error message and a brief description of what is missing or suspected to be the cause.
- If a fix is needed in the **evaluation script**, provide guidance to `write_eval_script_agent` on how to fix it, always include the original error message and a brief description of what is missing or suspected to be the cause.
- If the **generated test files** need improvement (e.g., tests are too weak for PASS2PASS, or broken for PASS2FAIL), provide guidance to `write_test_agent` on how to improve them.

### **Output Example**
Provide your answer in JSON format:
```json
{
    "is_finish": true/false,  # ONLY set true if F2P is FAIL2PASS and tests passed.
    "f2p_classification": "<FAIL2PASS/PASS2PASS/FAIL2FAIL/PASS2FAIL/ERROR>",
    "guidance_for_write_dockerfile_agent": "<Provide detailed guidance if modifications are needed>",
    "guidance_for_write_eval_script_agent": "<Provide detailed guidance if modifications are needed>",
    "guidance_for_write_test_agent": "<Provide detailed guidance if generated tests need improvement>",
}
```

**Important Notes:**
- If `is_finish` is `true`, all guidance fields can be empty.
- **You MUST NOT set `is_finish = true` unless F2P classification is `FAIL2PASS`.**
- Be specific in your guidance, providing detailed steps for the necessary fixes. Only provide guidance to the relevant agent based on the actual issue. For any agent not called, its guidance field must be empty.
- Provide detailed error information to tell agent what errors happen.
"""

ANALYZE_PROMPT_WITHOUT_RUN_TEST = """
Given the dockerfile and the eval script, do a quick static review. Since we cannot run Docker or tests, **be lenient** — do not try to fully verify import paths, module structure, or runtime behavior.

### **Step 1: Check Core Completeness**
Set `is_finish` to **true** if ALL of the following are met:
1. The Dockerfile installs dependencies and clones/checks-out the target repository.
2. The eval script invokes a test runner (e.g., pytest, unittest, node, go test) on the target test files.
3. The eval script captures the exit code and echoes `OMNIGRIL_EXIT_CODE=$rc`.

If these three conditions hold, **accept the artifacts** — do not request extra context or speculate about potential issues.

### **Step 2: Only Flag Obvious, Concrete Issues**
Set `is_finish` to **false** ONLY if you find a clear, specific problem such as:
- Syntax errors in the Dockerfile or eval script (e.g., malformed RUN command, unclosed quotes).
- A clearly wrong or missing command (e.g., eval script calls `pytest` but Dockerfile never installs pytest).
- The eval script does not capture `OMNIGRIL_EXIT_CODE`.
- The Dockerfile is missing a `git clone` or equivalent step for the target repository.

**Do NOT** set `is_finish` to false for speculative concerns like "import paths might be wrong" or "we should verify the project structure".

### **Step 3: Plan Corrective Actions (only if is_finish is false)**
- If a fix is needed in the **Dockerfile**, provide guidance to `write_dockerfile_agent`.
- If a fix is needed in the **evaluation script**, provide guidance to `write_eval_script_agent`.
- If the **generated test files** have obvious issues, provide guidance to `write_test_agent`.
- **Do NOT** request `context_retrieval_agent` unless there is a clear blocking error that cannot be resolved without additional information. Avoid speculative information gathering.

### **Output Example**
Provide your answer in JSON format:
```json
{
    "is_finish": true/false,  # true unless there is an obvious concrete problem.
    "guidance_for_write_dockerfile_agent": "<Provide detailed guidance if modifications are needed>",
    "guidance_for_write_eval_script_agent": "<Provide detailed guidance if modifications are needed>",
    "guidance_for_write_test_agent": "<Provide detailed guidance if the generated test files need improvement>",
    "guidance_for_context_retrieval_agent": "<Specify what additional information from the target repository is needed, if any>",
}
```

**Important Notes:**
- **Default to `is_finish = true`** when the Dockerfile and eval script look reasonable. Only reject for clear, concrete errors.
- If `is_finish` is `true`, all guidance fields can be empty.
- Do NOT request context retrieval for speculative concerns. Only use it for clear blocking errors.
"""


def run_with_retries( msg_thread: MessageThread, disable_context_retrieval=False,disable_run_test=False, enable_web_search = False, retries=3,print_callback: Callable[[dict], None] | None = None) -> tuple[str | None, list[MessageThread]]:
   
    for idx in range(1, retries + 1):
        logger.debug(
            "Trying to analyze the test log. Try {} of {}.", idx, retries
        )

        res_text = run(msg_thread,disable_context_retrieval,disable_run_test,enable_web_search)
        res_text = extract_json_from_response(res_text)
        # res_text = msg_threads.append(new_thread)
        res_text = res_text.lstrip('```json').rstrip('```')
        logger.debug(res_text)
        extract_status, data = is_valid_json(res_text)

        if extract_status != ExtractStatus.IS_VALID_JSON:
            logger.debug("Invalid json. Will retry.")
            continue

        valid, diagnosis = is_valid_response(data,disable_context_retrieval,enable_web_search)
        if not valid:
            logger.debug(f"{diagnosis}. Will retry.")
            continue

        logger.debug("Extracted a valid json")
        return res_text
    return None


def run(msg_thread: MessageThread,disable_context_retrieval:bool,disable_run_test:bool,enable_web_search:bool) -> tuple[str, MessageThread]:
    """
    Run the agent to extract issue to json format.
    """

    # msg_thread = MessageThread()
    # msg_thread.add_system(PROXY_PROMPT)
    if enable_web_search:
        msg_thread.add_user(ANALYZE_PROMPT_WITH_WEB_SEARCH)
    elif disable_context_retrieval:
        msg_thread.add_user(ANALYZE_PROMPT_WITHOUT_CONTEXT_RETRIEVAL)
    elif disable_run_test:
        msg_thread.add_user(ANALYZE_PROMPT_WITHOUT_RUN_TEST)
    else:
        msg_thread.add_user(ANALYZE_PROMPT)
    res_text, *_ = common.SELECTED_MODEL.call(
        msg_thread.to_msg(), response_format="json_object"
    )

    msg_thread.add_model(res_text, [])  # no tools

    return res_text


def is_valid_response(data: Any,disable_context_retrieval:bool,enable_web_search:bool) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "Json is not a dict"

    if not data.get("is_finish"):
        terminate = data.get("is_finish")
        if terminate is None:
            return False, "'is_finish' parameter is missing"

        if not isinstance(terminate, bool):
            return False, "'is_finish' parameter must be a boolean (true/false)"
    if disable_context_retrieval:
        key_list = ['guidance_for_write_dockerfile_agent',
            'guidance_for_write_eval_script_agent',
            'guidance_for_write_test_agent']
    else:
        key_list = ['guidance_for_write_dockerfile_agent',
            'guidance_for_write_eval_script_agent',
            'guidance_for_write_test_agent',
            'guidance_for_context_retrieval_agent']
    if enable_web_search:
        key_list.append('guidance_for_web_search_agent')
    for key in key_list:
        if not data.get(key):
            terminate = data.get(key)
            if terminate is None:
                return False, f"'{key}' parameter is missing"

            if not isinstance(terminate, str):
                return False, "'{key}' parameter must be a string"

        
    return True, "OK"



def extract_json_from_response(res_text: str):
    """
    Extarct json result from the LLM response
    """
    json_extracted = None

    
    json_matches = re.findall(r"```json([\s\S]*?)```", res_text, re.IGNORECASE)
    if json_matches:
        json_extracted = json_matches[0].strip()

   
    if not json_extracted:
        json_code_blocks = re.findall(r"```([\s\S]*?)```", res_text, re.IGNORECASE)
        for content in json_code_blocks:
            clean_content = content.strip()
           
            try:
                json.loads(clean_content)  
                json_extracted = clean_content
                break
            except json.JSONDecodeError:
                continue  

    return json_extracted if json_extracted else res_text  
