-- DDL for the PO review comment table used by the Requirements page review section
-- Co-authored with CoCo

USE ROLE ROLE_IF_CDA_PDH_SUPPLY_PLANNING_ANALYSIS_CONTRIBUTOR;

-- One row per review comment a PO raises against a module.
--
-- Keyed on MODULE_ID rather than a module name: renaming a module (which the
-- Manage modules page allows) would otherwise orphan every comment on it.
--
-- STATUS plus RESOLVED_BY / RESOLVED_TS instead of a bare "done" boolean, so
-- ticking a comment off records who closed it and when - the same audit habit
-- as RULE_CHANGE_HISTORY.CHANGED_BY / CHANGED_TS.
--
-- RAISED_BY / RESOLVED_BY default to CURRENT_USER() only as a backstop for rows
-- written directly by SQL. The app passes the viewer's name explicitly, because
-- it runs execute_as OWNER and CURRENT_USER() would otherwise credit every
-- comment to the Streamlit object's owner.
CREATE TABLE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.RULE_REVIEW_COMMENT (
    COMMENT_ID    NUMBER        NOT NULL,
    MODULE_ID     NUMBER        NOT NULL,
    COMMENT_TEXT  VARCHAR(4000) NOT NULL,
    STATUS        VARCHAR(10)   DEFAULT 'Open',
    RAISED_BY     VARCHAR(128)  DEFAULT CURRENT_USER(),
    RAISED_TS     TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    RESOLVED_BY   VARCHAR(128),
    RESOLVED_TS   TIMESTAMP_NTZ
);
