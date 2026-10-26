# Adds an LLM-driven "update an existing rule from plain English" flow.
# Co-authored with CoCo
"""
BA writes a sentence -> validated rows for the denormalized tables.

Three stages, in this order, because each one narrows the work of the next:

  1. Deterministic pre-parse. Splits IF / AND / THEN, pulls out comparisons.
     Costs nothing and often gets the whole rule right on its own.
  2. LLM extraction. Gets the pre-parse as a starting point and only the
     handful of column names that are actually relevant, not the whole
     catalog. Small prompt, so a fast response.
  3. Validation and repair. Operators and action types are forced into the
     allowed set, every field name is checked against INFORMATION_SCHEMA, and
     anything unrecognised becomes a visible warning rather than a silent
     insert.

The BA then confirms the rendered English, never the JSON.
"""

import copy
import difflib
import json
import re
from typing import Dict, List, Optional, Tuple

import pandas as pd

from catalog import get_column_catalog, get_object_columns, resolve_field, resolve_object
import rule_extractor as rx
from db import FAST_MODEL, WRITER_MODEL, llm_cached, llm_structured
from fdd_render import render_rule
from repository import (SCALAR_AUDIT_FIELDS, SECTION_AUDIT_FIELDS, content_changed,
                        get_rule_history, status_after_edit)

RULE_CATEGORIES = [
    "FILTER", "ACTIVE_STATUS", "INITIAL_LOAD", "DERIVATION", "METADATA_LOOKUP",
    "METADATA_DERIVATION", "STANDARDIZATION", "AGGREGATION", "JOIN_FILTER",
    "DEDUPLICATION", "NO_TRANSFORMATION", "ATTRIBUTE_CARRY_FORWARD",
    "TIME_TRAVEL", "KEY_DEFINITION",
]

ACTION_TYPES = [
    "SET_VALUE", "SET_NULL", "COPY", "DERIVE", "LOOKUP", "CONCAT", "SUBSTRING",
    "TRIM", "UPPERCASE", "LOWERCASE", "DEFAULT", "EXCLUDE_RECORD",
    "INCLUDE_RECORD", "AGGREGATE", "DEDUPLICATE", "CARRY_FORWARD", "FLAG",
]

OPERATORS = ["=", "!=", ">", ">=", "<", "<=", "IN", "NOT IN", "LIKE", "NOT LIKE",
             "IS NULL", "IS NOT NULL", "BETWEEN", "CONTAINS", "STARTS WITH"]

OPERATOR_SYNONYMS = {
    "EQUALS": "=", "EQUAL": "=", "EQ": "=", "IS": "=", "==": "=",
    "NOT EQUAL": "!=", "NOT EQUALS": "!=", "NE": "!=", "<>": "!=", "IS NOT": "!=",
    "GREATER THAN": ">", "GT": ">", "MORE THAN": ">",
    "GREATER THAN OR EQUAL": ">=", "GE": ">=", "AT LEAST": ">=",
    "LESS THAN": "<", "LT": "<", "AT MOST": "<=", "LESS THAN OR EQUAL": "<=", "LE": "<=",
    "BLANK": "IS NULL", "EMPTY": "IS NULL", "NULL": "IS NULL", "MISSING": "IS NULL",
    "NOT BLANK": "IS NOT NULL", "NOT NULL": "IS NOT NULL", "POPULATED": "IS NOT NULL",
    "ONE OF": "IN", "ANY OF": "IN", "NOT ONE OF": "NOT IN",
    "MATCHES": "LIKE", "STARTS": "STARTS WITH",
}


# ------------------------------------------------------------------
# Stage 1: deterministic pre-parse
# ------------------------------------------------------------------
COMPARISON = re.compile(
    r"(?P<field>[A-Za-z][A-Za-z0-9_ ]{2,40}?)\s*"
    r"(?P<op>>=|<=|!=|<>|=|\bis not blank\b|\bis blank\b|\bis not null\b|\bis null\b|"
    r"\bis not\b|\bis\b|\bequals?\b|\bin\b|\blike\b|\bgreater than\b|\bless than\b|"
    r"\bstarts with\b|\bcontains\b)\s*"
    r"(?P<value>'[^']*'|\"[^\"]*\"|\([^)]*\)|[A-Za-z0-9_.\-%]+)?",
    flags=re.IGNORECASE,
)


# A row filter stated inclusively, as an FDD almost always states one: "filter only
# ACTIVE_IND=TRUE records", "load only active rows", "restrict to current records".
#
# "only"/"just" next to a load verb is what makes the sentence restrictive, and one of
# them is required: without it, "load CLIENT_ID to the target" - an ordinary derivation -
# would be read as a row filter. "restrict to" is spelled out separately because it is
# the one phrasing that carries the same meaning with "to" instead of "only".
RESTRICTIVE_LOAD = re.compile(
    r"\brestrict(?:ed)?\s+to\b"
    r"|\b(?:filter|load|select|include|keep|pull|bring|carry)\b"
    r"(?:\s+\w+){0,3}?\s*\b(?:only|just)\b"
    r"|\b(?:only|just)\b(?:\s+\w+){0,3}?\s*"
    r"\b(?:filter|load|select|include|keep|pull|bring|carry)\b"
    r"|\b(?:filter|load|select|include|keep|pull|bring|carry)\b"
    r"(?:\s+[\w.]+){0,5}?\s*\b(?:where|by\s+filtering)\b",
    flags=re.IGNORECASE,
)


# Words that can sit in front of a column name in a requirement sentence without being
# part of it. COMPARISON lets the field group span spaces, so that "material type code"
# resolves as a phrase; the cost is that re.search is leftmost-first, so in "filter only
# ACTIVE_IND=TRUE" the whole run "filter only ACTIVE_IND" is matched as the field.
#
# That is harmless while the column is in the catalog, because resolve_field scores an
# exact column token at 1.0 and picks ACTIVE_IND out of the phrase on its own. It is not
# harmless when the column is absent from the catalog - a source view in a schema the
# catalog does not cover, which is the normal case for the objects these FDDs read from.
# resolve_field then returns nothing and the caller falls back to the raw phrase, giving
# the condition a field named FILTER_ONLY_ACTIVE_IND. Peeling the filler off the front
# yields ACTIVE_IND either way.
FIELD_FILLER = {
    "a", "all", "an", "and", "any", "as", "be", "bring", "carry", "column", "columns",
    "data", "field", "fields", "filter", "filtered", "filters", "flag", "for", "from",
    "has", "have", "if", "in", "include", "included", "includes", "into", "is", "its",
    "just", "keep", "keeps", "load", "loaded", "loads", "of", "on", "only", "pull",
    "pulls", "record", "records", "restrict", "restricted", "row", "rows", "select",
    "selected", "selects", "set", "that", "the", "their", "then", "these", "this",
    "those", "to", "value", "values", "when", "where", "which", "with",
}


def strip_field_filler(raw: Optional[str]) -> str:
    """Peel leading filler words off a matched field phrase.

    Stops as soon as a non-filler word appears, so a genuine multi-word field name
    survives intact ("material type code"). If every word is filler the phrase is
    returned unchanged rather than emptied - a condition naming nothing is worse than
    one naming something wrong, because validate() reports the bad column name and the
    author can see what happened.
    """
    words = str(raw or "").strip().split()
    kept = list(words)
    while len(kept) > 1 and kept[0].lower().strip(",.") in FIELD_FILLER:
        kept = kept[1:]
    if not kept or (len(kept) == 1 and kept[0].lower().strip(",.") in FIELD_FILLER):
        return " ".join(words)
    return " ".join(kept)


