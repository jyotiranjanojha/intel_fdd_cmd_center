# Rule Repository App ("FDD Command Center")

A Streamlit-in-Snowflake application for authoring, documenting, governing and validating
FDD transformation rules across the supply chain data platform.

The repository is the source of truth for *intent*: what each rule is supposed to do, which
columns each object is supposed to have, and where the data flows. Snowflake is the source
of truth for what is actually built. The app's job is to keep those two readable side by
side, and to make the difference visible when they diverge. Once a module's rules are
accepted it is handed to development, and the app tracks it through to completion together
with pdh-engineering-studio.

Current contents (live, October 2026): **101 active rules** across **74 rule sets**,
**80 business objects**, **2,577 declared object columns**, **6 modules** under **3 teams**.

## Pages

Pages are grouped in the sidebar by what you are trying to do.

### Browse

| Page | What it does |
|------|--------------|
| **Requirements** | Read rules as FDD English — headline plus bullets, rendered deterministically. Filter by module, object and approval status (All / Draft / Changes requested / Accepted). Per-rule **Accept** / **Request changes** controls and **Accept all** per target object. Inline sample data, PO review comments, rule notes, the module's dev hand-off status strip, Markdown export |
| **Publish FDD** | Assemble one module — lineage, object column specs and every rule — into a single self-contained HTML document for download |
| **Ask** | Natural-language questions about rules, columns or the data. Routes the question to metadata lookup, Cortex Analyst, or a plain LLM answer |
| **Object lineage** | Source → target flow as a Graphviz diagram. Scope to one module (`Stay within this module`) and objects outside it are drawn dashed as boundary nodes rather than pulling in the whole graph |
| **Dev pipeline** | Which modules are ready for development, in development or done, and what changed in their rules since the hand-off or since code was generated |

### Author

| Page | What it does |
|------|--------------|
| **Manage teams & modules** | Create teams and modules, assign objects and rule sets, map teams to logical databases, manage per-team approvers |
| **Import an FDD** | Three steps. **Read:** pick a module (or type a new name - picking an existing one imports into it in parts) and upload the `.xlsx` or paste the text; the default sheets are applied straight away. **Review:** one summary line and a single *Needs a look* list - one line per rule with its most important problem, **Choose** (when there are options), **Skip rule** and **Edit**. Fixes with one clear answer are applied automatically (undoable), and doubts and FDD notes go to the module's review comments, so only blocking problems are listed. Objects, name corrections, rules already in the module, verified rules and lines not imported are under **Details**. **Import:** one button that says what it will write - one transaction, all of it or none of it. Reviews are saved as drafts, so progress survives a reload, timeout or redeploy |
| **Object columns** | The column spec for a target object: source on the left, the spec being built on the right. Ticks, edits, new rows and deletions are staged until **Save spec**. Shows which rules cover each column, flags derived columns with no rule, and can link one to an existing rule. Tabs: Build spec, FDD export, Verify. Also hosts the sample data panel |
| **Write a rule** | Type a requirement in plain English, read the compiled preview, save as DRAFT. Offers generated test cases immediately after saving |
| **Update a rule** | Pick a rule, describe the change in plain English, confirm the preview, update in place. Warns when the module is already with development |
| **Metadata Entry** | Bulk-enter the business-rule metadata driver rows. Tabs: Header (column labels), Detail (column values) |
| **Manual entry** | Direct edit of individual rule, condition and action rows when the plain-English path is not precise enough |

### Govern

| Page | What it does |
|------|--------------|
| **Audit trail** | Who changed what, when. Tabs: Rule changes (field-level diff per rule), All activity (every repository mutation). Both export to CSV |
| **Rollback a rule** | Pick a past version from the change history, preview the change summary, restore it |
| **Retire a rule** | Reversible removal. A reason is required; the rule is set to `RETIRED` with `ACTIVE_IND = FALSE` and an effective end date, and drops out of the catalog. Tabs: Retire a rule, Retired rules (restore from here) |

