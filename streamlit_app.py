# Adds an "Update a rule" page: pick an object, pick a rule, describe the
# change in plain English, confirm the preview, then write it back. Also adds
# a "View rule audit trail" page over RULE_CHANGE_HISTORY, a "Rollback a rule"
# page that replays that history to restore a past version, a Module filter
# on the object pickers, a "Manage modules" admin page, an "Object lineage"
# page that traces source-to-target hops from the topmost object down, and a
# "Verify against Snowflake" page comparing FDD rules against the real
# deployed objects (structural + data-level + AI-assisted checks).
# Co-authored with CoCo
"""
Rule Repository - Streamlit in Snowflake.

Pages:
  Requirements        read rules as FDD sentences, filter, export
  Write a rule        BA types a requirement, confirms the English, it is saved
  Update a rule        pick a rule, describe the change, confirm, it is updated
  View rule audit trail   who changed what, and when, per rule
  Rollback a rule      pick a past version from the audit trail, confirm, restore it
  Manage modules       create/rename modules, reassign objects and rule sets
  Object lineage       trace source -> target hops from the topmost object down
  Verify against Snowflake   does the deployed object match the documented rule
  Ask                  question routing over metadata and data
  Manual entry         the original forms, for precise edits
"""

import re
import time

import pandas as pd
import streamlit as st

import nlp_router as router

# Manual sample data (sample_data.py / sample_data_ui.py). Guarded, so removing the two
# modules or switching SAMPLE_DATA_ENABLED off leaves the app exactly as it was.
try:
    import sample_data_ui as _sdu
    if not _sdu.enabled():
        _sdu = None
except Exception:
    _sdu = None
# Module hand-off to development (module_handoff_ui.py, create_module_handoff.sql).
# Guarded the same way: without the module or its views the pages render as before.
try:
    import module_handoff_ui as _mh
except Exception:
    _mh = None
import as_written as _aw
import req_pager as _pager
from catalog import (database_reachable, get_allowed_objects, get_lineage_edges,
                     get_modules, get_rule_catalog, get_team_databases,
                     lineage_roots, physical_databases, resolve_physical_name,
                     scope_edges_to_module)
from lineage_diagram import LEGEND, build_dot, family_title, lineage_families, lineage_ranks
try:
    from catalog import get_last_error
except ImportError:
    def get_last_error():
        return None
from compliance import run_compliance_check
from db import FAST_MODEL, MODEL_CHOICES, WRITER_MODEL, bump_cache, cached_read, cortex_analyst_query, execute, llm_cached, run_query, sql_value, tbl
from fdd_render import render_many, render_rule
from repository import (ACCEPTED_STATUS, CHANGES_REQUESTED_STATUS, DRAFT_STATUS,
                        add_approver, add_review_comment, add_team_database,
                        approvals_are_restricted, can_approve, can_approve_team,
                        create_business_object, create_module, create_rule_set,
                        create_team, delete_test_case,
                        get_audit_trail, get_default_team,
                        get_object_column_spec, get_review_comments, get_rule_history,
                        add_rule_notes, get_rule_notes, update_rule_note,
                        get_rule_set_endpoints,
                        get_rule_set_source_objects, get_rule_sets_for_module,
                        get_team_module_ids, get_test_cases,
                        list_approvers, list_business_objects,
                        list_database_map, list_rule_sets, list_team_databases,
                        list_teams, live_object_columns, load_bundles, load_bundles_all,
                        log_rollback_marker, next_rule_number, normalise_status,
                        reassign_module_team, reassign_object_module, reassign_rule_set_module,
                        remove_approver, remove_team_database, rename_module, rename_team,
                        save_rule, save_test_cases, set_default_team,
                        set_review_comment_done, set_rule_set_endpoints,
                        set_rule_status, set_rule_statuses,
                        set_team_database_primary, set_test_case_status,
                        sync_all_object_columns, sync_object_columns, update_rule,
                        upsert_database_map)
from repository import list_retired_rules, restore_rule, retire_rule
from requirement_compiler import compile_requirement, compile_rollback, compile_update, rollback_events
import requirement_compiler as _rc
import rule_editor as _rule_editor

st.set_page_config(page_title="FDD Command Center | Intel", layout="wide", page_icon="https://upload.wikimedia.org/wikipedia/commons/7/7d/Intel_logo_%282006-2020%29.svg")

# Intel-themed page styling
st.markdown("""
<style>
/* Main page theme */
.stApp {
    background: linear-gradient(180deg, #ffffff 0%, #f4f8fc 100%);
}
h1, h2, h3 {
    color: #0068b5 !important;
}
.stTabs [data-baseweb="tab"] {
    color: #0068b5;
}
.stTabs [aria-selected="true"] {
    border-bottom-color: #0068b5 !important;
}
div[data-testid="stMetric"] label {
    color: #0068b5 !important;
}
div[data-testid="stMetric"] [data-testid="stMetricValue"] {
    color: #00285a !important;
}
button[kind="primary"] {
    background-color: #0068b5 !important;
    border-color: #0068b5 !important;
}
button[kind="primary"]:hover {
    background-color: #004f8a !important;
    border-color: #004f8a !important;
}
/* Centre the lineage flow chart. It renders at its intrinsic size (not
   stretched), so by default it sits hard against the left edge. Both selectors
   are here on purpose: Streamlit has moved between a testid and a class name on
   this element across versions, and an unmatched selector is simply ignored.
   Safe to apply globally - the lineage diagram is the app's only Graphviz chart. */
[data-testid="stGraphVizChart"],
.stGraphVizChart {
    display: flex !important;
    justify-content: center !important;
}
[data-testid="stGraphVizChart"] > svg,
.stGraphVizChart > svg {
    margin-left: auto !important;
    margin-right: auto !important;
}
</style>
""", unsafe_allow_html=True)



def _save_notes_from_text(rule_id, text: str, source: str) -> None:
    """Keep the Note / Open / Question / Assumption sentences of a typed rule as notes
    on it. Best effort: a failed note never undoes the save that just succeeded."""
    from fdd_import import extract_notes
    try:
        found = extract_notes(text)
        if found and rule_id is not None:
            added = add_rule_notes(int(rule_id), [(k, t, source) for k, t in found],
                                   performed_by=current_viewer())
            if added:
                st.toast(f"{added} note(s) kept on rule {rule_id}.")
    except Exception as exc:
        st.caption(f"Notes were not saved: {exc}")


NOTE_BADGE = {"OPEN": "Open question", "RESOLVED": "Resolved", "INFO": "App",
              "NOTE": "Note"}


def render_rule_notes(rule_id, key_prefix: str, editable: bool = True,
                      notes: pd.DataFrame = None, inline: bool = False) -> None:
    """The notes attached to one rule, with a box to add one.

    inline=True draws them without an expander, for use inside a rule's own expander
    (Streamlit does not nest expanders); pass `notes` pre-filtered to save a query.
    """
    if notes is None:
        try:
            notes = get_rule_notes(int(rule_id))
        except Exception:
            notes = pd.DataFrame()
    count = 0 if notes is None or notes.empty else len(notes)
    open_n = 0 if not count else int((notes["NOTE_TYPE"] == "OPEN").sum())
    label = f"Notes on this rule ({count})" + (f" · {open_n} open" if open_n else "")
    if inline:
        st.markdown(f"**{label}**")
        box = st.container()
    else:
        box = st.expander(label, expanded=bool(open_n))
    with box:
        if not count:
            st.caption("No notes yet.")
        for _, n in (notes.iterrows() if count else []):
            cols = st.columns([8, 2]) if editable else [st.container()]
            cols[0].markdown(f"**{NOTE_BADGE.get(n['NOTE_TYPE'], n['NOTE_TYPE'])}** · "
                             f"{n['NOTE_TEXT']}")
            cols[0].caption(f"{str(n['SOURCE']).replace('_', ' ').lower()} · "
                            f"{n['CREATED_BY']} · {pd.Timestamp(n['CREATED_TS']):%d %b %Y}")
            if editable:
                nid = int(n["NOTE_ID"])
                if n["NOTE_TYPE"] == "OPEN" and cols[1].button(
                        "Mark resolved", key=f"{key_prefix}_res_{nid}"):
                    update_rule_note(nid, note_type="RESOLVED", performed_by=current_viewer())
                    st.rerun()
                if cols[1].button("Hide", key=f"{key_prefix}_hide_{nid}"):
                    update_rule_note(nid, active=False, performed_by=current_viewer())
                    st.rerun()
        if editable:
            # A small "+" that opens the form, instead of a form always on screen.
            with st.popover(":material/add: Note", help="Add a note to this rule"):
                with st.form(f"{key_prefix}_add_{rule_id}", clear_on_submit=True,
                             border=False):
                    kind = st.selectbox("Type", ["NOTE", "OPEN"], format_func=NOTE_BADGE.get)
                    text = st.text_area("Note", height=80, placeholder="Something the next "
                                        "reader of this rule should know")
                    if st.form_submit_button("Save note", type="primary") and text.strip():
                        add_rule_notes(int(rule_id), [(kind, text, "MANUAL")],
                                       performed_by=current_viewer())
                        st.rerun()

def _render_module_comments(comments: pd.DataFrame, module: str, toggle) -> None:
    """Review comments for one module: people's comments and the FDD's own questions
    first; the importer's per-rule notes folded away, one line per rule.

    An FDD import writes a note for every problem accepted or noted, which buried the
    handful of real questions under dozens of near-identical lines.
    """
    from fdd_import import classify_comment

    rows = []
    for _, c in (comments if comments is not None else pd.DataFrame()).iterrows():
        info = classify_comment(c["COMMENT_TEXT"])
        rows.append({**c.to_dict(), **info, "done": str(c["STATUS"]) == "Done"})
    main = [r for r in rows if r["kind"] != "import"]
    notes = [r for r in rows if r["kind"] == "import"]
    main.sort(key=lambda r: (r["done"], -pd.Timestamp(r["RAISED_TS"]).value
                             if pd.notna(r["RAISED_TS"]) else 0))
    main_open = sum(1 for r in main if not r["done"])
    notes_open = [r for r in notes if not r["done"] and r["what"] != "fixed automatically"]

    parts = [f"{main_open} open" if main else "none yet"]
    if notes:
        parts.append(f"{len(notes_open)} import note(s) to check")
    with st.expander(f"Review comments — {module} ({', '.join(parts)})",
                     expanded=main_open > 0):
        if not main:
            st.caption(f"No review comments on {module} yet. Add one with "
                       f"**✍ Review comments — {module}** beside the Module filter above.")
        else:
            st.caption("Tick a comment off when the change has been made — it saves "
                       "immediately.")
        for r in main:
            key = f"revtop_{int(r['COMMENT_ID'])}"
            tag = {"Open": "FDD question", "Note": "FDD note",
                   "Resolved": "FDD, resolved"}.get(r["what"], "")
            st.checkbox((f"**{tag}** · " if tag else "") + r["body"], value=r["done"],
                        key=key, on_change=toggle, args=(int(r["COMMENT_ID"]), key))
            trail = f"{r['RAISED_BY']} · {r['RAISED_TS']:%d %b %Y}"
            if r["done"] and pd.notna(r.get("RESOLVED_TS")):
                trail += f" · done by {r['RESOLVED_BY']} on {r['RESOLVED_TS']:%d %b %Y}"
            st.caption(trail)

        if not notes:
            return
        st.divider()
        show = st.toggle(f"Show import notes ({len(notes_open)} to check, "
                         f"{len(notes) - len(notes_open)} done or informational)",
                         key=f"revnotes_{module}")
        st.caption("Problems accepted or noted when the FDD was imported, one line per "
                   "rule. They are also on the rule itself.")
        if not show:
            return
        only_open = st.checkbox("Only notes still to check", value=True,
                                key=f"revnotes_open_{module}")
        by_rule: dict = {}
        for r in notes:
            if only_open and (r["done"] or r["what"] == "fixed automatically"):
                continue
            by_rule.setdefault(r["rule_id"], []).append(r)
        if notes_open and st.button(f"Mark all {len(notes_open)} import notes done",
                                    key=f"revnotes_all_{module}"):
            viewer = current_viewer()
            for r in notes_open:
                set_review_comment_done(int(r["COMMENT_ID"]), True, viewer)
            st.rerun()
        for rule_id, group in by_rule.items():
            first = group[0]
            done = all(g["done"] for g in group)
            summary = "; ".join(f"{g['what']}: {g['body']}" for g in group)
            if len(summary) > 220:
                summary = summary[:217] + "..."
            key = f"revnote_{rule_id}_{int(done)}"
            ticked = st.checkbox(f"**Rule {first['rule']}** ({first['rule_set'] or 'id ' + rule_id})"
                                 f" · {summary}", value=done, key=key)
            if ticked != done:
                viewer = current_viewer()
                for g in group:
                    set_review_comment_done(int(g["COMMENT_ID"]), ticked, viewer)
                st.rerun()

def _viewer_name() -> str:
    """
    The Snowflake username of the person looking at the app.

    Defined up here, above the header, because the team selector renders before
    the rest of the helpers and needs the viewer's identity to look up their
    default team. current_viewer() below delegates to this so there is one
    implementation rather than two that can drift.

    Deliberately not CURRENT_USER(): the app runs execute_as OWNER, so in SQL
    CURRENT_USER() is the Streamlit object's owner no matter who is viewing.
    """
    for holder in (getattr(st, "user", None), getattr(st, "experimental_user", None)):
        if holder is None:
            continue
        for reader in (
            lambda h: getattr(h, "user_name", None),
            lambda h: h["user_name"],
            lambda h: getattr(h, "email", None),
        ):
            try:
                value = reader(holder)
            except Exception:
                continue
            if value:
                return str(value)
    return "unknown"


def _render_team_selector():
    """
    The team picker under the title. Renders once per run, so it shows on every
    page, and returns (team_id, team_name) with team_id None for "All teams".

    Resolution order on first load is session, then the viewer's stored default,
    then All teams. The stored default is only written when the box is ticked, so
    a one-off look at another team never silently becomes someone's default.
    """
    teams = list_teams()
    viewer = _viewer_name()

    ALL = "All teams"
    labels = [ALL] + ([] if teams.empty else
                      [str(r["TEAM_NAME"]) for _, r in teams.iterrows()])
    by_label = {} if teams.empty else {
        str(r["TEAM_NAME"]): int(r["TEAM_ID"]) for _, r in teams.iterrows()
    }

    # Seed the widget from the stored default once per session. Done by writing
    # session_state before the widget exists, which is the supported way to give
    # a keyed widget an initial value.
    if "_team_seeded" not in st.session_state:
        st.session_state["_team_seeded"] = True
        stored = get_default_team(viewer)
        if stored is not None:
            match = next((lbl for lbl, tid in by_label.items() if tid == stored), None)
            if match:
                st.session_state["_team_pick"] = match

    left, right, spacer = st.columns([2, 2, 3])
    with left:
        chosen_label = st.selectbox(
            "Team", labels, key="_team_pick",
            help="Narrows every module, rule, object and test case list to this team.",
        )
    team_id = by_label.get(chosen_label)

    with right:
        stored_now = get_default_team(viewer)
        is_default = stored_now is not None and stored_now == team_id
        make_default = st.checkbox(
            "Make this my default team", value=is_default, key="_team_default",
            help="Remembers this team for you next time. Only you are affected.",
        )
        if make_default != is_default:
            try:
                set_default_team(viewer, team_id)
                if team_id is None:
                    st.caption(":grey[Default cleared.]")
                else:
                    st.caption(f":grey[Saved as your default.]" if make_default
                               else ":grey[Default cleared.]")
            except Exception as exc:
                st.caption(f":red[Could not save the default: {exc}]")

    if teams.empty:
        st.caption(":grey[No teams defined yet. Add one under Manage teams & modules.]")
    # Published to session state so page modules can reach it without importing
    # from here. The FDD importer needs it: a module created with TEAM_ID NULL
    # belongs to no team, so every page that scopes by team hides it immediately
    # and the import looks like it silently did nothing.
    st.session_state["_team_id"] = team_id
    return team_id, (chosen_label if team_id is not None else None)


st.title("FDD Command Center")
st.caption("Business rules for the supply planning transformation, in the words the FDD uses.")

# Pages in other modules (Import FDD, sample data) read the viewer from here. It was
# never set, so they passed None and every draft, import and sample row was credited
# to the Streamlit service identity (STPLATSTREAMLIT...), a new one after each deploy.
st.session_state["current_user"] = _viewer_name()

TEAM_ID, TEAM_NAME = _render_team_selector()
TEAM_MODULE_IDS = get_team_module_ids(TEAM_ID)

# The databases this team documents objects in. TEAM_LOGICAL_DATABASES holds the
# names as the FDD records them (used to pre-fill the object picker);
# TEAM_PHYSICAL_DATABASES holds what exists in this account (used to resolve and
# query objects). With no team selected both span every mapped database.
TEAM_LOGICAL_DATABASES = get_team_databases(TEAM_ID)
TEAM_PHYSICAL_DATABASES = physical_databases(TEAM_ID)


def _team_module_names():
    """
    The team's module names, for the few places that only have a name to filter on.

    None means no team selected, matching TEAM_MODULE_IDS.
    """
    if TEAM_MODULE_IDS is None:
        return None
    mods = get_modules()
    if mods is None or mods.empty or "MODULE_ID" not in mods.columns:
        return None
    return {str(r["MODULE_NAME"]) for _, r in mods.iterrows()
            if int(r["MODULE_ID"]) in TEAM_MODULE_IDS}


TEAM_MODULE_NAMES = _team_module_names()


def scope_to_team(df, id_column: str = "MODULE_ID"):
    """
    Narrow a frame to the selected team's modules.

    A None TEAM_MODULE_IDS means no team is selected, so nothing is filtered and
    the app behaves as it did before teams existed. An empty set means a real team
    that owns no modules yet, which correctly yields nothing rather than
    everything - otherwise a new team would open onto another team's work.

    Rows with no module survive filtering: an object or rule set not yet assigned
    to a module would otherwise become invisible and unassignable the moment any
    team was selected.
    """
    if TEAM_MODULE_IDS is None or df is None or getattr(df, "empty", True):
        return df
    if id_column not in df.columns:
        return df
    keep = df[id_column].isin(TEAM_MODULE_IDS) | df[id_column].isna()
    return df[keep]

_PAGES = {
    "Browse": {
        ":mag: Requirements": ("Requirements", "View and export FDD rules as structured English"),
        ":outbox_tray: Publish FDD": ("Publish FDD", "Download one module's rules, objects and lineage as one document"),
        ":speech_balloon: Ask": ("Ask", "Ask questions about rules, columns, or data in natural language"),
        ":link: Object lineage": ("Object lineage", "Trace source-to-target data flow across objects"),
        ":rocket: Dev pipeline": ("Dev pipeline", "Which modules are handed to development, in development or done"),
    },
    "Author": {
        ":file_folder: Manage teams & modules": ("Manage modules", "Create teams, modules, and assign objects and rule sets"),
        ":inbox_tray: Import an FDD": ("Import an FDD", "Paste a whole FDD and review what the app builds before saving"),
        ":straight_ruler: Object columns": ("Object columns", "The column spec for each target object, and which rules cover each column"),
        ":pencil2: Write a rule": ("Write a rule", "Write a new transformation rule in plain English"),
        ":arrows_counterclockwise: Update a rule": ("Update a rule", "Modify an existing rule by describing the change"),
        ":card_file_box: Metadata Entry": ("Metadata Entry", "Bulk-enter or update rule metadata"),
        ":keyboard: Manual entry": ("Manual entry", "Directly edit rule fields with full control"),
    },
    "Govern": {
        ":clipboard: Audit trail": ("View rule audit trail", "See who changed what, and when, for any rule"),
        ":rewind: Rollback a rule": ("Rollback a rule", "Restore a rule to a previous version from the audit trail"),
        ":wastebasket: Retire a rule": ("Retire a rule", "Remove a rule from the repository, reversibly"),
    },
    "Validate": {
        ":white_check_mark: Verify against Snowflake": ("Verify against Snowflake", "Compare FDD rules against the real deployed objects"),
        ":test_tube: Test cases": ("Test cases", "View, generate, and manage test cases for rules"),
        ":stethoscope: Repository health": ("Repository health", "Repository-wide gaps: rules, objects, columns, lineage"),
    },
}

with st.sidebar:
    st.markdown("""
    <style>
    [data-testid="stSidebar"] {
        background: linear-gradient(180deg, #f4f8fc 0%, #e8f0f8 100%);
    }
    [data-testid="stSidebar"] button {
        background: #ffffff !important;
        border: 1px solid #c8dced !important;
        border-radius: 10px !important;
        color: #0068b5 !important;
        font-weight: 500 !important;
        transition: all 0.2s ease !important;
        box-shadow: 0 1px 3px rgba(0, 104, 181, 0.06) !important;
    }
    [data-testid="stSidebar"] button:hover {
        background: linear-gradient(90deg, #0068b5 0%, #00aeef 100%) !important;
        border-color: transparent !important;
        color: #fff !important;
        box-shadow: 0 4px 12px rgba(0, 104, 181, 0.3) !important;
        transform: translateX(3px);
    }
    [data-testid="stSidebar"] button:focus {
        background: linear-gradient(90deg, #0068b5 0%, #00aeef 100%) !important;
        border-color: transparent !important;
        color: #fff !important;
    }
    /* Move tooltip below the button instead of above */
    [data-testid="stSidebar"] [data-testid="stTooltipIcon"] + div[data-testid="stMarkdownContainer"],
    [data-testid="stSidebar"] div[data-baseweb="tooltip"] {
        top: 100% !important;
        bottom: auto !important;
    }
    </style>
    """,  unsafe_allow_html=True)
    st.markdown('<h2 style="color: #0068b5; margin-bottom: 0;">intel</h2>', unsafe_allow_html=True)
    st.markdown("#### FDD Command Center")
    st.markdown("---")
    _selected = None
    for group, items in _PAGES.items():
        st.caption(group.upper())
        for label, (value, tooltip) in items.items():
            if st.button(label, key=f"nav_{value}", use_container_width=True, help=tooltip):
                st.session_state["_nav_page"] = value
        st.markdown("")  # spacer between groups

page = st.session_state.get("_nav_page", "Requirements")


def rule_set_picker(label="Rule set", module_id=None):
    """
    Pick a rule set to write into.

    Scoped to the selected team, not just filtered for display: without this a BA
    with a team selected could still save a new rule into another team's rule set,
    which is a write crossing the boundary the selector implies.

    When module_id is given, only rule sets in that module (plus unassigned ones)
    are offered.
    """
    sets = cached_read(
        f"""SELECT rs.RULE_SET_ID, rs.RULE_SET_NAME, rs.MODULE_ID
            FROM {tbl('RULE_SET')} rs ORDER BY rs.RULE_SET_ID"""
    )
    sets = scope_to_team(sets)
    if module_id is not None and sets is not None and not sets.empty:
        sets = sets[(sets["MODULE_ID"] == module_id) | sets["MODULE_ID"].isna()]
    if sets is None or sets.empty:
        if TEAM_ID is not None:
            st.warning(f"No rule sets belong to {TEAM_NAME}. Assign a module to this team "
                       "under Manage teams & modules, or switch to All teams.")
        else:
            st.warning("No rule sets found.")
        return None
    options = {f"{r['RULE_SET_ID']} — {r['RULE_SET_NAME']}": int(r["RULE_SET_ID"])
               for _, r in sets.iterrows()}
    return options[st.selectbox(label, list(options))]


def _lineage_tree_lines(edges: pd.DataFrame, object_id: int, object_name: str, module_name: str,
                        depth: int = 0, visited: tuple = ()) -> list:
    """
    Depth-first walk down the source -> target hops starting at object_id.
    `visited` guards against a cycle looping forever; edges already flag
    self-loops separately so those never recurse. Rendered as a plain
    markdown nested list - no HTML needed.
    """
    indent = "  " * depth
    marker = "📍" if depth == 0 else "-"
    module_tag = f" `[{module_name}]`" if module_name else ""
    lines = [f"{indent}{marker} **{object_name}**{module_tag}"]

    if object_id in visited:
        lines.append(f"{indent}  - *(already shown above - cycle guard)*")
        return lines

    hops = edges[(edges["SOURCE_OBJECT_ID"] == object_id) & (~edges["IS_SELF_LOOP"])]
    self_loops = edges[(edges["SOURCE_OBJECT_ID"] == object_id) & (edges["IS_SELF_LOOP"])]
    for _, loop in self_loops.iterrows():
        lines.append(f"{indent}  - 🔁 *self-referential rule set: {loop['RULE_SET_NAME']}*")

    for _, hop in hops.iterrows():
        cross_module = hop["SOURCE_MODULE_NAME"] != hop["TARGET_MODULE_NAME"]
        hop_tag = " ⚠️ cross-module" if cross_module else ""
        lines.append(f"{indent}  - *via rule set: {hop['RULE_SET_NAME']}*{hop_tag}")
        lines.extend(_lineage_tree_lines(
            edges, int(hop["TARGET_OBJECT_ID"]), hop["TARGET_OBJECT_NAME"], hop["TARGET_MODULE_NAME"],
            depth + 2, visited + (object_id,),
        ))
    return lines


def _lineage_ranks(edges: pd.DataFrame) -> dict:
    """
    Longest-path depth of every object from its nearest topmost root. The
    implementation now lives in lineage_diagram so the Requirements-page
    ordering and the lineage flow chart's horizontal bands can never disagree.
    """
    return lineage_ranks(edges)


def current_viewer() -> str:
    """
    The Snowflake username of the person looking at the app.

    Delegates to _viewer_name(), which is defined above the header because the
    team selector needs it before this point in the file. Kept as a name of its
    own because it is called from a dozen places.
    """
    return _viewer_name()


def _flash(message: str, level: str = "success") -> None:
    """Stash a toast message that survives st.rerun().

    Call before st.rerun(). The message is shown once on the next render
    and then cleared, so it does not reappear on further interactions.
    """
    st.session_state["_flash"] = {"message": message, "level": level}


def _clear_bulk_state(prefix: str) -> None:
    """Remove all session state keys starting with prefix (checkbox cleanup)."""
    to_drop = [k for k in st.session_state if k.startswith(prefix)]
    for k in to_drop:
        st.session_state.pop(k, None)


def _show_flash() -> None:
    """Render and clear any pending flash message."""
    flash = st.session_state.pop("_flash", None)
    if flash:
        getattr(st, flash.get("level", "success"), st.success)(flash["message"])


_show_flash()


def _grid_height(compact: bool = True, rows: int = None) -> dict:
    """
    st.dataframe kwargs. height is omitted rather than passed as None, because
    st.dataframe validates it and rejects None outright - passing it explicitly
    is an error, not a "use the default".
    """
    args = {"use_container_width": True, "hide_index": True}
    if rows is not None:
        if rows > 10:
            args["height"] = min(600, 60 + 35 * min(rows, 16))
    elif compact:
        args["height"] = 240
    return args