# A column carried across under a new name. FDDs write this several ways:
#   "IP_PLANT_CD renamed to CUSTOMER_PLANT_CD", "IP_PLANT_CD is renamed as ...",
#   "IP_PLANT_CD column renamed to ...", "Rename IP_PLANT_CD to ...",
#   and the SQL-ish "IP_PLANT_CD AS CUSTOMER_PLANT_CD".
# Before this, every one of them compiled to no action at all - or worse, to the
# condition IP_PLANT_CD = 'renamed', because COMPARISON read "is" as an operator.
_COLUMN = r"[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]*"
RENAME_PATTERNS = (
    re.compile(rf"\b(?P<src>{_COLUMN})\s+(?:column\s+)?(?:is\s+|was\s+|has\s+been\s+|will\s+be\s+)?"
               rf"renamed\s+(?:to|as|into)\s+(?P<tgt>{_COLUMN})\b", re.IGNORECASE),
    re.compile(rf"\brename\s+(?:the\s+)?(?:column\s+)?(?P<src>{_COLUMN})\s+(?:to|as|into)\s+"
               rf"(?P<tgt>{_COLUMN})\b", re.IGNORECASE),
    # Upper case only: in prose "as" is too common to trust between lower-case words.
    re.compile(rf"\b(?P<src>[A-Z][A-Z0-9]*_[A-Z0-9_]*)\s+AS\s+(?P<tgt>[A-Z][A-Z0-9]*_[A-Z0-9_]*)\b"),
)


# ------------------------------------------------------------------
# Joins from the sentence
# ------------------------------------------------------------------
# An object reference: fully qualified DB.SCHEMA.OBJECT, or a bare name that follows
# the layer convention (V_BRD_..., T_BRD_..., or ending in a layer suffix). The bare
# form matters: rule 6 of the Plant stage says "join SUPPLIER_NBR with SUPPLIER_ID from
# SUPPLIER_BUSINESS_ORGANIZATION_NAME_ROOT", with no prefix at all.
_OBJECT_REF = re.compile(
    r"\b(?:[A-Z][A-Z0-9_]*\.[A-Z][A-Z0-9_]*\.[A-Z][A-Z0-9_]*"
    r"|[TVF]_[A-Z0-9]+_[A-Z0-9_]+"
    r"|[A-Z][A-Z0-9_]*_(?:ROOT|STAGE|PIT|CURRENT|MEMORIES|HASH|MAPPING|DETAIL))\b")
_JOIN_COLUMN = re.compile(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]*\b")
_JOIN_START = re.compile(r"\b(?:left\s+(?:outer\s+)?|inner\s+)?join\b", re.IGNORECASE)
_JOIN_KEY = re.compile(r"\b(?:on|based\s+on|using|by)\s+(?:the\s+)?(?P<keys>.+)$", re.IGNORECASE)
_JOIN_WITH = re.compile(r"\s(?:with|to|against)\s", re.IGNORECASE)


def _clause_after(text: str, start: int) -> str:
    """The join clause: from "join" to the end of its sentence, capped for sanity."""
    rest = text[start:start + 400]
    end = re.search(r"\.\s+(?=[A-Z0-9])|;|\bthen\b|\band (?:take|get|populate|filter|set)\b",
                    rest, flags=re.IGNORECASE)
    return rest[:end.start()] if end else rest


def _split_refs(fragment: str) -> Tuple[List[str], List[str]]:
    """Objects and columns named in a fragment, in order, objects taken out first.

    Objects are removed before columns are read, so V_BRD_ITEM_ROOT is not also
    counted as a column, while a qualified column like V_BRD_ITEM_CORE_PIT.ITEM_ID
    still leaves ITEM_ID behind.
    """
    objects = [m.group(0) for m in _OBJECT_REF.finditer(fragment)]
    stripped = _OBJECT_REF.sub(" ", fragment)
    columns = [c for c in _JOIN_COLUMN.findall(stripped)]
    return objects, columns


def extract_joins(text: str, default_left: str = "") -> List[Dict]:
    """Read joins stated in plain English into the RULE_JOIN shape.

    Understands the forms these FDDs use:
      Join <COL> from|in <OBJECT A> with [the corresponding] <COL> in|from <OBJECT B>
      Join [the records] with <OBJECT B> on|based on|using <COL>[ = <COL>]
      Join <COL> with <COL> from <OBJECT B>          (left side = the rule's source)

    When the left object is not named, it is the hop's source - that is what "join
    the identified items with ..." means inside a Transformation cell. A join whose
    right-hand object cannot be found is not guessed at; validate() reports it.
    Join columns that are not stated are left empty rather than invented, and the
    rule preview shows the join so the author can see the gap.
    """
    joins: List[Dict] = []
    body = str(text or "")
    for match in _JOIN_START.finditer(body):
        clause = _clause_after(body, match.end())
        join_type = "LEFT" if match.group(0).lower().startswith("left") else "INNER"

        key_match = _JOIN_KEY.search(clause)
        keys_text = key_match.group("keys") if key_match else ""
        # The key list ends where the purpose starts: in "based on ITEM_ID to get the
        # OWNER_CD", OWNER_CD is what the join fetches, not a second key column.
        keys_text = re.split(r"\s(?:to|in order to|for|so that|and (?:take|get|fetch|bring))\s",
                             keys_text, maxsplit=1, flags=re.IGNORECASE)[0]
        head = clause[:key_match.start()] if key_match else clause

        with_match = _JOIN_WITH.search(" " + head + " ")
        if with_match:
            cut = max(0, with_match.start() - 1)
            left_part, right_part = head[:cut], head[cut + len(with_match.group(0)) - 1:]
        else:
            left_part, right_part = "", head

        left_objects, left_columns = _split_refs(left_part)
        right_objects, right_columns = _split_refs(right_part)
        if not right_objects and len(left_objects) > 1:
            right_objects = left_objects[1:]
            left_objects = left_objects[:1]
        if not right_objects:
            continue                        # nothing to join to; not guessed

        right_object = right_objects[0]
        left_object = left_objects[0] if left_objects else default_left
        if not left_object or left_object.upper() == right_object.upper():
            continue

        # Right-side columns are the ones named before the right object; anything
        # after it ("... and take SUPPLIER_NM") is what the join is for, not its key.
        right_at = right_part.find(right_object)
        right_columns = _split_refs(right_part[:right_at])[1] if right_at > 0 else []

        pairs: List[Tuple[str, str]] = []
        if keys_text:
            for piece in re.split(r"\s*(?:,|\band\b)\s*", keys_text):
                cols = _split_refs(piece)[1]
                if len(cols) >= 2:
                    pairs.append((cols[0], cols[1]))
                elif len(cols) == 1:
                    pairs.append((cols[0], cols[0]))
        elif left_columns and right_columns:
            pairs = list(zip(left_columns, right_columns))
        elif left_columns or right_columns:
            pairs = [(c, c) for c in (left_columns or right_columns)]

        joins.append({
            "left_object": left_object,
            "right_object": right_object,
            "join_type": join_type,
            "join_purpose": clause.strip()[:240],
            "join_columns": [{"left_column": l, "right_column": r} for l, r in pairs],
            "filters": [],
        })
    return joins


def find_renames(text: str) -> Tuple[List[Tuple[str, str]], str]:
    """Every (source column, target column) rename in the text, and the text without them.

    The remainder is returned so the ordinary condition parser never sees the rename
    phrase, which is where the bogus IP_PLANT_CD = 'renamed' condition came from.
    """
    renames: List[Tuple[str, str]] = []
    remainder = text
    for pattern in RENAME_PATTERNS:
        for match in pattern.finditer(remainder):
            pair = (match.group("src").upper(), match.group("tgt").upper())
            if pair[0] != pair[1] and pair not in renames:
                renames.append(pair)
        remainder = pattern.sub(" ", remainder)
    return renames, re.sub(r"\s+", " ", remainder).strip(" ,.;")


