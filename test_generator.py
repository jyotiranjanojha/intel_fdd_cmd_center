# Generates functional test cases and DQ checks from rule metadata, plus executable test scripts.
# Co-authored with CoCo
"""
Test case generation for business rules.

Two halves:
  * suggest_test_cases() asks the model for scenarios given a rule's conditions
    and actions. Structured JSON out, so the UI can render Accept/Reject per row
    rather than dumping prose at the user.
  * build_test_sql() turns a suggestion into a violation query against the real
    target object. Written as "count the rows that break the rule" so a passing
    test returns zero - that composes into one UNION ALL pack that can be run in
    a single execution.

Column names are checked against the live object before they reach generated SQL.
A suggestion naming a column that does not exist still shows up in the UI as a
documentation-only case, it just has no runnable SQL attached.
"""

import json
import re
from typing import Dict, List, Optional

from db import FAST_MODEL, WRITER_MODEL, llm_cached

FUNCTIONAL = "FUNCTIONAL"
DQ_CHECK = "DQ_CHECK"

# Operators that appear in RULE_CONDITION.OPERATOR and can be turned into a
# predicate. Anything outside this set leaves the case as a manual check.
# IS_BLANK and STARTS_WITH are this repository's own spellings, not SQL.
COMPARISON_OPERATORS = {"=", "!=", "<>", ">", ">=", "<", "<=", "LIKE", "NOT LIKE"}
NULL_OPERATORS = {"IS NULL", "IS NOT NULL"}
BLANK_OPERATORS = {"IS_BLANK", "IS BLANK"}
PREFIX_OPERATORS = {"STARTS_WITH", "STARTS WITH"}

# ACTION_TYPE values that assert a value on the target column, so a row matching
# the conditions but not the value is a violation.
VALUE_ACTIONS = {"SET_VALUE", "SET", "DERIVE", "ASSIGN", "DEFAULT", "TRANSFORM"}

# Filter semantics: the action decides which rows reach the target rather than
# setting a column. INCLUDE means the target should hold only rows satisfying the
# conditions, so the violation is the negation; EXCLUDE is the other way round.
INCLUDE_ACTIONS = {"INCLUDE_RECORD", "INCLUDE", "INCLUDE_MATCHING"}
EXCLUDE_ACTIONS = {"EXCLUDE_RECORD", "EXCLUDE"}

# Uniqueness semantics.
KEY_ACTIONS = {"DEFINE_KEY", "DEDUPLICATE"}

# ------------------------------------------------------------------
# Metadata-driven rules
# ------------------------------------------------------------------
# Many rules are not self-contained: their conditions name columns of the
# business rule metadata driver rather than the target object, and their actions
# map target columns onto that driver's generic COLUMN_VALUE_*_TXT slots. Testing
# those means joining the target to the driver on the three-part rule key and
# comparing, which is what METADATA_VIEW is for.
METADATA_VIEW = ("IF_SUPPLY_CHAIN.PDH_COMMON_ANALYSIS."
                 "V_CRR_BUSINESS_RULE_METADATA_DETAIL_CORE_PIT")

# The three columns that identify which metadata rows drive a rule.
METADATA_KEY_COLUMNS = ("SUBJECT_AREA_NM", "ENTITY_NM", "BUSINESS_RULE_NM")

# Generic value slots on the driver.
METADATA_VALUE_COLUMNS = tuple(
    f"COLUMN_VALUE_{word}_TXT" for word in (
        "ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT",
        "NINE", "TEN", "ELEVEN", "TWELVE", "THIRTEEN", "FOURTEEN",
        "FIFTEEN", "SIXTEEN",
    )
)

METADATA_COLUMNS = set(METADATA_KEY_COLUMNS) | set(METADATA_VALUE_COLUMNS) | {
    "CHARACTERISTIC_NM", "COMMENT_TXT",
}


