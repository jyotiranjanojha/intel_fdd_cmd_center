# Exposes MODULE_ID/MODULE_NAME on the rule catalog, adds get_modules(), and
# Co-authored with CoCo
# adds get_lineage_edges()/lineage_roots() for the object lineage page.
# get_lineage_edges() now reads every source via RULE_SET_SOURCE instead of
# just RULE_SET.SOURCE_OBJECT_ID, so multi-source rule sets rank correctly.
# get_rule_catalog() now captures the real exception (via get_last_error())
# instead of silently swallowing it into an empty result.
# Co-authored with CoCo
"""
One prefetched picture of the repository, used for entity resolution.

The old router only recognised a column if the BA typed it in exact upper
case (FIELD_PATTERN = r"\\b[A-Z][A-Z0-9_]{2,}\\b"). "how is item description
built" matched nothing, so every question fell through to Cortex Analyst.

Here the catalog is loaded once (two queries), cached for 15 minutes, and
matching happens in memory against real column names plus their readable
forms - so "item description" finds ITEM_DSC.
"""

import difflib
import re
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

import pandas as pd

from db import (DATABASE, ENV_DATABASE_PREFIX, METADATA_DATABASE, TABLE_SCHEMA,
                VIEW_SCHEMA, cached_read, ident, tbl, vw)
from fdd_render import humanize

DEFAULT_ALLOWED_OBJECTS = [
    "IF_SUPPLY_CHAIN.PDH_COMMON_XFRM.V_BRD_ITEM_STAGE",
    "IF_SUPPLY_CHAIN.PDH_COMMON_XFRM.V_BRD_ITEM_ROOT",
    "IF_SUPPLY_CHAIN.PDH_COMMON_XFRM.V_BRD_ITEM_CHARACTERISTIC_ROOT",
    "IF_SUPPLY_CHAIN.PDH_COMMON_XFRM.V_BRD_ITEM_SALES_DETAIL_ROOT",
    "IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.T_BRD_ITEM_ROOT",
    "IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE.T_BRD_ITEM_SALES_DETAIL_ROOT",
]

DEFAULT_ROW_LIMIT = 100

STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "for", "of", "to", "in", "on", "and",
    "or", "if", "then", "when", "how", "what", "which", "where", "show", "me",
    "please", "list", "all", "rule", "rules", "field", "column", "value",
    "table", "view", "data", "record", "records", "row", "rows", "set", "get",
    "does", "do", "it", "this", "that", "from", "by", "with", "as", "be",
}


# ------------------------------------------------------------------
# Objects and columns
# ------------------------------------------------------------------
def get_allowed_objects() -> Dict[str, int]:
    try:
        df = cached_read(f"""
            SELECT OBJECT_NAME, COALESCE(MAX_ROW_LIMIT, 100) AS MAX_ROW_LIMIT
            FROM {tbl('AGENT_ALLOWED_OBJECT')}
            WHERE ACTIVE_IND = TRUE
        """)
        if not df.empty:
            return {str(r["OBJECT_NAME"]).upper(): int(r["MAX_ROW_LIMIT"])
                    for _, r in df.iterrows()}
    except Exception:
        pass
    return {obj.upper(): 100 for obj in DEFAULT_ALLOWED_OBJECTS}


def get_column_catalog() -> pd.DataFrame:
    """
    Every column of every object in both schemas, in one query.
    Columns: OBJECT_NAME (schema-qualified), TABLE_NAME, COLUMN_NAME, DATA_TYPE, READABLE
    """
    schemas = "', '".join(s.split(".")[-1] for s in (TABLE_SCHEMA, VIEW_SCHEMA))
    try:
        df = cached_read(f"""
            SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE
            FROM {DATABASE}.INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA IN ('{schemas}')
            ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
        """)
    except Exception:
        return pd.DataFrame(columns=["OBJECT_NAME", "TABLE_NAME", "COLUMN_NAME",
                                     "DATA_TYPE", "READABLE"])
    if df.empty:
        df["OBJECT_NAME"] = []
        df["READABLE"] = []
        return df
    df["OBJECT_NAME"] = (DATABASE + "." + df["TABLE_SCHEMA"] + "." + df["TABLE_NAME"]).str.upper()
    df["READABLE"] = df["COLUMN_NAME"].map(lambda c: humanize(c).lower())
    return df


