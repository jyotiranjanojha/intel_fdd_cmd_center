"""Parse a pasted FDD document into a reviewable import plan.

Why this exists
---------------
Every FDD in this repository was loaded by hand. That is slow, and it is where
accuracy is lost: a module transcribed from the wrong environment, columns
declared DERIVED when they are straight pulls, rules recorded as prose with no
structured actions. Pasting the document and letting the app do the first pass
makes the transcription reproducible and, more importantly, reviewable.

Design rules
------------
1. Nothing is written until a human approves the plan. Every function here is
   read-only except apply_import_plan().

2. Nothing is silently dropped. Text the parser does not recognise is collected
   into `leftovers` and shown in the review UI. A parser that quietly discards
   half a document is worse than no parser, because the gap is invisible - which
   is exactly how the existing accuracy problems got in.

3. This module is pure: only stdlib imports at module scope. The LLM-backed rule
   compiler and the database are injected by the caller, so the whole parser is
   unit-testable without Streamlit, Snowpark or a warehouse.

4. Anchors are generous. FDDs are "almost similar" in structure rather than
   identical, so headings are matched on several spellings and rule numbering
   tolerates 1, 2A, Rule 3:, IL and DD alike.
"""

import inspect
import re
from typing import Callable, Dict, List, Optional, Tuple

import as_written as aw

# Heading spellings seen across the existing FDDs, plus the obvious variants.
# Order matters only for reporting; matching is by longest spelling first so
# "REQUIRED OBJECTS" does not get claimed by the bare "OBJECTS" anchor.
SECTION_ANCHORS: Dict[str, Tuple[str, ...]] = {
    "objects": ("REQUIRED OBJECTS", "OBJECT INVENTORY", "TABLES AND VIEWS",
                "OBJECTS", "OBJECT"),
    "data_model": ("TARGET STRUCTURE", "DATA MODEL", "STRUCTURE", "COLUMNS"),
    "rules": ("TRANSFORMATION RULES", "BUSINESS RULES", "RULES", "RULE", "LOGIC"),
    "lineage": ("DATA FLOW", "LINEAGE", "PIPELINE"),
    "overview": ("OVERVIEW", "PURPOSE", "SCOPE", "BACKGROUND", "INTRODUCTION"),
}

# DB.SCHEMA.OBJECT, which is how every correctly-recorded object in the
# repository is written. Anything looser is reported rather than guessed at.
# Parts need only two characters: real database names are longer, but abbreviated
# documents do write DB.SCH.T_THING and silently ignoring those loses objects.
TECHNICAL_NAME = re.compile(r"\b([A-Z][A-Z0-9_]+\.[A-Z][A-Z0-9_]+\.[A-Z][A-Z0-9_]+)\b")

# 1.  2A)  Rule 3:  IL -  DD:
RULE_NUMBER = re.compile(
    # "1.", "1)", "1:", "1 -" and "1.)" - the Finished Goods Inventory FDD numbers "1.)".
    r"^\s*(?:RULE\s+)?(?P<number>[0-9]{1,3}[A-Z]?|IL|DD)\s*(?:\.\)|[.):\-\u2013])"
    # "5.In Stage view": no space, but a capitalised word straight after the dot.
    r"(?:\s+|(?=(?-i:[A-Z][a-z])))(?P<body>\S.*)$",
    flags=re.IGNORECASE,
)

OBJECT_TYPE_HINTS = (
    ("BASE_TABLE", ("BASE TABLE", "BASE_TABLE")),
    ("FUNCTION", ("FUNCTION", "TABLE FUNCTION", "UDTF")),
    ("VIEW", ("VIEW",)),
    ("TABLE", ("TABLE",)),
    ("FILE", ("CSV", "FILE", "EXPORT", "EXTRACT")),
)


def _heading_key(line: str) -> Optional[str]:
    """Return the section key if this line reads as a heading, else None.

    A heading is short, mostly non-prose, and contains an anchor word. Requiring
    shortness stops a sentence that merely mentions "rules" from being treated
    as the start of the rules section.

    Numbered headings and numbered rules are genuinely ambiguous - "3. DATA MODEL"
    and "Rule 3: Set PRIMARY_IND to Y" both parse as a rule statement. They are
    told apart by what is left after the number: a heading's remainder *is* an
    anchor ("DATA MODEL"), whereas a rule's remainder is prose. Without this,
    "Rule 3: ..." matched the bare RULE anchor, was consumed as a heading, and the
    rule vanished from the import - the silent loss this parser exists to prevent.
    """
    stripped = line.strip().strip("#*=-_:").strip()
    if not stripped or len(stripped) > 60:
        return None
    upper = stripped.upper()
    # Strip a leading section number: "3. RULES" or "3.1 Business Rules".
    upper = re.sub(r"^[0-9]+(\.[0-9]+)*\s*[.)\-]?\s*", "", upper).strip()
    if not upper:
        return None

    looks_like_rule = RULE_NUMBER.match(line) is not None

    best: Optional[Tuple[int, str]] = None
    for key, spellings in SECTION_ANCHORS.items():
        for spelling in spellings:
            if looks_like_rule:
                # Only an exact anchor rescues a line that also parses as a rule.
                matched = upper == spelling
            else:
                matched = (upper == spelling
                           or upper.startswith(spelling + " ")
                           or upper.endswith(" " + spelling))
            if matched and (best is None or len(spelling) > best[0]):
                best = (len(spelling), key)
    return best[1] if best else None


def split_sections(text: str) -> Dict[str, object]:
    """Split a pasted document into named sections.

    Returns {"sections": {key: [lines]}, "leftovers": [lines]} where leftovers
    is everything appearing before the first recognised heading. Nothing is
    discarded - the two together always account for every non-empty input line.
    """
    sections: Dict[str, List[str]] = {}
    leftovers: List[str] = []
    current: Optional[str] = None

    for raw in (text or "").splitlines():
        key = _heading_key(raw)
        if key is not None:
            current = key
            sections.setdefault(current, [])
            continue
        if not raw.strip():
            if current:
                sections[current].append("")
            continue
        if current is None:
            leftovers.append(raw.rstrip())
        else:
            sections[current].append(raw.rstrip())

    return {"sections": {k: v for k, v in sections.items()},
            "leftovers": leftovers}


def parse_objects(lines: List[str]) -> Dict[str, object]:
    """Pull object declarations out of the objects section.

    Recognises a fully qualified DB.SCHEMA.NAME anywhere on the line and treats
    the remaining text as the business name. Lines with no qualified name are
    returned as unparsed rather than half-guessed, because an object recorded
    under the wrong technical name is the single worst defect this app can hold -
    it renders convincingly and verifies against nothing.
    """
    objects: List[Dict[str, str]] = []
    unparsed: List[str] = []
    seen = set()

    for line in lines:
        if not line.strip():
            continue
        match = TECHNICAL_NAME.search(line.upper())
        if not match:
            if len(line.strip()) > 3:
                unparsed.append(line.strip())
            continue
        technical = match.group(1)

        # Business name = the line minus the technical name and list punctuation.
        residue = TECHNICAL_NAME.sub("", line.upper(), count=1)
        business = re.sub(r"[|\-\u2013:,()\[\]]+", " ", line[:match.start()] or residue)
        business = re.sub(r"^\s*[0-9]+[.)]\s*", "", business)
        business = re.sub(r"\s+", " ", business).strip(" -|:")

        upper_line = line.upper()
        object_type = "VIEW" if technical.rsplit(".", 1)[-1].startswith("V_") else "BASE_TABLE"
        for candidate, hints in OBJECT_TYPE_HINTS:
            if any(hint in upper_line for hint in hints):
                object_type = candidate
                break

        if technical in seen:
            continue
        seen.add(technical)
        objects.append({
            "object_name": business or technical.rsplit(".", 1)[-1].title().replace("_", " "),
            "technical_object_name": technical,
            "object_type": object_type,
            "source_line": line.strip(),
        })

    return {"objects": objects, "unparsed": unparsed}


def parse_data_model(lines: List[str]) -> Dict[str, object]:
    """Pull column specifications out of the data model section.

    Handles markdown-style pipe tables and simple whitespace/tab columns. The
    first cell is the column name, the second - when it looks like a type - the
    data type, and any remaining text becomes the description.
    """
    columns: List[Dict[str, str]] = []
    unparsed: List[str] = []
    current_object: Optional[str] = None
    ordinal = 0

    type_like = re.compile(
        r"^(VARCHAR|CHAR|NUMBER|NUMERIC|DECIMAL|INT|INTEGER|BIGINT|SMALLINT|FLOAT|DOUBLE|"
        r"BOOLEAN|DATE|TIMESTAMP(_NTZ|_LTZ|_TZ)?|TIME|TEXT|STRING|BINARY|VARIANT|OBJECT|ARRAY)"
        r"(\s*\(\s*[0-9]+\s*(,\s*[0-9]+\s*)?\))?$",
        flags=re.IGNORECASE,
    )

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # A qualified name on its own line switches which object we are filling.
        name_match = TECHNICAL_NAME.search(stripped.upper())
        if name_match and len(stripped) < 120 and "|" not in stripped:
            current_object = name_match.group(1)
            ordinal = 0
            continue

        if "|" in stripped:
            cells = [c.strip() for c in stripped.strip("|").split("|")]
        else:
            cells = [c for c in re.split(r"\t+|\s{2,}", stripped) if c.strip()]

        cells = [c for c in cells if c not in ("", "-")]
        if not cells:
            continue
        # Markdown separator row, or a header row.
        if all(set(c) <= set("-: ") for c in cells):
            continue
        if cells[0].upper() in ("COLUMN", "COLUMN NAME", "FIELD", "FIELD NAME", "NAME", "#"):
            continue

        column_name = cells[0].strip().strip("`")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_ ]{0,60}", column_name):
            if len(stripped) > 3:
                unparsed.append(stripped)
            continue

        data_type = ""
        rest = cells[1:]
        if rest and type_like.match(rest[0].strip()):
            data_type = rest[0].strip().upper()
            rest = rest[1:]

        ordinal += 1
        columns.append({
            "technical_object_name": current_object or "",
            "column_name": column_name.replace(" ", "_").upper(),
            "data_type": data_type,
            "business_description": " ".join(r.strip() for r in rest).strip(" |"),
            "ordinal": ordinal,
            "source_line": stripped,
        })

    return {"columns": columns, "unparsed": unparsed}


def split_rule_statements(lines: List[str]) -> Dict[str, object]:
    """Split the rules section into one statement per rule.

    A new statement starts at a recognised rule number; everything until the
    next one is treated as continuation, so multi-line rules survive intact.
    Text before the first rule number is returned as preamble rather than being
    glued onto rule 1, where it would quietly corrupt that rule's description.
    """
    statements: List[Dict[str, str]] = []
    preamble: List[str] = []

    for line in lines:
        if not line.strip():
            if statements:
                statements[-1]["text"] += "\n"
            continue
        match = RULE_NUMBER.match(line)
        if match:
            statements.append({
                "rule_number": match.group("number").upper(),
                "text": match.group("body").strip(),
            })
        elif statements:
            statements[-1]["text"] += " " + line.strip()
        else:
            preamble.append(line.strip())

    for statement in statements:
        statement["text"] = re.sub(r"[ \t]+", " ", statement["text"]).strip()

    return {"statements": statements, "preamble": preamble}


def parse_document(text: str, object_index: Optional[Dict[str, List[str]]] = None
                   ) -> Dict[str, object]:
    """Run the whole deterministic pass. No LLM, no database, no writes.

    Picks a dialect first. The tabular label/value form is what the real FDDs
    use; the heading form was built first and is kept because it is a reasonable
    shape for newly written documents.
    """
    text = _split_rows_after_cells(text)
    text, completed = complete_two_part_names(text)
    resolved: List[Dict] = []
    if object_index:
        text, resolved = resolve_names_in_snowflake(text, object_index, completed)
    parsed = parse_tabular_fdd(text) if looks_tabular(text) else parse_heading_fdd(text)
    parsed["completed_names"] = resolved or completed
    return parsed


# `..."<TAB>Hybrid View<TAB>SCHEMA.OBJECT`: a quoted Transformation cell and the next
# label row arrived on one line (the row break was lost in copying). The closing quote,
# a short label and a dotted name are enough to put the break back.
_ROW_AFTER_CELL = re.compile(
    r'"[ \t]*\t(?=[A-Za-z][A-Za-z _&/-]{0,40}\t[ ]*[A-Za-z][A-Za-z0-9_]*\.[A-Za-z])')


def _split_rows_after_cells(text: str) -> str:
    return _ROW_AFTER_CELL.sub('"\n', text or "")


def build_object_index(full_names) -> Dict[str, List[str]]:
    """{OBJECT: [DB.SCHEMA.OBJECT, ...]} from every table and view in Snowflake."""
    index: Dict[str, List[str]] = {}
    for name in full_names or ():
        name = str(name).upper()
        if name.count(".") == 2:
            index.setdefault(name.rsplit(".", 1)[-1], []).append(name)
    return index


# A bare object name: V_BRD_PLANT_CORE_PIT, T_INV_X_ROOT. Not preceded by a dot (the
# tail of a longer name) and not followed by one (OBJECT.COLUMN).
_BARE_OBJECT = re.compile(r"(?<![\w.])([TV]_[A-Z0-9]+_[A-Z0-9_]+)\b(?!\.[A-Z])")


def resolve_names_in_snowflake(text: str, index: Dict[str, List[str]],
                               completed: List[Dict] = ()) -> Tuple[str, List[Dict]]:
    """Put every object name in the FDD onto the object that exists in Snowflake.

    FDDs write names three ways - DB.SCHEMA.OBJECT, SCHEMA.OBJECT, and just OBJECT -
    and sometimes with a database or schema that is out of date. Guessing the
    database only works when the guess is right, so each name is looked up in the
    account's own object index:

      confirmed          exists exactly as written (or as completed)
      found in Snowflake the object exists under one other DB/schema - use that
      matched            several exist; exactly one is in the database the rest of
                         this FDD uses
      ambiguous          several exist and nothing decides - left as written, and
                         listed with the options
      not found          nothing by that name - left as written (not built yet?)

    Returns the rewritten text and one row per distinct name, for the review screen.
    """
    text = str(text or "")
    written_as = {c["completed"]: c["written"] for c in completed or []}
    known = {n for names in index.values() for n in names}
    full = list(dict.fromkeys(TECHNICAL_NAME.findall(text.upper())))
    home_counts: Dict[str, int] = {}
    for name in full:
        if name in known:
            home_counts[name.split(".")[0]] = home_counts.get(name.split(".")[0], 0) + 1
    home = max(home_counts, key=home_counts.get) if home_counts else ""

    rows: List[Dict] = []
    replace: Dict[str, str] = {}

    def choose(name: str, leaf: str, schema: str = "") -> Tuple[Optional[str], str, List[str]]:
        options = index.get(leaf, [])
        if name in options:
            return name, "confirmed", []
        same_schema = [o for o in options if schema and o.split(".")[1] == schema]
        pool = same_schema or options
        if len(pool) == 1:
            return pool[0], "found in Snowflake", []
        in_home = [o for o in pool if o.split(".")[0] == home]
        if len(in_home) == 1:
            return in_home[0], "matched", []
        return None, ("ambiguous" if pool else "not found"), pool

    for name in full:
        _, schema, leaf = name.split(".")
        chosen, how, options = choose(name, leaf, schema)
        if how == "confirmed" and written_as.get(name, name) != name:
            how = "found in Snowflake"   # completed, then confirmed to exist
        if chosen and chosen != name:
            replace[name] = chosen
        rows.append({"written": written_as.get(name, name), "completed": chosen or name,
                     "database_from": how, "options": options})

    for leaf in dict.fromkeys(_BARE_OBJECT.findall(text.upper())):
        if any(r["completed"].endswith("." + leaf) for r in rows):
            chosen = next(r["completed"] for r in rows if r["completed"].endswith("." + leaf))
            how, options = "same as elsewhere in this FDD", []
        else:
            chosen, how, options = choose("", leaf)
        if chosen:
            replace[leaf] = chosen
        rows.append({"written": leaf, "completed": chosen or leaf,
                     "database_from": how, "options": options})

    if replace:
        # Longest first, so a three-part name is rewritten before its own leaf.
        keys = sorted(replace, key=len, reverse=True)
        pattern = re.compile(r"(?<![\w.])(" + "|".join(re.escape(k) for k in keys)
                             + r")\b(?!\.[A-Z])", re.IGNORECASE)
        text = pattern.sub(lambda m: replace[m.group(1).upper()], text)
    return text, rows