def pre_parse(text: str) -> Dict:
    """A first draft built from the sentence structure alone."""
    body = re.sub(r"\s+", " ", text.strip())
    renames, body = find_renames(body)
    rename_actions = [{
        "target_field": target,
        "action_type": "COPY",
        "action_value": source,
        "action_description": f"{source} renamed to {target}",
    } for source, target in renames]
    if renames and not body:
        return {"conditions": [], "actions": rename_actions}
    condition_part, action_part = body, ""

    match = re.search(r"\b(then|,\s*then)\b", body, flags=re.IGNORECASE)
    if match:
        condition_part = body[:match.start()]
        action_part = body[match.end():]
    else:
        match = re.search(r"\b(should be|must be|set|populate|default)\b", body, flags=re.IGNORECASE)
        if match:
            condition_part = body[:match.start()]
            action_part = body[match.start():]

    condition_part = re.sub(r"^\s*(if|when|where|for)\b", "", condition_part, flags=re.IGNORECASE)

    # A mid-sentence "where" separates a preamble ("Filter and Load all records
    # from INTELFOUNDRY_PLANT") from the actual conditions ("PLANT_STATUS_CD = ACT").
    # Without this, the COMPARISON regex captures the table name and "where" as part
    # of the field, producing INTELFOUNDRY_PLANT_WHERE_PLANT_STATUS_CD.
    where_mid = re.search(r"\bwhere\b", condition_part, flags=re.IGNORECASE)
    if where_mid:
        condition_part = condition_part[where_mid.end():]

    conditions = []
    for clause in re.split(r"\b(and|or)\b", condition_part, flags=re.IGNORECASE):
        clause = clause.strip(" ,.")
        if not clause or clause.lower() in ("and", "or"):
            continue
        found = COMPARISON.search(clause)
        if not found:
            continue
        raw_field = strip_field_filler(found.group("field"))
        candidates = resolve_field(raw_field, limit=1)
        operator = normalize_operator(found.group("op"))
        value = (found.group("value") or "").strip("'\" ")
        if value.upper() in BLANK_WORDS:
            operator = "IS NOT NULL" if operator == "!=" else "IS NULL"
            value = ""
        elif value.upper() in PRESENT_WORDS:
            operator = "IS NULL" if operator == "!=" else "IS NOT NULL"
            value = ""
        conditions.append({
            "condition_group": 1,
            "field_name": candidates[0] if candidates else raw_field.upper().replace(" ", "_"),
            "operator": operator,
            "field_value": value,
            "logical_operator": "AND",
            "condition_description": clause,
        })

    actions = []
    if action_part:
        text_lower = action_part.lower()
        if re.search(r"\b(exclude|reject|drop|filter out|do not load)\b", text_lower):
            actions.append({"target_field": "", "action_type": "EXCLUDE_RECORD",
                            "action_value": "", "action_description": action_part.strip(" ,.")})
        else:
            target = resolve_field(action_part, limit=1) or resolve_field(condition_part, limit=1)
            value_match = re.search(
                r"\b(?:to|as|with|=)\s*('[^']*'|\"[^\"]*\"|[A-Za-z0-9_.\-]+)",
                action_part, flags=re.IGNORECASE)
            kind = "DEFAULT" if re.search(r"\bdefault", text_lower) else (
                "COPY" if re.search(r"\bcopy|carry|same as\b", text_lower) else "SET_VALUE")
            if target:
                actions.append({
                    "target_field": target[0],
                    "action_type": kind,
                    "action_value": (value_match.group(1).strip("'\" ") if value_match else ""),
                    "action_description": action_part.strip(" ,."),
                })

    if not actions and conditions and RESTRICTIVE_LOAD.search(body):
        # A row filter stated inclusively: "filter only ACTIVE_IND=TRUE records and
        # load into V_...". There is no "then", and none of should be / must be / set /
        # populate / default, so the split above leaves action_part empty and the rule
        # compiles with a condition and nothing to do - which validate() rejects with
        # "The rule does not say what to do."
        #
        # The condition is the whole content of such a rule; keeping matching rows is
        # the action. This is the commonest filter phrasing in these FDDs, so leaving
        # it to the LLM meant paying for a model call to recover a fixed shape.
        #
        # Checked after the exclude branch above on purpose: "filter out" is a
        # different rule from "filter only", and this must not capture it.
        actions.append({
            "target_field": "",
            "action_type": "INCLUDE_RECORD",
            "action_value": "",
            "action_description": body.strip(" ,."),
        })

    # Added last, so a rename sitting next to a filter ("load only active records;
    # IP_PLANT_CD renamed to CUSTOMER_PLANT_CD") keeps both: the INCLUDE_RECORD
    # fallback above only fires when nothing else produced an action.
    actions.extend(rename_actions)
    return {"conditions": conditions, "actions": actions}


BLANK_WORDS = {"BLANK", "NULL", "EMPTY", "MISSING", "NOT POPULATED", "NOT SET"}
PRESENT_WORDS = {"POPULATED", "PRESENT", "SET", "PROVIDED", "FILLED"}


def normalize_operator(raw: Optional[str]) -> str:
    text = re.sub(r"\s+", " ", str(raw or "=").strip().upper())
    if text in OPERATORS:
        return text
    if text in OPERATOR_SYNONYMS:
        return OPERATOR_SYNONYMS[text]

    # "is blank", "are not populated", "is not null" and friends
    stripped = re.sub(r"^(IS|ARE|WAS|WERE|HAS|HAVE)\s+", "", text)
    negated = bool(re.match(r"^NOT\s+", stripped))
    core = re.sub(r"^NOT\s+", "", stripped)
    if core in BLANK_WORDS:
        return "IS NOT NULL" if negated else "IS NULL"
    if core in PRESENT_WORDS:
        return "IS NULL" if negated else "IS NOT NULL"
    if stripped in OPERATOR_SYNONYMS:
        return OPERATOR_SYNONYMS[stripped]

    close = difflib.get_close_matches(text, list(OPERATOR_SYNONYMS), n=1, cutoff=0.85)
    if close:
        return OPERATOR_SYNONYMS[close[0]]
    return "="


# ------------------------------------------------------------------
# Stage 2: LLM extraction
# ------------------------------------------------------------------
SCHEMA_HINT = """{
 "rule_name": "short name, max 8 words",
 "rule_category": "one of: %s",
 "business_description": "one sentence a business user would understand",
 "priority": 99,
 "conditions": [{"condition_group":1,"field_name":"COLUMN","operator":"=","field_value":"VALUE","logical_operator":"AND","condition_description":"text"}],
 "actions": [{"target_field":"COLUMN","action_type":"one of: %s","action_value":"VALUE or expression","action_description":"text"}],
 "joins": [{"left_object":"OBJECT","right_object":"OBJECT","join_type":"INNER","join_purpose":"why","join_columns":[{"left_column":"COL","right_column":"COL"}],"filters":[{"filter_column":"COL","filter_operator":"=","filter_value":"V","logical_operator":""}]}]
}""" % (", ".join(RULE_CATEGORIES), ", ".join(ACTION_TYPES))


def _relevant_columns(text: str, objects: List[str], cap: int = 60) -> List[str]:
    """
    Only the columns worth showing the model. Sending the whole catalog is
    what made the prompt slow; sending none is what made it hallucinate.

    When the objects' own columns cannot be read, the model is given only the
    fuzzy hints from the requirement text. The previous fallback pushed the
    repository's own metadata columns into the prompt, which is worse than a
    short list: it invites the model to map a business rule onto RULE_ID or
    CONDITION_GROUP.
    """
    picked: List[str] = []
    for obj in objects:
        picked.extend(get_object_columns(obj)[:cap])
    hinted = resolve_field(text, limit=8)
    ordered = list(dict.fromkeys(hinted + picked))
    return ordered[:cap]


