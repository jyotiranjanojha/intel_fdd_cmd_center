"""
Two-pane column builder behind the Object columns page.

An earlier version of this page split one job across three tabs: read a source's
columns, declare straight pulls, declare derived ones. A BA writing a spec moves
between all three constantly, so the tabs cost more than they organised. This module
puts the source on the left and the spec being built on the right, the way the FDD
itself is laid out. That older page has been removed; keeping both meant two
implementations of the same screen, and their copies of PSEUDO_COLUMNS had already
drifted apart.

Three things are deliberate:

  The right pane is the staging area, not a mirror of the database. Ticks, edits,
  new rows and deletions all accumulate until Save spec, so a BA can work through
  a whole object in one sitting and commit once. The working rows live in session
  state rather than in st.data_editor's internal state, because a tick on the
  left has to be able to add a row on the right — which means re-rendering the
  editor from a new frame, and any deltas held only inside the widget would be
  lost when it resets.

  A rule typed against a column is not saved silently. It goes through the same
  compile_requirement -> preview -> save_rule path as the Write a rule page, with
  the same errors and warnings, because a rule the BA has not read back is worse
  than no rule at all. The queue drains one at a time to keep that review
  readable.

  Columns are not the grain rules are written at. An FDD numbers its rules per hop,
  and one of them may set several columns or act on the whole record — which is why
  RULE_ACTION.TARGET_FIELD accepts RECORD and KEY_COLUMNS. So this page offers
  linking a column to a rule that already exists, not only writing a new one:
  typing the same requirement against five columns would produce five near-duplicate
  rules where the document has one. The column view is derived from
  RULE_ACTION.TARGET_FIELD, so one rule with five actions already shows against all
  five columns.

Nothing here writes to the database except through repository functions, and it
shares the Object columns page's helpers rather than reimplementing them: they
arrive in the `context` dict so this module never imports streamlit_app.
"""

from typing import Dict, List

import pandas as pd
import streamlit as st

from compliance import PSEUDO_COLUMNS, get_columns, object_exists
from repository import (add_object_columns, create_rule_set,
                        delete_object_column, get_object_column_spec,
                        get_rule_sets_for_target, get_rules_by_target_column,
                        link_column_to_rule, link_rule_set_objects,
                        next_rule_number, rules_available_for_column, save_rule,
                        update_object_column)
from requirement_compiler import compile_requirement

# PSEUDO_COLUMNS comes from compliance rather than being restated here. RULE_ACTION
# uses those names as a stand-in for a row-level action instead of naming a real
# column, so they must never be reported as columns missing from the spec - and when
# the old Object columns page kept its own copy the two had already drifted: that one
# omitted "ROW", so the two pages disagreed about whether a ROW-level action counted
# as an uncovered column.

ORIGINS = ["PASS_THROUGH", "DERIVED", "DISCOVERED"]

# How many candidate columns the left pane will draw at once. A wide source would
# otherwise make the page unusable; the search box is the way through a long list.
CANDIDATE_LIMIT = 60


# ------------------------------------------------------------------
# Working-row state
# ------------------------------------------------------------------
def _rows_key(object_id: int) -> str:
    return f"oc2_rows_{object_id}"


def _base_key(object_id: int) -> str:
    return f"oc2_base_{object_id}"


def _version_key(object_id: int) -> str:
    return f"oc2_ver_{object_id}"


def _row_from_spec(record) -> Dict:
    """One stored column, as a working row."""
    return {
        "spec_id": int(record["COLUMN_SPEC_ID"]),
        "ordinal": int(record["ORDINAL"]) if pd.notna(record["ORDINAL"]) else 1,
        "column": str(record["COLUMN_NAME"] or "").upper(),
        "type": str(record["DATA_TYPE"] or ""),
        "nullable": bool(record["NULLABLE_IND"]) if pd.notna(record["NULLABLE_IND"]) else True,
        "pk": bool(record["PRIMARY_KEY_IND"]) if pd.notna(record["PRIMARY_KEY_IND"]) else False,
        "origin": str(record["COLUMN_ORIGIN"] or "DERIVED"),
        "source_object": str(record["SOURCE_OBJECT"] or ""),
        "source_column": str(record["SOURCE_COLUMN"] or ""),
        "description": str(record["BUSINESS_DESCRIPTION"] or ""),
        # Rule text is transient: it is a request to author a rule, not a
        # property of the column, so it is never read back from the database.
        "rule": "",
    }


def _load_rows(object_id: int, spec: pd.DataFrame) -> List[Dict]:
    """
    Seed the working rows from the stored spec, once per object.

    The baseline is kept alongside so Save spec can send only changed cells, and
    so the Status column can tell a stored row from an edited one.
    """
    key, base = _rows_key(object_id), _base_key(object_id)
    if key not in st.session_state:
        rows = [] if spec.empty else [_row_from_spec(r) for _, r in spec.iterrows()]
        st.session_state[key] = rows
        st.session_state[base] = {r["spec_id"]: dict(r) for r in rows}
        st.session_state.setdefault(_version_key(object_id), 0)
    return st.session_state[key]