def _sample_data_panel(technical_object: str, key_prefix: str):
    """
    The body of the sample data preview. Split out from render_sample_data so
    it can be wrapped in st.fragment: changing the row count then re-runs only
    this panel, instead of re-reading the catalog and every rule bundle on the
    Requirements page.
    """
    # Recorded names may use a logical database that does not exist here, so
    # read from the physical one.
    physical = resolve_physical_name(technical_object) or technical_object
    short = physical.split(".")[-1]

    picker, counter = st.columns([1, 3])
    with picker:
        limit = st.selectbox(
            "Rows", [10, 100, 500, 1000], index=0,
            key=f"{key_prefix}_sample_rows",
        )

    try:
        sample = cached_read(f"SELECT * FROM {physical} LIMIT {int(limit)}")
    except Exception:
        if _sdu is not None:
            st.caption(f"`{short}` does not exist in Snowflake yet.")
            _sdu.render_panel(technical_object, f"{key_prefix}_manual", live_exists=False)
            return
        st.info(f"`{physical}` does not exist in Snowflake yet — no sample data available.")
        return

    # A documented object that is built but not yet loaded is a normal state
    # here, and an empty grid does not say that clearly.
    if sample.empty:
        st.info(f"`{short}` exists but has no rows yet.")
        if _sdu is not None:
            _sdu.render_panel(technical_object, f"{key_prefix}_manual", live_exists=False)
        return

    # A COUNT(*) on the widest views is a real scan, so it is cached and only
    # runs while the panel is open. If it fails (permissions, external table)
    # the sample is still worth showing, just without the total.
    total = None
    try:
        counted = cached_read(f"SELECT COUNT(*) AS ROW_COUNT FROM {physical}")
        if not counted.empty:
            total = int(counted.iloc[0]["ROW_COUNT"])
    except Exception:
        total = None

    with counter:
        if total is None:
            st.caption(f"Showing {len(sample):,} row(s) from `{short}`")
        else:
            st.caption(f"Showing {len(sample):,} of {total:,} row(s) from `{short}`")

    # Scale the height with the row count so 500 rows is a scroll area rather
    # than an endless page, but a 10 row preview stays compact.
    st.dataframe(sample, **_grid_height(rows=len(sample)))
    # Live data wins; a manual sample saved earlier stays one switch away.
    if _sdu is not None and st.toggle("Show the manual sample instead",
                                      key=f"{key_prefix}_manual_toggle"):
        _sdu.render_panel(technical_object, f"{key_prefix}_manual", live_exists=True)


if hasattr(st, "fragment"):
    @st.fragment
    def _sample_data_fragment(technical_object: str, key_prefix: str):
        """Reruns on its own when the row picker changes; the page does not."""
        _sample_data_panel(technical_object, key_prefix)
else:
    def _sample_data_fragment(technical_object: str, key_prefix: str):
        # Older Streamlit: correct, just reruns the whole page on a row change.
        _sample_data_panel(technical_object, key_prefix)


_STATUS_BADGE = {
    DRAFT_STATUS: ":grey[○]",
    CHANGES_REQUESTED_STATUS: ":orange[◐]",
    ACCEPTED_STATUS: ":green[●]",
}
_STATUS_WORDS = {
    DRAFT_STATUS: "Draft — not yet reviewed",
    CHANGES_REQUESTED_STATUS: "Changes requested",
    ACCEPTED_STATUS: "Accepted",
}


def _render_approval_controls(rule: dict, status: str, may_approve: bool,
                              team_id=None) -> None:
    """Accept / Request changes for one rule, plus who signed it off.

    Deliberately inside the rule's own expander, under the rendered English rather
    than beside a row in a grid: the decision should be made having read the rule,
    and render_rule() is the form the product owner can actually judge.

    `may_approve` only decides whether the buttons are drawn. The decision itself is
    made in set_rule_status() against the team that owns the rule, so a stale or
    wrong hint here can hide a button but cannot authorise anything.
    """
    rule_id = int(rule.get("RULE_ID") or 0)
    if not rule_id:
        return

    approved_by = str(rule.get("APPROVED_BY") or "").strip()
    if status == ACCEPTED_STATUS and approved_by:
        # The bundle is built with OBJECT_CONSTRUCT, which serialises timestamps as
        # strings ("2026-09-28 09:25:01.101"). Formatting one with %d %b %Y raised
        # ValueError, and a defensive except swallowed it - so no accepted rule ever
        # showed its date. Parse first; say nothing only if it really is unreadable.
        when = ""
        stamp = pd.to_datetime(rule.get("APPROVED_TS"), errors="coerce")
        if pd.notna(stamp):
            when = f" on {stamp:%d %b %Y}"
        st.success(f"Accepted by **{approved_by}**{when}.")
    elif status == CHANGES_REQUESTED_STATUS:
        st.warning(f"{_STATUS_WORDS[status]}. See the reason in the rule's history.")
    else:
        st.caption(f":grey[{_STATUS_WORDS.get(status, status)}.]")

    accept_col, changes_col = st.columns(2)
    with accept_col:
        # Only the approver's decisions are gated: accepting, and withdrawing an
        # acceptance. Hiding "Request changes" too, as this used to, contradicted
        # set_rule_status, which deliberately lets anyone raise a problem.
        if not may_approve:
            st.caption(":grey[Only an approver for this rule's team can accept it.]")
        elif status != ACCEPTED_STATUS:
            if st.button("Accept", key=f"acc_{rule_id}", type="primary",
                         use_container_width=True):
                try:
                    set_rule_status(rule_id, ACCEPTED_STATUS,
                                    performed_by=current_viewer(), team_id=team_id)
                    st.rerun()
                except (PermissionError, ValueError) as exc:
                    st.error(str(exc))
        else:
            if st.button("Return to draft", key=f"undo_{rule_id}",
                         use_container_width=True,
                         help="Withdraws the acceptance so it can be reworked."):
                try:
                    set_rule_status(rule_id, DRAFT_STATUS,
                                    performed_by=current_viewer(), team_id=team_id)
                    st.rerun()
                except (PermissionError, ValueError) as exc:
                    st.error(str(exc))
    with changes_col:
        with st.popover("Request changes", use_container_width=True):
            # The reason is mandatory in set_rule_status, so ask for it here rather
            # than letting the click fail. Rejections without a recorded reason are
            # how the review effort gets thrown away.
            reason = st.text_area("What needs changing?", key=f"rej_txt_{rule_id}",
                                  placeholder="The source column is wrong — it should "
                                              "read SOURCE_DELETE_IND, not DELETE_IND.")
            if st.button("Send back", key=f"rej_{rule_id}", type="primary"):
                try:
                    set_rule_status(rule_id, CHANGES_REQUESTED_STATUS, reason=reason,
                                    performed_by=current_viewer(), team_id=team_id)
                    st.rerun()
                except (PermissionError, ValueError) as exc:
                    st.error(str(exc))


def render_sample_data(technical_object: str, key_prefix: str):
    """
    Full-width sample rows for an object. Rendered outside any st.columns so it
    gets the whole width available - the old popover was both an overlay and
    inside a half-width column, which left no room for a wide view.
    """
    name = str(technical_object or "").strip()
    if not name or name == "(not set)":
        return
    _sample_data_fragment(name, key_prefix)


def render_object_columns(technical_object: str, key_prefix: str,
                          object_id: int = None, compact: bool = True):
    """
    Show an object's columns wherever an object is selected.

    Reads the stored spec first — that is the point of storing it, and it works
    for objects that are documented but not yet built. Falls back to a live
    INFORMATION_SCHEMA read so a newly registered object is useful immediately,
    and offers to store what it found.
    """
    name = str(technical_object or "").strip()
    if not name or name == "(not set)":
        return

    spec = pd.DataFrame()
    if object_id is None:
        objects = list_business_objects()
        if objects is not None and not objects.empty:
            hit = objects[objects["TECHNICAL_OBJECT_NAME"].astype(str).str.upper() == name.upper()]
            if not hit.empty:
                object_id = int(hit.iloc[0]["OBJECT_ID"])
    if object_id is not None:
        spec = get_object_column_spec(object_id)

    if not spec.empty:
        st.caption(f"{len(spec)} stored column(s) for `{name.split('.')[-1]}`")
        st.dataframe(
            spec.rename(columns={
                "COLUMN_NAME": "Column", "DATA_TYPE": "Type",
                "NULLABLE_IND": "Nullable", "PRIMARY_KEY_IND": "PK",
                "COLUMN_ORIGIN": "Origin", "BUSINESS_DESCRIPTION": "Description",
            })[["Column", "Type", "Nullable", "PK", "Origin", "Description"]],
            **_grid_height(compact),
        )
        return

    live = live_object_columns(name)
    if live.empty:
        st.info(f"No stored columns, and `{resolve_physical_name(name)}` could not be "
                "read from Snowflake — so there is nothing to show yet.")
        return

    st.caption(f"{len(live)} column(s) read from Snowflake — not stored yet")
    st.dataframe(
        live.rename(columns={
            "COLUMN_NAME": "Column", "DATA_TYPE": "Type",
            "NULLABLE_IND": "Nullable", "BUSINESS_DESCRIPTION": "Description",
        })[["Column", "Type", "Nullable", "Description"]],
        **_grid_height(compact),
    )
    if object_id is not None and st.button("Store these columns", key=f"{key_prefix}_store"):
        try:
            written = sync_object_columns(object_id, name, created_by=current_viewer())
            st.success(f"Stored {written} column(s).")
            st.rerun()
        except Exception as exc:
            st.error(f"Nothing was stored. {exc}")


def render_add_object_form(key_prefix: str, default_module: str = None,
                           expanded: bool = False):
    """
    Register a new business object. One implementation, used by both the Write a
    rule page and the Object columns page - the two places where you discover
    the object you need is not in the repository yet.

    key_prefix namespaces the widget keys so the same form can appear on more
    than one page (and more than once on a page) without colliding. The created
    object's id is left in session_state under "{key_prefix}_new_object_id" so
    the calling page can select it straight away.
    """
    with st.expander("+ Add new object", expanded=expanded):
        msg = st.session_state.pop(f"{key_prefix}_new_bo_msg", None)
        if msg:
            st.success(f"Object created (ID {msg['object_id']}) as `{msg['name']}`.")
            if msg.get("missing"):
                st.info(f"Note: `{msg['missing']}` does not exist in Snowflake yet. "
                        "The object is documented and will resolve once it is built.")

        obj_name = st.text_input("Business name", key=f"{key_prefix}_new_bo_name",
                                 placeholder="e.g. Item Root")

        # Database comes from the team's mapping; schema and object name are
        # typed. Examples are derived from objects already registered for this
        # team and database, so the hints stay accurate as the repository grows
        # instead of being hardcoded.
        db_choices = TEAM_LOGICAL_DATABASES or []
        if not db_choices:
            st.warning("No databases are mapped to this team yet. Add one under "
                       "**Manage teams & modules → Databases**.")
            obj_db = ""
        elif len(db_choices) == 1:
            obj_db = db_choices[0]
            st.text_input("Database", value=obj_db, disabled=True,
                          key=f"{key_prefix}_new_bo_db_ro",
                          help="Set by the team's database mapping.")
        else:
            obj_db = st.selectbox("Database", db_choices, key=f"{key_prefix}_new_bo_db",
                                  help="The team's databases, primary first.")

        # Schemas and an example object already in use for this team+database.
        existing = scope_to_team(list_business_objects())
        known_schemas, example_object = [], ""
        if existing is not None and not existing.empty and obj_db:
            names = existing["TECHNICAL_OBJECT_NAME"].dropna().astype(str)
            same_db = [n for n in names if n.upper().startswith(obj_db.upper() + ".")
                       and n.count(".") == 2]
            known_schemas = list(dict.fromkeys(n.split(".")[1] for n in same_db))
            if same_db:
                example_object = same_db[0].split(".")[2]

        obj_schema = st.text_input(
            "Schema", key=f"{key_prefix}_new_bo_schema",
            placeholder=f"e.g. {known_schemas[0]}" if known_schemas else "e.g. PDH_COMMON_XFRM",
        )
        if known_schemas:
            st.caption("Schemas already in use here: "
                       + ", ".join(f"`{s}`" for s in known_schemas[:6]))

        obj_object = st.text_input(
            "Object name", key=f"{key_prefix}_new_bo_object",
            placeholder=f"e.g. {example_object}" if example_object else "e.g. V_BRD_ITEM_ROOT",
        )

        obj_type = st.selectbox("Object type", ["TABLE", "VIEW"], key=f"{key_prefix}_new_bo_type")
        bo_modules = scope_to_team(get_modules())
        has_modules = bo_modules is not None and not bo_modules.empty
        mod_labels = (["(none)"] + [str(r["MODULE_NAME"]) for _, r in bo_modules.iterrows()]
                      if has_modules else ["(none)"])
        mod_by_label = ({str(r["MODULE_NAME"]): int(r["MODULE_ID"])
                         for _, r in bo_modules.iterrows()} if has_modules else {})
        obj_mod = st.selectbox(
            "Module", mod_labels, key=f"{key_prefix}_new_bo_module",
            index=(mod_labels.index(default_module)
                   if default_module in mod_labels else 0),
        )

        # Live preview so there is no doubt what gets saved.
        assembled = ""
        if obj_db and obj_schema.strip() and obj_object.strip():
            assembled = f"{obj_db}.{obj_schema.strip()}.{obj_object.strip()}".upper()
            st.caption(f"Will be saved as `{assembled}`")

        if st.button("Add object", key=f"{key_prefix}_btn_create_bo"):
            ident = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")
            if not obj_name.strip():
                st.error("Business name is required.")
            elif not obj_db:
                st.error("No database is mapped to this team.")
            elif not ident.match(obj_schema.strip()):
                st.error("Schema must be a plain SQL identifier, e.g. PDH_COMMON_XFRM.")
            elif not ident.match(obj_object.strip()):
                st.error("Object name must be a plain SQL identifier, e.g. V_BRD_ITEM_ROOT.")
            else:
                try:
                    new_obj_id = create_business_object(
                        obj_name.strip(),
                        assembled,
                        object_type=obj_type,
                        module_id=mod_by_label.get(obj_mod),
                        performed_by=current_viewer(),
                    )
                    # Stashed rather than shown inline: the rerun below (which
                    # refreshes the object lists) would discard anything drawn here.
                    from compliance import object_exists as _exists
                    st.session_state[f"{key_prefix}_new_bo_msg"] = {
                        "object_id": int(new_obj_id),
                        "name": assembled,
                        "missing": (None if _exists(assembled)
                                    else resolve_physical_name(assembled)),
                    }
                    st.session_state[f"{key_prefix}_new_object_id"] = int(new_obj_id)
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not create object: {exc}")


def _source_column_types(technical_object: str) -> dict:
    """
    {COLUMN_NAME: DATA_TYPE} for an object, so a pass-through column can inherit
    the source's type instead of the BA retyping it.

    Types are rendered the way a BA would write them in the FDD - VARCHAR(18)
    rather than TEXT with a separate length column.
    """
    parts = [p for p in str(technical_object or "").split(".") if p]
    if len(parts) != 3:
        return {}
    # The FDD may record a logical database that does not exist here, so translate
    # before reading INFORMATION_SCHEMA.
    parts = [p for p in resolve_physical_name(technical_object).split(".") if p]
    if len(parts) != 3:
        return {}
    db, schema, name = parts
    try:
        df = cached_read(f"""
            SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH AS LEN,
                   NUMERIC_PRECISION AS PREC, NUMERIC_SCALE AS SCALE
            FROM {db}.INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = {sql_value(schema.upper())}
              AND TABLE_NAME = {sql_value(name.upper())}
        """)
    except Exception:
        return {}
    if df.empty:
        return {}

    rendered = {}
    for _, r in df.iterrows():
        base = str(r["DATA_TYPE"] or "").upper()
        if base in ("TEXT", "VARCHAR", "CHAR", "STRING") and pd.notna(r["LEN"]):
            rendered[str(r["COLUMN_NAME"]).upper()] = f"VARCHAR({int(r['LEN'])})"
        elif base in ("NUMBER", "DECIMAL", "NUMERIC") and pd.notna(r["PREC"]):
            scale = int(r["SCALE"]) if pd.notna(r["SCALE"]) else 0
            rendered[str(r["COLUMN_NAME"]).upper()] = f"NUMBER({int(r['PREC'])},{scale})"
        else:
            rendered[str(r["COLUMN_NAME"]).upper()] = base
    return rendered


def _parse_pasted_columns(text: str) -> list:
    """
    Parse a pasted column list into spec entries.

    Accepts tab or comma separated, one column per line: name, then optionally
    data type, then description. Tab is tried first because that is what an Excel
    copy produces, and a description containing a comma would otherwise be split.
    A header row naming the first cell "column" or similar is skipped.
    """
    entries = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        cells = [c.strip() for c in (line.split("\t") if "\t" in line else line.split(","))]
        cells = [c for c in cells if c != ""]
        if not cells:
            continue
        name = cells[0]
        if name.lower().replace(" ", "_") in ("column", "column_name", "columnname", "field"):
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        entries.append({
            "column_name": name.upper(),
            "data_type": cells[1] if len(cells) > 1 else "",
            "business_description": cells[2] if len(cells) > 2 else "",
            "nullable_ind": True,
            "primary_key_ind": False,
        })
    return entries


def render_discovered_objects(text: str, key_prefix: str,
                              module_id=None) -> None:
    """Show and offer to register DB.SCHEMA.OBJECT names found in rule text.

    Scans the text for fully qualified names that are not yet registered as
    business objects. For each one, a one-click button creates the object and
    syncs its columns from Snowflake.
    """
    import fdd_import as fi

    mentioned = fi.TECHNICAL_NAME.findall(str(text or "").upper())
    if not mentioned:
        return

    bo_df = list_business_objects()
    known = set()
    if bo_df is not None and not bo_df.empty:
        known = set(bo_df["TECHNICAL_OBJECT_NAME"].dropna().astype(str).str.upper())

    new_names = list(dict.fromkeys(n for n in mentioned if n not in known))
    if not new_names:
        return

    st.markdown(f"**{len(new_names)} object(s) in this rule are not registered**")
    st.caption(
        "These fully qualified names appear in the rule text but are not in the "
        "repository. Register them to enable column validation and lineage."
    )

    for i, technical in enumerate(new_names):
        btn_key = f"{key_prefix}_register_{i}"
        done_key = f"{key_prefix}_registered_{technical}"

        if st.session_state.get(done_key):
            st.success(f"Registered: `{technical}` (ID {st.session_state[done_key]})")
            continue

        leaf = technical.rsplit(".", 1)[-1]
        object_type = "VIEW" if leaf.startswith("V_") else "BASE_TABLE"
        name = fi._object_name_from(technical)

        col_info, col_btn = st.columns([4, 1])
        with col_info:
            st.code(technical, language=None)
            st.caption(f"{name}  ·  {object_type}")
        with col_btn:
            if st.button("Register", key=btn_key):
                try:
                    obj_id = create_business_object(
                        name, technical, object_type=object_type,
                        module_id=module_id,
                        performed_by=current_viewer(),
                    )
                    sync_object_columns(int(obj_id), technical,
                                        created_by=current_viewer())
                    st.session_state[done_key] = int(obj_id)
                    _flash(f"Registered `{technical}` (ID {obj_id}) with columns from Snowflake.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not register {technical}: {exc}")


def render_test_suggestions(state_key: str):
    """
    Show generated test cases for a just-saved or just-updated rule.

    Reads its context out of st.session_state[state_key] rather than taking it as
    an argument, because it renders on the run *after* the Save click: the button
    handler stashes the context and this draws on every subsequent rerun, so
    accepting one case does not wipe the panel.

    Context shape: {rule_id, rule_number, proposal, previous, target_object,
                    source_action, cases}
    """
    ctx = st.session_state.get(state_key)
    if not ctx:
        return

    import test_generator as tg
    from compliance import get_columns

    st.divider()
    header = ("Suggested test cases" if ctx.get("source_action") == "ON_CREATE"
              else "Suggested test case changes")
    st.markdown(f"### {header}")

    # Generate only when explicitly requested, then cache in session state so
    # reruns from the Accept buttons don't re-bill the model.
    if ctx.get("cases") is None:
        if not st.button("Generate test suggestions", key=f"{state_key}_generate",
                         type="secondary"):
            st.caption("Click above to generate AI-suggested test cases for this rule.")
            return
        with st.spinner("Working out what should be tested…"):
            cases = tg.suggest_test_cases(
                ctx["proposal"],
                target_object=ctx.get("target_object"),
                previous=ctx.get("previous"),
            )
            columns = get_columns(ctx["target_object"]) if ctx.get("target_object") else set()
            sql, reason = tg.build_any_test_sql(
                ctx["proposal"], ctx.get("target_object"), columns, with_reason=True
            )
            for i, case in enumerate(cases):
                case["test_id"] = tg.test_id(ctx.get("rule_number"), i)
                case["test_sql"] = sql
                case["not_runnable_reason"] = None if sql else reason
        ctx["cases"] = cases
        st.session_state[state_key] = ctx

    cases = ctx.get("cases") or []
    if not cases:
        st.info("No test cases could be suggested for this rule. Add them by hand on "
                "the **Test cases** page.")
        if st.button("Dismiss", key=f"{state_key}_dismiss_empty"):
            st.session_state.pop(state_key, None)
            st.rerun()
        return

    if ctx.get("source_action") == "ON_UPDATE":
        existing = get_test_cases(ctx["rule_id"])
        if not existing.empty:
            st.caption(f"This rule already has {len(existing)} saved test case(s). "
                       "Review whether any of them need updating too.")

    accepted_flags = {}
    for group, label in ((tg.FUNCTIONAL, "Functional test cases"),
                         (tg.DQ_CHECK, "Data quality checks")):
        group_cases = [c for c in cases if c.get("test_type") == group]
        if not group_cases:
            continue
        st.markdown(f"**{label}**")
        for case in group_cases:
            key = f"{state_key}_{case['test_id']}"
            with st.container(border=True):
                accepted_flags[case["test_id"]] = st.checkbox(
                    f"`{case['test_id']}` {case['test_name']}", value=True, key=f"{key}_pick"
                )
                if case.get("scenario"):
                    st.caption(f"Scenario: {case['scenario']}")
                if case.get("expected_result"):
                    st.caption(f"Expected: {case['expected_result']}")
                if case.get("test_sql"):
                    with st.expander("Generated SQL"):
                        st.code(case["test_sql"], language="sql")
                else:
                    st.caption(":grey[Manual check — no runnable SQL: "
                               f"{case.get('not_runnable_reason') or 'not expressible as a query'}.]")

    save_col, skip_col = st.columns(2)
    with save_col:
        if st.button("Save selected test cases", type="primary", key=f"{state_key}_save"):
            picked = [c for c in cases if accepted_flags.get(c["test_id"])]
            if not picked:
                st.warning("Nothing selected.")
            else:
                try:
                    written = save_test_cases(
                        ctx["rule_id"], picked,
                        source_action=ctx.get("source_action", "BULK"),
                        status="ACCEPTED", created_by=current_viewer(),
                    )
                    st.success(f"Saved {written} test case(s).")
                    st.session_state.pop(state_key, None)
                    st.rerun()
                except Exception as exc:
                    st.error(f"Nothing was saved. {exc}")
    with skip_col:
        if st.button("Skip for now", key=f"{state_key}_skip"):
            st.session_state.pop(state_key, None)
            st.rerun()


def _choices(base, *current) -> list:
    return _rule_editor.choices(base, *current)


def _render_rule_editor(bundle: dict, rule_id: int) -> None:
    """The Current rule block as fields and tables. Check changes runs the same
    validation as a typed update and hands the result to the confirm step below."""
    k = f"edit_{rule_id}"
    edited = _rule_editor.render_fields(_rc._bundle_to_proposal(bundle), k)

    if st.button("Check changes", type="primary", key=f"{k}_check"):
        try:
            _ep = get_rule_set_endpoints(int(bundle["rule"].get("RULE_SET_ID")))
        except Exception:
            _ep = {"sources": []}
        result = _rc.compile_direct_edit(
            bundle, edited,
            _ep.get("sources") or bundle["rule"].get("SOURCE_TECHNICAL_OBJECT", ""),
            bundle["rule"].get("TARGET_TECHNICAL_OBJECT", ""))
        if not result.get("changed"):
            st.info("No changes - the rule is the same as the stored one.")
            st.session_state.pop("update_compiled", None)
            return
        result.update({"rule_id": rule_id, "source": "direct", "elapsed": 0})
        st.session_state["update_compiled"] = result
        st.caption("Checked - review it under **Check this reads the way you meant it** below.")


