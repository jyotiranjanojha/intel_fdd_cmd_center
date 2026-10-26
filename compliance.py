# Compares FDD rules against the real Snowflake objects developers built:
# structural (do the named columns exist), data-level (does the real data
# actually satisfy the rule), and AI-assisted (does the real DDL look
# consistent with the rule's description).
# Co-authored with CoCo
"""
Answers "does the developed code match the requirement?" for a rule.

Three checks, cheapest and most trustworthy first:
  1. Structural - do the source/target objects exist, and do the columns the
     rule names actually exist on them right now? Fully mechanical.
  2. Data - for simple filter conditions, how many rows in the real target
     violate what the rule claims? Fully mechanical, but only covers
     conditions with a single column and a plain operator.
  3. AI-assisted - does the target object's actual DDL look consistent with
     the rule's business description? A judgment call, not a guarantee -
     flag it to a human, don't treat it as a pass/fail gate.
"""

import json
from typing import Dict, List, Optional

import streamlit as st

import as_written as aw
from db import FAST_MODEL, WRITER_MODEL, ident, llm_cached, run_query, sql_escape, sql_value

SIMPLE_OPERATORS = {"=", "!=", ">", ">=", "<", "<=", "LIKE", "NOT LIKE", "IS NULL", "IS NOT NULL"}

# Columns that live on the metadata driver view, not on source/target objects.
# Rules referencing these are metadata-driven; the structural check should not
# flag them as missing from the actual business objects.
METADATA_COLUMNS = {
    "SUBJECT_AREA_NM", "ENTITY_NM", "BUSINESS_RULE_NM",
    "CHARACTERISTIC_NM", "COMMENT_TXT",
} | {f"COLUMN_VALUE_{w}_TXT" for w in (
    "ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT",
    "NINE", "TEN", "ELEVEN", "TWELVE", "THIRTEEN", "FOURTEEN",
    "FIFTEEN", "SIXTEEN",
)}

# Stand-ins RULE_ACTION uses instead of naming a real column: RECORD for a
# row-level action, KEY_COLUMNS for a key declaration, DUMMY_COMPONENT for the
# derived dummy BOM component. None of them is a column, so none should be
# reported as absent from the object.
PSEUDO_COLUMNS = {"RECORD", "ROW", "KEY_COLUMNS", "DUMMY_COMPONENT"}


# ------------------------------------------------------------------
# Object / column lookups (spans every database and schema, not just
# IF_SUPPLY_CHAIN - the source objects live in IF_MASTER_DATA too).
# ------------------------------------------------------------------
def _split(technical_object: str):
    parts = [p for p in str(technical_object or "").strip().split(".") if p]
    return tuple(parts) if len(parts) == 3 else None


def _short(technical_object: str) -> str:
    """Just the object name, for findings notes - the full path is already in the row."""
    name = str(technical_object or "").strip()
    return name.split(".")[-1] if name else "the target object"


def _physical(technical_object: str) -> str:
    """
    The name as it exists in this account. The FDD records objects under their
    production database (SUPPLY_CHAIN, MASTER_DATA) while this account holds the
    IF_-prefixed twins, so a recorded name has to be translated before it is
    queried.
    """
    try:
        from catalog import resolve_physical_name
        resolved = resolve_physical_name(technical_object)
        return resolved or str(technical_object or "")
    except Exception:
        return str(technical_object or "")


@st.cache_data(ttl=300, show_spinner=False)
def object_exists(technical_object: str) -> bool:
    parts = _split(technical_object)
    if not parts:
        return False
    # Try the name as recorded first, then its physical translation, so an object
    # already recorded correctly costs no extra round-trip.
    for candidate in (str(technical_object), _physical(technical_object)):
        try:
            run_query(f"SELECT 1 FROM {candidate} LIMIT 0")
            return True
        except Exception:
            continue
    return False


