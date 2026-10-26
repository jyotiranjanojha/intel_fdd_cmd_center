"""The Import an FDD page: paste a document, review what the app understood, then save.

The review step is the whole point. Loading an FDD by hand is where this
repository lost accuracy - a module transcribed against the wrong environment,
78 straight pulls marked as needing transformation logic, 25 rules recorded with
no structured actions. An importer that just wrote whatever it parsed would make
those mistakes faster. So this page is built to make the parse *inspectable*:
every object, every column flag, every compiled rule and every line the parser
could not place is shown before anything is written.

Nothing here writes until Import is pressed, and that single press is one
transaction - all of it or none of it.
"""

import copy
import re
import time
from typing import Dict, List, Optional

import pandas as pd
import streamlit as st

import as_written as aw
import fdd_import as fi
import requirement_compiler as rc
import rule_editor
import rule_extractor as rx
from db import FAST_MODEL, WRITER_MODEL
from catalog import get_modules
from repository import (list_business_objects, list_module_rules, live_column_failure,
                        live_object_columns)
from requirement_compiler import compile_requirement

FLAG_HELP = {
    "match": "In the FDD and in Snowflake, compatible types.",
    "type_mismatch": "Same column, different type. The FDD value is kept - it is the spec.",
    "pasted_only": "In the FDD but not in Snowflake. Either not built yet, or misnamed.",
    "snowflake_only": "In Snowflake but missing from the FDD. The spec is incomplete.",
}
STATUS_LABEL = {"green": "Verified", "amber": "Check", "red": "Fix before saving"}

FLAG_ICON = {"match": "ok", "type_mismatch": "type differs",
             "pasted_only": "not in Snowflake", "snowflake_only": "missing from FDD"}


def _compile_with_effort(text: str, rule_number: str, source_object: str,
                         target_object: str, effort: str, counter: Dict,
                         sources: List[str] = None, context: Dict = None) -> Dict:
    """Compile one rule, spending a model call only when it is likely to help.

    "accurate" (the default) is how rules should be read: every rule goes to
    WRITER_MODEL with the hop's objects and their real columns, the heading above it
    and the other rules on the hop, then an independent reviewer call checks the
    result against the sentence. Each rule comes back green / amber / red.

    Model calls are the entire runtime of an import. Measured against this account
    (on the retired mistral-large2 / llama3.1-8b; re-measure for the Claude models),
    one rule at a time:

        mistral-large2 (old WRITER_MODEL)  5-9 s warm, but the first calls of a session
                                       took 309 s and 215 s - cold start dominates,
                                       and a 30-rule document can exceed 20 minutes
        llama3.1-8b (old FAST_MODEL)       1.5-4.3 s, consistently

    So imports use FAST_MODEL, and only "Thorough" pays for WRITER_MODEL. The
    deterministic sentence parser costs nothing once the column catalog is cached
    (st.cache_data, 15 min) and already handles the many simple statements in these
    documents, so Balanced tries it first and escalates only on failure. Nothing is
    lost by trying: the cheap attempt is discarded when the model is consulted.

    rule_set_id is passed as 0 - the real one does not exist until the import
    runs, and apply_import_plan overwrites it.
    """
    all_sources = list(sources or []) or source_object
    if effort in ("accurate", "thorough"):
        counter["model"] += 2 if effort == "accurate" else 1
        result = compile_requirement(text, 0, rule_number, all_sources,
                                     target_object, model=WRITER_MODEL, use_llm=True,
                                     context=context, review_rule=(effort == "accurate"))
        result["used_model"] = True
        return result

    cheap = compile_requirement(text, 0, rule_number, all_sources,
                               target_object, use_llm=False, context=context)
    if effort == "fast" or not cheap.get("errors"):
        cheap["used_model"] = False
        return cheap

    counter["model"] += 1
    rich = compile_requirement(text, 0, rule_number, all_sources,
                               target_object, model=FAST_MODEL, use_llm=True,
                               context=context)
    rich["used_model"] = True
    # If the model did no better, keep its result anyway - its errors are the ones
    # worth showing, since it had strictly more information to work with.
    return rich


def _object_index(text: str) -> Dict[str, List[str]]:
    """Every table and view the FDD could mean, keyed by object name.

    Covers the team's databases, every database the FDD names in full, and
    IF_MASTER_DATA (where the source views live), so a name written without its
    database - or with an outdated one - is found where it really is.
    """
    from catalog import get_object_index, physical_databases
    databases = list(physical_databases(st.session_state.get("_team_id")))
    for name in fi.TECHNICAL_NAME.findall(str(text or "").upper()):
        databases.append(name.split(".")[0])
    databases += ["IF_SUPPLY_CHAIN", "IF_MASTER_DATA"]
    try:
        df = get_object_index(list(dict.fromkeys(databases)))
    except Exception:
        return {}
    if df is None or df.empty:
        return {}
    names = (df["DATABASE_NAME"].astype(str) + "." + df["TABLE_SCHEMA"].astype(str)
             + "." + df["TABLE_NAME"].astype(str))
    return fi.build_object_index(names.tolist())


def _existing_objects() -> List[Dict]:
    """Objects already in the repository, so the plan reuses instead of duplicating."""
    try:
        df = list_business_objects()
    except Exception:
        return []
    if df is None or df.empty:
        return []
    rows = []
    for _, row in df.iterrows():
        technical = row.get("TECHNICAL_OBJECT_NAME")
        if not technical:
            continue
        rows.append({
            "technical_object_name": str(technical),
            "object_id": int(row["OBJECT_ID"]),
            "object_name": str(row.get("OBJECT_NAME") or ""),
        })
    return rows


def _live_columns(technical_name: str) -> List[Dict]:
    """Snowflake's current columns for one object, in fdd_import's shape."""
    df = live_object_columns(technical_name)
    if df is None or df.empty:
        return []
    return [
        {
            "column_name": str(row["COLUMN_NAME"]),
            "data_type": str(row.get("DATA_TYPE") or ""),
            "business_description": str(row.get("BUSINESS_DESCRIPTION") or ""),
            "ordinal": row.get("ORDINAL"),
        }
        for _, row in df.iterrows()
    ]


NEW_MODULE = "+ New module"


def _pick_module() -> tuple:
    """One control for the module: pick an existing one, or type a new name.

    Picking an existing module is what used to be "import in parts" - the app
    compares this FDD with what the module already holds either way.
    Returns (module_name, module_id or None).
    """
    modules = _team_modules()
    names = sorted(modules)
    current = st.session_state.get("fddimp_module", "")
    options = [NEW_MODULE] + names
    default = options.index(current) if current in names else 0
    picked = st.selectbox("Module", options, index=default, key="fddimp_module_pick")
    if picked == NEW_MODULE:
        name = st.text_input("New module name", key="fddimp_module_new",
                             value="" if current in names else current,
                             placeholder="Item Loc BOM")
        st.session_state["fddimp_mode"] = "new"
        st.session_state["fddimp_module"] = name
        # Typing the name of a module that exists adds to it, as before.
        found = {k.strip().upper(): v for k, v in modules.items()}.get(name.strip().upper())
        return name, found
    st.session_state["fddimp_mode"] = "existing"
    st.session_state["fddimp_module"] = picked
    return picked, modules[picked]


def render() -> None:
    st.subheader("Import an FDD")
    st.caption("Upload the FDD, check anything the app could not settle itself, then import.")
    _render_saved_drafts()

    module_name, module_id = _pick_module()
    # One way to read rules: the accurate path (model + reviewer).
    st.session_state["fddimp_effort"] = "accurate"

    _render_xlsx_upload()
    uploaded = bool(st.session_state.get("fddimp_xlsx_map"))
    with st.expander("Or paste the FDD text", expanded=not uploaded):
        text = st.text_area(
            "FDD text", height=260, key="fddimp_text", label_visibility="collapsed",
            placeholder="Source\tIF_MASTER_DATA.ITEM_IF_ANALYSIS.INTELFOUNDRY_MATERIAL_DETAIL\n"
                        "Transformation\tSet ACTIVE_IND to TRUE when DELETE_IND is FALSE\n"
                        "Root Table\tIF_SUPPLY_CHAIN.PDH_COMMON_XFRM_BASE.T_BRD_ITEM_ROOT",
        )

    if st.button("Read FDD", type="primary", disabled=not (text or "").strip()):
        if not module_name.strip():
            st.error("Give the module a name first.")
        else:
            _read(text, module_name.strip(), module_id)
        st.session_state.pop("fddimp_done", None)

    done = st.session_state.get("fddimp_done")
    if done:
        _render_result(done)
        return

    plan = st.session_state.get("fddimp_plan")
    if plan:
        _render_plan(plan)
        _autosave()


