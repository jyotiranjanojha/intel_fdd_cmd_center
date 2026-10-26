# FDD Command Center — Technical Overview

**App:** Rule Repository App ("FDD Command Center")
**Platform:** Streamlit in Snowflake, container runtime (SPCS)
**Data schema:** `IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE` (read views in `PDH_SUPPLY_PLANNING_XFRM`)

---

## 1. Purpose

Replaces the Excel + wiki FDD process with a structured, database-backed rule repository.
Analysts still write requirements in plain English; the app stores them as structured,
versioned, queryable rules that can be checked against what is actually deployed in Snowflake.

- **Repository = source of truth for intent** (what a rule / object *should* do)
- **Snowflake = source of truth for what is built**
- The app keeps both side by side and surfaces drift.

Current scale: 131 active rules · 44 rule sets · 67 business objects · 1,708 declared columns · 5 modules · 3 teams · 87 test cases.

## 2. Architecture

```
                 ┌─────────────────────────── Streamlit in Snowflake (SPCS) ───────────────────────────┐
  Browser ─────► │ streamlit_app.py  (routing, UI, session state)                                     │
                 │   ├─ requirement_compiler.py  English → structured rule (LLM extract + validator)  │
                 │   ├─ fdd_import.py            Whole-FDD parser & import plan (pure Python)         │
                 │   ├─ fdd_render.py            Deterministic rule → FDD English (no LLM)            │
                 │   ├─ compliance.py            Structural / data / AI checks vs live objects        │
                 │   ├─ test_generator.py        Generates runnable SQL test cases                    │
                 │   ├─ catalog.py / lineage_diagram.py   Column catalog, lineage (Graphviz)          │
                 │   ├─ nlp_router.py            "Ask" page intent routing                            │
                 │   └─ repository.py / db.py    CRUD, audit logging, caching, escaping, LLM calls    │
                 └──────────────┬──────────────────────────────┬──────────────────────────────────────┘
                                │ SQL (owner's rights)          │ Cortex
                                ▼                               ▼
                 Rule & metadata tables (XFRM_BASE)    AI_COMPLETE / COMPLETE, Cortex Analyst
                 Deployed objects (INFORMATION_SCHEMA, GET_DDL)   (semantic model on stage)
```