def extract_with_llm(text: str, draft: Dict, objects: List[str], model: str = WRITER_MODEL) -> Tuple[Optional[Dict], str]:
    columns = _relevant_columns(text, objects)
    prompt = f"""You convert a business requirement into rule metadata. Return JSON only, no prose, no markdown fences.

Shape:
{SCHEMA_HINT}

Rules you must follow:
- Use only column names from this list: {", ".join(columns) if columns else "any column named in the requirement"}
- If the requirement names a column that is not in the list, keep the requirement's wording and let the reviewer correct it.
- operator must be one of: {", ".join(OPERATORS)}
- Do not invent conditions, actions or joins that the requirement does not state.
- Use an empty array when a section does not apply.

A rough parse of the same requirement (may be incomplete or wrong):
{json.dumps(draft)}

Requirement:
{text}
"""
    raw = llm_cached(prompt, model)
    return safe_json(raw), raw


def safe_json(text: str) -> Optional[Dict]:
    if not text:
        return None
    cleaned = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.IGNORECASE | re.MULTILINE)
    try:
        return json.loads(cleaned)
    except Exception:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except Exception:
                return None
    return None


# ------------------------------------------------------------------
# Stage 3: validation
# ------------------------------------------------------------------
def _resolve_join_object(name) -> Optional[str]:
    """Resolve a join's object, including ones outside IF_SUPPLY_CHAIN.

    resolve_object() with no databases looks only in the metadata database and the
    registered objects, so a join to IF_MASTER_DATA.LOCATION_IF_ANALYSIS.<view> was
    dropped as "outside the allowed list" even though the view exists. A fully
    qualified name is therefore also looked up in its own database.
    """
    text = str(name or "").strip()
    if not text:
        return None
    resolved, _ = resolve_object(text)
    if resolved:
        return resolved
    if text.count(".") == 2:
        resolved, _ = resolve_object(text, databases=[text.split(".", 1)[0].upper()])
    return resolved


def _group_number(value) -> int:
    """A condition group as a whole number; anything else (e.g. 'remove') is group 1."""
    try:
        return int(float(value)) if value not in (None, "") else 1
    except (TypeError, ValueError):
        return 1


def validate(proposal: Dict, objects: List[str]) -> Tuple[Dict, List[str], List[str]]:
    """Returns (repaired proposal, errors, warnings)."""
    errors: List[str] = []
    warnings: List[str] = []

    known_columns = set()
    for obj in objects:
        known_columns.update(get_object_columns(obj))

    # Also include columns from objects mentioned in the rule text but not
    # explicitly listed as source/target. These are typically metadata views or
    # join partners (e.g. "use metadata Column_Value_Five_Txt where
    # Subject_Area_Nm = 'Location'"). Without this, every metadata column
    # triggers a "not in the target objects" warning.
    _all_text = " ".join([
        str(proposal.get("business_description") or ""),
        " ".join(str(c.get("condition_description") or "")
                 for c in proposal.get("conditions") or []),
        " ".join(str(a.get("action_description") or "")
                 for a in proposal.get("actions") or []),
    ]).upper()
    _known_names = {o.upper() for o in objects}
    _TECH = re.compile(r"\b([A-Z][A-Z0-9_]+\.[A-Z][A-Z0-9_]+\.[A-Z][A-Z0-9_]+)\b")
    for mentioned in _TECH.findall(_all_text):
        if mentioned not in _known_names:
            extra_cols = get_object_columns(mentioned)
            if extra_cols:
                known_columns.update(extra_cols)
                _known_names.add(mentioned)
    # No fallback to the whole column catalog. That catalog covers TABLE_SCHEMA
    # and VIEW_SCHEMA - the repository's own metadata schemas - so falling back
    # to it validated FDD field names against 7,464 unrelated columns: it
    # accepted RULE_ID and CONDITION_GROUP while rejecting genuine source
    # columns like SOURCE_DELETE_IND. Not checking, and saying so, beats
    # checking against the wrong list.
    if not known_columns and objects:
        warnings.append(
            "Could not read the columns of "
            + ", ".join(o.rsplit(".", 1)[-1] for o in objects if o)
            + ", so column names in this rule have not been verified."
        )

    # Words that are never column names. When the model or sentence parser reads
    # English prose as structured data it turns "when there is an update" into a
    # condition on a field called WHEN. These are dropped silently rather than
    # warned about, because the warning ("WHEN is not in the target") is confusing.
    _never_columns = FIELD_FILLER | {
        "there", "it", "they", "we", "case", "either", "both", "each",
        "every", "other", "also", "otherwise", "default", "else",
        "update", "delete", "insert", "change", "master", "initial",
    }

    def check_column(name: str, where: str) -> str:
        value = str(name or "").strip().upper().replace(" ", "_")
        if not value:
            errors.append(f"{where} has no column name.")
            return value
        if value in ("*", "RECORD"):
            return value  # the whole record, not a column
        if value.lower() in _never_columns:
            return ""  # caller filters out empty fields
        if known_columns and value not in known_columns:
            close = difflib.get_close_matches(value, list(known_columns), n=1, cutoff=0.84)
            if close:
                warnings.append(f"{where}: {value} does not exist; using {close[0]}.")
                return close[0]
            warnings.append(f"{where}: {value} is not in the target objects. Check the name before saving.")
        return value

    category = str(proposal.get("rule_category") or "").upper().replace(" ", "_")
    # AS_WRITTEN (as_written.py) is never offered to the model, but a stored one is
    # valid and must not be fuzzed into another category when it is re-checked.
    if category not in RULE_CATEGORIES and category != "AS_WRITTEN":
        close = difflib.get_close_matches(category, RULE_CATEGORIES, n=1, cutoff=0.7)
        category = close[0] if close else "DERIVATION"
        warnings.append(f"Rule type set to {category}.")
    proposal["rule_category"] = category

    if not str(proposal.get("rule_name") or "").strip():
        errors.append("The rule needs a name.")
    if not str(proposal.get("business_description") or "").strip():
        warnings.append("No business description. Add one so the FDD reads properly.")

    try:
        proposal["priority"] = int(proposal.get("priority") or 99)
    except (TypeError, ValueError):
        proposal["priority"] = 99

    conditions = []
    for i, cond in enumerate(proposal.get("conditions") or [], start=1):
        operator = normalize_operator(cond.get("operator"))
        value = str(cond.get("field_value") or "").strip()
        if operator in ("IS NULL", "IS NOT NULL"):
            value = ""
        elif not value:
            warnings.append(f"Condition {i} compares {cond.get('field_name')} against nothing.")
        logical = str(cond.get("logical_operator") or "AND").upper()
        field = check_column(cond.get("field_name"), f"Condition {i}")
        if not field:
            continue  # English word like WHEN, THERE — not a real condition
        conditions.append({
            "condition_group": _group_number(cond.get("condition_group")),
            "field_name": field,
            "operator": operator,
            "field_value": value,
            "logical_operator": logical if logical in ("AND", "OR") else "AND",
            "condition_description": str(cond.get("condition_description") or "").strip(),
        })
    proposal["conditions"] = conditions

    actions = []
    for i, act in enumerate(proposal.get("actions") or [], start=1):
        kind = str(act.get("action_type") or "SET_VALUE").upper().replace(" ", "_")
        # NO_TRANSFORMATION on RECORD is the whole-record pass-through the importer
        # itself saves; turning it into SET_VALUE on a blank column broke every
        # collapsed "load all records" rule.
        if kind not in ACTION_TYPES and kind not in ("NO_TRANSFORMATION", "AS_WRITTEN"):
            close = difflib.get_close_matches(kind, ACTION_TYPES, n=1, cutoff=0.7)
            kind = close[0] if close else "SET_VALUE"
            warnings.append(f"Action {i} set to {kind}.")
        target = act.get("target_field")
        if kind in ("EXCLUDE_RECORD", "INCLUDE_RECORD") and not target:
            checked = ""
        else:
            checked = check_column(target, f"Action {i}")
        actions.append({
            "target_field": checked,
            "action_type": kind,
            "action_value": str(act.get("action_value") or "").strip(),
            "action_description": str(act.get("action_description") or "").strip(),
        })
    if not actions:
        errors.append("The rule does not say what to do. Add at least one action.")
    proposal["actions"] = actions

    joins = []
    for i, join in enumerate(proposal.get("joins") or [], start=1):
        left = _resolve_join_object(join.get("left_object"))
        right = _resolve_join_object(join.get("right_object"))
        if not left or not right:
            missing = join.get("right_object") if left else join.get("left_object")
            warnings.append(
                f"Join {i} names {missing or 'an object'}, which is not in Snowflake or "
                "the repository, so the join was not saved. Use the full "
                "DATABASE.SCHEMA.OBJECT name.")
            continue
        if not [c for c in (join.get("join_columns") or [])
                if c.get("left_column") and c.get("right_column")]:
            warnings.append(
                f"Join {i} to {right.rsplit('.', 1)[-1]} does not say which columns to "
                "join on. Add \"on <COLUMN>\" or \"on <LEFT_COLUMN> = <RIGHT_COLUMN>\".")
        joins.append({
            "left_object": left,
            "right_object": right,
            "join_type": str(join.get("join_type") or "INNER").upper(),
            "join_purpose": str(join.get("join_purpose") or "").strip(),
            "join_columns": [
                {"left_column": str(c.get("left_column") or "").upper(),
                 "right_column": str(c.get("right_column") or "").upper()}
                for c in (join.get("join_columns") or [])
                if c.get("left_column") and c.get("right_column")
            ],
            "filters": [
                {"filter_column": str(f.get("filter_column") or "").upper(),
                 "filter_operator": normalize_operator(f.get("filter_operator")),
                 "filter_value": str(f.get("filter_value") or "").strip(),
                 "logical_operator": str(f.get("logical_operator") or "").upper()}
                for f in (join.get("filters") or [])
                if f.get("filter_column")
            ],
        })
    proposal["joins"] = joins

    return proposal, errors, warnings


