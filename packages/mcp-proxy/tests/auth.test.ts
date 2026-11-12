// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { afterEach, describe, expect, it, vi } from "vitest";
import { mkdtempSync, rmSync, existsSync, readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// Redirect the token cache to a temp dir BEFORE importing the module (the cache
// path is resolved at import time from SCL_CACHE_DIR).
const CACHE_DIR = mkdtempSync(join(tmpdir(), "coa-mcp-proxy-test-"));
process.env.SCL_CACHE_DIR = CACHE_DIR;

const {
  loadCache,
  saveCache,
  discoverEndpoints,
  refreshToken,
  getToken,
  isSafeBrowserUrl,
  browserOpenCommand,
} = await import("../src/auth.js");

const CACHE_FILE = join(CACHE_DIR, "tokens.json");

afterEach(() => {
  vi.restoreAllMocks();
  if (existsSync(CACHE_FILE)) rmSync(CACHE_FILE);
});

describe("loadCache / saveCache", () => {
  it("returns null when no cache file exists", () => {
    expect(loadCache()).toBeNull();
  });

  it("round-trips saved tokens and stamps cached_at", () => {
    saveCache({ access_token: "a", id_token: "i", refresh_token: "r" });
    const loaded = loadCache();
    expect(loaded?.access_token).toBe("a");
    expect(loaded?.id_token).toBe("i");
    expect(loaded?.refresh_token).toBe("r");
    expect(typeof loaded?.cached_at).toBe("number");
  });

  it("returns null AND logs on corrupt cache JSON (file exists but unreadable)", () => {
    mkdirSync(CACHE_DIR, { recursive: true });
    writeFileSync(CACHE_FILE, "{ not valid json");
    const errSpy = vi.spyOn(process.stderr, "write").mockReturnValue(true);
    expect(loadCache()).toBeNull();
    expect(errSpy).toHaveBeenCalledWith(expect.stringContaining("could not be read"));
  });

  it("does NOT log for a missing cache file (silent first-run)", () => {
    const errSpy = vi.spyOn(process.stderr, "write").mockReturnValue(true);
    expect(loadCache()).toBeNull();
    expect(errSpy).not.toHaveBeenCalled();
  });

  it("returns null for a well-formed-JSON but wrong-shape cache", () => {
    mkdirSync(CACHE_DIR, { recursive: true });
    // valid JSON, but missing refresh_token / id_token / cached_at
    writeFileSync(CACHE_FILE, JSON.stringify({ access_token: "a" }));
    expect(loadCache()).toBeNull();
  });
});

describe("discoverEndpoints", () => {
  it("fetches the OIDC well-known document", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: () =>
        Promise.resolve({
          token_endpoint: "https://idp/token",
          authorization_endpoint: "https://idp/authorize",
        }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const cfg = await discoverEndpoints("https://idp");

    expect(fetchMock).toHaveBeenCalledWith("https://idp/.well-known/openid-configuration");
    expect(cfg.token_endpoint).toBe("https://idp/token");
    expect(cfg.authorization_endpoint).toBe("https://idp/authorize");
  });

  it("throws when discovery returns non-ok", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 404 }));
    await expect(discoverEndpoints("https://idp")).rejects.toThrow(/OIDC discovery failed: 404/);
  });
});

describe("refreshToken", () => {
  it("returns the id_token and caches new tokens on success", async () => {
    const fetchMock = vi
      .fn()
      // 1st call: discovery
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ token_endpoint: "https://idp/token", authorization_endpoint: "x" }),
      })
      // 2nd call: token refresh
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ access_token: "acc", id_token: "idt", refresh_token: "newr" }),
      });
    vi.stubGlobal("fetch", fetchMock);

    const token = await refreshToken("client", "https://idp", "oldr");

    expect(token).toBe("idt");
    // new refresh token persisted to cache
    expect(loadCache()?.refresh_token).toBe("newr");
  });

  it("falls back to access_token when id_token is absent", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ token_endpoint: "https://idp/token", authorization_endpoint: "x" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ access_token: "acc", refresh_token: "r2" }),
      });
    vi.stubGlobal("fetch", fetchMock);

    expect(await refreshToken("client", "https://idp", "oldr")).toBe("acc");
  });

  it("preserves the old refresh token when the response omits a new one", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ token_endpoint: "https://idp/token", authorization_endpoint: "x" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ access_token: "acc", id_token: "idt" }),
      });
    vi.stubGlobal("fetch", fetchMock);

    await refreshToken("client", "https://idp", "keepme");
    expect(loadCache()?.refresh_token).toBe("keepme");
  });

  it("returns null when the token endpoint rejects the refresh", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ token_endpoint: "https://idp/token", authorization_endpoint: "x" }),
      })
      .mockResolvedValueOnce({ ok: false, status: 401 });
    vi.stubGlobal("fetch", fetchMock);

    expect(await refreshToken("client", "https://idp", "oldr")).toBeNull();
  });

  it("returns null (swallows) when discovery throws, and logs the fallback reason", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network")));
    const errSpy = vi.spyOn(process.stderr, "write").mockReturnValue(true);
    expect(await refreshToken("client", "https://idp", "oldr")).toBeNull();
    expect(errSpy).toHaveBeenCalledWith(expect.stringContaining("Token refresh failed"));
  });

  it("returns null on a malformed 200 body (no access_token) — parseTokenResponse throws, caught", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ token_endpoint: "https://idp/token", authorization_endpoint: "x" }),
      })
      // 200 OK but the body is not a valid token response (missing access_token)
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ nonsense: true }) });
    vi.stubGlobal("fetch", fetchMock);
    expect(await refreshToken("client", "https://idp", "oldr")).toBeNull();
  });
});

