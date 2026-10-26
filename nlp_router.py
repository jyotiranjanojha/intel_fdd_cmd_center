"""
Understanding what the BA asked for.

The old router was a chain of if/elif over ~20 regexes: the first pattern that
matched won, "add" plus "rule" anywhere in a sentence hijacked the request, and
anything unmatched fell to Cortex Analyst.

This version scores every intent, resolves entities against the real catalog,
and only calls a model when the top two intents are close - which is rare, so
most questions are routed with zero LLM latency.
"""

import re
from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional

from catalog import resolve_field, resolve_object, resolve_rules
from db import FAST_MODEL, llm_cached

# ------------------------------------------------------------------
# Intents
# ------------------------------------------------------------------
EXPLAIN_RULE = "EXPLAIN_RULE"
FIELD_LINEAGE = "FIELD_LINEAGE"
LIST_RULES = "LIST_RULES"
NEW_REQUIREMENT = "NEW_REQUIREMENT"
UPDATE_RULE = "UPDATE_RULE"
GENERATE_TESTS = "GENERATE_TESTS"
GENERATE_DQ = "GENERATE_DQ"
DATA_SAMPLE = "DATA_SAMPLE"
DATA_COUNT = "DATA_COUNT"
DATA_NULL = "DATA_NULL"
DATA_DUPLICATE = "DATA_DUPLICATE"
DATA_DISTINCT = "DATA_DISTINCT"
UNKNOWN = "UNKNOWN"

LLM_LABELS = [EXPLAIN_RULE, FIELD_LINEAGE, LIST_RULES, NEW_REQUIREMENT,
              UPDATE_RULE, GENERATE_TESTS, GENERATE_DQ, DATA_SAMPLE,
              DATA_COUNT, DATA_NULL, DATA_DUPLICATE, DATA_DISTINCT]

# (regex, weight). Weights are deliberately coarse; ties go to the LLM.
SIGNALS: Dict[str, List] = {
    EXPLAIN_RULE: [
        (r"\bexplain\b", 3), (r"\bwhat does\b", 3), (r"\bhow does\b", 2),
        (r"\bwhat is (the )?rule\b", 3), (r"\bdescribe\b", 2), (r"\bshow rule\b", 3),
        (r"\bbusiness (rule|logic)\b", 2), (r"\brule\s+[A-Z0-9][A-Z0-9._-]*\b", 3),
        (r"\bin plain english\b", 3), (r"\bfdd\b", 2), (r"\bdocument(ation)?\b", 1),
    ],
    FIELD_LINEAGE: [
        (r"\bderiv(e|ed|ation)\b", 4), (r"\bpopulat(e|ed)\b", 3), (r"\bcalculat(e|ed)\b", 3),
        (r"\bwhere does .* come from\b", 4), (r"\bmapp(ed|ing)\b", 3),
        (r"\blineage\b", 4), (r"\bsource (of|for)\b", 2), (r"\bhow is\b.*\b(set|built|filled)\b", 3),
    ],
    LIST_RULES: [
        (r"\blist\b", 3), (r"\ball rules\b", 4), (r"\bwhich rules\b", 4),
        (r"\bhow many rules\b", 3), (r"\brules (for|on|about)\b", 3), (r"\bsummary\b", 2),
    ],
    NEW_REQUIREMENT: [
        (r"\badd\b.*\brule\b", 3), (r"\bcreate\b.*\brule\b", 3), (r"\bnew requirement\b", 4),
        (r"\bif\b.*\bthen\b", 4), (r"\bwhen\b.*\b(then|set|populate|default)\b", 4),
        (r"\bshould be (set|populated|defaulted)\b", 4), (r"\bmust be\b", 3),
        (r"\bdefault(s)? to\b", 3), (r"\brequirement\b", 2), (r"\bwe need to\b", 2),
        (r"\bexclude\b.*\bwhere\b", 3), (r"\bonly load\b", 3),
    ],
    UPDATE_RULE: [
        (r"\bupdate\b.*\brule\b", 4), (r"\bchange\b.*\brule\b", 4), (r"\bamend\b", 3),
        (r"\bmodify\b", 3), (r"\bretire\b", 3), (r"\bdeactivate\b", 3), (r"\bcorrect\b.*\brule\b", 3),
    ],
    GENERATE_TESTS: [
        (r"\btest case", 5), (r"\btest scenario", 5), (r"\bunit test", 4),
        (r"\bhow do (i|we) test\b", 4), (r"\bacceptance criteria\b", 4),
    ],
    GENERATE_DQ: [
        (r"\bdq\b", 5), (r"\bdata quality\b", 5), (r"\bvalidation rule", 4),
        (r"\bquality check", 4),
    ],
    DATA_SAMPLE: [
        (r"\bsample\b", 4), (r"\bshow (me )?(the )?(data|rows|records)\b", 4),
        (r"\bpreview\b", 3), (r"\bfirst \d+ rows\b", 4), (r"\btop \d+\b", 2),
    ],
    DATA_COUNT: [
        (r"\bcount\b", 4), (r"\bhow many (rows|records)\b", 5), (r"\bvolume\b", 2),
        (r"\brow count\b", 5),
    ],
    DATA_NULL: [(r"\bnull\b", 4), (r"\bblank\b", 3), (r"\bmissing values?\b", 4), (r"\bempty\b", 3)],
    DATA_DUPLICATE: [(r"\bduplicate", 5), (r"\bdupe", 4), (r"\bunique(ness)?\b", 3)],
    DATA_DISTINCT: [(r"\bdistinct\b", 4), (r"\bunique values\b", 4), (r"\bvalue distribution\b", 4),
                    (r"\bwhat values\b", 3)],
}

