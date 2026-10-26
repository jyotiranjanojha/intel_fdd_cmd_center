# Module hand-off to development: the status strip on Requirements, the per-object
# code badge, the "with dev" notice on Update a rule, and the Dev pipeline page.
# Co-authored with CoCo
"""
UI for the shared module lifecycle in create_module_handoff.sql.

    IN_REVIEW -> READY_FOR_DEV -> IN_DEV -> DEV_COMPLETED

pdh-engineering-studio reads the same views and calls the same procedure, so a
status set in either app shows in both.

This app reports what changed in a module's rules after it was handed over, or
after code was generated for an object. It does not decide whether that needs a
code change - the developer does. Each change carries a Logic / Wording tag only to
help them judge quickly. Notes (RULE_NOTE) and acceptance changes are not listed.
"""

from typing import Dict, Optional

import pandas as pd
import streamlit as st

import repository as repo

STATE_LABELS = {
    "IN_REVIEW": "In review",
    "READY_FOR_DEV": "Ready for dev",
    "IN_DEV": "In development",
    "DEV_COMPLETED": "Dev completed",
}
STATE_ICONS = {
    "IN_REVIEW": ":grey[●]",
    "READY_FOR_DEV": ":blue[●]",
    "IN_DEV": ":orange[●]",
    "DEV_COMPLETED": ":green[●]",
}
KIND_LABELS = {"LOGIC": "Logic", "WORDING": "Wording", "OTHER": "Other"}
CHANGE_LABELS = {
    "CONDITIONS": "Conditions", "ACTIONS": "Actions", "JOINS": "Joins",
    "RULE_CATEGORY": "Category", "PRIORITY": "Priority", "CREATED": "Rule added",
    "RETIRED": "Rule retired", "RESTORED": "Rule restored", "RULE_NAME": "Name",
    "BUSINESS_DESCRIPTION": "Description", "FDD_TEXT": "FDD text", "FDD_SOURCE": "FDD source",
}


def _present(value) -> bool:
    """Not None / NaN / NaT / blank - Snowflake hands back NaN for empty numbers."""
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    return str(value).strip() != ""


def _row(module_id: int) -> Optional[Dict]:
    df = repo.get_module_dev_status(module_id)
    if df is None or df.empty:
        return None
    return df.iloc[0].to_dict()


def _when(value) -> str:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return ""
    try:
        return pd.Timestamp(value).strftime("%d %b %Y %H:%M")
    except Exception:
        return str(value)


def _short(value, limit: int = 140) -> str:
    text = "" if value is None or (not isinstance(value, str) and pd.isna(value)) else str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def changes_table(changes: pd.DataFrame) -> pd.DataFrame:
    """The change list as people read it: rule, what changed, kind, before, after."""
    if changes is None or changes.empty:
        return pd.DataFrame()
    return pd.DataFrame({
        "Rule": [f"{n} {name}".strip() for n, name in
                 zip(changes["RULE_NUMBER"].fillna(""), changes["RULE_NAME"].fillna(""))],
        "Object": changes["TARGET_OBJECT"].fillna("").map(lambda t: str(t).split(".")[-1]),
        "Changed": changes["CHANGE_TYPE"].map(lambda c: CHANGE_LABELS.get(str(c), str(c).title())),
        "Kind": changes["CHANGE_KIND"].map(lambda k: KIND_LABELS.get(str(k), str(k))),
        "Before": changes["OLD_VALUE"].map(_short),
        "After": changes["NEW_VALUE"].map(_short),
        "By": changes["CHANGED_BY"].fillna(""),
        "When": changes["CHANGED_TS"].map(_when),
    })


def change_summary(changes: pd.DataFrame) -> str:
    """'5 changes (2 logic, 3 wording)'."""
    if changes is None or changes.empty:
        return "no changes"
    kinds = changes["CHANGE_KIND"].value_counts().to_dict()
    parts = [f"{kinds[k]} {KIND_LABELS[k].lower()}" for k in ("LOGIC", "WORDING", "OTHER")
             if kinds.get(k)]
    total = len(changes)
    return f"{total} change{'s' if total != 1 else ''} ({', '.join(parts)})"