def _read(text: str, module_name: str, module_id: Optional[int]) -> None:
    """Parse, compile every rule, then settle what needs no person (auto_resolve)."""
    _clear_rule_text_state()
    bar = st.progress(0.0, text="Looking up the objects in Snowflake...")
    index = _object_index(text)
    parsed = fi.parse_document(text, object_index=index)
    total = len(parsed.get("rule_statements", []))
    started = time.time()
    counter = {"model": 0}

    def progress_fn(done, of_total, label):
        bar.progress(min(done / of_total if of_total else 1.0, 1.0),
                     text=f"Reading rule {done} of {of_total}")

    def compile_fn(statement_text, rule_number="", source_object="",
                   target_object="", sources=None, context=None):
        return _compile_with_effort(statement_text, rule_number, source_object,
                                    target_object, "accurate", counter,
                                    sources=sources, context=context)

    plan = fi.build_import_plan(
        parsed, module_name,
        compile_fn=compile_fn,
        live_columns_fn=_live_columns,
        live_failure_fn=live_column_failure,
        existing_objects=_existing_objects(),
        progress_fn=progress_fn,
    )
    st.session_state["fddimp_plan"] = plan
    _mark_origins(plan, text)
    if module_id is not None:
        _mark_parts(plan, module_id)
    settled = _auto_resolve_all(plan)
    _refresh_summary(plan)
    bar.empty()
    st.session_state["fddimp_timing"] = {
        "seconds": round(time.time() - started, 1), "model_calls": counter["model"],
        "rules": total, **settled}
    # A large parse is minutes of model calls: never pay for it twice.
    st.session_state.pop("fddimp_draft", None)
    _autosave(force=True)


def _auto_resolve_all(plan: Dict) -> Dict:
    """auto_resolve on every rule; totals for the Details section."""
    totals = {"fixed": 0, "accepted": 0, "noted": 0, "open": 0}
    for entry in plan.get("rules") or []:
        counts = rx.auto_resolve(entry, lambda proposal, e=entry: _regrade(e, proposal))
        for k in totals:
            totals[k] += counts[k]
    return totals


def _render_plan(plan: Dict) -> None:
    """Three things only: a one-line summary, what needs a look, and Import.

    Everything else - objects, name corrections, lines not imported, rules already
    in the module, verified rules, timing - is in one collapsed Details section.
    """
    _forget_old_skips(plan)
    _refresh_summary(plan)
    rules = plan.get("rules", [])
    specs = plan.get("rule_sets") or []
    needs = _needs_list(plan)
    live = [i for i, r in enumerate(rules) if not _skipped(r) and not r.get("_hop_off")]
    ready = [i for i in live if _include(rules[i])]
    st.divider()
    st.markdown(
        f"**{len(live)} rule(s) on {len(specs) or 1} object(s)**  ·  {len(ready)} ready"
        + (f"  ·  **{len(needs)} need a look**" if needs else "")
    )

    st.toggle("New review layout (preview)", key="fddimp_new_layout",
              help="Each rule as View / Edit / FDD text. Switch off for the current layout; "
                   "both work on the same review.")
    if needs:
        _render_needs(plan, needs)
    elif rules:
        st.success("Nothing needs a look.")
    else:
        st.info("No rules were found. Rules live in the Transformation rows of the FDD.")

    keep_objects = _render_details(plan)
    keep_rules = _kept(rules)

    st.divider()
    _render_draft_bar()
    updates = sum(1 for i in keep_rules if rules[i].get("prior")
                  and rules[i].get("prior_choice") == "update")
    gaps = fi.coverage(plan)
    label = (f"Import {len(keep_rules)} rule(s) into \"{plan['module_name']}\""
             + (f" ({updates} updated)" if updates else ""))
    _render_coverage(plan, gaps)
    if st.button(label, type="primary",
                 disabled=not gaps["complete"] or not (keep_objects or keep_rules),
                 help="Every rule in the FDD is imported: the button waits until each one "
                      "is ready. One transaction: if any part fails, nothing is written."):
        try:
            with st.spinner("Writing..."):
                written = fi.apply_import_plan(
                    plan,
                    performed_by=_viewer(),
                    selected_objects=keep_objects,
                    selected_rules=keep_rules,
                    # The new module joins the team currently selected. Left NULL
                    # it belongs to no team, and every page that scopes by team
                    # hides it - the import appears to have done nothing.
                    team_id=st.session_state.get("_team_id"),
                    require_complete=True,
                )
            st.session_state["fddimp_done"] = written
            _close_draft(plan, written)
            st.session_state.pop("fddimp_plan", None)
            st.rerun()
        except Exception as exc:
            st.error(f"Import failed and nothing was written: {exc}")


def _needs_list(plan: Dict) -> List[tuple]:
    """(set_index, rule_index or None, problem) for everything a person must decide.

    rule_index None is a problem with the hop itself (no target, ambiguous target),
    shown as one entry on that hop instead of a separate warning box.
    """
    rules = plan.get("rules", [])
    out: List[tuple] = []
    specs = plan.get("rule_sets") or [{"rule_indexes": list(range(len(rules)))}]
    for set_index, spec in enumerate(specs):
        indexes = [i for i in spec.get("rule_indexes", [])
                   if not _skipped(rules[i]) and not rules[i].get("_hop_off")]
        if not indexes:
            continue
        if not spec.get("target_technical_object"):
            out.append((set_index, None, "No object follows this Transformation, so the "
                                          "target is unknown. Set it on Manage modules after importing."))
        elif spec.get("ambiguous_target"):
            out.append((set_index, None, "Another Transformation block also resolved to "
                                          "this object - check the rules belong here."))
        for i in indexes:
            if not _needs(rules[i]):
                continue
            out.append((set_index, i, _top_problem(rules[i])))
    return out


def _top_problem(entry: Dict) -> str:
    for message in entry.get("errors") or []:
        return message
    issues = _open(entry)
    if issues:
        return issues[0]["title"]
    if (entry.get("prior") or {}).get("state") == fi.CHANGED:
        return "Changed in the FDD since it was imported - keep, update or add."
    return (entry.get("reasons") or entry.get("warnings") or ["Needs a look"])[0]