@st.cache_data(ttl=300, show_spinner=False)
def get_columns(technical_object: str) -> set:
    parts = _split(technical_object)
    if not parts:
        return set()
    candidates = [parts]
    physical = _split(_physical(technical_object))
    if physical and physical != parts:
        candidates.append(physical)
    for db, schema, name in candidates:
        try:
            df = run_query(f"""
                SELECT COLUMN_NAME FROM {ident(db)}.INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = '{sql_escape(schema.upper())}'
                  AND TABLE_NAME = '{sql_escape(name.upper())}'
            """)
            columns = {str(c).upper() for c in df["COLUMN_NAME"].tolist()}
            if columns:
                return columns
        except Exception:
            continue
    return set()


@st.cache_data(ttl=300, show_spinner=False)
def get_ddl(technical_object: str) -> Optional[str]:
    """Tries VIEW then TABLE - GET_DDL needs the right kind or it errors."""
    if not _split(technical_object):
        return None
    for kind in ("VIEW", "TABLE"):
        try:
            df = run_query(f"SELECT GET_DDL('{kind}', '{sql_escape(technical_object)}') AS DDL")
            return str(df.iloc[0]["DDL"])
        except Exception:
            continue
    return None


# ------------------------------------------------------------------
# 1. Structural check
# ------------------------------------------------------------------
def structural_check(bundle: Dict, source_objects: List[str], target_object: str) -> List[Dict]:
    """
    One row per object/column the rule references, with whether it exists today.

    Three statuses, and the difference matters when reading the result:
      OK       - the object is built and the column is on it.
      MISSING  - the object is built, but the column is not there. A real gap.
      UNKNOWN  - the object is not built yet, so its columns cannot be read.
                 Not a gap, just unverifiable. Every finding carries a `note`
                 saying which of these applies, because a column list full of
                 bare UNKNOWNs otherwise looks like a fault in the checker.
    """
    findings = []

    target_object = (target_object or "").strip()
    target_exists = object_exists(target_object) if target_object else False
    not_built = "Documented in the FDD; not present in Snowflake yet."
    if target_object:
        findings.append({"check": "Target object exists", "object": target_object, "field": "",
                         "status": "OK" if target_exists else "MISSING",
                         "note": "" if target_exists else not_built})
    target_columns = get_columns(target_object) if target_exists else set()
    cannot_verify = f"Cannot verify: {_short(target_object)} does not exist yet."

    for action in bundle.get("actions", []):
        field = str(action.get("TARGET_FIELD") or "").upper()
        if not field or field in PSEUDO_COLUMNS:
            continue
        if field in target_columns:
            status, note = "OK", ""
        elif target_exists:
            status, note = "MISSING", f"Not a column of {_short(target_object)}."
        else:
            status, note = "UNKNOWN", cannot_verify
        findings.append({"check": "Action target field", "object": target_object,
                         "field": field, "status": status, "note": note})

    checked_sources = []
    source_columns = set()
    for src in source_objects:
        src = (src or "").strip()
        if not src:
            continue
        exists = object_exists(src)
        checked_sources.append((src, exists))
        findings.append({"check": "Source object exists", "object": src, "field": "",
                         "status": "OK" if exists else "MISSING",
                         "note": "" if exists else not_built})
        if exists:
            source_columns |= get_columns(src)

    # Resolve every join object once. Conditions can reference columns that live
    # on a joined object - metadata tables are cross joined in to supply filters
    # and defaults - so the condition check below needs these columns too. The
    # join loop further down reuses this map instead of re-querying, which
    # matters because one object (typically the metadata view) is commonly
    # joined more than once in the same rule.
    join_objs = {}
    for join in bundle.get("joins", []):
        j = join.get("join", {})
        for obj in (j.get("LEFT_OBJECT"), j.get("RIGHT_OBJECT")):
            obj = (obj or "").strip()
            if not obj or obj in join_objs:
                continue
            exists = object_exists(obj)
            join_objs[obj] = get_columns(obj) if exists else set()
            findings.append({"check": "Join object exists", "object": obj, "field": "",
                             "status": "OK" if exists else "MISSING",
                             "note": "" if exists else not_built})

    join_columns = set()
    for cols in join_objs.values():
        join_columns |= cols

    for cond in bundle.get("conditions", []):
        field = str(cond.get("FIELD_NAME") or "").upper()
        if not field:
            continue
        # Metadata-driven columns live on the rule metadata view, not on
        # source/target objects — skip them to avoid false MISSING gaps.
        if field in METADATA_COLUMNS:
            continue
        known = source_columns | target_columns | join_columns
        if not known:
            status, note = "UNKNOWN", "Cannot verify: none of this rule's objects exist yet."
        elif field in known:
            status, note = "OK", ""
        else:
            status, note = "MISSING", "Not a column of any source, target or joined object."
        findings.append({"check": "Condition field", "object": " / ".join(s for s, _ in checked_sources) or target_object,
                         "field": field, "status": status, "note": note})

    for join in bundle.get("joins", []):
        j = join.get("join", {})
        left_obj = (j.get("LEFT_OBJECT") or "").strip()
        right_obj = (j.get("RIGHT_OBJECT") or "").strip()
        for col in join.get("columns", []):
            for side_key, obj in (("LEFT_COLUMN", left_obj), ("RIGHT_COLUMN", right_obj)):
                field = str(col.get(side_key) or "").upper()
                if field and obj:
                    cols = join_objs.get(obj, set())
                    if not cols:
                        status, note = "UNKNOWN", f"Cannot verify: {_short(obj)} does not exist yet."
                    elif field in cols:
                        status, note = "OK", ""
                    else:
                        status, note = "MISSING", f"Not a column of {_short(obj)}."
                    findings.append({"check": "Join column", "object": obj, "field": field,
                                     "status": status, "note": note})

    return findings