def _reset_rows(object_id: int) -> None:
    """Drop the working rows so the next run reseeds them from the database."""
    for key in (_rows_key(object_id), _base_key(object_id)):
        st.session_state.pop(key, None)
    st.session_state[_version_key(object_id)] = \
        st.session_state.get(_version_key(object_id), 0) + 1


def _bump(object_id: int) -> None:
    """
    Force st.data_editor to re-render from the working rows.

    Changing the key is the only way to make the editor pick up rows added from
    outside it. Edits already made are safe because the editor's return value is
    written back into session state on every run, before this is ever called.
    """
    st.session_state[_version_key(object_id)] = \
        st.session_state.get(_version_key(object_id), 0) + 1


def reset_object(object_id: int) -> None:
    """
    Discard the working rows for an object.

    Public because the page has to call it after syncing columns from Snowflake:
    the newly stored columns would otherwise stay invisible behind the working
    copy until the object was reselected.
    """
    _reset_rows(object_id)


def _next_ordinal(rows: List[Dict]) -> int:
    return max([int(r.get("ordinal") or 0) for r in rows], default=0) + 1


def _status(row: Dict, baseline: Dict) -> str:
    if row.get("spec_id") is None:
        return "new"
    was = baseline.get(row["spec_id"])
    if not was:
        return "stored"
    compared = ("ordinal", "column", "type", "nullable", "pk", "origin",
                "source_object", "source_column", "description")
    return "edited" if any(was.get(f) != row.get(f) for f in compared) else "stored"


# ------------------------------------------------------------------
# Left pane - read a source
# ------------------------------------------------------------------
def _left_pane(context: Dict, rows: List[Dict]) -> None:
    object_id = context["object_id"]
    objects_df = context["objects_df"]
    source_objects = context["source_objects"]
    column_types = context["source_column_types"]

    st.markdown("##### 1 · Read a source")

    extra = st.multiselect(
        "Also offer columns from",
        sorted(objects_df["TECHNICAL_OBJECT_NAME"].dropna().astype(str).unique().tolist()),
        key="oc2_extra_sources",
        help="Use this when the FDD reads from an object not yet linked to a rule set.",
    )
    candidates = list(dict.fromkeys(list(source_objects) + list(extra)))

    if not candidates:
        st.info("No source objects are linked to the rule sets targeting this object. "
                "Name one above, or add columns straight into the pane on the right.")
        _paste_panel(context, rows)
        return

    # Anything already on the right - stored or newly ticked - is not a candidate
    # to add again.
    taken = {str(r.get("column") or "").upper() for r in rows}

    pool = []
    for src in candidates:
        cols = get_columns(src)
        if not cols:
            st.caption(f":grey[No columns readable on `{src}` — it may not exist, or may not "
                       "be visible to this app's role.]")
            continue
        types = column_types(src)
        for col in sorted(cols):
            pool.append({"source_object": src, "column": col,
                         "data_type": types.get(col, ""),
                         "on_spec": col.upper() in taken})

    if not pool:
        st.info("No columns found on those sources.")
        _paste_panel(context, rows)
        return

    search = st.text_input("Search", key="oc2_search",
                           placeholder="Column, source object or data type")
    hide_taken = st.checkbox("Hide columns already on the spec", value=True,
                             key="oc2_hide_taken")

    needle = search.strip().upper()
    shown = [
        r for r in pool
        if (not needle or needle in r["column"].upper()
            or needle in r["source_object"].upper()
            or needle in str(r["data_type"]).upper())
        and not (hide_taken and r["on_spec"])
    ]

    st.caption(f"{len(shown)} of {len(pool)} column(s)"
               + (f", filtered by “{search.strip()}”" if needle else ""))

    if not shown:
        st.info("Nothing matches that search.")
        _paste_panel(context, rows)
        return

    clipped = shown[:CANDIDATE_LIMIT]
    if len(shown) > CANDIDATE_LIMIT:
        st.caption(f":grey[Showing the first {CANDIDATE_LIMIT}. Narrow the search for the rest.]")

    grid = pd.DataFrame([{
        "Add": False,
        "Column": r["column"],
        "Type": r["data_type"],
        "Source": r["source_object"].split(".")[-1],
        "On spec": "✓" if r["on_spec"] else "",
    } for r in clipped])

    ticked = st.data_editor(
        grid, use_container_width=True, hide_index=True,
        height=min(520, 80 + 35 * min(len(clipped), 12)),
        key=f"oc2_candidates_{object_id}",
        column_config={
            "Add": st.column_config.CheckboxColumn("Add", width="small"),
            "Column": st.column_config.TextColumn(disabled=True),
            "Type": st.column_config.TextColumn(disabled=True, width="small"),
            "Source": st.column_config.TextColumn(disabled=True, width="small"),
            "On spec": st.column_config.TextColumn(disabled=True, width="small"),
        },
    )

    if st.button("Add ticked to spec  →", type="primary", key="oc2_add_ticked",
                 use_container_width=True):
        chosen = ticked[ticked["Add"]]
        if chosen.empty:
            st.warning("Nothing ticked.")
        else:
            # Map the shortened source label back to the full technical name.
            full_name = {r["source_object"].split(".")[-1]: r["source_object"] for r in clipped}
            added, ordinal = 0, _next_ordinal(rows)
            for _, r in chosen.iterrows():
                name = str(r["Column"]).upper()
                if name in {str(x.get("column") or "").upper() for x in rows}:
                    continue
                rows.append({
                    "spec_id": None, "ordinal": ordinal, "column": name,
                    "type": str(r["Type"] or ""), "nullable": True, "pk": False,
                    "origin": "PASS_THROUGH",
                    "source_object": full_name.get(str(r["Source"]), str(r["Source"])),
                    "source_column": name, "description": "", "rule": "",
                })
                ordinal += 1
                added += 1
            st.session_state[_rows_key(object_id)] = rows
            _bump(object_id)
            st.session_state["oc2_flash"] = (
                f"{added} column(s) staged as pass-through. Nothing is written until "
                "you press Save spec." if added else "Those columns are already on the spec."
            )
            st.rerun()

    _paste_panel(context, rows)


