"""
Turns denormalized rule rows into the "IF this, THEN that" language a BA
actually writes in an FDD.

No LLM is involved. The same rule always reads the same way, it renders in
milliseconds, and it cannot invent a condition that is not in the tables.
The LLM only sees this text later, which is also why the agent got faster:
a rule that was 8 KB of JSON is about 400 characters of English.
"""

import re
from typing import Dict, List

# Rules that are their sentence (as_written.py), plus the older CONTEXT prose rules.
TEXT_CATEGORIES = {"AS_WRITTEN", "CONTEXT"}

# ------------------------------------------------------------------
# Vocabulary
# ------------------------------------------------------------------
OPERATOR_PHRASE = {
    "=": "is", "==": "is", "EQ": "is", "EQUALS": "is",
    "!=": "is not", "<>": "is not", "NE": "is not",
    ">": "is greater than", ">=": "is greater than or equal to",
    "<": "is less than", "<=": "is less than or equal to",
    "IN": "is one of", "NOT IN": "is not one of",
    "LIKE": "matches the pattern", "NOT LIKE": "does not match the pattern",
    "IS NULL": "is blank", "ISNULL": "is blank", "NULL": "is blank",
    "IS BLANK": "is blank", "IS NOT BLANK": "has a value", "HAS VALUE": "has a value",
    "IS NOT NULL": "has a value", "NOT NULL": "has a value",
    "BETWEEN": "is between", "STARTS WITH": "starts with",
    "ENDS WITH": "ends with", "CONTAINS": "contains",
    "EXISTS": "exists in", "NOT EXISTS": "does not exist in",
}

NO_VALUE_OPERATORS = {
    "IS NULL", "ISNULL", "NULL", "IS NOT NULL", "NOT NULL", "EXISTS", "NOT EXISTS",
    "IS BLANK", "IS NOT BLANK", "HAS VALUE",
}


def _operator_key(operator) -> str:
    """IS_BLANK, 'is blank' and IS BLANK are all the same operator."""
    return re.sub(r"\s+", " ", str(operator or "").replace("_", " ")).strip().upper()

# Column abbreviations, so ITEM_DSC reads as "Item Description" for the BA.
ABBREVIATIONS = {
    "DSC": "Description", "DESC": "Description", "QTY": "Quantity",
    "IND": "Indicator", "FLG": "Flag", "DT": "Date", "TS": "Timestamp",
    "CD": "Code", "NBR": "Number", "NUM": "Number", "AMT": "Amount",
    "PCT": "Percent", "STS": "Status", "ORG": "Organization", "ID": "ID",
    "UOM": "Unit of Measure", "CURR": "Currency", "SRC": "Source",
    "TGT": "Target", "EFF": "Effective", "EXP": "Expiry", "MTRL": "Material",
    "PLNT": "Plant", "SLS": "Sales", "CHAR": "Characteristic", "ATTR": "Attribute",
    "REC": "Record", "SEQ": "Sequence", "GRP": "Group", "CAT": "Category",
    "BOM": "BOM", "PDH": "PDH", "UPI": "UPI", "PIT": "PIT", "DQ": "DQ",
    "MIN": "Minimum", "MAX": "Maximum", "AVG": "Average", "PRC": "Price",
    "NM": "Name", "TXT": "Text", "DTM": "Timestamp", "QT": "Quantity",
    "CHG": "Change", "LOC": "Location", "DTL": "Detail", "HDR": "Header",
}

CATEGORY_INTENT = {
    "FILTER": "Decides which source records are allowed into the target.",
    "ACTIVE_STATUS": "Decides whether a record counts as active.",
    "INITIAL_LOAD": "Applies only to the first full load.",
    "DERIVATION": "Calculates a target value from source values.",
    "METADATA_LOOKUP": "Looks a value up from a reference object.",
    "METADATA_DERIVATION": "Derives a value using reference metadata.",
    "STANDARDIZATION": "Cleans and standardizes a value.",
    "AGGREGATION": "Summarizes many source rows into one target row.",
    "JOIN_FILTER": "Restricts rows while joining two objects.",
    "DEDUPLICATION": "Keeps one row where the source has many.",
    "NO_TRANSFORMATION": "Carries the source value across unchanged.",
    "DATA_QUALITY": "Checks the data meets an expectation and flags what does not.",
    "ATTRIBUTE_CARRY_FORWARD": "Keeps the previous value when the source is blank.",
    "TIME_TRAVEL": "Selects the version of the record valid at a point in time.",
    "KEY_DEFINITION": "Defines the key that identifies a target record.",
}


def humanize(name) -> str:
    """ITEM_DSC -> Item Description"""
    if not name:
        return ""
    words = []
    for token in str(name).replace(".", " ").split("_"):
        if not token:
            continue
        upper = token.upper()
        words.append(ABBREVIATIONS.get(upper, token.capitalize()))
    return " ".join(words)


# ------------------------------------------------------------------
# Saying each thing once
# ------------------------------------------------------------------
# The tables carry the same fact in several columns: BUSINESS_DESCRIPTION
# restates the conditions, ACTION_DESCRIPTION restates the action, and
# RULE_CATEGORY restates both. Rendering all of them is why a two line rule
# read as thirteen. _covered_by() decides what has already been said.
_FILLER = {
    "a", "an", "and", "the", "is", "are", "as", "at", "be", "by", "for", "from",
    "in", "into", "of", "on", "or", "so", "that", "this", "to", "with", "then",
    "when", "where", "if", "it", "its", "only", "also", "record", "records",
    "value", "values", "set", "sets", "consider", "considered", "please", "both",
}

# The tables and the notes use different verbs for the same act. Folding them
# is what lets 'Include only active records' be recognised as a restatement of
# 'keep only records where ACTIVE_IND = TRUE'.
_SYNONYM = {
    "include": "keep", "included": "keep", "includes": "keep", "including": "keep",
    "bring": "keep", "brings": "keep", "load": "keep", "loads": "keep",
    "allow": "keep", "allowed": "keep", "filter": "keep", "filtered": "keep",
    "exclude": "drop", "excluded": "drop", "remove": "drop", "removed": "drop",
    "reject": "drop", "rejected": "drop",
    "populate": "assign", "populated": "assign", "derive": "assign",
    "derived": "assign", "derives": "assign", "assigned": "assign",
    "derivation": "assign", "derivations": "assign",
    "mark": "assign", "marked": "assign", "marks": "assign", "stamp": "assign",
    "true": "yes", "false": "no", "y": "yes", "n": "no",
    "blank": "empty", "null": "empty", "nulls": "empty",
    "fetch": "lookup", "fetched": "lookup", "look": "lookup", "map": "lookup",
    "mapping": "lookup", "mapped": "lookup", "match": "lookup", "matching": "lookup",
    "active": "activeflag", "inactive": "activeflag",
    "indicator": "flag", "ind": "flag", "status": "flag", "sts": "flag",
    "define": "identify", "defines": "identify", "defined": "identify",
    "identifies": "identify", "unique": "identify", "primary": "key",
    "column": "field", "columns": "field", "col": "field",
}