# ------------------------------------------------------------------
# Prompt / parsing
# ------------------------------------------------------------------
def _rule_context(proposal: Dict) -> str:
    """Compact text description of a rule for the prompt."""
    lines = [
        f"Rule name: {proposal.get('rule_name')}",
        f"Category: {proposal.get('rule_category')}",
        f"Description: {proposal.get('business_description')}",
    ]
    conditions = proposal.get("conditions") or []
    if conditions:
        lines.append("Conditions:")
        for c in conditions:
            lines.append(
                f"  - {c.get('field_name')} {c.get('operator')} {c.get('field_value')}"
                f" {c.get('logical_operator') or ''}".rstrip()
            )
    actions = proposal.get("actions") or []
    if actions:
        lines.append("Actions:")
        for a in actions:
            lines.append(
                f"  - {a.get('action_type')} {a.get('target_field')} = {a.get('action_value')}"
            )
    joins = proposal.get("joins") or []
    if joins:
        lines.append("Joins:")
        for j in joins:
            lines.append(
                f"  - {j.get('join_type')} {j.get('left_object')} to {j.get('right_object')}"
            )
    return "\n".join(lines)


def _parse_json_list(raw: str) -> List[Dict]:
    """
    Pull a JSON array out of a model response.

    The model is asked for bare JSON but sometimes wraps it in a fence or adds a
    sentence before it, so the array is located rather than assumed.
    """
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []

    cases = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        test_type = str(item.get("test_type", "")).upper().strip()
        if test_type not in (FUNCTIONAL, DQ_CHECK):
            test_type = FUNCTIONAL
        name = str(item.get("test_name", "")).strip()
        if not name:
            continue
        cases.append({
            "test_type": test_type,
            "test_name": name[:500],
            "scenario": str(item.get("scenario", "")).strip()[:4000],
            "expected_result": str(item.get("expected_result", "")).strip()[:4000],
        })
    return cases


def suggest_test_cases(proposal: Dict, target_object: Optional[str] = None,
                       previous: Optional[Dict] = None,
                       model: str = FAST_MODEL) -> List[Dict]:
    """
    Ask the model for test cases covering a rule.

    `previous` turns this into an update review: the model is shown the before
    and after so it proposes cases for what actually changed instead of
    re-proposing the whole original suite.
    """
    context = _rule_context(proposal)

    if previous is not None:
        intro = (
            "A business rule has been UPDATED. Suggest test cases that cover the change.\n\n"
            f"BEFORE:\n{_rule_context(previous)}\n\nAFTER:\n{context}"
        )
    else:
        intro = f"A new business rule was written. Suggest test cases for it.\n\n{context}"

    target_hint = f"\nThe rule writes to: {target_object}" if target_object else ""

    prompt = f"""{intro}{target_hint}

Return a JSON array only. No prose, no markdown fences.

Each element:
{{
  "test_type": "FUNCTIONAL" or "DQ_CHECK",
  "test_name": "short name, under 80 characters",
  "scenario": "the setup and the step being tested",
  "expected_result": "what must be true for the test to pass"
}}

Guidance:
- Give 1 to 2 FUNCTIONAL cases: one positive path where the conditions are met,
  and optionally one negative path where they are not met.
- Give 1 DQ_CHECK case: an assertion that should hold over the whole table,
  phrased as a condition that must never occur.
- CRITICAL: Only suggest cases that can be expressed as a single SQL violation
  query against the target object. Do not suggest manual checks, cases requiring
  external data, or cases that cannot be verified with a SQL SELECT.
- Only reference columns named in the rule above. Do not invent column names.
- Be specific about values. "ITEM_STATUS = 'BLOCKED'" not "a blocked status".
"""
    try:
        return _parse_json_list(llm_cached(prompt, model))
    except Exception:
        return []


