# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""SSM references in a synthesized cloud assembly, and the ones a deploy must refuse.

CloudFormation resolves an ``AWS::SSM::Parameter::Value<...>`` parameter and a ``{{resolve:ssm:...}}`` reference
at change-set time. A new value on a property whose change replaces the resource replaces it with no template
change, so ``cdk diff`` shows nothing. For example, a moving public AMI parameter can replace an EC2 instance.

Lists every reference as ``ok|replaces|REFUSE|UNLISTED public|own <stack> <resource>.<property> <- <parameter>``
and exits 5 on any REFUSE or UNLISTED:
- REFUSE: a public parameter (``/aws/service/...``, whose value AWS moves) feeds a property whose change replaces the
  resource. The reference is refused, not just a changed value: a dynamic reference has no deployed value to compare,
  and a parameter can move between this check and the change set. Pin the value instead, as a literal (an AMI id);
  a version-pinned reference (``{{resolve:ssm:/aws/service/...:42}}``) is still refused. The ARN form and a name
  without the leading slash are the same public parameter.
- replaces: the app's own parameter on a replacing property. It changes only when one of our stacks writes it, which
  that stack's diff shows.
- UNLISTED: the parameter's identity is unknown (no usable default, or a name the template builds from a token,
  such as ``Fn::Join ["", ["{{resolve:ssm:", {"Ref": ...}, "}}"]]``), or the type and property are in neither table.
  Check the CloudFormation reference and list them before deploying.

Run: ``python3 -I ssm_refs_check.py <cdk.out dir>``.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

REFUSED = 5
SSM_PARAMETER_TYPE = "AWS::SSM::Parameter::Value<"
# A name may hold ${...} variables (inside Fn::Sub), so "}" ends it only as part of the closing "}}".
DYNAMIC = re.compile(r"\{\{resolve:ssm(?:-secure)?:((?:[^}$]|\$\{[^}]*\}|\$(?!\{))+)\}\}")
SUB_NAME = re.compile(r"\$\{([A-Za-z0-9]+)\}")
TOKEN = "${"
JOINED_TOKEN = "${?}"
PUBLIC_PREFIX = "/aws/service/"
# An ARN field may be a ${...} pseudo parameter, which holds colons itself (${AWS::Partition}).
_ARN_FIELD = r"(?:\$\{[^}]*\}|[^:$])*"
ARN_PREFIX = re.compile(rf"^arn:{_ARN_FIELD}:ssm:{_ARN_FIELD}:(?P<account>{_ARN_FIELD}):parameter")
# The ARN account of a parameter the app itself owns: the deploying account, or none.
OWN_ACCOUNTS = frozenset({"", "${AWS::AccountId}"})
EVERY_PROPERTY = "*"
# Properties whose change replaces the resource ("Update requires: Replacement" in the CloudFormation reference).
REPLACING: dict[str, frozenset[str]] = {
    "AWS::EC2::Instance": frozenset(
        {"ImageId", "AvailabilityZone", "SubnetId", "KeyName", "PrivateIpAddress", "PlacementGroupName", "Tenancy"}
    ),
    "AWS::AutoScaling::LaunchConfiguration": frozenset({EVERY_PROPERTY}),
    "AWS::EC2::Volume": frozenset({"AvailabilityZone", "SnapshotId"}),
    "AWS::Lambda::Permission": frozenset({EVERY_PROPERTY}),
    "AWS::ECS::TaskDefinition": frozenset({EVERY_PROPERTY}),
}
# Properties that update in place ("No interruption"; custom resources get an Update event). Each pair is listed only
# after checking its "Update requires" line in the CloudFormation Template Reference
# (https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-<service>-<type>.html).
IN_PLACE: dict[str, frozenset[str]] = {
    "AWS::BedrockAgentCore::Runtime": frozenset({"AuthorizerConfiguration", "EnvironmentVariables"}),
    "AWS::IAM::ManagedPolicy": frozenset({"PolicyDocument"}),
    "AWS::ApiGateway::RestApi": frozenset({"Body"}),
    "AWS::CloudFront::ResponseHeadersPolicy": frozenset({"ResponseHeadersPolicyConfig"}),
    "AWS::IAM::Policy": frozenset({"PolicyDocument"}),
    "AWS::Lambda::Function": frozenset({"Environment"}),
    "AWS::OpenSearchServerless::AccessPolicy": frozenset({"Policy"}),
    "AWS::StepFunctions::StateMachine": frozenset({"DefinitionString"}),
    "Custom::AWS": frozenset({"Create", "Update"}),
    "Custom::CDKBucketDeployment": frozenset({"SourceMarkers"}),
}


