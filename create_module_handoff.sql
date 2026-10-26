-- Module hand-off to development, shared by the Rule Repository app and
-- pdh-engineering-studio (PDH_COMMAND_CENTER).
-- Co-authored with CoCo
--
-- Lifecycle of a module:
--   IN_REVIEW      no hand-off yet (or the last one was withdrawn)
--   READY_FOR_DEV  an approver handed it over; every live rule was ACCEPTED
--   IN_DEV         derived: the studio has generated code for one of its objects
--   DEV_COMPLETED  a developer closed it; every object has code and an approved review
--
-- The repository only REPORTS rule changes made after a hand-off or after code was
-- generated (VW_RULE_CHANGES_FOR_DEV). Whether a change needs new code is the
-- developer's decision; nothing here changes a module's state because a rule changed.
--
-- Revert: drop_module_handoff.sql

USE ROLE ROLE_IF_CDA_PDH_SUPPLY_PLANNING_ANALYSIS_CONTRIBUTOR;

-- One row per decision, never updated, so the history explains itself.
-- STATUS: READY_FOR_DEV | DEV_COMPLETED | WITHDRAWN | REOPENED
-- RULE_IDS: the live rules at the moment of the decision, comma separated.
-- SOURCE_APP: RULE_REPOSITORY or ENGINEERING_STUDIO.
CREATE TABLE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.MODULE_HANDOFF (
    HANDOFF_ID  NUMBER        NOT NULL,
    MODULE_ID   NUMBER        NOT NULL,
    STATUS      VARCHAR(20)   NOT NULL,
    RULE_IDS    VARCHAR(16000),
    RULE_COUNT  NUMBER,
    NOTE        VARCHAR(4000),
    SET_BY      VARCHAR(128)  DEFAULT CURRENT_USER(),
    SET_TS      TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    SOURCE_APP  VARCHAR(30)   DEFAULT 'RULE_REPOSITORY'
);

CREATE SEQUENCE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.SEQ_MODULE_HANDOFF
    START = 1 INCREMENT = 1;


-- Every rule change, tagged by kind, with the module and target object it belongs to.
-- LOGIC   what the rule does: conditions, actions, joins, category, priority, and a
--         rule being added, retired or restored.
-- WORDING how it is described: name, description, FDD text.
-- Status changes (acceptance) and notes (RULE_NOTE) are not rule changes.
CREATE OR REPLACE VIEW IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM.VW_RULE_CHANGES_FOR_DEV AS
SELECT
    h.CHANGE_ID,
    h.RULE_ID,
    r.RULE_NUMBER,
    r.RULE_NAME,
    rs.RULE_SET_ID,
    rs.RULE_SET_NAME,
    rs.MODULE_ID,
    UPPER(COALESCE(bo.TECHNICAL_OBJECT_NAME, rs.TARGET_TECHNICAL_OBJECT)) AS TARGET_OBJECT,
    h.CHANGE_TYPE,
    CASE
        WHEN h.CHANGE_TYPE IN ('CONDITIONS', 'ACTIONS', 'JOINS', 'RULE_CATEGORY', 'PRIORITY',
                               'CREATED', 'RETIRED', 'RESTORED') THEN 'LOGIC'
        WHEN h.CHANGE_TYPE IN ('RULE_NAME', 'BUSINESS_DESCRIPTION', 'FDD_TEXT', 'FDD_SOURCE')
            THEN 'WORDING'
        ELSE 'OTHER'
    END AS CHANGE_KIND,
    h.OLD_VALUE,
    h.NEW_VALUE,
    h.CHANGED_BY,
    h.CHANGED_TS
FROM IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.RULE_CHANGE_HISTORY h
JOIN IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.RULE r ON r.RULE_ID = h.RULE_ID
JOIN IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.RULE_SET rs ON rs.RULE_SET_ID = r.RULE_SET_ID
LEFT JOIN IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.BUSINESS_OBJECT bo
       ON bo.OBJECT_ID = rs.TARGET_OBJECT_ID