# ------------------------------------------------------------------
# Executable SQL
# ------------------------------------------------------------------
def _literal(value, text_only: bool = False) -> str:
    """
    Render a condition value as a SQL literal.

    Numbers and the boolean keywords stay bare, everything else is quoted. Rule
    values arrive as strings from the metadata tables, so "0" has to be
    recognised as numeric and "FALSE" as a boolean here, or the generated
    predicate compares a number or boolean column against a string.

    text_only forces quoting. The driver's COLUMN_VALUE_*_TXT slots are all TEXT,
    so emitting a bare 1 there would make Snowflake cast the whole column to a
    number and fail on the first non-numeric value in it.
    """
    text = str(value if value is not None else "").strip()
    if text == "":
        return "NULL"
    if not text_only:
        upper = text.upper()
        if upper in ("TRUE", "FALSE", "NULL"):
            return upper
        if re.fullmatch(r"-?\d+(\.\d+)?", text):
            return text
    escaped = text.replace("'", "''")
    return f"'{escaped}'"


def _is_literal_value(value) -> bool:
    """
    Whether an ACTION_VALUE is a concrete value rather than prose.

    DERIVE and TRANSFORM rows often carry a sentence describing the derivation
    ("concatenate the item and the plant, left padded"). Comparing a column to
    that sentence would produce a query that runs and is meaningless, which is
    worse than producing no query, so anything sentence-shaped is rejected.
    """
    text = str(value if value is not None else "").strip()
    if not text or len(text) > 60:
        return False
    if text.upper() in ("TRUE", "FALSE", "NULL"):
        return True
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        return True
    # Expression or prose markers: operators, punctuation, or several words.
    if re.search(r"[()\[\]{}+*/|=<>,;:]", text):
        return False
    if len(text.split()) > 3:
        return False
    return True


def _predicate(column: str, operator: str, value, text_only: bool = False) -> Optional[str]:
    """
    A predicate matching the rows the condition describes, or None when the
    operator has no mechanical equivalent.

    text_only is set when the column is known to be TEXT, which is the case for
    every metadata driver slot.
    """
    op = str(operator or "").strip().upper()

    if op in NULL_OPERATORS:
        return f"{column} {op}"
    if op in BLANK_OPERATORS:
        # "Blank" in this repository means null or empty string, not just null.
        return f"({column} IS NULL OR TRIM({column}) = '')"
    if op in PREFIX_OPERATORS:
        prefix = str(value if value is not None else "").strip().replace("'", "''")
        if not prefix:
            return None
        return f"{column} LIKE '{prefix}%'"
    if op in COMPARISON_OPERATORS:
        return f"{column} {op} {_literal(value, text_only=text_only)}"
    return None


def _violation_predicate(column: str, value) -> str:
    """
    The rows where the target column does not hold the asserted value.

    NULL is handled explicitly: `column <> 'X'` is unknown, not true, for a null
    column, so a row that was never populated would silently pass.
    """
    literal = _literal(value)
    if literal == "NULL":
        return f"{column} IS NOT NULL"
    return f"({column} IS NULL OR {column} <> {literal})"


