// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { ConnectSource } from "./ConnectSource";

// Wizard tests drive Cloudscape through userEvent and run near vitest's 5s default under CI load.
vi.setConfig({ testTimeout: 15000 });

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const NS_ID = "550e8400-e29b-41d4-a716-446655440000";

const mockCreate = vi.fn();
const mockCreateAsync = vi.fn();

// Matches the server-side derivation: `{resource-prefix}{namespaceId}`.
const DERIVED_EXTERNAL_ID = "coa-dev-550e8400-e29b-41d4-a716-446655440000";

vi.mock("@api-hooks", () => ({
  useCreateSource: ({
    onSuccess,
  }: {
    onSuccess?: (result: { body: { sourceId: string } }) => void;
  } = {}) => ({
    mutate: (args: unknown) => {
      mockCreate(args);
      onSuccess?.({ body: { sourceId: "src-new" } });
    },
    mutateAsync: (args: unknown) => {
      mockCreateAsync(args);
      return Promise.resolve({ body: { sourceId: "src-new" } });
    },
    isPending: false,
    error: null,
  }),
  useGetSourceUploadUrls: () => ({
    mutateAsync: vi.fn().mockResolvedValue({
      uploadId: "11111111-1111-4111-8111-111111111111",
      s3Prefix: "s3://bucket/prefix",
      uploadUrls: [],
    }),
    isPending: false,
  }),
  // The cross-account External ID is derived server-side and read from the
  // namespace detail — the page never accepts one as input.
  useGetNamespace: () => ({
    data: { namespace: { datasourceExternalId: DERIVED_EXTERNAL_ID } },
    isLoading: false,
    error: null,
  }),
}));

// Both DATABASE/DOCUMENTS enums and the exception class are imported in the
// component; provide a minimal shim that satisfies both call sites.
vi.mock("@coa/control-plane-client", () => ({
  SourceType: { DATABASE: "DATABASE", DOCUMENTS: "DOCUMENTS" },
  ControlPlaneServiceServiceException: class extends Error {
    constructor(msg = "ControlPlaneServiceServiceException") {
      super(msg);
      this.name = "ControlPlaneServiceServiceException";
    }
  },
}));

// Partial mock: the upload constraints are stubbed to keep this suite independent
// of the real MIME list.
vi.mock("@coa/shared", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@coa/shared")>()),
  SUPPORTED_UPLOAD_CONTENT_TYPES: new Set<string>([
    "application/pdf",
    "text/plain",
  ]),
  MAX_UPLOAD_FILES: 100,
}));

vi.mock("@utils/helpers", () => ({
  S3_ARN_RE: /^arn:aws:s3:::[a-z0-9.-]+$/,
  validateS3Prefix: () => null,
}));

// ---------------------------------------------------------------------------
// Wrapper
// ---------------------------------------------------------------------------

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={new QueryClient()}>
    <MemoryRouter initialEntries={[`/namespaces/${NS_ID}/sources/connect`]}>
      <Routes>
        <Route
          path="/namespaces/:namespaceId/sources/connect"
          element={children}
        />
        <Route
          path="/namespaces/:namespaceId/sources/:sourceId"
          element={<div>SOURCE_DETAIL</div>}
        />
        <Route
          path="/namespaces/:namespaceId/sources"
          element={<div>SOURCE_LIST</div>}
        />
      </Routes>
    </MemoryRouter>
  </QueryClientProvider>
);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const user = userEvent.setup({ delay: null });

/** Click the wizard's "Next" button. Cloudscape renders it with that label. */
async function clickNext() {
  await user.click(screen.getByRole("button", { name: /^next$/i }));
}

/** Walk the Glue wizard from step 1 → step 3 (enrichment) with valid inputs. */
async function navigateToEnrichmentStep() {
  await clickNext();
  await user.type(screen.getByPlaceholderText("My Glue Database"), "my-db");
  await user.type(screen.getByPlaceholderText("123456789012"), "123456789012");
  await user.type(screen.getByPlaceholderText("my_glue_db"), "my_db");
  await clickNext();
}