# ==================================================================
# Requirements - the reading view
# ==================================================================
if page == "Requirements":
    # Where "Back to top" and the floating Top button land.
    _pager.top_anchor()
    _all_rules = get_rule_catalog()
    catalog = scope_to_team(_all_rules)
    if catalog.empty:
        # Distinguish "there are no rules" from "this team owns none of them".
        # Blaming the view when the team filter emptied the frame sends you
        # looking in the wrong place - the rules are there, just out of scope.
        if _all_rules is not None and not _all_rules.empty:
            _hidden_modules = sorted(
                _all_rules["MODULE_NAME"].dropna().astype(str).unique().tolist())
            st.warning(
                f"{len(_all_rules)} rule(s) exist, but none belong to **{TEAM_NAME}**, "
                "so nothing is shown here."
            )
            st.info(
                "Switch the Team picker to **All teams** to see them, or assign the "
                "module to this team under **Manage teams & modules**."
                + (f"\n\nModules currently holding rules: {', '.join(_hidden_modules)}"
                   if _hidden_modules else "")
            )
        else:
            st.warning("No rules found in VW_RULE_SUMMARY.")
            error = get_last_error()
            if error:
                st.error(f"The read failed, that's why it looked empty: {error}")
        st.stop()

    # Filters on the left; everything about the chosen module as a whole - PO review
    # comments and the dev hand-off status - in a panel on the right, instead of the
    # hand-off strip sitting between the Module picker and the other filters.
    _filters, _side = st.columns([3, 2], gap="large")
    _module_ids = {
        str(r["MODULE_NAME"]): int(r["MODULE_ID"])
        for _, r in catalog[["MODULE_ID", "MODULE_NAME"]].dropna().drop_duplicates().iterrows()
    }
    with _filters:
        modules = sorted(catalog["MODULE_NAME"].dropna().astype(str).unique().tolist())
        chosen_module = st.selectbox("Module", modules, index=None, key="req_module",
                                     placeholder="Choose a module")
    # Nothing is listed until a module is chosen: every module's rules on one page was
    # slow to draw and not what anyone reads.
    if not chosen_module:
        st.info(f"Choose a module to see its rules ({len(modules)} module(s), "
                f"{len(catalog)} rule(s)).")
        st.stop()
    _chosen_module_id = _module_ids.get(str(chosen_module))
    scoped = catalog[catalog["MODULE_NAME"].astype(str) == chosen_module]

    with _filters:
        _f1, _f2 = st.columns(2)
        with _f1:
            rule_sets = ["All"] + sorted(scoped["RULE_SET_NAME"].dropna().astype(str).unique().tolist())
            chosen_set = st.selectbox("Rule set", rule_sets)
        with _f2:
            targets = ["All"] + sorted(
                scoped["TARGET_BUSINESS_OBJECT"].dropna().astype(str).unique().tolist()
            )
            chosen_target = st.selectbox("Target object", targets)
        search = st.text_input("Search", placeholder="item description, status, exclusions…")

    with _side:
        # Stands in for the Module label, so the panel starts level with the Module box
        # rather than with its label.
        st.markdown("<div style='height:1.75rem'></div>", unsafe_allow_html=True)
        if _chosen_module_id is not None:
            with st.popover(f"✍ Review comments — {chosen_module}", use_container_width=True):
                with st.form(f"add_review_{_chosen_module_id}", clear_on_submit=True):
                    _new_comment = st.text_area(
                        "What needs to change?",
                        key=f"review_text_{_chosen_module_id}",
                        height=110,
                        placeholder="e.g. the status mapping should exclude blocked plants",
                    )
                    st.caption(f"Will be added as **{current_viewer()}**.")
                    if st.form_submit_button("Add review comment", type="primary"):
                        if not _new_comment.strip():
                            st.error("Add some comment text first.")
                        else:
                            add_review_comment(_chosen_module_id, _new_comment, current_viewer())
                            st.success("Review comment added.")
                            st.rerun()

        # ---- dev hand-off -----------------------------------------------------
        # Where this module is between review and development. Shared with
        # pdh-engineering-studio through VW_MODULE_DEV_STATUS.
        _dev_objects = None
        if _mh is not None and _chosen_module_id is not None:
            try:
                _hand_mods = get_modules()
                _hand_owner = None
                if _hand_mods is not None and not _hand_mods.empty:
                    _hit = _hand_mods[_hand_mods["MODULE_ID"].astype(int) == int(_chosen_module_id)]
                    if not _hit.empty and pd.notna(_hit.iloc[0]["TEAM_ID"]):
                        _hand_owner = int(_hit.iloc[0]["TEAM_ID"])
                _hand_comments = get_review_comments()
                _hand_open = 0
                if _hand_comments is not None and not _hand_comments.empty:
                    _mine = _hand_comments[_hand_comments["MODULE_ID"].astype("Int64") == _chosen_module_id]
                    _hand_open = int((~_mine["STATUS"].astype(str).str.upper()
                                      .isin(["DONE", "RESOLVED", "CLOSED"])).sum())
                _mh.render_strip(_chosen_module_id, str(chosen_module), _hand_owner,
                                 current_viewer(), can_approve_team(current_viewer(), _hand_owner),
                                 _hand_open, compact=True)
                _dev_objects = _mh.repo.get_module_dev_objects(_chosen_module_id)
            except Exception as _hand_exc:
                # st.rerun() works by raising; let it through.
                if type(_hand_exc).__name__ in ("RerunException", "StopException"):
                    raise
                st.caption(f":grey[Dev hand-off status could not be shown: {_hand_exc}]")

    # ---- approval status ----------------------------------------------------
    # RULE_STATUS comes through the catalog already; retired rules are excluded
    # upstream by get_rule_catalog(), so every rule here is in the workflow.
    _status_of = scoped["RULE_STATUS"].map(normalise_status) if "RULE_STATUS" in scoped.columns \
        else pd.Series([DRAFT_STATUS] * len(scoped), index=scoped.index)
    _counts = _status_of.value_counts().to_dict()
    _labels = {
        "All": f"All ({len(scoped)})",
        DRAFT_STATUS: f"Draft ({_counts.get(DRAFT_STATUS, 0)})",
        CHANGES_REQUESTED_STATUS: f"Changes requested ({_counts.get(CHANGES_REQUESTED_STATUS, 0)})",
        ACCEPTED_STATUS: f"Accepted ({_counts.get(ACCEPTED_STATUS, 0)})",
    }
    _status_choice = st.radio(
        "Status", list(_labels), horizontal=True, key="req_status",
        format_func=lambda k: _labels[k],
        help="Draft and Changes requested still need a decision from the product "
             "owner. Accepted rules are signed off; editing one returns it to Draft.",
    )

    # Who may accept. While no approver has been named anyone can, and the page says
    # so - a locked door with no key would leave the app unable to accept its first
    # rule. _may_approve is only "is this person an approver at all", for the banner;
    # the buttons are decided per rule below.
    _approver_team_id = st.session_state.get("_team_id")
    _may_approve = can_approve(current_viewer(), _approver_team_id)
    # Per rule, the hint uses the team that owns the rule's module - the same thing
    # set_rule_status() authorises against - so a team-limited approver browsing
    # "All teams" is not shown Accept buttons that would only fail when clicked.
    _mods_for_team = get_modules()
    _module_team = ({} if _mods_for_team is None or _mods_for_team.empty else
                    {int(r["MODULE_ID"]): (None if pd.isna(r["TEAM_ID"]) else int(r["TEAM_ID"]))
                     for _, r in _mods_for_team.iterrows()})

    def _may_approve_module(module_id) -> bool:
        owner = _module_team.get(int(module_id)) if pd.notna(module_id) else None
        return can_approve_team(current_viewer(), owner)

    if not approvals_are_restricted():
        st.info("**Anyone can accept rules at the moment.** Name the product owner under "
                "Manage teams & modules to restrict it. Whoever accepts is recorded either way.")
    elif not _may_approve:
        st.caption(f":grey[You are not listed as an approver"
                   + (f" for {TEAM_NAME}" if _approver_team_id is not None else "")
                   + ", so the accept controls are hidden. You can still read and edit rules.]")

    view = scoped.copy()
    _as_written_n = int(scoped["RULE_CATEGORY"].astype(str).str.upper()
                        .isin(["AS_WRITTEN", "CONTEXT"]).sum()) if "RULE_CATEGORY" in scoped else 0
    if _as_written_n:
        st.caption(f":grey[{_as_written_n} rule(s) here are saved **as written** - kept in the "
                   "FDD's words because the app could not model their logic. Search "
                   "\"as_written\" (or \"context\" for older ones) to list them.]")
    if _status_choice != "All":
        view = view[_status_of.reindex(view.index) == _status_choice]
    if chosen_set != "All":
        view = view[view["RULE_SET_NAME"].astype(str) == chosen_set]
    if chosen_target != "All":
        view = view[view["TARGET_BUSINESS_OBJECT"].astype(str) == chosen_target]
    if search.strip():
        needle = search.strip().lower()
        blob = (view["RULE_NUMBER"].astype(str) + " " + view["RULE_NAME"].astype(str) + " " +
                view["BUSINESS_DESCRIPTION"].astype(str) + " " +
                view["RULE_CATEGORY"].astype(str)).str.lower()
        view = view[blob.str.contains(needle, regex=False)]

    st.write(f"{len(view)} rule(s)")
    if view.empty:
        st.info("Nothing matches those filters.")
        st.stop()

    # ---- PO review comments -------------------------------------------------
    # Keyed on the module, so the section only has something to show once a
    # specific module is picked. The add control itself sits beside the Module
    # filter above; this is the read/tick-off list.
    _comments = get_review_comments()

    def _toggle_comment(comment_id: int, widget_key: str):
        """
        on_change handler for the done checkbox: writes straight through, so the
        PO doesn't have to find a save button. Streamlit has already put the new
        value in session_state by the time this runs.
        """
        set_review_comment_done(
            int(comment_id), bool(st.session_state.get(widget_key)), current_viewer()
        )

    if _chosen_module_id is not None:
        _module_comments = (
            _comments[_comments["MODULE_ID"].astype("Int64") == _chosen_module_id]
            if not _comments.empty else _comments
        )
        _render_module_comments(_module_comments, chosen_module, _toggle_comment)

    # Group by target object first - this is what a BA actually wants to browse:
    # "how is this object built, and by which rules", not a flat rule-set list.
    # Order the groups by pipeline sequence (source -> ... -> target), not
    # alphabetically, so upstream objects always read before what they feed.
    lineage_edges = get_lineage_edges()
    lineage_rank = _lineage_ranks(lineage_edges)
    view = view.copy()
    view["_LINEAGE_RANK"] = view["TARGET_BUSINESS_OBJECT"].map(lambda t: lineage_rank.get(t, 10**6))
    view = view.sort_values(
        ["_LINEAGE_RANK", "TARGET_BUSINESS_OBJECT", "RULE_SET_ID", "PRIORITY"], na_position="last"
    ).drop(columns=["_LINEAGE_RANK"])
    target_col = view["TARGET_BUSINESS_OBJECT"].fillna("(no target recorded)")
    targets_in_order = list(dict.fromkeys(target_col.tolist()))

    # Page by target-object groups, not raw rules, so a group is never split
    # across pages. Keeps each page under load_bundles' 25-rule fetch cap.
    MAX_RULES_PER_PAGE = 20
    pages, current_page, current_count = [], [], 0
    for target in targets_in_order:
        count = int((target_col == target).sum())
        if current_page and current_count + count > MAX_RULES_PER_PAGE:
            pages.append(current_page)
            current_page, current_count = [], 0
        current_page.append(target)
        current_count += count
    if current_page:
        pages.append(current_page)

    total_pages = max(1, len(pages))
    page_no = _pager.current_page(total_pages, _pager.signature_of(
        TEAM_ID, chosen_module, chosen_set, chosen_target, search.strip(), _status_choice))
    window_targets = pages[page_no - 1]
    window = view[target_col.isin(window_targets)]
    _first = int(sum(int(target_col.isin(p).sum()) for p in pages[:page_no - 1]))
    _pager_caption = (f"Page {page_no} of {total_pages} · rules {_first + 1}–"
                      f"{_first + len(window)} of {len(view)}")
    _pager.render_pager(total_pages, page_no, "top", _pager_caption)
    _pager.floating_top_button()

    bundles = load_bundles(window["RULE_ID"].astype(int).tolist())
    # One query for every note on this page, rather than one per rule.
    _page_notes = get_rule_notes(window["RULE_ID"].astype(int).tolist())
    if _page_notes is None:
        _page_notes = pd.DataFrame()
    bundles_by_id = {int(b["rule"].get("RULE_ID", -1)): b for b in bundles}

    ordered_bundles = []
    for target in window_targets:
        group = window[window["TARGET_BUSINESS_OBJECT"].fillna("(no target recorded)") == target]
        sources = sorted(group["SOURCE_BUSINESS_OBJECT"].dropna().astype(str).unique().tolist())
        rule_set_names = sorted(group["RULE_SET_NAME"].dropna().astype(str).unique().tolist())
        categories = sorted(group["RULE_CATEGORY"].dropna().astype(str).unique().tolist())

        st.markdown(f"### {target}")
        rule_sets_text = ", ".join(rule_set_names) if rule_set_names else "no rule set recorded"
        if sources:
            st.caption(
                f"Derived from **{', '.join(sources)}** via {len(rule_set_names)} rule set(s) "
                f"({rule_sets_text}) — {len(group)} rule(s): {', '.join(categories)}."
            )
        else:
            st.caption(
                f"{len(group)} rule(s) across {len(rule_set_names)} rule set(s) "
                f"({rule_sets_text}): {', '.join(categories)}."
            )
        if _mh is not None and _dev_objects is not None:
            _badge = _mh.object_badge(_dev_objects, _mh.target_of(group))
            if _badge:
                st.markdown(_badge)

        group_bundles = [bundles_by_id[rid] for rid in group["RULE_ID"].astype(int).tolist()
                         if rid in bundles_by_id]
        ordered_bundles.extend(group_bundles)

        # Bulk accept for the whole target object. A product owner reviewing a module
        # should not have to open twenty expanders to sign off twenty pass-through
        # rules, which is most of what an imported FDD produces.
        # Only rules this viewer may accept for their owning team, so the button's
        # count is what will actually happen rather than what the batch attempts.
        _group_pending = [int(r["RULE_ID"]) for _, r in group.iterrows()
                          if normalise_status(r.get("RULE_STATUS")) != ACCEPTED_STATUS
                          and _may_approve_module(r.get("MODULE_ID"))]
        if _group_pending:
            _bulk, _bulk_note = st.columns([1.3, 3])
            with _bulk:
                if st.button(f"Accept all {len(_group_pending)}",
                             key=f"req_bulk_{target}", use_container_width=True):
                    try:
                        _res = set_rule_statuses(
                            _group_pending, ACCEPTED_STATUS,
                            performed_by=current_viewer(), team_id=_approver_team_id)
                        st.success(f"Accepted {len(_res['changed'])} rule(s).")
                        for _rid, _why in _res["skipped"]:
                            st.caption(f":grey[Rule {_rid} skipped — {_why}]")
                        st.rerun()
                    except PermissionError as exc:
                        st.error(str(exc))
            with _bulk_note:
                st.caption(f":grey[{len(_group_pending)} of {len(group)} rule(s) for this "
                           "object still need a decision.]")

        for bundle in group_bundles:
            rule = bundle["rule"]
            _status = normalise_status(rule.get("RULE_STATUS"))
            heading = (f"{_STATUS_BADGE.get(_status, '')} Rule {rule.get('RULE_NUMBER', '')} "
                       f"— {rule.get('RULE_NAME', '')}")
            with st.expander(heading, expanded=len(group_bundles) == 1 and len(window_targets) == 1):
                st.markdown(render_rule(bundle, "grouped"))
                _rid = int(rule.get("RULE_ID"))
                render_rule_notes(_rid, "reqnote", inline=True,
                                  notes=(_page_notes[_page_notes["RULE_ID"].astype(int) == _rid]
                                         if not _page_notes.empty else _page_notes))
                _render_approval_controls(rule, _status,
                                          _may_approve_module(rule.get("MODULE_ID")),
                                          _approver_team_id)
                _target_obj = str(rule.get("TARGET_TECHNICAL_OBJECT") or "").strip()
                if _target_obj:
                    _pv, _cols = st.columns(2)
                    with _pv:
                        _show_sample = st.toggle(
                            "Sample data",
                            key=f"req_sample_toggle_{rule.get('RULE_ID')}",
                            help="Rows from the target object, at full width.",
                        )
                    with _cols:
                        with st.popover("Columns"):
                            render_object_columns(
                                _target_obj, f"req_cols_{rule.get('RULE_ID')}")
                    # Outside the columns on purpose: this is what gives the
                    # preview the full expander width.
                    if _show_sample:
                        render_sample_data(_target_obj, f"req_sample_{rule.get('RULE_ID')}")
        _pager.back_to_top_link()
        st.divider()

    _pager.render_pager(total_pages, page_no, "bottom", _pager_caption)
    st.download_button(
        "Download these rules as FDD text",
        data=render_many(ordered_bundles),
        file_name="rules_fdd.md",
        mime="text/markdown",
    )
    _pager.back_to_top_link()

# ==================================================================
# Write a rule
# ==================================================================
elif page == "Dev pipeline":
    if _mh is None:
        st.warning("The dev hand-off module (module_handoff_ui.py) is not deployed.")
        st.stop()
    _pipe_mods = get_modules()
    _pipe_team = ({} if _pipe_mods is None or _pipe_mods.empty else
                  {int(r["MODULE_ID"]): (None if pd.isna(r["TEAM_ID"]) else int(r["TEAM_ID"]))
                   for _, r in _pipe_mods.iterrows()})
    _mh.render_pipeline_page(TEAM_MODULE_IDS, current_viewer(), _pipe_team)

elif page == "Write a rule":
    st.subheader("Write a requirement in plain English")
    st.caption(
        "Example: If the item type is FERT and the deletion flag is blank, "
        "then set the item status to ACTIVE and copy the source description into ITEM_DSC."
    )

    requirement = st.text_area("Requirement", height=140, key="requirement_text")

    # Module filter — scopes rule sets and objects below
    _all_modules = scope_to_team(get_modules())
    _write_mod_labels = ["All"]
    _write_mod_by_label = {}
    if _all_modules is not None and not _all_modules.empty:
        for _, _mr in _all_modules.iterrows():
            _write_mod_labels.append(str(_mr["MODULE_NAME"]))
            _write_mod_by_label[str(_mr["MODULE_NAME"])] = int(_mr["MODULE_ID"])
    _write_chosen_module = st.selectbox("Module", _write_mod_labels, key="write_module")
    _write_module_id = _write_mod_by_label.get(_write_chosen_module)

    col1, col2 = st.columns(2)
    with col1:
        # Rule set picker scoped to selected module
        _rs_df = cached_read(
            f"""SELECT rs.RULE_SET_ID, rs.RULE_SET_NAME, rs.MODULE_ID
                FROM {tbl('RULE_SET')} rs ORDER BY rs.RULE_SET_ID"""
        )
        _rs_df = scope_to_team(_rs_df)
        if _write_module_id is not None:
            _rs_df = _rs_df[_rs_df["MODULE_ID"].apply(
                lambda v: pd.isna(v) or int(v) == _write_module_id)]
        if _rs_df is None or _rs_df.empty:
            st.warning("No rule sets match this module. Create one below or switch to All.")
            rule_set_id = None
        else:
            _rs_options = {f"{r['RULE_SET_ID']} — {r['RULE_SET_NAME']}": int(r["RULE_SET_ID"])
                          for _, r in _rs_df.iterrows()}
            rule_set_id = _rs_options[st.selectbox("Rule set", list(_rs_options))]
        suggested_number = next_rule_number(rule_set_id) if rule_set_id is not None else ""
        rule_number = st.text_input(
            "Rule number", value=suggested_number, key=f"write_rule_number_{rule_set_id}",
            help="Auto-suggested as the next number in this rule set. Edit it if you use "
                 "a different scheme, like 3E, IL or NOTE1.",
        )

        # --- inline rule set creation ---
        with st.expander("+ Create new rule set"):
            _modules = scope_to_team(get_modules())
            _mod_labels = (["(none)"] + [str(r["MODULE_NAME"]) for _, r in _modules.iterrows()]
                           if _modules is not None and not _modules.empty else ["(none)"])
            _mod_by_label = ({str(r["MODULE_NAME"]): int(r["MODULE_ID"])
                              for _, r in _modules.iterrows()}
                             if _modules is not None and not _modules.empty else {})
            _rs_name = st.text_input("Rule set name", key="new_rs_name")
            _rs_mod = st.selectbox("Module", _mod_labels, key="new_rs_module",
                                   index=(_mod_labels.index(_write_chosen_module)
                                          if _write_chosen_module in _mod_labels else 0))
            _rs_desc = st.text_input("Description (optional)", key="new_rs_desc")
            if st.button("Create rule set", key="btn_create_rs"):
                if not _rs_name.strip():
                    st.error("Enter a rule set name.")
                else:
                    try:
                        _new_rs_id = create_rule_set(
                            _rs_name.strip(),
                            module_id=_mod_by_label.get(_rs_mod),
                            description=_rs_desc.strip(),
                            performed_by=current_viewer(),
                        )
                        st.success(f"Rule set created (ID {_new_rs_id}). Select it above.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Could not create rule set: {exc}")

    with col2:
        bo_df = list_business_objects()
        bo_df = scope_to_team(bo_df)
        if _write_module_id is not None and bo_df is not None and not bo_df.empty:
            bo_df = bo_df[bo_df["MODULE_ID"].apply(
                lambda v: pd.isna(v) or int(v) == _write_module_id)]
        if bo_df is None or bo_df.empty:
            objects = ["(not set)"]
        else:
            objects = ["(not set)"] + sorted(
                bo_df["TECHNICAL_OBJECT_NAME"].dropna().astype(str).unique().tolist()
            )

        # A rule set can read several objects - the Item stage view joins the item
        # root, the characteristic root and the sales detail root. The first one
        # picked becomes the primary source; the rest are recorded in
        # RULE_SET_SOURCE, which is what the lineage diagram is built from.
        _existing = get_rule_set_endpoints(rule_set_id) if rule_set_id is not None \
            else {"sources": [], "target": ""}
        _selectable = [o for o in objects if o != "(not set)"]
        source_objects = st.multiselect(
            "Reads from", _selectable,
            default=[s for s in _existing["sources"] if s in _selectable],
            key=f"write_sources_{rule_set_id}",
            help="Pick every object this rule set reads. The first is the primary "
                 "source. Each one becomes an arrow into the target on the lineage "
                 "diagram.",
        )
        if len(source_objects) > 1:
            st.caption(f"Primary source: **{source_objects[0].rsplit('.', 1)[-1]}**. "
                       "Reorder by clearing and re-picking.")
        _target_default = (objects.index(_existing["target"])
                           if _existing["target"] in objects else 0)
        target_object = st.selectbox("Writes to", objects, index=_target_default,
                                     key=f"write_target_{rule_set_id}")

        if _existing["sources"] and sorted(_existing["sources"]) != sorted(source_objects):
            st.warning("These are the rule set's sources, not this rule's, so changing "
                       "them affects every rule in the set and redraws the lineage. "
                       "They are applied when you save.")

        # Columns for whichever objects are selected. This is the highest-value
        # place to show them: the BA is naming columns in the requirement right
        # now, and the compiler warns when a named column is not on the target.
        for _i, _src in enumerate(source_objects):
            with st.expander(f"Columns in {_src.split('.')[-1]} "
                             f"({'primary source' if _i == 0 else 'source'})"):
                render_object_columns(_src, f"wr_src{_i}")
        if target_object != "(not set)":
            with st.expander(f"Columns in {target_object.split('.')[-1]} (target)"):
                render_object_columns(target_object, "wr_tgt")

        # --- inline business object creation ---
        # Same form as the Object columns page, one implementation.
        render_add_object_form("wr", default_module=_write_chosen_module)

    with st.expander("Options"):
        model = st.selectbox("Model", MODEL_CHOICES)
        use_llm = st.checkbox("Use the model to structure the requirement", value=True,
                              help="Turn off to use sentence parsing only. Faster, stricter, "
                                   "handles simple IF/THEN requirements well.")

    if st.button("Prepare the rule", type="primary"):
        if not requirement.strip():
            st.error("Type the requirement first.")
        elif rule_set_id is None:
            st.error("Pick a rule set.")
        elif not _existing["target"] and target_object == "(not set)":
            # A rule set with no target is invisible everywhere that lists by object -
            # Update a rule, Requirements, lineage. Two Location rules were saved this
            # way and "disappeared". Saving sets the target from Writes to, so asking
            # for it here is the whole fix.
            st.error("This rule set doesn't say what it writes to yet. Pick the object "
                     "in **Writes to** — otherwise the rule is saved but won't appear "
                     "under any object on Update a rule, Requirements or lineage.")
        else:
            started = time.time()
            with st.spinner("Reading the requirement…"):
                result = compile_requirement(
                    requirement, rule_set_id, rule_number,
                    source_objects,
                    target_object if target_object != "(not set)" else "",
                    model=model, use_llm=use_llm,
                )
            result["elapsed"] = round(time.time() - started, 1)
            st.session_state["compiled"] = result

    compiled = st.session_state.get("compiled")
    if compiled:
        st.markdown("### Check this reads the way you meant it")
        st.markdown(compiled["preview"])
        st.caption(f"Prepared in {compiled.get('elapsed', 0)}s")

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

        render_discovered_objects(requirement, "wr_disc", module_id=_write_module_id)

        save_col, discard_col = st.columns(2)
        with save_col:
            disabled = bool(compiled["errors"])
            if disabled and st.button(
                    "Save as written", key="write_as_written",
                    help="The app could not turn this into conditions and actions. Keep it "
                         "word for word as the rule: developers build from the text, and "
                         "Verify against Snowflake reads the code against it."):
                try:
                    _aw_prop = _aw.build_proposal(requirement)
                    for _k in ("rule_set_id", "rule_number"):
                        _aw_prop[_k] = compiled["proposal"].get(_k)
                    new_id = save_rule(_aw_prop, performed_by=current_viewer())
                    add_rule_notes(new_id, _aw.notes_for(requirement, compiled["proposal"],
                                                         "WRITE_RULE"),
                                   performed_by=current_viewer())
                    set_rule_set_endpoints(
                        compiled["proposal"].get("rule_set_id"),
                        source_technical_objects=source_objects,
                        target_technical_object=target_object if target_object != "(not set)" else "",
                        performed_by=current_viewer(),
                    )
                    st.session_state.pop("compiled", None)
                    st.success(f"Saved as written as RULE_ID {new_id}, status DRAFT.")
                except Exception as exc:
                    st.error(f"Could not save: {exc}")
            if st.button("Save rule", type="primary", disabled=disabled):
                try:
                    new_id = save_rule(compiled["proposal"], performed_by=current_viewer())
                    _save_notes_from_text(new_id, requirement, "WRITE_RULE")
                    # Record every source and the target on the rule set, which is
                    # where lineage is read from. set_rule_set_endpoints replaces the
                    # source list rather than only filling blanks, so deselecting a
                    # source here actually removes its edge.
                    _ep = set_rule_set_endpoints(
                        compiled["proposal"].get("rule_set_id"),
                        source_technical_objects=source_objects,
                        target_technical_object=target_object if target_object != "(not set)" else "",
                        performed_by=current_viewer(),
                    )
                    st.success(f"Saved as RULE_ID {new_id}, status DRAFT.")
                    if _ep["added"] or _ep["removed"]:
                        _bits = []
                        if _ep["added"]:
                            _bits.append("added " + ", ".join(
                                s.rsplit(".", 1)[-1] for s in _ep["added"]))
                        if _ep["removed"]:
                            _bits.append("removed " + ", ".join(
                                s.rsplit(".", 1)[-1] for s in _ep["removed"]))
                        st.info("Rule set lineage updated: " + "; ".join(_bits) + ".")
                    if _ep["unresolved"]:
                        st.warning("Not registered as business objects, so not linked: "
                                    + ", ".join(_ep["unresolved"]))
                    saved_proposal = compiled["proposal"]
                    st.session_state.pop("compiled", None)
                    # Hand off to the test suggestion panel, which draws below on
                    # this and every following rerun until saved or skipped.
                    st.session_state["write_test_ctx"] = {
                        "rule_id": int(new_id),
                        "rule_number": saved_proposal.get("rule_number"),
                        "proposal": saved_proposal,
                        "previous": None,
                        "target_object": target_object if target_object != "(not set)" else None,
                        "source_action": "ON_CREATE",
                        "cases": None,
                    }
                except Exception as exc:
                    st.error(f"Nothing was saved. {exc}")
        with discard_col:
            if st.button("Discard"):
                st.session_state.pop("compiled", None)
                st.session_state.pop("write_test_ctx", None)
                st.rerun()

    render_test_suggestions("write_test_ctx")

