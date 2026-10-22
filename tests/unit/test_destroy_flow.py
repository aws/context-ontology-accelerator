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
_NETWORK_FAILURES = {"netfail": _NAMESPACE, "heldnetfail": _NAMESPACE, "netsgpinned": _NETWORK_SG}

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
        lambdapinned|foreigneni) return 0 ;;
        released)  # AWS lets go during the ENI wait
          [ -f "$ST/network-deleting" ] || return 0
          n=$(cat "$ST/eni-lookups" 2>/dev/null || echo 0); echo $((n + 1)) > "$ST/eni-lookups"
          [ "$n" -lt 4 ]; return ;;
      esac
      return 1
    }
    rt=coa_dev; [ "$S" = otherenv ] && rt=coa_dev_b  # otherenv: a sibling env took over the pointers
    case "$a" in
      *"configure get region"*) echo us-east-1 ;;
      *"sts get-caller-identity"*) echo 123456789012 ;;
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
      *"describe-network-interfaces"*"group-id"*)
        case "$S" in
          enideny) echo "An error occurred (UnauthorizedOperation)" >&2; exit 254 ;;
          foreign) echo interface ;;
          *) if held; then printf 'agentic_ai\tagentic_ai\n'; fi ;;
        esac ;;
      *"describe-network-interfaces"*"vpc-id"*"agentic_ai"*)  # ENIs that are not AgentCore's
        [ "$S" = foreigneni ] && printf 'eni-foreign\tinterface\tRDSNetworkInterface\n' ;;
      *"describe-network-interfaces"*"available"*)
        case "$S" in
          lambda) [ -f "$ST/lambda-eni-deleted" ] || printf 'eni-lambda1\tAWS Lambda VPC ENI-coa-dev-old-fn\n' ;;
          lambdapinned)  # detached while CloudFormation deleted the network stack
            [ -f "$ST/network-deleting" ] && [ ! -f "$ST/lambda-eni-deleted" ] \
              && printf 'eni-lambda1\tAWS Lambda VPC ENI-coa-dev-old-fn\n' ;;
        esac ;;
      *"lambda get-function"*) echo "An error occurred (ResourceNotFoundException)" >&2; exit 254 ;;
      *"delete-network-interface"*"eni-lambda1"*) touch "$ST/lambda-eni-deleted" ;;
      *"ecs list-services"*) : ;;
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
        case "$S" in imported) echo None ;; lambda) echo vpc-from-stack ;; *) echo vpc-123 ;; esac ;;
      *"list-stack-resources"*"coa-dev-network"*"$billable"*)
        if [ "$S" = natleft ]; then echo NatGateway1
        elif [ ! -f "$ST/network-deleting" ] && [ ! -f "$ST/network-gone" ]; then
          printf 'NatGateway1\tVpcEndpoint1\n'
        fi ;;
      *"list-stack-resources"*"coa-dev-network"*"--output json"*)
        f=$FAKE_NETWORK_FAILURE  # lambdapinned: the Lambda ENI holds a network stack group until swept
        [ "$S" = lambdapinned ] && [ ! -f "$ST/lambda-eni-deleted" ] && f="$f,$FAKE_NETWORK_SG_FAILURE"
        echo "{\"StackResourceSummaries\":[$f]}" ;;
      *"list-stack-resources"*"coa-dev-network"*) printf 'AWS::EC2::VPC\tvpc-123\n' ;;
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
          domaingone) touch "$ST/network-gone" ;;
          netslow) touch "$ST/network-slow" ;;
          refused) echo "An error occurred (ValidationError): Role is invalid or cannot be assumed" >&2; exit 254 ;;
          lambda)
            if [ -f "$ST/lambda-eni-deleted" ]; then touch "$ST/network-gone"; else touch "$ST/network-deleting"; fi ;;
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
        sgfailonce|connbusy|foreigneni|domaingone|netslow) exit 0 ;;
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
    state = tmp_path / "state"
    bin_dir.mkdir()
    state.mkdir()
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


def test_imported_vpc_deletes_the_network_stack_and_waits_only_for_the_groups(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "imported", SCL_VPC_ID="vpc-imported")
    assert code == 3, output
    assert "delete-stack --stack-name coa-dev-network" in calls
    assert "aws ec2 delete-security-group --region us-east-1 --group-id sg-mcp" in output


def test_imported_vpc_waits_for_the_enis_too(tmp_path: Path) -> None:
    code, output, _ = _run(tmp_path, "imported", SCL_VPC_ID="vpc-imported", SCL_ENI_WAIT_MAX_SECONDS="1")
    assert code == 3, output
    assert "Waiting up to 1s" in output


def test_region_split_is_refused(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "clean", AWS_DEFAULT_REGION="eu-west-1")
    assert code == 1, output
    assert "set them to the same region" in output
    assert "delete-agent-runtime" not in calls