def _norm(text: str) -> set:
    """
    Content words of a sentence, with column names folded to their English
    form so 'ACTIVE_IND' and 'active indicator' compare equal, and near
    synonyms folded so 'include' and 'keep' compare equal.
    """
    raw = _val(text)
    if not raw:
        return set()
    expanded = re.sub(
        r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b",
        lambda m: humanize(m.group(0)),
        raw,
    )
    words = re.findall(r"[A-Za-z0-9]+", expanded.lower())
    return {_SYNONYM.get(w, w) for w in words if w not in _FILLER and len(w) > 1}


def _covered_by(candidate: str, established: set, threshold: float = 0.7) -> bool:
    """
    True when `candidate` says nothing that `established` has not already said.
    Short notes such as 'Set active indicator to true' are fully absorbed by
    the rendered action, so they get dropped.
    """
    words = _norm(candidate)
    if not words:
        return True
    overlap = len(words & established) / len(words)
    return overlap >= threshold


def _sentences(text: str) -> List[str]:
    """Splits prose without breaking on decimals or abbreviations like S4 MDG."""
    raw = _val(text)
    if not raw:
        return []
    parts = re.split(r"(?<=[.;])\s+(?=[A-Z(])", raw)
    return [p.strip() for p in parts if p.strip()]


def _val(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("nan", "none", "null") else text


def _quote(value: str) -> str:
    text = _val(value)
    if not text:
        return ""
    if text.startswith("(") or text.upper().startswith("SELECT"):
        return text
    if any(ch in text for ch in "()=<>+*|") or (" " in text and text.isupper()):
        return text
    return f"'{text}'"


# ------------------------------------------------------------------
# Conditions
# ------------------------------------------------------------------
def _label(field: str, seen=None) -> str:
    """
    'Active Indicator (ACTIVE_IND)' the first time it appears in a rule,
    'ACTIVE_IND' after that. Repeating the gloss on every mention was a large
    part of the noise.
    """
    name = _val(field)
    if not name:
        return "the record"
    if seen is None:
        return f"{humanize(name)} ({name})"
    if name in seen:
        return name
    seen.add(name)
    pretty = humanize(name)
    return f"{pretty} ({name})" if pretty.upper() != name.upper() else name


def condition_sentence(cond: Dict, seen=None) -> str:
    field = _val(cond.get("FIELD_NAME"))
    operator = _operator_key(cond.get("OPERATOR")) or "="
    value = _val(cond.get("FIELD_VALUE"))
    phrase = OPERATOR_PHRASE.get(operator, operator.lower())

    label = _label(field, seen)
    if operator in NO_VALUE_OPERATORS or not value:
        return f"{label} {phrase}".strip()
    if operator in ("IN", "NOT IN"):
        return f"{label} {phrase} {value}"
    if operator == "BETWEEN":
        return f"{label} {phrase} {value}"
    return f"{label} {phrase} {_quote(value)}"


def condition_block(conditions: List[Dict], seen=None) -> List[str]:
    """
    Rows inside one CONDITION_GROUP are chained with their own
    LOGICAL_OPERATOR. Separate groups are alternatives (OR), rendered as
    "Case 1 / Case 2" so a BA can see there are two ways in.
    """
    if not conditions:
        return []

    groups: Dict[int, List[Dict]] = {}
    for cond in conditions:
        try:
            key = int(cond.get("CONDITION_GROUP") or 1)
        except (TypeError, ValueError):
            key = 1
        groups.setdefault(key, []).append(cond)

    for key in groups:
        groups[key].sort(key=lambda c: int(c.get("CONDITION_SEQ") or 0))

    lines: List[str] = []
    multi = len(groups) > 1
    for idx, key in enumerate(sorted(groups)):
        rows = groups[key]
        parts = []
        for pos, cond in enumerate(rows):
            joiner = "" if pos == 0 else (_val(cond.get("LOGICAL_OPERATOR")).lower() or "and")
            parts.append(f"{joiner + ' ' if joiner else ''}{condition_sentence(cond, seen)}")
        clause = " ".join(parts)
        if multi:
            lines.append(f"either case {idx + 1} — {clause}" if idx == 0 else f"or case {idx + 1} — {clause}")
        else:
            lines.append(clause)
    return lines


# ------------------------------------------------------------------
# Actions
# ------------------------------------------------------------------
# ACTION_VALUE sometimes repeats the ACTION_TYPE instead of holding a value,
# which is how "copy PASS_THROUGH into Record (RECORD) unchanged" happened.
PASSTHROUGH_TOKENS = {
    "PASS_THROUGH", "PASSTHROUGH", "COPY", "NO_TRANSFORMATION", "DIRECT_MAP",
    "AS_IS", "ASIS", "SAME", "NONE",
}


def _is_passthrough_token(value: str) -> bool:
    return _val(value).replace(" ", "_").upper() in PASSTHROUGH_TOKENS


# Plain English for the expressions a developer needs to see verbatim.
EXPRESSION_GLOSS = [
    (r"^LTRIM\s*\(\s*\w+\s*,\s*'0'\s*\)$", "strip leading zeros"),
    (r"^RTRIM\s*\(", "strip trailing characters"),
    (r"^TRIM\s*\(", "remove surrounding spaces"),
    (r"^UPPER\s*\(", "convert to upper case"),
    (r"^LOWER\s*\(", "convert to lower case"),
    (r"^MIN\s*\(", "take the smallest value"),
    (r"^MAX\s*\(", "take the largest value"),
    (r"^SUM\s*\(", "add the values together"),
    (r"^COUNT\s*\(", "count the rows"),
    (r"^AVG\s*\(", "take the average"),
    (r"^COALESCE\s*\(", "use the first value that is not blank"),
    (r"^SUBSTR(?:ING)?\s*\(", "take part of the value"),
    (r"^CONCAT\s*\(|\|\|", "join the values together"),
]


def _looks_like_expression(value: str) -> bool:
    text = _val(value)
    return bool(text) and (any(ch in text for ch in "()|") or "=" in text)


def _expression(value: str) -> str:
    """Backticked so it stands out, with a plain reading when we recognise it."""
    text = _val(value)
    if not text:
        return ""
    if not _looks_like_expression(text):
        return text
    gloss = ""
    for pattern, phrase in EXPRESSION_GLOSS:
        if re.search(pattern, text, re.IGNORECASE):
            gloss = phrase
            break
    return f"`{text}`" + (f" — {gloss}" if gloss else "")


def action_sentence(action: Dict, seen=None) -> str:
    target = _val(action.get("TARGET_FIELD"))
    kind = (_val(action.get("ACTION_TYPE")) or "SET_VALUE").upper()
    value = _val(action.get("ACTION_VALUE"))
    whole_record = target.upper() in ("RECORD", "ROW", "")
    label = "the record" if whole_record else _label(target, seen)

    if kind in ("COPY", "PASS_THROUGH", "NO_TRANSFORMATION", "DIRECT_MAP"):
        if whole_record:
            return "copy the record across unchanged"
        source = "the source value" if _is_passthrough_token(value) or not value else _val(value)
        return f"copy {source} into {label} unchanged"
    if kind in ("SET_VALUE", "ASSIGN", "SET"):
        return f"set {label} = {_quote(value) or 'the value below'}"
    if kind in ("SET_NULL", "NULLIFY"):
        return f"leave {label} blank"
    if kind in ("DERIVE", "CALCULATE", "EXPRESSION", "TRANSFORM"):
        return f"set {label} = {_expression(value)}" if value else f"derive {label}"
    if kind in ("INCLUDE", "INCLUDE_RECORD", "KEEP"):
        return f"keep only records where {_expression(value)}" if value else "keep the record"
    if kind in ("INCLUDE_MATCHING", "MATCH"):
        return f"keep records that match on {_expression(value)}" if value else "keep matching records"
    if kind in ("DEFINE_KEY", "KEY_DEFINITION", "SET_KEY"):
        return f"identify each target record by {_val(value) or label}"
    if kind in ("LOOKUP", "METADATA_LOOKUP"):
        return f"look {label} up from {_val(value)}"
    if kind == "CONCAT":
        return f"build {label} by joining {_val(value)}"
    if kind in ("SUBSTRING", "SUBSTR"):
        return f"take part of the value for {label}: {_val(value)}"
    if kind in ("TRIM", "UPPERCASE", "LOWERCASE", "STANDARDIZE", "FORMAT"):
        verb = {"TRIM": "remove leading and trailing spaces from",
                "UPPERCASE": "convert to upper case",
                "LOWERCASE": "convert to lower case",
                "STANDARDIZE": "standardize",
                "FORMAT": "reformat"}[kind]
        suffix = f" ({_val(value)})" if value else ""
        return f"{verb} {label}{suffix}"
    if kind in ("DEFAULT", "SET_DEFAULT"):
        return f"default {label} to {_quote(value)} when it is blank"
    if kind in ("EXCLUDE_RECORD", "FILTER_OUT", "REJECT"):
        # Targeting a field rather than the row means "do not populate this",
        # not "throw the row away" - e.g. no dummy component for UNBW items.
        return "exclude the record from the target" if whole_record else f"do not create {label}"
    if kind in ("AGGREGATE", "SUM", "COUNT", "MIN", "MAX", "AVG"):
        # MIN(MINIMUM_ORDER_QTY) reads better as the expression itself than as
        # "the aggregate of MIN(...)".
        if _looks_like_expression(value):
            return f"set {label} = {_expression(value)}"
        return f"set {label} to the {kind.lower()} of {_val(value)}"
    if kind in ("DEDUPLICATE", "DEDUP"):
        return f"keep one row per {_val(value) or label}"
    if kind in ("CARRY_FORWARD", "ATTRIBUTE_CARRY_FORWARD"):
        return f"keep the previous {label} when the source is blank"
    if kind in ("FLAG", "SET_FLAG"):
        return f"flag the record with {label} = {_quote(value)}"
    if kind in ("ERROR", "RAISE"):
        return f"raise a data quality error: {_val(value)}"
    return f"{kind.replace('_', ' ').lower()} {label}" + (f" using {_val(value)}" if value else "")


def action_block(actions: List[Dict], seen=None, established=None) -> List[str]:
    """
    One plain sentence per action. ACTION_DESCRIPTION is appended only when it
    adds something the sentence does not already say - most of them are
    restatements ("Set active indicator to true") and get dropped.
    """
    if not actions:
        return []
    ordered = sorted(actions, key=lambda a: int(a.get("ACTION_SEQ") or 0))
    lines = []
    for action in ordered:
        line = action_sentence(action, seen)
        note = _val(action.get("ACTION_DESCRIPTION"))
        if note:
            covered = _norm(line) | (established or set())
            if not _covered_by(note, covered):
                line += f" ({note.rstrip('.')})"
        lines.append(line)
    return lines


# ------------------------------------------------------------------
# Joins
# ------------------------------------------------------------------
def join_sentence(join: Dict, columns: List[Dict], filters: List[Dict]) -> str:
    left = _val(join.get("LEFT_OBJECT"))
    right = _val(join.get("RIGHT_OBJECT"))
    kind = (_val(join.get("JOIN_TYPE")) or "INNER").upper()
    pairs = [
        f"{_val(c.get('LEFT_COLUMN'))} = {_val(c.get('RIGHT_COLUMN'))}"
        for c in columns
        if _val(c.get("LEFT_COLUMN")) or _val(c.get("RIGHT_COLUMN"))
    ]
    how = {
        "INNER": "only where both sides match",
        "LEFT": "keeping every row from the left object",
        "LEFT OUTER": "keeping every row from the left object",
        "RIGHT": "keeping every row from the right object",
        "FULL": "keeping rows from both objects",
        "CROSS": "pairing every row with every row",
    }.get(kind, kind.lower())

    text = f"Match **{left}** to **{right}** on {', '.join(pairs) or 'the recorded key'} — {how}."
    if filters:
        parts = []
        for f in sorted(filters, key=lambda x: int(x.get("FILTER_SEQ") or 0)):
            parts.append(condition_sentence({
                "FIELD_NAME": f.get("FILTER_COLUMN"),
                "OPERATOR": f.get("FILTER_OPERATOR"),
                "FIELD_VALUE": f.get("FILTER_VALUE"),
            }))
        text += " Restricted to rows where " + " and ".join(parts) + "."
    purpose = _val(join.get("JOIN_PURPOSE"))
    if purpose:
        text += f" _Why: {purpose}_"
    return text


# ------------------------------------------------------------------
# Whole rule
# ------------------------------------------------------------------
# The headline answers "what is this rule about" in one sentence a manager or
# PO can read. Built from the category and the actions, never from an LLM, so
# the same rule always gets the same headline.
_SUMMARY_VERB = {
    "SET_VALUE": "sets", "ASSIGN": "sets", "SET": "sets",
    "DERIVE": "works out", "CALCULATE": "works out", "TRANSFORM": "reformats",
    "COPY": "copies", "PASS_THROUGH": "copies", "NO_TRANSFORMATION": "copies",
    "DIRECT_MAP": "copies", "LOOKUP": "looks up", "METADATA_LOOKUP": "looks up",
    "AGGREGATE": "summarises", "DEDUPLICATE": "removes duplicates from",
    "INCLUDE": "filters", "INCLUDE_MATCHING": "filters", "EXCLUDE_RECORD": "filters out",
    "DEFINE_KEY": "defines the key for",
}


def _lower_keep_acronyms(text: str) -> str:
    """
    Lowercase a humanized name for use inside a sentence, but leave acronyms
    alone: "BOM Hash" stays "BOM hash" rather than becoming "bom hash".
    """
    keep = {v.upper() for v in ABBREVIATIONS.values() if v.isupper()}
    return " ".join(w if w.upper() in keep else w.lower() for w in str(text).split())


def _join_words(items: List[str], limit: int = 3) -> str:
    """Caps the list so a five field rule does not get a runaway headline."""
    if len(items) > limit:
        extra = len(items) - limit
        return f"{', '.join(items[:limit])} and {extra} more field{'s' if extra > 1 else ''}"
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


# A filter rule usually records the same predicate twice: once as a condition
# and once as the action value. Stating it under both bullets is repetition.
FILTER_ACTION_KINDS = {
    "INCLUDE", "INCLUDE_RECORD", "KEEP", "FILTER", "INCLUDE_MATCHING", "MATCH",
    "EXCLUDE_RECORD", "FILTER_OUT", "REJECT",
}


def _drop_restated_filters(actions: List[Dict], cond_words: set) -> List[Dict]:
    if not cond_words:
        return actions
    kept = []
    for action in actions:
        kind = (_val(action.get("ACTION_TYPE")) or "").upper()
        words = _norm(action.get("ACTION_VALUE"))
        if kind in FILTER_ACTION_KINDS and words and words <= cond_words:
            continue
        kept.append(action)
    return kept


def rule_summary(bundle: Dict) -> str:
    """One jargon free sentence describing what the rule achieves."""
    rule = bundle.get("rule", {}) or {}
    category = _val(rule.get("RULE_CATEGORY")).upper()
    actions = bundle.get("actions", []) or []
    conditions = bundle.get("conditions", []) or []
    target = _val(rule.get("TARGET_BUSINESS_OBJECT")) or _val(rule.get("TARGET_TECHNICAL_OBJECT"))
    scope = "every record" if not conditions else "matching records"

    kinds = {(_val(a.get("ACTION_TYPE")) or "").upper() for a in actions}
    fields = [_val(a.get("TARGET_FIELD")) for a in actions if _val(a.get("TARGET_FIELD"))]
    named = [_lower_keep_acronyms(humanize(f)) for f in fields if f.upper() not in ("RECORD", "ROW")]

    if category in TEXT_CATEGORIES:
        return "As written in the FDD; developers build from the text."
    if category == "NO_TRANSFORMATION" or kinds & {"PASS_THROUGH", "NO_TRANSFORMATION", "DIRECT_MAP", "COPY"}:
        where = f" to {target}" if target else ""
        return f"Copies every record straight through{where}, unchanged."
    # Row-level filtering only counts when the action targets the whole record.
    # An EXCLUDE_RECORD aimed at a single field suppresses that field, and
    # calling it "lets only matching records through" would be wrong.
    row_filter_kinds = {"INCLUDE", "INCLUDE_RECORD", "EXCLUDE_RECORD", "FILTER_OUT"}
    row_filters = [a for a in actions
                   if (_val(a.get("ACTION_TYPE")) or "").upper() in row_filter_kinds
                   and _val(a.get("TARGET_FIELD")).upper() in ("RECORD", "ROW", "")]
    if category == "FILTER" or row_filters:
        return f"Lets only {scope} through to {target}." if target else f"Lets only {scope} through."
    if category == "KEY_DEFINITION" or kinds & {"DEFINE_KEY", "SET_KEY"}:
        return "Defines what makes a target record unique."
    # JOIN_FILTER is tested before INCLUDE_MATCHING on purpose: a referential
    # integrity check also records INCLUDE_MATCHING actions, and calling that
    # "matching the reference metadata" would be wrong - it is matching a real
    # object, not a metadata table.
    if category == "JOIN_FILTER":
        pair = ""
        for item in bundle.get("joins", []) or []:
            joined = item.get("join", {}) or {}
            left, right = _val(joined.get("LEFT_OBJECT")), _val(joined.get("RIGHT_OBJECT"))
            if left and right:
                pair = f" between {left.split('.')[-1]} and {right.split('.')[-1]}"
                break
        if not pair:
            for a in actions:
                value = _val(a.get("ACTION_VALUE"))
                if "=" in value:
                    pair = f" on {value}"
                    break
        return f"Controls which rows survive the join{pair}."
    if kinds & {"INCLUDE_MATCHING", "MATCH"}:
        # Name the field being matched, otherwise every metadata mapping rule
        # in a set gets the identical headline.
        matched = ""
        for a in actions:
            value = _val(a.get("ACTION_VALUE"))
            if "=" in value:
                matched = humanize(value.split("=")[0].strip()).lower()
                break
        return (
            f"Keeps only the records whose {matched} matches the reference metadata."
            if matched else "Keeps only the records that match the reference metadata."
        )
    if category == "ACTIVE_STATUS":
        # 2.1 sets TRUE, 2.2-2.4 set FALSE - say which, or the whole family
        # reads the same.
        values = {_val(a.get("ACTION_VALUE")).upper() for a in actions if _val(a.get("ACTION_VALUE"))}
        if values == {"TRUE"}:
            return "Treats the record as active."
        if values == {"FALSE"}:
            return "Treats the record as inactive."
        return "Decides whether a record should be treated as active."
    if category == "INITIAL_LOAD":
        where = f" into {target}" if target else ""
        return f"Loads the starting set of {scope}{where} on the very first run."
    if category == "INCREMENTAL_LOAD":
        where = f" into {target}" if target else ""
        return f"Loads only the records that changed since the last run{where}."
    if category == "JOIN_FILTER":
        # These rules carry the meaning in the join, not in an action. Handled
        # above, before INCLUDE_MATCHING; kept unreachable-safe for callers that
        # pass a bundle with no actions at all.
        return "Controls which rows survive the join."
    if category == "DEDUPLICATION":
        return "Keeps a single row where the source has more than one."
    if category == "DATA_QUALITY":
        subject = named[0] if named else ""
        return (f"Flags records where the {subject} fails the expected check."
                if subject else "Flags records that fail the expected data quality check.")
    if category == "AGGREGATION":
        subject = named[0] if named else "a value"
        return f"Summarises source rows into one {subject} per target record."
    if category == "STANDARDIZATION":
        subject = _join_words(named) if named else "the value"
        verb = "they are" if len(named) > 1 else "it is"
        return f"Cleans up the {subject} so {verb} stored in a consistent form."
    if category == "ATTRIBUTE_CARRY_FORWARD":
        subject = named[0] if named else "value"
        return f"Keeps the previous {subject} when the source sends nothing."
    if category == "TIME_TRAVEL":
        values = " ".join(_val(a.get("ACTION_VALUE")).upper() for a in actions)
        if "MEMORIES" in values or "HISTORY" in values:
            return "Moves rows that are no longer current into the history table."
        if "CURRENT" in values:
            return "Keeps the rows that are current as of today."
        return "Picks the version of the record that was valid at the point in time being asked about."

    if named:
        verb = next((_SUMMARY_VERB[k] for k in kinds if k in _SUMMARY_VERB), "sets")
        return f"{verb.capitalize()} the {_join_words(named)} for {scope}."

    # No actions and no conditions recorded: the description is the only thing
    # that carries meaning, so lead with its first sentence rather than a
    # placeholder. The Context bullet then drops it as already said.
    if not actions:
        first = next(iter(_sentences(rule.get("BUSINESS_DESCRIPTION"))), "")
        if first:
            return first if first.endswith(".") else f"{first}."

    if category in CATEGORY_INTENT:
        return CATEGORY_INTENT[category]
    return f"Applies to {scope}."


def render_rule_classic(bundle: Dict) -> str:
    """
    bundle = {
      "rule": {...}, "conditions": [...], "actions": [...],
      "joins": [ {"join": {...}, "columns": [...], "filters": [...]} ],
      "mappings": [...], "references": [...]
    }

    Renders a headline plus only the bullets that have something to add. A
    fact is stated once: anything the headline or the logic already says is
    dropped rather than repeated under another label.
    """
    rule = bundle.get("rule", {}) or {}
    number = _val(rule.get("RULE_NUMBER"))
    name = _val(rule.get("RULE_NAME")) or "Unnamed rule"
    category = _val(rule.get("RULE_CATEGORY")).upper()
    seen: set = set()

    out = [f"### Rule {number} — {name}" if number else f"### {name}"]

    # Metadata chips: values only, no labels, and no "X → X".
    facts = []
    if _val(rule.get("RULE_SET_NAME")):
        facts.append(_val(rule.get("RULE_SET_NAME")))
    source = _val(rule.get("SOURCE_BUSINESS_OBJECT")) or _val(rule.get("SOURCE_TECHNICAL_OBJECT"))
    target = _val(rule.get("TARGET_BUSINESS_OBJECT")) or _val(rule.get("TARGET_TECHNICAL_OBJECT"))
    if source and target and source != target:
        facts.append(f"{source} → {target}")
    elif source or target:
        facts.append(source or target)
    if category:
        facts.append(category.replace("_", " ").capitalize())
    if _val(rule.get("PRIORITY")):
        facts.append(f"Priority {_val(rule.get('PRIORITY'))}")
    if _val(rule.get("RULE_STATUS")):
        facts.append(_val(rule.get("RULE_STATUS")))
    if facts:
        out.append(" · ".join(facts))

    headline = rule_summary(bundle)
    out.append(f"\n**{headline}**")

    # Kept in the FDD's words (as_written.py): the sentence is the rule. Rendering its
    # placeholder action would describe a "result" the rule never states.
    if category in TEXT_CATEGORIES:
        sentence = _val(rule.get("BUSINESS_DESCRIPTION"))
        if sentence:
            out.append("")
            out.extend(f"> {line}" if line.strip() else ">" for line in sentence.splitlines())
        return "\n".join(out)

    # Everything the reader has now been told, for the dedupe below.
    established = _norm(headline) | _norm(name)

    bullets: List[str] = []

    conditions = condition_block(bundle.get("conditions", []), seen)
    cond_words: set = set()
    for line in conditions:
        cond_words |= _norm(line)
    if conditions:
        if len(conditions) == 1:
            bullets.append(f"- **Applies to:** records where {conditions[0]}")
        else:
            bullets.append("- **Applies to:** records matching either case:")
            bullets.extend(f"    - {line}" for line in conditions)
        established |= cond_words

    actions = action_block(
        _drop_restated_filters(bundle.get("actions", []) or [], cond_words), seen, established
    )
    if actions:
        if len(actions) == 1:
            bullets.append(f"- **Result:** {actions[0]}")
        else:
            bullets.append("- **Result:**")
            bullets.extend(f"    - {line}" for line in actions)
        for line in actions:
            established |= _norm(line)

    joins = bundle.get("joins", [])
    if joins:
        rendered = [
            join_sentence(item.get("join", {}), item.get("columns", []), item.get("filters", []))
            for item in sorted(joins, key=lambda j: int((j.get("join") or {}).get("JOIN_SEQUENCE") or 0))
        ]
        if len(rendered) == 1:
            bullets.append(f"- **Matched on:** {strip_markdown(rendered[0])}")
        else:
            bullets.append("- **Matched on:**")
            bullets.extend(f"    - {strip_markdown(line)}" for line in rendered)
        for line in rendered:
            established |= _norm(line)

    # Only the sentences of the description that the logic has not covered:
    # the rationale, the examples, the exclusions.
    context = [s for s in _sentences(rule.get("BUSINESS_DESCRIPTION")) if not _covered_by(s, established)]
    if context:
        bullets.append("- **Context:** " + " ".join(context))
        for sentence in context:
            established |= _norm(sentence)

    if bullets:
        out.append("")
        out.extend(bullets)

    # The mapping table usually restates the same source fields, target field
    # and intent that the bullets above already carry. Keep only rows that add
    # something, and drop the table entirely when none do.
    rows = []
    for m in bundle.get("mappings", []) or []:
        source_col = _val(m.get("SOURCE_COLUMN"))
        target_col = _val(m.get("TARGET_COLUMN"))
        note = _val(m.get("TRANSFORMATION_DESCRIPTION"))
        if _covered_by(" ".join([source_col, target_col, note]), established):
            continue
        rows.append((source_col, target_col, note))

    if rows:
        out.append("\n**Field mapping**\n")
        out.append("| Source field | Target field | Transformation |")
        out.append("| --- | --- | --- |")
        for source_col, target_col, note in rows:
            out.append(
                f"| {source_col or '—'} | {target_col or '—'} "
                f"| {note or 'Carried across unchanged'} |"
            )

    references = bundle.get("references", [])
    if references:
        out.append("\n**References**\n")
        for r in references:
            bits = [f"{k}: {_val(v)}" for k, v in r.items()
                    if k not in ("RULE_ID", "REFERENCE_ID") and _val(v)]
            if bits:
                out.append("- " + "; ".join(bits))

    return "\n".join(out)


def strip_markdown(text: str) -> str:
    """Removes ** and italic underscores but keeps the ones inside ITEM_DSC."""
    text = text.replace("**", "")
    return re.sub(r"(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])", "", text)


def render_compact_classic(bundle: Dict) -> str:
    """
    Same content, no markdown, no headings. This is what gets sent to the LLM
    as context - roughly 90% smaller than dumping the raw JSON rows, and now
    smaller again because the duplicated wording is gone.
    """
    rule = bundle.get("rule", {}) or {}
    lines = [
        f"RULE {_val(rule.get('RULE_NUMBER'))} ({_val(rule.get('RULE_ID'))}): "
        f"{_val(rule.get('RULE_NAME'))} [{_val(rule.get('RULE_CATEGORY'))}]"
    ]
    source = _val(rule.get("SOURCE_BUSINESS_OBJECT")) or _val(rule.get("SOURCE_TECHNICAL_OBJECT"))
    target = _val(rule.get("TARGET_BUSINESS_OBJECT")) or _val(rule.get("TARGET_TECHNICAL_OBJECT"))
    if source and target and source != target:
        lines.append(f"flow: {source} -> {target}")
    elif source or target:
        lines.append(f"object: {source or target}")

    seen: set = set()
    headline = rule_summary(bundle)
    lines.append("summary: " + headline)
    if _val(rule.get("RULE_CATEGORY")).upper() in TEXT_CATEGORIES:
        lines.append("as written: " + " ".join(_val(rule.get("BUSINESS_DESCRIPTION")).split()))
        return "\n".join(lines)
    established = _norm(headline) | _norm(rule.get("RULE_NAME"))

    conditions = condition_block(bundle.get("conditions", []), seen)
    cond_words: set = set()
    for line in conditions:
        lines.append("when: " + strip_markdown(line))
        cond_words |= _norm(line)
    established |= cond_words

    for line in action_block(
        _drop_restated_filters(bundle.get("actions", []) or [], cond_words), seen, established
    ):
        clean = strip_markdown(line.split("  \n")[0])
        lines.append("then: " + clean)
        established |= _norm(clean)

    for item in bundle.get("joins", []):
        joined = strip_markdown(join_sentence(
            item.get("join", {}), item.get("columns", []), item.get("filters", [])
        ))
        lines.append("join: " + joined)
        established |= _norm(joined)

    context = [s for s in _sentences(rule.get("BUSINESS_DESCRIPTION")) if not _covered_by(s, established)]
    if context:
        lines.append("context: " + " ".join(context))
    return "\n".join(lines)


# ==================================================================
# Short format - the one every page, preview, published FDD and download uses.
#
# The classic text (render_rule_classic) named each column twice ("Shipping
# Condition Code (SHIPPING_CONDITION_CD)"), printed DB.SCHEMA.OBJECT everywhere,
# repeated the action as stored text and again in brackets, and showed the
# metadata selection as an "either case 1 or case 2" alternative to the data filter
# it actually feeds. This states each fact once, with the exact column names.
# ==================================================================
_FQN = re.compile(r"\b[A-Za-z][A-Za-z0-9_$]*\.[A-Za-z][A-Za-z0-9_$]*\.([A-Za-z][A-Za-z0-9_$]*)\b")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*)*$")
_METADATA_COL = re.compile(r"^COLUMN_VALUE_[A-Z]+_TXT$", re.IGNORECASE)
METADATA_KEYS = {"SUBJECT_AREA_NM", "ENTITY_NM", "BUSINESS_RULE_NM"}
DEFAULT_PRIORITY = {"", "99", "99.0"}
_SYMBOL = {"==": "=", "EQ": "=", "EQUALS": "=", "IS": "=", "NE": "!=", "<>": "!=",
           "ISNULL": "IS NULL", "NULL": "IS NULL", "NOT NULL": "IS NOT NULL",
           "IS BLANK": "IS NULL", "IS NOT BLANK": "IS NOT NULL", "HAS VALUE": "IS NOT NULL"}
