# Structured, context-aware rule extraction and self-review for FDD rules.
# Co-authored with CoCo
"""
Turns one FDD rule into repository rows the way a careful analyst would:

  1. extract()  - one structured model call. The model is given the rule, the
                  heading it sits under, the hop (source(s) -> target), and the REAL
                  column list of every object involved, read from Snowflake. The
                  response format is a JSON schema whose enums are the repository's
                  own RULE_CATEGORIES / ACTION_TYPES / OPERATORS, so the model cannot
                  answer "remove_leading_zeros" or "get_column".
  2. review()   - a second, independent call that compares the extracted rule with
                  the original sentence and lists what is missing, wrong or invented.
  3. checks()   - deterministic checks that need no model: a join described but not
                  captured, an object named but not used, a column that does not exist.
  4. assess()   - one status per rule: green (verified), amber (look at it),
                  red (do not save as is).

Why this exists: sending the model one cell with a flat column list and free-form
output produced rules that looked right but were not - LAYER != 'PDH' (no such
column), "loc = VF" (no such column), PARENT_ITEM_PLANT_CD = PLANT_CD (backwards),
and the Y/N contradiction in the FDD silently resolved one way. Each of those is now
either prevented by the prompt and schema, or caught and shown before saving.

Everything that touches Snowflake is injected (llm_fn, columns_fn), so this module
is tested without a session.
"""

import copy
import difflib
import json
import re
from typing import Callable, Dict, List, Optional

RED, AMBER, GREEN = "red", "amber", "green"
UNKNOWN = "UNKNOWN"

_QUALIFIED = re.compile(r"\b([A-Z][A-Z0-9_]+\.[A-Z][A-Z0-9_]+\.[A-Z][A-Z0-9_]+)\b")
_LEAF = re.compile(r"(?<![.\w])([TV]_(?:BRD|CRR)_[A-Z0-9_]+)\b")
_JOIN_WORD = re.compile(r"\bjoin(?:ing|ed|s)?\b", re.IGNORECASE)

# The business rule metadata driver. The FDDs say "join with metadata" and name its
# columns (SUBJECT_AREA_NM, COLUMN_VALUE_TWO_TXT ...) but never the view itself, so
# without this the model can only answer UNKNOWN_METADATA_TABLE.
METADATA_VIEW = "IF_SUPPLY_CHAIN.PDH_COMMON_ANALYSIS.V_CRR_BUSINESS_RULE_METADATA_DETAIL_CORE_PIT"
_METADATA_HINT = re.compile(r"\bmeta\s*data\b|\bCOLUMN_VALUE_[A-Z]+_TXT\b|\bSUBJECT_AREA_NM\b",
                            re.IGNORECASE)


def mentions_metadata(text: str) -> bool:
    return bool(_METADATA_HINT.search(str(text or "")))


def extraction_schema(categories: List[str], action_types: List[str],
                      operators: List[str]) -> Dict:
    """JSON schema for AI_COMPLETE's response_format, enums from the repository."""
    text = {"type": "string"}
    strings = {"type": "array", "items": text}
    return {
        "type": "object",
        "properties": {
            "rule_name": text,
            "rule_category": {"type": "string", "enum": list(categories)},
            "business_description": text,
            "conditions": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "field_name": text,
                    "operator": {"type": "string", "enum": list(operators)},
                    "field_value": text,
                    "logical_operator": {"type": "string", "enum": ["AND", "OR"]},
                    "condition_group": {"type": "integer"},
                    "condition_description": text,
                },
                "required": ["field_name", "operator", "field_value", "logical_operator"],
            }},
            "actions": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "target_field": text,
                    "action_type": {"type": "string", "enum": list(action_types)},
                    "action_value": text,
                    "action_description": text,
                },
                "required": ["target_field", "action_type", "action_value"],
            }},
            "joins": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "left_object": text,
                    "right_object": text,
                    "join_type": {"type": "string", "enum": ["INNER", "LEFT", "RIGHT", "FULL"]},
                    "join_purpose": text,
                    "join_columns": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"left_column": text, "right_column": text},
                        "required": ["left_column", "right_column"],
                    }},
                },
                "required": ["left_object", "right_object", "join_type", "join_columns"],
            }},
            "doubts": strings,
            "fdd_issues": strings,
        },
        "required": ["rule_name", "rule_category", "business_description",
                     "conditions", "actions", "joins", "doubts", "fdd_issues"],
    }


