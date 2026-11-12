# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""SageMaker Unified Studio (SMUS) implementation of MetadataStoreClient."""

from __future__ import annotations

import logging
import random
import time
from typing import TYPE_CHECKING, Any, cast

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_datazone import DataZoneClient as DataZoneServiceClient

from .base import AssetResult, MetadataStoreClient, ProjectResult, SearchResult
from .exceptions import MetadataStoreError

logger = logging.getLogger(__name__)

# Membership registration is racy right after CreateProject (DataZone's
# optimistic-lock version state has to settle) and can also be throttled.
# These control the retry loop in ``create_project_membership``.
#
# Namespace creation calls this synchronously from an API Gateway proxy Lambda
# (namespace-api, 15s function timeout; API Gateway caps at 29s), *after*
# CreateProject has already consumed part of that budget. The total worst-case
# retry sleep is therefore kept small (base 0.5 + 1 + 2 = ~3.5s plus jitter)
# so retries cannot push the request past the Lambda timeout and get it
# hard-killed mid-create.
_MEMBERSHIP_MAX_ATTEMPTS = 4
_MEMBERSHIP_RETRY_BASE_SECONDS = 0.5
_MEMBERSHIP_RETRY_MAX_SECONDS = 2.0
# DataZone error codes that are transient and safe to retry with backoff.
_RETRYABLE_MEMBERSHIP_ERROR_CODES = frozenset(
    {"ThrottlingException", "ThrottledException", "TooManyRequestsException", "InternalServerException"}
)


# Adaptive mode adds a client-side rate limiter for DataZone's account-wide write
# limit, which bulk CreateAsset calls hit.
# ponytail: config-only retry tuning; per-Lambda write concurrency is already
# bounded by the ThreadPoolExecutor in metadata_writer and scan-Lambda fan-out
# by the scan queue's maxConcurrency=5. Raise max_attempts if throttling still
# exhausts under heavier fan-out.
_DATAZONE_RETRY_CONFIG = Config(retries={"max_attempts": 10, "mode": "adaptive"})


def _sleep_membership_backoff(attempt: int) -> None:
    """Sleep with exponential backoff + jitter between membership retries."""
    delay = _MEMBERSHIP_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
    time.sleep(min(delay, _MEMBERSHIP_RETRY_MAX_SECONDS) + random.uniform(0.0, 0.5))