# SCHEMA.OBJECT with no database: "PDH_COMMON_XFRM_BASE.T_INV_FINISHED_GOODS_INVENTORY_ROOT".
# The schema must look like one (contain an underscore, not start with T_/V_) so an
# OBJECT.COLUMN reference such as V_BRD_ITEM_CORE_PIT.MATERIAL_TYPE_CD is left alone,
# and nothing may precede it with a dot (it would already be DB.SCHEMA.OBJECT).
_TWO_PART = re.compile(
    r"(?<![\w.])((?![TVF]_)[A-Z][A-Z0-9]*_[A-Z0-9_]*\.[TVF]_[A-Z0-9_]+)\b(?!\.[A-Z])")
DEFAULT_DATABASE = "IF_SUPPLY_CHAIN"


def complete_two_part_names(text: str) -> Tuple[str, List[Dict]]:
    """Prefix SCHEMA.OBJECT names with a database, and say which were completed.

    The Finished Goods Inventory FDD writes every pipeline object this way. Only
    full DB.SCHEMA.OBJECT names were recognised, so six of its eight objects were
    dropped - the import found two objects and one broken hop. The database is the
    one the document itself uses for that schema elsewhere, else the one it uses
    most, else IF_SUPPLY_CHAIN.
    """
    text = str(text or "")
    full = TECHNICAL_NAME.findall(text.upper())
    by_schema: Dict[str, str] = {}
    counts: Dict[str, int] = {}
    for name in full:
        db, schema, _ = name.split(".")
        by_schema.setdefault(schema, db)
        counts[db] = counts.get(db, 0) + 1
    fallback = DEFAULT_DATABASE if DEFAULT_DATABASE in counts or not counts else \
        max(counts, key=counts.get)
    completed: Dict[str, Dict] = {}

    def repl(match):
        two = match.group(1)
        db = by_schema.get(two.split(".", 1)[0].upper(), fallback)
        completed.setdefault(two.upper(), {"written": two, "completed": f"{db}.{two}".upper(),
                                           "database_from": ("this FDD"
                                                             if two.split(".")[0].upper() in by_schema
                                                             else "default")})
        return f"{db}.{two}"

    return _TWO_PART.sub(repl, text), list(completed.values())


def parse_heading_fdd(text: str) -> Dict[str, object]:
    """Parse the sectioned form: 1. OVERVIEW / REQUIRED OBJECTS / DATA MODEL / RULES."""
    split = split_sections(text)
    sections: Dict[str, List[str]] = split["sections"]  # type: ignore[assignment]

    objects = parse_objects(sections.get("objects", []))
    data_model = parse_data_model(sections.get("data_model", []))
    rules = split_rule_statements(sections.get("rules", []))

    # Anything in a section we recognised but could not turn into structure is
    # surfaced, together with sections we found no anchor for at all.
    unparsed = list(split["leftovers"])  # type: ignore[arg-type]
    unparsed += objects["unparsed"]      # type: ignore[operator]
    unparsed += data_model["unparsed"]   # type: ignore[operator]
    unparsed += rules["preamble"]        # type: ignore[operator]

    # One group for the whole document, so callers can treat both dialects the
    # same. Endpoints stay empty here and are guessed later from the lineage
    # section; the tabular dialect knows them exactly.
    groups: List[Dict] = []
    if rules["statements"]:
        groups = [{"source": "", "target": "", "text": "",
                   "statements": rules["statements"]}]

    return {
        "dialect": "heading",
        "sections_found": sorted(sections.keys()),
        "objects": objects["objects"],
        "columns": data_model["columns"],
        "rule_groups": groups,
        "rule_statements": rules["statements"],
        "unparsed": [u for u in unparsed if u.strip()],
        "overview": "\n".join(sections.get("overview", [])).strip(),
        "review_comments": [],
        "lineage_lines": sections.get("lineage", []),
    }


# ------------------------------------------------------------------
# Dialect 2: the tabular label/value FDD
# ------------------------------------------------------------------
# The real FDDs in use are not written with section headings. They are a
# two-column table pasted as tab-separated text:
#
#   Source<TAB>IF_MASTER_DATA.ITEM_IF_ANALYSIS.INTELFOUNDRY_MATERIAL_DETAIL
#   CLIENT_ID<TAB>ITEM_ID<TAB>COMMODITY_CD          <- column names
#   250<TAB>2000-309-879<TAB>5000055                 <- SAMPLE DATA, not a spec
#   Transformation<TAB>"For the first load, load only Active data ... to
#   1. Set ACTIVE_IND = TRUE, If ...
#   Root Table<TAB>IF_SUPPLY_CHAIN.PDH_COMMON_XFRM_BASE.T_BRD_ITEM_ROOT
#   Key Columns<TAB>CLIENT_ID, ITEM_ID, ITEM_CLASS_CD
#
# Two things drive the whole dialect:
#
# 1. A Transformation block describes the object that FOLLOWS it, not the one
#    before. The text says "bring only the active records to *this view*" and the
#    view is declared on the next label row. So a Transformation's target is the
#    next declared object and its source is the previous one - which is exactly a
#    source-to-target rule set, and why this dialect can produce proper lineage
#    where the heading dialect could only guess endpoints.
#
# 2. The tab rows under an object are sample data, with the first row being
#    column names. Treating the value rows as columns would invent hundreds of
#    columns named things like "250" and "Unified Sort item".

# label -> (object_type, layer) for labels that declare an object
OBJECT_LABELS: Dict[str, Tuple[str, str]] = {
    "SOURCE": ("TABLE", "source"),
    "ROOT TABLE": ("BASE_TABLE", "root"),
    "ROOT VIEW": ("VIEW", "root_view"),
    "STAGE": ("VIEW", "stage"),
    "STAGE VIEW": ("VIEW", "stage"),
    "TIME TRAVEL": ("BASE_TABLE", "core"),
    "COMMON_ANALYSIS VIEW": ("VIEW", "core_pit"),
    "COMMON ANALYSIS VIEW": ("VIEW", "core_pit"),
    "CORE PIT": ("VIEW", "core_pit"),
    "UPI CORE PIT": ("VIEW", "upi"),
    "HYBRID CORE PIT": ("VIEW", "hybrid"),
    "METADATA - HEADER": ("BASE_TABLE", "metadata"),
    "METADATA - DETAIL": ("BASE_TABLE", "metadata"),
    "METADATA HEADER": ("BASE_TABLE", "metadata"),
    "METADATA DETAIL": ("BASE_TABLE", "metadata"),
}

# Layers that are read by the next hop, never built by it. Metadata tables are kept by
# the metadata framework (no module's FDD builds them), so a Transformation above a
# "Metadata - Detail" row belongs to the next object in the chain, not to the table.
# Read as a target, the Item Transportation Route FDD drew Calendar Horizon -> Metadata
# Detail -> Calendar Horizon: a cycle, with the Detail table as both input and output.
INPUT_LAYERS = {"source", "metadata"}

# Objects that are always read, never built by a module's FDD: the business-rule
# metadata (Header / Detail, any layer) and the calendar horizon. They hold the
# configuration and the work-week calendar the rules look values up in. Matched by
# name, because the documents label them freely ("Business Rule Metadata Detail Core
# PIT View", "Calendar Horizon Core View") and V_CRR_CALENDAR_HORIZON_CORE has no
# layer suffix: read as a Stage it took six "no transformation" hops into itself in
# the Transport Parameter import, and the metadata never reached the Stage view.
ALWAYS_INPUT_MARKERS = ("BUSINESS_RULE_METADATA", "CALENDAR_HORIZON")


def is_always_input(technical: str) -> bool:
    leaf = str(technical or "").rsplit(".", 1)[-1].upper()
    return any(marker in leaf for marker in ALWAYS_INPUT_MARKERS)

# labels that attach information to the surrounding objects
ATTRIBUTE_LABELS = {"TRANSFORMATION", "KEY COLUMNS", "NOTE", "INITIAL LOAD",
                    "INCREMENTAL LOAD", "DAILY DELTA", "PURPOSE", "OVERVIEW",
                    "OPEN", "RESOLVED"}

# A sheet header block that declares the hop up front, before its Logic:
#   Definition / Inputs / Output Table name / Output View name / Timing / Consumer / Opens
# The Transformation under it builds the declared output from the declared inputs.
# Without these, the header was read as sample data and the rule waited for the next
# object row - in the Item Transportation Route FDD that was "Metadata - Header", so
# "Exclude Items which have PLANT_CD = 'VF' ..." was attached to the metadata table.
HEADER_INPUT_LABELS = {"INPUTS", "INPUT", "INPUT TABLE", "INPUT VIEW", "INPUT TABLE NAME",
                       "INPUT VIEW NAME"}
HEADER_OUTPUT_LABELS = {"OUTPUT TABLE NAME", "OUTPUT TABLE", "OUTPUT VIEW NAME",
                        "OUTPUT VIEW", "OUTPUT", "OUTPUTS"}
HEADER_CONSUMER_LABELS = {"CONSUMER", "CONSUMERS"}
# The module's end product, declared up front ("Final View Name"). It is the last step
# of the chain, never the input of a hop: read as a chain object it became the source of
# the Item Customer Stage view and of the CUSTOMER_TYPE rule, which then ran backwards.
# Only the header's "... Name" form: a "Final View" object row at the end of the Logic
# (Production Method FDD) is the chain's last step and stays one.
HEADER_FINAL_LABELS = {"FINAL VIEW NAME", "FINAL TABLE NAME"}
HEADER_LABELS = (HEADER_INPUT_LABELS | HEADER_OUTPUT_LABELS | HEADER_CONSUMER_LABELS
                 | HEADER_FINAL_LABELS)
# An empty Transformation cell between two chain objects: the FDD says nothing happens.
EMPTY_TRANSFORMATION_TEXT = "No transformation (the Transformation cell is empty in the FDD)."
# Header text rows, read as the attribute they mean.
LABEL_ALIASES = {"OPENS": "OPEN", "OPEN QUESTIONS": "OPEN", "DEFINITION": "PURPOSE",
                 "TIMING": "PURPOSE", "FREQUENCY": "PURPOSE"}

IDENTIFIER = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _looks_like_header_row(cells: List[str]) -> bool:
    """True when every cell reads as a column name rather than a value.

    Sample data in these documents contains digits, spaces and mixed case
    ("2000-309-879", "Unified Sort item"), so requiring every cell to be a bare
    UPPER_SNAKE identifier separates the header row from the rows beneath it.
    """
    if len(cells) < 2:
        return False
    return all(IDENTIFIER.match(cell.strip()) for cell in cells if cell.strip())


def _split_label_row(line: str) -> Optional[Tuple[str, str]]:
    """Split "Label<TAB>value" into (LABEL, value) when the label is known.

    Also accepts an unknown label when the row has the shape of an object row - a
    short, word-only label and a fully qualified DB.SCHEMA.OBJECT name after it. The
    label list is a fixed vocabulary, and the Location FDD's plant-code-mapping
    section used labels outside it: every object in that chain fell through to
    "unparsed", and with them the IP_PLANT_CD -> CUSTOMER_PLANT_CD rename, because a
    rule with no object to attach to is never compiled.
    """
    if "\t" not in line:
        # An attribute label alone on its line, with its text on the lines below:
        # "Transformation" then "1. We need to remove the leading zeros ...". That is
        # how the cell arrives when the label and the text were copied separately, or
        # the text was typed under the label. Requiring a tab sent the label and all
        # ten rules under it to "unparsed". Object labels still need their name on
        # the same line, so only attribute labels are accepted this way.
        bare = re.sub(r"\s+", " ", line.strip()).upper().rstrip(":").strip()
        if bare in ATTRIBUTE_LABELS:
            return bare, ""
        if _is_bare_object_name(line):
            return "", line.strip()
        return None
    head, _, tail = line.partition("\t")
    label = re.sub(r"\s+", " ", head.strip()).upper().rstrip(":")
    # A label row shifted right by empty cells: "<TAB><TAB>Inputs<TAB>DB.SCHEMA.V_...".
    # Sheets that indent their header block arrive this way from Excel. Only a known
    # label is accepted after the shift, so indented sample data is not read as labels.
    if not label and tail.strip():
        cells = line.split("\t")
        first = next((i for i, c in enumerate(cells) if c.strip()), None)
        if first is not None and first + 1 < len(cells):
            shifted = re.sub(r"\s+", " ", cells[first].strip()).upper().rstrip(":")
            known = (shifted in OBJECT_LABELS or shifted in ATTRIBUTE_LABELS
                     or shifted in HEADER_LABELS or shifted in LABEL_ALIASES)
            if known:
                rest = "\t".join(cells[first + 1:]).strip()
                return LABEL_ALIASES.get(shifted, shifted), rest
    label = LABEL_ALIASES.get(label, label)
    if label in HEADER_LABELS:
        return label, tail.strip()
    # An object row whose label cell is empty: "<TAB>DB.SCHEMA.T_..._CORE_MEMORIES".
    # The FDDs leave the label blank under a merged "Time Travel" cell, so the
    # Memories table arrived with no label. It used to be read as a continuation of
    # the Current table's Transformation text - the Current -> Memories hop vanished
    # and every rule after it shifted one hop down the chain.
    if not label and _is_bare_object_name(tail):
        return "", tail.strip()
    if label in OBJECT_LABELS or label in ATTRIBUTE_LABELS:
        return label, tail.strip()
    if _looks_like_object_label(label) and TECHNICAL_NAME.search(tail.upper()):
        return label, tail.strip()
    return None


def _is_bare_object_name(value: str) -> bool:
    """True when the whole value is one DB.SCHEMA.OBJECT name and nothing else."""
    return bool(TECHNICAL_NAME.fullmatch(str(value or "").strip().upper()))


