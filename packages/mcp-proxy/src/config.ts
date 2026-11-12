// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Configuration resolver. Tries env vars first, falls back to SSM.
 */

import { SSMClient, GetParameterCommand } from "@aws-sdk/client-ssm";

export interface ProxyConfig {
  clientId: string;
  issuer: string;
  runtimeArn: string;
  runtimeUrl: string;
  region: string;
}

export async function resolveConfig(): Promise<ProxyConfig> {
  const region = process.env.AWS_REGION || "us-east-1";
  // RESOURCE_PREFIX (default "coa") sets the SSM parameter prefix.
  const resourcePrefix = process.env.RESOURCE_PREFIX || "coa";
  const prefix = process.env.SCL_PREFIX || `/${resourcePrefix}`;

  let clientId = process.env.SCL_CLIENT_ID || "";
  let issuer = process.env.SCL_ISSUER || "";
  let runtimeArn = process.env.SCL_RUNTIME_ARN || "";
  let runtimeUrl = process.env.SCL_RUNTIME_URL || "";

  if (!clientId || !issuer || (!runtimeArn && !runtimeUrl)) {
    process.stderr.write("Discovering configuration from SSM...\n");
    const ssm = new SSMClient({ region });

    const getParam = async (name: string): Promise<string> => {
      try {
        const resp = await ssm.send(new GetParameterCommand({ Name: name }));
        return resp.Parameter?.Value || "";
      } catch (err) {
        // Missing param / access-denied / network fault — return empty so the
        // caller can try the next source, but surface why SSM discovery failed
        // (otherwise the final "could not resolve" error hides the real cause).
        process.stderr.write(
          `SSM parameter ${name} unavailable: ${err instanceof Error ? err.message : String(err)}\n`,
        );
        return "";
      }
    };

    if (!clientId) clientId = await getParam(`${prefix}/mcp-client-id`);
    if (!issuer) issuer = await getParam(`${prefix}/issuer`);
    if (!runtimeArn && !runtimeUrl) runtimeArn = await getParam(`${prefix}/mcp/runtime-arn`);
  }

  if (!clientId) throw new Error("Could not resolve SCL_CLIENT_ID (set env var or check SSM)");
  if (!issuer) throw new Error("Could not resolve SCL_ISSUER (set env var or check SSM)");
  if (!runtimeArn && !runtimeUrl) throw new Error("Could not resolve runtime ARN (set env var or check SSM)");

  if (!runtimeUrl) {
    const encodedArn = encodeURIComponent(runtimeArn);
    runtimeUrl = `https://bedrock-agentcore.${region}.amazonaws.com/runtimes/${encodedArn}/invocations?qualifier=DEFAULT`;
  }

  return { clientId, issuer, runtimeArn, runtimeUrl, region };
}