WHERE h.CHANGE_TYPE <> 'RULE_STATUS';


-- One row per module target object: its latest generated code, latest review, and
-- how many rule changes were made after that code was generated.
CREATE OR REPLACE VIEW IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM.VW_MODULE_DEV_OBJECTS AS
WITH targets AS (
    SELECT DISTINCT
        rs.MODULE_ID,
        UPPER(COALESCE(bo.TECHNICAL_OBJECT_NAME, rs.TARGET_TECHNICAL_OBJECT)) AS TARGET_OBJECT
    FROM IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.RULE_SET rs
    JOIN IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.RULE r ON r.RULE_SET_ID = rs.RULE_SET_ID
    LEFT JOIN IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.BUSINESS_OBJECT bo
           ON bo.OBJECT_ID = rs.TARGET_OBJECT_ID
    WHERE COALESCE(UPPER(r.RULE_STATUS), '') <> 'RETIRED'
      AND COALESCE(bo.TECHNICAL_OBJECT_NAME, rs.TARGET_TECHNICAL_OBJECT) IS NOT NULL
),
code AS (
    SELECT UPPER(TARGET_OBJECT) AS TARGET_OBJECT, DEPLOYMENT_ID, STATUS, RULE_IDS,
           DEPLOYED_BY, GENERATED_TS, DEPLOYED_TS
    FROM IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.T_CMD_CODE_DEPLOYMENT
    QUALIFY ROW_NUMBER() OVER (PARTITION BY UPPER(TARGET_OBJECT)
                               ORDER BY GENERATED_TS DESC NULLS LAST, DEPLOYMENT_ID DESC) = 1
),
review AS (
    SELECT UPPER(SCHEMA_NAME) AS SCHEMA_NAME, UPPER(OBJECT_NAME) AS OBJECT_NAME,
           STATUS, REVIEWER, SUBMITTED_AT, APPROVED_AT
    FROM IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.T_CMD_REVIEW_REQUEST
    QUALIFY ROW_NUMBER() OVER (PARTITION BY UPPER(SCHEMA_NAME), UPPER(OBJECT_NAME)
                               ORDER BY SUBMITTED_AT DESC NULLS LAST, REVIEW_ID DESC) = 1
),
changed AS (
    SELECT t.MODULE_ID, t.TARGET_OBJECT,
           COUNT(*) AS CHANGES_SINCE_CODE,
           COUNT_IF(x.CHANGE_KIND = 'LOGIC') AS LOGIC_CHANGES_SINCE_CODE
    FROM targets t
    JOIN code c ON c.TARGET_OBJECT = t.TARGET_OBJECT
    JOIN IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM.VW_RULE_CHANGES_FOR_DEV x
      ON x.TARGET_OBJECT = t.TARGET_OBJECT AND x.MODULE_ID = t.MODULE_ID
     AND x.CHANGED_TS > c.GENERATED_TS
    GROUP BY t.MODULE_ID, t.TARGET_OBJECT
)
SELECT
    t.MODULE_ID,
    t.TARGET_OBJECT,
    c.DEPLOYMENT_ID,
    c.STATUS          AS CODE_STATUS,
    c.RULE_IDS        AS CODE_RULE_IDS,
    c.DEPLOYED_BY     AS CODE_BY,
    c.GENERATED_TS    AS CODE_GENERATED_TS,
    c.DEPLOYED_TS     AS CODE_DEPLOYED_TS,
    v.STATUS          AS REVIEW_STATUS,
    v.REVIEWER,
    v.APPROVED_AT     AS REVIEW_APPROVED_TS,
    c.DEPLOYMENT_ID IS NOT NULL AS HAS_CODE,
    -- The studio moves code GENERATED -> REVIEWED -> VERIFIED -> DEV_COMPLETED.
    -- Reviewed: past GENERATED there, or the latest code review was approved.
    (COALESCE(UPPER(v.STATUS) = 'APPROVED', FALSE)
     OR COALESCE(UPPER(c.STATUS) IN ('REVIEWED', 'VERIFIED', 'DEV_COMPLETED', 'DEPLOYED'), FALSE))
        AS IS_REVIEWED,
    COALESCE(UPPER(c.STATUS) IN ('DEV_COMPLETED', 'DEPLOYED'), FALSE) AS IS_DEV_COMPLETED,
    COALESCE(ch.CHANGES_SINCE_CODE, 0)       AS CHANGES_SINCE_CODE,
    COALESCE(ch.LOGIC_CHANGES_SINCE_CODE, 0) AS LOGIC_CHANGES_SINCE_CODE
