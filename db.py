# Database access layer for the rule repository, including Cortex Analyst integration.
# Co-authored with CoCo
"""
Database access layer — includes Cortex Analyst integration.

Everything that touches Snowflake lives here so the rest of the app stays
testable and so we can control round-trips in one place.

Speed notes:
  * cached_read() is versioned. Writing data bumps the version instead of
    calling st.cache_data.clear(), so a single insert no longer throws away
    the whole metadata catalog.
  * reserve_ids() takes one round-trip per table instead of one per row.
"""

import hashlib
import json
import re
from typing import Dict, List, Optional

import pandas as pd
import streamlit as st
from snowflake.snowpark.context import get_active_session

TABLE_SCHEMA = "IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE"
VIEW_SCHEMA = "IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM"

# Where the repository's own metadata lives: RULE, RULE_SET, BUSINESS_OBJECT,
# TEAM, MODULE and friends. Single and shared on purpose - if this varied per
# team, each team would get its own isolated repository and cross-team lineage
# would break. tbl() and vw() resolve against it.
METADATA_DATABASE = "IF_SUPPLY_CHAIN"

# The prefix this account puts on every database name. The FDD records objects
# under their production names (SUPPLY_CHAIN, MASTER_DATA), while this account
# holds IF_SUPPLY_CHAIN and IF_MASTER_DATA. Used as the fallback when a name is
# not in DATABASE_MAP.
ENV_DATABASE_PREFIX = "IF_"

# Kept as an alias so existing callers keep working. Note this is the *metadata*
# database - the databases holding the documented data objects are per team and
# come from TEAM_DATABASE / DATABASE_MAP via catalog.py.
DATABASE = METADATA_DATABASE

# Small model for classification / slot filling, large model for prose.
# mistral-large2 / llama3.1-* are LEGACY on this account (EOL from 2026-10-14);
# the Claude models run via cross-region inference (CORTEX_ENABLED_CROSS_REGION).
FAST_MODEL = "claude-haiku-4-5"
WRITER_MODEL = "claude-sonnet-4-6"
# Used only when the chosen model errors (unavailable, throttled, disallowed).
FALLBACK_MODEL = "llama3.3-70b"
MODEL_CHOICES = [WRITER_MODEL, FAST_MODEL, FALLBACK_MODEL]


def session():
    return get_active_session()


def tbl(name: str) -> str:
    return f"{TABLE_SCHEMA}.{name}"


def vw(name: str) -> str:
    return f"{VIEW_SCHEMA}.{name}"


# ------------------------------------------------------------------
# Escaping
# ------------------------------------------------------------------
def sql_escape(value) -> str:
    if value is None:
        return ""
    return str(value).replace("\\", "\\\\").replace("'", "''")


def sql_value(value) -> str:
    """Literal for a VARCHAR column. Empty string becomes NULL."""
    if value is None:
        return "NULL"
    text = str(value).strip()
    if text == "":
        return "NULL"
    return "'" + sql_escape(text) + "'"


def sql_int(value, default=None) -> str:
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return "NULL" if default is None else str(int(default))


def ident(name: str) -> str:
    """
    Guard for anything interpolated as an identifier (column/object name).
    Rejects everything that is not a plain qualified SQL name.
    """
    text = str(name or "").strip().strip('"')
    if not text:
        raise ValueError("Empty identifier")
    for part in text.split("."):
        if not part or not all(ch.isalnum() or ch == "_" for ch in part):
            raise ValueError(f"Unsafe identifier: {name}")
        if part[0].isdigit():
            raise ValueError(f"Unsafe identifier: {name}")
    return text.upper()


# ------------------------------------------------------------------
# Reads
# ------------------------------------------------------------------
# One write counter for the whole app process, shared by every viewer. A module global
# rather than st.cache_resource: imported modules live as long as the server process,
# and a global does not depend on how Streamlit (or a test stub) implements caching.
_WRITES = {"version": 0}


def cache_version() -> int:
    # App-wide, not per session. st.cache_data is shared by every session, so a
    # per-session version let a reloaded page (a new session, back at 0) read results
    # cached before a write for up to 15 minutes: an import was saved, but the page
    # after a refresh still showed the old rules.
    return _WRITES["version"]


def bump_cache():
    """Call after any write. Invalidates reads without nuking unrelated caches."""
    _WRITES["version"] += 1


@st.cache_data(ttl=900, show_spinner=False, max_entries=256)
def _cached_sql(sql: str, version: int) -> pd.DataFrame:
    return get_active_session().sql(sql).to_pandas()