def _paste_panel(context: Dict, rows: List[Dict]) -> None:
    """
    Paste a column list straight from Excel.

    Kept because there is no other way to get a long FDD column list in without
    the source object existing in Snowflake first.
    """
    object_id = context["object_id"]
    source_objects = context["source_objects"]
    parse = context["parse_pasted_columns"]

    with st.expander("Paste a column list from Excel"):
        st.caption("One column per line, tab or comma separated: name, then optionally "
                   "data type, then description. A name matching a source column is "
                   "staged as pass-through, everything else as derived.")
        pasted = st.text_area("Rows", height=140, key="oc2_paste",
                              placeholder="ITEM_ID\tVARCHAR(18)\tItem identifier\n"
                                          "OWNER_CD\tVARCHAR(10)\tDerived owner code")
        if st.button("Stage pasted columns", key="oc2_paste_add"):
            parsed = parse(pasted)
            if not parsed:
                st.warning("Nothing readable in that paste.")
                return
            # Matching the name against the sources is what stops a pasted list
            # landing as entirely derived and overstating the rule gap.
            lookup = {}
            for src in source_objects:
                for col in get_columns(src):
                    lookup.setdefault(col.upper(), src)
            taken = {str(r.get("column") or "").upper() for r in rows}
            added, ordinal = 0, _next_ordinal(rows)
            for entry in parsed:
                name = str(entry["column_name"]).upper()
                if name in taken:
                    continue
                src = lookup.get(name)
                rows.append({
                    "spec_id": None, "ordinal": ordinal, "column": name,
                    "type": entry.get("data_type", ""), "nullable": True, "pk": False,
                    "origin": "PASS_THROUGH" if src else "DERIVED",
                    "source_object": src or "", "source_column": name if src else "",
                    "description": entry.get("business_description", ""), "rule": "",
                })
                taken.add(name)
                ordinal += 1
                added += 1
            st.session_state[_rows_key(object_id)] = rows
            _bump(object_id)
            st.session_state["oc2_flash"] = f"{added} column(s) staged from the paste."
            st.rerun()


