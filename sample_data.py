"""Manual sample data for objects that are not (yet) in Snowflake.

Legacy Excel FDDs carried sample rows typed by hand under each object. This module
keeps such samples in OBJECT_SAMPLE_DATA and can draft one from the object's saved
rules, by applying them to real rows read from its source objects.

Everything for the feature lives here and in sample_data_ui.py. The rest of the app
only calls in through guarded hooks, so setting SAMPLE_DATA_ENABLED = False switches
the feature off without touching anything else (see
/workspace/RULE_REPOSITORY_APP_BEFORE_SAMPLE_DATA/REVERT_SAMPLE_DATA.md).

Three sources of a sample, recorded in SOURCE:
    MANUAL      typed or pasted on the sample data panel
    FDD_IMPORT  the rows under the object's header row in an imported FDD
    GENERATED   drafted from the rules ("Draft from rules"), then reviewed

Each saved version keeps CELL_ORIGIN - for every cell, where its value came from
("source", "rule 3", "model (rule 5)", "unknown", "manual") - so a drafted sample
never passes off a guess as a derived value.
"""

import csv
import io
import json
import re
from typing import Callable, Dict, List, Optional, Tuple

# The one switch. False hides every sample-data control and skips every hook.
SAMPLE_DATA_ENABLED = True

MAX_ROWS = 200
MAX_COLUMNS = 60
SOURCES = ("MANUAL", "FDD_IMPORT", "GENERATED")

ORIGIN_SOURCE = "source"
ORIGIN_UNKNOWN = "unknown"
ORIGIN_MANUAL = "manual"


# ------------------------------------------------------------------
# Pasting from Excel
# ------------------------------------------------------------------
_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def parse_pasted(text: str, declared_columns: List[str] = ()) -> Tuple[List[str], List[Dict]]:
    """(columns, rows) from cells copied out of Excel.

    Excel copies a range as tab-separated lines, quoting a cell that holds a line
    break, so the csv module (not str.split) reads it. The first line is the header
    when every cell is a column name; otherwise the declared columns name the cells
    in order, and anything past them becomes COLUMN_<n>.
    """
    text = str(text or "").strip("\n")
    if not text.strip():
        return [], []
    delimiter = "\t" if "\t" in text else ","
    lines = [row for row in csv.reader(io.StringIO(text), delimiter=delimiter)]
    lines = [[c.strip() for c in row] for row in lines if any(c.strip() for c in row)]
    if not lines:
        return [], []

    first = lines[0]
    declared = [str(c).upper() for c in declared_columns or []]
    # With declared columns known, a header must name at least one of them: "A1  P1"
    # is data even though each cell looks like an identifier.
    names_ok = all(_IDENT.match(c) for c in first if c)
    is_header = names_ok and (any(c.upper() in declared for c in first) if declared
                              else True)
    if is_header:
        columns, body = [c.upper() for c in first], lines[1:]
    else:
        width = max(len(r) for r in lines)
        columns = (declared + [f"COLUMN_{i + 1}" for i in range(len(declared), width)])[:width]
        body = lines
    columns = _unique(columns)[:MAX_COLUMNS]
    rows = [{col: (row[i] if i < len(row) and row[i] != "" else None)
             for i, col in enumerate(columns)} for row in body[:MAX_ROWS]]
    return columns, rows


def _unique(names: List[str]) -> List[str]:
    seen: Dict[str, int] = {}
    out = []
    for name in names:
        name = name or "COLUMN"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        out.append(name)
    return out


def rows_from_fdd(columns: List[str], cell_rows: List[List[str]]) -> List[Dict]:
    """Rows captured under an object's header row in a tabular FDD."""
    out = []
    for cells in cell_rows[:MAX_ROWS]:
        out.append({col: (cells[i].strip() if i < len(cells) and cells[i].strip() else None)
                    for i, col in enumerate(columns[:MAX_COLUMNS])})
    return out


# ------------------------------------------------------------------
# Storage
# ------------------------------------------------------------------
def _db():
    from db import cached_read, execute, reserve_ids, sql_int, sql_value, tbl, bump_cache
    return cached_read, execute, reserve_ids, sql_int, sql_value, tbl, bump_cache