def _sources(value: Any, ssm_parameters: dict[str, str]) -> Iterator[str]:
    """The SSM parameter names a property value reads, through Ref, Fn::Sub, Fn::Join and dynamic references."""
    if isinstance(value, str):
        yield from DYNAMIC.findall(value)
    elif isinstance(value, list):
        for item in value:
            yield from _sources(item, ssm_parameters)
    elif isinstance(value, dict):
        if value.keys() == {"Ref"} and value["Ref"] in ssm_parameters:
            yield ssm_parameters[value["Ref"]]
            return
        if "Fn::Join" in value:
            yield from _joined_references(value["Fn::Join"])
        if "Fn::Sub" in value:
            sub = value["Fn::Sub"]
            text = sub if isinstance(sub, str) else sub[0]
            # A name in the variable map is substituted from that map, never from a parameter.
            local = sub[1] if isinstance(sub, list) and len(sub) > 1 and isinstance(sub[1], dict) else {}
            yield from (
                ssm_parameters[name] for name in SUB_NAME.findall(text) if name in ssm_parameters and name not in local
            )
        for item in value.values():
            yield from _sources(item, ssm_parameters)


def _joined_references(join: Any) -> Iterator[str]:
    """Dynamic references spread over Fn::Join pieces, each non-literal piece standing in as ``${?}``."""
    if not (isinstance(join, list) and len(join) == 2 and isinstance(join[1], list)):
        return
    separator, pieces = join
    text = str(separator).join(piece if isinstance(piece, str) else JOINED_TOKEN for piece in pieces)
    yield from DYNAMIC.findall(text)


def ownership(parameter: str) -> str:
    """``public``, ``own`` or ``unknown``, from the literal start of the name, before any token.

    ``arn:...:parameter/aws/service/x`` and ``aws/service/x`` are the same parameter as ``/aws/service/x``. A name
    whose literal start could still become ``/aws/service/`` once a token resolves is ``unknown``, and so is an ARN in
    another (or a token-built) account: a parameter shared from another account can move like a public one.
    """
    if parameter.startswith("<unresolved:"):
        return "unknown"
    arn = ARN_PREFIX.match(parameter)
    name = parameter[arn.end() :] if arn else parameter
    literal = name.split(TOKEN, 1)[0]
    if not literal:
        return "unknown"
    path = literal if literal.startswith("/") else f"/{literal}"
    if path.startswith(PUBLIC_PREFIX):
        return "public"
    if TOKEN in name and PUBLIC_PREFIX.startswith(path):
        return "unknown"
    if arn and arn.group("account") not in OWN_ACCOUNTS:
        return "unknown"
    return "own"


def _parameter_name(name: str, parameter: dict[str, Any]) -> str:
    default = parameter.get("Default")
    return default if isinstance(default, str) and default else f"<unresolved:{name}>"


def references(template: dict[str, Any]) -> Iterator[tuple[str, str, str, str]]:
    """``(resource, type, top-level property, parameter name)`` for every SSM value a resource property reads."""
    ssm_parameters = {
        name: _parameter_name(name, p)
        for name, p in template.get("Parameters", {}).items()
        if str(p.get("Type", "")).startswith(SSM_PARAMETER_TYPE)
    }
    for resource, body in template.get("Resources", {}).items():
        for prop, value in (body.get("Properties") or {}).items():
            for parameter in sorted(set(_sources(value, ssm_parameters))):
                yield resource, body.get("Type", ""), prop, parameter


def _listed(table: dict[str, frozenset[str]], resource_type: str, prop: str) -> bool:
    properties = table.get(resource_type, frozenset())
    return EVERY_PROPERTY in properties or prop in properties


def verdict(resource_type: str, prop: str, *, public: bool) -> str:
    """``REFUSE`` or ``replaces`` on a replacing property, ``ok`` on an in-place one, else ``UNLISTED``."""
    if _listed(REPLACING, resource_type, prop):
        return "REFUSE" if public else "replaces"
    return "ok" if _listed(IN_PLACE, resource_type, prop) else "UNLISTED"


def main(folder: Path) -> int:
    templates = sorted(folder.glob("*.template.json")) if folder.is_dir() else []
    if not templates:
        print(f"no *.template.json in {folder}", file=sys.stderr)
        return 2
    refused = 0
    for path in templates:
        stack = path.name.removesuffix(".template.json")
        for resource, resource_type, prop, parameter in references(json.loads(path.read_text())):
            owner = ownership(parameter)
            found = "UNLISTED" if owner == "unknown" else verdict(resource_type, prop, public=owner == "public")
            refused += found in {"REFUSE", "UNLISTED"}
            print(f"{found:<8} {owner:<6} {stack} {resource}.{prop} <- {parameter}")
    if refused:
        print(
            f"REFUSE: {refused} SSM reference(s) have an unresolved identity, feed a replacing property (public), "
            "or a property not known to update in place (UNLISTED); pin them, or list the property after checking "
            "the CloudFormation reference"
        )
        return REFUSED
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: ssm_refs_check.py <cdk.out dir>")
    sys.exit(main(Path(sys.argv[1])))
