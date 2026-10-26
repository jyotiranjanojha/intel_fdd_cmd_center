"""Screens for manual sample data (see sample_data.py).

render_panel() is the whole feature on screen: the saved sample with a badge saying
where it came from, and Paste from Excel / Edit / Draft from rules / History / Remove.
It is called from the sample data panel (Requirements page) and the Object columns
page. Switched off by sample_data.SAMPLE_DATA_ENABLED.
"""

from typing import Dict, List, Optional

import pandas as pd
import streamlit as st

import sample_data as sd

SOURCE_LABEL = {"MANUAL": "entered manually", "FDD_IMPORT": "from the FDD",
                "GENERATED": "drafted from the rules"}
_ORIGIN_COLOUR = {"model": "background-color: #fff4ce", "unknown": "background-color: #fde2e1",
                  "rule": "background-color: #e5f3ff"}


def enabled() -> bool:
    return bool(sd.SAMPLE_DATA_ENABLED)


def _viewer() -> Optional[str]:
    return st.session_state.get("current_user")


# ------------------------------------------------------------------
# Lookups the panel needs, kept here so no other module changes
# ------------------------------------------------------------------
def _object_id(technical: str) -> Optional[int]:
    try:
        from repository import _resolve_object_id
        return _resolve_object_id(technical)
    except Exception:
        return None


def declared_columns(technical: str) -> List[str]:
    """Declared columns, else the live ones, else none."""
    try:
        from repository import get_object_column_spec, live_object_columns
        oid = _object_id(technical)
        if oid is not None:
            spec = get_object_column_spec(oid)
            if spec is not None and not spec.empty:
                return [str(c).upper() for c in spec["COLUMN_NAME"].tolist()]
        live = live_object_columns(technical)
        if live is not None and not live.empty:
            return [str(c).upper() for c in live["COLUMN_NAME"].tolist()]
    except Exception:
        pass
    return []


def _live_columns(technical: str) -> List[str]:
    try:
        from repository import live_object_columns
        live = live_object_columns(technical)
        return [] if live is None or live.empty else [str(c).upper() for c in live["COLUMN_NAME"]]
    except Exception:
        return []


def rule_sets_for(technical: str) -> List[Dict]:
    """The live rule sets that build `technical`, with their sources and rule ids."""
    from db import cached_read, sql_value, tbl
    try:
        df = cached_read(f"""
            SELECT rs.RULE_SET_ID, rs.RULE_SET_NAME, rs.SOURCE_TECHNICAL_OBJECT,
                   ARRAY_AGG(r.RULE_ID) WITHIN GROUP (ORDER BY r.PRIORITY, r.RULE_NUMBER) AS RULE_IDS
            FROM {tbl('RULE_SET')} rs
            JOIN {tbl('RULE')} r ON r.RULE_SET_ID = rs.RULE_SET_ID
                 AND COALESCE(r.RULE_STATUS, '') <> 'RETIRED'
            WHERE UPPER(rs.TARGET_TECHNICAL_OBJECT) = {sql_value(technical.upper())}
              AND COALESCE(rs.ACTIVE_IND, TRUE)
            GROUP BY 1, 2, 3 ORDER BY 1
        """)
    except Exception:
        return []
    out = []
    for _, row in df.iterrows():
        ids = sd._loads(row["RULE_IDS"], [])
        out.append({"rule_set_id": int(row["RULE_SET_ID"]), "name": row["RULE_SET_NAME"],
                    "source": row["SOURCE_TECHNICAL_OBJECT"] or "",
                    "rule_ids": [int(i) for i in ids]})
    return out