_RECORD_TARGETS = {"", "RECORD", "ROW", "INCLUDE_RECORD", "EXCLUDE_RECORD", "UNKNOWN",
                   "DEDUPLICATE", "FILTER"}


def short_name(text) -> str:
    """DB.SCHEMA.OBJECT -> OBJECT inside any text (DB.SCHEMA.OBJECT.COLUMN -> OBJECT.COLUMN)."""
    return _FQN.sub(lambda m: m.group(1), str(text or ""))


def _field(name) -> str:
    return short_name(_val(name))


_COLUMN_SUFFIX = re.compile(
    r"_(CD|ID|NM|NAME|DSC|DESC|TXT|QTY|IND|FLG|DT|DTM|TS|NBR|NUM|AMT|PCT|STS|KEY|UOM|SEQ)$")


def _is_column(value: str) -> bool:
    """A value that names a column rather than holding text: alias.COLUMN, OBJECT.COLUMN,
    or an upper-case name with a column suffix or several underscores. 'Air', 'Parameter'
    and short codes like 'VF_S' are text."""
    v = value.strip()
    if not v or not _IDENT.match(v):
        return False
    if "." in v:
        return True
    return v.upper() == v and (bool(_COLUMN_SUFFIX.search(v)) or v.count("_") >= 2)