# ------------------------------------------------------------------
# Right pane - the spec being built
# ------------------------------------------------------------------
def _right_pane(context: Dict, rows: List[Dict]) -> None:
    object_id = context["object_id"]
    baseline = st.session_state.get(_base_key(object_id), {})

    st.markdown("##### 2 · The spec")
    st.caption("Edit any cell. The **+** row at the bottom adds a derived column. "
               "Type a rule in plain English and it is compiled for review when you save.")

    frame = pd.DataFrame([{
        "Status": _status(r, baseline),
        "#": int(r.get("ordinal") or 1),
        "Column": r.get("column", ""),
        "Type": r.get("type", ""),
        "Null?": bool(r.get("nullable", True)),
        "PK": bool(r.get("pk", False)),
        "Origin": r.get("origin") or "DERIVED",
        "Source object": r.get("source_object", ""),
        "Source column": r.get("source_column", ""),
        "Rule (plain English)": r.get("rule", ""),
        "Description": r.get("description", ""),
        "_spec_id": r.get("spec_id"),
    } for r in rows])

    if frame.empty:
        # An empty dynamic editor still needs its columns declared, or the first
        # added row has nowhere to land.
        frame = pd.DataFrame({
            "Status": pd.Series(dtype="object"), "#": pd.Series(dtype="int64"),
            "Column": pd.Series(dtype="object"), "Type": pd.Series(dtype="object"),
            "Null?": pd.Series(dtype="bool"), "PK": pd.Series(dtype="bool"),
            "Origin": pd.Series(dtype="object"),
            "Source object": pd.Series(dtype="object"),
            "Source column": pd.Series(dtype="object"),
            "Rule (plain English)": pd.Series(dtype="object"),
            "Description": pd.Series(dtype="object"),
            "_spec_id": pd.Series(dtype="object"),
        })

    version = st.session_state.get(_version_key(object_id), 0)
    edited = st.data_editor(
        frame,
        key=f"oc2_spec_{object_id}_{version}",
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        height=min(620, 120 + 35 * max(len(frame), 4)),
        column_config={
            # Status and _spec_id are bookkeeping: the BA reads one and never
            # needs to see the other.
            "Status": st.column_config.TextColumn(
                "", disabled=True, width="small",
                help="new = not yet written · edited = changed since it was loaded"),
            "_spec_id": None,
            "#": st.column_config.NumberColumn("#", min_value=1, step=1, width="small"),
            "Column": st.column_config.TextColumn("Column", required=True),
            "Type": st.column_config.TextColumn("Type", width="small"),
            "Null?": st.column_config.CheckboxColumn("Null?", width="small"),
            "PK": st.column_config.CheckboxColumn("PK", width="small"),
            # DISCOVERED has to stay on offer: without it, editing a column read
            # back from Snowflake would silently reclassify it as declared intent,
            # which is exactly what the Verify panel compares against.
            "Origin": st.column_config.SelectboxColumn("Origin", options=ORIGINS,
                                                       required=True, width="medium"),
            "Source object": st.column_config.TextColumn("Source object"),
            "Source column": st.column_config.TextColumn("Source column"),
            "Rule (plain English)": st.column_config.TextColumn(
                "Rule", width="large",
                help="Describe the transformation. On save this is compiled into a rule "
                     "and shown for review before anything is written."),
            "Description": st.column_config.TextColumn("Description", width="medium"),
        },
    )

    # Write the editor's result back so a tick on the left, or the next rerun for
    # any other reason, does not lose what was just typed.
    st.session_state[_rows_key(object_id)] = [{
        "spec_id": (int(r["_spec_id"]) if pd.notna(r["_spec_id"]) else None),
        "ordinal": int(r["#"]) if pd.notna(r["#"]) else 1,
        "column": str(r["Column"] or "").upper(),
        "type": str(r["Type"] or ""),
        "nullable": bool(r["Null?"]) if pd.notna(r["Null?"]) else True,
        "pk": bool(r["PK"]) if pd.notna(r["PK"]) else False,
        "origin": str(r["Origin"] or "DERIVED"),
        "source_object": str(r["Source object"] or ""),
        "source_column": str(r["Source column"] or ""),
        "rule": str(r["Rule (plain English)"] or ""),
        "description": str(r["Description"] or ""),
    } for _, r in edited.iterrows() if str(r["Column"] or "").strip()]

    current = st.session_state[_rows_key(object_id)]
    _footer(context, current, baseline)


def _footer(context: Dict, rows: List[Dict], baseline: Dict) -> None:
    object_id = context["object_id"]

    new_rows = [r for r in rows if r["spec_id"] is None]
    live_ids = {r["spec_id"] for r in rows if r["spec_id"] is not None}
    removed = [i for i in baseline if i not in live_ids]
    changed = [r for r in rows if r["spec_id"] is not None
               and _status(r, baseline) == "edited"]
    queued = [r for r in rows if str(r.get("rule") or "").strip()]
    pending = len(new_rows) + len(removed) + len(changed)

    bits = []
    if new_rows:
        bits.append(f"{len(new_rows)} new")
    if changed:
        bits.append(f"{len(changed)} edited")
    if removed:
        bits.append(f"{len(removed)} removed")
    if queued:
        bits.append(f"{len(queued)} rule(s) to compile")

    st.markdown("")
    save, clear, note = st.columns([1.1, 1, 3])
    with save:
        if st.button("Save spec", type="primary", key="oc2_save",
                     disabled=not pending and not queued, use_container_width=True):
            _save(context, rows, baseline, new_rows, changed, removed, queued)
    with clear:
        if st.button("Discard changes", key="oc2_clear", disabled=not pending and not queued,
                     use_container_width=True):
            _reset_rows(object_id)
            st.rerun()
    with note:
        if pending or queued:
            st.caption(f"**Pending:** {' · '.join(bits)}. Nothing is written until you save.")
        else:
            st.caption(":grey[No pending changes.]")

    # A derived column with no rule is a real gap. A pass-through without one is
    # the expected state, so it is not counted.
    rules_by_col = get_rules_by_target_column(object_id)
    has_rule = (set(rules_by_col["TARGET_FIELD"].astype(str).str.upper())
                if not rules_by_col.empty else set())
    gaps = [r["column"] for r in rows
            if r["origin"] == "DERIVED"
            and r["column"].upper() not in has_rule
            and not str(r.get("rule") or "").strip()]
    if gaps:
        st.warning(f"{len(gaps)} derived column(s) have no rule: "
                   + ", ".join(f"`{g}`" for g in gaps[:12])
                   + (f" and {len(gaps) - 12} more" if len(gaps) > 12 else ""))
        _link_panel(context, gaps)

    if not rules_by_col.empty:
        row_level = rules_by_col[rules_by_col["TARGET_FIELD"].isin(PSEUDO_COLUMNS)]
        if not row_level.empty:
            with st.expander(f"{len(row_level)} row-level rule(s) — filters and pass-through, "
                             "not tied to one column"):
                st.dataframe(
                    row_level[["RULE_NUMBER", "RULE_NAME", "ACTION_TYPE", "TARGET_FIELD"]]
                    .rename(columns={"RULE_NUMBER": "Rule", "RULE_NAME": "Name",
                                     "ACTION_TYPE": "Action", "TARGET_FIELD": "Applies to"}),
                    use_container_width=True, hide_index=True)


