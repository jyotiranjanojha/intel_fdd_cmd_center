"""
Assembles a module's FDDs into one downloadable, self-contained HTML document:
lineage, the objects and column structure the module's rule sets touch, and
every rule in FDD-readable form.

This intentionally reuses rather than reimplements:
  - fdd_render.render_rule()      the same short rule text as every other page
  - md_to_html                    the markdown -> HTML converter (pure, unit tested)
  - catalog.scope_edges_to_module() the same lineage scoping as Object lineage
  - lineage_diagram.build_dot()    the same DOT the Object lineage page draws
  - lineage_diagram.lineage_ranks() the same pipeline ordering as Requirements
so a published document can never drift from what the app itself shows.

Lineage is rendered client-side. The app draws lineage with st.graphviz_chart(),
which renders in the browser with no graphviz binary or python package on the
server - see lineage_diagram.py. A static downloaded file has no server at all,
so the same DOT source is embedded here with a small script that renders it the
same way, the first time the file is opened with network access. If that fails
(no network), the diagram degrades to a message plus the raw DOT text rather
than breaking the rest of the document.
"""

import html as _html
from datetime import datetime
from typing import Dict, List

import pandas as pd

from catalog import get_lineage_edges, lineage_roots, scope_edges_to_module
from compliance import object_exists
from db import cached_read, tbl
from fdd_render import render_rule
from lineage_diagram import build_dot, lineage_ranks
from md_to_html import inline as _inline, rule_markdown_to_html
from repository import (get_module_objects, get_object_column_spec,
                        get_rule_sets_for_module, load_bundles_all)


