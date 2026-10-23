// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * MCP page — everything an agent developer needs to connect an MCP client to
 * this deployment's Model Context Protocol server (#1166 Cut #1).
 *
 * Route: /mcp
 *
 * Built around the PKCE browser-login flow that packages/mcp-proxy already
 * implements: the proxy opens a browser on first run (no stored password),
 * caches a 30-day refresh token in ~/.scl/tokens.json, and auto-discovers the
 * runtime ARN / MCP client id / issuer from SSM under the deployment's prefix.
 * This gives each user their OWN Cognito identity — the server keys Cedar
 * grants and the audit log off the token's `sub`
 * (mcp-server/src/coa_mcp/auth/claims.py) — instead of the shared
 * integration-test secret the old get-token.sh path produced.
 *
 * Laid out actionable-first, in the order a reader actually does them:
 *   1. Before you start  — one line of prerequisites.
 *   2. Add the server    — per-client tabs, each a copy-paste config.
 *   3. Verify            — ask a question inside the agent, with the namespace
 *                          id this deployment needs, and the expected shape of
 *                          a good answer.
 * Reference sections (connection details, tool list, troubleshooting) are
 * collapsed below. All deployment-specific values come from
 * runtime-config.json + the namespace switcher, so nothing is rebuilt per env.
 *
 * When mcpRuntimeArn is absent (MCP stack not deployed, or an older
 * runtime-config.json), the page shows the same "not configured" guidance the
 * Playground uses for a missing serveRuntimeArn rather than a broken endpoint.
 */
import React, { useContext, useState } from "react";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import ContentLayout from "@cloudscape-design/components/content-layout";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import Tabs from "@cloudscape-design/components/tabs";
import { RuntimeConfigContext, useCurrentNamespace } from "@components";
import { buildMcpEndpoint } from "@utils/build-mcp-endpoint";

/**
 * Tools exposed by the MCP server. Mirrors the @mcp.tool() functions in
 * packages/mcp-server/src/coa_mcp/server.py — names and one-line summaries are
 * copied from their docstrings so the console and the server never drift.
 */
const MCP_TOOLS: readonly { name: string; description: string }[] = [
  {
    name: "list_metrics",
    description: "List governed metric definitions in the namespace.",
  },
  {
    name: "describe_schema",
    description:
      "Describe available ontology classes, properties, and data source tables.",
  },
  {
    name: "query",
    description: "End-to-end natural language query with tiered resolution.",
  },
  {
    name: "translate_sparql",
    description:
      "Translate a natural language query to SPARQL using the published ontology.",
  },
  {
    name: "rag_retrieval",
    description:
      "Retrieve semantically similar document chunks from the knowledge base.",
  },
  {
    name: "graph_traversal",
    description:
      "Traverse the semantic graph for entity relationships and context.",
  },
];

/** The SSM prefix this deployment publishes MCP config under. The proxy reads
 * `${prefix}/mcp-client-id`, `${prefix}/issuer`, and `${prefix}/mcp/runtime-arn`
 * (see packages/mcp-proxy/src/config.ts); default aligns with the proxy's own
 * default so a reader who leaves it unset still resolves. */
const SSM_RESOURCE_PREFIX = "coa";

/** A labelled, copy-enabled code block. Mirrors ChatBubble's CodeBlock styling
 * (Box variant="code" + .coa-code-block) plus a copy button, so code surfaces
 * look the same across the console. */
function CopyableCode({
  code,
  ariaLabel,
}: {
  code: string;
  ariaLabel: string;
}) {
  const [copied, setCopied] = useState(false);
  const handleCopy = () => {
    navigator.clipboard.writeText(code).catch(() => {
      // Silently ignore denied clipboard access — the code is still visible.
    });
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };
  return (
    <div style={{ position: "relative" }}>
      <div style={{ position: "absolute", top: 4, right: 4, zIndex: 1 }}>
        <Button
          iconName={copied ? "status-positive" : "copy"}
          variant="icon"
          ariaLabel={ariaLabel}
          onClick={handleCopy}
        />
      </div>
      <Box variant="code">
        <pre className="coa-code-block">{code}</pre>
      </Box>
    </div>
  );
}

/**
 * The proxy command every client runs. packages/mcp-proxy is an in-repo package
 * (coa-mcp-proxy, not published), so the reader runs the built entrypoint from
 * a checkout. RESOURCE_PREFIX and AWS_REGION are the only env vars worth
 * setting; the proxy auto-discovers everything else (client id, issuer, runtime
 * ARN) from SSM and authenticates via a browser PKCE login on first run. We do
 * NOT set AWS_PROFILE — the proxy uses the default credential chain, and a
 * literal "your-profile" placeholder breaks anyone on default credentials.
 */