### Validate

| Page | What it does |
|------|--------------|
| **Verify against Snowflake** | Compare rules against the deployed objects. Tabs: Structural Gaps, Rule Violations, AI Logic Review |
| **Test cases** | Generate, run, edit and manage SQL test cases per rule. Tabs: Generate, Run & Review, Add Manual, View All, Test script |
| **Repository health** | Repository-wide gaps, each with its offending rows: rules with no actions, objects with no columns, rule sets with no target or no junction source, derived columns with no rule (split from probable misclassified pass-throughs), rules with no test case, test cases never reviewed |

## Architecture

```
streamlit_app.py          Page routing, UI, session state, shared render helpers
db.py                     Database access, LLM calls, versioned caching, id sequences, escaping
repository.py             CRUD for every rule table, audit logging, object column spec,
                          approval workflow and approvers, retire/restore, test cases,
                          teams, rule notes, module hand-off
requirement_compiler.py   Plain English -> structured rule: pre-parse, LLM extract,
                          validate, rollback replay
rule_extractor.py         Rule extraction helpers used by the compiler and FDD import
fdd_render.py             Deterministic FDD renderer - headline, bullets, references, no LLM
compliance.py             Structural / data / AI compliance checks against live objects
test_generator.py         Test case generation and executable SQL builder
catalog.py                Entity resolution, column catalog, lineage edges, database map
nlp_router.py             Intent classification and scoring for the Ask page
lineage_diagram.py        Graphviz DOT generation, banding, boundary and co-input nodes
fdd_import.py             FDD document parser and import-plan builder (pure, no Streamlit)
fdd_import_page.py        The Import an FDD review UI
fdd_draft.py              Saved Import FDD reviews (compressed, chunked drafts)
xlsx_import.py            .xlsx FDD workbook -> the text Import FDD understands
object_columns_builder.py Two-pane column builder behind the Object columns page
sample_data.py            Sample data for objects not (yet) in Snowflake; draft from rules
sample_data_ui.py         Sample data panel
fdd_publish.py            Publish FDD - one module as a self-contained HTML document
md_to_html.py             Pure Markdown -> HTML converter used by Publish FDD
repository_health.py      Repository-wide health checks
module_handoff_ui.py      Dev hand-off status strip, code badges and the Dev pipeline page
generate_architecture_diagram.py   Regenerates architecture_diagram.png
```

`fdd_render.py` is the single source of truth for how a rule reads. `streamlit_app.py`,
`requirement_compiler.py`, `catalog.py` and `fdd_publish.py` all import it rather than
formatting rules themselves, so a rule reads identically on every page and in every export.

## Database objects

Schema: `IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE` (read views in `PDH_SUPPLY_PLANNING_XFRM`)

### Rules