def _link_panel(context: Dict, gaps: List[str]) -> None:
    """Attach an uncovered column to a rule that already exists.

    The alternative a column-first page pushes you towards is typing the requirement
    again, which produces a second rule saying what rule 3 already said. An FDD
    numbers its rules per hop and one rule routinely sets several columns, so the
    honest fix for "this column has no rule" is often "rule 3 covers it, say so"
    rather than "write a new rule".
    """
    object_id = context["object_id"]
    available = rules_available_for_column(object_id)
    with st.expander("Link a column to a rule that already exists"):
        if available is None or available.empty:
            st.caption(":grey[No live rules target this object yet, so there is nothing "
                       "to link to. Type the requirement against the column instead, or "
                       "write it on the Write a rule page.]")
            return
        st.caption(
            "Use this when the rule is already written and simply does not name the "
            "column. It adds one action to that rule — it does not create a second "
            "rule. An accepted rule returns to Draft, because adding a column changes "
            "what it does."
        )
        pick_col, pick_rule = st.columns([1, 2])
        with pick_col:
            column = st.selectbox("Column", gaps, key="oc2_link_col")
        with pick_rule:
            options = {
                f"{r['RULE_NUMBER']} — {r['RULE_NAME']} "
                f"({int(r['ACTION_COUNT'])} action(s), {r['RULE_STATUS']})":
                int(r["RULE_ID"]) for _, r in available.iterrows()
            }
            rule_label = st.selectbox("Covered by rule", list(options), key="oc2_link_rule")
        how, note = st.columns([1, 2])
        with how:
            action_type = st.selectbox(
                "Action", ["DERIVE", "SET_VALUE", "COPY", "LOOKUP",
                           "CONCAT", "DEFAULT", "FLAG"],
                key="oc2_link_action")
        with note:
            note_text = st.text_input("How the rule sets it (optional)",
                                      key="oc2_link_note",
                                      placeholder="Concatenated from the lab and office codes")
        if st.button("Link the column", type="primary", key="oc2_link_go"):
            try:
                added = link_column_to_rule(
                    options[rule_label], column, action_type=action_type,
                    action_description=note_text,
                    performed_by=context.get("viewer"))
                st.session_state["oc2_flash"] = (
                    f"`{column}` is now recorded against {rule_label.split(' — ')[0]}."
                    if added else
                    ("info", f"{rule_label.split(' — ')[0]} already names `{column}`, "
                             "so nothing changed."))
                st.rerun()
            except Exception as exc:
                st.error(f"Nothing was linked. {exc}")


