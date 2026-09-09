-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: Apache-2.0
--
-- Databricks fixtures for verifying this connector against a real Unity Catalog schema.
--
-- NOT RUN BY CI, AND NO COMMITTED TEST DEPENDS ON IT. There is no Databricks workspace available to
-- this project — the one used during design was a 14-day trial — so the connector ships with unit
-- tests only, and its behaviour against real Databricks is evidenced by the one-off manual record in
-- the connector LLD (section 8.1, "One-off manual verification record") rather than by a suite.
--
-- This file is kept for two reasons. It is the **provenance** of that record: every observation in it
-- was made against the schema this script creates. And it is the starting point for anyone who
-- acquires a workspace and wants to build the suite section 8.1 describes — which should be done
-- before changing discovery, since nothing automated would catch a regression there today.
--
-- To use it: run it once against a Unity Catalog schema, point DATABRICKS_* at that schema, and write
-- the tests. The fixtures are deliberately chosen to catch specific bugs rather than to be
-- representative — a composite foreign key whose child and parent columns are named differently and
-- ordered so that alphabetical sorting gives the wrong pairing, a table whose name contains its
-- schema name, a table needing backtick quoting, a column comment containing a malformed tag and an
-- email address that must not be read as one, a shallow clone that must be excluded, and a foreign key
-- pointing out of the exposed schema.
--
-- The ROW VALUES below are illustrative. Any test written against them should calibrate against whatever the
-- target schema actually contains rather than asserting these literals — an earlier version asserted
-- them and failed against a schema loaded with different rows, which said nothing about the connector.
-- Column names, comments, types and constraints ARE asserted, so those must match.
--
-- Requires: USE CATALOG and CREATE SCHEMA on the catalog; USE SCHEMA, CREATE TABLE and MODIFY on
-- the schemas it creates.
-- Idempotent: every statement is CREATE OR REPLACE or DROP IF EXISTS first.
--
-- Run it AS A WHOLE, in order. The drops below remove `orders`, which the materialized view and the
-- streaming table further down depend on; the script recreates all three, but stopping halfway leaves
-- those two broken.
--
-- Substitute your own catalog and schema for `workspace`.`coa_dbx_test` throughout, or run
--   USE CATALOG workspace; USE SCHEMA coa_dbx_test;
-- first and drop the qualification.

-- ---------------------------------------------------------------------------------------------
-- The exposed schema itself. First, because every statement below is qualified with it and the
-- first of them is a DROP: without this, pasting the file into a fresh workspace fails on line one
-- with "schema not found" rather than on anything to do with the fixtures.
--
-- `coa_dbx_test_other` is created further down, next to the one table that lives in it.
-- ---------------------------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS `workspace`.`coa_dbx_test`;

-- ---------------------------------------------------------------------------------------------
-- orders - the parent. Composite primary key, column comments, one mixed-case column name.
--
-- The mixed-case column is load-bearing: information_schema lower-cases a TABLE name but PRESERVES
-- a COLUMN name, so a connector that lower-cases both generates SQL naming a column that does not
-- exist.
-- ---------------------------------------------------------------------------------------------
DROP TABLE IF EXISTS `workspace`.`coa_dbx_test`.`order_lines`;
DROP TABLE IF EXISTS `workspace`.`coa_dbx_test`.`orders`;

CREATE TABLE `workspace`.`coa_dbx_test`.`orders` (
  region_code   STRING        NOT NULL COMMENT 'Region code. Parent key part 1.',
  order_num     BIGINT        NOT NULL COMMENT 'Order number within region. Parent key part 2.',
  order_total   DECIMAL(10,2)          COMMENT 'Order total in account currency',
  CustomerName  STRING                 COMMENT 'Mixed-case column name on purpose'
) USING DELTA;

ALTER TABLE `workspace`.`coa_dbx_test`.`orders`
  ADD CONSTRAINT orders_pk PRIMARY KEY (region_code, order_num);

INSERT INTO `workspace`.`coa_dbx_test`.`orders` VALUES
  ('emea', 1001, 120.50, 'Acme GmbH'),
  ('emea', 1002,  75.00, 'Beta Ltd'),
  ('amer', 2001, 310.25, 'Cyrus Inc'),
  ('amer', 2002,  40.00, 'Delta LLC'),
  ('apac', 3001, 999.99, 'Epsilon Pty');