# ------------------------------------------------------------------
# 2. Data-level check
# ------------------------------------------------------------------
def _condition_sql(field: str, operator: str, value) -> Optional[str]:
    col = ident(field)
    op = (operator or "").upper().strip()
    if op not in SIMPLE_OPERATORS:
        return None  # BETWEEN, IN, CONTAINS, STARTS WITH need multi-value handling - skip for now
    if op in ("IS NULL", "IS NOT NULL"):
        return f"{col} {op}"
    return f"{col} {op} {sql_value(value)}"


def data_level_check(bundle: Dict, target_object: str) -> List[Dict]:
    """
    For each filter condition whose column exists on the real target, counts
    how many actual rows violate it. NULLs in the checked column are not
    counted as violations unless the rule itself checks IS NULL/IS NOT NULL -
    that is a known blind spot of three-valued SQL logic, not a bug.
    """
    findings = []
    target_object = (target_object or "").strip()
    if not target_object or not object_exists(target_object):
        return findings

    target_columns = get_columns(target_object)
    try:
        total = int(run_query(f"SELECT COUNT(*) AS N FROM {target_object}").iloc[0]["N"])
    except Exception:
        return findings

    for cond in bundle.get("conditions", []):
        field = str(cond.get("FIELD_NAME") or "").upper()
        if field not in target_columns:
            continue
        where = _condition_sql(field, cond.get("OPERATOR"), cond.get("FIELD_VALUE"))
        if not where:
            continue
        try:
            violations = int(run_query(
                f"SELECT COUNT(*) AS N FROM {target_object} WHERE NOT ({where})"
            ).iloc[0]["N"])
        except Exception:
            continue
        findings.append({
            "condition": cond.get("CONDITION_DESCRIPTION") or f"{field} {cond.get('OPERATOR')} {cond.get('FIELD_VALUE')}",
            "field": field, "total_rows": total, "violating_rows": violations,
            "status": "OK" if violations == 0 else "MISMATCH",
        })
    return findings