def cached_read(sql: str) -> pd.DataFrame:
    return _cached_sql(sql, cache_version())


def run_query(sql: str) -> pd.DataFrame:
    return session().sql(sql).to_pandas()


def execute(sql: str):
    return session().sql(sql).collect()


# ------------------------------------------------------------------
# Writes
# ------------------------------------------------------------------
def reserve_ids(table_name: str, id_col: str, count: int) -> List[int]:
    """
    Reserve `count` unique ids for a table, in one round-trip.

    Uses the table's SEQ_<TABLE> sequence: NEXTVAL is atomic server-side, so two
    sessions can never receive the same value. This matters because Snowflake
    does not enforce primary keys - they are declarative metadata only - so an
    id collision raises no error. It silently duplicates the id, after which
    load_bundles() merges one rule's conditions into another, and retire or
    rollback by RULE_ID hits both rows.

    Falls back to the old SELECT MAX(id)+1 if the sequence is missing or not
    granted, so this is safe to deploy before create_id_sequences.sql has been
    run. The fallback carries the original race condition; a warning is recorded
    rather than raised, because refusing to save would be worse than saving with
    the old behaviour.

    Ids are unique but NOT contiguous, and the gaps are large: Snowflake
    reserves a block (observed: 100 values) per NEXTVAL call, so ids jump. No
    caller depends on contiguity - they only index into the returned list - and
    the id columns are NUMBER(38,0), so this is cosmetic. Never treat an id as a
    row count.
    """
    if count <= 0:
        return []

    sequence = f"SEQ_{str(table_name).upper()}"
    try:
        df = run_query(
            f"SELECT {tbl(ident(sequence))}.NEXTVAL AS ID "
            f"FROM TABLE(GENERATOR(ROWCOUNT => {int(count)}))"
        )
        ids = [int(v) for v in df["ID"].tolist()]
        if len(ids) == count:
            return ids
        # A short read should never happen, but falling through to the old path
        # is better than handing back too few ids and inserting NULLs.
        _note_sequence_fallback(sequence, f"expected {count} ids, got {len(ids)}")
    except Exception as exc:
        _note_sequence_fallback(sequence, str(exc))

    df = run_query(
        f"SELECT COALESCE(MAX({ident(id_col)}), 0) + 1 AS NEXT_ID FROM {tbl(ident(table_name))}"
    )
    start = int(df.iloc[0]["NEXT_ID"])
    return list(range(start, start + count))


# Which sequences have already fallen back, so the warning is recorded once per
# session per table rather than on every single save.
_SEQUENCE_FALLBACKS: dict = {}


def _note_sequence_fallback(sequence: str, reason: str) -> None:
    if sequence not in _SEQUENCE_FALLBACKS:
        _SEQUENCE_FALLBACKS[sequence] = reason


def sequence_fallbacks() -> dict:
    """
    {sequence: reason} for every sequence that could not be used this session.

    Non-empty means ids are being allocated with the old MAX(id)+1 race - run
    create_id_sequences.sql. Surfaced on the Repository health page.
    """
    return dict(_SEQUENCE_FALLBACKS)


def insert_rows(table_name: str, columns: List[str], rows: List[list]):
    """One multi-row INSERT instead of N single-row INSERTs."""
    if not rows:
        return
    col_sql = ", ".join(ident(c) for c in columns)
    values_sql = ",\n".join("(" + ", ".join(r) + ")" for r in rows)
    execute(
        f"INSERT INTO {tbl(ident(table_name))} ({col_sql}) VALUES\n{values_sql}"
    )


# ------------------------------------------------------------------
# LLM
# ------------------------------------------------------------------
def _complete_once(prompt: str, model_name: str) -> str:
    df = run_query(
        f"SELECT SNOWFLAKE.CORTEX.COMPLETE('{sql_escape(model_name)}', "
        f"'{sql_escape(prompt)}') AS RESPONSE"
    )
    return str(df.iloc[0]["RESPONSE"])


def llm_complete(prompt: str, model_name: str = WRITER_MODEL) -> str:
    try:
        return _complete_once(prompt, model_name)
    except Exception:
        if model_name == FALLBACK_MODEL:
            raise
        return _complete_once(prompt, FALLBACK_MODEL)