def _is_unlabelled_rule_text(raw: str) -> bool:
    """A row whose label cell is blank and whose next cell is rule prose.

    "<TAB>\"1. For the initial run (R0), we need to create ..." - the first cell is
    empty, there is exactly one more cell, and it starts with a rule number or is a
    sentence. Sample data has several cells, and a short note in a far column
    ("<TAB><TAB><TAB>Dropdown value from 1-5") is neither.
    """
    cells = raw.rstrip().split("\t")
    if len(cells) < 2 or cells[0].strip():
        return False
    filled = [c.strip() for c in cells if c.strip()]
    if len(filled) != 1 or cells[1].strip() != filled[0]:
        return False
    text = filled[0].lstrip('"').strip()
    return bool(RULE_NUMBER.match(text)) or len(text.split()) >= 8


def _looks_like_object_label(label: str) -> bool:
    """A label short and plain enough to be an object-type name, not rule text.

    Rule numbers ("1.", "2A", "IL") and sentences are excluded, so a Transformation
    line that happens to contain a tab and a qualified name is not mistaken for an
    object row.
    """
    if not label or len(label) > 40 or RULE_NUMBER.match(label + ". x"):
        return False
    words = label.split()
    return (1 <= len(words) <= 5
            and all(re.fullmatch(r"[A-Z][A-Z_&/-]*", w) for w in words))


# Object layer from the naming convention, for rows whose label is not in
# OBJECT_LABELS. Longest suffix first, so _CORE_PIT is not read as _PIT alone.
_LAYER_BY_SUFFIX = (
    ("_CORE_MEMORIES", ("BASE_TABLE", "core")),
    ("_CORE_CURRENT", ("BASE_TABLE", "core")),
    ("_CORE_STAGE", ("VIEW", "stage")),
    ("_CORE_PIT", ("VIEW", "core_pit")),
    ("_STAGE", ("VIEW", "stage")),
    ("_ROOT", None),                      # table or view, decided by the prefix
    ("_PIT", ("VIEW", "core_pit")),
)


def object_kind(label: str, technical: str) -> Tuple[str, str]:
    """(object_type, layer) for an object row, known label or inferred.

    Anything not following the T_BRD_/V_BRD_ convention is treated as a source: the
    unknown objects in these documents are upstream master-data views, like
    IF_MASTER_DATA.LOCATION_IF_ANALYSIS.INTELFOUNDRY_FOUNDRY_TO_PRODUCT_PLANT_MAPPING.
    Treating a source as a target is what fabricated two backwards edges in the Item
    import, so the unknown case errs towards source.
    """
    if is_always_input(technical):
        if label in OBJECT_LABELS:
            return (OBJECT_LABELS[label][0], "metadata")
        return ("VIEW" if technical.rsplit(".", 1)[-1].upper().startswith("V_")
                else "BASE_TABLE", "metadata")
    if label in OBJECT_LABELS:
        return OBJECT_LABELS[label]
    leaf = technical.rsplit(".", 1)[-1].upper()
    is_view = leaf.startswith("V_")
    is_table = leaf.startswith("T_")
    if not (is_view or is_table):
        return ("VIEW" if "VIEW" in label else "TABLE", "source")
    for suffix, kind in _LAYER_BY_SUFFIX:
        if leaf.endswith(suffix):
            if kind is None:
                return ("VIEW", "root_view") if is_view else ("BASE_TABLE", "root")
            return kind
    return ("VIEW", "stage") if is_view else ("BASE_TABLE", "core")


def looks_tabular(text: str) -> bool:
    """Decide whether to use this dialect rather than the heading dialect.

    Keyed on recognised label rows, not merely on the presence of tabs: the
    heading dialect's data model tables are tab-separated too.
    """
    labelled, has_rules, has_object = 0, False, False
    for line in (text or "").splitlines():
        row = _split_label_row(line)
        if row:
            labelled += 1
            if row[0] == "TRANSFORMATION":
                has_rules = True
            elif row[0] not in ATTRIBUTE_LABELS:
                has_object = True
            if labelled >= 3:
                return True
    # A part of an FDD imported into an existing module can be just one hop: a
    # Transformation and the object it builds. That is two label rows, and needing
    # three sent the part to the heading parser, which found nothing in it.
    return has_rules and has_object


def parse_tabular_fdd(text: str) -> Dict[str, object]:
    """Parse the tabular label/value dialect into the same shape parse_document returns.

    Produces rule_groups: one per Transformation block, each carrying the source
    and target object it sits between, so every group becomes its own rule set
    with real lineage endpoints.
    """
    objects: List[Dict] = []
    columns: List[Dict] = []
    rule_groups: List[Dict] = []
    notes: List[str] = []
    review_comments: List[Dict] = []
    unparsed: List[str] = []

    seen_objects = set()
    # The last object in the pipeline chain itself (not a declared Source). A hop's
    # real input is usually this object, even when Source rows sit in between: the
    # Item Location Root View reads T_BRD_ITEM_LOCATION_ROOT, but V_BRD_ITEM_ROOT and
    # V_BRD_PLANT_ROOT are declared between them, and the root table was lost.
    last_chain: Optional[str] = None
    pending_transformations: List[Dict] = []   # waiting for their target object
    current_object: Optional[str] = None
    awaiting_header_for: Optional[str] = None
    header_captured: set = set()
    # Sample rows typed under an object's header row: {object: {columns, offset, rows}}.
    # Kept for objects that are not in Snowflake (sample_data.py); rows are raw cells
    # so a blank cell keeps its column.
    sample_rows: Dict[str, Dict] = {}
    ordinal_by_object: Dict[str, int] = {}
    # Text from an Initial/Incremental Load row, held back to become context on
    # the next Transformation. On its own it usually describes the source data
    # ("assuming we will have both active and inactive records") rather than
    # stating a rule, and the next label row is the *source* object - so making it
    # a rule in its own right produced a rule targeting the source table.
    pending_context: List[str] = []
    # Lines belonging to the Transformation cell currently being accumulated.
    open_block: Optional[Dict] = None
    # A sheet header block's declared hop (HEADER_LABELS): its inputs and its output.
    # Held until a chain object row arrives, which starts the next section.
    header_inputs: List[str] = []
    header_output: Optional[str] = None
    header_output_label = ""
    header_used = False
    declared_inputs: List[str] = []   # every header input in the document, in order
    # Consecutive Source rows with no Transformation cell between them all feed the
    # Transformation above them: "Union of all the source views" followed by six Source
    # rows kept only one of the six as its input (Item Customer FDD, V1 sheet). One
    # Source row per cell is the other layout, and keeps its own hop.
    source_run: List[str] = []
    run_owner: Optional[Dict] = None
    final_objects: set = set()        # declared end products (HEADER_FINAL_LABELS)

    def hop_source(obj: Optional[str]) -> str:
        """A rule's input: never the declared end product - fall back to the inputs."""
        if obj and obj in final_objects:
            return declared_inputs[0] if declared_inputs else ""
        return obj or ""

    def close_block():
        nonlocal open_block
        if open_block is not None and open_block["lines"]:
            # Bound to a declared output: it already knows its target.
            if open_block.get("target"):
                rule_groups.append(open_block)
            else:
                pending_transformations.append(open_block)
        open_block = None

    def _bind_to_header(block: Dict) -> None:
        """Give a Transformation the hop its sheet header declared."""
        nonlocal header_used, last_chain
        block.update({
            "source": header_inputs[0] if header_inputs else block.get("source", ""),
            "target": header_output,
            "ambiguous_target": False,
            "declared_is_input": bool(header_inputs) or block.get("declared_is_input", False),
            "extra_sources": list(header_inputs[1:]),
            "target_from_header": True,
        })
        header_used = True
        last_chain = header_output

    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue

        split = _split_label_row(line)

        if split is None:
            cells = [c.strip() for c in line.split("\t")]
            cells = [c for c in cells if c]

            # A header row gives the current object's column names. Accepted once
            # per object, and still accepted after an intervening Key Columns or
            # Note row - the real documents put the header below those.
            if (awaiting_header_for and awaiting_header_for not in header_captured
                    and _looks_like_header_row(cells)):
                target = awaiting_header_for
                start = ordinal_by_object.get(target, 0)
                for offset, cell in enumerate(cells, start=1):
                    columns.append({
                        "technical_object_name": target,
                        "column_name": cell.strip().upper(),
                        "data_type": "",
                        "business_description": "",
                        "ordinal": start + offset,
                        "source_line": line.strip(),
                    })
                ordinal_by_object[target] = start + len(cells)
                header_captured.add(target)
                raw_cells = line.split("\t")
                offset = next((i for i, c in enumerate(raw_cells) if c.strip()), 0)
                sample_rows[target] = {"columns": [c.strip().upper() for c in cells],
                                       "offset": offset, "rows": []}
                continue

            # Continuation of an open Transformation cell.
            if open_block is not None:
                open_block["lines"].append(line.strip())
                continue

            # Rule text in the value column with the label cell left blank, because the
            # "Transformation" label is a merged cell further up. The Capacity Corridor
            # FDD put all five Stage rules this way, under its input object rows, and
            # every one went to "unparsed": the Stage got no rule set at all.
            if _is_unlabelled_rule_text(raw):
                open_block = {"source": hop_source(current_object), "target": "",
                              "lines": [*pending_context, raw.strip()],
                              "chain_source": hop_source(last_chain),
                              "declared_is_input": bool(current_object
                                                         and current_object != last_chain)}
                if header_output:
                    _bind_to_header(open_block)
                pending_context = []
                continue

            # Sample data rows under the current object's header are kept as its
            # sample; anything else is reported, not guessed at.
            if (len(cells) > 1 and awaiting_header_for in sample_rows
                    and len(sample_rows[awaiting_header_for]["rows"]) < 200):
                held = sample_rows[awaiting_header_for]
                held["rows"].append(line.split("\t")[held["offset"]:])
            elif len(cells) > 1:
                unparsed.append(f"[sample data ignored] {line.strip()[:160]}")
            else:
                unparsed.append(line.strip())
            continue

        label, value = split
        # The final view's name repeated on its own (under "Timing" in the Item Customer
        # FDD) is the same declaration, not a step of the chain.
        if label == "" and TECHNICAL_NAME.search(value.upper()) \
                and TECHNICAL_NAME.search(value.upper()).group(1) in final_objects:
            continue

        if label in HEADER_LABELS:
            close_block()
            match = TECHNICAL_NAME.search(value.upper())
            if not match:
                unparsed.append(f"[{label.title()} without a qualified name] {value[:140]}")
                continue
            technical = match.group(1)
            # A new header block after one whose rules were read starts a new section.
            if header_used and label in HEADER_INPUT_LABELS:
                header_inputs, header_output, header_output_label = [], None, ""
                header_used = False
            if label in HEADER_INPUT_LABELS:
                object_type, layer = object_kind("SOURCE", technical)
                if technical not in header_inputs:
                    header_inputs.append(technical)
                if technical not in declared_inputs:
                    declared_inputs.append(technical)
            elif label in HEADER_FINAL_LABELS:
                object_type, layer = object_kind("", technical)
                final_objects.add(technical)
            elif label in HEADER_OUTPUT_LABELS:
                object_type, layer = object_kind("", technical)
                if is_always_input(technical):
                    # Metadata and the calendar are read, never built: the rules under
                    # this header belong to the next real object.
                    review_comments.append({
                        "status": "Open",
                        "text": f"{technical} is declared as an output, but metadata and "
                                "calendar objects are inputs only; its rules were attached "
                                "to the next object instead.",
                        "near_object": technical,
                    })
                # The table is what the rules build; a view over it is the next layer.
                elif header_output is None or ("TABLE" in label
                                               and "TABLE" not in header_output_label):
                    header_output, header_output_label = technical, label
            else:
                # Reads the output; not an input or a target of these rules.
                object_type, layer = object_kind("", technical)
                layer = "consumer"
            if technical not in seen_objects:
                seen_objects.add(technical)
                objects.append({
                    "object_name": _object_name_from(technical),
                    "technical_object_name": technical,
                    "object_type": object_type,
                    "layer": layer,
                    "source_line": line.strip()[:200],
                    "inferred_from_label": label,
                })
            # Column lists under a header block are a spec table, not one object's
            # header row; do not guess which object they belong to.
            awaiting_header_for = None
            continue
        if label not in ATTRIBUTE_LABELS:
            # An empty Transformation cell waits to see the row below it (see below).
            empty_block = open_block if open_block is not None and not open_block["lines"] \
                else None
            if empty_block is None:
                close_block()
            match = TECHNICAL_NAME.search(value.upper())
            if not match:
                open_block = None
                unparsed.append(f"[{label} without a qualified name] {value[:140]}")
                continue
            technical = match.group(1)
            object_type, layer = object_kind(label, technical)
            # An object under a label outside the known list, with no Transformation
            # text waiting for it, is an input to the next hop rather than a step of
            # the chain: "Root Customer view" and "Metadata" rows sit between the Root
            # view and the Stage rules that join them. Read as chain steps, the last of
            # them became the Stage's source and the Root view dropped out.
            builds = (empty_block is not None and layer not in INPUT_LAYERS
                      and "SOURCE" not in label and bool(empty_block.get("source")))
            if (layer not in INPUT_LAYERS and label and label not in OBJECT_LABELS
                    and not pending_transformations and not builds):
                layer = "source"
            # An empty Transformation cell followed by the object it builds is a hop with
            # no change. Dropped, that object lost its hop, and with no rule waiting for
            # it a "Table" row was taken for an input. Followed by a Source row it is
            # just a blank cell above the inputs, and is dropped.
            if empty_block is not None:
                # "Source View" rows are inputs whatever their layer (FSCO Schedule WIP).
                if builds:
                    empty_block["lines"] = [EMPTY_TRANSFORMATION_TEXT]
                    close_block()
                else:
                    open_block = None

            if technical not in seen_objects:
                seen_objects.add(technical)
                objects.append({
                    "object_name": _object_name_from(technical),
                    "technical_object_name": technical,
                    "object_type": object_type,
                    "layer": layer,
                    "source_line": line.strip()[:200],
                    # Shown on the review screen, so an object picked up from a
                    # label outside the known list is a visible call, not a silent one.
                    "inferred_from_label": None if label in OBJECT_LABELS else label,
                })

            # A "Source" row declares an INPUT, never something the pipeline
            # produces, so a pending Transformation must not take it as its
            # target. Letting it happen produced two fabricated edges in the Item
            # import: Item Core PIT -> Hybrid BOM Location Core PIT, and Hybrid
            # BOM Location -> UPI BOM Location. Both are inputs from another
            # module, and both read backwards.
            if layer not in INPUT_LAYERS:
                ambiguous = len(pending_transformations) > 1
                for block in pending_transformations:
                    block["target"] = technical
                    block["ambiguous_target"] = ambiguous
                rule_groups.extend(pending_transformations)
                pending_transformations = []
            else:
                # A Transformation written above the first Source row ("Since the data
                # is transactional, load everything from the source to ROOT") has no
                # input yet. The Source declared after it is that input; without this
                # it became a second, source-less rule set for the same target.
                for block in pending_transformations:
                    if not block["source"]:
                        block["source"] = technical
                        block["declared_is_input"] = True

            if layer in INPUT_LAYERS and "SOURCE" in label and pending_transformations:
                owner = pending_transformations[-1]
                if run_owner is not owner:
                    source_run, run_owner = [], owner
                if technical not in source_run:
                    source_run.append(technical)
                if len(source_run) > 1:
                    owner.setdefault("co_inputs", [])
                    for name in source_run:
                        if name not in owner["co_inputs"]:
                            owner["co_inputs"].append(name)
            elif layer not in INPUT_LAYERS:
                source_run, run_owner = [], None
            if layer not in INPUT_LAYERS:
                last_chain = technical
                # A chain object row begins the next section; the header's hop is done.
                header_inputs, header_output, header_output_label = [], None, ""
                header_used = False
            elif header_used:
                # Any object row after the header's own rules ends the header's reach:
                # the "Source" row under its Logic, or metadata the next section reads.
                header_inputs, header_output, header_output_label = [], None, ""
                header_used = False
            current_object = technical
            awaiting_header_for = technical
            continue

        # attribute labels
        close_block()

        if label == "TRANSFORMATION":
            lines = []
            if pending_context:
                lines.extend(pending_context)
                pending_context = []
            if value.strip():
                lines.append(value.strip())
            source_run, run_owner = [], None
            open_block = {"source": hop_source(current_object), "target": "", "lines": lines,
                          "chain_source": hop_source(last_chain),
                          # Declared by a "Source" row, i.e. an input of the next target.
                          "declared_is_input": bool(current_object
                                                    and current_object != last_chain)}
            # Under a header that declared the hop, the rule builds that output from
            # those inputs - not whatever object row happens to come next.
            if header_output:
                _bind_to_header(open_block)
        elif label == "KEY COLUMNS":
            keys = [k.strip().upper() for k in re.split(r"[,;]", value) if k.strip()]
            for key in keys:
                columns.append({
                    "technical_object_name": current_object or "",
                    "column_name": key,
                    "data_type": "",
                    "business_description": "Key column",
                    "primary_key_ind": True,
                    "ordinal": None,
                    "source_line": line.strip(),
                })
        elif label in ("NOTE", "PURPOSE", "OVERVIEW"):
            notes.append(value.strip())
            # A Note is usually a caveat the approver needs to see ("VF has been
            # hardcoded for now"). It only went into the module description, where
            # nobody reviews it, so FDDs with Notes and no Open rows produced no
            # review comments at all.
            if label == "NOTE" and value.strip():
                review_comments.append({
                    "status": "Note",
                    "text": value.strip(),
                    "near_object": current_object or "",
                })
        elif label in ("OPEN", "RESOLVED"):
            if value.strip():
                review_comments.append({
                    "status": "Resolved" if label == "RESOLVED" else "Open",
                    "text": value.strip(),
                    "near_object": current_object or "",
                })
        else:
            # Initial / incremental load: held as context for the next
            # Transformation rather than becoming a rule on its own.
            if value.strip():
                pending_context.append(f"{label.title()}: {value.strip()}")

    close_block()
    # Blocks with no following object keep an empty target; the reviewer sets it.
    for block in pending_transformations:
        block.setdefault("ambiguous_target", False)
    rule_groups.extend(pending_transformations)
    # Context never followed by a Transformation is still worth keeping.
    notes.extend(pending_context)

    # Turn each block's text into numbered statements, one Excel cell at a time.
    for group in rule_groups:
        group["text"] = " ".join(group.pop("lines")).strip().strip('"').strip()
        group["statements"] = _statements_from_cells(group["text"])

    _resolve_sources_from_text(rule_groups, known_objects=seen_objects)
    _link_generic_metadata(rule_groups, [o["technical_object_name"] for o in objects
                                         if "BUSINESS_RULE_METADATA" in
                                         o["technical_object_name"].upper()])
    rule_groups = [g for g in rule_groups if g.get("statements")]
    rule_groups = _drop_redundant_boilerplate(rule_groups)
    rule_groups = _merge_same_hop(_dedupe_groups(rule_groups))

    # Objects mentioned as source or target in rule groups but never declared
    # by a label row (e.g. a qualified name inside a Transformation cell).
    # Without this, T_BRD_PLANT_CORE_MEMORIES appears as a lineage source but
    # is not in the object list, so it is never created or shown.
    for group in rule_groups:
        for endpoint in (group.get("source"), group.get("target")):
            if not endpoint:
                continue
            technical = str(endpoint).upper()
            if technical in seen_objects:
                continue
            seen_objects.add(technical)
            leaf = technical.rsplit(".", 1)[-1]
            object_type = "VIEW" if leaf.startswith("V_") else "BASE_TABLE"
            objects.append({
                "object_name": _object_name_from(technical),
                "technical_object_name": technical,
                "object_type": object_type,
                "layer": "metadata" if is_always_input(technical) else "inferred",
                "source_line": "(inferred from rule text)",
            })
        for extra in group.get("extra_sources") or []:
            extra_upper = str(extra).upper()
            if extra_upper not in seen_objects:
                seen_objects.add(extra_upper)
                leaf = extra_upper.rsplit(".", 1)[-1]
                object_type = "VIEW" if leaf.startswith("V_") else "BASE_TABLE"
                objects.append({
                    "object_name": _object_name_from(extra_upper),
                    "technical_object_name": extra_upper,
                    "object_type": object_type,
                    "layer": "metadata" if is_always_input(extra_upper) else "inferred",
                    "source_line": "(inferred from rule text)",
                })

    return {
        "dialect": "tabular",
        "sections_found": ["tabular"],
        "objects": objects,
        "columns": _dedupe_columns(columns),
        "rule_groups": rule_groups,
        "rule_statements": [s for g in rule_groups for s in g["statements"]],
        "unparsed": [u for u in unparsed if u.strip()],
        "overview": "\n".join(notes).strip(),
        "review_comments": review_comments,
        "lineage_lines": [],
        "sample_rows": {k: v for k, v in sample_rows.items() if v["rows"]},
    }