def _value(value, operator: str) -> str:
    v = short_name(_val(value)).strip()
    if not v:
        return ""
    if _METADATA_COL.match(v):
        return f"metadata.{v.upper()}"
    if operator in ("IN", "NOT IN"):
        return v if v.startswith("(") else f"({v})"
    if v.startswith("'") or v.startswith('"') or _is_column(v):
        return v
    if re.fullmatch(r"-?\d+(\.\d+)?", v):
        return v
    if v.upper() in ("TRUE", "FALSE"):
        return v.upper()
    return f"'{v}'"


def _operator(raw) -> str:
    key = _operator_key(raw) or "="
    return _SYMBOL.get(key, key)


def _condition(cond: Dict) -> str:
    field = _field(cond.get("FIELD_NAME"))
    op = _operator(cond.get("OPERATOR"))
    if op in NO_VALUE_OPERATORS:
        return f"{field} {op}"
    return f"{field} {op} {_value(cond.get('FIELD_VALUE'), op)}".strip()


def _split_metadata(conditions: List[Dict]):
    """(metadata row selection, the rest). Only when the rule names SubjectArea /
    Entity / BusinessRule; then those, and COLUMN_VALUE_* conditions compared with
    plain text, pick the metadata row rather than filter the data."""
    def leaf(c):
        return _val(c.get("FIELD_NAME")).upper().rsplit(".", 1)[-1]
    if not {leaf(c) for c in conditions} & METADATA_KEYS:
        return [], list(conditions)
    meta, rest = [], []
    for c in conditions:
        name, value = leaf(c), _val(c.get("FIELD_VALUE"))
        if name in METADATA_KEYS or (_METADATA_COL.match(name) and not _is_column(value)):
            meta.append(c)
        else:
            rest.append(c)
    return meta, rest


