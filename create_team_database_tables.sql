-- DDL for the team-to-database mapping and the logical-to-physical database map
-- Co-authored with CoCo

-- ---------------------------------------------------------------------------
-- TEAM_DATABASE: which databases a team's documented objects live in.
--
-- Many-to-many on purpose. Both existing teams already span two databases (a
-- supply-chain one and a master-data one), so a single column on TEAM could not
-- express it. IS_PRIMARY marks the one to pre-select when registering a new
-- object.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.TEAM_DATABASE (
    TEAM_DATABASE_ID INT           NOT NULL,
    TEAM_ID          INT           NOT NULL,
    LOGICAL_DATABASE VARCHAR(255)  NOT NULL,
    IS_PRIMARY       BOOLEAN       DEFAULT FALSE,
    ACTIVE_IND       BOOLEAN       DEFAULT TRUE,
    CREATED_BY       VARCHAR(256)  DEFAULT CURRENT_USER(),
    CREATED_TS       TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP(),
    UPDATED_BY       VARCHAR(256),
    UPDATED_TS       TIMESTAMP_LTZ
);

COMMENT ON TABLE IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.TEAM_DATABASE IS
    'Databases each team documents objects in. Many-to-many: a team commonly spans a subject-area database and a master-data database. Drives object resolution on the Ask page and the database picker when registering an object.';

-- ---------------------------------------------------------------------------
-- DATABASE_MAP: logical name as recorded in the FDD -> physical name in this
-- account.
--
-- Kept separate from TEAM_DATABASE because the two describe different things.
-- The environment prefix (IF_ in this account) is an account-level fact, while
-- team-to-database is organisational. Folded into one table, two teams sharing a
-- logical database would each carry a copy of the physical mapping and the two
-- copies could drift.
--
-- Identity rows matter: a name already recorded with the physical prefix must
-- pass through untouched rather than being prefixed twice.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.DATABASE_MAP (
    LOGICAL_DATABASE  VARCHAR(255)  NOT NULL,
    PHYSICAL_DATABASE VARCHAR(255)  NOT NULL,
    ACTIVE_IND        BOOLEAN       DEFAULT TRUE,
    CREATED_BY        VARCHAR(256)  DEFAULT CURRENT_USER(),
    CREATED_TS        TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP(),
    UPDATED_BY        VARCHAR(256),
    UPDATED_TS        TIMESTAMP_LTZ
);

COMMENT ON TABLE IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.DATABASE_MAP IS
    'Maps the database name recorded in the FDD to the one that exists in this account. Lets the same metadata resolve correctly across environments.';

-- ---------------------------------------------------------------------------
-- Seed DATABASE_MAP
-- ---------------------------------------------------------------------------
INSERT INTO IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.DATABASE_MAP
    (LOGICAL_DATABASE, PHYSICAL_DATABASE)
SELECT src.LOGICAL_DATABASE, src.PHYSICAL_DATABASE
FROM (
    SELECT 'SUPPLY_CHAIN'    AS LOGICAL_DATABASE, 'IF_SUPPLY_CHAIN' AS PHYSICAL_DATABASE
    UNION ALL SELECT 'MASTER_DATA',     'IF_MASTER_DATA'
    -- Identity rows: already-physical names must not be prefixed again.
    UNION ALL SELECT 'IF_SUPPLY_CHAIN', 'IF_SUPPLY_CHAIN'
    UNION ALL SELECT 'IF_MASTER_DATA',  'IF_MASTER_DATA'
) src
WHERE NOT EXISTS (
    SELECT 1 FROM IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.DATABASE_MAP d
    WHERE d.LOGICAL_DATABASE = src.LOGICAL_DATABASE
);

-- ---------------------------------------------------------------------------
-- Seed TEAM_DATABASE from the databases each team's objects already use, so the
-- mapping starts out matching reality instead of an assumption. The primary is
-- the database holding the most of that team's objects.
-- ---------------------------------------------------------------------------
INSERT INTO IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.TEAM_DATABASE
    (TEAM_DATABASE_ID, TEAM_ID, LOGICAL_DATABASE, IS_PRIMARY)
WITH usage AS (
    SELECT m.TEAM_ID,
           SPLIT_PART(bo.TECHNICAL_OBJECT_NAME, '.', 1) AS LOGICAL_DATABASE,
           COUNT(*)                                     AS OBJECTS
    FROM IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.BUSINESS_OBJECT bo
    JOIN IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.MODULE m
      ON m.MODULE_ID = bo.MODULE_ID
    WHERE bo.TECHNICAL_OBJECT_NAME LIKE '%.%.%'
      AND m.TEAM_ID IS NOT NULL
    GROUP BY 1, 2
), ranked AS (
    SELECT TEAM_ID, LOGICAL_DATABASE, OBJECTS,
           ROW_NUMBER() OVER (ORDER BY TEAM_ID, OBJECTS DESC)            AS RN,
           ROW_NUMBER() OVER (PARTITION BY TEAM_ID ORDER BY OBJECTS DESC) AS RANK_IN_TEAM
    FROM usage
)
SELECT RN, TEAM_ID, LOGICAL_DATABASE, (RANK_IN_TEAM = 1)
FROM ranked r
WHERE NOT EXISTS (
    SELECT 1 FROM IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.TEAM_DATABASE td
    WHERE td.TEAM_ID = r.TEAM_ID
      AND td.LOGICAL_DATABASE = r.LOGICAL_DATABASE
);