_REMOVE_WORDS = {"remove", "delete", "drop"}
_NUMERIC_FIELDS = {"condition_group", "condition_seq", "action_seq", "priority", "index"}


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                "kind": {"type": "string",
                         "enum": ["missing", "wrong", "invented", "contradiction", "other"]},
                "detail": {"type": "string"},
                "suggested_fix": {"type": "string"},
                "patch": {"type": "object", "properties": {
                    "section": {"type": "string", "enum": ["actions", "conditions", "none"]},
                    "index": {"type": "integer"},
                    "field": {"type": "string"},
                    "value": {"type": "string"},
                }},
            },
            "required": ["severity", "kind", "detail", "suggested_fix"],
        }},
    },
    "required": ["findings"],
}


# ------------------------------------------------------------------
# Objects and columns the model is allowed to use
# ------------------------------------------------------------------
def objects_in_text(text: str, known: List[str]) -> List[str]:
    """Qualified names in the text, plus leaf names that match a known object."""
    upper = str(text or "").upper()
    by_leaf = {k.upper().rsplit(".", 1)[-1]: k.upper() for k in known if k}
    found = list(_QUALIFIED.findall(upper))
    found += [by_leaf[l] for l in _LEAF.findall(upper) if l in by_leaf]
    return list(dict.fromkeys(found))


def column_context(objects: List[str], columns_fn: Callable[[str], List[str]],
                   cap: int = 120) -> Dict[str, List[str]]:
    """{object: [columns]} for every object whose columns can be read."""
    out: Dict[str, List[str]] = {}
    for obj in objects:
        if not obj or obj in out:
            continue
        try:
            cols = [str(c).upper() for c in (columns_fn(obj) or [])]
        except Exception:
            cols = []
        out[obj] = cols[:cap]
    return out


# ------------------------------------------------------------------
# 1. Extraction
# ------------------------------------------------------------------
def build_prompt(text: str, *, section: str = "", sources: List[str] = (),
                 target: str = "", columns: Dict[str, List[str]] = None,
                 neighbours: List[str] = (), draft: Optional[Dict] = None) -> str:
    columns = columns or {}
    listing = "\n".join(
        f"- {obj}: {', '.join(cols) if cols else '(columns could not be read)'}"
        for obj, cols in columns.items()) or "- (no object columns available)"
    hop = (f"Reads from: {', '.join(sources) or '(not stated)'}\n"
           f"Writes to: {target or '(not stated)'}")
    others = "\n".join(f"- {n[:300]}" for n in neighbours if n) or "- (none)"
    return f"""You turn one rule from a Functional Design Document (FDD) into structured rule metadata for a data pipeline. Be exact; a reviewer will compare your answer with the sentence.

PIPELINE HOP
{hop}

OBJECTS AND THEIR REAL COLUMNS (from Snowflake)
{listing}

SECTION HEADING (context only, not a rule): {section or '(none)'}

OTHER RULES ON THIS HOP (context only - do not extract them):
{others}

RULE TO EXTRACT:
{text}

How to answer:
- Use only column names from the lists above. If the rule refers to a column that is in no list, or describes a value without naming a column, use "{UNKNOWN}" as the name and explain in doubts. Never make up a column name.
- Use full DATABASE.SCHEMA.OBJECT names from the list for join objects. "metadata" means {METADATA_VIEW} when it is listed above; its rows are selected by SUBJECT_AREA_NM, ENTITY_NM and BUSINESS_RULE_NM. If the rule names a table that is not listed, use the name as written and say so in doubts.
- Join keys pair up in the order written: "join A_COL1, A_COL2 in X with B_COL1, B_COL2 in Y" gives A_COL1=B_COL1 and A_COL2=B_COL2.
- "SOURCE_COL as TARGET_COL" means TARGET_COL takes its value from SOURCE_COL: action COPY, target_field TARGET_COL, action_value SOURCE_COL.
- A filter that keeps rows ("bring only", "include only", "where ...") is conditions plus an INCLUDE_RECORD action. A rule that adds rows ("new record", "union") is an INCLUDE_RECORD action whose action_value describes the new row, plus SET_VALUE actions for the values it sets.
- A value derived by IF/ELSE is one DERIVE action whose action_value is the CASE logic, or SET_VALUE plus DEFAULT.
- Never list columns one by one when the rule does not. "Load all records / all data", "no transformation" or "straight load" is ONE action: NO_TRANSFORMATION with target_field RECORD. "Rest of the columns from the parent / source" is ONE action: CARRY_FORWARD with target_field "*". List a column only when the rule gives it its own value or mapping.
- Represent every instruction in the rule. Anything you cannot represent goes into doubts.
- If the rule contradicts itself, is incomplete, or refers to something undefined (another document, an unnamed table), describe it in fdd_issues. Do not resolve it by guessing.
- business_description: one plain sentence a business user would understand.

A rough deterministic parse of the same rule (may be wrong or incomplete):
{json.dumps(draft or {})}
"""


