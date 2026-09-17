# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Hand the repository to AWS Security Agent for a full-repository code review.

Driven by the ``security-code-review`` CI job (``templates/security-code-review.yml``):
automatically on the weekday-nightly schedule, and behind a manual play button on
mainline. See ``ci/security-agent/README.md``.

Fire-and-forget by design — this zips the checkout, uploads it to the agent space's
S3 bucket, points a code review at that object and starts a review job, then exits.
It does not wait for the ~1h review, so findings are read in the Security Agent
console, never in the job log.

Two properties matter more than anything else here, because the nightly run has no
audience:

1. **Every misconfiguration names itself.** Nothing should fail with a bare
   ``IndexError``/``KeyError`` traceback that leaves you guessing which resource is
   missing. :func:`die` exits 1 with a one-line cause; :func:`first` turns "the list
   was empty" into "which resource is missing, and what deploys it".
2. **A reused code review is repointed before it is started.** Assets are bound per
   code review, not per job, so reuse without :func:`~botocore.client.BaseClient`
   ``update_code_review`` would re-scan whatever snapshot created the review — every
   night, forever, while still paying to upload a fresh zip.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import zipfile
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import PaginationError

# Never worth shipping to the scanner. The job sets `cache: []`, so the four global
# caches should not be present at all — these are belt-and-braces for a local run
# and for anything a future `default:` change restores into $CI_PROJECT_DIR.
# Matched against a single directory NAME, not a path substring: the old
# `'/.git' in root` test also swallowed `.github/` and `.gitlab/`, silently hiding
# the CI/CD workflows from a security review.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".mise",
        ".uv-cache",
        ".pnpm-store",
        ".gradle-cache",
        ".openapi-generator-cache",
        ".m2-repo",
        ".nx",
        ".venv",
        "node_modules",
        "cdk.out",
    }
)

# The `name` component input documents this contract; enforce it rather than trusting
# it, since `name` becomes both the S3 key prefix and the review title.
NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,100}")

# botocore itself raises PaginationError when the service repeats a token, so this
# bound only catches an endless stream of *distinct* tokens. Without it that spins
# until the job timeout instead of failing in seconds.
MAX_PAGES = 100

# Defaults are 60s with legacy retries and no retry quota. An unattended job wants a
# bounded per-request budget and standard-mode retries.
BOTO_CONFIG = Config(
    connect_timeout=10,
    read_timeout=60,
    retries={"mode": "standard", "max_attempts": 5},
)


def die(msg: str) -> None:
    """Exit 1 with a one-line cause on stderr.

    Raises SystemExit, which derives from BaseException, so it is deliberately not
    caught by the ``except Exception`` handlers below.
    """
    sys.exit(f"ERROR: {msg}")


def first(items: list[Any], what: str, hint: str) -> Any:
    """Return ``items[0]``, or die naming what was missing and where to look."""
    if not items:
        die(f"no {what} in {hint}")
    return items[0]


def bucket_name(raw: str) -> str:
    """Normalise an S3 bucket ARN or bare name to a bare name.

    ``ci/security-agent/template.yaml`` registers the bucket as
    ``!GetAtt SourceBucket.Arn`` and ``BatchGetAgentSpaces`` is a pass-through, so
    this usually arrives as ``arn:aws:s3:::<name>``. boto3 rejects that outright as a
    ``Bucket=`` parameter (``ParamValidationError: Invalid bucket name``), which would
    fail the upload on every single run. Written to accept either form, so it stays
    correct if the service ever normalises on its side.
    """
    return raw.split(":::")[-1].split("/")[0]


def build_zip(dest: str, root: str = ".") -> int:
    """Zip the working tree into ``dest``, returning the number of files written.

    Prunes :data:`SKIP_DIRS` in place so ``os.walk`` does not even descend into them,
    and skips dangling symlinks — ``ZipFile.write`` follows links, and a single broken
    one (endemic to pnpm stores) would otherwise kill the job with an uncaught
    ``FileNotFoundError``.

    ``dest`` itself is skipped, so pointing it inside ``root`` cannot make the archive
    try to contain itself.
    """
    written = 0
    dest_real = os.path.realpath(dest)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                path = os.path.join(dirpath, f)
                if os.path.realpath(path) == dest_real:
                    continue
                if os.path.islink(path) and not os.path.exists(path):
                    continue
                zf.write(path, os.path.relpath(path, root))
                written += 1
    return written


def resolve_agent_space(client: Any, agent_space_id: str) -> tuple[str, str]:
    """Return ``(bucket_name, service_role_arn)`` for the agent space.

    ``batch_get_agent_spaces`` silently omits ids it cannot resolve, so an empty list
    means a wrong id or a CI role that cannot read it — not an API error.
    """
    space = first(
        client.batch_get_agent_spaces(agentSpaceIds=[agent_space_id])["agentSpaces"],
        "agent space",
        f"batch_get_agent_spaces({agent_space_id}) — check the id and the CI role",
    )
    # Both are provisioned by ci/security-agent/template.yaml; missing means the stack
    # is not deployed, or was deployed with EnableCredentialVendor=false.
    hint = f"agent space {agent_space_id} — is ci/security-agent/template.yaml deployed?"
    resources = space.get("awsResources", {})
    return (
        bucket_name(first(resources.get("s3Buckets", []), "S3 bucket", hint)),
        first(resources.get("iamRoles", []), "IAM role", hint),
    )