def _when(conditions: List[Dict]) -> List[str]:
    """One line, or one line per OR group."""
    groups: Dict[str, List[Dict]] = {}
    for c in conditions:
        groups.setdefault(_val(c.get("CONDITION_GROUP")) or "1", []).append(c)

    def joined(items: List[Dict]) -> str:
        out = ""
        for i, c in enumerate(items):
            if i:
                logic = (_val(items[i - 1].get("LOGICAL_OPERATOR")) or "AND").upper()
                out += " or " if logic == "OR" else " and "
            out += _condition(c)
        return out

    lines = [joined(items) for items in groups.values() if items]
    if len(lines) > 1:
        return [("either " if i == 0 else "or ") + line for i, line in enumerate(lines)]
    return lines


def _sentence_like(text: str) -> bool:
    words = text.split()
    return len(words) >= 9 and sum(1 for w in words if w.islower()) >= 4


def _action(action: Dict) -> str:
    kind = (_val(action.get("ACTION_TYPE")) or "").upper()
    target = _field(action.get("TARGET_FIELD"))
    value = short_name(_val(action.get("ACTION_VALUE"))).strip()
    note = short_name(_val(action.get("ACTION_DESCRIPTION"))).strip()
    if _METADATA_COL.match(value):
        value = f"metadata.{value.upper()}"
    # A stored value that is a stray lower-case word ("the") left by extraction: the
    # description carries the meaning.
    if note and re.fullmatch(r"[a-z]+", value or "") and len(note.split()) > 2:
        value = ""
    whole_record = target.upper() in _RECORD_TARGETS
    if kind in ("INCLUDE_RECORD", "INCLUDE", "INCLUDE_MATCHING") and whole_record:
        return "keep the row"
    if kind in ("EXCLUDE_RECORD", "FILTER_OUT") and whole_record:
        return "drop the row"
    if kind in ("NO_TRANSFORMATION", "PASS_THROUGH", "DIRECT_MAP") and whole_record:
        return "carry every column across unchanged"
    # The stored value, unless it is prose - then the description, never both.
    shown = note if (not value or _sentence_like(value)) and note else value
    if target.upper() == "UNKNOWN":
        return f"{shown} → (target column not recorded)" if shown else ""
    if whole_record or not target:
        label = kind.replace("_", " ").lower()
        return f"{label}: {shown}" if label and shown else (shown or label)
    if kind == "SET_NULL":
        return f"{target} ← NULL"
    if kind == "DEFAULT":
        return f"{target} defaults to {shown}" if shown else f"{target} gets a default"
    if kind == "CARRY_FORWARD":
        return f"{target}: carry forward" + (f" - {shown}" if shown else "")
    if kind == "FLAG":
        return f"flag {target}" + (f": {shown}" if shown else "")
    if not shown:
        return f"{target} ({kind.replace('_', ' ').lower()})" if kind else target
    if _sentence_like(shown):
        return f"{target}: {shown}"
    return f"{target} ← {shown}"


