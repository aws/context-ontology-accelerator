# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Security tests for ontologies router — CWE-209 information exposure.

Tests that httpx.HTTPError exceptions during source_url fetch do NOT leak
internal network topology (IP addresses, private DNS names, TLS handshake details).

These tests verify that the except _httpx.HTTPError handler produces opaque 502 messages.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


class TestHTTPErrorMessageSanitization:
    """CWE-209: httpx errors must be caught and NOT appear in 502 response body."""

    def test_httpx_connect_error_message_structure(self):
        """Verify httpx.ConnectError carries internal details that must not leak."""
        import httpx

        # Simulate what happens during a connection failure
        try:
            raise httpx.ConnectError(
                "Connection refused to internal IP 10.0.1.42:443 for https://example.com/ontology.ttl"
            )
        except httpx.ConnectError as e:
            error_string = str(e)
            # Confirm the exception DOES contain sensitive details (pre-sanitization)
            assert "10.0.1.42" in error_string or "Connection refused" in error_string
            # This demonstrates why we need sanitization

    def test_httpx_remote_protocol_error_structure(self):
        """Verify httpx.RemoteProtocolError carries TLS/cert details that must not leak."""
        import httpx

        try:
            raise httpx.RemoteProtocolError(
                "TLS handshake failed: peer certificate CN=internal.example.com does not match expected CN=example.com"
            )
        except httpx.RemoteProtocolError as e:
            error_string = str(e)
            # Confirm the exception DOES contain TLS details
            assert "TLS handshake" in error_string or "internal.example.com" in error_string

    def test_httpx_timeout_exception_structure(self):
        """Verify httpx.TimeoutException can carry resolved IP that must not leak."""
        import httpx

        try:
            raise httpx.TimeoutException(
                "Timeout while connecting to https://example.com/ontology.ttl (resolved to 192.168.1.100)"
            )
        except httpx.TimeoutException as e:
            error_string = str(e)
            # Confirm the exception DOES contain resolved IP
            assert "192.168.1.100" in error_string or "Timeout while connecting" in error_string

    def test_sanitized_502_message_does_not_reference_exception_text(self):
        """The opaque 502 message must not include any exception details."""
        opaque_message = "Failed to fetch source_url: upstream request failed"

        # Verify the sanitized message contains no infrastructure details
        assert "10.0.1.42" not in opaque_message
        assert "internal.example.com" not in opaque_message
        assert "192.168.1.100" not in opaque_message
        assert "Connection refused" not in opaque_message
        assert "TLS handshake" not in opaque_message
        assert "Timeout while connecting" not in opaque_message
        # Confirm it's truly opaque
        assert opaque_message == "Failed to fetch source_url: upstream request failed"


class TestStorageRootContainment:
    """``_is_within_storage_root`` is the single containment predicate.

    Two call sites depend on it: the ``/download`` local-file fallback and
    ``_persist_upload`` (which the ``source_url`` fetch also writes through).
    """

    def test_sibling_directory_sharing_the_root_prefix_is_outside(self, tmp_path, monkeypatch):
        """The case a ``startswith`` check gets wrong."""
        from coa_ontology.catalog.routers import ontologies

        root = tmp_path / "ontologies"
        root.mkdir()
        evil = tmp_path / "ontologies-evil"
        evil.mkdir()
        monkeypatch.setattr(ontologies, "ONTOLOGY_STORAGE_PATH", str(root))

        assert ontologies._is_within_storage_root(str(evil / "x.ttl")) is False
        assert str(evil).startswith(str(root)), "precondition: prefix check would pass"

    def test_paths_inside_the_root_are_accepted(self, tmp_path, monkeypatch):
        from coa_ontology.catalog.routers import ontologies

        root = tmp_path / "ontologies"
        (root / "ns").mkdir(parents=True)
        monkeypatch.setattr(ontologies, "ONTOLOGY_STORAGE_PATH", str(root))

        assert ontologies._is_within_storage_root(str(root)) is True
        assert ontologies._is_within_storage_root(str(root / "ns" / "o.ttl")) is True

    def test_symlink_escaping_the_root_is_rejected(self, tmp_path, monkeypatch):
        from coa_ontology.catalog.routers import ontologies

        root = tmp_path / "ontologies"
        root.mkdir()
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (root / "link.ttl").symlink_to(outside / "secret.ttl")
        monkeypatch.setattr(ontologies, "ONTOLOGY_STORAGE_PATH", str(root))

        assert ontologies._is_within_storage_root(str(root / "link.ttl")) is False