def _column_spec_table_html(object_id: int) -> str:
    spec = get_object_column_spec(object_id)
    if spec.empty:
        return '<p class="muted">No columns declared yet.</p>'
    rows = []
    for _, r in spec.sort_values("ORDINAL").iterrows():
        origin = str(r["COLUMN_ORIGIN"] or "")
        source = (f"{r['SOURCE_OBJECT']}.{r['SOURCE_COLUMN']}"
                  if origin == "PASS_THROUGH" and r["SOURCE_OBJECT"] else "—")
        rows.append(
            f"<tr><td>{_inline(r['ORDINAL'])}</td><td><code>{_inline(r['COLUMN_NAME'])}</code></td>"
            f"<td>{_inline(r['DATA_TYPE'] or '')}</td>"
            f"<td>{'Y' if r['NULLABLE_IND'] else 'N'}</td>"
            f"<td>{'Y' if r['PRIMARY_KEY_IND'] else ''}</td>"
            f"<td>{_inline(origin)}</td><td>{_inline(source)}</td>"
            f"<td>{_inline(r['BUSINESS_DESCRIPTION'] or '')}</td></tr>"
        )
    return (
        '<table class="colspec"><thead><tr>'
        "<th>#</th><th>Column</th><th>Type</th><th>Null?</th><th>PK</th>"
        "<th>Origin</th><th>Source</th><th>Description</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _sample_html(technical_name: str) -> str:
    """Manual / FDD / drafted sample rows, when the feature is on and one is saved."""
    try:
        import sample_data_ui
        return sample_data_ui.sample_table_html(technical_name) if technical_name else ""
    except Exception:
        return ""


def _object_card_html(row: pd.Series) -> str:
    technical_name = str(row.get("TECHNICAL_OBJECT_NAME") or "")
    built_note = ""
    if technical_name:
        try:
            built = object_exists(technical_name)
            built_note = (
                '<span class="pill pill-ok">Built in Snowflake</span>' if built
                else '<span class="pill pill-warn">Not built yet — declared ahead of the table</span>'
            )
        except Exception:
            built_note = '<span class="pill pill-unknown">Build status unknown</span>'
    module_note = f" · module: {_inline(row.get('MODULE_NAME'))}" if row.get("MODULE_NAME") else ""
    return (
        f'<div class="object-card" id="obj-{int(row["OBJECT_ID"])}">'
        f'<h4>{_inline(row.get("OBJECT_NAME"))} {built_note}</h4>'
        f'<p class="muted"><code>{_inline(technical_name or "no technical name")}</code>'
        f' · {_inline(row.get("OBJECT_TYPE") or "object")}{module_note}</p>'
        + _column_spec_table_html(int(row["OBJECT_ID"]))
        + _sample_html(technical_name)
        + "</div>"
    )


def _lineage_section_html(edges: pd.DataFrame, module_name: str) -> str:
    if edges.empty:
        return '<p class="muted">No rule sets are linked into the lineage graph yet.</p>'
    roots = lineage_roots(edges)
    scoped_edges, boundary_ids, scoped_roots = scope_edges_to_module(
        edges, roots, module_name, stay_in_module=True
    )
    if scoped_edges.empty or not scoped_roots:
        return f'<p class="muted">No lineage recorded for {_inline(module_name)} yet.</p>'
    dot = build_dot(scoped_edges, [r["object_id"] for r in scoped_roots], dashed_ids=boundary_ids)
    dot_escaped = dot.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
    boundary_note = (
        "<p class=\"muted\">Dashed boxes are where the data leaves this module — "
        "their onward chain is not drawn.</p>" if boundary_ids else ""
    )
    return f"""
    <div id="lineage-diagram"></div>
    {boundary_note}
    <script src="https://unpkg.com/@viz-js/viz@3.4.0/lib/viz-standalone.js"></script>
    <script>
      (function() {{
        var dot = `{dot_escaped}`;
        var target = document.getElementById('lineage-diagram');
        if (typeof Viz === 'undefined') {{
          target.innerHTML = '<p class="muted">Diagram requires network access to render '
            + '(could not load the rendering library). Raw diagram source is below.</p>';
          return;
        }}
        Viz.instance().then(function(viz) {{
          target.appendChild(viz.renderSVGElement(dot));
        }}).catch(function(e) {{
          target.innerHTML = '<p class="muted">The diagram could not be rendered. '
            + 'Raw diagram source is below.</p>';
        }});
      }})();
    </script>
    <details><summary>Raw diagram source (DOT)</summary><pre>{_html.escape(dot)}</pre></details>
    """


def _rule_blocks_html(rule_ids: List[int]) -> str:
    """Renders a sequence of rules (already in the order they should appear)."""
    if not rule_ids:
        return '<p class="muted">No active rules target this object yet.</p>'
    bundles = load_bundles_all(rule_ids)
    by_id = {int(b["rule"].get("RULE_ID")): b for b in bundles if b.get("rule", {}).get("RULE_ID")}
    blocks = [f'<div class="rule">{rule_markdown_to_html(render_rule(by_id[rid], "grouped"))}</div>'
             for rid in rule_ids if rid in by_id]
    return "".join(blocks) or '<p class="muted">No active rules target this object yet.</p>'


_CSS = """
body { font-family: -apple-system, Segoe UI, Helvetica, Arial, sans-serif;
       max-width: 920px; margin: 2rem auto; padding: 0 1.5rem; color: #1a1a1a; line-height: 1.55; }
h1 { border-bottom: 3px solid #0068b5; padding-bottom: .5rem; }
h2 { margin-top: 2.5rem; border-bottom: 1px solid #ccc; padding-bottom: .3rem; color: #0068b5; }
h3 { margin-top: 1.8rem; }
h4 { margin-top: 1.2rem; margin-bottom: .3rem; }
.muted { color: #666; font-size: .92rem; }
.chip { color: #555; font-size: .88rem; margin: .2rem 0; }
.headline { font-weight: 600; margin: .5rem 0; }
.rationale { background: #f4f8fc; border-left: 3px solid #0068b5; padding: .5rem .8rem; }
.rule { border: 1px solid #e0e0e0; border-radius: 8px; padding: .8rem 1.2rem; margin: 1rem 0; }
.rule-bullets { padding-left: 1.2rem; }
table.mapping, table.colspec { border-collapse: collapse; width: 100%; margin: .6rem 0; font-size: .88rem; }
table.mapping th, table.mapping td, table.colspec th, table.colspec td {
  border: 1px solid #ddd; padding: .35rem .6rem; text-align: left; }
table.colspec th { background: #f5f5f5; }
code { background: #f0f0f0; padding: .1rem .3rem; border-radius: 4px; font-size: .9em; }
.pill { display: inline-block; font-size: .72rem; padding: .1rem .5rem; border-radius: 10px;
        margin-left: .5rem; vertical-align: middle; }
.pill-ok { background: #e6f4ea; color: #1a7431; }
.pill-warn { background: #fff3e0; color: #8a5300; }
.pill-unknown { background: #eee; color: #666; }
.object-card { border: 1px solid #e0e0e0; border-radius: 8px; padding: .8rem 1.2rem; margin: 1rem 0; }
.toc a { text-decoration: none; color: #0068b5; }
.toc li { margin: .25rem 0; }
footer { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid #ccc; color: #888; font-size: .82rem; }
details summary { cursor: pointer; color: #666; font-size: .85rem; }
details pre { font-size: .75rem; overflow-x: auto; background: #f7f7f7; padding: .6rem; }
"""


def build_module_document(module_id: int, module_name: str, viewer: str) -> str:
    """
    The full HTML document for one module: output objects (rule-set targets only —
    input/source-only objects are not included), each with its column structure and
    its rules in sequence, plus module-scoped lineage.
    """
    rule_sets = get_rule_sets_for_module(module_id)
    objects = get_module_objects(module_id)
    edges = get_lineage_edges()
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Every rule in the module, tagged with the target object of its rule set,
    # sorted so a target fed by more than one rule set still reads in one
    # consistent sequence rather than one block per rule set.
    rule_ids_by_object: Dict = {}
    if not rule_sets.empty:
        catalog_sql_ids = ",".join(str(int(r)) for r in rule_sets["RULE_SET_ID"])
        try:
            df = cached_read(f"""
                SELECT RULE_ID, RULE_SET_ID, PRIORITY, RULE_NUMBER
                FROM {tbl('RULE')}
                WHERE RULE_SET_ID IN ({catalog_sql_ids})
                  AND COALESCE(RULE_STATUS, '') <> 'RETIRED'
            """)
        except Exception:
            df = pd.DataFrame()
        if not df.empty:
            target_by_set = rule_sets.set_index("RULE_SET_ID")["TARGET_OBJECT_ID"]
            df["TARGET_OBJECT_ID"] = df["RULE_SET_ID"].map(target_by_set)
            df = df.sort_values(["PRIORITY", "RULE_NUMBER"])
            for tid, grp in df.groupby("TARGET_OBJECT_ID", dropna=False):
                key = int(tid) if pd.notna(tid) else None
                rule_ids_by_object[key] = [int(x) for x in grp["RULE_ID"].tolist()]

    # Output objects only - the objects this module's rule sets write to. Every
    # input/source-only object (never a target within this module) is excluded,
    # even though get_module_objects() would include it.
    #
    # Ordered by pipeline sequence (source -> ... -> target), the same
    # lineage_ranks() the Object lineage diagram and the Requirements page use -
    # not alphabetically by rule set name. Root table, then root view, then
    # stage, then whatever reads the stage, is the order the data actually
    # moves in; sorting rule sets by name breaks that (e.g. "Hash Load" sorts
    # before "Root Table Load"). An object with no rank (outside the lineage
    # graph) sorts last rather than disappearing.
    objects_by_id = {int(r["OBJECT_ID"]): r for _, r in objects.iterrows()}
    ranks = lineage_ranks(edges)

    def _pipeline_key(object_id: int):
        row = objects_by_id.get(object_id)
        name = str(row["OBJECT_NAME"]) if row is not None else ""
        return (ranks.get(name, 10**6), name)

    raw_target_ids = list(dict.fromkeys(
        int(t) for t in rule_sets["TARGET_OBJECT_ID"] if pd.notna(t)
    )) if not rule_sets.empty else []
    ordered_target_ids = sorted(raw_target_ids, key=_pipeline_key)

    sections, toc_items = [], []
    for tid in ordered_target_ids:
        row = objects_by_id.get(tid)
        if row is None:
            continue  # target recorded but the object itself is missing - nothing to show
        rule_ids = rule_ids_by_object.get(tid, [])
        anchor = f"obj-{tid}"
        sections.append(
            f'<section class="rule-set" id="{anchor}">'
            + _object_card_html(row)
            + _rule_blocks_html(rule_ids)
            + "</section>"
        )
        toc_items.append(
            f'<li><a href="#{anchor}">{_inline(row["OBJECT_NAME"])}</a> '
            f'<span class="muted">({len(rule_ids)} rule(s))</span></li>'
        )

    unassigned_ids = rule_ids_by_object.get(None, [])
    if unassigned_ids:
        sections.append(
            '<section class="rule-set" id="obj-none">'
            '<h3>Rules with no declared target object</h3>'
            '<p class="muted">These rule sets do not record a target object.</p>'
            + _rule_blocks_html(unassigned_ids)
            + "</section>"
        )
        toc_items.append(
            f'<li><a href="#obj-none">Rules with no declared target object</a> '
            f'<span class="muted">({len(unassigned_ids)} rule(s))</span></li>'
        )

    toc_html = "".join(toc_items) or '<li class="muted">No output objects in this module yet.</li>'
    sections_html = "".join(sections) or '<p class="muted">No output objects in this module yet.</p>'
    total_rules = sum(len(v) for v in rule_ids_by_object.values())
    rendered_object_count = sum(1 for tid in ordered_target_ids if tid in objects_by_id)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{_inline(module_name)} — FDD</title>
<style>{_CSS}</style>
</head>
<body>

<h1>{_inline(module_name)} — Functional Design Document</h1>
<p class="muted">
  Generated {generated} by {_inline(viewer)} from the FDD Command Center.
  This reflects <strong>declared intent</strong> — what the specification says should exist.
  It is not a guarantee that every object is built in Snowflake; see the
  "Not built yet" markers below, and the app's own Verify against Snowflake
  page for a live check. Input objects are shown in the lineage diagram but not
  repeated here — only the objects this module writes to are documented below.
</p>

<h2>Contents</h2>
<ul class="toc">
  <li><a href="#lineage">Lineage</a></li>
  <li><a href="#objects">Output objects, structure &amp; rules</a></li>
  {toc_html}
</ul>

<h2 id="lineage">Lineage</h2>
<p class="muted">Scoped to {_inline(module_name)}. Objects outside the module are drawn dashed —
that is where the data enters or leaves this module's chain.</p>
{_lineage_section_html(edges, module_name)}

<h2 id="objects">Output objects, structure &amp; rules ({total_rules} rule(s) across {rendered_object_count} object(s))</h2>
<p class="muted">Only the objects this module writes to. Each object's declared column
structure is followed by the rules that populate it, in sequence.</p>
{sections_html}

<footer>
  Generated by the FDD Command Center. Declared specification, not a guarantee of what is
  deployed — cross-check against Verify against Snowflake in the app before treating this
  as final.
</footer>

</body>
</html>"""
