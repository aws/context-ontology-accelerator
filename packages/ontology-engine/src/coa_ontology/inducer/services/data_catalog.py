# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Client for the data-catalog mock service."""

import httpx
from pydantic import BaseModel


class CatalogColumn(BaseModel):
    """A column entry from the data catalog, with type, constraints, and sample values."""

    name: str
    dataType: str
    dataLength: int | None = None
    precision: int | None = None
    scale: int | None = None
    description: str | None = None
    synonyms: list[str] = []
    constraint: str | None = None
    ordinalPosition: int | None = None
    tags: list[dict] = []
    # Sampled distinct values for low-cardinality categorical columns; emitted
    # into the induced ontology so serve's NL→SQL context can hint enum literals.
    distinctValues: list[str] = []


class CatalogConstraint(BaseModel):
    """A table constraint (e.g. primary or foreign key) reported by the data catalog."""

    constraintType: str
    columns: list[str]
    referredColumns: list[str] | None = None
    relationshipType: str | None = None


def parse_referred_column(ref: str) -> tuple[str, str | None]:
    """Split a ``referredColumns`` entry into ``(target_table, target_column)``.

    The catalog reports an FK target as a dotted identifier whose LAST TWO
    segments are ``TABLE.COLUMN``. Everything before them is qualification
    (database, schema, catalog) and is dropped:

        ``"orders.order_id"``               → ``("orders", "order_id")``
        ``"public.orders.order_id"``        → ``("orders", "order_id")``
        ``"db.public.orders.order_id"``     → ``("orders", "order_id")``
        ``"orders"``                        → ``("orders", None)``

    A single-segment value carries no column, so the caller cannot build a
    join condition from it — ``build_r2rml`` treats that as a plain datatype
    column rather than emitting a bare ``rr:parentTriplesMap``, which would make
    Ontop produce a Cartesian product (R2RML §7.5).

    This lives here, next to :class:`CatalogConstraint`, because the rule was
    previously re-implemented at six call sites under TWO INCOMPATIBLE
    conventions: ``parts[0]`` in the R2RML builder and the RIGOR topological sort,
    ``parts[-2]`` in the ontology builder, the SHACL config generator, the subtype
    detector, and the fingerprint normalizer. For a three-part reference the
    former yields the SCHEMA name, so the mapping pointed ``rr:parentTriplesMap``
    at a TriplesMap that does not exist while the ontology and shapes correctly
    referenced the table — the artifacts described different graphs.

    Args:
        ref: A ``referredColumns`` entry.

    Returns:
        ``(target_table, target_column)``; ``target_column`` is ``None`` when
        ``ref`` has no dot.
    """
    parts = ref.split(".")
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    return ref, None


class CatalogTable(BaseModel):
    """A table from the data catalog, with its columns, constraints, and datasource."""

    id: str
    name: str
    fullyQualifiedName: str
    description: str | None = None
    synonyms: list[str] = []
    columns: list[CatalogColumn] = []
    tableConstraints: list[CatalogConstraint] | None = None
    datasourceId: str | None = None
    sourceSchema: str | None = None


class DataCatalogClient:
    """HTTP client for fetching table metadata from the data-catalog service."""

    def __init__(self, base_url: str):
        """Store the base URL of the data-catalog service.

        Args:
            base_url: Root URL of the data-catalog HTTP API.
        """
        self.base_url = base_url

    def get_table(self, table_id: str) -> CatalogTable:
        """Fetch a single table's metadata by id.

        Args:
            table_id: Catalog table id or fully qualified name.

        Returns:
            The parsed :class:`CatalogTable`.
        """
        with httpx.Client(timeout=30) as client:
            resp = client.get(f"{self.base_url}/api/v1/tables/{table_id}")
            resp.raise_for_status()
            return CatalogTable(**resp.json())

    def list_tables(self) -> list[CatalogTable]:
        """Fetch metadata for all tables in the catalog.

        Returns:
            A list of :class:`CatalogTable` for every table the catalog exposes.
        """
        with httpx.Client(timeout=30) as client:
            resp = client.get(f"{self.base_url}/api/v1/tables")
            resp.raise_for_status()
            return [CatalogTable(**t) for t in resp.json()]
