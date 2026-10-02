#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Migrate existing credential secrets onto the ``<prefix>:namespace`` binding.

A credential secret must carry a ``<prefix>:namespace`` tag whose
value lists the namespace that owns the source (see ``coa_common.constants`` for
the key derivation and the strict value format). That is enforced at
registration, on update, and — since the scan-time re-check — on **every scan**.
So a secret that predates the rule does not merely block edits: the source's next
scan fails until its namespace is listed. This script closes that gap for an
existing deployment.

Both sub-types that hold a credential secret are covered. ``DATABRICKS_SQL_WAREHOUSE``
is here for repair rather than migration — its secrets are bound at registration,
so none predates the rule — but a tag edited out of band leaves the source
unscannable in exactly the same way.

It reads the sources table, resolves each source's credential secret, and
reports what the binding requires. ``--apply`` then writes the tag. It is
idempotent: a second run over a migrated deployment reports everything ``ok`` and
changes nothing.

Because the tag value is a **list**, a secret shared by several namespaces is a
supported configuration rather than a conflict — the fix is to add the missing
namespaces to the existing value, which ``--apply`` does. Two consequences worth
knowing:

* **Single-writer only.** Appending is a read-modify-write on one tag, so two
  copies of this script (or anything else tagging concurrently) can drop entries.
  Run it once, from one place. The integ suite deliberately takes per-namespace
  *copies* of a secret for exactly this reason — see ``bind_secret_to_namespace``
  in ``tests/integ/fixtures.py``.
* **There is a ceiling.** Secrets Manager caps a tag value at 256 characters,
  which is 6 namespace UUIDs. A secret that would exceed it is reported rather
  than truncated, because a truncated value fails to parse and would take down
  every namespace already on it.

Outcomes:

``ok``
    Already lists every namespace that uses it. Nothing to do.

``needs-tag``
    In-account, and one or more namespaces that use it are missing from the tag
    (including the case of no tag at all). ``--apply`` writes the union of what
    is already there and what the sources table says is needed, preserving
    existing entries.

``over-capacity``
    The union would exceed the 256-character tag-value cap. Reported, never
    written: give some namespaces their own copy of the secret instead.

``malformed``
    A tag exists but does not parse (not canonical, or an entry that is not a
    namespace UUID). Never overwritten — the value was written by whoever owns
    the secret, and silently replacing it could revoke a namespace that is
    currently working. Fix the value by hand, then re-run.

``cross-account``
    The secret lives in another account. Out of scope: the deployment does not
    own those tags, and access there is authorized by the secret's own resource
    policy.

``unreadable``
    ``DescribeSecret`` failed (deleted secret, denied, wrong region). Reported so
    a dangling reference surfaces here rather than as a mystery SCAN_FAILED later.

Exit status: ``0`` when nothing needs attention (or ``--apply`` fixed everything
it could), ``1`` when any ``over-capacity`` / ``malformed`` / ``unreadable`` row
remains — those need a human, and a non-zero exit keeps a deploy pipeline from
rolling past them.

Usage
-----
    # Report only. Safe, read-only, makes no changes.
    scripts/tag_credential_secrets.py --table coa-dev-sources --region us-east-1

    # Apply the unambiguous fixes.
    scripts/tag_credential_secrets.py --table coa-dev-sources --region us-east-1 --apply

The tag KEY carries the deployment's prefix, so pass ``--tag-prefix`` when the
deployment is not the default (or set ``RESOURCE_TAG_PREFIX``) — tagging under the
wrong key leaves the secret invisible to the deployment's own IAM conditions.

Run this BEFORE deploying the scan-time re-check, so no source scans in the
window between the deploy and the tagging.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import BotoCoreError, ClientError

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "libs" / "common" / "src"))

from coa_common.constants import (  # noqa: E402
    NAMESPACE_TAG_SEPARATOR,
    namespace_tag_key,
    parse_namespace_tag,
)

# Sub-types whose configuration carries a `credentialSecretArn` governed by the
# namespace binding.
CREDENTIAL_SUB_TYPES = frozenset({"JDBC_DATABASE", "DATABRICKS_SQL_WAREHOUSE"})

# Secrets Manager caps a tag VALUE at 256 characters.
TAG_VALUE_MAX = 256

# Ordered worst-first so the report leads with what needs a human.
_BLOCKING = ("malformed", "over-capacity", "unreadable")
_STATUS_ORDER = (*_BLOCKING, "needs-tag", "cross-account", "ok")