def get_object_columns(full_object_name: str) -> List[str]:
    """The columns of one object: everything Snowflake has, plus everything the FDD declares.

    The UNION of two sources, not one or the other, because each covers a gap the
    other has:

      live in Snowflake   - the implementation. Needed for every object, not just
                            ones in other databases: T_BRD_ITEM_ROOT is in
                            IF_SUPPLY_CHAIN, has 58 columns, and the FDD declares
                            only 7 of them. All seven columns its own rules touch
                            (ACTIVE_IND, DELETE_IND, SOURCE_DELETE_IND ...) exist
                            live and are absent from the declared set, so relying
                            on the spec alone flags every rule on it as broken.
                            It also keeps a source view in another team's schema
                            current without re-importing.

      declared in the FDD - the specification, which legitimately runs AHEAD of
                            the build. A new FDD names columns before the table
                            exists; a rule has to be writable then. Taking live
                            alone would flag those as nonexistent.

    Used for validating field names, so a name present in either place is
    acceptable and only a name in neither is worth warning about. An empty result
    means neither source could be read - the caller says so rather than falling
    back to an unrelated catalog.

    Both reads go through cached_read (st.cache_data, 15 min), so authoring a rule
    costs one round trip per object, not one per keystroke.
    """
    name = str(full_object_name or "").strip()
    if not name:
        return []

    ordered: List[str] = []
    seen = set()

    # Live first, so the ordering a developer sees matches the real object.
    try:
        from repository import live_object_columns
        live = live_object_columns(name)
        if live is not None and not live.empty:
            for column in live["COLUMN_NAME"].tolist():
                upper = str(column).upper()
                if upper not in seen:
                    seen.add(upper)
                    ordered.append(upper)
    except Exception:
        pass

    # Then anything the FDD declares that is not built yet.
    try:
        from repository import declared_columns
        for column in declared_columns(name):
            upper = str(column).upper()
            if upper not in seen:
                seen.add(upper)
                ordered.append(upper)
    except Exception:
        pass

    return ordered


def get_database_map() -> Dict[str, str]:
    """
    Logical database name (as recorded in the FDD) -> physical name in this
    account. Falls back to the environment prefix for anything unmapped.
    """
    try:
        df = cached_read(f"""
            SELECT LOGICAL_DATABASE, PHYSICAL_DATABASE
            FROM {tbl('DATABASE_MAP')}
            WHERE ACTIVE_IND = TRUE
        """)
        if not df.empty:
            return {str(r["LOGICAL_DATABASE"]).upper(): str(r["PHYSICAL_DATABASE"]).upper()
                    for _, r in df.iterrows()}
    except Exception:
        pass
    return {}


def get_team_databases(team_id: Optional[int] = None) -> List[str]:
    """
    The logical databases a team documents objects in, primary first. With no
    team (the "All teams" view) every mapped database is returned.
    """
    where = "WHERE td.ACTIVE_IND = TRUE"
    if team_id is not None:
        where += f" AND td.TEAM_ID = {int(team_id)}"
    try:
        df = cached_read(f"""
            SELECT td.LOGICAL_DATABASE, td.IS_PRIMARY
            FROM {tbl('TEAM_DATABASE')} td
            {where}
            ORDER BY td.IS_PRIMARY DESC, td.LOGICAL_DATABASE
        """)
        if not df.empty:
            seen, out = set(), []
            for _, r in df.iterrows():
                name = str(r["LOGICAL_DATABASE"]).upper()
                if name not in seen:
                    seen.add(name)
                    out.append(name)
            return out
    except Exception:
        pass
    return []


