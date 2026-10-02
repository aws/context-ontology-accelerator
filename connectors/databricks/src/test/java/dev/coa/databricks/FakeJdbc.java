// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks;

import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Function;

/**
 * A JDBC layer made of dynamic proxies: records every statement and its bound parameters, and answers
 * {@code executeQuery} from rows a test supplies. Not thread-safe, and does not need to be.
 *
 * <p>Proxies rather than a mocking framework. {@link ResultSet} declares about two hundred methods, of
 * which the code under test calls four, so a hand-written stub would be two hundred methods of noise and
 * a mocking framework would be a new dependency in a module set that has only JUnit. A proxy is thirty
 * lines and gives the better failure message: a method the production code calls that this class does not
 * implement fails naming it, which is what a test wants when the code under test starts using a new part
 * of JDBC.
 *
 * <p>A row is a {@link LinkedHashMap} from column label to value, so {@code getString(int)} and
 * {@code getString(String)} both work and column order is the map's insertion order.
 */
public final class FakeJdbc
{
    /** One statement the code under test prepared, with the parameters it bound. */
    public static final class Statement
    {
        private final String sql;
        private final List<Object> parameters = new ArrayList<>();

        private Statement(String sql)
        {
            this.sql = sql;
        }

        /** The SQL text, exactly as prepared. */
        public String sql()
        {
            return sql;
        }

        /** The bound parameters, in position order. */
        public List<Object> parameters()
        {
            return Collections.unmodifiableList(parameters);
        }

        @Override
        public String toString()
        {
            return sql + " <- " + parameters;
        }
    }

    private final Function<String, List<Map<String, Object>>> resultsFor;
    private final List<Statement> statements = new ArrayList<>();
    private int connectionsOpened;
    private int connectionsClosed;

    /**
     * @param resultsFor given a statement's SQL, the rows {@code executeQuery} should return. An empty
     *                   list for a statement whose result the test does not care about.
     */
    public FakeJdbc(Function<String, List<Map<String, Object>>> resultsFor)
    {
        this.resultsFor = resultsFor;
    }

    /** A fresh connection proxy. Counts as one open. */
    public Connection connection()
    {
        connectionsOpened++;
        return proxy(Connection.class, (proxy, method, args) -> {
            switch (method.getName()) {
                case "prepareStatement":
                    return preparedStatement((String) args[0]);
                case "getMetaData":
                    return databaseMetaData();
                case "close":
                    connectionsClosed++;
                    return null;
                case "isClosed":
                    return false;
                case "setAutoCommit":
                case "commit":
                case "rollback":
                    return null;
                case "getAutoCommit":
                    return true;
                default:
                    return unsupported(method);
            }
        });
    }

    /** Every statement prepared so far, in order. */
    public List<Statement> statements()
    {
        return Collections.unmodifiableList(statements);
    }

    /** The statements whose SQL contains {@code fragment}. */
    public List<Statement> statementsContaining(String fragment)
    {
        List<Statement> matches = new ArrayList<>();
        for (Statement statement : statements) {
            if (statement.sql().contains(fragment)) {
                matches.add(statement);
            }
        }
        return matches;
    }

    /** How many connections were opened. */
    public int connectionsOpened()
    {
        return connectionsOpened;
    }

    /** How many were closed. Equal to {@link #connectionsOpened()} in healthy code. */
    public int connectionsClosed()
    {
        return connectionsClosed;
    }

    /**
     * Just enough {@link java.sql.DatabaseMetaData} for {@code athena-jdbc}'s inherited read loop, which
     * asks for {@code getDatabaseProductName()} before it does anything else — it compares the answer to
     * {@code "clickhouse"} to decide whether to disable auto-commit.
     *
     * <p>The product name is the driver's own, not a Databricks constant, so it is spelled here as the
     * driver spells it. Anything but {@code "clickhouse"} takes the same branch, so nothing depends on
     * the exact value; what depends on this existing at all is that a read can be driven through the real
     * loop rather than stopping at the first metadata call.
     */
    private java.sql.DatabaseMetaData databaseMetaData()
    {
        return proxy(java.sql.DatabaseMetaData.class, (proxy, method, args) -> {
            switch (method.getName()) {
                case "getDatabaseProductName":
                    return "SparkSQL";
                case "getDatabaseProductVersion":
                    return "3.5.0";
                default:
                    return unsupported(method);
            }
        });
    }