# ------------------------------------------------------------------
# Showing a sample
# ------------------------------------------------------------------
def _styled(columns: List[str], rows: List[Dict], origin: Dict):
    df = pd.DataFrame(rows, columns=columns)
    if not origin:
        return df

    def colour(row):
        o = origin.get(str(row.name)) or origin.get(row.name) or {}
        styles = []
        for col in df.columns:
            tag = str(o.get(col, ""))
            key = ("model" if tag.startswith("model") else "rule" if tag.startswith("rule")
                   else "unknown" if tag == sd.ORIGIN_UNKNOWN else "")
            styles.append(_ORIGIN_COLOUR.get(key, ""))
        return styles
    try:
        return df.style.apply(colour, axis=1)
    except Exception:
        return df


def _badge(sample: Dict) -> str:
    when = pd.Timestamp(sample["created_ts"]).strftime("%d %b %Y") if sample.get(
        "created_ts") is not None else ""
    return (f":orange-badge[Not from Snowflake] Sample {SOURCE_LABEL.get(sample['source'], '')}"
            f" · {len(sample['rows'])} row(s) · {sample.get('created_by') or ''} {when}")


def show_saved(sample: Dict, key: str) -> None:
    st.markdown(_badge(sample))
    if sample.get("note"):
        st.caption(sample["note"])
    st.dataframe(_styled(sample["columns"], sample["rows"], sample.get("cell_origin") or {}),
                 use_container_width=True, hide_index=True)
    if sample["source"] == "GENERATED":
        _origin_legend()


def _origin_legend() -> None:
    st.caption("Blue: set by a rule in code · Yellow: worked out by the model · "
               "Red: could not be derived - fill it in · White: carried from the source row.")


# ------------------------------------------------------------------
# The panel
# ------------------------------------------------------------------
def render_panel(technical: str, key: str, live_exists: bool) -> None:
    """Saved sample plus the controls to add, edit, draft, restore or remove one."""
    if not enabled():
        return
    technical = str(technical or "").strip().upper()
    if not technical:
        return
    sample = sd.get_sample(technical)
    draft_key = f"{key}_draft"

    if st.session_state.get(draft_key):
        _render_draft(technical, key, st.session_state[draft_key])
        return

    if sample:
        show_saved(sample, key)
    elif not live_exists:
        st.caption("No sample yet. Paste rows from the Excel FDD, type them in, or draft "
                   "them from the rules.")

    cols = st.columns(5)
    with cols[0].popover(":material/content_paste: Paste from Excel"):
        _paste_form(technical, key)
    with cols[1].popover(":material/edit: Edit rows" if sample else ":material/add: Type rows"):
        _editor_form(technical, key, sample)
    rule_sets = rule_sets_for(technical)
    if cols[2].button(":material/auto_awesome: Draft from rules", key=f"{key}_draftbtn",
                      disabled=not rule_sets,
                      help=("Reads real rows from the source objects that exercise each "
                            "rule, applies the rules, and opens the result for review."
                            if rule_sets else "No rules build this object yet.")):
        with st.spinner("Reading source rows and applying the rules..."):
            st.session_state[draft_key] = _make_draft(technical, rule_sets)
        st.rerun()
    versions = sd.sample_versions(technical)
    with cols[3].popover(f":material/history: History ({len(versions)})",
                         disabled=not versions):
        _history(technical, key, versions)
    if sample and cols[4].button(":material/delete: Remove", key=f"{key}_remove",
                                 help="Hides it; History can bring it back."):
        sd.remove_sample(technical, _viewer())
        st.rerun()


def _paste_form(technical: str, key: str) -> None:
    declared = declared_columns(technical)
    st.caption("Copy the cells in Excel (with or without the header row) and paste here."
               + (f" Without a header the columns are taken in order: "
                  f"{', '.join(declared[:8])}{'...' if len(declared) > 8 else ''}."
                  if declared else ""))
    with st.form(f"{key}_paste", border=False, clear_on_submit=True):
        text = st.text_area("Rows", height=160, label_visibility="collapsed")
        note = st.text_input("Where is this from? (optional)", placeholder="Legacy FDD v3, tab 'Stage'")
        if st.form_submit_button("Save sample", type="primary"):
            columns, rows = sd.parse_pasted(text, declared)
            if not rows:
                st.error("No rows found in what was pasted.")
                return
            sd.save_sample(technical, columns, rows, source="MANUAL", note=note,
                           object_id=_object_id(technical), performed_by=_viewer(),
                           cell_origin={i: {c: sd.ORIGIN_MANUAL for c in columns}
                                        for i in range(len(rows))})
            st.rerun()