def _statements_from_cells(text: str) -> List[Dict]:
    """Numbered statements for one Transformation block.

    A block is often several Excel cells pasted together (`..." "...`). Each cell is
    split on its own, because cells are independent: the Hybrid block is one
    unnumbered cell of bullets followed by a "Substrate locations ..." cell numbered
    1-5. Split as one text, the bullets and the substrate heading became preamble
    glued onto substrate rule 1, and the substrate-only filter MATERIAL_TYPE_CD <>
    UNBW ended up applying to the whole hybrid rule.

    Text before a cell's first number is either a heading ("Substrate locations from
    source for MP team: ...") - kept as `section` on every rule of that cell so the
    model knows what the numbered rules are about - or a rule in its own right.
    Numbers that repeat across cells are prefixed with the cell ("2.1") so a rule
    set never holds two rule 1s.
    """
    cells = [c for c in _EXCEL_CELL_BREAK.split(text or "") if c.strip()] or [text or ""]
    per_cell: List[List[Dict]] = []
    for cell in cells:
        split_rules = split_rule_statements(_reflow(cell))
        statements = split_rules["statements"]
        lead = " ".join(split_rules["preamble"]).strip()
        if not statements:
            flat = _unquote_excel(cell).strip()
            column_parts = _reflow_by_column_anchors(flat)
            if len(column_parts) > 1:
                statements = []
                for part in column_parts:
                    stmt = {"rule_number": "", "text": part}
                    # Setup prose before the first COLUMN_NAME anchor (join setup,
                    # metadata lookup) is context, turned into a descriptive rule
                    # without a model call by build_import_plan.
                    if not statements and not _COLUMN_ANCHOR.search(part):
                        stmt["context"] = True
                    statements.append(stmt)
            elif flat:
                statements = [{"rule_number": "", "text": re.sub(r"\s+", " ", flat)}]
        elif lead:
            if _is_heading(lead):
                for stmt in statements:
                    stmt["section"] = lead
            elif len(cells) == 1:
                # One cell: the lead-in introduces its own list ("... depending on
                # below. 1. ..."), so it stays with rule 1 as it always has.
                statements[0]["text"] = f"{lead} {statements[0]['text']}"
            else:
                statements.insert(0, {"rule_number": "", "text": lead})
        if statements:
            per_cell.append(statements)

    used: set = set()
    out: List[Dict] = []
    next_free = 1
    for cell_index, statements in enumerate(per_cell, start=1):
        numbers = [s["rule_number"] for s in statements if s.get("rule_number")]
        prefix = f"{cell_index}." if any(n in used for n in numbers) else ""
        taken = used | {prefix + n for n in numbers}
        for stmt in statements:
            if stmt.get("rule_number"):
                number = prefix + stmt["rule_number"]
                # The same number twice in one cell ("3c." for IHS, "3c." again for the
                # plant-code mapping) is two rules, not one overwriting the other.
                copy_no = 2
                while number in used:
                    number = f"{prefix}{stmt['rule_number']}-{copy_no}"
                    copy_no += 1
                stmt["rule_number"] = number
            else:
                while str(next_free) in taken:
                    next_free += 1
                stmt["rule_number"] = str(next_free)
            used.add(stmt["rule_number"])
            taken.add(stmt["rule_number"])
            out.append(stmt)
    return out


def _is_heading(text: str) -> bool:
    """Short descriptive lead-in with nothing a rule would act on."""
    text = str(text or "").strip()
    if not text or len(text) > 250:
        return False
    if re.search(r"\bjoin\b", text, re.IGNORECASE):
        return False
    return not re.search(r"=|<>|\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b", text)


def _reflow(text: str) -> List[str]:
    """Break a Transformation cell so each numbered rule starts its own line.

    The cell arrives as one long run of text once newlines are lost to copy and
    paste, so split_rule_statements has nothing to anchor on until the numbers
    are put back at line starts.

    An earlier version required punctuation *before* the number, which swallowed
    rule 2A in "... ACTIVE_IND=TRUE 2A. Fetch the matching records ...". The fix
    for that then over-corrected by excluding a preceding period, which broke the
    ordinary case - sentences end with a period right before the next rule number
    ("... depending on below. 1. Set ACTIVE_IND = TRUE ...") - and silently lost
    four rules. The anchor is simply the numbering token at a word boundary.

    Requiring whitespace before the token is what keeps "3.1. " from splitting:
    there is no space between "3." and "1.", so no lookbehind is needed.

    Excel's cell quoting is removed first. A cell holding line breaks is copied as
    "...", with any inner quote doubled, so consecutive cells arrive joined as
    `... TURNKEY_IND = 'N'" "9. NSB_FLAG ...`. The quote sat between the space and
    the "9.", the whitespace anchor never matched, and rules 5, 9 and 10 came out as
    one rule.

    The suffix may be lowercase ("1a.", "3c.") - the Transport FDD writes them so, and
    rules 3a-3d were swallowed into rule 2. A capitalised word may follow the dot with
    no space ("5.In Stage view"); a digit may not, so "3.1" still does not split.
    """
    text = _unquote_excel(text)
    spaced = re.sub(r"\s+(?=(?:[0-9]{1,3}[A-Za-z]?|IL|DD)(?:\.\)|[.)])(?:\s|[A-Z][a-z]))",
                    "\n", text)
    return spaced.strip().splitlines()


# `" "` or `"<newline>"` between two quoted cells; a lone quote at the start or end
# of a line. The rule text itself uses single quotes for literals ('Y', 'ACT'), so a
# double quote in these positions is always Excel's, never the author's.
_EXCEL_CELL_BREAK = re.compile(r'"\s+"')
_EXCEL_EDGE_QUOTE = re.compile(r'(?m)^\s*"|"\s*$')


def _unquote_excel(text: str) -> str:
    """Undo the quoting Excel adds when multi-line cells are copied."""
    text = _EXCEL_CELL_BREAK.sub("\n", str(text or ""))
    text = _EXCEL_EDGE_QUOTE.sub("", text)
    return text.replace('""', '"')


# Column-name anchors found in real FDDs: UPPER_SNAKE_CASE followed by a
# separator (em-dash, en-dash, hyphen-minus, or a keyword like If/For/Use/Set).
# Used as a fallback when _reflow finds no numbered rules, so that multi-column
# Transformation blocks like "VENDOR_ID If SUBCONTRACTOR_IND = 'Y'... VENDOR_NM
# If SUBCONTRACTOR_IND = 'Y'..." are split into one rule per column.
#
# The name must contain an underscore to distinguish real column names
# (VENDOR_ID, LOCATION_TYPE_NM) from ordinary words (Otherwise, Setup).
_COLUMN_ANCHOR = re.compile(
    r"(?<=\S)\s+"
    r"(?=[A-Z][A-Z0-9_]*_[A-Z0-9_]+\s*(?:[\u2014\u2013\-]+\s|(?:If|For|Use|Set|Map|Fetch|Join|Load|Get|Default)\s))",
)


def _reflow_by_column_anchors(text: str) -> List[str]:
    """Split on COLUMN_NAME boundaries when no numbered rules are present.

    If the text has a preamble section before column-specific rules, the first
    chunk (everything before the first column anchor) becomes one statement that
    covers the preamble.
    """
    parts = _COLUMN_ANCHOR.split(text)
    return [p.strip() for p in parts if p.strip()]


def _object_name_from(technical: str) -> str:
    """A readable business name from T_BRD_ITEM_SALES_DETAIL_ROOT.

    The kind is appended because stripping the prefix alone collides: the Item
    FDD declares both T_BRD_ITEM_ROOT and V_BRD_ITEM_ROOT, which would otherwise
    both become "Item Root" - two objects with one name, and two rule sets with
    one name. This also matches how the repository already names things
    ("BOM Root Base Table", "BOM Stage View").
    """
    leaf = technical.rsplit(".", 1)[-1]
    kind = ""
    if leaf.startswith("V_"):
        kind = " View"
    elif leaf.startswith("T_"):
        kind = " Table"
    elif leaf.startswith("F_"):
        kind = " Function"

    # Longer prefixes first: regex alternation is leftmost-first, so a bare "F_"
    # listed before "F_BRD_" would strip only two characters and leave "Brd ...".
    body = re.sub(r"^(T_BRD_|V_BRD_|F_BRD_|T_CRR_|V_CRR_|F_CRR_|T_|V_|F_)", "", leaf)
    words = [_ACRONYMS.get(w.upper(), w.title()) for w in body.split("_") if w]
    return (" ".join(words) + kind).strip()


# Left uppercase rather than title-cased, so a name reads the way the team writes
# it: "Item Core PIT View", not "Item Core Pit View".
_ACRONYMS = {"PIT": "PIT", "UPI": "UPI", "BOM": "BOM", "ID": "ID", "DP": "DP",
             "CD": "CD", "NM": "NM", "IND": "IND", "UI": "UI", "CSV": "CSV"}


# An object named without database and schema: "Join V_BRD_ITEM_LOCATION_ROOT with
# V_BRD_ITEM_CORE_PIT". Not preceded by a dot, so it is not the tail of a full name.
_LEAF_REF = re.compile(r"(?<![.\w])([TV]_(?:BRD|CRR)_[A-Z0-9_]+)\b")
_LAYER_SUFFIXES = ("_CORE_MEMORIES", "_CORE_CURRENT", "_CORE_STAGE", "_CORE_PIT",
                   "_MEMORIES", "_CURRENT", "_STAGE", "_ROOT", "_PIT")