    private PreparedStatement preparedStatement(String sql)
    {
        Statement recorded = new Statement(sql);
        statements.add(recorded);
        return proxy(PreparedStatement.class, (proxy, method, args) -> {
            switch (method.getName()) {
                case "setString":
                case "setLong":
                case "setInt":
                case "setShort":
                case "setByte":
                case "setDouble":
                case "setFloat":
                case "setBoolean":
                case "setDate":
                case "setTimestamp":
                case "setBytes":
                case "setBigDecimal":
                case "setObject": {
                    int position = ((Number) args[0]).intValue();
                    while (recorded.parameters.size() < position) {
                        recorded.parameters.add(null);
                    }
                    recorded.parameters.set(position - 1, args[1]);
                    return null;
                }
                case "executeQuery":
                    return resultSet(resultsFor.apply(sql));
                case "setQueryTimeout":
                case "setFetchSize":
                case "setMaxRows":
                case "setLargeMaxRows":
                case "close":
                    return null;
                default:
                    return unsupported(method);
            }
        });
    }

    private ResultSet resultSet(List<Map<String, Object>> rows)
    {
        List<Map<String, Object>> safeRows = (rows == null) ? Collections.emptyList() : rows;
        // A one-element array so the lambda can mutate it; the cursor starts before the first row.
        final int[] cursor = {-1};
        final boolean[] lastWasNull = {false};
        return proxy(ResultSet.class, (proxy, method, args) -> {
            switch (method.getName()) {
                case "next":
                    cursor[0]++;
                    return cursor[0] < safeRows.size();
                case "getString":
                case "getObject": {
                    Map<String, Object> row = safeRows.get(cursor[0]);
                    Object value = (args[0] instanceof Number)
                            ? byPosition(row, ((Number) args[0]).intValue())
                            : byLabel(row, String.valueOf(args[0]));
                    lastWasNull[0] = (value == null);
                    return (value == null) ? null : String.valueOf(value);
                }
                case "wasNull":
                    return lastWasNull[0];
                case "close":
                    return null;
                default:
                    return unsupported(method);
            }
        });
    }

    /**
     * The value at {@code label}.
     *
     * @throws SQLException if the row has no such column, which is what a real driver does and what makes
     *         a column label assertable. Returning null instead lets the unit tests pin the labels against
     *         themselves: a fixture keyed {@code table_name} and production code asking for
     *         {@code tableName} both "work", leaving the real labels ({@code tableName},
     *         {@code full_data_type}, {@code position_in_unique_constraint}) load-bearing only in the
     *         integration suite, which skips without a warehouse.
     */
    private static Object byLabel(Map<String, Object> row, String label) throws SQLException
    {
        if (!row.containsKey(label)) {
            throw new SQLException("No column labelled \"" + label + "\" in this result set."
                    + " The row has " + row.keySet() + "."
                    + " Either the code under test asked for the wrong label, or the fixture spells it"
                    + " differently from the view it stands in for.");
        }
        return row.get(label);
    }

    private static Object byPosition(Map<String, Object> row, int oneBasedPosition)
    {
        int index = 0;
        for (Object value : row.values()) {
            if (++index == oneBasedPosition) {
                return value;
            }
        }
        throw new IllegalArgumentException(
                "No column at position " + oneBasedPosition + " in row " + row);
    }

    @SuppressWarnings("unchecked")
    private static <T> T proxy(Class<T> type, InvocationHandler handler)
    {
        return (T) Proxy.newProxyInstance(
                FakeJdbc.class.getClassLoader(),
                new Class<?>[] {type},
                (proxyInstance, method, args) -> {
                    // Object's own methods must not reach the handler: toString() is called by assertion
                    // messages and by try-with-resources diagnostics.
                    switch (method.getName()) {
                        case "toString":
                            return "fake " + type.getSimpleName();
                        case "hashCode":
                            return System.identityHashCode(proxyInstance);
                        case "equals":
                            return proxyInstance == args[0];
                        case "unwrap":
                            return proxyInstance;
                        case "isWrapperFor":
                            return false;
                        default:
                            return handler.invoke(proxyInstance, method, args);
                    }
                });
    }

    private static Object unsupported(Method method) throws SQLException
    {
        throw new SQLException(
                "FakeJdbc does not implement " + method.getDeclaringClass().getSimpleName() + "."
                        + method.getName() + ". Add it if the code under test now needs it.");
    }

    /** One row, from alternating column label and value. */
    public static Map<String, Object> row(Object... entries)
    {
        if (entries.length % 2 != 0) {
            throw new IllegalArgumentException("row() takes label/value pairs");
        }
        Map<String, Object> row = new LinkedHashMap<>();
        for (int i = 0; i < entries.length; i += 2) {
            row.put(String.valueOf(entries[i]), entries[i + 1]);
        }
        return row;
    }
}