# ------------------------------------------------------------------
# 3. AI-assisted logic review
# ------------------------------------------------------------------
def ai_logic_review(bundle: Dict, target_object: str, model: str = FAST_MODEL) -> Dict:
    rule = bundle.get("rule", {})
    ddl = get_ddl(target_object) if target_object else None
    if not ddl:
        return {"verdict": "NOT_CHECKED",
                "explanation": "Could not retrieve DDL for the target object (it may be a base table "
                                "loaded by an external process, not a view)."}

    rule_summary = {
        "name": rule.get("RULE_NAME"),
        "category": rule.get("RULE_CATEGORY"),
        "description": rule.get("BUSINESS_DESCRIPTION"),
        "conditions": [{"field": c.get("FIELD_NAME"), "operator": c.get("OPERATOR"), "value": c.get("FIELD_VALUE")}
                       for c in bundle.get("conditions", [])],
        "actions": [{"target": a.get("TARGET_FIELD"), "type": a.get("ACTION_TYPE"), "value": a.get("ACTION_VALUE")}
                    for a in bundle.get("actions", [])],
    }

    prompt = f"""You review whether a deployed Snowflake object's DDL matches a documented business rule from an FDD.
Return JSON only, no prose, no markdown fences: {{"verdict": "MATCH" | "PARTIAL" | "MISMATCH" | "UNCLEAR", "explanation": "one or two sentences"}}

- MATCH: the DDL logic clearly implements what the rule describes.
- PARTIAL: some of the rule is implemented, some is missing or different.
- MISMATCH: the DDL contradicts or does not implement the rule.
- UNCLEAR: the DDL doesn't give enough information to judge (e.g. a plain passthrough).

Business rule (from the FDD):
{json.dumps(rule_summary, default=str)}

Actual object DDL:
{ddl[:6000]}
"""
    raw = llm_cached(prompt, model)
    try:
        start, end = raw.index("{"), raw.rindex("}") + 1
        parsed = json.loads(raw[start:end])
        verdict = str(parsed.get("verdict", "UNCLEAR")).upper()
        if verdict not in ("MATCH", "PARTIAL", "MISMATCH", "UNCLEAR"):
            verdict = "UNCLEAR"
        return {"verdict": verdict, "explanation": str(parsed.get("explanation", "")).strip()}
    except Exception:
        return {"verdict": "UNCLEAR", "explanation": raw[:300]}


# ------------------------------------------------------------------
# 4. Rules saved as written (as_written.py) - advisory only
# ------------------------------------------------------------------
def as_written_review(bundle: Dict, target_object: str, model: str = FAST_MODEL) -> Dict:
    """Does the deployed code implement this sentence? A reading, not a test."""
    rule = bundle.get("rule", {})
    ddl = get_ddl(target_object) if target_object else None
    if not ddl:
        return {"verdict": "NOT_CHECKED",
                "explanation": "No code to read for the target object - it is not built "
                               "yet, or it is a table loaded by another process."}
    sentence = str(rule.get("BUSINESS_DESCRIPTION") or "").strip()
    prompt = f"""A business rule was written in plain English in an FDD. It may contain logic
(filters, conditions, priorities, joins) stated in words rather than column terms.
Read the deployed Snowflake object's DDL and judge whether it implements what the
sentence asks. Return JSON only: {{"verdict": "MATCH" | "PARTIAL" | "MISMATCH" | "UNCLEAR", "explanation": "one or two sentences naming the part of the code you relied on"}}

- MATCH: the code clearly does what the sentence says.
- PARTIAL: some of it is there, some is missing or different.
- MISMATCH: you cannot find it in the code, or the code does something else.
- UNCLEAR: the code does not give enough to judge.

Rule (as written): {sentence[:3000]}

DDL:
{ddl[:6000]}
"""
    raw = llm_cached(prompt, model)
    try:
        start, end = raw.index("{"), raw.rindex("}") + 1
        parsed = json.loads(raw[start:end])
        verdict = str(parsed.get("verdict", "UNCLEAR")).upper()
        if verdict not in ("MATCH", "PARTIAL", "MISMATCH", "UNCLEAR"):
            verdict = "UNCLEAR"
        return {"verdict": verdict, "explanation": str(parsed.get("explanation", "")).strip()}
    except Exception:
        return {"verdict": "UNCLEAR", "explanation": raw[:300]}


