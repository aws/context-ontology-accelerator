# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Base exception hierarchy for Context Ontology Accelerator."""


class SCLError(Exception):
    """Base exception for all services."""

    def __init__(self, message: str, code: str = "INTERNAL_ERROR") -> None:
        """Initialize the error with a human-readable message and machine code."""
        self.code = code
        super().__init__(message)


class ConfigurationError(SCLError):
    """Raised when service configuration is invalid."""

    def __init__(self, message: str) -> None:
        """Initialize with ``message`` and the CONFIGURATION_ERROR code.

        Args:
            message: Human-readable description of the configuration problem.
        """
        super().__init__(message, code="CONFIGURATION_ERROR")


class DataSourceError(SCLError):
    """Raised when a data source operation fails."""

    def __init__(self, message: str) -> None:
        """Initialize with ``message`` and the DATA_SOURCE_ERROR code.

        Args:
            message: Human-readable description of the data source failure.
        """
        super().__init__(message, code="DATA_SOURCE_ERROR")


class IngestionError(SCLError):
    """Raised when data ingestion fails."""

    def __init__(self, message: str) -> None:
        """Initialize with ``message`` and the INGESTION_ERROR code.

        Args:
            message: Human-readable description of the ingestion failure.
        """
        super().__init__(message, code="INGESTION_ERROR")