def physical_databases(team_id: Optional[int] = None) -> List[str]:
    """
    The physical databases to search for a team. Always includes the metadata
    database so the app still works before any mapping is configured.
    """
    mapping = get_database_map()
    out = []
    for logical in get_team_databases(team_id):
        physical = mapping.get(logical) or _prefixed(logical)
        if physical not in out:
            out.append(physical)
    if not out:
        out = [METADATA_DATABASE.upper()]
    return out


def _prefixed(database: str) -> str:
    """Environment fallback: add the account prefix unless it is already there."""
    name = str(database or "").upper()
    prefix = ENV_DATABASE_PREFIX.upper()
    return name if name.startswith(prefix) else prefix + name


def resolve_physical_name(name: str) -> str:
    """
    Rewrite a three-part name onto the database that actually exists here.

    Consults DATABASE_MAP first, then falls back to prefixing. Replaces the
    earlier hardcoded rewrite to a single database, which could not express
    MASTER_DATA -> IF_MASTER_DATA.
    """
    parts = [p for p in str(name or "").upper().split(".") if p]
    if len(parts) != 3:
        return ".".join(parts)
    mapping = get_database_map()
    physical = mapping.get(parts[0]) or _prefixed(parts[0])
    return f"{physical}.{parts[1]}.{parts[2]}"


def get_object_index(databases: Optional[List[str]] = None) -> pd.DataFrame:
    """
    Every table and view across the given physical databases — names only.

    Deliberately separate from get_column_catalog(): this account has ~3,900
    objects per database but ~79,500 columns, so loading every column of every
    schema would make resolve_field()'s fuzzy matching slow and less precise.
    The column catalog stays narrow; object resolution gets the wide view.

    A database that errors (absent or not granted in this environment) is
    skipped rather than failing the page.
    """
    targets = [d.upper() for d in (databases or [METADATA_DATABASE])]
    frames = []
    for database in targets:
        try:
            df = cached_read(f"""
                SELECT '{database}' AS DATABASE_NAME, TABLE_SCHEMA, TABLE_NAME
                FROM {database}.INFORMATION_SCHEMA.TABLES
                WHERE TABLE_SCHEMA <> 'INFORMATION_SCHEMA'
            """)
            if not df.empty:
                frames.append(df)
        except Exception:
            continue
    if not frames:
        return pd.DataFrame(columns=["DATABASE_NAME", "TABLE_SCHEMA", "TABLE_NAME"])
    return pd.concat(frames, ignore_index=True)


def database_reachable(database: str) -> bool:
    """Whether a physical database can be read from here. Used by the admin UI."""
    try:
        cached_read(f"""
            SELECT 1 FROM {database.upper()}.INFORMATION_SCHEMA.SCHEMATA LIMIT 1
        """)
        return True
    except Exception:
        return False


def get_queryable_objects(databases: Optional[List[str]] = None) -> Dict[str, int]:
    """
    Every object the Ask page may read, mapped to its row cap.

    Three sources, in increasing precedence:
      1. Every real table/view in the given physical databases.
      2. Objects the repository documents (BUSINESS_OBJECT), under the physical
         name when that resolves to something real, otherwise as recorded.
      3. AGENT_ALLOWED_OBJECT, so its explicit per-object row limits win.

    Without this, resolve_object() only recognised the handful of names in
    AGENT_ALLOWED_OBJECT — so "show sample rows from <anything else>" resolved to
    no object, and nlp_router then demoted the data question to a documentation
    question. The object has to resolve before the intent can survive.
    """
    objects: Dict[str, int] = {}

    # 1. Everything that actually exists in the team's databases.
    index = get_object_index(databases)
    if not index.empty:
        names = (index["DATABASE_NAME"].astype(str).str.upper() + "."
                 + index["TABLE_SCHEMA"].astype(str).str.upper() + "."
                 + index["TABLE_NAME"].astype(str).str.upper())
        objects = dict.fromkeys(names.tolist(), DEFAULT_ROW_LIMIT)

    # 2. Objects the repository documents. Prefer the physical form when it names
    #    something real; otherwise keep what was recorded, so an object that is
    #    documented but not yet built still resolves and reports honestly.
    try:
        df = cached_read(f"""
            SELECT DISTINCT TECHNICAL_OBJECT_NAME
            FROM {tbl('BUSINESS_OBJECT')}
            WHERE TECHNICAL_OBJECT_NAME IS NOT NULL
        """)
        for _, row in df.iterrows():
            recorded = str(row["TECHNICAL_OBJECT_NAME"]).upper()
            if recorded.count(".") != 2:
                continue
            if resolve_physical_name(recorded) in objects:
                continue  # already queryable under its physical name
            objects.setdefault(recorded, DEFAULT_ROW_LIMIT)
    except Exception:
        pass

    # 3. Explicit allowlist last: its row limits take precedence.
    objects.update(get_allowed_objects())
    return objects


