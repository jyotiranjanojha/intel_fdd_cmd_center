"""Draft answer-key extractor for the FDD importer evaluation.

Deliberately independent of fdd_import / xlsx_import: it reads the workbooks with
openpyxl directly and uses its own, simpler reading of the layout, so the answer key
is not just the importer agreeing with itself. Its output is a *draft* - every file
is reviewed (tests/gold/review.py) before it is treated as the answer key.

Per FDD it records:
  sheets    - which design sheets were read, and which were skipped and why
  blocks    - each Transformation block: the object it builds (label + name), the
              raw text, and the text split into single statements
  extras    - requirement-like rows outside Transformation blocks (Notes, Opens,
              DQ, free-standing sentences, scenario and column-mapping tables)

Usage (from RULE_REPOSITORY_APP/):
  python3 tests/gold/gold_extract.py            # all FDDs under MASTER DATA
  python3 tests/gold/gold_extract.py ITEM       # only paths containing ITEM
"""

import glob
import json
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

import openpyxl

ROOT = "/workspace/MASTER DATA"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "draft")

# Object names: DB.SCHEMA.OBJECT, SCHEMA.OBJECT for the PDH schemas, or a bare
# T_/V_ object name.
FULL = re.compile(r"\b[A-Z][A-Z0-9_]*\.[A-Z][A-Z0-9_]*\.[A-Z][A-Z0-9_]+\b")
PART = re.compile(r"(?<![A-Z0-9_.])[A-Z][A-Z0-9_]*(?:ANALYSIS|XFRM|SRC|BASE|ANALYSIS_BASE)"
                  r"\.[A-Z][A-Z0-9_]+\b")
BARE = re.compile(r"^(?:[TV]_[A-Z0-9_]+|BY_[A-Z0-9_]+|UI_[A-Z0-9_]+)$")

TRANSFORMATION = "transformation"
# Rows that carry information about the block above, never an object.
ATTRIBUTE = ("key columns", "key column", "note", "notes", "opens", "open", "observations",
             "initial load", "incremental load", "dq", "data pull logic", "logic",
             "new column", "add column", "delete column", "resolved", "comments",
             "for the subsequent load", "insert/update/delete")
HEADER = ("definition", "inputs", "input view name", "outputs", "output table name",
          "output view name", "timing", "consumer", "final view name")
REQUIREMENT_HINT = re.compile(
    r"(?i)\b(need to|should|must|we have to|only (?:for|when|if)|stop the load|"
    r"set .* to|derive|join|filter|exclude|include|populate|if .* then)\b")


def norm(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).replace("\r", "").strip()


def object_name(cell: str) -> Optional[str]:
    up = cell.upper().strip()
    m = FULL.search(up)
    if m:
        return m.group(0)
    m = PART.search(up)
    if m:
        return m.group(0)
    if BARE.match(up):
        return up
    return None


def is_label(cell: str) -> bool:
    return bool(cell) and len(cell) <= 45 and "\n" not in cell


def split_statements(text: str) -> List[str]:
    """One statement per numbered point, sub-point, dash bullet or Step.

    Kept simple on purpose; review.py is where a person or a careful pass fixes the
    edge cases. Unnumbered prose stays one statement.
    """
    lines = [ln.rstrip() for ln in text.replace("\r", "").split("\n")]
    starts = re.compile(r"^\s*(?:\d+[A-Za-z]?[.)]|\d+\.\d+[.)]?|[A-Z][.)]\s|-\s*\S|•|>\s|"
                        r"step\s*\d+|\d+\.\))", re.I)
    out: List[str] = []
    cur: List[str] = []
    for ln in lines:
        if not ln.strip():
            continue
        if starts.match(ln) and cur:
            out.append(" ".join(cur).strip())
            cur = [ln.strip()]
        else:
            cur.append(ln.strip())
    if cur:
        out.append(" ".join(cur).strip())
    return [s for s in out if s]


def sheet_rows(ws) -> List[List[str]]:
    return [[norm(c) for c in row] for row in ws.iter_rows(values_only=True)]


LABEL_COL = 0  # set per sheet by extract_sheet()


