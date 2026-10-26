# Rule Repository ⇄ pdh-engineering-studio: module hand-off

For the owner of pdh-engineering-studio (PDH_COMMAND_CENTER). Everything here is in
`IF_SUPPLY_CHAIN`, readable and callable with `ROLE_IF_CDA_PDH_SUPPLY_PLANNING_ANALYSIS_CONTRIBUTOR`,
the role both apps already run as. DDL: `create_module_handoff.sql` (revert:
`drop_module_handoff.sql`).

## The lifecycle

| State | Set by | Meaning |
|---|---|---|
| `IN_REVIEW` | default | The product owner is still accepting rules. |
| `READY_FOR_DEV` | Rule Repository, approver | Every live rule in the module is ACCEPTED and it was handed over. |
| `IN_DEV` | derived | The studio has a row in `T_CMD_CODE_DEPLOYMENT` for one of the module's target objects. |
| `DEV_COMPLETED` | derived **or** decided | Every target object's latest code row is `DEV_COMPLETED` (or `DEPLOYED`) — no extra call needed — or a developer called the procedure. |

`WITHDRAWN` (back to review) and `REOPENED` (back to development) are decisions recorded
in the same table.

## Nothing to change for the basics

The repository already reads your tables:

- `T_CMD_CODE_DEPLOYMENT` — latest row per `TARGET_OBJECT` (matched on the full technical
  name, upper-cased). Your `STATUS` values are used as they are:
  `GENERATED` = coded, `REVIEWED` / `VERIFIED` = reviewed, `DEV_COMPLETED` / `DEPLOYED` = done.
- `T_CMD_REVIEW_REQUEST` — latest row per `SCHEMA_NAME` + `OBJECT_NAME`; `APPROVED` counts as reviewed.

So `IN_DEV` and `DEV_COMPLETED` already show in the Rule Repository from what the studio
writes today.

## What to read

```sql
-- Modules handed to development (show these as "ready to build")
SELECT MODULE_ID, MODULE_NAME, DEV_STATE, ACCEPTED_RULES, LIVE_RULES,
       OBJECTS, OBJECTS_WITH_CODE, OBJECTS_DEV_COMPLETED,
       LAST_DECISION_BY, LAST_DECISION_TS, LAST_DECISION_NOTE,
       CHANGES_SINCE_DECISION, LOGIC_CHANGES_SINCE_DECISION
FROM IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM.VW_MODULE_DEV_STATUS
WHERE DEV_STATE IN ('READY_FOR_DEV', 'IN_DEV');

-- The objects of one module, with your code and review status and how many rule
-- changes were made after the code was generated
SELECT TARGET_OBJECT, CODE_STATUS, CODE_GENERATED_TS, REVIEW_STATUS,
       CHANGES_SINCE_CODE, LOGIC_CHANGES_SINCE_CODE
FROM IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM.VW_MODULE_DEV_OBJECTS
WHERE MODULE_ID = :module_id;

-- What changed in an object's rules since you generated its code
SELECT RULE_ID, RULE_NUMBER, RULE_NAME, CHANGE_TYPE, CHANGE_KIND,
       OLD_VALUE, NEW_VALUE, CHANGED_BY, CHANGED_TS
FROM IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM.VW_RULE_CHANGES_FOR_DEV
WHERE TARGET_OBJECT = UPPER(:target_object)
  AND CHANGED_TS > :generated_ts
ORDER BY CHANGED_TS DESC;
```

### Rule changes: you decide

The repository does **not** say code is out of date. It lists what changed after the
code was generated and tags each change, to help a developer judge quickly:

- `LOGIC` — conditions, actions, joins, category, priority; a rule added, retired or restored.
- `WORDING` — rule name, description, FDD text.

Notes on a rule (`RULE_NOTE`) and acceptance changes are not listed. Whether a change
needs new code — for example if your generator uses the description — is the
developer's call: regenerate, ignore, or `REOPENED`.

## What to call

One procedure for every decision; it checks the entry conditions and writes
`MODULE_HANDOFF` and `AUDIT_TRAIL`. It returns `OK: …` or `REFUSED: <reason>` and never
raises for a refused decision.

```sql
CALL IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.SP_SET_MODULE_DEV_STATUS(
    :module_id,
    'DEV_COMPLETED',        -- or 'REOPENED'
    :note,                  -- optional, shown to the product owner
    :viewer_email,          -- the person, not the service user
    'ENGINEERING_STUDIO');
```

| Decision | Allowed when |
|---|---|
| `DEV_COMPLETED` | Module is `READY_FOR_DEV` or `IN_DEV`, and every target object has generated code with an approved review. |
| `REOPENED` | Module is `DEV_COMPLETED`. |
| `READY_FOR_DEV`, `WITHDRAWN` | Product-owner decisions, made in the Rule Repository. The procedure cannot see who the viewer is (owner's rights), so the calling app checks approvers; please don't call these from the studio. |

Pass the viewer as `P_SET_BY`: under owner's rights `CURRENT_USER()` is the Streamlit
service identity.

## Suggested studio changes

1. A **"Ready for dev"** list from `VW_MODULE_DEV_STATUS`.
2. When generating from the repository, warn if the module is not `READY_FOR_DEV` /
   `IN_DEV`, or if a rule is not `ACCEPTED`.
3. On an object with `CHANGES_SINCE_CODE > 0`, show the change list from
   `VW_RULE_CHANGES_FOR_DEV` with Regenerate / Dismiss.
4. A **"Mark module dev complete"** and **"Reopen"** button calling the procedure.
   (Not strictly needed for completion: setting every object's code to `DEV_COMPLETED`
   already completes the module.)
5. Optionally fill `DEPLOYED_TS` when code is deployed.

## Limits worth knowing

- Times are compared as `TIMESTAMP_NTZ` in both apps' session time zone; both currently
  run in the same account default.
- `T_CMD_REVIEW_REQUEST` has no database column, so reviews match on schema + object name.
- `IN_DEV` counts any code row for the module's objects, including code generated before
  the hand-off.