def _join(item: Dict) -> str:
    join = item.get("join", {}) or {}
    left, right = _field(join.get("LEFT_OBJECT")), _field(join.get("RIGHT_OBJECT"))
    kind = (_val(join.get("JOIN_TYPE")) or "").lower()
    keys = ", ".join(f"{_field(c.get('LEFT_COLUMN'))} = {_field(c.get('RIGHT_COLUMN'))}"
                     for c in item.get("columns", []) or [])
    filters = " and ".join(
        f"{_field(f.get('FILTER_COLUMN'))} {_operator(f.get('FILTER_OPERATOR'))} "
        f"{_value(f.get('FILTER_VALUE'), _operator(f.get('FILTER_OPERATOR')))}".strip()
        for f in item.get("filters", []) or [])
    detail = "; ".join(x for x in (kind, keys or "keys not recorded",
                                   f"where {filters}" if filters else "") if x)
    return f"{left or '?'} ⟶ {right or '?'} ({detail})"


_LEAD_WORDS = 28
_CLAUSE_BREAK = re.compile(r":\s|;\s|,\s+where\s|\s+where\s|,\s+(?:and\s+)?(?=\w)")


def _lead_and_rest(rule: Dict, bundle: Dict, detailed: bool = True):
    """(lead, the part of the first sentence the lead left out)."""
    first = next(iter(_sentences(rule.get("BUSINESS_DESCRIPTION"))), "")
    full = short_name(first).strip()
    lead = _lead(rule, bundle, detailed)
    rest = full[len(lead.rstrip(".")):].strip(" ,;:.") if full and full.startswith(lead.rstrip(".")) else ""
    return lead, rest