const CONNECTOR_ARN =
  "arn:aws:lambda:us-east-1:123456789012:function:my-connector";

/** Select the custom connector tile and fill its required step-2 fields. */
async function fillCustomConnectorStep() {
  await user.click(screen.getByLabelText("Custom connector"));
  await clickNext();
  await user.type(
    screen.getByPlaceholderText("My Custom Connector"),
    "my-connector",
  );
  await user.type(screen.getByPlaceholderText(CONNECTOR_ARN), CONNECTOR_ARN);
  await user.type(screen.getByPlaceholderText("my_connector_db"), "my_db");
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("ConnectSource — metadata enrichment toggle", () => {
  beforeEach(() => {
    mockCreate.mockReset();
    mockCreateAsync.mockReset();
  });

  it("renders the AI metadata enrichment toggle in the enrichment step", async () => {
    render(<ConnectSource />, { wrapper });

    await navigateToEnrichmentStep();

    expect(
      screen.getByText(/Enable AI metadata enrichment/i),
    ).toBeInTheDocument();
  });

  it("submits metadataEnrichmentEnabled=true when the toggle is left on", async () => {
    render(<ConnectSource />, { wrapper });

    await navigateToEnrichmentStep();
    // Step 3 — leave the toggle in its default (on) state, advance to review.
    await clickNext();
    // Step 4 — submit. The Wizard's submit button uses i18nStrings text.
    await user.click(screen.getByRole("button", { name: /^connect source$/i }));

    expect(mockCreate).toHaveBeenCalledTimes(1);
    const body = mockCreate.mock.calls[0][0].body;
    expect(body.databaseSource.metadataEnrichmentEnabled).toBe(true);
  });

  it("submits metadataEnrichmentEnabled=false after toggling the switch off", async () => {
    render(<ConnectSource />, { wrapper });

    await navigateToEnrichmentStep();

    // Cloudscape Toggle exposes a checkbox-role input under the hood; clicking
    // the visible label flips it.
    await user.click(screen.getByText(/Enable AI metadata enrichment/i));

    await clickNext();
    await user.click(screen.getByRole("button", { name: /^connect source$/i }));

    expect(mockCreate).toHaveBeenCalledTimes(1);
    const body = mockCreate.mock.calls[0][0].body;
    expect(body.databaseSource.metadataEnrichmentEnabled).toBe(false);
  });

  it("review step reflects the toggle state in the enrichment summary", async () => {
    render(<ConnectSource />, { wrapper });

    await navigateToEnrichmentStep();
    await user.click(screen.getByText(/Enable AI metadata enrichment/i));
    await clickNext();

    // Review step renders a "Metadata enrichment" KeyValuePair whose value
    // is "Disabled" when the toggle is off.
    expect(screen.getByText("Metadata enrichment")).toBeInTheDocument();
    expect(screen.getByText("Disabled")).toBeInTheDocument();
  });
});

describe("ConnectSource — advanced configuration", () => {
  beforeEach(() => {
    mockCreate.mockReset();
    mockCreateAsync.mockReset();
  });

  it("renders required field indicators on JDBC form", async () => {
    render(<ConnectSource />, { wrapper });

    // Select JDBC tile
    await user.click(screen.getByLabelText("JDBC database"));
    await clickNext();

    // Required fields should have asterisks
    expect(
      screen.getByText("Source name", { exact: false }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Database engine", { exact: false }),
    ).toBeInTheDocument();
    expect(screen.getByText("Host", { exact: false })).toBeInTheDocument();
  });

  it("renders advanced configuration section for JDBC with new filter fields", async () => {
    render(<ConnectSource />, { wrapper });

    await user.click(screen.getByLabelText("JDBC database"));
    await clickNext();

    // Advanced section should be present
    const advancedSection = screen.getByText("Advanced configuration");
    expect(advancedSection).toBeInTheDocument();

    // Expand it
    await user.click(advancedSection);

    // New exclude filter fields should be present
    expect(screen.getByText("Schema exclude filter")).toBeInTheDocument();
    expect(screen.getByText("Table exclude filter")).toBeInTheDocument();
  });

  // Types out several long fields character-by-character across a multi-step
  // wizard — exceeds the 5000ms vitest default under CI load.
  it(
    "submits schemaExcludeFilter and tableExcludeFilter for JDBC when provided",
    { timeout: 15000 },
    async () => {
      render(<ConnectSource />, { wrapper });

      await user.click(screen.getByLabelText("JDBC database"));
      await clickNext();

      // Fill required fields
      await user.type(
        screen.getByPlaceholderText("My JDBC Database"),
        "test-db",
      );
      await user.click(screen.getByText("Select an engine"));
      await user.click(screen.getAllByText("PostgreSQL")[0]);
      await user.type(
        screen.getByPlaceholderText("db.example.com"),
        "localhost",
      );
      await user.type(screen.getByPlaceholderText("my_database"), "testdb");
      await user.type(
        screen.getByPlaceholderText("arn:aws:secretsmanager:..."),
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:db",
      );

      // Expand advanced config
      await user.click(screen.getByText("Advanced configuration"));

      // Fill exclude filters
      await user.type(
        screen.getByPlaceholderText("temp_*|staging_*"),
        "temp_*|test_*",
      );
      await user.type(
        screen.getByPlaceholderText("tmp_*|staging_*"),
        "tmp_*|scratch_*",
      );

      await clickNext(); // → enrichment
      await clickNext(); // → review
      await user.click(
        screen.getByRole("button", { name: /^connect source$/i }),
      );

      expect(mockCreate).toHaveBeenCalledTimes(1);
      const cfg =
        mockCreate.mock.calls[0][0].body.databaseSource.jdbcConfiguration;
      expect(cfg.schemaExcludeFilter).toBe("temp_*|test_*");
      expect(cfg.tableExcludeFilter).toBe("tmp_*|scratch_*");
    },
  );

  it("renders tableExcludeFilter for Glue in advanced config", async () => {
    render(<ConnectSource />, { wrapper });

    // Glue is default
    await clickNext();

    // Fill required fields
    await user.type(screen.getByPlaceholderText("My Glue Database"), "test-db");
    await user.type(
      screen.getByPlaceholderText("123456789012"),
      "123456789012",
    );
    await user.type(screen.getByPlaceholderText("my_glue_db"), "testdb");

    // Expand advanced config
    await user.click(screen.getByText("Advanced configuration"));

    // New exclude filter field should be present
    expect(screen.getByText("Table exclude filter")).toBeInTheDocument();
  });

  it("submits tableExcludeFilter for Glue when provided", async () => {
    render(<ConnectSource />, { wrapper });

    await clickNext();
    await user.type(screen.getByPlaceholderText("My Glue Database"), "test-db");
    await user.type(
      screen.getByPlaceholderText("123456789012"),
      "123456789012",
    );
    await user.type(screen.getByPlaceholderText("my_glue_db"), "testdb");

    // Expand advanced and fill exclude filter
    await user.click(screen.getByText("Advanced configuration"));
    await user.type(
      screen.getByPlaceholderText("tmp_.*|staging_.*"),
      "tmp_.*|backup_.*",
    );

    await clickNext(); // → enrichment
    await clickNext(); // → review
    await user.click(screen.getByRole("button", { name: /^connect source$/i }));

    expect(mockCreate).toHaveBeenCalledTimes(1);
    const cfg =
      mockCreate.mock.calls[0][0].body.databaseSource.glueConfiguration;
    expect(cfg.tableExcludeFilter).toBe("tmp_.*|backup_.*");
  });

  it("does not offer engines withheld from the product", async () => {
    render(<ConnectSource />, { wrapper });

    await user.click(screen.getByLabelText("JDBC database"));
    await clickNext();
    await user.click(screen.getByText("Select an engine"));

    // All engines are offered in the picker.
    expect(screen.getAllByText("Snowflake")[0]).toBeInTheDocument();
    expect(screen.getAllByText("Oracle")[0]).toBeInTheDocument();
    expect(screen.getAllByText("PostgreSQL")[0]).toBeInTheDocument();
    expect(screen.getAllByText("SQL Server")[0]).toBeInTheDocument();
  });

  it("advertises all engines in the JDBC source description", () => {
    render(<ConnectSource />, { wrapper });

    const description = screen.getByText(/Supported engines:/);
    expect(description.textContent).toContain("PostgreSQL");
    expect(description.textContent).toContain("Oracle");
    expect(description.textContent).toContain("Snowflake");
  });
});

describe("ConnectSource — cross-account IAM role validation", () => {
  /**
   * Validate cross-account IAM role ARN format.
   * Backend IAM policy restricts AssumeRole to roles matching:
   *   arn:aws:iam::*:role/{prefix}-{env}-datasource-access-*
   * Default prefix is "coa", default env is "dev", so pattern becomes:
   *   arn:aws:iam::ACCOUNT:role/coa-dev-datasource-access-SUFFIX
   */
  function validateCrossAccountRoleArn(arn: string): string | undefined {
    if (!arn.trim()) return undefined;
    const roleArnPattern =
      /^arn:aws:iam::\d{12}:role\/[a-zA-Z0-9_+=,.@-]+-datasource-access-[a-zA-Z0-9_+=,.@-]+$/;
    if (!roleArnPattern.test(arn.trim())) {
      return "Role ARN must match pattern: arn:aws:iam::ACCOUNT:role/{prefix}-datasource-access-*";
    }
    return undefined;
  }

  it("accepts empty role ARN (optional field)", () => {
    expect(validateCrossAccountRoleArn("")).toBeUndefined();
    expect(validateCrossAccountRoleArn("   ")).toBeUndefined();
  });

  it("accepts valid role ARN with datasource-access pattern", () => {
    expect(
      validateCrossAccountRoleArn(
        "arn:aws:iam::123456789012:role/coa-dev-datasource-access-acme",
      ),
    ).toBeUndefined();
    expect(
      validateCrossAccountRoleArn(
        "arn:aws:iam::222222222222:role/coa-dev-datasource-access-glue-catalog",
      ),
    ).toBeUndefined();
  });

  it("rejects role ARN missing datasource-access infix", () => {
    const error = validateCrossAccountRoleArn(
      "arn:aws:iam::123456789012:role/coa-dev-my-role",
    );
    expect(error).toBe(
      "Role ARN must match pattern: arn:aws:iam::ACCOUNT:role/{prefix}-datasource-access-*",
    );
  });

  it("rejects role ARN with datasource-access but no prefix", () => {
    const error = validateCrossAccountRoleArn(
      "arn:aws:iam::123456789012:role/datasource-access-acme",
    );
    expect(error).toBe(
      "Role ARN must match pattern: arn:aws:iam::ACCOUNT:role/{prefix}-datasource-access-*",
    );
  });

  it("rejects role ARN with datasource-access but no suffix", () => {
    const error = validateCrossAccountRoleArn(
      "arn:aws:iam::123456789012:role/coa-dev-datasource-access-",
    );
    expect(error).toBe(
      "Role ARN must match pattern: arn:aws:iam::ACCOUNT:role/{prefix}-datasource-access-*",
    );
  });

  it("rejects invalid account number", () => {
    const error = validateCrossAccountRoleArn(
      "arn:aws:iam::12345:role/coa-dev-datasource-access-acme",
    );
    expect(error).toBe(
      "Role ARN must match pattern: arn:aws:iam::ACCOUNT:role/{prefix}-datasource-access-*",
    );
  });
});

describe("ConnectSource — cross-account IAM role", () => {
  beforeEach(() => {
    mockCreate.mockReset();
    mockCreateAsync.mockReset();
  });

  // Types out several long fields (including a full IAM role ARN)
  // character-by-character via user-event across a 3-step wizard, which is
  // consistently slower than the 5000ms vitest default under CI load —
  // bump the timeout for this test rather than the global default.
  it("submits the Glue cross-account role ARN and never sends an external ID", async () => {
    render(<ConnectSource />, { wrapper });

    // Step 1 — Glue is the default tile.
    await clickNext();
    // Step 2 — required fields + cross-account fields.
    await user.type(screen.getByPlaceholderText("My Glue Database"), "my-db");
    await user.type(
      screen.getByPlaceholderText("123456789012"),
      "123456789012",
    );
    await user.type(screen.getByPlaceholderText("my_glue_db"), "my_db");
    await user.type(
      screen.getByPlaceholderText(
        "arn:aws:iam::222222222222:role/coa-dev-datasource-access-acme",
      ),
      "arn:aws:iam::222222222222:role/coa-dev-datasource-access-acme",
    );
    await clickNext(); // → enrichment step
    await clickNext(); // → review step
    await user.click(screen.getByRole("button", { name: /^connect source$/i }));

    expect(mockCreate).toHaveBeenCalledTimes(1);
    const cfg =
      mockCreate.mock.calls[0][0].body.databaseSource.glueConfiguration;
    expect(cfg.crossAccountRoleArn).toBe(
      "arn:aws:iam::222222222222:role/coa-dev-datasource-access-acme",
    );
    // A caller-supplied ExternalId would defeat the namespace binding, so the
    // form has no such input and the payload must never carry one.
    expect(cfg.externalId).toBeUndefined();
  }, 15000);

  it("shows the namespace-derived External ID for the role's trust policy", async () => {
    render(<ConnectSource />, { wrapper });

    await clickNext();

    expect(screen.getByText(DERIVED_EXTERNAL_ID)).toBeInTheDocument();
    // It is displayed, never entered — an input would hand the caller back
    // control of the value that binds the assume to this namespace.
    expect(
      screen.queryByPlaceholderText("optional-external-id"),
    ).not.toBeInTheDocument();
  }, 15000);

  it("blocks navigation to next step when Glue cross-account role ARN is invalid", async () => {
    render(<ConnectSource />, { wrapper });

    await clickNext();
    await user.type(screen.getByPlaceholderText("My Glue Database"), "my-db");
    await user.type(
      screen.getByPlaceholderText("123456789012"),
      "123456789012",
    );
    await user.type(screen.getByPlaceholderText("my_glue_db"), "my_db");
    // Type an invalid role ARN (missing datasource-access infix)
    await user.type(
      screen.getByPlaceholderText(
        "arn:aws:iam::222222222222:role/coa-dev-datasource-access-acme",
      ),
      "arn:aws:iam::222222222222:role/coa-dev-my-role",
    );
    await clickNext();

    // Should show error and block navigation
    expect(
      screen.getByText(/Role ARN must match pattern/i),
    ).toBeInTheDocument();
    // Still on step 2 — enrichment toggle not visible (only appears on enrichment step)
    expect(
      screen.queryByText(/Enable AI metadata enrichment/i),
    ).not.toBeInTheDocument();
  });

  // Types out several long fields (engine selection, host, database, a
  // Secrets Manager ARN, and a full IAM role ARN) character-by-character via
  // user-event across the wizard, which is consistently slower than the 5000ms
  // vitest default under CI load — bump the timeout for this test rather than
  // the global default.
  it("blocks navigation to next step when JDBC cross-account role ARN is invalid", async () => {
    render(<ConnectSource />, { wrapper });

    await user.click(screen.getByLabelText("JDBC database"));
    await clickNext();
    await user.type(screen.getByPlaceholderText("My JDBC Database"), "test-db");
    await user.click(screen.getByText("Select an engine"));
    await user.click(screen.getAllByText("PostgreSQL")[0]);
    await user.type(screen.getByPlaceholderText("db.example.com"), "localhost");
    await user.type(screen.getByPlaceholderText("my_database"), "testdb");
    await user.type(
      screen.getByPlaceholderText("arn:aws:secretsmanager:..."),
      "arn:aws:secretsmanager:us-east-1:123456789012:secret:db",
    );

    // Expand advanced config
    await user.click(screen.getByText("Advanced configuration"));

    // Type an invalid role ARN (no prefix before datasource-access)
    await user.type(
      screen.getByPlaceholderText(
        "arn:aws:iam::222222222222:role/coa-dev-datasource-access-acme",
      ),
      "arn:aws:iam::222222222222:role/datasource-access-acme",
    );
    await clickNext();

    // Should show error and block navigation
    expect(
      screen.getByText(/Role ARN must match pattern/i),
    ).toBeInTheDocument();
    // Still on step 2 — enrichment toggle not visible (only appears on enrichment step)
    expect(
      screen.queryByText(/Enable AI metadata enrichment/i),
    ).not.toBeInTheDocument();
  }, 15000);

  it("omits cross-account fields from the Glue payload when left blank", async () => {
    render(<ConnectSource />, { wrapper });

    await clickNext();
    await user.type(screen.getByPlaceholderText("My Glue Database"), "my-db");
    await user.type(
      screen.getByPlaceholderText("123456789012"),
      "123456789012",
    );
    await user.type(screen.getByPlaceholderText("my_glue_db"), "my_db");
    await clickNext();
    await clickNext();
    await user.click(screen.getByRole("button", { name: /^connect source$/i }));

    const cfg =
      mockCreate.mock.calls[0][0].body.databaseSource.glueConfiguration;
    expect(cfg.crossAccountRoleArn).toBeUndefined();
    expect(cfg.externalId).toBeUndefined();
  });
});

describe("ConnectSource — Glue execution engine", () => {
  beforeEach(() => {
    mockCreate.mockReset();
    mockCreateAsync.mockReset();
  });

  it("defaults to Athena — omits executionEngine/redshiftWorkgroup from the payload", async () => {
    render(<ConnectSource />, { wrapper });

    await clickNext();
    await user.type(screen.getByPlaceholderText("My Glue Database"), "my-db");
    await user.type(
      screen.getByPlaceholderText("123456789012"),
      "123456789012",
    );
    await user.type(screen.getByPlaceholderText("my_glue_db"), "my_db");
    await clickNext();
    await clickNext();
    await user.click(screen.getByRole("button", { name: /^connect source$/i }));

    const cfg =
      mockCreate.mock.calls[0][0].body.databaseSource.glueConfiguration;
    expect(cfg.executionEngine).toBeUndefined();
    expect(cfg.redshiftWorkgroup).toBeUndefined();
  });

  it("does not show the Redshift workgroup field until Redshift is selected", async () => {
    render(<ConnectSource />, { wrapper });
    await clickNext();
    // On the Glue config step, the workgroup input is hidden by default (Athena).
    expect(
      screen.queryByPlaceholderText("my-workgroup"),
    ).not.toBeInTheDocument();
  });

  it("submits executionEngine=REDSHIFT and the workgroup when Redshift is chosen", async () => {
    render(<ConnectSource />, { wrapper });

    await clickNext();
    await user.type(screen.getByPlaceholderText("My Glue Database"), "my-db");
    await user.type(
      screen.getByPlaceholderText("123456789012"),
      "123456789012",
    );
    await user.type(screen.getByPlaceholderText("my_glue_db"), "my_db");

    // Open the Execution engine Select and pick Redshift Serverless.
    await user.click(screen.getByText("Athena (default)"));
    await user.click(screen.getByText("Redshift Serverless"));

    // The workgroup field now appears; fill it.
    await user.type(
      screen.getByPlaceholderText("my-workgroup"),
      "my-workgroup",
    );

    await clickNext();
    await clickNext();
    await user.click(screen.getByRole("button", { name: /^connect source$/i }));

    expect(mockCreate).toHaveBeenCalledTimes(1);
    const cfg =
      mockCreate.mock.calls[0][0].body.databaseSource.glueConfiguration;
    expect(cfg.executionEngine).toBe("REDSHIFT");
    expect(cfg.redshiftWorkgroup).toBe("my-workgroup");
  }, 15000);

  it("blocks Next when Redshift is selected but no workgroup is entered", async () => {
    render(<ConnectSource />, { wrapper });

    await clickNext();
    await user.type(screen.getByPlaceholderText("My Glue Database"), "my-db");
    await user.type(
      screen.getByPlaceholderText("123456789012"),
      "123456789012",
    );
    await user.type(screen.getByPlaceholderText("my_glue_db"), "my_db");
    await user.click(screen.getByText("Athena (default)"));
    await user.click(screen.getByText("Redshift Serverless"));

    // Attempt to advance without a workgroup — validation must surface an error
    // and NOT submit.
    await clickNext();
    expect(
      screen.getByText(/Redshift workgroup is required/i),
    ).toBeInTheDocument();
    expect(mockCreate).not.toHaveBeenCalled();
  }, 15000);

  it("hides the Athena DataCatalog field when Redshift is selected (Athena-only concept)", async () => {
    render(<ConnectSource />, { wrapper });
    await clickNext();

    // Under the default Athena engine the Athena DataCatalog field is present.
    expect(
      screen.getByPlaceholderText("my_federated_catalog"),
    ).toBeInTheDocument();

    // Switch to Redshift — the Athena DataCatalog field disappears (it does not
    // apply to the awsdatacatalog auto-mount path), and the workgroup appears.
    await user.click(screen.getByText("Athena (default)"));
    await user.click(screen.getByText("Redshift Serverless"));

    expect(
      screen.queryByPlaceholderText("my_federated_catalog"),
    ).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText("my-workgroup")).toBeInTheDocument();
  }, 15000);

  it("omits athenaDataCatalogName from the payload on the Redshift path", async () => {
    render(<ConnectSource />, { wrapper });

    await clickNext();
    await user.type(screen.getByPlaceholderText("My Glue Database"), "my-db");
    await user.type(
      screen.getByPlaceholderText("123456789012"),
      "123456789012",
    );
    await user.type(screen.getByPlaceholderText("my_glue_db"), "my_db");
    await user.click(screen.getByText("Athena (default)"));
    await user.click(screen.getByText("Redshift Serverless"));
    await user.type(
      screen.getByPlaceholderText("my-workgroup"),
      "my-workgroup",
    );

    await clickNext();
    await clickNext();
    await user.click(screen.getByRole("button", { name: /^connect source$/i }));

    const cfg =
      mockCreate.mock.calls[0][0].body.databaseSource.glueConfiguration;
    expect(cfg.executionEngine).toBe("REDSHIFT");
    expect(cfg.athenaDataCatalogName).toBeUndefined();
  }, 15000);
});

describe("ConnectSource — Athena Query Federation connector", () => {
  beforeEach(() => {
    mockCreate.mockReset();
    mockCreateAsync.mockReset();
  });

  it("offers the connector as a step-1 source kind", () => {
    render(<ConnectSource />, { wrapper });

    expect(screen.getByText(/Athena Query Federation SDK/)).toBeInTheDocument();
  });

  it("treats the connector as a database source — the enrichment step is present", async () => {
    render(<ConnectSource />, { wrapper });

    await fillCustomConnectorStep();
    await clickNext();

    expect(
      screen.getByText(/Enable AI metadata enrichment/i),
    ).toBeInTheDocument();
  }, 15000);

  it("submits customConnectorConfiguration and nothing else", async () => {
    render(<ConnectSource />, { wrapper });

    await fillCustomConnectorStep();
    await clickNext(); // → enrichment
    await clickNext(); // → review
    await user.click(screen.getByRole("button", { name: /^connect source$/i }));

    expect(mockCreate).toHaveBeenCalledTimes(1);
    const databaseSource = mockCreate.mock.calls[0][0].body.databaseSource;
    expect(databaseSource.name).toBe("my-connector");
    expect(databaseSource.customConnectorConfiguration).toEqual({
      connectorFunctionArn: CONNECTOR_ARN,
      databaseName: "my_db",
      tableFilter: undefined,
      tableExcludeFilter: undefined,
    });
    // The backend 400s when more than one configuration is present — the
    // configuration sent is what selects the sub-type.
    expect(databaseSource.glueConfiguration).toBeUndefined();
    expect(databaseSource.jdbcConfiguration).toBeUndefined();
  }, 15000);

  it("asks for exactly one connector ARN, with no metadata/record split", async () => {
    render(<ConnectSource />, { wrapper });

    await fillCustomConnectorStep();

    // Pins the single-field contract rather than merely not exercising a second
    // field. Athena itself accepts a split metadata/record pair, so this is what
    // stops one being reintroduced and asking every customer to choose between two
    // adjacent ARNs — where the wrong choice fails at query time, not here.
    expect(screen.queryByText(/record function arn/i)).not.toBeInTheDocument();
    expect(
      screen.queryByText(/metadata function arn/i),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/connector function arn/i)).toBeInTheDocument();
  }, 15000);

  it("submits the table filters when provided", async () => {
    render(<ConnectSource />, { wrapper });

    await fillCustomConnectorStep();
    await user.click(screen.getByText("Advanced configuration"));
    await user.type(
      screen.getByPlaceholderText("orders|customers"),
      "orders|customers",
    );
    await user.type(
      screen.getByPlaceholderText("tmp_*|staging_*"),
      "tmp_*|scratch_*",
    );
    await clickNext();
    await clickNext();
    await user.click(screen.getByRole("button", { name: /^connect source$/i }));

    const cfg =
      mockCreate.mock.calls[0][0].body.databaseSource
        .customConnectorConfiguration;
    expect(cfg.tableFilter).toBe("orders|customers");
    expect(cfg.tableExcludeFilter).toBe("tmp_*|scratch_*");
  }, 20000);

  it("blocks Next when the metadata function ARN is missing", async () => {
    render(<ConnectSource />, { wrapper });

    await user.click(screen.getByLabelText("Custom connector"));
    await clickNext();
    await user.type(
      screen.getByPlaceholderText("My Custom Connector"),
      "my-connector",
    );
    await user.type(screen.getByPlaceholderText("my_connector_db"), "my_db");
    await clickNext();

    expect(
      screen.getByText(/Connector function ARN is required/i),
    ).toBeInTheDocument();
    expect(mockCreate).not.toHaveBeenCalled();
  }, 15000);

  it("rejects a partial Lambda ARN — an unqualified name resolves to the wrong account", async () => {
    render(<ConnectSource />, { wrapper });

    await user.click(screen.getByLabelText("Custom connector"));
    await clickNext();
    await user.type(
      screen.getByPlaceholderText("My Custom Connector"),
      "my-connector",
    );
    await user.type(
      screen.getByPlaceholderText(CONNECTOR_ARN),
      "123456789012:function:my-connector",
    );
    await user.type(screen.getByPlaceholderText("my_connector_db"), "my_db");
    await clickNext();

    expect(
      screen.getByText(/Must be a full Lambda function ARN/i),
    ).toBeInTheDocument();
    expect(mockCreate).not.toHaveBeenCalled();
  }, 20000);

  it("blocks Next when the database name is missing", async () => {
    render(<ConnectSource />, { wrapper });

    await user.click(screen.getByLabelText("Custom connector"));
    await clickNext();
    await user.type(
      screen.getByPlaceholderText("My Custom Connector"),
      "my-connector",
    );
    await user.type(screen.getByPlaceholderText(CONNECTOR_ARN), CONNECTOR_ARN);
    await clickNext();

    expect(screen.getByText(/Database name is required/i)).toBeInTheDocument();
    expect(mockCreate).not.toHaveBeenCalled();
  }, 20000);

  it("summarises the connector on the review step", async () => {
    render(<ConnectSource />, { wrapper });

    await fillCustomConnectorStep();
    await clickNext();
    await clickNext();

    expect(screen.getByText("my-connector")).toBeInTheDocument();
    expect(screen.getByText(CONNECTOR_ARN)).toBeInTheDocument();
    // The review step mirrors the form, and the form has no record-handler field —
    // so it must not report a composite/split distinction the customer was never
    // asked about.
    expect(screen.queryByText(/composite handler/i)).not.toBeInTheDocument();
    expect(screen.queryByText("Record function ARN")).not.toBeInTheDocument();
  }, 15000);
});
