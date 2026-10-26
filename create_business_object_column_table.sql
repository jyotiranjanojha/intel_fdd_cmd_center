-- DDL for BUSINESS_OBJECT_COLUMN, the BA-declared column specification for a target object
-- Co-authored with CoCo

CREATE TABLE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.BUSINESS_OBJECT_COLUMN (
    COLUMN_SPEC_ID       INT           NOT NULL,
    OBJECT_ID            INT           NOT NULL,   -- -> BUSINESS_OBJECT.OBJECT_ID
    ORDINAL              INT           DEFAULT 1,  -- order the column reads in the FDD
    COLUMN_NAME          VARCHAR(255)  NOT NULL,
    DATA_TYPE            VARCHAR(100),             -- free text, e.g. VARCHAR(18), NUMBER(10,2)
    NULLABLE_IND         BOOLEAN       DEFAULT TRUE,
    PRIMARY_KEY_IND      BOOLEAN       DEFAULT FALSE,
    DEFAULT_VALUE        VARCHAR(500),
    BUSINESS_DESCRIPTION VARCHAR(4000),
    COLUMN_ORIGIN        VARCHAR(20)   DEFAULT 'DERIVED',  -- PASS_THROUGH | DERIVED
    SOURCE_OBJECT        VARCHAR(500),             -- pass-through only
    SOURCE_COLUMN        VARCHAR(255),             -- pass-through only
    ACTIVE_IND           BOOLEAN       DEFAULT TRUE,
    CREATED_BY           VARCHAR(256)  DEFAULT CURRENT_USER(),
    CREATED_TS           TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP(),
    UPDATED_BY           VARCHAR(256),
    UPDATED_TS           TIMESTAMP_LTZ
);

COMMENT ON TABLE IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.BUSINESS_OBJECT_COLUMN IS
    'Columns a target object is required to have, declared by the BA as part of the FDD. COLUMN_ORIGIN records whether a column is a straight pull from a source (PASS_THROUGH) or needs transformation logic (DERIVED).';