FROM targets t
LEFT JOIN code c ON c.TARGET_OBJECT = t.TARGET_OBJECT
LEFT JOIN review v ON v.SCHEMA_NAME = SPLIT_PART(t.TARGET_OBJECT, '.', 2)
                  AND v.OBJECT_NAME = SPLIT_PART(t.TARGET_OBJECT, '.', 3)
LEFT JOIN changed ch ON ch.MODULE_ID = t.MODULE_ID AND ch.TARGET_OBJECT = t.TARGET_OBJECT;


-- One row per module: where it is in the lifecycle and what changed since.
CREATE OR REPLACE VIEW IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM.VW_MODULE_DEV_STATUS AS
WITH latest AS (
    SELECT * FROM IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.MODULE_HANDOFF
    QUALIFY ROW_NUMBER() OVER (PARTITION BY MODULE_ID ORDER BY SET_TS DESC, HANDOFF_ID DESC) = 1
),
rules AS (
    SELECT rs.MODULE_ID,
           COUNT(*) AS LIVE_RULES,
           COUNT_IF(UPPER(r.RULE_STATUS) = 'ACCEPTED') AS ACCEPTED_RULES,
           -- Kept in the FDD's words (as_written.py) - developers build from the text.
           COUNT_IF(UPPER(r.RULE_CATEGORY) IN ('AS_WRITTEN', 'CONTEXT')) AS AS_WRITTEN_RULES
    FROM IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.RULE r
    JOIN IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.RULE_SET rs ON rs.RULE_SET_ID = r.RULE_SET_ID
    WHERE COALESCE(UPPER(r.RULE_STATUS), '') <> 'RETIRED'
    GROUP BY rs.MODULE_ID
),
objects AS (
    SELECT MODULE_ID,
           COUNT(*) AS OBJECTS,
           COUNT_IF(HAS_CODE) AS OBJECTS_WITH_CODE,
           COUNT_IF(HAS_CODE AND IS_REVIEWED) AS OBJECTS_REVIEWED,
           COUNT_IF(IS_DEV_COMPLETED) AS OBJECTS_DEV_COMPLETED
    FROM IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM.VW_MODULE_DEV_OBJECTS
    GROUP BY MODULE_ID
),
changed AS (
    SELECT l.MODULE_ID,
           COUNT(*) AS CHANGES_SINCE_DECISION,
           COUNT_IF(x.CHANGE_KIND = 'LOGIC') AS LOGIC_CHANGES_SINCE_DECISION
    FROM latest l
    JOIN IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM.VW_RULE_CHANGES_FOR_DEV x
      ON x.MODULE_ID = l.MODULE_ID AND x.CHANGED_TS > l.SET_TS
    WHERE l.STATUS IN ('READY_FOR_DEV', 'DEV_COMPLETED', 'REOPENED')
    GROUP BY l.MODULE_ID
)
SELECT
    m.MODULE_ID,
    m.MODULE_NAME,
    m.TEAM_ID,
    COALESCE(ru.LIVE_RULES, 0)      AS LIVE_RULES,
    COALESCE(ru.ACCEPTED_RULES, 0)  AS ACCEPTED_RULES,
    COALESCE(ru.AS_WRITTEN_RULES, 0) AS AS_WRITTEN_RULES,
    COALESCE(o.OBJECTS, 0)          AS OBJECTS,
    COALESCE(o.OBJECTS_WITH_CODE, 0) AS OBJECTS_WITH_CODE,
    COALESCE(o.OBJECTS_REVIEWED, 0) AS OBJECTS_REVIEWED,
    COALESCE(o.OBJECTS_DEV_COMPLETED, 0) AS OBJECTS_DEV_COMPLETED,
    -- DEV_COMPLETED is either decided (SP_SET_MODULE_DEV_STATUS) or reached: the module
    -- was handed over and the studio has marked every one of its objects DEV_COMPLETED.
    -- A REOPENED decision overrides the derived completion until it is decided again.
    CASE
        WHEN l.STATUS IS NULL OR l.STATUS = 'WITHDRAWN' THEN 'IN_REVIEW'
        WHEN l.STATUS = 'DEV_COMPLETED' THEN 'DEV_COMPLETED'
        WHEN l.STATUS = 'READY_FOR_DEV' AND COALESCE(o.OBJECTS, 0) > 0
             AND o.OBJECTS_DEV_COMPLETED = o.OBJECTS THEN 'DEV_COMPLETED'
        WHEN l.STATUS = 'REOPENED' OR COALESCE(o.OBJECTS_WITH_CODE, 0) > 0 THEN 'IN_DEV'
        ELSE 'READY_FOR_DEV'
    END AS DEV_STATE,
    l.STATUS     AS LAST_DECISION,
    l.SET_BY     AS LAST_DECISION_BY,
    l.SET_TS     AS LAST_DECISION_TS,
    l.NOTE       AS LAST_DECISION_NOTE,
    l.SOURCE_APP AS LAST_DECISION_APP,
    COALESCE(ch.CHANGES_SINCE_DECISION, 0)       AS CHANGES_SINCE_DECISION,
    COALESCE(ch.LOGIC_CHANGES_SINCE_DECISION, 0) AS LOGIC_CHANGES_SINCE_DECISION