def build_test_sql(test_case: Dict, proposal: Dict, target_object: Optional[str],
                   available_columns: Optional[set] = None,
                   with_reason: bool = False):
    """
    Build a violation query for a rule.

    The query counts rows that break the rule, so zero rows means PASS.

    Returns the SQL string, or None when the rule cannot be expressed
    mechanically. With with_reason=True returns (sql, reason) instead, where
    reason explains the None so the UI can tell the user why a case is
    documentation-only rather than just showing "No".

    Four shapes are supported, picked from the action semantics:
      value   - rows meeting the conditions must hold an asserted column value
      include - the target must hold only rows meeting the conditions
      exclude - the target must hold no rows meeting the conditions
      key     - the named columns must be unique
    """
    def out(sql, reason=None):
        return (sql, reason) if with_reason else sql

    if not target_object:
        return out(None, "no target object on the rule")

    conditions = proposal.get("conditions") or []
    actions = proposal.get("actions") or []
    if not actions:
        return out(None, "the rule has no actions")

    columns = {c.upper() for c in (available_columns or set())}

    def known(col: str) -> bool:
        return not columns or str(col or "").upper() in columns

    def missing(cols) -> List[str]:
        return [c for c in cols if c and not known(c)]

    action_types = {str(a.get("action_type") or "").strip().upper() for a in actions}

    # ---- uniqueness ------------------------------------------------
    if action_types & KEY_ACTIONS:
        key_columns = [str(a.get("target_field") or "").strip()
                       for a in actions
                       if str(a.get("action_type") or "").strip().upper() in KEY_ACTIONS]
        key_columns = [c for c in key_columns if c]
        gone = missing(key_columns)
        if gone:
            return out(None, f"key column(s) not on the target: {', '.join(gone)}")
        if not key_columns:
            return out(None, "no key column named on the rule")
        key_list = ", ".join(key_columns)
        return out(
            f"SELECT CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS RESULT,\n"
            f"       COUNT(*) AS VIOLATING_ROWS\n"
            f"FROM (\n"
            f"    SELECT {key_list}\n"
            f"    FROM {target_object}\n"
            f"    GROUP BY {key_list}\n"
            f"    HAVING COUNT(*) > 1\n"
            f")"
        )

    # ---- condition predicates, shared by the remaining shapes ------
    condition_columns = [str(c.get("field_name") or "").strip() for c in conditions]
    gone = missing(condition_columns)
    if gone:
        # Frequently the condition names columns of the metadata driver table
        # rather than the target, which cannot be checked against the target.
        return out(None, f"condition column(s) not on the target: {', '.join(sorted(set(gone)))}")

    when_parts = []
    for c in conditions:
        col = str(c.get("field_name") or "").strip()
        if not col:
            return out(None, "a condition has no column name")
        pred = _predicate(col, c.get("operator"), c.get("field_value"))
        if not pred:
            return out(None, f"operator not supported mechanically: {c.get('operator')}")
        when_parts.append(pred)

    # ---- include / exclude ----------------------------------------
    if action_types & (INCLUDE_ACTIONS | EXCLUDE_ACTIONS):
        if not when_parts:
            return out(None, "a filter rule needs conditions to check against")
        scope = " AND ".join(when_parts)
        if action_types & INCLUDE_ACTIONS:
            # Only matching rows should be present, so anything else is a violation.
            return out(
                f"SELECT CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS RESULT,\n"
                f"       COUNT(*) AS VIOLATING_ROWS\n"
                f"FROM {target_object}\n"
                f"WHERE NOT ({scope})"
            )
        return out(
            f"SELECT CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS RESULT,\n"
            f"       COUNT(*) AS VIOLATING_ROWS\n"
            f"FROM {target_object}\n"
            f"WHERE {scope}"
        )

    # ---- asserted column values -----------------------------------
    unsupported = action_types - VALUE_ACTIONS
    if unsupported:
        return out(None, "action type describes flow rather than a checkable value: "
                         + ", ".join(sorted(unsupported)))

    action_columns = [str(a.get("target_field") or "").strip() for a in actions]
    gone = missing(action_columns)
    if gone:
        return out(None, f"target column(s) not on the object: {', '.join(sorted(set(gone)))}")

    violation_parts = []
    for a in actions:
        col = str(a.get("target_field") or "").strip()
        if not col:
            return out(None, "an action has no target column")
        value = a.get("action_value")
        if not _is_literal_value(value):
            return out(None, f"action value is an expression, not a literal: "
                             f"{str(value)[:60]}")
        violation_parts.append(_violation_predicate(col, value))
    if not violation_parts:
        return out(None, "nothing assertable on the rule")

    # Any one asserted column being wrong is a violation, hence OR across them.
    violation = (violation_parts[0] if len(violation_parts) == 1
                 else "(" + " OR ".join(violation_parts) + ")")
    where = " AND ".join(when_parts + [violation]) if when_parts else violation

    return out(
        f"SELECT CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS RESULT,\n"
        f"       COUNT(*) AS VIOLATING_ROWS\n"
        f"FROM {target_object}\n"
        f"WHERE {where}"
    )


def _is_metadata_column(name: str) -> bool:
    return str(name or "").strip().upper() in METADATA_COLUMNS


def _metadata_column_in(text) -> Optional[str]:
    """The metadata value column named by an action value, if that is all it is."""
    token = str(text or "").strip().upper()
    return token if token in METADATA_COLUMNS else None