def label_of(row: List[str]) -> Tuple[int, str]:
    """(column, text) of the label cell, or (-1, "") when the label column is empty.

    The label column is the one the sheet writes "Transformation" in. A row whose
    label cell is empty is either a continuation of the text above or an unlabelled
    object row (the MEMORIES table under a merged "Time Travel" cell).
    """
    if LABEL_COL < len(row) and row[LABEL_COL]:
        return LABEL_COL, row[LABEL_COL]
    return -1, ""


def detect_label_col(rows: List[List[str]]) -> int:
    counts: Dict[int, int] = {}
    for r in rows:
        for j, c in enumerate(r[:4]):
            if c.lower() == TRANSFORMATION:
                counts[j] = counts.get(j, 0) + 1
    return max(counts, key=counts.get) if counts else 0


def choose_sheets(wb) -> Tuple[List[str], List[Dict]]:
    visible = [ws.title for ws in wb.worksheets if ws.sheet_state == "visible"]
    hidden = [ws.title for ws in wb.worksheets if ws.sheet_state != "visible"]
    skipped = [{"sheet": s, "why": "hidden"} for s in hidden]
    design = []
    for name in visible:
        rows = sheet_rows(wb[name])
        if any(c.lower() == TRANSFORMATION for r in rows for c in r[:4]):
            design.append(name)
    # SheetN copies duplicate a named design sheet; versioned sheets keep the latest.
    keep = []
    for name in design:
        if re.fullmatch(r"(?i)sheet\d+", name) and len(design) > 1:
            skipped.append({"sheet": name, "why": "SheetN duplicate of a named design sheet"})
            continue
        keep.append(name)
    base = {}
    for name in keep:
        m = re.match(r"(?i)^(.*?)[_ -]?v(\d+)$", name)
        key, ver = (m.group(1).lower(), int(m.group(2))) if m else (name.lower(), 0)
        base.setdefault(key, []).append((ver, name))
    chosen = []
    for key, items in base.items():
        items.sort()
        chosen.append(items[-1][1])
        for ver, name in items[:-1]:
            skipped.append({"sheet": name, "why": f"older version of {items[-1][1]}"})
    order = [n for n in keep if n in chosen]
    return order, skipped