def resolve_object(name: Optional[str],
                   databases: Optional[List[str]] = None) -> Tuple[Optional[str], Optional[int]]:
    """
    Find the real object a name refers to, scoped to the given physical
    databases. Scoping keeps fuzzy matching from picking an object in a database
    the current team does not use.
    """
    if not name:
        return None, None
    allowed = get_queryable_objects(databases)
    upper = name.upper().strip()

    # Fully qualified, exactly as stored.
    if upper in allowed:
        return upper, allowed[upper]

    # Fully qualified but under a logical database name, as the FDD records them.
    physical = resolve_physical_name(upper)
    if physical in allowed:
        return physical, allowed[physical]

    # Bare or partially qualified name.
    for full, limit in allowed.items():
        if full.split(".")[-1] == upper or full.endswith("." + upper):
            return full, limit

    # Tolerate a typo.
    shorts = {full.split(".")[-1]: full for full in allowed}
    close = difflib.get_close_matches(upper.split(".")[-1], list(shorts), n=1, cutoff=0.82)
    if close:
        full = shorts[close[0]]
        return full, allowed[full]
    return None, None


# ------------------------------------------------------------------
# Rule catalog
# ------------------------------------------------------------------
_LAST_ERROR: Optional[str] = None


def get_last_error() -> Optional[str]:
    """The real exception from the most recent failed catalog read, if any."""
    return _LAST_ERROR


def get_rule_catalog(include_retired: bool = False) -> pd.DataFrame:
    """
    Lightweight index of every rule. Loaded once; all rule lookup is then done
    in pandas instead of firing three LIKE queries per question.

    Retired rules are excluded by default: retiring is this repository's delete,
    so a retired rule must not surface in the catalog, the FDD or the LLM
    context. Pass include_retired=True for the restore picker and for audit
    views that have to show them.
    """
    global _LAST_ERROR
    where = "" if include_retired else "WHERE COALESCE(RULE_STATUS, '') <> 'RETIRED'"
    try:
        df = cached_read(f"""
            SELECT DISTINCT
                   RULE_ID, RULE_NUMBER, RULE_NAME, RULE_CATEGORY, RULE_STATUS,
                   RULE_SET_ID, RULE_SET_NAME, SUBJECT_AREA, MODULE_ID, MODULE_NAME,
                   SOURCE_BUSINESS_OBJECT, TARGET_BUSINESS_OBJECT,
                   SOURCE_TECHNICAL_OBJECT, TARGET_TECHNICAL_OBJECT,
                   COALESCE(BUSINESS_DESCRIPTION, '') AS BUSINESS_DESCRIPTION,
                   PRIORITY, APPROVED_BY, APPROVED_TS
            FROM {vw('VW_RULE_SUMMARY')}
            {where}
        """)
        _LAST_ERROR = None
        return df
    except Exception as exc:
        _LAST_ERROR = str(exc)
        return pd.DataFrame()