def save_sample(technical_object: str, columns: List[str], rows: List[Dict], *,
                source: str = "MANUAL", note: str = "", cell_origin: Optional[Dict] = None,
                source_rows: Optional[Dict] = None, object_id: Optional[int] = None,
                performed_by: Optional[str] = None, skip_if_same: bool = False) -> Optional[int]:
    """Save a new version of an object's sample and make it the active one.

    Earlier versions stay in the table (inactive) so a bad draft can be undone with
    restore_version(). skip_if_same leaves the table alone when the rows are the
    ones already active - re-importing an FDD must not stack copies.
    """
    cached_read, execute, reserve_ids, sql_int, sql_value, tbl, bump_cache = _db()
    technical = str(technical_object or "").strip().upper()
    if not technical or not columns:
        return None
    columns = list(columns)[:MAX_COLUMNS]
    rows = [{c: r.get(c) for c in columns} for r in list(rows)[:MAX_ROWS]]
    if skip_if_same:
        current = get_sample(technical)
        if current and current["columns"] == columns and current["rows"] == rows:
            return current["sample_id"]
    source = str(source or "MANUAL").upper()
    source = source if source in SOURCES else "MANUAL"
    from repository import _actor
    sample_id = reserve_ids("OBJECT_SAMPLE_DATA", "SAMPLE_ID", 1)[0]
    execute(f"""
        UPDATE {tbl('OBJECT_SAMPLE_DATA')} SET ACTIVE_IND = FALSE
        WHERE UPPER(TECHNICAL_OBJECT_NAME) = {sql_value(technical)} AND ACTIVE_IND
    """)
    execute(f"""
        INSERT INTO {tbl('OBJECT_SAMPLE_DATA')}
            (SAMPLE_ID, OBJECT_ID, TECHNICAL_OBJECT_NAME, COLUMNS, ROWS_JSON, CELL_ORIGIN,
             SOURCE_ROWS, SOURCE, NOTE, ACTIVE_IND, CREATED_BY)
        SELECT {int(sample_id)}, {sql_int(object_id) if object_id is not None else 'NULL'},
               {sql_value(technical)},
               PARSE_JSON({sql_value(json.dumps(columns))}),
               PARSE_JSON({sql_value(json.dumps(rows, default=str))}),
               PARSE_JSON({sql_value(json.dumps(cell_origin or {}))}),
               PARSE_JSON({sql_value(json.dumps(source_rows or {}, default=str))}),
               {sql_value(source)}, {sql_value(str(note or '')[:1000])}, TRUE,
               {_actor(performed_by)}
    """)
    bump_cache()
    return int(sample_id)


def _loads(value, default):
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def _row_to_sample(row) -> Dict:
    return {"sample_id": int(row["SAMPLE_ID"]),
            "technical_object": row["TECHNICAL_OBJECT_NAME"],
            "columns": _loads(row["COLUMNS"], []),
            "rows": _loads(row["ROWS_JSON"], []),
            "cell_origin": _loads(row["CELL_ORIGIN"], {}),
            "source_rows": _loads(row.get("SOURCE_ROWS"), {}),
            "source": row["SOURCE"], "note": row.get("NOTE") or "",
            "active": bool(row.get("ACTIVE_IND")),
            "created_by": row.get("CREATED_BY"), "created_ts": row.get("CREATED_TS")}


def get_sample(technical_object: str) -> Optional[Dict]:
    """The active sample for an object, or None."""
    if not SAMPLE_DATA_ENABLED:
        return None
    cached_read, _, _, _, sql_value, tbl, _ = _db()
    try:
        df = cached_read(f"""
            SELECT * FROM {tbl('OBJECT_SAMPLE_DATA')}
            WHERE UPPER(TECHNICAL_OBJECT_NAME) = {sql_value(str(technical_object).upper())}
              AND ACTIVE_IND
            ORDER BY SAMPLE_ID DESC LIMIT 1
        """)
    except Exception:
        return None
    return None if df.empty else _row_to_sample(df.iloc[0])


def sample_versions(technical_object: str) -> List[Dict]:
    """Every saved version, newest first, for the History list."""
    cached_read, _, _, _, sql_value, tbl, _ = _db()
    try:
        df = cached_read(f"""
            SELECT * FROM {tbl('OBJECT_SAMPLE_DATA')}
            WHERE UPPER(TECHNICAL_OBJECT_NAME) = {sql_value(str(technical_object).upper())}
            ORDER BY SAMPLE_ID DESC LIMIT 30
        """)
    except Exception:
        return []
    return [_row_to_sample(r) for _, r in df.iterrows()]


def restore_version(technical_object: str, sample_id: int,
                    performed_by: Optional[str] = None) -> None:
    """Make an older version the active one (the undo for a save or a draft)."""
    _, execute, _, sql_int, sql_value, tbl, bump_cache = _db()
    execute(f"""
        UPDATE {tbl('OBJECT_SAMPLE_DATA')}
        SET ACTIVE_IND = (SAMPLE_ID = {sql_int(sample_id)})
        WHERE UPPER(TECHNICAL_OBJECT_NAME) = {sql_value(str(technical_object).upper())}
    """)
    bump_cache()


def remove_sample(technical_object: str, performed_by: Optional[str] = None) -> None:
    """Hide the manual sample. Versions are kept, so History can bring it back."""
    _, execute, _, _, sql_value, tbl, bump_cache = _db()
    execute(f"""
        UPDATE {tbl('OBJECT_SAMPLE_DATA')} SET ACTIVE_IND = FALSE
        WHERE UPPER(TECHNICAL_OBJECT_NAME) = {sql_value(str(technical_object).upper())}
    """)
    bump_cache()