def _decide(module_id: int, decision: str, note: str, viewer: str,
            owner_team_id: Optional[int]) -> None:
    try:
        result = repo.set_module_dev_status(module_id, decision, note=note,
                                            performed_by=viewer, owner_team_id=owner_team_id)
        st.session_state["_handoff_flash"] = result.replace("OK: ", "", 1)
        st.rerun()
    except PermissionError as exc:
        st.error(str(exc))
    except ValueError as exc:
        st.error(f"Not done: {exc}")


# Set by render_strip for the buttons it draws: full-width in the two-column strip,
# natural width in the compact side panel.
_LAYOUT = {"compact": False}


def _decision_button(label: str, module_id: int, decision: str, row: Dict, viewer: str,
                     owner_team_id: Optional[int], key: str, primary: bool = False,
                     confirm: Optional[str] = None) -> None:
    """A popover with an optional note and a confirm button, or a disabled reason."""
    blockers = repo.handoff_blockers(row, decision)
    full = not _LAYOUT["compact"]
    if blockers:
        st.button(label, key=f"{key}_off", disabled=True, use_container_width=full,
                  help="Not available: " + "; ".join(blockers))
        return
    with st.popover(label, use_container_width=full):
        if confirm:
            st.caption(confirm)
        note = st.text_area("Note for the other side (optional)", key=f"{key}_note", height=80)
        if st.button(f"Confirm: {label}", key=f"{key}_go",
                     type="primary" if primary else "secondary"):
            _decide(module_id, decision, note, viewer, owner_team_id)


def render_strip(module_id: int, module_name: str, owner_team_id: Optional[int],
                 viewer: str, may_approve: bool, open_comments: int = 0,
                 compact: bool = False) -> None:
    """Where the module is in the hand-off, what changed since, and the actions.

    compact stacks the status above the buttons, for the narrow panel beside the
    Requirements filters; side by side there, both halves wrapped word by word."""
    flash = st.session_state.pop("_handoff_flash", None)
    if flash:
        st.success(flash)
    row = _row(module_id)
    if row is None:
        st.caption(":grey[Dev hand-off status is unavailable - run create_module_handoff.sql.]")
        return
    state = str(row.get("DEV_STATE") or "IN_REVIEW")
    live, accepted = int(row.get("LIVE_RULES") or 0), int(row.get("ACCEPTED_RULES") or 0)
    objects = int(row.get("OBJECTS") or 0)
    coded, done = int(row.get("OBJECTS_WITH_CODE") or 0), int(row.get("OBJECTS_DEV_COMPLETED") or 0)

    with st.container(border=True):
        _LAYOUT["compact"] = compact
        if compact:
            info = st.container()
            try:
                # Buttons in one row at their natural width, under the status.
                actions = st.container(horizontal=True, gap="small")
            except TypeError:                 # Streamlit without horizontal containers
                actions = st.container()
        else:
            info, actions = st.columns([3, 2])
        with info:
            headline = f"{STATE_ICONS[state]} **Dev hand-off: {STATE_LABELS[state]}**"
            if state == "IN_REVIEW":
                headline += f" — {accepted}/{live} rules accepted"
            else:
                headline += f" — {coded}/{objects} objects coded, {done} done in the studio"
            st.markdown(headline)
            if _present(row.get("LAST_DECISION")) and state != "IN_REVIEW":
                st.caption(f"{STATE_LABELS.get(str(row['LAST_DECISION']), str(row['LAST_DECISION']).title())}"
                           f" by {row['LAST_DECISION_BY'] if _present(row.get('LAST_DECISION_BY')) else 'unknown'}"
                           f" on {_when(row.get('LAST_DECISION_TS'))}"
                           + (f" via {str(row['LAST_DECISION_APP']).replace('_', ' ').title()}"
                              if _present(row.get("LAST_DECISION_APP")) else "")
                           + (f" — “{row['LAST_DECISION_NOTE']}”" if _present(row.get("LAST_DECISION_NOTE")) else ""))
            elif state == "IN_REVIEW" and live and accepted == live:
                st.caption("Every rule is accepted - the module can be handed to development.")
            written = int(row.get("AS_WRITTEN_RULES") or 0) if _present(row.get("AS_WRITTEN_RULES")) else 0
            if written:
                st.caption(f"{written} rule(s) are saved as written - developers build them "
                           "from the FDD text. Verify against Snowflake reads the code "
                           "against the sentence, advisory only.")

            if state != "IN_REVIEW" and int(row.get("CHANGES_SINCE_DECISION") or 0):
                changes = repo.get_rule_changes_for_dev(module_id, since=row.get("LAST_DECISION_TS"))
                st.markdown(f":orange[Rules changed since this was decided: "
                            f"{change_summary(changes)}.] The developers decide whether the "
                            "code needs to change.")
                with st.popover("What changed", use_container_width=False):
                    st.dataframe(changes_table(changes), hide_index=True,
                                 use_container_width=True)

        with actions:
            key = f"handoff_{module_id}"
            approver_hint = None if may_approve else "Only an approver for this team can do this."
            if state == "IN_REVIEW":
                if may_approve:
                    _decision_button(
                        "Mark ready for dev", module_id, "READY_FOR_DEV", row, viewer,
                        owner_team_id, f"{key}_ready", primary=True,
                        confirm=(f"Hands **{module_name}** ({live} accepted rules) to development. "
                                 + (f"{open_comments} review comment(s) are still open. "
                                    if open_comments else "")
                                 + "Developers see it in pdh-engineering-studio and on the "
                                   "Dev pipeline page."))
                else:
                    st.button("Mark ready for dev", key=f"{key}_ready_na", disabled=True,
                              use_container_width=not compact, help=approver_hint)
            else:
                if state in ("READY_FOR_DEV", "IN_DEV") and int(row.get("CHANGES_SINCE_DECISION") or 0):
                    if may_approve:
                        _decision_button("Re-confirm hand-off", module_id, "READY_FOR_DEV", row,
                                         viewer, owner_team_id, f"{key}_reconfirm",
                                         confirm="Records that the changed rules are what "
                                                 "development should build. The change list "
                                                 "starts again from now.")
                if state in ("READY_FOR_DEV", "IN_DEV"):
                    _decision_button("Mark dev completed", module_id, "DEV_COMPLETED", row,
                                     viewer, owner_team_id, f"{key}_done",
                                     confirm="Normally done in pdh-engineering-studio. Use this "
                                             "if the studio has not been updated to do it.")
                if state == "DEV_COMPLETED":
                    _decision_button("Reopen development", module_id, "REOPENED", row, viewer,
                                     owner_team_id, f"{key}_reopen",
                                     confirm="Sends the module back to development, e.g. "
                                             "after a rule change that needs new code.")
                if may_approve:
                    _decision_button("Withdraw hand-off", module_id, "WITHDRAWN", row, viewer,
                                     owner_team_id, f"{key}_withdraw",
                                     confirm="Takes the module back into review. The studio "
                                             "keeps its code; nothing is deleted.")