def get_modules() -> pd.DataFrame:
    """Every module, with its team and how many business objects and rule sets sit under it."""
    try:
        return cached_read(f"""
            SELECT m.MODULE_ID, m.MODULE_NAME, m.MODULE_CODE, m.DESCRIPTION, m.ACTIVE_IND,
                   m.TEAM_ID, t.TEAM_NAME,
                   (SELECT COUNT(*) FROM {tbl('BUSINESS_OBJECT')} bo WHERE bo.MODULE_ID = m.MODULE_ID) AS OBJECT_COUNT,
                   (SELECT COUNT(*) FROM {tbl('RULE_SET')} rs WHERE rs.MODULE_ID = m.MODULE_ID) AS RULE_SET_COUNT,
                   COALESCE(lr.LIVE_RULE_COUNT, 0) AS LIVE_RULE_COUNT
            FROM {tbl('MODULE')} m
            LEFT JOIN {tbl('TEAM')} t ON t.TEAM_ID = m.TEAM_ID
            -- Rules not retired. A module whose rules were all retired (replaced by a
            -- re-import into a new module) still has its rule sets, so RULE_SET_COUNT
            -- alone does not tell a live module from an abandoned one.
            LEFT JOIN (
                SELECT rs.MODULE_ID, COUNT(*) AS LIVE_RULE_COUNT
                FROM {tbl('RULE_SET')} rs
                JOIN {tbl('RULE')} r ON r.RULE_SET_ID = rs.RULE_SET_ID
                WHERE r.ACTIVE_IND AND r.RULE_STATUS <> 'RETIRED'
                GROUP BY rs.MODULE_ID
            ) lr ON lr.MODULE_ID = m.MODULE_ID
            ORDER BY m.MODULE_NAME
        """)
    except Exception:
        return pd.DataFrame()