# ------------------------------------------------------------------
# Drafting a sample from the rules
# ------------------------------------------------------------------
# Rules are applied in code where the meaning is mechanical, and only the rest goes
# to the model. Code-only: COPY / CARRY_FORWARD / NO_TRANSFORMATION, fixed values,
# SET_NULL, TRIM / UPPER / LOWER, LOOKUP through a join, and record filters
# (EXCLUDE_RECORD / INCLUDE_RECORD). Everything else - DERIVE, AGGREGATE, CONCAT,
# SUBSTRING, free text - is left for the model, and a cell nobody could derive stays
# empty with origin "unknown" rather than being made up.

CODE_ACTIONS = {"COPY", "CARRY_FORWARD", "NO_TRANSFORMATION", "SET_VALUE", "DEFAULT",
                "FLAG", "SET_NULL", "TRIM", "UPPERCASE", "LOWERCASE", "LOOKUP",
                "EXCLUDE_RECORD", "INCLUDE_RECORD", "TIME_TRAVEL"}
_WHOLE_RECORD = {"*", "RECORD", "ALL", "ALL COLUMNS"}


def _g(d: Dict, *keys, default=None):
    """A field from a bundle part, whatever the key case (payloads are upper case)."""
    for k in keys:
        for cand in (k, k.upper(), k.lower()):
            if cand in d and d[cand] is not None:
                return d[cand]
    return default


def _leaf(name) -> str:
    return str(name or "").strip().rsplit(".", 1)[-1].upper()


def _elsewhere(text, source: str = "") -> bool:
    """True when `text` is a column qualified with an object other than the source,
    e.g. V_BRD_ITEM_LOCATION_CORE_PIT.ITEM_ID in a rule on another view. Reading it
    as the source's own ITEM_ID would write a wrong filter."""
    text = str(text or "").strip().strip("`\"").upper()
    if "." not in text or " " in text:
        return False
    qualifier = text.rsplit(".", 1)[0].rsplit(".", 1)[-1]
    return bool(qualifier) and qualifier != _leaf(source) and qualifier not in ("SRC", "SOURCE")


def _col_ref(value, columns: List[str], source: str = "") -> Optional[str]:
    """The source column a value names ("SRC.ITEM_ID", "ITEM_ID"), if it is one."""
    text = str(value or "").strip().strip("`\"")
    if not text or _elsewhere(text, source):
        return None
    leaf = text.rsplit(".", 1)[-1].upper()
    return leaf if leaf in columns else None


