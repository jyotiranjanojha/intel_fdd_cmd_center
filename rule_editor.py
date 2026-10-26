"""The rule as fields and tables, shared by Update a rule and the Import FDD review.

render_fields draws the editor for a proposal and returns the edited values in the
shape requirement_compiler.apply_edits / compile_direct_edit expect.
"""
from typing import Dict

import pandas as pd
import streamlit as st

import as_written as aw
import requirement_compiler as rc


def choices(base, *current) -> list:
    """Dropdown options, keeping any stored value that is not in the standard list -
    otherwise the editor would show it blank and save it empty."""
    extra = [str(v) for v in current if v not in (None, "") and str(v) not in base]
    return list(dict.fromkeys([*base, *extra]))


def render_fields(cur: Dict, k: str) -> Dict:
    """cur: a rule proposal. k: widget key prefix, unique per rule."""
    if aw.is_as_written(cur):
        st.caption("Saved as written: the rule is the FDD sentence itself.")
        _text = st.text_area("Rule text", value=str(cur.get("business_description") or ""),
                             height=160, key=f"{k}_aw")
        edited = {"business_description": _text}
    else:
        _h1, _h2, _h3 = st.columns([3, 2, 1])
        _name = _h1.text_input("Rule name", value=str(cur.get("rule_name") or ""), key=f"{k}_name")
        _cats = choices([*rc.RULE_CATEGORIES, "AS_WRITTEN"], cur.get("rule_category"))
        _cat = _h2.selectbox("Category", _cats,
                             index=_cats.index(str(cur.get("rule_category")))
                             if str(cur.get("rule_category")) in _cats else 0, key=f"{k}_cat")
        try:
            _prio_now = int(cur.get("priority"))
        except (TypeError, ValueError):
            _prio_now = 99
        _prio = _h3.number_input("Priority", value=_prio_now, step=1, key=f"{k}_prio")
        _desc = st.text_area("Business description", value=str(cur.get("business_description") or ""),
                             height=90, key=f"{k}_desc")

        st.markdown("**Conditions**")
        _cond_df = pd.DataFrame(cur.get("conditions") or [], columns=list(rc._EDIT_SECTIONS["conditions"]))
        _conds = st.data_editor(
            _cond_df, num_rows="dynamic", use_container_width=True, key=f"{k}_conds",
            column_config={
                "condition_group": st.column_config.NumberColumn("Group", min_value=1, step=1),
                "field_name": st.column_config.TextColumn("Field"),
                "operator": st.column_config.SelectboxColumn(
                    "Operator", options=choices(rc.OPERATORS, *_cond_df["operator"].tolist())),
                "field_value": st.column_config.TextColumn("Value"),
                "logical_operator": st.column_config.SelectboxColumn("AND / OR", options=["AND", "OR"]),
                "condition_description": st.column_config.TextColumn("Description"),
            })

        st.markdown("**Actions**")
        _act_df = pd.DataFrame(cur.get("actions") or [], columns=list(rc._EDIT_SECTIONS["actions"]))
        _acts = st.data_editor(
            _act_df, num_rows="dynamic", use_container_width=True, key=f"{k}_acts",
            column_config={
                "target_field": st.column_config.TextColumn("Target field"),
                "action_type": st.column_config.SelectboxColumn(
                    "Action", options=choices(rc.ACTION_TYPES, "NO_TRANSFORMATION",
                                               *_act_df["action_type"].tolist())),
                "action_value": st.column_config.TextColumn("Value"),
                "action_description": st.column_config.TextColumn("Description"),
            })

        st.markdown("**Joins**")
        _join_rows = [{"left_object": j.get("left_object"), "right_object": j.get("right_object"),
                       "join_type": j.get("join_type"), "join_purpose": j.get("join_purpose"),
                       "join_columns_text": rc.join_columns_text(j)} for j in cur.get("joins") or []]
        _join_df = pd.DataFrame(_join_rows, columns=["left_object", "right_object", "join_type",
                                                     "join_purpose", "join_columns_text"])
        _joins = st.data_editor(
            _join_df, num_rows="dynamic", use_container_width=True, key=f"{k}_joins",
            column_config={
                "left_object": st.column_config.TextColumn("Left object"),
                "right_object": st.column_config.TextColumn("Right object"),
                "join_type": st.column_config.SelectboxColumn(
                    "Type", options=choices(["INNER", "LEFT", "RIGHT", "FULL"],
                                             *_join_df["join_type"].tolist())),
                "join_purpose": st.column_config.TextColumn("Purpose"),
                "join_columns_text": st.column_config.TextColumn(
                    "Key columns", help="LEFT_COLUMN = RIGHT_COLUMN, separated by commas"),
            })
        if any(j.get("filters") for j in cur.get("joins") or []):
            st.caption(":grey[Join filters are kept as they are here; change them by "
                       "describing the change in words.]")
        edited = {"rule_name": _name, "rule_category": _cat, "business_description": _desc,
                  "priority": int(_prio),
                  "conditions": _conds.to_dict("records"), "actions": _acts.to_dict("records"),
                  "joins": _joins.to_dict("records")}
    return edited
