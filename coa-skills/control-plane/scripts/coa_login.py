#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""COA sign-in helper. OAuth 2.0 authorization code + PKCE against one of
COA's two public app clients. Caches tokens and refreshes them, so nobody
copies tokens out of DevTools.

  --client mcp  MCP/CLI client, redirect http://localhost:9876/oauth/callback.
                ID token works on the MCP server and on direct calls to the
                serve AgentCore runtime; the REST API (including /query)
                rejects it. Refresh 30 days.
  --client web  Web-app client, redirect http://localhost:5173/authenticate/.
                ID token works on every REST endpoint, including the serve
                runtime's /query (the authorizer checks aud against this
                client). Refresh 12 hours.

  coa_login.py login  [--client C] [--file PATH] [--no-browser]
  coa_login.py token  [--client C] [--kind id|access] [--file PATH]
  coa_login.py status [--client C] [--file PATH]   expiry times, never tokens
  coa_login.py logout [--client C] [--file PATH]   deletes the local cache only

Python use:  from coa_login import get_token; get_token("id", client="web")

Set COA_DOMAIN first: the OAuth base URL that serves /oauth2/authorize and
/oauth2/token. That is the Cognito hosted-UI domain by default, or a custom
domain in front of it. Also set COA_WEB_CLIENT_ID and COA_MCP_CLIENT_ID.
COA_TOKEN_FILE overrides the cache path for --client mcp only; use --file for
either client. Standard library only.
"""

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def _need(name: str) -> str:
    v = os.environ.get(name, "")
    if not v:
        raise SystemExit(f"set {name} (see SKILL.md, Quick Start)")
    return v


DOMAIN = os.environ.get("COA_DOMAIN", "")
CLIENTS = {
    "mcp": {
        "id": os.environ.get("COA_MCP_CLIENT_ID", ""),
        "redirect": "http://localhost:9876/oauth/callback",
        "file": Path.home() / ".coa" / "tokens.json",
        "refresh_life": "30 days",
    },
    "web": {
        "id": os.environ.get("COA_WEB_CLIENT_ID", ""),
        "redirect": "http://localhost:5173/authenticate/",
        "file": Path.home() / ".coa" / "tokens-web.json",
        "refresh_life": "12 hours",
    },
}
SCOPES = "openid email profile"
SKEW = 120  # refresh this many seconds before expiry
LOGIN_TIMEOUT = 300


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _jwt_exp(token: str) -> int:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return int(json.loads(base64.urlsafe_b64decode(payload))["exp"])


class TokenEndpointError(SystemExit):
    """The token endpoint answered with an HTTP error. ``status`` holds the code."""

    def __init__(self, status: int, body: str):
        # Error bodies carry an error code, never a token.
        super().__init__(f"token endpoint returned {status}: {body[:300]}")
        self.status = status


def _post_token(form: dict) -> dict:
    _need("COA_DOMAIN")
    if not form.get("client_id"):
        raise SystemExit("set COA_MCP_CLIENT_ID or COA_WEB_CLIENT_ID (see SKILL.md, Quick Start)")
    req = urllib.request.Request(
        f"{DOMAIN}/oauth2/token",
        data=urllib.parse.urlencode(form).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise TokenEndpointError(e.code, e.read().decode()) from None


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _default_file(client: str) -> Path:
    env = os.environ.get("COA_TOKEN_FILE")
    return Path(env) if env and client == "mcp" else CLIENTS[client]["file"]


def _store(path: Path, resp: dict, client_id: str, old: dict = None) -> dict:
    old = old or {}
    # Cognito does not rotate refresh tokens by default: keep the old one.
    refresh = resp.get("refresh_token") or old.get("refresh_token")
    if not refresh:
        raise SystemExit(
            "the token endpoint returned no refresh token; check that the app client "
            "allows the refresh-token grant, then sign in again"
        )
    data = {
        "domain": DOMAIN,
        "client_id": client_id,
        "id_token": resp["id_token"],
        "access_token": resp["access_token"],
        "refresh_token": refresh,
        "id_exp": _jwt_exp(resp["id_token"]),
        "access_exp": _jwt_exp(resp["access_token"]),
        "saved_at": int(time.time()),
    }
    _save(path, data)
    return data


def login(path: Path, open_browser: bool = True, client: str = "mcp") -> None:
    cfg = CLIENTS[client]
    redirect = urllib.parse.urlparse(cfg["redirect"])
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(24)
    url = f"{DOMAIN}/oauth2/authorize?" + urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": cfg["id"],
            "redirect_uri": cfg["redirect"],
            "scope": SCOPES,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != redirect.path:
                self.send_response(404)
                self.end_headers()
                return
            q = urllib.parse.parse_qs(parsed.query)
            if q.get("state", [""])[0] != state:
                result["error"] = "state mismatch"
            elif "error" in q:
                result["error"] = q["error"][0]
            else:
                result["code"] = q.get("code", [""])[0]
            ok = "code" in result
            body = (
                "Signed in to COA. You can close this tab." if ok else f"COA sign-in failed: {result.get('error')}"
            ).encode()
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", redirect.port), Handler)  # loopback only
    server.timeout = 5
    print(f"Sign in here if no browser opens:\n{url}", flush=True)
    if open_browser:
        webbrowser.open(url)
    deadline = time.time() + LOGIN_TIMEOUT
    while not result and time.time() < deadline:
        server.handle_request()
    server.server_close()
    if "code" not in result:
        raise SystemExit(f"sign-in did not finish: {result.get('error', 'timed out')}")
    resp = _post_token(
        {
            "grant_type": "authorization_code",
            "client_id": cfg["id"],
            "code": result["code"],
            "redirect_uri": cfg["redirect"],
            "code_verifier": verifier,
        }
    )
    data = _store(path, resp, cfg["id"])
    print(f"Saved tokens to {path}. ID token good until {time.ctime(data['id_exp'])}.")


def get_token(kind: str = "id", path: Path = None, client: str = "mcp") -> str:
    """Return a valid id or access token, refreshing it when close to expiry."""
    path = Path(path) if path else _default_file(client)
    if not path.exists():
        raise SystemExit(f"no token cache at {path}; run: coa_login.py login --client {client}")
    data = json.loads(path.read_text())
    if data[f"{kind}_exp"] - SKEW < time.time():
        try:
            resp = _post_token(
                {
                    "grant_type": "refresh_token",
                    "client_id": data.get("client_id", CLIENTS[client]["id"]),
                    "refresh_token": data["refresh_token"],
                }
            )
        except TokenEndpointError as e:
            if e.status == 400:  # invalid_grant: the refresh token expired or was revoked
                raise SystemExit(f"sign-in expired; run: coa_login.py login --client {client}") from None
            raise
        data = _store(path, resp, data.get("client_id", CLIENTS[client]["id"]), old=data)
    return data[f"{kind}_token"]


def status(path: Path, client: str = "mcp") -> None:
    if not path.exists():
        print(f"no token cache at {path}")
        return
    d = json.loads(path.read_text())
    now = time.time()
    for k in ("id", "access"):
        left = int((d[f"{k}_exp"] - now) / 60)
        print(f"{k} token: {'valid' if left > 0 else 'expired'} ({left} min left)")
    print(f"refresh token: present ({CLIENTS[client]['refresh_life']} from sign-in)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["login", "token", "status", "logout"])
    p.add_argument("--client", choices=sorted(CLIENTS), default="mcp")
    p.add_argument("--file", type=Path, default=None)
    p.add_argument("--kind", choices=["id", "access"], default="id")
    p.add_argument("--no-browser", action="store_true")
    a = p.parse_args()
    a.file = a.file or _default_file(a.client)
    if a.command == "logout":  # local only: needs no domain or client id
        a.file.unlink(missing_ok=True)
        print(f"deleted {a.file}")
        return
    _need("COA_DOMAIN")
    _need("COA_MCP_CLIENT_ID" if a.client == "mcp" else "COA_WEB_CLIENT_ID")
    if a.command == "login":
        login(a.file, open_browser=not a.no_browser, client=a.client)
    elif a.command == "token":
        sys.stdout.write(get_token(a.kind, a.file, a.client))
    else:
        status(a.file, a.client)


if __name__ == "__main__":
    main()