def _literal(value):
    text = str(value if value is not None else "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    if text.upper() == "NULL":
        return None
    return text


def _sql_lit(value) -> str:
    text = str(_literal(value) if value is not None else "")
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        return text
    return "'" + text.replace("'", "''") + "'"


def condition_sql(cond: Dict, source_columns: List[str], source: str = "") -> Optional[str]:
    """One rule condition as SQL on the source, or None when it cannot be written."""
    col = _leaf(_g(cond, "field_name"))
    val = _g(cond, "field_value")
    if col not in source_columns or _elsewhere(_g(cond, "field_name"), source) \
            or _elsewhere(val, source):
        return None
    op = str(_g(cond, "operator", default="=")).strip().upper()
    if op in ("IS NULL", "IS NOT NULL"):
        return f"{col} {op}"
    if op in ("IN", "NOT IN"):
        items = [v.strip() for v in re.split(r",", str(val or "").strip("() ")) if v.strip()]
        if not items:
            return None
        return f"{col} {op} ({', '.join(_sql_lit(v) for v in items)})"
    if op in ("LIKE", "NOT LIKE"):
        return f"{col} {op} {_sql_lit(val)}"
    if op == "STARTS WITH":
        return f"{col} LIKE {_sql_lit(str(_literal(val)) + '%')}"
    if op == "CONTAINS":
        return f"{col} LIKE {_sql_lit('%' + str(_literal(val)) + '%')}"
    if op in ("=", "!=", ">", ">=", "<", "<="):
        ref = _col_ref(val, source_columns, source)
        rhs = ref if ref and not str(val).strip().startswith("'") else _sql_lit(val)
        return f"{col} {'<>' if op == '!=' else op} {rhs}"
    return None


def rule_predicate(bundle: Dict, source_columns: List[str], source: str = "") -> Optional[str]:
    parts = [condition_sql(c, source_columns, source) for c in bundle.get("conditions") or []]
    parts = [p for p in parts if p]
    return " AND ".join(f"({p})" for p in parts) if parts else None


def pick_rows_sql(source_physical: str, columns: List[str], bundles: List[Dict],
                  source_columns: List[str], per_rule: int = 3, fallback: int = 10,
                  source: str = "") -> str:
    """One SELECT that picks source rows exercising each rule.

    For every rule with a condition: a few rows that match it and one that does not,
    so the draft shows both branches. Always a few unconditioned rows as well. Each
    row carries _PICKED_FOR saying why it was chosen.
    """
    cols = ", ".join(columns) if columns else "*"
    parts = [f"(SELECT {cols}, 'any' AS _PICKED_FOR FROM {source_physical} "
             f"LIMIT {int(fallback)})"]
    for bundle in bundles:
        specs = _bundle_aggregates(bundle, source_columns)
        if specs:
            # Complete groups, a few of them with several rows, so the totals are true.
            number = str(_g(bundle.get("rule") or {}, "rule_number", default="?")).replace("'", "")
            keys = ", ".join(specs[0]["group_by"])
            parts.append(
                f"(SELECT {cols}, 'rule {number} group' AS _PICKED_FOR FROM {source_physical} "
                f"WHERE ({keys}) IN (SELECT {keys} FROM (SELECT {keys}, COUNT(*) AS _N "
                f"FROM {source_physical} GROUP BY {keys} HAVING COUNT(*) BETWEEN 2 AND 20 "
                f"LIMIT 3)) LIMIT 60)")
        pred = rule_predicate(bundle, source_columns, source or source_physical)
        if not pred:
            continue
        number = str(_g(bundle.get("rule") or {}, "rule_number", default="?")).replace("'", "")
        parts.append(f"(SELECT {cols}, 'rule {number}' AS _PICKED_FOR FROM {source_physical} "
                     f"WHERE {pred} LIMIT {int(per_rule)})")
        parts.append(f"(SELECT {cols}, 'not rule {number}' AS _PICKED_FOR "
                     f"FROM {source_physical} WHERE NOT ({pred}) LIMIT 1)")
    return "\nUNION ALL\n".join(parts)


def _match(cond: Dict, row: Dict, source: str = "") -> Optional[bool]:
    """Evaluate one condition on a row; None when it cannot be evaluated."""
    col = _leaf(_g(cond, "field_name"))
    if col not in row or _elsewhere(_g(cond, "field_name"), source) \
            or _elsewhere(_g(cond, "field_value"), source):
        return None
    op = str(_g(cond, "operator", default="=")).strip().upper()
    left = row.get(col)
    raw = _g(cond, "field_value")
    right = row.get(_col_ref(raw, list(row))) if (_col_ref(raw, list(row))
                                                   and not str(raw).strip().startswith("'")
                                                   and op in ("=", "!=")) else _literal(raw)
    ls = None if left is None else str(left).strip()
    rs = None if right is None else str(right).strip()
    if op == "IS NULL":
        return ls is None or ls == ""
    if op == "IS NOT NULL":
        return not (ls is None or ls == "")
    if op in ("IN", "NOT IN"):
        items = [_literal(v.strip()) for v in str(raw or "").strip("() ").split(",") if v.strip()]
        hit = ls in [str(i) for i in items]
        return hit if op == "IN" else not hit
    if ls is None:
        return False if op in ("=", "LIKE", "STARTS WITH", "CONTAINS") else None
    if op == "=":
        return ls == rs
    if op == "!=":
        return ls != rs
    if op == "STARTS WITH":
        return ls.startswith(rs or "")
    if op == "CONTAINS":
        return (rs or "") in ls
    if op in ("LIKE", "NOT LIKE"):
        pattern = "^" + re.escape(rs or "").replace("%", ".*").replace("_", ".") + "$"
        hit = re.match(pattern.replace("\\%", ".*").replace("\\_", "."), ls) is not None
        return hit if op == "LIKE" else not hit
    try:
        a, b = float(ls), float(rs)
    except (TypeError, ValueError):
        a, b = ls, rs
    return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}.get(op)


def _applies(bundle: Dict, row: Dict, source: str = "") -> Optional[bool]:
    results = [_match(c, row, source) for c in bundle.get("conditions") or []]
    if any(r is None for r in results):
        return None
    return all(results)


def _lookup(join: Dict, row: Dict, right_rows: List[Dict], want: str) -> Tuple[bool, object]:
    """(found, value) of column `want` from the joined object's row matching `row`."""
    keys = [(_leaf(_g(c, "left_column")), _leaf(_g(c, "right_column")))
            for c in join.get("columns") or []]
    if not keys:
        return False, None
    for right in right_rows:
        if all(str(row.get(l)) == str(right.get(r)) for l, r in keys):
            return True, right.get(want)
    return False, None