# ==================================================================
# Update a rule
# ==================================================================
elif page == "Update a rule":
    st.subheader("Update an existing rule")

    catalog = scope_to_team(get_rule_catalog())
    if catalog.empty:
        st.warning("No rules found in VW_RULE_SUMMARY.")
        error = get_last_error()
        if error:
            st.error(f"The read failed, that's why it looked empty: {error}")
        st.stop()

    modules = ["(all modules)"] + sorted(catalog["MODULE_NAME"].dropna().astype(str).unique().tolist())
    chosen_module = st.selectbox("Module", modules, key="update_module_filter")
    scoped = catalog if chosen_module == "(all modules)" else \
        catalog[catalog["MODULE_NAME"].astype(str) == chosen_module]

    left, right = st.columns(2)
    with left:
        targets = sorted(scoped["TARGET_BUSINESS_OBJECT"].dropna().astype(str).unique().tolist())
        if not targets:
            st.warning("No rules in this module yet, so there is nothing to update. "
                       "Use **Write a rule** to add the first one.")
            st.stop()
        chosen_target = st.selectbox(
            "Object", targets, key="update_target",
            help="Only objects that at least one live rule writes to. You can only "
                 "update a rule that exists.")

    # The list above comes from rules, not from objects, so an object in the module
    # with no live rule simply does not appear - and Location was showing 6 of its 10
    # objects with no hint why. Everything unlisted is named, rather than guessing
    # which are "just sources": Plant Core Memories is read by a hop yet nothing
    # builds it, so any source-only test would have hidden exactly the gap worth seeing.
    if chosen_module != "(all modules)":
        _mod_objs = scope_to_team(list_business_objects())
        if _mod_objs is not None and not _mod_objs.empty:
            _mod_objs = _mod_objs[_mod_objs["MODULE_NAME"].astype(str) == chosen_module]
            _missing = sorted(
                str(n) for n in _mod_objs["OBJECT_NAME"].dropna().astype(str)
                if n not in targets)
            if _missing:
                st.caption(
                    f":grey[Not listed — {len(_missing)} object(s) in {chosen_module} that "
                    "no live rule writes to: "
                    + ", ".join(f"`{m}`" for m in _missing)
                    + ". Source-system objects are expected here. Any other is a hop with "
                      "no rules written yet — add them on **Write a rule**.]")

    view = scoped[scoped["TARGET_BUSINESS_OBJECT"].astype(str) == chosen_target]
    view = view.sort_values(["PRIORITY"], na_position="last")
    if view.empty:
        st.info("No rules write to that object.")
        st.stop()

    with right:
        options = {f"{r['RULE_NUMBER']} — {r['RULE_NAME']}": int(r["RULE_ID"])
                   for _, r in view.iterrows()}
        chosen_label = st.selectbox("Rule", list(options), key="update_rule_pick")
        rule_id = options[chosen_label]

    bundles = load_bundles([rule_id])
    if not bundles:
        st.warning("Could not load that rule.")
        st.stop()
    bundle = bundles[0]

    st.markdown("#### Current rule")
    _view_tab, _edit_tab = st.tabs(["View", "Edit"])
    with _view_tab:
        st.markdown(render_rule(bundle))
    with _edit_tab:
        _render_rule_editor(bundle, rule_id)
    render_rule_notes(rule_id, "updnote")

    # Source and target live on the rule set, not the rule, so this edits the hop
    # every rule in the set belongs to. The alternative - not offering it here - is
    # what left the Item lineage wrong: the only way to fix a hop was raw SQL.
    _rs_id = bundle["rule"].get("RULE_SET_ID")
    _ep_now = get_rule_set_endpoints(int(_rs_id)) if _rs_id is not None \
        else {"sources": [], "target": ""}
    with st.expander("Reads from / writes to — the rule set's lineage", expanded=False):
        _bo = scope_to_team(list_business_objects())
        _names = (sorted(_bo["TECHNICAL_OBJECT_NAME"].dropna().astype(str).unique().tolist())
                  if _bo is not None and not _bo.empty else [])
        if not _names:
            st.info("No business objects registered, so there is nothing to link.")
            _upd_sources, _upd_target = [], ""
        else:
            st.caption(f"These belong to rule set **{_rs_id}**, so a change here applies "
                       "to every rule in it and redraws the lineage diagram.")
            _upd_sources = st.multiselect(
                "Reads from", _names,
                default=[s for s in _ep_now["sources"] if s in _names],
                key=f"upd_sources_{rule_id}",
                help="Every object this rule set reads. The first is the primary "
                     "source. Removing one deletes its arrow on the lineage diagram.",
            )
            _target_choices = ["(unchanged)"] + _names
            _upd_target_pick = st.selectbox(
                "Writes to", _target_choices,
                index=(_target_choices.index(_ep_now["target"])
                       if _ep_now["target"] in _target_choices else 0),
                key=f"upd_target_{rule_id}",
            )
            _upd_target = "" if _upd_target_pick == "(unchanged)" else _upd_target_pick
            if _upd_sources and len(_upd_sources) > 1:
                st.caption(f"Primary source: **{_upd_sources[0].rsplit('.', 1)[-1]}**")
            _removing = [s for s in _ep_now["sources"] if s not in _upd_sources]
            if _removing and _upd_sources:
                st.warning("Will remove " + ", ".join(s.rsplit('.', 1)[-1] for s in _removing)
                           + " from this hop's sources.")
            elif _removing and not _upd_sources:
                st.info("An empty list leaves the existing sources alone — clearing every "
                        "source would strip the rule set off the lineage graph, so it is "
                        "treated as 'no change' rather than 'delete all'.")
            if st.button("Apply lineage change", key=f"upd_apply_ep_{rule_id}",
                         disabled=not _upd_sources and not _upd_target):
                try:
                    _res = set_rule_set_endpoints(
                        int(_rs_id), source_technical_objects=_upd_sources,
                        target_technical_object=_upd_target,
                        performed_by=current_viewer())
                    _msg = []
                    if _res["added"]:
                        _msg.append("added " + ", ".join(
                            s.rsplit(".", 1)[-1] for s in _res["added"]))
                    if _res["removed"]:
                        _msg.append("removed " + ", ".join(
                            s.rsplit(".", 1)[-1] for s in _res["removed"]))
                    if _res["target_changed"]:
                        _msg.append(f"target set to {_upd_target.rsplit('.', 1)[-1]}")
                    st.success("Lineage updated: " + ("; ".join(_msg) if _msg
                                                       else "nothing needed changing") + ".")
                    if _res["unresolved"]:
                        st.warning("Not registered as business objects: "
                                   + ", ".join(_res["unresolved"]))
                    st.rerun()
                except Exception as exc:
                    st.error(f"Nothing was changed. {exc}")

    update_text = st.text_area(
        "Describe the update",
        height=120,
        key="update_text",
        placeholder="Change the priority to 10 and also exclude records where the deletion flag is Y.",
    )

    with st.expander("Options"):
        model = st.selectbox("Model", MODEL_CHOICES, key="update_model")
        use_llm = st.checkbox("Use the model to structure the update", value=True, key="update_use_llm",
                               help="Turn off to keep the rule unchanged except for fields the sentence "
                                    "parser can confidently identify.")

    if st.button("Prepare the update", type="primary"):
        if not update_text.strip():
            st.error("Describe the update first.")
        else:
            started = time.time()
            with st.spinner("Reading the update…"):
                result = compile_update(
                    update_text, bundle,
                    # Every source, so a column that exists on the second or third
                    # input is not reported as unknown.
                    _ep_now["sources"] or bundle["rule"].get("SOURCE_TECHNICAL_OBJECT", ""),
                    bundle["rule"].get("TARGET_TECHNICAL_OBJECT", ""),
                    model=model, use_llm=use_llm,
                )
            result["elapsed"] = round(time.time() - started, 1)
            result["rule_id"] = rule_id
            st.session_state["update_compiled"] = result

    compiled = st.session_state.get("update_compiled")
    if compiled and compiled.get("rule_id") == rule_id:
        st.markdown("### Check this reads the way you meant it")
        st.markdown(compiled["preview"])
        st.caption("From your edits in the **Edit** tab." if compiled.get("source") == "direct"
                   else f"Prepared in {compiled.get('elapsed', 0)}s")

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

        if compiled.get("source") != "direct":
            render_discovered_objects(
                update_text, "upd_disc",
                module_id=bundle["rule"].get("MODULE_ID"),
            )

        if _mh is not None:
            try:
                _with_dev = _mh.edit_notice(bundle["rule"].get("MODULE_ID"))
            except Exception:
                _with_dev = None
            if _with_dev:
                st.info(_with_dev)

        confirm_col, discard_col = st.columns(2)
        with confirm_col:
            disabled = bool(compiled["errors"])
            _direct = compiled.get("source") == "direct"
            if disabled and not _direct and st.button(
                    "Save as written", key="update_as_written",
                    help="Replace this rule with your text, word for word, as an as-written "
                         "rule. Use it when the logic is in plain English the app cannot "
                         "model. The previous version stays in the history."):
                try:
                    _cur = compiled.get("current") or {}
                    _aw_prop = _aw.build_proposal(update_text,
                                                  rule_name=_cur.get("rule_name"),
                                                  priority=_cur.get("priority"))
                    for _k in ("rule_set_id", "rule_number"):
                        if _cur.get(_k) is not None:
                            _aw_prop[_k] = _cur.get(_k)
                    update_rule(rule_id, _aw_prop, previous=_cur or None,
                                performed_by=current_viewer())
                    add_rule_notes(rule_id, _aw.notes_for(update_text, compiled["proposal"],
                                                          "UPDATE_RULE"),
                                   performed_by=current_viewer())
                    st.session_state.pop("update_compiled", None)
                    st.success(f"RULE_ID {rule_id} saved as written.")
                except Exception as exc:
                    st.error(f"Nothing was updated. {exc}")
            if st.button("Confirm update", type="primary", disabled=disabled):
                try:
                    update_rule(rule_id, compiled["proposal"], previous=compiled["current"],
                                performed_by=current_viewer())
                    if not _direct:
                        _save_notes_from_text(rule_id, update_text, "UPDATE_RULE")
                    st.success(f"RULE_ID {rule_id} updated.")
                    # Stashed before the rerun so the suggestion panel, which
                    # compares before against after, draws on the way back.
                    chosen_row = view[view["RULE_ID"].astype(int) == int(rule_id)]
                    technical_target = (str(chosen_row.iloc[0]["TARGET_TECHNICAL_OBJECT"])
                                        if not chosen_row.empty
                                        and chosen_row.iloc[0]["TARGET_TECHNICAL_OBJECT"] else None)
                    st.session_state["update_test_ctx"] = {
                        "rule_id": int(rule_id),
                        "rule_number": compiled["proposal"].get("rule_number")
                                       or chosen_label.split(" — ")[0],
                        "proposal": compiled["proposal"],
                        "previous": compiled["current"],
                        "target_object": technical_target,
                        "source_action": "ON_UPDATE",
                        "cases": None,
                    }
                    st.session_state.pop("update_compiled", None)
                    st.rerun()
                except Exception as exc:
                    st.error(f"Nothing was updated. {exc}")
        with discard_col:
            if st.button("Discard", key="update_discard"):
                st.session_state.pop("update_compiled", None)
                st.session_state.pop("update_test_ctx", None)
                st.rerun()

    render_test_suggestions("update_test_ctx")

# ==================================================================
# View rule audit trail
# ==================================================================
elif page == "View rule audit trail":
    st.subheader("Rule audit trail")

    tab_rules, tab_all = st.tabs(["Rule changes", "All activity"])

    with tab_all:
        st.caption("Every mutation across the repository: rule saves, review comments, module changes.")
        type_filter = st.selectbox(
            "Filter by type",
            ["(all)", "RULE", "REVIEW_COMMENT", "TEST_CASE", "OBJECT_COLUMN",
             "TEAM", "MODULE", "BUSINESS_OBJECT", "RULE_SET"],
            key="audit_all_type_filter",
        )
        audit_df = get_audit_trail(entity_type=None if type_filter == "(all)" else type_filter)
        if audit_df.empty:
            st.info("No activity recorded yet.")
        else:
            display_all = audit_df.copy()
            display_all["PERFORMED_TS"] = pd.to_datetime(display_all["PERFORMED_TS"]).dt.strftime("%Y-%m-%d %H:%M:%S")
            display_all = display_all.rename(columns={
                "PERFORMED_TS": "When", "ENTITY_TYPE": "Type", "ENTITY_ID": "ID",
                "ACTION": "Action", "DETAIL": "Detail", "PERFORMED_BY": "Who",
            })
            st.dataframe(display_all[["When", "Type", "ID", "Action", "Detail", "Who"]],
                         use_container_width=True, hide_index=True)
            st.caption(f"{len(display_all)} event(s).")
            csv_all = display_all[["When", "Type", "ID", "Action", "Detail", "Who"]].to_csv(index=False).encode("utf-8")
            st.download_button("Download as CSV", csv_all, file_name="audit_trail_all_activity.csv", mime="text/csv")

    with tab_rules:
        st.caption("Every change made to a rule, with who changed it and when.")

        catalog = scope_to_team(get_rule_catalog())
        if catalog.empty:
            st.warning("No rules found in VW_RULE_SUMMARY.")
            error = get_last_error()
            if error:
                st.error(f"The read failed, that's why it looked empty: {error}")
            st.stop()

        modules = ["(all modules)"] + sorted(catalog["MODULE_NAME"].dropna().astype(str).unique().tolist())
        chosen_module = st.selectbox("Module", modules, key="audit_module_filter")
        scoped = catalog if chosen_module == "(all modules)" else \
            catalog[catalog["MODULE_NAME"].astype(str) == chosen_module]

        left, right = st.columns(2)
        with left:
            targets = ["(all objects)"] + sorted(scoped["TARGET_BUSINESS_OBJECT"].dropna().astype(str).unique().tolist())
            chosen_target = st.selectbox("Object", targets, key="audit_target")

        view = scoped if chosen_target == "(all objects)" else \
            scoped[scoped["TARGET_BUSINESS_OBJECT"].astype(str) == chosen_target]
        if view.empty:
            st.info("No rules for that object.")
            st.stop()

        with right:
            options = {"(all rules for this object)": None}
            for _, r in view.sort_values("RULE_NUMBER").iterrows():
                options[f"{r['RULE_NUMBER']} — {r['RULE_NAME']}"] = int(r["RULE_ID"])
            chosen_label = st.selectbox("Rule", list(options), key="audit_rule_pick")
            rule_id = options[chosen_label]

        history = get_rule_history(rule_id if rule_id is not None else view["RULE_ID"].astype(int).tolist())

        if history.empty:
            st.info("No changes recorded yet for this selection.")
        else:
            rule_lookup = catalog.drop_duplicates("RULE_ID").set_index("RULE_ID")

            def rule_label(rid):
                if rid in rule_lookup.index:
                    row = rule_lookup.loc[rid]
                    return f"{row['RULE_NUMBER']} — {row['RULE_NAME']}"
                return str(rid)

            display = history.copy()
            display["CHANGED_TS"] = pd.to_datetime(display["CHANGED_TS"]).dt.strftime("%Y-%m-%d %H:%M:%S")
            display["RULE"] = display["RULE_ID"].map(rule_label)
            display = display.rename(columns={
                "CHANGED_TS": "When", "RULE": "Rule", "CHANGE_TYPE": "Field changed",
                "OLD_VALUE": "Before", "NEW_VALUE": "After", "CHANGED_BY": "Changed by",
            })
            columns = ["When", "Rule", "Field changed", "Before", "After", "Changed by"]
            st.dataframe(display[columns], use_container_width=True, hide_index=True)
            st.caption(f"{len(display)} change(s) recorded.")

            csv = display[columns].to_csv(index=False).encode("utf-8")
            st.download_button("Download as CSV", csv, file_name="rule_audit_trail.csv", mime="text/csv")

# ==================================================================
# Rollback a rule
# ==================================================================
elif page == "Rollback a rule":
    st.subheader("Rollback a rule to a previous version")
    st.caption(
        "Pick a rule, choose a version from its audit trail, confirm the preview, "
        "and the rule is restored to exactly how it read at that point."
    )

    catalog = scope_to_team(get_rule_catalog())
    if catalog.empty:
        st.warning("No rules found in VW_RULE_SUMMARY.")
        error = get_last_error()
        if error:
            st.error(f"The read failed, that's why it looked empty: {error}")
        st.stop()

    modules = ["All"] + sorted(catalog["MODULE_NAME"].dropna().astype(str).unique().tolist())
    chosen_module = st.selectbox("Module", modules, key="rollback_module_filter")
    scoped = catalog if chosen_module == "All" else catalog[catalog["MODULE_NAME"].astype(str) == chosen_module]

    left, right = st.columns(2)
    with left:
        targets = sorted(scoped["TARGET_BUSINESS_OBJECT"].dropna().astype(str).unique().tolist())
        if not targets:
            st.warning("No target objects found.")
            st.stop()
        chosen_target = st.selectbox("Object", targets, key="rollback_target")

    view = scoped[scoped["TARGET_BUSINESS_OBJECT"].astype(str) == chosen_target]
    view = view.sort_values(["PRIORITY"], na_position="last")
    if view.empty:
        st.info("No rules write to that object.")
        st.stop()

    with right:
        options = {f"{r['RULE_NUMBER']} — {r['RULE_NAME']}": int(r["RULE_ID"])
                   for _, r in view.iterrows()}
        chosen_label = st.selectbox("Rule", list(options), key="rollback_rule_pick")
        rule_id = options[chosen_label]

    bundles = load_bundles([rule_id])
    if not bundles:
        st.warning("Could not load that rule.")
        st.stop()
    bundle = bundles[0]

    st.markdown("#### Current version")
    st.markdown(render_rule(bundle))

    events = rollback_events(rule_id)
    if not events:
        st.info("No earlier version recorded for this rule yet. Make an update first, then a version to roll back to will appear here.")
        st.stop()

    version_options = {}
    for event in events:
        label = f"{event['ts']} — {event['changed_by']} — changed {', '.join(event['fields'])}"
        version_options[label] = event
    chosen_version_label = st.selectbox("Restore to this version", list(version_options), key="rollback_version_pick")
    chosen_event = version_options[chosen_version_label]

    # Show a summary of what changed in this version so the user can decide
    # whether this is the right one to roll back to.
    changes = chosen_event.get("changes") or []
    if changes:
        with st.expander("Summary of changes in this version", expanded=True):
            for c in changes:
                field = c["field"]
                if field in ("CREATED", "ROLLBACK"):
                    st.caption(f"**{field}**")
                    continue
                old_display = c["old"] or "_(empty)_"
                new_display = c["new"] or "_(empty)_"
                # Truncate long values (e.g. JSON sections) for readability
                if len(old_display) > 120:
                    old_display = old_display[:120] + "…"
                if len(new_display) > 120:
                    new_display = new_display[:120] + "…"
                st.markdown(f"**{field}**:  `{old_display}` → `{new_display}`")

    if st.button("Preview this version", type="primary"):
        result = compile_rollback(
            bundle, chosen_event["change_id"],
            bundle["rule"].get("SOURCE_TECHNICAL_OBJECT", ""),
            bundle["rule"].get("TARGET_TECHNICAL_OBJECT", ""),
        )
        result["rule_id"] = rule_id
        result["target_event"] = chosen_event
        st.session_state["rollback_compiled"] = result

    compiled = st.session_state.get("rollback_compiled")
    if compiled and compiled.get("rule_id") == rule_id:
        st.markdown("### This is the version that will be restored")
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

        confirm_col, discard_col = st.columns(2)
        with confirm_col:
            disabled = bool(compiled["errors"])
            if st.button("Confirm rollback", type="primary", disabled=disabled):
                try:
                    event = compiled["target_event"]
                    update_rule(rule_id, compiled["proposal"], previous=compiled["current"],
                                performed_by=current_viewer())
                    log_rollback_marker(rule_id, event["change_id"], event["ts"])
                    st.success(f"RULE_ID {rule_id} restored to the version from {event['ts']}.")
                    st.session_state.pop("rollback_compiled", None)
                    st.rerun()
                except Exception as exc:
                    st.error(f"Nothing was restored. {exc}")
        with discard_col:
            if st.button("Discard", key="rollback_discard"):
                st.session_state.pop("rollback_compiled", None)
                st.rerun()

# ==================================================================
# Retire a rule
# ==================================================================
elif page == "Retire a rule":
    st.subheader("Retire a rule")
    st.caption(
        "Retiring is how a rule is removed here. It disappears from the catalog, the "
        "FDD and the Ask context, but its definition, history and test cases are kept "
        "so the decision stays auditable and can be undone."
    )

    tab_retire, tab_bulk, tab_restore = st.tabs(
        ["Retire a rule", "Bulk retire", "Retired rules"])

    with tab_retire:
        catalog = scope_to_team(get_rule_catalog())
        if catalog.empty:
            st.warning("No rules found in VW_RULE_SUMMARY.")
            error = get_last_error()
            if error:
                st.error(f"The read failed, that's why it looked empty: {error}")
        else:
            modules = ["All"] + sorted(catalog["MODULE_NAME"].dropna().astype(str).unique().tolist())
            chosen_module = st.selectbox("Module", modules, key="retire_module_filter")
            scoped = (catalog if chosen_module == "All"
                      else catalog[catalog["MODULE_NAME"].astype(str) == chosen_module])

            left, right = st.columns(2)
            with left:
                targets = sorted(scoped["TARGET_BUSINESS_OBJECT"].dropna().astype(str).unique().tolist())
                if not targets:
                    st.warning("No target objects found.")
                    st.stop()
                chosen_target = st.selectbox("Object", targets, key="retire_target")

            view = scoped[scoped["TARGET_BUSINESS_OBJECT"].astype(str) == chosen_target]
            view = view.sort_values(["PRIORITY"], na_position="last")
            if view.empty:
                st.info("No rules write to that object.")
                st.stop()

            with right:
                options = {f"{r['RULE_NUMBER']} — {r['RULE_NAME']}": int(r["RULE_ID"])
                           for _, r in view.iterrows()}
                chosen_label = st.selectbox("Rule", list(options), key="retire_rule_pick")
                rule_id = options[chosen_label]

            bundles = load_bundles([rule_id])
            if not bundles:
                st.warning("Could not load that rule.")
                st.stop()
            bundle = bundles[0]

            st.markdown("#### The rule you are about to retire")
            st.markdown(render_rule(bundle))
            render_rule_notes(rule_id, "retnote", editable=False)

            # Retiring a rule that another rule's priority ordering depends on is
            # worth a warning: the remaining rules keep their numbers and a gap
            # appears in the sequence.
            siblings = view[view["RULE_ID"].astype(int) != rule_id]
            if not siblings.empty:
                st.info(
                    f"{len(siblings)} other rule(s) write to {chosen_target}. Their priorities "
                    "are unchanged, so retiring this one leaves a gap in the sequence rather "
                    "than renumbering them."
                )

            cases = get_test_cases([rule_id])
            if not cases.empty:
                st.warning(
                    f"This rule has {len(cases)} test case(s). They are kept, but they will "
                    "no longer appear against an active rule."
                )

            reason = st.text_input(
                "Why is it being retired?",
                key="retire_reason",
                placeholder="Superseded by rule 3F, agreed with the PO on 2 Sep.",
            )
            confirm = st.checkbox(
                f"I want to retire rule {chosen_label} (RULE_ID {rule_id})",
                key="retire_confirm",
            )
            if st.button("Retire this rule", type="primary", disabled=not confirm):
                if not (reason or "").strip():
                    st.error("Give a reason first - it is written to the audit trail.")
                else:
                    try:
                        retire_rule(rule_id, reason=reason, performed_by=current_viewer())
                        st.success(
                            f"RULE_ID {rule_id} retired. It is out of the catalog and the FDD, "
                            "and can be restored from the Retired rules tab."
                        )
                        st.session_state.pop("retire_confirm", None)
                        st.session_state.pop("retire_reason", None)
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Nothing was retired. {exc}")

    with tab_bulk:
        bulk_catalog = scope_to_team(get_rule_catalog())
        if bulk_catalog.empty:
            st.warning("No rules found.")
        else:
            bulk_modules = sorted(
                bulk_catalog["MODULE_NAME"].dropna().astype(str).unique().tolist())
            if not bulk_modules:
                st.info("No modules found.")
            else:
                bulk_module = st.selectbox("Module", bulk_modules, key="bulk_retire_module")
                bulk_scoped = bulk_catalog[
                    bulk_catalog["MODULE_NAME"].astype(str) == bulk_module]

                bulk_objects = ["(all objects)"] + sorted(
                    bulk_scoped["TARGET_BUSINESS_OBJECT"].dropna().astype(str).unique().tolist())
                bulk_object = st.selectbox("Object", bulk_objects, key="bulk_retire_object")
                if bulk_object != "(all objects)":
                    bulk_scoped = bulk_scoped[
                        bulk_scoped["TARGET_BUSINESS_OBJECT"].astype(str) == bulk_object]

                bulk_scoped = bulk_scoped.sort_values(
                    ["TARGET_BUSINESS_OBJECT", "PRIORITY"], na_position="last")

                if bulk_scoped.empty:
                    st.info("No active rules match this selection.")
                else:
                    st.markdown(f"**{len(bulk_scoped)} rule(s)** in `{bulk_module}`"
                                + (f" / `{bulk_object}`" if bulk_object != "(all objects)" else ""))

                    # Select-all toggle — explicitly sets each checkbox key on change
                    select_all = st.checkbox("Select all", value=False, key="bulk_retire_all")
                    _retire_ids_in_scope = [int(r["RULE_ID"]) for _, r in bulk_scoped.iterrows()]
                    if select_all:
                        for _rid in _retire_ids_in_scope:
                            st.session_state[f"bulk_retire_{_rid}"] = True

                    selected_ids = []
                    for _, row in bulk_scoped.iterrows():
                        rid = int(row["RULE_ID"])
                        label = (f"{row['RULE_NUMBER']} — {row['RULE_NAME']}  "
                                 f"({row['TARGET_BUSINESS_OBJECT']})")
                        if st.checkbox(label, key=f"bulk_retire_{rid}"):
                            selected_ids.append(rid)

                    if selected_ids:
                        st.caption(f"{len(selected_ids)} of {len(bulk_scoped)} selected.")

                    bulk_reason = st.text_input(
                        "Reason for retiring these rules",
                        key="bulk_retire_reason",
                        placeholder="Module replaced by new FDD, agreed on 15 Sep.",
                    )

                    if st.button("Retire selected rules", type="primary",
                                 disabled=not selected_ids):
                        if not (bulk_reason or "").strip():
                            st.error("Give a reason — it is written to the audit trail "
                                     "for every rule.")
                        else:
                            retired_count = 0
                            failed = []
                            bar = st.progress(0.0)
                            for i, rid in enumerate(selected_ids):
                                bar.progress((i + 1) / len(selected_ids))
                                try:
                                    retire_rule(rid, reason=bulk_reason.strip(),
                                                performed_by=current_viewer())
                                    retired_count += 1
                                except Exception as exc:
                                    failed.append((rid, str(exc)))
                            bar.empty()

                            msg_parts = []
                            if retired_count:
                                msg_parts.append(f"{retired_count} rule(s) retired.")
                            if failed:
                                msg_parts.append(
                                    f"{len(failed)} failed: "
                                    + "; ".join(f"ID {r}: {e}" for r, e in failed))
                            _flash(
                                " ".join(msg_parts) or "Nothing changed.",
                                "success" if not failed else "warning",
                            )
                            _clear_bulk_state("bulk_retire_")
                            st.rerun()

    with tab_restore:
        retired = list_retired_rules()
        if retired.empty:
            st.info("No retired rules. Anything retired here can be brought back from this tab.")
        else:
            restore_single, restore_bulk = st.tabs(["Restore one", "Bulk restore"])

            with restore_single:
                options = {
                    f"{r['RULE_NUMBER']} — {r['RULE_NAME']} (RULE_ID {int(r['RULE_ID'])})": int(r["RULE_ID"])
                    for _, r in retired.iterrows()
                }
                chosen_label = st.selectbox("Rule to restore", list(options), key="restore_rule_pick")
                restore_id = options[chosen_label]
                target_status = st.selectbox(
                    "Bring it back as", ["ACTIVE", "DRAFT"], key="restore_status",
                    help="DRAFT if it still needs review before it counts as documented.",
                )
                if st.button("Restore this rule", key="restore_button"):
                    try:
                        restore_rule(restore_id, status=target_status, performed_by=current_viewer())
                        st.success(f"RULE_ID {restore_id} restored as {target_status}.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Nothing was restored. {exc}")

            with restore_bulk:
                restore_modules = ["(all modules)"] + sorted(
                    retired["MODULE_NAME"].dropna().astype(str).unique().tolist())
                restore_module = st.selectbox(
                    "Module", restore_modules, key="bulk_restore_module")
                filtered = (retired if restore_module == "(all modules)"
                            else retired[retired["MODULE_NAME"].astype(str) == restore_module])

                if filtered.empty:
                    st.info("No retired rules match this module.")
                else:
                    st.dataframe(filtered, use_container_width=True, hide_index=True)

                    restore_all = st.checkbox("Select all", value=False, key="bulk_restore_all")
                    _restore_ids_in_scope = [int(r["RULE_ID"]) for _, r in filtered.iterrows()]
                    if restore_all:
                        for _rid in _restore_ids_in_scope:
                            st.session_state[f"bulk_restore_{_rid}"] = True

                    restore_ids = []
                    for _, row in filtered.iterrows():
                        rid = int(row["RULE_ID"])
                        label = f"{row['RULE_NUMBER']} — {row['RULE_NAME']} (ID {rid})"
                        if st.checkbox(label, key=f"bulk_restore_{rid}"):
                            restore_ids.append(rid)

                    if restore_ids:
                        st.caption(f"{len(restore_ids)} of {len(filtered)} selected.")

                    bulk_status = st.selectbox(
                        "Bring them back as", ["ACTIVE", "DRAFT"],
                        key="bulk_restore_status",
                        help="DRAFT if they still need review.",
                    )

                    if st.button("Restore selected rules", type="primary",
                                 disabled=not restore_ids, key="bulk_restore_btn"):
                        restored_count = 0
                        failed = []
                        bar = st.progress(0.0)
                        for i, rid in enumerate(restore_ids):
                            bar.progress((i + 1) / len(restore_ids))
                            try:
                                restore_rule(rid, status=bulk_status,
                                             performed_by=current_viewer())
                                restored_count += 1
                            except Exception as exc:
                                failed.append((rid, str(exc)))
                        bar.empty()

                        msg_parts = []
                        if restored_count:
                            msg_parts.append(
                                f"{restored_count} rule(s) restored as {bulk_status}.")
                        if failed:
                            msg_parts.append(
                                f"{len(failed)} failed: "
                                + "; ".join(f"ID {r}: {e}" for r, e in failed))
                        _flash(
                            " ".join(msg_parts) or "Nothing changed.",
                            "success" if not failed else "warning",
                        )
                        _clear_bulk_state("bulk_restore_")
                        st.rerun()