def entity_stem(technical: str) -> str:
    """ITEM_LOCATION for T_BRD_ITEM_LOCATION_ROOT, V_BRD_ITEM_LOCATION_STAGE, ...

    Objects with the same stem are layers of one entity, so one feeds the next. The
    stem is what separates V_BRD_UPI_ITEM_LOCATION_CORE_PIT (UPI_ITEM_LOCATION) from
    V_BRD_HYBRID_ITEM_LOCATION_STAGE (HYBRID_ITEM_LOCATION), which sit next to each
    other in the FDD without one feeding the other.
    """
    leaf = _LEAF_PREFIX.sub("", str(technical or "").rsplit(".", 1)[-1].upper())
    for suffix in _LAYER_SUFFIXES:
        if leaf.endswith(suffix):
            return leaf[: -len(suffix)]
    return leaf


def _resolve_sources_from_text(groups: List[Dict], known_objects=None) -> None:
    """Work out each hop's inputs: the chain object before it, plus what the text names.

    Positional adjacency - "the source is the previous declared object" - is only a
    guess, and it was wrong in the Item FDD: the Hybrid Item Core PIT rules join the
    Hybrid BOM with V_BRD_ITEM_CORE_PIT, but the object declared just above them was
    V_BRD_UPI_ITEM_CORE_PIT. So the inputs are, in order:

      1. the previous object in the chain, when it is a layer of the same entity as
         the target (T_BRD_ITEM_LOCATION_ROOT -> V_BRD_ITEM_LOCATION_ROOT) - true even
         when the text never mentions it, which is the normal case;
      2. the declared object above the cell, when the text refers to it loosely
         ("the ITEM_ID in ITEM_LOCATION root View");
      3. every object the text names - in full, or by its leaf name when the leaf
         matches an object in the document ("Join V_BRD_ITEM_LOCATION_ROOT with ...").

    Leaf names that match nothing in the document are kept in `unqualified_refs`, for
    the plan to resolve against the repository.
    """
    by_leaf: Dict[str, str] = {}
    for name in known_objects or ():
        by_leaf.setdefault(str(name).upper().rsplit(".", 1)[-1], str(name).upper())

    for group in groups:
        target = str(group.get("target") or "").upper()
        declared = str(group.get("source") or "").upper()
        chain = str(group.get("chain_source") or "").upper()
        text = str(group.get("text") or "")
        upper = text.upper()

        mentioned = list(TECHNICAL_NAME.findall(upper))
        unqualified: List[str] = []
        for leaf in _LEAF_REF.findall(upper):
            if leaf in by_leaf:
                mentioned.append(by_leaf[leaf])
            elif leaf not in unqualified:
                unqualified.append(leaf)
        candidates = [n for n in dict.fromkeys(mentioned) if n != target]
        from_text = bool(candidates)

        if declared and declared != target and declared not in candidates \
                and _text_refers_to(text, declared):
            candidates.insert(0, declared)
            from_text = True
        # A "Source" row declares an input of the next object in the chain, even when
        # its Transformation cell only says "no major transformation".
        if group.get("declared_is_input") and declared and declared != target \
                and declared not in candidates:
            candidates.append(declared)
        if chain and chain != target and target and entity_stem(chain) == entity_stem(target):
            if chain in candidates:
                candidates.remove(chain)
            candidates.insert(0, chain)
            from_text = True

        group["unqualified_refs"] = unqualified
        for extra in group.get("co_inputs") or []:
            if extra != target and extra not in candidates:
                candidates.append(extra)
        if candidates:
            group["source"] = candidates[0]
            group["extra_sources"] = candidates[1:]
            # Only a declared Source row, nothing stated: positional, so it gives way
            # to a hop whose inputs the text does state (_drop_redundant_boilerplate).
            group["source_from_text"] = from_text
        else:
            group.setdefault("extra_sources", [])
            group["source_from_text"] = False


_LEAF_PREFIX = re.compile(r"^(?:[TVF]_(?:BRD|CRR)_|[TVF]_)")

# "using metadata where SubjectAreaNm = 'Parameter' ..." - the metadata is read, but no
# object is named. Needs the word and one of the metadata keys, so a passing "metadata"
# in prose does not add a source.
_METADATA_WORD = re.compile(r"\bmeta\s*-?\s*data\b", re.IGNORECASE)
_METADATA_KEYS = re.compile(
    r"\b(?:SUBJECT_?AREA_?NM|ENTITY_?NM|BUSINESS_?RULE_?NM|COLUMN_?VALUE_?\w*_?TXT)\b",
    re.IGNORECASE)


def _link_generic_metadata(groups: List[Dict], metadata_objects: List[str]) -> None:
    """Add the document's metadata objects as inputs of a hop whose rules read
    "the metadata" without naming it. Rules 2, 4 and 6 of the Transport Parameter
    Stage view do this; only rule 3 named the Detail view."""
    metadata_objects = [str(m).upper() for m in dict.fromkeys(metadata_objects or [])]
    if not metadata_objects:
        return
    for group in groups:
        text = str(group.get("text") or "")
        if not (_METADATA_WORD.search(text) and _METADATA_KEYS.search(text)):
            continue
        target = str(group.get("target") or "").upper()
        have = {str(group.get("source") or "").upper(),
                *[str(e).upper() for e in group.get("extra_sources") or []]}
        added = [m for m in metadata_objects if m not in have and m != target]
        if not added:
            continue
        if not group.get("source"):
            group["source"], added = added[0], added[1:]
        group.setdefault("extra_sources", []).extend(added)


def _text_refers_to(text: str, technical: str) -> bool:
    """True when the text names this object loosely - its words, in order, nearby.

    "ITEM_LOCATION root View" refers to V_BRD_ITEM_LOCATION_ROOT: the words ITEM,
    LOCATION and ROOT appear in that order within a few words of each other. The
    prefix (V_BRD_) and view/table wording are ignored because authors drop them.
    """
    leaf = technical.rsplit(".", 1)[-1].upper()
    wanted = [w for w in _LEAF_PREFIX.sub("", leaf).split("_") if w]
    if not wanted:
        return False
    words = re.sub(r"[^A-Z0-9]+", " ", str(text or "").upper()).split()
    window = len(wanted) + 2
    for start, word in enumerate(words):
        if word != wanted[0]:
            continue
        position, found = start, 1
        for needed in wanted[1:]:
            try:
                position = words.index(needed, position + 1, start + window)
            except ValueError:
                break
            found += 1
        if found == len(wanted):
            return True
    return False


def _drop_redundant_boilerplate(groups: List[Dict]) -> List[Dict]:
    """Remove boilerplate hops whose target already has a properly sourced hop.

    A "We don't have any major transformation now." cell carries no information
    about lineage, and it drifts: sitting between two Source rows it waits for the
    next real output and arrives with only a positional guess for its source. In
    the Item FDD that produced HYBRID BOM Location -> UPI Item Core PIT, an edge
    the document never states.

    So when a target already has a hop whose source came from the Transformation
    text, any boilerplate hop pointing at the same target with a merely positional
    source is dropped. The authoritative statement wins and the guess goes.

    Boilerplate that is the ONLY thing describing a target is kept - a genuine
    pass-through hop still needs its edge and its rule.
    """
    authoritative = {
        str(g.get("target") or "").upper()
        for g in groups if g.get("source_from_text")
    }
    # Targets described by more than one hop. A "no transformation" cell under one
    # chain object that drifted to the next object of a *different* entity is a guess,
    # not lineage, when that object has another hop of its own: the Transport FDD drew
    # Delivery Route Root View -> Shipping Point Detail Root Table this way, next to the
    # real Intelfoundry Shipping Point Detail -> Shipping Point Detail Root Table hop.
    def _drifted(g: Dict) -> bool:
        source = str(g.get("source") or "").upper()
        target = str(g.get("target") or "").upper()
        return bool(source and target
                    and all(is_no_transformation(s.get("text", ""))
                            for s in g.get("statements") or [])
                    and not g.get("declared_is_input") and not g.get("extra_sources")
                    and entity_stem(source) != entity_stem(target))

    other_hops_into: Dict[str, int] = {}
    for g in groups:
        if not _drifted(g):
            key = str(g.get("target") or "").upper()
            other_hops_into[key] = other_hops_into.get(key, 0) + 1
    kept: List[Dict] = []
    dropped_inputs: Dict[str, List[str]] = {}
    for group in groups:
        target = str(group.get("target") or "").upper()
        is_boilerplate = all(is_no_transformation(s.get("text", ""))
                             for s in group.get("statements") or [])
        if (target in authoritative and is_boilerplate
                and not group.get("source_from_text")):
            if group.get("declared_is_input") and group.get("source"):
                dropped_inputs.setdefault(target, []).append(str(group["source"]).upper())
            continue
        if _drifted(group) and other_hops_into.get(target, 0) > 0:
            continue
        kept.append(group)
    # A declared Source the real rules never mention: kept visible for review rather
    # than silently becoming an input (it may feed a later object in the chain).
    for group in kept:
        target = str(group.get("target") or "").upper()
        used = {str(group.get("source") or "").upper(),
                *[str(e).upper() for e in group.get("extra_sources") or []]}
        unused = [d for d in dropped_inputs.get(target, []) if d not in used]
        if unused and group.get("source_from_text"):
            group["declared_unused"] = list(dict.fromkeys(unused))
            dropped_inputs[target] = []
    return kept


def _merge_same_hop(groups: List[Dict]) -> List[Dict]:
    """Collapse Transformation cells that describe the same source-to-target hop.

    A hop is one edge in the pipeline and should own one rule set. The Item import
    produced two rule sets both called "Item Core PIT View Transformation", both
    T_BRD_ITEM_CORE_CURRENT -> V_BRD_ITEM_CORE_PIT, because two separate
    Transformation cells resolved to the same hop with different wording.
    """
    merged: Dict[Tuple[str, str], Dict] = {}
    order: List[Tuple[str, str]] = []
    for group in groups:
        key = (str(group.get("source") or "").upper(),
               str(group.get("target") or "").upper())
        if key in merged:
            host = merged[key]
            combined = list(host["statements"]) + list(group["statements"])
            real = [st for st in combined if not is_no_transformation(st.get("text", ""))]
            # "No major transformation" next to real rules on the same hop says
            # nothing; keeping it gave the hop a rule 1 that contradicts rule 2.
            host["statements"] = real or combined[:1]
            host["text"] = f"{host.get('text', '')} {group.get('text', '')}".strip()
            for extra in group.get("extra_sources") or []:
                if extra not in host.setdefault("extra_sources", []):
                    host["extra_sources"].append(extra)
            # Merging resolves the ambiguity: both cells belong to this hop.
            host["ambiguous_target"] = False
            continue
        merged[key] = dict(group)
        order.append(key)
    out = [merged[key] for key in order]

    # The same cell repeated under a later Source row (the Item Customer Union cell)
    # names one of the inputs a fuller hop into that target already has: one hop.
    def inputs(g):
        return {str(x).upper() for x in [g.get("source"), *(g.get("extra_sources") or [])] if x}

    def words(g):
        return " ".join(str(g.get("text") or "").split()).upper()

    kept: List[Dict] = []
    for group in out:
        covered = any(other is not group
                      and str(other.get("target") or "").upper() == str(group.get("target") or "").upper()
                      and inputs(group) < inputs(other) and words(group) == words(other)
                      for other in out)
        if not covered:
            kept.append(group)
    return kept


def _dedupe_groups(groups: List[Dict]) -> List[Dict]:
    """Collapse groups that are the same hop with the same text.

    Two consecutive Transformation cells both resolve to the next object, so a
    document that repeats boilerplate like "We don't have any major
    transformation now." would otherwise produce the identical rule twice on the
    same hop.
    """
    seen = set()
    kept: List[Dict] = []
    for group in groups:
        key = (str(group.get("source", "")).upper(),
               str(group.get("target", "")).upper(),
               re.sub(r"\s+", " ", str(group.get("text", ""))).strip().lower())
        if key in seen:
            continue
        seen.add(key)
        kept.append(group)
    return kept


def _dedupe_columns(columns: List[Dict]) -> List[Dict]:
    """One row per object+column, with key-column flags preserved."""
    merged: Dict[Tuple[str, str], Dict] = {}
    for column in columns:
        key = (str(column.get("technical_object_name", "")).upper(),
               str(column.get("column_name", "")).upper())
        if key in merged:
            if column.get("primary_key_ind"):
                merged[key]["primary_key_ind"] = True
                merged[key]["business_description"] = (
                    merged[key].get("business_description") or "Key column")
            continue
        merged[key] = dict(column)
    return list(merged.values())


# ------------------------------------------------------------------
# Stage 2: turn the parse into a reviewable plan
# ------------------------------------------------------------------
# Default origin for a pasted column.
#
# Deliberately PASS_THROUGH, not DERIVED. A bulk load that defaulted to DERIVED
# put 78 straight pulls into the repository marked as needing transformation
# logic, which made the FDD read as though rules were missing and inflated the
# apparent rule gap to 118. DERIVED should be a choice the reviewer makes, not a
# default they inherit.
DEFAULT_COLUMN_ORIGIN = "PASS_THROUGH"


def compare_columns(pasted: List[Dict], live: List[Dict]) -> List[Dict]:
    """Reconcile the pasted DATA MODEL against Snowflake's actual columns.

    Both sources are kept and every disagreement is flagged rather than one
    silently winning. Flags:
      match           - same name, compatible type
      type_mismatch   - same name, different declared type
      pasted_only     - in the FDD but not in Snowflake (not built, or misnamed)
      snowflake_only  - in Snowflake but absent from the FDD (spec incomplete)
    """
    live_by_name = {str(c.get("column_name", "")).upper(): c for c in live}
    pasted_by_name = {str(c.get("column_name", "")).upper(): c for c in pasted}
    rows: List[Dict] = []

    for column in pasted:
        name = str(column.get("column_name", "")).upper()
        match = live_by_name.get(name)
        if match is None:
            flag = "pasted_only"
        else:
            pasted_type = _normalise_type(column.get("data_type"))
            live_type = _normalise_type(match.get("data_type"))
            # An unspecified type in the FDD is not a disagreement; Snowflake fills it.
            flag = "match" if (not pasted_type or pasted_type == live_type) else "type_mismatch"
        rows.append({
            "column_name": name,
            "pasted_type": (column.get("data_type") or "").upper(),
            "live_type": (match or {}).get("data_type", "") or "",
            "business_description": column.get("business_description", ""),
            "ordinal": column.get("ordinal"),
            "flag": flag,
        })

    for name, column in live_by_name.items():
        if name not in pasted_by_name:
            rows.append({
                "column_name": name,
                "pasted_type": "",
                "live_type": column.get("data_type", "") or "",
                "business_description": column.get("business_description", "") or "",
                "ordinal": column.get("ordinal"),
                "flag": "snowflake_only",
            })

    return rows


def _normalise_type(raw: Optional[str]) -> str:
    """Compare types by family and precision, ignoring spelling differences."""
    text = re.sub(r"\s+", "", str(raw or "")).upper()
    if not text:
        return ""
    text = text.replace("TEXT", "VARCHAR").replace("STRING", "VARCHAR")
    text = text.replace("INTEGER", "NUMBER").replace("INT", "NUMBER")
    text = text.replace("DECIMAL", "NUMBER").replace("NUMERIC", "NUMBER")
    # VARCHAR with no length is the same family as VARCHAR(n) for review purposes.
    return text