| Table | Purpose |
|-------|---------|
| `RULE` | Rule definitions — name, number, category, business description, priority, status, effective dates |
| `RULE_CONDITION` | Filter conditions — field, operator, value, logical connector |
| `RULE_ACTION` | What the rule does — target field, action type, action value |
| `RULE_JOIN` | Joins linking source and target objects within a rule |
| `RULE_JOIN_COLUMN` | Join keys — left/right column pairs |
| `RULE_JOIN_FILTER` | Additional predicates applied inside a join |
| `RULE_MAPPING` | Column-level source → target mappings |
| `RULE_REFERENCE` | Reference and lookup associations attached to a rule; rendered as a **References** section in the rule text |
| `RULE_NOTE` | Free-text notes on a rule (not part of the rule's logic or change tracking for dev) |
| `RULE_SET` | A group of related rules — one FDD. Carries source and target object, module, subject area |
| `RULE_SET_SOURCE` | Junction table of every source object feeding a rule set. **This is what drives the lineage graph** |

### Objects and organisation

| Table | Purpose |
|-------|---------|
| `BUSINESS_OBJECT` | Registry of source and target technical objects. Objects can be registered before they exist in Snowflake |
| `BUSINESS_OBJECT_COLUMN` | The declared column specification per object — name, type, nullability, PK, origin (`PASS_THROUGH` / `DERIVED` / `DISCOVERED`), source object and column, description |
| `OBJECT_SAMPLE_DATA` | Versioned sample rows per object, with source (`MANUAL` / `FDD_IMPORT` / `GENERATED`) and per-cell origin |
| `MODULE` | Logical grouping of rule sets and objects by domain |
| `TEAM` | Sits one level above a module, so several teams can share the app |
| `TEAM_DATABASE` | Which logical databases a team documents objects in, primary first |
| `DATABASE_MAP` | Logical database name as written in the FDD → physical name in this account |
| `USER_PREFERENCE` | Per-user default team |
| `AGENT_ALLOWED_OBJECT` | Objects the Ask page may query, with a per-object row limit |

### Governance, validation and hand-off

| Object | Purpose |
|-------|---------|
| `RULE_CHANGE_HISTORY` | Field-level history — change type, old value, new value, who, when. Rollback replays from here; review decisions and their reasons are recorded here too |
| `AUDIT_TRAIL` | General-purpose log of every repository mutation |
| `RULE_APPROVER` | Who may accept rules and hand modules to development, per team |
| `RULE_REVIEW_COMMENT` | PO review comments per module, with open/resolved tracking |
| `RULE_TEST_CASE` | Generated and manual test cases with runnable SQL. Status `SUGGESTED` / `ACCEPTED` / `REJECTED` |
| `FDD_IMPORT_DRAFT`, `FDD_IMPORT_DRAFT_PART` | Saved Import FDD reviews, zlib-compressed JSON split into parts |
| `MODULE_HANDOFF` | Module lifecycle decisions (`READY_FOR_DEV`, `WITHDRAWN`, `DEV_COMPLETED`, `REOPENED`) |
| `T_MOCK_METADATA_HEADER` / `T_MOCK_METADATA_DETAIL` | Business rule metadata driver — column labels and parameter values per rule |
| `SEQ_<TABLE>` | One sequence per table for surrogate keys (`create_id_sequences.sql`) |
| `SP_SET_MODULE_DEV_STATUS` | The single entry point for hand-off decisions, shared with pdh-engineering-studio. Returns `OK: …` or `REFUSED: <reason>` |

Views (`PDH_SUPPLY_PLANNING_XFRM`):

- `VW_RULE_SUMMARY` — denormalised read view for fast catalog queries. It does **not** filter
  retired rules; `catalog.get_rule_catalog()` applies that filter.
- `VW_MODULE_DEV_STATUS`, `VW_MODULE_DEV_OBJECTS`, `VW_RULE_CHANGES_FOR_DEV` — module
  hand-off state, per-object code/review status, and rule changes since hand-off or code
  generation. See `ENGINEERING_STUDIO_INTEGRATION.md`.

DDL scripts are the `create_*.sql` files in this folder; `drop_module_handoff.sql` and
`drop_object_sample_data_table.sql` revert their features.

## Rule categories

17 categories are in use. The renderer has a purpose sentence and a headline shape for each,
which is why a rule reads as a sentence rather than a field dump.

`DERIVATION` · `FILTER` · `ACTIVE_STATUS` · `INITIAL_LOAD` · `JOIN_FILTER` ·
`NO_TRANSFORMATION` · `TIME_TRAVEL` · `TRANSFORMATION` · `METADATA_LOOKUP` ·
`METADATA_DERIVATION` · `INCREMENTAL_LOAD` · `ATTRIBUTE_CARRY_FORWARD` · `DEDUPLICATION` ·
`STANDARDIZATION` · `DATA_QUALITY` · `KEY_DEFINITION` · `AGGREGATION`

## Key design decisions

### Rule rendering is deterministic

No LLM is involved in reading a rule back. `fdd_render` produces a one-line headline in
business language followed by `Applies to` / `Result` / `Matched on` / `Context` bullets.
Mapping rows already implied by the bullets are suppressed, so the same fact is never stated
twice. An LLM is used only in the other direction — turning prose into a structured rule.

### Approval workflow

Rules move between `DRAFT`, `CHANGES_REQUESTED` and `ACCEPTED`. Only approvers named for
the rule's team in `RULE_APPROVER` can accept; requesting changes requires a reason, which
is stored in the rule's change history. Editing an accepted rule's content returns it to
review. Legacy statuses map onto the workflow (`ACTIVE` → `DRAFT`, `REJECTED` →
`CHANGES_REQUESTED`).

### Module hand-off to development

`IN_REVIEW → READY_FOR_DEV → IN_DEV → DEV_COMPLETED`, shared with pdh-engineering-studio.
An approver hands a module over once every live rule is accepted. `IN_DEV` and
`DEV_COMPLETED` are derived from the studio's own `T_CMD_CODE_DEPLOYMENT` /
`T_CMD_REVIEW_REQUEST` rows. Rule changes made after hand-off are listed and tagged `LOGIC`
or `WORDING`; the app does not decide whether code is out of date — the developer does.

### Object columns are intent, not reflection

A column is declared with an origin. `PASS_THROUGH` and `DERIVED` are BA intent; `DISCOVERED`
means it was read back from Snowflake. Sync is additive — a hand-declared column keeps its
description and origin when the object is re-read, so a sync never overwrites intent. A
derived column with no rule is flagged as a gap; a pass-through without one is the expected
state and is not flagged.

### Objects may be documented before they are built

Registering an object whose table does not exist yet is supported and expected. Verify
surfaces three states: `OK`, `MISSING` (object built, column absent) and `UNKNOWN` (object not
built, columns unreadable). Only `MISSING` counts as a gap. Sample data for such objects can
be typed, imported from the FDD, or drafted from the rules; every drafted cell records where
its value came from, so a guess never passes as a derived value.

### Lineage reads the junction table

Edges come from `RULE_SET_SOURCE`, not from `RULE_SET.SOURCE_OBJECT_ID` alone, because a rule
set can have several sources. `repository.link_rule_set_objects()` should always be called
alongside `create_rule_set`, or the rule set is invisible to lineage.

### Module scoping stops at the boundary

Selecting a module filters the *edges*, not just the roots. Objects on the far side of a
filtered edge are drawn with a dashed border.

### Publishing reuses, never reimplements

Publish FDD uses the same renderer, lineage scoping and DOT as the app pages, so a published
document cannot drift from what the app shows. The lineage diagram renders client-side the
first time the file is opened with network access; offline it degrades to the raw DOT text.

### Surrogate keys

`db.reserve_ids()` draws from `SEQ_<TABLE>.NEXTVAL`, which is atomic server-side. Snowflake
does not enforce primary keys, so this is what prevents silent duplicate ids. Snowflake
reserves blocks of values, so **large gaps in ids are expected**. If a sequence is missing or
not granted, it falls back to `MAX(id) + 1` with a recorded warning.

### Performance

- Versioned caching (`@st.cache_data` with TTL plus a version bump on write) — no full cache clears
- Object metadata (`object_exists`, `get_columns`, `get_ddl`) cached, so rules sharing a target read it once
- `claude-haiku-4-5` for structured output and classification; `claude-sonnet-4-6` for rule extraction and prose; `llama3.3-70b` as automatic fallback. Structured calls use `AI_COMPLETE` with `response_format`
- One multi-row `INSERT` per table with block id reservation

### Test case generation

Only runnable cases with executable SQL are kept. Failed queries are auto-fixed once on first
run, and any SQL can be edited and re-run individually. Two to three cases per rule — one or
two functional, plus a data quality check.

### Compliance verification

Three tiers: structural (do the named columns exist?) → data (are there violations?) → AI
logic review (does the DDL match the rule?). The AI tier runs only for rules with data
violations, to keep LLM cost down. Row-level pseudo-columns (`RECORD`, `ROW`, `KEY_COLUMNS`,
`DUMMY_COMPONENT`) are excluded from structural checks.

### Retire, not delete

Retiring sets `RULE_STATUS = 'RETIRED'`, `ACTIVE_IND = FALSE` and an effective end date, and
records the reason. Nothing is deleted, so a retired rule can be restored and the audit trail
stays intact.

### Viewer identity

The app runs with owner's rights, so `CURRENT_USER()` is the service identity. The viewer's
identity is passed explicitly (`current_viewer()`) to every audit, review and hand-off write.

## Tests

Unit tests live in `tests/` (26 test modules, fixtures in `tests/fixtures/`), covering FDD import
and re-import, xlsx import, drafts, rendering, join extraction, column resolution, lineage
edges and layout, rollback ordering, approval workflow, module hand-off, rule notes, sample
data and Markdown → HTML. Most target the pure modules (`fdd_import`, `fdd_render`,
`md_to_html`, …) and need no Snowflake session.

## Deploying

The app is a **workspace-sourced** Streamlit app on SPCS (`SYSTEM_COMPUTE_POOL_CPU`,
`execute_as: OWNER`, Python 3.11). `DESCRIBE STREAMLIT` shows its source as
`snow://workspace/"USER$"."PUBLIC"."DEFAULT$"/.../RULE_REPOSITORY_APP/`.

Redeploy with the **Deploy** button in the workspace project pane. There is no SQL or CLI
equivalent for a workspace-sourced app — `snow streamlit deploy` targets stage-deployed apps
and will not republish this one. Only the app owner can redeploy.

Every `.py` file must be listed in the `artifacts` section of `snowflake.yml` or it will not
ship.

## Configuration

- `db.py` — `TABLE_SCHEMA`, `VIEW_SCHEMA`, `DATABASE`, `FAST_MODEL`, `WRITER_MODEL`, `FALLBACK_MODEL`, `SEMANTIC_MODEL_STAGE`
- `sample_data.py` — `SAMPLE_DATA_ENABLED` switches the sample data feature off without touching anything else
- `snowflake.yml` — `query_warehouse`, `compute_pool`, `run_mode`, `execute_as`, `artifacts`, `sharing`

## Known limitations

1. **String interpolation** — values are escaped and identifiers validated, but parameterised binding would be safer long-term.
2. **No justification on edit** — Retire and Request changes require a reason; Write, Update and Rollback still save without one, and `RULE_CHANGE_HISTORY` has no dedicated reason column.
3. **Generated test cases bypass review** — both save paths write `ACCEPTED` rather than the schema default `SUGGESTED`, so generated cases count as reviewed without anyone validating them.
4. **Business description is not enforced** — a blank description produces a compiler warning, not a blocking error.
5. **No attachments** — no file upload and no way to link supporting documents to a rule, rule set or module.
6. **No ownership or contact fields** — `TEAM` and `RULE_APPROVER` say who may act, not who is accountable or whom to contact.
7. **Rule references are display-only** — `RULE_REFERENCE` is rendered in the rule text but cannot be authored on any page and does not appear in lineage.
8. **Export formats** — Markdown, CSV, SQL and HTML (Publish FDD). No PDF or Word, and the metadata parameter matrix is not in any export.
9. **Sequence fallback** — if a `SEQ_*` sequence is missing, id reservation falls back to `MAX(id) + 1` and its race condition.
10. **Hand-off matching** — `T_CMD_REVIEW_REQUEST` has no database column, so reviews match on schema + object name; `IN_DEV` counts code generated before the hand-off too.
