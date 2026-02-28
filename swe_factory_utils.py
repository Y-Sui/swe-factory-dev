"""Shared utilities for SWE-Factory pipeline.

Canonical implementations of token injection, exit-code extraction,
F2P classification, and Dockerfile essentials injection.  Every module
in the project should import from here instead of maintaining its own copy.
"""

from __future__ import annotations

import os
import re

# ---------------------------------------------------------------------------
# Exit-code extraction
# ---------------------------------------------------------------------------

EXIT_CODE_RE = re.compile(r"OMNIGRIL_EXIT_CODE=(\d+)")


def extract_exit_code(output: str) -> int | None:
    """Extract the OMNIGRIL exit code from test output; returns None if not found."""
    m = EXIT_CODE_RE.search(output)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Fail-to-Pass classification
# ---------------------------------------------------------------------------


def classify_f2p(pre_exit: int | None, post_exit: int | None) -> str:
    """Classify Fail-to-Pass result from pre-patch and post-patch exit codes."""
    if pre_exit is None or post_exit is None:
        return "ERROR"
    pre_pass = pre_exit == 0
    post_pass = post_exit == 0
    if not pre_pass and post_pass:
        return "FAIL2PASS"
    elif pre_pass and post_pass:
        return "PASS2PASS"
    elif not pre_pass and not post_pass:
        return "FAIL2FAIL"
    else:  # pre_pass and not post_pass
        return "PASS2FAIL"


# ---------------------------------------------------------------------------
# GitHub token injection
# ---------------------------------------------------------------------------


def inject_github_token(url: str) -> str:
    """Inject GITHUB_TOKEN into a GitHub HTTPS URL for private repo access.

    Returns the original URL unchanged when no token is set or the URL
    already contains credentials.
    """
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token and "github.com" in url and "x-access-token" not in url:
        return url.replace(
            "https://github.com", f"https://x-access-token:{token}@github.com"
        )
    return url


# ---------------------------------------------------------------------------
# Dockerfile essentials injection
# ---------------------------------------------------------------------------

ESSENTIALS_RUN = (
    "RUN apt-get update && apt-get install -y --no-install-recommends "
    "curl git ca-certificates && rm -rf /var/lib/apt/lists/*"
)


def ensure_essentials_in_dockerfile(dockerfile: str) -> str:
    """Inject an early apt-get layer for curl/git/ca-certificates.

    LLMs frequently generate ``RUN curl ...`` before installing curl.
    This inserts the essentials right after the first FROM line so that
    every subsequent RUN can rely on them.  If the Dockerfile already
    installs them, the extra apt-get is a harmless no-op.
    """
    lines = dockerfile.split("\n")
    out: list[str] = []
    inserted = False
    for line in lines:
        out.append(line)
        # Insert right after the first FROM (possibly with --platform)
        if not inserted and line.strip().upper().startswith("FROM "):
            out.append(ESSENTIALS_RUN)
            inserted = True
    return "\n".join(out)