def _render_needs(plan: Dict, needs: List[tuple]) -> None:
    rules = plan["rules"]
    specs = plan.get("rule_sets") or [{}]
    red = sum(1 for r in rules if _status(r) == "red")
    if red > max(3, len(rules) // 2):
        st.caption("Most rules have a problem. That usually means the layout was not "
                   "recognised - check the objects in Details first.")
    _render_as_written_bar(plan, needs)
    current = None
    for set_index, index, problem in needs:
        if set_index != current:
            current = set_index
            spec = specs[set_index] if set_index < len(specs) else {}
            st.markdown(f"**{_leaf(spec.get('target_technical_object')) or '(no target)'}**")
        if index is None:
            st.markdown(f":orange[{problem}]")
            continue
        entry = rules[index]
        name = (entry.get("proposal") or {}).get("rule_name") or rx.short(entry["text"], 8)
        cols = st.columns([6, 1.6, 1.6, 1.1])
        cols[0].markdown(f"{entry.get('rule_number') or '-'} · {name}  \n:grey[{problem}]")
        issue = next((i for i in _open(entry) if i.get("suggestion")), None)
        editing = st.session_state.get("fddimp_open") == index
        if entry.get("prior") and (entry["prior"] or {}).get("state") == fi.CHANGED:
            _render_prior_choice(plan, index, compact=True)
        elif issue and not editing and cols[1].button(
                "Choose", key=f"fddimp_need_fix_{index}", type="primary",
                help="Single-answer fixes were applied already; this one has options."):
            # Anything still carrying a suggestion has competing options (auto_resolve
            # applied the single-answer ones), so open the rule to pick one.
            st.session_state["fddimp_open"] = index
            st.rerun()
        if cols[2].button("Save as written", key=f"fddimp_need_aw_{index}",
                          help="Keep the FDD's sentence word for word as the rule. For "
                               "logic in plain English the app cannot model; developers "
                               "build from the text and Verify reads the code against it."):
            aw.make_entry_as_written(entry)
            _refresh_summary(plan)
            st.rerun()
        if cols[3].button("Close" if editing else "Edit", key=f"fddimp_need_edit_{index}"):
            st.session_state["fddimp_open"] = None if editing else index
            st.rerun()
        if editing:
            _render_rule(plan, index, expanded=True)


def _render_as_written_bar(plan: Dict, needs: List[tuple]) -> None:
    """Bulk "save the rest as written", and the list of those chosen, with Undo."""
    rules = plan["rules"]
    waiting = [i for _, i, _ in needs if i is not None
               and not (rules[i].get("prior") or {}).get("state") == fi.CHANGED]
    chosen = [i for i, r in enumerate(rules) if r.get("as_written") == "chosen"]
    if len(waiting) > 1:
        if st.button(f"Save the remaining {len(waiting)} as written",
                     key="fddimp_aw_all",
                     help="Every rule still on this list is kept in the FDD's words. "
                          "Each one can be undone below."):
            for i in waiting:
                aw.make_entry_as_written(rules[i])
            _refresh_summary(plan)
            st.rerun()
    if chosen:
        with st.popover(f"Saved as written ({len(chosen)})"):
            for i in chosen:
                left, right = st.columns([5, 1])
                left.caption(f"{rules[i].get('rule_number') or '-'} · {rx.short(rules[i]['text'], 14)}")
                if right.button("Undo", key=f"fddimp_aw_undo_{i}"):
                    aw.undo_entry_as_written(rules[i])
                    _refresh_summary(plan)
                    st.rerun()


def _render_details(plan: Dict) -> List[int]:
    """Everything that is not a decision, in one place. Returns the objects to keep."""
    rules = plan.get("rules", [])
    summary = plan["summary"]
    timing = st.session_state.get("fddimp_timing") or {}
    skipped = [i for i in range(len(rules)) if _skipped(rules[i])]
    verified = [i for i in range(len(rules)) if i not in skipped and not _needs(rules[i])
                and not rules[i].get("_hop_off")]
    left = summary["unparsed_lines"] + summary["orphan_columns"]
    # Read from the plan, not from widgets: Details may be closed, and Streamlit drops
    # the state of widgets that are not drawn.
    keep_objects = [i for i, o in enumerate(plan["objects"]) if o.get("include", True)]
    # A toggle, not an expander: the objects, rules and hop picker inside use
    # expanders of their own, and Streamlit does not allow them to be nested.
    if not st.toggle("Details", key="fddimp_details"):
        return keep_objects
    with st.container(border=True):
        bits = [f"{len(plan['objects'])} object(s)", f"{summary['columns']} column(s)"]
        if timing:
            bits.append(f"read in {timing['seconds']}s")
            settled = [f"{timing[k]} {w}" for k, w in (("fixed", "fixed automatically"),
                                                        ("accepted", "doubts"),
                                                        ("noted", "FDD notes")) if timing.get(k)]
            if settled:
                bits.append(", ".join(settled) + " (saved as review comments)")
        st.caption("  ·  ".join(bits))
        if plan.get("parts"):
            _render_progress(plan["parts"]["module_id"])
        specs = plan.get("rule_sets") or []
        if plan.get("parts") or len(specs) > 1:
            _render_hop_picker(plan, specs)

        tabs = st.tabs([f"Rules ({len(verified)} ready)",
                        f"Objects ({len(plan['objects'])})",
                        f"Review comments ({summary.get('review_comments', 0)})",
                        f"Lines not placed ({left})"])
        with tabs[0]:
            for index in verified:
                _render_rule(plan, index)
            if skipped:
                st.markdown(f"**{len(skipped)} already in the module - not written again**")
                for index in skipped:
                    _render_prior_choice(plan, index, compact=True)
        with tabs[1]:
            _render_name_corrections(plan)
            _render_objects(plan)
        with tabs[2]:
            _render_review_comments(plan)
        with tabs[3]:
            _render_leftovers(plan)
    return keep_objects


def _render_name_corrections(plan: Dict) -> None:
    completed = [c for c in plan.get("completed_names") or []
                 if c["written"].upper() != c["completed"].upper()
                 or c["database_from"] in ("ambiguous", "not found")]
    if not completed:
        return
    st.markdown(f"**{len(completed)} object name(s) completed from Snowflake**")
    st.dataframe(pd.DataFrame([{
        "Written in FDD": c["written"], "Used as": c["completed"],
        "How": c["database_from"], "Options": ", ".join(c.get("options") or []),
    } for c in completed]), hide_index=True, use_container_width=True)


def _render_objects(plan: Dict) -> List[int]:
    keep: List[int] = []
    if not plan["objects"]:
        st.info("No objects were found. Check that the objects section lists fully "
                "qualified DB.SCHEMA.NAME values.")
        return keep

    for index, obj in enumerate(plan["objects"]):
        counts = obj["counts"]
        flags = counts["type_mismatch"] + counts["pasted_only"] + counts["snowflake_only"]
        status = "reuse existing" if obj["action"] == "reuse" else "new"
        discovered = obj.get("layer") == "discovered"
        if discovered:
            status = "discovered"
        error = obj.get("lookup_error")
        if error:
            built = "could not read Snowflake"
        elif obj["exists_in_snowflake"]:
            built = "in Snowflake"
        else:
            built = "not in Snowflake"
        inferred = obj.get("inferred_from_label")
        label = (f"{obj['object_name']}  ·  {obj['object_type']}  ·  {status}  ·  {built}"
                 f"  ·  {len(obj['columns'])} columns"
                 + (f"  ·  {flags} flagged" if flags else "")
                 + ("  ·  layer inferred" if inferred else ""))

        with st.expander(label, expanded=bool(error) or flags > 0 or discovered or bool(inferred)):
            st.code(obj["technical_object_name"], language=None)
            if inferred:
                st.info(
                    f"The FDD labels this row **{inferred.title()}**, which isn't one of the "
                    f"labels the importer knows, so its layer was taken from the object's "
                    f"name: **{obj.get('layer', '?').replace('_', ' ')}**. Check it reads "
                    "right. A source is only ever an input, so it never gets rules of its own."
                )
            if discovered:
                st.info(
                    "Discovered from edited rule text. This object was not in the "
                    "original FDD but appeared in a rule you edited. Its columns "
                    "come from Snowflake if the object exists there."
                )
            if error:
                st.error(
                    "Could not read this object's columns from Snowflake, so the "
                    "comparison below shows only what the FDD says. This is a lookup "
                    "failure, not evidence the object is missing."
                )
                st.caption(f"Reason: {error}")
            elif not obj["exists_in_snowflake"]:
                st.info(
                    "Not found in Snowflake. Either it is not built yet, or the name in "
                    "the FDD does not match what exists. Columns below come from the "
                    "document only."
                )
            if st.checkbox("Include this object", value=obj.get("include", True),
                           key=f"fddimp_obj_{index}_{int(obj.get('include', True))}") \
                    != obj.get("include", True):
                obj["include"] = not obj.get("include", True)
                st.rerun()
            if obj.get("include", True):
                keep.append(index)

            if obj["columns"]:
                frame = pd.DataFrame([
                    {
                        "Column": c["column_name"],
                        "FDD type": c["pasted_type"],
                        "Snowflake type": c["live_type"],
                        "Status": FLAG_ICON.get(c["flag"], c["flag"]),
                        "Description": c["business_description"],
                    }
                    for c in obj["columns"]
                ])
                st.dataframe(frame, use_container_width=True, hide_index=True)
                shown = {c["flag"] for c in obj["columns"]} - {"match"}
                for flag in sorted(shown):
                    st.caption(f"**{FLAG_ICON[flag]}** — {FLAG_HELP[flag]}")
            else:
                st.caption("No columns were parsed for this object.")

            _render_fdd_sample(obj, index)

            st.caption(
                f"Columns import as {fi.DEFAULT_COLUMN_ORIGIN}. Mark the genuinely "
                "derived ones on the Object columns page afterwards - defaulting to "
                "DERIVED is what previously put 78 straight pulls in as rule gaps."
            )
    return keep


def _render_fdd_sample(obj: Dict, index: int) -> None:
    """Sample rows the FDD carries under this object (sample_data.py)."""
    held = obj.get("fdd_sample")
    if not held:
        return
    try:
        import sample_data as sd
    except Exception:
        return
    if not sd.SAMPLE_DATA_ENABLED:
        return
    rows = sd.rows_from_fdd(held["columns"], held["rows"])
    if obj.get("exists_in_snowflake"):
        obj["keep_fdd_sample"] = st.checkbox(
            f"Keep the {len(rows)} sample row(s) from the FDD as a manual sample",
            value=bool(obj.get("keep_fdd_sample")), key=f"fddimp_keep_sample_{index}",
            help="The object is in Snowflake, so live rows are shown by default. Tick to "
                 "keep the FDD's rows as well, e.g. as the author's intended example.")
    else:
        st.caption(f"**{len(rows)} sample row(s) from the FDD** will be saved as this "
                   "object's sample (marked \"from the FDD - not from Snowflake\").")
    st.dataframe(pd.DataFrame(rows, columns=held["columns"]).head(10),
                 use_container_width=True, hide_index=True)


LEVEL_LABEL = {"fix": "Fix", "check": "Check", "note": "Note"}


def _status(entry: Dict) -> str:
    """Current status, counting the problems the reviewer has already dealt with."""
    return fi.entry_status(entry)


def _refresh_summary(plan: Dict) -> None:
    rules = plan.get("rules", [])
    for entry in rules:
        entry["status"] = _status(entry)
    plan["summary"]["status_counts"] = {
        k: sum(1 for r in rules if r.get("status") == k) for k in ("green", "amber", "red")}
    plan["summary"]["rules_with_errors"] = sum(1 for r in rules if r.get("errors"))
    plan["summary"]["rules"] = len(rules)


def _include(entry: Dict) -> bool:
    """Written by this import: every rule that is ready. There is no opt-out - the
    import waits until each rule is ready (see fi.coverage)."""
    if _skipped(entry) or entry.get("_hop_off"):
        return False
    return fi.entry_covered(entry)


def _kept(rules: List[Dict]) -> List[int]:
    # Read from the plan, not from checkbox widgets: rules hidden by the filter or by
    # one-at-a-time mode have no widget on screen, and must keep their state.
    return [i for i, entry in enumerate(rules) if _include(entry)]


def _open(entry: Dict) -> List[Dict]:
    return rx.open_issues(entry.get("issues") or [], entry.get("resolutions"))


def _render_rule(plan: Dict, index: int, expanded: bool = False) -> None:
    if st.session_state.get("fddimp_new_layout"):
        _render_rule_card(plan, index, expanded)
        return
    rules = plan["rules"]
    entry = rules[index]
    status = _status(entry)
    edited = _is_rule_edited(rules, index)
    open_issues = _open(entry)
    headline = (entry.get("proposal") or {}).get("rule_name") or entry["text"][:70]
    fixes = sum(1 for i in open_issues if i["level"] == "fix")
    checks = len(open_issues) - fixes
    tally = ", ".join(x for x in (f"{fixes} to fix" if fixes else "",
                                  f"{checks} to check" if checks else "") if x)
    mark = "edited" if edited else STATUS_LABEL.get(status, status)
    with st.expander(f"{entry['rule_number'] or '-'}  ·  {headline}  ·  {mark}"
                     + (f" ({tally})" if tally else ""), expanded=expanded or edited):
        if entry.get("origin"):
            st.caption(f"From the workbook: {entry['origin']}")
        if entry.get("prior"):
            _render_prior_choice(plan, index)
        if entry.get("context", {}).get("section"):
            st.caption(f"Under heading: {entry['context']['section'][:160]}")
        st.text_area("Rule text",
                     value=st.session_state.get(f"fddimp_rule_text_{index}", entry["text"]),
                     key=f"fddimp_rule_text_{index}",
                     height=max(68, min(200, len(entry["text"]) // 2)),
                     label_visibility="collapsed",
                     help="Change the text and click away: only this rule is read again.",
                     on_change=_on_text_change, args=(plan, index))
        handled = entry.get("resolutions") or {}
        if handled and st.button(f"Undo ({len(handled)} handled)",
                                 key=f"fddimp_undo_{index}"):
            _undo(plan, index)
            st.session_state["fddimp_open"] = index
            st.rerun()

        if not open_issues and "issues" not in entry:
            for message in entry.get("errors") or []:
                st.error(message)
            for message in entry.get("reasons") or entry.get("warnings") or []:
                st.warning(message)
        for issue in open_issues[:3]:
            _render_issue(plan, index, issue)
        if len(open_issues) > 3:
            with st.expander(f"+{len(open_issues) - 3} more"):
                for issue in open_issues[3:]:
                    _render_issue(plan, index, issue)
        if handled:
            kinds = [h.get("how") for h in handled.values()]
            st.caption("Handled: " + ", ".join(
                f"{kinds.count(k)} {k}" for k in ("auto-fixed", "fixed", "accepted", "noted")
            if k in kinds))

        if entry.get("preview"):
            with st.expander("How it will read in the FDD", expanded=False):
                st.markdown(entry["preview"])

        _render_readiness(plan, index)


def _render_issue(plan: Dict, index: int, issue: Dict) -> None:
    """One line per problem: what is wrong, the fix, ignore. Everything else in Details."""
    entry = plan["rules"][index]
    key = f"fddimp_{index}_{issue['id']}"
    cols = st.columns([6, 3, 1.4, 1.4])
    cols[0].markdown(f"**{LEVEL_LABEL.get(issue['level'], issue['level'])}**  ·  "
                     f"{issue['title']}")
    suggestion = issue.get("suggestion")
    if suggestion and cols[1].button(suggestion["label"][:40], key=key + "_fix",
                                     type="primary"):
        _apply(plan, index, issue, suggestion["patch"])
        st.rerun()
    is_note = issue["level"] == "note"
    if cols[2].button("Ignore", key=key + "_accept",
                      help="Keep the rule as it is; the problem goes on the review list."):
        _resolve(entry, issue, "noted" if is_note else "accepted")
        _refresh_summary(plan)
        st.session_state["fddimp_open"] = index
        st.rerun()
    choices = issue.get("choices")
    detail = issue.get("detail") and issue["detail"] != issue["title"]
    if detail or choices or issue.get("remove"):
        with cols[3].popover("More"):
            if detail:
                st.write(issue["detail"])
            if choices and choices.get("options"):
                picked = st.selectbox(choices["label"], choices["options"], key=key + "_pick")
                if st.button("Use this", key=key + "_use"):
                    _apply(plan, index, issue, dict(choices["patch"], value=picked))
                    st.rerun()
            if issue.get("remove") and st.button("Remove this part of the rule",
                                                 key=key + "_remove"):
                _apply(plan, index, issue, issue["remove"])
                st.rerun()


# ------------------------------------------------------------------
# Completeness and the new rule card (preview layout)
# ------------------------------------------------------------------
def _forget_old_skips(plan: Dict) -> None:
    """Reviews saved before Skip was removed: their skipped rules come back to review."""
    for entry in plan.get("rules") or []:
        entry.pop("user_skip", None)
        entry.pop("include_choice", None)


def _render_coverage(plan: Dict, gaps: Dict) -> None:
    """Rules covered: n of N, what is still open, and the lines the parser could not place."""
    rules = plan.get("rules") or []
    done = len(gaps["covered"])
    text = f"**Rules covered: {done} of {gaps['total']}**"
    if gaps["deferred"]:
        text += f"  ·  {len(gaps['deferred'])} on hops left for a later part"
    if gaps["complete"]:
        st.success(text + "  ·  every rule is ready.")
    else:
        bits = []
        if gaps["open"]:
            bits.append(f"{len(gaps['open'])} rule(s) still need fixing: " + ", ".join(
                str(rules[i].get("rule_number") or "-") for i in gaps["open"][:20])
                + ("…" if len(gaps["open"]) > 20 else ""))
        if gaps["open_lines"]:
            bits.append(f"{len(gaps['open_lines'])} FDD line(s) not placed yet")
        st.warning(text + "  ·  " + "; ".join(bits)
                   + ". Every rule must be fixed or saved as written before importing.")
    if plan.get("unparsed"):
        with st.expander(f"FDD lines the parser could not place "
                         f"({len(gaps['open_lines'])} to decide)",
                         expanded=bool(gaps["open_lines"])):
            _render_leftover_choices(plan, "cov")


def _render_leftover_choices(plan: Dict, where: str) -> None:
    """Each unplaced line: make it a rule on a hop, or mark it as not a rule with a reason.
    Sample rows and column lists are settled already and listed at the end."""
    lines = plan.get("unparsed") or []
    choices = plan.setdefault("leftover_choices", {})
    specs = plan.get("rule_sets") or []
    samples = [n for n, line in enumerate(lines)
               if fi.is_sample_line(line) and str(n) not in choices]
    waiting = [n for n in range(len(lines)) if n not in samples and str(n) not in choices]
    if len(waiting) > 1:
        c1, c2 = st.columns([5, 2], vertical_alignment="bottom")
        why_all = c1.text_input("Reason for all the lines still open",
                                key=f"fddimp_left_{where}_allwhy",
                                placeholder="e.g. headings and notes, no logic")
        if c2.button(f"Mark {len(waiting)} as not a rule", key=f"fddimp_left_{where}_all",
                     disabled=not why_all.strip()):
            for n in waiting:
                choices[str(n)] = {"how": "not_a_rule", "reason": why_all.strip()}
            st.rerun()
    for n, line in enumerate(lines):
        if n in samples:
            continue
        key = f"fddimp_left_{where}_{n}"
        with st.container(border=True):
            st.markdown(f"`{n + 1}` {line}")
            choice = choices.get(str(n))
            if choice:
                how = ("Made into a rule" if choice.get("how") == "rule"
                       else f"Not a rule - {choice.get('reason') or 'no reason'}")
                left, right = st.columns([6, 1])
                left.caption(how)
                if choice.get("how") == "not_a_rule" and right.button("Undo", key=key + "_undo"):
                    choices.pop(str(n), None)
                    st.rerun()
                continue
            c1, c2, c3, c4 = st.columns([3, 1.3, 3, 1.3], vertical_alignment="bottom")
            if specs:
                hop = c1.selectbox(
                    "Hop", list(range(len(specs))), key=key + "_hop",
                    format_func=lambda i: f"{_leaf(specs[i].get('source_technical_object')) or '-'}"
                                          f" → {_leaf(specs[i].get('target_technical_object')) or '-'}")
                if c2.button("Make a rule", key=key + "_rule", type="primary",
                             help="Reads this line as a rule on the chosen hop (uses the model)."):
                    with st.spinner("Reading the line..."):
                        _make_rule_from_line(plan, n, hop)
                    st.rerun()
            reason = c3.text_input("Why it is not a rule", key=key + "_why",
                                   placeholder="e.g. heading, note, column list")
            if c4.button("Not a rule", key=key + "_not", disabled=not reason.strip(),
                         help="Saved as a review comment on the module, with the reason."):
                choices[str(n)] = {"how": "not_a_rule", "reason": reason.strip()}
                st.rerun()
    if samples:
        with st.popover(f"{len(samples)} sample row(s) and column list(s) - settled"):
            st.caption("Sample data and column lists, not rules. Make one a rule if needed.")
            for n in samples:
                left, right = st.columns([6, 1.4])
                left.caption(f"`{n + 1}` {lines[n][len(fi.SAMPLE_TAG):].strip()[:160]}")
                if specs and right.button("Make a rule", key=f"fddimp_left_{where}_{n}_srule"):
                    with st.spinner("Reading the line..."):
                        _make_rule_from_line(plan, n, 0)
                    st.rerun()


def _make_rule_from_line(plan: Dict, n: int, set_index: int) -> None:
    line = plan["unparsed"][n]
    spec = plan["rule_sets"][set_index]
    entry = {"rule_number": "", "text": line, "original_text": line,
             "rule_set_index": set_index,
             "source": spec.get("source_technical_object") or "",
             "target": spec.get("target_technical_object") or "",
             "proposal": None, "preview": "", "errors": [], "warnings": [],
             "used_model": False, "sources": spec.get("sources") or None,
             "context": {}, "status": "green", "reasons": [],
             "origin": "An FDD line the parser could not place"}
    plan["rules"].append(entry)
    index = len(plan["rules"]) - 1
    spec.setdefault("rule_indexes", []).append(index)
    plan.setdefault("leftover_choices", {})[str(n)] = {"how": "rule", "rule_index": index}
    _recheck(plan, index)
    rx.auto_resolve(entry, lambda proposal, e=entry: _regrade(e, proposal))
    _refresh_summary(plan)
    st.session_state["fddimp_open"] = index


def _render_readiness(plan: Dict, index: int) -> None:
    """Instead of an Include box: whether the rule is ready, and what stops it."""
    entry = plan["rules"][index]
    if _skipped(entry):
        st.caption(":green[Already in the module - kept as saved.]")
    elif entry.get("_hop_off"):
        st.caption(":grey[On a hop left for a later part.]")
    elif fi.entry_covered(entry):
        st.caption(":green[Ready - will be imported.]")
    else:
        st.caption(f":orange[Not ready yet: {_top_problem(entry)}]")
        # Older warnings carry no issue to Ignore: let the reviewer accept them.
        if (entry.get("proposal") and not entry.get("errors") and not _open(entry)
                and st.button("Accept as it is", key=f"fddimp_accept_{index}")):
            entry["accepted_as_is"] = True
            _refresh_summary(plan)
            st.rerun()


def _render_rule_card(plan: Dict, index: int, expanded: bool = False) -> None:
    """One rule as View / Edit / FDD text, then its problems - like Update a rule."""
    rules = plan["rules"]
    entry = rules[index]
    open_issues = _open(entry)
    headline = (entry.get("proposal") or {}).get("rule_name") or entry["text"][:70]
    fixes = sum(1 for i in open_issues if i["level"] == "fix")
    checks = len(open_issues) - fixes
    tally = ", ".join(x for x in (f"{fixes} to fix" if fixes else "",
                                  f"{checks} to check" if checks else "") if x)
    mark = ("edited" if entry.get("hand_edited") or _is_rule_edited(rules, index)
            else STATUS_LABEL.get(_status(entry), _status(entry)))
    with st.expander(f"{entry['rule_number'] or '-'}  ·  {headline}  ·  {mark}"
                     + (f" ({tally})" if tally else ""), expanded=expanded):
        where = [x for x in (entry.get("origin") and f"From the workbook: {entry['origin']}",
                             (entry.get("context") or {}).get("section")
                             and f"Under heading: {entry['context']['section'][:160]}") if x]
        if where:
            st.caption("  ·  ".join(where))
        if entry.get("prior"):
            _render_prior_choice(plan, index)

        view, edit, text = st.tabs(["View", "Edit", "FDD text"])
        with view:
            if entry.get("preview"):
                st.markdown(entry["preview"])
            else:
                st.caption("Nothing could be read from this rule yet - fix it in **Edit**, "
                           "change the **FDD text**, or save it as written.")
        with edit:
            _render_card_editor(plan, index)
        with text:
            st.caption("The FDD's sentence. Changing it reads the rule again with the "
                       "model and replaces any edits made in Edit.")
            st.text_area("FDD text",
                         value=st.session_state.get(f"fddimp_rule_text_{index}", entry["text"]),
                         key=f"fddimp_rule_text_{index}",
                         height=max(90, min(220, len(entry["text"]) // 2)),
                         label_visibility="collapsed",
                         on_change=_on_text_change, args=(plan, index))
            if not aw.is_as_written(entry.get("proposal") or {}) and st.button(
                    "Save as written", key=f"fddimp_card_aw_{index}",
                    help="Keep this sentence word for word as the rule."):
                aw.make_entry_as_written(entry)
                _refresh_summary(plan)
                st.rerun()

        if not open_issues and "issues" not in entry:
            for message in entry.get("errors") or []:
                st.error(message)
            for message in entry.get("reasons") or entry.get("warnings") or []:
                st.caption(f":orange[{message}]")
        if open_issues:
            st.markdown(f"**Needs your attention ({len(open_issues)})**")
            for issue in open_issues:
                _render_issue_compact(plan, index, issue)
        handled = entry.get("resolutions") or {}
        if handled or entry.get("original_proposal") is not None:
            kinds = [h.get("how") for h in handled.values()]
            done = ", ".join(f"{kinds.count(k)} {k}" for k in
                             ("auto-fixed", "fixed", "accepted", "noted") if k in kinds)
            if entry.get("hand_edited"):
                done = ", ".join(x for x in (done, "edited by hand") if x)
            left, right = st.columns([6, 1])
            left.caption("Handled: " + (done or "changes made"))
            if right.button("Undo", key=f"fddimp_card_undo_{index}"):
                _undo(plan, index)
                st.session_state["fddimp_open"] = index
                st.rerun()
        _render_readiness(plan, index)


def _render_card_editor(plan: Dict, index: int) -> None:
    entry = plan["rules"][index]
    proposal = entry.get("proposal")
    if not proposal:
        st.caption("There is no rule to edit yet. Change the **FDD text**, or save it as "
                   "written from that tab.")
        return
    k = f"fddimp_ed_{index}_{entry.get('edit_rev', 0)}"
    edited = rule_editor.render_fields(proposal, k)
    if st.button("Check changes", type="primary", key=k + "_check",
                 help="Checks the edited rule here, without the model."):
        message = _apply_manual_edit(plan, index, edited)
        if message:
            st.info(message)
        else:
            st.rerun()


def _apply_manual_edit(plan: Dict, index: int, edited: Dict) -> str:
    """Put the editor's values into the rule and re-check it. Returns a message when
    nothing was applied (no change, or key columns that could not be read)."""
    entry = plan["rules"][index]
    current = entry.get("proposal") or {}
    proposal, problems = rc.apply_edits(current, edited)
    if problems:
        return "Not applied: " + "; ".join(problems)
    if not rc.content_changed(current, proposal):
        return "No changes."
    entry.setdefault("original_proposal", copy.deepcopy(current))
    if aw.is_as_written(proposal):
        entry["proposal"] = proposal
        entry["preview"] = aw.preview(proposal.get("business_description"))
    else:
        _regrade(entry, proposal)
    entry["hand_edited"] = True
    entry["edit_rev"] = entry.get("edit_rev", 0) + 1
    _refresh_summary(plan)
    st.session_state["fddimp_open"] = index
    return ""


def _render_issue_compact(plan: Dict, index: int, issue: Dict) -> None:
    """Problem text full width; the fix, Ignore and a single ⋯ menu beside it."""
    entry = plan["rules"][index]
    key = f"fddimp_c_{index}_{issue['id']}"
    cols = st.columns([7, 3, 1.2, 0.8], vertical_alignment="center")
    cols[0].markdown(f"**{LEVEL_LABEL.get(issue['level'], issue['level'])}** · {issue['title']}")
    suggestion = issue.get("suggestion")
    if suggestion and cols[1].button(suggestion["label"][:40], key=key + "_fix",
                                     type="primary", use_container_width=True):
        _apply(plan, index, issue, suggestion["patch"])
        st.rerun()
    if cols[2].button("Ignore", key=key + "_accept",
                      help="Keep the rule as it is; the problem goes on the review list."):
        _resolve(entry, issue, "noted" if issue["level"] == "note" else "accepted")
        _refresh_summary(plan)
        st.session_state["fddimp_open"] = index
        st.rerun()
    choices = issue.get("choices")
    detail = issue.get("detail") and issue["detail"] != issue["title"]
    if detail or choices or issue.get("remove"):
        with cols[3].popover("⋯"):
            if detail:
                st.write(issue["detail"])
            if choices and choices.get("options"):
                picked = st.selectbox(choices["label"], choices["options"], key=key + "_pick")
                if st.button("Use this", key=key + "_use"):
                    _apply(plan, index, issue, dict(choices["patch"], value=picked))
                    st.rerun()
            if issue.get("remove") and st.button("Remove this part of the rule",
                                                 key=key + "_remove"):
                _apply(plan, index, issue, issue["remove"])
                st.rerun()


def _resolve(entry: Dict, issue: Dict, how: str, reason: str = "") -> None:
    entry.setdefault("resolutions", {})[issue["id"]] = {
        "how": how, "reason": reason, "title": issue["title"],
        "detail": issue.get("detail", ""), "level": issue["level"]}


def _regrade(entry: Dict, proposal: Dict) -> None:
    result = rc.regrade(entry["text"], proposal,
                        sources=entry.get("sources") or [entry.get("source", "")],
                        target=entry.get("target", ""), context=entry.get("context"),
                        model_issues=entry.get("model_issues") or [])
    entry["proposal"] = result["proposal"]
    entry["errors"] = result["errors"]
    entry["warnings"] = result["warnings"]
    entry["issues"] = result["issues"]
    entry["preview"] = rc.preview_of(result["proposal"], entry.get("source", ""),
                                     entry.get("target", ""))


def _apply(plan: Dict, index: int, issue: Dict, patch: Dict) -> None:
    """Apply one fix in place and re-check the rule without a model call."""
    entry = plan["rules"][index]
    entry.setdefault("original_proposal", copy.deepcopy(entry.get("proposal")))
    _regrade(entry, rx.apply_patch(entry.get("proposal"), patch))
    _resolve(entry, issue, "fixed")
    _settle_related(entry, patch)
    _refresh_summary(plan)
    st.session_state["fddimp_open"] = index


def _settle_related(entry: Dict, patch: Dict) -> None:
    """Close the model's doubts the fix answered; FDD notes are kept as notes, so the
    FDD author still hears about the gap."""
    for other in rx.related_model_issues(entry.get("issues") or [], patch,
                                         entry.get("resolutions")):
        _resolve(entry, other, "noted" if other["kind"] == "fdd" else "fixed",
                 "settled by a fix on this rule")


def _on_text_change(plan: Dict, index: int) -> None:
    if _is_rule_edited(plan["rules"], index):
        # Edited text is read again; a previous "save as written" no longer applies.
        for key in ("as_written", "as_written_notes", "before_as_written"):
            plan["rules"][index].pop(key, None)
        _recheck(plan, index)
        entry = plan["rules"][index]
        rx.auto_resolve(entry, lambda proposal: _regrade(entry, proposal))
        _refresh_summary(plan)
        st.session_state["fddimp_open"] = index


def _render_hop_picker(plan: Dict, rule_sets: List[Dict]) -> None:
    """Which hops go in now. Unticked hops are left for a later part: paste the same
    text into the module again and whatever is saved by then is skipped."""
    rules = plan["rules"]
    on = sum(1 for sp in rule_sets if sp.get("import_now", True))
    with st.expander(f"Hops in this part: importing {on} of {len(rule_sets)}",
                     expanded=on != len(rule_sets)):
        for set_index, spec in enumerate(rule_sets):
            label = (f"{_leaf(spec.get('source_technical_object')) or '(no source)'} -> "
                     f"{_leaf(spec.get('target_technical_object')) or '(no target)'}  "
                     f"({len(spec.get('rule_indexes', []))} rule(s)"
                     + (", hop already saved" if spec.get("already_saved") else "") + ")")
            now = st.checkbox(label, value=spec.get("import_now", True),
                              key=f"fddimp_hop_now_{set_index}_{int(spec.get('import_now', True))}")
            if now != spec.get("import_now", True):
                spec["import_now"] = now
                for i in spec.get("rule_indexes", []):
                    rules[i]["_hop_off"] = not now
                st.rerun()


def _skipped(entry: Dict) -> bool:
    return bool(entry.get("prior")) and entry.get("prior_choice") == "skip"


def _needs(entry: Dict) -> bool:
    """On the review list: an open problem, or a saved rule the FDD has changed since."""
    return _status(entry) != "green" or (
        (entry.get("prior") or {}).get("state") == fi.CHANGED and not entry.get("prior_seen"))


PRIOR_LABEL = {"skip": "Keep the saved rule (skip)", "update": "Update the saved rule",
               "new": "Add as a new rule"}


def _render_prior_choice(plan: Dict, index: int, compact: bool = False) -> None:
    entry = plan["rules"][index]
    prior = entry["prior"]
    state = {fi.SAME: "Already imported", fi.CHANGED: "Changed in the FDD since it was imported",
             fi.SAVED_NO_TEXT: "Same rule number already saved"}[prior["state"]]
    cols = st.columns([5, 3])
    cols[0].markdown(f"**{state}**  ·  rule {entry.get('rule_number') or '-'} → saved id "
                     f"{prior['rule_id']} ({prior.get('rule_name') or ''})"[:200])
    if prior["state"] == fi.CHANGED and not compact:
        with cols[0].popover("What changed?"):
            st.caption("Saved from:")
            st.write(prior.get("fdd_text") or "-")
            st.caption("Now in the FDD:")
            st.write(entry.get("text") or "-")
    options = list(PRIOR_LABEL)
    picked = cols[1].selectbox("What to do", options,
                               index=options.index(entry.get("prior_choice", "skip")),
                               format_func=PRIOR_LABEL.get, label_visibility="collapsed",
                               key=f"fddimp_prior_{index}_{entry.get('prior_choice')}")
    if picked != entry.get("prior_choice"):
        entry["prior_choice"] = picked
        entry["prior_seen"] = True
        st.rerun()
    if prior["state"] == fi.CHANGED and not entry.get("prior_seen") and cols[1].button(
            "Confirm", key=f"fddimp_prior_ok_{index}",
            help="Use the choice above for this rule."):
        entry["prior_seen"] = True
        st.rerun()


def _team_modules() -> Dict[str, int]:
    df = get_modules()
    if df is None or df.empty:
        return {}
    team = st.session_state.get("_team_id")
    if team is not None and "TEAM_ID" in df:
        df = df[df["TEAM_ID"] == team]
    return {str(r["MODULE_NAME"]): int(r["MODULE_ID"]) for _, r in df.iterrows()}


def _saved_rows(module_id: int) -> List[Dict]:
    try:
        df = list_module_rules(module_id)
    except Exception:
        return []
    return [] if df is None or df.empty else df.to_dict("records")


def _mark_parts(plan: Dict, module_id: int) -> None:
    """Compare this part with what the module already holds (see fi.mark_already_imported)."""
    hops = fi.saved_hops(_saved_rows(module_id))
    end = fi.last_saved_target(hops)
    continued = fi.continue_chain(plan, end)
    counts = fi.mark_already_imported(plan, hops)
    plan["parts"] = {"module_id": module_id, "continued_from": end if continued else "",
                     **counts}


def _render_progress(module_id: int) -> None:
    """What the module holds so far, in pipeline order, so the next part is obvious."""
    from lineage_diagram import LAYER_ORDER, LAYER_TITLES, object_layer
    hops = fi.saved_hops(_saved_rows(module_id))
    if not hops:
        st.caption("Nothing saved in this module yet.")
        return
    rank = lambda t: LAYER_ORDER.index(object_layer(t)) if object_layer(t) in LAYER_ORDER else -1
    rows = sorted(({"From": _leaf(s) or "-", "To": _leaf(t) or "-",
                    "Layer": LAYER_TITLES.get(object_layer(t), object_layer(t)),
                    "Rules": len(h["rules"]), "_r": rank(t)}
                   for (s, t), h in hops.items()), key=lambda r: (r["_r"], r["To"]))
    have = {object_layer(t) for _, t in hops}
    gaps = [LAYER_TITLES.get(l, l) for l in LAYER_ORDER[1:] if l not in have]
    with st.expander(f"Already in this module: {len(hops)} hop(s), "
                     f"{sum(r['Rules'] for r in rows)} rule(s)", expanded=False):
        st.dataframe(pd.DataFrame(rows).drop(columns="_r"), hide_index=True,
                     use_container_width=True)
        end = fi.last_saved_target(hops)
        if end:
            st.caption(f"The chain currently ends at `{_leaf(end)}`. A part that starts "
                       "without a source continues from there.")
        if gaps:
            st.caption("No hop yet into: " + ", ".join(gaps))


def _undo(plan: Dict, index: int) -> None:
    entry = plan["rules"][index]
    if entry.get("original_proposal") is not None:
        original = entry.pop("original_proposal")
        if aw.is_as_written(original or {}):
            entry["proposal"] = original
            entry["preview"] = aw.preview((original or {}).get("business_description"))
        else:
            _regrade(entry, original)
    entry["resolutions"] = {}
    entry.pop("hand_edited", None)
    entry.pop("accepted_as_is", None)
    entry["edit_rev"] = entry.get("edit_rev", 0) + 1
    _refresh_summary(plan)


def _recheck(plan: Dict, index: int) -> None:
    """Read one rule again with the model, using its current text and hop context."""
    entry = plan["rules"][index]
    new_text = st.session_state.get(f"fddimp_rule_text_{index}", entry["text"]).strip()
    entry["text"] = new_text
    entry["original_text"] = new_text
    entry["resolutions"] = {}
    entry.pop("original_proposal", None)
    entry.pop("include_choice", None)

    if fi.is_no_transformation(new_text):
        entry.update({
            "proposal": fi.no_transformation_proposal(new_text, entry["rule_number"]),
            "preview": "**No transformation** — records are carried through unchanged.",
            "errors": [], "warnings": [], "used_model": False,
            "issues": [], "model_issues": [], "reasons": []})
    else:
        result = _compile_with_effort(
            new_text, entry["rule_number"], entry.get("source", ""),
            entry.get("target", ""), st.session_state.get("fddimp_effort", "accurate"),
            {"model": 0}, sources=entry.get("sources"), context=entry.get("context"))
        entry.update({
            "proposal": result.get("proposal"),
            "preview": result.get("preview", ""),
            "errors": list(result.get("errors") or []),
            "warnings": list(result.get("warnings") or []),
            "used_model": bool(result.get("used_model", True)),
            "issues": list(result.get("issues") or []),
            "model_issues": list(result.get("model_issues") or []),
            "reasons": list(result.get("reasons") or [])})
    _discover_new_objects(plan)
    _refresh_summary(plan)
    plan["summary"]["objects_to_create"] = sum(
        1 for o in plan["objects"] if o["action"] == "create")


def _is_rule_edited(rules: list, index: int) -> bool:
    """True when the user changed the text of this rule in the text_area."""
    key = f"fddimp_rule_text_{index}"
    if key not in st.session_state:
        return False
    original = rules[index].get("original_text", rules[index]["text"])
    return st.session_state[key].strip() != original.strip()


def _discover_new_objects(plan: Dict) -> None:
    """Add objects referenced in rule texts but not yet in the plan's object list."""
    import fdd_import as fi

    known = {str(o.get("technical_object_name", "")).upper()
             for o in plan.get("objects", [])}

    new_names: List[str] = []
    for rule in plan.get("rules", []):
        for name in fi.TECHNICAL_NAME.findall(str(rule.get("text", "")).upper()):
            if name not in known:
                known.add(name)
                new_names.append(name)

    if not new_names:
        return

    for technical in new_names:
        leaf = technical.rsplit(".", 1)[-1]
        object_type = "VIEW" if leaf.startswith("V_") else "BASE_TABLE"

        live_cols: List[Dict] = []
        lookup_error = None
        try:
            live_cols = _live_columns(technical) or []
        except Exception as exc:
            lookup_error = str(exc)
        if not live_cols and lookup_error is None:
            try:
                lookup_error = live_column_failure(technical)
            except Exception:
                lookup_error = None

        comparison = fi.compare_columns([], live_cols)
        plan["objects"].append({
            "object_name": fi._object_name_from(technical),
            "technical_object_name": technical,
            "object_type": object_type,
            "layer": "discovered",
            "source_line": "(discovered from edited rule text)",
            "action": "create",
            "object_id": None,
            "exists_in_snowflake": bool(live_cols),
            "lookup_error": lookup_error,
            "columns": comparison,
            "counts": {
                flag: sum(1 for c in comparison if c["flag"] == flag)
                for flag in ("match", "type_mismatch", "pasted_only", "snowflake_only")
            },
        })

    plan["summary"]["columns"] = sum(len(o["columns"]) for o in plan["objects"])


# ------------------------------------------------------------------
# Upload an .xlsx FDD (xlsx_import.py)
# ------------------------------------------------------------------
def _render_xlsx_upload() -> None:
    """Upload the workbook instead of pasting: the chosen sheets are turned into the
    same text a paste gives, and put in the box below to check before parsing."""
    try:
        import xlsx_import as xi
    except Exception:
        return
    upload = st.file_uploader("FDD workbook (.xlsx)", type=["xlsx"], key="fddimp_xlsx")
    if upload is None:
        st.session_state.pop("fddimp_xlsx_state", None)
        st.session_state.pop("fddimp_xlsx_map", None)
        return
    data = upload.getvalue()
    state = st.session_state.get("fddimp_xlsx_state") or {}
    if state.get("name") != upload.name or state.get("size") != len(data):
        try:
            sheets, engine = xi.read_workbook(data)
        except xi.WorkbookError as exc:
            st.error(str(exc))
            return
        state = {"name": upload.name, "size": len(data), "sheets": sheets, "engine": engine}
        st.session_state["fddimp_xlsx_state"] = state
    sheets = state["sheets"]
    counts = {n: sum(1 for r in rows if any(c.strip() for c in r)) for n, rows in sheets.items()}
    defaults = xi.default_sheets(sheets)
    with st.expander(f"Sheets: {len(st.session_state.get('fddimp_xlsx_sheets') or defaults)} "
                     f"of {len(sheets)}", expanded=False):
        chosen = st.multiselect(
            "Sheets to import", list(sheets), default=defaults, label_visibility="collapsed",
            format_func=lambda n: f"{n} ({counts.get(n, 0)} rows)", key="fddimp_xlsx_sheets",
            help="Cover, revision and history sheets start unticked.")
    # The chosen sheets go straight into the text the FDD is read from - no extra
    # "use these sheets" step. Rebuilt only when the file or the choice changes, so
    # an edit to the pasted text is not overwritten on every rerun.
    # A list, not a tuple: saved reviews store this as JSON and must compare equal.
    marker = [upload.name, len(data), list(chosen)]
    held = st.session_state.get("fddimp_xlsx_map") or {}
    if chosen and held.get("marker") != marker:
        text, line_map = xi.sheets_to_text(sheets, chosen)
        st.session_state["fddimp_text"] = text
        st.session_state["fddimp_xlsx_map"] = {"text": text, "map": line_map,
                                               "file": upload.name, "marker": marker}


def _mark_origins(plan: Dict, text: str) -> None:
    """Workbook sheet and row of each rule, when the text came from an upload."""
    held = st.session_state.get("fddimp_xlsx_map")
    if not held:
        return
    try:
        import xlsx_import as xi
    except Exception:
        return
    # The text may have been edited after the upload; locate against what was parsed.
    for entry in plan.get("rules") or []:
        where = xi.locate(entry.get("text"), held["text"], held["map"])
        if where:
            entry["origin"] = where


# ------------------------------------------------------------------
# Saved reviews (fdd_draft.py, table FDD_IMPORT_DRAFT)
# ------------------------------------------------------------------
def _drafts():
    try:
        import fdd_draft
        return fdd_draft, fdd_draft.DraftStore.default()
    except Exception:
        return None, None


def _autosave(force: bool = False) -> None:
    """Save the review when it changed (at most every few seconds), or now if forced."""
    fd, store = _drafts()
    if store is None:
        return
    held = st.session_state.get("fddimp_draft") or {}
    if held.get("conflict"):
        return                      # wait for the user's choice in the draft bar
    payload = fd.capture(st.session_state)
    if not force and not fd.should_autosave(payload, held):
        return
    if not payload.get("fddimp_plan"):
        return
    try:
        saved = store.save(payload, draft_id=held.get("draft_id"),
                           version=held.get("version"),
                           team_id=st.session_state.get("_team_id"),
                           module_id=(payload["fddimp_plan"].get("parts") or {}).get("module_id"),
                           performed_by=_viewer(),
                           force=bool(held.get("force")))
        st.session_state["fddimp_draft"] = {**saved, "fingerprint": fd.fingerprint(payload),
                                            "at": time.time(),
                                            "saved_at": time.strftime("%H:%M:%S")}
    except fd.DraftConflict as exc:
        st.session_state["fddimp_draft"] = {**held, "conflict": str(exc)}
    except Exception as exc:
        st.session_state["fddimp_draft"] = {**held, "error": str(exc)[:200]}


def _render_draft_bar() -> None:
    """Saved status, Save progress, and what to do when someone else saved meanwhile."""
    fd, store = _drafts()
    if store is None:
        return
    held = st.session_state.get("fddimp_draft") or {}
    cols = st.columns([5, 2])
    if held.get("conflict"):
        cols[0].warning(f"This review was changed elsewhere: {held['conflict']} "
                        "Keep yours to overwrite it, or load the saved one.")
        a, b = st.columns(2)
        if a.button("Keep mine", key="fddimp_draft_keep"):
            st.session_state["fddimp_draft"] = {**held, "conflict": None, "force": True}
            _autosave(force=True)
            st.session_state["fddimp_draft"].pop("force", None)
            st.rerun()
        if b.button("Load the saved one", key="fddimp_draft_reload"):
            st.session_state["fddimp_resume_pending"] = held["draft_id"]
            st.rerun()
        return
    if held.get("error"):
        cols[0].caption(f":orange[Not saved: {held['error']}]")
    elif held.get("draft_id"):
        cols[0].caption(f"Saved {held.get('saved_at', '')} · draft {held['draft_id']} - "
                        "you can close the page and continue later.")
    if cols[1].button(":material/save: Save progress", key="fddimp_draft_save"):
        _autosave(force=True)
        st.toast("Review saved.")
        st.rerun()


def _render_saved_drafts() -> None:
    """Continue a saved review: the team's open drafts, newest first."""
    fd, store = _drafts()
    if store is None:
        return
    # Restored here, before any review widget exists on this run: Streamlit refuses a
    # new value for a widget it has already drawn.
    pending = st.session_state.pop("fddimp_resume_pending", None)
    if pending is not None:
        try:
            _resume(int(pending))
        except Exception as exc:
            st.error(f"Could not open the saved review: {exc}")
    if st.session_state.get("fddimp_plan"):
        return
    me = _viewer()
    show_team = st.session_state.get("fddimp_show_team_drafts", False)
    try:
        team_id = st.session_state.get("_team_id")
        drafts = store.list_open(team_id, owner=None if show_team or not me else me)
        others = 0
        if not show_team and me:
            everyone = store.list_open(team_id)
            others = 0 if everyone is None else len(everyone) - (0 if drafts is None else len(drafts))
    except Exception:
        return
    if (drafts is None or drafts.empty) and not others and not show_team:
        return
    title = "Continue a saved review" + ("" if show_team else " - mine")
    with st.expander(f"{title} ({0 if drafts is None else len(drafts)})", expanded=True):
        st.toggle("Show the team's drafts", key="fddimp_show_team_drafts",
                  help="Drafts saved by anyone in the team. Opening one is safe: if its "
                       "owner saves meanwhile, you are told rather than overwriting them.")
        if not show_team and others:
            st.caption(f"{others} more draft(s) by other people (or saved before names were "
                       "recorded) - turn on **Show the team's drafts** to see them.")
        for _, d in (drafts if drafts is not None else pd.DataFrame()).iterrows():
            cols = st.columns([6, 1.3, 1.3])
            when = pd.Timestamp(d["UPDATED_TS"]).strftime("%d %b %H:%M") \
                if pd.notna(d["UPDATED_TS"]) else ""
            cols[0].markdown(
                f"**{d['MODULE_NAME'] or '(no module)'}** · {d['TITLE']}  \n"
                f":grey[{int(d['RULE_COUNT'] or 0)} rules · {int(d['OPEN_ISSUES'] or 0)} open "
                f"problem(s) · created by {d['CREATED_BY']} · last saved by "
                f"{d['UPDATED_BY'] or d['CREATED_BY']} {when}]")
            if cols[1].button("Continue", key=f"fddimp_resume_{int(d['DRAFT_ID'])}",
                              type="primary"):
                st.session_state["fddimp_resume_pending"] = int(d["DRAFT_ID"])
                st.rerun()
            if cols[2].button("Discard", key=f"fddimp_discard_{int(d['DRAFT_ID'])}",
                              help="Marks it discarded; it is kept, not deleted."):
                store.set_status(int(d["DRAFT_ID"]), "DISCARDED", _viewer())
                st.rerun()


def _viewer():
    """The person using the app (set by streamlit_app from st.user), for every *_BY
    column this page writes. Without it the repository falls back to the session's
    viewer, and only outside a session to the service identity."""
    from repository import session_viewer
    return session_viewer()


def _resume(draft_id: int) -> None:
    fd, store = _drafts()
    loaded = store.load(draft_id)
    _clear_rule_text_state()
    fd.restore(st.session_state, loaded["payload"])
    st.session_state.pop("fddimp_done", None)
    st.session_state["fddimp_draft"] = {
        "draft_id": int(draft_id), "version": loaded["version"],
        "fingerprint": fd.fingerprint(fd.capture(st.session_state)), "at": time.time(),
        "saved_at": str(loaded["header"].get("UPDATED_TS") or "")[:16]}


def _close_draft(plan: Dict, written: Dict) -> None:
    """After an import: closed as IMPORTED, or kept open with the hops left for later."""
    fd, store = _drafts()
    held = st.session_state.get("fddimp_draft") or {}
    if store is None or not held.get("draft_id"):
        return
    try:
        if fd.remaining_after_import(plan):
            # What was imported is now in the module; the rest stays to be reviewed.
            for spec in plan.get("rule_sets") or []:
                now = spec.get("import_now", True)
                spec["import_now"] = not now
                spec["already_saved"] = spec.get("already_saved") or now
                for i in spec.get("rule_indexes", []):
                    plan["rules"][i]["_hop_off"] = now
            payload = fd.capture(st.session_state)
            payload["fddimp_plan"] = plan
            store.save(payload, draft_id=held["draft_id"], version=held.get("version"),
                       performed_by=_viewer(), force=True)
            written["draft_kept_open"] = held["draft_id"]
        else:
            store.set_status(held["draft_id"], "IMPORTED",
                             _viewer(),
                             rule_ids=written.get("rule_ids"))
    except Exception:
        pass
    st.session_state.pop("fddimp_draft", None)


def _clear_rule_text_state() -> None:
    """Remove all fddimp_rule_text_* keys from session state."""
    to_drop = [k for k in st.session_state if k.startswith("fddimp_rule_text_")
               or k.startswith("fddimp_rule_")]
    for k in to_drop:
        st.session_state.pop(k, None)


def _leaf(technical: str) -> str:
    """Object name without the database and schema, for compact display."""
    return str(technical or "").rsplit(".", 1)[-1]


def _render_review_comments(plan: Dict) -> None:
    comments = plan.get("review_comments", [])
    if not comments:
        st.success("No Open, Resolved or Note rows were found in the document.")
        return

    st.info(
        "These Open, Resolved and Note rows from the FDD will be saved as review "
        "comments on the module (ones it already has are skipped). Review them on the "
        "Requirements page after importing. Problems you accept or keep as notes on the "
        "Rules tab are added there too."
    )
    st.dataframe(
        pd.DataFrame([
            {
                "Status": c.get("status", "Open"),
                "Near object": _leaf(c.get("near_object", "")),
                "Text": c.get("text", ""),
            }
            for c in comments
        ]),
        use_container_width=True, hide_index=True,
    )


def _render_leftovers(plan: Dict) -> None:
    unparsed = plan.get("unparsed", [])
    orphans = plan.get("orphan_columns", [])

    if not unparsed and not orphans:
        st.success("Every line was placed. Nothing will be dropped.")
        return

    st.warning(
        "Shown so the gap is visible rather than silent. Columns with no object heading "
        "are not imported; adjust the document's headings and parse again if needed."
    )
    if unparsed:
        st.markdown(f"**{len(unparsed)} line(s) the parser could not place** - each one "
                    "must be made into a rule or marked as not a rule (see above Import).")
        st.code("\n".join(unparsed), language=None)
    if orphans:
        st.markdown(f"**{len(orphans)} column(s) with no object heading above them**")
        st.dataframe(
            pd.DataFrame([
                {"Column": c.get("column_name"), "Type": c.get("data_type"),
                 "From line": c.get("source_line")}
                for c in orphans
            ]),
            use_container_width=True, hide_index=True,
        )


def _render_result(written: Dict) -> None:
    """One line for what was written; one warning only if something was not."""
    parts = [f"{written.get('rules', 0)} rule(s)"]
    if written.get("rules_updated"):
        parts.append(f"{written['rules_updated']} updated")
    parts.append(f"{written.get('objects', 0)} new object(s)")
    if written.get("review_comments"):
        parts.append(f"{written['review_comments']} review comment(s)")
    st.success("Imported " + ", ".join(parts) + ". Rules are saved as DRAFT - review them "
               "on the Requirements page.")
    if written.get("draft_kept_open"):
        st.caption("The hops left for later stay in your saved review - continue it from "
                   "the top of this page.")

    skipped = written.get("skipped") or []
    lost = written.get("skipped_rule_sets") or []
    if skipped or lost:
        st.warning(f"{len(skipped)} rule(s) were not written because they failed validation"
                   + (f"; {len(lost)} hop(s) have no rules, so lineage has a gap there"
                      if lost else "") + ". Add them on Write a rule.")
        with st.expander("Which ones"):
            if skipped:
                st.dataframe(pd.DataFrame([{"Rule": s.get("rule_number"),
                                            "Rule set": s.get("rule_set"),
                                            "Reason": s.get("why")} for s in skipped]),
                             use_container_width=True, hide_index=True)
            if lost:
                st.dataframe(pd.DataFrame([{"From": _leaf(s.get("source")),
                                            "To": _leaf(s.get("target")),
                                            "Rules lost": s.get("rules")} for s in lost]),
                             use_container_width=True, hide_index=True)

    if st.button("Import another"):
        st.session_state.pop("fddimp_done", None)
        st.session_state.pop("fddimp_plan", None)
        st.session_state.pop("fddimp_draft", None)
        st.session_state.pop("fddimp_xlsx_map", None)
        _clear_rule_text_state()
        st.rerun()
