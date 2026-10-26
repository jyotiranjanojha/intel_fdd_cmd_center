"""
Repository-wide health checks.

Every other validation surface in the app is per-rule or per-object: the Verify
page checks one rule, the Object columns page checks one object. Nothing tells you
how the repository is doing as a whole, so gaps stay invisible unless someone goes
looking. These checks aggregate.

Each check returns its offending rows, not just a count, so a finding is
actionable - the count is len(rows). Detail sets are small at this repository's
size (131 rules, 1,708 columns), so one query per check is simpler than a
separate count query and gives the drill-down for free.

All counts here were validated against live data when written:
  rules with no actions 25 · objects with no columns 13 · rule sets with no target 1 ·
  with no junction source 1 · rules with no test case 114 · rules with no description
  0 · test cases never reviewed 0.

The derived-column checks are deliberately two checks, not one. A naive "derived
column with no rule" reported 118, of which none were genuine rule gaps: 81 were
covered by a whole-record rule, 20 were framework audit columns, and the bulk of
the remainder name-match a source column and are misclassified pass-throughs. The
remediations differ - write a rule versus reclassify the origin - so conflating
them produced a number nobody could act on.
"""

from typing import Dict, List

import pandas as pd

from db import cached_read, sequence_fallbacks, tbl

# severity drives ordering and the icon, not behaviour:
#   high   - breaks something downstream (a rule that does nothing, lineage gaps)
#   medium - incomplete specification, still usable
#   low    - hygiene, worth knowing
CHECKS: List[Dict] = [
    {
        "key": "rules_no_actions",
        "label": "Rules with no actions",
        "severity": "high",
        "why": ("The rule renders as a headline with no Result line, so it reads as "
                "complete but does nothing. Either it is unfinished or its actions "
                "were lost."),
        "sql": lambda: f"""
            SELECT r.RULE_ID, r.RULE_NUMBER, r.RULE_NAME, r.RULE_CATEGORY,
                   rs.RULE_SET_NAME, m.MODULE_NAME
            FROM {tbl('RULE')} r
            LEFT JOIN {tbl('RULE_SET')} rs ON rs.RULE_SET_ID = r.RULE_SET_ID
            LEFT JOIN {tbl('MODULE')} m ON m.MODULE_ID = rs.MODULE_ID
            WHERE COALESCE(r.RULE_STATUS, '') <> 'RETIRED'
              AND NOT EXISTS (SELECT 1 FROM {tbl('RULE_ACTION')} a WHERE a.RULE_ID = r.RULE_ID)
            ORDER BY m.MODULE_NAME, rs.RULE_SET_NAME, r.RULE_NUMBER
        """,
    },
    {
        "key": "rules_as_written",
        "label": "Rules saved as written",
        "severity": "low",
        "why": ("Kept in the FDD's words because the app could not model their logic. "
                "Developers build from the text and Verify against Snowflake reads the "
                "code against it - not a fault, a to-do list for turning them into "
                "structured rules on Update a rule when someone has time."),
        "sql": lambda: f"""
            SELECT r.RULE_ID, r.RULE_NUMBER, r.RULE_NAME, m.MODULE_NAME, rs.RULE_SET_NAME,
                   LEFT(r.BUSINESS_DESCRIPTION, 200) AS AS_WRITTEN_TEXT
            FROM {tbl('RULE')} r
            LEFT JOIN {tbl('RULE_SET')} rs ON rs.RULE_SET_ID = r.RULE_SET_ID
            LEFT JOIN {tbl('MODULE')} m ON m.MODULE_ID = rs.MODULE_ID
            WHERE COALESCE(r.RULE_STATUS, '') <> 'RETIRED'
              AND UPPER(r.RULE_CATEGORY) IN ('AS_WRITTEN', 'CONTEXT')
            ORDER BY m.MODULE_NAME, rs.RULE_SET_NAME, r.RULE_NUMBER
        """,
    },
    {
        "key": "rule_sets_no_source",
        "label": "Rule sets with no source in the junction table",
        "severity": "high",
        "why": ("Lineage is built from RULE_SET_SOURCE. A rule set with no row there "
                "is invisible to the lineage graph and to the FDD export, however "
                "well its rules are written. Fixed by repository.link_rule_set_objects()."),
        "sql": lambda: f"""
            SELECT rs.RULE_SET_ID, rs.RULE_SET_NAME, m.MODULE_NAME,
                   rs.SOURCE_TECHNICAL_OBJECT, rs.TARGET_TECHNICAL_OBJECT
            FROM {tbl('RULE_SET')} rs
            LEFT JOIN {tbl('MODULE')} m ON m.MODULE_ID = rs.MODULE_ID
            WHERE NOT EXISTS (
                SELECT 1 FROM {tbl('RULE_SET_SOURCE')} s WHERE s.RULE_SET_ID = rs.RULE_SET_ID)
            ORDER BY m.MODULE_NAME, rs.RULE_SET_NAME
        """,
    },
    {
        "key": "rule_sets_no_target",
        "label": "Rule sets with no target object",
        "severity": "high",
        "why": ("With no TARGET_OBJECT_ID the rule set produces no lineage edge and "
                "its rules are left out of the per-object FDD export, which groups by "
                "output object."),
        "sql": lambda: f"""
            SELECT rs.RULE_SET_ID, rs.RULE_SET_NAME, m.MODULE_NAME,
                   rs.SOURCE_TECHNICAL_OBJECT, rs.TARGET_TECHNICAL_OBJECT
            FROM {tbl('RULE_SET')} rs
            LEFT JOIN {tbl('MODULE')} m ON m.MODULE_ID = rs.MODULE_ID
            WHERE rs.TARGET_OBJECT_ID IS NULL
            ORDER BY m.MODULE_NAME, rs.RULE_SET_NAME
        """,
    },
    {
        "key": "derived_cols_unexplained",
        "label": "Derived columns nothing explains",
        "severity": "medium",
        "why": ("A column declared as needing transformation logic that nothing accounts "
                "for: no rule targets it, no whole-record rule covers it, it does not "
                "match a declared source column by name, and it is not a framework "
                "column. A developer has nothing at all to build from.\n\n"
                "The three exclusions matter. A naive 'derived column with no rule' "
                "check reported 118 here, of which 0 were real: 81 were covered by a "
                "whole-record rule (TIME_TRAVEL / INCLUDE on RECORD carries every "
                "column), 20 were audit columns the framework populates, and most of "
                "the rest name-match a source column and are simply misclassified - "
                "see the separate pass-through check for those."),
        "sql": lambda: f"""
            WITH whole_record_rule AS (
                -- A rule acting on the whole record populates or carries every column,
                -- so per-column rules are not expected. INCLUDE_MATCHING is excluded:
                -- it filters which rows survive, it does not populate columns.
                SELECT DISTINCT rs.TARGET_OBJECT_ID AS OBJECT_ID
                FROM {tbl('RULE_SET')} rs
                JOIN {tbl('RULE')} r ON r.RULE_SET_ID = rs.RULE_SET_ID
                                    AND COALESCE(r.RULE_STATUS, '') <> 'RETIRED'
                JOIN {tbl('RULE_ACTION')} a ON a.RULE_ID = r.RULE_ID
                WHERE UPPER(a.TARGET_FIELD) IN ('RECORD', 'ROW')
                  AND UPPER(a.ACTION_TYPE) IN ('NO_TRANSFORMATION', 'TIME_TRAVEL_CURRENT',
                                               'TIME_TRAVEL_HISTORY', 'INCLUDE',
                                               'INCLUDE_RECORD', 'CARRY_FORWARD',
                                               -- a sentence may cover any column
                                               'AS_WRITTEN')
            ),
            source_cols AS (
                -- Every column name declared on any source feeding this target.
                SELECT DISTINCT rs.TARGET_OBJECT_ID AS OBJECT_ID,
                       UPPER(sc.COLUMN_NAME) AS COLUMN_NAME
                FROM {tbl('RULE_SET')} rs
                JOIN {tbl('RULE_SET_SOURCE')} rss ON rss.RULE_SET_ID = rs.RULE_SET_ID
                JOIN {tbl('BUSINESS_OBJECT_COLUMN')} sc ON sc.OBJECT_ID = rss.OBJECT_ID
                                                       AND sc.ACTIVE_IND = TRUE
                WHERE rs.TARGET_OBJECT_ID IS NOT NULL
                UNION
                SELECT DISTINCT rs.TARGET_OBJECT_ID, UPPER(sc.COLUMN_NAME)
                FROM {tbl('RULE_SET')} rs
                JOIN {tbl('BUSINESS_OBJECT_COLUMN')} sc ON sc.OBJECT_ID = rs.SOURCE_OBJECT_ID
                                                       AND sc.ACTIVE_IND = TRUE
                WHERE rs.TARGET_OBJECT_ID IS NOT NULL
            )
            SELECT c.OBJECT_ID, bo.OBJECT_NAME, bo.TECHNICAL_OBJECT_NAME,
                   c.COLUMN_NAME, c.DATA_TYPE, c.BUSINESS_DESCRIPTION
            FROM {tbl('BUSINESS_OBJECT_COLUMN')} c
            JOIN {tbl('BUSINESS_OBJECT')} bo ON bo.OBJECT_ID = c.OBJECT_ID
            WHERE c.ACTIVE_IND = TRUE
              AND c.COLUMN_ORIGIN = 'DERIVED'
              AND UPPER(c.COLUMN_NAME) NOT IN ('CREATE_DTM', 'CHANGE_DTM', 'CREATE_AGENT_ID',
                                               'CHANGE_AGENT_ID', 'SOURCE_SYSTEM_NM')
              AND c.OBJECT_ID NOT IN (SELECT OBJECT_ID FROM whole_record_rule)
              AND NOT EXISTS (
                  SELECT 1 FROM source_cols s
                  WHERE s.OBJECT_ID = c.OBJECT_ID
                    AND s.COLUMN_NAME = UPPER(c.COLUMN_NAME))
              AND NOT EXISTS (
                  SELECT 1
                  FROM {tbl('RULE_SET')} rs
                  JOIN {tbl('RULE')} r ON r.RULE_SET_ID = rs.RULE_SET_ID
                  JOIN {tbl('RULE_ACTION')} a ON a.RULE_ID = r.RULE_ID
                  WHERE rs.TARGET_OBJECT_ID = c.OBJECT_ID
                    AND UPPER(a.TARGET_FIELD) = UPPER(c.COLUMN_NAME))
            ORDER BY bo.OBJECT_NAME, c.ORDINAL
        """,
    },
    {
        "key": "derived_cols_look_like_passthrough",
        "label": "Derived columns that look like pass-throughs",
        "severity": "low",
        "why": ("Declared DERIVED but a source feeding the object declares the same "
                "column name, so this is probably a straight pull misclassified during "
                "a bulk load. The remediation is to reclassify the origin on the Object "
                "columns page, NOT to write a rule - which is why this is separate from "
                "the unexplained-columns check. Misclassification inflates the apparent "
                "rule gap and makes the FDD read as though logic is missing."),
        "sql": lambda: f"""
            WITH source_cols AS (
                SELECT DISTINCT rs.TARGET_OBJECT_ID AS OBJECT_ID,
                       UPPER(sc.COLUMN_NAME) AS COLUMN_NAME,
                       sbo.OBJECT_NAME AS SOURCE_OBJECT_NAME
                FROM {tbl('RULE_SET')} rs
                JOIN {tbl('RULE_SET_SOURCE')} rss ON rss.RULE_SET_ID = rs.RULE_SET_ID
                JOIN {tbl('BUSINESS_OBJECT')} sbo ON sbo.OBJECT_ID = rss.OBJECT_ID
                JOIN {tbl('BUSINESS_OBJECT_COLUMN')} sc ON sc.OBJECT_ID = rss.OBJECT_ID
                                                       AND sc.ACTIVE_IND = TRUE
                WHERE rs.TARGET_OBJECT_ID IS NOT NULL
                UNION
                SELECT DISTINCT rs.TARGET_OBJECT_ID, UPPER(sc.COLUMN_NAME), sbo.OBJECT_NAME
                FROM {tbl('RULE_SET')} rs
                JOIN {tbl('BUSINESS_OBJECT')} sbo ON sbo.OBJECT_ID = rs.SOURCE_OBJECT_ID
                JOIN {tbl('BUSINESS_OBJECT_COLUMN')} sc ON sc.OBJECT_ID = rs.SOURCE_OBJECT_ID
                                                       AND sc.ACTIVE_IND = TRUE
                WHERE rs.TARGET_OBJECT_ID IS NOT NULL
            )
            SELECT c.OBJECT_ID, bo.OBJECT_NAME, c.COLUMN_NAME, c.DATA_TYPE,
                   MIN(s.SOURCE_OBJECT_NAME) AS MATCHING_SOURCE
            FROM {tbl('BUSINESS_OBJECT_COLUMN')} c
            JOIN {tbl('BUSINESS_OBJECT')} bo ON bo.OBJECT_ID = c.OBJECT_ID
            JOIN source_cols s ON s.OBJECT_ID = c.OBJECT_ID
                              AND s.COLUMN_NAME = UPPER(c.COLUMN_NAME)
            WHERE c.ACTIVE_IND = TRUE
              AND c.COLUMN_ORIGIN = 'DERIVED'
              AND NOT EXISTS (
                  SELECT 1
                  FROM {tbl('RULE_SET')} rs
                  JOIN {tbl('RULE')} r ON r.RULE_SET_ID = rs.RULE_SET_ID
                  JOIN {tbl('RULE_ACTION')} a ON a.RULE_ID = r.RULE_ID
                  WHERE rs.TARGET_OBJECT_ID = c.OBJECT_ID
                    AND UPPER(a.TARGET_FIELD) = UPPER(c.COLUMN_NAME))
            GROUP BY c.OBJECT_ID, bo.OBJECT_NAME, c.COLUMN_NAME, c.DATA_TYPE, c.ORDINAL
            ORDER BY bo.OBJECT_NAME, c.ORDINAL
        """,
    },
    {
        "key": "objects_no_columns",
        "label": "Objects with no declared columns",
        "severity": "medium",
        "why": ("The object is registered but its structure is undocumented, so it "
                "contributes nothing to the FDD export and cannot be verified against "
                "Snowflake."),
        "sql": lambda: f"""
            SELECT bo.OBJECT_ID, bo.OBJECT_NAME, bo.TECHNICAL_OBJECT_NAME,
                   bo.OBJECT_TYPE, m.MODULE_NAME
            FROM {tbl('BUSINESS_OBJECT')} bo
            LEFT JOIN {tbl('MODULE')} m ON m.MODULE_ID = bo.MODULE_ID
            WHERE NOT EXISTS (
                SELECT 1 FROM {tbl('BUSINESS_OBJECT_COLUMN')} c
                WHERE c.OBJECT_ID = bo.OBJECT_ID AND c.ACTIVE_IND = TRUE)
            ORDER BY m.MODULE_NAME, bo.OBJECT_NAME
        """,
    },
    {
        "key": "rules_no_test_case",
        "label": "Rules with no test case",
        "severity": "medium",
        "why": ("Nothing verifies the rule against real data. Generate test cases from "
                "the Test cases page."),
        "sql": lambda: f"""
            SELECT r.RULE_ID, r.RULE_NUMBER, r.RULE_NAME, r.RULE_CATEGORY,
                   rs.RULE_SET_NAME, m.MODULE_NAME
            FROM {tbl('RULE')} r
            LEFT JOIN {tbl('RULE_SET')} rs ON rs.RULE_SET_ID = r.RULE_SET_ID
            LEFT JOIN {tbl('MODULE')} m ON m.MODULE_ID = rs.MODULE_ID
            WHERE COALESCE(r.RULE_STATUS, '') <> 'RETIRED'
              AND NOT EXISTS (
                  SELECT 1 FROM {tbl('RULE_TEST_CASE')} t WHERE t.RULE_ID = r.RULE_ID)
            ORDER BY m.MODULE_NAME, rs.RULE_SET_NAME, r.RULE_NUMBER
        """,
    },
    {
        "key": "rules_no_description",
        "label": "Rules with no business description",
        "severity": "medium",
        "why": ("No record of *why* the rule exists, only what it does. The compiler "
                "warns about this but does not block saving."),
        "sql": lambda: f"""
            SELECT r.RULE_ID, r.RULE_NUMBER, r.RULE_NAME, r.RULE_CATEGORY,
                   rs.RULE_SET_NAME, m.MODULE_NAME
            FROM {tbl('RULE')} r
            LEFT JOIN {tbl('RULE_SET')} rs ON rs.RULE_SET_ID = r.RULE_SET_ID
            LEFT JOIN {tbl('MODULE')} m ON m.MODULE_ID = rs.MODULE_ID
            WHERE COALESCE(r.RULE_STATUS, '') <> 'RETIRED'
              AND COALESCE(TRIM(r.BUSINESS_DESCRIPTION), '') = ''
            ORDER BY m.MODULE_NAME, rs.RULE_SET_NAME, r.RULE_NUMBER
        """,
    },
    {
        "key": "rule_sets_no_rules",
        "label": "Rule sets with no active rules",
        "severity": "low",
        "why": ("An empty rule set - either a placeholder never filled in, or every "
                "rule in it has been retired."),
        "sql": lambda: f"""
            SELECT rs.RULE_SET_ID, rs.RULE_SET_NAME, m.MODULE_NAME,
                   rs.SOURCE_TECHNICAL_OBJECT, rs.TARGET_TECHNICAL_OBJECT
            FROM {tbl('RULE_SET')} rs
            LEFT JOIN {tbl('MODULE')} m ON m.MODULE_ID = rs.MODULE_ID
            WHERE NOT EXISTS (
                SELECT 1 FROM {tbl('RULE')} r
                WHERE r.RULE_SET_ID = rs.RULE_SET_ID
                  AND COALESCE(r.RULE_STATUS, '') <> 'RETIRED')
            ORDER BY m.MODULE_NAME, rs.RULE_SET_NAME
        """,
    },
    {
        "key": "tests_never_reviewed",
        "label": "Test cases still awaiting review",
        "severity": "low",
        "why": ("Generated but never accepted or rejected. Note that the app saves "
                "ticked cases straight to ACCEPTED, so this is usually zero - it does "
                "not mean every accepted case was read carefully."),
        "sql": lambda: f"""
            SELECT t.TEST_CASE_ID, t.RULE_ID, r.RULE_NUMBER, r.RULE_NAME,
                   t.TEST_NAME, t.TEST_TYPE, t.STATUS
            FROM {tbl('RULE_TEST_CASE')} t
            LEFT JOIN {tbl('RULE')} r ON r.RULE_ID = t.RULE_ID
            WHERE COALESCE(t.STATUS, '') = 'SUGGESTED'
            ORDER BY t.RULE_ID, t.TEST_CASE_ID
        """,
    },
]

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def run_check(check: Dict) -> pd.DataFrame:
    """Offending rows for one check. An empty frame on failure, never an exception."""
    try:
        return cached_read(check["sql"]())
    except Exception:
        return pd.DataFrame()