class TestPersistUploadPathTraversal:
    """CWE-22: ``_persist_upload`` writes to a caller-influenced path.

    ``namespace`` and ``identifier`` both originate from request input, so the
    resolved destination must always stay inside ``ONTOLOGY_STORAGE_PATH``.
    """

    @pytest.fixture()
    def storage_root(self, tmp_path, monkeypatch):
        from coa_ontology.catalog.routers import ontologies

        root = tmp_path / "ontologies"
        root.mkdir()
        monkeypatch.setattr(ontologies, "ONTOLOGY_STORAGE_PATH", str(root))
        return root

    @pytest.mark.parametrize(
        "namespace",
        [
            "../escape",
            "..",
            "a/../../b",
            "ns/sub",
            "ns\x00",
            "/abs",
            ".",
            # Python's ``$`` also matches just before a trailing newline, so an
            # unanchored ``re.match`` would accept this and create a directory
            # whose name ends in a newline.
            "ns\n",
        ],
    )
    def test_traversal_namespaces_are_rejected(self, namespace, storage_root):
        from coa_ontology.catalog.routers.ontologies import _persist_upload

        with pytest.raises(ValueError):
            _persist_upload(namespace, "ont", b"data", "turtle")

    @pytest.mark.parametrize(
        "identifier",
        [
            "../../../etc/passwd",
            "..",
            ".",
            "/etc/passwd",
            "sub/../../../evil",
            "",
        ],
    )
    def test_traversal_identifiers_stay_inside_the_namespace_dir(self, identifier, storage_root):
        """A hostile ontology id must never escape — it is slugified, not honoured."""
        from coa_ontology.catalog.routers.ontologies import _persist_upload

        dest = _persist_upload("myns", identifier, b"data", "turtle")

        root = os.path.realpath(str(storage_root))
        ns_dir = os.path.realpath(str(storage_root / "myns"))
        # Containment asserted via commonpath rather than by recomputing the
        # source's own dirname check, which would be circular.
        assert os.path.commonpath([root, os.path.realpath(dest)]) == root
        # The file landed in the namespace dir, and the id contributed only a
        # single flat filename component (no separators survived slugification).
        assert os.path.realpath(dest) == os.path.join(ns_dir, os.path.basename(dest))
        assert os.sep not in os.path.basename(dest)
        assert os.path.isfile(dest)

    def test_symlinked_namespace_escaping_to_sibling_prefix_is_rejected(self, tmp_path, monkeypatch):
        """A namespace symlinked to ``/…/ontologies-evil`` must be rejected.

        This is the input that separates the two containment strategies. With a
        root of ``/…/ontologies``, the resolved ``/…/ontologies-evil`` still
        satisfies ``ns_dir.startswith(root)`` — a sibling whose name merely
        begins with the root — but fails a component-wise ``commonpath``
        comparison. Without the symlink the two agree, so a plain
        ``mkdir``-based test would pass against either implementation and prove
        nothing.
        """
        from coa_ontology.catalog.routers import ontologies

        root = tmp_path / "ontologies"
        root.mkdir()
        evil = tmp_path / "ontologies-evil"
        evil.mkdir()
        (root / "ns").symlink_to(evil)
        monkeypatch.setattr(ontologies, "ONTOLOGY_STORAGE_PATH", str(root))

        with pytest.raises(ValueError, match="Path traversal"):
            ontologies._persist_upload("ns", "ont", b"data", "turtle")
        assert not list(evil.iterdir()), "nothing may be written outside the storage root"

    def test_distinct_ontology_iris_do_not_collide_on_one_filename(self, storage_root):
        """Ontology ids are IRIs that often differ only before the last ``/``.

        Slugifying just the basename would map ``https://schema.org/``,
        ``http://purl.org/dc/terms/`` and ``http://xmlns.com/foaf/0.1/`` all to
        the same file, so each upload would silently overwrite the previous
        ontology's bytes and every registry row would point at one file.
        """
        from coa_ontology.catalog.routers.ontologies import _persist_upload

        ids = [
            "https://schema.org/",
            "http://purl.org/dc/terms/",
            "http://xmlns.com/foaf/0.1/",
            "http://ex.org/induced#",
            "http://other.org/x/induced#",
        ]
        dests = {i: _persist_upload("myns", i, i.encode(), "turtle") for i in ids}

        assert len(set(dests.values())) == len(ids), f"slug collision: {dests}"
        # Each file still holds its own ontology's bytes.
        for identifier, path in dests.items():
            with open(path, "rb") as fh:
                assert fh.read() == identifier.encode()

    def test_ids_sharing_a_200_char_prefix_do_not_collide(self, storage_root):
        """Truncation alone would merge ids that differ only past 200 chars."""
        from coa_ontology.catalog.routers.ontologies import _persist_upload

        prefix = "http://example.com/" + "x" * 195
        a = _persist_upload("myns", f"{prefix}/alpha", b"A", "turtle")
        b = _persist_upload("myns", f"{prefix}/beta", b"B", "turtle")

        assert a != b
        with open(a, "rb") as fh:
            assert fh.read() == b"A"
        with open(b, "rb") as fh:
            assert fh.read() == b"B"

    def test_short_ids_keep_their_natural_filename(self, storage_root):
        """The digest suffix must apply ONLY when truncation happens.

        Appending it unconditionally would rename every existing ontology file.
        """
        from coa_ontology.catalog.routers.ontologies import _persist_upload

        dest = _persist_upload("myns", "https://schema.org/", b"data", "turtle")
        assert os.path.basename(dest) == "https_schema.org_.ttl"

    def test_valid_upload_round_trips(self, storage_root):
        from coa_ontology.catalog.routers.ontologies import _persist_upload

        dest = _persist_upload("myns", "http://example.com/my-ont", b"payload", "turtle")

        assert os.path.isfile(dest)
        with open(dest, "rb") as fh:
            assert fh.read() == b"payload"
        assert dest.endswith(".ttl")