-- ---------------------------------------------------------------------------------------------
-- order_lines - the composite-foreign-key child.
--
-- The child columns are deliberately named DIFFERENTLY from their parents
--   order_region -> orders.region_code
--   order_id     -> orders.order_num
-- and in an order where sorting either side alphabetically gives the WRONG pairing. That makes two
-- distinct bugs detectable rather than coincidentally correct:
--
--   * the cartesian product from joining key_column_usage to constraint_column_usage on
--     constraint_name, which yields four rows for this two-column key, two of them pairing
--     order_id with region_code and order_region with order_num;
--   * an ordinal mis-sort, which swaps the two pairings.
--
-- sku's comment carries a hand-written @pk and an email address, which together exercise the
-- left-boundary rule (bob@pk.example.com must NOT mint a primary key) and the requirement that a
-- customer-authored tag is stripped before the comment is forwarded.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE `workspace`.`coa_dbx_test`.`order_lines` (
  line_id       BIGINT NOT NULL COMMENT 'Line surrogate key.',
  order_region  STRING NOT NULL COMMENT 'Parent region.',
  order_id      BIGINT NOT NULL COMMENT 'Parent order number.',
  sku           STRING          COMMENT 'Contact bob@pk.example.com about this @pk column',
  quantity      INT             COMMENT 'Units on this line'
) USING DELTA;

ALTER TABLE `workspace`.`coa_dbx_test`.`order_lines`
  ADD CONSTRAINT order_lines_pk PRIMARY KEY (line_id);

ALTER TABLE `workspace`.`coa_dbx_test`.`order_lines`
  ADD CONSTRAINT order_lines_fk FOREIGN KEY (order_region, order_id)
  REFERENCES `workspace`.`coa_dbx_test`.`orders` (region_code, order_num);

INSERT INTO `workspace`.`coa_dbx_test`.`order_lines` VALUES
  (1, 'emea', 1001, 'SKU-0001', 2),
  (2, 'emea', 1001, 'SKU-0002', 1),
  (3, 'emea', 1002, 'SKU-0001', 5),
  (4, 'amer', 2001, 'SKU-0003', 3),
  (5, 'amer', 2002, 'SKU-0002', 1),
  (6, 'apac', 3001, 'SKU-0004', 7);

-- ---------------------------------------------------------------------------------------------
-- One object per table_type the connector exposes beyond MANAGED, so the allowlist is exercised
-- against real Databricks values rather than only against fixtures in a unit test.
--
-- Note what creating the last two ALSO creates: internal side tables named
-- __materialization_mat_<uuid>_<name>_1 and event_log_<uuid>, which information_schema.tables lists
-- as ordinary MANAGED tables with nothing in any of its fifteen columns marking them internal.
-- SHOW TABLES omits them, which is why the connector enumerates with SHOW TABLES.
-- ---------------------------------------------------------------------------------------------
CREATE OR REPLACE VIEW `workspace`.`coa_dbx_test`.`order_totals_vw` AS
  SELECT region_code, SUM(order_total) AS total
    FROM `workspace`.`coa_dbx_test`.`orders`
   GROUP BY region_code;

CREATE OR REPLACE MATERIALIZED VIEW `workspace`.`coa_dbx_test`.`order_counts_mv` AS
  SELECT region_code, COUNT(*) AS orders
    FROM `workspace`.`coa_dbx_test`.`orders`
   GROUP BY region_code;

CREATE OR REPLACE STREAMING TABLE `workspace`.`coa_dbx_test`.`order_stream_st` AS
  SELECT * FROM STREAM `workspace`.`coa_dbx_test`.`orders`;

-- ---------------------------------------------------------------------------------------------
-- coa_dbx_test_audit - a table whose name CONTAINS its schema's name.
--
-- COA's serve layer carries a crawled-name rewrite that strips the schema name from wherever it
-- appears in a table name, so `coa_dbx_test_audit` would become `_audit` - a table its connector has
-- never heard of. Nothing in this connector can prevent that, but the fixture makes the bug visible
-- from the connector's side rather than only at serve time.
-- ---------------------------------------------------------------------------------------------
CREATE OR REPLACE TABLE `workspace`.`coa_dbx_test`.`coa_dbx_test_audit` (
  audit_id  BIGINT COMMENT 'Audit row id',
  note      STRING COMMENT 'What happened'
) USING DELTA;

INSERT INTO `workspace`.`coa_dbx_test`.`coa_dbx_test_audit` VALUES
  (1, 'created'), (2, 'updated');

-- ---------------------------------------------------------------------------------------------
-- mixedcasetable - declared MixedCaseTable, stored lower-cased. Pairs with orders.CustomerName to
-- pin both halves of the case-folding asymmetry.
-- ---------------------------------------------------------------------------------------------
CREATE OR REPLACE TABLE `workspace`.`coa_dbx_test`.`MixedCaseTable` (
  Value STRING COMMENT 'Mixed-case column in a mixed-case table'
) USING DELTA;

INSERT INTO `workspace`.`coa_dbx_test`.`MixedCaseTable` VALUES ('one'), ('two');