def run_all() -> List[Dict]:
    """
    Every check, worst severity first, each with its count and offending rows.

    A check that errors reports count 0 with `ok` False and an `errored` flag, so a
    broken query is never mistaken for a clean bill of health.
    """
    results = []
    for check in CHECKS:
        try:
            rows = cached_read(check["sql"]())
            errored = False
        except Exception:
            rows = pd.DataFrame()
            errored = True
        count = 0 if rows is None or rows.empty else len(rows)
        results.append({
            "key": check["key"],
            "label": check["label"],
            "severity": check["severity"],
            "why": check["why"],
            "count": count,
            "rows": rows if rows is not None else pd.DataFrame(),
            "ok": (count == 0 and not errored),
            "errored": errored,
        })
    results.sort(key=lambda r: (SEVERITY_ORDER.get(r["severity"], 9), -r["count"]))
    return results


def id_allocation_warning() -> Dict:
    """
    Whether id allocation has fallen back to the old SELECT MAX(id)+1 race.

    Not a SQL check - it reports what this app session actually did. Non-empty
    means create_id_sequences.sql has not been run, or the sequences are not
    reachable, and concurrent saves can silently collide on ids.
    """
    fallbacks = sequence_fallbacks()
    return {
        "ok": not fallbacks,
        "fallbacks": fallbacks,
        "detail": ("All ids allocated from sequences." if not fallbacks else
                   "Falling back to MAX(id)+1 for: " + ", ".join(sorted(fallbacks))),
    }


def totals() -> Dict:
    """Headline repository counts, for context above the checks."""
    try:
        df = cached_read(f"""
            SELECT
              (SELECT COUNT(*) FROM {tbl('RULE')}
                WHERE COALESCE(RULE_STATUS,'') <> 'RETIRED')      AS ACTIVE_RULES,
              (SELECT COUNT(*) FROM {tbl('RULE')}
                WHERE RULE_STATUS = 'RETIRED')                     AS RETIRED_RULES,
              (SELECT COUNT(*) FROM {tbl('RULE_SET')})             AS RULE_SETS,
              (SELECT COUNT(*) FROM {tbl('BUSINESS_OBJECT')})      AS OBJECTS,
              (SELECT COUNT(*) FROM {tbl('BUSINESS_OBJECT_COLUMN')}
                WHERE ACTIVE_IND = TRUE)                           AS COLUMNS,
              (SELECT COUNT(*) FROM {tbl('MODULE')})               AS MODULES,
              (SELECT COUNT(*) FROM {tbl('RULE_TEST_CASE')})       AS TEST_CASES
        """)
        return {} if df.empty else {k: int(v) for k, v in df.iloc[0].items()}
    except Exception:
        return {}