# ==================================================================
# Manage modules
# ==================================================================
elif page == "Manage modules":
    st.subheader("Manage teams & modules")
    st.caption(
        "A team owns modules; a module groups the objects that belong to one business "
        "area (e.g. ITEM: its core, current, memories, PIT and root tables and views) so "
        "they can be filtered together everywhere else in this app."
    )

    # ---- Teams -----------------------------------------------------
    teams_df = list_teams()
    st.markdown("#### Teams")
    if teams_df.empty:
        st.info("No teams yet. Create one below.")
    else:
        st.dataframe(
            teams_df.rename(columns={
                "TEAM_NAME": "Name", "TEAM_CODE": "Code", "DESCRIPTION": "Description",
                "ACTIVE_IND": "Active", "MODULE_COUNT": "Modules",
            })[["Name", "Code", "Description", "Active", "Modules"]],
            use_container_width=True, hide_index=True,
        )

    team_create_col, team_rename_col = st.columns(2)

    with team_create_col:
        st.markdown("#### Create a team")
        with st.form("create_team_form", clear_on_submit=True):
            new_team_name = st.text_input("Team name", placeholder="Materials Planning")
            new_team_code = st.text_input("Team code", placeholder="MP")
            new_team_desc = st.text_area("Description", height=70)
            if st.form_submit_button("Create team", type="primary"):
                if not new_team_name.strip():
                    st.error("Give the team a name.")
                else:
                    try:
                        new_id = create_team(new_team_name, new_team_code, new_team_desc,
                                             created_by=current_viewer())
                        st.success(f"Team '{new_team_name.strip()}' created (id {new_id}). "
                                   "Assign modules to it below.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Nothing was created. {exc}")

    with team_rename_col:
        st.markdown("#### Rename a team")
        if teams_df.empty:
            st.info("No teams to rename.")
        else:
            team_opts = {f"{r['TEAM_NAME']} ({r['TEAM_CODE'] or 'no code'})": r
                         for _, r in teams_df.iterrows()}
            pick_team_label = st.selectbox("Team", list(team_opts), key="rename_team_pick")
            pick_team = team_opts[pick_team_label]
            with st.form("rename_team_form"):
                rn_name = st.text_input("Team name", value=str(pick_team["TEAM_NAME"] or ""))
                rn_code = st.text_input("Team code", value=str(pick_team["TEAM_CODE"] or ""))
                rn_desc = st.text_area("Description", value=str(pick_team["DESCRIPTION"] or ""),
                                       height=70)
                if st.form_submit_button("Save team", type="primary"):
                    try:
                        rename_team(int(pick_team["TEAM_ID"]), rn_name, rn_code, rn_desc,
                                    performed_by=current_viewer())
                        st.success("Team updated.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Nothing was changed. {exc}")

    st.divider()

    # ---- Column backfill -------------------------------------------
    # An account-wide action, so it lives here rather than on the Object columns
    # page: that page is about one object at a time, and a button that touches
    # every registered object has no business sitting next to a per-object one.
    st.markdown("#### Backfill object columns")
    st.caption(
        "Reads the real columns from Snowflake for every registered object that has "
        "no spec yet, and stores them as DISCOVERED. Additive — a column someone "
        "declared by hand keeps its description, origin and ordinal."
    )
    _bf_go, _bf_msg = st.columns([1.3, 3])
    with _bf_go:
        if st.button("Backfill every object", key="mm_sync_all", use_container_width=True):
            try:
                _bf = sync_all_object_columns(created_by=current_viewer())
                st.session_state["mm_sync_msg"] = (
                    f"Stored {_bf['columns_added']} column(s) across "
                    f"{_bf['objects_synced']} object(s). {_bf['objects_skipped']} skipped "
                    "(already stored, no three-part name, or not in Snowflake).")
                st.rerun()
            except Exception as exc:
                st.error(f"Backfill failed, nothing was stored. {exc}")
    with _bf_msg:
        _mm_msg = st.session_state.pop("mm_sync_msg", None)
        if _mm_msg:
            st.success(_mm_msg)

    st.divider()

    # ---- Approvers -------------------------------------------------
    # Who may accept a rule. An app-level control rather than a Snowflake grant,
    # because the app runs execute_as OWNER: every viewer is the same principal as
    # far as the database is concerned, so RBAC cannot separate them here.
    st.markdown("#### Who can accept rules")
    _approvers = list_approvers()
    if _approvers is None or _approvers.empty:
        st.warning(
            "**No approvers named, so anyone using this app can accept rules.** "
            "Add the product owner below to restrict it. Until then every acceptance is "
            "still recorded against whoever made it, so nothing is untraceable — it is "
            "just unrestricted."
        )
    else:
        st.dataframe(
            _approvers.assign(Scope=_approvers["TEAM_NAME"].fillna("Any team"))
            .rename(columns={"APPROVER_NAME": "Approver", "DESCRIPTION": "Note"})
            [["Approver", "Scope", "Note"]],
            use_container_width=True, hide_index=True,
        )
    st.caption(
        ":grey[This stops the wrong person clicking Accept; it is not a security "
        "boundary. The name is the signed-in Snowsight user, not a database role.]"
    )

    _ap_left, _ap_right = st.columns(2)
    with _ap_left:
        with st.form("add_approver_form", clear_on_submit=True):
            _ap_name = st.text_input(
                "Approver", placeholder="first.last@intel.com",
                help="Must match the Snowsight login exactly. Yours is shown below.")
            _ap_team_labels = ["Any team"] + ([] if teams_df.empty else
                                              [str(r["TEAM_NAME"]) for _, r in teams_df.iterrows()])
            _ap_team_by_label = {} if teams_df.empty else {
                str(r["TEAM_NAME"]): int(r["TEAM_ID"]) for _, r in teams_df.iterrows()}
            _ap_scope = st.selectbox("Can accept for", _ap_team_labels)
            _ap_note = st.text_input("Note (optional)", placeholder="Product owner, Item")
            st.caption(f"You are signed in as **{current_viewer()}**.")
            if st.form_submit_button("Add approver", type="primary"):
                if not _ap_name.strip():
                    st.error("Enter the approver's login.")
                else:
                    try:
                        add_approver(_ap_name.strip(),
                                     team_id=_ap_team_by_label.get(_ap_scope),
                                     description=_ap_note.strip(),
                                     performed_by=current_viewer())
                        st.success(f"{_ap_name.strip()} can now accept rules.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Nothing was added. {exc}")
    with _ap_right:
        if _approvers is not None and not _approvers.empty:
            _ap_pick = {f"{r['APPROVER_NAME']} — {r['TEAM_NAME'] or 'Any team'}":
                        int(r["APPROVER_ID"]) for _, r in _approvers.iterrows()}
            _ap_remove = st.selectbox("Remove an approver", list(_ap_pick),
                                      key="ap_remove_pick")
            st.caption(":grey[Removing the last one returns the app to "
                       "'anyone can accept'.]")
            if st.button("Remove", key="ap_remove_btn"):
                try:
                    remove_approver(_ap_pick[_ap_remove], performed_by=current_viewer())
                    st.success("Removed.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Nothing was removed. {exc}")

    st.divider()

    # ---- Team databases --------------------------------------------
    # Which databases a team documents objects in, and how each logical name maps
    # onto a database that exists in this account. Drives object resolution on the
    # Ask page and the database picker when registering an object.
    st.markdown("#### Databases")
    st.caption(
        "A team's objects usually span more than one database — a subject-area one "
        "and a master-data one. The logical name is what the FDD records; the "
        "physical name is what exists in this account."
    )

    td_df = list_team_databases()
    dbmap_df = list_database_map()

    # Reachability is checked per distinct physical database, not per row, so a
    # team with four mappings does not cost four probes.
    _physical_names = sorted(
        {str(p).upper() for p in dbmap_df["PHYSICAL_DATABASE"].dropna()} if not dbmap_df.empty else set()
    )
    _reach = {name: database_reachable(name) for name in _physical_names}

    db_left, db_right = st.columns(2)

    with db_left:
        st.markdown("**Per team**")
        if td_df.empty:
            st.info("No databases mapped yet.")
        else:
            shown = td_df.copy()
            shown["Reachable"] = shown["PHYSICAL_DATABASE"].apply(
                lambda p: "—" if pd.isna(p) else ("yes" if _reach.get(str(p).upper()) else "no")
            )
            st.dataframe(
                shown.rename(columns={
                    "TEAM_NAME": "Team", "LOGICAL_DATABASE": "Logical",
                    "PHYSICAL_DATABASE": "Physical", "IS_PRIMARY": "Primary",
                })[["Team", "Logical", "Physical", "Primary", "Reachable"]],
                use_container_width=True, hide_index=True,
            )

        if not teams_df.empty:
            with st.form("add_team_db_form", clear_on_submit=True):
                _td_team_opts = {f"{r['TEAM_NAME']}": int(r["TEAM_ID"])
                                 for _, r in teams_df.iterrows()}
                _td_team = st.selectbox("Team", list(_td_team_opts), key="td_team_pick")
                _td_db = st.text_input("Logical database", placeholder="SUPPLY_CHAIN")
                _td_primary = st.checkbox("Primary for this team")
                if st.form_submit_button("Attach database", type="primary"):
                    if not _td_db.strip():
                        st.error("Enter a database name.")
                    else:
                        try:
                            add_team_database(_td_team_opts[_td_team], _td_db,
                                              is_primary=_td_primary,
                                              performed_by=current_viewer())
                            st.success(f"`{_td_db.strip().upper()}` attached to {_td_team}.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Nothing was attached. {exc}")

        if not td_df.empty:
            _td_opts = {
                f"{r['TEAM_NAME']} — {r['LOGICAL_DATABASE']}"
                + ("  (primary)" if r["IS_PRIMARY"] else ""): r
                for _, r in td_df.iterrows()
            }
            _td_pick_label = st.selectbox("Change a mapping", list(_td_opts), key="td_edit_pick")
            _td_pick = _td_opts[_td_pick_label]
            act_primary, act_remove = st.columns(2)
            with act_primary:
                if st.button("Make primary", key="td_make_primary",
                             disabled=bool(_td_pick["IS_PRIMARY"])):
                    try:
                        set_team_database_primary(int(_td_pick["TEAM_DATABASE_ID"]),
                                                  int(_td_pick["TEAM_ID"]),
                                                  performed_by=current_viewer())
                        st.success("Primary updated.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Nothing was changed. {exc}")
            with act_remove:
                if st.button("Detach", key="td_remove"):
                    try:
                        remove_team_database(int(_td_pick["TEAM_DATABASE_ID"]),
                                             performed_by=current_viewer())
                        st.success("Database detached.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Nothing was changed. {exc}")

    with db_right:
        st.markdown("**Logical → physical**")
        st.caption(
            "Lets the same metadata resolve in more than one environment. An "
            "identity row (both names the same) leaves a name untouched."
        )
        if dbmap_df.empty:
            st.info("No mappings yet.")
        else:
            shown_map = dbmap_df.copy()
            shown_map["Reachable"] = shown_map["PHYSICAL_DATABASE"].apply(
                lambda p: "yes" if _reach.get(str(p).upper()) else "no"
            )
            st.dataframe(
                shown_map.rename(columns={
                    "LOGICAL_DATABASE": "Logical", "PHYSICAL_DATABASE": "Physical",
                    "ACTIVE_IND": "Active",
                })[["Logical", "Physical", "Active", "Reachable"]],
                use_container_width=True, hide_index=True,
            )
            _unreachable = [n for n, ok in _reach.items() if not ok]
            if _unreachable:
                st.warning(
                    "Not readable from this account: "
                    + ", ".join(f"`{n}`" for n in _unreachable)
                    + ". Objects mapped there will resolve but every query will fail "
                      "until the database exists and is granted."
                )

        with st.form("upsert_dbmap_form", clear_on_submit=True):
            _map_logical = st.text_input("Logical database", placeholder="SUPPLY_CHAIN")
            _map_physical = st.text_input("Physical database", placeholder="IF_SUPPLY_CHAIN")
            if st.form_submit_button("Save mapping", type="primary"):
                if not _map_logical.strip() or not _map_physical.strip():
                    st.error("Both names are required.")
                else:
                    try:
                        upsert_database_map(_map_logical, _map_physical,
                                            performed_by=current_viewer())
                        st.success(f"`{_map_logical.strip().upper()}` → "
                                   f"`{_map_physical.strip().upper()}`.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Nothing was saved. {exc}")

    st.divider()

    # ---- Module to team assignment ---------------------------------
    # Deliberately reads every module, not the team-scoped list: moving a module
    # out of the selected team would otherwise be impossible, because the module
    # would vanish from the picker the moment the team was selected.
    st.markdown("#### Move a module to a different team")
    all_modules_for_team = get_modules()
    if all_modules_for_team.empty:
        st.info("No modules to assign.")
    else:
        team_choices = {"(no team)": None}
        if not teams_df.empty:
            for _, t in teams_df.iterrows():
                team_choices[str(t["TEAM_NAME"])] = int(t["TEAM_ID"])

        mv_module_opts = {
            f"{r['MODULE_NAME']}  ·  currently {r['TEAM_NAME'] or 'no team'}": r
            for _, r in all_modules_for_team.iterrows()
        }
        mv_label = st.selectbox("Module", list(mv_module_opts), key="move_module_team_pick")
        mv_module = mv_module_opts[mv_label]
        current_team_label = next(
            (lbl for lbl, tid in team_choices.items()
             if tid == (None if pd.isna(mv_module["TEAM_ID"]) else int(mv_module["TEAM_ID"]))),
            "(no team)",
        )
        mv_team_label = st.selectbox(
            "Team", list(team_choices), key="move_module_team_target",
            index=list(team_choices).index(current_team_label),
        )
        if st.button("Move module", key="move_module_team_button"):
            try:
                reassign_module_team(int(mv_module["MODULE_ID"]), team_choices[mv_team_label],
                                     performed_by=current_viewer())
                st.success(f"{mv_module['MODULE_NAME']} moved to {mv_team_label}.")
                st.rerun()
            except Exception as exc:
                st.error(f"Nothing was moved. {exc}")

    st.divider()

    modules = scope_to_team(get_modules())
    st.markdown("#### Modules")
    if modules.empty:
        if TEAM_ID is not None:
            st.info(f"{TEAM_NAME} owns no modules yet. Assign one above, or create one below.")
        else:
            st.info("No modules yet. Create one below.")
    else:
        st.dataframe(
            modules.rename(columns={
                "MODULE_NAME": "Name", "MODULE_CODE": "Code", "TEAM_NAME": "Team",
                "DESCRIPTION": "Description",
                "ACTIVE_IND": "Active", "OBJECT_COUNT": "Objects", "RULE_SET_COUNT": "Rule sets",
            })[["Name", "Code", "Team", "Description", "Active", "Objects", "Rule sets"]],
            use_container_width=True, hide_index=True,
        )

    create_col, rename_col = st.columns(2)

    with create_col:
        st.markdown("#### Create a module")
        with st.form("create_module_form"):
            new_name = st.text_input("Name", key="new_module_name")
            new_code = st.text_input("Code", key="new_module_code")
            # Defaults to the team in the header, so a module created while a team
            # is selected lands in that team rather than orphaned with no team.
            module_team_choices = {"(no team)": None}
            if not teams_df.empty:
                for _, t in teams_df.iterrows():
                    module_team_choices[str(t["TEAM_NAME"])] = int(t["TEAM_ID"])
            default_team_label = next(
                (lbl for lbl, tid in module_team_choices.items() if tid == TEAM_ID),
                "(no team)",
            )
            new_team_label = st.selectbox(
                "Team", list(module_team_choices), key="new_module_team",
                index=list(module_team_choices).index(default_team_label),
            )
            new_description = st.text_area("Description", key="new_module_description", height=80)
            if st.form_submit_button("Create module", type="primary"):
                if not new_name.strip():
                    st.error("The module needs a name.")
                else:
                    module_id = create_module(new_name.strip(), new_code.strip() or new_name.strip(),
                                              new_description.strip(),
                                              team_id=module_team_choices[new_team_label])
                    st.success(f"Created module {new_name} (MODULE_ID {module_id}) "
                               f"under {new_team_label}.")
                    st.rerun()

    with rename_col:
        st.markdown("#### Rename a module")
        if modules.empty:
            st.info("Nothing to rename yet.")
        else:
            module_options = {f"{r['MODULE_NAME']} ({r['MODULE_CODE']})": r for _, r in modules.iterrows()}
            chosen_module_label = st.selectbox("Module", list(module_options), key="rename_module_pick")
            chosen_row = module_options[chosen_module_label]
            with st.form("rename_module_form"):
                renamed_name = st.text_input("Name", value=str(chosen_row["MODULE_NAME"]), key="rename_module_name")
                renamed_code = st.text_input("Code", value=str(chosen_row["MODULE_CODE"]), key="rename_module_code")
                renamed_description = st.text_area(
                    "Description", value=str(chosen_row["DESCRIPTION"] or ""),
                    key="rename_module_description", height=80,
                )
                if st.form_submit_button("Save changes", type="primary"):
                    if not renamed_name.strip():
                        st.error("The module needs a name.")
                    else:
                        rename_module(int(chosen_row["MODULE_ID"]), renamed_name.strip(),
                                     renamed_code.strip() or renamed_name.strip(), renamed_description.strip())
                        st.success(f"Updated {renamed_name}.")
                        st.rerun()

    st.divider()
    reassign_object_col, reassign_ruleset_col = st.columns(2)
    module_choices = {"(none)": None}
    for _, r in modules.iterrows():
        module_choices[f"{r['MODULE_NAME']} ({r['MODULE_CODE']})"] = int(r["MODULE_ID"])

    with reassign_object_col:
        st.markdown("#### Move an object to a different module")
        objects = scope_to_team(list_business_objects())
        if objects.empty:
            st.info("No business objects found.")
        else:
            object_options = {
                f"{r['OBJECT_NAME']} — {r['TECHNICAL_OBJECT_NAME']} "
                f"[{r['MODULE_NAME'] or 'no module'}]": r
                for _, r in objects.iterrows()
            }
            chosen_object_label = st.selectbox("Object", list(object_options), key="reassign_object_pick")
            chosen_object = object_options[chosen_object_label]
            current_object_label = next(
                (label for label, mid in module_choices.items() if mid == chosen_object["MODULE_ID"]), "(none)"
            )
            new_object_module_label = st.selectbox(
                "Module", list(module_choices), key="reassign_object_module_pick",
                index=list(module_choices).index(current_object_label),
            )
            if st.button("Move object", key="reassign_object_button"):
                reassign_object_module(int(chosen_object["OBJECT_ID"]), module_choices[new_object_module_label])
                st.success(f"{chosen_object['OBJECT_NAME']} moved to {new_object_module_label}.")
                st.rerun()

    with reassign_ruleset_col:
        st.markdown("#### Move a rule set to a different module")
        rule_sets = scope_to_team(list_rule_sets())
        if rule_sets.empty:
            st.info("No rule sets found.")
        else:
            rule_set_options = {
                f"{r['RULE_SET_NAME']} [{r['MODULE_NAME'] or 'no module'}]": r
                for _, r in rule_sets.iterrows()
            }
            chosen_rule_set_label = st.selectbox("Rule set", list(rule_set_options), key="reassign_ruleset_pick")
            chosen_rule_set = rule_set_options[chosen_rule_set_label]
            current_ruleset_label = next(
                (label for label, mid in module_choices.items() if mid == chosen_rule_set["MODULE_ID"]), "(none)"
            )
            new_ruleset_module_label = st.selectbox(
                "Module", list(module_choices), key="reassign_ruleset_module_pick",
                index=list(module_choices).index(current_ruleset_label),
            )
            if st.button("Move rule set", key="reassign_ruleset_button"):
                reassign_rule_set_module(int(chosen_rule_set["RULE_SET_ID"]), module_choices[new_ruleset_module_label])
                st.success(f"{chosen_rule_set['RULE_SET_NAME']} moved to {new_ruleset_module_label}.")
                st.rerun()

# ==================================================================
# Object lineage
# ==================================================================
elif page == "Object lineage":
    st.subheader("Object lineage")
    st.caption(
        "Starting from the topmost object (never a target itself), traces every "
        "source → target hop forward through the rule sets that use it. "
        "Read each band left to right: inputs, root, root view, stage, current, memories, "
        "core PIT. Picking a module keeps the chart inside it, stopping at the boundary "
        "where the data leaves."
    )

    edges = get_lineage_edges()
    if edges.empty:
        st.warning("No rule sets found to build lineage from.")
        st.stop()

    roots = lineage_roots(edges)
    if not roots:
        st.info("No topmost object found — every object is a target of some other object.")
        st.stop()

    # Roots are scoped to the team, but the chains below them are not: lineage
    # legitimately crosses into other modules and teams, and the page already
    # marks those hops. Hiding them would misrepresent where the data goes.
    if TEAM_MODULE_NAMES is not None:
        roots = [r for r in roots
                 if not r["module_name"] or r["module_name"] in TEAM_MODULE_NAMES]
        if not roots:
            st.info(f"No topmost objects belong to {TEAM_NAME}. Switch to All teams, or "
                    "assign a module to this team under Manage teams & modules.")
            st.stop()

    # No "All": the whole repository in one chart is unreadable, and a module is
    # what a reader is actually asking about. Nothing is drawn until one is chosen.
    module_filter_options = sorted({r["module_name"] for r in roots if r["module_name"]})
    chosen_module = st.selectbox("Module", module_filter_options, index=None,
                                 placeholder="Choose a module", key="lineage_module_filter")
    if not chosen_module:
        st.info("Choose a module to see its lineage.")
        st.stop()
    stay_in_module = st.checkbox(
        "Stay within this module", value=True, key="lineage_stay_in_module",
        help="Stops the chart at the point where the data leaves the module. "
             "Turn off to follow every hop downstream, into other modules.",
    )

    # Filtering the edges frame - rather than only the roots - is what actually
    # scopes the picture: build_dot() and _lineage_tree_lines() both walk whatever
    # frame they are handed. Shared with the Publish FDD export via
    # catalog.scope_edges_to_module(), so both draw the same picture.
    scoped_edges, boundary_ids, scoped_roots = scope_edges_to_module(
        edges, roots, chosen_module, stay_in_module
    )
    if stay_in_module and scoped_edges.empty:
        st.info(f"No hops recorded for {chosen_module}.")
        st.stop()

    if not scoped_roots:
        st.info("No topmost object in that module.")
        st.stop()

    root_options = {f"{r['object_name']} [{r['module_name'] or 'no module'}]": r for r in scoped_roots}
    show_all = st.checkbox("Show all topmost objects at once", value=True, key="lineage_show_all")

    if show_all:
        chosen_roots = list(root_options.values())
    else:
        chosen_label = st.selectbox("Topmost object", list(root_options), key="lineage_root_pick")
        chosen_roots = [root_options[chosen_label]]

    families = lineage_families(scoped_edges)
    view_cols = st.columns([3, 2, 2])
    chosen_family = view_cols[0].selectbox(
        "Family", ["All families"] + families, key="lineage_family",
        format_func=lambda f: f if f == "All families" else family_title(f),
        help="One band of the chart - e.g. only Hybrid Item Location and what feeds it.",
    )
    show_inputs = view_cols[1].checkbox(
        "Show inputs", value=True, key="lineage_show_inputs",
        help="Objects that feed a family from outside its own chain (other modules' "
             "views, master-data sources). Untick to see only the main chains.")
    compact = view_cols[2].checkbox(
        "Compact time travel", value=False, key="lineage_compact",
        help="Folds Current and Memories into one 'time travel' arrow from Stage to "
             "Core PIT, when those hops only pass the data through.")

    # Rendered at its intrinsic size, not stretched: use_container_width=True
    # scales the SVG up to fill the column, which magnifies the label text well
    # past the surrounding body copy.
    st.graphviz_chart(
        build_dot(scoped_edges, [r["object_id"] for r in chosen_roots], dashed_ids=boundary_ids,
                  family=None if chosen_family == "All families" else chosen_family,
                  show_inputs=show_inputs, compact_time_travel=compact),
        use_container_width=False,
    )
    st.caption(LEGEND)
    if boundary_ids:
        st.caption(
            f"Showing {chosen_module} only. A dashed box on the way out is where the data "
            "leaves this module — its onward chain is not drawn. Untick "
            "**Stay within this module** to follow it."
        )

    with st.expander("As an indented list"):
        for root in chosen_roots:
            lines = _lineage_tree_lines(scoped_edges, root["object_id"], root["object_name"],
                                       root["module_name"])
            st.markdown("<br>".join(lines), unsafe_allow_html=True)
            st.divider()

    with st.expander("All hops, as a table"):
        # Same frame the chart was built from, then trimmed to team scope so the
        # table can never disagree with the picture above it.
        table_edges = scoped_edges
        if TEAM_MODULE_NAMES is not None:
            table_edges = table_edges[
                table_edges["SOURCE_MODULE_NAME"].isin(TEAM_MODULE_NAMES)
                | table_edges["TARGET_MODULE_NAME"].isin(TEAM_MODULE_NAMES)
                | table_edges["SOURCE_MODULE_NAME"].isna()
                | table_edges["TARGET_MODULE_NAME"].isna()
            ]
        if chosen_module != "All" and not stay_in_module:
            table_edges = table_edges[
                (table_edges["SOURCE_MODULE_NAME"] == chosen_module)
                | (table_edges["TARGET_MODULE_NAME"] == chosen_module)
            ]
        display = table_edges.rename(columns={
            "SOURCE_OBJECT_NAME": "Source object", "SOURCE_MODULE_NAME": "Source module",
            "TARGET_OBJECT_NAME": "Target object", "TARGET_MODULE_NAME": "Target module",
            "RULE_SET_NAME": "Rule set", "IS_SELF_LOOP": "Self-loop",
        })
        st.dataframe(
            display[["Source object", "Source module", "Rule set", "Target object", "Target module", "Self-loop"]],
            use_container_width=True, hide_index=True,
        )

