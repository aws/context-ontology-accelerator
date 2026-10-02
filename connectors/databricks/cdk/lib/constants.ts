// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as path from "path";

/**
 * The connector's id. Names the stack and the Lambda (`databricks-coa-connector`), so a second
 * connector deployed into the same account cannot collide with this one.
 */
export const CONNECTOR_ID = "databricks";

/** Handler class in the connector's fat jar. */
export const HANDLER = "dev.coa.databricks.DatabricksCompositeHandler";

/**
 * 3008 MB and 600 s. The memory is above the construct's 1024 MB default; the timeout matches it.
 *
 * 600 s covers a stopped SQL Warehouse resuming ahead of a large read. What bounds a runaway read is
 * the row ceiling ({@link DEFAULT_MAX_ROWS_PER_TABLE}), which fails with an error naming the table,
 * and the statement and socket timeouts inside the jar, which both sit below this value.
 *
 * The memory is sized for the read path: Athena's federation protocol cannot express aggregation, so
 * a `GROUP BY` reads every predicate-matching row out of the warehouse.
 */
export const MEMORY_SIZE_MB = 3008;
export const TIMEOUT_SECONDS = 600;

/** The fat jar, relative to this app. Built by `pnpm run package` before deploy. */
export const DEFAULT_JAR_PATH = path.join(
  __dirname,
  "..",
  "..",
  "target",
  "databricks-connector-1.0.0.jar",
);

/**
 * The row ceiling the connector applies when `DATABRICKS_MAX_ROWS_PER_TABLE` is unset.
 *
 * Restated from `Settings.DEFAULT_MAX_ROWS_PER_TABLE` in Java, with nothing enforcing that the two
 * agree. Only the `ConnectorRowsReturned` alarm's threshold depends on it, so a drift makes that
 * alarm fire early or late rather than changing what the connector does.
 */
export const DEFAULT_MAX_ROWS_PER_TABLE = 2_000_000;

/** Lambda environment key selecting the mode. Absent means `environment`, as in the jar. */
export const CONFIG_SOURCE_ENV_VAR = "DATABRICKS_CONFIG_SOURCE";

/** The modes the jar accepts, mirroring `ConfigSource` on the Java side. */
export type ConnectorMode = "environment" | "coa-managed";

/** What a managed deployment sets {@link CONFIG_SOURCE_ENV_VAR} to. */
export const MANAGED_MODE: ConnectorMode = "coa-managed";

/**
 * What `COA_CONFIG_SSM_PREFIX` ends with: `/{prefix}/{envName}` then this.
 *
 * Keyed on the sub-type rather than the connector, so the sources API reads a published fact instead
 * of reconstructing the function's name to build the lookup. Restated here rather than imported so
 * the app builds when copied out; the copy in the sources stack is the cross-component contract.
 */
export const CONFIG_SSM_PREFIX_SUFFIX = "/connectors/databricks/sources";

/**
 * Where the connector publishes its own function ARN, replacing {@link CONFIG_SSM_PREFIX_SUFFIX}.
 *
 * A sibling subtree rather than a deeper key under `sources/`, because the writers and the
 * directions differ: the sources API writes `sources/` and reads `deployment/`, and a single prefix
 * would let it overwrite the ARN it later reads.
 */
export const FUNCTION_ARN_PARAMETER_SUFFIX = "/connectors/databricks/deployment/function-arn";

/**
 * Where the connector publishes its own execution role ARN, beside its function ARN.
 *
 * Published rather than derived because the customer's trust policy names this role, and one naming
 * the wrong principal fails every assume with an `AccessDenied` that points at nothing.
 */
export const ROLE_ARN_PARAMETER_SUFFIX = "/connectors/databricks/deployment/role-arn";

/**
 * The segment that tells COA's own deployment of this connector apart from a customer's.
 *
 * Reserved, and `assertNotManagedFunctionName` refuses it in `environment` mode: such a deploy would
 * not collide with the managed stack but *update* it, and since `athena:CreateDataCatalog` embeds the
 * function ARN for good, every catalog COA registered would keep invoking a function that has
 * stopped reading the catalog name.
 */
export const MANAGED_FUNCTION_NAME_SEGMENT = "managed-";

/** What a managed deployment's execution role name appends to the resource prefix. */
export const CONNECTOR_ROLE_NAME_SUFFIX = "databricks-connector-role";

/**
 * The environment whose spill bucket and CMK survive a `cdk destroy`, matching how `infra` decides
 * the same thing (`envName === "prod" ? RETAIN : DESTROY`).
 */
export const PROD_ENV_NAME = "prod";
