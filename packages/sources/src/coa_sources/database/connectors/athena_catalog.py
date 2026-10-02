# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Athena data-catalog lifecycle for custom-connector (``CUSTOM_CONNECTOR``) sources.

A customer authors an Athena Query Federation SDK connector and deploys it as a
Lambda in **their own** account. An Athena data catalog, however, is account- and
region-local: a query running in our account can only reference catalogs
registered in our account. So we register the catalog **here**, pointing at their
Lambda ARN — we do not consume a catalog registered on their side, which would be
unreferenceable from our Athena.

Registration only records a name → ARN mapping. It does not invoke the Lambda, so
it succeeds even before the customer has granted us invoke on it; a missing grant
surfaces later, at discovery, where ``test_connection`` reports it with an
actionable message.

Two design points that are easy to get wrong:

* **We derive the catalog name; the customer never supplies one.** Athena catalog
  names are account+region-global, so a customer-chosen name would let two
  sources collide — and under a get-then-create guard the second source would
  silently bind to the *first* source's Lambda, so deleting one would break the
  other. The name is therefore derived from the source id via the same
  :func:`~.glue_connection_provisioner.build_catalog_name` the federated-JDBC path
  uses, which both guarantees a 1:1 source↔catalog mapping and keeps every
  catalog inside the ``{sanitizedPrefix}ds_*`` window the IAM policy is scoped to.
* **Idempotency is a get-then-create check, not a caught duplicate error.**
  ``CreateDataCatalog`` declares only ``InternalServerException`` (500) and
  ``InvalidRequestException`` (400) — there is no ``AlreadyExistsException`` — so
  a duplicate name arrives as a 400 that no error code distinguishes from a
  malformed request.

* **The idempotency check stops discriminating when the handler ARN is shared.** It
  compares catalog type plus the *set of handler ARNs*, so where one connector Lambda
  serves every source of a sub-type that comparison is trivially true and a name
  collision reads as a retried create (``False``) instead of raising
  ``AthenaCatalogConflictError``. Such callers pass ``require_absent=True`` plus a
  ``source_id`` for the ``coa:sourceId`` tag delete verifies against. Both are opt-in,
  so ``CUSTOM_CONNECTOR``'s per-source ARN keeps its existing behaviour.
* Tagging needs ``athena:TagResource`` on create and ``athena:ListTagsForResource``
  to read back, because ``GetDataCatalog`` returns no tags at all.
