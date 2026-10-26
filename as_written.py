# Rules kept in the FDD's own words when the app cannot model their logic.
# Co-authored with CoCo
"""
"As written" rules.

Some FDD rules are human language with real logic inside that the parser and the
model cannot turn into conditions and actions ("for synergy sites take the plant's
priority unless the route is manual ..."). They used to be lost: a rule with errors
is never written. A developer can still build from the sentence, so the repository
keeps it word for word:

    RULE_CATEGORY  AS_WRITTEN
    one action     AS_WRITTEN on RECORD, the sentence in ACTION_DESCRIPTION
    no conditions, no joins

Everything that runs rules (sample data, test generation, data checks) skips them;
Verify against Snowflake reads the deployed code against the sentence and reports
an advisory verdict, never a failure. Pieces the compiler did recognise are kept as
a hint note - not executed, but useful to the developer and the verifier.
"""

import re
from typing import Dict, Iterable, List, Optional

CATEGORY = "AS_WRITTEN"
ACTION = "AS_WRITTEN"
NOTE_TEXT = ("Saved as written - the app could not model this logic; developers build "
             "from the text.")
HINT_PREFIX = "Mentions: "

# Verdicts of compliance.ai_logic_review, read softly for an as-written rule.
VERDICT_LABELS = {
    "MATCH": "Looks implemented",
    "PARTIAL": "Partly implemented",
    "MISMATCH": "Not found in the code",
    "UNCLEAR": "Can't tell",
    "NOT_CHECKED": "Can't tell",
}

_COLUMN = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_QUOTED = re.compile(r"'([^']{1,60})'")
_OBJECT = re.compile(r"\b[A-Z][A-Z0-9_$]*\.[A-Z][A-Z0-9_$]*\.[A-Z][A-Z0-9_$]*\b")


def is_as_written(rule) -> bool:
    """For a proposal (lowercase keys), a stored rule row, or a bundle."""
    if not rule:
        return False
    if isinstance(rule, dict) and "rule" in rule and isinstance(rule["rule"], dict):
        rule = rule["rule"]
    category = rule.get("rule_category") if "rule_category" in rule else rule.get("RULE_CATEGORY")
    return str(category or "").upper() == CATEGORY


def hints_from(text: str, proposal: Optional[Dict] = None) -> Dict[str, List[str]]:
    """Columns, quoted values and qualified objects the sentence or a failed proposal names."""
    upper = str(text or "").upper()
    objects = list(dict.fromkeys(_OBJECT.findall(upper)))
    leaves = {o.rsplit(".", 1)[-1] for o in objects}
    columns = [c for c in dict.fromkeys(_COLUMN.findall(upper))
               if c not in leaves and not any(c in o for o in objects)]
    values = list(dict.fromkeys(_QUOTED.findall(str(text or ""))))
    for section, key in (("conditions", "field_name"), ("actions", "target_field")):
        for item in (proposal or {}).get(section) or []:
            name = str(item.get(key) or "").strip().upper()
            if name and name not in ("RECORD", "UNKNOWN", "*") and name not in columns:
                columns.append(name)
    return {"columns": columns[:30], "values": values[:30], "objects": objects[:15]}


def hint_note(hints: Dict[str, List[str]]) -> Optional[str]:
    """'Mentions: PLANT_CD, ITEM_LOCATION_TYPE_CD; values: 'VF', 'C'' or None."""
    parts = []
    if hints.get("columns"):
        parts.append(", ".join(hints["columns"]))
    if hints.get("values"):
        parts.append("values: " + ", ".join(f"'{v}'" for v in hints["values"]))
    if hints.get("objects"):
        parts.append("objects: " + ", ".join(hints["objects"]))
    return HINT_PREFIX + "; ".join(parts) if parts else None


def hints_from_notes(notes: Iterable[str]) -> List[str]:
    """Column names back out of a stored hint note, for the verifier."""
    for note in notes or ():
        note = str(note or "")
        if note.startswith(HINT_PREFIX):
            head = note[len(HINT_PREFIX):].split(";")[0]
            if not head.strip().startswith(("values:", "objects:")):
                return [c.strip() for c in head.split(",") if c.strip()]
    return []


def rule_name_for(text: str) -> str:
    words = " ".join(str(text or "").split())
    words = re.sub(r"^\d+[A-Z]?[.)]\s*", "", words)
    return (words[:77] + "...") if len(words) > 80 else (words or "Rule as written")


def build_proposal(text: str, rule_name: Optional[str] = None,
                   priority: Optional[int] = None) -> Dict:
    """The proposal saved for an as-written rule: the sentence, verbatim."""
    sentence = str(text or "").strip()
    try:
        rank = int(priority)
    except (TypeError, ValueError):
        rank = 99
    return {
        "rule_name": (rule_name.strip() if isinstance(rule_name, str) else "")
                     or rule_name_for(sentence),
        "rule_category": CATEGORY,
        "business_description": sentence[:4000],
        "priority": rank,
        "rule_status": "DRAFT",
        "conditions": [],
        "actions": [{
            "target_field": "RECORD",
            "action_type": ACTION,
            "action_value": "",
            "action_description": sentence[:4000],
        }],
        "joins": [],
    }


def notes_for(text: str, failed_proposal: Optional[Dict] = None, source: str = "MANUAL"):
    """[(note_type, text, source)] for add_rule_notes."""
    notes = [("INFO", NOTE_TEXT, source)]
    hint = hint_note(hints_from(text, failed_proposal))
    if hint:
        notes.append(("INFO", hint, source))
    return notes


def preview(text: str) -> str:
    return ("**As written** — kept in the FDD's words; the app could not model this "
            f"logic.\n\n> {str(text or '').strip()}")


def make_entry_as_written(entry: Dict) -> None:
    """Turn an import plan entry into an as-written rule, undoably."""
    text = entry.get("text") or entry.get("original_text") or ""
    entry.setdefault("before_as_written", {
        k: entry.get(k) for k in ("proposal", "errors", "status", "reasons", "preview",
                                  "include_choice")})
    failed = entry.get("proposal") if isinstance(entry.get("proposal"), dict) else None
    entry["as_written_notes"] = notes_for(text, failed, "FDD_IMPORT")
    entry["proposal"] = build_proposal(text, priority=(failed or {}).get("priority"))
    entry["errors"] = []
    entry["status"] = "green"
    entry["reasons"] = ["Saved as written - no technical logic."]
    entry["preview"] = preview(text)
    entry["include_choice"] = None
    # "chosen" by a person, as opposed to True for text the importer itself knows is
    # descriptive (setup context): only a person's choice settles the rule's problems.
    entry["as_written"] = "chosen"


def undo_entry_as_written(entry: Dict) -> None:
    before = entry.pop("before_as_written", None)
    if before is None:
        return
    entry.update(before)
    entry.pop("as_written", None)
    entry.pop("as_written_notes", None)


def verdict_label(verdict: Optional[str]) -> str:
    return VERDICT_LABELS.get(str(verdict or "").upper(), "Can't tell")