# ------------------------------------------------------------------
# Orchestration
# ------------------------------------------------------------------
def _resolved_objects(source_object, target_object) -> List[str]:
    """Resolve the objects a rule is written against, source(s) first.

    source_object accepts a single technical name or a list of them, because a rule
    set can read several objects - the Item stage view joins three root views. Every
    source's columns have to count as known, or validate() rejects a genuine column
    for being absent from the one source that happened to be recorded first.
    """
    names = (list(source_object) if isinstance(source_object, (list, tuple, set))
             else [source_object])
    names.append(target_object)
    out: List[str] = []
    for name in names:
        if not name:
            continue
        resolved = resolve_object(name)[0]
        if resolved and resolved not in out:
            out.append(resolved)
    return out


EXTRACTION_SCHEMA = rx.extraction_schema(RULE_CATEGORIES, ACTION_TYPES, OPERATORS)


def compile_requirement(text: str, rule_set_id: int, rule_number: str,
                        source_object="", target_object: str = "",
                        model: str = WRITER_MODEL, use_llm: bool = True,
                        context: Optional[Dict] = None, review_rule: bool = False,
                        llm_fn=None, columns_fn=None) -> Dict:
    """
    source_object may be a single technical name or a list of them.

    context (optional, from the FDD importer):
      section     - heading the rule sits under ("Substrate locations ...")
      neighbours  - the other rules on the same hop, so the model knows what is
                    covered elsewhere and does not fold them into this one
      objects     - every object the document declares, so a leaf name like
                    V_BRD_ITEM_CORE_PIT resolves to its full name
    review_rule: run the independent reviewer pass (one more model call).

    Returns:
      proposal  - ready for repository.save_rule()
      preview   - FDD-style English for the BA to confirm
      errors    - must be fixed before saving
      warnings  - worth a look, not blocking
      raw       - the model's response, for debugging
      status    - green / amber / red, with reasons (rule_extractor.assess)
      doubts, fdd_issues, findings - what the model and reviewer were unsure of
    """
    context = context or {}
    columns_fn = columns_fn or get_object_columns
    objects = _resolved_objects(source_object, target_object)
    draft = pre_parse(text)
    sources = _source_list(source_object)
    scope = _rule_scope(text, sources, target_object, context)
    columns = rx.column_context(scope, columns_fn)

    proposal: Dict = {}
    raw = ""
    doubts: List[str] = []
    fdd_issues: List[str] = []
    if use_llm:
        structured = rx.extract(
            text, llm_fn=llm_fn or llm_structured, schema=EXTRACTION_SCHEMA,
            section=context.get("section", ""), sources=sources, target=target_object,
            columns=columns,
            neighbours=context.get("neighbours") or [], draft=draft, model=model)
        if structured:
            doubts = [str(d) for d in structured.pop("doubts", []) or []]
            fdd_issues = [str(d) for d in structured.pop("fdd_issues", []) or []]
            proposal = structured
            raw = json.dumps(structured)
        else:
            parsed, raw = extract_with_llm(text, draft, objects, model)
            if parsed:
                proposal = parsed

    if not proposal:
        proposal = {
            "rule_name": text.strip()[:60],
            "rule_category": "DERIVATION",
            "business_description": text.strip(),
            "priority": 99,
            **draft,
            "joins": [],
        }

    proposal.setdefault("conditions", draft["conditions"])
    if not proposal.get("actions"):
        proposal["actions"] = draft["actions"]

    # Joins read from the sentence when nothing else supplied them. Until this, only
    # the model ever produced a join - and the importer's Fast and Balanced modes call
    # the model only when the sentence parser reports an error, so a rule like
    # "Join the ITEM_ID from V_BRD_ITEM_ROOT with the ITEM_ID in
    # V_BRD_ITEM_CHARACTERISTIC_ROOT" parsed cleanly and was saved with no join.
    # Five of the seven join rules in the repository were lost that way.
    if not proposal.get("joins"):
        default_left = next(iter(_resolved_objects(source_object, "")), "")
        proposal["joins"] = extract_joins(text, default_left)

    proposal["rule_set_id"] = int(rule_set_id)
    proposal["rule_number"] = (rule_number or "").strip() or "TBD"
    proposal["rule_status"] = "DRAFT"

    # If the model expanded a pass-through into per-column COPY actions with no
    # value transformation, collapse it back. A 22-action "copy LOCATION_ID
    # unchanged, copy LOCATION_TYPE_NM unchanged, ..." is not a specification —
    # it is the model restating the schema. One NO_TRANSFORMATION action says the
    # same thing and reads cleanly in the FDD.
    proposal["actions"] = collapse_column_copies(proposal.get("actions") or [])
    if [a.get("action_type") for a in proposal["actions"]] == ["NO_TRANSFORMATION"]:
        proposal["rule_category"] = (proposal.get("rule_category")
                                     if proposal.get("rule_category") in ("INITIAL_LOAD",
                                                                          "TIME_TRAVEL")
                                     else "NO_TRANSFORMATION")
        if not proposal.get("rule_name") or "copy" in proposal.get("rule_name", "").lower():
            proposal["rule_name"] = "No transformation"

    proposal, errors, warnings = validate(proposal, scope)

    findings: List[Dict] = []
    if use_llm and review_rule:
        findings = rx.review(text, proposal, llm_fn=llm_fn or llm_structured,
                             section=context.get("section", ""), model=FAST_MODEL)
    from_model = rx.model_issues(doubts=doubts, fdd_issues=fdd_issues, findings=findings)
    issues = rx.merge_issues(
        rx.deterministic_issues(text, proposal, columns=columns, target=target_object,
                                sources=sources, errors=errors,
                                extract_joins_fn=extract_joins,
                                hints=_hints(context, from_model))
        + from_model)

    preview = preview_of(proposal, source_object, target_object)

    return {"proposal": proposal, "preview": preview, "errors": errors,
            "warnings": warnings, "raw": raw, "draft": draft,
            "doubts": doubts, "fdd_issues": fdd_issues, "findings": findings,
            "issues": issues, "model_issues": from_model,
            "status": rx.status_of(issues), "reasons": [i["title"] for i in issues]}


