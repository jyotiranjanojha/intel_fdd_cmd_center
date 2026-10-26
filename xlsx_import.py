"""Read an FDD workbook (.xlsx) into the text Import FDD already understands.

Same approach as the pdh-engineering-studio app: pandas.ExcelFile / read_excel, whose
openpyxl engine ships with the Streamlit container runtime (no external access needed).
If pandas cannot read a file, a small reader built on zipfile + xml takes over - an
.xlsx is a zip of XML parts - so a workbook openpyxl rejects still imports.

Each sheet becomes Label<TAB>Value lines, exactly what copying the cells out of Excel
gives, with one improvement: a cell holding line breaks is quoted the way Excel quotes
it, so the break survives. Pasting loses those breaks, which is what ran "1.)" and
"2.)" rules together.
"""

import datetime as _dt
import io
import re
import zipfile
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

MAX_BYTES = 20 * 1024 * 1024
_SKIP_BY_DEFAULT = re.compile(r"cover|revision|history|change\s*log|toc|contents|index",
                              re.IGNORECASE)


class WorkbookError(Exception):
    """A workbook that cannot be read, with a message fit to show the user."""


# ------------------------------------------------------------------
# Reading
# ------------------------------------------------------------------
def _cell_text(value) -> str:
    """A cell as the text Excel shows: no 4000.0, dates as YYYY-MM-DD."""
    if value is None:
        return ""
    try:
        import math
        if isinstance(value, float) and math.isnan(value):
            return ""
    except Exception:
        pass
    if isinstance(value, (_dt.datetime,)):
        return value.strftime("%Y-%m-%d") if (value.hour, value.minute, value.second) == (0, 0, 0) \
            else value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, _dt.date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value)
    if text.lower() in ("nan", "nat", "none"):
        return ""
    if re.fullmatch(r"-?\d+\.0", text):
        return text[:-2]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _read_with_pandas(data: bytes) -> Dict[str, List[List[str]]]:
    import pandas as pd
    book = pd.ExcelFile(io.BytesIO(data))
    sheets: Dict[str, List[List[str]]] = {}
    hidden = _hidden_sheets(data)
    for name in book.sheet_names:
        if name in hidden:
            continue
        # dtype=str keeps IDs such as 0000008268 as typed; dates arrive as datetime
        # only when the cell really is a date, which _cell_text formats.
        frame = pd.read_excel(book, sheet_name=name, header=None, dtype=object)
        sheets[name] = [[_cell_text(v) for v in row] for row in frame.itertuples(index=False)]
    return sheets


def _hidden_sheets(data: bytes) -> set:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            root = ET.fromstring(z.read("xl/workbook.xml"))
    except Exception:
        return set()
    ns = {"m": root.tag.split("}")[0].strip("{")}
    return {s.get("name") for s in root.findall(".//m:sheets/m:sheet", ns)
            if s.get("state") in ("hidden", "veryHidden")}