def llm_structured(prompt: str, schema: Dict, model_name: str = WRITER_MODEL) -> Optional[Dict]:
    """A JSON object shaped by `schema`, or None when no model could produce one.

    AI_COMPLETE's response_format makes the model return exactly the schema - enums
    included - so an action type is always one the repository accepts, rather than
    "get_column" or "remove_leading_zeros" repaired afterwards by fuzzy matching.
    Temperature 0 makes a re-import of the same text give the same rules.
    Falls back to a plain COMPLETE on FALLBACK_MODEL, parsed leniently.
    """
    key = hashlib.sha1((model_name + json.dumps(schema, sort_keys=True) + prompt)
                       .encode("utf-8")).hexdigest()
    try:
        return _cached_structured(key, prompt, json.dumps(schema), model_name)
    except Exception:
        # Not cached: a transient failure must not stick for an hour.
        return None


@st.cache_data(ttl=3600, show_spinner=False, max_entries=256)
def _cached_structured(prompt_hash: str, prompt: str, schema_json: str,
                       model_name: str) -> Optional[Dict]:
    fmt = json.dumps({"type": "json", "schema": json.loads(schema_json)})
    try:
        df = run_query(
            f"SELECT TO_VARCHAR(AI_COMPLETE(model => '{sql_escape(model_name)}', "
            f"prompt => '{sql_escape(prompt)}', "
            "model_parameters => PARSE_JSON('{\"temperature\": 0}'), "
            f"response_format => PARSE_JSON('{sql_escape(fmt)}'))) AS RESPONSE"
        )
        return json.loads(str(df.iloc[0]["RESPONSE"]))
    except Exception:
        pass
    raw = _complete_once(prompt + "\n\nReturn only JSON matching this schema:\n"
                         + schema_json, FALLBACK_MODEL)
    parsed = parse_json_loose(raw)
    if parsed is None:
        raise ValueError("model reply was not JSON")
    return parsed


def parse_json_loose(text: str) -> Optional[Dict]:
    """The first JSON object in a model reply, ignoring fences and prose around it."""
    if not text:
        return None
    cleaned = re.sub(r"^```(json)?|```$", "", str(text).strip(),
                     flags=re.IGNORECASE | re.MULTILINE)
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


@st.cache_data(ttl=3600, show_spinner=False, max_entries=128)
def _cached_llm(prompt_hash: str, prompt: str, model_name: str) -> str:
    return llm_complete(prompt, model_name)


def llm_cached(prompt: str, model_name: str = WRITER_MODEL) -> str:
    """Same question twice in a session costs one call, not two."""
    key = hashlib.sha1((model_name + prompt).encode("utf-8")).hexdigest()
    return _cached_llm(key, prompt, model_name)


# ------------------------------------------------------------------
# Cortex Analyst
# ------------------------------------------------------------------
SEMANTIC_MODEL_STAGE = (
    '@SNOWFLAKE_LEARNING_DB."PRITIX.M.SHARMA@INTEL.COM_SEMANTIC_MODEL"'
    '.SEMANTIC_MODELS/supply_chain_planning.yaml'
)


def cortex_analyst_query(question: str, conversation: list = None) -> dict:
    """
    Call Cortex Analyst REST API with a natural language question.
    Returns dict with keys: 'sql', 'text', 'suggestions', 'error'.
    """
    import json
    import requests

    messages = list(conversation or [])
    messages.append({"role": "user", "content": [{"type": "text", "text": question}]})

    request_body = {
        "messages": messages,
        "semantic_model_file": SEMANTIC_MODEL_STAGE,
    }

    # Get the host and token from the Snowpark session
    sess = session()
    conn = sess.connection
    host = conn.host
    token = conn.rest.token

    try:
        resp = requests.post(
            url=f"https://{host}/api/v2/cortex/analyst/message",
            json=request_body,
            headers={
                "Authorization": f'Snowflake Token="{token}"',
                "Content-Type": "application/json",
            },
            timeout=60,
        )
        if resp.status_code >= 400:
            return {"sql": None, "text": None, "suggestions": None,
                    "error": f"Cortex Analyst returned {resp.status_code}: {resp.text[:500]}"}

        data = resp.json()
        content = data.get("message", {}).get("content", [])

        result = {"sql": None, "text": None, "suggestions": None, "error": None}
        for item in content:
            if item.get("type") == "sql":
                result["sql"] = item.get("statement", "")
            elif item.get("type") == "text":
                result["text"] = item.get("text", "")
            elif item.get("type") == "suggestions":
                result["suggestions"] = item.get("suggestions", [])
        return result

    except Exception as exc:
        return {"sql": None, "text": None, "suggestions": None,
                "error": f"Cortex Analyst call failed: {exc}"}