def apply_rules(source_rows: List[Dict], bundles: List[Dict], target_columns: List[str],
                right_rows: Optional[Dict[str, List[Dict]]] = None, source: str = "") -> Dict:
    """Apply what can be applied in code. Returns rows, origins, exclusions and the
    (row, column, rule) cells still needing the model.
    """
    right_rows = right_rows or {}
    ordered = sorted(bundles, key=lambda b: (int(_g(b.get("rule") or {}, "priority",
                                                     default=99) or 99)))
    out_rows, origins, excluded, pending, coverage = [], {}, [], [], {}
    kept_sources: List[Dict] = []
    include_rules = [b for b in ordered if any(
        str(_g(a, "action_type", default="")).upper() == "INCLUDE_RECORD"
        for a in b.get("actions") or [])]

    for src in source_rows:
        row = {c: None for c in target_columns}
        origin = {c: ORIGIN_UNKNOWN for c in target_columns}
        for c in target_columns:           # same-named source column: carried through
            if c in src:
                row[c], origin[c] = src[c], ORIGIN_SOURCE
        drop_reason = None
        if include_rules and not any(_applies(b, src, source) for b in include_rules):
            unknown = any(_applies(b, src, source) is None for b in include_rules)
            if not unknown:
                drop_reason = "not included by rule " + ", ".join(
                    str(_g(b["rule"], "rule_number", default="?")) for b in include_rules)
        index = len(out_rows)
        for bundle in ordered:
            number = str(_g(bundle.get("rule") or {}, "rule_number", default="?"))
            applies = _applies(bundle, src, source)
            for action in bundle.get("actions") or []:
                kind = str(_g(action, "action_type", default="")).upper()
                target = _leaf(_g(action, "target_field"))
                value = _g(action, "action_value")
                if kind == "EXCLUDE_RECORD":
                    if applies:
                        drop_reason = f"excluded by rule {number}"
                    elif applies is None:
                        pending.append({"row": index, "column": "_KEEP", "rule": number})
                    continue
                if kind == "INCLUDE_RECORD":
                    continue
                if target in _WHOLE_RECORD or not target:
                    if kind in ("NO_TRANSFORMATION", "CARRY_FORWARD", "COPY", "TIME_TRAVEL"):
                        coverage.setdefault(number, []).append(index)
                    continue
                if target not in row:
                    row[target], origin[target] = None, ORIGIN_UNKNOWN
                if aggregate_spec(action, list(src)):
                    continue            # done once per group, after every row is built
                if applies is False:
                    continue
                if applies is None or kind not in CODE_ACTIONS:
                    pending.append({"row": index, "column": target, "rule": number})
                    continue
                coverage.setdefault(number, []).append(index)
                ref = _col_ref(value, list(src), source)
                if kind in ("COPY", "CARRY_FORWARD", "NO_TRANSFORMATION", "TIME_TRAVEL"):
                    row[target] = src.get(ref or target)
                elif kind in ("SET_VALUE", "DEFAULT", "FLAG"):
                    if kind == "DEFAULT" and row.get(target) not in (None, ""):
                        continue
                    row[target] = src.get(ref) if ref else _literal(value)
                elif kind == "SET_NULL":
                    row[target] = None
                elif kind in ("TRIM", "UPPERCASE", "LOWERCASE"):
                    base = src.get(ref or target)
                    base = None if base is None else str(base)
                    row[target] = (None if base is None else base.strip() if kind == "TRIM"
                                   else base.upper() if kind == "UPPERCASE" else base.lower())
                elif kind == "LOOKUP":
                    found = False
                    for join in bundle.get("joins") or []:
                        right = _leaf(_g(join.get("join") or {}, "right_object"))
                        want = _leaf(value) if value else target
                        found, got = _lookup(join, src, right_rows.get(right, []), want)
                        if found:
                            row[target] = got
                            break
                    if not found:
                        pending.append({"row": index, "column": target, "rule": number})
                        continue
                origin[target] = f"rule {number}"
        if drop_reason:
            excluded.append({"source_row": src, "reason": drop_reason})
            pending = [p for p in pending if p["row"] != index]
            continue
        out_rows.append(row)
        origins[index] = origin
        kept_sources.append(src)
    columns = list(dict.fromkeys(list(target_columns)
                                 + [c for r in out_rows for c in r]))
    for r in out_rows:
        for c in columns:
            r.setdefault(c, None)
    for o in origins.values():
        for c in columns:
            o.setdefault(c, ORIGIN_UNKNOWN)
    return {"columns": columns, "rows": out_rows, "origin": origins,
            "excluded": excluded, "pending": pending, "coverage": coverage,
            "sources": kept_sources}


def collapse_aggregates(draft: Dict, bundles: List[Dict]) -> Dict:
    """Apply AGGREGATE rules once per group, after every per-row rule (including the
    model's) has run - rule 1 may replace ITEM_ID before rule 2 groups on it."""
    sources = draft.get("sources") or []
    for bundle in sorted(bundles, key=lambda b: int(_g(b.get("rule") or {}, "priority",
                                                        default=99) or 99)):
        specs = _bundle_aggregates(bundle, list(sources[0]) if sources else [])
        if not specs or not draft["rows"]:
            continue
        number = str(_g(bundle.get("rule") or {}, "rule_number", default="?"))
        before = len(draft["rows"])
        rows, origin, members = aggregate_rows(draft["rows"], draft["origin"], sources,
                                               specs, number)
        draft["rows"], draft["origin"] = rows, origin
        sources = [sources[m[0]] for m in members.values()]
        draft["coverage"][number] = list(range(len(rows)))
        draft.setdefault("notes", []).append(
            f"Rule {number}: {before} row(s) grouped by {', '.join(specs[0]['group_by'])} "
            f"into {len(rows)}, totals computed in code over complete groups.")
    draft["sources"] = sources
    return draft