_COPY_KINDS = ("COPY", "CARRY_FORWARD", "NO_TRANSFORMATION")


def _keeps_own_value(action: Dict) -> bool:
    """COPY ITEM_ID = ITEM_ID, CARRY_FORWARD OWNER_CD = PARENT.OWNER_CD, or no value:
    the column simply keeps what it had. Says nothing a single "all columns" action
    does not say."""
    kind = str(action.get("action_type", "")).upper()
    target = str(action.get("target_field") or "").strip().upper()
    value = str(action.get("action_value") or "").strip().upper()
    last = re.split(r"[.\s]+", value)[-1] if value else ""
    if kind in _COPY_KINDS:
        return not value or (bool(target) and last == target)
    # SET_VALUE LOCATION_ID = IF_SUPPLY_CHAIN...T_BRD_PLANT_CORE_CURRENT.LOCATION_ID:
    # the same column read from another object, which is a copy. Rule 104860 ("Load
    # all records from T_BRD_PLANT_CORE_CURRENT") was saved as 22 of these.
    return kind == "SET_VALUE" and bool(target) and "." in value and last == target


def collapse_column_copies(actions: List[Dict]) -> List[Dict]:
    """Replace per-column "keep the same value" actions with one action.

    "Load all records" and "rest all other cols from parent" name no columns, but a
    model given the target's column list answers with one COPY per column - 8 of them
    for Item Location stage rule 4, and a whole schema for a straight load. That reads
    as a column-by-column spec the FDD never wrote. Three or more such actions become
    one: NO_TRANSFORMATION on RECORD when nothing else happens, otherwise a single
    CARRY_FORWARD of "*" next to the actions that do change something. Copies that
    map one column to a different one (COLUMN_VALUE_TWO_TXT -> PLANT_CD) are kept.
    """
    same = [a for a in actions if _keeps_own_value(a)]
    if len(same) < 3:
        return list(actions)
    rest = [a for a in actions if not _keeps_own_value(a)]
    if not rest:
        return [{"target_field": "RECORD", "action_type": "NO_TRANSFORMATION",
                 "action_value": "",
                 "action_description": "Every column is carried through unchanged."}]
    return rest + [{"target_field": "*", "action_type": "CARRY_FORWARD",
                    "action_value": "all other columns",
                    "action_description": "All other columns are carried across unchanged "
                                          f"({len(same)} listed by the model)."}]


def preview_of(proposal: Dict, source_object="", target_object: str = "") -> str:
    """How the rule will read in the FDD."""
    return render_rule({
        "rule": {
            "RULE_NUMBER": proposal.get("rule_number"),
            "RULE_NAME": proposal.get("rule_name"),
            "RULE_CATEGORY": proposal.get("rule_category"),
            "BUSINESS_DESCRIPTION": proposal.get("business_description"),
            "PRIORITY": proposal.get("priority"),
            "RULE_STATUS": "DRAFT",
            "SOURCE_BUSINESS_OBJECT": source_object,
            "TARGET_BUSINESS_OBJECT": target_object,
        },
        "conditions": [{k.upper(): v for k, v in c.items()} for c in proposal.get("conditions") or []],
        "actions": [{k.upper(): v for k, v in a.items()} for a in proposal.get("actions") or []],
        "joins": [{
            "join": {k.upper(): v for k, v in j.items() if k not in ("join_columns", "filters")},
            "columns": [{k.upper(): v for k, v in c.items()} for c in j.get("join_columns", [])],
            "filters": [{k.upper(): v for k, v in f.items()} for f in j.get("filters", [])],
        } for j in proposal.get("joins") or []],
    })


def _hints(context: Dict, model_issues) -> str:
    """Text that helps pick a replacement column: the heading, and what the model and
    reviewer said (they usually name the likely column, e.g. "PLANT_CD is closest")."""
    return " ".join([str((context or {}).get("section", ""))]
                    + [str(i.get("detail", "")) for i in model_issues or []])


def _source_list(source_object) -> List[str]:
    items = (list(source_object) if isinstance(source_object, (list, tuple, set))
             else [source_object])
    return [str(s) for s in items if s]


def _rule_scope(text: str, sources: List[str], target: str, context: Dict) -> List[str]:
    """Every object a rule may touch: the hop, plus what the text names (in full, or
    by a leaf name the document declares), plus the metadata driver when the text
    says "metadata". Their real columns go to the model and to validation, so a join
    partner's columns are not rejected as unknown."""
    scope: List[str] = list(_resolved_objects(sources, target))
    declared = [str(o).upper() for o in (context or {}).get("objects") or []]
    for name in rx.objects_in_text(text, declared + list(sources) + [target]):
        resolved = _resolve_join_object(name) or name
        if resolved not in scope:
            scope.append(resolved)
    if rx.mentions_metadata(text) and rx.METADATA_VIEW not in scope:
        scope.append(rx.METADATA_VIEW)
    return scope


def regrade(text: str, proposal: Dict, *, sources, target: str,
            context: Optional[Dict] = None, model_issues: List[Dict] = (),
            columns_fn=None) -> Dict:
    """Re-check an edited proposal without calling a model.

    Used after the reviewer applies a suggested fix: validation and the deterministic
    checks run again on the changed rule, and the model's earlier issues are carried
    over (the page marks the ones it has dealt with). Returns the same keys the page
    reads from compile_requirement.
    """
    context = context or {}
    sources = _source_list(sources)
    scope = _rule_scope(text, sources, target, context)
    columns = rx.column_context(scope, columns_fn or get_object_columns)
    checked, errors, warnings = validate(copy.deepcopy(proposal), scope)
    issues = rx.merge_issues(
        rx.deterministic_issues(text, checked, columns=columns, target=target,
                                sources=sources, errors=errors,
                                extract_joins_fn=extract_joins,
                                hints=_hints(context, model_issues))
        + [dict(i) for i in model_issues or []])
    return {"proposal": checked, "errors": errors, "warnings": warnings,
            "issues": issues, "status": rx.status_of(issues)}