# ==================================================================
# Publish FDD - one module, assembled into one downloadable document
# ==================================================================
elif page == "Publish FDD":
    import fdd_publish as fp

    st.subheader("Publish FDD")
    st.caption(
        "Assembles one module's rules, required objects and column structure, and lineage "
        "into a single downloadable HTML document — the FDD in a form that can be attached "
        "to a wiki page or shared directly. Declared specification, not a live check; use "
        "Verify against Snowflake for that."
    )

    _pub_modules = scope_to_team(get_modules())
    if _pub_modules is not None and not _pub_modules.empty:
        # A module whose rules were all retired has nothing to publish; listing it next
        # to its replacement ("Transport Parameter" vs "Transportation Parameter")
        # invites publishing the dead copy.
        _pub_live = _pub_modules["ACTIVE_IND"].fillna(True).astype(bool)
        if "LIVE_RULE_COUNT" in _pub_modules:
            _pub_live &= _pub_modules["LIVE_RULE_COUNT"].fillna(0).astype(int) > 0
        _pub_hidden = int((~_pub_live).sum())
        _pub_modules = _pub_modules[_pub_live]
        if _pub_hidden:
            st.caption(f"{_pub_hidden} module(s) with no live rules (all retired, or none yet) "
                       "are not listed.")
    if _pub_modules is None or _pub_modules.empty:
        st.warning("No modules with live rules. Create one under Manage teams & modules first.")
        st.stop()

    _pub_options = {
        f"{r['MODULE_NAME']}": int(r["MODULE_ID"])
        for _, r in _pub_modules.sort_values("MODULE_NAME").iterrows()
    }
    _pub_module_name = st.selectbox("Module", list(_pub_options), key="pub_module")
    _pub_module_id = _pub_options[_pub_module_name]

    _pub_rule_sets = get_rule_sets_for_module(_pub_module_id)
    _pub_output_count = _pub_rule_sets["TARGET_OBJECT_ID"].dropna().astype(int).nunique() \
        if not _pub_rule_sets.empty else 0
    m1, m2 = st.columns(2)
    m1.metric("Rule sets", len(_pub_rule_sets))
    m2.metric("Output objects", _pub_output_count,
             help="Only these appear in the document — input/source-only objects are excluded.")

    if st.button("Generate document", type="primary", key="pub_generate"):
        with st.spinner(f"Assembling the {_pub_module_name} FDD…"):
            try:
                st.session_state["pub_html"] = fp.build_module_document(
                    _pub_module_id, _pub_module_name, current_viewer()
                )
                st.session_state["pub_html_module"] = _pub_module_name
            except Exception as exc:
                st.error(f"Could not assemble the document. {exc}")
                st.session_state.pop("pub_html", None)

    _pub_html = st.session_state.get("pub_html")
    if _pub_html and st.session_state.get("pub_html_module") == _pub_module_name:
        st.success("Document ready.")
        st.download_button(
            f"Download {_pub_module_name} FDD (.html)",
            _pub_html.encode("utf-8"),
            file_name=f"{_pub_module_name.replace(' ', '_')}_FDD.html",
            mime="text/html", type="primary", key="pub_download",
        )
        with st.expander("Preview"):
            st.caption("Lineage renders when the downloaded file is opened with network "
                      "access. It will not render inside this preview.")
            st.components.v1.html(_pub_html, height=700, scrolling=True)

# ==================================================================
# Verify against Snowflake
# ==================================================================
elif page == "Verify against Snowflake":
    st.subheader("Verify against Snowflake")
    st.caption(
        "Does the object the developer actually built match what this rule documents? "
        "Checks the real object's columns, the real data, and (optionally) its DDL logic."
    )

    catalog = scope_to_team(get_rule_catalog())
    if catalog.empty:
        st.warning("No rules found in VW_RULE_SUMMARY.")
        error = get_last_error()
        if error:
            st.error(f"The read failed, that's why it looked empty: {error}")
        st.stop()

    modules = ["All"] + sorted(catalog["MODULE_NAME"].dropna().astype(str).unique().tolist())
    chosen_module = st.selectbox("Module", modules, key="verify_module")
    scoped = catalog if chosen_module == "All" else catalog[catalog["MODULE_NAME"].astype(str) == chosen_module]

    targets = sorted(scoped["TARGET_BUSINESS_OBJECT"].dropna().astype(str).unique().tolist())
    if not targets:
        st.warning("No target objects found.")
        st.stop()
    chosen_target = st.selectbox("Object", targets, key="verify_target")

    view = scoped[scoped["TARGET_BUSINESS_OBJECT"].astype(str) == chosen_target]
    view = view.sort_values(["PRIORITY"], na_position="last")
    if view.empty:
        st.info("No rules write to that object.")
        st.stop()

    all_rule_ids = [int(r["RULE_ID"]) for _, r in view.iterrows()]
    st.info(f"**{len(all_rule_ids)}** rule(s) target this object.")

    bundles = load_bundles(all_rule_ids)
    if not bundles:
        st.warning("Could not load rules for this object.")
        st.stop()

    with st.expander("Options"):
        run_ai = st.checkbox("Run AI-assisted logic review", value=True, key="verify_run_ai",
                             help="Reads the real object's DDL and asks a model whether it looks "
                                  "consistent with each rule. A judgment call, not a guarantee.")
        model = st.selectbox("Model", [FAST_MODEL, WRITER_MODEL, "llama3.3-70b"], key="verify_model")

    if st.button("Run compliance check for all rules", type="primary"):
        results = []
        progress = st.progress(0, text="Checking rules…")
        for i, bundle in enumerate(bundles):
            rule = bundle["rule"]
            target_object = str(rule.get("TARGET_TECHNICAL_OBJECT") or "")
            source_objects = get_rule_set_source_objects(int(rule["RULE_SET_ID"])) if rule.get("RULE_SET_ID") else []
            result = run_compliance_check(bundle, source_objects, target_object, run_ai=run_ai, model=model)
            result["rule_id"] = int(rule.get("RULE_ID", 0))
            result["rule_number"] = str(rule.get("RULE_NUMBER", ""))
            result["rule_name"] = str(rule.get("RULE_NAME", ""))
            results.append(result)
            progress.progress((i + 1) / len(bundles), text=f"Checked {i + 1}/{len(bundles)} rules…")
        progress.empty()
        st.session_state["compliance_results"] = {"target": chosen_target, "results": results}

    stored = st.session_state.get("compliance_results")
    if stored and stored.get("target") == chosen_target:
        # Rules saved as written are read against the code, advisory only: they get
        # their own tab and never count towards gaps or violations.
        written_results = [r for r in stored["results"] if r.get("as_written")]
        results = [r for r in stored["results"] if not r.get("as_written")]

        total_structural = sum(r["structural_gaps"] for r in results)
        total_data = sum(r["data_gaps"] for r in results)
        st.markdown("---")
        st.markdown("### Summary")
        col1, col2, col3 = st.columns(3)
        col1.metric("Rules checked", len(results) + len(written_results))
        col2.metric("Structural gaps", total_structural)
        col3.metric("Data violations", total_data)
        if total_structural == 0 and total_data == 0:
            st.success("All rules pass structural and data checks.")
        else:
            st.warning(f"{total_structural} structural gap(s) and {total_data} data violation(s) found across all rules.")
        if written_results:
            _aw_counts = pd.Series([r.get("advisory_label") for r in written_results]).value_counts()
            st.caption(f"{len(written_results)} rule(s) are saved as written and were read against "
                       "the code, not tested: "
                       + ", ".join(f"{n} {label.lower()}" for label, n in _aw_counts.items())
                       + ". See the As written tab — these are never counted as failures.")

        st.markdown("---")
        tab_structural, tab_violations, tab_ai, tab_written = st.tabs([
            f"Structural Gaps ({total_structural})",
            f"Rule Violations ({total_data})",
            "AI Logic Review",
            f"As written ({len(written_results)})",
        ])

        with tab_written:
            if not written_results:
                st.info("No rules on this object are saved as written.")
            for result in written_results:
                label = f"{result['rule_number']} — {result['rule_name']}"
                verdict = result.get("advisory_verdict") or "NOT_CHECKED"
                color = {"MATCH": "green", "PARTIAL": "orange", "MISMATCH": "orange"}.get(verdict, "gray")
                with st.expander(f"{label} — {result.get('advisory_label')}"):
                    st.badge(result.get("advisory_label") or "Can't tell", color=color)
                    st.caption("Advisory: the rule is in plain English, so the code was read "
                               "against the sentence rather than tested. A developer can confirm.")
                    if result.get("ai"):
                        st.write(result["ai"].get("explanation") or "")
                    findings = pd.DataFrame(result["structural"])
                    if not findings.empty:
                        st.dataframe(findings.rename(columns={
                            "check": "Check", "object": "Object", "field": "Field",
                            "status": "Status", "note": "Note"}),
                            use_container_width=True, hide_index=True)

        with tab_structural:
            has_structural = False
            for result in results:
                structural_df = pd.DataFrame(result["structural"])
                if structural_df.empty:
                    continue
                has_structural = True
                label = f"{result['rule_number']} — {result['rule_name']}"
                # A rule whose target is not built yet is not a defect, so it does
                # not get the red circle a real mismatch does.
                only_not_built = (result.get("target_missing")
                                  and result["structural_gaps"] == 1)
                if only_not_built:
                    icon = ":large_orange_diamond:"
                elif result["structural_gaps"]:
                    icon = ":red_circle:"
                else:
                    icon = ":large_green_circle:"
                with st.expander(f"{icon} {label} ({result['structural_gaps']} gap(s))",
                                 expanded=result["structural_gaps"] > 0 and not only_not_built):
                    if result.get("target_missing"):
                        st.info(
                            "The target object does not exist in Snowflake yet, so its columns "
                            f"cannot be read — that is why {result.get('unverifiable', 0)} row(s) "
                            "below say UNKNOWN rather than OK or MISSING. This is expected for a "
                            "rule that documents an object still to be built; it is not a defect."
                        )
                    columns = {"check": "Check", "object": "Object", "field": "Field",
                               "status": "Status", "note": "Why"}
                    present = [c for c in columns if c in structural_df.columns]
                    st.dataframe(
                        structural_df[present].rename(columns=columns),
                        use_container_width=True, hide_index=True,
                    )
                    if only_not_built:
                        st.warning("Nothing to compare yet — the object is documented but not built.")
                    elif result["structural_gaps"]:
                        st.error(f"{result['structural_gaps']} gap(s) found.")
                    else:
                        st.success("No structural gaps.")
            if not has_structural:
                st.info("No field-level conditions or actions to check structurally across all rules.")

        with tab_violations:
            has_data = False
            for result in results:
                data_df = pd.DataFrame(result["data"])
                if data_df.empty:
                    continue
                has_data = True
                label = f"{result['rule_number']} — {result['rule_name']}"
                icon = ":red_circle:" if result["data_gaps"] else ":large_green_circle:"
                with st.expander(f"{icon} {label} ({result['data_gaps']} violation(s))",
                                 expanded=result["data_gaps"] > 0):
                    st.dataframe(data_df.rename(columns={
                        "condition": "Condition", "field": "Field", "total_rows": "Total rows",
                        "violating_rows": "Violating rows", "status": "Status",
                    }), use_container_width=True, hide_index=True)
                    if result["data_gaps"]:
                        st.error(f"{result['data_gaps']} condition(s) violated.")
                    else:
                        st.success("Real data matches every checkable condition.")
            if not has_data:
                st.info("No checkable filter conditions found across all rules.")

        with tab_ai:
            has_ai = False
            for result in results:
                if not result["ai"]:
                    continue
                has_ai = True
                label = f"{result['rule_number']} — {result['rule_name']}"
                verdict = result["ai"]["verdict"]
                badge_color = {"MATCH": "green", "PARTIAL": "orange", "MISMATCH": "red"}.get(verdict, "gray")
                with st.expander(f"{label} — {verdict}", expanded=verdict != "MATCH"):
                    st.badge(verdict, color=badge_color)
                    st.write(result["ai"]["explanation"])
            if not has_ai:
                st.info("AI logic review was not run or produced no results.")

# ==================================================================
# Ask
# ==================================================================
elif page == "Ask":
    st.subheader("Ask about a rule or the data")
    st.caption("Follow-up questions keep their context, so you can refine an answer "
               "without repeating yourself.")

    # Rules the current team is allowed to see. Every other page scopes to the
    # team selector; without this the Ask page would answer from another team's
    # rules, which is the one place the boundary was leaking.
    _ask_catalog = scope_to_team(get_rule_catalog())
    _ask_allowed_rule_ids = (
        set(_ask_catalog["RULE_ID"].astype(int).tolist())
        if _ask_catalog is not None and not _ask_catalog.empty else set()
    )

    with st.expander("Options"):
        use_analyst = st.checkbox("Use Cortex Analyst AI for data questions", value=True,
                                  help="When enabled, data questions are answered by Cortex Analyst "
                                       "which generates SQL from your semantic model.")
        allow_llm_route = st.checkbox("Let the model break ties on unclear questions", value=True)
        auto_run = st.checkbox("Run generated queries automatically", value=True,
                               help="Read-only SELECTs are executed as soon as they are generated. "
                                    "Turn off to review the SQL before it runs.")
        show_debug = st.checkbox("Show how the question was read", value=False)
        model = st.selectbox("Model for written answers", MODEL_CHOICES)
        if st.button("Clear conversation", key="ask_clear"):
            st.session_state.pop("ask_history", None)
            st.rerun()

    ask_history = st.session_state.setdefault("ask_history", [])

    def _analyst_conversation(history: list) -> list:
        """
        Prior turns in the shape Cortex Analyst expects, so a follow-up like
        "and for plant 200?" resolves against what was already asked.
        """
        messages = []
        for turn in history:
            if turn.get("role") == "user":
                messages.append({"role": "user",
                                 "content": [{"type": "text", "text": turn.get("text", "")}]})
            else:
                content = []
                if turn.get("analyst_text"):
                    content.append({"type": "text", "text": turn["analyst_text"]})
                if turn.get("sql"):
                    content.append({"type": "sql", "statement": turn["sql"]})
                if content:
                    messages.append({"role": "analyst", "content": content})
        return messages

    def _render_result(turn: dict):
        """Draw one assistant turn: prose, SQL, table, download, matching rules."""
        if turn.get("text"):
            st.markdown(turn["text"])
        if turn.get("error"):
            st.error(turn["error"])
        if turn.get("warning"):
            st.warning(turn["warning"])
        if turn.get("sql"):
            with st.expander("SQL", expanded=not turn.get("ran")):
                st.code(turn["sql"], language="sql")
        table = turn.get("table")
        if table is not None and not table.empty:
            st.dataframe(table, use_container_width=True, hide_index=True)
            st.caption(f"{len(table)} row(s)")
            st.download_button(
                "Download as CSV", data=table.to_csv(index=False).encode("utf-8"),
                file_name="ask_result.csv", mime="text/csv",
                key=f"ask_dl_{turn.get('seq', 0)}",
            )
        elif table is not None:
            st.info("The query ran and returned no rows.")
        if turn.get("rules_markdown"):
            st.divider()
            st.markdown(turn["rules_markdown"])
        if turn.get("elapsed") is not None:
            st.caption(f"Answered in {turn['elapsed']}s")

    def _build_followups(asked: str, intent, turn: dict) -> list:
        """
        Contextual next questions, derived from what the router actually resolved
        out of the question — so every suggestion names a real object, field or
        rule and can be answered. Cortex Analyst's own suggestions come first
        when it offered any, since those are grounded in the semantic model.
        """
        if intent.is_write:
            return []

        out = list(turn.get("suggestions") or [])
        kind = intent.intent
        obj_short = intent.objects[0].split(".")[-1] if intent.objects else ""
        field = intent.fields[0] if intent.fields else ""

        # Data questions pivot to other probes on the same object.
        if obj_short:
            by_intent = {
                router.DATA_SAMPLE: [f"How many records are in {obj_short}?",
                                     f"Are there duplicates in {obj_short}?",
                                     f"Which rules write to {obj_short}?"],
                router.DATA_COUNT: [f"Show sample rows from {obj_short}",
                                    f"Which rules write to {obj_short}?"],
                router.DATA_NULL: [f"Show sample rows from {obj_short}",
                                   f"What distinct values does {field} have?" if field else ""],
                router.DATA_DUPLICATE: [f"Show sample rows from {obj_short}",
                                        f"How many records are in {obj_short}?"],
                router.DATA_DISTINCT: [f"How many records are in {obj_short}?",
                                       f"Show sample rows from {obj_short}"],
            }
            out.extend(by_intent.get(kind, [f"Show sample rows from {obj_short}",
                                            f"Which rules write to {obj_short}?"]))

        # Rule questions pivot to the rule's target object and its tests.
        if intent.rule_ids and _ask_catalog is not None and not _ask_catalog.empty:
            match = _ask_catalog[_ask_catalog["RULE_ID"].astype(int) == int(intent.rule_ids[0])]
            if not match.empty:
                row = match.iloc[0]
                number = str(row.get("RULE_NUMBER") or "")
                target = str(row.get("TARGET_TECHNICAL_OBJECT") or "")
                target_short = target.split(".")[-1] if target else ""
                if number and kind != router.EXPLAIN_RULE:
                    out.append(f"Explain rule {number}")
                if number:
                    out.append(f"What test cases cover rule {number}?")
                if target_short:
                    out.append(f"Show sample rows from {target_short}")
                    out.append(f"Which rules write to {target_short}?")

        # Field questions pivot to usage and data quality on that field.
        if field:
            out.append(f"Which rules use {field}?")
            out.append(f"How many rows have {field} blank?")

        # Drop blanks, duplicates, and anything identical to what was just asked.
        seen = {(asked or "").strip().lower()}
        final = []
        for item in out:
            text = (item or "").strip()
            if not text or text.lower() in seen:
                continue
            seen.add(text.lower())
            final.append(text)
        return final[:4]

    # ---- replay the conversation so far -------------------------------
    # Follow-up buttons are drawn only under the most recent answer, so the
    # page doesn't accumulate a button for every turn ever taken.
    _followup_clicked = None
    _last_index = len(ask_history) - 1
    for _idx, turn in enumerate(ask_history):
        with st.chat_message("user" if turn.get("role") == "user" else "assistant"):
            if turn.get("role") == "user":
                st.markdown(turn.get("text", ""))
            else:
                _render_result(turn)
                if _idx == _last_index and turn.get("followups"):
                    st.caption("Ask next:")
                    for _j, _followup in enumerate(turn["followups"]):
                        if st.button(_followup, key=f"ask_fu_{_idx}_{_j}",
                                     use_container_width=True):
                            _followup_clicked = _followup

    def _build_starters(catalog_df) -> list:
        """
        A few questions grounded in this team's real rules, objects and columns,
        so the examples are always runnable rather than illustrative.
        """
        if catalog_df is None or catalog_df.empty:
            return []
        starters = []

        numbers = catalog_df["RULE_NUMBER"].dropna().astype(str)
        if not numbers.empty:
            starters.append(f"Explain rule {numbers.iloc[0]}")

        targets = catalog_df["TARGET_TECHNICAL_OBJECT"].dropna().astype(str)
        shorts = [t.split(".")[-1] for t in targets.unique() if t]
        if shorts:
            starters.append(f"Which rules write to {shorts[0]}?")
            starters.append(f"Show sample rows from {shorts[0]}")

        # A descriptive column off the first target makes for a real lineage question.
        if not targets.empty:
            try:
                from catalog import get_object_columns
                columns = get_object_columns(targets.iloc[0])
                pick = next((c for c in columns
                             if len(c) > 4 and not c.endswith("_ID")
                             and not c.startswith("CREATED") and not c.startswith("UPDATED")),
                            None)
                if pick:
                    starters.append(f"How is {pick} populated?")
            except Exception:
                pass

        modules = catalog_df["MODULE_NAME"].dropna().astype(str)
        if not modules.empty:
            starters.append(f"List all rules for {modules.iloc[0]}")

        # De-duplicate while keeping order.
        return list(dict.fromkeys(starters))[:5]

    _starter_clicked = None
    _starters = _build_starters(_ask_catalog)
    if _starters:
        if not ask_history:
            st.markdown("**Try one of these:**")
            for i, starter in enumerate(_starters):
                if st.button(starter, key=f"ask_starter_{i}", use_container_width=True):
                    _starter_clicked = starter
        else:
            with st.expander("Starter questions"):
                for i, starter in enumerate(_starters):
                    if st.button(starter, key=f"ask_starter_{i}", use_container_width=True):
                        _starter_clicked = starter

    question = st.chat_input("How is ITEM_DSC populated?  ·  Which rules write to T_BRD_ITEM_ROOT?")
    question = question or _starter_clicked or _followup_clicked

    if question and question.strip():
        question = question.strip()
        # Conversation context must be built before this turn is appended.
        prior_conversation = _analyst_conversation(ask_history)
        ask_history.append({"role": "user", "text": question})
        with st.chat_message("user"):
            st.markdown(question)

        answer = {"role": "assistant", "seq": len(ask_history)}

        with st.chat_message("assistant"):
            started = time.time()
            # Scoped to the team's databases so object resolution searches only
            # where that team's data lives - and so master-data objects, which
            # sit in a second database, are findable at all.
            intent = router.route(question, allow_llm=allow_llm_route,
                                  databases=TEAM_PHYSICAL_DATABASES)

            # Narrow to the team's rules before anything is loaded or summarised.
            if intent.rule_ids and _ask_allowed_rule_ids:
                kept = [int(r) for r in intent.rule_ids if int(r) in _ask_allowed_rule_ids]
                dropped = len(intent.rule_ids) - len(kept)
                intent.rule_ids = kept
                if dropped and TEAM_ID is not None:
                    answer["warning"] = (
                        f"{dropped} matching rule(s) belong to another team and were left out. "
                        f"Switch to All teams to include them."
                    )

            if show_debug:
                st.json({"intent": intent.intent, "confidence": round(intent.confidence, 2),
                         "decided_by": intent.resolved_by, "objects": intent.objects,
                         "fields": intent.fields, "rules": intent.rule_ids,
                         "scores": intent.scores})

            def _maybe_run(sql: str):
                """Execute a read-only SELECT and attach the result to the turn."""
                if not auto_run or not sql:
                    return
                if not sql.strip().upper().startswith("SELECT"):
                    answer["warning"] = "Only SELECT statements are run automatically."
                    return
                try:
                    answer["table"] = run_query(sql)
                    answer["ran"] = True
                except Exception as exc:
                    answer["error"] = f"The query did not run. {exc}"

            if intent.is_write:
                answer["text"] = ("That reads like a new requirement. Open **Write a rule** to turn "
                                  "it into metadata — this page only answers questions.")

            elif intent.is_data:
                # A structural probe against a named object is answered straight
                # from Snowflake: the object is already resolved and the SQL is
                # deterministic, so there is nothing for a model to add. Cortex
                # Analyst can only answer for objects that happen to be in the
                # semantic model, which is why "show sample rows from X" used to
                # come back wrong for anything outside it.
                #
                # An explicit signal score is required: the router's no-signal
                # fallback also lands on DATA_SAMPLE, and an analytical question
                # like "total quantity by plant in V_X" must still go to Analyst
                # rather than being answered with SELECT *.
                explicit_probe = intent.scores.get(intent.intent, 0) > 0
                sql, problem = (router.build_data_sql(question, intent)
                                if explicit_probe else (None, None))
                if sql:
                    answer["sql"] = sql
                    answer["text"] = f"Read directly from `{intent.objects[0]}`."
                    _maybe_run(sql)
                elif use_analyst:
                    with st.spinner("Cortex Analyst is thinking…"):
                        result = cortex_analyst_query(question, conversation=prior_conversation)
                    if result["error"]:
                        answer["error"] = result["error"]
                        if problem:
                            answer["warning"] = problem
                    else:
                        if result["text"]:
                            answer["text"] = result["text"]
                            answer["analyst_text"] = result["text"]
                        if result["sql"]:
                            answer["sql"] = result["sql"]
                            _maybe_run(result["sql"])
                        if result["suggestions"]:
                            answer["suggestions"] = result["suggestions"]
                elif problem:
                    answer["warning"] = problem

            else:
                bundles = load_bundles(intent.rule_ids)
                if not bundles:
                    with st.spinner("No matching rules found. Asking Cortex Analyst…"):
                        result = cortex_analyst_query(question, conversation=prior_conversation)
                    if result["error"]:
                        answer["text"] = ("No rule in the repository covers that, and Cortex Analyst "
                                          "could not help either. Try naming the field or target object.")
                        if show_debug:
                            answer["error"] = result["error"]
                    else:
                        if result["text"]:
                            answer["text"] = result["text"]
                            answer["analyst_text"] = result["text"]
                        if result["sql"]:
                            answer["sql"] = result["sql"]
                            _maybe_run(result["sql"])
                else:
                    from fdd_render import render_many as compact_many
                    context = compact_many(bundles, compact=True)
                    prompt = ("Answer the question in three sentences for a business audience. "
                              "Cite the rule numbers you relied on in brackets, like [Rule 3A].\n\n"
                              "Use only these rules. Do not add logic that is not stated.\n\n"
                              f"Question: {question}\n\nRules:\n{context}")
                    with st.spinner("Summarizing…"):
                        answer["text"] = llm_cached(prompt, model)
                    answer["rules_markdown"] = (f"**{len(bundles)} matching rule(s):**\n\n"
                                                + render_many(bundles))

            answer["elapsed"] = round(time.time() - started, 1)
            answer["followups"] = _build_followups(question, intent, answer)

        ask_history.append(answer)
        st.session_state["ask_history"] = ask_history
        # Rerun so this answer renders through the replay loop, which is what
        # draws the follow-up buttons. Everything needed is already in history,
        # so the rerun costs no model call and no query.
        st.rerun()