def is_no_transformation(text: str) -> bool:
    """True when a Transformation cell says, in effect, "nothing happens here".

    These are everywhere in the real documents - "We don't have any major
    transformation now." appears on most hops. Sending them to the model costs
    seconds to minutes each and can only ever come back with no actions, so they
    are recognised here and turned into a NO_TRANSFORMATION rule directly.

    Apostrophes are deleted rather than blanked: turning "don't" into "don t"
    stopped the single most common phrasing in these documents from matching.
    """
    text = re.sub(r"['\u2019]", "", str(text or "").lower())
    normalised = re.sub(r"[^a-z ]", " ", text)
    normalised = re.sub(r"\s+", " ", normalised).strip()
    if not normalised or len(normalised) > 200:
        return False
    return any(phrase in normalised for phrase in (
        "no major transformation",
        "have any major transformation",
        "no transformation",
        "straight pull",
        "no change",
        "as is",
        "carried through unchanged",
        "copies every record straight through",
        "copy every record",
        "records are carried through",
        "time traveled",
        "time travel",
        "data moves to memories",
        "moves to memories table",
        "current table data moves to memories",
    ))


# "all data will be loaded from the source to the ROOT table", "load all records",
# "truncate and load". A full load names no columns because it moves every one; the
# model, given the target's column list, answered with one COPY per column.
_FULL_LOAD = re.compile(
    r"\ball\s+(?:the\s+)?(?:data|records|rows|columns)\b[^.]{0,40}?\bload"
    r"|\bload(?:ed|s|ing)?\s+all\b|\bfull\s+load\b|\btruncat\w*\b[^.]{0,60}\bload"
    r"|\bload\w*\b[^.]{0,60}\btruncat", re.IGNORECASE)
# Anything that makes the load selective or derived is a real rule, not a pass-through.
_SELECTIVE = re.compile(r"=|<>|\bjoin\w*\b|\bwhere\b|\bonly\b|\bexcept\b|"
                        r"\bderive\w*\b|\bcase\b|\baggregat\w*\b|\bgroup\s+by\b",
                        re.IGNORECASE)


def is_full_load(text: str) -> bool:
    """True for "load everything as it is" - no filter, join or derivation stated."""
    text = str(text or "")
    return bool(_FULL_LOAD.search(text)) and not _SELECTIVE.search(text)


def full_load_proposal(text: str) -> Dict:
    """One action for the whole record, never one per column."""
    truncate = bool(re.search(r"\btruncat", str(text or ""), re.IGNORECASE))
    return {
        "rule_name": "Full load" + (" (truncate and load)" if truncate else ""),
        "rule_category": "INITIAL_LOAD",
        "business_description": str(text or "").strip(),
        "priority": 99,
        "rule_status": "DRAFT",
        "conditions": [],
        "actions": [{
            "target_field": "RECORD",
            "action_type": "NO_TRANSFORMATION",
            "action_value": "TRUNCATE_AND_LOAD" if truncate else "",
            "action_description": ("The target is truncated, then every record and every "
                                   "column is loaded from the source unchanged."
                                   if truncate else
                                   "Every record and every column is loaded from the source "
                                   "unchanged."),
        }],
        "joins": [],
    }


_TIME_TRAVEL_WORDS = re.compile(r"time[\s-]*travel|moves? to memories", re.IGNORECASE)


def no_transformation_proposal(text: str, rule_number: str) -> Dict:
    """A complete, valid proposal for a pass-through hop - no model needed.

    "Active records will be time-traveled ... moves to memories table" is a pass-through
    too, but it states the history mechanism, so it is recorded as TIME_TRAVEL rather
    than NO_TRANSFORMATION - the category the repository uses for that hop.
    """
    if _TIME_TRAVEL_WORDS.search(str(text or "")):
        return {
            "rule_name": "Time travel",
            "rule_category": "TIME_TRAVEL",
            "business_description": str(text or "").strip(),
            "priority": 99,
            "rule_status": "DRAFT",
            "conditions": [],
            "actions": [{
                "target_field": "RECORD",
                "action_type": "CARRY_FORWARD",
                "action_value": "",
                "action_description": "Records are versioned: current rows are kept, "
                                      "superseded rows move to history.",
            }],
            "joins": [],
        }
    return {
        "rule_name": "No transformation",
        "rule_category": "NO_TRANSFORMATION",
        "business_description": str(text or "").strip(),
        "priority": 99,
        "rule_status": "DRAFT",
        "conditions": [],
        "actions": [{
            "target_field": "RECORD",
            "action_type": "NO_TRANSFORMATION",
            "action_value": "",
            "action_description": "Records are carried through unchanged.",
        }],
        "joins": [],
    }


def build_import_plan(
    parsed: Dict,
    module_name: str,
    *,
    compile_fn: Optional[Callable] = None,
    live_columns_fn: Optional[Callable] = None,
    existing_objects: Optional[List[Dict]] = None,
    rule_set_name: str = "",
    progress_fn: Optional[Callable] = None,
    live_failure_fn: Optional[Callable] = None,
) -> Dict:
    """Build a complete, reviewable plan. Reads only - writes nothing.

    compile_fn(text, rule_number, source_object, target_object)
        the existing requirement compiler, injected so this stays testable and so
        rules imported from a document go through exactly the same validation as
        rules typed into the Write a rule page. A second parser would be a second
        thing to keep correct.

    live_columns_fn(technical_name) -> list of {column_name, data_type, ...}
        Snowflake's current columns, for the pasted-vs-actual comparison.

    existing_objects: [{technical_object_name, object_id, object_name}]
        already in the repository, so the plan reuses rather than duplicates.

    progress_fn(done, total, label)
        called before each rule is compiled. Compiling is the slow part - one
        model call per rule, tens of seconds each - so without this the page shows
        a bare spinner and a long document is indistinguishable from a hang.
    """
    existing_by_name = {
        str(o.get("technical_object_name", "")).upper(): o
        for o in (existing_objects or [])
    }

    planned_objects: List[Dict] = []
    for obj in parsed.get("objects", []):
        technical = str(obj.get("technical_object_name", "")).upper()
        existing = existing_by_name.get(technical)

        pasted_cols = [c for c in parsed.get("columns", [])
                       if str(c.get("technical_object_name", "")).upper() == technical]
        live_cols: List[Dict] = []
        lookup_error = None
        if live_columns_fn is not None:
            try:
                live_cols = live_columns_fn(technical) or []
            except Exception as exc:
                lookup_error = str(exc)
        # An empty result and a failed lookup are different things. Reporting both
        # as "not built" is what hid a NameError that made every live column
        # lookup in the app return nothing.
        if not live_cols and lookup_error is None and live_failure_fn is not None:
            try:
                lookup_error = live_failure_fn(technical)
            except Exception:
                lookup_error = None

        comparison = compare_columns(pasted_cols, live_cols)
        held = (parsed.get("sample_rows") or {}).get(technical) or {}
        planned_objects.append({
            **obj,
            # Sample rows typed under the object in the FDD. Saved at import only when
            # the object is not in Snowflake, unless the reviewer ticks keep_fdd_sample.
            "fdd_sample": ({"columns": held["columns"], "rows": held["rows"]}
                           if held.get("rows") else None),
            "action": "reuse" if existing else "create",
            "object_id": (existing or {}).get("object_id"),
            "exists_in_snowflake": bool(live_cols),
            "lookup_error": lookup_error,
            "columns": comparison,
            "counts": {
                flag: sum(1 for c in comparison if c["flag"] == flag)
                for flag in ("match", "type_mismatch", "pasted_only", "snowflake_only")
            },
        })

    # Columns whose object heading was never established, so they cannot be
    # attached to anything. Surfaced instead of silently attached to the wrong
    # object, which would be indistinguishable from a correct import.
    known = {str(o.get("technical_object_name", "")).upper() for o in parsed.get("objects", [])}
    orphan_columns = [c for c in parsed.get("columns", [])
                      if str(c.get("technical_object_name", "")).upper() not in known]

    planned_rules: List[Dict] = []
    planned_rule_sets: List[Dict] = []

    groups = parsed.get("rule_groups") or []
    if not groups and parsed.get("rule_statements"):
        groups = [{"source": "", "target": "",
                   "statements": parsed["rule_statements"]}]

    total_statements = sum(len(g.get("statements", [])) for g in groups)
    done = 0
    document_objects = [str(o.get("technical_object_name", "")).upper()
                        for o in parsed.get("objects", [])]
    rich_compile = _accepts_context(compile_fn)

    for group_index, group in enumerate(groups):
        source = str(group.get("source") or "")
        target = str(group.get("target") or "")
        # Endpoints are known exactly in the tabular dialect. The heading dialect
        # has one group and no endpoints, so fall back to the lineage guess.
        if not source and not target:
            source, target = _endpoints(
                {"lineage_lines": parsed.get("lineage_lines", [])},
                parsed.get("objects", []),
                range(len(parsed.get("objects", []))),
            )

        indexes: List[int] = []
        hop_sources = [x for x in [source, *(group.get("extra_sources") or [])] if x]
        hop_texts = [st_.get("text", "") for st_ in group.get("statements", [])]
        for statement in group.get("statements", []):
            text = statement.get("text", "")
            number = statement.get("rule_number") or ""
            context = {
                "section": statement.get("section", ""),
                "neighbours": [t for t in hop_texts if t != text],
                "objects": document_objects,
            }
            entry = {
                "rule_number": number,
                "text": text,
                "original_text": text,
                "rule_set_index": group_index,
                "source": source,
                "target": target,
                "proposal": None,
                "preview": "",
                "errors": [],
                "warnings": [],
                "used_model": False,
                "sources": hop_sources,
                "context": context,
                "status": "green",
                "reasons": [],
            }

            done += 1
            if progress_fn is not None:
                try:
                    progress_fn(done, total_statements,
                                f"{_leaf_name(target) or module_name}: rule {number or done}")
                except Exception:
                    pass

            if is_full_load(text):
                entry["proposal"] = full_load_proposal(text)
                entry["preview"] = ("**Full load** — every record and column is loaded "
                                    "unchanged; no column-by-column rule is needed.")
                indexes.append(len(planned_rules))
                planned_rules.append(entry)
                continue

            if is_no_transformation(text):
                # Recognised boilerplate. Handled without a model call, which is
                # the single biggest saving available: most hops in these
                # documents say nothing more than "no major transformation now".
                entry["proposal"] = no_transformation_proposal(text, number)
                entry["preview"] = ("**No transformation** — records are carried "
                                    "through unchanged.")
                indexes.append(len(planned_rules))
                planned_rules.append(entry)
                continue

            if statement.get("context"):
                entry["status"] = "amber"
                entry["reasons"] = ["Saved as written, not as executable logic."]
                # Setup preamble from a column-anchor split. This is descriptive
                # prose (join setup, metadata lookup instructions) that is not a
                # per-column rule. Sending it to the model would hang because it
                # is long and multi-topic. Saved as written so the text is kept
                # without paying for a model call - and without the NO_TRANSFORMATION
                # action it used to carry, which every reader took as "copies the
                # source unchanged".
                entry["proposal"] = aw.build_proposal(text, rule_name="Setup context",
                                                      priority=0)
                entry["as_written"] = True
                entry["as_written_notes"] = aw.notes_for(text, None, "FDD_IMPORT")
                entry["preview"] = aw.preview(text)
                indexes.append(len(planned_rules))
                planned_rules.append(entry)
                continue

            if compile_fn is not None:
                try:
                    # Source and target are passed through so the compiler can
                    # validate field names against the real objects instead of
                    # guessing - the heading dialect could not supply these.
                    extra = ({"sources": hop_sources, "context": context}
                             if rich_compile else {})
                    result = compile_fn(
                        text,
                        rule_number=number,
                        source_object=source,
                        target_object=target,
                        **extra,
                    )
                    entry["proposal"] = result.get("proposal")
                    entry["preview"] = result.get("preview", "")
                    entry["errors"] = list(result.get("errors") or [])
                    entry["warnings"] = list(result.get("warnings") or [])
                    entry["used_model"] = bool(result.get("used_model", True))
                    entry["doubts"] = list(result.get("doubts") or [])
                    entry["fdd_issues"] = list(result.get("fdd_issues") or [])
                    entry["findings"] = list(result.get("findings") or [])
                    entry["issues"] = list(result.get("issues") or [])
                    entry["model_issues"] = list(result.get("model_issues") or [])
                    entry["resolutions"] = {}
                    entry["status"] = result.get("status") or (
                        "red" if entry["errors"] else "amber" if entry["warnings"] else "green")
                    entry["reasons"] = list(result.get("reasons")
                                            or entry["errors"] + entry["warnings"])
                except Exception as exc:
                    entry["errors"] = [f"could not compile: {exc}"]
                    entry["status"] = "red"
                    entry["reasons"] = entry["errors"]
            indexes.append(len(planned_rules))
            planned_rules.append(entry)

        planned_rule_sets.append({
            "name": _rule_set_name(module_name, source, target),
            "source_technical_object": source,
            "target_technical_object": target,
            # Additional inputs the Transformation text names. A stage view really
            # does read several objects, and RULE_SET_SOURCE is built to hold them.
            "extra_sources": list(group.get("extra_sources") or []),
            "source_from_text": bool(group.get("source_from_text")),
            "ambiguous_target": bool(group.get("ambiguous_target")),
            # Declared as a Source for this target, but no rule here uses it.
            "declared_unused": list(group.get("declared_unused") or []),
            # Named without database/schema and not declared anywhere in the FDD.
            "unqualified_refs": list(group.get("unqualified_refs") or []),
            "rule_indexes": indexes,
            "text": group.get("text", ""),
        })

    blocking = sum(1 for r in planned_rules if r["errors"])
    return {
        "module_name": module_name,
        "dialect": parsed.get("dialect", "heading"),
        "rule_set_name": rule_set_name or f"{module_name} Transformation",
        "rule_sets": planned_rule_sets,
        "overview": parsed.get("overview", ""),
        "objects": planned_objects,
        "orphan_columns": orphan_columns,
        "rules": planned_rules,
        "unparsed": parsed.get("unparsed", []),
        "review_comments": parsed.get("review_comments", []),
        "lineage_lines": parsed.get("lineage_lines", []),
        "completed_names": parsed.get("completed_names", []),
        "summary": {
            "objects_to_create": sum(1 for o in planned_objects if o["action"] == "create"),
            "objects_reused": sum(1 for o in planned_objects if o["action"] == "reuse"),
            "columns": sum(len(o["columns"]) for o in planned_objects),
            "type_mismatches": sum(o["counts"]["type_mismatch"] for o in planned_objects),
            "pasted_only": sum(o["counts"]["pasted_only"] for o in planned_objects),
            "snowflake_only": sum(o["counts"]["snowflake_only"] for o in planned_objects),
            "orphan_columns": len(orphan_columns),
            "lookup_errors": sum(1 for o in planned_objects if o.get("lookup_error")),
            "rules": len(planned_rules),
            "rule_sets": len(planned_rule_sets),
            "ambiguous_targets": sum(1 for rs in planned_rule_sets if rs["ambiguous_target"]),
            "extra_sources": sum(len(rs.get("extra_sources") or []) for rs in planned_rule_sets),
            "sources_from_text": sum(1 for rs in planned_rule_sets
                                     if rs.get("source_from_text")),
            "missing_targets": sum(1 for rs in planned_rule_sets
                                   if not rs["target_technical_object"]),
            "rules_with_errors": blocking,
            "status_counts": {k: sum(1 for r in planned_rules if r.get("status") == k)
                              for k in ("green", "amber", "red")},
            "unparsed_lines": len(parsed.get("unparsed", [])),
            "review_comments": len(parsed.get("review_comments", [])),
        },
        "can_apply": blocking == 0 and bool(planned_objects or planned_rules),
    }


