-- DDL for manual / FDD / rule-drafted sample data of objects not (yet) in Snowflake.
-- Undo: drop_object_sample_data_table.sql (and REVERT_SAMPLE_DATA.md).
-- Co-authored with CoCo

USE ROLE ROLE_IF_CDA_PDH_SUPPLY_PLANNING_ANALYSIS_CONTRIBUTOR;

-- One row per saved version of an object's sample. Only the latest ACTIVE_IND row is
-- shown; older versions are kept so History can bring one back.
--   COLUMNS      ordered list of column names
--   ROWS_JSON    array of {column: value}
--   CELL_ORIGIN  {row index: {column: "source" | "rule 3" | "model (rule 5)" |
--                "unknown" | "manual"}} - where each value came from
--   SOURCE_ROWS  for SOURCE = GENERATED, the source rows the draft was made from
--   SOURCE       MANUAL, FDD_IMPORT or GENERATED
CREATE TABLE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.OBJECT_SAMPLE_DATA (
    SAMPLE_ID              NUMBER        NOT NULL,
    OBJECT_ID              NUMBER,
    TECHNICAL_OBJECT_NAME  VARCHAR(1000) NOT NULL,
    COLUMNS                VARIANT,
    ROWS_JSON              VARIANT,
    CELL_ORIGIN            VARIANT,
    SOURCE_ROWS            VARIANT,
    SOURCE                 VARCHAR(20)   DEFAULT 'MANUAL',
    NOTE                   VARCHAR(1000),
    ACTIVE_IND             BOOLEAN       DEFAULT TRUE,
    CREATED_BY             VARCHAR(128)  DEFAULT CURRENT_USER(),
    CREATED_TS             TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

CREATE SEQUENCE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.SEQ_OBJECT_SAMPLE_DATA
    START = 1 INCREMENT = 1;