def target_of(group: pd.DataFrame) -> Optional[str]:
    """The technical name of a Requirements target-object group, if the catalog has it."""
    if group is None or group.empty or "TARGET_TECHNICAL_OBJECT" not in group.columns:
        return None
    names = group["TARGET_TECHNICAL_OBJECT"].dropna().astype(str)
    return names.iloc[0] if not names.empty else None


def object_badge(objects: pd.DataFrame, target_object: str) -> Optional[str]:
    """One line on a target object's code in the studio, or None if there is none."""
    if objects is None or objects.empty or not target_object:
        return None
    hit = objects[objects["TARGET_OBJECT"].astype(str).str.upper() == str(target_object).upper()]
    if hit.empty or not bool(hit.iloc[0].get("HAS_CODE")):
        return None
    row = hit.iloc[0]
    status = str(row["CODE_STATUS"] if _present(row.get("CODE_STATUS")) else "GENERATED").replace("_", " ").lower()
    text = f":blue[Code in the studio: {status}, generated {_when(row.get('CODE_GENERATED_TS'))}"
    if _present(row.get("REVIEW_STATUS")):
        text += f"; review {str(row['REVIEW_STATUS']).lower()}"
    text += ".]"
    n = int(row.get("CHANGES_SINCE_CODE") or 0)
    if n:
        logic = int(row.get("LOGIC_CHANGES_SINCE_CODE") or 0)
        text += (f" :orange[{n} rule change{'s' if n != 1 else ''} since the code was generated"
                 f" ({logic} logic, {n - logic} wording).]")
    return text