def find_code_review(client: Any, agent_space_id: str, title: str, max_pages: int = MAX_PAGES) -> str | None:
    """Return the id of the code review with this title, or None if there is none.

    Reusing one review keeps a year of nightly runs in a single history instead of
    ~250 separate reviews. ``ListCodeReviews`` has no title filter, so the match is
    client-side over every page.

    The page bound is checked *after* the end-of-stream test, so a legitimate final
    page cannot trip it.
    """
    pages = client.get_paginator("list_code_reviews").paginate(agentSpaceId=agent_space_id)
    try:
        for page_num, page in enumerate(pages, 1):
            for cr in page.get("codeReviewSummaries", []):
                if cr["title"] == title:
                    return cr["codeReviewId"]
            if not page.get("nextToken"):
                return None
            if page_num >= max_pages:
                die(
                    f"list_code_reviews still paginating after {max_pages} pages — "
                    "aborting instead of spinning to the job timeout"
                )
    except PaginationError as e:
        # botocore's own repeated-token guard. Route it through die() so it reads like
        # every other failure here instead of a raw traceback.
        die(f"list_code_reviews pagination broke for {agent_space_id}: {e}")
    return None


def ensure_code_review(client: Any, agent_space_id: str, title: str, assets: dict[str, Any], service_role: str) -> str:
    """Return the id of a code review whose assets point at ``assets``.

    Reuses the review with this title if one exists, **repointing it** first. Assets are
    bound to the review and not to the job, so a reused review that is merely started
    re-scans whatever snapshot created it — every night, forever, while still paying to
    upload a fresh zip. That is the single most important line in this file.
    """
    cr_id = find_code_review(client, agent_space_id, title)
    if cr_id:
        location = assets["sourceCode"][0]["s3Location"]
        print(f"Reusing code review {cr_id} → repointing it at {location}")
        try:
            client.update_code_review(agentSpaceId=agent_space_id, codeReviewId=cr_id, assets=assets)
        except Exception as e:
            die(f"update_code_review failed for {cr_id} — could not repoint it at {location}: {e}")
        return cr_id

    print(f"Creating code review: {title}")
    try:
        cr = client.create_code_review(
            agentSpaceId=agent_space_id,
            title=title,
            assets=assets,
            serviceRole=service_role,
        )
    except Exception as e:
        die(f"create_code_review failed for title {title!r}: {e}")
    return str(cr["codeReviewId"])


def main() -> None:
    agent_space_id = os.environ.get("SA_AGENT_SPACE_ID", "").strip()
    name = os.environ.get("SA_NAME", "").strip()
    region = os.environ.get("AWS_DEFAULT_REGION", "").strip()
    # The three account-specific values come from CI/CD variables, so name the variable
    # rather than the component input — that is where the fix has to be made.
    if not agent_space_id:
        die("SA_AGENT_SPACE_ID is empty — set SECURITY_AGENT_SPACE_ID in CI/CD variables")
    if not NAME_RE.fullmatch(name):
        die(f"SA_NAME must match [A-Za-z0-9_-]{{1,100}} — got {name!r}")
    if not region:
        die("AWS_DEFAULT_REGION is empty — set SECURITY_AGENT_REGION in CI/CD variables")

    commit_sha = os.environ.get("CI_COMMIT_SHORT_SHA", "latest")

    client = boto3.client("securityagent", config=BOTO_CONFIG)
    s3 = boto3.client("s3", config=BOTO_CONFIG)

    # Resolved before zipping: a misconfigured agent space should fail in seconds
    # rather than after deflating the whole tree.
    bucket, service_role = resolve_agent_space(client, agent_space_id)

    # mkstemp rather than NamedTemporaryFile: build_zip and upload_file both reopen by
    # path, so the handle is closed immediately and nothing depends on flush ordering.
    fd, zip_path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    print(f"Zipped {build_zip(zip_path)} files")

    key = f"{name}/{commit_sha}.zip"
    s3_uri = f"s3://{bucket}/{key}"
    print(f"Uploading → {s3_uri}")
    try:
        s3.upload_file(zip_path, bucket, key)
    except Exception as e:
        # upload_file funnels every cause (credentials, bucket policy, network, quota)
        # into S3UploadFailedError, so there is no useful per-cause branch to take.
        # Name the target the CI role failed to write to; the wrapped boto3 message
        # supplies the specifics.
        die(f"upload to {s3_uri} failed: {e}")

    assets = {"sourceCode": [{"s3Location": s3_uri}]}
    cr_id = ensure_code_review(client, agent_space_id, name, assets, service_role)

    try:
        job = client.start_code_review_job(agentSpaceId=agent_space_id, codeReviewId=cr_id)
    except Exception as e:
        die(f"start_code_review_job failed for {cr_id} — check agent space capacity and the review state: {e}")

    job_id = job["codeReviewJobId"]
    print("\n✓ Code review running")
    print(f"  Job: {job_id}")
    print(f"  Sign in: https://{region}.console.aws.amazon.com/securityagent/agents/{agent_space_id}?tab=code-reviews")

    try:
        apps = client.list_applications().get("applicationSummaries", [])
    except Exception as e:
        # The review is already running. A missing deep link must not red a job whose
        # actual work succeeded — that inverts the signal for an unattended run.
        print(f"  (deep link unavailable: {type(e).__name__})")
        apps = []
    if apps:
        print(f"  Deep link: https://{apps[0]['domain']}/{agent_space_id}/code-review-jobs/{job_id}")


if __name__ == "__main__":
    main()