FROM IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.MODULE m
LEFT JOIN latest  l  ON l.MODULE_ID = m.MODULE_ID
LEFT JOIN rules   ru ON ru.MODULE_ID = m.MODULE_ID
LEFT JOIN objects o  ON o.MODULE_ID = m.MODULE_ID
LEFT JOIN changed ch ON ch.MODULE_ID = m.MODULE_ID
WHERE COALESCE(m.ACTIVE_IND, TRUE);


-- The one way either app changes a module's dev status. It checks the entry
-- conditions so neither app can skip them, then writes MODULE_HANDOFF and AUDIT_TRAIL.
-- Who may call it for READY_FOR_DEV / WITHDRAWN (approvers) is checked by the calling
-- app, because under owner's rights the procedure cannot see the viewer; pass them in
-- P_SET_BY.
-- Returns 'OK: ...' or 'REFUSED: <reason>'.
CREATE OR REPLACE PROCEDURE IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.SP_SET_MODULE_DEV_STATUS(
    P_MODULE_ID NUMBER, P_STATUS VARCHAR, P_NOTE VARCHAR, P_SET_BY VARCHAR, P_SOURCE_APP VARCHAR)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS CALLER
AS
$$
DECLARE
    v_status   VARCHAR DEFAULT UPPER(TRIM(:P_STATUS));
    v_state    VARCHAR;
    v_live     NUMBER;
    v_accepted NUMBER;
    v_objects  NUMBER;
    v_coded    NUMBER;
    v_reviewed NUMBER;
    v_rule_ids VARCHAR;
    v_id       NUMBER;
    v_who      VARCHAR DEFAULT COALESCE(NULLIF(TRIM(:P_SET_BY), ''), CURRENT_USER());
    v_app      VARCHAR DEFAULT COALESCE(NULLIF(UPPER(TRIM(:P_SOURCE_APP)), ''), 'RULE_REPOSITORY');