def _parse_record_match(value) -> Optional[tuple]:
    """
    Pull (target_column, metadata_column) out of an INCLUDE_MATCHING value.

    These arrive as a small expression rather than a value, e.g.
    "ITEM_CLASS_NM = COLUMN_VALUE_ONE_TXT".
    """
    match = re.fullmatch(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\s*",
                         str(value or ""))
    if not match:
        return None
    left, right = match.group(1).upper(), match.group(2).upper()
    if _is_metadata_column(right) and not _is_metadata_column(left):
        return left, right
    if _is_metadata_column(left) and not _is_metadata_column(right):
        return right, left
    return None


def build_metadata_test_sql(proposal: Dict, target_object: Optional[str],
                            available_columns: Optional[set] = None,
                            metadata_view: str = METADATA_VIEW,
                            with_reason: bool = False):
    """
    Build a violation query for a rule driven by the business rule metadata.

    The target is joined to the metadata driver, scoped by the rule's three-part
    key (SUBJECT_AREA_NM / ENTITY_NM / BUSINESS_RULE_NM) plus any further
    conditions on metadata columns. Three shapes come out of the action:

      membership  the target column must match one of the driver's values
                  -> NOT EXISTS against the scoped driver rows
      mapping     several target columns are populated from driver columns
                  -> join on the first mapping, compare the rest
      assertion   a target column must hold a literal, or a driver column's
                  value, for the scoped driver rows
                  -> cross join the scoped driver rows and compare

    Comparisons use IS DISTINCT FROM so a null on either side counts as a
    mismatch instead of evaluating to unknown and passing silently.
    """
    def out(sql, reason=None):
        return (sql, reason) if with_reason else sql

    if not target_object:
        return out(None, "no target object on the rule")

    conditions = proposal.get("conditions") or []
    actions = proposal.get("actions") or []
    if not actions:
        return out(None, "the rule has no actions")

    target_cols = {c.upper() for c in (available_columns or set())}

    def on_target(col: str) -> bool:
        return not target_cols or str(col or "").upper() in target_cols

    meta_conditions = [c for c in conditions if _is_metadata_column(c.get("field_name"))]
    target_conditions = [c for c in conditions if not _is_metadata_column(c.get("field_name"))]

    if not meta_conditions:
        return out(None, "the rule does not reference metadata columns")

    # Scope on the driver. Without at least one key column the join would span
    # every rule's metadata, so refuse rather than produce a misleading count.
    scope_parts = []
    for c in meta_conditions:
        col = str(c.get("field_name") or "").strip().upper()
        pred = _predicate(f"m.{col}", c.get("operator"), c.get("field_value"),
                          text_only=True)
        if not pred:
            return out(None, f"metadata condition operator not supported: {c.get('operator')}")
        scope_parts.append(pred)
    named_keys = {str(c.get("field_name") or "").strip().upper() for c in meta_conditions}
    if not named_keys & set(METADATA_KEY_COLUMNS):
        return out(None, "no metadata key column (subject area / entity / business rule) "
                         "to scope the join")
    scope = "\n      AND ".join(scope_parts)

    # Extra predicates on the target itself, applied to the driving rows.
    where_target = []
    for c in target_conditions:
        col = str(c.get("field_name") or "").strip()
        if not on_target(col):
            return out(None, f"condition column not on the target: {col}")
        pred = _predicate(f"t.{col}", c.get("operator"), c.get("field_value"))
        if not pred:
            return out(None, f"operator not supported mechanically: {c.get('operator')}")
        where_target.append(pred)

    # ---- membership: INCLUDE_MATCHING carrying "COL = META_COL" ----
    for a in actions:
        action_type = str(a.get("action_type") or "").strip().upper()
        if action_type not in INCLUDE_ACTIONS:
            continue
        pair = _parse_record_match(a.get("action_value"))
        if not pair:
            continue
        target_col, meta_col = pair
        if not on_target(target_col):
            return out(None, f"matched column not on the target: {target_col}")
        prefix = (" AND ".join(where_target) + "\n  AND ") if where_target else ""
        return out(
            f"SELECT CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS RESULT,\n"
            f"       COUNT(*) AS VIOLATING_ROWS\n"
            f"FROM {target_object} t\n"
            f"WHERE {prefix}NOT EXISTS (\n"
            f"    SELECT 1\n"
            f"    FROM {metadata_view} m\n"
            f"    WHERE {scope}\n"
            f"      AND t.{target_col} = m.{meta_col}\n"
            f")"
        )

    # ---- mapping: target columns populated from driver columns ----
    mappings = []
    literals = []
    for a in actions:
        col = str(a.get("target_field") or "").strip()
        if not col:
            return out(None, "an action has no target column")
        meta_col = _metadata_column_in(a.get("action_value"))
        if meta_col:
            if not on_target(col):
                return out(None, f"mapped column not on the target: {col}")
            mappings.append((col.upper(), meta_col))
        elif _is_literal_value(a.get("action_value")):
            if not on_target(col):
                return out(None, f"target column not on the object: {col}")
            literals.append((col.upper(), a.get("action_value")))
        else:
            return out(None, "action value is neither a metadata column nor a literal: "
                             f"{str(a.get('action_value'))[:60]}")

    if mappings:
        if len(mappings) == 1 and not literals:
            # A single mapping states only that the value comes from the driver,
            # which is a membership check rather than a row-for-row comparison.
            target_col, meta_col = mappings[0]
            prefix = (" AND ".join(where_target) + "\n  AND ") if where_target else ""
            return out(
                f"SELECT CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS RESULT,\n"
                f"       COUNT(*) AS VIOLATING_ROWS\n"
                f"FROM {target_object} t\n"
                f"WHERE {prefix}NOT EXISTS (\n"
                f"    SELECT 1\n"
                f"    FROM {metadata_view} m\n"
                f"    WHERE {scope}\n"
                f"      AND t.{target_col} = m.{meta_col}\n"
                f")"
            )

        # The first mapping is taken as the join key and the rest are compared.
        # That is an assumption: the rule metadata does not mark which mapping is
        # the key, and the first is the one that carries the identifier in every
        # rule seen so far.
        key_col, key_meta = mappings[0]
        compares = [f"t.{c} IS DISTINCT FROM m.{m}" for c, m in mappings[1:]]
        compares += [f"t.{c} IS DISTINCT FROM {_literal(v)}" for c, v in literals]
        if not compares:
            return out(None, "only a join key was mapped, nothing left to compare")
        mismatch = "\n       OR ".join(compares)
        extra = ("\n  AND " + " AND ".join(where_target)) if where_target else ""
        return out(
            f"SELECT CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS RESULT,\n"
            f"       COUNT(*) AS VIOLATING_ROWS\n"
            f"FROM {target_object} t\n"
            f"JOIN {metadata_view} m\n"
            f"  ON  {scope}\n"
            f"  AND t.{key_col} = m.{key_meta}{extra}\n"
            f"WHERE {mismatch}"
        )

    # ---- assertion with no linking column: not checkable -----------
    # A rule whose conditions only scope the metadata driver, and whose action
    # asserts a plain literal, does not say which target rows the driver rows
    # apply to. Cross joining the two was tried and is unsound on both counts:
    # it asserts the rule against every target row, and it multiplies the
    # violation count by the number of driver rows in scope. Rule 3A produced
    # 6601 "violations" that way - 161 genuinely non-matching rows times 41
    # driver rows - which is worse than reporting nothing. So it stays manual.
    if literals:
        return out(None,
                   "the rule scopes metadata rows but does not say which target rows "
                   "they apply to, so there is no sound join key. Add a mapping action "
                   "linking a target column to a metadata column to make this checkable")

    return out(None, "no checkable action on the metadata rule")