# ------------------------------------------------------------------
# Aggregation
# ------------------------------------------------------------------
# "SUM(UNRESTRICTED_STOCK_QTY) GROUP BY ITEM_ID, PLANT_CD" is mechanical, so it is done
# in code, on COMPLETE groups: summing only the rows that happened to be sampled gives a
# wrong total (the live run showed 1000 where the real view has 4000), and the model,
# asked per cell, repeated the group total on every row instead of collapsing the group.
_AGG = re.compile(r"^\s*(SUM|COUNT|MIN|MAX|AVG)\s*\(\s*([A-Za-z0-9_.*]+)\s*\)", re.I)
_GROUP_BY = re.compile(r"GROUP\s+BY\s+([A-Za-z0-9_.,\s]+?)(?:\s+(?:AT|FOR|ON|WHERE|HAVING)\b|$)",
                       re.I)


def aggregate_spec(action: Dict, source_columns: List[str]) -> Optional[Dict]:
    """{func, column, target, group_by} for an AGGREGATE action written as SQL, else None."""
    if str(_g(action, "action_type", default="")).upper() != "AGGREGATE":
        return None
    value = str(_g(action, "action_value", default="") or "")
    agg = _AGG.search(value)
    group = _GROUP_BY.search(value)
    if not agg or not group:
        return None
    column = _leaf(agg.group(2))
    keys = [_leaf(k) for k in group.group(1).split(",") if k.strip()]
    if (column != "*" and column not in source_columns) or not keys \
            or any(k not in source_columns for k in keys):
        return None
    return {"func": agg.group(1).upper(), "column": column,
            "target": _leaf(_g(action, "target_field")), "group_by": keys}


def _bundle_aggregates(bundle: Dict, source_columns: List[str]) -> List[Dict]:
    return [spec for spec in (aggregate_spec(a, source_columns)
                              for a in bundle.get("actions") or []) if spec]


def _num(value):
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _fmt(number, like) -> str:
    """Keep the source's decimals: 4000.000, not 4000.0."""
    text = str(like or "")
    decimals = len(text.split(".", 1)[1]) if "." in text else 0
    return f"{number:.{decimals}f}"


def aggregate_rows(rows: List[Dict], origins: Dict[int, Dict], sources: List[Dict],
                   specs: List[Dict], number: str) -> Tuple[List[Dict], Dict[int, Dict], Dict]:
    """Collapse rows to one per group. Columns that are neither a group key nor an
    aggregate keep their value only when every row of the group agrees; otherwise
    they are left empty and marked unknown."""
    keys = specs[0]["group_by"]
    groups: Dict[tuple, List[int]] = {}
    for i, row in enumerate(rows):
        groups.setdefault(tuple(str(row.get(k, sources[i].get(k))) for k in keys), []).append(i)
    out_rows, out_origin, members = [], {}, {}
    for group_key, idx in groups.items():
        first = idx[0]
        row, origin = dict(rows[first]), dict(origins.get(first) or {})
        for col in row:
            if col in keys or any(s["target"] == col for s in specs):
                continue
            values = {str(rows[i].get(col)) for i in idx}
            if len(values) > 1:
                row[col], origin[col] = None, ORIGIN_UNKNOWN
        for spec in specs:
            vals = [sources[i].get(spec["column"]) for i in idx] if spec["column"] != "*" else idx
            nums = [n for n in (_num(v) for v in vals) if n is not None]
            like = next((v for v in vals if v is not None), "")
            if spec["func"] == "COUNT":
                result = str(len([v for v in vals if v is not None]))
            elif not nums:
                result = None
            else:
                result = _fmt({"SUM": sum(nums), "MIN": min(nums), "MAX": max(nums),
                               "AVG": sum(nums) / len(nums)}[spec["func"]], like)
            row[spec["target"]] = result
            origin[spec["target"]] = f"rule {number}"
        members[len(out_rows)] = idx
        out_origin[len(out_rows)] = origin
        out_rows.append(row)
    return out_rows, out_origin, members


FILL_SCHEMA = {
    "type": "object",
    "properties": {"cells": {"type": "array", "items": {
        "type": "object",
        "properties": {"row": {"type": "integer"}, "column": {"type": "string"},
                       "value": {"type": "string"}, "derivable": {"type": "boolean"},
                       "rule": {"type": "string"}},
        "required": ["row", "column", "value", "derivable", "rule"]}}},
    "required": ["cells"],
}


