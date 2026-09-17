# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for scan.py — the AWS Security Agent code-review driver.

The emphasis is the failure paths, because the job that runs this fires at 22:00 on
weeknights with nobody watching: a silent wrong answer is worse here than a crash.
Specifically covered are the four things that were wrong before this suite existed —
the bucket ARN, the un-repointed reused review, the `.github` exclusion, and the
dangling symlink — plus the page bound, which cannot be exercised any other way.
"""

from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scan  # noqa: E402

AGENT_SPACE = "as-1234"


def _client() -> Any:
    return boto3.client(
        "securityagent",
        region_name="us-east-1",
        aws_access_key_id="x",
        aws_secret_access_key="y",
    )


def _summaries(*titles: str) -> list[dict[str, str]]:
    return [{"codeReviewId": f"cr-{t}", "agentSpaceId": AGENT_SPACE, "title": t} for t in titles]


class TestBucketName:
    """The bucket arrives as an ARN, and boto3 rejects an ARN as Bucket=."""

    def test_arn_is_reduced_to_the_name(self) -> None:
        assert scan.bucket_name("arn:aws:s3:::coa-security-review-source") == ("coa-security-review-source")

    def test_bare_name_is_unchanged(self) -> None:
        assert scan.bucket_name("coa-security-review-source") == "coa-security-review-source"

    def test_arn_with_trailing_path_keeps_only_the_bucket(self) -> None:
        assert scan.bucket_name("arn:aws:s3:::bkt/some/prefix") == "bkt"

    def test_result_is_a_valid_bucket_parameter(self) -> None:
        # The regression this guards: boto3 raises ParamValidationError on the ARN.
        s3 = boto3.client("s3", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y")
        with Stubber(s3) as stub:
            stub.add_response("head_bucket", {})
            s3.head_bucket(Bucket=scan.bucket_name("arn:aws:s3:::bkt"))


class TestBuildZip:
    def test_skips_cache_dirs_but_keeps_dotgithub(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("x")
        # `.github` was excluded by the old `'/.git' in root` substring test — the
        # exact files a security review most wants to read.
        (tmp_path / ".github" / "workflows").mkdir(parents=True)
        (tmp_path / ".github" / "workflows" / "pr.yml").write_text("x")
        (tmp_path / ".gitlab" / "issue_templates").mkdir(parents=True)
        (tmp_path / ".gitlab" / "issue_templates" / "bug.md").write_text("x")
        # Pruned.
        (tmp_path / ".git" / "objects").mkdir(parents=True)
        (tmp_path / ".git" / "objects" / "aa").write_text("x")
        (tmp_path / ".uv-cache").mkdir()
        (tmp_path / ".uv-cache" / "wheel").write_text("x")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "dep.js").write_text("x")

        dest = tmp_path / "out.zip"
        count = scan.build_zip(str(dest), str(tmp_path))

        names = set(zipfile.ZipFile(dest).namelist())
        assert "src/app.py" in names
        assert ".github/workflows/pr.yml" in names
        assert ".gitlab/issue_templates/bug.md" in names
        assert not [n for n in names if n.startswith((".git/", ".uv-cache/", "node_modules/"))]
        assert count == len(names)

    def test_dangling_symlink_does_not_crash(self, tmp_path: Path) -> None:
        (tmp_path / "real.txt").write_text("x")
        os.symlink(tmp_path / "missing.txt", tmp_path / "broken")

        dest = tmp_path / "out.zip"
        count = scan.build_zip(str(dest), str(tmp_path))

        assert count == 1
        assert zipfile.ZipFile(dest).namelist() == ["real.txt"]

    def test_dest_inside_root_is_not_self_included(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("x")
        dest = tmp_path / "out.zip"
        count = scan.build_zip(str(dest), str(tmp_path))
        assert count == 1
        assert zipfile.ZipFile(dest).namelist() == ["a.txt"]

    def test_produces_a_readable_archive(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("hello")
        dest = tmp_path / "out.zip"
        scan.build_zip(str(dest), str(tmp_path))
        assert zipfile.ZipFile(dest).read("a.txt") == b"hello"


class TestResolveAgentSpace:
    def test_returns_normalised_bucket_and_role(self) -> None:
        client = _client()
        with Stubber(client) as stub:
            stub.add_response(
                "batch_get_agent_spaces",
                {
                    "agentSpaces": [
                        {
                            "agentSpaceId": AGENT_SPACE,
                            "name": "coa-security-review",
                            "awsResources": {
                                "s3Buckets": ["arn:aws:s3:::src-bucket"],
                                "iamRoles": ["arn:aws:iam::1234:role/svc"],
                            },
                        }
                    ]
                },
            )
            assert scan.resolve_agent_space(client, AGENT_SPACE) == (
                "src-bucket",
                "arn:aws:iam::1234:role/svc",
            )

    def test_unresolvable_id_dies_naming_the_id(self) -> None:
        # batch_get silently omits ids it cannot resolve rather than erroring.
        client = _client()
        with Stubber(client) as stub:
            stub.add_response("batch_get_agent_spaces", {"agentSpaces": []})
            with pytest.raises(SystemExit) as exc:
                scan.resolve_agent_space(client, AGENT_SPACE)
        assert "agent space" in str(exc.value)
        assert AGENT_SPACE in str(exc.value)

    def test_missing_bucket_points_at_the_cloudformation_template(self) -> None:
        client = _client()
        with Stubber(client) as stub:
            stub.add_response(
                "batch_get_agent_spaces",
                {
                    "agentSpaces": [
                        {
                            "agentSpaceId": AGENT_SPACE,
                            "name": "coa-security-review",
                            "awsResources": {"s3Buckets": [], "iamRoles": ["r"]},
                        }
                    ]
                },
            )
            with pytest.raises(SystemExit) as exc:
                scan.resolve_agent_space(client, AGENT_SPACE)
        assert "S3 bucket" in str(exc.value)
        assert "template.yaml" in str(exc.value)

    def test_absent_awsresources_key_is_tolerated(self) -> None:
        # awsResources is not a required member of the response shape.
        client = _client()
        with Stubber(client) as stub:
            stub.add_response(
                "batch_get_agent_spaces",
                {"agentSpaces": [{"agentSpaceId": AGENT_SPACE, "name": "coa-security-review"}]},
            )
            with pytest.raises(SystemExit):
                scan.resolve_agent_space(client, AGENT_SPACE)


class TestFindCodeReview:
    def test_match_on_first_page(self) -> None:
        client = _client()
        with Stubber(client) as stub:
            stub.add_response("list_code_reviews", {"codeReviewSummaries": _summaries("other", "target")})
            assert scan.find_code_review(client, AGENT_SPACE, "target") == "cr-target"

    def test_match_on_a_later_page(self) -> None:
        client = _client()
        with Stubber(client) as stub:
            stub.add_response("list_code_reviews", {"codeReviewSummaries": _summaries("a"), "nextToken": "t1"})
            stub.add_response("list_code_reviews", {"codeReviewSummaries": _summaries("b"), "nextToken": "t2"})
            stub.add_response("list_code_reviews", {"codeReviewSummaries": _summaries("target")})
            assert scan.find_code_review(client, AGENT_SPACE, "target") == "cr-target"

    def test_no_match_returns_none(self) -> None:
        client = _client()
        with Stubber(client) as stub:
            stub.add_response("list_code_reviews", {"codeReviewSummaries": _summaries("a"), "nextToken": "t1"})
            stub.add_response("list_code_reviews", {"codeReviewSummaries": _summaries("b")})
            assert scan.find_code_review(client, AGENT_SPACE, "target") is None

    def test_empty_agent_space_returns_none(self) -> None:
        client = _client()
        with Stubber(client) as stub:
            stub.add_response("list_code_reviews", {"codeReviewSummaries": []})
            assert scan.find_code_review(client, AGENT_SPACE, "target") is None

    def test_exactly_max_pages_does_not_trip_the_bound(self) -> None:
        # The bound must fire on a runaway, never on a legitimate final page.
        client = _client()
        with Stubber(client) as stub:
            for i in range(3):
                stub.add_response(
                    "list_code_reviews",
                    {"codeReviewSummaries": _summaries(f"p{i}"), "nextToken": f"t{i}"},
                )
            stub.add_response("list_code_reviews", {"codeReviewSummaries": _summaries("last")})
            assert scan.find_code_review(client, AGENT_SPACE, "target", max_pages=4) is None

    def test_endless_distinct_tokens_die_at_the_bound(self) -> None:
        client = _client()
        with Stubber(client) as stub:
            for i in range(10):
                stub.add_response(
                    "list_code_reviews",
                    {"codeReviewSummaries": _summaries(f"p{i}"), "nextToken": f"t{i}"},
                )
            with pytest.raises(SystemExit) as exc:
                scan.find_code_review(client, AGENT_SPACE, "target", max_pages=4)
        assert "still paginating after 4 pages" in str(exc.value)

    def test_repeated_token_is_reported_as_a_named_failure(self) -> None:
        # botocore raises PaginationError itself; it must not escape as a traceback.
        client = _client()
        with Stubber(client) as stub:
            stub.add_response("list_code_reviews", {"codeReviewSummaries": _summaries("a"), "nextToken": "same"})
            stub.add_response("list_code_reviews", {"codeReviewSummaries": _summaries("b"), "nextToken": "same"})
            with pytest.raises(SystemExit) as exc:
                scan.find_code_review(client, AGENT_SPACE, "target")
        assert "pagination broke" in str(exc.value)


class TestEnsureCodeReview:
    """The reuse path must repoint the review, or the nightly re-scans night one."""

    ASSETS = {"sourceCode": [{"s3Location": "s3://bkt/scan/abc123.zip"}]}

    def test_existing_review_is_repointed_at_the_new_zip(self) -> None:
        client = _client()
        with Stubber(client) as stub:
            stub.add_response("list_code_reviews", {"codeReviewSummaries": _summaries("target")})
            stub.add_response(
                "update_code_review",
                {"codeReviewId": "cr-target", "agentSpaceId": AGENT_SPACE},
                # The assertion that matters: the fresh s3Location is sent.
                {
                    "agentSpaceId": AGENT_SPACE,
                    "codeReviewId": "cr-target",
                    "assets": self.ASSETS,
                },
            )
            assert scan.ensure_code_review(client, AGENT_SPACE, "target", self.ASSETS, "role") == "cr-target"
            stub.assert_no_pending_responses()

    def test_missing_review_is_created_with_the_assets(self) -> None:
        client = _client()
        with Stubber(client) as stub:
            stub.add_response("list_code_reviews", {"codeReviewSummaries": _summaries("other")})
            stub.add_response(
                "create_code_review",
                {"codeReviewId": "cr-new", "agentSpaceId": AGENT_SPACE},
                {
                    "agentSpaceId": AGENT_SPACE,
                    "title": "target",
                    "assets": self.ASSETS,
                    "serviceRole": "role",
                },
            )
            assert scan.ensure_code_review(client, AGENT_SPACE, "target", self.ASSETS, "role") == "cr-new"
            stub.assert_no_pending_responses()

    def test_failed_repoint_dies_rather_than_scanning_a_stale_snapshot(self) -> None:
        client = _client()
        with Stubber(client) as stub:
            stub.add_response("list_code_reviews", {"codeReviewSummaries": _summaries("target")})
            stub.add_client_error("update_code_review", service_error_code="ValidationException")
            with pytest.raises(SystemExit) as exc:
                scan.ensure_code_review(client, AGENT_SPACE, "target", self.ASSETS, "role")
        assert "update_code_review failed" in str(exc.value)

    def test_failed_create_names_the_title(self) -> None:
        client = _client()
        with Stubber(client) as stub:
            stub.add_response("list_code_reviews", {"codeReviewSummaries": []})
            stub.add_client_error("create_code_review", service_error_code="ValidationException")
            with pytest.raises(SystemExit) as exc:
                scan.ensure_code_review(client, AGENT_SPACE, "target", self.ASSETS, "role")
        assert "create_code_review failed" in str(exc.value)
        assert "'target'" in str(exc.value)


class TestMainValidation:
    @pytest.mark.parametrize(
        ("env", "expected"),
        [
            ({"SA_AGENT_SPACE_ID": "", "SA_NAME": "ok", "AWS_DEFAULT_REGION": "us-east-1"}, "SECURITY_AGENT_SPACE_ID"),
            ({"SA_AGENT_SPACE_ID": "as-1", "SA_NAME": "", "AWS_DEFAULT_REGION": "us-east-1"}, "SA_NAME"),
            ({"SA_AGENT_SPACE_ID": "as-1", "SA_NAME": "bad name!", "AWS_DEFAULT_REGION": "us-east-1"}, "SA_NAME"),
            ({"SA_AGENT_SPACE_ID": "as-1", "SA_NAME": "x" * 101, "AWS_DEFAULT_REGION": "us-east-1"}, "SA_NAME"),
            ({"SA_AGENT_SPACE_ID": "as-1", "SA_NAME": "ok", "AWS_DEFAULT_REGION": ""}, "SECURITY_AGENT_REGION"),
        ],
    )
    def test_bad_input_dies_before_any_aws_call(
        self, monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: str
    ) -> None:
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        # No credentials are configured and no client is stubbed, so reaching AWS would
        # raise something other than SystemExit.
        monkeypatch.setattr(boto3, "client", lambda *a, **k: pytest.fail("validated too late — hit AWS"))
        with pytest.raises(SystemExit) as exc:
            scan.main()
        assert expected in str(exc.value)
