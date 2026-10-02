// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks;

import com.amazonaws.athena.connector.lambda.data.Block;
import com.amazonaws.athena.connector.lambda.data.BlockSpiller;
import com.amazonaws.athena.connector.lambda.domain.predicate.ConstraintEvaluator;
import com.amazonaws.athena.connector.lambda.domain.spill.SpillLocation;
import com.amazonaws.athena.connector.lambda.exceptions.AthenaConnectorException;
import dev.coa.databricks.jdbc.DatabricksErrors;
import software.amazon.awssdk.services.glue.model.ErrorDetails;
import software.amazon.awssdk.services.glue.model.FederationSourceErrorCode;

import java.util.List;
import java.util.Objects;

/**
 * A {@link BlockSpiller} that fails with an explicit error once a table has returned too many rows.
 *
 * <p>Athena's federation protocol cannot express aggregation, so a {@code GROUP BY} against this
 * connector reads every predicate-matching row out of the warehouse for Athena to aggregate. That is a
 * permanent property of this route. At 3008 MB there is a point past which the invocation does not
 * return, and a timeout is the worst available diagnosis: it names no table,
 * suggests no action, and Athena retries it, paying for the read again. So the connector stops first
 * and says which table, how many rows, and what to do. The default ceiling is two million rows
 * ({@link Settings#DEFAULT_MAX_ROWS_PER_TABLE}).
 *
 * <p>A spiller rather than a loop or a {@code ResultSet} wrapper. The ceiling has to be enforced inside
 * the read loop, and that loop is the part of {@code athena-jdbc}'s record handler worth inheriting.
 * Wrapping the result set puts a reflective proxy in front of every cell read; wrapping the loop means
 * re-implementing it. This costs one delegation per row on a seven-method interface.
 *
 * <p>There is no byte ceiling. Row width is not knowable before the read, and measuring the encoded
 * size of each Arrow block would mean reaching inside the spiller's own accounting. A very wide table
 * can still exhaust the invocation below the row ceiling; lower
 * {@link Settings#MAX_ROWS_PER_TABLE_VAR} for such a schema.
 */
public final class RowCeilingSpiller implements BlockSpiller
{
    private final BlockSpiller delegate;
    private final String tableName;
    private final long maxRows;

    private long rowsWritten;

    /**
     * @param tableName the table being read, for the error message.
     * @param maxRows   the ceiling. Must be positive.
     */
    public RowCeilingSpiller(BlockSpiller delegate, String tableName, long maxRows)
    {
        this.delegate = Objects.requireNonNull(delegate, "delegate");
        this.tableName = Objects.requireNonNull(tableName, "tableName");
        if (maxRows <= 0) {
            throw new IllegalArgumentException("maxRows must be positive; got " + maxRows);
        }
        this.maxRows = maxRows;
    }

    /** How many rows have been offered so far. For tests and for a post-read log line. */
    public long rowsWritten()
    {
        return rowsWritten;
    }

    /**
     * {@inheritDoc}
     *
     * @throws AthenaConnectorException naming the table and the ceiling.
     */
    @Override
    public void writeRows(RowWriter rowWriter)
    {
        // Counted before the write, so the ceiling bounds what the connector reads rather than what it
        // manages to encode.
        rowsWritten++;
        if (rowsWritten > maxRows) {
            throw new AthenaConnectorException(
                    DatabricksErrors.TABLE_TOO_LARGE_PREFIX + ": table \"" + tableName
                            + "\" returned more than " + maxRows + " rows for this query, which is"
                            + " this connector's ceiling (" + Settings.MAX_ROWS_PER_TABLE_VAR + ")."
                            + " Athena cannot push aggregation into a federation connector, so a"
                            + " GROUP BY reads every matching row out of the warehouse. Narrow the"
                            + " query's predicate, or raise the ceiling and the connector's memory"
                            + " and timeout together.",
                    // NOT a timeout code. A ceiling breach is deterministic, so labelling it transient
                    // invites the retry storm the ceiling exists to prevent: Athena re-invokes a failed
                    // connector, and one oversized GROUP BY would bill N full warehouse reads.
                    // OPERATION_TIMEOUT_EXCEPTION is for the warehouse-starting case, where retrying is
                    // the right advice.
                    ErrorDetails.builder()
                            .errorCode(FederationSourceErrorCode
                                    .OPERATION_NOT_SUPPORTED_EXCEPTION.toString())
                            .build());
        }
        delegate.writeRows(rowWriter);
    }

    /** {@inheritDoc} */
    @Override
    public ConstraintEvaluator getConstraintEvaluator()
    {
        return delegate.getConstraintEvaluator();
    }

    /** {@inheritDoc} */
    @Override
    public boolean spilled()
    {
        return delegate.spilled();
    }

    /** {@inheritDoc} */
    @Override
    public Block getBlock()
    {
        return delegate.getBlock();
    }

    /** {@inheritDoc} */
    @Override
    public List<SpillLocation> getSpillLocations()
    {
        return delegate.getSpillLocations();
    }

    /**
     * {@inheritDoc}
     *
     * <p>Delegated. The SDK owns the spiller's lifecycle and closes the instance it created after
     * {@code readWithConstraint} returns; nothing closes this wrapper, since the inherited read loop
     * never calls {@code close()}. So the delegation is unreachable today, and it is what would keep the
     * wrapper transparent if a future SDK release did close the spiller it hands out.
     */
    @Override
    public void close()
    {
        delegate.close();
    }
}