def fill_prompt(draft: Dict, source_rows: List[Dict], bundles: List[Dict],
                joined_rows: Optional[Dict[str, List[Dict]]] = None) -> str:
    rules = []
    for b in bundles:
        r = b.get("rule") or {}
        rules.append({"rule": str(_g(r, "rule_number", default="?")),
                      "description": _g(r, "business_description", default=""),
                      "conditions": [{k: _g(c, k) for k in ("field_name", "operator",
                                                             "field_value")}
                                     for c in b.get("conditions") or []],
                      "actions": [{k: _g(a, k) for k in ("target_field", "action_type",
                                                          "action_value")}
                                  for a in b.get("actions") or []]})
    wanted = [{"row": p["row"], "column": p["column"], "rule": p["rule"]}
              for p in draft["pending"]]
    return (
        "You fill in sample data for a target table from the rules that build it.\n"
        "For each requested cell, apply the named rule to that row's SOURCE values and "
        "give the resulting value as text. Use only the source rows and rules below. "
        "If the rule cannot be applied with these values (missing column, needs data "
        "not shown, unclear rule), set derivable=false and value=\"\". Never invent "
        "values. Column \"_KEEP\" means: answer \"true\" if the row is kept, \"false\" "
        "if the rule removes it.\n\n"
        f"RULES:\n{json.dumps(rules, default=str)}\n\n"
        f"SOURCE ROWS (index = row):\n"
        f"{json.dumps({i: r for i, r in enumerate(source_rows)}, default=str)[:12000]}\n\n"
        # Rows of the joined objects, matched on the join keys. Without them a rule like
        # "use PROC_ITEM_ID from the item mapping" cannot be applied at all. An item
        # missing here has no match in that object.
        f"JOINED OBJECT ROWS (only rows matching the source rows' join keys; an item "
        f"not listed has no match):\n"
        f"{json.dumps(joined_rows or {}, default=str)[:6000]}\n\n"
        f"CELLS TO FILL:\n{json.dumps(wanted)}")


def fill_with_model(draft: Dict, source_rows: List[Dict], bundles: List[Dict],
                    llm_fn: Optional[Callable] = None,
                    joined_rows: Optional[Dict[str, List[Dict]]] = None) -> Dict:
    """Ask the model for the pending cells only; unanswered cells stay empty."""
    if not draft["pending"]:
        return draft
    if llm_fn is None:
        from db import WRITER_MODEL, llm_structured
        llm_fn = lambda prompt, schema: llm_structured(prompt, schema, WRITER_MODEL)
    try:
        answer = llm_fn(fill_prompt(draft, source_rows, bundles, joined_rows),
                        FILL_SCHEMA) or {}
    except Exception:
        answer = {}
    wanted = {(p["row"], p["column"]): p["rule"] for p in draft["pending"]}
    drop = set()
    for cell in answer.get("cells") or []:
        key = (int(cell.get("row", -1)), str(cell.get("column", "")).upper())
        if key not in wanted or not cell.get("derivable"):
            continue
        row_i, col = key
        if col == "_KEEP":
            if str(cell.get("value")).strip().lower() == "false":
                drop.add(row_i)
            continue
        if 0 <= row_i < len(draft["rows"]):
            draft["rows"][row_i][col] = cell.get("value")
            draft["origin"][row_i][col] = f"model (rule {wanted[key]})"
    if drop:
        kept_rows, kept_origin, kept_src = [], {}, []
        srcs = draft.get("sources") or []
        for i, r in enumerate(draft["rows"]):
            if i in drop:
                draft["excluded"].append({"source_row": source_rows[i] if i < len(source_rows)
                                          else {}, "reason": "removed by a rule (model)"})
                continue
            kept_origin[len(kept_rows)] = draft["origin"][i]
            kept_rows.append(r)
            if i < len(srcs):
                kept_src.append(srcs[i])
        draft["rows"], draft["origin"] = kept_rows, kept_origin
        if srcs:
            draft["sources"] = kept_src
    draft["pending"] = []
    return draft


