# Logs every save/update to RULE_CHANGE_HISTORY and exposes get_rule_history()
# for the audit trail page. Also adds module CRUD (create/rename/reassign)
# backing the Manage modules page, next_rule_number() to auto-suggest the
# next rule number within a rule set, and get_rule_set_source_objects() for
# the compliance check against real Snowflake objects.
# Co-authored with CoCo
"""
Reads and writes for rule metadata.

Before: five sequential queries per question to build context, then one INSERT
statement per condition, action, join column and filter when saving.
Now: one query to load any number of rules, and one INSERT per table to save.
"""

import json
import re
from typing import Dict, List, Optional, Tuple

import pandas as pd

from db import (bump_cache, cached_read, execute, ident, insert_rows, reserve_ids,
                run_query, sql_int, sql_value, tbl, vw)

RULE_PARTS = ("RULE", "CONDITION", "ACTION", "JOIN", "JOIN_COLUMN",
               "JOIN_FILTER", "MAPPING", "REFERENCE")


# ------------------------------------------------------------------
# General audit trail
# ------------------------------------------------------------------
def _text(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def log_audit_event(entity_type: str, entity_id, action: str,
                    detail: Optional[str] = None,
                    performed_by: Optional[str] = None) -> None:
    """
    Insert a row into AUDIT_TRAIL for any repository mutation.

    entity_type: RULE, MODULE, REVIEW_COMMENT, BUSINESS_OBJECT, RULE_SET
    action: e.g. COMMENT_ADDED, COMMENT_RESOLVED, MODULE_CREATED, etc.
    performed_by: override for the viewer name (same OWNER-mode issue as comments).
    """
    audit_id = reserve_ids("AUDIT_TRAIL", "AUDIT_ID", 1)[0]
    performer = _actor(performed_by)
    insert_rows(
        "AUDIT_TRAIL",
        ["AUDIT_ID", "ENTITY_TYPE", "ENTITY_ID", "ACTION", "DETAIL", "PERFORMED_BY"],
        [[str(audit_id), sql_value(entity_type), sql_int(entity_id) if entity_id else "NULL",
          sql_value(action), sql_value(detail), performer]],
    )


def get_audit_trail(entity_type: Optional[str] = None, limit: int = 500) -> pd.DataFrame:
    """All activity from the general audit trail, optionally filtered by entity type."""
    where = f"WHERE ENTITY_TYPE = {sql_value(entity_type)}" if entity_type else ""
    return cached_read(f"""
        SELECT AUDIT_ID, ENTITY_TYPE, ENTITY_ID, ACTION, DETAIL,
               PERFORMED_BY, PERFORMED_TS
        FROM {tbl('AUDIT_TRAIL')}
        {where}
        ORDER BY PERFORMED_TS DESC, AUDIT_ID DESC
        LIMIT {int(limit)}
    """)

SCALAR_AUDIT_FIELDS = (
    ("rule_name", "RULE_NAME"),
    ("rule_category", "RULE_CATEGORY"),
    ("business_description", "BUSINESS_DESCRIPTION"),
    ("priority", "PRIORITY"),
    ("rule_status", "RULE_STATUS"),
)
SECTION_AUDIT_FIELDS = (
    ("conditions", "CONDITIONS"),
    ("actions", "ACTIONS"),
    ("joins", "JOINS"),
)


def session_viewer() -> Optional[str]:
    """The viewer of the current Streamlit session, if there is one.

    streamlit_app sets session_state["current_user"] from st.user at startup. Read per
    call - session_state belongs to one browser session, so two people using the app
    at once are never mixed up, which a module-level default would do.
    """
    try:
        import streamlit as st
        name = st.session_state.get("current_user")
    except Exception:
        return None
    return _text(name) or None


def _actor(performed_by: Optional[str] = None) -> str:
    """SQL for the person responsible, for UPDATED_BY / CHANGED_BY / APPROVED_BY.

    The caller's name first, then this session's viewer. CURRENT_USER() is only the
    last resort (no session, e.g. a script): under execute_as OWNER it records the
    Streamlit service identity, so it exists to avoid writing NULL, not because it
    is useful.
    """
    name = _text(performed_by) or session_viewer()
    return sql_value(name) if name else "CURRENT_USER()"


# ------------------------------------------------------------------
# Audit trail (per-field rule history)
# ------------------------------------------------------------------
def log_rule_changes(rule_id: int, changes: List[tuple],
                     performed_by: Optional[str] = None) -> None:
    """
    changes: list of (change_type, old_value, new_value). Rows where the
    before and after are identical are skipped, so a no-op save leaves no
    trace in the audit trail.

    performed_by names the person. Without it CHANGED_BY falls back to its
    CURRENT_USER() default, and because the app runs execute_as OWNER that records
    the Streamlit service identity - every row in this table read
    STPLATSTREAMLIT9088925997 rather than a name. An approval nobody can be
    attributed to is not an approval, so callers that know the viewer must say so.
    """
    rows = [(t, _text(o), _text(n)) for t, o, n in changes]
    rows = [(t, o, n) for t, o, n in rows if o != n]
    if not rows:
        return
    who = _actor(performed_by)
    ids = reserve_ids("RULE_CHANGE_HISTORY", "CHANGE_ID", len(rows))
    insert_rows(
        "RULE_CHANGE_HISTORY",
        ["CHANGE_ID", "RULE_ID", "CHANGE_TYPE", "OLD_VALUE", "NEW_VALUE", "CHANGED_BY"],
        [[str(ids[i]), str(rule_id), sql_value(t), sql_value(o), sql_value(n), who]
         for i, (t, o, n) in enumerate(rows)],
    )


def _diff_scalars(previous: Dict, proposal: Dict) -> List[tuple]:
    return [(label, previous.get(key), proposal.get(key)) for key, label in SCALAR_AUDIT_FIELDS]


def _diff_sections(previous: Dict, proposal: Dict) -> List[tuple]:
    changes = []
    for key, label in SECTION_AUDIT_FIELDS:
        before = previous.get(key) or []
        after = proposal.get(key) or []
        if json.dumps(before, sort_keys=True, default=str) != json.dumps(after, sort_keys=True, default=str):
            before_text = json.dumps(before, default=str) if before else None
            after_text = json.dumps(after, default=str) if after else None
            changes.append((label, before_text, after_text))
    return changes


def get_rule_set_source_objects(rule_set_id: int) -> List[str]:
    """
    Every source object a rule set actually joins, via RULE_SET_SOURCE - not
    just the single SOURCE_OBJECT_ID column, which can't hold more than one.
    Used by the compliance check so a multi-source rule set is checked
    against everything it really reads from.
    """
    df = cached_read(f"""
        SELECT bo.TECHNICAL_OBJECT_NAME
        FROM {tbl('RULE_SET_SOURCE')} rss
        JOIN {tbl('BUSINESS_OBJECT')} bo ON rss.OBJECT_ID = bo.OBJECT_ID
        WHERE rss.RULE_SET_ID = {sql_int(rule_set_id)}
        ORDER BY rss.SOURCE_SEQ
    """)
    if df.empty:
        return []
    return [str(v) for v in df["TECHNICAL_OBJECT_NAME"].tolist() if v]


def get_rule_history(rule_ids) -> pd.DataFrame:
    """Every logged change for one rule or a list of rules, most recent first."""
    if not isinstance(rule_ids, (list, tuple, set)):
        rule_ids = [rule_ids]
    ids = sorted({int(r) for r in rule_ids})
    columns = ["CHANGE_ID", "RULE_ID", "CHANGE_TYPE", "OLD_VALUE", "NEW_VALUE", "CHANGED_BY", "CHANGED_TS"]
    if not ids:
        return pd.DataFrame(columns=columns)
    id_list = ", ".join(str(i) for i in ids)
    return cached_read(f"""
        SELECT CHANGE_ID, RULE_ID, CHANGE_TYPE, OLD_VALUE, NEW_VALUE, CHANGED_BY, CHANGED_TS
        FROM {tbl('RULE_CHANGE_HISTORY')}
        WHERE RULE_ID IN ({id_list})
        ORDER BY CHANGED_TS DESC, CHANGE_ID DESC
    """)


def log_rollback_marker(rule_id: int, restored_change_id: int, restored_ts) -> None:
    """A visible breadcrumb in the audit trail marking that a rollback happened."""
    log_rule_changes(rule_id, [
        ("ROLLBACK", None, f"Restored to the version from {restored_ts} (change #{restored_change_id})."),
    ])


def _bundle_sql(ids: str) -> str:
    return f"""
        SELECT 'RULE' AS PART, RULE_ID, OBJECT_CONSTRUCT(*) AS PAYLOAD
        FROM {vw('VW_RULE_SUMMARY')} WHERE RULE_ID IN ({ids})
        UNION ALL
        SELECT 'CONDITION', RULE_ID, OBJECT_CONSTRUCT(*)
        FROM {tbl('RULE_CONDITION')} WHERE RULE_ID IN ({ids})
        UNION ALL
        SELECT 'ACTION', RULE_ID, OBJECT_CONSTRUCT(*)
        FROM {tbl('RULE_ACTION')} WHERE RULE_ID IN ({ids})
        UNION ALL
        SELECT 'JOIN', RULE_ID, OBJECT_CONSTRUCT(*)
        FROM {tbl('RULE_JOIN')} WHERE RULE_ID IN ({ids})
        UNION ALL
        SELECT 'JOIN_COLUMN', RULE_ID, OBJECT_CONSTRUCT(*)
        FROM (
            SELECT j.RULE_ID AS RULE_ID, jc.*
            FROM {tbl('RULE_JOIN_COLUMN')} jc
            JOIN {tbl('RULE_JOIN')} j ON j.JOIN_ID = jc.JOIN_ID
            WHERE j.RULE_ID IN ({ids})
        )
        UNION ALL
        SELECT 'JOIN_FILTER', RULE_ID, OBJECT_CONSTRUCT(*)
        FROM (
            SELECT j.RULE_ID AS RULE_ID, jf.*
            FROM {tbl('RULE_JOIN_FILTER')} jf
            JOIN {tbl('RULE_JOIN')} j ON j.JOIN_ID = jf.JOIN_ID
            WHERE j.RULE_ID IN ({ids})
        )
        UNION ALL
        SELECT 'MAPPING', RULE_ID, OBJECT_CONSTRUCT(*)
        FROM {tbl('RULE_MAPPING')} WHERE RULE_ID IN ({ids})
        UNION ALL
        SELECT 'REFERENCE', RULE_ID, OBJECT_CONSTRUCT(*)
        FROM {tbl('RULE_REFERENCE')} WHERE RULE_ID IN ({ids})
    """


def load_bundles(rule_ids: List[int]) -> List[Dict]:
    """
    Returns one nested bundle per rule, ready for fdd_render.render_rule().

    Caps at 25 rules. The reading pages render a window of rules and the cap
    keeps one payload query bounded; callers that genuinely need every rule
    should use load_bundles_all(), which batches instead of truncating.
    """
    rule_ids = [int(r) for r in dict.fromkeys(rule_ids)][:25]
    if not rule_ids:
        return []
    ids = ",".join(str(r) for r in rule_ids)

    try:
        df = cached_read(_bundle_sql(ids))
    except Exception:
        df = _load_bundles_fallback(ids)

    if df is None or df.empty:
        return []

    grouped: Dict[int, Dict] = {
        rid: {"rule": {}, "conditions": [], "actions": [], "joins": [],
              "mappings": [], "references": [], "_joins_by_id": {}}
        for rid in rule_ids
    }

    for _, row in df.iterrows():
        try:
            rid = int(row["RULE_ID"])
        except (TypeError, ValueError):
            continue
        bundle = grouped.setdefault(rid, {"rule": {}, "conditions": [], "actions": [],
                                          "joins": [], "mappings": [], "references": [],
                                          "_joins_by_id": {}})
        payload = row["PAYLOAD"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                continue
        if not isinstance(payload, dict):
            continue

        part = str(row["PART"])
        if part == "RULE":
            bundle["rule"].update(payload)
        elif part == "CONDITION":
            bundle["conditions"].append(payload)
        elif part == "ACTION":
            bundle["actions"].append(payload)
        elif part == "JOIN":
            entry = {"join": payload, "columns": [], "filters": []}
            bundle["_joins_by_id"][str(payload.get("JOIN_ID"))] = entry
            bundle["joins"].append(entry)
        elif part == "JOIN_COLUMN":
            entry = bundle["_joins_by_id"].get(str(payload.get("JOIN_ID")))
            if entry:
                entry["columns"].append(payload)
        elif part == "JOIN_FILTER":
            entry = bundle["_joins_by_id"].get(str(payload.get("JOIN_ID")))
            if entry:
                entry["filters"].append(payload)
        elif part == "MAPPING":
            bundle["mappings"].append(payload)
        elif part == "REFERENCE":
            bundle["references"].append(payload)

    ordered = []
    for rid in rule_ids:
        bundle = grouped.get(rid)
        if bundle and (bundle["rule"] or bundle["conditions"] or bundle["actions"]):
            bundle.pop("_joins_by_id", None)
            ordered.append(bundle)
    return ordered


def load_bundles_all(rule_ids: List[int], batch_size: int = 25) -> List[Dict]:
    """
    Every bundle for every id, batched around load_bundles()'s 25-rule cap.

    Bulk test case generation needs all of them: passing 59 ids straight to
    load_bundles() silently returned the first 25 and dropped the rest, which
    made rules past the cap look untestable rather than unread.
    """
    ids = [int(r) for r in dict.fromkeys(rule_ids)]
    bundles: List[Dict] = []
    for start in range(0, len(ids), batch_size):
        bundles.extend(load_bundles(ids[start:start + batch_size]))
    return bundles


def _load_bundles_fallback(ids: str) -> pd.DataFrame:
    """If OBJECT_CONSTRUCT or a table is unavailable, degrade to per-part reads."""
    frames = []
    sources = {
        "RULE": f"SELECT * FROM {vw('VW_RULE_SUMMARY')} WHERE RULE_ID IN ({ids})",
        "CONDITION": f"SELECT * FROM {tbl('RULE_CONDITION')} WHERE RULE_ID IN ({ids})",
        "ACTION": f"SELECT * FROM {tbl('RULE_ACTION')} WHERE RULE_ID IN ({ids})",
        "JOIN": f"SELECT * FROM {tbl('RULE_JOIN')} WHERE RULE_ID IN ({ids})",
        "JOIN_COLUMN": (f"SELECT j.RULE_ID, jc.* FROM {tbl('RULE_JOIN_COLUMN')} jc "
                        f"JOIN {tbl('RULE_JOIN')} j ON j.JOIN_ID = jc.JOIN_ID "
                        f"WHERE j.RULE_ID IN ({ids})"),
        "JOIN_FILTER": (f"SELECT j.RULE_ID, jf.* FROM {tbl('RULE_JOIN_FILTER')} jf "
                        f"JOIN {tbl('RULE_JOIN')} j ON j.JOIN_ID = jf.JOIN_ID "
                        f"WHERE j.RULE_ID IN ({ids})"),
        "MAPPING": f"SELECT * FROM {tbl('RULE_MAPPING')} WHERE RULE_ID IN ({ids})",
        "REFERENCE": f"SELECT * FROM {tbl('RULE_REFERENCE')} WHERE RULE_ID IN ({ids})",
    }
    for part, sql in sources.items():
        try:
            df = cached_read(sql)
        except Exception:
            continue
        if df.empty:
            continue
        frames.append(pd.DataFrame({
            "PART": part,
            "RULE_ID": df["RULE_ID"],
            "PAYLOAD": [json.dumps({k: (None if pd.isna(v) else v) for k, v in rec.items()}, default=str)
                        for rec in df.to_dict(orient="records")],
        }))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# ------------------------------------------------------------------
# Writes
# ------------------------------------------------------------------
def next_rule_number(rule_set_id: int) -> str:
    """
    Suggests the next rule number in a rule set. BAs use all sorts of
    schemes here (1, 2.1, 3A, IL, NOTE1, ...), so this only looks at the
    leading integer of each existing number (2.1 -> 2, 3A -> 3, IL -> ignored)
    and proposes one past the highest it finds. It is a starting point, not
    a constraint - the BA can still type anything over it.
    """
    df = cached_read(f"SELECT RULE_NUMBER FROM {tbl('RULE')} WHERE RULE_SET_ID = {sql_int(rule_set_id)}")
    highest = 0
    for value in df["RULE_NUMBER"].dropna().astype(str) if not df.empty else []:
        match = re.match(r"^(\d+)", value.strip())
        if match:
            highest = max(highest, int(match.group(1)))
    return str(highest + 1)


def save_rule(proposal: Dict, manage_transaction: bool = True,
              performed_by: Optional[str] = None) -> int:
    """
    Persists a validated proposal. One INSERT per table, wrapped in a
    transaction so a half-written rule can never reach the repository.

    manage_transaction=False lets a caller own an outer transaction spanning
    several saves - a bulk FDD import wanting all-or-nothing across many rules.
    This flag is necessary rather than convenient: Snowflake does not nest
    transactions, so an inner BEGIN is ignored while an inner COMMIT commits the
    *outer* transaction. Leaving this function to manage its own transaction
    inside someone else's would silently end their transaction after the first
    rule, and every later write would auto-commit individually.

    Ids are reserved inside the transaction. That is safe now that reserve_ids()
    reads a sequence (NEXTVAL is a query) rather than SELECT MAX(id)+1.
    """
    conditions = proposal.get("conditions", []) or []
    actions = proposal.get("actions", []) or []
    joins = proposal.get("joins", []) or []
    join_columns = [(ji, jc) for ji, j in enumerate(joins) for jc in (j.get("join_columns") or [])]
    join_filters = [(ji, jf) for ji, j in enumerate(joins) for jf in (j.get("filters") or [])]

    if manage_transaction:
        execute("BEGIN")
    try:
        rule_id = reserve_ids("RULE", "RULE_ID", 1)[0]
        condition_ids = reserve_ids("RULE_CONDITION", "CONDITION_ID", len(conditions))
        action_ids = reserve_ids("RULE_ACTION", "ACTION_ID", len(actions))
        join_ids = reserve_ids("RULE_JOIN", "JOIN_ID", len(joins))
        join_column_ids = reserve_ids("RULE_JOIN_COLUMN", "JOIN_COLUMN_ID", len(join_columns))
        join_filter_ids = reserve_ids("RULE_JOIN_FILTER", "FILTER_ID", len(join_filters))

        insert_rows(
            "RULE",
            ["RULE_ID", "RULE_SET_ID", "RULE_NUMBER", "RULE_NAME", "RULE_CATEGORY",
             "BUSINESS_DESCRIPTION", "PRIORITY", "RULE_STATUS", "ACTIVE_IND",
             "CREATED_BY"],
            [[str(rule_id), sql_int(proposal.get("rule_set_id")),
              sql_value(proposal.get("rule_number")), sql_value(proposal.get("rule_name")),
              sql_value(proposal.get("rule_category")),
              sql_value(proposal.get("business_description")),
              sql_int(proposal.get("priority"), 99),
              sql_value(proposal.get("rule_status") or DRAFT_STATUS), "TRUE",
              _actor(performed_by)]],
        )

        insert_rows(
            "RULE_CONDITION",
            ["CONDITION_ID", "RULE_ID", "CONDITION_GROUP", "CONDITION_SEQ", "FIELD_NAME",
             "OPERATOR", "FIELD_VALUE", "LOGICAL_OPERATOR", "CONDITION_DESCRIPTION"],
            [[str(condition_ids[i]), str(rule_id), sql_int(c.get("condition_group"), 1),
              str(i + 1), sql_value(c.get("field_name")), sql_value(c.get("operator")),
              sql_value(c.get("field_value")), sql_value(c.get("logical_operator")),
              sql_value(c.get("condition_description"))]
             for i, c in enumerate(conditions)],
        )

        insert_rows(
            "RULE_ACTION",
            ["ACTION_ID", "RULE_ID", "ACTION_SEQ", "TARGET_FIELD", "ACTION_TYPE",
             "ACTION_VALUE", "ACTION_DESCRIPTION"],
            [[str(action_ids[i]), str(rule_id), str(i + 1), sql_value(a.get("target_field")),
              sql_value(a.get("action_type")), sql_value(a.get("action_value")),
              sql_value(a.get("action_description"))]
             for i, a in enumerate(actions)],
        )

        insert_rows(
            "RULE_JOIN",
            ["JOIN_ID", "RULE_ID", "JOIN_SEQUENCE", "LEFT_OBJECT", "RIGHT_OBJECT",
             "JOIN_TYPE", "JOIN_PURPOSE"],
            [[str(join_ids[i]), str(rule_id), str(i + 1), sql_value(j.get("left_object")),
              sql_value(j.get("right_object")), sql_value(j.get("join_type") or "INNER"),
              sql_value(j.get("join_purpose"))]
             for i, j in enumerate(joins)],
        )

        insert_rows(
            "RULE_JOIN_COLUMN",
            ["JOIN_COLUMN_ID", "JOIN_ID", "LEFT_COLUMN", "RIGHT_COLUMN"],
            [[str(join_column_ids[i]), str(join_ids[ji]),
              sql_value(jc.get("left_column")), sql_value(jc.get("right_column"))]
             for i, (ji, jc) in enumerate(join_columns)],
        )

        insert_rows(
            "RULE_JOIN_FILTER",
            ["FILTER_ID", "JOIN_ID", "FILTER_SEQ", "FILTER_COLUMN", "FILTER_OPERATOR",
             "FILTER_VALUE", "LOGICAL_OPERATOR"],
            [[str(join_filter_ids[i]), str(join_ids[ji]), str(i + 1),
              sql_value(jf.get("filter_column")), sql_value(jf.get("filter_operator")),
              sql_value(jf.get("filter_value")), sql_value(jf.get("logical_operator"))]
             for i, (ji, jf) in enumerate(join_filters)],
        )

        log_rule_changes(rule_id, [
            ("CREATED", None, proposal.get("rule_name") or f"Rule {proposal.get('rule_number', rule_id)}"),
        ], performed_by=performed_by)
        if manage_transaction:
            execute("COMMIT")
    except Exception:
        if manage_transaction:
            execute("ROLLBACK")
        # When someone else owns the transaction, leave it open and let them
        # decide: Snowflake keeps a transaction active after a failed statement,
        # so rolling back here would silently discard their earlier work too.
        raise

    log_audit_event("RULE", rule_id, "RULE_CREATED",
                    f"Rule '{proposal.get('rule_name')}' (#{proposal.get('rule_number')})")
    bump_cache()
    return rule_id


def update_rule(rule_id: int, proposal: Dict, previous: Optional[Dict] = None,
                performed_by: Optional[str] = None, manage_transaction: bool = True) -> None:
    """
    Overwrites an existing rule: updates the RULE row, then replaces its
    conditions, actions and joins (delete-then-insert) so the child rows
    always match the confirmed proposal exactly. One transaction, so a
    half-written update can never reach the repository.

    `previous` is the rule as it was before this update (same shape as
    `proposal`, e.g. requirement_compiler._bundle_to_proposal()). When given,
    every changed field is logged to RULE_CHANGE_HISTORY as part of the same
    transaction.

    Editing a rule sends it back to DRAFT when its content changes - see
    status_after_edit(). For an ACCEPTED rule that clears the approval: this function
    can rewrite every condition and action, so keeping the stamp would make the status
    a lie. For CHANGES_REQUESTED it is the "addressed, review it again" signal. The
    status on `proposal` is ignored, deliberately; status belongs to set_rule_status().
    """
    rule_id = int(rule_id)
    conditions = proposal.get("conditions", []) or []
    actions = proposal.get("actions", []) or []
    joins = proposal.get("joins", []) or []
    join_columns = [(ji, jc) for ji, j in enumerate(joins) for jc in (j.get("join_columns") or [])]
    join_filters = [(ji, jf) for ji, j in enumerate(joins) for jf in (j.get("filters") or [])]

    # Status comes from the row and the policy in status_after_edit(), never from the
    # proposal. The proposal's rule_status is an echo of the stored status on Update
    # and a replay from history on rollback - trusting it is how an edited ACCEPTED
    # rule used to stay ACCEPTED, and how a rollback could restore an acceptance with
    # no approver check.
    stored = current_rule_status(rule_id)
    changed = content_changed(previous, proposal)
    status = status_after_edit(stored, changed)
    before = normalise_status(stored)
    reverted_from = before if (status != before
                               and before in (ACCEPTED_STATUS, CHANGES_REQUESTED_STATUS)) else None
    # Keep the proposal honest for the history diff below, which would otherwise
    # record the echoed status as though it had been written.
    proposal = dict(proposal)
    proposal["rule_status"] = status

    condition_ids = reserve_ids("RULE_CONDITION", "CONDITION_ID", len(conditions))
    action_ids = reserve_ids("RULE_ACTION", "ACTION_ID", len(actions))
    join_ids = reserve_ids("RULE_JOIN", "JOIN_ID", len(joins))
    join_column_ids = reserve_ids("RULE_JOIN_COLUMN", "JOIN_COLUMN_ID", len(join_columns))
    join_filter_ids = reserve_ids("RULE_JOIN_FILTER", "FILTER_ID", len(join_filters))

    if manage_transaction:  # see save_rule(): Snowflake does not nest transactions
        execute("BEGIN")
    try:
        execute(f"""
            UPDATE {tbl('RULE')}
            SET RULE_NAME = {sql_value(proposal.get('rule_name'))},
                RULE_CATEGORY = {sql_value(proposal.get('rule_category'))},
                BUSINESS_DESCRIPTION = {sql_value(proposal.get('business_description'))},
                PRIORITY = {sql_int(proposal.get('priority'), 99)},
                RULE_STATUS = {sql_value(status)},
                APPROVED_BY = {'NULL' if reverted_from else 'APPROVED_BY'},
                APPROVED_TS = {'NULL' if reverted_from else 'APPROVED_TS'},
                UPDATED_BY = {_actor(performed_by)}, UPDATED_TS = CURRENT_TIMESTAMP()
            WHERE RULE_ID = {sql_int(rule_id)}
        """)

        execute(f"""
            DELETE FROM {tbl('RULE_JOIN_COLUMN')}
            WHERE JOIN_ID IN (SELECT JOIN_ID FROM {tbl('RULE_JOIN')} WHERE RULE_ID = {sql_int(rule_id)})
        """)
        execute(f"""
            DELETE FROM {tbl('RULE_JOIN_FILTER')}
            WHERE JOIN_ID IN (SELECT JOIN_ID FROM {tbl('RULE_JOIN')} WHERE RULE_ID = {sql_int(rule_id)})
        """)
        execute(f"DELETE FROM {tbl('RULE_JOIN')} WHERE RULE_ID = {sql_int(rule_id)}")
        execute(f"DELETE FROM {tbl('RULE_CONDITION')} WHERE RULE_ID = {sql_int(rule_id)}")
        execute(f"DELETE FROM {tbl('RULE_ACTION')} WHERE RULE_ID = {sql_int(rule_id)}")

        insert_rows(
            "RULE_CONDITION",
            ["CONDITION_ID", "RULE_ID", "CONDITION_GROUP", "CONDITION_SEQ", "FIELD_NAME",
             "OPERATOR", "FIELD_VALUE", "LOGICAL_OPERATOR", "CONDITION_DESCRIPTION"],
            [[str(condition_ids[i]), str(rule_id), sql_int(c.get("condition_group"), 1),
              str(i + 1), sql_value(c.get("field_name")), sql_value(c.get("operator")),
              sql_value(c.get("field_value")), sql_value(c.get("logical_operator")),
              sql_value(c.get("condition_description"))]
             for i, c in enumerate(conditions)],
        )

        insert_rows(
            "RULE_ACTION",
            ["ACTION_ID", "RULE_ID", "ACTION_SEQ", "TARGET_FIELD", "ACTION_TYPE",
             "ACTION_VALUE", "ACTION_DESCRIPTION"],
            [[str(action_ids[i]), str(rule_id), str(i + 1), sql_value(a.get("target_field")),
              sql_value(a.get("action_type")), sql_value(a.get("action_value")),
              sql_value(a.get("action_description"))]
             for i, a in enumerate(actions)],
        )

        insert_rows(
            "RULE_JOIN",
            ["JOIN_ID", "RULE_ID", "JOIN_SEQUENCE", "LEFT_OBJECT", "RIGHT_OBJECT",
             "JOIN_TYPE", "JOIN_PURPOSE"],
            [[str(join_ids[i]), str(rule_id), str(i + 1), sql_value(j.get("left_object")),
              sql_value(j.get("right_object")), sql_value(j.get("join_type") or "INNER"),
              sql_value(j.get("join_purpose"))]
             for i, j in enumerate(joins)],
        )

        insert_rows(
            "RULE_JOIN_COLUMN",
            ["JOIN_COLUMN_ID", "JOIN_ID", "LEFT_COLUMN", "RIGHT_COLUMN"],
            [[str(join_column_ids[i]), str(join_ids[ji]),
              sql_value(jc.get("left_column")), sql_value(jc.get("right_column"))]
             for i, (ji, jc) in enumerate(join_columns)],
        )

        insert_rows(
            "RULE_JOIN_FILTER",
            ["FILTER_ID", "JOIN_ID", "FILTER_SEQ", "FILTER_COLUMN", "FILTER_OPERATOR",
             "FILTER_VALUE", "LOGICAL_OPERATOR"],
            [[str(join_filter_ids[i]), str(join_ids[ji]), str(i + 1),
              sql_value(jf.get("filter_column")), sql_value(jf.get("filter_operator")),
              sql_value(jf.get("filter_value")), sql_value(jf.get("logical_operator"))]
             for i, (ji, jf) in enumerate(join_filters)],
        )
        if previous is not None:
            log_rule_changes(rule_id, _diff_scalars(previous, proposal) + _diff_sections(previous, proposal),
                             performed_by=performed_by)
        if reverted_from:
            # With a `previous`, _diff_scalars above has already recorded the status
            # change; logging it again would show two transitions for one edit.
            changes = [] if previous is not None else [("RULE_STATUS", reverted_from, status)]
            changes.append((
                "APPROVAL_CLEARED" if reverted_from == ACCEPTED_STATUS else "REVIEW_ADDRESSED",
                None,
                "Edited after acceptance, so it needs accepting again."
                if reverted_from == ACCEPTED_STATUS else
                "Edited after changes were requested, so it is ready for review again."))
            log_rule_changes(rule_id, changes, performed_by=performed_by)
        if manage_transaction:
            execute("COMMIT")
    except Exception:
        if manage_transaction:
            execute("ROLLBACK")
        raise

    log_audit_event("RULE", rule_id, "RULE_UPDATED",
                    f"Rule '{proposal.get('rule_name')}' updated"
                    + (f" (returned to DRAFT from {reverted_from})" if reverted_from else ""),
                    performed_by=performed_by)
    bump_cache()


# ------------------------------------------------------------------
# Retire and restore (soft delete)
# ------------------------------------------------------------------
# A rule is never physically deleted. RULE_ID is referenced by RULE_CONDITION,
# RULE_ACTION, RULE_JOIN, RULE_MAPPING, RULE_REFERENCE, RULE_TEST_CASE and
# RULE_CHANGE_HISTORY with no cascade, and db.reserve_ids() hands out MAX(id)+1
# - so a hard DELETE both orphans child rows and lets a future rule inherit the
# dead rule's id and its history. Retiring keeps the evidence and is reversible.
RETIRED_STATUS = "RETIRED"
DRAFT_STATUS = "DRAFT"
ACCEPTED_STATUS = "ACCEPTED"
CHANGES_REQUESTED_STATUS = "CHANGES_REQUESTED"

# Only DRAFT and RETIRED existed before the approval workflow; "ACTIVE" appeared in
# restore_rule's default and nowhere else, so it is accepted on the way in and mapped
# to DRAFT rather than becoming a fifth state nothing else understands.
LEGACY_STATUS_ALIASES = {"ACTIVE": DRAFT_STATUS, "": DRAFT_STATUS, "REJECTED": CHANGES_REQUESTED_STATUS}

WORKFLOW_STATUSES = (DRAFT_STATUS, CHANGES_REQUESTED_STATUS, ACCEPTED_STATUS)

# Retiring and restoring are deliberately not in here: they have their own functions,
# their own audit actions and their own reversibility story.
LEGAL_TRANSITIONS = {
    DRAFT_STATUS: {ACCEPTED_STATUS, CHANGES_REQUESTED_STATUS},
    CHANGES_REQUESTED_STATUS: {ACCEPTED_STATUS, DRAFT_STATUS},
    ACCEPTED_STATUS: {DRAFT_STATUS, CHANGES_REQUESTED_STATUS},
}


def normalise_status(raw: Optional[str]) -> str:
    """Map whatever is on the row to one of the statuses the workflow knows."""
    value = (_text(raw) or "").upper()
    if value == RETIRED_STATUS:
        return RETIRED_STATUS
    value = LEGACY_STATUS_ALIASES.get(value, value)
    return value if value in WORKFLOW_STATUSES else DRAFT_STATUS


def content_changed(previous: Optional[Dict], proposal: Dict) -> bool:
    """Whether an edit changes what the rule says, ignoring its status.

    RULE_STATUS is excluded because it is the thing being decided, not part of the
    content. With no `previous` to compare against, assume it changed: demoting an
    approval unnecessarily is recoverable, keeping one over changed content is not.
    """
    if previous is None:
        return True
    scalars = [c for c in _diff_scalars(previous, proposal) if c[0] != "RULE_STATUS"]
    diffs = [c for c in scalars + _diff_sections(previous, proposal)
             if _text(c[1]) != _text(c[2])]
    return bool(diffs)


def status_after_edit(stored_status: Optional[str], changed: bool = True) -> str:
    """The status a rule holds after its content is edited.

    Status belongs to set_rule_status() - and to retire/restore - and to nothing
    else. Update and rollback are content edits, and the policy for those is fixed:

      RETIRED            stays retired. An edit must never resurrect a deleted rule.
      unchanged content  keeps its status, so confirming a no-op does not throw away
                         an approval nobody meant to withdraw.
      anything else      returns to DRAFT. For ACCEPTED that withdraws a sign-off the
                         new content no longer carries; for CHANGES_REQUESTED it is
                         the "addressed, review it again" signal.

    Callers must not pass the status a proposal carries. That value is an echo of the
    stored row (update), a replay from history (rollback) or model output, and
    trusting it is exactly how an edited rule stayed ACCEPTED.
    """
    current = normalise_status(stored_status)
    if current == RETIRED_STATUS:
        return RETIRED_STATUS
    if not changed:
        return current
    return DRAFT_STATUS


def current_rule_status(rule_id: int) -> Optional[str]:
    """The status held in the table right now, or None if the rule is gone."""
    df = run_query(f"SELECT RULE_STATUS FROM {tbl('RULE')} WHERE RULE_ID = {sql_int(rule_id)}")
    if df.empty:
        return None
    return _text(df.iloc[0]["RULE_STATUS"])


def retire_rule(rule_id: int, reason: Optional[str] = None,
                performed_by: Optional[str] = None) -> None:
    """
    Soft-deletes a rule: it stops appearing in the catalog, the FDD and the
    LLM context, while its definition, history and test cases stay intact so
    the decision can be explained later and undone.
    """
    rule_id = int(rule_id)
    previous = current_rule_status(rule_id)
    if previous is None:
        raise ValueError(f"Rule {rule_id} does not exist.")
    if previous == RETIRED_STATUS:
        return

    execute("BEGIN")
    try:
        execute(f"""
            UPDATE {tbl('RULE')}
            SET RULE_STATUS = {sql_value(RETIRED_STATUS)},
                ACTIVE_IND = FALSE,
                EFFECTIVE_END_DT = CURRENT_DATE(),
                UPDATED_BY = {_actor(performed_by)}, UPDATED_TS = CURRENT_TIMESTAMP()
            WHERE RULE_ID = {sql_int(rule_id)}
        """)
        log_rule_changes(rule_id, [
            ("RULE_STATUS", previous, RETIRED_STATUS),
            ("RETIRED", None, _text(reason) or "Retired from the repository."),
        ], performed_by=performed_by)
        execute("COMMIT")
    except Exception:
        execute("ROLLBACK")
        raise

    log_audit_event("RULE", rule_id, "RULE_RETIRED",
                    _text(reason) or f"Rule {rule_id} retired (was {previous})",
                    performed_by=performed_by)
    bump_cache()


def restore_rule(rule_id: int, status: str = "ACTIVE",
                 performed_by: Optional[str] = None) -> None:
    """Brings a retired rule back, the exact inverse of retire_rule()."""
    rule_id = int(rule_id)
    previous = current_rule_status(rule_id)
    if previous is None:
        raise ValueError(f"Rule {rule_id} does not exist.")
    target = _text(status) or "ACTIVE"

    execute("BEGIN")
    try:
        execute(f"""
            UPDATE {tbl('RULE')}
            SET RULE_STATUS = {sql_value(target)},
                ACTIVE_IND = TRUE,
                EFFECTIVE_END_DT = NULL,
                UPDATED_BY = {_actor(performed_by)}, UPDATED_TS = CURRENT_TIMESTAMP()
            WHERE RULE_ID = {sql_int(rule_id)}
        """)
        log_rule_changes(rule_id, [
            ("RULE_STATUS", previous, target),
            ("RESTORED", None, f"Restored to {target} from {previous}."),
        ], performed_by=performed_by)
        execute("COMMIT")
    except Exception:
        execute("ROLLBACK")
        raise

    log_audit_event("RULE", rule_id, "RULE_RESTORED",
                    f"Rule {rule_id} restored to {target}", performed_by=performed_by)
    bump_cache()


def list_retired_rules() -> pd.DataFrame:
    """Retired rules, most recently retired first, for the restore picker."""
    return cached_read(f"""
        SELECT v.RULE_ID, v.RULE_NUMBER, v.RULE_NAME, v.RULE_CATEGORY,
               v.RULE_SET_NAME, v.MODULE_NAME, v.TARGET_BUSINESS_OBJECT,
               r.UPDATED_BY AS RETIRED_BY, r.UPDATED_TS AS RETIRED_TS
        FROM {vw('VW_RULE_SUMMARY')} v
        JOIN {tbl('RULE')} r ON r.RULE_ID = v.RULE_ID
        WHERE v.RULE_STATUS = {sql_value(RETIRED_STATUS)}
        ORDER BY r.UPDATED_TS DESC NULLS LAST, v.RULE_ID DESC
    """)


# ------------------------------------------------------------------
# Approval workflow
# ------------------------------------------------------------------
# Authorisation here is an app-level control, not a security boundary. The app runs
# execute_as OWNER, so CURRENT_USER() and CURRENT_ROLE() are the app owner's for every
# viewer - Snowflake RBAC cannot tell one stakeholder from another inside this app.
# Identity comes from st.user via current_viewer(), which a determined person could
# not forge through the UI but which is not enforced by the database. Say so plainly
# rather than letting anyone believe the grant model is doing the work.
def list_approvers(active_only: bool = True) -> pd.DataFrame:
    """Everyone who may accept rules, with the team they may accept for."""
    columns = ["APPROVER_ID", "APPROVER_NAME", "TEAM_ID", "TEAM_NAME",
               "DESCRIPTION", "ACTIVE_IND"]
    try:
        where = "WHERE COALESCE(a.ACTIVE_IND, TRUE) = TRUE" if active_only else ""
        return cached_read(f"""
            SELECT a.APPROVER_ID, a.APPROVER_NAME, a.TEAM_ID, t.TEAM_NAME,
                   a.DESCRIPTION, a.ACTIVE_IND
            FROM {tbl('RULE_APPROVER')} a
            LEFT JOIN {tbl('TEAM')} t ON t.TEAM_ID = a.TEAM_ID
            {where}
            ORDER BY a.APPROVER_NAME
        """)
    except Exception:
        return pd.DataFrame(columns=columns)


def add_approver(approver_name: str, team_id: Optional[int] = None,
                 description: str = "", performed_by: Optional[str] = None) -> int:
    name = _text(approver_name)
    if not name:
        raise ValueError("An approver needs a name.")
    approver_id = reserve_ids("RULE_APPROVER", "APPROVER_ID", 1)[0]
    insert_rows(
        "RULE_APPROVER",
        ["APPROVER_ID", "APPROVER_NAME", "TEAM_ID", "DESCRIPTION", "ACTIVE_IND"],
        [[str(approver_id), sql_value(name),
          "NULL" if team_id is None else sql_int(team_id),
          sql_value(_text(description)), "TRUE"]],
    )
    log_audit_event("RULE_APPROVER", approver_id, "APPROVER_ADDED",
                    f"{name} may accept rules"
                    + (f" for team {team_id}" if team_id is not None else " for any team"),
                    performed_by=performed_by)
    bump_cache()
    return approver_id


def remove_approver(approver_id: int, performed_by: Optional[str] = None) -> None:
    execute(f"""
        UPDATE {tbl('RULE_APPROVER')} SET ACTIVE_IND = FALSE
        WHERE APPROVER_ID = {sql_int(approver_id)}
    """)
    log_audit_event("RULE_APPROVER", approver_id, "APPROVER_REMOVED",
                    "No longer an approver", performed_by=performed_by)
    bump_cache()


def approvals_are_restricted() -> bool:
    """True once at least one approver has been named.

    While the list is empty anyone may accept. That is deliberate: shipping a locked
    door with no key would leave the app with no way to accept its own first rule, and
    an admin screen guarded by the permission it grants is a bootstrap deadlock. The
    UI says which mode it is in, so "anyone can accept" is never a silent state.
    """
    approvers = list_approvers()
    return approvers is not None and not approvers.empty


def can_approve(viewer: Optional[str], team_id: Optional[int] = None) -> bool:
    """Whether this person is an approver at all, or for a given team.

    A UI hint, not the decision. The Requirements page uses it to decide whether to
    draw the Accept buttons, and it passes the team the page is scoped to. That scope
    is a viewing preference - "All teams" is None - so it must never authorise
    anything: set_rule_status() checks can_approve_rule() against the team that owns
    the rule instead.
    """
    approvers = list_approvers()
    if approvers is None or approvers.empty:
        return True                      # unrestricted until someone is named
    name = (_text(viewer) or "").upper()
    if not name:
        return False
    rows = approvers[approvers["APPROVER_NAME"].astype(str).str.upper() == name]
    if rows.empty:
        return False
    if team_id is None:
        return True
    # A NULL TEAM_ID means any team; otherwise it has to match.
    return bool(rows["TEAM_ID"].isna().any()
                or (rows["TEAM_ID"].dropna().astype(int) == int(team_id)).any())


def rule_team_id(rule_id: int) -> Optional[int]:
    """The team that owns a rule: RULE -> RULE_SET -> MODULE -> TEAM_ID."""
    df = cached_read(f"""
        SELECT m.TEAM_ID
        FROM {tbl('RULE')} r
        JOIN {tbl('RULE_SET')} rs ON rs.RULE_SET_ID = r.RULE_SET_ID
        LEFT JOIN {tbl('MODULE')} m ON m.MODULE_ID = rs.MODULE_ID
        WHERE r.RULE_ID = {sql_int(rule_id)}
    """)
    if df is None or df.empty or pd.isna(df.iloc[0]["TEAM_ID"]):
        return None
    return int(df.iloc[0]["TEAM_ID"])


def can_approve_team(viewer: Optional[str], owner_team_id: Optional[int]) -> bool:
    """The authorisation rule, given the team that owns the rule.

    Split out from can_approve_rule() so the Requirements page can apply exactly the
    same rule per rule from a module -> team map it already holds, instead of one
    database round trip per rule to find the owner.

    An owner of None means the rule's module has no team, and only an approver scoped
    to any team may act on it. Letting a team-limited approver in would make "no
    team" a way round the limit.
    """
    approvers = list_approvers()
    if approvers is None or approvers.empty:
        return True                      # unrestricted until someone is named
    name = (_text(viewer) or "").upper()
    if not name:
        return False
    rows = approvers[approvers["APPROVER_NAME"].astype(str).str.upper() == name]
    if rows.empty:
        return False
    if rows["TEAM_ID"].isna().any():
        return True                      # an any-team approver
    if owner_team_id is None:
        return False
    return bool((rows["TEAM_ID"].dropna().astype(int) == int(owner_team_id)).any())


def can_approve_rule(viewer: Optional[str], rule_id: int) -> bool:
    """The authorisation decision: may this person accept this particular rule?

    Checked against the team that owns the rule. The earlier check used the team the
    page happened to be filtered to, so an approver scoped to one team could accept
    any team's rules by switching the Team picker to "All teams".
    """
    return can_approve_team(viewer, rule_team_id(rule_id))


def set_rule_status(rule_id: int, target_status: str,
                    reason: Optional[str] = None,
                    performed_by: Optional[str] = None,
                    enforce_approver: bool = True,
                    team_id: Optional[int] = None) -> str:
    """Move one rule through the approval workflow. Returns the status it now holds.

    Refuses illegal moves rather than writing them, so the history cannot show a rule
    going straight from RETIRED to ACCEPTED. Retiring and restoring keep their own
    functions; this only covers DRAFT, CHANGES_REQUESTED and ACCEPTED.

    A reason is required to request changes. Without it the workflow throws away the
    only thing that made the review worth doing - RULE_REVIEW_COMMENT is module-scoped
    and has never held a row, so the reason goes into the rule's own change history
    where it sits beside the rule it is about.
    """
    rule_id = int(rule_id)
    target = normalise_status(target_status)
    previous_raw = current_rule_status(rule_id)
    if previous_raw is None:
        raise ValueError(f"Rule {rule_id} does not exist.")
    previous = normalise_status(previous_raw)

    if target == previous:
        return previous
    if previous == RETIRED_STATUS:
        raise ValueError("That rule is retired. Restore it before changing its status.")
    if target == RETIRED_STATUS:
        raise ValueError("Use retire_rule() to retire a rule.")
    if target not in LEGAL_TRANSITIONS.get(previous, set()):
        raise ValueError(f"Cannot go from {previous} to {target}.")
    if target == CHANGES_REQUESTED_STATUS and not _text(reason):
        raise ValueError("Say what needs changing — the reason is the point of the review.")
    # Accepting, and withdrawing an acceptance, are the approver's decisions. Both are
    # checked. Withdrawing was not, so anyone could silently undo the product owner's
    # sign-off with "Return to draft" and no reason. Raising a problem stays open to
    # everyone - ACCEPTED -> CHANGES_REQUESTED - because it cannot happen silently: a
    # reason is required and recorded.
    approver_decision = (target == ACCEPTED_STATUS
                         or (previous == ACCEPTED_STATUS and target == DRAFT_STATUS))
    if approver_decision and enforce_approver \
            and not can_approve_rule(performed_by, rule_id):
        # Deliberately not the caller's team_id: that is the page's Team filter, and
        # "All teams" passes None. Authorising against it let a team-scoped approver
        # accept any team's rules by changing a dropdown.
        owner = rule_team_id(rule_id)
        raise PermissionError(
            f"{performed_by or 'This viewer'} is not listed as an approver"
            + (f" for team {owner}, which owns this rule." if owner is not None
               else " for rules with no owning team."))

    who = _actor(performed_by)
    accepting = target == ACCEPTED_STATUS
    execute("BEGIN")
    try:
        execute(f"""
            UPDATE {tbl('RULE')}
            SET RULE_STATUS = {sql_value(target)},
                APPROVED_BY = {who if accepting else 'NULL'},
                APPROVED_TS = {'CURRENT_TIMESTAMP()' if accepting else 'NULL'},
                UPDATED_BY = {who}, UPDATED_TS = CURRENT_TIMESTAMP()
            WHERE RULE_ID = {sql_int(rule_id)}
        """)
        changes = [("RULE_STATUS", previous, target)]
        if _text(reason):
            changes.append((f"REVIEW_{target}", None, _text(reason)))
        log_rule_changes(rule_id, changes, performed_by=performed_by)
        execute("COMMIT")
    except Exception:
        execute("ROLLBACK")
        raise

    log_audit_event("RULE", rule_id, f"RULE_{target}",
                    _text(reason) or f"Status {previous} -> {target}",
                    performed_by=performed_by)
    bump_cache()
    return target


def set_rule_statuses(rule_ids: List[int], target_status: str,
                      reason: Optional[str] = None,
                      performed_by: Optional[str] = None,
                      team_id: Optional[int] = None) -> Dict[str, List]:
    """Bulk version, so a product owner can clear a rule set in one action.

    Each rule is moved on its own rather than in one statement, because a rule already
    in the target status, or retired, must be skipped rather than failing the batch.
    Returns {"changed": [...], "skipped": [(id, why), ...]}.

    Two kinds of authorisation failure, handled differently on purpose. Someone who is
    not an approver at all is refused before anything is written, so the batch cannot
    half-succeed. Someone who is an approver but not for the team owning one of these
    rules gets that rule skipped with the reason - authorisation is per rule now, and
    raising part-way would accept some rules while reporting only the failure.
    """
    target = normalise_status(target_status)
    if target == ACCEPTED_STATUS and not can_approve(performed_by):
        raise PermissionError(f"{performed_by or 'This viewer'} is not listed as an approver.")

    changed: List[int] = []
    skipped: List[tuple] = []
    for rule_id in rule_ids:
        try:
            before = normalise_status(current_rule_status(rule_id))
            after = set_rule_status(rule_id, target_status, reason=reason,
                                    performed_by=performed_by, team_id=team_id)
            if after != before:
                changed.append(int(rule_id))
            else:
                skipped.append((int(rule_id), f"already {after}"))
        except Exception as exc:         # PermissionError included - see above
            skipped.append((int(rule_id), str(exc)))
    return {"changed": changed, "skipped": skipped}


def rules_available_for_column(object_id: int) -> pd.DataFrame:
    """Live rules on the rule sets that write to this object, for the link picker.

    Scoped to this object's rule sets for the same reason get_rules_by_target_column
    is: a rule that populates ACTIVE_IND on a different object has no business being
    offered here. Retired rules are left out - linking a column to a retired rule
    would claim coverage that the FDD no longer contains.
    """
    columns = ["RULE_ID", "RULE_NUMBER", "RULE_NAME", "RULE_CATEGORY",
               "RULE_SET_NAME", "RULE_STATUS", "ACTION_COUNT"]
    try:
        return cached_read(f"""
            SELECT r.RULE_ID, r.RULE_NUMBER, r.RULE_NAME, r.RULE_CATEGORY,
                   rs.RULE_SET_NAME, r.RULE_STATUS,
                   COUNT(a.ACTION_ID) AS ACTION_COUNT
            FROM {tbl('RULE_SET')} rs
            JOIN {tbl('RULE')} r ON r.RULE_SET_ID = rs.RULE_SET_ID
            LEFT JOIN {tbl('RULE_ACTION')} a ON a.RULE_ID = r.RULE_ID
            WHERE rs.TARGET_OBJECT_ID = {sql_int(object_id)}
              AND COALESCE(r.RULE_STATUS, '') <> {sql_value(RETIRED_STATUS)}
              AND COALESCE(r.ACTIVE_IND, TRUE) = TRUE
            GROUP BY r.RULE_ID, r.RULE_NUMBER, r.RULE_NAME, r.RULE_CATEGORY,
                     rs.RULE_SET_NAME, r.RULE_STATUS
            ORDER BY r.RULE_NUMBER, r.RULE_ID
        """)
    except Exception:
        return pd.DataFrame(columns=columns)


def link_column_to_rule(rule_id: int, column_name: str,
                        action_type: str = "DERIVE",
                        action_value: str = "",
                        action_description: str = "",
                        performed_by: Optional[str] = None) -> bool:
    """Record that an existing rule also populates this column.

    An FDD numbers its rules per hop, and one rule often sets several columns. The
    column view is derived from RULE_ACTION.TARGET_FIELD, so the way to say "rule 3
    covers this column too" is to give rule 3 another action - not to write a second
    rule saying the same thing, which is what a column-first page pushes you towards.

    Appends one RULE_ACTION row. Idempotent: a rule that already targets the column
    is left alone rather than gaining a duplicate action.

    Deliberately does not go through update_rule(). update_rule replaces conditions,
    actions and joins wholesale from a compiled proposal, and would need the whole
    rule rebuilt to add one row. It follows the same status policy, though, through
    status_after_edit(): adding an action changes what the rule does, so an existing
    approval no longer covers it, and an outstanding change request counts as
    addressed.
    """
    rule_id = int(rule_id)
    column = (_text(column_name) or "").upper()
    if not column:
        raise ValueError("Name the column to link.")

    existing = cached_read(f"""
        SELECT 1 FROM {tbl('RULE_ACTION')}
        WHERE RULE_ID = {sql_int(rule_id)}
          AND UPPER(TARGET_FIELD) = {sql_value(column)} LIMIT 1
    """)
    if not existing.empty:
        return False

    seq_df = cached_read(f"""
        SELECT COALESCE(MAX(ACTION_SEQ), 0) + 1 AS NEXT_SEQ
        FROM {tbl('RULE_ACTION')} WHERE RULE_ID = {sql_int(rule_id)}
    """)
    next_seq = int(seq_df.iloc[0]["NEXT_SEQ"]) if not seq_df.empty else 1
    action_id = reserve_ids("RULE_ACTION", "ACTION_ID", 1)[0]

    # Same policy as update_rule, so the two edit paths cannot disagree about what an
    # edit does to a rule's status.
    before = normalise_status(current_rule_status(rule_id))
    after = status_after_edit(before, changed=True)
    demote = after != before and before in (ACCEPTED_STATUS, CHANGES_REQUESTED_STATUS)

    execute("BEGIN")
    try:
        insert_rows(
            "RULE_ACTION",
            ["ACTION_ID", "RULE_ID", "ACTION_SEQ", "TARGET_FIELD", "ACTION_TYPE",
             "ACTION_VALUE", "ACTION_DESCRIPTION"],
            [[str(action_id), str(rule_id), str(next_seq), sql_value(column),
              sql_value((_text(action_type) or "DERIVE").upper()),
              sql_value(_text(action_value)), sql_value(_text(action_description))]],
        )
        if demote:
            execute(f"""
                UPDATE {tbl('RULE')}
                SET RULE_STATUS = {sql_value(after)},
                    APPROVED_BY = NULL, APPROVED_TS = NULL,
                    UPDATED_BY = {_actor(performed_by)}, UPDATED_TS = CURRENT_TIMESTAMP()
                WHERE RULE_ID = {sql_int(rule_id)}
            """)
        else:
            execute(f"""
                UPDATE {tbl('RULE')}
                SET UPDATED_BY = {_actor(performed_by)}, UPDATED_TS = CURRENT_TIMESTAMP()
                WHERE RULE_ID = {sql_int(rule_id)}
            """)
        changes = [("ACTIONS", None, f"Also populates {column}.")]
        if demote:
            changes += [
                ("RULE_STATUS", before, after),
                ("APPROVAL_CLEARED" if before == ACCEPTED_STATUS else "REVIEW_ADDRESSED",
                 None,
                 f"{column} was added to this rule after acceptance."
                 if before == ACCEPTED_STATUS else
                 f"{column} was added after changes were requested."),
            ]
        log_rule_changes(rule_id, changes, performed_by=performed_by)
        execute("COMMIT")
    except Exception:
        execute("ROLLBACK")
        raise

    log_audit_event("RULE", rule_id, "RULE_COLUMN_LINKED",
                    f"{column} linked to this rule"
                    + (f" (returned to DRAFT from {before})" if demote else ""),
                    performed_by=performed_by)
    bump_cache()
    return True


# ------------------------------------------------------------------
# Modules
# ------------------------------------------------------------------
def list_business_objects() -> pd.DataFrame:
    """Every business object, with its module (if any), for the manage-modules page."""
    return cached_read(f"""
        SELECT bo.OBJECT_ID, bo.OBJECT_NAME, bo.TECHNICAL_OBJECT_NAME, bo.OBJECT_TYPE,
               bo.SUBJECT_AREA, bo.MODULE_ID, m.MODULE_NAME
        FROM {tbl('BUSINESS_OBJECT')} bo
        LEFT JOIN {tbl('MODULE')} m ON bo.MODULE_ID = m.MODULE_ID
        ORDER BY bo.OBJECT_NAME
    """)


def list_rule_sets() -> pd.DataFrame:
    """Every rule set, with its module (if any), for the manage-modules page."""
    return cached_read(f"""
        SELECT rs.RULE_SET_ID, rs.RULE_SET_NAME, rs.SUBJECT_AREA, rs.MODULE_ID, m.MODULE_NAME
        FROM {tbl('RULE_SET')} rs
        LEFT JOIN {tbl('MODULE')} m ON rs.MODULE_ID = m.MODULE_ID
        ORDER BY rs.RULE_SET_NAME
    """)


def get_rule_sets_for_module(module_id: int) -> pd.DataFrame:
    """
    Every rule set assigned to a module, with the source/target technical object
    names it declares. Used by the Publish FDD export to know which rule sets
    (and therefore which objects) belong in the module's document.
    """
    columns = ["RULE_SET_ID", "RULE_SET_NAME", "SUBJECT_AREA", "DESCRIPTION",
               "SOURCE_OBJECT_ID", "TARGET_OBJECT_ID",
               "SOURCE_TECHNICAL_OBJECT", "TARGET_TECHNICAL_OBJECT"]
    try:
        return cached_read(f"""
            SELECT RULE_SET_ID, RULE_SET_NAME, SUBJECT_AREA, DESCRIPTION,
                   SOURCE_OBJECT_ID, TARGET_OBJECT_ID,
                   SOURCE_TECHNICAL_OBJECT, TARGET_TECHNICAL_OBJECT
            FROM {tbl('RULE_SET')}
            WHERE MODULE_ID = {sql_int(module_id)}
            ORDER BY RULE_SET_NAME
        """)
    except Exception:
        return pd.DataFrame(columns=columns)


def get_module_objects(module_id: int) -> pd.DataFrame:
    """
    Every business object relevant to a module: objects assigned to it directly,
    plus every object any of its rule sets reads from or writes to - including
    a common lookup table that belongs to a different module. This is the
    "required objects" list for the module's published FDD, which has to be
    everything a developer would need to build against, not only what happens
    to be filed under this module.
    """
    columns = ["OBJECT_ID", "OBJECT_NAME", "TECHNICAL_OBJECT_NAME", "OBJECT_TYPE",
               "MODULE_ID", "MODULE_NAME"]
    try:
        return cached_read(f"""
            WITH rs AS (
                SELECT RULE_SET_ID, SOURCE_OBJECT_ID, TARGET_OBJECT_ID
                FROM {tbl('RULE_SET')} WHERE MODULE_ID = {sql_int(module_id)}
            ),
            wanted AS (
                SELECT TARGET_OBJECT_ID AS OBJECT_ID FROM rs WHERE TARGET_OBJECT_ID IS NOT NULL
                UNION SELECT SOURCE_OBJECT_ID FROM rs WHERE SOURCE_OBJECT_ID IS NOT NULL
                UNION
                SELECT rss.OBJECT_ID FROM {tbl('RULE_SET_SOURCE')} rss
                JOIN rs ON rs.RULE_SET_ID = rss.RULE_SET_ID
                UNION
                SELECT OBJECT_ID FROM {tbl('BUSINESS_OBJECT')} WHERE MODULE_ID = {sql_int(module_id)}
            )
            SELECT bo.OBJECT_ID, bo.OBJECT_NAME, bo.TECHNICAL_OBJECT_NAME, bo.OBJECT_TYPE,
                   bo.MODULE_ID, m.MODULE_NAME
            FROM {tbl('BUSINESS_OBJECT')} bo
            LEFT JOIN {tbl('MODULE')} m ON bo.MODULE_ID = m.MODULE_ID
            WHERE bo.OBJECT_ID IN (SELECT OBJECT_ID FROM wanted)
            ORDER BY bo.OBJECT_NAME
        """)
    except Exception:
        return pd.DataFrame(columns=columns)


def find_active_rule_set(module_id: int, target_technical_object: str,
                         source_technical_object: str = "") -> Optional[int]:
    """An active rule set in this module for the same hop, if one exists.

    Matched on target and primary source, not on name: the Location re-import
    produced "Plant Root Table Transformation" twice with identical endpoints, and a
    second import of the same FDD should add to the hop that is already there. Two
    rule sets with the same target but different sources are different hops and are
    both kept. Returns None when the target does not resolve, so the caller creates.
    """
    target_id = _resolve_object_id(str(target_technical_object or ""))
    if target_id is None:
        return None
    source_id = _resolve_object_id(str(source_technical_object or ""))
    source_clause = (f"AND SOURCE_OBJECT_ID = {sql_int(source_id)}" if source_id is not None
                     else "AND SOURCE_OBJECT_ID IS NULL")
    df = cached_read(f"""
        SELECT RULE_SET_ID FROM {tbl('RULE_SET')}
        WHERE MODULE_ID = {sql_int(module_id)}
          AND TARGET_OBJECT_ID = {sql_int(target_id)}
          AND COALESCE(ACTIVE_IND, TRUE) = TRUE
          {source_clause}
        ORDER BY RULE_SET_ID DESC LIMIT 1
    """)
    return None if df.empty else int(df.iloc[0]["RULE_SET_ID"])


def list_module_rules(module_id: int) -> pd.DataFrame:
    """Every live hop of a module with its non-retired rules, for importing an FDD in parts.

    One row per rule; a hop with no rules still has one row (RULE_ID NULL) so it shows
    as saved. FDD_TEXT is the sentence the rule was imported from (RULE_CHANGE_HISTORY
    FDD_TEXT, latest), which is how a re-pasted part tells "already imported" from
    "changed in the FDD since".
    """
    return cached_read(f"""
        WITH fdd AS (
            SELECT RULE_ID, NEW_VALUE AS FDD_TEXT FROM {tbl('RULE_CHANGE_HISTORY')}
            WHERE CHANGE_TYPE = 'FDD_TEXT'
            QUALIFY ROW_NUMBER() OVER (PARTITION BY RULE_ID ORDER BY CHANGE_ID DESC) = 1)
        SELECT rs.RULE_SET_ID, rs.RULE_SET_NAME, rs.SOURCE_TECHNICAL_OBJECT,
               rs.TARGET_TECHNICAL_OBJECT, r.RULE_ID, r.RULE_NUMBER, r.RULE_NAME,
               r.RULE_STATUS, fdd.FDD_TEXT
        FROM {tbl('RULE_SET')} rs
        LEFT JOIN {tbl('RULE')} r ON r.RULE_SET_ID = rs.RULE_SET_ID
             AND COALESCE(r.RULE_STATUS, '') <> 'RETIRED'
        LEFT JOIN fdd ON fdd.RULE_ID = r.RULE_ID
        WHERE rs.MODULE_ID = {sql_int(module_id)} AND COALESCE(rs.ACTIVE_IND, TRUE) = TRUE
        ORDER BY rs.RULE_SET_ID, r.RULE_NUMBER
    """)


def find_module_by_name(module_name: str, team_id: Optional[int] = None) -> Optional[int]:
    """The id of an existing module with this name, ignoring case and outer spaces.

    Re-importing an FDD used to call create_module() unconditionally, so importing
    Location twice left two modules both called "Location", with every hop duplicated
    across them. Matching is scoped to the team when one is given, because two teams
    may legitimately own modules with the same name.
    """
    name = (_text(module_name) or "").strip().upper()
    if not name:
        return None
    team_clause = f"AND TEAM_ID = {sql_int(team_id)}" if team_id is not None else ""
    df = cached_read(f"""
        SELECT MODULE_ID FROM {tbl('MODULE')}
        WHERE UPPER(TRIM(MODULE_NAME)) = {sql_value(name)} {team_clause}
        ORDER BY MODULE_ID LIMIT 1
    """)
    return None if df.empty else int(df.iloc[0]["MODULE_ID"])


def create_module(module_name: str, module_code: str, description: str = "",
                  team_id: Optional[int] = None) -> int:
    module_id = reserve_ids("MODULE", "MODULE_ID", 1)[0]
    insert_rows(
        "MODULE",
        ["MODULE_ID", "MODULE_NAME", "MODULE_CODE", "DESCRIPTION", "TEAM_ID", "CREATED_BY"],
        [[str(module_id), sql_value(module_name), sql_value(module_code),
          sql_value(description), "NULL" if team_id is None else sql_int(team_id),
          _actor()]],
    )
    log_audit_event("MODULE", module_id, "MODULE_CREATED",
                    f"Created module '{module_name}' ({module_code})"
                    + (f" under team {team_id}" if team_id is not None else ""))
    bump_cache()
    return module_id


def rename_module(module_id: int, module_name: str, module_code: str, description: str = "") -> None:
    execute(f"""
        UPDATE {tbl('MODULE')}
        SET MODULE_NAME = {sql_value(module_name)},
            MODULE_CODE = {sql_value(module_code)},
            DESCRIPTION = {sql_value(description)},
            UPDATED_BY = {_actor()},
            UPDATED_TS = CURRENT_TIMESTAMP()
        WHERE MODULE_ID = {sql_int(module_id)}
    """)
    log_audit_event("MODULE", module_id, "MODULE_RENAMED",
                    f"Renamed to '{module_name}' ({module_code})")
    bump_cache()


def reassign_object_module(object_id: int, module_id: Optional[int]) -> None:
    module_sql = "NULL" if module_id is None else sql_int(module_id)
    execute(f"""
        UPDATE {tbl('BUSINESS_OBJECT')}
        SET MODULE_ID = {module_sql}, UPDATED_BY = {_actor()}, UPDATED_TS = CURRENT_TIMESTAMP()
        WHERE OBJECT_ID = {sql_int(object_id)}
    """)
    log_audit_event("BUSINESS_OBJECT", object_id, "OBJECT_REASSIGNED",
                    f"Moved to module {module_id or '(none)'}")
    bump_cache()


def adopt_abandoned_objects(object_ids, module_id: int) -> int:
    """Move objects to module_id when their owner has no live rules left (or no owner).

    A module re-imported under a new name reuses its objects, and they kept pointing
    at the retired module: the new module showed no objects and no lineage, since the
    lineage page scopes by object owner. Objects still used by a live module (shared
    ones like the calendar horizon view) are left with that owner.
    Returns how many were moved.
    """
    ids = sorted({int(i) for i in object_ids or () if i is not None})
    if not ids or module_id is None:
        return 0
    id_list = ", ".join(sql_int(i) for i in ids)
    moving = run_query(f"""
        SELECT bo.OBJECT_ID, bo.MODULE_ID
        FROM {tbl('BUSINESS_OBJECT')} bo
        WHERE bo.OBJECT_ID IN ({id_list})
          AND COALESCE(bo.MODULE_ID, -1) <> {sql_int(module_id)}
          -- "Had rules and all are retired", not "has no live rules": a module still
          -- being built (rule sets declared, no rules yet) keeps its objects.
          AND (bo.MODULE_ID IS NULL OR (
                EXISTS (SELECT 1 FROM {tbl('RULE_SET')} rs
                        JOIN {tbl('RULE')} r ON r.RULE_SET_ID = rs.RULE_SET_ID
                        WHERE rs.MODULE_ID = bo.MODULE_ID)
                AND NOT EXISTS (
                SELECT 1 FROM {tbl('RULE_SET')} rs
                JOIN {tbl('RULE')} r ON r.RULE_SET_ID = rs.RULE_SET_ID
                WHERE rs.MODULE_ID = bo.MODULE_ID
                  AND COALESCE(r.ACTIVE_IND, TRUE) AND COALESCE(r.RULE_STATUS, '') <> 'RETIRED')))
    """)
    if moving is None or moving.empty:
        return 0
    for _, row in moving.iterrows():
        execute(f"""
            UPDATE {tbl('BUSINESS_OBJECT')}
            SET MODULE_ID = {sql_int(module_id)}, UPDATED_BY = {_actor()},
                UPDATED_TS = CURRENT_TIMESTAMP()
            WHERE OBJECT_ID = {sql_int(row['OBJECT_ID'])}
        """)
        old = row["MODULE_ID"]
        log_audit_event("BUSINESS_OBJECT", int(row["OBJECT_ID"]), "OBJECT_REASSIGNED",
                        f"Moved to module {module_id} at import (module "
                        f"{'(none)' if pd.isna(old) else int(old)} has no live rules)")
    bump_cache()
    return len(moving)


def reassign_rule_set_module(rule_set_id: int, module_id: Optional[int]) -> None:
    module_sql = "NULL" if module_id is None else sql_int(module_id)
    execute(f"""
        UPDATE {tbl('RULE_SET')}
        SET MODULE_ID = {module_sql}, UPDATED_BY = {_actor()}, UPDATED_TS = CURRENT_TIMESTAMP()
        WHERE RULE_SET_ID = {sql_int(rule_set_id)}
    """)
    log_audit_event("RULE_SET", rule_set_id, "RULESET_REASSIGNED",
                    f"Moved to module {module_id or '(none)'}")
    bump_cache()


# ------------------------------------------------------------------
# Create rule set / business object (inline from Write-a-rule page)
# ------------------------------------------------------------------
def create_rule_set(
    rule_set_name: str,
    module_id: Optional[int] = None,
    source_object_id: Optional[int] = None,
    target_object_id: Optional[int] = None,
    source_technical_object: str = "",
    target_technical_object: str = "",
    subject_area: str = "",
    description: str = "",
    performed_by: Optional[str] = None,
) -> int:
    rs_id = reserve_ids("RULE_SET", "RULE_SET_ID", 1)[0]
    insert_rows(
        "RULE_SET",
        ["RULE_SET_ID", "RULE_SET_NAME", "MODULE_ID",
         "SOURCE_OBJECT_ID", "TARGET_OBJECT_ID",
         "SOURCE_TECHNICAL_OBJECT", "TARGET_TECHNICAL_OBJECT",
         "SUBJECT_AREA", "DESCRIPTION", "ACTIVE_IND", "CREATED_BY"],
        [[str(rs_id), sql_value(rule_set_name),
          "NULL" if module_id is None else sql_int(module_id),
          "NULL" if source_object_id is None else sql_int(source_object_id),
          "NULL" if target_object_id is None else sql_int(target_object_id),
          sql_value(source_technical_object), sql_value(target_technical_object),
          sql_value(subject_area), sql_value(description), "TRUE",
          _actor(performed_by)]],
    )
    # Link source object in the junction table when provided.
    if source_object_id is not None:
        src_id = reserve_ids("RULE_SET_SOURCE", "SOURCE_ID", 1)[0]
        insert_rows(
            "RULE_SET_SOURCE",
            ["SOURCE_ID", "RULE_SET_ID", "OBJECT_ID", "SOURCE_SEQ", "IS_PRIMARY"],
            [[str(src_id), str(rs_id), sql_int(source_object_id), "1", "TRUE"]],
        )
    log_audit_event("RULE_SET", rs_id, "RULESET_CREATED",
                    f"Created rule set '{rule_set_name}'", performed_by=performed_by)
    bump_cache()
    return rs_id


def create_business_object(
    object_name: str,
    technical_object_name: str,
    object_type: str = "TABLE",
    module_id: Optional[int] = None,
    subject_area: str = "",
    description: str = "",
    performed_by: Optional[str] = None,
) -> int:
    obj_id = reserve_ids("BUSINESS_OBJECT", "OBJECT_ID", 1)[0]
    insert_rows(
        "BUSINESS_OBJECT",
        ["OBJECT_ID", "OBJECT_NAME", "TECHNICAL_OBJECT_NAME",
         "OBJECT_TYPE", "MODULE_ID", "SUBJECT_AREA", "DESCRIPTION", "ACTIVE_IND",
         "CREATED_BY"],
        [[str(obj_id), sql_value(object_name), sql_value(technical_object_name),
          sql_value(object_type),
          "NULL" if module_id is None else sql_int(module_id),
          sql_value(subject_area), sql_value(description), "TRUE",
          _actor(performed_by)]],
    )
    log_audit_event("BUSINESS_OBJECT", obj_id, "OBJECT_CREATED",
                    f"Created business object '{object_name}' ({technical_object_name})",
                    performed_by=performed_by)
    bump_cache()
    return obj_id


def _resolve_object_id(technical_name: str) -> Optional[int]:
    """Look up the OBJECT_ID for a fully-qualified technical object name."""
    if not technical_name:
        return None
    df = cached_read(f"""
        SELECT OBJECT_ID FROM {tbl('BUSINESS_OBJECT')}
        WHERE UPPER(TECHNICAL_OBJECT_NAME) = {sql_value(technical_name.upper())}
        LIMIT 1
    """)
    if df.empty:
        return None
    return int(df.iloc[0]["OBJECT_ID"])


def add_rule_set_source(rule_set_id: int, technical_object: str) -> bool:
    """Register an additional input object for a rule set.

    RULE_SET.SOURCE_OBJECT_ID holds one primary source, but a real view often
    reads several - the Item stage view joins the item root, the characteristic
    root and the sales detail root. RULE_SET_SOURCE is the junction that carries
    the rest, and the lineage graph is built from it, so an upstream left out here
    simply does not appear on the diagram.

    Returns True when a row was added. Idempotent: an object already registered is
    a no-op rather than a duplicate edge.
    """
    object_id = _resolve_object_id(str(technical_object or ""))
    if object_id is None:
        return False
    existing = cached_read(f"""
        SELECT 1 FROM {tbl('RULE_SET_SOURCE')}
        WHERE RULE_SET_ID = {sql_int(rule_set_id)} AND OBJECT_ID = {sql_int(object_id)}
        LIMIT 1
    """)
    if not existing.empty:
        return False
    # IS_PRIMARY stays FALSE: the primary source is the one on RULE_SET itself,
    # set by link_rule_set_objects. SOURCE_SEQ continues after whatever is there.
    seq_df = cached_read(f"""
        SELECT COALESCE(MAX(SOURCE_SEQ), 0) + 1 AS NEXT_SEQ
        FROM {tbl('RULE_SET_SOURCE')} WHERE RULE_SET_ID = {sql_int(rule_set_id)}
    """)
    next_seq = int(seq_df.iloc[0]["NEXT_SEQ"]) if not seq_df.empty else 1
    source_id = reserve_ids("RULE_SET_SOURCE", "SOURCE_ID", 1)[0]
    insert_rows(
        "RULE_SET_SOURCE",
        ["SOURCE_ID", "RULE_SET_ID", "OBJECT_ID", "SOURCE_SEQ", "IS_PRIMARY"],
        [[str(source_id), str(rule_set_id), str(object_id), str(next_seq), "FALSE"]],
    )
    bump_cache()
    return True


def get_rule_set_endpoints(rule_set_id: int) -> Dict[str, object]:
    """What a rule set currently reads from and writes to.

    Returns {"sources": [technical name, ...], "target": technical name or ""}, with
    sources in SOURCE_SEQ order so the primary input comes first. Reads the junction
    table, then falls back to RULE_SET.SOURCE_OBJECT_ID for rule sets created before
    RULE_SET_SOURCE existed, which carry only the single column.
    """
    empty: Dict[str, object] = {"sources": [], "target": ""}
    try:
        rs = cached_read(f"""
            SELECT bo.TECHNICAL_OBJECT_NAME AS TARGET_NAME,
                   src.TECHNICAL_OBJECT_NAME AS PRIMARY_SOURCE
            FROM {tbl('RULE_SET')} rs
            LEFT JOIN {tbl('BUSINESS_OBJECT')} bo ON bo.OBJECT_ID = rs.TARGET_OBJECT_ID
            LEFT JOIN {tbl('BUSINESS_OBJECT')} src ON src.OBJECT_ID = rs.SOURCE_OBJECT_ID
            WHERE rs.RULE_SET_ID = {sql_int(rule_set_id)}
        """)
        if rs.empty:
            return empty
        target = str(rs.iloc[0]["TARGET_NAME"] or "")
        primary = str(rs.iloc[0]["PRIMARY_SOURCE"] or "")

        joined = cached_read(f"""
            SELECT bo.TECHNICAL_OBJECT_NAME AS OBJ
            FROM {tbl('RULE_SET_SOURCE')} rss
            JOIN {tbl('BUSINESS_OBJECT')} bo ON bo.OBJECT_ID = rss.OBJECT_ID
            WHERE rss.RULE_SET_ID = {sql_int(rule_set_id)}
              AND bo.TECHNICAL_OBJECT_NAME IS NOT NULL
            ORDER BY rss.SOURCE_SEQ
        """)
        sources = [str(v) for v in joined["OBJ"].tolist() if v] if not joined.empty else []
        if primary and primary not in sources:
            sources.insert(0, primary)
        return {"sources": sources, "target": target}
    except Exception:
        return empty


def set_rule_set_endpoints(
    rule_set_id: int,
    source_technical_objects: Optional[List[str]] = None,
    target_technical_object: str = "",
    performed_by: Optional[str] = None,
) -> Dict[str, object]:
    """Set what a rule set reads from and writes to, authoritatively.

    Unlike link_rule_set_objects(), which only fills in blanks, this replaces the
    source list: objects removed from it have their RULE_SET_SOURCE rows deleted, so
    the lineage diagram stops drawing an edge the author has taken away. That is the
    whole point of letting sources be edited, and it is why this is a separate
    function rather than a flag on the old one - callers that only want to backfill
    must not start deleting.

    The first name in source_technical_objects becomes the primary: it goes on
    RULE_SET.SOURCE_OBJECT_ID and gets IS_PRIMARY on its junction row. The rest follow
    in the order given, which is the order the lineage tree shows them in.

    Passing target_technical_object="" leaves the target alone rather than clearing it.
    A rule set with no target draws no edge at all, so silently erasing one on an empty
    string would make hops disappear.

    Returns {"added": [...], "removed": [...], "target_changed": bool, "unresolved":
    [...]}. Names that are not registered business objects are reported in
    "unresolved" and otherwise ignored - the caller can offer to create them.
    """
    requested = [str(n).strip() for n in (source_technical_objects or []) if str(n).strip()]
    # dict.fromkeys keeps the author's ordering while dropping repeats, which matters
    # because position decides which source is primary.
    requested = list(dict.fromkeys(requested))

    resolved: List[Tuple[str, int]] = []
    unresolved: List[str] = []
    for name in requested:
        object_id = _resolve_object_id(name)
        if object_id is None:
            unresolved.append(name)
        else:
            resolved.append((name, object_id))

    result: Dict[str, object] = {"added": [], "removed": [], "target_changed": False,
                                "unresolved": unresolved}

    if target_technical_object:
        target_id = _resolve_object_id(target_technical_object)
        if target_id is None:
            unresolved.append(target_technical_object)
        else:
            execute(f"""
                UPDATE {tbl('RULE_SET')}
                SET TARGET_OBJECT_ID = {sql_int(target_id)},
                    TARGET_TECHNICAL_OBJECT = {sql_value(target_technical_object)},
                    UPDATED_BY = {_actor(performed_by)}, UPDATED_TS = CURRENT_TIMESTAMP()
                WHERE RULE_SET_ID = {sql_int(rule_set_id)}
            """)
            result["target_changed"] = True

    if not resolved:
        # Nothing resolvable was asked for. Deleting every source here would strip the
        # rule set off the lineage graph on a typo, so leave the existing ones alone.
        bump_cache()
        return result

    existing = cached_read(f"""
        SELECT rss.OBJECT_ID, bo.TECHNICAL_OBJECT_NAME AS OBJ
        FROM {tbl('RULE_SET_SOURCE')} rss
        LEFT JOIN {tbl('BUSINESS_OBJECT')} bo ON bo.OBJECT_ID = rss.OBJECT_ID
        WHERE rss.RULE_SET_ID = {sql_int(rule_set_id)}
    """)
    existing_ids = ({int(v) for v in existing["OBJECT_ID"].dropna().tolist()}
                    if not existing.empty else set())
    existing_names = ({int(r["OBJECT_ID"]): str(r["OBJ"] or "")
                       for _, r in existing.iterrows()} if not existing.empty else {})

    wanted_ids = [oid for _, oid in resolved]
    doomed = [oid for oid in existing_ids if oid not in wanted_ids]
    if doomed:
        execute(f"""
            DELETE FROM {tbl('RULE_SET_SOURCE')}
            WHERE RULE_SET_ID = {sql_int(rule_set_id)}
              AND OBJECT_ID IN ({', '.join(sql_int(o) for o in doomed)})
        """)
        result["removed"] = [existing_names.get(o, str(o)) for o in doomed]

    # Rewrite SOURCE_SEQ and IS_PRIMARY for everything that stays, because position
    # carries meaning and a reordered list must not keep the old sequence numbers.
    new_ids = [oid for oid in wanted_ids if oid not in existing_ids]
    if new_ids:
        reserved = reserve_ids("RULE_SET_SOURCE", "SOURCE_ID", len(new_ids))
        insert_rows(
            "RULE_SET_SOURCE",
            ["SOURCE_ID", "RULE_SET_ID", "OBJECT_ID", "SOURCE_SEQ", "IS_PRIMARY"],
            [[str(reserved[i]), str(rule_set_id), sql_int(oid),
              str(wanted_ids.index(oid) + 1),
              "TRUE" if wanted_ids.index(oid) == 0 else "FALSE"]
             for i, oid in enumerate(new_ids)],
        )
        by_id = {oid: name for name, oid in resolved}
        result["added"] = [by_id.get(o, str(o)) for o in new_ids]

    for position, (_, object_id) in enumerate(resolved, start=1):
        execute(f"""
            UPDATE {tbl('RULE_SET_SOURCE')}
            SET SOURCE_SEQ = {sql_int(position)},
                IS_PRIMARY = {'TRUE' if position == 1 else 'FALSE'}
            WHERE RULE_SET_ID = {sql_int(rule_set_id)}
              AND OBJECT_ID = {sql_int(object_id)}
        """)

    primary_name, primary_id = resolved[0]
    execute(f"""
        UPDATE {tbl('RULE_SET')}
        SET SOURCE_OBJECT_ID = {sql_int(primary_id)},
            SOURCE_TECHNICAL_OBJECT = {sql_value(primary_name)},
            UPDATED_BY = {_actor(performed_by)}, UPDATED_TS = CURRENT_TIMESTAMP()
        WHERE RULE_SET_ID = {sql_int(rule_set_id)}
    """)

    if result["added"] or result["removed"] or result["target_changed"]:
        detail = []
        if result["added"]:
            detail.append("added " + ", ".join(result["added"]))
        if result["removed"]:
            detail.append("removed " + ", ".join(result["removed"]))
        if result["target_changed"]:
            detail.append(f"target set to {target_technical_object}")
        log_audit_event("RULE_SET", rule_set_id, "RULESET_ENDPOINTS_CHANGED",
                        "; ".join(detail), performed_by=performed_by)
    bump_cache()
    return result


def link_rule_set_objects(
    rule_set_id: int,
    source_technical_object: str = "",
    target_technical_object: str = "",
) -> None:
    """
    Ensure the rule set's SOURCE_OBJECT_ID/TARGET_OBJECT_ID and RULE_SET_SOURCE
    are populated, so the lineage graph can trace edges. Only fills in missing
    links — never overwrites an existing one.

    Use set_rule_set_endpoints() to change endpoints that are already set; this
    function deliberately cannot, so a backfill can never delete a source.
    """
    rs_df = cached_read(f"""
        SELECT SOURCE_OBJECT_ID, TARGET_OBJECT_ID
        FROM {tbl('RULE_SET')} WHERE RULE_SET_ID = {sql_int(rule_set_id)}
    """)
    if rs_df.empty:
        return
    current_src = rs_df.iloc[0]["SOURCE_OBJECT_ID"]
    current_tgt = rs_df.iloc[0]["TARGET_OBJECT_ID"]

    # Link target object
    if pd.isna(current_tgt) and target_technical_object:
        tgt_id = _resolve_object_id(target_technical_object)
        if tgt_id is not None:
            execute(f"""
                UPDATE {tbl('RULE_SET')}
                SET TARGET_OBJECT_ID = {tgt_id},
                    TARGET_TECHNICAL_OBJECT = {sql_value(target_technical_object)},
                    UPDATED_BY = {_actor()}, UPDATED_TS = CURRENT_TIMESTAMP()
                WHERE RULE_SET_ID = {sql_int(rule_set_id)}
            """)

    # Link source object
    if pd.isna(current_src) and source_technical_object:
        src_id = _resolve_object_id(source_technical_object)
        if src_id is not None:
            execute(f"""
                UPDATE {tbl('RULE_SET')}
                SET SOURCE_OBJECT_ID = {src_id},
                    SOURCE_TECHNICAL_OBJECT = {sql_value(source_technical_object)},
                    UPDATED_BY = {_actor()}, UPDATED_TS = CURRENT_TIMESTAMP()
                WHERE RULE_SET_ID = {sql_int(rule_set_id)}
            """)
            # Also add to RULE_SET_SOURCE junction table if not already there
            existing = cached_read(f"""
                SELECT 1 FROM {tbl('RULE_SET_SOURCE')}
                WHERE RULE_SET_ID = {sql_int(rule_set_id)} AND OBJECT_ID = {src_id}
                LIMIT 1
            """)
            if existing.empty:
                sid = reserve_ids("RULE_SET_SOURCE", "SOURCE_ID", 1)[0]
                insert_rows(
                    "RULE_SET_SOURCE",
                    ["SOURCE_ID", "RULE_SET_ID", "OBJECT_ID", "SOURCE_SEQ", "IS_PRIMARY"],
                    [[str(sid), str(rule_set_id), str(src_id), "1", "TRUE"]],
                )

    bump_cache()


# ------------------------------------------------------------------
# Team <-> database mapping
# ------------------------------------------------------------------
def list_team_databases(team_id: Optional[int] = None) -> pd.DataFrame:
    """Team-to-database rows with the physical name resolved, for the admin UI."""
    where = "WHERE td.ACTIVE_IND = TRUE"
    if team_id is not None:
        where += f" AND td.TEAM_ID = {sql_int(team_id)}"
    try:
        return cached_read(f"""
            SELECT td.TEAM_DATABASE_ID, td.TEAM_ID, t.TEAM_NAME,
                   td.LOGICAL_DATABASE, td.IS_PRIMARY,
                   dm.PHYSICAL_DATABASE
            FROM {tbl('TEAM_DATABASE')} td
            LEFT JOIN {tbl('TEAM')} t ON t.TEAM_ID = td.TEAM_ID
            LEFT JOIN {tbl('DATABASE_MAP')} dm
                   ON dm.LOGICAL_DATABASE = td.LOGICAL_DATABASE
                  AND dm.ACTIVE_IND = TRUE
            {where}
            ORDER BY t.TEAM_NAME, td.IS_PRIMARY DESC, td.LOGICAL_DATABASE
        """)
    except Exception:
        return pd.DataFrame(columns=["TEAM_DATABASE_ID", "TEAM_ID", "TEAM_NAME",
                                     "LOGICAL_DATABASE", "IS_PRIMARY", "PHYSICAL_DATABASE"])


def add_team_database(team_id: int, logical_database: str, is_primary: bool = False,
                      performed_by: Optional[str] = None) -> Optional[int]:
    """
    Attach a database to a team. Re-activates an existing row rather than
    inserting a duplicate, so removing and re-adding does not accumulate rows.
    """
    name = _text(logical_database).upper()
    if not name:
        return None
    existing = cached_read(f"""
        SELECT TEAM_DATABASE_ID FROM {tbl('TEAM_DATABASE')}
        WHERE TEAM_ID = {sql_int(team_id)} AND UPPER(LOGICAL_DATABASE) = {sql_value(name)}
        LIMIT 1
    """)
    if not existing.empty:
        td_id = int(existing.iloc[0]["TEAM_DATABASE_ID"])
        execute(f"""
            UPDATE {tbl('TEAM_DATABASE')}
            SET ACTIVE_IND = TRUE, IS_PRIMARY = {'TRUE' if is_primary else 'FALSE'},
                UPDATED_BY = {_actor(performed_by)}, UPDATED_TS = CURRENT_TIMESTAMP()
            WHERE TEAM_DATABASE_ID = {sql_int(td_id)}
        """)
    else:
        td_id = reserve_ids("TEAM_DATABASE", "TEAM_DATABASE_ID", 1)[0]
        insert_rows(
            "TEAM_DATABASE",
            ["TEAM_DATABASE_ID", "TEAM_ID", "LOGICAL_DATABASE", "IS_PRIMARY"],
            [[str(td_id), sql_int(team_id), sql_value(name),
              "TRUE" if is_primary else "FALSE"]],
        )
    if is_primary:
        _clear_other_primaries(team_id, td_id)
    log_audit_event("TEAM", team_id, "TEAM_DATABASE_ADDED",
                    f"Database '{name}' attached to team {team_id}",
                    performed_by=performed_by)
    bump_cache()
    return td_id


def _clear_other_primaries(team_id: int, keep_id: int) -> None:
    """Only one primary per team; the newest wins."""
    execute(f"""
        UPDATE {tbl('TEAM_DATABASE')}
        SET IS_PRIMARY = FALSE, UPDATED_BY = {_actor()}, UPDATED_TS = CURRENT_TIMESTAMP()
        WHERE TEAM_ID = {sql_int(team_id)} AND TEAM_DATABASE_ID <> {sql_int(keep_id)}
    """)


def set_team_database_primary(team_database_id: int, team_id: int,
                              performed_by: Optional[str] = None) -> None:
    execute(f"""
        UPDATE {tbl('TEAM_DATABASE')}
        SET IS_PRIMARY = TRUE, UPDATED_BY = {_actor(performed_by)}, UPDATED_TS = CURRENT_TIMESTAMP()
        WHERE TEAM_DATABASE_ID = {sql_int(team_database_id)}
    """)
    _clear_other_primaries(team_id, team_database_id)
    log_audit_event("TEAM", team_id, "TEAM_DATABASE_PRIMARY_SET",
                    f"Team database {team_database_id} set as primary",
                    performed_by=performed_by)
    bump_cache()


def remove_team_database(team_database_id: int, performed_by: Optional[str] = None) -> None:
    """Deactivate rather than delete, so the change is reversible and auditable."""
    execute(f"""
        UPDATE {tbl('TEAM_DATABASE')}
        SET ACTIVE_IND = FALSE, IS_PRIMARY = FALSE,
            UPDATED_BY = {_actor(performed_by)}, UPDATED_TS = CURRENT_TIMESTAMP()
        WHERE TEAM_DATABASE_ID = {sql_int(team_database_id)}
    """)
    log_audit_event("TEAM", team_database_id, "TEAM_DATABASE_REMOVED",
                    f"Team database {team_database_id} detached",
                    performed_by=performed_by)
    bump_cache()


def list_database_map() -> pd.DataFrame:
    try:
        return cached_read(f"""
            SELECT LOGICAL_DATABASE, PHYSICAL_DATABASE, ACTIVE_IND
            FROM {tbl('DATABASE_MAP')}
            ORDER BY LOGICAL_DATABASE
        """)
    except Exception:
        return pd.DataFrame(columns=["LOGICAL_DATABASE", "PHYSICAL_DATABASE", "ACTIVE_IND"])


def upsert_database_map(logical_database: str, physical_database: str,
                        performed_by: Optional[str] = None) -> None:
    """Point a logical database at a physical one, inserting or updating."""
    logical = _text(logical_database).upper()
    physical = _text(physical_database).upper()
    if not logical or not physical:
        return
    existing = cached_read(f"""
        SELECT 1 FROM {tbl('DATABASE_MAP')}
        WHERE UPPER(LOGICAL_DATABASE) = {sql_value(logical)} LIMIT 1
    """)
    if existing.empty:
        insert_rows("DATABASE_MAP", ["LOGICAL_DATABASE", "PHYSICAL_DATABASE"],
                    [[sql_value(logical), sql_value(physical)]])
    else:
        execute(f"""
            UPDATE {tbl('DATABASE_MAP')}
            SET PHYSICAL_DATABASE = {sql_value(physical)}, ACTIVE_IND = TRUE,
                UPDATED_BY = {_actor(performed_by)}, UPDATED_TS = CURRENT_TIMESTAMP()
            WHERE UPPER(LOGICAL_DATABASE) = {sql_value(logical)}
        """)
    log_audit_event("DATABASE_MAP", 0, "DATABASE_MAPPED",
                    f"{logical} -> {physical}", performed_by=performed_by)
    bump_cache()


# ------------------------------------------------------------------
# PO review comments
# ------------------------------------------------------------------
def get_review_comments() -> pd.DataFrame:
    """
    Every review comment, newest first, with the module's name resolved.

    Joined rather than denormalised because comments are keyed on MODULE_ID: the
    name has to come from MODULE at read time, so a renamed module shows its
    current name against historic comments.
    """
    try:
        return cached_read(f"""
            SELECT c.COMMENT_ID, c.MODULE_ID, m.MODULE_NAME,
                   c.COMMENT_TEXT, c.STATUS,
                   c.RAISED_BY, c.RAISED_TS, c.RESOLVED_BY, c.RESOLVED_TS
            FROM {tbl('RULE_REVIEW_COMMENT')} c
            LEFT JOIN {tbl('MODULE')} m ON c.MODULE_ID = m.MODULE_ID
            ORDER BY c.RAISED_TS DESC, c.COMMENT_ID DESC
        """)
    except Exception:
        return pd.DataFrame()


def add_review_comment(module_id: int, comment_text: str,
                       raised_by: Optional[str] = None) -> Optional[int]:
    """
    Raise a comment against a module. Returns the new id, or None when the text
    was blank - an empty comment is a no-op, not an error.

    `raised_by` is passed in rather than left to the column's CURRENT_USER()
    default: this app runs execute_as OWNER, so CURRENT_USER() is the Streamlit
    object's owner for every viewer, which would credit every comment to the same
    person. The caller supplies the viewer's name (st.user.user_name). The column
    default stays as a backstop for rows inserted directly by SQL.
    """
    text = _text(comment_text)
    if not text:
        return None
    author = _text(raised_by)
    comment_id = reserve_ids("RULE_REVIEW_COMMENT", "COMMENT_ID", 1)[0]
    insert_rows(
        "RULE_REVIEW_COMMENT",
        ["COMMENT_ID", "MODULE_ID", "COMMENT_TEXT", "STATUS", "RAISED_BY"],
        [[str(comment_id), sql_int(module_id), sql_value(text), sql_value("Open"),
          _actor(author)]],
    )
    log_audit_event("REVIEW_COMMENT", comment_id, "COMMENT_ADDED",
                    f"Module {module_id}: {text[:200]}", performed_by=author)
    bump_cache()
    return comment_id


def set_review_comment_done(comment_id: int, done: bool,
                            resolved_by: Optional[str] = None) -> None:
    """
    Tick a comment off, or reopen it.

    Un-ticking clears RESOLVED_BY / RESOLVED_TS rather than leaving them behind,
    so a reopened comment doesn't read as though it were still closed by someone.

    `resolved_by` is the viewer, for the same reason as add_review_comment().
    """
    if done:
        closer = _text(resolved_by)
        closer_sql = _actor(closer)
        execute(f"""
            UPDATE {tbl('RULE_REVIEW_COMMENT')}
            SET STATUS = 'Done',
                RESOLVED_BY = {closer_sql},
                RESOLVED_TS = CURRENT_TIMESTAMP()
            WHERE COMMENT_ID = {sql_int(comment_id)}
        """)
        log_audit_event("REVIEW_COMMENT", comment_id, "COMMENT_RESOLVED",
                        "Marked as Done", performed_by=resolved_by)
    else:
        execute(f"""
            UPDATE {tbl('RULE_REVIEW_COMMENT')}
            SET STATUS = 'Open', RESOLVED_BY = NULL, RESOLVED_TS = NULL
            WHERE COMMENT_ID = {sql_int(comment_id)}
        """)
        log_audit_event("REVIEW_COMMENT", comment_id, "COMMENT_REOPENED",
                        "Reopened", performed_by=resolved_by)
    bump_cache()


# ------------------------------------------------------------------
# Test cases
# ------------------------------------------------------------------
def get_test_cases(rule_ids=None, status: Optional[str] = None) -> pd.DataFrame:
    """
    Saved test cases, newest first. Scoped to specific rules when rule_ids is
    given, otherwise the whole table.

    Returns an empty frame with the right columns when the table is missing, so
    the page renders an empty state instead of raising - the DDL may not have
    been run yet on a fresh environment.
    """
    columns = ["TEST_CASE_ID", "RULE_ID", "TEST_TYPE", "TEST_NAME", "SCENARIO",
               "EXPECTED_RESULT", "TEST_SQL", "STATUS", "SOURCE_ACTION",
               "CREATED_BY", "CREATED_TS"]
    where = []
    if rule_ids is not None:
        if not isinstance(rule_ids, (list, tuple, set)):
            rule_ids = [rule_ids]
        ids = sorted({int(r) for r in rule_ids})
        if not ids:
            return pd.DataFrame(columns=columns)
        where.append(f"RULE_ID IN ({', '.join(str(i) for i in ids)})")
    if status:
        where.append(f"STATUS = {sql_value(status)}")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    try:
        return cached_read(f"""
            SELECT {', '.join(columns)}
            FROM {tbl('RULE_TEST_CASE')}
            {clause}
            ORDER BY RULE_ID, TEST_CASE_ID
        """)
    except Exception:
        return pd.DataFrame(columns=columns)


def save_test_cases(rule_id: int, cases: List[Dict], source_action: str = "BULK",
                    status: str = "ACCEPTED", created_by: Optional[str] = None) -> int:
    """
    Persist test cases for a rule in one INSERT. Returns how many were written.

    cases: [{test_type, test_name, scenario, expected_result, test_sql}]

    `created_by` is passed through for the same reason as review comments: the
    app runs as OWNER, so CURRENT_USER() would credit every case to the app owner
    rather than the person who accepted it.
    """
    rows_in = [c for c in (cases or []) if _text(c.get("test_name"))]
    if not rows_in:
        return 0

    author = _text(created_by)
    author_sql = _actor(author)
    ids = reserve_ids("RULE_TEST_CASE", "TEST_CASE_ID", len(rows_in))

    insert_rows(
        "RULE_TEST_CASE",
        ["TEST_CASE_ID", "RULE_ID", "TEST_TYPE", "TEST_NAME", "SCENARIO",
         "EXPECTED_RESULT", "TEST_SQL", "STATUS", "SOURCE_ACTION", "CREATED_BY"],
        [[str(ids[i]), sql_int(rule_id),
          sql_value(c.get("test_type") or "FUNCTIONAL"),
          sql_value(c.get("test_name")),
          sql_value(c.get("scenario")),
          sql_value(c.get("expected_result")),
          sql_value(c.get("test_sql")),
          sql_value(status),
          sql_value(source_action),
          author_sql]
         for i, c in enumerate(rows_in)],
    )
    log_audit_event("TEST_CASE", rule_id, "TEST_CASES_ADDED",
                    f"{len(rows_in)} {status.lower()} test case(s) via {source_action}",
                    performed_by=author)
    bump_cache()
    return len(rows_in)


def set_test_case_status(test_case_id: int, status: str,
                         performed_by: Optional[str] = None) -> None:
    """Accept or reject a saved test case."""
    execute(f"""
        UPDATE {tbl('RULE_TEST_CASE')}
        SET STATUS = {sql_value(status)}
        WHERE TEST_CASE_ID = {sql_int(test_case_id)}
    """)
    log_audit_event("TEST_CASE", test_case_id, f"TEST_CASE_{status.upper()}",
                    f"Test case {test_case_id} marked {status}",
                    performed_by=performed_by)
    bump_cache()


def delete_test_case(test_case_id: int, performed_by: Optional[str] = None) -> None:
    execute(f"DELETE FROM {tbl('RULE_TEST_CASE')} WHERE TEST_CASE_ID = {sql_int(test_case_id)}")
    log_audit_event("TEST_CASE", test_case_id, "TEST_CASE_DELETED",
                    f"Test case {test_case_id} deleted", performed_by=performed_by)
    bump_cache()


# ------------------------------------------------------------------
# Declared column specification
# ------------------------------------------------------------------
# The FDD says which columns a target object must have. That is design intent
# authored by the BA, not a reflection of what is currently deployed, so it is
# stored rather than read back from INFORMATION_SCHEMA. COLUMN_ORIGIN separates a
# straight pull from a source (PASS_THROUGH) from one needing transformation
# logic (DERIVED) - without it, "this column has no rule" would fire on every
# straight pull, where having no rule is the correct state.
PASS_THROUGH = "PASS_THROUGH"
DERIVED = "DERIVED"
# Columns read back from Snowflake rather than declared by a BA. Kept distinct so
# the page can show what the object actually has separately from what someone
# intended it to have - the two disagreeing is exactly what the Verify page looks
# for, and collapsing them would hide it.
DISCOVERED = "DISCOVERED"

_COLUMN_SPEC_FIELDS = ["COLUMN_SPEC_ID", "OBJECT_ID", "ORDINAL", "COLUMN_NAME",
                       "DATA_TYPE", "NULLABLE_IND", "PRIMARY_KEY_IND",
                       "DEFAULT_VALUE", "BUSINESS_DESCRIPTION", "COLUMN_ORIGIN",
                       "SOURCE_OBJECT", "SOURCE_COLUMN", "ACTIVE_IND",
                       "CREATED_BY", "CREATED_TS", "UPDATED_BY", "UPDATED_TS"]


def get_object_column_spec(object_id: Optional[int] = None,
                           active_only: bool = True) -> pd.DataFrame:
    """
    Declared columns, in FDD order. Whole table when object_id is None.

    Returns an empty frame with the right columns if the table is absent, so a
    fresh environment renders an empty state instead of raising.
    """
    where = []
    if object_id is not None:
        where.append(f"OBJECT_ID = {sql_int(object_id)}")
    if active_only:
        where.append("ACTIVE_IND = TRUE")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    try:
        return cached_read(f"""
            SELECT {', '.join(_COLUMN_SPEC_FIELDS)}
            FROM {tbl('BUSINESS_OBJECT_COLUMN')}
            {clause}
            ORDER BY OBJECT_ID, ORDINAL, COLUMN_SPEC_ID
        """)
    except Exception:
        return pd.DataFrame(columns=_COLUMN_SPEC_FIELDS)


def _next_ordinal(object_id: int) -> int:
    """One past the highest ordinal already declared for the object."""
    try:
        df = run_query(f"""
            SELECT COALESCE(MAX(ORDINAL), 0) + 1 AS NEXT_ORDINAL
            FROM {tbl('BUSINESS_OBJECT_COLUMN')}
            WHERE OBJECT_ID = {sql_int(object_id)}
        """)
        return int(df.iloc[0]["NEXT_ORDINAL"])
    except Exception:
        return 1


def add_object_columns(object_id: int, columns: List[Dict],
                       created_by: Optional[str] = None) -> int:
    """
    Declare columns on an object in one INSERT. Returns how many were written.

    columns: [{column_name, data_type, nullable_ind, primary_key_ind,
               default_value, business_description, column_origin,
               source_object, source_column}]

    Names already declared on the object are skipped rather than duplicated, so
    re-ticking a source column that is already in the spec is a no-op instead of
    a second row. Ordinals continue from the current maximum, keeping FDD order
    stable as columns are added over several sittings.
    """
    wanted = [c for c in (columns or []) if _text(c.get("column_name"))]
    if not wanted:
        return 0

    existing = get_object_column_spec(object_id, active_only=False)
    taken = ({str(n).strip().upper() for n in existing["COLUMN_NAME"].tolist()}
             if not existing.empty else set())

    fresh, seen = [], set()
    for c in wanted:
        name = _text(c.get("column_name")).upper()
        if name in taken or name in seen:
            continue
        seen.add(name)
        fresh.append(c)
    if not fresh:
        return 0

    author = _text(created_by)
    author_sql = _actor(author)
    ids = reserve_ids("BUSINESS_OBJECT_COLUMN", "COLUMN_SPEC_ID", len(fresh))
    start = _next_ordinal(object_id)

    insert_rows(
        "BUSINESS_OBJECT_COLUMN",
        ["COLUMN_SPEC_ID", "OBJECT_ID", "ORDINAL", "COLUMN_NAME", "DATA_TYPE",
         "NULLABLE_IND", "PRIMARY_KEY_IND", "DEFAULT_VALUE",
         "BUSINESS_DESCRIPTION", "COLUMN_ORIGIN", "SOURCE_OBJECT",
         "SOURCE_COLUMN", "CREATED_BY"],
        [[str(ids[i]), sql_int(object_id), str(start + i),
          sql_value(_text(c.get("column_name")).upper()),
          sql_value(c.get("data_type")),
          "TRUE" if c.get("nullable_ind", True) else "FALSE",
          "TRUE" if c.get("primary_key_ind") else "FALSE",
          sql_value(c.get("default_value")),
          sql_value(c.get("business_description")),
          sql_value(c.get("column_origin") or DERIVED),
          sql_value(c.get("source_object")),
          sql_value(c.get("source_column")),
          author_sql]
         for i, c in enumerate(fresh)],
    )
    origins = {str(c.get("column_origin") or DERIVED) for c in fresh}
    log_audit_event("OBJECT_COLUMN", object_id, "COLUMNS_DECLARED",
                    f"{len(fresh)} column(s) declared ({', '.join(sorted(origins))})",
                    performed_by=author)
    bump_cache()
    return len(fresh)


# Why a live column lookup came back empty, keyed on the technical name.
#
# Exists because the alternative is what actually happened: live_object_columns
# caught every exception and returned an empty frame, a missing `ident` import
# made it raise NameError every single time, and three features - the FDD
# importer's column reconciliation, sync_object_columns, and the Object columns
# page - reported "not built" for objects that plainly existed. Nothing surfaced
# a reason, so the bug was indistinguishable from the object genuinely being
# absent.
_LIVE_COLUMN_FAILURES: Dict[str, str] = {}


def _note_live_failure(technical_object: str, reason: str) -> None:
    _LIVE_COLUMN_FAILURES[str(technical_object or "")] = reason


def live_column_failure(technical_object: str) -> Optional[str]:
    """Why the last lookup for this object returned nothing, if it failed."""
    return _LIVE_COLUMN_FAILURES.get(str(technical_object or ""))


def live_column_failures() -> Dict[str, str]:
    """Every object whose live column lookup failed, for surfacing in the UI."""
    return dict(_LIVE_COLUMN_FAILURES)


def live_object_columns(technical_object: str) -> pd.DataFrame:
    """
    Column metadata as Snowflake currently has it, for one object.

    Data types are rendered the way a BA writes them in the FDD - VARCHAR(18)
    rather than TEXT with a separate length column - so a discovered column reads
    the same as a hand-declared one.

    Failures are recorded in _LIVE_COLUMN_FAILURES rather than only swallowed.
    An earlier version caught every exception and returned an empty frame, so a
    missing `ident` import raised NameError on the very first line of the query
    and this function returned zero columns for EVERY object, in every caller,
    indefinitely. The import page reported real tables as "not built", and
    nothing anywhere said why. An empty result and a broken lookup have to be
    distinguishable.
    """
    empty = pd.DataFrame(columns=["COLUMN_NAME", "DATA_TYPE", "NULLABLE_IND",
                                  "DEFAULT_VALUE", "BUSINESS_DESCRIPTION", "ORDINAL"])
    key = str(technical_object or "")
    _LIVE_COLUMN_FAILURES.pop(key, None)

    try:
        from catalog import resolve_physical_name
        physical = resolve_physical_name(technical_object) or str(technical_object or "")
    except Exception as exc:
        _note_live_failure(key, f"could not resolve the physical name: {exc}")
        physical = str(technical_object or "")
    parts = [p for p in physical.split(".") if p]
    if len(parts) != 3:
        _note_live_failure(key, f"{physical!r} is not a three-part DB.SCHEMA.OBJECT name")
        return empty
    db, schema, name = parts
    try:
        df = cached_read(f"""
            SELECT COLUMN_NAME, DATA_TYPE,
                   CHARACTER_MAXIMUM_LENGTH AS LEN,
                   NUMERIC_PRECISION AS PREC, NUMERIC_SCALE AS SCALE,
                   IS_NULLABLE, COLUMN_DEFAULT, COMMENT, ORDINAL_POSITION
            FROM {ident(db)}.INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = {sql_value(schema.upper())}
              AND TABLE_NAME = {sql_value(name.upper())}
            ORDER BY ORDINAL_POSITION
        """)
    except Exception as exc:
        # Most likely the database is absent or not granted to the active role.
        # INFORMATION_SCHEMA is privilege-filtered, so this is a real possibility
        # and worth telling the user about rather than showing "not built".
        _note_live_failure(key, f"querying {db}.INFORMATION_SCHEMA.COLUMNS failed: {exc}")
        return empty
    if df.empty:
        return empty

    rows = []
    for _, r in df.iterrows():
        base = str(r["DATA_TYPE"] or "").upper()
        if base in ("TEXT", "VARCHAR", "CHAR", "STRING") and pd.notna(r["LEN"]):
            rendered = f"VARCHAR({int(r['LEN'])})"
        elif base in ("NUMBER", "DECIMAL", "NUMERIC") and pd.notna(r["PREC"]):
            scale = int(r["SCALE"]) if pd.notna(r["SCALE"]) else 0
            rendered = f"NUMBER({int(r['PREC'])},{scale})"
        else:
            rendered = base
        rows.append({
            "COLUMN_NAME": str(r["COLUMN_NAME"]).upper(),
            "DATA_TYPE": rendered,
            "NULLABLE_IND": str(r["IS_NULLABLE"] or "YES").upper() == "YES",
            "DEFAULT_VALUE": None if pd.isna(r["COLUMN_DEFAULT"]) else str(r["COLUMN_DEFAULT"]),
            "BUSINESS_DESCRIPTION": None if pd.isna(r["COMMENT"]) else str(r["COMMENT"]),
            "ORDINAL": int(r["ORDINAL_POSITION"]),
        })
    return pd.DataFrame(rows)


def declared_columns(technical_object: str) -> List[str]:
    """Column names the FDD declares for an object, by technical name.

    The fallback for objects that are not built yet: a rule must be writable
    before the table exists. Kept here rather than reached for from catalog.py so
    the object-id resolution stays in one place.
    """
    object_id = _resolve_object_id(str(technical_object or ""))
    if object_id is None:
        return []
    spec = get_object_column_spec(object_id)
    if spec is None or spec.empty:
        return []
    return [str(c).upper() for c in spec["COLUMN_NAME"].tolist()]


def sync_object_columns(object_id: int, technical_object: str,
                        created_by: Optional[str] = None) -> int:
    """
    Read the object's real columns from Snowflake into its spec, as DISCOVERED.

    Additive: add_object_columns() skips names already on the object, so a column
    a BA declared by hand keeps its description, origin and ordinal. Returns how
    many were newly stored - 0 means the spec was already complete or the object
    does not exist here.
    """
    live = live_object_columns(technical_object)
    if live.empty:
        return 0
    return add_object_columns(
        object_id,
        [{
            "column_name": r["COLUMN_NAME"],
            "data_type": r["DATA_TYPE"],
            "nullable_ind": bool(r["NULLABLE_IND"]),
            "primary_key_ind": False,
            "default_value": r["DEFAULT_VALUE"],
            "business_description": r["BUSINESS_DESCRIPTION"],
            "column_origin": DISCOVERED,
        } for _, r in live.iterrows()],
        created_by=created_by,
    )


def sync_all_object_columns(created_by: Optional[str] = None,
                            only_missing: bool = True) -> Dict[str, int]:
    """
    Backfill column details for every registered object that resolves in
    Snowflake. Returns {objects_synced, columns_added, objects_skipped}.

    only_missing limits the pass to objects with no spec at all, which is the
    common case and keeps a re-run cheap.
    """
    objects = list_business_objects()
    if objects is None or objects.empty:
        return {"objects_synced": 0, "columns_added": 0, "objects_skipped": 0}

    have_spec = set()
    spec = get_object_column_spec(active_only=False)
    if not spec.empty:
        have_spec = {int(v) for v in spec["OBJECT_ID"].dropna().tolist()}

    synced = added = skipped = 0
    for _, row in objects.iterrows():
        name = str(row.get("TECHNICAL_OBJECT_NAME") or "")
        if name.count(".") != 2:
            skipped += 1
            continue
        object_id = int(row["OBJECT_ID"])
        if only_missing and object_id in have_spec:
            skipped += 1
            continue
        count = sync_object_columns(object_id, name, created_by=created_by)
        if count:
            synced += 1
            added += count
        else:
            skipped += 1
    return {"objects_synced": synced, "columns_added": added, "objects_skipped": skipped}


def update_object_column(column_spec_id: int, changes: Dict,
                         performed_by: Optional[str] = None) -> None:
    """
    Edit one declared column. Only the keys present in `changes` are written, so
    the grid can send back a single edited cell without clearing the rest.
    """
    assignable = {
        "ordinal": ("ORDINAL", lambda v: sql_int(v, 1)),
        "column_name": ("COLUMN_NAME", lambda v: sql_value(str(v).upper())),
        "data_type": ("DATA_TYPE", sql_value),
        "nullable_ind": ("NULLABLE_IND", lambda v: "TRUE" if v else "FALSE"),
        "primary_key_ind": ("PRIMARY_KEY_IND", lambda v: "TRUE" if v else "FALSE"),
        "default_value": ("DEFAULT_VALUE", sql_value),
        "business_description": ("BUSINESS_DESCRIPTION", sql_value),
        "column_origin": ("COLUMN_ORIGIN", sql_value),
        "source_object": ("SOURCE_OBJECT", sql_value),
        "source_column": ("SOURCE_COLUMN", sql_value),
        "active_ind": ("ACTIVE_IND", lambda v: "TRUE" if v else "FALSE"),
    }
    sets = [f"{col} = {render(changes[key])}"
            for key, (col, render) in assignable.items() if key in changes]
    if not sets:
        return

    who = _text(performed_by)
    sets.append(f"UPDATED_BY = {_actor(who)}")
    sets.append("UPDATED_TS = CURRENT_TIMESTAMP()")

    execute(f"""
        UPDATE {tbl('BUSINESS_OBJECT_COLUMN')}
        SET {', '.join(sets)}
        WHERE COLUMN_SPEC_ID = {sql_int(column_spec_id)}
    """)
    log_audit_event("OBJECT_COLUMN", column_spec_id, "COLUMN_UPDATED",
                    ", ".join(sorted(changes)), performed_by=who)
    bump_cache()


def delete_object_column(column_spec_id: int,
                         performed_by: Optional[str] = None) -> None:
    execute(f"""
        DELETE FROM {tbl('BUSINESS_OBJECT_COLUMN')}
        WHERE COLUMN_SPEC_ID = {sql_int(column_spec_id)}
    """)
    log_audit_event("OBJECT_COLUMN", column_spec_id, "COLUMN_DELETED",
                    f"Column spec {column_spec_id} deleted", performed_by=performed_by)
    bump_cache()


def get_source_objects_for_target(object_id: int) -> List[str]:
    """
    Technical names of every source object feeding the rule sets that target this
    object. These are the objects whose columns can be offered as pass-through
    candidates.

    Reads RULE_SET_SOURCE and also RULE_SET.SOURCE_OBJECT_ID: older rule sets
    predate RULE_SET_SOURCE and carry only the single-source column, so relying
    on the join alone would offer no candidates for them.
    """
    try:
        df = cached_read(f"""
            SELECT DISTINCT bo.TECHNICAL_OBJECT_NAME AS OBJ
            FROM {tbl('RULE_SET')} rs
            JOIN {tbl('RULE_SET_SOURCE')} rss ON rss.RULE_SET_ID = rs.RULE_SET_ID
            JOIN {tbl('BUSINESS_OBJECT')} bo ON bo.OBJECT_ID = rss.OBJECT_ID
            WHERE rs.TARGET_OBJECT_ID = {sql_int(object_id)}
              AND bo.TECHNICAL_OBJECT_NAME IS NOT NULL
            UNION
            SELECT DISTINCT bo.TECHNICAL_OBJECT_NAME
            FROM {tbl('RULE_SET')} rs
            JOIN {tbl('BUSINESS_OBJECT')} bo ON bo.OBJECT_ID = rs.SOURCE_OBJECT_ID
            WHERE rs.TARGET_OBJECT_ID = {sql_int(object_id)}
              AND bo.TECHNICAL_OBJECT_NAME IS NOT NULL
            ORDER BY OBJ
        """)
        return [str(v) for v in df["OBJ"].tolist() if v] if not df.empty else []
    except Exception:
        return []


def get_rule_sets_for_target(object_id: int) -> pd.DataFrame:
    """
    Rule sets that write to this object, newest last.

    The column builder needs this so a rule typed against a column can be filed
    without leaving the page to hunt for its rule set. Returns an empty frame
    rather than raising, so the caller can offer inline creation instead.
    """
    columns = ["RULE_SET_ID", "RULE_SET_NAME", "MODULE_ID", "RULE_COUNT"]
    try:
        return cached_read(f"""
            SELECT rs.RULE_SET_ID, rs.RULE_SET_NAME, rs.MODULE_ID,
                   COUNT(r.RULE_ID) AS RULE_COUNT
            FROM {tbl('RULE_SET')} rs
            LEFT JOIN {tbl('RULE')} r ON r.RULE_SET_ID = rs.RULE_SET_ID
            WHERE rs.TARGET_OBJECT_ID = {sql_int(object_id)}
            GROUP BY rs.RULE_SET_ID, rs.RULE_SET_NAME, rs.MODULE_ID
            ORDER BY rs.RULE_SET_ID
        """)
    except Exception:
        return pd.DataFrame(columns=columns)


def get_rules_by_target_column(object_id: int) -> pd.DataFrame:
    """
    Which rules name each column of this object as an action target.

    Scoped to rule sets targeting this object. An unscoped match on
    RULE_ACTION.TARGET_FIELD alone would attach the wrong rules, because a name
    like ACTIVE_IND is a target on many different objects.
    """
    columns = ["TARGET_FIELD", "RULE_ID", "RULE_NUMBER", "RULE_NAME", "ACTION_TYPE"]
    try:
        return cached_read(f"""
            SELECT UPPER(a.TARGET_FIELD) AS TARGET_FIELD,
                   r.RULE_ID, r.RULE_NUMBER, r.RULE_NAME, a.ACTION_TYPE
            FROM {tbl('RULE_SET')} rs
            JOIN {tbl('RULE')} r ON r.RULE_SET_ID = rs.RULE_SET_ID
            JOIN {tbl('RULE_ACTION')} a ON a.RULE_ID = r.RULE_ID
            WHERE rs.TARGET_OBJECT_ID = {sql_int(object_id)}
              AND a.TARGET_FIELD IS NOT NULL
            ORDER BY TARGET_FIELD, r.RULE_NUMBER
        """)
    except Exception:
        return pd.DataFrame(columns=columns)


# ------------------------------------------------------------------
# Teams
# ------------------------------------------------------------------
# A team sits one level above a module, so several teams can share the app
# without seeing each other's modules. MODULE.TEAM_ID is nullable: a module with
# no team stays visible under "All teams", which is also the state in which the
# app behaves exactly as it did before teams existed.
def list_teams(active_only: bool = True) -> pd.DataFrame:
    """Every team, with how many modules sit under it."""
    columns = ["TEAM_ID", "TEAM_NAME", "TEAM_CODE", "DESCRIPTION",
               "ACTIVE_IND", "MODULE_COUNT"]
    where = "WHERE t.ACTIVE_IND = TRUE" if active_only else ""
    try:
        return cached_read(f"""
            SELECT t.TEAM_ID, t.TEAM_NAME, t.TEAM_CODE, t.DESCRIPTION, t.ACTIVE_IND,
                   (SELECT COUNT(*) FROM {tbl('MODULE')} m
                     WHERE m.TEAM_ID = t.TEAM_ID) AS MODULE_COUNT
            FROM {tbl('TEAM')} t
            {where}
            ORDER BY t.TEAM_NAME
        """)
    except Exception:
        return pd.DataFrame(columns=columns)


def create_team(team_name: str, team_code: str = "", description: str = "",
                created_by: Optional[str] = None) -> Optional[int]:
    """Add a team. Returns the new id, or None when the name is blank."""
    name = _text(team_name)
    if not name:
        return None
    team_id = reserve_ids("TEAM", "TEAM_ID", 1)[0]
    insert_rows(
        "TEAM",
        ["TEAM_ID", "TEAM_NAME", "TEAM_CODE", "DESCRIPTION"],
        [[str(team_id), sql_value(name), sql_value(team_code), sql_value(description)]],
    )
    log_audit_event("TEAM", team_id, "TEAM_CREATED",
                    f"Created team '{name}'", performed_by=created_by)
    bump_cache()
    return team_id


def rename_team(team_id: int, team_name: str, team_code: str = "",
                description: str = "", performed_by: Optional[str] = None) -> None:
    who = _text(performed_by)
    execute(f"""
        UPDATE {tbl('TEAM')}
        SET TEAM_NAME = {sql_value(team_name)},
            TEAM_CODE = {sql_value(team_code)},
            DESCRIPTION = {sql_value(description)},
            UPDATED_BY = {_actor(who)},
            UPDATED_TS = CURRENT_TIMESTAMP()
        WHERE TEAM_ID = {sql_int(team_id)}
    """)
    log_audit_event("TEAM", team_id, "TEAM_RENAMED",
                    f"Renamed to '{team_name}'", performed_by=who)
    bump_cache()


def reassign_module_team(module_id: int, team_id: Optional[int],
                         performed_by: Optional[str] = None) -> None:
    """Move a module under a different team, or detach it with team_id=None."""
    who = _text(performed_by)
    team_sql = "NULL" if team_id is None else sql_int(team_id)
    execute(f"""
        UPDATE {tbl('MODULE')}
        SET TEAM_ID = {team_sql},
            UPDATED_BY = {_actor(who)},
            UPDATED_TS = CURRENT_TIMESTAMP()
        WHERE MODULE_ID = {sql_int(module_id)}
    """)
    log_audit_event("TEAM", team_id, "MODULE_REASSIGNED",
                    f"Module {module_id} moved to team {team_id or '(none)'}",
                    performed_by=who)
    bump_cache()


def get_team_module_ids(team_id: Optional[int]) -> Optional[set]:
    """
    The MODULE_IDs a team owns, for scoping the pages.

    Returns None for "no team selected", which callers treat as no filtering at
    all. That is deliberately distinct from an empty set, which means the team is
    real but owns nothing yet - the pages must then show nothing rather than
    everything, or a brand new team would silently see another team's work.
    """
    if team_id is None:
        return None
    try:
        df = cached_read(f"""
            SELECT MODULE_ID FROM {tbl('MODULE')}
            WHERE TEAM_ID = {sql_int(team_id)}
        """)
        return {int(v) for v in df["MODULE_ID"].tolist()} if not df.empty else set()
    except Exception:
        return None


# ------------------------------------------------------------------
# Per-viewer preferences
# ------------------------------------------------------------------
def get_default_team(user_name: str) -> Optional[int]:
    """The team this viewer chose to default to, if they ticked the box."""
    who = _text(user_name)
    if not who:
        return None
    try:
        df = cached_read(f"""
            SELECT DEFAULT_TEAM_ID FROM {tbl('USER_PREFERENCE')}
            WHERE USER_NAME = {sql_value(who)}
        """)
        if df.empty:
            return None
        value = df.iloc[0]["DEFAULT_TEAM_ID"]
        return None if pd.isna(value) else int(value)
    except Exception:
        return None


def set_default_team(user_name: str, team_id: Optional[int]) -> None:
    """
    Store or clear a viewer's default team.

    Delete-then-insert rather than MERGE: USER_PREFERENCE has no key constraint,
    so this keeps one row per user without relying on one.
    """
    who = _text(user_name)
    if not who:
        return
    execute(f"DELETE FROM {tbl('USER_PREFERENCE')} WHERE USER_NAME = {sql_value(who)}")
    if team_id is not None:
        insert_rows(
            "USER_PREFERENCE",
            ["USER_NAME", "DEFAULT_TEAM_ID"],
            [[sql_value(who), sql_int(team_id)]],
        )
    bump_cache()


# ------------------------------------------------------------------
# Rule notes: what the next reader of a rule should know
# ------------------------------------------------------------------
NOTE_TYPES = ("NOTE", "OPEN", "RESOLVED", "INFO")


def add_rule_notes(rule_id: int, notes: List[tuple],
                   performed_by: Optional[str] = None) -> int:
    """Attach notes to a rule. notes: [(note_type, text, source), ...].

    A note already on the rule (same text, active) is not added again, so
    re-importing an FDD or re-saving a rule does not stack duplicates. Returns how
    many were written.
    """
    rule_id = int(rule_id)
    clean = []
    for note_type, text, source in notes or []:
        text = (_text(text) or "").strip()
        if not text:
            continue
        note_type = str(note_type or "NOTE").upper()
        clean.append((note_type if note_type in NOTE_TYPES else "NOTE", text[:4000],
                      str(source or "MANUAL").upper()[:30]))
    if not clean:
        return 0
    existing = cached_read(f"""
        SELECT NOTE_TEXT FROM {tbl('RULE_NOTE')}
        WHERE RULE_ID = {sql_int(rule_id)} AND COALESCE(ACTIVE_IND, TRUE)
    """)
    seen = {str(t).strip().upper() for t in
            (existing["NOTE_TEXT"].tolist() if not existing.empty else [])}
    rows = []
    for note_type, text, source in clean:
        if text.upper() in seen:
            continue
        seen.add(text.upper())
        rows.append((note_type, text, source))
    if not rows:
        return 0
    ids = reserve_ids("RULE_NOTE", "NOTE_ID", len(rows))
    who = _actor(performed_by)
    insert_rows(
        "RULE_NOTE",
        ["NOTE_ID", "RULE_ID", "NOTE_TYPE", "NOTE_TEXT", "SOURCE", "CREATED_BY"],
        [[str(ids[i]), str(rule_id), sql_value(t), sql_value(x), sql_value(src), who]
         for i, (t, x, src) in enumerate(rows)],
    )
    bump_cache()
    return len(rows)


def get_rule_notes(rule_ids) -> pd.DataFrame:
    """Active notes for one rule id or a list of them, oldest first."""
    ids = [int(r) for r in ([rule_ids] if isinstance(rule_ids, (int, float, str))
                            else list(rule_ids or []))]
    if not ids:
        return pd.DataFrame()
    try:
        return cached_read(f"""
            SELECT NOTE_ID, RULE_ID, NOTE_TYPE, NOTE_TEXT, SOURCE, CREATED_BY, CREATED_TS
            FROM {tbl('RULE_NOTE')}
            WHERE RULE_ID IN ({", ".join(str(i) for i in ids)})
              AND COALESCE(ACTIVE_IND, TRUE)
            ORDER BY RULE_ID, CREATED_TS, NOTE_ID
        """)
    except Exception:
        return pd.DataFrame()


def update_rule_note(note_id: int, note_type: Optional[str] = None,
                     active: Optional[bool] = None,
                     performed_by: Optional[str] = None) -> None:
    """Change a note's type (e.g. OPEN -> RESOLVED) or hide it. Never deletes."""
    sets = []
    if note_type:
        sets.append(f"NOTE_TYPE = {sql_value(str(note_type).upper())}")
    if active is not None:
        sets.append(f"ACTIVE_IND = {'TRUE' if active else 'FALSE'}")
    if not sets:
        return
    execute(f"UPDATE {tbl('RULE_NOTE')} SET {', '.join(sets)} "
            f"WHERE NOTE_ID = {sql_int(note_id)}")
    log_audit_event("RULE_NOTE", int(note_id), "NOTE_UPDATED", "; ".join(sets),
                    performed_by=performed_by)
    bump_cache()


# ------------------------------------------------------------------
# Module hand-off to development (create_module_handoff.sql)
# ------------------------------------------------------------------
# The lifecycle is shared with pdh-engineering-studio: both apps read
# VW_MODULE_DEV_STATUS / VW_MODULE_DEV_OBJECTS / VW_RULE_CHANGES_FOR_DEV and change a
# module's status only through SP_SET_MODULE_DEV_STATUS, which enforces the entry
# conditions. This app reports rule changes made after a hand-off; it never decides
# that code is out of date - the developer does.
DEV_STATES = ("IN_REVIEW", "READY_FOR_DEV", "IN_DEV", "DEV_COMPLETED")
DEV_DECISIONS = ("READY_FOR_DEV", "DEV_COMPLETED", "WITHDRAWN", "REOPENED")
# Handing over and taking back are product-owner decisions; closing and reopening
# development are the developer's.
APPROVER_DECISIONS = ("READY_FOR_DEV", "WITHDRAWN")


def get_module_dev_status(module_id: Optional[int] = None) -> pd.DataFrame:
    """VW_MODULE_DEV_STATUS, for one module or all. Empty if the view is missing."""
    where = f"WHERE MODULE_ID = {sql_int(module_id)}" if module_id is not None else ""
    try:
        return cached_read(f"""
            SELECT * FROM {vw('VW_MODULE_DEV_STATUS')} {where}
            ORDER BY MODULE_NAME
        """)
    except Exception:
        return pd.DataFrame()


def get_module_dev_objects(module_id: int) -> pd.DataFrame:
    """One row per target object of the module: latest code, review, changes since."""
    try:
        return cached_read(f"""
            SELECT * FROM {vw('VW_MODULE_DEV_OBJECTS')}
            WHERE MODULE_ID = {sql_int(module_id)}
            ORDER BY TARGET_OBJECT
        """)
    except Exception:
        return pd.DataFrame()


def get_rule_changes_for_dev(module_id: int, since=None,
                             target_object: Optional[str] = None) -> pd.DataFrame:
    """Rule changes in a module after `since`, newest first, tagged LOGIC / WORDING."""
    filters = [f"MODULE_ID = {sql_int(module_id)}"]
    if since is not None and not pd.isna(since):
        filters.append(f"CHANGED_TS > {sql_value(str(since))}::TIMESTAMP_NTZ")
    if _text(target_object):
        filters.append(f"TARGET_OBJECT = {sql_value(str(target_object).upper())}")
    try:
        return cached_read(f"""
            SELECT CHANGE_ID, RULE_ID, RULE_NUMBER, RULE_NAME, RULE_SET_NAME, TARGET_OBJECT,
                   CHANGE_TYPE, CHANGE_KIND, OLD_VALUE, NEW_VALUE, CHANGED_BY, CHANGED_TS
            FROM {vw('VW_RULE_CHANGES_FOR_DEV')}
            WHERE {' AND '.join(filters)}
            ORDER BY CHANGED_TS DESC, CHANGE_ID DESC
        """)
    except Exception:
        return pd.DataFrame()


def get_module_handoff_history(module_id: int) -> pd.DataFrame:
    """Every hand-off decision for a module, newest first."""
    try:
        return cached_read(f"""
            SELECT HANDOFF_ID, STATUS, RULE_COUNT, NOTE, SET_BY, SET_TS, SOURCE_APP
            FROM {tbl('MODULE_HANDOFF')}
            WHERE MODULE_ID = {sql_int(module_id)}
            ORDER BY SET_TS DESC, HANDOFF_ID DESC
        """)
    except Exception:
        return pd.DataFrame()


def handoff_blockers(status_row: Optional[Dict], decision: str) -> List[str]:
    """Why `decision` cannot be taken right now - the same checks as the procedure.

    Used to disable buttons with a reason before anyone clicks. The procedure stays
    the authority: it re-checks, because the two apps can act at the same time.
    """
    decision = str(decision or "").upper()
    if decision not in DEV_DECISIONS:
        return [f"unknown decision {decision}"]
    if not status_row:
        return ["the module's dev status could not be read"]
    state = str(status_row.get("DEV_STATE") or "IN_REVIEW")
    live = int(status_row.get("LIVE_RULES") or 0)
    accepted = int(status_row.get("ACCEPTED_RULES") or 0)
    objects = int(status_row.get("OBJECTS") or 0)
    coded = int(status_row.get("OBJECTS_WITH_CODE") or 0)
    reviewed = int(status_row.get("OBJECTS_REVIEWED") or 0)
    reasons = []
    if decision == "READY_FOR_DEV":
        if live == 0:
            reasons.append("the module has no live rules")
        elif accepted < live:
            reasons.append(f"{live - accepted} of {live} rules are not accepted yet")
        if state == "DEV_COMPLETED":
            reasons.append("development is completed; reopen it instead")
    elif decision == "DEV_COMPLETED":
        if state not in ("READY_FOR_DEV", "IN_DEV"):
            reasons.append("the module has not been handed to development"
                           if state == "IN_REVIEW" else "development is already completed")
        else:
            if coded < objects:
                reasons.append(f"{objects - coded} of {objects} objects have no generated code")
            if reviewed < objects:
                reasons.append(f"{objects - reviewed} of {objects} objects have no approved review")
    elif decision == "WITHDRAWN":
        if state == "IN_REVIEW":
            reasons.append("the module has not been handed to development")
    elif decision == "REOPENED":
        if state != "DEV_COMPLETED":
            reasons.append("only a completed module can be reopened")
    return reasons


def set_module_dev_status(module_id: int, decision: str, note: Optional[str] = None,
                          performed_by: Optional[str] = None,
                          owner_team_id: Optional[int] = None) -> str:
    """Record a hand-off decision through SP_SET_MODULE_DEV_STATUS.

    Handing over and withdrawing need an approver for the module's team - checked
    here, because under owner's rights the procedure cannot see who the viewer is.
    Raises PermissionError for that, ValueError when the procedure refuses.
    """
    decision = str(decision or "").upper()
    if decision not in DEV_DECISIONS:
        raise ValueError(f"Unknown decision {decision}.")
    if decision in APPROVER_DECISIONS and not can_approve_team(performed_by, owner_team_id):
        raise PermissionError("Only an approver for this module's team can "
                              + ("hand it to development." if decision == "READY_FOR_DEV"
                                 else "withdraw the hand-off."))
    df = run_query(
        f"CALL {tbl('SP_SET_MODULE_DEV_STATUS')}({sql_int(module_id)}, "
        f"{sql_value(decision)}, {sql_value(_text(note))}, "
        f"{sql_value(_text(performed_by))}, 'RULE_REPOSITORY')"
    )
    result = str(df.iloc[0, 0]) if df is not None and not df.empty else ""
    if not result.startswith("OK"):
        raise ValueError(result.replace("REFUSED: ", "", 1) or "The procedure returned nothing.")
    bump_cache()
    return result
