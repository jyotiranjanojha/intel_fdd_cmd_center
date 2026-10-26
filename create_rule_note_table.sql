-- DDL for notes attached to a rule: the FDD's Note / Open / Resolved remarks, the
-- "Note:" sentences inside a rule, and anything a reviewer wants the next reader to know.
-- Co-authored with CoCo

USE ROLE ROLE_IF_CDA_PDH_SUPPLY_PLANNING_ANALYSIS_CONTRIBUTOR;

-- One row per note. Keyed on RULE_ID so the note follows the rule through edits,
-- renames and moves between rule sets; review comments are per module and are the
-- to-do list, notes are the rule's memory.
--
-- NOTE_TYPE:   NOTE (background), OPEN (a question not yet answered),
--              RESOLVED (a question that was answered), INFO (written by the app,
--              e.g. a fix applied automatically at import).
-- SOURCE:      FDD_IMPORT, WRITE_RULE, UPDATE_RULE or MANUAL.
-- ACTIVE_IND:  notes are hidden, never deleted, so the history stays explainable.
CREATE TABLE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.RULE_NOTE (
    NOTE_ID     NUMBER        NOT NULL,
    RULE_ID     NUMBER        NOT NULL,
    NOTE_TYPE   VARCHAR(20)   DEFAULT 'NOTE',
    NOTE_TEXT   VARCHAR(4000) NOT NULL,
    SOURCE      VARCHAR(30)   DEFAULT 'MANUAL',
    ACTIVE_IND  BOOLEAN       DEFAULT TRUE,
    CREATED_BY  VARCHAR(128)  DEFAULT CURRENT_USER(),
    CREATED_TS  TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

CREATE SEQUENCE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.SEQ_RULE_NOTE
    START = 1 INCREMENT = 1;