@dataclass
class SecretRef:
    """One credential secret and every source that points at it."""

    arn: str
    namespaces: set[str] = field(default_factory=set)
    sources: list[tuple[str, str]] = field(default_factory=list)  # (namespaceId, name)
    status: str = ""
    detail: str = ""
    new_value: str = ""  # the tag value --apply would write

    @property
    def region(self) -> str:
        parts = self.arn.split(":")
        return parts[3] if len(parts) > 5 else ""

    @property
    def account(self) -> str:
        parts = self.arn.split(":")
        return parts[4] if len(parts) > 5 else ""

    @property
    def short(self) -> str:
        return self.arn.split(":secret:")[-1] or self.arn


def _iter_credential_sources(table: str, region: str):
    """Yield ``(namespaceId, sourceName, credentialSecretArn)`` per credential-holding source.

    A full table scan is right here: this runs once per deployment as a migration,
    the table holds sources (tens to low thousands), and there is no index on
    sourceSubType. Paginated so a large table does not truncate silently.

    Items are unwrapped with ``TypeDeserializer`` rather than by taking the single
    value of each top-level attribute. ``configuration`` is a JSON *string* on
    records the API wrote, but a DynamoDB *map* on some older ones (the handlers
    tolerate both — see ``discovery_handler``), and a map's contents are themselves
    still in wire form. A one-level unwrap would hand back ``{"S": "arn:..."}`` and
    the ARN would be silently missed.
    """
    deserializer = TypeDeserializer()
    ddb = boto3.client("dynamodb", region_name=region)
    for page in ddb.get_paginator("scan").paginate(TableName=table):
        for raw in page.get("Items", []):
            item = {k: deserializer.deserialize(v) for k, v in raw.items()}
            if item.get("sourceSubType") not in CREDENTIAL_SUB_TYPES:
                continue
            config: Any = item.get("configuration") or "{}"
            if isinstance(config, str):
                try:
                    config = json.loads(config)
                except (json.JSONDecodeError, TypeError):
                    config = {}
            if not isinstance(config, dict):
                config = {}
            arn = config.get("credentialSecretArn")
            if not arn:
                continue
            namespace_id = item.get("namespaceId") or str(item.get("PK", "")).removeprefix("NS#")
            yield namespace_id, item.get("name", "(unnamed)"), arn


def _collect(table: str, region: str) -> dict[str, SecretRef]:
    refs: dict[str, SecretRef] = {}
    for namespace_id, name, arn in _iter_credential_sources(table, region):
        ref = refs.setdefault(arn, SecretRef(arn=arn))
        ref.namespaces.add(namespace_id)
        ref.sources.append((namespace_id, name))
    return refs


def _classify(refs: dict[str, SecretRef], deployment_account: str, default_region: str, tag_key: str) -> None:
    """Assign a status to every ref. Read-only — never writes a tag."""
    for ref in refs.values():
        if ref.account and ref.account != deployment_account:
            ref.status = "cross-account"
            ref.detail = f"owned by {ref.account}; authorized by its own resource policy"
            continue
        if not ref.account:
            ref.status = "unreadable"
            ref.detail = "malformed ARN: no account segment"
            continue

        sm = boto3.client("secretsmanager", region_name=ref.region or default_region)
        try:
            described = sm.describe_secret(SecretId=ref.arn)
        except (ClientError, BotoCoreError) as exc:
            ref.status = "unreadable"
            ref.detail = f"DescribeSecret failed: {type(exc).__name__}"
            continue
        tags = {t["Key"]: t.get("Value", "") for t in described.get("Tags", []) if "Key" in t}
        raw = tags.get(tag_key)

        existing: list[str] = []
        if raw is not None:
            try:
                existing = parse_namespace_tag(raw)
            except ValueError as exc:
                # Never overwrite a value we cannot parse: it was written by whoever
                # owns the secret, and replacing it could revoke a namespace that is
                # working today.
                ref.status = "malformed"
                ref.detail = f"tag {tag_key}={raw!r} does not parse: {exc}"
                continue

        missing = sorted(ref.namespaces - set(existing))
        if not missing:
            ref.status = "ok"
            ref.detail = f"already lists {len(existing)} namespace(s), including every source's"
            continue

        # Preserve the existing order and append what is missing, so an operator
        # reading the tag can see what was there before.
        union = existing + missing
        value = NAMESPACE_TAG_SEPARATOR.join(union)
        if len(value) > TAG_VALUE_MAX:
            ref.status = "over-capacity"
            ref.detail = (
                f"would need {len(union)} namespaces ({len(value)} chars), over the {TAG_VALUE_MAX}-char tag-value cap"
            )
            continue
        ref.status = "needs-tag"
        ref.new_value = value
        ref.detail = f"would add {', '.join(missing)}" + (
            f" to the {len(existing)} already listed" if existing else " (no tag today)"
        )