def _lead(rule: Dict, bundle: Dict, detailed: bool = True) -> str:
    """What the rule is for, in its own words; the generic summary only fills in.

    When the bullets below spell out the logic (detailed), a long description is cut at
    its first clause - "Filter rows from the Delivery Route data using metadata-driven
    values" - because the rest is exactly what When / Then go on to say."""
    first = next(iter(_sentences(rule.get("BUSINESS_DESCRIPTION"))), "")
    if not first:
        return rule_summary(bundle)
    first = short_name(first).strip()
    if detailed and len(first.split()) > _LEAD_WORDS:
        for match in _CLAUSE_BREAK.finditer(first):
            head = first[:match.start()].strip()
            if len(head.split()) >= 8:
                first = head
                break
    first = first.rstrip(" ,;:")
    return first if first.endswith((".", "!", "?")) else first + "."


def _chips(rule: Dict, context: str) -> str:
    bits = []
    category = _val(rule.get("RULE_CATEGORY"))
    if category:
        bits.append(category.replace("_", " ").capitalize())
    status = _val(rule.get("RULE_STATUS"))
    if status:
        bits.append(status.replace("_", " ").capitalize())
    priority = _val(rule.get("PRIORITY"))
    if priority not in DEFAULT_PRIORITY:
        bits.append(f"Priority {priority}")
    if context != "grouped":
        source = _field(rule.get("SOURCE_TECHNICAL_OBJECT")) or _val(rule.get("SOURCE_BUSINESS_OBJECT"))
        target = _field(rule.get("TARGET_TECHNICAL_OBJECT")) or _val(rule.get("TARGET_BUSINESS_OBJECT"))
        if source and target and source != target:
            bits.append(f"{source} ⟶ {target}")
        elif source or target:
            bits.append(source or target)
    return " · ".join(bits)


