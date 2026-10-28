# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for coa_common.s3."""

import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from coa_common.s3 import (
    _extension,
    get_bucket_tags,
    get_object_metadata_and_tags,
    get_s3_client,
    list_objects,
    parse_bucket_from_arn,
    read_file_bytes,
    regional_s3_client,
    upload_file,
    upload_json,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("region", "host"),
    [
        ("eu-west-1", "example-bucket.s3.eu-west-1.amazonaws.com"),
        ("cn-north-1", "example-bucket.s3.cn-north-1.amazonaws.com.cn"),
    ],
)
def test_regional_s3_client_presigns_against_regional_hostname(monkeypatch, tmp_path, region, host):
    # Dummy credentials and an empty shared config: presigning is local, and a
    # developer's profile or endpoint override must not change the host.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    for name in ("AWS_PROFILE", "AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3"):
        monkeypatch.delenv(name, raising=False)

    url = regional_s3_client(region).generate_presigned_url(
        "put_object", Params={"Bucket": "example-bucket", "Key": "a/b.pdf", "ContentType": "application/pdf"}
    )

    assert url.startswith(f"https://{host}/a/b.pdf?")
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url
    assert f"%2F{region}%2Fs3%2Faws4_request" in url


@pytest.mark.unit
class TestParseBucketFromArn:
    def test_simple_arn(self):
        assert parse_bucket_from_arn("arn:aws:s3:::my-bucket") == "my-bucket"

    def test_arn_with_prefix(self):
        assert parse_bucket_from_arn("arn:aws:s3:::my-bucket/prefix/path") == "my-bucket"

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            parse_bucket_from_arn("")

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_bucket_from_arn("not-an-arn")

    def test_malformed_raises(self):
        with pytest.raises(ValueError):
            parse_bucket_from_arn("arn:aws:s3:::")


@pytest.mark.unit
class TestExtension:
    def test_pdf(self):
        assert _extension("path/to/file.pdf") == ".pdf"

    def test_uppercase(self):
        assert _extension("FILE.PDF") == ".pdf"

    def test_no_extension(self):
        assert _extension("no-extension") == ""

    def test_multiple_dots(self):
        assert _extension("archive.tar.gz") == ".gz"


@pytest.mark.unit
class TestListObjects:
    def _make_paginator(self, pages):
        paginator = MagicMock()
        paginator.paginate.return_value = pages
        client = MagicMock()
        client.get_paginator.return_value = paginator
        return client

    def test_filters_by_extension(self):
        client = self._make_paginator(
            [
                {
                    "Contents": [
                        {"Key": "prefix/file.txt", "Size": 100},
                        {"Key": "prefix/file.exe", "Size": 200},
                        {"Key": "prefix/doc.pdf", "Size": 300},
                    ]
                }
            ]
        )
        result = list_objects(client, "bucket", "prefix/", extensions={".txt", ".pdf"})
        assert len(result) == 2
        assert result[0]["Key"] == "prefix/file.txt"
        assert result[1]["Key"] == "prefix/doc.pdf"

    def test_no_filter_returns_all(self):
        client = self._make_paginator(
            [
                {
                    "Contents": [
                        {"Key": "a.txt", "Size": 10},
                        {"Key": "b.exe", "Size": 20},
                    ]
                }
            ]
        )
        result = list_objects(client, "bucket", "", extensions=None)
        assert len(result) == 2

    def test_skips_directory_markers(self):
        client = self._make_paginator(
            [
                {
                    "Contents": [
                        {"Key": "prefix/", "Size": 0},
                        {"Key": "prefix/file.txt", "Size": 100},
                    ]
                }
            ]
        )
        result = list_objects(client, "bucket", "prefix/")
        assert len(result) == 1
        assert result[0]["Key"] == "prefix/file.txt"


@pytest.mark.unit
class TestGetS3Client:
    @patch("coa_common.s3.boto3")
    def test_no_role_returns_default(self, mock_boto3):
        get_s3_client()
        # No role: a single plain S3 client, now with the shared default
        # timeout/retry config (socket timeouts + bounded retries).
        assert mock_boto3.client.call_count == 1
        args, kwargs = mock_boto3.client.call_args
        assert args == ("s3",)
        assert kwargs["config"].connect_timeout == 2

    @patch("coa_common.s3.boto3")
    def test_with_role_calls_assume_role(self, mock_boto3):
        mock_sts = MagicMock()
        mock_sts.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "AK",
                "SecretAccessKey": "SK",
                "SessionToken": "ST",
            }
        }
        mock_boto3.client.side_effect = [mock_sts, MagicMock()]
        get_s3_client(role_arn="arn:aws:iam::123456789012:role/test-role")
        mock_sts.assume_role.assert_called_once()

    def test_invalid_role_arn_raises(self):
        with pytest.raises(ValueError, match="Invalid IAM role ARN"):
            get_s3_client(role_arn="not-an-arn")

    def test_role_arn_missing_account_id_raises(self):
        with pytest.raises(ValueError, match="Invalid IAM role ARN"):
            get_s3_client(role_arn="arn:aws:iam:::role/test")

    def test_role_arn_short_account_id_raises(self):
        with pytest.raises(ValueError, match="Invalid IAM role ARN"):
            get_s3_client(role_arn="arn:aws:iam::123:role/test")

    @patch("coa_common.s3.boto3")
    def test_assume_role_client_error_raises_value_error(self, mock_boto3):
        from botocore.exceptions import ClientError

        mock_sts = MagicMock()
        mock_sts.assume_role.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Not authorized"}},
            "AssumeRole",
        )
        mock_boto3.client.side_effect = [mock_sts, MagicMock()]
        with pytest.raises(ValueError, match="Cannot assume cross-account role"):
            get_s3_client(role_arn="arn:aws:iam::123456789012:role/coa-cross-role")


