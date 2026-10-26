-- Surrogate-key sequences for the Rule Repository app.
-- Co-authored with CoCo
--
-- Replaces db.reserve_ids()'s SELECT MAX(id)+1 read-then-write pattern, which has
-- a race window: two sessions can read the same MAX and insert the same id.
-- Snowflake does NOT enforce primary keys (they are declarative metadata only),
-- so a collision produces no error - it silently duplicates the id, after which
-- load_bundles() merges one rule's conditions into another, and retire/rollback
-- by RULE_ID hit both rows. SEQ.NEXTVAL is atomic server-side, so concurrent
-- sessions can never receive the same value.
--
-- START values are seeded one past each table's current MAX so existing id
-- ranges continue rather than restarting at 1 (RULE_ID stays in the 102xxx
-- range). IF NOT EXISTS makes this safe to re-run, but note that re-running
-- will NOT re-seed an existing sequence - if a sequence is ever recreated,
-- re-read MAX first, because a sequence starting below the current MAX would
-- hand out colliding ids.
--
-- Run standalone. DDL implicitly commits any active transaction, so this must
-- not be executed inside one.
--
-- Requires CREATE SEQUENCE on the schema. Verified working with
-- IF_CDA_PDH_SUPPLY_PLANNING_IF_IP_ZSHARE_DEV1_CONTRIBUTOR; the analyst roles
-- and PUBLIC do not have it. Reading a sequence (NEXTVAL) needs no extra grant -
-- verified that even PUBLIC can allocate - so the app works regardless of which
-- role created them.
--
-- EXPECT LARGE ID GAPS. Snowflake reserves a block (observed: 100 values) per
-- NEXTVAL call, so ids jump - drawing one value moved SEQ_TEAM's next_value from
-- 4 to 104. Uniqueness and ordering still hold and the id columns are
-- NUMBER(38,0), so this is cosmetic, but do not mistake a jump for a bug and do
-- not treat an id as a row count.
--
-- RULE_MAPPING and RULE_REFERENCE are deliberately absent: the app reads them
-- but has no write path, so they never call reserve_ids().

USE ROLE IF_CDA_PDH_SUPPLY_PLANNING_IF_IP_ZSHARE_DEV1_CONTRIBUTOR;

CREATE SEQUENCE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.SEQ_RULE
    START = 102954 INCREMENT = 1
    COMMENT = 'RULE.RULE_ID';

CREATE SEQUENCE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.SEQ_RULE_CONDITION
    START = 164 INCREMENT = 1
    COMMENT = 'RULE_CONDITION.CONDITION_ID';

CREATE SEQUENCE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.SEQ_RULE_ACTION
    START = 161 INCREMENT = 1
    COMMENT = 'RULE_ACTION.ACTION_ID';

CREATE SEQUENCE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.SEQ_RULE_JOIN
    START = 33 INCREMENT = 1
    COMMENT = 'RULE_JOIN.JOIN_ID';

CREATE SEQUENCE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.SEQ_RULE_JOIN_COLUMN
    START = 40 INCREMENT = 1
    COMMENT = 'RULE_JOIN_COLUMN.JOIN_COLUMN_ID';

CREATE SEQUENCE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.SEQ_RULE_JOIN_FILTER
    START = 18 INCREMENT = 1
    COMMENT = 'RULE_JOIN_FILTER.FILTER_ID';

CREATE SEQUENCE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.SEQ_RULE_SET
    START = 1045 INCREMENT = 1
    COMMENT = 'RULE_SET.RULE_SET_ID';

CREATE SEQUENCE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.SEQ_RULE_SET_SOURCE
    START = 59 INCREMENT = 1
    COMMENT = 'RULE_SET_SOURCE.SOURCE_ID';

CREATE SEQUENCE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.SEQ_BUSINESS_OBJECT
    START = 68 INCREMENT = 1
    COMMENT = 'BUSINESS_OBJECT.OBJECT_ID';

CREATE SEQUENCE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.SEQ_BUSINESS_OBJECT_COLUMN
    START = 1709 INCREMENT = 1
    COMMENT = 'BUSINESS_OBJECT_COLUMN.COLUMN_SPEC_ID';

CREATE SEQUENCE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.SEQ_MODULE
    START = 6 INCREMENT = 1
    COMMENT = 'MODULE.MODULE_ID';

CREATE SEQUENCE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.SEQ_TEAM
    START = 4 INCREMENT = 1
    COMMENT = 'TEAM.TEAM_ID';

CREATE SEQUENCE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.SEQ_TEAM_DATABASE
    START = 5 INCREMENT = 1
    COMMENT = 'TEAM_DATABASE.TEAM_DATABASE_ID';

CREATE SEQUENCE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.SEQ_RULE_TEST_CASE
    START = 88 INCREMENT = 1
    COMMENT = 'RULE_TEST_CASE.TEST_CASE_ID';

CREATE SEQUENCE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.SEQ_RULE_REVIEW_COMMENT
    START = 8 INCREMENT = 1
    COMMENT = 'RULE_REVIEW_COMMENT.COMMENT_ID';

CREATE SEQUENCE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.SEQ_RULE_CHANGE_HISTORY
    START = 67 INCREMENT = 1
    COMMENT = 'RULE_CHANGE_HISTORY.CHANGE_ID';

CREATE SEQUENCE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.SEQ_AUDIT_TRAIL
    START = 121 INCREMENT = 1
    COMMENT = 'AUDIT_TRAIL.AUDIT_ID';

-- Reading a sequence needs no explicit grant (verified: even PUBLIC can call
-- NEXTVAL), so this GRANT is belt-and-braces. It fails in a managed-access
-- schema unless run by the schema owner or a role with MANAGE GRANTS - that
-- failure is safe to ignore, the sequences still work.
GRANT USAGE ON ALL SEQUENCES IN SCHEMA IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE
    TO ROLE IF_CDA_PDH_SUPPLY_PLANNING_IF_IP_ZSHARE_DEV1_CONTRIBUTOR;