def as_written_check(bundle: Dict, source_objects: List[str], target_object: str,
                     run_ai: bool = True, model: str = WRITER_MODEL) -> Dict:
    """Verify an as-written rule without failing it.

    Objects and the columns the sentence mentions are looked up, but a column that is
    not found is a NOTE - the sentence may name it loosely. There are no conditions,
    so no data checks. The AI reading always runs, and nothing here counts as a gap:
    the result is advisory, for whoever reviews the rule.
    """
    rule = bundle.get("rule", {})
    sentence = str(rule.get("BUSINESS_DESCRIPTION") or "")
    target_object = (target_object or "").strip()
    findings = []
    target_exists = object_exists(target_object) if target_object else False
    if target_object:
        findings.append({"check": "Target object exists", "object": target_object, "field": "",
                         "status": "OK" if target_exists else "UNKNOWN",
                         "note": "" if target_exists else
                         "Documented in the FDD; not present in Snowflake yet."})
    available = set(get_columns(target_object)) if target_exists else set()
    for src in source_objects or []:
        if src and object_exists(src):
            available |= get_columns(src)
    for column in aw.hints_from(sentence)["columns"]:
        if column in PSEUDO_COLUMNS or column in METADATA_COLUMNS:
            continue
        if not available:
            status, note = "UNKNOWN", "No columns could be read to compare with."
        elif column in available:
            status, note = "OK", ""
        else:
            status, note = "NOTE", ("Mentioned in the sentence, not found on the target or "
                                    "its sources - the FDD may name it loosely.")
        findings.append({"check": "Mentioned column", "object": target_object,
                         "field": column, "status": status, "note": note})

    ai = as_written_review(bundle, target_object, model) if run_ai else None
    verdict = (ai or {}).get("verdict") or "NOT_CHECKED"
    return {
        "structural": findings, "data": [], "ai": ai,
        # Advisory: never a gap, whatever the reading says.
        "structural_gaps": 0, "data_gaps": 0,
        "target_missing": bool(target_object) and not target_exists,
        "unverifiable": sum(1 for f in findings if f["status"] == "UNKNOWN"),
        "as_written": True,
        "advisory_verdict": verdict,
        "advisory_label": aw.verdict_label(verdict) if run_ai else "Not read",
    }


# ------------------------------------------------------------------
# Orchestration
# ------------------------------------------------------------------
def run_compliance_check(bundle: Dict, source_objects: List[str], target_object: str,
                         run_ai: bool = True, model: str = WRITER_MODEL) -> Dict:
    if aw.is_as_written(bundle):
        return as_written_check(bundle, source_objects, target_object, run_ai, model)
    structural = structural_check(bundle, source_objects, target_object)
    data = data_level_check(bundle, target_object)

    structural_gaps = sum(1 for f in structural if f["status"] == "MISSING")
    data_gaps = sum(1 for f in data if f["status"] == "MISMATCH")

    # "The object is not built yet" is a different state from "the object is
    # built and does not match". The caller needs to tell them apart, or a rule
    # documenting a not-yet-built object reads as a defect.
    target_missing = any(
        f["check"] == "Target object exists" and f["status"] == "MISSING" for f in structural
    )
    unverifiable = sum(1 for f in structural if f["status"] == "UNKNOWN")

    # Only run the expensive AI review for rules with data violations.
    ai = None
    if run_ai and data_gaps > 0:
        ai = ai_logic_review(bundle, target_object, model)

    return {
        "structural": structural, "data": data, "ai": ai,
        "structural_gaps": structural_gaps, "data_gaps": data_gaps,
        "target_missing": target_missing, "unverifiable": unverifiable,
    }