def extract(text: str, *, llm_fn: Callable, schema: Dict, section: str = "",
            sources: List[str] = (), target: str = "",
            columns: Dict[str, List[str]] = None, neighbours: List[str] = (),
            draft: Optional[Dict] = None, model: str = "") -> Optional[Dict]:
    prompt = build_prompt(text, section=section, sources=list(sources), target=target,
                          columns=columns, neighbours=list(neighbours), draft=draft)
    result = llm_fn(prompt, schema, model) if model else llm_fn(prompt, schema)
    if not isinstance(result, dict):
        return None
    result.setdefault("doubts", [])
    result.setdefault("fdd_issues", [])
    return result


# ------------------------------------------------------------------
# 2. Independent review
# ------------------------------------------------------------------
def review(text: str, proposal: Dict, *, llm_fn: Callable, section: str = "",
           model: str = "") -> List[Dict]:
    """Findings from a second model call that checks the rule against the sentence."""
    shown = {k: proposal.get(k) for k in ("rule_category", "conditions", "actions", "joins")}
    prompt = f"""You are reviewing a rule extracted from a Functional Design Document.
Compare the EXTRACTED RULE with the ORIGINAL TEXT and list every problem:
- missing: an instruction, condition, join or join key in the text that the rule does not capture
- wrong: a value, column, operator or direction that differs from the text (e.g. a mapping reversed)
- invented: a condition, column or value that the text does not state
- contradiction: the text itself contradicts itself or is incomplete
For each problem give suggested_fix: one short sentence saying what to change. When
the fix is a single value in the EXTRACTED RULE, also give patch: section ("actions"
or "conditions"), index (0-based position in that list), field (e.g. target_field,
action_value, field_name, field_value, operator) and the new value. Otherwise use
section "none".
Return an empty list when the rule is faithful. Do not comment on style.

SECTION HEADING: {section or '(none)'}
ORIGINAL TEXT:
{text}

EXTRACTED RULE:
{json.dumps(shown, indent=1)}
"""
    result = llm_fn(prompt, REVIEW_SCHEMA, model) if model else llm_fn(prompt, REVIEW_SCHEMA)
    if not isinstance(result, dict):
        return []
    return [f for f in result.get("findings") or [] if isinstance(f, dict) and f.get("detail")]