describe("discoverEndpoints — malformed document", () => {
  it("throws when the discovery document is missing endpoints", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ nope: 1 }) }),
    );
    await expect(discoverEndpoints("https://idp")).rejects.toThrow(/Malformed OIDC discovery/);
  });
});

describe("getToken", () => {
  it("uses a cached refresh token and returns the refreshed id_token without a browser flow", async () => {
    saveCache({ access_token: "a", id_token: "i", refresh_token: "cachedR" });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ token_endpoint: "https://idp/token", authorization_endpoint: "x" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ access_token: "acc2", id_token: "idt2", refresh_token: "r3" }),
      });
    vi.stubGlobal("fetch", fetchMock);

    const token = await getToken("client", "https://idp");
    expect(token).toBe("idt2");
  });
});

describe("isSafeBrowserUrl", () => {
  it("accepts the http(s) authorization URLs the PKCE flow builds", () => {
    expect(isSafeBrowserUrl("https://idp.example.com/oauth2/authorize?response_type=code")).toBe(
      true,
    );
    expect(isSafeBrowserUrl("http://localhost:9876/oauth/callback")).toBe(true);
  });

  it("allows percent-encoding, which a real redirect_uri parameter requires", () => {
    expect(
      isSafeBrowserUrl(
        "https://idp.example.com/authorize?redirect_uri=http%3A%2F%2Flocalhost%3A9876%2Foauth%2Fcallback",
      ),
    ).toBe(true);
  });

  it.each([
    ["a non-http scheme", "file:///etc/passwd"],
    ["a javascript: URL", "javascript:alert(1)"],
    ["a relative/unparseable value", "not-a-url"],
    ["an empty string", ""],
  ])("rejects %s", (_label, url) => {
    expect(isSafeBrowserUrl(url)).toBe(false);
  });

  it("rejects quotes and newlines that could split an OS command line", () => {
    expect(isSafeBrowserUrl('https://idp.example.com/a"b')).toBe(false);
    expect(isSafeBrowserUrl("https://idp.example.com/a'b")).toBe(false);
    expect(isSafeBrowserUrl("https://idp.example.com/a\nb")).toBe(false);
  });

  it.each([
    ["backtick", "https://idp.example.com/a`b"],
    ["subexpression", "https://idp.example.com/$(x)"],
    ["semicolon", "https://idp.example.com/a;b"],
    ["pipe", "https://idp.example.com/a|b"],
  ])(
    "still allows %s — safety comes from argv separation, not this denylist",
    (_label, url) => {
      // Documents the intentional limit: these are shell/PowerShell
      // metacharacters, and the URL is never passed through a shell or a
      // PowerShell script string (see browserOpenCommand), so they are inert.
      expect(isSafeBrowserUrl(url)).toBe(true);
    },
  );
});

describe("browserOpenCommand", () => {
  // A realistic authorization URL: no space/tab/quote (so Windows argv
  // flattening would NOT quote it) but several `&` characters.
  const OAUTH_URL =
    "https://idp.example.com/oauth2/authorize?response_type=code&client_id=abc" +
    "&redirect_uri=http%3A%2F%2Flocalhost%3A9876%2Foauth%2Fcallback&state=xyz" +
    "&code_challenge=c1&code_challenge_method=S256";

  it.each([
    ["darwin", "open"],
    ["win32", "rundll32.exe"],
    ["linux", "xdg-open"],
  ])("uses the %s opener", (platform, expected) => {
    const [command] = browserOpenCommand(platform, OAUTH_URL);
    expect(command).toBe(expected);
  });

  it.each(["darwin", "win32", "linux"])(
    "passes the URL as its own argv entry on %s (never re-tokenized)",
    (platform) => {
      const [, args] = browserOpenCommand(platform, OAUTH_URL);
      // Exactly one argv slot equals the URL verbatim — nothing concatenates it
      // into a larger string that a shell or script parser would re-split.
      expect(args.filter((a) => a === OAUTH_URL)).toHaveLength(1);
      expect(args.some((a) => a !== OAUTH_URL && a.includes(OAUTH_URL))).toBe(false);
    },
  );

  it("does not invoke cmd.exe or powershell on win32", () => {
    const [, args] = browserOpenCommand("win32", OAUTH_URL);
    expect(args).toEqual(["url.dll,FileProtocolHandler", OAUTH_URL]);
  });

  it("defaults unknown platforms to xdg-open", () => {
    expect(browserOpenCommand("freebsd", OAUTH_URL)[0]).toBe("xdg-open");
  });
});