def _accepts_context(fn) -> bool:
    """True when compile_fn takes the hop's sources and context (the app's does)."""
    if fn is None:
        return False
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.kind == p.VAR_KEYWORD or p.name == "context" for p in params)


def _rule_set_name(module_name: str, source: str, target: str) -> str:
    """A readable rule set name for one source-to-target hop."""
    if target:
        return f"{_object_name_from(target)} Transformation"
    return f"{module_name} Transformation"


def _leaf_name(technical: str) -> str:
    """Object name without database and schema, for progress messages."""
    return str(technical or "").rsplit(".", 1)[-1]


# ------------------------------------------------------------------
# Stage 3: apply an approved plan
# ------------------------------------------------------------------

# ------------------------------------------------------------------
# Completeness: every rule in the FDD is imported, none is left out
# ------------------------------------------------------------------
def entry_status(entry: Dict) -> str:
    """green / amber / red for one plan rule, counting what the reviewer dealt with."""
    import rule_extractor as rx
    if entry.get("errors"):
        return "red"
    # A person chose to keep it in the FDD's words, or accepted it as it is.
    if entry.get("as_written") == "chosen" or entry.get("accepted_as_is"):
        return "green"
    if "issues" in entry:
        return rx.status_of(entry["issues"], entry.get("resolutions"))
    return entry.get("status") or ("amber" if entry.get("warnings") else "green")


def entry_covered(entry: Dict) -> bool:
    """Ready to import: already in the module, or compiled with every problem settled."""
    if entry.get("prior") and entry.get("prior_choice") == "skip":
        return (entry.get("prior") or {}).get("state") != CHANGED or bool(entry.get("prior_seen"))
    return bool(entry.get("proposal")) and entry_status(entry) == "green"


SAMPLE_TAG = "[sample data ignored]"
# Words that make a "sample" line worth a person's look: an FDD note or assumption.
_NOTE_WORDS = re.compile(r"\b(note|assumption|open|resolved|only|must|should|will)\b", re.I)


def is_sample_line(line: str) -> bool:
    """Sample rows and column lists the parser set aside: settled without a decision.
    A note or assumption caught among them still needs one."""
    text = str(line or "")
    return text.startswith(SAMPLE_TAG) and not _NOTE_WORDS.search(text[len(SAMPLE_TAG):])


def coverage(plan: Dict) -> Dict:
    """Which rules and leftover lines still stop the import.

    Rules on hops left for a later part are "deferred": they stay in the saved review
    and are imported with that part. Every other rule must be covered, and every line
    the parser could not place must be made into a rule or marked as not a rule.
    """
    rules = plan.get("rules") or []
    deferred = sorted({i for spec in plan.get("rule_sets") or []
                       if spec.get("import_now") is False
                       for i in spec.get("rule_indexes", [])})
    now = [i for i in range(len(rules)) if i not in set(deferred)]
    covered = [i for i in now if entry_covered(rules[i])]
    handled = plan.get("leftover_choices") or {}
    lines = plan.get("unparsed") or []
    open_lines = [n for n in range(len(lines))
                  if str(n) not in handled and not is_sample_line(lines[n])]
    return {"total": len(now), "covered": covered,
            "open": [i for i in now if i not in set(covered)],
            "deferred": deferred, "open_lines": open_lines,
            "complete": len(covered) == len(now) and not open_lines}


def apply_import_plan(plan: Dict, *, performed_by: Optional[str] = None,
                      selected_rules: Optional[List[int]] = None,
                      selected_objects: Optional[List[int]] = None,
                      team_id: Optional[int] = None,
                      require_complete: bool = False) -> Dict:
    """Write an approved plan to the repository. All of it, or none of it.

    require_complete (the review page): refuse unless coverage(plan) is complete, and
    roll back if any rule is still not written, so an FDD is never imported short.

    Everything happens inside one transaction. A partially imported FDD is the
    worst outcome available here: it looks like a successful import, so nobody
    goes looking for the missing half. save_rule() is called with
    manage_transaction=False because Snowflake does not nest transactions - an
    inner COMMIT would commit this outer one and every later write would
    auto-commit individually, which is precisely the partial state we are
    avoiding.

    selected_rules / selected_objects are indexes into plan["rules"] and
    plan["objects"]; None means all. The reviewer can therefore drop the items
    they are not happy with and import the rest.
    """
    from db import execute
    from repository import (add_object_columns, add_review_comment, add_rule_set_source,
                            adopt_abandoned_objects,
                            create_business_object, create_module, create_rule_set,
                            find_active_rule_set, find_module_by_name,
                            add_rule_notes, get_review_comments, link_rule_set_objects,
                            log_rule_changes, save_rule, update_rule)

    objects = plan.get("objects", [])
    rules = plan.get("rules", [])
    want_objects = range(len(objects)) if selected_objects is None else selected_objects
    want_rules = range(len(rules)) if selected_rules is None else selected_rules

    written = {"module_id": None, "objects": 0, "columns": 0, "rule_set_id": None,
               "rule_set_ids": [], "rule_sets": 0, "rules": 0, "rule_ids": [],
               "skipped": [], "skipped_rule_sets": [], "extra_sources": 0}

    if require_complete:
        gaps = coverage(plan)
        if not gaps["complete"]:
            raise ValueError(
                f"{len(gaps['open'])} rule(s) and {len(gaps['open_lines'])} unplaced "
                "line(s) still need a decision - every rule in the FDD must be imported.")

    execute("BEGIN")
    try:
        # Reuse a module of the same name rather than creating another. Importing the
        # Location FDD twice left two modules both called "Location", each holding a
        # copy of every hop, and the lists that group by module showed half of each.
        module_id = find_module_by_name(plan["module_name"], team_id=team_id)
        written["module_reused"] = module_id is not None
        if module_id is None:
            module_id = create_module(
                plan["module_name"],
                _module_code(plan["module_name"]),
                (plan.get("overview") or "")[:1000],
                team_id=team_id,
            )
        written["module_id"] = module_id
        written["team_id"] = team_id

        for index in want_objects:
            obj = objects[index]
            technical = obj["technical_object_name"]
            object_id = obj.get("object_id")
            if not object_id:
                object_id = create_business_object(
                    obj["object_name"], technical, obj.get("object_type", "TABLE"),
                    module_id=module_id, performed_by=performed_by,
                )
                written["objects"] += 1

            # Every reconciled column is declared, whatever its flag.
            # snowflake_only means the FDD omitted a column that exists - recording
            # it is better than leaving the spec short, and 13 objects in this
            # repository currently have no columns at all. type_mismatch keeps the
            # *pasted* type, because the FDD is the specification; the mismatch is
            # flagged for a human rather than silently reconciled to whatever
            # happens to be built today.
            payload = [
                {
                    "column_name": column["column_name"],
                    "data_type": column["pasted_type"] or column["live_type"],
                    "business_description": column.get("business_description", ""),
                    "column_origin": DEFAULT_COLUMN_ORIGIN,
                }
                for column in obj.get("columns", [])
            ]
            if payload:
                written["columns"] += add_object_columns(object_id, payload,
                                                        created_by=performed_by)

        source_technical, target_technical = _endpoints(plan, objects, want_objects)
        rule_sets = plan.get("rule_sets") or []
        if not rule_sets:
            rule_sets = [{
                "name": plan.get("rule_set_name") or f"{plan['module_name']} Transformation",
                "source_technical_object": source_technical,
                "target_technical_object": target_technical,
                "rule_indexes": list(range(len(rules))),
            }]

        # One rule set per source-to-target hop. The tabular dialect knows each
        # hop exactly, which is what gives the imported module real lineage -
        # a single rule set spanning the whole document would collapse the
        # pipeline into one edge and the Object lineage page would show nothing
        # useful.
        wanted_rules = set(want_rules)
        # FDD Note / Open / Resolved rows, by the hop they sit under; attached to the
        # first rule written on that hop.
        hop_notes = {id(spec): notes for spec, notes in fdd_notes_by_hop(plan, rule_sets)}
        for spec in rule_sets:
            if spec.get("import_now") is False:
                written.setdefault("hops_left_for_later", []).append(spec.get("name"))
                continue
            indexes = [i for i in spec.get("rule_indexes", []) if i in wanted_rules]
            writable = [i for i in indexes
                        if rules[i].get("proposal") and not rules[i].get("errors")]
            if not writable and spec.get("already_saved"):
                continue  # every rule on this hop is already in the module: nothing lost
            if not writable:
                # No rule set is created for a hop with nothing to put in it -
                # an empty rule set is already a finding in the health report.
                # The cost is that this hop contributes no lineage edge, so it is
                # recorded explicitly rather than just vanishing from the result.
                written["skipped_rule_sets"].append({
                    "name": spec.get("name"),
                    "source": spec.get("source_technical_object") or "",
                    "target": spec.get("target_technical_object") or "",
                    "rules": len(indexes),
                })
                for i in indexes:
                    entry = rules[i]
                    written["skipped"].append({
                        "rule_number": entry.get("rule_number"),
                        "rule_set": spec.get("name"),
                        "why": "; ".join(str(e) for e in entry.get("errors") or [])
                               or "nothing compiled",
                    })
                continue

            rs_source = spec.get("source_technical_object") or ""
            rs_target = spec.get("target_technical_object") or ""
            # Same hop already documented in this module: add to it. A second rule
            # set for identical endpoints draws the edge twice and splits the rules
            # between two places, which is how every Location hop ended up doubled.
            rule_set_id = find_active_rule_set(module_id, rs_target, rs_source)
            if rule_set_id is None:
                rule_set_id = create_rule_set(
                    spec.get("name") or f"{plan['module_name']} Transformation",
                    module_id=module_id,
                    source_technical_object=rs_source,
                    target_technical_object=rs_target,
                    description=(spec.get("text") or plan.get("overview") or "")[:1000],
                    performed_by=performed_by,
                )
            else:
                written.setdefault("reused_rule_sets", []).append(rule_set_id)
            written["rule_set_ids"].append(rule_set_id)
            if rs_source or rs_target:
                link_rule_set_objects(rule_set_id, rs_source, rs_target)
            # Every other object the text names is registered as a further input,
            # so the lineage graph shows all of a view's upstreams and not just one.
            for extra in spec.get("extra_sources") or []:
                if extra and extra != rs_source and extra != rs_target:
                    if add_rule_set_source(rule_set_id, extra):
                        written["extra_sources"] += 1

            for index in indexes:
                entry = rules[index]
                proposal = entry.get("proposal")
                if not proposal:
                    written["skipped"].append(
                        {"rule_number": entry.get("rule_number"),
                         "rule_set": spec.get("name"), "why": "nothing compiled"})
                    continue
                # A rule that failed validation is never written, even if the
                # caller asked for it. The review page disables those checkboxes,
                # but relying on the UI alone means any other caller - a script, a
                # future page - can put an invalid rule in the repository
                # silently. Enforced here so there is one place that decides.
                if entry.get("errors"):
                    written["skipped"].append(
                        {"rule_number": entry.get("rule_number"),
                         "rule_set": spec.get("name"),
                         "why": "; ".join(str(e) for e in entry["errors"])})
                    continue
                proposal = dict(proposal)
                proposal["rule_set_id"] = rule_set_id
                proposal["rule_number"] = entry.get("rule_number") or "TBD"
                prior = entry.get("prior") or {}
                choice = entry.get("prior_choice") if prior else "new"
                if choice == "skip":
                    continue
                if choice == "update":
                    rule_id = int(prior["rule_id"])
                    update_rule(rule_id, proposal, performed_by=performed_by,
                                manage_transaction=False)
                    log_rule_changes(rule_id, [(
                        "FDD_REIMPORT", prior.get("fdd_text") or None,
                        "Updated from a re-imported part of the FDD.")],
                        performed_by=performed_by)
                    written["rules_updated"] = written.get("rules_updated", 0) + 1
                else:
                    rule_id = save_rule(proposal, manage_transaction=False,
                                        performed_by=performed_by)
                    written["rules"] += 1
                # The sentence the rule came from, so a later part can tell a rule
                # already imported from one the FDD has changed since.
                log_rule_changes(rule_id, [("FDD_TEXT", prior.get("fdd_text") or None,
                                            entry.get("text") or "")]
                                 + ([("FDD_SOURCE", None, entry["origin"])]
                                    if entry.get("origin") else []),
                                 performed_by=performed_by)
                written["rule_ids"].append(rule_id)
                # What the next reader of this rule should know goes on the rule itself:
                # the rule's own "Note:" sentences, the FDD's Note / Open rows for this
                # hop, and what was accepted or fixed at import. The module's review
                # list is kept for the FDD's open questions only.
                notes = rule_notes_for(entry, hop_notes.pop(id(spec), []))
                if notes:
                    written["rule_notes"] = written.get("rule_notes", 0) + add_rule_notes(
                        rule_id, notes, performed_by=performed_by)

        written["already_imported"] = sum(
            1 for spec in rule_sets if spec.get("import_now") is not False
            for i in spec.get("rule_indexes", [])
            if rules[i].get("prior") and rules[i].get("prior_choice") == "skip")
        written["rule_sets"] = len(written["rule_set_ids"])
        written["rule_set_id"] = (written["rule_set_ids"][0]
                                  if written["rule_set_ids"] else None)

        # Sample rows the FDD carries for objects that are not in Snowflake.
        written["samples"] = _save_fdd_samples(objects, want_objects, performed_by)

        # Reused objects whose module was retired (re-import under a new module name)
        # move to this module, so it shows its objects and lineage.
        written["objects_adopted"] = adopt_abandoned_objects(
            [o.get("object_id") for o in objects if o.get("object_id")], module_id)

        # Re-importing the same FDD must not add the same comments again.
        try:
            existing_df = get_review_comments()
            existing = {str(t).strip() for t in existing_df.loc[
                existing_df["MODULE_ID"] == module_id, "COMMENT_TEXT"]} \
                if not existing_df.empty else set()
        except Exception:
            existing = set()
        for text in document_comments(plan):
            if text in existing:
                written["review_comments_skipped"] = written.get("review_comments_skipped", 0) + 1
                continue
            add_review_comment(module_id, text, raised_by=performed_by)
            existing.add(text)
            written["review_comments"] = written.get("review_comments", 0) + 1

        if require_complete and written["skipped"]:
            raise ValueError("Rule(s) " + ", ".join(
                str(x.get("rule_number") or "-") for x in written["skipped"])
                + " could not be written; nothing was imported.")
        execute("COMMIT")
    except Exception:
        execute("ROLLBACK")
        raise

    return written


# ------------------------------------------------------------------
# Importing an FDD in parts
# ------------------------------------------------------------------
# A long FDD is imported one section at a time into the same module. Three things make
# that safe: the chain continues from what is already saved, a rule already saved is
# not written twice, and a rule whose FDD text changed can update the saved one.

SAME, CHANGED, SAVED_NO_TEXT = "same", "changed", "saved"


def _norm_text(text) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", str(text or "").upper()).strip()