| Setting | Value |
|---|---|
| Runtime | SPCS container runtime, `SYSTEM_COMPUTE_POOL_CPU`, Python 3.11 |
| Execution | `execute_as: OWNER` (owner's-rights) |
| Query warehouse | `SNOWFLAKE_LEARNING_WH` |
| Deployment | Workspace-sourced app; redeployed from the Workspace *Deploy* button (all `.py` files must be listed in `snowflake.yml` artifacts) |
| Codebase | ~21k lines of Python across 23 modules + SQL DDL scripts; 26 unit-test modules under `tests/` |

## 3. Functional areas

| Area | Capabilities |
|---|---|
| **Browse** | Requirements (FDD-style reading, filter by module/object/approval status, inline sample data, PO review comments, Markdown export) · Ask (natural-language Q&A) · Object lineage |
| **Author** | Import a whole FDD (paste → parse → review → single-transaction write) · Write/Update a rule in plain English · Object column spec builder · Teams & modules · Metadata entry · Manual edit |
| **Govern** | Field-level audit trail · Rollback to any prior version · Retire/restore (soft delete with reason) · Accept / Request-changes approval per rule |
| **Validate** | Verify against Snowflake (structural gaps, rule violations, AI logic review) · Test case generation, execution and management |
| **Hand-off** | Module lifecycle `IN_REVIEW → READY_FOR_DEV → IN_DEV → DEV_COMPLETED` integrated with pdh-engineering-studio (PDH_COMMAND_CENTER) |

## 4. Data model (key tables)

- **Rules:** `RULE`, `RULE_CONDITION`, `RULE_ACTION`, `RULE_JOIN`, `RULE_JOIN_COLUMN`, `RULE_JOIN_FILTER`, `RULE_MAPPING`, `RULE_REFERENCE`, `RULE_SET`, `RULE_SET_SOURCE` (drives lineage)
- **Objects & organisation:** `BUSINESS_OBJECT`, `BUSINESS_OBJECT_COLUMN` (origin `PASS_THROUGH` / `DERIVED` / `DISCOVERED`), `MODULE`, `TEAM`, `TEAM_DATABASE`, `DATABASE_MAP`, `AGENT_ALLOWED_OBJECT`
- **Governance & validation:** `RULE_CHANGE_HISTORY`, `AUDIT_TRAIL`, `RULE_REVIEW_COMMENT`, `RULE_TEST_CASE`, `MODULE_HANDOFF`
- **Read views:** `VW_RULE_SUMMARY`, `VW_MODULE_DEV_STATUS`, `VW_MODULE_DEV_OBJECTS`, `VW_RULE_CHANGES_FOR_DEV`
- **Procedure:** `SP_SET_MODULE_DEV_STATUS` — single entry point for hand-off decisions; validates entry conditions, writes hand-off + audit, returns `OK` / `REFUSED` (never raises)

17 rule categories are supported (DERIVATION, FILTER, JOIN_FILTER, INCREMENTAL_LOAD, DEDUPLICATION, DATA_QUALITY, KEY_DEFINITION, AGGREGATION, etc.).

## 5. AI usage (Snowflake Cortex — data stays in Snowflake)

| Use | Model |
|---|---|
| Rule extraction from prose, writing | `claude-sonnet-4-6` |
| Structured output, classification, AI logic review | `claude-haiku-4-5` |
| Automatic fallback on error/throttle | `llama3.3-70b` |
| Data questions on the Ask page | Cortex Analyst over a semantic model (`supply_chain_planning.yaml`) |

Design principles:
- **LLM in one direction only.** AI turns English into a structured rule; reading a rule back is fully deterministic (`fdd_render.py`), so every page and export reads identically.
- **Schema-constrained output.** `AI_COMPLETE` with `response_format` forces a JSON schema; output is then validated before anything is saved. User always confirms a preview.
- **Cost control.** AI logic review only runs for rules that already show data violations; LLM responses are cached.
- **Ask page guardrails.** Only objects listed in `AGENT_ALLOWED_OBJECT` may be queried, with per-object row limits.

## 6. Key engineering decisions

- **Spec before build.** Objects can be documented before the table exists. Verification distinguishes `OK`, `MISSING` (built, column absent — counted as a gap) and `UNKNOWN` (not built yet).
- **Additive sync.** Re-reading an object from Snowflake never overwrites analyst-declared columns or descriptions.
- **Three-tier compliance:** structural (columns exist?) → data (violating rows?) → AI (does the DDL implement the rule?).
- **Atomic FDD import.** Everything parsed is shown for review, including lines that could not be placed; write is all-or-nothing.
- **Retire, not delete.** Full history retained; any retired rule can be restored; rollback replays `RULE_CHANGE_HISTORY`.
- **Module-scoped lineage.** Edges outside the selected module are drawn as dashed boundary nodes instead of expanding the graph.
- **Performance.** Versioned caching (TTL + version bump on write, no global cache clears), cached object metadata, multi-row inserts with block ID reservation.
- **Test generation.** 2–3 runnable SQL cases per rule (functional + data quality); failing SQL is auto-fixed once and can be edited/re-run.

## 7. Integration with pdh-engineering-studio

- Studio-side tables `T_CMD_CODE_DEPLOYMENT` and `T_CMD_REVIEW_REQUEST` are read as-is to derive `IN_DEV` / `DEV_COMPLETED` — no studio changes needed for the basics.
- Rule changes after code generation are listed and tagged `LOGIC` vs `WORDING`; the developer decides whether to regenerate, ignore or reopen.
- Both apps run under `ROLE_IF_CDA_PDH_SUPPLY_PLANNING_ANALYSIS_CONTRIBUTOR`.

## 8. Known limitations / roadmap

1. Surrogate keys use `MAX(id)+1`; move to `SEQUENCE`/`IDENTITY` for concurrent production use.
2. SQL uses escaped string interpolation with validated identifiers; parameter binding is the planned hardening.
3. Change history records who/what/when but not a justification for edits (only Retire requires a reason).
4. No document attachments or owner/contact fields yet.
5. No repository-wide coverage dashboard (completeness is per rule / per object).
6. Exports are Markdown, CSV and SQL; no PDF/Word.
7. Under owner's rights `CURRENT_USER()` is the service identity, so the viewer's identity is passed explicitly for audit.
