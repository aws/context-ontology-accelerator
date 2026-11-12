// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Cognito PKCE authentication with token caching.
 *
 * First run: opens browser for login, caches refresh token.
 * Subsequent runs: refreshes silently using cached refresh token.
 */

import { createServer } from "node:http";
import { randomBytes, createHash } from "node:crypto";
import { readFileSync, writeFileSync, mkdirSync, existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { spawn } from "node:child_process";

// Cache location is overridable via SCL_CACHE_DIR (used by tests to avoid
// touching the real ~/.scl token cache).
const CACHE_DIR = process.env.SCL_CACHE_DIR || join(homedir(), ".scl");
const CACHE_FILE = join(CACHE_DIR, "tokens.json");
const CALLBACK_PORT = 9876;
const CALLBACK_PATH = "/oauth/callback";

interface TokenCache {
  access_token: string;
  id_token: string;
  refresh_token: string;
  cached_at: number;
}

interface OidcConfig {
  token_endpoint: string;
  authorization_endpoint: string;
}

interface TokenResponse {
  access_token: string;
  id_token?: string;
  refresh_token?: string;
}

/** Narrow `unknown` to an indexable record without a type assertion. */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/** Narrow an unknown JSON body to a TokenResponse (requires a string access_token). */
function isTokenResponse(value: unknown): value is TokenResponse {
  return isRecord(value) && typeof value.access_token === "string";
}

function parseTokenResponse(value: unknown): TokenResponse {
  if (!isTokenResponse(value)) {
    throw new Error("Malformed token response: missing access_token");
  }
  return value;
}

export async function getToken(clientId: string, issuerUrl: string): Promise<string> {
  const cached = loadCache();

  if (cached?.refresh_token) {
    const token = await refreshToken(clientId, issuerUrl, cached.refresh_token);
    if (token) return token;
  }

  return pkceFlow(clientId, issuerUrl);
}

function isOidcConfig(value: unknown): value is OidcConfig {
  return (
    isRecord(value) &&
    typeof value.token_endpoint === "string" &&
    typeof value.authorization_endpoint === "string"
  );
}

export async function discoverEndpoints(issuerUrl: string): Promise<OidcConfig> {
  const resp = await fetch(`${issuerUrl}/.well-known/openid-configuration`);
  if (!resp.ok) throw new Error(`OIDC discovery failed: ${resp.status}`);
  const body: unknown = await resp.json();
  if (!isOidcConfig(body)) {
    throw new Error("Malformed OIDC discovery document: missing token/authorization endpoint");
  }
  return body;
}

export async function refreshToken(
  clientId: string,
  issuerUrl: string,
  refreshTokenValue: string,
): Promise<string | null> {
  try {
    const { token_endpoint } = await discoverEndpoints(issuerUrl);
    const resp = await fetch(token_endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "refresh_token",
        client_id: clientId,
        refresh_token: refreshTokenValue,
      }),
    });

    if (resp.ok) {
      const data = parseTokenResponse(await resp.json());
      saveCache({
        access_token: data.access_token,
        id_token: data.id_token || "",
        refresh_token: data.refresh_token || refreshTokenValue,
      });
      return data.id_token || data.access_token;
    }
  } catch (err) {
    // Refresh failed — fall through to the interactive PKCE flow. Surface the
    // reason so an expired/revoked refresh token or network fault is diagnosable.
    process.stderr.write(
      `Token refresh failed, falling back to browser login: ${err instanceof Error ? err.message : String(err)}\n`,
    );
  }
  return null;
}

/**
 * True when `raw` is a well-formed absolute http(s) URL safe to pass to the OS
 * browser opener.
 *
 * Rejects any other scheme — `file:`, `javascript:`, and the `cmd:`-style
 * schemes Windows shell handlers may act on — plus quote and newline
 * characters, which are the building blocks of argument-splitting attacks
 * against command-line parsers. Percent-encoding is allowed: the query string
 * legitimately contains `%` (e.g. the encoded `redirect_uri`).
 */