# ------------------------------------------------------------------
# Updating an existing rule
# ------------------------------------------------------------------
def _bundle_to_proposal(bundle: Dict) -> Dict:
    """The stored rule, reshaped into the same dict shape compile_requirement produces."""
    rule = bundle.get("rule", {}) or {}
    return {
        "rule_name": rule.get("RULE_NAME"),
        "rule_category": rule.get("RULE_CATEGORY"),
        "business_description": rule.get("BUSINESS_DESCRIPTION"),
        "priority": rule.get("PRIORITY"),
        "rule_status": rule.get("RULE_STATUS"),
        "conditions": [{
            "condition_group": c.get("CONDITION_GROUP"),
            "field_name": c.get("FIELD_NAME"),
            "operator": c.get("OPERATOR"),
            "field_value": c.get("FIELD_VALUE"),
            "logical_operator": c.get("LOGICAL_OPERATOR"),
            "condition_description": c.get("CONDITION_DESCRIPTION"),
        } for c in bundle.get("conditions", []) or []],
        "actions": [{
            "target_field": a.get("TARGET_FIELD"),
            "action_type": a.get("ACTION_TYPE"),
            "action_value": a.get("ACTION_VALUE"),
            "action_description": a.get("ACTION_DESCRIPTION"),
        } for a in bundle.get("actions", []) or []],
        "joins": [{
            "left_object": j["join"].get("LEFT_OBJECT"),
            "right_object": j["join"].get("RIGHT_OBJECT"),
            "join_type": j["join"].get("JOIN_TYPE"),
            "join_purpose": j["join"].get("JOIN_PURPOSE"),
            "join_columns": [{"left_column": c.get("LEFT_COLUMN"), "right_column": c.get("RIGHT_COLUMN")}
                              for c in j.get("columns", []) or []],
            "filters": [{"filter_column": f.get("FILTER_COLUMN"), "filter_operator": f.get("FILTER_OPERATOR"),
                         "filter_value": f.get("FILTER_VALUE"), "logical_operator": f.get("LOGICAL_OPERATOR")}
                        for f in j.get("filters", []) or []],
        } for j in bundle.get("joins", []) or []],
    }


def extract_update_with_llm(text: str, current: Dict, objects: List[str],
                             model: str = WRITER_MODEL) -> Tuple[Optional[Dict], str]:
    columns = _relevant_columns(text, objects)
    prompt = f"""You update an existing business rule based on a plain-English change request. Return JSON only, no prose, no markdown fences.

Shape (return the COMPLETE rule after applying the update, not just the changed parts):
{SCHEMA_HINT}

Rules you must follow:
- Start from the existing rule below and change only what the update request asks for.
- Any field, condition, action or join not mentioned in the update keeps its existing value exactly.
- Use only column names from this list: {", ".join(columns) if columns else "any column named in the update"}
- operator must be one of: {", ".join(OPERATORS)}
- Do not invent conditions, actions or joins that the update request does not state.
- Use an empty array when a section does not apply.

Existing rule:
{json.dumps(current)}

Update request:
{text}
"""
    raw = llm_cached(prompt, model)
    return safe_json(raw), raw


def _finish_update(proposal: Dict, bundle: Dict, current: Dict, objects: List[str],
                   raw: str = "") -> Dict:
    """Validate an edited rule and render its preview - shared by a typed update and a
    direct edit of the fields, so both are checked and shown the same way."""
    rule = bundle.get("rule", {}) or {}
    proposal["rule_set_id"] = rule.get("RULE_SET_ID")
    proposal["rule_number"] = rule.get("RULE_NUMBER")

    proposal, errors, warnings = validate(proposal, objects)
    # Show the status the save will actually write. update_rule() decides it from the
    # stored row and ignores this value, so echoing the stored status here would make
    # the preview claim an edited ACCEPTED rule stays accepted.
    proposal["rule_status"] = status_after_edit(rule.get("RULE_STATUS"),
                                                content_changed(current, proposal))

    preview = render_rule({
        "rule": {
            "RULE_NUMBER": proposal["rule_number"],
            "RULE_NAME": proposal.get("rule_name"),
            "RULE_CATEGORY": proposal.get("rule_category"),
            "BUSINESS_DESCRIPTION": proposal.get("business_description"),
            "PRIORITY": proposal.get("priority"),
            "RULE_STATUS": proposal["rule_status"],
            "SOURCE_BUSINESS_OBJECT": rule.get("SOURCE_BUSINESS_OBJECT", ""),
            "TARGET_BUSINESS_OBJECT": rule.get("TARGET_BUSINESS_OBJECT", ""),
        },
        "conditions": [{k.upper(): v for k, v in c.items()} for c in proposal["conditions"]],
        "actions": [{k.upper(): v for k, v in a.items()} for a in proposal["actions"]],
        "joins": [{
            "join": {k.upper(): v for k, v in j.items() if k not in ("join_columns", "filters")},
            "columns": [{k.upper(): v for k, v in c.items()} for c in j.get("join_columns", [])],
            "filters": [{k.upper(): v for k, v in f.items()} for f in j.get("filters", [])],
        } for j in proposal["joins"]],
    })

    return {"proposal": proposal, "preview": preview, "errors": errors,
            "warnings": warnings, "raw": raw, "current": current}

def compile_update(text: str, bundle: Dict, source_object="", target_object: str = "",
                    model: str = WRITER_MODEL, use_llm: bool = True) -> Dict:
    """
    Same contract as compile_requirement, but starts from an existing rule
    bundle instead of a blank one. source_object may be a name or a list of them.

    Returns:
      proposal  - ready for repository.update_rule()
      preview   - FDD-style English for the BA to confirm
      errors    - must be fixed before saving
      warnings  - worth a look, not blocking
      raw       - the model's response, for debugging
      current   - the rule as it was before this update, for reference
    """
    objects = _resolved_objects(source_object, target_object)
    current = _bundle_to_proposal(bundle)

    proposal: Dict = {}
    raw = ""
    if use_llm:
        parsed, raw = extract_update_with_llm(text, current, objects, model)
        if parsed:
            proposal = parsed

    if not proposal:
        proposal = current

    return _finish_update(proposal, bundle, current, objects, raw)


_EDIT_SECTIONS = {
    "conditions": ("condition_group", "field_name", "operator", "field_value",
                   "logical_operator", "condition_description"),
    "actions": ("target_field", "action_type", "action_value", "action_description"),
}


def _blank(value) -> bool:
    try:
        if value is None or value != value:      # None or NaN from the table editor
            return True
    except Exception:
        return False
    return str(value).strip() == ""


def parse_join_columns(text) -> Tuple[List[Dict], List[str]]:
    """'ITEM_ID = ITEM_ID, PLANT_CD = LOCATION_ID' -> join column pairs, plus problems."""
    pairs, problems = [], []
    for part in [p.strip() for p in str(text or "").split(",") if p.strip()]:
        if "=" not in part:
            problems.append(f"Join key '{part}' needs the form LEFT_COLUMN = RIGHT_COLUMN.")
            continue
        left, right = (x.strip().upper() for x in part.split("=", 1))
        if not left or not right:
            problems.append(f"Join key '{part}' is missing a column on one side.")
            continue
        pairs.append({"left_column": left, "right_column": right})
    return pairs, problems


def join_columns_text(join: Dict) -> str:
    return ", ".join(f"{c.get('left_column')} = {c.get('right_column')}"
                     for c in join.get("join_columns") or [])


def apply_edits(current: Dict, edited: Dict) -> tuple:
    """The proposal with the editor's values put in, and any key columns that could not
    be read. Blank rows are dropped; join filters are kept from the matching join."""
    proposal = copy.deepcopy(current)
    for key in ("rule_name", "rule_category", "business_description", "priority"):
        if key in edited:
            proposal[key] = edited[key]
    for section, fields in _EDIT_SECTIONS.items():
        if section in edited:
            proposal[section] = [
                {f: (None if _blank(row.get(f)) else row.get(f)) for f in fields}
                for row in edited[section] or []
                if not all(_blank(row.get(f)) for f in fields)]
    problems: List[str] = []
    if "joins" in edited:
        old_filters = {(str(j.get("left_object")), str(j.get("right_object"))): j.get("filters") or []
                       for j in current.get("joins") or []}
        joins = []
        for row in edited["joins"] or []:
            if all(_blank(row.get(f)) for f in ("left_object", "right_object", "join_columns_text")):
                continue
            pairs, bad = parse_join_columns(row.get("join_columns_text"))
            problems.extend(bad)
            key = (str(row.get("left_object")), str(row.get("right_object")))
            joins.append({
                "left_object": None if _blank(row.get("left_object")) else row.get("left_object"),
                "right_object": None if _blank(row.get("right_object")) else row.get("right_object"),
                "join_type": None if _blank(row.get("join_type")) else row.get("join_type"),
                "join_purpose": None if _blank(row.get("join_purpose")) else row.get("join_purpose"),
                "join_columns": pairs,
                # Filters are not edited here; a join keeps the ones it had.
                "filters": old_filters.get(key, []),
            })
        proposal["joins"] = joins
    return proposal, problems