# ------------------------------------------------------------------
# 3. Deterministic checks
# ------------------------------------------------------------------
def checks(text: str, proposal: Dict, *, known_objects: List[str],
           implicit: List[str] = ()) -> List[Dict]:
    """Problems detectable without a model. Each: {severity, detail}.

    implicit: the hop's own target and primary source - a rule does not need to name
    the object it reads from or writes to, so those are never "named but unused".
    """
    issues: List[Dict] = []
    joins = proposal.get("joins") or []
    if _JOIN_WORD.search(text or "") and not joins:
        issues.append({"severity": AMBER,
                       "detail": "The text describes a join, but no join was captured."})
    for join in joins:
        if not join.get("join_columns"):
            issues.append({"severity": AMBER, "detail":
                           f"Join to {str(join.get('right_object', '')).rsplit('.', 1)[-1]} "
                           "has no join keys."})

    used = " ".join(
        [str(j.get("left_object", "")) + " " + str(j.get("right_object", "")) for j in joins]
        + [str(a.get("action_value", "")) for a in proposal.get("actions") or []]
        + [str(c.get("field_value", "")) for c in proposal.get("conditions") or []]
    ).upper()
    hop = {str(o).upper() for o in implicit if o}
    for obj in objects_in_text(text, known_objects):
        leaf = obj.rsplit(".", 1)[-1]
        if obj in hop or leaf in used:
            continue
        issues.append({"severity": AMBER,
                       "detail": f"{leaf} is named in the text but not used by the rule."})

    names = ([c.get("field_name") for c in proposal.get("conditions") or []]
             + [a.get("target_field") for a in proposal.get("actions") or []])
    if any(str(n or "").upper() == UNKNOWN for n in names):
        issues.append({"severity": RED, "detail":
                       "The FDD does not name a column for part of this rule (marked UNKNOWN)."})
    return issues


# ------------------------------------------------------------------
# 4. One status per rule
# ------------------------------------------------------------------
_RED_WARNINGS = ("is not in the target objects", "not in Snowflake or the repository")


def assess(*, errors: List[str], warnings: List[str], doubts: List[str],
           fdd_issues: List[str], findings: List[Dict], check_issues: List[Dict]) -> Dict:
    """{status, reasons} - reasons are what the reviewer should look at, worst first."""
    red: List[str] = list(errors or [])
    amber: List[str] = []
    for w in warnings or []:
        (red if any(k in w for k in _RED_WARNINGS) else amber).append(w)
    for issue in check_issues or []:
        (red if issue.get("severity") == RED else amber).append(issue["detail"])
    for f in findings or []:
        line = f"Reviewer ({f.get('kind', 'other')}): {f['detail']}"
        (red if f.get("severity") == "high" else amber).append(line)
    amber += [f"Model doubt: {d}" for d in doubts or [] if str(d).strip()]
    fdd = [f"FDD issue: {d}" for d in fdd_issues or [] if str(d).strip()]
    # An FDD problem blocks nothing in the app, but it cannot be "verified" either.
    amber += fdd
    status = RED if red else AMBER if amber else GREEN
    return {"status": status, "reasons": red + amber, "fdd_issues": list(fdd_issues or [])}