def _apply(refs: dict[str, SecretRef], default_region: str, tag_key: str) -> tuple[int, int]:
    """Write the tag for every ``needs-tag`` ref. Returns ``(tagged, failed)``."""
    tagged = failed = 0
    for ref in refs.values():
        if ref.status != "needs-tag":
            continue
        sm = boto3.client("secretsmanager", region_name=ref.region or default_region)
        try:
            sm.tag_resource(SecretId=ref.arn, Tags=[{"Key": tag_key, "Value": ref.new_value}])
        except (ClientError, BotoCoreError) as exc:
            ref.status = "unreadable"
            ref.detail = f"TagResource failed: {type(exc).__name__}"
            failed += 1
            continue
        ref.status = "ok"
        ref.detail = f"tagged {tag_key}={ref.new_value!r}"
        tagged += 1
    return tagged, failed


def _report(refs: dict[str, SecretRef], applied: bool, tag_key: str) -> None:
    by_status: dict[str, list[SecretRef]] = defaultdict(list)
    for ref in refs.values():
        by_status[ref.status].append(ref)

    print(f"\n{len(refs)} credential secret(s) referenced by database sources; tag key {tag_key!r}\n")
    for status in _STATUS_ORDER:
        rows = sorted(by_status.get(status, []), key=lambda r: r.short)
        if not rows:
            continue
        print(f"  {status.upper()} ({len(rows)})")
        for ref in rows:
            print(f"    {ref.short}")
            print(f"      {ref.detail}")
            for ns, name in sorted(ref.sources):
                print(f"      source: {name}  (namespace {ns})")
        print()

    blocking = [r for s in _BLOCKING for r in by_status.get(s, [])]
    pending = by_status.get("needs-tag", [])
    if pending and not applied:
        print(f"{len(pending)} secret(s) can be tagged automatically — re-run with --apply.\n")
    if blocking:
        print(
            f"{len(blocking)} secret(s) need a human:\n"
            "  MALFORMED     the existing tag value does not parse. It is never overwritten —\n"
            "                fix it by hand (namespace UUIDs, single spaces) and re-run.\n"
            "  OVER-CAPACITY the union would exceed the 256-char tag-value cap. Give some\n"
            "                namespaces their own copy of the secret instead of truncating.\n"
            "  UNREADABLE    the reference is dangling or unreadable. Fix or delete the source.\n"
        )
    if not blocking and not pending:
        print("Nothing to do — every in-account credential secret lists the namespaces that use it.\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--table", required=True, help="Sources table name, e.g. coa-dev-sources")
    ap.add_argument("--region", required=True, help="Region the sources table lives in")
    ap.add_argument(
        "--tag-prefix",
        default=None,
        help=(
            "Deployment's BARE resource prefix, keying the namespace tag (default: RESOURCE_TAG_PREFIX env, else 'coa')"
        ),
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Write the tag for the unambiguous (needs-tag) secrets. Without this the run is read-only.",
    )
    args = ap.parse_args()

    tag_key = namespace_tag_key(args.tag_prefix)

    try:
        account = boto3.client("sts", region_name=args.region).get_caller_identity()["Account"]
    except (ClientError, BotoCoreError) as exc:
        print(f"Could not resolve the deployment account: {exc}", file=sys.stderr)
        return 2
    print(f"Deployment account {account}, table {args.table} ({args.region}), tag key {tag_key!r}")

    refs = _collect(args.table, args.region)
    if not refs:
        print("\nNo sources with a credential secret — nothing to migrate.\n")
        return 0

    _classify(refs, account, args.region, tag_key)
    if args.apply:
        tagged, failed = _apply(refs, args.region, tag_key)
        print(f"\nApplied: {tagged} tagged, {failed} failed")
    _report(refs, applied=args.apply, tag_key=tag_key)

    return 1 if any(r.status in _BLOCKING for r in refs.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