def _parts(bundle: Dict):
    """(metadata line, when lines, then lines, join lines) for both short renderers."""
    meta, rest = _split_metadata(bundle.get("conditions", []) or [])
    metadata = ", ".join(_condition(c) for c in meta)
    when = _when(rest)
    then = list(dict.fromkeys(a for a in (_action(x) for x in bundle.get("actions", []) or []) if a))
    joins = [_join(item) for item in sorted(
        bundle.get("joins", []) or [],
        key=lambda j: int((j.get("join") or {}).get("JOIN_SEQUENCE") or 0))]
    return metadata, when, then, joins


def _extra_context(rule: Dict, said: set, rest: str = "") -> List[str]:
    """Description sentences nothing above says yet - including the part of the first
    sentence the lead was cut from - at most two."""
    candidates = ([rest[0].upper() + rest[1:] + "."] if rest else []) + \
        _sentences(rule.get("BUSINESS_DESCRIPTION"))[1:]
    return [short_name(s) for s in candidates if not _covered_by(s, said)][:2]


def _not_restating(then: List[str], lead: str) -> List[str]:
    """A Then line that is only the lead again ("TARGET: <the same sentence>") keeps
    just its target column; the sentence is already the line above."""
    lead_words = _norm(lead)
    out = []
    for line in then:
        target, sep, text = line.partition(": ")
        if sep and len(text.split()) > 8 and _covered_by(text, lead_words):
            out.append(f"{target} ← as described above")
        else:
            out.append(line)
    return out


def _bullet(label: str, lines: List[str]) -> List[str]:
    if not lines:
        return []
    if len(lines) == 1:
        return [f"- **{label}:** {lines[0]}"]
    return [f"- **{label}:**"] + [f"    - {line}" for line in lines]


def render_rule(bundle: Dict, context: str = "single") -> str:
    """
    bundle = {"rule": {...}, "conditions": [...], "actions": [...],
              "joins": [{"join": {...}, "columns": [...], "filters": [...]}],
              "mappings": [...], "references": [...]}

    context="grouped" where the page already shows the rule set and its source ->
    target above the rules (Requirements, Publish FDD), so the rule does not repeat it.
    """
    rule = bundle.get("rule", {}) or {}
    number = _val(rule.get("RULE_NUMBER"))
    name = short_name(_val(rule.get("RULE_NAME"))) or "Unnamed rule"
    out = [f"### Rule {number} — {name}" if number else f"### {name}"]
    chips = _chips(rule, context)
    if chips:
        out.append(chips)

    if _val(rule.get("RULE_CATEGORY")).upper() in TEXT_CATEGORIES:
        out.append(f"\n**{rule_summary(bundle)}**")
        sentence = _val(rule.get("BUSINESS_DESCRIPTION"))
        if sentence:
            out.append("")
            out.extend(f"> {line}" if line.strip() else ">" for line in sentence.splitlines())
        return "\n".join(out)

    metadata, when, then, joins = _parts(bundle)
    lead, rest = _lead_and_rest(rule, bundle, detailed=bool(metadata or when or then or joins))
    then = _not_restating(then, lead)
    out.append(f"\n**{lead}**")
    bullets = (_bullet("Metadata", [metadata] if metadata else []) + _bullet("When", when)
               + _bullet("Then", then) + _bullet("Joins", joins))
    said = _norm(lead) | _norm(name)
    for line in [metadata, *when, *then, *joins]:
        said |= _norm(line)
    extra = _extra_context(rule, said, rest)
    if extra:
        bullets.append("- **Context:** " + " ".join(extra))
    if bullets:
        out.append("")
        out.extend(bullets)

    rows = []
    for m in bundle.get("mappings", []) or []:
        src, tgt = _field(m.get("SOURCE_COLUMN")), _field(m.get("TARGET_COLUMN"))
        note = short_name(_val(m.get("TRANSFORMATION_DESCRIPTION")))
        if not _covered_by(" ".join([src, tgt, note]), said):
            rows.append((src, tgt, note))
    if rows:
        out.append("\n**Field mapping**\n")
        out.append("| Source | Target | Transformation |")
        out.append("| --- | --- | --- |")
        out.extend(f"| {s or '—'} | {t or '—'} | {n or 'unchanged'} |" for s, t, n in rows)

    references = bundle.get("references", []) or []
    if references:
        out.append("\n**References**\n")
        for r in references:
            bits = [f"{k}: {short_name(_val(v))}" for k, v in r.items()
                    if k not in ("RULE_ID", "REFERENCE_ID") and _val(v)]
            if bits:
                out.append("- " + "; ".join(bits))
    return "\n".join(out)


def render_compact(bundle: Dict) -> str:
    """The short format without markdown, one fact per line - what the model reads."""
    rule = bundle.get("rule", {}) or {}
    lines = [f"RULE {_val(rule.get('RULE_NUMBER'))} ({_val(rule.get('RULE_ID'))}): "
             f"{short_name(_val(rule.get('RULE_NAME')))} [{_val(rule.get('RULE_CATEGORY'))}]"]
    source = _field(rule.get("SOURCE_TECHNICAL_OBJECT")) or _val(rule.get("SOURCE_BUSINESS_OBJECT"))
    target = _field(rule.get("TARGET_TECHNICAL_OBJECT")) or _val(rule.get("TARGET_BUSINESS_OBJECT"))
    if source and target and source != target:
        lines.append(f"flow: {source} -> {target}")
    elif source or target:
        lines.append(f"object: {source or target}")
    if _val(rule.get("RULE_CATEGORY")).upper() in TEXT_CATEGORIES:
        lines.append("summary: " + rule_summary(bundle))
        lines.append("as written: " + " ".join(_val(rule.get("BUSINESS_DESCRIPTION")).split()))
        return "\n".join(lines)
    metadata, when, then, joins = _parts(bundle)
    lead, rest = _lead_and_rest(rule, bundle, detailed=bool(metadata or when or then or joins))
    then = _not_restating(then, lead)
    lines.append("summary: " + lead)
    if metadata:
        lines.append("metadata: " + metadata)
    lines += ["when: " + w for w in when] + ["then: " + t for t in then] + ["join: " + j for j in joins]
    said = _norm(lead)
    for line in [metadata, *when, *then, *joins]:
        said |= _norm(line)
    extra = _extra_context(rule, said, rest)
    if extra:
        lines.append("context: " + " ".join(extra))
    return "\n".join(lines)


def render_many(bundles: List[Dict], compact: bool = False, context: str = "single") -> str:
    if compact:
        return "\n\n".join(render_compact(b) for b in bundles)
    return "\n\n---\n\n".join(render_rule(b, context) for b in bundles)
