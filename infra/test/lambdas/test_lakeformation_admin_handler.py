# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the LakeFormationAdmin custom-resource handler.

The behaviour under test is which principal a Delete deregisters. CloudFormation
replacement semantics make this subtle: when an Update changes the role ARN, the
new physical resource is created first and CloudFormation then sends a cleanup
Delete carrying the OLD PhysicalResourceId. Resolving the target from SSM at that
point yields the NEW arn, so the handler deregisters the role it just registered.
"""

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_HANDLER_DIR = Path(__file__).resolve().parents[2] / "lib" / "lambdas" / "lakeformation-admin"

ACCOUNT = "111122223333"
ROLE_A = f"arn:aws:iam::{ACCOUNT}:role/provisioner-a"
ROLE_B = f"arn:aws:iam::{ACCOUNT}:role/provisioner-b"
PARAM = "/coa/sources/federation-provisioner-role-arn"


@pytest.fixture
def index(monkeypatch):
    """Import the handler with its module-level boto3 clients stubbed out."""
    sys.path.insert(0, str(_HANDLER_DIR))
    try:
        mod = importlib.import_module("index")
        mod = importlib.reload(mod)
    finally:
        sys.path.remove(str(_HANDLER_DIR))

    monkeypatch.setattr(mod, "_ssm", MagicMock())
    monkeypatch.setattr(mod, "_lf", MagicMock())
    monkeypatch.setattr(mod, "_sts", MagicMock())
    mod._sts.get_caller_identity.return_value = {"Account": ACCOUNT}
    return mod


def _ssm_returns(index, arn):
    index._ssm.get_parameter.return_value = {"Parameter": {"Value": arn}}


def _lf_admins(index, *arns):
    index._lf.get_data_lake_settings.return_value = {
        "DataLakeSettings": {"DataLakeAdmins": [{"DataLakePrincipalIdentifier": a} for a in arns]}
    }


def _written_admins(index):
    """Return the principals from the last PutDataLakeSettings call."""
    settings = index._lf.put_data_lake_settings.call_args.kwargs["DataLakeSettings"]
    return [a["DataLakePrincipalIdentifier"] for a in settings["DataLakeAdmins"]]


def _event(request_type, *, physical_id=None):
    event = {
        "RequestType": request_type,
        "ResourceProperties": {"RoleArnSsmParameterName": PARAM, "RoleArn": ROLE_B},
    }
    if physical_id is not None:
        event["PhysicalResourceId"] = physical_id
    return event


class TestCreate:
    def test_appends_role_and_encodes_it_in_physical_id(self, index):
        _ssm_returns(index, ROLE_A)
        _lf_admins(index, "arn:aws:iam::111122223333:role/existing")

        result = index.handler(_event("Create"))

        assert result["PhysicalResourceId"] == f"lf-admin-{ROLE_A}"
        assert ROLE_A in _written_admins(index)

    def test_preserves_existing_admins(self, index):
        existing = f"arn:aws:iam::{ACCOUNT}:role/existing"
        _ssm_returns(index, ROLE_A)
        _lf_admins(index, existing)

        index.handler(_event("Create"))

        assert existing in _written_admins(index)

    def test_is_idempotent_when_already_admin(self, index):
        _ssm_returns(index, ROLE_A)
        _lf_admins(index, ROLE_A)

        index.handler(_event("Create"))

        assert _written_admins(index).count(ROLE_A) == 1


class TestDeleteTargetsThePhysicalResource:
    """Delete must deregister its OWN principal, not the current SSM value."""

    def test_replacement_cleanup_removes_the_old_role_not_the_new_one(self, index):
        # An Update repointed the parameter from A to B and registered B. CFN now
        # sends the cleanup Delete for the OLD physical resource, lf-admin-A.
        _ssm_returns(index, ROLE_B)
        _lf_admins(index, ROLE_A, ROLE_B)

        index.handler(_event("Delete", physical_id=f"lf-admin-{ROLE_A}"))

        remaining = _written_admins(index)
        assert ROLE_B in remaining, "deregistered the newly-registered role"
        assert ROLE_A not in remaining, "left the stale role registered"

    def test_delete_does_not_read_ssm(self, index):
        _lf_admins(index, ROLE_A)

        index.handler(_event("Delete", physical_id=f"lf-admin-{ROLE_A}"))

        index._ssm.get_parameter.assert_not_called()

    def test_normal_teardown_removes_the_role(self, index):
        other = f"arn:aws:iam::{ACCOUNT}:role/other-admin"
        _lf_admins(index, ROLE_A, other)

        index.handler(_event("Delete", physical_id=f"lf-admin-{ROLE_A}"))

        remaining = _written_admins(index)
        assert ROLE_A not in remaining
        assert other in remaining, "clobbered an unrelated admin"


class TestDeleteEdgeCases:
    def test_no_op_when_physical_id_carries_no_arn(self, index):
        """A failed Create returns the bare "lf-admin" sentinel — nothing to remove."""
        index.handler(_event("Delete", physical_id="lf-admin"))

        index._lf.put_data_lake_settings.assert_not_called()

    def test_no_op_when_physical_id_is_absent(self, index):
        index.handler(_event("Delete"))

        index._lf.put_data_lake_settings.assert_not_called()

    def test_no_op_for_unrecognized_physical_id(self, index):
        index.handler(_event("Delete", physical_id="some-other-resource"))

        index._lf.put_data_lake_settings.assert_not_called()

    def test_lf_error_never_blocks_teardown(self, index):
        _lf_admins(index, ROLE_A)
        index._lf.put_data_lake_settings.side_effect = RuntimeError("LF unavailable")

        result = index.handler(_event("Delete", physical_id=f"lf-admin-{ROLE_A}"))

        assert result["PhysicalResourceId"] == f"lf-admin-{ROLE_A}"

    def test_cross_account_arn_in_physical_id_is_refused(self, index):
        """The cross-account guard applies on the deregister path too."""
        foreign = "arn:aws:iam::999988887777:role/attacker"
        _lf_admins(index, foreign)

        index.handler(_event("Delete", physical_id=f"lf-admin-{foreign}"))

        index._lf.put_data_lake_settings.assert_not_called()


class TestTargetValidation:
    def test_rejects_non_role_arn_from_ssm(self, index):
        _ssm_returns(index, f"arn:aws:iam::{ACCOUNT}:user/not-a-role")

        with pytest.raises(ValueError, match="not an IAM role ARN"):
            index.handler(_event("Create"))

    def test_rejects_cross_account_role(self, index):
        _ssm_returns(index, "arn:aws:iam::999988887777:role/foreign")

        with pytest.raises(ValueError, match="cross-account"):
            index.handler(_event("Create"))

    def test_create_failure_propagates(self, index):
        """A failed registration must fail the deployment, not silently pass."""
        _ssm_returns(index, ROLE_A)
        _lf_admins(index, ROLE_A)
        index._lf.put_data_lake_settings.side_effect = RuntimeError("boom")

        with pytest.raises(RuntimeError):
            index.handler(_event("Create"))
