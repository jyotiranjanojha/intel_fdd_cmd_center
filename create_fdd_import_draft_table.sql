-- DDL for saved Import FDD reviews, so a large FDD can be reviewed over several sittings.
-- Co-authored with CoCo

USE ROLE ROLE_IF_CDA_PDH_SUPPLY_PLANNING_ANALYSIS_CONTRIBUTOR;

-- One row per review in progress. VERSION_NO goes up on every save; a save made from
-- an older version is refused (two tabs, or two people, on the same draft).
-- STATUS: OPEN, IMPORTED or DISCARDED - drafts are never deleted.
CREATE TABLE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.FDD_IMPORT_DRAFT (
    DRAFT_ID     NUMBER        NOT NULL,
    MODULE_NAME  VARCHAR(500),
    MODULE_ID    NUMBER,
    TEAM_ID      NUMBER,
    TITLE        VARCHAR(500),
    RULE_COUNT   NUMBER,
    OPEN_ISSUES  NUMBER,
    STATUS       VARCHAR(20)   DEFAULT 'OPEN',
    VERSION_NO   NUMBER        DEFAULT 1,
    IMPORTED_RULE_IDS VARCHAR(4000),
    CREATED_BY   VARCHAR(128)  DEFAULT CURRENT_USER(),
    CREATED_TS   TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    UPDATED_BY   VARCHAR(128),
    UPDATED_TS   TIMESTAMP_NTZ
);

-- The review state itself: zlib-compressed JSON, base64, split into parts so a very
-- large FDD never meets a statement-size limit. Only the current version is kept.
CREATE TABLE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.FDD_IMPORT_DRAFT_PART (
    DRAFT_ID    NUMBER        NOT NULL,
    VERSION_NO  NUMBER        NOT NULL,
    PART_NO     NUMBER        NOT NULL,
    DATA        VARCHAR       NOT NULL
);

CREATE SEQUENCE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.SEQ_FDD_IMPORT_DRAFT
    START = 1 INCREMENT = 1;