# ------------------------------------------------------------------
# Object lineage
# ------------------------------------------------------------------
def get_lineage_edges() -> pd.DataFrame:
    """
    One row per (source object, target object) hop in the lineage graph.
    Reads from RULE_SET_SOURCE rather than RULE_SET.SOURCE_OBJECT_ID directly,
    because several rule sets join more than one source object (visible in
    SOURCE_TECHNICAL_OBJECT as a comma-separated list) - a single FK column
    can only ever record one of them. Using the junction table means a rule
    set with three sources produces three edges into the same target, so
    ranking and the lineage tree see every real dependency, not just the
    first one someone happened to record.

    Self-loops (a PIT/analysis view defined against itself) are flagged
    rather than dropped, so the caller can decide whether to show them.

    Retired work is excluded. Retiring a rule does not delete its rule set, and
    with no status filter here the hop kept drawing: an Item import created
    Hybrid BOM Location -> UPI BOM Location, the rule was retired as wrong, and
    the edge stayed on the diagram with nothing left to justify it.

    The test is deliberately "had rules and all of them are retired", not "has no
    active rules". A rule set with no rules at all is a hop someone has just
    declared and not yet documented, and dropping it would make a module being
    built up hop by hop disappear from its own diagram. Emptied by retirement is a
    decision; empty from the start is work in progress.
    """
    try:
        return cached_read(f"""
            SELECT
                rs.RULE_SET_ID, rs.RULE_SET_NAME,
                src.OBJECT_ID AS SOURCE_OBJECT_ID, src.OBJECT_NAME AS SOURCE_OBJECT_NAME,
                src.TECHNICAL_OBJECT_NAME AS SOURCE_TECHNICAL_OBJECT, srcm.MODULE_NAME AS SOURCE_MODULE_NAME,
                src.OBJECT_TYPE AS SOURCE_OBJECT_TYPE,
                tgt.OBJECT_ID AS TARGET_OBJECT_ID, tgt.OBJECT_NAME AS TARGET_OBJECT_NAME,
                tgt.TECHNICAL_OBJECT_NAME AS TARGET_TECHNICAL_OBJECT, tgtm.MODULE_NAME AS TARGET_MODULE_NAME,
                tgt.OBJECT_TYPE AS TARGET_OBJECT_TYPE,
                (src.OBJECT_ID = tgt.OBJECT_ID) AS IS_SELF_LOOP,
                rss.SOURCE_SEQ,
                COALESCE(rc.RULE_COUNT, 0) AS RULE_COUNT,
                rc.RULE_KIND
            FROM {tbl('RULE_SET')} rs
            -- What the hop does, for a short arrow label: how many live rules, and
            -- whether they only pass data through (no change / time travel).
            LEFT JOIN (
                SELECT RULE_SET_ID, COUNT(*) AS RULE_COUNT,
                       CASE WHEN COUNT_IF(RULE_CATEGORY NOT IN
                                          ('NO_TRANSFORMATION', 'TIME_TRAVEL')) > 0 THEN 'RULES'
                            WHEN COUNT_IF(RULE_CATEGORY = 'TIME_TRAVEL') > 0 THEN 'TIME_TRAVEL'
                            ELSE 'NO_TRANSFORMATION' END AS RULE_KIND
                FROM {tbl('RULE')}
                WHERE COALESCE(RULE_STATUS, '') <> 'RETIRED' AND COALESCE(ACTIVE_IND, TRUE) = TRUE
                GROUP BY RULE_SET_ID
            ) rc ON rc.RULE_SET_ID = rs.RULE_SET_ID
            JOIN {tbl('RULE_SET_SOURCE')} rss ON rs.RULE_SET_ID = rss.RULE_SET_ID
            JOIN {tbl('BUSINESS_OBJECT')} src ON rss.OBJECT_ID = src.OBJECT_ID
            JOIN {tbl('BUSINESS_OBJECT')} tgt ON rs.TARGET_OBJECT_ID = tgt.OBJECT_ID
            LEFT JOIN {tbl('MODULE')} srcm ON src.MODULE_ID = srcm.MODULE_ID
            LEFT JOIN {tbl('MODULE')} tgtm ON tgt.MODULE_ID = tgtm.MODULE_ID
            WHERE COALESCE(rs.ACTIVE_IND, TRUE) = TRUE
              AND COALESCE(src.ACTIVE_IND, TRUE) = TRUE
              AND COALESCE(tgt.ACTIVE_IND, TRUE) = TRUE
              AND NOT (
                    (SELECT COUNT(*) FROM {tbl('RULE')} r_all
                      WHERE r_all.RULE_SET_ID = rs.RULE_SET_ID) > 0
                AND (SELECT COUNT(*) FROM {tbl('RULE')} r_live
                      WHERE r_live.RULE_SET_ID = rs.RULE_SET_ID
                        AND COALESCE(r_live.RULE_STATUS, '') <> 'RETIRED'
                        AND COALESCE(r_live.ACTIVE_IND, TRUE) = TRUE) = 0
              )
            ORDER BY rs.RULE_SET_ID, rss.SOURCE_SEQ
        """)
    except Exception:
        return pd.DataFrame()


def lineage_roots(edges: pd.DataFrame) -> List[Dict]:
    """
    The topmost objects: everything that is a source but never the target of
    a hop from a *different* object. An object that only ever appears in a
    self-loop (a PIT/analysis view defined against itself, with no other
    inbound hop) still counts as a source here, so it isn't silently dropped.
    """
    if edges.empty:
        return []
    all_sources = edges[["SOURCE_OBJECT_ID", "SOURCE_OBJECT_NAME", "SOURCE_MODULE_NAME"]].drop_duplicates()
    real_targets = set(edges.loc[~edges["IS_SELF_LOOP"], "TARGET_OBJECT_ID"].tolist())
    roots = all_sources[~all_sources["SOURCE_OBJECT_ID"].isin(real_targets)]
    return [
        {"object_id": int(r["SOURCE_OBJECT_ID"]), "object_name": r["SOURCE_OBJECT_NAME"],
         "module_name": r["SOURCE_MODULE_NAME"]}
        for _, r in roots.iterrows()
    ]