def _editor_form(technical: str, key: str, sample: Optional[Dict]) -> None:
    columns = (sample or {}).get("columns") or declared_columns(technical)
    if not columns:
        st.caption("No columns are declared for this object. Paste from Excel with a "
                   "header row instead.")
        return
    rows = (sample or {}).get("rows") or [{c: None for c in columns}]
    edited = st.data_editor(pd.DataFrame(rows, columns=columns), num_rows="dynamic",
                            use_container_width=True, key=f"{key}_editor", hide_index=True)
    if st.button("Save rows", type="primary", key=f"{key}_editor_save"):
        clean = [{c: (None if pd.isna(v) or str(v).strip() == "" else str(v)) for c, v in r.items()}
                 for r in edited.to_dict("records")]
        clean = [r for r in clean if any(v is not None for v in r.values())]
        origin = dict((sample or {}).get("cell_origin") or {})
        sd.save_sample(technical, list(edited.columns), clean,
                       source=(sample or {}).get("source") or "MANUAL",
                       note=(sample or {}).get("note", ""), object_id=_object_id(technical),
                       performed_by=_viewer(), cell_origin=origin,
                       source_rows=(sample or {}).get("source_rows"))
        st.rerun()


def _history(technical: str, key: str, versions: List[Dict]) -> None:
    st.caption("Every saved version. Restore makes it the one shown.")
    for v in versions:
        when = pd.Timestamp(v["created_ts"]).strftime("%d %b %Y %H:%M") if v.get(
            "created_ts") is not None else ""
        c = st.columns([6, 2])
        c[0].markdown(f"{'**Shown** · ' if v['active'] else ''}{SOURCE_LABEL.get(v['source'], '')}"
                      f" · {len(v['rows'])} row(s) · {v.get('created_by') or ''} · {when}")
        if not v["active"] and c[1].button("Restore", key=f"{key}_restore_{v['sample_id']}"):
            sd.restore_version(technical, v["sample_id"], _viewer())
            st.rerun()


# ------------------------------------------------------------------
# Draft from rules
# ------------------------------------------------------------------
def _make_draft(technical: str, rule_sets: List[Dict]) -> Dict:
    from catalog import resolve_physical_name
    from db import cached_read
    from repository import load_bundles_all
    declared = declared_columns(technical)
    draft = sd.draft_from_rules(
        technical, read_fn=cached_read, rule_sets=rule_sets,
        bundles_fn=lambda ids: load_bundles_all(list(ids)) if ids else [],
        columns_fn=_live_columns, declared_columns=declared,
        physical_fn=lambda n: resolve_physical_name(n) or n)
    draft["rule_sets"] = [r["name"] for r in rule_sets]
    return draft


