// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import deploying from '@docs/deploying.md?raw'
import namespaces from '@docs/namespaces.md?raw'
import authenticationSetup from '@docs/authentication-setup.md?raw'
import rolePermissions from '@docs/role-permission-management.md?raw'
import agentAccess from '@docs/agent-access.md?raw'
import cedarPolicies from '@docs/cedar-policy-authoring.md?raw'
import sources from '@docs/sources.md?raw'
import crossAccountSources from '@docs/cross-account-sources.md?raw'
import customConnectorSources from '@docs/custom-connector-sources.md?raw'
import databricksSources from '@docs/databricks-sources.md?raw'
import ontologies from '@docs/ontologies.md?raw'
import metrics from '@docs/metrics.md?raw'
import serve from '@docs/serve.md?raw'
import gettingStarted from '@docs/getting-started.md?raw'
import packageGuide from '@docs/package-guide.md?raw'
import smithyCodegen from '@docs/smithy-codegen.md?raw'

export type PageId =
  | 'deploying'
  | 'namespaces'
  | 'authentication-setup'
  | 'role-permissions'
  | 'agent-access'
  | 'cedar-policies'
  | 'sources'
  | 'cross-account-sources'
  | 'custom-connector-sources'
  | 'databricks-sources'
  | 'ontologies'
  | 'metrics'
  | 'serve'
  | 'getting-started'
  | 'package-guide'
  | 'smithy-codegen'

export const pages: Record<PageId, string> = {
  deploying,
  namespaces,
  'authentication-setup': authenticationSetup,
  'role-permissions': rolePermissions,
  'agent-access': agentAccess,
  'cedar-policies': cedarPolicies,
  sources,
  'cross-account-sources': crossAccountSources,
  'custom-connector-sources': customConnectorSources,
  'databricks-sources': databricksSources,
  ontologies,
  metrics,
  serve,
  'getting-started': gettingStarted,
  'package-guide': packageGuide,
  'smithy-codegen': smithyCodegen
}