@pytest.mark.unit
class TestUploadFile:
    def test_encodes_string(self):
        client = MagicMock()
        upload_file(client, "bucket", "key", "hello")
        client.put_object.assert_called_once()
        call_kwargs = client.put_object.call_args[1]
        assert call_kwargs["Body"] == b"hello"

    def test_bytes_passthrough(self):
        client = MagicMock()
        upload_file(client, "bucket", "key", b"raw")
        call_kwargs = client.put_object.call_args[1]
        assert call_kwargs["Body"] == b"raw"


@pytest.mark.unit
class TestUploadJson:
    def test_content_type(self):
        client = MagicMock()
        upload_json(client, "bucket", "key", {"a": 1})
        call_kwargs = client.put_object.call_args[1]
        assert call_kwargs["ContentType"] == "application/json"
        body = json.loads(call_kwargs["Body"])
        assert body["a"] == 1


@pytest.mark.unit
class TestDownloadFile:
    def test_returns_bytes(self):
        client = MagicMock()
        client.get_object.return_value = {"Body": MagicMock(read=lambda: b"content")}
        result = read_file_bytes(client, "bucket", "key")
        assert result == b"content"


@pytest.mark.unit
class TestGetObjectMetadataAndTags:
    def _make_client(self, metadata=None, tags=None):
        client = MagicMock()
        client.head_object.return_value = {"Metadata": metadata or {}}
        client.get_object_tagging.return_value = {"TagSet": [{"Key": k, "Value": v} for k, v in (tags or {}).items()]}
        return client

    def test_returns_metadata_and_tags(self):
        client = self._make_client(
            metadata={"author": "alice", "department": "eng"},
            tags={"env": "prod", "team": "data"},
        )
        meta, tags = get_object_metadata_and_tags(client, "bucket", "key")
        assert meta == {"author": "alice", "department": "eng"}
        assert tags == {"env": "prod", "team": "data"}

    def test_empty_metadata_and_tags(self):
        client = self._make_client(metadata={}, tags={})
        meta, tags = get_object_metadata_and_tags(client, "bucket", "key")
        assert meta == {}
        assert tags == {}

    def test_head_object_failure_returns_empty_metadata(self):
        client = MagicMock()
        client.head_object.side_effect = ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject")
        client.get_object_tagging.return_value = {"TagSet": [{"Key": "k", "Value": "v"}]}
        meta, tags = get_object_metadata_and_tags(client, "bucket", "key")
        assert meta == {}
        assert tags == {"k": "v"}

    def test_get_object_tagging_failure_returns_empty_tags(self):
        client = MagicMock()
        client.head_object.return_value = {"Metadata": {"x": "y"}}
        client.get_object_tagging.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Denied"}}, "GetObjectTagging"
        )
        meta, tags = get_object_metadata_and_tags(client, "bucket", "key")
        assert meta == {"x": "y"}
        assert tags == {}

    def test_both_calls_fail_returns_empty_dicts(self):
        client = MagicMock()
        client.head_object.side_effect = ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject")
        client.get_object_tagging.side_effect = ClientError(
            {"Error": {"Code": "403", "Message": "Forbidden"}}, "GetObjectTagging"
        )
        meta, tags = get_object_metadata_and_tags(client, "bucket", "key")
        assert meta == {}
        assert tags == {}

    def test_calls_correct_bucket_and_key(self):
        client = self._make_client()
        get_object_metadata_and_tags(client, "my-bucket", "path/to/file.txt")
        client.head_object.assert_called_once_with(Bucket="my-bucket", Key="path/to/file.txt")
        client.get_object_tagging.assert_called_once_with(Bucket="my-bucket", Key="path/to/file.txt")


@pytest.mark.unit
class TestGetBucketTags:
    """Bucket tags feed an authorization decision, so faults must propagate.

    The sibling ``get_object_metadata_and_tags`` swallows errors on purpose — its
    tags are enrichment. These are not, and a swallowed ``AccessDenied`` would be
    indistinguishable from an untagged bucket.
    """

    def test_returns_the_tag_set_as_a_dict(self):
        s3 = MagicMock()
        s3.get_bucket_tagging.return_value = {
            "TagSet": [
                {"Key": "coa:namespace", "Value": "ns-a ns-b"},
                {"Key": "owner", "Value": "team"},
            ]
        }
        assert get_bucket_tags(s3, "b") == {"coa:namespace": "ns-a ns-b", "owner": "team"}
        s3.get_bucket_tagging.assert_called_once_with(Bucket="b")

    def test_no_tag_set_is_an_empty_dict_not_an_error(self):
        """An untagged bucket is a legitimate answer; it authorizes nothing."""
        s3 = MagicMock()
        s3.get_bucket_tagging.side_effect = ClientError(
            {"Error": {"Code": "NoSuchTagSet", "Message": "The TagSet does not exist"}},
            "GetBucketTagging",
        )
        assert get_bucket_tags(s3, "b") == {}

    def test_empty_tag_set_is_an_empty_dict(self):
        s3 = MagicMock()
        s3.get_bucket_tagging.return_value = {"TagSet": []}
        assert get_bucket_tags(s3, "b") == {}

    @pytest.mark.parametrize("code", ["AccessDenied", "NoSuchBucket", "ThrottlingException"])
    def test_other_errors_propagate_so_the_caller_fails_closed(self, code):
        s3 = MagicMock()
        s3.get_bucket_tagging.side_effect = ClientError({"Error": {"Code": code, "Message": code}}, "GetBucketTagging")
        with pytest.raises(ClientError):
            get_bucket_tags(s3, "b")