def draft_from_rules(target_object: str, *, read_fn: Callable, rule_sets: List[Dict],
                     bundles_fn: Callable, columns_fn: Callable, declared_columns: List[str],
                     sample_fn: Callable = get_sample, physical_fn: Callable = lambda n: n,
                     llm_fn: Optional[Callable] = None, per_rule: int = 3) -> Dict:
    """Draft sample rows for `target_object` from the rule sets that build it.

    rule_sets:    [{rule_set_id, source, extra_sources, rule_ids}]
    read_fn(sql)  -> DataFrame (live read); columns_fn(obj) -> [COLUMN_NAME]
    sample_fn(obj) is used for a source that is not in Snowflake but has its own
    sample, so a chain can be drafted hop by hop.
    """
    result = {"columns": list(declared_columns), "rows": [], "origin": {}, "excluded": [],
              "coverage": {}, "source_rows": {}, "messages": [], "rules_used": []}
    for spec in rule_sets:
        source = spec.get("source") or ""
        bundles = bundles_fn(spec.get("rule_ids") or [])
        if not bundles:
            continue
        result["rules_used"] += [str(_g(b["rule"], "rule_number", default="?")) for b in bundles]
        src_cols = [str(c).upper() for c in (columns_fn(source) or [])]
        rows: List[Dict] = []
        if src_cols:
            wanted = _needed_columns(bundles, declared_columns, src_cols)
            sql = pick_rows_sql(physical_fn(source), wanted, bundles, src_cols, per_rule,
                                source=source)
            try:
                df = read_fn(sql)
                rows = [] if df is None or df.empty else df.to_dict("records")
            except Exception as exc:
                result["messages"].append(f"Could not read {_leaf(source)}: {exc}")
        else:
            saved = sample_fn(source)
            if saved:
                rows = [dict(r, _PICKED_FOR="sample") for r in saved["rows"]]
                result["messages"].append(f"{_leaf(source)} is not in Snowflake; used its "
                                          "saved sample instead.")
            else:
                result["messages"].append(f"{_leaf(source) or '(no source)'} is not in "
                                          "Snowflake and has no sample, so this hop was "
                                          "skipped.")
                continue
        rows = [{str(k).upper(): v for k, v in r.items()} for r in rows][:MAX_ROWS]
        # An aggregating hop is drafted from complete groups only: a row picked for
        # another reason is a partial group and would make the totals wrong.
        if any(_bundle_aggregates(b, src_cols) for b in bundles) and any(
                str(r.get("_PICKED_FOR", "")).endswith(" group") for r in rows):
            # Not de-duplicated: identical rows are real (the Finished Goods PIT view
            # holds each lot 4 times) and the target sums every one of them.
            rows = [r for r in rows if str(r.get("_PICKED_FOR", "")).endswith(" group")]
            result["messages"].append("This hop aggregates, so the draft uses complete "
                                      "groups of source rows only.")
        else:
            # The same row picked by two reasons (UNION ALL of the picks) counts once.
            rows = _dedupe(rows)
        picked = [r.pop("_PICKED_FOR", "") for r in rows]
        right = _read_joined(bundles, rows, read_fn, columns_fn, physical_fn)
        draft = apply_rules(rows, bundles, list(declared_columns) or [], right, source=source)
        draft = fill_with_model(draft, rows, bundles, llm_fn, joined_rows=right)
        draft = collapse_aggregates(draft, bundles)
        result["messages"] += draft.get("notes") or []
        offset = len(result["rows"])
        result["rows"] += draft["rows"]
        result["origin"].update({offset + i: o for i, o in draft["origin"].items()})
        result["excluded"] += draft["excluded"]
        for number, idx in draft["coverage"].items():
            result["coverage"].setdefault(number, []).extend(offset + i for i in idx
                                                             if i < len(draft["rows"]))
        result["columns"] = list(dict.fromkeys(result["columns"] + draft["columns"]))
        result["source_rows"][_leaf(source)] = [dict(r, _PICKED_FOR=p)
                                                for r, p in zip(rows, picked)][:50]
    for r in result["rows"]:
        for c in result["columns"]:
            r.setdefault(c, None)
    for o in result["origin"].values():
        for c in result["columns"]:
            o.setdefault(c, ORIGIN_UNKNOWN)
    return result


def _needed_columns(bundles: List[Dict], declared: List[str], src_cols: List[str]) -> List[str]:
    names = set(c.upper() for c in declared)
    for b in bundles:
        for c in b.get("conditions") or []:
            names.add(_leaf(_g(c, "field_name")))
        for a in b.get("actions") or []:
            names.add(_leaf(_g(a, "target_field")))
            ref = _col_ref(_g(a, "action_value"), src_cols)
            if ref:
                names.add(ref)
        for j in b.get("joins") or []:
            for jc in j.get("columns") or []:
                names.add(_leaf(_g(jc, "left_column")))
    cols = [c for c in src_cols if c in names]
    return cols or src_cols[:MAX_COLUMNS]


def _dedupe(rows: List[Dict]) -> List[Dict]:
    seen, out = set(), []
    for r in rows:
        key = json.dumps({k: r[k] for k in r if k != "_PICKED_FOR"}, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _read_joined(bundles, rows, read_fn, columns_fn, physical_fn) -> Dict[str, List[Dict]]:
    """Rows of each joined object matching the picked source rows' join keys."""
    out: Dict[str, List[Dict]] = {}
    for b in bundles:
        for j in b.get("joins") or []:
            right = _g(j.get("join") or {}, "right_object")
            keys = [(_leaf(_g(c, "left_column")), _leaf(_g(c, "right_column")))
                    for c in j.get("columns") or []]
            if not right or not keys or _leaf(right) in out:
                continue
            right_cols = [str(c).upper() for c in (columns_fn(right) or [])]
            left, right_key = keys[0]
            values = sorted({str(r.get(left)) for r in rows if r.get(left) is not None})[:100]
            if not right_cols or right_key not in right_cols or not values:
                continue
            sql = (f"SELECT * FROM {physical_fn(right)} WHERE {right_key} IN "
                   f"({', '.join(_sql_lit(v) for v in values)}) LIMIT 500")
            try:
                df = read_fn(sql)
                out[_leaf(right)] = [] if df is None or df.empty else [
                    {str(k).upper(): v for k, v in r.items()} for r in df.to_dict("records")]
            except Exception:
                out[_leaf(right)] = []
    return out
