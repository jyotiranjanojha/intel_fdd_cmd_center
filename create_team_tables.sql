-- DDL for the team layer above modules, plus the per-user default team preference
-- Co-authored with CoCo

-- A team owns modules. TEAM sits one level above MODULE so several teams can use
-- the same app without seeing each other's work.
CREATE TABLE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.TEAM (
    TEAM_ID     INT           NOT NULL,
    TEAM_NAME   VARCHAR(255)  NOT NULL,
    TEAM_CODE   VARCHAR(50),
    DESCRIPTION VARCHAR(4000),
    ACTIVE_IND  BOOLEAN       DEFAULT TRUE,
    CREATED_BY  VARCHAR(256)  DEFAULT CURRENT_USER(),
    CREATED_TS  TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP(),
    UPDATED_BY  VARCHAR(256),
    UPDATED_TS  TIMESTAMP_LTZ
);

COMMENT ON TABLE IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.TEAM IS
    'Teams using the rule repository. A team owns modules, which in turn own business objects and rule sets.';

-- Nullable on purpose: adding the column must not break the modules that already
-- exist, and a module with no team stays visible under "All teams".
ALTER TABLE IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.MODULE
    ADD COLUMN IF NOT EXISTS TEAM_ID INT;

-- Remembers a viewer's default team, written only when they tick the box next to
-- the team selector. Keyed on the Snowflake user rather than the app owner,
-- because the app runs execute_as OWNER and CURRENT_USER() would collapse every
-- viewer onto the same row.
CREATE TABLE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.USER_PREFERENCE (
    USER_NAME       VARCHAR(256) NOT NULL,
    DEFAULT_TEAM_ID INT,
    UPDATED_TS      TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
);

COMMENT ON TABLE IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.USER_PREFERENCE IS
    'Per-viewer app preferences. DEFAULT_TEAM_ID pre-selects a team on load.';

-- Seed one starter team and put every existing module under it, so the hierarchy
-- is complete from the first load. Rename it from Manage teams & modules.
INSERT INTO IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.TEAM
    (TEAM_ID, TEAM_NAME, TEAM_CODE, DESCRIPTION)
SELECT 1, 'Supply Planning', 'SP',
       'Starter team holding the modules that existed before teams were introduced.'
WHERE NOT EXISTS (
    SELECT 1 FROM IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.TEAM WHERE TEAM_ID = 1
);

UPDATE IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.MODULE
SET TEAM_ID = 1
WHERE TEAM_ID IS NULL;
