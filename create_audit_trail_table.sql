-- DDL for the general-purpose AUDIT_TRAIL table tracking all rule repository mutations
-- Co-authored with CoCo

CREATE TABLE IF NOT EXISTS IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.AUDIT_TRAIL (
    AUDIT_ID        INT          NOT NULL,
    ENTITY_TYPE     VARCHAR(50)  NOT NULL,
    ENTITY_ID       INT,
    ACTION          VARCHAR(100) NOT NULL,
    DETAIL          VARCHAR(4000),
    PERFORMED_BY    VARCHAR(256) DEFAULT CURRENT_USER(),
    PERFORMED_TS    TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
);

COMMENT ON TABLE IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.AUDIT_TRAIL IS
    'General audit trail for all rule repository mutations: rule changes, review comments, module operations.';