def scope_edges_to_module(edges: pd.DataFrame, roots: List[Dict], module_name: str,
                          stay_in_module: bool = True):
    """
    Trim the lineage graph to one module, the way the Object lineage page and
    the Publish FDD export both need it. Shared here so the two draw an
    identical picture rather than two implementations quietly drifting apart.

    Filtering the edges frame - not just the roots - is what actually scopes
    the picture: build_dot() walks whatever frame it is handed. Keeping hops
    with either end in the module means a boundary hop survives (so the chart
    shows the data leaving) while the foreign chain beyond it does not, because
    neither of its ends is in the module.

    Returns (scoped_edges, boundary_ids, scoped_roots). boundary_ids is empty
    and scoped_edges is the full frame when module_name is "All" or
    stay_in_module is False.
    """
    scoped_roots = roots if module_name == "All" else [
        r for r in roots if r["module_name"] == module_name
    ]
    if module_name == "All" or not stay_in_module:
        return edges, set(), scoped_roots

    scoped_edges = edges[
        (edges["SOURCE_MODULE_NAME"] == module_name)
        | (edges["TARGET_MODULE_NAME"] == module_name)
    ]
    if scoped_edges.empty:
        return scoped_edges, set(), scoped_roots

    boundary_ids = {int(i) for i in (
        set(scoped_edges.loc[scoped_edges["SOURCE_MODULE_NAME"] != module_name, "SOURCE_OBJECT_ID"])
        | set(scoped_edges.loc[scoped_edges["TARGET_MODULE_NAME"] != module_name, "TARGET_OBJECT_ID"])
    )}

    # A module fed only from outside has no root of its own in the full graph,
    # and would otherwise draw nothing.
    in_scope = lineage_roots(scoped_edges)
    known = {r["object_id"] for r in scoped_roots}
    scoped_roots = scoped_roots + [
        r for r in in_scope
        if r["object_id"] not in known and r["object_id"] not in boundary_ids
    ]
    return scoped_edges, boundary_ids, scoped_roots


def get_field_index() -> pd.DataFrame:
    """
    Which rules touch which field. One query, replaces the per-question
    LIKE scans over VW_RULE_DETAIL and RULE_MAPPING.
    """
    try:
        return cached_read(f"""
            SELECT FIELD, RULE_ID FROM (
                SELECT UPPER(FIELD_NAME)     AS FIELD, RULE_ID FROM {vw('VW_RULE_DETAIL')} WHERE FIELD_NAME     IS NOT NULL
                UNION
                SELECT UPPER(TARGET_FIELD)   AS FIELD, RULE_ID FROM {vw('VW_RULE_DETAIL')} WHERE TARGET_FIELD   IS NOT NULL
                UNION
                SELECT UPPER(SOURCE_COLUMN)  AS FIELD, RULE_ID FROM {tbl('RULE_MAPPING')}  WHERE SOURCE_COLUMN  IS NOT NULL
                UNION
                SELECT UPPER(TARGET_COLUMN)  AS FIELD, RULE_ID FROM {tbl('RULE_MAPPING')}  WHERE TARGET_COLUMN  IS NOT NULL
            )
        """)
    except Exception:
        return pd.DataFrame(columns=["FIELD", "RULE_ID"])


# ------------------------------------------------------------------
# Fuzzy resolution
# ------------------------------------------------------------------
def _ngrams(text: str, max_n: int = 4) -> List[str]:
    words = [w for w in re.findall(r"[A-Za-z0-9_]+", text.lower()) if w not in STOPWORDS]
    out = []
    for n in range(min(max_n, len(words)), 0, -1):
        for i in range(len(words) - n + 1):
            out.append(" ".join(words[i:i + n]))
    return out