export function isSafeBrowserUrl(raw: string): boolean {
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    return false;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return false;
  return !/["'\r\n]/.test(raw);
}

/**
 * The `[command, argv]` pair used to open `url` in the platform browser.
 * The URL is always its own argv entry and no shell is involved: `cmd /c start`
 * re-parses arguments, and PowerShell treats the `&` in OAuth URLs as an operator.
 */
export function browserOpenCommand(platform: string, url: string): [string, string[]] {
  if (platform === "darwin") return ["open", [url]];
  if (platform === "win32") return ["rundll32.exe", ["url.dll,FileProtocolHandler", url]];
  return ["xdg-open", [url]];
}

/* v8 ignore start -- interactive browser + local-callback-server OAuth flow;
   exercised by integration/manual login, not unit-testable without a real
   browser and loopback HTTP server. The pure token helpers above are covered. */

/** Open `url` in the platform browser without going through a shell. */
function openInBrowser(url: string): void {
  const [command, args] = browserOpenCommand(process.platform, url);
  spawn(command, args, { detached: true, stdio: "ignore" }).unref();
}
async function pkceFlow(clientId: string, issuerUrl: string): Promise<string> {
  const verifier = randomBytes(64).toString("base64url").slice(0, 128);
  const challenge = createHash("sha256").update(verifier).digest("base64url");
  const state = randomBytes(32).toString("base64url");
  const redirectUri = `http://localhost:${CALLBACK_PORT}${CALLBACK_PATH}`;

  const { authorization_endpoint, token_endpoint } = await discoverEndpoints(issuerUrl);

  // Start local callback server and wait for the auth code
  const code = await new Promise<string>((resolve, reject) => {
    const server = createServer((req, res) => {
      const url = new URL(req.url!, `http://localhost:${CALLBACK_PORT}`);
      if (url.pathname !== CALLBACK_PATH) {
        res.writeHead(404);
        res.end();
        return;
      }

      const reqState = url.searchParams.get("state");
      const reqCode = url.searchParams.get("code");
      const error = url.searchParams.get("error");

      if (error) {
        res.writeHead(400);
        res.end("Authentication failed");
        server.close();
        reject(new Error(`OAuth error: ${error}`));
        return;
      }

      if (reqCode && reqState === state) {
        res.writeHead(200, { "Content-Type": "text/html" });
        res.end("<html><body><h1>Login successful!</h1><p>You can close this tab.</p></body></html>");
        server.close();
        resolve(reqCode);
      } else {
        res.writeHead(400);
        res.end("Invalid callback");
        server.close();
        reject(new Error("State mismatch"));
      }
    });

    server.listen(CALLBACK_PORT, "localhost", () => {
      const params = new URLSearchParams({
        response_type: "code",
        client_id: clientId,
        redirect_uri: redirectUri,
        scope: "openid email profile",
        state,
        code_challenge: challenge,
        code_challenge_method: "S256",
      });
      const loginUrl = `${authorization_endpoint}?${params}`;

      process.stderr.write("Opening browser for login...\n");
      process.stderr.write(`If browser doesn't open, visit: ${loginUrl}\n`);

      // Only hand the URL to the OS opener once it is a validated http(s) URL.
      // `authorization_endpoint` comes from the remote OIDC discovery document,
      // so it is untrusted input on this command line.
      if (isSafeBrowserUrl(loginUrl)) {
        openInBrowser(loginUrl);
      } else {
        process.stderr.write("Refusing to open a non-http(s) authorization URL.\n");
      }
    });

    setTimeout(() => {
      server.close();
      reject(new Error("Login timeout (120s)"));
    }, 120_000);
  });

  // Exchange code for tokens
  const resp = await fetch(token_endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      client_id: clientId,
      code,
      redirect_uri: redirectUri,
      code_verifier: verifier,
    }),
  });

  if (!resp.ok) {
    const body = await resp.text();
    throw new Error(`Token exchange failed: ${resp.status} ${body}`);
  }

  const data = parseTokenResponse(await resp.json());
  saveCache({
    access_token: data.access_token,
    id_token: data.id_token || "",
    refresh_token: data.refresh_token || "",
  });

  return data.id_token || data.access_token;
}
/* v8 ignore stop */

function isTokenCache(value: unknown): value is TokenCache {
  return (
    isRecord(value) &&
    typeof value.access_token === "string" &&
    typeof value.id_token === "string" &&
    typeof value.refresh_token === "string" &&
    typeof value.cached_at === "number"
  );
}

export function loadCache(): TokenCache | null {
  // A missing cache file is the normal first-run case — stay silent.
  if (!existsSync(CACHE_FILE)) return null;
  try {
    const parsed: unknown = JSON.parse(readFileSync(CACHE_FILE, "utf-8"));
    return isTokenCache(parsed) ? parsed : null;
  } catch (err) {
    // The file exists but is unreadable/corrupt — that is unexpected, so surface it.
    process.stderr.write(
      `Token cache at ${CACHE_FILE} could not be read: ${err instanceof Error ? err.message : String(err)}\n`,
    );
    return null;
  }
}

export function saveCache(tokens: Omit<TokenCache, "cached_at">): void {
  mkdirSync(CACHE_DIR, { recursive: true, mode: 0o700 });
  writeFileSync(CACHE_FILE, JSON.stringify({ ...tokens, cached_at: Date.now() }), { mode: 0o600 });
}
