# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for scripts/lib/teardown_failures.py, destroy.sh's DELETE_FAILED classifier.

The reason strings are those of real teardowns of COA dev deployments, with their IDs replaced.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_MODULE = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "teardown_failures.py"
_spec = importlib.util.spec_from_file_location("teardown_failures", _MODULE)
assert _spec and _spec.loader
teardown_failures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(teardown_failures)

SG_PINNED = (
    "resource sg-0123456789abcdef0 has a dependent object (Service: Ec2, Status Code: 400, "
    "Request ID: 00000000-0000-0000-0000-000000000001)"
)
SUBNET_PINNED = (
    "Resource handler returned message: \"The subnet 'subnet-0123456789abcdef0' has dependencies "
    'and cannot be deleted. (Service: Ec2, Status Code: 400, Request ID: 00000000-0000-0000-0000-000000000002)"'
)
VPC_PINNED = "The vpc 'vpc-1' has dependencies and cannot be deleted."
BUCKET_REFILLED = (
    'Resource handler returned message: "The bucket you tried to delete is not empty '
    '(Service: S3, Status Code: 409, Request ID: EXAMPLE000000003)"'
)
RUNTIME_DENIED = (
    "Resource handler returned message: \"Access denied for operation 'AWS::BedrockAgentCore::Runtime'.\" "
    "(RequestToken: 00000000-0000-0000-0000-000000000004, HandlerErrorCode: AccessDenied)"
)
_SG = "AWS::EC2::SecurityGroup"
_RUNTIME = "AWS::BedrockAgentCore::Runtime"
_BUCKET = "AWS::S3::Bucket"
_MCP = "coa_dev_mcp_server-EXAMPLE001"
_DELETED = {"deleted_runtimes": f"coa_dev_context_manager-EXAMPLE002 {_MCP}"}


def _resource(logical: str, rtype: str, reason: str, physical: str = "", status: str = "DELETE_FAILED") -> dict:
    return {
        "LogicalResourceId": logical,
        "PhysicalResourceId": physical or f"{logical}-physical",
        "ResourceType": rtype,
        "ResourceStatus": status,
        "ResourceStatusReason": reason,
    }


def _run(resources: list[dict], runtime_sgs: str = "", deleted_runtimes: str = "") -> list[list[str]]:
    out = io.StringIO()
    teardown_failures.run(
        io.StringIO(json.dumps({"StackResourceSummaries": resources})),
        out,
        set(runtime_sgs.split()),
        frozenset(deleted_runtimes.split()),
    )
    return [line.split("\t") for line in out.getvalue().splitlines()]


# (case, logical id, type, reason, physical id, known ids, expected action)
_CASES = [
    ("serve runtime sg", "AgentCoreSGC3461D0C", _SG, SG_PINNED, "", {}, "retain-runtime-sg"),
    ("mcp runtime sg", "McpSGB45EC69C", _SG, SG_PINNED, "", {}, "retain-runtime-sg"),
    ("runtime sg by id", "Other", _SG, SG_PINNED, "sg-0aaa", {"runtime_sgs": "sg-0bbb sg-0aaa"}, "retain-runtime-sg"),
    # Network groups stay with the network stack, which deletes them once Lambda's ENIs are gone.
    ("lambda sg", "LambdaSG9DBFCFB7", _SG, SG_PINNED, "", {}, "eni-pinned"),
    ("ecs sg", "EcsSGAFCFCDDA", _SG, SG_PINNED, "", {}, "eni-pinned"),
    ("ocsp sg", "DiscoveryOcspSG4E002852", _SG, SG_PINNED, "", {}, "eni-pinned"),
    ("subnet", "VpcPrivateSubnet1Subnet536B997A", "AWS::EC2::Subnet", SUBNET_PINNED, "", {}, "eni-pinned"),
    ("vpc", "Vpc8378EB38", "AWS::EC2::VPC", VPC_PINNED, "", {}, "eni-pinned"),
    ("access-log bucket", "SourcesAccessLogsBucket1E4BB12D", _BUCKET, BUCKET_REFILLED, "", {}, "empty-bucket"),
    # Only access-log buckets refill on their own; another may hold data nobody meant to delete.
    ("other bucket", "OntologyArtifactsBucket12345678", _BUCKET, BUCKET_REFILLED, "", {}, "unknown"),
    ("runtime sg, other reason", "AgentCoreSGC3461D0C", _SG, "Access Denied", "", {}, "unknown"),
    # Seen live: AgentCore was still deleting the runtime step 1 deleted when the stack delete reached it.
    ("deleted runtime", "McpRuntimeC9F015E5", _RUNTIME, RUNTIME_DENIED, _MCP, _DELETED, "retain-deleted-runtime"),
    # Retaining a live runtime would leave it running.
    ("live runtime", "McpRuntimeC9F015E5", _RUNTIME, RUNTIME_DENIED, _MCP, {}, "unknown"),
]