_REF = re.compile(r"([A-Z]+)(\d+)")


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _read_with_xml(data: bytes) -> Dict[str, List[List[str]]]:
    """Minimal .xlsx reader: shared/inline strings, numbers, booleans, date formats."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        book = ET.fromstring(z.read("xl/workbook.xml"))
        ns_main = book.tag.split("}")[0].strip("{")
        ns = {"m": ns_main,
              "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target") for r in rels}
        shared: List[str] = []
        if "xl/sharedStrings.xml" in names:
            sroot = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in sroot.findall("m:si", ns):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{ns_main}}}t")))
        date_styles = _date_styles(z, names, ns)
        sheets: Dict[str, List[List[str]]] = {}
        for sheet in book.findall(".//m:sheets/m:sheet", ns):
            if sheet.get("state") in ("hidden", "veryHidden"):
                continue
            target = targets.get(sheet.get(f"{{{ns['r']}}}id"), "")
            path = target.lstrip("/") if target.startswith("/") else "xl/" + target
            if path not in names:
                continue
            root = ET.fromstring(z.read(path))
            grid: Dict[Tuple[int, int], str] = {}
            for c in root.iter(f"{{{ns_main}}}c"):
                match = _REF.match(c.get("r") or "")
                if not match:
                    continue
                col, row = _col_index(match.group(1)), int(match.group(2)) - 1
                kind, value = c.get("t"), c.find("m:v", ns)
                if kind == "s" and value is not None:
                    text = shared[int(value.text)]
                elif kind == "inlineStr":
                    text = "".join(t.text or "" for t in c.iter(f"{{{ns_main}}}t"))
                elif kind == "b" and value is not None:
                    text = "TRUE" if value.text == "1" else "FALSE"
                elif value is not None and value.text is not None:
                    text = value.text
                    if c.get("s") and int(c.get("s")) in date_styles:
                        try:
                            day = _dt.datetime(1899, 12, 30) + _dt.timedelta(days=float(text))
                            text = _cell_text(day)
                        except ValueError:
                            pass
                    else:
                        text = _cell_text(float(text)) if re.fullmatch(r"-?\d+(\.\d+)?(E-?\d+)?", text) \
                            else text
                else:
                    text = ""
                grid[(row, col)] = text.replace("\r\n", "\n")
            if not grid:
                sheets[sheet.get("name")] = []
                continue
            rows = max(r for r, _ in grid) + 1
            cols = max(c for _, c in grid) + 1
            sheets[sheet.get("name")] = [[grid.get((r, c), "") for c in range(cols)]
                                         for r in range(rows)]
    return sheets


_BUILTIN_DATE_FORMATS = set(range(14, 23)) | {45, 46, 47}


def _date_styles(z, names, ns) -> set:
    if "xl/styles.xml" not in names:
        return set()
    root = ET.fromstring(z.read("xl/styles.xml"))
    custom = {int(f.get("numFmtId")) for f in root.findall(".//m:numFmts/m:numFmt", ns)
              if re.search(r"[dmy]", re.sub(r'"[^"]*"|\[[^\]]*\]', "", f.get("formatCode", "")),
                           re.IGNORECASE)}
    xfs = root.findall(".//m:cellXfs/m:xf", ns)
    return {i for i, xf in enumerate(xfs)
            if int(xf.get("numFmtId", 0)) in (_BUILTIN_DATE_FORMATS | custom)}


def read_workbook(data: bytes) -> Tuple[Dict[str, List[List[str]]], str]:
    """({sheet: rows of cell text}, engine used). Raises WorkbookError with a message."""
    if not data:
        raise WorkbookError("The file is empty.")
    if len(data) > MAX_BYTES:
        raise WorkbookError(f"The file is larger than {MAX_BYTES // (1024 * 1024)} MB.")
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise WorkbookError("This workbook is password-protected (or an old .xls file). "
                            "Remove the password or save it as .xlsx, then upload again.")
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise WorkbookError("This is not an .xlsx file. Save it as Excel Workbook (.xlsx).")
    try:
        return _read_with_pandas(data), "pandas"
    except Exception as first:
        try:
            return _read_with_xml(data), "built-in reader"
        except Exception as second:
            raise WorkbookError(f"The workbook could not be read ({first}; {second}). "
                                "Copy the cells from Excel and paste them instead.")


# ------------------------------------------------------------------
# Sheets to text
# ------------------------------------------------------------------
def default_sheets(sheets: Dict[str, List[List[str]]]) -> List[str]:
    """Sheets ticked by default: everything with content except cover/history sheets."""
    return [name for name, rows in sheets.items()
            if any(any(c.strip() for c in r) for r in rows) and not _SKIP_BY_DEFAULT.search(name)]


def _trim(rows: List[List[str]]) -> List[Tuple[int, List[str]]]:
    """Drop empty rows and the empty columns on the left and right; keep row numbers."""
    kept = [(i + 1, r) for i, r in enumerate(rows) if any(str(c).strip() for c in r)]
    if not kept:
        return []
    used = [j for _, r in kept for j, c in enumerate(r) if str(c).strip()]
    first, last = min(used), max(used)
    return [(n, [str(c) for c in r[first:last + 1]]) for n, r in kept]


def _quote(cell: str) -> str:
    """Excel's clipboard format: a cell with a line break is quoted, quotes doubled."""
    cell = cell.strip()
    if "\n" in cell or "\t" in cell:
        return '"' + cell.replace('"', '""').replace("\t", " ") + '"'
    return cell


def sheets_to_text(sheets: Dict[str, List[List[str]]], chosen: List[str]
                   ) -> Tuple[str, List[Tuple[str, int]]]:
    """(text, line_map). line_map[i] = (sheet, excel row) of text line i, so a rule
    can be traced back to where it sits in the workbook."""
    lines: List[str] = []
    line_map: List[Tuple[str, int]] = []
    for name in chosen:
        rows = _trim(sheets.get(name) or [])
        if not rows:
            continue
        if lines:
            lines.append("")
            line_map.append((name, 0))
        for number, cells in rows:
            while cells and not cells[-1].strip():
                cells = cells[:-1]
            text = "\t".join(_quote(c) for c in cells)
            for k, part in enumerate(text.split("\n")):
                lines.append(part)
                line_map.append((name, number))
    return "\n".join(lines), line_map


def locate(rule_text: str, text: str, line_map: List[Tuple[str, int]]) -> Optional[str]:
    """"Stage, row 14" for the workbook cell a rule's text came from, or None."""
    probe = re.sub(r"\s+", " ", str(rule_text or "")).strip()
    if len(probe) < 8 or not line_map:
        return None
    flat, starts = [], []
    for i, line in enumerate(text.split("\n")):
        starts.append(sum(len(x) + 1 for x in flat))
        flat.append(re.sub(r"\s+", " ", line))
    joined = " ".join(flat)
    for size in (60, 30, 15):
        key = probe[:size]
        pos = joined.find(key)
        if pos < 0:
            # Rule text may have been re-numbered or had its lead-in removed.
            key = probe[len(probe) // 3:len(probe) // 3 + size]
            pos = joined.find(key) if len(key) >= 8 else -1
        if pos >= 0:
            line = max(i for i, s in enumerate(starts) if s <= pos)
            sheet, row = line_map[min(line, len(line_map) - 1)]
            return f"{sheet}, row {row}" if row else sheet
    return None