def extract_sheet(rows: List[List[str]]) -> Tuple[List[Dict], List[Dict]]:
    global LABEL_COL
    LABEL_COL = detect_label_col(rows)
    blocks: List[Dict] = []
    extras: List[Dict] = []
    open_block: Optional[Dict] = None
    in_logic = False
    i = 0
    while i < len(rows):
        row = rows[i]
        col, lab = label_of(row)
        cells = [(j, c) for j, c in enumerate(row) if c]
        low = lab.lower().rstrip(":").strip()
        if low == "logic":
            in_logic = True
        if low == TRANSFORMATION:
            if open_block and not open_block.get("target"):
                open_block["target"] = None
                blocks.append(open_block)
            text = "\n".join(c for j, c in cells if j > col)
            open_block = {"row": i + 1, "text": text, "target": None, "target_label": None,
                          "context": []}
            i += 1
            # Continuation rows: no label in the first columns, long text after.
            while i < len(rows):
                c2, l2 = label_of(rows[i])
                nxt = [(j, c) for j, c in enumerate(rows[i]) if c]
                if not nxt:
                    i += 1
                    continue
                # Long prose is rule text even when it names objects; an object row
                # is a short cell that is nothing but a name.
                is_object_row = any(object_name(c) and len(c) < 120 for _, c in nxt[:2])
                if c2 == -1 and not is_object_row and any(len(c) > 25 for _, c in nxt):
                    open_block["text"] += "\n" + "\n".join(c for _, c in nxt)
                    i += 1
                    continue
                break
            continue
        if open_block is not None and cells:
            name = None
            for j, c in cells:
                name = object_name(c)
                if name:
                    break
            lowlab = lab.lower().rstrip(":").strip() if col >= 0 else ""
            if lowlab in ATTRIBUTE or lowlab.startswith(("note", "open")):
                open_block["context"].append({"label": lab, "text": " ".join(c for j, c in cells if j > col)})
                i += 1
                continue
            # An object row: a short label (or none) and a name in the next cell. Only
            # the first two non-empty cells count, so a sample-data row that happens
            # to contain a dotted value is not mistaken for one.
            first = [c for _, c in cells[:2]]
            name = next((object_name(c) for c in first if object_name(c) and len(c) < 120), None)
            if name and (col == -1 or is_label(lab)) and len(cells) <= 3:
                open_block["target"] = name
                open_block["target_label"] = lab if col >= 0 and not object_name(lab) else ""
                open_block["statements"] = split_statements(open_block["text"])
                blocks.append(open_block)
                open_block = None
                i += 1
                continue
            if col >= 0 and is_label(lab) and lowlab and not name and len(cells) >= 2 \
                    and lowlab not in HEADER and not cells[0][1].isupper():
                # An object label whose name cell is empty (e.g. "Common_Analysis View").
                open_block["target"] = None
                open_block["target_label"] = lab
                open_block["missing_name"] = True
        # Requirement-like rows outside a Transformation block.
        if in_logic and open_block is None and cells:
            lowlab = lab.lower().rstrip(":").strip()
            text = " ".join(c for _, c in cells)
            if (lowlab.startswith(("note", "open", "observations", "dq", "data pull")) and len(text) > 40) \
                    or (col == -1 and len(text) > 40 and REQUIREMENT_HINT.search(text)):
                extras.append({"row": i + 1, "kind": lowlab or "free text", "text": text[:2000]})
            if any(c.lower() == "scenario" for _, c in cells):
                extras.append({"row": i + 1, "kind": "scenario table", "text": text[:500]})
            if any(c.lower() in ("target column", "target columns") for _, c in cells):
                # Each following row is TARGET_COLUMN ... mapping text; keep the rows
                # whose mapping is logic, not a plain "X from Source".
                k = i + 1
                while k < len(rows) and any(rows[k]):
                    mc = [c for c in rows[k] if c]
                    if len(mc) >= 2 and re.fullmatch(r"[A-Z][A-Z0-9_]+", mc[0]):
                        mapping = " ".join(mc[1:])
                        plain = re.fullmatch(r"(?i)[A-Z0-9_]+\s+from\s+source|sys column.*|[A-Z0-9_]+",
                                             mapping.strip())
                        if not plain:
                            extras.append({"row": k + 1, "kind": "column mapping",
                                           "column": mc[0], "text": mapping[:2000]})
                    k += 1
        elif not in_logic and cells:
            text = " ".join(c for _, c in cells)
            if col == -1 and len(text) > 40 and REQUIREMENT_HINT.search(text) and not object_name(text):
                extras.append({"row": i + 1, "kind": "header-area requirement", "text": text[:2000]})
        i += 1
    if open_block is not None:
        open_block["statements"] = split_statements(open_block["text"])
        blocks.append(open_block)
    for b in blocks:
        b.setdefault("statements", split_statements(b["text"]))
    return blocks, extras


def extract(path: str) -> Dict:
    wb = openpyxl.load_workbook(path, read_only=False, data_only=True)
    chosen, skipped = choose_sheets(wb)
    out = {"file": os.path.relpath(path, ROOT), "sheets": chosen, "skipped": skipped,
           "by_sheet": {}}
    for name in chosen:
        blocks, extras = extract_sheet(sheet_rows(wb[name]))
        out["by_sheet"][name] = {"blocks": blocks, "extras": extras}
    return out


def slug(rel: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", os.path.splitext(rel)[0]).strip("_")


def main(argv: List[str]) -> None:
    os.makedirs(OUT, exist_ok=True)
    pattern = argv[1] if len(argv) > 1 else ""
    for path in sorted(glob.glob(os.path.join(ROOT, "**", "*.xlsx"), recursive=True)):
        rel = os.path.relpath(path, ROOT)
        if pattern and pattern.lower() not in rel.lower():
            continue
        data = extract(path)
        nb = sum(len(v["blocks"]) for v in data["by_sheet"].values())
        ns = sum(len(b["statements"]) for v in data["by_sheet"].values() for b in v["blocks"])
        nx = sum(len(v["extras"]) for v in data["by_sheet"].values())
        nt = sum(1 for v in data["by_sheet"].values() for b in v["blocks"] if not b["target"])
        with open(os.path.join(OUT, slug(rel) + ".json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
        print(f"{rel[:70]:70} sheets={len(data['sheets'])} blocks={nb:3} "
              f"statements={ns:3} extras={nx:3} no_target={nt}")


if __name__ == "__main__":
    main(sys.argv)