def compile_direct_edit(bundle: Dict, edited: Dict, source_object="",
                        target_object: str = "") -> Dict:
    """The rule as edited field by field on Update a rule, checked like a typed update.

    edited: rule_name, rule_category, business_description, priority, and lists of row
    dicts for conditions, actions and joins (joins carry key columns as text in
    "join_columns_text"). Blank rows are dropped. No model is called. Returns the keys
    compile_update returns, plus "changed".
    """
    objects = _resolved_objects(source_object, target_object)
    current = _bundle_to_proposal(bundle)
    proposal, problems = apply_edits(current, edited)
    result = _finish_update(proposal, bundle, current, objects)
    result["errors"] = problems + list(result["errors"])
    # Against the stored rule after the same normalisation, so untouched fields that
    # validate() tidies (upper-cased names, default groups) do not count as a change.
    baseline, _, _ = validate(copy.deepcopy({**current,
                                             "rule_set_id": result["proposal"].get("rule_set_id"),
                                             "rule_number": result["proposal"].get("rule_number")}),
                              objects)
    result["changed"] = content_changed(baseline, result["proposal"])
    return result


# ------------------------------------------------------------------
# Rolling back to a previous version
# ------------------------------------------------------------------
def rollback_events(rule_id: int) -> List[Dict]:
    """
    History rows grouped by the moment they were written (one save or update
    call can touch several fields, all with the same CHANGED_TS). Each group
    is a version you can roll back to. Most recent first; the very first
    group is the current live version, so it is not offered as a target.
    """
    history = get_rule_history(rule_id)
    if history.empty:
        return []

    events = []
    for ts, group in history.groupby("CHANGED_TS", sort=False):
        raw_fields = list(dict.fromkeys(group["CHANGE_TYPE"].tolist()))
        fields = [f for f in raw_fields if f not in ("CREATED", "ROLLBACK")]
        if not fields:
            fields = raw_fields
        # Build a per-field change summary so users can identify which version
        # to restore without having to preview each one.
        changes = []
        for _, row in group.iterrows():
            ct = row["CHANGE_TYPE"]
            if ct in ("CREATED", "ROLLBACK"):
                changes.append({"field": ct, "old": None, "new": None})
                continue
            old = None if pd.isna(row["OLD_VALUE"]) else str(row["OLD_VALUE"])
            new = None if pd.isna(row["NEW_VALUE"]) else str(row["NEW_VALUE"])
            changes.append({"field": ct, "old": old, "new": new})
        events.append({
            "change_id": int(group["CHANGE_ID"].max()),
            "ts": ts,
            "changed_by": group["CHANGED_BY"].iloc[0],
            "fields": fields,
            "changes": changes,
        })
    return events[1:]  # drop the current version - rolling back to it is a no-op


def _apply_rollback(current: Dict, rule_id: int, target_change_id: int) -> Dict:
    """
    Undo every logged change more recent than the target change, in reverse order.

    "More recent" is decided by CHANGED_TS, not by comparing CHANGE_ID numerically.
    Surrogate ids come from a Snowflake sequence that reserves blocks, so they are
    unique but NOT monotonic with insertion order - a change written later can get
    a lower id. Comparing ids would then break out of the loop early and silently
    under-apply the rollback, leaving the rule in a state it was never in.
    """
    scalar_map = {label: key for key, label in SCALAR_AUDIT_FIELDS}
    section_map = {label: key for key, label in SECTION_AUDIT_FIELDS}

    reconstructed = copy.deepcopy(current)
    history = get_rule_history(rule_id)  # most recent first
    if history.empty:
        return reconstructed

    # Resolve the target change to the moment it was written. Any id within the
    # target's timestamp group resolves to the same moment, which is what
    # rollback_events() hands back.
    match = history[history["CHANGE_ID"].astype(int) == int(target_change_id)]
    if match.empty:
        # Unknown target: changing nothing is safer than guessing. The caller's
        # preview will be identical to the current rule, which is visible.
        return reconstructed
    target_ts = match.iloc[0]["CHANGED_TS"]

    for _, row in history.iterrows():
        # Stop at the target's own group: rolling back *to* a version keeps that
        # version's changes applied.
        if row["CHANGED_TS"] <= target_ts:
            break
        change_type = row["CHANGE_TYPE"]
        old_value = row["OLD_VALUE"]
        old_value = None if pd.isna(old_value) else old_value
        if change_type in scalar_map:
            reconstructed[scalar_map[change_type]] = old_value
        elif change_type in section_map:
            key = section_map[change_type]
            if old_value is None:
                reconstructed[key] = []
            else:
                try:
                    reconstructed[key] = json.loads(old_value)
                except (TypeError, ValueError):
                    pass
    return reconstructed


def compile_rollback(bundle: Dict, target_change_id: int,
                      source_object: str = "", target_object: str = "") -> Dict:
    """
    Rebuilds the rule exactly as it stood right after a past change, replayed
    from RULE_CHANGE_HISTORY. No LLM involved - this is a deterministic
    replay, not a re-interpretation.

    Returns the same shape as compile_update(): proposal, preview, errors,
    warnings, current.
    """
    rule = bundle.get("rule", {}) or {}
    rule_id = int(rule.get("RULE_ID"))
    objects = [o for o in (resolve_object(source_object)[0], resolve_object(target_object)[0]) if o]

    current = _bundle_to_proposal(bundle)
    reconstructed = _apply_rollback(current, rule_id, int(target_change_id))

    reconstructed["rule_set_id"] = rule.get("RULE_SET_ID")
    reconstructed["rule_number"] = rule.get("RULE_NUMBER")

    proposal, errors, warnings = validate(reconstructed, objects)
    # Never the status from the version being restored. _apply_rollback replays
    # RULE_STATUS from history like any other scalar, and set_rule_status writes its
    # transitions there - so rolling back past an acceptance would otherwise restore
    # ACCEPTED with no approver check. A rollback is a content edit like any other.
    proposal["rule_status"] = status_after_edit(rule.get("RULE_STATUS"),
                                                content_changed(current, proposal))

    preview = render_rule({
        "rule": {
            "RULE_NUMBER": proposal["rule_number"],
            "RULE_NAME": proposal.get("rule_name"),
            "RULE_CATEGORY": proposal.get("rule_category"),
            "BUSINESS_DESCRIPTION": proposal.get("business_description"),
            "PRIORITY": proposal.get("priority"),
            "RULE_STATUS": proposal["rule_status"],
            "SOURCE_BUSINESS_OBJECT": rule.get("SOURCE_BUSINESS_OBJECT", ""),
            "TARGET_BUSINESS_OBJECT": rule.get("TARGET_BUSINESS_OBJECT", ""),
        },
        "conditions": [{k.upper(): v for k, v in c.items()} for c in proposal["conditions"]],
        "actions": [{k.upper(): v for k, v in a.items()} for a in proposal["actions"]],
        "joins": [{
            "join": {k.upper(): v for k, v in j.items() if k not in ("join_columns", "filters")},
            "columns": [{k.upper(): v for k, v in c.items()} for c in j.get("join_columns", [])],
            "filters": [{k.upper(): v for k, v in f.items()} for f in j.get("filters", [])],
        } for j in proposal["joins"]],
    })

    return {"proposal": proposal, "preview": preview, "errors": errors,
            "warnings": warnings, "current": current}