# ==================================================================
# Metadata Entry
# ==================================================================
elif page == "Metadata Entry":
    st.subheader("Mock Metadata Entry")
    st.caption(
        "Add, edit, or delete rows in the mock metadata tables. "
        "Developers will use these to prepare inserts into the actual "
        "T_CRR_BUSINESS_RULE_METADATA_HEADER/DETAIL tables."
    )

    MOCK_HEADER_TBL = tbl("T_MOCK_METADATA_HEADER")
    MOCK_DETAIL_TBL = tbl("T_MOCK_METADATA_DETAIL")

    HEADER_LABEL_COLS = [f"COLUMN_LABEL_{w}_TXT" for w in [
        "ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT",
        "NINE", "TEN", "ELEVEN", "TWELVE", "THIRTEEN", "FOURTEEN",
        "FIFTEEN", "SIXTEEN"]]
    DETAIL_VALUE_COLS = [f"COLUMN_VALUE_{w}_TXT" for w in [
        "ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT",
        "NINE", "TEN", "ELEVEN", "TWELVE", "THIRTEEN", "FOURTEEN",
        "FIFTEEN", "SIXTEEN"]]

    meta_tab_header, meta_tab_detail = st.tabs(["Header (Column Labels)", "Detail (Column Values)"])

    # ---- HEADER TAB ----
    with meta_tab_header:
        st.markdown("##### Metadata Header — defines what each column label means")

        header_df = cached_read(
            f"SELECT SUBJECT_AREA_NM, ENTITY_NM, BUSINESS_RULE_NM, "
            + ", ".join(HEADER_LABEL_COLS)
            + f", COMMENT_TXT, SOURCE_SYSTEM_NM FROM {MOCK_HEADER_TBL} ORDER BY SUBJECT_AREA_NM, ENTITY_NM, BUSINESS_RULE_NM"
        )

        if header_df.empty:
            st.info("No header rows yet. Add a new entry below.")
        else:
            st.dataframe(header_df, use_container_width=True, hide_index=True, height=300)

        with st.expander("Add new header row", expanded=header_df.empty):
            with st.form("add_header_form", clear_on_submit=True):
                st.markdown("**Key fields**")
                hc1, hc2, hc3 = st.columns(3)
                h_subj = hc1.text_input("SUBJECT_AREA_NM", key="h_subj")
                h_ent = hc2.text_input("ENTITY_NM", key="h_ent")
                h_rule = hc3.text_input("BUSINESS_RULE_NM", key="h_rule")

                st.markdown("**Column Labels (fill as many as needed)**")
                label_vals = []
                cols_row = st.columns(4)
                for i, col_name in enumerate(HEADER_LABEL_COLS):
                    short = f"Label {i+1}"
                    label_vals.append(cols_row[i % 4].text_input(short, key=f"h_lbl_{i}"))

                hc4, hc5 = st.columns(2)
                h_comment = hc4.text_input("COMMENT_TXT", key="h_comment")
                h_source = hc5.text_input("SOURCE_SYSTEM_NM", key="h_source")

                if st.form_submit_button("Insert Header Row", type="primary"):
                    if not h_subj.strip() or not h_ent.strip() or not h_rule.strip():
                        st.error("SUBJECT_AREA_NM, ENTITY_NM, and BUSINESS_RULE_NM are required.")
                    else:
                        col_list = "SUBJECT_AREA_NM, ENTITY_NM, BUSINESS_RULE_NM, " + ", ".join(HEADER_LABEL_COLS) + ", COMMENT_TXT, SOURCE_SYSTEM_NM, CREATE_AGENT_ID, CREATE_DTM"
                        val_list = (
                            f"{sql_value(h_subj)}, {sql_value(h_ent)}, {sql_value(h_rule)}, "
                            + ", ".join(sql_value(v) for v in label_vals)
                            + f", {sql_value(h_comment)}, {sql_value(h_source)}, {sql_value(st.session_state.get('user', 'APP_USER'))}, CURRENT_TIMESTAMP()"
                        )
                        execute(f"INSERT INTO {MOCK_HEADER_TBL} ({col_list}) VALUES ({val_list})")
                        bump_cache()
                        st.success("Header row inserted.")
                        st.rerun()

        if not header_df.empty:
            with st.expander("Update / Delete a header row"):
                row_keys = [f"{r['SUBJECT_AREA_NM']} | {r['ENTITY_NM']} | {r['BUSINESS_RULE_NM']}" for _, r in header_df.iterrows()]
                sel_key = st.selectbox("Select row to edit/delete", row_keys, key="h_sel")
                sel_idx = row_keys.index(sel_key)
                sel_row = header_df.iloc[sel_idx]

                with st.form("update_header_form"):
                    st.markdown("**Edit column labels**")
                    upd_labels = []
                    ucols = st.columns(4)
                    for i, col_name in enumerate(HEADER_LABEL_COLS):
                        current_val = sel_row[col_name] if pd.notna(sel_row[col_name]) else ""
                        upd_labels.append(ucols[i % 4].text_input(f"Label {i+1}", value=current_val, key=f"h_upd_{i}"))

                    uhc1, uhc2 = st.columns(2)
                    upd_comment = uhc1.text_input("COMMENT_TXT", value=sel_row["COMMENT_TXT"] if pd.notna(sel_row["COMMENT_TXT"]) else "", key="h_upd_comment")
                    upd_source = uhc2.text_input("SOURCE_SYSTEM_NM", value=sel_row["SOURCE_SYSTEM_NM"] if pd.notna(sel_row["SOURCE_SYSTEM_NM"]) else "", key="h_upd_source")

                    bcol1, bcol2 = st.columns(2)
                    do_update = bcol1.form_submit_button("Update Row", type="primary")
                    do_delete = bcol2.form_submit_button("Delete Row")

                if do_update:
                    set_parts = [f"{HEADER_LABEL_COLS[i]} = {sql_value(upd_labels[i])}" for i in range(16)]
                    set_parts.append(f"COMMENT_TXT = {sql_value(upd_comment)}")
                    set_parts.append(f"SOURCE_SYSTEM_NM = {sql_value(upd_source)}")
                    set_parts.append(f"CHANGE_AGENT_ID = {sql_value(st.session_state.get('user', 'APP_USER'))}")
                    set_parts.append("CHANGE_DTM = CURRENT_TIMESTAMP()")
                    where = (
                        f"SUBJECT_AREA_NM = {sql_value(sel_row['SUBJECT_AREA_NM'])} "
                        f"AND ENTITY_NM = {sql_value(sel_row['ENTITY_NM'])} "
                        f"AND BUSINESS_RULE_NM = {sql_value(sel_row['BUSINESS_RULE_NM'])}"
                    )
                    execute(f"UPDATE {MOCK_HEADER_TBL} SET {', '.join(set_parts)} WHERE {where}")
                    bump_cache()
                    st.success("Header row updated.")
                    st.rerun()

                if do_delete:
                    where = (
                        f"SUBJECT_AREA_NM = {sql_value(sel_row['SUBJECT_AREA_NM'])} "
                        f"AND ENTITY_NM = {sql_value(sel_row['ENTITY_NM'])} "
                        f"AND BUSINESS_RULE_NM = {sql_value(sel_row['BUSINESS_RULE_NM'])}"
                    )
                    execute(f"DELETE FROM {MOCK_HEADER_TBL} WHERE {where}")
                    bump_cache()
                    st.success("Header row deleted.")
                    st.rerun()

    # ---- DETAIL TAB ----
    with meta_tab_detail:
        st.markdown("##### Metadata Detail — actual column values for a given rule")

        detail_df = cached_read(
            f"SELECT BUSINESS_RULE_METADATA_DETAIL_ID, SUBJECT_AREA_NM, ENTITY_NM, BUSINESS_RULE_NM, "
            + ", ".join(DETAIL_VALUE_COLS)
            + f", COMMENT_TXT, SOURCE_SYSTEM_NM FROM {MOCK_DETAIL_TBL} ORDER BY BUSINESS_RULE_METADATA_DETAIL_ID"
        )

        if detail_df.empty:
            st.info("No detail rows yet. Add a new entry below.")
        else:
            st.dataframe(detail_df, use_container_width=True, hide_index=True, height=300)

        with st.expander("Add new detail row", expanded=detail_df.empty):
            with st.form("add_detail_form", clear_on_submit=True):
                st.markdown("**Key fields**")
                dc1, dc2, dc3 = st.columns(3)
                d_subj = dc1.text_input("SUBJECT_AREA_NM", key="d_subj")
                d_ent = dc2.text_input("ENTITY_NM", key="d_ent")
                d_rule = dc3.text_input("BUSINESS_RULE_NM", key="d_rule")

                st.markdown("**Column Values (fill as many as needed)**")
                value_vals = []
                dcols_row = st.columns(4)
                for i, col_name in enumerate(DETAIL_VALUE_COLS):
                    short = f"Value {i+1}"
                    value_vals.append(dcols_row[i % 4].text_input(short, key=f"d_val_{i}"))

                dc4, dc5 = st.columns(2)
                d_comment = dc4.text_input("COMMENT_TXT", key="d_comment")
                d_source = dc5.text_input("SOURCE_SYSTEM_NM", key="d_source")

                if st.form_submit_button("Insert Detail Row", type="primary"):
                    if not d_subj.strip() or not d_ent.strip() or not d_rule.strip():
                        st.error("SUBJECT_AREA_NM, ENTITY_NM, and BUSINESS_RULE_NM are required.")
                    else:
                        col_list = "SUBJECT_AREA_NM, ENTITY_NM, BUSINESS_RULE_NM, " + ", ".join(DETAIL_VALUE_COLS) + ", COMMENT_TXT, SOURCE_SYSTEM_NM, CREATE_AGENT_ID, CREATE_DTM"
                        val_list = (
                            f"{sql_value(d_subj)}, {sql_value(d_ent)}, {sql_value(d_rule)}, "
                            + ", ".join(sql_value(v) for v in value_vals)
                            + f", {sql_value(d_comment)}, {sql_value(d_source)}, {sql_value(st.session_state.get('user', 'APP_USER'))}, CURRENT_TIMESTAMP()"
                        )
                        execute(f"INSERT INTO {MOCK_DETAIL_TBL} ({col_list}) VALUES ({val_list})")
                        bump_cache()
                        st.success("Detail row inserted.")
                        st.rerun()

        if not detail_df.empty:
            with st.expander("Update / Delete a detail row"):
                detail_keys = [f"ID {r['BUSINESS_RULE_METADATA_DETAIL_ID']} | {r['SUBJECT_AREA_NM']} | {r['ENTITY_NM']} | {r['BUSINESS_RULE_NM']}" for _, r in detail_df.iterrows()]
                sel_dkey = st.selectbox("Select row to edit/delete", detail_keys, key="d_sel")
                sel_didx = detail_keys.index(sel_dkey)
                sel_drow = detail_df.iloc[sel_didx]

                with st.form("update_detail_form"):
                    st.markdown("**Edit column values**")
                    upd_values = []
                    udcols = st.columns(4)
                    for i, col_name in enumerate(DETAIL_VALUE_COLS):
                        current_val = sel_drow[col_name] if pd.notna(sel_drow[col_name]) else ""
                        upd_values.append(udcols[i % 4].text_input(f"Value {i+1}", value=current_val, key=f"d_upd_{i}"))

                    udc1, udc2 = st.columns(2)
                    upd_dcomment = udc1.text_input("COMMENT_TXT", value=sel_drow["COMMENT_TXT"] if pd.notna(sel_drow["COMMENT_TXT"]) else "", key="d_upd_comment")
                    upd_dsource = udc2.text_input("SOURCE_SYSTEM_NM", value=sel_drow["SOURCE_SYSTEM_NM"] if pd.notna(sel_drow["SOURCE_SYSTEM_NM"]) else "", key="d_upd_source")

                    dbcol1, dbcol2 = st.columns(2)
                    do_dupdate = dbcol1.form_submit_button("Update Row", type="primary")
                    do_ddelete = dbcol2.form_submit_button("Delete Row")

                if do_dupdate:
                    set_parts = [f"{DETAIL_VALUE_COLS[i]} = {sql_value(upd_values[i])}" for i in range(16)]
                    set_parts.append(f"COMMENT_TXT = {sql_value(upd_dcomment)}")
                    set_parts.append(f"SOURCE_SYSTEM_NM = {sql_value(upd_dsource)}")
                    set_parts.append(f"CHANGE_AGENT_ID = {sql_value(st.session_state.get('user', 'APP_USER'))}")
                    set_parts.append("CHANGE_DTM = CURRENT_TIMESTAMP()")
                    row_id = int(sel_drow["BUSINESS_RULE_METADATA_DETAIL_ID"])
                    execute(f"UPDATE {MOCK_DETAIL_TBL} SET {', '.join(set_parts)} WHERE BUSINESS_RULE_METADATA_DETAIL_ID = {row_id}")
                    bump_cache()
                    st.success("Detail row updated.")
                    st.rerun()

                if do_ddelete:
                    row_id = int(sel_drow["BUSINESS_RULE_METADATA_DETAIL_ID"])
                    execute(f"DELETE FROM {MOCK_DETAIL_TBL} WHERE BUSINESS_RULE_METADATA_DETAIL_ID = {row_id}")
                    bump_cache()
                    st.success("Detail row deleted.")
                    st.rerun()

# ==================================================================
# Object columns - the declared FDD column specification
# ==================================================================
elif page == "Object columns":
    import object_columns_builder as ocb
    from repository import get_object_column_spec, get_source_objects_for_target

    st.subheader("Object columns")
    st.caption(
        "What columns this object has, where each comes from, and which rules cover "
        "them. Source on the left, the spec you are building on the right. Edits are "
        "staged — nothing is written until you press **Save spec**. *Sync columns from "
        "Snowflake is the exception: it writes immediately.*"
    )

    objects_df = scope_to_team(list_business_objects())
    if objects_df.empty:
        st.warning("No business objects found. Add the first one here.")
        render_add_object_form("oc2", expanded=True)
        st.stop()

    # A just-created object should be the one you are looking at. The module
    # filter is reset first, because the new object may sit under a different
    # module than the one currently filtered to.
    _oc2_new = st.session_state.pop("oc2_new_object_id", None)
    if _oc2_new is not None:
        st.session_state["oc2_module"] = "All"

    pick = st.columns([2, 3])
    with pick[0]:
        _mods = ["All"] + sorted(objects_df["MODULE_NAME"].dropna().astype(str).unique().tolist())
        _module = st.selectbox("Module", _mods, key="oc2_module")
    _scoped = objects_df if _module == "All" else \
        objects_df[objects_df["MODULE_NAME"].astype(str) == _module]
    if _scoped.empty:
        st.info("No objects in that module.")
        render_add_object_form("oc2", default_module=None if _module == "All" else _module)
        st.stop()

    with pick[1]:
        _choices = {
            f"{r['OBJECT_NAME']}  ·  {r['TECHNICAL_OBJECT_NAME'] or 'no technical name'}":
                (int(r["OBJECT_ID"]), r["OBJECT_NAME"], r["TECHNICAL_OBJECT_NAME"])
            for _, r in _scoped.sort_values("OBJECT_NAME").iterrows()
        }
        # The widget keeps its own state, so index= would be ignored on a rerun.
        # Writing the label before the widget is created is what actually moves
        # the selection onto a newly added object.
        if _oc2_new is not None:
            for _lbl, (_oid, _, _) in _choices.items():
                if _oid == _oc2_new:
                    st.session_state["oc2_object"] = _lbl
                    break
        _label = st.selectbox("Object", list(_choices), key="oc2_object")
    _object_id, _object_name, _technical_name = _choices[_label]

    # Same form as the Write a rule page, one implementation.
    render_add_object_form("oc2", default_module=None if _module == "All" else _module)

    _sync, _msg = st.columns([1, 3])
    with _sync:
        if st.button("Sync columns from Snowflake", key="oc2_sync"):
            try:
                _written = sync_object_columns(_object_id, _technical_name,
                                              created_by=current_viewer())
                ocb.reset_object(_object_id)
                if _written:
                    st.session_state["oc2_flash"] = (
                        f"Stored {_written} new column(s) for {_object_name}. "
                        "That is already saved — Save spec is only for edits you make "
                        "in the grid.")
                else:
                    # Three different things produce zero, and telling the author the
                    # object "does not exist yet" when the lookup actually failed sends
                    # them looking for the wrong problem. live_column_failure() records
                    # why the read returned nothing.
                    from repository import live_column_failure
                    _why = live_column_failure(_technical_name)
                    if _why:
                        st.session_state["oc2_flash"] = (
                            "warning",
                            f"Could not read the columns of {_technical_name} from "
                            f"Snowflake: {_why}")
                    elif str(_technical_name or "").count(".") != 2:
                        st.session_state["oc2_flash"] = (
                            "warning",
                            f"{_object_name} has no three-part DB.SCHEMA.OBJECT name, so "
                            "there is nothing to look up in Snowflake.")
                    else:
                        st.session_state["oc2_flash"] = (
                            "info",
                            f"Nothing to add — every column {_technical_name} has in "
                            "Snowflake is already on the spec.")
                st.rerun()
            except Exception as exc:
                st.error(f"Nothing was stored. {exc}")

    _tab_names = ["Build spec", "FDD export", "Verify"] + (["Sample data"] if _sdu else [])
    _tabs = st.tabs(_tab_names)
    _tab_build, _tab_export, _tab_verify = _tabs[:3]
    if _sdu is not None:
        with _tabs[3]:
            st.caption("Sample rows for this object. For an object not in Snowflake yet, "
                       "paste them from the Excel FDD, type them, or draft them from the "
                       "rules that build it. Always marked as not from Snowflake.")
            _sample_data_panel(_technical_name, f"oc2_sample_{_object_id}")

    with _tab_build:
        ocb.render_builder({
            "object_id": _object_id,
            "object_name": _object_name,
            "technical_name": _technical_name,
            "source_objects": get_source_objects_for_target(_object_id),
            "objects_df": objects_df,
            "viewer": current_viewer(),
            "source_column_types": _source_column_types,
            "parse_pasted_columns": _parse_pasted_columns,
            "test_suggestions": render_test_suggestions,
            "writer_model": WRITER_MODEL,
        })

    _spec = get_object_column_spec(_object_id)
    with _tab_export:
        ocb.render_export(_object_name, _technical_name, _object_id, _spec,
                          get_source_objects_for_target(_object_id))
    with _tab_verify:
        ocb.render_verify(_technical_name, _spec)