-- ---------------------------------------------------------------------------------------------
-- wide_types - one column per Databricks type the connector maps, so the type mapping is exercised
-- against the driver rather than only against strings in a unit test. The last three are the
-- complex types that must arrive as VARCHAR: BlockUtils has no case for a struct, list or map
-- vector and throws "Unknown type Struct" at read time.
-- ---------------------------------------------------------------------------------------------
CREATE OR REPLACE TABLE `workspace`.`coa_dbx_test`.`wide_types` (
  c_boolean    BOOLEAN       COMMENT 'BIT',
  c_tinyint    TINYINT       COMMENT 'TINYINT',
  c_smallint   SMALLINT      COMMENT 'SMALLINT',
  c_int        INT           COMMENT 'INT',
  c_bigint     BIGINT        COMMENT 'BIGINT',
  c_float      FLOAT         COMMENT 'FLOAT4',
  c_double     DOUBLE        COMMENT 'FLOAT8',
  c_decimal    DECIMAL(38,9) COMMENT 'DECIMAL, precision and scale preserved',
  c_string     STRING        COMMENT 'VARCHAR',
  c_binary     BINARY        COMMENT 'VARBINARY',
  c_date       DATE          COMMENT 'DATEDAY, epoch day',
  c_timestamp  TIMESTAMP     COMMENT 'DATEMILLI, epoch millis UTC',
  c_ts_ntz     TIMESTAMP_NTZ COMMENT 'DATEMILLI, read as UTC',
  c_array      ARRAY<STRING> COMMENT 'VARCHAR, flattened',
  c_map        MAP<STRING, INT> COMMENT 'VARCHAR, flattened',
  c_struct     STRUCT<a: INT, b: STRING> COMMENT 'VARCHAR, flattened'
) USING DELTA;

INSERT INTO `workspace`.`coa_dbx_test`.`wide_types` VALUES (
  true, 1, 2, 3, 4, 5.5, 6.5, 7.123456789, 'eight', X'09',
  DATE'2026-01-02', TIMESTAMP'2026-01-02 03:04:05',
  TIMESTAMP_NTZ'2026-01-02 03:04:05',
  ARRAY('a', 'b'), MAP('k', 1), NAMED_STRUCT('a', 1, 'b', 'two')
);

-- ---------------------------------------------------------------------------------------------
-- orders_clone - a MANAGED_SHALLOW_CLONE, i.e. a table_type the connector deliberately excludes.
--
-- Its rows duplicate orders'. Two things must hold: it is absent from listTables, AND describeTable
-- refuses it by name. The second is the one that is easy to miss - Athena calls GetTable for a name
-- the USER typed, not only for names ListTables returned, so filtering only in listTables left
-- `SELECT * FROM cat.coa_dbx_test.orders_clone` describing and reading perfectly.
-- ---------------------------------------------------------------------------------------------
CREATE OR REPLACE TABLE `workspace`.`coa_dbx_test`.`orders_clone`
  SHALLOW CLONE `workspace`.`coa_dbx_test`.`orders`;

-- ---------------------------------------------------------------------------------------------
-- malformed_comments - customer comments that are tag-SHAPED but not valid tags.
--
-- The unterminated `@fk(` is the important one. COA's parser reports it and leaves it in the stored
-- description, so the connector's strip step deliberately preserves it - but the toolkit's ENCODER
-- refuses any `@fk(` in prose, closed or not, and refuses by throwing. Forwarding the stripped text
-- straight to the toolkit therefore made this one comment enough to fail the table's DESCRIBE
-- permanently and take the whole schema's scan with it. See CommentTags.neutralise.
--
-- The rest are near misses that must SURVIVE untouched, because COA stores them and deleting them
-- here would destroy text a customer wrote.
-- ---------------------------------------------------------------------------------------------
CREATE OR REPLACE TABLE `workspace`.`coa_dbx_test`.`malformed_comments` (
  unterminated_fk  STRING COMMENT 'Line total @fk(orders.order_id',
  double_open_fk   STRING COMMENT 'Two of them @fk(@fk(',
  fk_then_pk       STRING COMMENT 'Bracket then a key @fk( @pk',
  pk_near_miss     STRING COMMENT 'Near misses: @PK @pkey @pk=x @pk(x) and bob@pk.example.com',
  bare_fk          STRING COMMENT 'No bracket so no tag: @fk'
) USING DELTA;

INSERT INTO `workspace`.`coa_dbx_test`.`malformed_comments` VALUES ('a', 'b', 'c', 'd', 'e');