const PROXY_COMMAND = "node";
const PROXY_ARGS = ["/absolute/path/to/mcp-proxy/dist/index.js"];

/**
 * The raw-source download bundle, generated at build time by
 * `web-app/scripts/pack-mcp-proxy.mjs` (wired as the `prebuild` step) and
 * served from the same auth-gated CloudFront distribution as the rest of the
 * console — not a public endpoint. It contains the proxy's TypeScript SOURCE
 * (not a compiled blob) so a user without repo access can read exactly what
 * runs with their AWS credentials before trusting it, then build it with
 * `npm install && npx tsc --outDir dist`. The `.sha256` sidecar lets the user
 * verify the download was not tampered with.
 */
const PROXY_SRC_ZIP = "/downloads/coa-mcp-proxy-src.zip";
const PROXY_SRC_SHA = "/downloads/coa-mcp-proxy-src.zip.sha256";

/**
 * Shows the published SHA-256 of the source bundle, fetched at runtime from its
 * `.sha256` sidecar so it is always correct for the deployed artifact and never
 * hardcoded (which would silently go stale). A copy button lets the user paste
 * it into `sha256sum -c`. Degrades quietly if the sidecar is absent.
 */
function BundleChecksum() {
  const [sha, setSha] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  React.useEffect(() => {
    let cancelled = false;
    fetch(PROXY_SRC_SHA)
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error("no sha"))))
      .then((text) => {
        if (cancelled) return;
        // Sidecar format: "<hex>  <filename>"; keep just the hex digest.
        const digest = text.trim().split(/\s+/)[0] ?? "";
        // A missing sidecar is served as the SPA index.html with HTTP 200
        // (not a 404), so r.ok is not enough: reject anything that is not a
        // real 64-char hex SHA-256 rather than rendering "<!DOCTYPE" as the sum.
        if (!/^[0-9a-f]{64}$/i.test(digest)) {
          setFailed(true);
          return;
        }
        setSha(digest);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);
  if (failed || !sha) return null;
  return (
    <SpaceBetween size="xxs">
      <Box variant="awsui-key-label">SHA-256 (verify your download)</Box>
      <CopyableCode code={sha} ariaLabel="Copy the bundle SHA-256 checksum" />
    </SpaceBetween>
  );
}

/** Build the mcp.json-style block Kiro / Claude Desktop / Cursor all share. */
function buildJsonConfig(region: string, serverKey: string): string {
  return JSON.stringify(
    {
      mcpServers: {
        [serverKey]: {
          command: PROXY_COMMAND,
          args: PROXY_ARGS,
          env: {
            AWS_REGION: region,
            RESOURCE_PREFIX: SSM_RESOURCE_PREFIX,
          },
          autoApprove: MCP_TOOLS.map((t) => t.name),
        },
      },
    },
    null,
    2,
  );
}

/** Claude Code adds servers via a CLI command rather than an edited file. */
function buildClaudeCodeCommand(region: string): string {
  return `claude mcp add coa -- \\
  env AWS_REGION=${region} RESOURCE_PREFIX=${SSM_RESOURCE_PREFIX} \\
  ${PROXY_COMMAND} ${PROXY_ARGS[0]}`;
}