DATA_INTENTS = {DATA_SAMPLE, DATA_COUNT, DATA_NULL, DATA_DUPLICATE, DATA_DISTINCT}
METADATA_INTENTS = {EXPLAIN_RULE, FIELD_LINEAGE, LIST_RULES, GENERATE_TESTS, GENERATE_DQ}
WRITE_INTENTS = {NEW_REQUIREMENT, UPDATE_RULE}


@dataclass
class Intent:
    intent: str = UNKNOWN
    confidence: float = 0.0
    objects: List[str] = dc_field(default_factory=list)
    fields: List[str] = dc_field(default_factory=list)
    rule_ids: List[int] = dc_field(default_factory=list)
    values: List[str] = dc_field(default_factory=list)
    row_limit: int = 100
    resolved_by: str = "rules"
    scores: Dict[str, float] = dc_field(default_factory=dict)

    @property
    def is_data(self) -> bool:
        return self.intent in DATA_INTENTS

    @property
    def is_write(self) -> bool:
        return self.intent in WRITE_INTENTS


# ------------------------------------------------------------------
# Entity extraction
# ------------------------------------------------------------------
OBJECT_PATTERN = r"\b(?:[A-Z0-9_]+\.){0,2}(?:V|T)_[A-Z0-9_]+\b"
QUOTED_VALUE = r"'([^']{1,60})'|\"([^\"]{1,60})\""


def extract_objects(text: str, databases: Optional[List[str]] = None) -> List[str]:
    found = []
    for raw in re.findall(OBJECT_PATTERN, text.upper()):
        full, _ = resolve_object(raw, databases)
        if full and full not in found:
            found.append(full)
    return found


def extract_values(text: str) -> List[str]:
    values = []
    for a, b in re.findall(QUOTED_VALUE, text):
        values.append(a or b)
    return values


# ------------------------------------------------------------------
# Routing
# ------------------------------------------------------------------
def score_intents(text: str) -> Dict[str, float]:
    lowered = " " + text.lower().strip() + " "
    scores: Dict[str, float] = {}
    for intent, signals in SIGNALS.items():
        total = 0.0
        for pattern, weight in signals:
            if re.search(pattern, lowered, flags=re.IGNORECASE):
                total += weight
        if total:
            scores[intent] = total
    return scores


def _tiebreak_with_llm(text: str, candidates: List[str]) -> Optional[str]:
    """
    Small model, tiny output, cached. Used only when scoring is ambiguous.
    """
    options = ", ".join(candidates)
    prompt = (
        "Classify the user request into exactly one label. "
        f"Allowed labels: {options}. "
        "Answer with the label only, no explanation.\n\n"
        f"Request: {text[:600]}"
    )
    try:
        answer = llm_cached(prompt, FAST_MODEL).strip().upper()
    except Exception:
        return None
    for label in candidates:
        if label in answer:
            return label
    return None


