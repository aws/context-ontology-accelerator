# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for DNS-rebinding SSRF prevention in ``_resolve_and_validate_url``."""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest
from coa_ontology.catalog.routers.ontologies import _resolve_and_validate_url
from fastapi import HTTPException

pytestmark = pytest.mark.unit


def test_resolve_and_validate_url_rejects_http_scheme():
    """Only https:// is allowed."""
    with pytest.raises(HTTPException) as exc:
        _resolve_and_validate_url("http://example.com/ontology.ttl")
    assert exc.value.status_code == 400
    assert "https://" in str(exc.value.detail).lower()


def test_resolve_and_validate_url_rejects_private_ips():
    """All RFC 1918 private addresses must be rejected."""
    private_ips = [
        "10.0.0.1",  # RFC 1918
        "172.16.0.1",  # RFC 1918
        "192.168.1.1",  # RFC 1918
        "127.0.0.1",  # loopback
        "169.254.169.254",  # link-local (AWS IMDS)
    ]

    for ip in private_ips:
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0))]
            with pytest.raises(HTTPException) as exc:
                _resolve_and_validate_url("https://example.com/ontology.ttl")
            assert exc.value.status_code == 400
            assert "non-routable" in str(exc.value.detail).lower()


def test_resolve_and_validate_url_rejects_multicast():
    """Multicast addresses must be rejected."""
    with patch("socket.getaddrinfo") as mock_getaddrinfo:
        mock_getaddrinfo.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("224.0.0.1", 0))]
        with pytest.raises(HTTPException) as exc:
            _resolve_and_validate_url("https://example.com/ontology.ttl")
        assert exc.value.status_code == 400
        assert "non-routable" in str(exc.value.detail).lower()


def test_resolve_and_validate_url_rejects_cgnat_and_unspecified():
    """CGNAT (100.64.0.0/10) and unspecified (0.0.0.0) — missed by an explicit
    flag enumeration — must be rejected by the is_global check."""
    for ip in ["100.64.0.1", "0.0.0.0"]:
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0))]
            with pytest.raises(HTTPException) as exc:
                _resolve_and_validate_url("https://example.com/ontology.ttl")
            assert exc.value.status_code == 400
            assert "non-routable" in str(exc.value.detail).lower()


def test_resolve_and_validate_url_rejects_ipv4_mapped_imds():
    """IPv4-mapped IPv6 pointing at IMDS (::ffff:169.254.169.254) must be
    rejected — an SSRF bypass that per-flag checks can miss."""
    with patch("socket.getaddrinfo") as mock_getaddrinfo:
        mock_getaddrinfo.return_value = [
            (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::ffff:169.254.169.254", 0, 0, 0)),
        ]
        with pytest.raises(HTTPException) as exc:
            _resolve_and_validate_url("https://example.com/ontology.ttl")
        assert exc.value.status_code == 400
        assert "non-routable" in str(exc.value.detail).lower()


def test_resolve_and_validate_url_accepts_public_ipv4():
    """Valid public IPv4 addresses are accepted and pinned."""
    with patch("socket.getaddrinfo") as mock_getaddrinfo:
        mock_getaddrinfo.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.1.1.1", 0))]
        pinned_url, hostname, port = _resolve_and_validate_url("https://example.com/ontology.ttl")

    assert "1.1.1.1" in pinned_url
    assert "443" in pinned_url
    assert hostname == "example.com"
    assert port == "443"


def test_resolve_and_validate_url_accepts_public_ipv6():
    """Valid public IPv6 addresses are accepted with bracket notation."""
    with patch("socket.getaddrinfo") as mock_getaddrinfo:
        mock_getaddrinfo.return_value = [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("2606:4700:4700::1111", 0))]
        pinned_url, hostname, port = _resolve_and_validate_url("https://example.com/ontology.ttl")

    assert "[2606:4700:4700::1111]" in pinned_url
    assert hostname == "example.com"


def test_resolve_and_validate_url_uses_custom_port():
    """Custom HTTPS ports are preserved in the pinned URL."""
    with patch("socket.getaddrinfo") as mock_getaddrinfo:
        mock_getaddrinfo.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))]
        pinned_url, hostname, port = _resolve_and_validate_url("https://example.com:8443/ontology.ttl")

    assert "93.184.216.34:8443" in pinned_url
    assert port == "8443"


def test_resolve_and_validate_url_rejects_dns_failure():
    """DNS resolution failures are rejected with 400."""
    with patch("socket.getaddrinfo") as mock_getaddrinfo:
        mock_getaddrinfo.side_effect = socket.gaierror("Name or service not known")
        with pytest.raises(HTTPException) as exc:
            _resolve_and_validate_url("https://nonexistent.invalid/ontology.ttl")
        assert exc.value.status_code == 400
        assert "Cannot resolve" in exc.value.detail


def test_resolve_and_validate_url_rejects_empty_resolution():
    """DNS resolution returning no addresses is rejected."""
    with patch("socket.getaddrinfo") as mock_getaddrinfo:
        mock_getaddrinfo.return_value = []
        with pytest.raises(HTTPException) as exc:
            _resolve_and_validate_url("https://example.com/ontology.ttl")
        assert exc.value.status_code == 400
        assert "no addresses" in str(exc.value.detail).lower()


def test_resolve_and_validate_url_rejects_if_any_ip_is_private():
    """If ANY returned IP is private, the entire request is rejected.

    This prevents an attacker from relying on round-robin DNS where
    some IPs are public (pass validation) but others are private.
    """
    with patch("socket.getaddrinfo") as mock_getaddrinfo:
        # Mock: first IP is public, second is private
        mock_getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.1.1.1", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("192.168.1.1", 0)),
        ]
        with pytest.raises(HTTPException) as exc:
            _resolve_and_validate_url("https://example.com/ontology.ttl")
        assert exc.value.status_code == 400
        assert "non-routable" in str(exc.value.detail).lower()


def test_resolve_and_validate_url_missing_hostname():
    """URLs without a hostname are rejected."""
    with pytest.raises(HTTPException) as exc:
        _resolve_and_validate_url("https:///path/to/file.ttl")
    assert exc.value.status_code == 400
    assert "missing a hostname" in str(exc.value.detail).lower()