"""

from __future__ import annotations

import boto3
import structlog
from botocore.exceptions import BotoCoreError, ClientError
from coa_common import resolve_region
from coa_common.aws_config import sync_boto_config
from coa_common.constants import require_resource_prefix

from coa_sources.database.connectors.glue_connection_provisioner import build_catalog_name

# structlog, not stdlib logging: this module runs in the sources-api Lambda, whose
# setup_logging() pins the stdlib root logger to WARNING and builds a formatter
# without ExtraAdder — so stdlib `logger.info(..., extra={...})` here would emit
# nothing at all, leaving the create and delete of a customer-visible AWS resource
# with no record.
logger = structlog.get_logger(__name__)

AWS_REGION = resolve_region()

# Athena's catalog type for a connector-Lambda-backed catalog, as opposed to
# GLUE (the account's Data Catalog) or FEDERATED (the managed-connector shape the
# JDBC path uses).
CATALOG_TYPE_LAMBDA = "LAMBDA"

# Parameter keys Athena uses to bind a LAMBDA catalog to its handler(s).
_PARAM_COMPOSITE_FUNCTION = "function"
_PARAM_METADATA_FUNCTION = "metadata-function"
_PARAM_RECORD_FUNCTION = "record-function"

# Tag recording which source a catalog was created for, so a delete can verify the
# catalog it is about to remove. Brand-fixed rather than derived from RESOURCE_PREFIX,
# matching the literal ``coa:connector`` tag in `coa-contract.ts`: a prefix-derived key
# would make every catalog written under an earlier prefix unverifiable after a rename.
SOURCE_ID_TAG_KEY = "coa:sourceId"

_athena_client = None
_account_id: str | None = None


def _athena():
    global _athena_client
    if _athena_client is None:
        # Source create/delete is a synchronous customer-path call, so the
        # fail-fast preset applies.
        _athena_client = boto3.client("athena", region_name=AWS_REGION, config=sync_boto_config())
    return _athena_client


class AthenaCatalogError(RuntimeError):
    """Registering or removing an Athena data catalog failed."""


class AthenaCatalogConflictError(AthenaCatalogError):
    """A catalog of that name exists and does not describe this source.

    Distinct from its parent so a caller's rollback can tell the two apart: on
    every other failure the catalog may be ours and half-created, and deleting it
    is the right cleanup — but here it demonstrably belongs to something else, and
    deleting it would destroy a resource we did not create.
    """


def derive_catalog_name(source_id: str) -> str:
    """Return the Athena data-catalog name for a source id.

    Deterministic, so every caller — create, delete, discovery, serve — arrives at
    the same name from the source record alone, with nothing extra persisted for
    them to disagree about.

    Raises:
        RuntimeError: ``RESOURCE_PREFIX`` is unset. The prefix reaches the name, so a
            default registers the source under a catalog nothing else in this deployment
            resolves, and the first query is where that shows up.
    """
    return build_catalog_name(require_resource_prefix(), source_id.removeprefix("DS#"))


def build_catalog_parameters(connector_function_arn: str) -> dict[str, str]:
    """Build the ``Parameters`` map binding a LAMBDA catalog to its handler.

    Athena accepts exactly one of two forms, and mixing them is rejected:

    * ``function=<arn>`` alone for a **composite** handler — one Lambda serving
      both the metadata and record paths, which is what the SDK's
      ``CompositeHandler`` produces and what almost every connector is.
    * ``metadata-function=<arn>`` **and** ``record-function=<arn>`` for a split
      deployment.

    Only the composite form is produced. ``CustomConnectorConfiguration`` models one ARN, so
    the split pair is unreachable from the API — see that shape for why. Athena still
    understands the pair, so restoring it means adding the member back and branching
    here; nothing else about the catalog changes.

    Note the pair keys are NOT dead here even so: Athena rewrites what it stores,
    which is what :func:`_handler_arns` has to cope with.
    """
    return {_PARAM_COMPOSITE_FUNCTION: connector_function_arn}


def _handler_arns(parameters: dict[str, str]) -> set[str]:
    """Every Lambda ARN a catalog's parameters point at, in either form.

    Reading all three keys is load-bearing, not defensive breadth: **Athena
    rewrites the parameter map on write.** Send the composite
    ``{"function": arn}`` and ``GetDataCatalog`` reports
    ``{"catalog": <name>, "metadata-function": arn, "record-function": arn}`` —
    the single key expanded into the pair, plus a ``catalog`` entry Athena adds
    itself. Verified live against a real registration; it is not documented.

    So the form sent is never the form read back, and comparing parameter *keys*
    would make :func:`register_lambda_catalog`'s idempotency check raise a
    spurious conflict on every re-registration. Comparing the set of ARNs makes
    both spellings equal, which is what keeps that check correct.
    """
    return {
        parameters[key]
        for key in (_PARAM_COMPOSITE_FUNCTION, _PARAM_METADATA_FUNCTION, _PARAM_RECORD_FUNCTION)
        if parameters.get(key)
    }


def register_lambda_catalog(
    *,
    catalog_name: str,
    connector_function_arn: str,
    client=None,
    require_absent: bool = False,
    source_id: str = "",
) -> bool:
    """Register (or confirm) a LAMBDA-type Athena data catalog.

    Args:
        catalog_name: The derived name; see :func:`derive_catalog_name`.
        connector_function_arn: The connector Lambda, serving both the metadata and
            record paths (Athena's composite form).
        client: Athena client override, for tests.
        require_absent: Treat ANY existing catalog of that name as a conflict rather
            than comparing handler ARNs. Set it wherever the handler ARN is **shared**
            across sources, which makes the ARN comparison trivially true and would
            report a real collision as a retried create.
        source_id: Source this catalog is being created for. When given, the catalog is
            tagged ``coa:sourceId`` so :func:`catalog_source_id` can verify ownership at
            delete time. Needs ``athena:TagResource``.

    Returns:
        ``True`` when this call created the catalog, ``False`` when an equivalent
        registration already existed (a retried create). Never ``False`` under
        ``require_absent``.

    Raises:
        AthenaCatalogConflictError: a catalog of that name exists and does not
            describe this source. Fatal rather than reconciled: the name is
            derived from a freshly generated source id, so a mismatch means an
            assumption has broken, and silently serving a source from someone
            else's connector would be a disclosure bug. Callers must NOT delete
            the catalog in response — it is not ours.
        AthenaCatalogError: the catalog could not be registered. The catalog may
            or may not exist afterwards (a read timeout after Athena committed the
            create is indistinguishable from a failure), so a caller rolling back
            should attempt a best-effort delete.
    """
    athena = client or _athena()
    parameters = build_catalog_parameters(connector_function_arn)

    existing = _describe_catalog(athena, catalog_name)
    if existing is not None:
        existing_type = existing.get("Type", "")
        existing_arns = _handler_arns(existing.get("Parameters") or {})
        if not require_absent and existing_type == CATALOG_TYPE_LAMBDA and existing_arns == _handler_arns(parameters):
            logger.info("athena_data_catalog_already_registered", catalog_name=catalog_name)
            return False
        # Type is part of the comparison because a GLUE-type catalog carries no
        # Parameters at all, so comparing ARNs alone would report "points at a
        # different Lambda ([])" for something that is not a Lambda catalog.
        raise AthenaCatalogConflictError(
            f"Athena data catalog {catalog_name!r} already exists (type {existing_type or 'unknown'}, "
            f"handlers {sorted(existing_arns)}) and does not describe this source; refusing to rebind it"
        )

    try:
        athena.create_data_catalog(
            Name=catalog_name,
            Type=CATALOG_TYPE_LAMBDA,
            Parameters=parameters,
            **({"Tags": [{"Key": SOURCE_ID_TAG_KEY, "Value": source_id}]} if source_id else {}),
        )
    except (ClientError, BotoCoreError) as exc:
        raise AthenaCatalogError(f"Failed to register Athena data catalog {catalog_name!r}: {exc}") from exc

    logger.info("athena_data_catalog_registered", catalog_name=catalog_name, parameter_keys=sorted(parameters))
    return True


def _partition(region: str) -> str:
    if region.startswith("us-gov-"):
        return "aws-us-gov"
    if region.startswith("cn-"):
        return "aws-cn"
    return "aws"


def _deployment_account(client=None) -> str:
    """This deployment's account id (cached STS lookup)."""
    global _account_id  # noqa: PLW0603
    if _account_id is None:
        sts = client or boto3.client("sts", region_name=AWS_REGION, config=sync_boto_config())
        _account_id = sts.get_caller_identity()["Account"]
    return _account_id


def catalog_arn(catalog_name: str, sts_client=None) -> str:
    """ARN of a data catalog, which is what the tagging APIs address it by.

    ``GetDataCatalog`` takes a name and returns no tags; ``ListTagsForResource`` takes an
    ARN, so reading a catalog's tags needs the account id too.
    """
    account = _deployment_account(sts_client)
    return f"arn:{_partition(AWS_REGION)}:athena:{AWS_REGION}:{account}:datacatalog/{catalog_name}"


def catalog_source_id(catalog_name: str, *, client=None, sts_client=None) -> str | None:
    """Source id a catalog is tagged with, or ``None`` when it carries no such tag.

    ``None`` and a *different* source id are not the same answer: no tag means the catalog
    predates tagging (or came from the ``CUSTOM_CONNECTOR`` path, which does not tag) and
    a delete should proceed, while a different id means the name resolves to somebody
    else's catalog and a delete must refuse.

    Raises:
        AthenaCatalogError: the tags could not be read. Deliberately not swallowed into
            ``None``: a permissions or throttling fault read as "untagged" would turn the
            verification off exactly when it is least trustworthy.
    """
    athena = client or _athena()
    try:
        arn = catalog_arn(catalog_name, sts_client)
        tags = athena.list_tags_for_resource(ResourceARN=arn).get("Tags") or []
    except ClientError as exc:
        if _is_not_found(exc):
            return None
        raise AthenaCatalogError(f"Failed to read tags for Athena data catalog {catalog_name!r}: {exc}") from exc
    except BotoCoreError as exc:
        raise AthenaCatalogError(f"Failed to read tags for Athena data catalog {catalog_name!r}: {exc}") from exc
    for tag in tags:
        if tag.get("Key") == SOURCE_ID_TAG_KEY:
            return tag.get("Value") or None
    return None


def delete_lambda_catalog(*, catalog_name: str, client=None) -> None:
    """Remove a LAMBDA-type Athena data catalog.

    Treats an already-absent catalog as success, so a retried delete converges
    instead of stalling on a 404 for work that is already done.

    Raises:
        AthenaCatalogError: the catalog exists and could not be removed. The
            caller must surface this rather than proceeding, because every delete
            path keys off the source record — drop the record and the catalog
            becomes unreachable, with no way left to find it.
    """
    athena = client or _athena()
    try:
        athena.delete_data_catalog(Name=catalog_name)
    except ClientError as exc:
        # Confirm with a lookup rather than trusting the message. `_is_not_found`
        # has to read error text (Athena declares no not-found code for data
        # catalogs), and a false positive HERE is the expensive one: it reports
        # success, the caller drops the source row, and the catalog is left with
        # nothing able to find it again. One extra call on an already-exceptional
        # path converts a guess into a checked fact.
        if _is_not_found(exc) and _describe_catalog(athena, catalog_name) is None:
            logger.info("athena_data_catalog_already_absent", catalog_name=catalog_name)
            return
        raise AthenaCatalogError(f"Failed to delete Athena data catalog {catalog_name!r}: {exc}") from exc
    except BotoCoreError as exc:
        raise AthenaCatalogError(f"Failed to delete Athena data catalog {catalog_name!r}: {exc}") from exc
    logger.info("athena_data_catalog_deleted", catalog_name=catalog_name)


def _describe_catalog(athena, catalog_name: str) -> dict | None:
    """Return the catalog's description, or ``None`` when it does not exist.

    Raises:
        AthenaCatalogError: the lookup failed for any reason other than the
            catalog being absent. Swallowing that would turn a permissions or
            network fault into "not registered", and the create that followed
            would fail with a duplicate-name 400 that says nothing useful.
    """
    try:
        return athena.get_data_catalog(Name=catalog_name).get("DataCatalog") or {}
    except ClientError as exc:
        if _is_not_found(exc):
            return None
        raise AthenaCatalogError(f"Failed to look up Athena data catalog {catalog_name!r}: {exc}") from exc
    except BotoCoreError as exc:
        raise AthenaCatalogError(f"Failed to look up Athena data catalog {catalog_name!r}: {exc}") from exc


def _is_not_found(exc: ClientError) -> bool:
    """Whether an Athena error means "no catalog by that name".

    Athena declares no not-found code for data catalogs — ``CreateDataCatalog``,
    ``GetDataCatalog`` and ``DeleteDataCatalog`` each declare only
    ``InternalServerException`` and ``InvalidRequestException`` — so an unknown
    name has to be recognised from the message. ``ResourceNotFoundException`` is
    matched defensively; it exists in the Athena model but is not declared on
    these operations.

    Both error directions are contained, but not equally, which is why the two
    callers treat this result differently:

    * On the lookup path, a *false positive* (a real error read as "absent") leads
      to a create that fails loudly on the duplicate name, and a *false negative*
      raises. Both end in a 500 and a rollback.
    * On the delete path, a false positive would report success — so the caller
      drops the source row and the catalog becomes unfindable. That is why
      :func:`delete_lambda_catalog` confirms with a lookup instead of trusting
      this alone.
    """
    error = exc.response.get("Error") or {}
    code = error.get("Code", "")
    if code == "ResourceNotFoundException":
        return True
    if code != "InvalidRequestException":
        return False
    message = (error.get("Message") or "").lower()
    return "not found" in message or "does not exist" in message