def _save(context: Dict, rows: List[Dict], baseline: Dict,
          new_rows: List[Dict], changed: List[Dict], removed: List[int],
          queued: List[Dict]) -> None:
    object_id, viewer = context["object_id"], context["viewer"]

    _FIELDS = {"ordinal": "ordinal", "column": "column_name", "type": "data_type",
               "nullable": "nullable_ind", "pk": "primary_key_ind",
               "origin": "column_origin", "source_object": "source_object",
               "source_column": "source_column", "description": "business_description"}

    written, updated, deleted, failures = 0, 0, 0, []

    if new_rows:
        try:
            written = add_object_columns(object_id, [{
                "column_name": r["column"], "data_type": r["type"],
                "nullable_ind": r["nullable"], "primary_key_ind": r["pk"],
                "column_origin": r["origin"], "source_object": r["source_object"],
                "source_column": r["source_column"],
                "business_description": r["description"],
            } for r in new_rows], created_by=viewer)
        except Exception as exc:
            failures.append(f"Adding columns: {exc}")

    for row in changed:
        was = baseline.get(row["spec_id"], {})
        changes = {arg: row[field] for field, arg in _FIELDS.items()
                   if was.get(field) != row.get(field)}
        if not changes:
            continue
        try:
            update_object_column(int(row["spec_id"]), changes, performed_by=viewer)
            updated += 1
        except Exception as exc:
            failures.append(f"{row['column']}: {exc}")

    for spec_id in removed:
        try:
            delete_object_column(int(spec_id), performed_by=viewer)
            deleted += 1
        except Exception as exc:
            failures.append(f"Removing column {spec_id}: {exc}")

    for message in failures:
        st.error(message)

    # Rules are queued rather than compiled here: the review panel draws on the
    # run after this one, so it survives the rerun that refreshes the spec.
    if queued:
        st.session_state["oc2_rule_queue"] = [
            {"column": r["column"], "requirement": str(r["rule"]).strip(),
             "source_object": r.get("source_object") or ""}
            for r in queued if str(r.get("rule") or "").strip()
        ]

    parts = []
    if written:
        parts.append(f"{written} column(s) added")
    if updated:
        parts.append(f"{updated} updated")
    if deleted:
        parts.append(f"{deleted} removed")
    if queued:
        parts.append(f"{len(queued)} rule(s) ready to review below")
    st.session_state["oc2_flash"] = (
        ("Saved: " + ", ".join(parts) + ".") if parts else "Nothing changed."
    )
    _reset_rows(object_id)
    st.rerun()


# ------------------------------------------------------------------
# Rule review - one at a time, same path as Write a rule
# ------------------------------------------------------------------
def _rule_panel(context: Dict) -> None:
    queue = st.session_state.get("oc2_rule_queue") or []
    if not queue:
        return

    object_id = context["object_id"]
    technical_name = context["technical_name"]
    viewer = context["viewer"]
    item = queue[0]

    st.divider()
    st.markdown(f"### Review the rule for `{item['column']}`")
    st.caption(
        "This creates an ordinary rule on a rule set — the same thing the Write a rule "
        "page produces. Best for a derivation that belongs to this one column. A rule "
        "that populates several columns, filters rows or joins is better written once "
        "on **Write a rule**: it will still show against every column it touches."
    )
    if len(queue) > 1:
        st.caption(f"{len(queue)} rules queued. They are reviewed one at a time so each "
                   "preview stays readable.")

    requirement = st.text_area(
        "Requirement", value=item["requirement"], height=110, key="oc2_req",
        help="Edit it here if the compiled rule below does not read the way you meant.",
    )

    rule_sets = get_rule_sets_for_target(object_id)
    pick, number = st.columns([3, 1])
    with pick:
        if rule_sets.empty:
            st.warning("No rule set writes to this object yet. Create one below.")
            rule_set_id = None
        else:
            options = {
                f"{int(r['RULE_SET_ID'])} — {r['RULE_SET_NAME']} "
                f"({int(r['RULE_COUNT'])} rule(s))": int(r["RULE_SET_ID"])
                for _, r in rule_sets.iterrows()
            }
            rule_set_id = options[st.selectbox("Rule set", list(options), key="oc2_rs")]
    with number:
        rule_number = st.text_input(
            "Rule number",
            value=next_rule_number(rule_set_id) if rule_set_id is not None else "",
            key=f"oc2_num_{rule_set_id}")

    if rule_sets.empty:
        with st.expander("Create a rule set for this object", expanded=True):
            name = st.text_input("Rule set name", key="oc2_new_rs",
                                 value=f"{str(technical_name or '').split('.')[-1]} load")
            if st.button("Create rule set", key="oc2_new_rs_go"):
                if not name.strip():
                    st.error("Give the rule set a name.")
                else:
                    try:
                        new_id = create_rule_set(
                            name.strip(),
                            target_technical_object=str(technical_name or ""),
                            source_technical_object=item.get("source_object") or "",
                            performed_by=viewer)
                        st.session_state["oc2_flash"] = f"Rule set {new_id} created."
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Could not create the rule set: {exc}")

    act = st.columns([1.2, 1.2, 1, 3])
    with act[0]:
        prepare = st.button("Prepare the rule", type="primary", key="oc2_prep",
                            disabled=rule_set_id is None, use_container_width=True)
    with act[1]:
        skip = st.button("Skip this column", key="oc2_skip", use_container_width=True)
    with act[2]:
        skip_all = st.button("Skip all", key="oc2_skip_all", use_container_width=True)

    if skip or skip_all:
        st.session_state["oc2_rule_queue"] = [] if skip_all else queue[1:]
        st.session_state.pop("oc2_compiled", None)
        st.rerun()

    if prepare:
        source = item.get("source_object") or (
            context["source_objects"][0] if context["source_objects"] else "")
        with st.spinner("Reading the requirement…"):
            st.session_state["oc2_compiled"] = compile_requirement(
                requirement, rule_set_id, rule_number,
                source, str(technical_name or ""),
                model=context.get("writer_model") or "", use_llm=True)

    compiled = st.session_state.get("oc2_compiled")
    if not compiled:
        return

    st.markdown("#### Check this reads the way you meant it")
    st.markdown(compiled["preview"])
    for message in compiled["errors"]:
        st.error(message)
    for message in compiled["warnings"]:
        st.warning(message)

    with st.expander("What will be written to the tables"):
        proposal = compiled["proposal"]
        st.write("RULE")
        st.dataframe(pd.DataFrame([{k: v for k, v in proposal.items()
                                    if k not in ("conditions", "actions", "joins")}]),
                     use_container_width=True)
        st.write("RULE_CONDITION")
        st.dataframe(pd.DataFrame(proposal["conditions"]), use_container_width=True)
        st.write("RULE_ACTION")
        st.dataframe(pd.DataFrame(proposal["actions"]), use_container_width=True)
        if proposal["joins"]:
            st.write("RULE_JOIN")
            st.dataframe(pd.DataFrame(proposal["joins"]), use_container_width=True)

    keep, drop = st.columns([1, 3])
    with keep:
        if st.button("Save rule", type="primary", key="oc2_save_rule",
                     disabled=bool(compiled["errors"]), use_container_width=True):
            try:
                new_id = save_rule(compiled["proposal"], performed_by=context.get("viewer"))
                link_rule_set_objects(
                    compiled["proposal"].get("rule_set_id"),
                    source_technical_object=item.get("source_object") or "",
                    target_technical_object=str(technical_name or ""))
                saved = compiled["proposal"]
                st.session_state["oc2_rule_queue"] = queue[1:]
                st.session_state.pop("oc2_compiled", None)
                st.session_state["oc2_flash"] = (
                    f"Rule {saved.get('rule_number')} saved as RULE_ID {new_id}, status DRAFT.")
                # Hand off to the shared test-case panel, the same way the Write a
                # rule page does.
                st.session_state["oc2_test_ctx"] = {
                    "rule_id": int(new_id),
                    "rule_number": saved.get("rule_number"),
                    "proposal": saved,
                    "previous": None,
                    "target_object": str(technical_name or "") or None,
                    "source_action": "ON_CREATE",
                    "cases": None,
                }
                st.rerun()
            except Exception as exc:
                st.error(f"Nothing was saved. {exc}")
    with drop:
        if st.button("Discard this draft", key="oc2_drop_rule"):
            st.session_state.pop("oc2_compiled", None)
            st.rerun()


