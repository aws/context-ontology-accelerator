# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Flow tests for scripts/destroy.sh against fake `aws` and `pnpm`, one scenario per
teardown seen on COA dev deployments."""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "destroy.sh"


def _failed(logical: str, physical: str, rtype: str, reason: str) -> str:
    return (
        f'{{"LogicalResourceId":"{logical}","PhysicalResourceId":"{physical}",'
        f'"ResourceType":"{rtype}","ResourceStatus":"DELETE_FAILED","ResourceStatusReason":"{reason}"}}'
    )


_SG = _failed("McpSGB45EC69C", "sg-mcp", "AWS::EC2::SecurityGroup", "resource sg-mcp has a dependent object")
_RUNTIME = _failed("McpRuntimeC9F015E5", "coa_dev_mcp_server-BBB", "AWS::BedrockAgentCore::Runtime", "Access denied")
_MCP_FAILURES = {
    "pinned": _SG,
    "wait": _SG,
    "runtime": _RUNTIME,
    "rerun": _RUNTIME,
    "rerundeleting": _RUNTIME,
    "bucket": _failed(
        "SourcesAccessLogsBucket1E4BB12D", "coa-dev-sources-logs", "AWS::S3::Bucket", "The bucket is not empty"
    ),
    "unknown": _failed("SomeRole", "role", "AWS::IAM::Role", "Internal failure"),
    "rtfailed": _RUNTIME,
}
_SUBNET = _failed("Subnet1", "subnet-1", "AWS::EC2::Subnet", "The subnet has dependencies and cannot be deleted.")
_NAMESPACE = _failed("Ns", "ns-1", "AWS::ServiceDiscovery::PrivateDnsNamespace", "Namespace has services")
_NETWORK_SG = _failed(
    "LambdaSG9DBFCFB7", "sg-lambda", "AWS::EC2::SecurityGroup", "resource sg-lambda has a dependent object"
)
_CONNECTOR_SG = _failed(
    "ConnectorSG4C9B3C14", "sg-connector", "AWS::EC2::SecurityGroup", "resource sg-connector has a dependent object"
)
_NETWORK_FAILURES = {
    "netfail": _NAMESPACE,
    "heldnetfail": _NAMESPACE,
    "netsgpinned": _NETWORK_SG,
    "glue_held": _CONNECTOR_SG,
    "glue_rolealias_sg": _CONNECTOR_SG,
}

# Fake AWS CLI. State lives in files under $FAKE_STATE so a scenario can move
# through "failed once, then fixed" the way the real stacks do.
_FAKE_AWS = textwrap.dedent(
    r"""
    #!/usr/bin/env bash
    S=$FAKE_SCENARIO; ST=$FAKE_STATE
    echo "aws $*" >> "$ST/calls.log"
    a="$*"
    gone() { echo "An error occurred (ValidationError): Stack with id $1 does not exist" >&2; exit 254; }
    # AgentCore still holds the runtime security groups' ENIs.
    billable="contains(['AWS::EC2::NatGateway','AWS::EC2::EIP','AWS::EC2::VPCEndpoint'], ResourceType)"
    held() {
      case "$S" in
        pinned|wait|runtime|retry|imported|natleft|heldnetfail|netsgpinned|listfail|throttle) return 0 ;;
        lambdapinned|foreigneni|glue_agentcore) return 0 ;;
        gd_held|gd_heldnoflag|gd_sgonly|gd_recreated|gd_vpclookupfail|gd_epdeny) return 0 ;;
        released)  # AWS lets go during the ENI wait
          [ -f "$ST/network-deleting" ] || return 0
          n=$(cat "$ST/eni-lookups" 2>/dev/null || echo 0); echo $((n + 1)) > "$ST/eni-lookups"
          [ "$n" -lt 4 ]; return ;;
      esac
      return 1
    }
    # Federated Glue connections, "<name>:<status>:<prefix-env of its role>:<security group>".
    GLUE_OURS=coadevds_0123456789abcdef
    denied() { echo "An error occurred (AccessDeniedException) when calling the $1 operation" >&2; exit 254; }
    glue_connections() {
      ours=$GLUE_OURS
      case "$S" in
        glue_inprogress) echo "$ours:IN_PROGRESS:coa-dev:sg-connector" ;;
        glue_several)  # three of ours; aliases (same name shape, other role), a sibling, another group
          echo "$ours:READY:coa-dev:sg-connector coadevds_fedcba9876543210:FAILED:coa-dev:sg-connector
            coadevds_00112233445566aa:READY:coa-dev:sg-connector coadevds_aaaaaaaaaaaaaaaa:READY:COA-dev:sg-alias
            coadevds_bbbbbbbbbbbbbbbb:READY:coa-d-ev:sg-alias2 coadev1ds_cccccccccccccccc:READY:coa-dev-1:sg-sibling
            coadevds_dddddddddddddddd:READY:coa-dev:sg-other demo-pg:READY:demo:sg-connector" ;;
        glue_rolealias_sg) echo "coadevds_aaaaaaaaaaaaaaaa:READY:COA-dev:sg-connector" ;;  # an alias's, on our group
        glue_foreign_available_eni) echo "demo-pg:READY:demo:sg-connector" ;;
        glue_dangling|glue_orphancatalog|glue_nosg) ;;  # deleted by an earlier run, which left the rest
        glue_*) echo "$ours:READY:coa-dev:sg-connector" ;;
      esac
    }
    rt=coa_dev; [ "$S" = otherenv ] && rt=coa_dev_b  # otherenv: a sibling env took over the pointers
    # GuardDuty Runtime Monitoring's endpoint and security group (gd_* scenarios), as
    # describe-vpc-endpoints / describe-security-groups rows: id, service or name, VPC, owner, tag.
    gds=com.amazonaws.us-east-1.guardduty-data
    gd_endpoints() {
      case "$S" in
        gd_othervpc) printf 'vpce-gd\t%s\tvpc-other\t123456789012\ttrue\n' "$gds"; return ;;
        gd_forged) printf 'vpce-s3\tcom.amazonaws.us-east-1.s3\tvpc-123\t123456789012\ttrue\n'; return ;;
        gd_untagged) printf 'vpce-gd\t%s\tvpc-123\t123456789012\tNone\n' "$gds"; return ;;
        gd_otherowner) printf 'vpce-gd\t%s\tvpc-123\t999999999999\ttrue\n' "$gds"; return ;;
        gd_sgonly|gd_sgname|gd_step7|gd_imported|gd_vpclookupfail|gd_epdeny) return ;;
        gd_*) ;;
        *) return ;;
      esac
      if [ ! -f "$ST/gd-ep-deleted" ] || [ "$S" = gd_slow ]; then  # gd_slow: still deleting
        printf 'vpce-gd\t%s\tvpc-123\t123456789012\ttrue\n' "$gds"
      elif [ "$S" = gd_recreated ] && [ -f "$ST/network-deleting" ] && [ ! -f "$ST/gd-ep2-deleted" ]; then
        printf 'vpce-gd2\t%s\tvpc-123\t123456789012\ttrue\n' "$gds"  # put back while the network stack deletes
      fi
    }
    gd_sgs() {
      case "$S" in
        gd_sgname) printf 'sg-gdx\tGuardDutyManagedSecurityGroup-vpc-other\tvpc-123\t123456789012\ttrue\n'
          printf 'sg-gdy\tlaunch-wizard-1\tvpc-123\t123456789012\ttrue\n'; return ;;
        gd_othervpc|gd_forged|gd_step7|gd_imported) return ;;
        gd_*) [ -f "$ST/gd-sg-deleted" ] \
          || printf 'sg-gd\tGuardDutyManagedSecurityGroup-vpc-123\tvpc-123\t123456789012\ttrue\n' ;;
      esac
    }
    gd_blocks() {
      gd_endpoints | grep -q "$gds"$'\t'vpc-123 || gd_sgs | grep -q 'GuardDutyManagedSecurityGroup-vpc-123'
    }
    case "$a" in
      *"describe-vpc-endpoints"*"--vpc-endpoint-ids"*)  # the wait for a deleted one
        [ "$S" = gd_slow ] && echo vpce-gd ;;
      *"describe-vpc-endpoints"*)
        case "$S" in
          gd_epdeny) echo "An error occurred (UnauthorizedOperation) when calling the DescribeVpcEndpoints" \
            "operation: User: arn:aws:sts::123456789012:assumed-role/dev/x is not authorized to perform:" \
            "ec2:DescribeVpcEndpoints" >&2; exit 254 ;;
          gd_lookupfail) echo "An error occurred (RequestLimitExceeded) when calling the DescribeVpcEndpoints" \
            "operation" >&2; exit 254 ;;
        esac
        gd_endpoints ;;
      *"delete-vpc-endpoints"*)
        if [ "$S" = gd_denied ]; then
          echo "An error occurred (UnauthorizedOperation): explicit deny in a service control policy" >&2; exit 254
        fi
        if [ "$S" = gd_unsuccessful ]; then  # refused, with exit status 0
          printf 'vpce-gd\tOperationNotPermitted\tThe endpoint cannot be deleted now\n'; exit 0
        fi
        if [[ "$a" == *vpce-gd2* ]]; then touch "$ST/gd-ep2-deleted"; else touch "$ST/gd-ep-deleted"; fi ;;
      *"describe-security-groups"*"GuardDutyManagedSecurityGroup-"*) gd_sgs ;;
      *"delete-security-group"*"sg-gd"*)
        if [ "$S" = gd_sgretry ] && [ ! -f "$ST/gd-sg-tried" ]; then
          touch "$ST/gd-sg-tried"; echo "An error occurred (DependencyViolation)" >&2; exit 254
        fi
        touch "$ST/gd-sg-deleted" ;;
      *"describe-vpcs"*)
        [ "$S" = gd_vpcgone ] && { echo "An error occurred (InvalidVpcID.NotFound)" >&2; exit 254; }
        [ "$S" = gd_vpcowner ] && echo 999999999999 || echo 123456789012 ;;
      *"describe-subnets"*)
        [ "$S" = gd_subnetfail ] && { echo "An error occurred (RequestLimitExceeded)" >&2; exit 254; }
        printf 'subnet-1\tsubnet-2\n' ;;
      *"ram list-resources"*)
        sn=arn:aws:ec2:us-east-1:123456789012:subnet
        case "$S" in
          gd_shared) echo "$sn/subnet-2" ;;
          gd_rampaged) printf '%s\t%s\n%s\n' "$sn/subnet-8" "$sn/subnet-2" "$sn/subnet-9" ;;  # a line per page
          gd_ramfail) echo "An error occurred (ThrottlingException)" >&2; exit 254 ;;
          gd_ramdeny) echo "An error occurred (AccessDeniedException) when calling the ListResources operation:" \
            "User: arn:aws:sts::123456789012:assumed-role/dev/x is not authorized to perform: ram:ListResources" >&2
            exit 254 ;;
        esac ;;
      *"list-stack-resources"*"coa-dev-network"*"StackResourceSummaries[].PhysicalResourceId"*)
        case "$S" in
          gd_stackfail) echo "An error occurred (Throttling): Rate exceeded" >&2; exit 254 ;;
          gd_stackpaged) printf 'vpc-123\tsubnet-1\tvpce-ours\nnat-1\n' ;;  # a line per page
          *) printf 'vpc-123\tsubnet-1\tnat-1\tvpce-ours\n' ;;
        esac ;;
      *"configure get region"*) echo us-east-1 ;;
      *"sts get-caller-identity"*"--query Arn"*) echo arn:aws:sts::123456789012:assumed-role/Admin/me ;;
      *"sts get-caller-identity"*) echo 123456789012 ;;
      *"iam get-role --role-name Admin"*) echo arn:aws:iam::123456789012:role/Admin ;;
      *"ssm get-parameter"*"/serve/runtime-arn"*)
        [[ "$S" == rerun* ]] && exit 255
        echo "arn:aws:bedrock-agentcore:us-east-1:1:runtime/${rt}_context_manager-AAA" ;;
      *"ssm get-parameter"*"/mcp/runtime-arn"*)
        [[ "$S" == rerun* ]] && exit 255
        echo "arn:aws:bedrock-agentcore:us-east-1:1:runtime/${rt}_mcp_server-BBB" ;;
      *"ssm get-parameter"*"/smus/domain-id"*)
        case "$S" in otherenv) echo dzd-other ;; ourdomain|domain*) echo dzd-ours ;; *) exit 255 ;; esac ;;
      *"ssm get-parameter"*) exit 255 ;;
      *"datazone get-domain"*"--query name"*)
        case "$S" in
          domainerror) echo "An error occurred (ThrottlingException)" >&2; exit 254 ;;
          domaingone) echo "An error occurred (AccessDeniedException)" >&2; exit 254 ;;
          domainmismatch) echo coa-dev-catalog ;;  # renamed, or a CLI warning in the output
          *) echo "${rt//_/-}-smus-catalog" ;;
        esac ;;
      *"datazone delete-domain"*) echo '{}' ;;
      *"datazone get-domain"*) echo "An error occurred (ResourceNotFoundException)" >&2; exit 254 ;;
      *"get-agent-runtime"*"--query status"*)
        case "$S" in
          runtime) echo "An error occurred (ThrottlingException)" >&2; exit 254 ;;  # unreadable
          rerundeleting) echo DELETING ;;
          rtfailed) echo DELETE_FAILED ;;
          onefail) [[ "$a" == *context_manager-AAA* ]] && { echo READY; exit 0; }
            echo "An error occurred (ResourceNotFoundException)" >&2; exit 254 ;;
          *) echo "An error occurred (ResourceNotFoundException)" >&2; exit 254 ;;
        esac ;;
      *"get-agent-runtime"*"context_manager-AAA"*) echo sg-serve ;;
      *"get-agent-runtime"*"mcp_server-BBB"*) echo sg-mcp ;;
      *"delete-agent-runtime"*)
        if [ "$S" = onefail ] && [[ "$a" == *context_manager-AAA* ]]; then
          echo "An error occurred (Conflict)" >&2; exit 254
        fi
        echo '{}' ;;
      *"describe-security-groups"*"group-name"*)
        [ "$S" = rerunsgfail ] && { echo "An error occurred (RequestLimitExceeded)" >&2; exit 254; }
        if [ "$S" = sgfailonce ] && [ ! -f "$ST/sg-lookup-failed" ]; then
          touch "$ST/sg-lookup-failed"; echo "An error occurred (RequestLimitExceeded)" >&2; exit 254
        fi
        [ -f "$ST/sgs-deleted" ] || printf 'sg-serve\tsg-mcp\n' ;;
      *"describe-security-groups"*"--group-ids sg-"*"stack-name"*)  # the group's owner stack
        if [ "$S" = sgnotfound ] && [[ "$a" == *sg-mcp* ]]; then
          echo "An error occurred (InvalidGroup.NotFound)" >&2; exit 254
        fi
        if [[ "$a" == *sg-mcp* ]]; then echo coa-dev-mcp; else echo coa-dev-serve; fi ;;
      *"describe-security-groups"*) echo None ;;
      *"create-tags"*) : ;;
      *"delete-security-group"*"sg-mcp"*)
        if [ "$S" = sgnotfound ]; then echo "An error occurred (InvalidGroup.NotFound)" >&2; exit 254; fi
        if [ "$S" = sgretry ] && [ ! -f "$ST/sg-tried" ]; then
          touch "$ST/sg-tried"; echo "An error occurred (DependencyViolation)" >&2; exit 254
        fi
        touch "$ST/sgs-deleted" ;;
      *"delete-security-group"*) touch "$ST/sgs-deleted" ;;
      *"describe-network-interfaces"*"Values=sg-connector"*)  # ENIs of federated Glue connections
        case "$S" in
          glue_held|glue_rolealias_sg) printf 'eni-glue1\tin-use\tvpc-123\t5baf48d80110-POSTGRESQL\n' ;;
          glue_foreign_available_eni) printf 'eni-glue-foreign\tavailable\tvpc-123\t5baf48d80110-POSTGRESQL\n' ;;
          glue_enilookupfail) echo "An error occurred (UnauthorizedOperation)" >&2; exit 254 ;;
          glue_orphan)  # one Glue detached but left; one it releases after a few lookups; not Glue's
            [ -f "$ST/glue-orphan-deleted" ] || printf 'eni-glue-orphan\tavailable\tvpc-123\t5baf48d80110-POSTGRESQL\n'
            n=$(cat "$ST/glue-eni-lookups" 2>/dev/null || echo 0); echo $((n + 1)) > "$ST/glue-eni-lookups"
            [ "$n" -lt 3 ] && printf 'eni-glue-attached\tin-use\tvpc-123\t0123456789ab-MYSQL\n'
            printf 'eni-lambda2\tavailable\tvpc-123\tAWS Lambda VPC ENI-coa-dev-x\n' ;;
        esac ;;
      *"delete-network-interface"*"eni-glue-orphan"*) touch "$ST/glue-orphan-deleted" ;;
      *"delete-network-interface"*"eni-glue-foreign"*) : ;;
      *"glue get-connections"*)
        [ "$S" = glue_listdeny ] && denied GetConnections
        if [ "$S" = glue_listonce ] && [ ! -f "$ST/glue-listed" ]; then  # throttled in step 6 only
          touch "$ST/glue-listed"
          echo "An error occurred (ThrottlingException) when calling the GetConnections operation" >&2; exit 254
        fi
        for c in $(glue_connections); do
          IFS=: read -r name status owner sg <<< "$c"
          [ -f "$ST/glue-deleted-$name" ] && continue
          role="arn:aws:iam::123456789012:role/$owner-federated-catalog-role"
          printf '%s\t%s\t%s\t%s\n' "$name" "$status" "$role" "$sg"
        done ;;
      *"glue get-connection "*"Connection.Status"*)  # glue_inprogress: READY on the third look
        n=$(cat "$ST/glue-polls" 2>/dev/null || echo 0); echo $((n + 1)) > "$ST/glue-polls"
        if [ "$n" -lt 2 ]; then echo IN_PROGRESS; else echo READY; fi ;;
      *"glue get-connection "*) echo "An error occurred (EntityNotFoundException)" >&2; exit 254 ;;
      *"glue delete-catalog"*)  # glue_lfadmin: a data lake administrator, once it granted itself DROP
        if [ -f "$ST/glue-catalog-dropped" ]; then  # an administrator ran the printed commands
          echo "An error occurred (EntityNotFoundException) when calling the DeleteCatalog operation" >&2; exit 254
        fi
        if [ "$S" = glue_catalogdeny ] || { [[ "$S" == glue_lfadmin* ]] && [ ! -f "$ST/glue-drop-granted" ]; }; then
          echo "An error occurred (AccessDeniedException) when calling the DeleteCatalog operation:" \
            "Insufficient Lake Formation permission(s): Required Drop on Catalog" >&2; exit 254
        fi
        case "$S" in
          glue_lfadmin_dropfails|glue_lfadmin_revokefail)  # granted, but the delete itself fails
            echo "An error occurred (InternalServiceException) when calling the DeleteCatalog operation" >&2
            exit 254 ;;
          glue_lfadmin_retrynotfound)  # someone else dropped it in between
            echo "An error occurred (EntityNotFoundException) when calling the DeleteCatalog operation" >&2
            exit 254 ;;
        esac ;;
      *"lakeformation grant-permissions"*)
        [[ "$S" == glue_lfadmin* ]] || denied GrantPermissions
        touch "$ST/glue-drop-granted" ;;
      *"lakeformation revoke-permissions"*)
        [ "$S" = glue_lfadmin_revokefail ] && denied RevokePermissions
        rm -f "$ST/glue-drop-granted" ;;
      *"lakeformation deregister-resource"*)
        [ "$S" = glue_deregdeny ] && denied DeregisterResource
        touch "$ST/glue-deregistered" ;;
      *"glue delete-connection"*)
        [ "$S" = glue_deletedeny ] && denied DeleteConnection
        name=${a##*--connection-name }; touch "$ST/glue-deleted-${name%% *}" ;;
      *"lakeformation list-resources"*)  # a registration whose connection an earlier run deleted
        case "$S" in glue_dangling|glue_deregdeny)
          [ -f "$ST/glue-deregistered" ] || echo "arn:aws:glue:us-east-1:123456789012:connection/$GLUE_OURS" ;;
        esac ;;
      *"glue get-catalogs"*)
        case "$S" in
          glue_catalogdeny|glue_orphancatalog) echo "$GLUE_OURS" ;;
          glue_several) printf 'coadevds_aaaaaaaaaaaaaaaa\tcoadev1ds_cccccccccccccccc\n' ;;  # theirs
        esac ;;
      *"describe-network-interfaces"*"group-id"*)
        case "$S" in
          enideny) echo "An error occurred (UnauthorizedOperation)" >&2; exit 254 ;;
          foreign) echo interface ;;
          *) if held; then printf 'agentic_ai\tagentic_ai\n'; fi ;;
        esac ;;
      *"describe-network-interfaces"*"InterfaceType=='agentic_ai'"*)  # AgentCore's, with their groups
        held && printf 'eni-ac\tsg-serve sg-mcp\n'
        [ "$S" = gd_otherac ] && printf 'eni-ac-other\tsg-other\n' ;;
      *"describe-network-interfaces"*"vpc-id"*"agentic_ai"*)  # ENIs that are not AgentCore's
        [ "$S" = foreigneni ] && printf 'eni-foreign\tinterface\tRDSNetworkInterface\n'
        if [[ "$S" == gd_* ]]; then  # the network stack's own until its delete, then GuardDuty's
          [ -f "$ST/network-deleting" ] || printf '%s\n' $'eni-nat\tnat_gateway\tInterface for NAT Gateway nat-1' \
            $'eni-ep\tvpc_endpoint\tVPC Endpoint Interface vpce-ours'
          gd_endpoints | while IFS=$'\t' read -r id svc vpc _; do
            [ "$svc" = "$gds" ] && [ "$vpc" = vpc-123 ] \
              && printf 'eni-%s\tvpc_endpoint\tVPC Endpoint Interface %s\n' "$id" "$id"
          done
          [ "$S" = gd_foreign ] && printf 'eni-foreign\tinterface\tRDSNetworkInterface\n'
          [ "$S" = gd_transient ] && [ ! -f "$ST/network-deleting" ] \
            && printf 'eni-detaching\tinterface\tRDSNetworkInterface\n'  # gone once the stack's delete fails
        fi ;;
      *"describe-network-interfaces"*"available"*)
        case "$S" in
          lambda) [ -f "$ST/lambda-eni-deleted" ] || printf 'eni-lambda1\tAWS Lambda VPC ENI-coa-dev-old-fn\n' ;;
          lambdapinned)  # detached while CloudFormation deleted the network stack
            [ -f "$ST/network-deleting" ] && [ ! -f "$ST/lambda-eni-deleted" ] \
              && printf 'eni-lambda1\tAWS Lambda VPC ENI-coa-dev-old-fn\n' ;;
        esac ;;
      *"lambda get-function"*) echo "An error occurred (ResourceNotFoundException)" >&2; exit 254 ;;
      *"delete-network-interface"*"eni-lambda1"*) touch "$ST/lambda-eni-deleted" ;;
      *"ecs list-services"*)  # vkg_many: more VKG services than DescribeServices takes at once
        case "$S" in
          vkg_many|vkg_batchfail|vkg_batchnone) n=11 ;;
          vkg_drain) n=23 ;;
          vkg_warning|vkg_throttled|vkg_none|vkg_clustergone|vkg_alwaysthrottled|vkg_clustermention) n=3 ;;
          *) n=0 ;;
        esac
        for (( i = 1; i <= n; i++ )); do
          printf 'arn:aws:ecs:us-east-1:123456789012:service/coa-dev-vkg-cluster/coa-dev-vkg-ns%s\t' "$i"
        done ;;
      *"ecs update-service"*|*"ecs delete-service"*) echo '{}' ;;
      *"ecs describe-services"*)
        if [ "$(grep -o 'service/coa-dev-vkg-cluster/' <<< "$a" | wc -l)" -gt 10 ]; then
          echo "An error occurred (InvalidParameterException) when calling the DescribeServices operation:" \
            "service names can have at most 10 items." >&2; exit 254
        fi
        n=$(cat "$ST/describes" 2>/dev/null || echo 0); echo $((n + 1)) > "$ST/describes"
        case "$S" in
          vkg_warning) echo "PythonDeprecationWarning: Boto3 will no longer support Python 3.8" >&2 ;;
          vkg_throttled)  # the CLI's error text starts with a newline
            msg="An error occurred (ThrottlingException) when calling the DescribeServices operation"
            [ "$n" -eq 0 ] && { printf '\n%s: Rate exceeded\n' "$msg" >&2; exit 254; } ;;
          vkg_none) [ "$n" -eq 0 ] && { echo None; exit 0; } ;;
          vkg_clustergone)  # deleted between list-services and the wait
            echo "An error occurred (ClusterNotFoundException) when calling the DescribeServices operation" >&2
            exit 254 ;;
          vkg_clustermention)  # names the exception in its message, but is another error
            [ "$n" -eq 0 ] && { echo "An error occurred (AccessDeniedException) when calling the" \
              "DescribeServices operation: not authorized to map ClusterNotFoundException" >&2; exit 254; } ;;
          vkg_alwaysthrottled)
            echo "An error occurred (ThrottlingException) when calling the DescribeServices operation" >&2
            exit 254 ;;
          vkg_batchfail|vkg_batchnone)  # batch 1 (ns1-10) is drained; batch 2 (ns11) fails once, then drains
            [[ "$a" == *"/coa-dev-vkg-ns1 "* ]] && { echo 0; exit 0; }
            b=$(cat "$ST/batch2" 2>/dev/null || echo 0); echo $((b + 1)) > "$ST/batch2"
            if [ "$b" -eq 0 ]; then
              [ "$S" = vkg_batchnone ] && { echo None; exit 0; }
              echo "An error occurred (ThrottlingException) when calling the DescribeServices operation" >&2
              exit 254
            fi
            [ "$b" -eq 1 ] && { echo 1; exit 0; } ;;
          vkg_drain)  # a poll starts with ns1's batch; nsK is active while poll <= its drain poll
            p=$(cat "$ST/polls" 2>/dev/null || echo 0)
            [[ "$a" == *"/coa-dev-vkg-ns1 "* ]] && { p=$((p + 1)); echo "$p" > "$ST/polls"; }
            left=0
            for k in $(grep -o 'coa-dev-vkg-ns[0-9]*' <<< "$a" | sed 's/.*ns//'); do
              if [ "$k" -le 10 ]; then d=$((k % 3)); elif [ "$k" -le 20 ]; then d=3; else d=1; fi
              [ "$p" -le "$d" ] && left=$((left + 1))
            done
            echo "$left"; exit 0 ;;
        esac
        echo 0 ;;
      *"servicediscovery list-namespaces"*) echo None ;;
      *"cloudformation list-stacks"*)
        case "$S" in
          listfail) echo "An error occurred (Throttling): Rate exceeded" >&2; exit 254 ;;
          sibling) stacks="coa-dev-network:CREATE_COMPLETE coa-dev-b-network:$FAKE_SIBLING_STATUS
            coa-dev-b-x-coa-connector:UPDATE_COMPLETE coa-dev-x-coa-connector:CREATE_COMPLETE" ;;
          connretry) stacks=coa-dev-example-coa-connector:CREATE_COMPLETE ;;
          orphan) stacks="coa-dev-network:CREATE_COMPLETE coa-dev-old-structured:CREATE_COMPLETE" ;;
          conninprogress) stacks=coa-dev-example-coa-connector:DELETE_IN_PROGRESS ;;
          connbusy) stacks=coa-dev-example-coa-connector:CREATE_IN_PROGRESS ;;  # created by the verification
        esac
        for s in ${stacks:-}; do  # only the states --stack-status-filter asks for
          [[ "$a" == *" ${s#*:} "* ]] && printf '%s\t%s\n' "${s%%:*}" "${s#*:}"
        done ;;
      *"describe-stack-events"*) echo "[]" ;;
      *"describe-stacks"*"-coa-connector"*)
        if [ "$S" = connretry ] && [ ! -f "$ST/conn-redeleted" ]; then echo DELETE_FAILED
        elif [ "$S" = connbusy ]; then echo CREATE_COMPLETE; else gone connector; fi ;;
      *"describe-stacks"*"coa-dev-mcp"*)
        if [ "$S" = ownerlive ]; then echo CREATE_COMPLETE
        elif [ -f "$ST/mcp-failed" ] && [ ! -f "$ST/mcp-fixed" ]; then echo DELETE_FAILED; else gone coa-dev-mcp; fi ;;
      *"describe-stacks"*"coa-dev-network"*)
        if [ "$S" = refused ]; then echo DELETE_FAILED  # left so by an earlier run
        elif [ "$S" = rerunfail ] || [ -f "$ST/network-gone" ]; then gone coa-dev-network
        elif [ -f "$ST/network-deleting" ]; then echo DELETE_FAILED
        elif [ -f "$ST/network-slow" ]; then echo DELETE_IN_PROGRESS
        else echo CREATE_COMPLETE; fi ;;
      *"describe-stacks"*"coa-dev-old-structured"*) echo CREATE_COMPLETE ;;
      *"describe-stacks"*"coa-integ-aoss-inspect"*)  # tests/cdk's inspect stack, in the VPC
        case "$S" in
          inspect) [ -f "$ST/inspect-deleted" ] && gone inspect; echo CREATE_COMPLETE ;;
          inspectfail) echo DELETE_FAILED ;;
          inspectunknown) echo "An error occurred (Throttling) when calling DescribeStacks" >&2; exit 254 ;;
          inspectflaky)  # throttled once at step 6, then seen: it exists and was never deleted
            if [ -f "$ST/inspect-looked" ]; then echo CREATE_COMPLETE
            else touch "$ST/inspect-looked"; echo "An error occurred (Throttling)" >&2; exit 254; fi ;;
          *) gone inspect ;;
        esac ;;
      *"describe-stacks"*"coa-dev-namespace"*)
        case "$S" in domainerror|domainmismatch) echo CREATE_COMPLETE ;; *) gone coa-dev-namespace ;; esac ;;
      *"describe-stacks"*"coa-dev-storage"*)
        [ "$S" = throttle ] && { echo "An error occurred (Throttling): Rate exceeded" >&2; exit 254; }
        [ "$S" = review ] && { echo REVIEW_IN_PROGRESS; exit 0; }
        gone coa-dev-storage ;;
      *"describe-stacks"*) gone other ;;
      *"list-stack-resources"*"coa-dev-mcp"*)
        if [ "$S" = retry ]; then
          if [ -f "$ST/retain1" ]; then echo "{\"StackResourceSummaries\":[$FAKE_SG_FAILURE]}"
          else echo "{\"StackResourceSummaries\":[$FAKE_RUNTIME_FAILURE]}"; fi
        else echo "{\"StackResourceSummaries\":[$FAKE_MCP_FAILURE]}"; fi ;;
      *"list-stack-resources"*"coa-dev-network"*"AWS::EC2::VPC'"*)
        [ "$S" = gd_vpclookupfail ] && { echo "An error occurred (Throttling): Rate exceeded" >&2; exit 254; }
        case "$S" in imported|gd_imported) echo None ;; lambda) echo vpc-from-stack ;; *) echo vpc-123 ;; esac ;;
      *"list-stack-resources"*"coa-dev-network"*"$billable"*)
        if [ "$S" = natleft ]; then echo NatGateway1
        elif [ ! -f "$ST/network-deleting" ] && [ ! -f "$ST/network-gone" ]; then
          printf 'NatGateway1\tVpcEndpoint1\n'
        fi ;;
      *"list-stack-resources"*"coa-dev-network"*"--output json"*)
        f=$FAKE_NETWORK_FAILURE  # lambdapinned: the Lambda ENI holds a network stack group until swept
        [ "$S" = lambdapinned ] && [ ! -f "$ST/lambda-eni-deleted" ] && f="$f,$FAKE_NETWORK_SG_FAILURE"
        echo "{\"StackResourceSummaries\":[$f]}" ;;
      *"list-stack-resources"*"coa-dev-network"*"ConnectorSG"*) [ "$S" = glue_nosg ] || echo sg-connector ;;
      *"list-stack-resources"*"coa-dev-network"*) printf 'AWS::EC2::VPC\tvpc-123\n' ;;
      *"delete-stack"*"coa-integ-aoss-inspect"*) touch "$ST/inspect-deleted" ;;
      *"delete-stack"*"-coa-connector"*)
        if [ -f "$ST/conn-deleted" ]; then touch "$ST/conn-redeleted"; else touch "$ST/conn-deleted"; fi ;;
      *"delete-stack"*"coa-dev-mcp"*"--retain-resources"*)
        if [ "$S" = retry ] && [ ! -f "$ST/retain1" ]; then touch "$ST/retain1"; else touch "$ST/mcp-fixed"; fi ;;
      *"delete-stack"*"coa-dev-network"*)
        case "$S" in
          clean|bucket|rerun|rerundeleting|onefail|sgnotfound|imported|sibling|connretry|conninprogress|listfail|enideny|foreign)
            touch "$ST/network-gone" ;;
          ownerlive|throttle|orphan|rerunsgfail|review|sgretry|otherenv|ourdomain|sgfailonce|connbusy)
            touch "$ST/network-gone" ;;
          released) if [ -f "$ST/sgs-deleted" ]; then touch "$ST/network-gone"; else touch "$ST/network-deleting"; fi ;;
          domaingone|inspect|inspectfail|inspectunknown|inspectflaky|vkg_*) touch "$ST/network-gone" ;;
          glue_held|glue_agentcore|glue_rolealias_sg) touch "$ST/network-deleting" ;;
          glue_*) touch "$ST/network-gone" ;;
          netslow) touch "$ST/network-slow" ;;
          refused) echo "An error occurred (ValidationError): Role is invalid or cannot be assumed" >&2; exit 254 ;;
          lambda)
            if [ -f "$ST/lambda-eni-deleted" ]; then touch "$ST/network-gone"; else touch "$ST/network-deleting"; fi ;;
          gd_imported) touch "$ST/network-gone" ;;  # GuardDuty's endpoint is in a VPC the stack does not own
          gd_vpcgone) touch "$ST/network-gone" ;;
          gd_*) if held || gd_blocks || [ "$S" = gd_foreign ]; then touch "$ST/network-deleting"
            else touch "$ST/network-gone"; fi ;;
          *) touch "$ST/network-deleting" ;;
        esac ;;
      *"s3 rm"*) touch "$ST/mcp-fixed" ;;
      *) echo "UNHANDLED aws $a" >> "$ST/unhandled.log" ;;
    esac
    exit 0
    """
).lstrip()

# Fake pnpm: `pnpm --filter coa-infra exec cdk ls|destroy ...`.
_FAKE_PNPM = textwrap.dedent(
    r"""
    #!/usr/bin/env bash
    ST=$FAKE_STATE; echo "pnpm $* AWS_REGION=$AWS_REGION" >> "$ST/calls.log"; a="$*"
    if [[ "$a" == *"cdk ls"* ]]; then
      [ "$FAKE_SCENARIO" = synthfail ] && exit 1
      out=$(echo "$a" | sed -E 's/.*--output ([^ ]+).*/\1/'); mkdir -p "$out"
      printf '%s' '{"artifacts":{
        "coa-dev-network":{"type":"aws:cloudformation:stack","environment":"aws://unknown-account/unknown-region"},
        "coa-dev-mcp":{"type":"aws:cloudformation:stack","environment":"aws://unknown-account/unknown-region"},
        "coa-dev-storage":{"type":"aws:cloudformation:stack","environment":"aws://unknown-account/unknown-region"}}}' \
        > "$out/manifest.json"
      exit 0
    fi
    if [[ "$a" == *"cdk destroy"* ]]; then
      [[ "$a" == *"coa-dev-network"* ]] && { echo "network must wait for step 8" >&2; exit 9; }
      case "$FAKE_SCENARIO" in
        clean|lambda|imported|netfail|netpinned|heldnetfail|sgnotfound|natleft|refused|enideny|foreign) exit 0 ;;
        listfail|sibling|connretry|conninprogress|onefail|ownerlive|throttle) exit 0 ;;
        orphan|rerunsgfail|review|sgretry|netsgpinned|lambdapinned|released|otherenv|ourdomain) exit 0 ;;
        sgfailonce|connbusy|foreigneni|domaingone|netslow|inspect|inspectfail|inspectunknown|inspectflaky) exit 0 ;;
        glue_*|vkg_*) exit 0 ;;
        gd_step7) ;;  # the mcp stack fails with nothing to fix
        gd_*) exit 0 ;;
        rerunfail) exit 1 ;;
      esac
      if [ ! -f "$ST/mcp-fixed" ]; then touch "$ST/mcp-failed"; exit 1; fi
      exit 0
    fi
    """
).lstrip()


# Every `date +%s` moves the clock 30 seconds on, so a wait that never succeeds ends at once.
_FAKE_DATE = textwrap.dedent(
    r"""
    #!/usr/bin/env bash
    [ "${1:-}" = "+%s" ] || exec /bin/date "$@"
    step=30; [ "$FAKE_SCENARIO" = netslow ] && step=600  # its one wait runs to the end
    n=$(cat "$FAKE_STATE/clock" 2>/dev/null || echo 0); echo $((n + step)) > "$FAKE_STATE/clock"
    echo $(( $(/bin/date +%s) + n ))
    """
).lstrip()


def _run(tmp_path: Path, scenario: str, **extra_env: str) -> tuple[int, str, str]:
    bin_dir = tmp_path / "bin"
    state = tmp_path / "state"  # kept between runs on one tmp_path: the second is a re-run
    bin_dir.mkdir(exist_ok=True)
    state.mkdir(exist_ok=True)
    (state / "calls.log").unlink(missing_ok=True)
    fakes = (
        ("aws", _FAKE_AWS),
        ("pnpm", _FAKE_PNPM),
        ("sleep", "#!/usr/bin/env bash\n/bin/sleep 0.01\n"),
        ("date", _FAKE_DATE),
    )
    for name, body in fakes:
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)
    # The caller's own deployment settings must not leak into the scenario.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SCL_", "AWS_", "CDK_"))}
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_SCENARIO": scenario,
            "FAKE_STATE": str(state),
            "FAKE_MCP_FAILURE": _MCP_FAILURES.get(scenario, ""),
            "FAKE_NETWORK_FAILURE": _NETWORK_FAILURES.get(scenario, _SUBNET),
            "FAKE_SG_FAILURE": _SG,
            "FAKE_NETWORK_SG_FAILURE": _NETWORK_SG,
            "FAKE_SIBLING_STATUS": "UPDATE_COMPLETE",
            "FAKE_RUNTIME_FAILURE": _RUNTIME,
            "SCL_DESTROY_YES": "1",
            "AWS_REGION": "us-east-1",
            **extra_env,
        }
    )
    proc = subprocess.run(
        ["bash", str(_SCRIPT), "dev"],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    calls = (state / "calls.log").read_text() if (state / "calls.log").exists() else ""
    unhandled = state / "unhandled.log"
    assert not unhandled.exists(), unhandled.read_text()
    return proc.returncode, proc.stdout + proc.stderr, calls


def test_clean_teardown_deletes_network_last_and_exits_zero(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "clean")
    assert code == 0, output
    assert "--exclusively" in calls
    assert calls.index("cdk destroy") < calls.index("delete-stack --stack-name coa-dev-network")
    assert "AWS_REGION=us-east-1" in calls
    assert "delete-security-group --group-id sg-serve" in calls
    assert "delete-security-group --group-id sg-mcp" in calls


def test_cdk_destroys_the_app_synthesized_for_this_deployment(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "clean", SCL_PREFIX="coa")
    assert code == 0, output
    synth = re.search(r"cdk ls --context env=dev --context resource_prefix=coa --output (\S+)", calls)
    assert synth, calls
    assert f"--app {synth.group(1)} --force" in calls


def test_cdk_runs_in_the_region_the_script_resolved(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "clean", AWS_REGION="", CDK_DEFAULT_REGION="eu-west-1")
    assert code == 0, output
    cdk_calls = [line for line in calls.splitlines() if line.startswith("pnpm ")]
    assert cdk_calls, calls
    assert all(line.endswith("AWS_REGION=eu-west-1") for line in cdk_calls), cdk_calls


def test_pointers_to_another_envs_runtimes_and_domain_are_left_alone(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "otherenv")
    assert code == 0, output
    assert "delete-agent-runtime" not in calls
    assert "delete-domain" not in calls
    assert "coa_dev_b_context_manager-AAA, not a Runtime of this deployment" in output


def test_this_deployments_domain_is_deleted(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "ourdomain")
    assert code == 0, output
    assert "datazone delete-domain --identifier dzd-ours" in calls


@pytest.mark.parametrize("scenario", ["domainerror", "domainmismatch"])
def test_an_unconfirmed_domain_stops_the_run_before_its_pointer_is_deleted(tmp_path: Path, scenario: str) -> None:
    code, output, calls = _run(tmp_path, scenario)
    assert code == 1, output
    assert "Could not confirm DataZone domain dzd-ours" in output
    assert "cdk destroy" not in calls


def test_a_domain_already_deleted_is_not_deleted_again(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "domaingone")
    assert code == 0, output
    assert "delete-domain" not in calls


def test_a_failing_synth_stops_the_run_before_anything_is_deleted(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "synthfail")
    assert code == 1, output
    assert "cdk synth failed" in output
    assert "delete-agent-runtime" not in calls


def test_a_failed_runtime_group_lookup_in_step_1_is_looked_up_again(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "sgfailonce")
    assert code == 0, output
    assert "delete-security-group --group-id sg-mcp" in calls


def test_pinned_security_group_is_retained_and_teardown_deferred(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "pinned")
    assert code == 3, output
    assert "--retain-resources McpSGB45EC69C" in calls
    assert "delete-stack --stack-name coa-dev-network" in calls  # NAT gateway and endpoints go now
    assert "Teardown deferred" in output
    assert "aws ec2 delete-security-group --region us-east-1 --group-id sg-mcp" in output


def test_eni_wait_starts_only_after_the_nat_gateway_and_endpoints_are_deleted(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "wait", SCL_ENI_WAIT_MAX_SECONDS="1")
    assert code == 3, output
    assert "Waiting up to 1s" in output
    eni_lookup = "describe-network-interfaces --region us-east-1 --filters Name=group-id"
    assert calls.index("delete-stack --stack-name coa-dev-network") < calls.rindex(eni_lookup)


def test_one_run_wait_finishes_once_aws_releases_the_enis(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "released", SCL_ENI_WAIT_MAX_SECONDS="32400")
    assert code == 0, output
    assert "delete-security-group --group-id sg-mcp" in calls
    assert calls.count("delete-stack --stack-name coa-dev-network") == 2


def test_runtime_still_deleting_is_retained(tmp_path: Path) -> None:
    # Its status cannot be read either: only step 1's record says it is going.
    code, output, calls = _run(tmp_path, "runtime")
    assert code == 3, output
    assert "--retain-resources McpRuntimeC9F015E5" in calls


def test_retained_runtime_then_pinned_group_is_retained_too(tmp_path: Path) -> None:
    # With the Runtime out of the way, CloudFormation reaches the security group
    # AgentCore still holds: the stack must be deleted again, not given up on.
    code, output, calls = _run(tmp_path, "retry")
    assert code == 3, output
    assert "--retain-resources McpRuntimeC9F015E5 McpSGB45EC69C" in calls


def test_runtime_deleted_by_an_earlier_run_is_retained(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "rerun")
    assert code == 0, output
    assert "--retain-resources McpRuntimeC9F015E5" in calls


def test_runtime_an_earlier_run_left_deleting_is_retained(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "rerundeleting")
    assert code == 0, output
    assert "--retain-resources McpRuntimeC9F015E5" in calls


def test_only_the_groups_of_deleted_runtimes_get_the_deletion_time(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "onefail")
    assert code == 0, output
    assert "create-tags --region us-east-1 --resources sg-mcp" in calls
    assert "create-tags --region us-east-1 --resources sg-serve" not in calls


def test_a_failed_stack_with_nothing_to_fix_stops_after_one_pass(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "nofailed")
    assert code == 1, output
    assert calls.count("cdk destroy") == 1


def test_refilled_access_log_bucket_is_emptied_and_retried(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "bucket")
    assert code == 0, output
    assert "s3 rm s3://coa-dev-sources-logs --recursive" in calls
    assert "--retain-resources" not in calls


def test_unknown_failure_stops_without_retaining(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "unknown")
    assert code == 1, output
    assert "does not handle" in output
    assert "--retain-resources" not in calls
    assert "delete-stack --stack-name coa-dev-network" not in calls


_NOT_AGENTCORE = "not only because of AgentCore's ENIs"


# Exit 3 only while AgentCore's own ENIs hold what is left: anything else is a failure,
# reported as itself.
@pytest.mark.parametrize(
    ("scenario", "marker"),
    [
        ("netfail", _NOT_AGENTCORE),  # the network stack failed on something no ENI holds
        ("netpinned", _NOT_AGENTCORE),  # dependencies with no AgentCore ENI left to explain them
        ("netsgpinned", _NOT_AGENTCORE),  # one of the network stack's own groups is held
        ("heldnetfail", _NOT_AGENTCORE),  # AgentCore holds the subnets, but the namespace failed too
        ("natleft", _NOT_AGENTCORE),  # the NAT gateway is still there
        ("refused", "delete-stack failed for coa-dev-network"),
        ("enideny", "Could not look up the ENIs of"),
        ("foreign", "ENIs other than AgentCore's hold"),
        ("rerunfail", "The teardown did not finish"),  # cdk destroy failed with the network stack gone
        ("listfail", "Could not list the connector stacks"),  # while AgentCore holds the groups
        ("throttle", "Could not look up everything"),  # likewise, at verification
        ("rerunsgfail", "Could not look up the runtime security groups"),
        ("orphan", "coa-dev-old-structured"),  # a stack the app no longer synthesizes
        ("rtfailed", "does not handle"),  # a Runtime whose delete failed is not left out
        ("foreigneni", "eni-foreign"),  # an ENI in the VPC that is not AgentCore's
        ("netslow", "is still DELETE_IN_PROGRESS"),  # the network stack outlasts the wait
    ],
)
def test_anything_else_holding_the_rest_is_a_failure_not_deferred(tmp_path: Path, scenario: str, marker: str) -> None:
    code, output, _ = _run(tmp_path, scenario)
    assert code == 1, output
    assert marker in output
    assert "Teardown deferred" not in output
    assert "unbound variable" not in output


def test_a_change_set_only_stack_is_not_a_leftover(tmp_path: Path) -> None:
    code, output, _ = _run(tmp_path, "review")
    assert code == 0, output


def test_a_runtime_group_that_is_still_settling_is_deleted_on_a_retry(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "sgretry")
    assert code == 0, output
    assert calls.count("delete-security-group --group-id sg-mcp") == 2


def test_a_malformed_wait_stops_the_run_before_anything_is_deleted(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "clean", SCL_CONNECTOR_DELETE_WAIT_MAX_SECONDS="30m")
    assert code == 1, output
    assert "must be a whole number" in output
    assert "delete-agent-runtime" not in calls


def test_detached_lambda_enis_are_swept_before_the_network_stack(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "lambda")
    assert code == 0, output
    assert "Name=vpc-id,Values=vpc-from-stack" in calls
    assert "lambda get-function --function-name coa-dev-old-fn" in calls
    assert calls.index("delete-network-interface") < calls.index("delete-stack --stack-name coa-dev-network")


_GD_FLAG = {"SCL_DESTROY_DELETE_GUARDDUTY_ENDPOINT": "1"}
_GD_DELETE_ENDPOINT = "delete-vpc-endpoints --region us-east-1 --vpc-endpoint-ids vpce-gd"
_GD_DELETE_SG = "delete-security-group --region us-east-1 --group-id sg-gd"


def test_guardduty_endpoint_in_our_vpc_is_deleted_when_asked(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "gd_flag", **_GD_FLAG)
    assert code == 0, output
    assert "Name=vpc-id,Values=vpc-123 Name=service-name,Values=com.amazonaws.us-east-1.guardduty-data" in calls
    # Endpoint, then its group, then the stack.
    assert calls.index(_GD_DELETE_ENDPOINT) < calls.index(_GD_DELETE_SG)
    assert calls.index(_GD_DELETE_SG) < calls.index("delete-stack --stack-name coa-dev-network")
    assert "Deleted GuardDuty's endpoint vpce-gd" in output


def test_guardduty_endpoint_without_the_flag_fails_with_the_ids_and_the_flag(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "gd_noflag")
    assert code == 1, output
    assert "delete-vpc-endpoints" not in calls
    assert _GD_DELETE_SG not in calls
    assert "delete-stack --stack-name coa-dev-network" in calls  # the NAT gateway and endpoints still go
    assert (
        "GuardDuty Runtime Monitoring left endpoint vpce-gd and security group sg-gd in vpc-123, outside CloudFormation"
        in output
    )
    assert "SCL_DESTROY_DELETE_GUARDDUTY_ENDPOINT=1 SCL_PREFIX=coa" in output
    assert "aws ec2 delete-vpc-endpoints --region us-east-1 --vpc-endpoint-ids vpce-gd" in output
    assert "aws ec2 delete-security-group --region us-east-1 --group-id sg-gd" in output
    assert "disable GuardDuty Runtime Monitoring" in output
    assert "Teardown deferred" not in output
    assert output.count("GuardDuty Runtime Monitoring left endpoint") == 1  # reported once, at the end


@pytest.mark.parametrize(("scenario", "rerun_code"), [("gd_noflag", 0), ("gd_heldnoflag", 3)])
def test_guardduty_printed_commands_then_a_rerun_finish_the_teardown(
    tmp_path: Path, scenario: str, rerun_code: int
) -> None:
    code, output, _ = _run(tmp_path, scenario)
    assert code == 1, output
    printed = [line.removeprefix("ERROR:   ") for line in output.splitlines() if line.startswith("ERROR:   ")]
    manual = [cmd for cmd in printed if cmd.startswith(("aws ", "while aws "))]
    assert manual == [
        "aws ec2 delete-vpc-endpoints --region us-east-1 --vpc-endpoint-ids vpce-gd",
        "while aws ec2 describe-vpc-endpoints --region us-east-1 --vpc-endpoint-ids vpce-gd"
        " --query 'VpcEndpoints[].State' --output text 2>/dev/null | grep -qv deleted; do sleep 15; done",
        "aws ec2 delete-security-group --region us-east-1 --group-id sg-gd",
    ]
    assert "SCL_PREFIX=coa AWS_REGION=us-east-1 ./scripts/destroy.sh dev" in printed
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}"}
    env.update(FAKE_SCENARIO=scenario, FAKE_STATE=str(tmp_path / "state"))
    for cmd in manual:  # as an operator would paste them
        subprocess.run(["bash", "-c", cmd], env=env, check=True, timeout=30)
    code, output, calls = _run(tmp_path, scenario)  # still without the flag
    assert code == rerun_code, output
    assert "GuardDuty Runtime Monitoring left" not in output


def test_guardduty_endpoint_deleted_while_agentcore_still_holds_the_vpc_defers(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "gd_held", **_GD_FLAG)
    assert code == 3, output
    assert _GD_DELETE_ENDPOINT in calls
    assert "Teardown deferred" in output


@pytest.mark.parametrize("scenario", ["gd_heldnoflag", "gd_sgonly"])
def test_guardduty_leftovers_are_never_deferred(tmp_path: Path, scenario: str) -> None:
    # gd_sgonly: the group holds the VPC without an ENI, which reads like AgentCore's hold.
    code, output, _ = _run(tmp_path, scenario)
    assert code == 1, output
    assert "Teardown deferred" not in output
    assert "SCL_DESTROY_DELETE_GUARDDUTY_ENDPOINT=1" in output


def test_guardduty_endpoint_put_back_on_a_retry_is_deleted_again(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "gd_recreated", SCL_ENI_WAIT_MAX_SECONDS="1", **_GD_FLAG)
    assert code == 3, output
    assert f"{_GD_DELETE_ENDPOINT} --query" in calls
    assert "delete-vpc-endpoints --region us-east-1 --vpc-endpoint-ids vpce-gd2" in calls


def test_guardduty_is_left_alone_in_an_imported_vpc(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "gd_imported", **_IMPORTED_VPC, **_GD_FLAG)
    assert code == 0, output
    assert "describe-vpc-endpoints" not in calls
    assert "sg-gd" not in calls


@pytest.mark.parametrize("scenario", ["gd_othervpc", "gd_forged", "gd_sgname"])
def test_only_guardduty_resources_of_our_vpc_are_deleted(tmp_path: Path, scenario: str) -> None:
    # Another VPC's endpoint, a GuardDutyManaged tag on another service, a group of another name.
    code, output, calls = _run(tmp_path, scenario, **_GD_FLAG)
    assert code == 0, output
    assert "delete-vpc-endpoints" not in calls
    assert "delete-security-group --region us-east-1 --group-id sg-gd" not in calls


@pytest.mark.parametrize(
    ("scenario", "reason"),
    [
        ("gd_foreign", "other ENIs are still in the VPC: eni-foreign"),
        ("gd_shared", "subnet subnet-2 is shared through AWS RAM"),
        ("gd_vpcowner", "vpc-123 belongs to account 999999999999"),
        ("gd_untagged", "vpce-gd look like GuardDuty's but are not tagged GuardDutyManaged=true"),
        ("gd_otherowner", "vpce-gd look like GuardDuty's but are not tagged GuardDutyManaged=true in this account"),
        ("gd_otherac", "other ENIs are still in the VPC: eni-ac-other"),  # another deployment's AgentCore ENI
    ],
)
def test_guardduty_endpoint_others_may_use_is_not_deleted(tmp_path: Path, scenario: str, reason: str) -> None:
    code, output, calls = _run(tmp_path, scenario, **_GD_FLAG)
    assert code == 1, output
    assert "delete-vpc-endpoints" not in calls
    assert _GD_DELETE_SG not in calls
    assert reason in output


@pytest.mark.parametrize(
    ("scenario", "reason"),
    [
        ("gd_subnetfail", "could not list its subnets"),
        ("gd_ramfail", "could not check for RAM-shared subnets"),
        ("gd_stackfail", "could not list coa-dev-network"),
    ],
)
def test_guardduty_check_that_fails_refuses_the_delete(tmp_path: Path, scenario: str, reason: str) -> None:
    code, output, calls = _run(tmp_path, scenario, **_GD_FLAG)
    assert code == 1, output
    assert "delete-vpc-endpoints" not in calls
    assert reason in output


def test_guardduty_ram_shared_subnet_at_a_page_boundary_is_found(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "gd_rampaged", **_GD_FLAG)
    assert code == 1, output
    assert "delete-vpc-endpoints" not in calls
    assert "subnet subnet-2 is shared through AWS RAM" in output


def test_guardduty_network_stack_endpoint_at_a_page_boundary_is_not_foreign(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "gd_stackpaged", **_GD_FLAG)
    assert code == 0, output
    # Before the first delete-stack: the network stack's ENIs are still there, unlike on the retry.
    assert calls.index(_GD_DELETE_ENDPOINT) < calls.index("delete-stack --stack-name coa-dev-network")


def test_guardduty_refusal_that_clears_is_deleted_on_the_retry(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "gd_transient", **_GD_FLAG)
    assert code == 0, output
    first, second = (m.start() for m in re.finditer("delete-stack --stack-name coa-dev-network", calls))
    assert first < calls.index(_GD_DELETE_ENDPOINT) < second


def test_guardduty_lookup_denied_names_the_api_not_the_delete_permissions(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "gd_ramdeny", **_GD_FLAG)
    assert code == 1, output
    assert "delete-vpc-endpoints" not in calls
    assert "An IAM policy or SCP denies ram:ListResources" in output
    assert "ec2:DeleteVpcEndpoints" not in output


def test_guardduty_lookup_failure_with_nothing_found_fails_without_the_disable_advice(tmp_path: Path) -> None:
    code, output, _ = _run(tmp_path, "gd_lookupfail", **_GD_FLAG)
    assert code == 1, output
    assert "Could not look up GuardDuty's endpoint in vpc-123" in output
    assert "disable GuardDuty Runtime Monitoring" not in output


def test_guardduty_endpoint_lookup_denied_fails_without_deferring(tmp_path: Path) -> None:
    code, output, _ = _run(tmp_path, "gd_epdeny", **_GD_FLAG)
    assert code == 1, output
    assert "Could not look up GuardDuty's endpoint in vpc-123" in output
    assert "An IAM policy or SCP denies ec2:DescribeVpcEndpoints" in output
    assert "Teardown deferred" not in output


def test_network_vpc_lookup_failure_fails_instead_of_deferring(tmp_path: Path) -> None:
    # Without its VPC, the sweep cannot see GuardDuty's group, which holds the VPC like AgentCore's ENIs.
    code, output, _ = _run(tmp_path, "gd_vpclookupfail", **_GD_FLAG)
    assert code == 1, output
    assert "Could not look up the network stack's VPC" in output
    assert "Teardown deferred" not in output


def test_guardduty_vpc_already_gone_is_not_an_error(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "gd_vpcgone", **_GD_FLAG)
    assert code == 0, output
    assert "delete-vpc-endpoints" not in calls
    assert "GuardDuty Runtime Monitoring left" not in output
    assert "Could not look up" not in output


def test_guardduty_endpoint_delete_refused_in_unsuccessful_fails(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "gd_unsuccessful", **_GD_FLAG)
    assert code == 1, output
    assert "Could not delete GuardDuty's endpoint vpce-gd: vpce-gd\tOperationNotPermitted" in output
    assert _GD_DELETE_SG not in calls
    assert "An IAM policy or SCP denies" not in output


def test_guardduty_delete_denied_fails_at_once_with_a_hint(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "gd_denied", **_GD_FLAG)
    assert code == 1, output
    assert calls.count("delete-vpc-endpoints") == 1  # once per sweep, no retry loop
    assert "An IAM policy or SCP denies it" in output
    assert "Teardown deferred" not in output


def test_guardduty_endpoint_slower_than_the_wait_fails(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "gd_slow", **_GD_FLAG)
    assert code == 1, output
    assert "vpce-gd was still being deleted after 600s" in output
    assert _GD_DELETE_SG not in calls


def test_guardduty_group_still_settling_is_deleted_on_a_retry(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "gd_sgretry", **_GD_FLAG)
    assert code == 0, output
    assert calls.count(_GD_DELETE_SG) == 2


def test_guardduty_sweep_waits_for_step_7(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "gd_step7", **_GD_FLAG)
    assert code == 1, output
    assert "describe-vpc-endpoints" not in calls


@pytest.mark.parametrize("sibling_network", ["UPDATE_COMPLETE", "UPDATE_IN_PROGRESS"])
def test_connectors_of_a_sibling_deployment_are_left_alone(tmp_path: Path, sibling_network: str) -> None:
    code, output, calls = _run(tmp_path, "sibling", FAKE_SIBLING_STATUS=sibling_network)
    assert code == 0, output
    assert "delete-stack --stack-name coa-dev-x-coa-connector" in calls
    assert "delete-stack --stack-name coa-dev-b-x-coa-connector" not in calls


def test_a_connector_an_earlier_run_started_deleting_is_awaited(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "conninprogress")
    assert code == 0, output
    assert "describe-stacks --stack-name coa-dev-example-coa-connector" in calls


def test_a_connector_still_being_created_is_reported_not_deleted(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "connbusy")
    assert code == 1, output
    assert "coa-dev-example-coa-connector\tCREATE_COMPLETE" in output
    assert "delete-stack --stack-name coa-dev-example-coa-connector" not in calls


def test_a_failed_connector_is_deleted_again_after_the_lambda_eni_sweep(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "connretry")
    assert code == 0, output
    deletes = "delete-stack --stack-name coa-dev-example-coa-connector"
    assert calls.count(deletes) == 2
    assert calls.index("Name=vpc-id,Values=") < calls.rindex(deletes)


_GLUE_OURS = "coadevds_0123456789abcdef"
_GLUE_ARN = f"arn:aws:glue:us-east-1:123456789012:connection/{_GLUE_OURS}"
_GLUE_CATALOG = f'{{"Catalog":{{"Id":"123456789012:{_GLUE_OURS}"}}}}'


def test_no_federated_glue_connection_leaves_glue_alone(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "clean")
    assert code == 0, output
    assert "No federated Glue connections of this deployment (coadevds_*)" in output
    assert "glue delete-" not in calls
    assert "deregister-resource" not in calls


def test_a_leftover_federated_connection_is_deleted_before_cdk_destroy(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "glue_one")
    assert code == 0, output
    assert f"Deleted federated Glue connection {_GLUE_OURS}" in output
    # The order of the API's own source delete (cleanup_federated_resources).
    steps = [
        f"glue delete-catalog --catalog-id {_GLUE_OURS}",
        f"lakeformation deregister-resource --resource-arn {_GLUE_ARN}",
        f"glue delete-connection --connection-name {_GLUE_OURS}",
        "cdk destroy",
    ]
    positions = [calls.index(step) for step in steps]
    assert positions == sorted(positions), calls


def test_a_rerun_after_the_glue_sweep_deletes_nothing_more(tmp_path: Path) -> None:
    _run(tmp_path, "glue_one")
    code, output, calls = _run(tmp_path, "glue_one")
    assert code == 0, output
    assert "glue delete-" not in calls
    assert "No federated Glue connections of this deployment" in output


def test_only_this_deployments_federated_connections_are_deleted(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "glue_several")
    assert code == 0, output
    deleted = re.findall(r"glue delete-connection --connection-name (\S+)", calls)
    assert sorted(deleted) == sorted([_GLUE_OURS, "coadevds_fedcba9876543210", "coadevds_00112233445566aa"])
    # Same name shape, another deployment's role: "COA"/"dev" and "coa"/"d-ev" alias "coa"/"dev".
    assert "its role is arn:aws:iam::123456789012:role/COA-dev-federated-catalog-role" in output
    assert "its role is arn:aws:iam::123456789012:role/coa-d-ev-federated-catalog-role" in output
    assert "Leaving Glue connection coadevds_dddddddddddddddd: it does not use" in output
    assert "coadev1ds_" not in output  # a sibling env's ("dev-1") is not even named like ours
    assert "Glue connection demo-pg is not this deployment's but uses its connector security group" in output
    assert "Federated catalogs left" not in output  # the aliases' catalogs are theirs


def test_a_catalog_the_operator_cannot_drop_keeps_its_connection_and_fails_the_run(tmp_path: Path) -> None:
    # Glue refuses DeleteConnection while the catalog exists: nothing else is tried, nor waited for.
    code, output, calls = _run(tmp_path, "glue_catalogdeny")
    assert code == 1, output
    assert "lakeformation grant-permissions" in calls  # tried, as an administrator could
    assert output.count(f"aws glue delete-catalog --region us-east-1 --catalog-id {_GLUE_OURS}") == 1
    assert (
        "aws lakeformation grant-permissions --region us-east-1 --permissions DROP --principal"
        f" DataLakePrincipalIdentifier=<your-principal-arn> --resource '{_GLUE_CATALOG}'"
    ) in output
    assert "only a data lake administrator grant DROP" in output
    assert "glue delete-connection" not in calls
    assert "deregister-resource" not in calls
    assert "Waiting up to" not in output
    assert "whose connection is gone" not in output  # its connection is still there


def test_a_rerun_after_an_admin_dropped_the_catalogs_deletes_the_connections(tmp_path: Path) -> None:
    code, output, _ = _run(tmp_path, "glue_catalogdeny")
    assert code == 1, output
    (tmp_path / "state" / "glue-catalog-dropped").touch()  # an administrator ran the printed commands
    code, output, calls = _run(tmp_path, "glue_catalogdeny")
    assert code == 0, output
    assert f"lakeformation deregister-resource --resource-arn {_GLUE_ARN}" in calls
    assert f"glue delete-connection --connection-name {_GLUE_OURS}" in calls
    assert "lakeformation grant-permissions" not in calls


def test_a_lake_formation_admin_grants_itself_drop_and_deletes_everything(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "glue_lfadmin")
    assert code == 0, output
    grant = (
        "lakeformation grant-permissions --region us-east-1 --principal"
        " DataLakePrincipalIdentifier=arn:aws:iam::123456789012:role/Admin"
        f" --resource {_GLUE_CATALOG} --permissions DROP"
    )
    delete = f"glue delete-connection --connection-name {_GLUE_OURS}"
    steps = [f"glue delete-catalog --catalog-id {_GLUE_OURS}", grant, delete]
    positions = [calls.find(step) for step in steps]
    assert -1 not in positions and positions == sorted(positions), calls
    assert calls.count(f"glue delete-catalog --catalog-id {_GLUE_OURS}") == 2
    assert "after granting arn:aws:iam::123456789012:role/Admin DROP on it" in output


def test_a_failed_delete_after_granting_drop_revokes_the_grant(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "glue_lfadmin_dropfails")
    assert code == 1, output
    grants, revokes = calls.count("lakeformation grant-permissions"), calls.count("lakeformation revoke-permissions")
    assert grants >= 1 and revokes == grants, calls
    assert calls.index("lakeformation grant-permissions") < calls.index("lakeformation revoke-permissions")
    assert f"aws glue delete-catalog --region us-east-1 --catalog-id {_GLUE_OURS}" in output


def test_a_revoke_that_fails_after_a_failed_delete_reports_the_grant_left(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "glue_lfadmin_revokefail")
    assert code == 1, output
    assert "so the grant is still on it" in output
    revoke = (
        "aws lakeformation revoke-permissions --region us-east-1 --permissions DROP --principal"
        f" DataLakePrincipalIdentifier=arn:aws:iam::123456789012:role/Admin --resource '{_GLUE_CATALOG}'"
    )
    assert f"ERROR:   {revoke}" in output


def test_a_catalog_gone_by_the_retry_after_the_grant_counts_as_dropped(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "glue_lfadmin_retrynotfound")
    assert code == 0, output
    assert calls.count(f"glue delete-catalog --catalog-id {_GLUE_OURS}") == 2
    assert f"glue delete-connection --connection-name {_GLUE_OURS}" in calls
    assert "lakeformation revoke-permissions" not in calls  # the grant went with the catalog


def test_a_failure_step_8_clears_does_not_fail_the_run(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "glue_listonce")
    assert code == 0, output
    assert "Could not list the Glue connections" in output  # step 6, throttled
    assert "could not delete or look up" not in output
    assert calls.count(f"glue delete-connection --connection-name {_GLUE_OURS}") == 1


def test_a_catalog_whose_connection_is_gone_is_reported_not_a_failure(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "glue_orphancatalog")
    assert code == 0, output
    assert "Federated catalogs left whose connection is gone" in output
    # Matched by name alone: it may be an alias's, so the commands come with a warning.
    assert "they may belong to another" in output
    assert "deployment whose prefix and env sanitise to the same name: check that before running" in output
    assert f"aws glue delete-catalog --region us-east-1 --catalog-id {_GLUE_OURS}" in output


@pytest.mark.parametrize(
    ("scenario", "command"),
    [
        ("glue_deregdeny", f"aws lakeformation deregister-resource --region us-east-1 --resource-arn {_GLUE_ARN}"),
        ("glue_deletedeny", f"aws glue delete-connection --region us-east-1 --connection-name {_GLUE_OURS}"),
        ("glue_listdeny", "aws glue get-connections --region us-east-1"),  # not taken as "none"
    ],
)
def test_federated_glue_left_behind_fails_the_run_with_the_command(tmp_path: Path, scenario: str, command: str) -> None:
    code, output, calls = _run(tmp_path, scenario)
    assert code == 1, output
    assert command in output
    assert "No federated Glue connections" not in output
    if scenario == "glue_deregdeny":  # the connection still goes, so the network stack can
        assert calls.count(f"glue delete-connection --connection-name {_GLUE_OURS}") == 1
        assert calls.count("lakeformation deregister-resource") == 1


def test_a_registration_whose_connection_is_gone_is_deregistered(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "glue_dangling")
    assert code == 0, output
    assert f"Deregistered {_GLUE_ARN} from Lake Formation" in output


def test_a_connection_still_being_created_is_deleted_once_ready(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "glue_inprogress")
    assert code == 0, output
    assert "is still being created" in output
    assert calls.count("glue get-connection --name") == 3
    assert calls.rindex("glue get-connection --name") < calls.index("glue delete-connection")


def test_glue_enis_still_held_after_the_wait_fail_the_run(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "glue_held")
    assert code == 1, output
    assert "Glue is still releasing the ENIs of deleted federated connections on sg-connector: eni-glue1" in output
    assert "Teardown deferred" not in output
    assert "unbound variable" not in output
    assert "delete-network-interface" not in calls  # attached: AWS's to release
    assert "delete-stack --stack-name coa-dev-network" in calls  # its NAT gateway and endpoints still go


def test_a_network_stack_without_a_connector_group_is_not_a_glue_failure(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "glue_nosg")
    assert code == 0, output
    assert "No federated Glue connections of this deployment" in output
    assert "Values=sg-connector" not in calls


def test_a_failed_glue_eni_lookup_fails_the_run_without_waiting(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "glue_enilookupfail")
    assert code == 1, output
    assert "Could not tell whether Glue still holds ENIs of federated connections on sg-connector" in output
    assert "unknown (the lookup failed: An error occurred (UnauthorizedOperation))" in output
    assert "Waiting up to" not in output
    assert calls.count("Values=sg-connector") == 1  # not polled for 30 minutes


def test_an_aliased_connection_on_the_connector_group_is_not_waited_for(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "glue_rolealias_sg")
    assert code == 1, output
    assert "Leaving Glue connection coadevds_aaaaaaaaaaaaaaaa: named like this deployment's" in output
    assert "It uses the connector security group sg-connector: coa-dev-network cannot be deleted" in output
    assert "Waiting up to" not in output
    assert "glue delete-connection" not in calls


def test_no_glue_eni_is_deleted_while_a_connection_still_uses_the_group(tmp_path: Path) -> None:
    _, output, calls = _run(tmp_path, "glue_foreign_available_eni")
    assert "Glue connection demo-pg is not this deployment's but uses its connector security group" in output
    assert "delete-network-interface" not in calls  # the available ENI may be demo-pg's
    assert "Waiting up to" not in output


def test_an_orphaned_glue_eni_is_deleted_and_an_attached_one_awaited(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "glue_orphan")
    assert code == 0, output
    assert "Glue released them" in output
    deleted = re.findall(r"delete-network-interface --network-interface-id (\S+)", calls)
    assert deleted == ["eni-glue-orphan"]  # neither the attached one nor one that is not Glue's
    assert calls.index("eni-glue-orphan") < calls.index("delete-stack --stack-name coa-dev-network")


def test_agentcore_alone_holding_after_the_glue_sweep_still_defers(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "glue_agentcore")
    assert code == 3, output
    assert "Teardown deferred" in output
    assert f"glue delete-connection --connection-name {_GLUE_OURS}" in calls


def test_a_lambda_eni_holding_a_network_group_is_swept_and_the_stack_deleted_again(tmp_path: Path) -> None:
    # AgentCore holds the runtime groups too: still a deferral, once the Lambda ENI is gone.
    code, output, calls = _run(tmp_path, "lambdapinned")
    assert code == 3, output
    assert "delete-network-interface --network-interface-id eni-lambda1" in calls
    assert calls.count("delete-stack --stack-name coa-dev-network") == 2


def test_a_group_whose_stack_is_still_there_is_left_to_it(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "ownerlive")
    assert code == 1, output  # the verification reports the live stack
    assert "delete-security-group --group-id sg-mcp" not in calls
    assert "delete-security-group --group-id sg-serve" in calls


def test_already_deleted_security_group_is_not_pending(tmp_path: Path) -> None:
    code, output, _ = _run(tmp_path, "sgnotfound")
    assert code == 0, output


# An imported VPC needs its OpenSearch Serverless endpoint: synth fails without it.
_IMPORTED_VPC = {"SCL_VPC_ID": "vpc-imported", "SCL_AOSS_VPC_ENDPOINT_ID": "vpce-0abc123def4567890"}


def test_imported_vpc_deletes_the_network_stack_and_waits_only_for_the_groups(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "imported", **_IMPORTED_VPC)
    assert code == 3, output
    assert "delete-stack --stack-name coa-dev-network" in calls
    assert "aws ec2 delete-security-group --region us-east-1 --group-id sg-mcp" in output


def test_imported_vpc_rerun_command_keeps_the_vpc_variables(tmp_path: Path) -> None:
    code, output, _ = _run(tmp_path, "imported", **_IMPORTED_VPC)
    assert code == 3, output
    assert "SCL_VPC_ID=vpc-imported SCL_AOSS_VPC_ENDPOINT_ID=vpce-0abc123def4567890 ./scripts/destroy.sh" in output


def test_imported_vpc_without_endpoint_fails_before_synth(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "imported", SCL_VPC_ID="vpc-imported")
    assert code == 1, output
    assert "also set SCL_AOSS_VPC_ENDPOINT_ID" in output
    assert "cdk" not in calls


def test_imported_vpc_waits_for_the_enis_too(tmp_path: Path) -> None:
    code, output, _ = _run(tmp_path, "imported", **_IMPORTED_VPC, SCL_ENI_WAIT_MAX_SECONDS="1")
    assert code == 3, output
    assert "Waiting up to 1s" in output


def test_region_split_is_refused(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "clean", AWS_DEFAULT_REGION="eu-west-1")
    assert code == 1, output
    assert "set them to the same region" in output
    assert "delete-agent-runtime" not in calls


def test_the_aoss_inspect_stack_is_deleted_before_the_network_stack(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "inspect")
    assert code == 0, output
    delete_inspect = "delete-stack --stack-name coa-integ-aoss-inspect"
    assert calls.index(delete_inspect) < calls.index("delete-stack --stack-name coa-dev-network")
    # Awaited in step 8, and its detached Lambda ENIs are swept like the deployment's own.
    assert calls.rindex("describe-stacks --stack-name coa-integ-aoss-inspect") > calls.index(delete_inspect)
    assert "AWS Lambda VPC ENI-coa-integ-aoss-inspect*" in calls


def test_a_deployment_without_the_inspect_stack_never_mentions_it(tmp_path: Path) -> None:
    # The inspect stack is internal test infrastructure; customers never have it.
    code, output, _ = _run(tmp_path, "clean")
    assert code == 0, output
    assert "aoss-inspect" not in output


def test_an_aoss_inspect_stack_that_will_not_delete_fails_the_teardown(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "inspectfail")
    assert code == 1, output
    assert calls.count("delete-stack --stack-name coa-integ-aoss-inspect") == 2  # again after the ENI sweep
    assert "coa-integ-aoss-inspect\tDELETE_FAILED" in output


def test_a_failed_inspect_stack_lookup_does_not_mark_a_customer_run_unverified(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "inspectunknown")
    assert code == 0, output
    assert "Could not check for the integ test inspect stack" in output
    assert "delete-stack --stack-name coa-integ-aoss-inspect" not in calls


def test_an_inspect_stack_hidden_by_a_failed_lookup_is_still_reported(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "inspectflaky")
    assert code == 1, output
    assert "coa-integ-aoss-inspect\tCREATE_COMPLETE" in output
    assert "delete-stack --stack-name coa-integ-aoss-inspect" not in calls


def test_the_vkg_drain_wait_describes_more_than_ten_services_in_batches(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "vkg_many")
    assert code == 0, output
    describes = [line for line in calls.splitlines() if "ecs describe-services" in line]
    assert len(describes) == 2  # 11 services: one call of 10, one of 1
    assert all(line.count("service/coa-dev-vkg-cluster/") <= 10 for line in describes)
    assert "All VKG services INACTIVE." in output
    assert "describe-services failed" not in output


def test_a_cli_warning_on_a_successful_describe_is_not_a_failure(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "vkg_warning")
    assert code == 0, output
    assert calls.count("ecs describe-services") == 1
    assert "All VKG services INACTIVE." in output
    assert "describe-services failed" not in output


def test_a_failed_describe_is_reported_with_its_error_on_one_line(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "vkg_throttled")
    assert code == 0, output
    assert calls.count("ecs describe-services") == 2  # retried after the failure
    assert (
        "WARN:  describe-services failed: An error occurred (ThrottlingException) when calling the"
        " DescribeServices operation: Rate exceeded\n"
    ) in output
    assert "All VKG services INACTIVE." in output


def test_a_describe_that_returns_no_count_is_not_reported_as_a_failed_call(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "vkg_none")
    assert code == 0, output
    assert calls.count("ecs describe-services") == 2
    assert "describe-services returned unexpected output instead of a count: 'None'" in output
    assert "describe-services failed" not in output
    assert "All VKG services INACTIVE." in output


def test_the_vkg_drain_wait_sums_the_batches_until_every_service_is_inactive(tmp_path: Path) -> None:
    # 23 services in batches of 10, 10 and 3. Still active per poll: 7+10+3, 3+10+0, 0+10+0, then 0.
    # The middle batch drains last, so counting only the first batch would stop after the third
    # poll, and counting only the last one after the second.
    code, output, calls = _run(tmp_path, "vkg_drain")
    assert code == 0, output
    describes = [line for line in calls.splitlines() if "ecs describe-services" in line]
    assert len(describes) == 12  # 4 polls of 3 batches
    assert [line.count("service/coa-dev-vkg-cluster/") for line in describes] == [10, 10, 3] * 4
    for k in range(1, 24):  # every service in exactly one batch per poll
        assert sum(bool(re.search(rf"/coa-dev-vkg-ns{k} ", line)) for line in describes) == 4, k
    assert "All VKG services INACTIVE." in output
    assert "still not INACTIVE" not in output


def test_a_vkg_cluster_gone_during_the_drain_wait_counts_as_no_services_left(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "vkg_clustergone")
    assert code == 0, output
    assert calls.count("ecs describe-services") == 1  # no retries for the whole wait budget
    assert "VKG cluster 'coa-dev-vkg-cluster' no longer exists, so no services remain" in output
    assert "ClusterNotFoundException" in output
    assert "describe-services failed" not in output


@pytest.mark.parametrize(
    ("scenario", "warning"),
    [
        ("vkg_batchfail", "describe-services failed: An error occurred (ThrottlingException)"),
        ("vkg_batchnone", "describe-services returned unexpected output instead of a count: 'None'"),
    ],
)
def test_a_later_batch_failing_discards_the_poll_instead_of_using_the_partial_count(
    tmp_path: Path, scenario: str, warning: str
) -> None:
    # 11 services: batch 1 is drained from the start, batch 2 fails on the first poll,
    # has 1 left on the second and none on the third. Batch 1's 0 alone must not end the wait.
    code, output, calls = _run(tmp_path, scenario)
    assert code == 0, output
    describes = [line for line in calls.splitlines() if "ecs describe-services" in line]
    assert [line.count("service/coa-dev-vkg-cluster/") for line in describes] == [10, 1] * 3
    assert output.count(warning) == 1
    assert "Treating the services as still active and retrying" in output
    assert "All VKG services INACTIVE." in output  # only after the third, complete poll


def test_describe_failures_through_the_whole_wait_budget_go_on_to_cdk_destroy(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "vkg_alwaysthrottled", SCL_ECS_WAIT_MAX_SECONDS="0")
    assert code == 0, output
    assert calls.count("ecs describe-services") == 1  # the budget ends the retries
    assert "Could not count the VKG services through the 0s" in output
    assert "All VKG services INACTIVE." not in output
    assert "cdk destroy" in calls


def test_an_error_that_only_mentions_cluster_not_found_is_retried(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "vkg_clustermention")
    assert code == 0, output
    assert calls.count("ecs describe-services") == 2
    assert "describe-services failed: An error occurred (AccessDeniedException)" in output
    assert "no longer exists" not in output
    assert "All VKG services INACTIVE." in output