class SMUSClient(MetadataStoreClient):
    """MetadataStoreClient backed by SageMaker Unified Studio (DataZone API).

    Args:
        domain_id: SMUS domain identifier.
        region_name: AWS region. Defaults to the SDK default chain.
        assume_role_arn: Optional IAM role ARN to assume before making DataZone calls.
        session_name: Optional STS session name for CloudTrail audit trail.
    """

    def __init__(
        self,
        domain_id: str,
        region_name: str | None = None,
        assume_role_arn: str | None = None,
        session_name: str | None = None,
    ) -> None:
        """Build the DataZone client, optionally assuming an IAM role first."""
        self._domain_id = domain_id

        if assume_role_arn:
            sts = boto3.client("sts", region_name=region_name)
            creds = sts.assume_role(
                RoleArn=assume_role_arn,
                RoleSessionName=session_name or "smus-project-access",
            )["Credentials"]
            session = boto3.Session(
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
                region_name=region_name,
            )
            self._client: DataZoneServiceClient = session.client("datazone", config=_DATAZONE_RETRY_CONFIG)
        else:
            self._client = boto3.client("datazone", region_name=region_name, config=_DATAZONE_RETRY_CONFIG)

    def create_project(self, *, name: str, description: str = "", project_profile_id: str = "") -> ProjectResult:
        """Create a DataZone project in the configured domain.

        Raises:
            MetadataStoreError: If the API call fails or returns no project id.
        """
        try:
            kwargs: dict = {
                "domainIdentifier": self._domain_id,
                "name": name,
            }
            if description:
                kwargs["description"] = description
            if project_profile_id:
                kwargs["projectProfileId"] = project_profile_id
            resp = self._client.create_project(**kwargs)
        except Exception as exc:
            msg = exc.args[0] if exc.args else str(exc)
            raise MetadataStoreError(f"Failed to create project: {msg}") from exc

        pid = resp.get("id")
        if not pid:
            raise MetadataStoreError("SMUS API returned no project ID")
        return ProjectResult(project_id=pid, name=resp.get("name", name))

    def delete_project(self, *, project_id: str) -> None:
        """Delete a DataZone project by id.

        Raises:
            MetadataStoreError: If the API call fails.
        """
        try:
            self._client.delete_project(
                domainIdentifier=self._domain_id,
                identifier=project_id,
                skipDeletionCheck=False,
            )
        except Exception as exc:
            msg = exc.args[0] if exc.args else str(exc)
            raise MetadataStoreError(f"Failed to delete project: {msg}") from exc

    def create_asset(
        self,
        *,
        project_id: str,
        name: str,
        type_identifier: str,
        description: str = "",
        forms_input: list[dict[str, str]] | None = None,
    ) -> AssetResult:
        """Create a new asset in a project.

        Raises:
            MetadataStoreError: If the API call fails or returns no asset id.
        """
        try:
            kwargs: dict = {
                "domainIdentifier": self._domain_id,
                "owningProjectIdentifier": project_id,
                "name": name,
                "typeIdentifier": type_identifier,
            }
            if description:
                if len(description) > 2048:
                    logger.warning(
                        "Truncating asset description from %d chars to 2048 for asset=%s", len(description), name
                    )
                    description = description[:2048]
                kwargs["description"] = description
            if forms_input:
                kwargs["formsInput"] = forms_input
            resp = self._client.create_asset(**kwargs)
        except Exception as exc:
            msg = exc.args[0] if exc.args else str(exc)
            raise MetadataStoreError(f"Failed to create asset: {msg}") from exc

        aid = resp.get("id")
        if not aid:
            raise MetadataStoreError("SMUS API returned no asset ID")
        return AssetResult(
            asset_id=aid,
            name=resp.get("name", name),
            project_id=resp.get("owningProjectId", project_id),
        )

    def create_asset_revision(
        self,
        *,
        asset_id: str,
        name: str,
        description: str = "",
        forms_input: list[dict[str, str]] | None = None,
    ) -> AssetResult:
        """Publish a new revision of an existing asset.

        Raises:
            MetadataStoreError: If the API call fails or returns no asset id.
        """
        try:
            kwargs: dict = {
                "domainIdentifier": self._domain_id,
                "identifier": asset_id,
                "name": name,
            }
            if description:
                if len(description) > 2048:
                    logger.warning(
                        "Truncating asset description from %d chars to 2048 for asset=%s", len(description), name
                    )
                    description = description[:2048]
                kwargs["description"] = description
            if forms_input:
                kwargs["formsInput"] = forms_input
            resp = self._client.create_asset_revision(**kwargs)
        except Exception as exc:
            msg = exc.args[0] if exc.args else str(exc)
            raise MetadataStoreError(f"Failed to create asset revision: {msg}") from exc

        aid = resp.get("id")
        if not aid:
            raise MetadataStoreError("SMUS API returned no asset ID")
        return AssetResult(
            asset_id=aid,
            name=resp.get("name", name),
            project_id=resp.get("owningProjectId", ""),
        )

    def delete_asset(self, *, asset_id: str) -> None:
        """Delete an asset by id.

        Raises:
            MetadataStoreError: If the API call fails.
        """
        try:
            self._client.delete_asset(
                domainIdentifier=self._domain_id,
                identifier=asset_id,
            )
        except Exception as exc:
            msg = exc.args[0] if exc.args else str(exc)
            raise MetadataStoreError(f"Failed to delete asset: {msg}") from exc

    def search_assets(
        self,
        *,
        project_id: str,
        search_text: str,
        max_results: int = 50,
        next_token: str | None = None,
        search_in_attributes: list[str] | None = None,
        include_forms: bool = False,
    ) -> SearchResult:
        """Full-text search assets within a project.

        Args:
            project_id: Owning project to scope the search to.
            search_text: Free-text query.
            max_results: Maximum results to return per page.
            next_token: Pagination cursor from a prior call.
            search_in_attributes: Restrict matching to these asset attributes
                instead of every searchable one. Pass ``["name"]`` to look an
                asset up by name — see :meth:`find_asset_by_name` for why that
                matters. Omit for an unrestricted full-text search.
            include_forms: When True, request inline form data via
                ``additionalAttributes=["FORMS"]``. Each returned
                :class:`AssetResult` will have its ``forms_output``
                populated from the response (or ``None`` when absent).

        Returns:
            Matching assets plus the next pagination token, if any.

        Raises:
            MetadataStoreError: If the API call fails.
        """
        try:
            kwargs: dict = {
                "domainIdentifier": self._domain_id,
                "owningProjectIdentifier": project_id,
                "searchScope": "ASSET",
                "searchText": search_text,
                "maxResults": max_results,
            }
            if search_in_attributes:
                kwargs["searchIn"] = [{"attribute": a} for a in search_in_attributes]
            if next_token:
                kwargs["nextToken"] = next_token
            if include_forms:
                kwargs["additionalAttributes"] = ["FORMS"]
            resp = self._client.search(**kwargs)
        except Exception as exc:
            msg = exc.args[0] if exc.args else str(exc)
            raise MetadataStoreError(f"Failed to search assets: {msg}") from exc

        items = [
            AssetResult(
                asset_id=item["assetItem"]["identifier"],
                name=item["assetItem"].get("name", ""),
                project_id=item["assetItem"].get("owningProjectId", project_id),
                forms_output=(
                    cast(
                        "list[dict[str, Any]] | None",
                        item["assetItem"].get("additionalAttributes", {}).get("formsOutput"),
                    )
                    if include_forms
                    else None
                ),
            )
            for item in resp.get("items", [])
            if "assetItem" in item and "identifier" in item["assetItem"]
        ]
        return SearchResult(items=items, next_token=resp.get("nextToken"))

    def find_asset_by_name(self, *, project_id: str, name: str, max_pages: int = 20) -> AssetResult | None:
        """Look an asset up by its exact name, or ``None`` when it does not exist.

        DataZone's ``searchText`` is token based, so a full asset name such as
        ``DS#<sourceId>:<db>.orders`` can also match ``order_items`` and require
        many pages before the exact asset appears.

        DataZone's ``Search`` rejects an ``EQ`` filter on string attributes, so this
        pages the token search and compares names exactly.

        Args:
            project_id: Owning project to scope the search to.
            name: Full asset name to match exactly.
            max_pages: Stop after this many pages. A bound is needed because a
                name that is a prefix of many others can match widely; hitting
                it returns ``None``, which callers already treat as "not found".

        Returns:
            The asset whose name equals ``name``, or ``None``.

        Raises:
            MetadataStoreError: If the underlying search call fails. An absent
                asset is ``None``; a failed lookup is an error, so a caller
                never reads "not found" out of a broken search.
        """
        token: str | None = None
        for _ in range(max_pages):
            result = self.search_assets(
                project_id=project_id,
                search_text=name,
                max_results=50,
                next_token=token,
                search_in_attributes=["name"],
            )
            for item in result.items:
                if item.name == name:
                    return item
            token = result.next_token
            if not token:
                return None
        return None

    def get_asset_forms(self, *, asset_id: str) -> dict[str, Any]:
        """Return the raw DataZone asset payload (including its metadata forms).

        Raises:
            MetadataStoreError: If the API call fails.
        """
        try:
            return dict(
                self._client.get_asset(
                    domainIdentifier=self._domain_id,
                    identifier=asset_id,
                )
            )
        except Exception as exc:
            msg = exc.args[0] if exc.args else str(exc)
            raise MetadataStoreError(f"Failed to get asset forms: {msg}") from exc

    def create_project_membership(
        self, *, project_id: str, user_identifier: str, designation: str = "PROJECT_CONTRIBUTOR"
    ) -> None:
        """Add a user as a project member. Raises MetadataStoreError on failure.

        Transient conflicts and throttling are retried with backoff; a conflict
        or error that persists is raised. Not idempotent on an existing
        membership: DataZone documents no distinct "already a member" outcome.
        """
        max_attempts = _MEMBERSHIP_MAX_ATTEMPTS
        for attempt in range(1, max_attempts + 1):
            try:
                self._client.create_project_membership(
                    domainIdentifier=self._domain_id,
                    projectIdentifier=project_id,
                    member={"userIdentifier": user_identifier},
                    designation=designation,  # type: ignore[arg-type]
                )
                return
            except self._client.exceptions.ConflictException as exc:
                if attempt < max_attempts:
                    logger.warning(
                        "Conflict adding project member (attempt %d/%d), retrying...",
                        attempt,
                        max_attempts,
                    )
                    _sleep_membership_backoff(attempt)
                    continue
                msg = getattr(exc, "response", {}).get("Error", {}).get("Message", "")
                raise MetadataStoreError(
                    f"Conflict persisted after {max_attempts} attempts adding project member: {msg}"
                ) from exc
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in _RETRYABLE_MEMBERSHIP_ERROR_CODES and attempt < max_attempts:
                    logger.warning(
                        "Transient %s adding project member (attempt %d/%d), retrying...",
                        code or "error",
                        attempt,
                        max_attempts,
                    )
                    _sleep_membership_backoff(attempt)
                    continue
                # Non-retryable (AccessDenied, ValidationException, ...) or retries
                # exhausted: surface it so the caller can act on it.
                raise MetadataStoreError(f"Failed to create project membership: {exc}") from exc
            except Exception as exc:
                msg = exc.args[0] if exc.args else str(exc)
                raise MetadataStoreError(f"Failed to create project membership: {msg}") from exc