def route(text: str, allow_llm: bool = True,
          databases: Optional[List[str]] = None) -> Intent:
    """
    `databases` scopes object resolution to the physical databases the current
    team uses. Passed in rather than read from a module-level global: Streamlit
    keeps module state across sessions in one process, so a global would let one
    user's team leak into another's resolution.
    """
    text = (text or "").strip()
    if not text:
        return Intent()

    scores = score_intents(text)
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])

    objects = extract_objects(text, databases)
    fields = resolve_field(text)
    values = extract_values(text)

    intent = Intent(objects=objects, fields=fields, values=values, scores=scores)

    # A sentence that reads like a requirement, with a condition and an
    # instruction, is a write no matter what other words it contains.
    if re.search(r"\bif\b.{3,}\bthen\b", text, flags=re.IGNORECASE) or \
       re.search(r"\bwhen\b.{3,}\b(set|populate|default|exclude|derive)\b", text, flags=re.IGNORECASE):
        scores[NEW_REQUIREMENT] = scores.get(NEW_REQUIREMENT, 0) + 4
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])

    if not ranked:
        # No signal at all. If an object was named it is probably a data
        # question, otherwise treat it as a documentation question.
        intent.intent = DATA_SAMPLE if objects else EXPLAIN_RULE
        intent.confidence = 0.3
        intent.resolved_by = "default"
    else:
        top_intent, top_score = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
        margin = top_score - runner_up
        intent.intent = top_intent
        intent.confidence = min(1.0, 0.4 + 0.1 * top_score + 0.1 * margin)

        if allow_llm and margin <= 1.0 and len(ranked) > 1:
            candidates = [i for i, _ in ranked[:3]]
            chosen = _tiebreak_with_llm(text, candidates)
            if chosen:
                intent.intent = chosen
                intent.resolved_by = "llm-tiebreak"
                intent.confidence = 0.75

    # A data question needs an object; without one it is a documentation question.
    if intent.is_data and not objects:
        intent.intent = FIELD_LINEAGE if fields else EXPLAIN_RULE
        intent.resolved_by += "+no-object"

    if intent.intent in METADATA_INTENTS:
        intent.rule_ids = resolve_rules(text)

    if objects:
        _, limit = resolve_object(objects[0], databases)
        intent.row_limit = min(int(limit or 100), 500)

    return intent


# ------------------------------------------------------------------
# Safe SQL for data questions
# ------------------------------------------------------------------
def build_data_sql(text: str, intent: Intent) -> (Optional[str], Optional[str]):
    from catalog import get_object_columns  # local import keeps startup fast

    if not intent.objects:
        return None, "Name an object to query, for example V_BRD_ITEM_STAGE."

    obj = intent.objects[0]
    limit = intent.row_limit
    columns = set(get_object_columns(obj))
    field = next((f for f in intent.fields if not columns or f in columns), None)

    if intent.intent == DATA_SAMPLE:
        return f"SELECT *\nFROM {obj}\nLIMIT {limit}", None

    if intent.intent == DATA_COUNT:
        return f"SELECT COUNT(*) AS RECORD_COUNT\nFROM {obj}", None

    if intent.intent == DATA_NULL:
        if not field:
            return None, f"Which column should be checked for blanks in {obj}?"
        return (f"SELECT COUNT(*) AS TOTAL_RECORDS,\n"
                f"       COUNT_IF({field} IS NULL) AS BLANK_RECORDS,\n"
                f"       ROUND(100 * COUNT_IF({field} IS NULL) / NULLIF(COUNT(*), 0), 2) AS BLANK_PCT\n"
                f"FROM {obj}"), None

    if intent.intent == DATA_DUPLICATE:
        if not field:
            return None, f"Which column should be checked for duplicates in {obj}?"
        return (f"SELECT {field}, COUNT(*) AS TIMES_REPEATED\n"
                f"FROM {obj}\nGROUP BY {field}\nHAVING COUNT(*) > 1\n"
                f"ORDER BY TIMES_REPEATED DESC\nLIMIT {limit}"), None

    if intent.intent == DATA_DISTINCT:
        if not field:
            return None, f"Which column's values should be listed from {obj}?"
        return (f"SELECT {field}, COUNT(*) AS RECORD_COUNT\n"
                f"FROM {obj}\nGROUP BY {field}\nORDER BY RECORD_COUNT DESC\nLIMIT {limit}"), None

    return None, None