# ------------------------------------------------------------------
# 5. Issues a reviewer can act on
# ------------------------------------------------------------------
# An issue is one short question with an answer attached:
#   {id, level: fix|check|note, kind, title, detail,
#    suggestion: {label, patch} | None,
#    choices:    {label, options: [...], patch: {... "value": None}} | None,
#    remove:     patch | None}
# The page renders the suggestion as one button and the choices as a dropdown, so
# the reviewer clicks instead of reading paragraphs. apply_patch() makes the change
# and requirement_compiler.regrade() re-checks the rule without a model call, so the
# status updates at once.
FIX, CHECK, NOTE = "fix", "check", "note"
_LEVEL_ORDER = {FIX: 0, CHECK: 1, NOTE: 2}
_COLUMN_TOKEN = re.compile(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b")
_SKIP_FIELDS = {"", "RECORD", "*", "INCLUDE_RECORD", "EXCLUDE_RECORD"}


def _issue(level, kind, title, detail="", suggestion=None, choices=None) -> Dict:
    ident_ = f"{kind}:{re.sub(r'[^a-z0-9]+', '-', title.lower())[:60]}"
    return {"id": ident_, "level": level, "kind": kind, "title": title,
            "detail": detail, "suggestion": suggestion, "choices": choices, "remove": None}


def short(text: str, words: int = 12) -> str:
    """First sentence, at most `words` words - enough to decide whether to open it."""
    first = re.split(r"(?<=[.;])\s+", str(text or "").strip(), maxsplit=1)[0]
    parts = first.split()
    return " ".join(parts[:words]) + (" ..." if len(parts) > words else "")


def _rank_columns(candidates: List[str], hints: str, bad: str = "",
                  taken: set = frozenset()) -> List[str]:
    """Candidate columns, best first: named in the hints (the model's own doubts
    usually name the likely column), then close to the bad name, then sharing a word
    with the hints, then the rest. Columns the rule already sets elsewhere go last -
    rule 4 sets ITEM_LOCATION_TYPE_CD in its next action, so that is not the
    "location" column."""
    hints_upper = str(hints or "").upper()
    named = [c for c in candidates if re.search(rf"\b{re.escape(c)}\b", hints_upper)]
    close = (difflib.get_close_matches(str(bad).upper(), candidates, n=3, cutoff=0.6)
             if bad else [])
    words = set(re.findall(r"[A-Z]{3,}", hints_upper))
    sharing = [c for c in candidates if set(c.split("_")) & words]
    ranked = list(dict.fromkeys(named + close + sharing + list(candidates)))
    return [c for c in ranked if c not in taken] + [c for c in ranked if c in taken]


def _guess_keys(left_cols: List[str], right_cols: List[str], text: str) -> List[Dict]:
    """Join keys both objects have, preferring the ones the text mentions."""
    right = set(right_cols or [])
    common = [c for c in left_cols or [] if c in right]
    named = [c for c in common if re.search(rf"\b{re.escape(c)}\b", str(text or "").upper())]
    keys = named or [c for c in common if c in ("ITEM_ID", "PLANT_CD")]
    return [{"left_column": k, "right_column": k} for k in keys]


def deterministic_issues(text: str, proposal: Dict, *, columns: Dict[str, List[str]],
                         target: str = "", sources: List[str] = (),
                         errors: List[str] = (), extract_joins_fn: Callable = None,
                         hints: str = "") -> List[Dict]:
    """Issues found without a model, each with a ready-made fix where one exists."""
    issues: List[Dict] = []
    known = {c for cols in columns.values() for c in cols}
    target_cols = list(columns.get(str(target).upper()) or columns.get(target) or [])
    hint_text = f"{text} {hints}"
    sources = [s for s in sources if s]

    for message in errors or []:
        issues.append(_issue(FIX, "invalid", short(message, 14), message))

    # Columns that do not exist, or that the FDD never named.
    for section, field in (("conditions", "field_name"), ("actions", "target_field")):
        for i, row in enumerate(proposal.get(section) or []):
            name = str(row.get(field) or "").strip().upper()
            if name in _SKIP_FIELDS or not known:
                continue
            if name != UNKNOWN and name in known:
                continue
            pool = target_cols if section == "actions" and target_cols else sorted(known)
            taken = {str(r.get(field) or "").upper()
                     for j, r in enumerate(proposal.get(section) or []) if j != i}
            ranked = _rank_columns(pool, hint_text, "" if name == UNKNOWN else name, taken)
            what = "Condition column" if section == "conditions" else "Column to set"
            title = (f"{what} not named in the FDD" if name == UNKNOWN
                     else f"{name} is not a real column")
            best = ranked[0] if ranked else None
            where = {"op": "set", "section": section, "index": i, "field": field}
            issue = _issue(
                FIX, "column", title,
                f"{what} {i + 1} uses {name}. "
                + (f"Closest real column: {best}." if best else "No close match found."),
                suggestion=({"label": f"Use {best}", "patch": {**where, "value": best}}
                            if best else None),
                choices={"label": "Or pick a column", "options": ranked[:40],
                         "patch": {**where, "value": None}})
            issue["id"] += f"-{section}-{i}"
            issue["remove"] = {"op": "remove", "section": section, "index": i}
            issues.append(issue)

    # A join the text describes but the rule does not have.
    joins = proposal.get("joins") or []
    if _JOIN_WORD.search(text or "") and not joins:
        guessed: List[Dict] = []
        if extract_joins_fn is not None:
            try:
                guessed = list(extract_joins_fn(text, (sources or [""])[0]) or [])
            except Exception:
                guessed = []
        if not guessed and mentions_metadata(text):
            guessed = [{"left_object": (sources or [target])[0], "right_object": METADATA_VIEW,
                        "join_type": "INNER", "join_purpose": "business rule metadata",
                        "join_columns": []}]
        if guessed:
            j = dict(guessed[0])
            if not j.get("join_columns"):
                j["join_columns"] = _guess_keys(columns.get(j["left_object"], []),
                                                columns.get(j["right_object"], []), text)
            keys = ", ".join(f"{k['left_column']}={k['right_column']}"
                             for k in j.get("join_columns") or []) or "no keys yet"
            right = j["right_object"].rsplit(".", 1)[-1]
            issues.append(_issue(
                FIX, "join", f"Join to {right} not captured",
                f"The text describes a join. Suggested: "
                f"{str(j['left_object']).rsplit('.', 1)[-1]} to {right} on {keys}.",
                suggestion={"label": "Add join", "patch": {"op": "add_join", "join": j}}))
        else:
            issues.append(_issue(CHECK, "join", "Join described but not captured",
                                 "The text mentions a join but names no object to join to."))

    for i, join in enumerate(joins):
        if join.get("join_columns"):
            continue
        keys = _guess_keys(columns.get(join.get("left_object", ""), []),
                           columns.get(join.get("right_object", ""), []), text)
        right = str(join.get("right_object", "")).rsplit(".", 1)[-1]
        issue = _issue(
            CHECK if keys else FIX, "join_keys", f"Join to {right} has no keys",
            "Suggested keys are columns both objects have, preferring ones the text names.",
            suggestion=({"label": "Use " + ", ".join(k["left_column"] for k in keys),
                         "patch": {"op": "set_join_columns", "index": i, "columns": keys}}
                        if keys else None))
        issue["id"] += f"-{i}"
        issues.append(issue)

    # Objects the text names that the rule never uses.
    used = " ".join(
        [f"{j.get('left_object', '')} {j.get('right_object', '')}" for j in joins]
        + [str(a.get("action_value", "")) for a in proposal.get("actions") or []]
        + [str(c.get("field_value", "")) for c in proposal.get("conditions") or []]).upper()
    implicit = {str(o).upper() for o in [target, *sources[:1]] if o}
    for obj in objects_in_text(text, list(columns)):
        leaf = obj.rsplit(".", 1)[-1]
        if obj in implicit or leaf in used:
            continue
        left = (sources or [target])[0]
        keys = _guess_keys(columns.get(left, []), columns.get(obj, []), text)
        issues.append(_issue(
            CHECK, "unused_object", f"{leaf} is named but not used",
            "The rule text names this object, but no join or value uses it.",
            suggestion={"label": f"Join to {leaf}",
                        "patch": {"op": "add_join", "join": {
                            "left_object": left, "right_object": obj, "join_type": "INNER",
                            "join_purpose": "named in the rule", "join_columns": keys}}}))
    return issues


def model_issues(*, doubts: List[str] = (), fdd_issues: List[str] = (),
                 findings: List[Dict] = ()) -> List[Dict]:
    """Issues from the model's doubts, its FDD issues and the reviewer's findings."""
    issues: List[Dict] = []
    for f in findings or []:
        level = {"high": FIX, "medium": CHECK}.get(f.get("severity"), NOTE)
        patch = f.get("patch") or {}
        suggestion = None
        if patch.get("section") in ("actions", "conditions") and patch.get("field"):
            try:
                index = int(patch.get("index") or 0)
            except (TypeError, ValueError):
                index = 0
            value = patch.get("value", "")
            label = short(f.get("suggested_fix") or f"Set {patch['field']} to {value}", 8)
            if str(value or "").strip().lower() in _REMOVE_WORDS:
                # The reviewer means "drop this row", not "set the field to 'remove'".
                suggestion = {"label": label, "patch": {"op": "remove", "section": patch["section"],
                                                        "index": index}}
            elif patch["field"] in _NUMERIC_FIELDS and not str(value or "").strip().isdigit():
                suggestion = None   # a word in a number field is not a safe fix
            else:
                suggestion = {"label": label,
                              "patch": {"op": "set", "section": patch["section"], "index": index,
                                        "field": patch["field"], "value": value}}
        detail = f["detail"] + (f"\n\nSuggested: {f['suggested_fix']}"
                                if f.get("suggested_fix") else "")
        issues.append(_issue(level, f"review_{f.get('kind', 'other')}", short(f["detail"]),
                             detail, suggestion=suggestion))
    for d in doubts or []:
        if str(d).strip():
            issues.append(_issue(CHECK, "doubt", short(d), str(d)))
    for d in fdd_issues or []:
        if str(d).strip():
            issues.append(_issue(NOTE, "fdd", short(d), str(d)))
    return issues


def _tokens(issue: Dict) -> set:
    return set(_COLUMN_TOKEN.findall(f"{issue['title']} {issue['detail']}".upper()))


def merge_issues(issues: List[Dict]) -> List[Dict]:
    """One item per problem, worst first.

    The model's doubt, the FDD issue and the reviewer's finding often describe the
    same gap three times ("location column not named"). A model-sourced issue is
    folded into an earlier one when the titles read nearly the same, or when it
    names a column the earlier one already names. The survivor keeps the higher
    level and the other text as extra detail. Checks found without a model are
    never folded into each other - each has its own fix.
    """
    ordered = sorted(issues, key=lambda i: (_LEVEL_ORDER.get(i["level"], 3),
                                            0 if i.get("suggestion") else 1))
    kept: List[Dict] = []
    for issue in ordered:
        from_model = issue["kind"] in ("doubt", "fdd") or issue["kind"].startswith("review")
        host = None
        if from_model:
            mine = _tokens(issue)
            for other in kept:
                similar = difflib.SequenceMatcher(
                    None, issue["title"].lower(), other["title"].lower()).ratio() > 0.6
                if similar or (mine and mine & _tokens(other)):
                    host = other
                    break
        if host is None:
            kept.append(dict(issue))
            continue
        if issue["detail"] and issue["detail"] not in host["detail"]:
            host["detail"] = f"{host['detail']}\n\n{issue['detail']}".strip()
        if issue["kind"] == "fdd":
            host["also_fdd"] = True
        if not host.get("suggestion") and issue.get("suggestion"):
            host["suggestion"] = issue["suggestion"]
    return kept


def open_issues(issues: List[Dict], resolutions: Dict = None) -> List[Dict]:
    """Issues not yet fixed, accepted or noted."""
    return [i for i in issues or [] if i["id"] not in (resolutions or {})]


def status_of(issues: List[Dict], resolutions: Dict = None) -> str:
    levels = {i["level"] for i in open_issues(issues, resolutions)}
    return RED if FIX in levels else AMBER if levels else GREEN


def _one_clear_fix(issue: Dict) -> bool:
    """A suggestion with no competing choices - safe to apply without asking."""
    if not (issue.get("suggestion") or {}).get("patch"):
        return False
    options = (issue.get("choices") or {}).get("options") or []
    return len(options) <= 1


def auto_resolve(entry: Dict, regrade: Callable[[Dict], None], limit: int = 12) -> Dict:
    """Settle everything on one import rule that does not need a person.

    1. Apply every suggestion that has no competing choice, one at a time (a fix can
       change the next issue), recorded as "auto-fixed" so Undo still works.
    2. Mark every remaining check as "accepted" and every note as "noted". The import
       turns those into review comments on the module - exactly what the reviewer's
       Ignore button did, without the click.
    3. Leave open only "fix" level issues with no safe fix: those are what a person
       has to look at, and they keep the rule red (unticked) until handled.

    regrade(proposal) must re-check the rule in place (entry["proposal"], ["issues"],
    ["errors"] ...); it is injected so this runs without Snowflake in tests.
    Returns counts: {"fixed": n, "accepted": n, "noted": n, "open": n}.
    """
    counts = {"fixed": 0, "accepted": 0, "noted": 0, "open": 0}
    resolutions = entry.setdefault("resolutions", {})
    for _ in range(limit):
        issue = next((i for i in open_issues(entry.get("issues"), resolutions)
                      if _one_clear_fix(i)), None)
        if issue is None:
            break
        entry.setdefault("original_proposal", copy.deepcopy(entry.get("proposal")))
        patch = issue["suggestion"]["patch"]
        regrade(apply_patch(entry.get("proposal"), patch))
        resolutions = entry.setdefault("resolutions", resolutions)
        resolutions[issue["id"]] = {"how": "auto-fixed", "reason": "", "title": issue["title"],
                                    "detail": issue.get("detail", ""), "level": issue["level"]}
        counts["fixed"] += 1
    for issue in open_issues(entry.get("issues"), resolutions):
        if issue["level"] == FIX:
            counts["open"] += 1
            continue
        how = "noted" if issue["level"] == NOTE else "accepted"
        resolutions[issue["id"]] = {"how": how, "reason": "settled automatically on import",
                                    "title": issue["title"], "detail": issue.get("detail", ""),
                                    "level": issue["level"]}
        counts[how] += 1
    return counts


def apply_patch(proposal: Dict, patch: Dict) -> Dict:
    """A copy of the proposal with one suggested fix applied."""
    out = copy.deepcopy(proposal or {})
    op = (patch or {}).get("op")
    if op == "set":
        rows = out.setdefault(patch["section"], [])
        if 0 <= patch["index"] < len(rows):
            rows[patch["index"]][patch["field"]] = patch.get("value") or ""
    elif op == "remove":
        rows = out.get(patch["section"]) or []
        if 0 <= patch["index"] < len(rows):
            rows.pop(patch["index"])
    elif op == "add_join":
        out.setdefault("joins", []).append(copy.deepcopy(patch["join"]))
    elif op == "set_join_columns":
        joins = out.get("joins") or []
        if 0 <= patch["index"] < len(joins):
            joins[patch["index"]]["join_columns"] = list(patch.get("columns") or [])
    return out


def related_model_issues(issues: List[Dict], patch: Dict, resolutions: Dict = None) -> List[Dict]:
    """Open model doubts / FDD notes about the thing a fix just settled.

    A fix that sets PLANT_CD settles "PLANT_CD is the closest column for location" as
    well; leaving it open made the reviewer click twice for one problem.
    """
    value = str((patch or {}).get("value") or "").upper()
    names = {value} if _COLUMN_TOKEN.fullmatch(value or "-") else set()
    join = (patch or {}).get("join") or {}
    if join.get("right_object"):
        names.add(str(join["right_object"]).upper().rsplit(".", 1)[-1])
    if not names:
        return []
    return [i for i in open_issues(issues, resolutions)
            if (i["kind"] in ("doubt", "fdd") or i["kind"].startswith("review"))
            and names & _tokens(i)]