# ------------------------------------------------------------------
# FDD export
# ------------------------------------------------------------------
def render_export(object_name: str, technical_name: str, object_id: int,
                  spec: pd.DataFrame, source_objects: List[str]) -> None:
    if spec.empty:
        st.info("Declare some columns first.")
        return

    rules_by_col = get_rules_by_target_column(object_id)
    detail = {}
    if not rules_by_col.empty:
        for field, grp in rules_by_col.groupby("TARGET_FIELD"):
            detail[str(field).upper()] = [
                f"{r['RULE_NUMBER']} — {r['RULE_NAME']} ({r['ACTION_TYPE']})"
                for _, r in grp.iterrows()]

    lines = [
        f"# {object_name} — column specification", "",
        f"**Technical object:** `{technical_name or 'not set'}`  ",
        f"**Columns declared:** {len(spec)}  ",
        f"**Sources:** {', '.join(source_objects) if source_objects else 'none linked'}", "",
        "| # | Column | Type | Null | PK | Origin | Source | Rules | Description |",
        "|---|--------|------|------|----|--------|--------|-------|-------------|",
    ]
    for _, r in spec.iterrows():
        name = str(r["COLUMN_NAME"])
        rules = detail.get(name.upper(), [])
        source = (f"{r['SOURCE_OBJECT']}.{r['SOURCE_COLUMN']}"
                  if r["COLUMN_ORIGIN"] == "PASS_THROUGH" and r["SOURCE_OBJECT"] else "—")
        lines.append(
            f"| {r['ORDINAL']} | `{name}` | {r['DATA_TYPE'] or ''} | "
            f"{'Y' if r['NULLABLE_IND'] else 'N'} | {'Y' if r['PRIMARY_KEY_IND'] else ''} | "
            f"{r['COLUMN_ORIGIN']} | {source} | "
            f"{'; '.join(rules) if rules else '—'} | "
            f"{str(r['BUSINESS_DESCRIPTION'] or '').replace('|', '/')} |")

    missing = [str(r["COLUMN_NAME"]) for _, r in spec.iterrows()
               if r["COLUMN_ORIGIN"] == "DERIVED"
               and not detail.get(str(r["COLUMN_NAME"]).upper())]
    if missing:
        lines += ["", "## Derived columns still needing a rule", ""]
        lines += [f"- `{m}`" for m in missing]

    markdown = "\n".join(lines)
    st.markdown(markdown)

    stem = (technical_name or object_name).split(".")[-1]
    left, right = st.columns(2)
    with left:
        st.download_button("Download spec (.md)", markdown.encode("utf-8"),
                           file_name=f"{stem}_columns.md", mime="text/markdown",
                           key="oc2_dl_md")
    with right:
        csv_out = spec.copy()
        csv_out["RULES"] = csv_out["COLUMN_NAME"].astype(str).str.upper().map(
            lambda n: "; ".join(detail.get(n, [])))
        st.download_button("Download spec (.csv)", csv_out.to_csv(index=False).encode("utf-8"),
                           file_name=f"{stem}_columns.csv", mime="text/csv", key="oc2_dl_csv")


