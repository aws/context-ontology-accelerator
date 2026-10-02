#!/usr/bin/env node
// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import {
  connectorFunctionName,
  deploymentEnv,
  functionNamePrefix,
  loadEnvFiles,
} from "coa-connector-cdk";
import { CONNECTOR_ID, MANAGED_MODE } from "../lib/constants";
import { DatabricksConnectorStack } from "../lib/databricks-connector-stack";
import { ManagedDatabricksConnectorStack } from "../lib/managed-databricks-connector-stack";
import { resolveManagedDeployment } from "../lib/managed-deployment";
import { connectorMode } from "../lib/mode";

// Two locations, named rather than found by searching upwards: this app's own directory, then the shared
// one at connectors/. Most specific first, and nothing overwrites a variable that is already set, so an
// app-local .env beats the shared file and a pipeline's exported values beat both.
const appDir = path.join(__dirname, "..");
const connectorsRoot = path.join(appDir, "..", "..");
loadEnvFiles(appDir, connectorsRoot);

const app = new cdk.App();

// Both branches give the stack and the Lambda the same name, from one call, so a redeploy replaces a
// connector rather than adding one.
if (connectorMode() === MANAGED_MODE) {
  // COA's own deployment: one Lambda for every Databricks source in one COA environment. The name is
  // fixed for the life of the deployment, because athena:CreateDataCatalog embeds the function ARN and
  // never re-resolves it. Run by scripts/deploy-managed-databricks-connector.sh.
  const stackName = resolveManagedDeployment().functionName;
  new ManagedDatabricksConnectorStack(app, stackName, {
    stackName,
    env: deploymentEnv(),
  });
} else {
  // The customer-deployed shape: one workspace, one warehouse, one credential, all from this
  // deployment's own variables. FUNCTION_NAME_PREFIX is what tells two such deployments in one account
  // apart.
  const prefix = functionNamePrefix();
  const stackName = connectorFunctionName(CONNECTOR_ID, prefix);
  new DatabricksConnectorStack(app, stackName, {
    stackName,
    env: deploymentEnv(),
    functionNamePrefix: prefix,
  });
}