def _hop_key(source, target) -> Tuple[str, str]:
    return (str(source or "").strip().upper(), str(target or "").strip().upper())


def _val(value) -> str:
    """A cell as text; None and pandas NaN (float, not equal to itself) become ""."""
    if value is None or (isinstance(value, float) and value != value):
        return ""
    return str(value).strip()


def saved_hops(rows: List[Dict]) -> Dict[Tuple[str, str], Dict]:
    """{(source, target): {rule_set_id, name, rules: [...]}} from list_module_rules rows."""
    hops: Dict[Tuple[str, str], Dict] = {}
    for row in rows or []:
        key = _hop_key(_val(row.get("SOURCE_TECHNICAL_OBJECT")),
                       _val(row.get("TARGET_TECHNICAL_OBJECT")))
        hop = hops.setdefault(key, {"rule_set_id": row.get("RULE_SET_ID"),
                                    "name": _val(row.get("RULE_SET_NAME")), "rules": []})
        rule_id = _val(row.get("RULE_ID"))
        if rule_id:
            hop["rules"].append({"rule_id": int(float(rule_id)),
                                 "rule_number": _val(row.get("RULE_NUMBER")),
                                 "rule_name": _val(row.get("RULE_NAME")),
                                 "fdd_text": _val(row.get("FDD_TEXT"))})
    return hops


def last_saved_target(hops: Dict[Tuple[str, str], Dict]) -> str:
    """The end of the saved chain: a target nothing saved reads from, furthest along."""
    from lineage_diagram import LAYER_ORDER, object_layer  # lineage_diagram imports us
    sources = {s for s, _ in hops}
    ends = [t for _, t in hops if t and t not in sources]
    if not ends:
        return ""
    return max(ends, key=lambda t: LAYER_ORDER.index(object_layer(t))
               if object_layer(t) in LAYER_ORDER else -1)


def continue_chain(plan: Dict, previous_target: str) -> bool:
    """A part that starts mid-chain has no source on its first hop: take the end of
    what is already saved, so Stage -> Current follows Root view -> Stage."""
    specs = plan.get("rule_sets") or []
    if not previous_target or not specs or specs[0].get("source_technical_object"):
        return False
    first = specs[0]
    if _hop_key("", first.get("target_technical_object"))[1] == previous_target.upper():
        return False
    first["source_technical_object"] = previous_target
    first["continued_from_saved"] = True
    for index in first.get("rule_indexes", []):
        entry = plan["rules"][index]
        entry["source"] = previous_target
        entry["sources"] = [previous_target] + [x for x in entry.get("sources") or []
                                                if x and x != previous_target]
    return True


def mark_already_imported(plan: Dict, hops: Dict[Tuple[str, str], Dict]) -> Dict[str, int]:
    """Mark each planned rule against what the module already holds.

    Matched within the same hop: by the FDD sentence (normalised), else by rule number.
    "same" is skipped at import; "changed" (same number, different sentence) updates the
    saved rule by default; "saved" is a number match on a rule imported before the FDD
    sentence was recorded - skipped by default, since there is nothing to compare.
    """
    counts = {SAME: 0, CHANGED: 0, SAVED_NO_TEXT: 0, "hops_saved": 0}
    for spec in plan.get("rule_sets") or []:
        hop = hops.get(_hop_key(spec.get("source_technical_object"),
                                spec.get("target_technical_object")))
        spec["already_saved"] = bool(hop)
        if not hop:
            continue
        counts["hops_saved"] += 1
        by_text = {_norm_text(r["fdd_text"]): r for r in hop["rules"] if r["fdd_text"]}
        by_number = {r["rule_number"]: r for r in hop["rules"] if r["rule_number"]}
        for index in spec.get("rule_indexes", []):
            entry = plan["rules"][index]
            text = _norm_text(entry.get("text"))
            match, state = by_text.get(text), SAME
            if match is None:
                match = by_number.get(str(entry.get("rule_number") or "").strip())
                if match is None:
                    continue
                state = CHANGED if match["fdd_text"] else SAVED_NO_TEXT
            entry["prior"] = {"rule_id": match["rule_id"], "state": state,
                              "rule_name": match["rule_name"],
                              "fdd_text": match["fdd_text"]}
            entry["prior_choice"] = "update" if state == CHANGED else "skip"
            counts[state] += 1
    return counts


def _save_fdd_samples(objects: List[Dict], wanted, performed_by: Optional[str]) -> int:
    """Save each selected object's FDD sample rows (sample_data.py). Skipped entirely
    when the feature is off; an identical sample already saved is not saved again."""
    try:
        import sample_data as sd
    except Exception:
        return 0
    if not sd.SAMPLE_DATA_ENABLED:
        return 0
    saved = 0
    for index in wanted:
        obj = objects[index]
        held = obj.get("fdd_sample")
        if not held or (obj.get("exists_in_snowflake") and not obj.get("keep_fdd_sample")):
            continue
        rows = sd.rows_from_fdd(held["columns"], held["rows"])
        if sd.save_sample(obj["technical_object_name"], held["columns"], rows,
                          source="FDD_IMPORT", note="Sample rows from the imported FDD.",
                          object_id=obj.get("object_id"), performed_by=performed_by,
                          skip_if_same=True,
                          cell_origin={i: {c: sd.ORIGIN_MANUAL for c in held["columns"]}
                                       for i in range(len(rows))}):
            saved += 1
    return saved


def document_comments(plan: Dict) -> List[str]:
    """Review-comment texts for the FDD's own Open / Resolved / Note rows."""
    out: List[str] = []
    for comment in plan.get("review_comments") or []:
        near = comment.get("near_object") or ""
        prefix = f"(near {_leaf_name(near)}) " if near else ""
        out.append(f"[{comment.get('status', 'Open')}] {prefix}{comment['text']}")
    # FDD lines the reviewer marked as not a rule: kept visible, with the reason.
    lines = plan.get("unparsed") or []
    for n, choice in sorted((plan.get("leftover_choices") or {}).items(),
                            key=lambda kv: int(kv[0])):
        if choice.get("how") == "not_a_rule" and int(n) < len(lines):
            out.append(f"[Note] FDD line not imported as a rule: \"{lines[int(n)][:300]}\" "
                       f"(reason: {choice.get('reason') or 'not given'})")
    return list(dict.fromkeys(out))


def resolution_comments(entry: Dict, rule_id, rule_set_name: str = "") -> List[str]:
    """Review comments for what the reviewer accepted anyway or kept as a note.

    Accepting a problem on the import screen must not make it disappear: the rule is
    saved as a draft, and the problem and the reason go on the module's review list,
    so whoever approves the rule sees them.
    """
    where = f"Rule {entry.get('rule_number') or '?'} (id {rule_id}"
    where += f", {rule_set_name})" if rule_set_name else ")"
    # One comment per rule and kind, not one per problem: a 25-rule import used to put
    # 54 near-identical lines on the Requirements page.
    groups: Dict[str, List[str]] = {"accepted": [], "noted": [], "auto-fixed": []}
    for issue_id, how in (entry.get("resolutions") or {}).items():
        kind = how.get("how")
        if kind not in groups:
            continue
        title = how.get("title") or issue_id
        reason = str(how.get("reason") or "").strip()
        item = _full(how, title)
        groups[kind].append(item + (f" (reason: {reason})" if reason else ""))
    out: List[str] = []
    for kind, head in (("accepted", f"[Open] {where}: accepted at import despite: "),
                       ("noted", f"[Open] {where}: FDD note - "),
                       ("auto-fixed", f"[Info] {where}: fixed automatically at import - ")):
        out.extend(_pack(head, groups[kind]))
    return out


def _pack(head: str, items: List[str], limit: int = 3990) -> List[str]:
    """head + items joined by "; ", split over as many comments as the column needs,
    so no item is cut off mid-sentence."""
    out: List[str] = []
    current = ""
    for item in items:
        item = item[:limit - len(head)]
        joined = f"{current}; {item}" if current else item
        if current and len(head) + len(joined) > limit:
            out.append(head + current)
            current = item
        else:
            current = joined
    if current:
        out.append(head + current)
    return out


# ------------------------------------------------------------------
# Rule notes
# ------------------------------------------------------------------
# "Note: we re-aggregate ... as they were previously aggregated at UPI level" is not a
# rule, but whoever reads the rule later needs it. Sentences that start like this are
# lifted out as notes on the rule (the text of the rule keeps them too).
_NOTE_SENTENCE = re.compile(
    r"(?:^|(?<=[\s.;]))(?P<label>Note|NB|N\.B\.|Open(?:\s+(?:item|question|point))?|"
    r"Question|Assumption|TBD|To be confirmed|Pending|Resolved)\s*[:\-\u2013]\s*"
    r"(?P<body>.+?)(?=(?:\s(?:Note|NB|Open|Question|Assumption|TBD|Resolved)\s*[:\-\u2013])|"
    r"(?:\s[0-9]{1,3}[A-Z]?(?:\.\)|[.)])\s)|$)",
    re.IGNORECASE | re.S)

_NOTE_TYPE_OF = {"open": "OPEN", "question": "OPEN", "tbd": "OPEN", "to be confirmed": "OPEN",
                 "pending": "OPEN", "resolved": "RESOLVED"}


def extract_notes(text: str) -> List[Tuple[str, str]]:
    """[(note_type, text)] for the Note / Open / Question / Assumption sentences in a
    rule's text. Works on typed rules and imported ones alike."""
    out: List[Tuple[str, str]] = []
    for match in _NOTE_SENTENCE.finditer(str(text or "")):
        label = re.sub(r"\s+", " ", match["label"].lower().rstrip("."))
        body = re.sub(r"\s+", " ", match["body"]).strip(' "')
        if len(body) < 4:
            continue
        kind = _NOTE_TYPE_OF.get(label.split(" ")[0] if label.startswith("open") else label,
                                 "NOTE")
        if label == "assumption":
            body = "Assumption: " + body
        out.append((kind, body))
    return out


def fdd_notes_by_hop(plan: Dict, rule_sets: List[Dict]) -> List[Tuple[Dict, List[tuple]]]:
    """The FDD's own Note / Open / Resolved rows, matched to the hop they sit under
    (the object declared just before them is the hop's target, else its source)."""
    out: Dict[int, List[tuple]] = {}
    specs = {id(s): s for s in rule_sets}
    for comment in plan.get("review_comments") or []:
        near = str(comment.get("near_object") or "").upper()
        if not near:
            continue
        spec = (next((s for s in rule_sets
                      if str(s.get("target_technical_object") or "").upper() == near), None)
                or next((s for s in rule_sets
                         if str(s.get("source_technical_object") or "").upper() == near), None))
        if spec is None:
            continue
        kind = {"Open": "OPEN", "Resolved": "RESOLVED"}.get(comment.get("status"), "NOTE")
        out.setdefault(id(spec), []).append((kind, comment["text"], "FDD_IMPORT"))
    return [(specs[k], v) for k, v in out.items()]


def _full(how: Dict, title: str) -> str:
    """The problem in full: its detail when it has one, else the untruncated title."""
    detail = str(how.get("detail") or "").strip()
    return detail or str(title)


def rule_notes_for(entry: Dict, fdd_rows: List[tuple] = ()) -> List[tuple]:
    """Every note one imported rule should carry: (note_type, text, source)."""
    notes = [(kind, text, "FDD_IMPORT") for kind, text in extract_notes(entry.get("text"))]
    notes += list(fdd_rows or [])
    # Saved as written: say so on the rule, with what the text mentions (as_written.py).
    notes += [tuple(n) for n in entry.get("as_written_notes") or []]
    accepted, noted, fixed = [], [], []
    # Saved as written by a person: the checker's doubts were about the proposal that
    # was thrown away, so they would only mislead the next reader of the sentence.
    resolutions = {} if entry.get("as_written") == "chosen" else (entry.get("resolutions") or {})
    for issue_id, how in resolutions.items():
        title = how.get("title") or issue_id
        reason = str(how.get("reason") or "").strip()
        tail = f" (reason: {reason})" if reason else ""
        # The full text, not the one-line title: the title is cut at 12 words ("..."),
        # which left the Requirements page with half a question.
        {"accepted": accepted, "noted": noted, "auto-fixed": fixed}.get(
            how.get("how"), []).append(_full(how, title) + tail)
    # In full, several items may not fit one 4000-character note: split, never cut.
    notes += [("OPEN", t, "FDD_IMPORT") for t in
              _pack("Accepted at import although the checker flagged: ", accepted, 4000)]
    notes += [("NOTE", n, "FDD_IMPORT") for n in noted]
    notes += [("INFO", t, "FDD_IMPORT") for t in
              _pack("Fixed automatically at import: ", fixed, 4000)]
    return notes


_IMPORT_NOTE = re.compile(
    r"^\[(?P<status>Open|Info|Note|Resolved)\]\s+Rule\s+(?P<rule>\S+)\s+"
    r"\(id\s+(?P<id>\d+)(?:,\s*(?P<set>[^)]*))?\):\s*(?P<body>.*)$", re.S)


def classify_comment(text: str) -> Dict[str, str]:
    """What a review comment is, for the Requirements page.

    kind: "import" (written by the importer about one rule: accepted, FDD note,
    auto-fixed), "fdd" (an Open/Note/Resolved row of the FDD itself) or "typed"
    (written by a person). Import notes carry the rule, its id and a short body
    without the bracketed prefix.
    """
    text = str(text or "").strip()
    match = _IMPORT_NOTE.match(text)
    if match:
        body = match["body"]
        what = ("fixed automatically" if body.startswith("fixed automatically")
                else "FDD note" if body.startswith("FDD note")
                else "accepted at import")
        body = re.sub(r"^(fixed automatically at import - |FDD note - |"
                      r"accepted at import despite:?\s*)", "", body)
        return {"kind": "import", "what": what, "rule": match["rule"],
                "rule_id": match["id"], "rule_set": (match["set"] or "").strip(),
                "body": body.strip(' "')}
    doc = re.match(r"^\[(Open|Note|Resolved)\]\s*(.*)$", text, re.S)
    if doc:
        return {"kind": "fdd", "what": doc.group(1), "body": doc.group(2)}
    return {"kind": "typed", "what": "", "body": text}


def _module_code(module_name: str) -> str:
    """ITEMLOCBOM from 'Item Loc BOM'."""
    letters = re.sub(r"[^A-Za-z0-9]", "", module_name or "")
    return (letters[:20] or "MODULE").upper()


def _endpoints(plan: Dict, objects: List[Dict], want) -> Tuple[str, str]:
    """Guess the rule set's source and target from the lineage section.

    Lineage lines read root -> ... -> output, so the first and last qualified
    names mentioned are the endpoints. Falls back to the first and last declared
    object. This is a guess and the review UI presents it as editable - getting
    it wrong leaves a rule set whose lineage edge points the wrong way, which is
    exactly the defect behind the 1 rule set currently holding no target.
    """
    names: List[str] = []
    for line in plan.get("lineage_lines", []):
        names.extend(TECHNICAL_NAME.findall(str(line).upper()))
    if len(names) >= 2:
        return names[0], names[-1]

    chosen = [objects[i]["technical_object_name"] for i in want] if objects else []
    if len(chosen) >= 2:
        return chosen[0], chosen[-1]
    if len(chosen) == 1:
        return "", chosen[0]
    return "", ""