-- ---------------------------------------------------------------------------------------------
-- A foreign key pointing OUT of the exposed schema.
--
-- The `@fk(table.column)` tag has no slot for a schema and COA resolves it inside the one schema the
-- connector exposes, so emitting `@fk(external_parent.parent_id)` here would either dangle or - if a
-- table of that name existed in coa_dbx_test - assert a WRONG relationship in the ontology. The
-- connector drops the reference and logs it.
--
-- Needs a second schema, which is the only fixture that reaches outside coa_dbx_test.
-- ---------------------------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS `workspace`.`coa_dbx_test_other`;

CREATE OR REPLACE TABLE `workspace`.`coa_dbx_test_other`.`external_parent` (
  parent_id BIGINT NOT NULL COMMENT 'Parent key, in a schema the connector does not expose'
) USING DELTA;

ALTER TABLE `workspace`.`coa_dbx_test_other`.`external_parent`
  ADD CONSTRAINT external_parent_pk PRIMARY KEY (parent_id);

INSERT INTO `workspace`.`coa_dbx_test_other`.`external_parent` VALUES (1), (2);

DROP TABLE IF EXISTS `workspace`.`coa_dbx_test`.`cross_schema_child`;

-- Two foreign keys on one table, so the drop is provably a drop rather than a bail-out: the outward
-- one must vanish and the in-schema composite one must survive.
--
-- Note the in-schema key has to be COMPOSITE: `orders`' primary key is (region_code, order_num), and a
-- foreign key must reference a primary or unique constraint in full, so there is no single-column
-- reference to `orders` to be had.
CREATE TABLE `workspace`.`coa_dbx_test`.`cross_schema_child` (
  child_id      BIGINT NOT NULL COMMENT 'Child surrogate key.',
  parent_id     BIGINT NOT NULL COMMENT 'References a parent in another schema.',
  order_region  STRING NOT NULL COMMENT 'In-schema parent, part 1.',
  order_num     BIGINT NOT NULL COMMENT 'In-schema parent, part 2.'
) USING DELTA;

ALTER TABLE `workspace`.`coa_dbx_test`.`cross_schema_child`
  ADD CONSTRAINT cross_schema_child_pk PRIMARY KEY (child_id);

-- Must be DROPPED by the connector, with a warning logged.
ALTER TABLE `workspace`.`coa_dbx_test`.`cross_schema_child`
  ADD CONSTRAINT cross_schema_child_out_fk FOREIGN KEY (parent_id)
  REFERENCES `workspace`.`coa_dbx_test_other`.`external_parent` (parent_id);

-- Must SURVIVE, as two @fk tags.
ALTER TABLE `workspace`.`coa_dbx_test`.`cross_schema_child`
  ADD CONSTRAINT cross_schema_child_in_fk FOREIGN KEY (order_region, order_num)
  REFERENCES `workspace`.`coa_dbx_test`.`orders` (region_code, order_num);

INSERT INTO `workspace`.`coa_dbx_test`.`cross_schema_child` VALUES
  (1, 1, 'emea', 1001),
  (2, 2, 'amer', 2001);

-- ---------------------------------------------------------------------------------------------
-- odd-name-table - identifiers that ONLY a backtick can delimit, so the quoting is exercised against
-- the warehouse rather than only against a string in a unit test.
--
-- Measured, because Unity Catalog is stricter than SQL here and the first draft of this fixture was
-- not creatable at all:
--
--   table names   - a space, period or forward slash is REJECTED by Unity Catalog outright
--                   ("name \"odd name\" is not a valid name"). A hyphen and a reserved word are
--                   accepted, and both need quoting to be addressable.
--   column names  - a space is rejected by Delta unless Column Mapping is enabled
--                   ([DELTA_INVALID_CHARACTERS_IN_COLUMN_NAMES]). A double quote and a reserved word
--                   are accepted.
--
-- So the hyphenated table name is the case that proves the point, and Databricks says so itself:
--   SELECT * FROM workspace.coa_dbx_test.odd-name-table
--   -> [INVALID_IDENTIFIER] The unquoted identifier odd-name-table is invalid and must be back
--      quoted as: `odd-name-table`
--
-- ConnectionConfig refuses a catalog or schema shaped like this, so such a name can only ever reach
-- generated SQL as a TABLE or COLUMN name - which is exactly what this covers.
-- ---------------------------------------------------------------------------------------------
CREATE OR REPLACE TABLE `workspace`.`coa_dbx_test`.`odd-name-table` (
  `has"quote`   STRING COMMENT 'Double quote in a column name',
  `select`      STRING COMMENT 'Reserved word as a column name',
  `Mixed_Case`  STRING COMMENT 'Mixed case preserved'
) USING DELTA;

INSERT INTO `workspace`.`coa_dbx_test`.`odd-name-table` VALUES ('x', 'y', 'z');
