# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Abstract base for metadata store operations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProjectResult:
    """Result of a project creation or retrieval."""

    project_id: str
    name: str


@dataclass(frozen=True)
class AssetResult:
    """Result of a data asset creation or retrieval."""

    asset_id: str
    name: str
    project_id: str
    forms_output: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class SearchResult:
    """Result of an asset search."""

    items: list[AssetResult]
    next_token: str | None = None


class MetadataStoreClient(ABC):
    """Abstract interface for metadata store operations.

    Implementations may target SageMaker Unified Studio (SMUS),
    AWS Glue Data Catalog, or any other catalog backend.
    """

    # ── Projects ──────────────────────────────────────────────────

    @abstractmethod
    def create_project(self, *, name: str, description: str = "", project_profile_id: str = "") -> ProjectResult:
        """Create a project.

        Raises:
            MetadataStoreError: on failure.
        """

    @abstractmethod
    def delete_project(self, *, project_id: str) -> None:
        """Delete a project.

        Raises:
            MetadataStoreError: on failure.
        """

    @abstractmethod
    def get_project(self, *, project_id: str) -> ProjectResult:
        """Retrieve a project by ID.

        Raises:
            MetadataStoreError: on failure or if not found.
        """

    # ── Assets ────────────────────────────────────────────────────

    @abstractmethod
    def create_asset(
        self,
        *,
        project_id: str,
        name: str,
        type_identifier: str,
        description: str = "",
        forms_input: list[dict[str, str]] | None = None,
    ) -> AssetResult:
        """Create a data asset inside a project.

        Raises:
            MetadataStoreError: on failure.
        """

    @abstractmethod
    def create_asset_revision(
        self,
        *,
        asset_id: str,
        name: str,
        description: str = "",
        forms_input: list[dict[str, str]] | None = None,
    ) -> AssetResult:
        """Create a new revision of an existing asset.

        Raises:
            MetadataStoreError: on failure.
        """

    @abstractmethod
    def delete_asset(self, *, asset_id: str) -> None:
        """Delete a data asset.

        Raises:
            MetadataStoreError: on failure.
        """

    @abstractmethod
    def get_asset(self, *, asset_id: str) -> AssetResult:
        """Retrieve a data asset by ID.

        Raises:
            MetadataStoreError: on failure or if not found.
        """

    @abstractmethod
    def search_assets(
        self,
        *,
        project_id: str,
        search_text: str,
        max_results: int = 50,
        next_token: str | None = None,
        include_forms: bool = False,
    ) -> SearchResult:
        """Search for assets within a project.

        When *include_forms* is True, implementations should request that the
        backend inline each asset's form data so that callers can avoid a
        per-asset ``get_asset_forms`` round-trip.  The form data is populated
        in :pyattr:`AssetResult.forms_output` only when requested.

        Raises:
            MetadataStoreError: on failure.
        """

    @abstractmethod
    def get_asset_forms(self, *, asset_id: str) -> dict[str, Any]:
        """Retrieve a data asset with its form content.

        Returns the full API response dict including formsOutput.

        Raises:
            MetadataStoreError: on failure or if not found.
        """

    # ── User Profiles & Memberships ───────────────────────────────

    @abstractmethod
    def create_project_membership(
        self, *, project_id: str, user_identifier: str, designation: str = "PROJECT_CONTRIBUTOR"
    ) -> None:
        """Add a user as a project member.

        Transient conflicts and throttling are retried with backoff; a conflict
        or error that persists is raised. Not idempotent on an existing
        membership — re-adding an already-present member may raise, since the
        backend does not document a distinct "already a member" success.

        Raises:
            MetadataStoreError: on failure.
        """