BEGIN
    IF (v_status NOT IN ('READY_FOR_DEV', 'DEV_COMPLETED', 'WITHDRAWN', 'REOPENED')) THEN
        RETURN 'REFUSED: unknown status ' || COALESCE(v_status, 'NULL');
    END IF;

    SELECT DEV_STATE, LIVE_RULES, ACCEPTED_RULES, OBJECTS, OBJECTS_WITH_CODE, OBJECTS_REVIEWED
      INTO :v_state, :v_live, :v_accepted, :v_objects, :v_coded, :v_reviewed
      FROM IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM.VW_MODULE_DEV_STATUS
     WHERE MODULE_ID = :P_MODULE_ID;

    IF (v_state IS NULL) THEN
        RETURN 'REFUSED: module ' || :P_MODULE_ID || ' does not exist or is inactive';
    END IF;

    IF (v_status = 'READY_FOR_DEV') THEN
        IF (v_live = 0) THEN
            RETURN 'REFUSED: the module has no live rules';
        END IF;
        IF (v_accepted < v_live) THEN
            RETURN 'REFUSED: ' || (v_live - v_accepted) || ' of ' || v_live || ' rules are not accepted yet';
        END IF;
        IF (v_state = 'DEV_COMPLETED') THEN
            RETURN 'REFUSED: development is completed; reopen it instead';
        END IF;
    ELSEIF (v_status = 'DEV_COMPLETED') THEN
        IF (v_state NOT IN ('READY_FOR_DEV', 'IN_DEV')) THEN
            RETURN 'REFUSED: the module is ' || v_state || ', not handed to development';
        END IF;
        IF (v_coded < v_objects) THEN
            RETURN 'REFUSED: ' || (v_objects - v_coded) || ' of ' || v_objects || ' objects have no generated code';
        END IF;
        IF (v_reviewed < v_objects) THEN
            RETURN 'REFUSED: ' || (v_objects - v_reviewed) || ' of ' || v_objects || ' objects have no approved review';
        END IF;
    ELSEIF (v_status = 'WITHDRAWN') THEN
        IF (v_state = 'IN_REVIEW') THEN
            RETURN 'REFUSED: the module has not been handed to development';
        END IF;
    ELSEIF (v_status = 'REOPENED') THEN
        IF (v_state <> 'DEV_COMPLETED') THEN
            RETURN 'REFUSED: only a completed module can be reopened';
        END IF;
    END IF;

    SELECT LISTAGG(r.RULE_ID, ',') WITHIN GROUP (ORDER BY r.RULE_ID)
      INTO :v_rule_ids
      FROM IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.RULE r
      JOIN IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.RULE_SET rs ON rs.RULE_SET_ID = r.RULE_SET_ID
     WHERE rs.MODULE_ID = :P_MODULE_ID
       AND COALESCE(UPPER(r.RULE_STATUS), '') <> 'RETIRED';

    v_id := (SELECT IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.SEQ_MODULE_HANDOFF.NEXTVAL);

    -- INSERT ... SELECT, not VALUES: Snowflake rejects expressions such as NULLIF in
    -- a VALUES list.
    INSERT INTO IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.MODULE_HANDOFF
        (HANDOFF_ID, MODULE_ID, STATUS, RULE_IDS, RULE_COUNT, NOTE, SET_BY, SOURCE_APP)
    SELECT :v_id, :P_MODULE_ID, :v_status, :v_rule_ids, :v_live,
           NULLIF(TRIM(:P_NOTE), ''), :v_who, :v_app;

    INSERT INTO IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.AUDIT_TRAIL
        (AUDIT_ID, ENTITY_TYPE, ENTITY_ID, ACTION, DETAIL, PERFORMED_BY)
    SELECT IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.SEQ_AUDIT_TRAIL.NEXTVAL, 'MODULE',
           :P_MODULE_ID, 'MODULE_' || :v_status,
           'Module ' || :P_MODULE_ID || ' ' || :v_status || ' via ' || :v_app
           || COALESCE(': ' || NULLIF(TRIM(:P_NOTE), ''), ''),
           :v_who;

    RETURN 'OK: module ' || :P_MODULE_ID || ' is ' || v_status;
END;
$$;