def resolve_field(text: str, limit: int = 3) -> List[str]:
    """
    Returns the most likely real column names for a phrase.
    'item description' -> ['ITEM_DSC', 'ITEM_DESCRIPTION', ...]
    """
    catalog = get_column_catalog()
    if catalog.empty:
        return [t for t in re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", text.upper())][:limit]

    known = catalog[["COLUMN_NAME", "READABLE"]].drop_duplicates()
    exact = {str(c).upper() for c in known["COLUMN_NAME"]}

    scores: Dict[str, float] = {}
    for token in re.findall(r"\b[A-Za-z][A-Za-z0-9_]{2,}\b", text.upper()):
        if token in exact:
            scores[token] = 1.0

    phrases = _ngrams(text)
    readable_map: Dict[str, str] = {}
    for _, row in known.iterrows():
        readable_map.setdefault(str(row["READABLE"]), str(row["COLUMN_NAME"]).upper())

    for phrase in phrases:
        if phrase in readable_map:
            scores[readable_map[phrase]] = max(scores.get(readable_map[phrase], 0), 0.95)
            continue
        for match in difflib.get_close_matches(phrase, list(readable_map), n=2, cutoff=0.86):
            column = readable_map[match]
            ratio = difflib.SequenceMatcher(None, phrase, match).ratio()
            scores[column] = max(scores.get(column, 0), ratio * 0.9)

    return [c for c, _ in sorted(scores.items(), key=lambda kv: -kv[1])[:limit]]


def resolve_rules(text: str, limit: int = 8) -> List[int]:
    """
    Rank rules against a question using rule number, name, objects, description
    and the field index. All in memory - no round-trip per question.
    """
    catalog = get_rule_catalog()
    if catalog.empty:
        return []

    upper = text.upper()
    scores: Dict[int, float] = {}

    def add(rule_id, weight):
        try:
            rid = int(rule_id)
        except (TypeError, ValueError):
            return
        scores[rid] = scores.get(rid, 0) + weight

    # explicit id
    for match in re.finditer(r"RULE[_ ]?ID\s*[=:]?\s*(\d+)", upper):
        add(match.group(1), 10)

    # rule number, e.g. "rule 3A", "rule 12.1"
    numbers = set(re.findall(r"\bRULE\s+([A-Z0-9][A-Z0-9._-]*)\b", upper))
    if numbers:
        hit = catalog[catalog["RULE_NUMBER"].astype(str).str.upper().isin(numbers)]
        for rid in hit["RULE_ID"]:
            add(rid, 10)

    # object mentions
    for _, row in catalog.iterrows():
        for col, weight in (("SOURCE_BUSINESS_OBJECT", 2), ("TARGET_BUSINESS_OBJECT", 3),
                            ("SOURCE_TECHNICAL_OBJECT", 2), ("TARGET_TECHNICAL_OBJECT", 3),
                            ("RULE_SET_NAME", 1)):
            value = str(row.get(col) or "").upper()
            if value and len(value) > 3 and value.split(".")[-1] in upper:
                add(row["RULE_ID"], weight)

    # field mentions
    fields = resolve_field(text)
    if fields:
        index = get_field_index()
        if not index.empty:
            hit = index[index["FIELD"].astype(str).str.upper().isin(fields)]
            for rid in hit["RULE_ID"]:
                add(rid, 4)
        for _, row in catalog.iterrows():
            name = str(row.get("RULE_NAME") or "").upper()
            if any(f in name for f in fields):
                add(row["RULE_ID"], 3)

    # description keywords, last resort
    if not scores:
        words = [w for w in _ngrams(text, max_n=2) if len(w) > 4][:6]
        for _, row in catalog.iterrows():
            blob = (str(row.get("RULE_NAME") or "") + " " +
                    str(row.get("BUSINESS_DESCRIPTION") or "")).lower()
            for word in words:
                if word in blob:
                    add(row["RULE_ID"], 1)

    return [rid for rid, _ in sorted(scores.items(), key=lambda kv: -kv[1])[:limit]]


@lru_cache(maxsize=1)
def _noop():
    return None