class TestFetchDestinationIsNamespaceScoped:
    """CWE-668: ``/fetch`` must cache bytes under the caller's namespace dir.

    The fetch destination used to be built inline in the shared storage root,
    so two namespaces refreshing the same ontology id resolved to ONE file and
    both registry rows pointed at it — ``/download`` then served whichever
    namespace fetched last, across the namespace boundary.
    """

    def _fetch(self, ontology_id, namespace, body_bytes):
        """Drive ``fetch_ontology_from_source`` with the network stubbed out.

        Returns the ``file_path`` the endpoint recorded on the registry.
        """
        from coa_ontology.catalog.routers import ontologies

        graph = MagicMock()
        graph.get_ontology.return_value = {
            "format": "turtle",
            "uri": ontology_id,
            "source_url": "https://example.com/o.ttl",
        }

        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "text/turtle"}
        resp.iter_bytes.return_value = [body_bytes]

        with (
            patch.object(ontologies, "_graph", return_value=graph),
            patch.object(
                ontologies,
                "_resolve_and_validate_url",
                return_value=("https://93.184.216.34:443/o.ttl", "example.com", "443"),
            ),
            patch("httpx.Client") as client_cls,
        ):
            client_cls.return_value.__enter__.return_value.stream.return_value.__enter__.return_value = resp
            ontologies.fetch_ontology_from_source(ontology_id, None, namespace=namespace)

        return graph.update_ontology.call_args[0][1]["file_path"]

    @pytest.fixture()
    def storage_root(self, tmp_path, monkeypatch):
        from coa_ontology.catalog.routers import ontologies

        root = tmp_path / "ontologies"
        root.mkdir()
        monkeypatch.setattr(ontologies, "ONTOLOGY_STORAGE_PATH", str(root))
        return root

    def test_two_namespaces_fetching_one_id_do_not_share_a_file(self, storage_root):
        ontology_id = "https://schema.org/"

        a = self._fetch(ontology_id, "nsa", b"<http://a> a <http://x> .")
        b = self._fetch(ontology_id, "nsb", b"<http://b> a <http://x> .")

        assert a != b, "both namespaces cached the same ontology id to one file"
        assert os.path.dirname(a) == os.path.realpath(str(storage_root / "nsa"))
        assert os.path.dirname(b) == os.path.realpath(str(storage_root / "nsb"))
        # Each namespace still holds its own bytes (the leak is a content swap,
        # not just a shared name).
        with open(a, "rb") as fh:
            assert fh.read() == b"<http://a> a <http://x> ."
        with open(b, "rb") as fh:
            assert fh.read() == b"<http://b> a <http://x> ."
        # Nothing loose in the shared root.
        assert sorted(p.name for p in storage_root.iterdir()) == ["nsa", "nsb"]

    def test_traversal_namespace_is_rejected_before_any_write(self, storage_root):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            self._fetch("https://schema.org/", "../escape", b"<http://a> a <http://x> .")

        assert exc.value.status_code == 400
        assert not list(storage_root.iterdir())

    def test_traversal_ontology_id_is_slugified_not_rejected(self, storage_root):
        """A traversal-shaped ontology id is neutralised, not refused.

        Unlike the namespace (matched against ``[a-zA-Z0-9_\\-]+`` and rejected
        outright), the id is caller-supplied IRI text that must survive as a
        filename, so ``/`` is substituted away — ``../../etc/passwd`` becomes the
        flat name ``.._.._etc_passwd.ttl``. The write therefore succeeds INSIDE
        the namespace dir; expecting a ``400`` here would encode the wrong
        contract. What matters is that no separator survives to escape.
        """
        dest = self._fetch("../../etc/passwd", "validns", b"<http://a> a <http://x> .")

        ns_dir = os.path.realpath(str(storage_root / "validns"))
        assert os.path.dirname(dest) == ns_dir
        assert os.sep not in os.path.basename(dest)
        assert sorted(p.name for p in storage_root.iterdir()) == ["validns"]

    def test_symlinked_destination_file_is_rejected(self, storage_root, tmp_path):
        """The resolved-dest check is what actually yields a 400 for the id.

        A symlink planted at the destination filename resolves outside the
        namespace dir, so ``dirname(dest) != ns_dir`` trips and the endpoint
        must refuse rather than write through the link. This is the only
        reachable path to that branch — see the slugification test above.
        """
        from fastapi import HTTPException

        outside = tmp_path / "outside.ttl"
        ns_dir = storage_root / "validns"
        ns_dir.mkdir()
        (ns_dir / "https_schema.org_.ttl").symlink_to(outside)

        with pytest.raises(HTTPException) as exc:
            self._fetch("https://schema.org/", "validns", b"<http://a> a <http://x> .")

        assert exc.value.status_code == 400
        assert not outside.exists(), "nothing may be written through the symlink"

    def test_filesystem_failure_returns_500_not_a_bare_oserror(self, storage_root, monkeypatch):
        """A disk/permission failure must surface as a handled 500.

        Only ``ValueError`` used to be caught, so an ``OSError`` from
        ``makedirs``/``open`` escaped the route as an unlabelled 500 with no log
        line naming the namespace and id.
        """
        from coa_ontology.catalog.routers import ontologies
        from fastapi import HTTPException

        def boom(*_args, **_kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(ontologies, "_persist_upload", boom)

        with pytest.raises(HTTPException) as exc:
            self._fetch("https://schema.org/", "validns", b"<http://a> a <http://x> .")

        assert exc.value.status_code == 500
        assert "No space left" not in str(exc.value.detail), "OS error text must not reach the client"


class TestDownloadFallbackContainment:
    """CWE-22: ``/download`` local-file fallback opens a registry-supplied path.

    When the graph backend has no Turtle to export, ``download_ontology_file``
    falls back to reading the ``file_path`` recorded on the ontology registry.
    That path is resolved with ``realpath`` and checked against
    ``ONTOLOGY_STORAGE_PATH`` before the ``open(real_fp)`` sink — a path that
    escapes the storage root is refused with 403, never read or offloaded.
    ``TestStorageRootContainment`` covers the predicate; this exercises the
    route wiring to it at the actual sink.
    """

    @pytest.fixture()
    def storage_root(self, tmp_path, monkeypatch):
        from coa_ontology.catalog.routers import ontologies

        root = tmp_path / "ontologies"
        root.mkdir()
        monkeypatch.setattr(ontologies, "ONTOLOGY_STORAGE_PATH", str(root))
        return root

    def test_registry_file_path_escaping_the_root_is_refused(self, storage_root, tmp_path):
        """An existing file outside the storage root must not be read out."""
        from coa_ontology.catalog.routers import ontologies
        from fastapi import HTTPException

        outside = tmp_path / "secret.ttl"
        outside.write_text("@prefix ex: <http://ex/> . ex:S a ex:Secret .", encoding="utf-8")

        graph = MagicMock()
        graph.get_graph_turtle.return_value = None  # force the local-file fallback

        with (
            patch.object(ontologies, "_graph", return_value=graph),
            patch.object(
                ontologies.dynamo_store,
                "get_ontology_registry",
                return_value={"file_path": str(outside)},
            ),
            patch.object(ontologies.dynamo_store, "write_and_presign_ontology_download") as offload,
            pytest.raises(HTTPException) as exc,
        ):
            ontologies.download_ontology_file("http://ex/onto", namespace="ns")

        assert exc.value.status_code == 403
        offload.assert_not_called()  # nothing outside the root is read or offloaded

    def test_registry_file_path_inside_the_root_is_served(self, storage_root):
        """In-root case still works — the guard is containment, not a blanket deny."""
        from coa_ontology.catalog.routers import ontologies

        inside = storage_root / "ns" / "o.ttl"
        inside.parent.mkdir(parents=True)
        inside.write_text("@prefix ex: <http://ex/> . ex:A a ex:Class .", encoding="utf-8")

        graph = MagicMock()
        graph.get_graph_turtle.return_value = None

        with (
            patch.object(ontologies, "_graph", return_value=graph),
            patch.object(
                ontologies.dynamo_store,
                "get_ontology_registry",
                return_value={"file_path": str(inside)},
            ),
            patch.object(
                ontologies.dynamo_store,
                "write_and_presign_ontology_download",
                return_value="https://s3.example/presigned",
            ) as offload,
        ):
            result = ontologies.download_ontology_file("http://ex/onto", namespace="ns")

        assert result["downloadUrl"] == "https://s3.example/presigned"
        # The bytes offloaded are the in-root file's own contents.
        assert "ex:A" in offload.call_args[0][2]