def build_any_test_sql(proposal: Dict, target_object: Optional[str],
                       available_columns: Optional[set] = None,
                       metadata_view: str = METADATA_VIEW,
                       with_reason: bool = False):
    """
    Build a violation query, choosing the self-contained or the metadata-joined
    form depending on whether the rule's conditions reference metadata columns.

    This is the entry point the app should call; build_test_sql and
    build_metadata_test_sql remain available for the two shapes individually.
    """
    conditions = proposal.get("conditions") or []
    uses_metadata = any(_is_metadata_column(c.get("field_name")) for c in conditions)
    if not uses_metadata:
        actions = proposal.get("actions") or []
        uses_metadata = any(_metadata_column_in(a.get("action_value"))
                            or _parse_record_match(a.get("action_value"))
                            for a in actions)

    if uses_metadata:
        return build_metadata_test_sql(proposal, target_object, available_columns,
                                       metadata_view, with_reason=with_reason)
    return build_test_sql({}, proposal, target_object, available_columns,
                          with_reason=with_reason)


def test_id(rule_number, index: int) -> str:
    """Stable, readable id for a case within a rule, e.g. TC-12-01."""
    number = re.sub(r"[^A-Za-z0-9]", "", str(rule_number or "X"))
    return f"TC-{number}-{index + 1:02d}"