# ==================================================================
# Test cases
# ==================================================================
elif page == "Test cases":
    import test_generator as tg
    from compliance import get_columns

    st.subheader("Test cases and test scripts")
    st.caption("Generate functional test cases and DQ checks from existing rules, "
               "then export or run them as a SQL test pack.")

    catalog = scope_to_team(get_rule_catalog())
    if catalog.empty:
        st.warning("No rules found in VW_RULE_SUMMARY.")
        error = get_last_error()
        if error:
            st.error(f"The read failed, that's why it looked empty: {error}")
        st.stop()

    tab_generate, tab_review, tab_manual, tab_view_all, tab_script = st.tabs([
        "Generate", "Run & Review", "Add Manual", "View All", "Test script"
    ])

    # --------------------------------------------------------------
    # Generate
    # --------------------------------------------------------------
    with tab_generate:
        # Pending-review callout
        _pending_gen = st.session_state.get("tc_generated") or []
        if _pending_gen:
            st.info(f"**{len(_pending_gen)} test case(s) waiting for review** — open the "
                    "**Run & Review** tab above to validate and save them.")

        modules = ["All"] + sorted(catalog["MODULE_NAME"].dropna().astype(str).unique().tolist())
        pick_module = st.selectbox("Module", modules, key="tc_module")
        scoped = catalog if pick_module == "All" else \
            catalog[catalog["MODULE_NAME"].astype(str) == pick_module]

        col_a, col_b = st.columns(2)
        with col_a:
            objects = ["All"] + sorted(scoped["TARGET_BUSINESS_OBJECT"].dropna().astype(str).unique().tolist())
            pick_object = st.selectbox("Target object", objects, key="tc_object")
        scoped = scoped if pick_object == "All" else \
            scoped[scoped["TARGET_BUSINESS_OBJECT"].astype(str) == pick_object]

        with col_b:
            rule_sets = ["All"] + sorted(scoped["RULE_SET_NAME"].dropna().astype(str).unique().tolist())
            pick_set = st.selectbox("Rule set", rule_sets, key="tc_ruleset")
        scoped = scoped if pick_set == "All" else \
            scoped[scoped["RULE_SET_NAME"].astype(str) == pick_set]

        if scoped.empty:
            st.info("No rules in that scope.")
            rule_options = {}
        else:
            rule_options = {f"{r['RULE_NUMBER']} — {r['RULE_NAME']}": int(r["RULE_ID"])
                            for _, r in scoped.sort_values("RULE_NUMBER").iterrows()}

        # Guarded rather than short-circuited with st.stop(): stopping inside a
        # tab would also stop the sibling tabs from rendering.
        if rule_options:
            existing = get_test_cases()
            have_cases = set(existing["RULE_ID"].astype(int).tolist()) if not existing.empty else set()
            already = [lbl for lbl, rid in rule_options.items() if rid in have_cases]
            if already:
                st.caption(f"{len(already)} of these rules already have saved test cases.")

            skip_existing = st.checkbox("Skip rules that already have saved test cases", value=True,
                                        key="tc_skip_existing")

            # Whether a rule can yield runnable SQL is decided by its metadata
            # alone, so it can be checked before spending any model calls. Without
            # this the only way to find out was to generate, save, and discover
            # every case had no SQL.
            st.markdown("**Which of these rules can produce runnable SQL?**")
            check_col, filter_col = st.columns([1, 2])
            with check_col:
                run_precheck = st.button("Check now", key="tc_precheck_btn",
                                         help="Reads rule metadata only — no model calls, no cost.")
            with filter_col:
                only_runnable = st.checkbox("Generate only for rules that can produce SQL",
                                            value=False, key="tc_only_runnable")

            if run_precheck:
                from requirement_compiler import _bundle_to_proposal
                bundles = load_bundles_all(list(rule_options.values()))
                lookup_pre = catalog.drop_duplicates("RULE_ID").set_index("RULE_ID")
                col_cache_pre = {}
                verdicts = {}
                for b in bundles:
                    rid = int((b.get("rule") or {}).get("RULE_ID"))
                    row = lookup_pre.loc[rid] if rid in lookup_pre.index else None
                    tgt = (str(row["TARGET_TECHNICAL_OBJECT"])
                           if row is not None and row["TARGET_TECHNICAL_OBJECT"] else None)
                    if tgt and tgt not in col_cache_pre:
                        col_cache_pre[tgt] = get_columns(tgt)
                    sql, reason = tg.build_any_test_sql(
                        _bundle_to_proposal(b), tgt, col_cache_pre.get(tgt, set()),
                        with_reason=True,
                    )
                    verdicts[rid] = (bool(sql), reason)
                st.session_state["tc_precheck_result"] = verdicts

            verdicts = st.session_state.get("tc_precheck_result") or {}
            if verdicts:
                can = sum(1 for ok, _ in verdicts.values() if ok)
                if can == 0:
                    st.warning(f"None of the {len(verdicts)} rule(s) in scope can produce "
                               "runnable SQL. Generating here will give you documentation-only "
                               "test cases.")
                else:
                    st.success(f"{can} of {len(verdicts)} rule(s) in scope can produce runnable SQL.")
                pre_rows = []
                for label, rid in rule_options.items():
                    ok, reason = verdicts.get(rid, (None, "not checked"))
                    pre_rows.append({
                        "Rule": label,
                        "Runnable SQL": "Yes" if ok else ("No" if ok is False else "—"),
                        "Why not": "" if ok else (reason or ""),
                    })
                st.dataframe(pd.DataFrame(pre_rows), use_container_width=True, hide_index=True)

            if st.button(f"Generate for all in scope ({len(rule_options)})", type="primary", key="tc_gen_all"):
                target_ids = list(rule_options.values())
                if only_runnable:
                    if not verdicts:
                        st.warning("Press 'Check now' first so there is something to filter on.")
                        target_ids = []
                    else:
                        target_ids = [r for r in target_ids if verdicts.get(r, (False,))[0]]
                if skip_existing:
                    target_ids = [r for r in target_ids if r not in have_cases]

                if not target_ids:
                    st.info("Nothing left to generate for after the filters above — "
                            "loosen 'Skip rules that already have saved test cases' or "
                            "'Generate only for rules that can produce SQL'.")
                else:
                    bundles = load_bundles_all(target_ids)
                    by_rule = {}
                    for b in bundles:
                        rid = int((b.get("rule") or {}).get("RULE_ID"))
                        by_rule[rid] = b

                    lookup = catalog.drop_duplicates("RULE_ID").set_index("RULE_ID")
                    # get_columns() hits INFORMATION_SCHEMA per object; rules sharing a
                    # target would otherwise pay for the same lookup repeatedly.
                    column_cache = {}
                    generated = []
                    progress = st.progress(0.0, text="Generating…")

                    for n, rid in enumerate(target_ids, start=1):
                        bundle = by_rule.get(rid)
                        if not bundle:
                            continue
                        row = lookup.loc[rid] if rid in lookup.index else None
                        rule_number = row["RULE_NUMBER"] if row is not None else rid
                        rule_name = row["RULE_NAME"] if row is not None else ""
                        target = (str(row["TARGET_TECHNICAL_OBJECT"])
                                  if row is not None and row["TARGET_TECHNICAL_OBJECT"] else None)

                        from requirement_compiler import _bundle_to_proposal
                        proposal = _bundle_to_proposal(bundle)

                        if target and target not in column_cache:
                            column_cache[target] = get_columns(target)
                        columns = column_cache.get(target, set())

                        cases = tg.suggest_test_cases(proposal, target_object=target)
                        sql, reason = tg.build_any_test_sql(
                            proposal, target, columns, with_reason=True
                        )
                        # Only keep cases that have runnable SQL — discard the rest
                        if not sql:
                            continue
                        for i, case in enumerate(cases):
                            case["rule_id"] = rid
                            case["rule_number"] = rule_number
                            case["rule_name"] = rule_name
                            case["target_object"] = target
                            case["test_id"] = tg.test_id(rule_number, i)
                            case["test_sql"] = sql
                        generated.extend(cases)
                        progress.progress(n / len(target_ids),
                                          text=f"Generating… {n} of {len(target_ids)} rules")

                    progress.empty()
                    st.session_state["tc_generated"] = generated
                    st.session_state.pop("tc_run_results", None)
                    st.success(f"Generated {len(generated)} runnable test case(s) across {len(target_ids)} rule(s).")

            generated = st.session_state.get("tc_generated") or []
            if generated:
                st.divider()
                st.success(f"{len(generated)} runnable test case(s) ready. "
                           "Go to the **Run & Review** tab to validate, edit, and save them.")

    # --------------------------------------------------------------
    # Run & Review
    # --------------------------------------------------------------
    with tab_review:
        generated = st.session_state.get("tc_generated") or []
        if not generated:
            st.info("No test cases generated yet. Use the **Generate** tab first.")
        else:
            # Header with Run All button
            header_col, run_all_col = st.columns([3, 1])
            with header_col:
                st.markdown(f"#### {len(generated)} runnable test case(s)")
            with run_all_col:
                run_all_btn = st.button("Run all queries", key="tc_run_all_btn", type="primary")

            # Run all queries and attempt auto-fix on failures
            run_results = st.session_state.get("tc_run_results") or {}
            if run_all_btn:
                results = {}
                run_progress = st.progress(0.0, text="Running queries…")
                for idx, case in enumerate(generated):
                    sql = case.get("test_sql", "")
                    if not sql.strip():
                        results[idx] = {"ok": False, "error": "Empty SQL", "fixed_sql": None}
                        continue
                    try:
                        df = run_query(sql)
                        if not df.empty and "VIOLATING_ROWS" in df.columns:
                            rows = int(df.iloc[0]["VIOLATING_ROWS"])
                        elif not df.empty:
                            rows = int(df.iloc[0][0])
                        else:
                            rows = 0
                        results[idx] = {"ok": True, "rows": rows, "fixed_sql": None}
                    except Exception as exc:
                        error_msg = str(exc)[:500]
                        # Attempt AI-assisted fix
                        fix_prompt = (
                            f"This Snowflake SQL query failed with the error below. "
                            f"Fix the query and return ONLY the corrected SQL, no explanation.\n\n"
                            f"Error: {error_msg}\n\nQuery:\n{sql}"
                        )
                        try:
                            fixed = llm_cached(fix_prompt, FAST_MODEL).strip()
                            import re as _re
                            fixed = _re.sub(r"^```(?:sql)?", "", fixed).strip()
                            fixed = _re.sub(r"```$", "", fixed).strip()
                            if fixed and fixed.upper().startswith("SELECT"):
                                results[idx] = {"ok": False, "error": error_msg,
                                                "fixed_sql": fixed}
                                generated[idx]["test_sql"] = fixed
                            else:
                                results[idx] = {"ok": False, "error": error_msg, "fixed_sql": None}
                        except Exception:
                            results[idx] = {"ok": False, "error": error_msg, "fixed_sql": None}
                    run_progress.progress((idx + 1) / len(generated),
                                          text=f"Running… {idx + 1}/{len(generated)}")
                run_progress.empty()
                st.session_state["tc_run_results"] = results
                st.session_state["tc_generated"] = generated
                st.rerun()

            # Summary of run results
            if run_results:
                ok_count = sum(1 for r in run_results.values() if r["ok"])
                fail_count = len(run_results) - ok_count
                fixed_count = sum(1 for r in run_results.values()
                                  if not r["ok"] and r.get("fixed_sql"))
                sum_col1, sum_col2, sum_col3 = st.columns(3)
                sum_col1.metric("Passed", ok_count)
                sum_col2.metric("Failed", fail_count)
                if fixed_count:
                    sum_col3.metric("Auto-fixed", fixed_count)
                if fail_count and fixed_count:
                    st.info(f"{fixed_count} failed query(ies) were auto-corrected by AI. "
                            "Review the updated SQL below, then Run again to verify.")

            # Editable SQL per case — all auto-selected
            edited_sqls = {}
            selected = {}
            for case_idx, case in enumerate(generated):
                tid = case["test_id"]
                ukey = f"{tid}_{case.get('rule_id', case_idx)}"
                with st.container(border=True):
                    col_check, col_info = st.columns([1, 5])
                    with col_check:
                        selected[case_idx] = st.checkbox("Save", value=True, key=f"tc_sel_{ukey}")
                    with col_info:
                        st.markdown(f"**`{tid}`** {case['test_name']}")
                        st.caption(f"Rule: {case.get('rule_number')} — {case.get('rule_name')}  |  "
                                   f"Type: {case['test_type']}  |  Target: `{case.get('target_object') or 'n/a'}`")
                        if case.get("scenario"):
                            st.caption(f"Scenario: {case['scenario']}")

                    # Editable SQL
                    edited_sqls[case_idx] = st.text_area(
                        "SQL", value=case.get("test_sql", ""), height=120,
                        key=f"tc_sql_{ukey}", label_visibility="collapsed",
                    )

                    # Show run result if available
                    if case_idx in run_results:
                        res = run_results[case_idx]
                        if res["ok"]:
                            st.success(f"Query OK — {res.get('rows', 0)} violating row(s)")
                        else:
                            st.error(f"Error: {res['error']}")
                            if res.get("fixed_sql"):
                                st.caption("AI suggested a fix (applied above). Run again to verify.")

                    # Run individual query button
                    if st.button("Run this query", key=f"tc_run_one_{ukey}"):
                        sql = edited_sqls.get(case_idx, case.get("test_sql", ""))
                        if not sql.strip():
                            st.error("Empty SQL")
                        else:
                            try:
                                df = run_query(sql)
                                if not df.empty and "VIOLATING_ROWS" in df.columns:
                                    rows = int(df.iloc[0]["VIOLATING_ROWS"])
                                elif not df.empty:
                                    rows = int(df.iloc[0][0])
                                else:
                                    rows = 0
                                run_results[case_idx] = {"ok": True, "rows": rows, "fixed_sql": None}
                                st.session_state["tc_run_results"] = run_results
                                st.success(f"Query OK — {rows} violating row(s)")
                            except Exception as exc:
                                run_results[case_idx] = {"ok": False, "error": str(exc)[:300], "fixed_sql": None}
                                st.session_state["tc_run_results"] = run_results
                                st.error(f"Error: {str(exc)[:300]}")

            # Save and Clear buttons
            st.divider()
            save_col, clear_col = st.columns(2)
            with save_col:
                if st.button("Save selected test cases", type="primary", key="tc_save"):
                    per_rule = {}
                    for idx, c in enumerate(generated):
                        if not selected.get(idx):
                            continue
                        per_rule.setdefault(c["rule_id"], []).append({
                            "test_type": c["test_type"],
                            "test_name": c["test_name"],
                            "scenario": c.get("scenario"),
                            "expected_result": c.get("expected_result"),
                            "test_sql": edited_sqls.get(idx, c.get("test_sql")),
                        })
                    if not per_rule:
                        st.warning("Nothing selected.")
                    else:
                        try:
                            total = sum(
                                save_test_cases(rid, cases, source_action="BULK",
                                                status="ACCEPTED", created_by=current_viewer())
                                for rid, cases in per_rule.items()
                            )
                            st.success(f"Saved {total} test case(s).")
                            st.session_state.pop("tc_generated", None)
                            st.session_state.pop("tc_run_results", None)
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Nothing was saved. {exc}")

            with clear_col:
                if st.button("Clear all", key="tc_clear"):
                    st.session_state.pop("tc_generated", None)
                    st.session_state.pop("tc_run_results", None)
                    st.rerun()

    # --------------------------------------------------------------
    # Add Manual
    # --------------------------------------------------------------
    with tab_manual:
        st.markdown("#### Add a test case manually")
        st.caption("Describe a test in plain English. AI will generate a runnable SQL test case "
                   "and add it to your generated batch. Put each test on its own line to create "
                   "several at once.")

        # Outcome of the last generate, stashed before the rerun that lets the
        # Run & Review tab pick up the new cases.
        _msg = st.session_state.pop("tc_manual_msg", None)
        if _msg:
            if _msg.get("added"):
                st.success(f"Added {_msg['added']} test case(s). "
                           "Switch to the **Run & Review** tab to validate and save.")
            if _msg.get("skipped"):
                st.warning(f"{_msg['skipped']} description(s) produced no runnable SQL. "
                           "Try being more specific about columns and values.")

        # Pending-review callout
        _pending = st.session_state.get("tc_generated") or []
        if _pending:
            st.info(f"**{len(_pending)} test case(s) waiting for review** — open the "
                    "**Run & Review** tab above to validate and save them.")

        # Cascading module -> object -> rule filters
        mf1, mf2 = st.columns(2)
        with mf1:
            man_modules = ["All"] + sorted(catalog["MODULE_NAME"].dropna().astype(str).unique().tolist())
            man_module = st.selectbox("Module", man_modules, key="tc_manual_module")
        man_scoped = catalog if man_module == "All" else \
            catalog[catalog["MODULE_NAME"].astype(str) == man_module]

        with mf2:
            man_objects = ["All"] + sorted(
                man_scoped["TARGET_BUSINESS_OBJECT"].dropna().astype(str).unique().tolist())
            man_object = st.selectbox("Target object", man_objects, key="tc_manual_object")
        man_scoped = man_scoped if man_object == "All" else \
            man_scoped[man_scoped["TARGET_BUSINESS_OBJECT"].astype(str) == man_object]

        if man_scoped.empty:
            st.info("No rules in that scope. Widen the module or object filter.")
        else:
            rule_options_manual = {f"{r['RULE_NUMBER']} — {r['RULE_NAME']}": int(r["RULE_ID"])
                                   for _, r in man_scoped.sort_values("RULE_NUMBER").iterrows()}

            manual_rule_label = st.selectbox(
                "For which rule?", list(rule_options_manual), key="tc_manual_rule")
            manual_description = st.text_area(
                "Describe the test case(s) — one per line",
                placeholder=("e.g.\n"
                             "Check that no rows have ITEM_STATUS = 'BLOCKED' when SOURCE_DELETE_IND = 'N'\n"
                             "Verify ITEM_DSC is never null for active items\n"
                             "Confirm row count matches the source table"),
                key="tc_manual_desc", height=140,
            )

            # Split into individual test descriptions: one per non-blank line.
            _lines = [ln.strip() for ln in str(manual_description or "").splitlines() if ln.strip()]
            if len(_lines) > 1:
                st.caption(f"Detected {len(_lines)} test case description(s).")

            if st.button("Generate from description", key="tc_manual_gen", type="primary",
                         disabled=not _lines):
                manual_rid = rule_options_manual[manual_rule_label]
                manual_row = catalog[catalog["RULE_ID"].astype(int) == manual_rid].iloc[0]
                manual_target = (str(manual_row["TARGET_TECHNICAL_OBJECT"])
                                 if manual_row["TARGET_TECHNICAL_OBJECT"] else None)

                _descriptions_block = "\n".join(f"{i + 1}. {ln}" for i, ln in enumerate(_lines))
                gen_prompt = (
                    f"Generate one Snowflake SQL test query for EACH numbered test case "
                    f"description below.\n\n"
                    f"Rule: {manual_row['RULE_NUMBER']} — {manual_row['RULE_NAME']}\n"
                    f"Target object: {manual_target or 'unknown'}\n"
                    f"Test descriptions:\n{_descriptions_block}\n\n"
                    f"Return ONLY a JSON array with one object per description, in the same "
                    f"order (no prose, no fences):\n"
                    f'[{{"test_type": "FUNCTIONAL" or "DQ_CHECK", '
                    f'"test_name": "short name under 80 chars", '
                    f'"scenario": "what is being tested", '
                    f'"expected_result": "what must be true", '
                    f'"test_sql": "SELECT COUNT(*) ... violation query"}}]'
                )
                try:
                    raw = llm_cached(gen_prompt, FAST_MODEL)
                    import re as _re
                    raw = _re.sub(r"^```(?:json)?", "", raw.strip()).strip()
                    raw = _re.sub(r"```$", "", raw).strip()
                    import json as _json
                    # Accept either an array or a single object.
                    a_start, a_end = raw.find("["), raw.rfind("]") + 1
                    if a_start != -1 and a_end > a_start:
                        parsed_list = _json.loads(raw[a_start:a_end])
                    else:
                        o_start, o_end = raw.find("{"), raw.rfind("}") + 1
                        parsed_list = [_json.loads(raw[o_start:o_end])]
                    if isinstance(parsed_list, dict):
                        parsed_list = [parsed_list]

                    existing_generated = st.session_state.get("tc_generated") or []
                    added, skipped = 0, 0
                    for offset, parsed in enumerate(parsed_list):
                        fallback_desc = _lines[offset] if offset < len(_lines) else ""
                        new_case = {
                            "rule_id": manual_rid,
                            "rule_number": str(manual_row["RULE_NUMBER"]),
                            "rule_name": str(manual_row["RULE_NAME"]),
                            "target_object": manual_target,
                            "test_id": tg.test_id(manual_row["RULE_NUMBER"],
                                                  len(existing_generated)),
                            "test_type": parsed.get("test_type", "FUNCTIONAL"),
                            "test_name": parsed.get("test_name", "Manual test"),
                            "scenario": parsed.get("scenario", fallback_desc),
                            "expected_result": parsed.get("expected_result", ""),
                            "test_sql": parsed.get("test_sql", ""),
                        }
                        if new_case["test_sql"]:
                            existing_generated.append(new_case)
                            added += 1
                        else:
                            skipped += 1

                    if added:
                        st.session_state["tc_generated"] = existing_generated
                        st.session_state.pop("tc_run_results", None)
                        # Rerun so the Run & Review tab (which renders earlier in
                        # the script) picks up the new cases. Messages are stashed
                        # and shown on the next pass.
                        st.session_state["tc_manual_msg"] = {
                            "added": added, "skipped": skipped,
                        }
                        st.rerun()
                    if skipped:
                        st.warning(f"{skipped} description(s) produced no runnable SQL. "
                                   "Try being more specific about columns and values.")
                    if not added and not skipped:
                        st.warning("AI returned nothing usable. Try rephrasing the description.")
                except Exception as exc:
                    st.error(f"Failed to generate: {exc}")

    # --------------------------------------------------------------
    # View All (module-wise saved cases)
    # --------------------------------------------------------------
    with tab_view_all:
        saved = get_test_cases()
        if saved.empty:
            st.info("No test cases saved yet.")
        else:
            lookup = catalog.drop_duplicates("RULE_ID").set_index("RULE_ID")

            def rule_label_va(rid):
                if rid in lookup.index:
                    row = lookup.loc[rid]
                    return f"{row['RULE_NUMBER']} — {row['RULE_NAME']}"
                return str(rid)

            # Module filter
            all_modules = sorted(catalog["MODULE_NAME"].dropna().astype(str).unique().tolist())
            view_module = st.selectbox("Module", ["All"] + all_modules, key="tc_view_all_module")

            f1, f2 = st.columns(2)
            with f1:
                status_filter = st.selectbox("Status", ["All", "ACCEPTED", "SUGGESTED", "REJECTED"],
                                             key="tc_view_status")
            with f2:
                type_filter = st.selectbox("Type", ["All", tg.FUNCTIONAL, tg.DQ_CHECK],
                                           key="tc_view_type")

            shown = saved.copy()
            # Filter by module via rule catalog
            if view_module != "All":
                module_rule_ids = set(catalog[catalog["MODULE_NAME"].astype(str) == view_module]["RULE_ID"].astype(int).tolist())
                shown = shown[shown["RULE_ID"].astype(int).isin(module_rule_ids)]
            if status_filter != "All":
                shown = shown[shown["STATUS"] == status_filter]
            if type_filter != "All":
                shown = shown[shown["TEST_TYPE"] == type_filter]

            if shown.empty:
                st.info("Nothing matches those filters.")
            else:
                display = shown.copy()
                display["RULE"] = display["RULE_ID"].astype(int).map(rule_label_va)
                display["MODULE"] = display["RULE_ID"].astype(int).map(
                    lambda rid: str(lookup.loc[rid]["MODULE_NAME"]) if rid in lookup.index else "—")
                display["CREATED_TS"] = pd.to_datetime(display["CREATED_TS"]).dt.strftime("%Y-%m-%d %H:%M")
                display["RUNNABLE"] = display["TEST_SQL"].apply(lambda v: "Y" if v else "N")
                cols = ["TEST_CASE_ID", "MODULE", "RULE", "TEST_TYPE", "TEST_NAME", "SCENARIO",
                        "EXPECTED_RESULT", "RUNNABLE", "STATUS", "CREATED_BY", "CREATED_TS"]
                st.dataframe(display[cols].rename(columns={
                    "TEST_CASE_ID": "ID", "MODULE": "Module", "TEST_TYPE": "Type",
                    "TEST_NAME": "Test name", "SCENARIO": "Scenario",
                    "EXPECTED_RESULT": "Expected", "CREATED_BY": "Added by", "CREATED_TS": "Added",
                }), use_container_width=True, hide_index=True)
                st.caption(f"{len(display)} test case(s).")

                st.download_button("Download as CSV",
                                   display[cols].to_csv(index=False).encode("utf-8"),
                                   file_name="saved_test_cases.csv", mime="text/csv",
                                   key="tc_view_all_dl")

                st.divider()
                st.markdown("##### Change status")
                ids = display["TEST_CASE_ID"].astype(int).tolist()
                pick_id = st.selectbox("Test case", ids, key="tc_view_pick",
                                       format_func=lambda i: f"{i} — " +
                                       str(display[display['TEST_CASE_ID'] == i].iloc[0]['TEST_NAME']))
                row = shown[shown["TEST_CASE_ID"].astype(int) == int(pick_id)].iloc[0]
                if row["TEST_SQL"]:
                    with st.expander("Stored SQL"):
                        st.code(str(row["TEST_SQL"]), language="sql")

                b1, b2, b3 = st.columns(3)
                with b1:
                    if st.button("Mark accepted", key="tc_view_accept"):
                        set_test_case_status(int(pick_id), "ACCEPTED", performed_by=current_viewer())
                        st.success("Marked accepted.")
                        st.rerun()
                with b2:
                    if st.button("Mark rejected", key="tc_view_reject"):
                        set_test_case_status(int(pick_id), "REJECTED", performed_by=current_viewer())
                        st.success("Marked rejected.")
                        st.rerun()
                with b3:
                    if st.button("Reset to suggested", key="tc_view_reset"):
                        set_test_case_status(int(pick_id), "SUGGESTED", performed_by=current_viewer())
                        st.success("Reset to suggested.")
                        st.rerun()

    # --------------------------------------------------------------
    # Test script
    # --------------------------------------------------------------
    with tab_script:
        st.markdown("#### Build a SQL test pack")

        source = st.radio(
            "Build from", ["Saved test cases", "Last generated batch"],
            horizontal=True, key="tc_script_source",
        )

        entries = []
        if source == "Saved test cases":
            saved = get_test_cases(status="ACCEPTED")
            if saved.empty:
                st.info("No accepted test cases saved yet. Generate some on the **Generate** tab.")
            else:
                lookup = catalog.drop_duplicates("RULE_ID").set_index("RULE_ID")
                scope_modules = ["All"] + sorted(catalog["MODULE_NAME"].dropna().astype(str).unique().tolist())
                script_module = st.selectbox("Module", scope_modules, key="tc_script_module")
                for _, r in saved.iterrows():
                    rid = int(r["RULE_ID"])
                    row = lookup.loc[rid] if rid in lookup.index else None
                    if script_module != "All":
                        if row is None or str(row["MODULE_NAME"]) != script_module:
                            continue
                    entries.append({
                        "rule_number": row["RULE_NUMBER"] if row is not None else rid,
                        "rule_name": row["RULE_NAME"] if row is not None else "",
                        "target_object": (str(row["TARGET_TECHNICAL_OBJECT"])
                                          if row is not None and row["TARGET_TECHNICAL_OBJECT"] else None),
                        "test_id": f"TC-{r['TEST_CASE_ID']}",
                        "test_type": r["TEST_TYPE"],
                        "test_name": r["TEST_NAME"],
                        "scenario": r["SCENARIO"],
                        "expected_result": r["EXPECTED_RESULT"],
                        "test_sql": r["TEST_SQL"] if r["TEST_SQL"] else None,
                    })
        else:
            entries = st.session_state.get("tc_generated") or []
            if not entries:
                st.info("Nothing generated in this session yet.")

        if entries:
            runnable = sum(1 for e in entries if e.get("test_sql"))
            st.caption(f"{len(entries)} test case(s) — {runnable} runnable, "
                       f"{len(entries) - runnable} manual.")

            pack = tg.build_test_pack(entries)
            st.code(pack, language="sql")

            dl_col, run_col = st.columns(2)
            with dl_col:
                st.download_button("Download test pack (.sql)", pack.encode("utf-8"),
                                   file_name="rule_test_pack.sql", mime="text/plain",
                                   key="tc_dl_sql")
                matrix = pd.DataFrame([{
                    "TEST_ID": e["test_id"], "RULE_NUMBER": e.get("rule_number"),
                    "RULE_NAME": e.get("rule_name"), "TEST_TYPE": e["test_type"],
                    "TEST_NAME": e["test_name"], "SCENARIO": e.get("scenario"),
                    "EXPECTED_RESULT": e.get("expected_result"),
                    "TARGET_OBJECT": e.get("target_object"),
                    "RUNNABLE": "Y" if e.get("test_sql") else "N",
                } for e in entries])
                st.download_button("Download test matrix (.csv)",
                                   matrix.to_csv(index=False).encode("utf-8"),
                                   file_name="rule_test_matrix.csv", mime="text/csv",
                                   key="tc_dl_csv")
            with run_col:
                if runnable and st.button("Run the automated checks", key="tc_run"):
                    # Each case is executed on its own rather than as the unioned
                    # pack: one bad object then reports against that test instead of
                    # failing the whole run, which is more useful when generated SQL
                    # references something that has since been dropped.
                    results = []
                    for e in [x for x in entries if x.get("test_sql")]:
                        sql = str(e["test_sql"]).strip().rstrip(";")
                        if not sql.upper().startswith("SELECT"):
                            results.append({"TEST_ID": e["test_id"], "TEST_NAME": e["test_name"],
                                            "RESULT": "SKIPPED", "VIOLATING_ROWS": None,
                                            "NOTE": "Not a SELECT"})
                            continue
                        try:
                            df = run_query(sql)
                            results.append({
                                "TEST_ID": e["test_id"], "TEST_NAME": e["test_name"],
                                "RESULT": str(df.iloc[0]["RESULT"]),
                                "VIOLATING_ROWS": int(df.iloc[0]["VIOLATING_ROWS"]),
                                "NOTE": "",
                            })
                        except Exception as exc:
                            results.append({"TEST_ID": e["test_id"], "TEST_NAME": e["test_name"],
                                            "RESULT": "ERROR", "VIOLATING_ROWS": None,
                                            "NOTE": str(exc)[:200]})
                    outcome = pd.DataFrame(results)
                    passed = int((outcome["RESULT"] == "PASS").sum())
                    failed = int((outcome["RESULT"] == "FAIL").sum())
                    errored = int(outcome["RESULT"].isin(["ERROR", "SKIPPED"]).sum())
                    if failed or errored:
                        st.warning(f"{passed} passed, {failed} failed, {errored} could not run.")
                    else:
                        st.success(f"All {passed} automated check(s) passed.")
                    st.dataframe(outcome, use_container_width=True, hide_index=True)
                    st.download_button("Download results (.csv)",
                                       outcome.to_csv(index=False).encode("utf-8"),
                                       file_name="rule_test_results.csv", mime="text/csv",
                                       key="tc_dl_results")



# ==================================================================
# Repository health - repository-wide gaps, not per-rule
# ==================================================================
elif page == "Repository health":
    import repository_health as rhealth

    st.subheader("Repository health")
    st.caption(
        "Every other check in the app looks at one rule or one object. These look at "
        "the whole repository, so a gap cannot hide. Declared intent only — use "
        "Verify against Snowflake to compare against what is deployed."
    )

    _rh_totals = rhealth.totals()
    if _rh_totals:
        _t = st.columns(6)
        _t[0].metric("Active rules", _rh_totals.get("ACTIVE_RULES", 0))
        _t[1].metric("Retired", _rh_totals.get("RETIRED_RULES", 0))
        _t[2].metric("Rule sets", _rh_totals.get("RULE_SETS", 0))
        _t[3].metric("Objects", _rh_totals.get("OBJECTS", 0))
        _t[4].metric("Columns", _rh_totals.get("COLUMNS", 0))
        _t[5].metric("Test cases", _rh_totals.get("TEST_CASES", 0))

    # Id allocation: reports what this session actually did, not a SQL check.
    _rh_ids = rhealth.id_allocation_warning()
    if not _rh_ids["ok"]:
        st.error(
            f"**Id allocation has fallen back to `MAX(id)+1`.** {_rh_ids['detail']} "
            "Concurrent saves can silently collide on ids — Snowflake does not enforce "
            "primary keys. Run `create_id_sequences.sql`."
        )

    with st.spinner("Running repository checks…"):
        _rh_results = rhealth.run_all()

    _rh_flagged = [r for r in _rh_results if not r["ok"]]
    _rh_errored = [r for r in _rh_results if r["errored"]]
    if _rh_errored:
        st.warning(f"{len(_rh_errored)} check(s) could not run — their counts are not "
                   "a clean result, just unknown.")
    if not _rh_flagged:
        st.success("Every check is clean.")
    else:
        st.info(f"{len(_rh_flagged)} of {len(_rh_results)} check(s) flagged something. "
                "Worst first.")

    _RH_ICON = {"high": ":red_circle:", "medium": ":large_orange_diamond:",
                "low": ":large_yellow_circle:"}

    for _rh in _rh_results:
        if _rh["errored"]:
            _icon, _suffix = ":grey_question:", " — check could not run"
        elif _rh["ok"]:
            _icon, _suffix = ":large_green_circle:", " — none"
        else:
            _icon, _suffix = _RH_ICON.get(_rh["severity"], ":white_circle:"), f" — {_rh['count']}"

        with st.expander(f"{_icon} {_rh['label']}{_suffix}", expanded=False):
            st.caption(_rh["why"])
            if _rh["errored"]:
                st.warning("This check's query failed, so nothing can be concluded from it.")
            elif _rh["rows"].empty:
                st.success("Nothing to report.")
            else:
                st.dataframe(_rh["rows"], use_container_width=True, hide_index=True)
                st.download_button(
                    f"Download as CSV ({_rh['count']} row(s))",
                    _rh["rows"].to_csv(index=False).encode("utf-8"),
                    file_name=f"health_{_rh['key']}.csv",
                    mime="text/csv", key=f"rh_dl_{_rh['key']}",
                )

# ==================================================================
# Import an FDD - paste a document, review the plan, then write it
# ==================================================================
elif page == "Import an FDD":
    import fdd_import_page

    fdd_import_page.render()

# ==================================================================
# Manual entry
# ==================================================================
else:
    st.subheader("Manual entry")
    st.caption("Use this when you need to set a single field exactly.")

    tab_rule, tab_condition, tab_action = st.tabs(["Rule", "Condition", "Action"])

    with tab_rule:
        rule_set_id = rule_set_picker("Rule set for the new rule")
        suggested_number = next_rule_number(rule_set_id) if rule_set_id is not None else ""
        number = st.text_input(
            "Rule number", value=suggested_number, key=f"manual_rule_number_{rule_set_id}",
            help="Auto-suggested as the next number in this rule set. Edit it if you use "
                 "a different scheme, like 3E, IL or NOTE1.",
        )
        name = st.text_input("Rule name")
        category = st.selectbox("Rule type", [
            "DERIVATION", "FILTER", "ACTIVE_STATUS", "INITIAL_LOAD", "METADATA_LOOKUP",
            "METADATA_DERIVATION", "STANDARDIZATION", "AGGREGATION", "JOIN_FILTER",
            "DEDUPLICATION", "NO_TRANSFORMATION", "ATTRIBUTE_CARRY_FORWARD",
            "TIME_TRAVEL", "KEY_DEFINITION"])
        description = st.text_area("What this rule is for")
        priority = st.number_input("Priority", 1, 999, 99)
        if st.button("Save rule", key="manual_rule"):
            if not name.strip() or rule_set_id is None:
                st.error("Rule set and rule name are required.")
            else:
                new_id = save_rule({
                    "rule_set_id": rule_set_id, "rule_number": number, "rule_name": name,
                    "rule_category": category, "business_description": description,
                    "priority": priority, "rule_status": DRAFT_STATUS,
                    "conditions": [], "actions": [], "joins": [],
                }, performed_by=current_viewer())
                _flash(f"Saved as RULE_ID {new_id}, status DRAFT.")
                st.rerun()

    with tab_condition:
        st.info("Conditions and actions attach to an existing rule. Keep the original "
                "manual forms here, or add them through **Write a rule**, which fills both "
                "tables in one step.")

    with tab_action:
        st.info("Same as conditions — add through **Write a rule** for the common case.")

with st.sidebar:
    st.divider()
    if st.button(":arrows_counterclockwise: Refresh metadata", use_container_width=True):
        bump_cache()
        st.success("Reloading on next read.")