def edit_notice(module_id: Optional[int]) -> Optional[str]:
    """Shown before saving an edit to a rule whose module is with development."""
    if module_id is None:
        return None
    row = _row(int(module_id))
    if not row:
        return None
    state = str(row.get("DEV_STATE") or "IN_REVIEW")
    if state == "IN_REVIEW":
        return None
    return (f"This module is **{STATE_LABELS[state].lower()}**. Your change will be listed for "
            "the developers as a change since the hand-off; they decide whether the code "
            "needs to change.")


def render_pipeline_page(module_ids=None, viewer: str = "",
                         module_team: Optional[Dict[int, Optional[int]]] = None) -> None:
    """Every module's place in the lifecycle, with the change list for each."""
    st.header("Dev pipeline")
    st.caption("Shared with pdh-engineering-studio: a status set in either app shows in both. "
               "Rule changes after a hand-off are listed for the developers to judge.")
    df = repo.get_module_dev_status()
    if df is None or df.empty:
        st.warning("No hand-off data - run create_module_handoff.sql.")
        return
    if module_ids is not None:
        df = df[df["MODULE_ID"].isin(list(module_ids))]
    df = df[df["LIVE_RULES"].fillna(0) > 0]
    if df.empty:
        st.info("No modules with rules in this team.")
        return

    order = {s: i for i, s in enumerate(("READY_FOR_DEV", "IN_DEV", "DEV_COMPLETED", "IN_REVIEW"))}
    df = df.assign(_o=df["DEV_STATE"].map(order)).sort_values(["_o", "MODULE_NAME"])

    # The all-modules report is on request only, for whoever needs to send one.
    if st.toggle("Show report for all modules", key="devpipe_report"):
        table = pd.DataFrame({
            "Module": df["MODULE_NAME"],
            "Status": df["DEV_STATE"].map(lambda s: STATE_LABELS.get(str(s), str(s))),
            "Rules accepted": [f"{a}/{l}" for a, l in zip(df["ACCEPTED_RULES"], df["LIVE_RULES"])],
            "Objects coded": [f"{c}/{o}" for c, o in zip(df["OBJECTS_WITH_CODE"], df["OBJECTS"])],
            "Done in studio": df["OBJECTS_DEV_COMPLETED"],
            "Changes since decision": df["CHANGES_SINCE_DECISION"],
            "Decided by": df["LAST_DECISION_BY"].fillna(""),
            "Decided on": df["LAST_DECISION_TS"].map(_when),
        })
        st.dataframe(table, hide_index=True, use_container_width=True)
        st.download_button("Download as CSV", table.to_csv(index=False),
                           file_name="dev_pipeline.csv", mime="text/csv",
                           key="devpipe_report_csv")

    names = df["MODULE_NAME"].astype(str).tolist()
    labels = {n: f"{n} — {STATE_LABELS.get(str(s), str(s))}"
              for n, s in zip(names, df["DEV_STATE"])}
    pick = st.selectbox("Module", names, key="devpipe_module", format_func=labels.get)
    row = df[df["MODULE_NAME"].astype(str) == pick].iloc[0]
    module_id = int(row["MODULE_ID"])
    owner = (module_team or {}).get(module_id)
    render_strip(module_id, pick, owner, viewer, repo.can_approve_team(viewer, owner))

    objects = repo.get_module_dev_objects(module_id)
    if objects is not None and not objects.empty:
        st.markdown("**Objects**")
        st.dataframe(pd.DataFrame({
            "Object": objects["TARGET_OBJECT"],
            "Code": objects["CODE_STATUS"].fillna("not generated"),
            "Generated": objects["CODE_GENERATED_TS"].map(_when),
            "Review": objects["REVIEW_STATUS"].fillna(""),
            "Changes since code": objects["CHANGES_SINCE_CODE"],
            "of which logic": objects["LOGIC_CHANGES_SINCE_CODE"],
        }), hide_index=True, use_container_width=True)

    history = repo.get_module_handoff_history(module_id)
    if history is not None and not history.empty:
        if st.toggle("Show hand-off history", key=f"devpipe_hist_{module_id}"):
            st.dataframe(pd.DataFrame({
                "Decision": history["STATUS"].map(lambda s: STATE_LABELS.get(str(s), str(s).title())),
                "By": history["SET_BY"], "When": history["SET_TS"].map(_when),
                "App": history["SOURCE_APP"], "Rules": history["RULE_COUNT"],
                "Note": history["NOTE"].fillna(""),
            }), hide_index=True, use_container_width=True)