# ------------------------------------------------------------------
# Verify against the live object
# ------------------------------------------------------------------
def render_verify(technical_name: str, spec: pd.DataFrame) -> None:
    st.caption("Compares the declared specification against the object as it is deployed "
               "now. The specification is the intent; this is only a check.")
    if not technical_name:
        st.info("This object has no technical name, so there is nothing to compare against.")
        return
    if spec.empty:
        st.info("Declare some columns first.")
        return
    if not object_exists(str(technical_name)):
        st.warning(f"`{technical_name}` does not exist yet, or is not visible to this app's "
                   "role. Every declared column counts as not built.")
        st.dataframe(
            pd.DataFrame({"Declared but not built": spec["COLUMN_NAME"].astype(str).tolist()}),
            use_container_width=True, hide_index=True)
        return

    live = {c.upper() for c in get_columns(str(technical_name))}
    declared = {str(c).upper() for c in spec["COLUMN_NAME"].tolist()}

    met = st.columns(3)
    met[0].metric("Declared", len(declared))
    met[1].metric("Built", len(live))
    met[2].metric("Match", len(declared & live))

    not_built, not_declared = sorted(declared - live), sorted(live - declared)
    left, right = st.columns(2)
    with left:
        st.markdown("**Declared but not built**")
        if not_built:
            st.dataframe(pd.DataFrame({"Column": not_built}),
                         use_container_width=True, hide_index=True)
        else:
            st.success("Every declared column exists.")
    with right:
        st.markdown("**Built but not declared**")
        if not_declared:
            st.caption("Columns on the object that the FDD does not mention.")
            st.dataframe(pd.DataFrame({"Column": not_declared}),
                         use_container_width=True, hide_index=True)
        else:
            st.success("Every built column is declared.")


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------
def render_builder(context: Dict) -> None:
    """
    Draw the two-pane builder for one object.

    context keys:
      object_id, object_name, technical_name  the object being specified
      source_objects                          technical names feeding it
      objects_df                              every registered object, for the
                                              "also offer columns from" picker
      viewer                                  who to record as the author
      source_column_types(technical_object)   -> {COLUMN_NAME: DATA_TYPE}
      parse_pasted_columns(text)              -> [spec entry, ...]
      test_suggestions(state_key)             optional, draws the test-case panel
      writer_model                            model name for compile_requirement
    """
    object_id = context["object_id"]
    spec = get_object_column_spec(object_id)
    rows = _load_rows(object_id, spec)

    flash = st.session_state.pop("oc2_flash", None)
    if flash:
        # A flash may carry a severity as ("warning", text); a plain string stays a
        # success, which is what the staging messages want. Reporting "could not read
        # the columns of X from Snowflake" in green with a tick was actively misleading.
        level, text = flash if isinstance(flash, tuple) else ("success", flash)
        {"warning": st.warning, "error": st.error,
         "info": st.info}.get(level, st.success)(text)

    counts = st.columns(4)
    counts[0].metric("On the spec", len(rows))
    counts[1].metric("Pass-through", sum(1 for r in rows if r["origin"] == "PASS_THROUGH"))
    counts[2].metric("Derived", sum(1 for r in rows if r["origin"] == "DERIVED"))
    counts[3].metric("Discovered", sum(1 for r in rows if r["origin"] == "DISCOVERED"),
                     help="Read back from Snowflake rather than declared by hand.")

    if context["source_objects"]:
        st.caption("Sources feeding this object: "
                   + ", ".join(f"`{s}`" for s in context["source_objects"]))

    left, right = st.columns([5, 7], gap="medium")
    with left:
        _left_pane(context, rows)
    with right:
        _right_pane(context, rows)

    _rule_panel(context)

    hook = context.get("test_suggestions")
    if callable(hook):
        hook("oc2_test_ctx")