@pytest.mark.parametrize(
    ("logical", "rtype", "reason", "physical", "known", "expected"),
    [pytest.param(*case[1:], id=case[0]) for case in _CASES],
)
def test_classify(logical: str, rtype: str, reason: str, physical: str, known: dict, expected: str) -> None:
    assert _run([_resource(logical, rtype, reason, physical)], **known)[0][0] == expected


def test_rows_are_action_logical_physical_type() -> None:
    rows = _run([_resource("Subnet1", "AWS::EC2::Subnet", SUBNET_PINNED)])
    assert rows == [["eni-pinned", "Subnet1", "Subnet1-physical", "AWS::EC2::Subnet"]]


def test_only_delete_failed_resources_are_reported() -> None:
    rows = _run(
        [
            _resource("AgentCoreSGC3461D0C", _SG, SG_PINNED),
            _resource("Runtime", _RUNTIME, "", status="DELETE_COMPLETE"),
            _resource("Fn", "AWS::Lambda::Function", "", status="DELETE_SKIPPED"),
            _resource("coa-dev-serve", "AWS::CloudFormation::Stack", "The following resource(s) failed to delete"),
        ]
    )
    assert [r[1] for r in rows] == ["AgentCoreSGC3461D0C"]


def test_mixed_failures_report_each_resource() -> None:
    rows = _run(
        [
            _resource("VpcPrivateSubnet2Subnet3788AAA1", "AWS::EC2::Subnet", SUBNET_PINNED),
            _resource("LambdaSG9DBFCFB7", _SG, SG_PINNED),
            _resource("Vpc8378EB38", "AWS::EC2::VPC", VPC_PINNED),
            _resource("SomeRole", "AWS::IAM::Role", "Internal failure"),
        ]
    )
    assert [r[0] for r in rows] == ["eni-pinned", "eni-pinned", "eni-pinned", "unknown"]


def test_describe_stack_resources_shape_is_accepted() -> None:
    out = io.StringIO()
    teardown_failures.run(
        io.StringIO(json.dumps({"StackResources": [_resource("McpSGB45EC69C", _SG, SG_PINNED)]})), out, set()
    )
    assert out.getvalue().startswith("retain-runtime-sg\t")


def test_unreadable_input_is_one_unknown_row() -> None:
    # A throttled or denied list call hands the classifier nothing: never act on that.
    out = io.StringIO()
    teardown_failures.run(io.StringIO(""), out, set())
    assert out.getvalue().split("\t")[0] == "unknown"


def test_assembly_stacks_lists_every_stack_with_its_region() -> None:
    env = "aws://123456789012/{}".format
    manifest = {
        "artifacts": {
            "coa-dev-network": {
                "type": "aws:cloudformation:stack",
                "environment": env("eu-west-1"),
                "properties": {"stackName": "coa-dev-network"},
            },
            "coa-dev-edge-waf": {"type": "aws:cloudformation:stack", "environment": env("us-east-1"), "properties": {}},
            "coa-dev-api": {
                "type": "aws:cloudformation:stack",
                "environment": "aws://unknown-account/unknown-region",
                "properties": {"stackName": "coa-dev-api"},
            },
            "coa-dev-network.assets": {"type": "cdk:asset-manifest"},
            "Tree": {"type": "cdk:tree"},
        }
    }
    assert sorted(teardown_failures.assembly_stacks(manifest, "eu-west-1")) == [
        ("coa-dev-api", "coa-dev-api", "eu-west-1"),
        ("coa-dev-edge-waf", "coa-dev-edge-waf", "us-east-1"),
        ("coa-dev-network", "coa-dev-network", "eu-west-1"),
    ]