export const McpPage: React.FC = () => {
  const runtimeContext = useContext(RuntimeConfigContext);
  const { namespaceId, current } = useCurrentNamespace();
  const endpoint = buildMcpEndpoint(runtimeContext);

  // Mirror the Playground's guidance for a missing serveRuntimeArn: the MCP
  // stack publishes mcpRuntimeArn into runtime-config.json, so an absent value
  // means MCP is not deployed (or the config predates it) rather than an error.
  if (!endpoint) {
    return (
      <ContentLayout header={<Header variant="h1">MCP Server</Header>}>
        <Container>
          <StatusIndicator type="warning">
            MCP endpoint not configured. Deploy the MCP stack and ensure
            mcpRuntimeArn is present in runtime-config.json.
          </StatusIndicator>
        </Container>
      </ContentLayout>
    );
  }

  const region = runtimeContext!.region;
  const kiroConfig = buildJsonConfig(region, "coa");
  const claudeDesktopConfig = buildJsonConfig(region, "coa");
  const cursorConfig = buildJsonConfig(region, "coa");
  const claudeCodeCmd = buildClaudeCodeCommand(region);

  // The id the agent needs to scope every tool call. The header shows the name;
  // the id lives only in the switcher (hidden on narrow screens), so surface it
  // here where setup happens.
  const hasNamespace = namespaceId !== "";
  const nsLabel = current?.name
    ? `${current.name} (${namespaceId})`
    : namespaceId;
  const verifyPrompt = hasNamespace
    ? `Using namespace ${namespaceId}, list the metrics you can see.`
    : `List the metrics you can see. (Open a namespace first so the agent has an id to scope to.)`;

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description="Connect an MCP client (Kiro, Claude Code, Claude Desktop, Cursor, or any streamable-http client) to this deployment's Model Context Protocol server."
        >
          MCP Server
        </Header>
      }
    >
      <SpaceBetween size="l">
        {/* One-line auth summary — the whole "how this authenticates" story. */}
        <Box variant="p">
          Each request is signed in with your own browser login (Cognito PKCE).
          The bundled proxy opens a browser the first time, then keeps you
          signed in for 30 days — no passwords stored, and your access and the
          audit log reflect you, not a shared account.
        </Box>

        {/* ---- STEP 1: before you start ---- */}
        <ExpandableSection
          variant="container"
          headerText="1. Before you start"
          headerDescription="One-time prerequisites. Download the proxy source below (no repo access needed) and build it, or build it from a repo checkout."
        >
          <ul className="coa-mcp-prereqs">
            <li>Node.js 18+ (the proxy runs on Node).</li>
            <li>
              AWS credentials for this account on your default profile (any
              read-only role that can read SSM). The proxy uses your default
              credential chain — no profile name to configure.
            </li>
            <li>
              A built copy of the proxy. You do not need repo access — download
              the proxy <strong>source</strong> and build it locally:
              <SpaceBetween size="xs">
                <Box variant="p">
                  <Button
                    iconName="download"
                    href={PROXY_SRC_ZIP}
                    download="coa-mcp-proxy-src.zip"
                    target="_blank"
                  >
                    Download proxy source (.zip)
                  </Button>
                </Box>
                <Box variant="p">
                  It is the proxy's TypeScript <strong>source</strong> (not a
                  prebuilt binary) so you — or your security team — can read
                  exactly what runs with your AWS credentials before trusting
                  it. Unzip, then build:
                </Box>
                <Box variant="code">
                  cd coa-mcp-proxy &amp;&amp; npm install &amp;&amp; npx tsc
                  --outDir dist
                </Box>
                <BundleChecksum />
                <Box variant="p" color="text-body-secondary">
                  Verify the download with{" "}
                  <Box variant="code">
                    sha256sum -c coa-mcp-proxy-src.zip.sha256
                  </Box>{" "}
                  (the <Box variant="code">.sha256</Box> file downloads
                  alongside the zip). If you already have a repo checkout, build{" "}
                  <Box variant="code">packages/mcp-proxy</Box> the same way
                  instead.
                </Box>
                <Box variant="p">
                  Use the absolute path to the resulting{" "}
                  <Box variant="code">dist/index.js</Box> in the config below.
                </Box>
              </SpaceBetween>
            </li>
          </ul>
        </ExpandableSection>

        {/* ---- STEP 2: add the server (the hero) ---- */}
        <ExpandableSection
          variant="container"
          defaultExpanded
          headerText="2. Add the MCP server to your client"
          headerDescription="Pick your client, point command at your built proxy path, and paste. Region and SSM prefix are pre-filled for this deployment; everything else is auto-discovered."
        >
          <Tabs
            tabs={[
              {
                id: "kiro",
                label: "Kiro",
                content: (
                  <SpaceBetween size="xs">
                    <Box variant="p">
                      Add to <Box variant="code">.kiro/settings/mcp.json</Box>.
                    </Box>
                    <CopyableCode
                      code={kiroConfig}
                      ariaLabel="Copy Kiro MCP configuration"
                    />
                  </SpaceBetween>
                ),
              },
              {
                id: "claude-code",
                label: "Claude Code",
                content: (
                  <SpaceBetween size="xs">
                    <Box variant="p">
                      Run from your terminal (registers the server globally).
                    </Box>
                    <CopyableCode
                      code={claudeCodeCmd}
                      ariaLabel="Copy the claude mcp add command"
                    />
                  </SpaceBetween>
                ),
              },
              {
                id: "claude-desktop",
                label: "Claude Desktop",
                content: (
                  <SpaceBetween size="xs">
                    <Box variant="p">
                      Add to{" "}
                      <Box variant="code">claude_desktop_config.json</Box>{" "}
                      (Settings → Developer → Edit Config).
                    </Box>
                    <CopyableCode
                      code={claudeDesktopConfig}
                      ariaLabel="Copy Claude Desktop MCP configuration"
                    />
                  </SpaceBetween>
                ),
              },
              {
                id: "cursor",
                label: "Cursor",
                content: (
                  <SpaceBetween size="xs">
                    <Box variant="p">
                      Add to <Box variant="code">~/.cursor/mcp.json</Box> (or a
                      project-level <Box variant="code">.cursor/mcp.json</Box>).
                    </Box>
                    <CopyableCode
                      code={cursorConfig}
                      ariaLabel="Copy Cursor MCP configuration"
                    />
                  </SpaceBetween>
                ),
              },
            ]}
          />
        </ExpandableSection>

        {/* ---- STEP 3: verify inside the agent ---- */}
        <ExpandableSection
          variant="container"
          defaultExpanded
          headerText="3. Verify it works"
          headerDescription="Ask the agent a question that uses a tool. The first call opens a browser to sign you in."
        >
          <SpaceBetween size="s">
            <Box variant="p">
              This deployment scopes tools to a namespace. Use this namespace
              id:
            </Box>
            <Box variant="awsui-key-label">Active namespace</Box>
            {hasNamespace ? (
              <CopyableCode
                code={nsLabel}
                ariaLabel="Copy the active namespace id"
              />
            ) : (
              <StatusIndicator type="info">
                No namespace yet — create one (Administration → Namespaces)
                first, then its id appears here.
              </StatusIndicator>
            )}
            <Box variant="p">Then ask your agent:</Box>
            <CopyableCode
              code={verifyPrompt}
              ariaLabel="Copy the verification prompt"
            />
            <Box variant="p">
              A working setup returns a list of governed metrics for the
              namespace (not an error and not an empty refusal). If the browser
              login did not open, the proxy path in your config is likely wrong.
            </Box>
          </SpaceBetween>
        </ExpandableSection>

        {/* ---- INFORMATIONAL (reference only) ---- */}

        <ExpandableSection
          variant="container"
          headerText="Troubleshooting"
          headerDescription="The three failures people actually hit."
        >
          <SpaceBetween size="s">
            <Box variant="p">
              <strong>"profile not found" / credential errors.</strong> The
              proxy uses your default AWS credential chain. Don't set an
              AWS_PROFILE you don't have; run{" "}
              <Box variant="code">aws sts get-caller-identity</Box> to confirm
              your default credentials resolve for this account.
            </Box>
            <Box variant="p">
              <strong>"access denied" on a namespace.</strong> Your Cognito user
              is now your real identity (not a shared account), so you only see
              namespaces you are granted. Ask a namespace admin to grant your
              user, or switch to a namespace you own.
            </Box>
            <Box variant="p">
              <strong>Tools stop working mid-session.</strong> The PKCE session
              lasts 30 days and refreshes silently; if it lapses after long
              inactivity, the next call reopens the browser login. To force a
              fresh login, delete <Box variant="code">~/.scl/tokens.json</Box>.
            </Box>
          </SpaceBetween>
        </ExpandableSection>

        <ExpandableSection
          variant="container"
          headerText="Connection details"
          headerDescription="Reference values — the proxy discovers these from SSM automatically. Expand only if you are hand-assembling a request or overriding discovery."
        >
          <SpaceBetween size="s">
            <Box variant="p">
              The MCP server is exposed as an AgentCore Runtime, invoked over
              streamable-http. The proxy signs each request with a Cognito
              bearer token (Authorization: Bearer &lt;IdToken&gt;) obtained via
              the browser PKCE login. It auto-discovers the runtime ARN, MCP
              client id, and issuer from SSM under{" "}
              <Box variant="code">/{SSM_RESOURCE_PREFIX}</Box>.
            </Box>
            <Box variant="awsui-key-label">Endpoint</Box>
            <CopyableCode code={endpoint} ariaLabel="Copy MCP endpoint URL" />
          </SpaceBetween>
        </ExpandableSection>

        <ExpandableSection
          variant="container"
          headerText={`Available tools (${MCP_TOOLS.length})`}
          headerDescription="The tools this MCP server exposes, for reference while you use it."
        >
          <Table
            variant="embedded"
            items={[...MCP_TOOLS]}
            columnDefinitions={[
              {
                id: "name",
                header: "Tool",
                cell: (t) => <Box variant="code">{t.name}</Box>,
              },
              {
                id: "description",
                header: "Description",
                cell: (t) => t.description,
              },
            ]}
          />
        </ExpandableSection>
      </SpaceBetween>
    </ContentLayout>
  );
};