# ------------------------------------------------------------------
# Test pack assembly
# ------------------------------------------------------------------
def build_test_pack(entries: List[Dict], title: str = "RULE REPOSITORY TEST PACK") -> str:
    """
    Assemble one runnable .sql pack from many test cases.

    entries: [{rule_number, rule_name, target_object, test_id, test_type,
               test_name, scenario, expected_result, test_sql}]

    Runnable cases are unioned into a single statement returning one row per
    test, so the whole pack is one execution rather than N. Cases without SQL are
    written out as comments in the same file so the pack stays a complete record
    of what was meant to be tested, not just the automatable subset.
    """
    from datetime import datetime

    runnable = [e for e in entries if e.get("test_sql")]
    manual = [e for e in entries if not e.get("test_sql")]

    header = [
        "/*",
        "=" * 78,
        f" {title}",
        f" Generated : {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f" Test cases: {len(entries)} total, {len(runnable)} runnable, {len(manual)} manual",
        "=" * 78,
        "*/",
        "",
    ]

    body = []
    if runnable:
        body += ["-- " + "=" * 74,
                 "-- AUTOMATED CHECKS - one row per test case, RESULT is PASS or FAIL",
                 "-- " + "=" * 74,
                 ""]
        blocks = []
        for e in runnable:
            inner = e["test_sql"].strip().rstrip(";")
            name = str(e.get("test_name", "")).replace("'", "''")
            rule_no = str(e.get("rule_number", "")).replace("'", "''")
            blocks.append(
                f"-- {e['test_id']} [{e['test_type']}] {e.get('test_name', '')}\n"
                f"SELECT '{e['test_id']}' AS TEST_ID,\n"
                f"       '{rule_no}' AS RULE_NUMBER,\n"
                f"       '{e['test_type']}' AS TEST_TYPE,\n"
                f"       '{name}' AS TEST_NAME,\n"
                f"       t.RESULT,\n"
                f"       t.VIOLATING_ROWS\n"
                f"FROM ({inner}) t"
            )
        body.append("\nUNION ALL\n\n".join(blocks) + "\nORDER BY TEST_ID;")
        body.append("")

    if manual:
        body += ["", "-- " + "=" * 74,
                 "-- MANUAL CHECKS - could not be expressed as a single query",
                 "-- " + "=" * 74,
                 ""]
        for e in manual:
            body += [
                f"-- {e['test_id']} [{e['test_type']}] {e.get('test_name', '')}",
                f"--   Rule      : {e.get('rule_number')} {e.get('rule_name')}",
                f"--   Object    : {e.get('target_object') or 'n/a'}",
                f"--   Scenario  : {e.get('scenario') or ''}",
                f"--   Expected  : {e.get('expected_result') or ''}",
                f"--   Not auto  : {e.get('not_runnable_reason') or 'not expressible as a single query'}",
                "",
            ]

    return "\n".join(header + body)