def _render_draft(technical: str, key: str, draft: Dict) -> None:
    st.markdown(f":blue-badge[Draft] Sample drafted from {len(draft.get('rules_used', []))} "
                f"rule(s) of {', '.join(draft.get('rule_sets') or [])} - review, edit, then save.")
    for message in draft.get("messages") or []:
        st.caption(message)
    if not draft["rows"]:
        st.warning("No rows could be drafted. The sources may be empty, not in Snowflake, "
                   "or every picked row was removed by a rule.")
    else:
        unknown = sum(1 for o in draft["origin"].values() for v in o.values()
                      if v == sd.ORIGIN_UNKNOWN)
        st.dataframe(_styled(draft["columns"], draft["rows"],
                             {str(k): v for k, v in draft["origin"].items()}),
                     use_container_width=True, hide_index=False)
        _origin_legend()
        if unknown:
            st.caption(f"{unknown} cell(s) could not be derived from the rules and source "
                       "rows. Fill them in below, or leave them empty.")
        if draft.get("coverage"):
            st.caption("Rows per rule: " + "; ".join(
                f"rule {n}: rows {', '.join(str(i) for i in sorted(set(ix))[:8])}"
                for n, ix in sorted(draft["coverage"].items())))
    # Toggles, not expanders: this panel is drawn inside a rule's expander.
    if draft.get("excluded") and st.toggle(
            f"Show {len(draft['excluded'])} source row(s) filtered out by the rules",
            key=f"{key}_show_excluded"):
        if True:
            st.dataframe(pd.DataFrame([dict(e["source_row"], _REASON=e["reason"])
                                       for e in draft["excluded"]]),
                         use_container_width=True, hide_index=True)
    if draft.get("source_rows") and st.toggle("Show the source rows used",
                                              key=f"{key}_show_sources"):
        if True:
            for src, rows in draft["source_rows"].items():
                st.caption(src)
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    edited = None
    if draft["rows"]:
        edited = st.data_editor(pd.DataFrame(draft["rows"], columns=draft["columns"]),
                                num_rows="dynamic", use_container_width=True,
                                key=f"{key}_draft_editor", hide_index=True)
    c = st.columns(3)
    if edited is not None and c[0].button("Save sample", type="primary", key=f"{key}_draft_save"):
        rows = [{col: (None if pd.isna(v) or str(v).strip() == "" else str(v))
                 for col, v in r.items()} for r in edited.to_dict("records")]
        origin = {}
        for i, r in enumerate(rows):
            before = draft["rows"][i] if i < len(draft["rows"]) else {}
            base = dict(draft["origin"].get(i) or draft["origin"].get(str(i)) or {})
            for col, v in r.items():
                if str(before.get(col)) != str(v):
                    base[col] = sd.ORIGIN_MANUAL
            origin[i] = base
        sd.save_sample(technical, list(edited.columns), rows, source="GENERATED",
                       note=f"Drafted from rules {', '.join(draft.get('rules_used', []))} "
                            f"and reviewed.", cell_origin=origin,
                       source_rows=draft.get("source_rows"), object_id=_object_id(technical),
                       performed_by=_viewer())
        st.session_state.pop(f"{key}_draft", None)
        st.rerun()
    if c[1].button("Redraft", key=f"{key}_redraft"):
        with st.spinner("Drafting again..."):
            st.session_state[f"{key}_draft"] = _make_draft(technical, rule_sets_for(technical))
        st.rerun()
    if c[2].button("Discard draft", key=f"{key}_discard"):
        st.session_state.pop(f"{key}_draft", None)
        st.rerun()


# ------------------------------------------------------------------
# Published FDD
# ------------------------------------------------------------------
def sample_table_html(technical: str, max_rows: int = 15) -> str:
    """A small HTML table for the published FDD's object card, or "" when none."""
    if not enabled():
        return ""
    sample = sd.get_sample(technical)
    if not sample or not sample["rows"]:
        return ""
    import html
    cols = sample["columns"]
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{html.escape('' if r.get(c) is None else str(r.get(c)))}</td>"
                                    for c in cols) + "</tr>" for r in sample["rows"][:max_rows])
    more = (f'<p class="muted">{len(sample["rows"]) - max_rows} more row(s) not shown.</p>'
            if len(sample["rows"]) > max_rows else "")
    return (f'<p class="muted"><b>Sample data ({SOURCE_LABEL.get(sample["source"], "")}) - '
            f'not from Snowflake</b></p><table class="colspec"><thead><tr>{head}</tr></thead>'
            f"<tbody>{body}</tbody></table>{more}")
