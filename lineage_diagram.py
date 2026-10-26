# Renders the object lineage graph as a left-to-right Graphviz chart, one band per family.
# Co-authored with CoCo
"""
Top-down flow chart rendering for the object lineage graph.

Everything here is derived from the rule repository metadata tables
(BUSINESS_OBJECT / RULE_SET / RULE_SET_SOURCE / MODULE) as returned by
catalog.get_lineage_edges(). Nothing in this module introspects the real
Snowflake objects - no row counts, no DDL, no INFORMATION_SCHEMA. If a box
says BASE_TABLE it is because the repository says so, not because we looked.

build_dot() emits a Graphviz DOT string for st.graphviz_chart(), which
renders it client-side - so no graphviz python package is required.
"""

from typing import Dict, List, Optional, Set, Tuple

import pandas as pd

from catalog import lineage_roots
from fdd_import import entity_stem

# Longest-path ranking is iterative rather than a real topological sort; this
# caps the work if the metadata ever contains a cycle the self-loop flag
# doesn't cover.
_MAX_RANK_PASSES = 50

# Fill / border per BUSINESS_OBJECT.OBJECT_TYPE, drawn from the Intel blue
# palette the app injects in streamlit_app.py (#0068b5 primary, #00285a navy,
# #004f8a, #00aeef, and the #f4f8fc / #e8f0f8 / #c8dced tints) rather than a
# rainbow of unrelated hues. It's a single ramp that darkens as data moves
# down the pipeline - sources palest, analysis views deepest - so the colour
# reinforces the top-to-bottom reading order instead of fighting it.
# Anything unrecognised falls back to _DEFAULT_STYLE rather than breaking the
# diagram, because OBJECT_TYPE is free text in the repository and new values
# show up over time.
_TYPE_STYLES: Dict[str, Tuple[str, str]] = {
    "SOURCE_TABLE": ("#f4f8fc", "#00aeef"),
    "BASE_TABLE": ("#e8f0f8", "#004f8a"),
    "VIEW": ("#d9eaf7", "#0068b5"),
    "METADATA_VIEW": ("#d9eaf7", "#0068b5"),
    "ANALYSIS_VIEW": ("#c8dced", "#00285a"),
}
_DEFAULT_STYLE = ("#f5f5f5", "#9aa5ad")

# Node text: the same navy the app uses for metric values.
_TEXT_COLOR = "#00285a"



def lineage_ranks(edges: pd.DataFrame) -> dict:
    """
    Longest-path depth of every object from its nearest topmost root: roots
    get rank 0, each hop downstream adds 1. Used to sort the Requirements
    page in pipeline order - what feeds what - instead of alphabetically, and
    to band the flow chart into horizontal layers.
    Longest path (not shortest) guarantees a node's rank is always greater
    than every one of its direct sources, even when it has more than one.
    """
    if edges.empty:
        return {}
    real_edges = edges[~edges["IS_SELF_LOOP"]]
    ranks = {r["object_name"]: 0 for r in lineage_roots(edges)}
    for _ in range(_MAX_RANK_PASSES):
        changed = False
        for _, hop in real_edges.iterrows():
            source_name = hop["SOURCE_OBJECT_NAME"]
            if source_name not in ranks:
                continue
            candidate = ranks[source_name] + 1
            target_name = hop["TARGET_OBJECT_NAME"]
            if candidate > ranks.get(target_name, -1):
                ranks[target_name] = candidate
                changed = True
        if not changed:
            break
    return ranks


# ------------------------------------------------------------------
# Label helpers
# ------------------------------------------------------------------
def _clean(value) -> str:
    """
    Trimmed string for a value that may arrive as None or as a pandas NaN
    (which is truthy, so `value or ""` doesn't catch it).
    """
    text = str(value or "").strip()
    return "" if text.lower() == "nan" else text


def _short_name(technical_name, fallback: str) -> str:
    """
    Last segment of a qualified name: the DB.SCHEMA. prefix is the same for
    most objects and just makes every box wider without telling the reader
    anything. Falls back to the business object name when the technical name
    isn't recorded.
    """
    text = _clean(technical_name)
    if not text:
        return _clean(fallback) or "?"
    return text.split(".")[-1]


def _wrap(text: str, width: int = 22) -> List[str]:
    """
    Break a long identifier into lines on underscores so the boxes stay
    narrow. Graphviz won't wrap for us. Underscores are kept on the trailing
    edge of each line so the name is still copy-pasteable by eye.

    Returns the lines unjoined: the caller escapes each one before joining
    with a DOT newline, because escaping after the join would turn the "\\n"
    separator into literal backslash-n text inside the box.
    """
    parts = str(text).split("_")
    lines: List[str] = []
    current = ""
    for index, part in enumerate(parts):
        piece = part if index == len(parts) - 1 else part + "_"
        if current and len(current) + len(piece) > width:
            lines.append(current)
            current = piece
        else:
            current += piece
    if current:
        lines.append(current)
    return lines or [str(text)]


def _escape(text) -> str:
    """Quote-safe for a DOT double-quoted string."""
    return str(text or "").replace("\\", "\\\\").replace('"', '\\"')


def _box_label(short_name: str, type_line: str) -> str:
    """
    A DOT label: wrapped short name, then the object type on its own line.
    Each segment is escaped individually so the "\\n" separators survive as
    real line breaks.

    An object with no OBJECT_TYPE recorded gets just its name - no filler
    placeholder line, which would only add noise to the box.
    """
    segments = _wrap(short_name)
    if type_line:
        segments.append(f"({type_line})")
    return "\\n".join(_escape(segment) for segment in segments)


# ------------------------------------------------------------------
# Graph shaping
# ------------------------------------------------------------------
def _collect_nodes(edges: pd.DataFrame) -> Dict[int, dict]:
    """
    One entry per object appearing anywhere in `edges`, keyed by OBJECT_ID.
    Both endpoints of every hop are harvested so an object that is only ever
    a target (the bottom of the pipeline) still gets a box.
    """
    nodes: Dict[int, dict] = {}
    for side in ("SOURCE", "TARGET"):
        for _, row in edges.iterrows():
            object_id = int(row[f"{side}_OBJECT_ID"])
            if object_id in nodes:
                continue
            nodes[object_id] = {
                "object_name": row[f"{side}_OBJECT_NAME"],
                "technical_name": row.get(f"{side}_TECHNICAL_OBJECT"),
                "object_type": row.get(f"{side}_OBJECT_TYPE"),
                "module_name": row[f"{side}_MODULE_NAME"],
            }
    return nodes


def _walk(edges: pd.DataFrame, start_ids, from_col: str, to_col: str) -> Set[int]:
    """
    Transitive closure over `edges` from `start_ids`, hopping from_col -> to_col.
    Pass SOURCE->TARGET to walk downstream, TARGET->SOURCE to walk upstream.
    Breadth-first with a visited set, so a cycle in the metadata terminates
    instead of looping - the same guard the indented text tree uses.
    """
    real_edges = edges[~edges["IS_SELF_LOOP"]]
    seen: Set[int] = set()
    queue = [int(i) for i in start_ids]
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        nxt = real_edges.loc[real_edges[from_col] == current, to_col]
        queue.extend(int(t) for t in nxt if int(t) not in seen)
    return seen


def _scope(edges: pd.DataFrame, root_ids: List[int]) -> Tuple[Set[int], Set[int]]:
    """
    Which objects belong in a diagram rooted at `root_ids`.

    Returns (descendants, co_inputs).

    `descendants` is the downstream closure - the branch the user picked.
    `co_inputs` are the *immediate* parents of any descendant that aren't
    themselves descendants: the other objects a converging rule set consumes.

    Downstream-only scoping is actively misleading: V_BRD_ITEM_STAGE is built by
    a rule set joining three views, so drawing only the branch below the selected
    root makes it look like it has one input. Including the direct parents means
    every box shows its complete input set.

    Deliberately one hop up, not the full ancestor closure. An object like
    T_BRD_ITEM_CHARACTERISTIC_ROOT is owned by a separate design that isn't in
    this repository yet; the Item pipeline needs to show *that the characteristic
    view is consumed*, not to trace where that view came from. One hop draws the
    boundary in the right place - and it keeps the diagram from ballooning into
    every upstream branch whenever a pipeline converges.
    """
    descendants = _walk(edges, root_ids, "SOURCE_OBJECT_ID", "TARGET_OBJECT_ID")
    real_edges = edges[~edges["IS_SELF_LOOP"]]
    direct_parents = {
        int(s)
        for s in real_edges.loc[
            real_edges["TARGET_OBJECT_ID"].isin(descendants), "SOURCE_OBJECT_ID"
        ]
    }
    return descendants, direct_parents - descendants


# ------------------------------------------------------------------
# Structured layout: columns = pipeline layer, bands = entity family
# ------------------------------------------------------------------
# Left to right in the order data moves. "input" holds objects that feed a family
# without being part of its own chain (another module's view, a master-data source).
LAYER_ORDER = ("input", "root", "root_view", "stage", "current", "memories", "core_pit")
LAYER_TITLES = {
    "input": "Inputs", "root": "Root table", "root_view": "Root view", "stage": "Stage",
    "current": "Current", "memories": "Memories", "core_pit": "Core PIT",
}
# One ramp, pale to deep, so a band reads left to right the way the data flows.
_LAYER_STYLES: Dict[str, Tuple[str, str]] = {
    "input": ("#ffffff", "#9aa5ad"),
    "root": ("#f4f8fc", "#00aeef"),
    "root_view": ("#e8f0f8", "#0068b5"),
    "stage": ("#dcebf7", "#0068b5"),
    "current": ("#d0e3f3", "#004f8a"),
    "memories": ("#d0e3f3", "#004f8a"),
    "core_pit": ("#c8dced", "#00285a"),
}
_PASS_THROUGH_KINDS = {"TIME_TRAVEL", "NO_TRANSFORMATION"}

LEGEND = (
    "Columns are pipeline layers, left to right: Inputs → Root → Root view → Stage → "
    "Current → Memories → Core PIT. Each band is one family (e.g. Item Location, Hybrid "
    "Item Location). Arrow labels: number of rules, \"time travel\" or \"no change\"; "
    "hover an arrow for its rule set. Dashed arrow = an input or a hop between families; "
    "dotted grey arrow = reads from further down the pipeline; light-blue arrow = crosses "
    "into another module; dashed box = the edge of this view (its own chain is not drawn)."
)


def object_layer(technical_name) -> str:
    """Pipeline layer from the naming convention (T_/V_ prefix, _ROOT/_STAGE/... suffix).

    Anything that does not follow the convention - a master-data view such as
    IF_MASTER_DATA.ITEM_IF_ANALYSIS.INTELFOUNDRY_MATERIAL_PLANT - is an input.
    """
    leaf = _clean(technical_name).rsplit(".", 1)[-1].upper()
    is_table, is_view = leaf.startswith("T_"), leaf.startswith("V_")
    if not (is_table or is_view):
        return "input"
    if leaf.endswith("MEMORIES"):
        return "memories"
    if leaf.endswith("CURRENT"):
        return "current"
    if leaf.endswith("STAGE"):
        return "stage"
    if leaf.endswith("_ROOT"):
        return "root" if is_table else "root_view"
    if leaf.endswith("PIT"):
        return "core_pit"
    return "stage" if is_view else "root"


def object_family(technical_name) -> str:
    """ITEM_LOCATION for every layer of Item Location; HYBRID_ITEM_LOCATION for Hybrid."""
    return entity_stem(_clean(technical_name))


def family_title(family: str) -> str:
    words = [w for w in str(family or "").split("_") if w]
    return " ".join(w if w in ("UPI", "BOM", "PIT", "IF") else w.capitalize() for w in words)


def _hop_label(kind, count) -> str:
    kind = _clean(kind).upper()
    try:
        count = int(count or 0)
    except (TypeError, ValueError):
        count = 0
    if not count:
        return ""
    if kind == "TIME_TRAVEL":
        return "time travel"
    if kind == "NO_TRANSFORMATION":
        return "no change"
    return f"{count} rule" + ("" if count == 1 else "s")


def _column_of(layer: str) -> int:
    return LAYER_ORDER.index(layer) if layer in LAYER_ORDER else 0


def _plan_layout(edges: pd.DataFrame, family: Optional[str], show_inputs: bool,
                 compact_time_travel: bool):
    """Decide each object's band and column, and which hops to draw.

    Returns (nodes, hops, bands):
      nodes  {id: {..., layer, family, band, column, is_input}}
      hops   [{source, target, label, tooltip, kind, cross_module, backwards}]
      bands  [band, ...] in drawing order
    """
    nodes = _collect_nodes(edges)
    for node in nodes.values():
        node["layer"] = object_layer(node["technical_name"] or node["object_name"])
        node["family"] = object_family(node["technical_name"] or node["object_name"])

    real = edges[~edges["IS_SELF_LOOP"]]
    targets = {int(t) for t in real["TARGET_OBJECT_ID"]}
    members: Dict[str, List[int]] = {}
    for object_id, node in nodes.items():
        members.setdefault(node["family"], []).append(object_id)
    # A family has its own band when the graph builds something in it. A lone view
    # from another design that is only read (V_BRD_ITEM_ROOT, the BOM hash) is an
    # input of whoever reads it, not a one-box band of its own.
    chains = {fam for fam, ids in members.items()
              if any(i in targets for i in ids) and any(nodes[i]["layer"] != "input" for i in ids)}

    consumers: Dict[int, Set[str]] = {}
    for _, hop in real.iterrows():
        consumers.setdefault(int(hop["SOURCE_OBJECT_ID"]), set()).add(
            nodes[int(hop["TARGET_OBJECT_ID"])]["family"])

    for object_id, node in nodes.items():
        own_chain = node["family"] in chains and node["layer"] != "input"
        if own_chain:
            node.update(band=node["family"], is_input=False)
            continue
        fams = sorted(consumers.get(object_id, set()) & chains) or ["SHARED_INPUTS"]
        node.update(band=fams[0] if len(fams) == 1 else "SHARED_INPUTS", is_input=True)

    hops: List[Dict] = []
    labelled: Set[int] = set()
    # Label a rule set once - on its main-chain arrow when it has one - because a
    # stage built from six inputs is one rule set, not six.
    ordered = real.sort_values(
        by=["RULE_SET_ID", "SOURCE_SEQ"] if "SOURCE_SEQ" in real.columns else ["RULE_SET_ID"])
    for _, hop in ordered.iterrows():
        source, target = int(hop["SOURCE_OBJECT_ID"]), int(hop["TARGET_OBJECT_ID"])
        rule_set = int(hop["RULE_SET_ID"]) if pd.notna(hop.get("RULE_SET_ID")) else -1
        same_band = nodes[source]["band"] == nodes[target]["band"] and not nodes[source]["is_input"]
        label = ""
        if rule_set not in labelled and (same_band or not any(
                int(o["TARGET_OBJECT_ID"]) == target and nodes[int(o["SOURCE_OBJECT_ID"])]["band"]
                == nodes[target]["band"] and not nodes[int(o["SOURCE_OBJECT_ID"])]["is_input"]
                for _, o in ordered[ordered["RULE_SET_ID"] == hop["RULE_SET_ID"]].iterrows())):
            label = _hop_label(hop.get("RULE_KIND"), hop.get("RULE_COUNT"))
            labelled.add(rule_set)
        hops.append({
            "source": source, "target": target, "label": label,
            "tooltip": _clean(hop.get("RULE_SET_NAME")),
            "kind": _clean(hop.get("RULE_KIND")).upper(),
            "cross_module": _clean(hop.get("SOURCE_MODULE_NAME")) != _clean(hop.get("TARGET_MODULE_NAME")),
            "same_band": same_band,
        })

    if compact_time_travel:
        nodes, hops = _compact_time_travel(nodes, hops)

    if family:
        keep = {i for i, n in nodes.items() if n["band"] == family}
        feeders = {h["source"] for h in hops if h["target"] in keep and h["source"] not in keep}
        for object_id in feeders:
            nodes[object_id].update(band=family, is_input=True)
        keep |= feeders
        nodes = {i: n for i, n in nodes.items() if i in keep}
        hops = [h for h in hops if h["source"] in keep and h["target"] in keep]

    if not show_inputs:
        nodes = {i: n for i, n in nodes.items() if not n["is_input"]}
        hops = [h for h in hops if h["source"] in nodes and h["target"] in nodes]

    for node in nodes.values():
        node["column"] = 0 if node["is_input"] else _column_of(node["layer"])
    for hop in hops:
        hop["backwards"] = (nodes[hop["target"]]["column"] <= nodes[hop["source"]]["column"]
                            and not nodes[hop["source"]]["is_input"])

    # Bands top to bottom by where their chain starts, then by name.
    band_names = sorted({n["band"] for n in nodes.values()},
                        key=lambda b: (b == "SHARED_INPUTS",
                                       min((n["column"] for n in nodes.values()
                                            if n["band"] == b and not n["is_input"]), default=99),
                                       b))
    return nodes, hops, band_names


def _compact_time_travel(nodes: Dict[int, dict], hops: List[Dict]):
    """Fold Current and Memories into one "time travel" arrow from Stage to Core PIT."""
    removed: Set[int] = set()
    extra: List[Dict] = []
    by_band: Dict[str, Dict[str, int]] = {}
    for object_id, node in nodes.items():
        if not node["is_input"]:
            by_band.setdefault(node["band"], {})[node["layer"]] = object_id
    for band, layers in by_band.items():
        stage, pit = layers.get("stage"), layers.get("core_pit")
        middle = [layers[k] for k in ("current", "memories") if k in layers]
        if not middle or stage is None or pit is None:
            continue
        chain = [h for h in hops if h["source"] in middle or h["target"] in middle]
        if not all(h["kind"] in _PASS_THROUGH_KINDS for h in chain):
            continue
        removed.update(middle)
        names = " → ".join(_short_name(nodes[i]["technical_name"], nodes[i]["object_name"])
                           for i in middle)
        extra.append({"source": stage, "target": pit, "label": "time travel",
                      "tooltip": f"Time travel through {names}", "kind": "TIME_TRAVEL",
                      "cross_module": False, "same_band": True})
    if not removed:
        return nodes, hops
    return ({i: n for i, n in nodes.items() if i not in removed},
            [h for h in hops if h["source"] not in removed and h["target"] not in removed] + extra)


def lineage_families(edges: pd.DataFrame) -> List[str]:
    """The bands a diagram of these edges would draw, for the family picker."""
    if edges is None or edges.empty:
        return []
    _, _, bands = _plan_layout(edges, None, True, False)
    return [b for b in bands if b != "SHARED_INPUTS"]


def build_dot(edges: pd.DataFrame, root_ids: Optional[List[int]] = None,
              dashed_ids: Optional[Set[int]] = None, family: Optional[str] = None,
              show_inputs: bool = True, compact_time_travel: bool = False) -> str:
    """A left-to-right Graphviz chart: one band per family, one column per layer.

    Replaces a top-down chart whose rows were "hops from a root" and whose every
    arrow carried the rule set's full name. With several families in one module and
    a stage that reads its own Core PIT, the rows never lined up and ~30 long labels
    crossed each other. Here the column comes from the object's layer, so Root, Stage,
    Current, Memories and Core PIT always line up; each family gets its own band;
    inputs from other designs sit in the first column; and arrows say only "7 rules",
    "time travel" or "no change", with the rule set name on hover.

    root_ids / dashed_ids scope and mark the diagram exactly as before: the branch
    below the chosen roots plus the immediate co-inputs (dashed boxes), and boundary
    objects where the data leaves the module (dashed boxes).
    """
    if edges.empty:
        return "digraph lineage { label=\"No lineage to draw\"; }"

    co_inputs: Set[int] = set()
    boundary: Set[int] = {int(i) for i in (dashed_ids or set())}
    if root_ids:
        descendants, co_inputs = _scope(edges, [int(r) for r in root_ids])
        edges = edges[
            edges["TARGET_OBJECT_ID"].isin(descendants)
            & edges["SOURCE_OBJECT_ID"].isin(descendants | co_inputs)
        ]
        if edges.empty:
            return "digraph lineage { label=\"No lineage to draw\"; }"

    nodes, hops, bands = _plan_layout(edges, family, show_inputs, compact_time_travel)
    if not nodes:
        return "digraph lineage { label=\"No lineage to draw\"; }"

    lines = [
        "digraph lineage {",
        "  rankdir=LR;",
        "  newrank=true;",          # lets a column (rank=same) span several bands
        "  bgcolor=\"transparent\";",
        "  splines=ortho;",         # right angles; labels are short xlabels
        "  nodesep=0.18;",
        "  ranksep=0.55;",
        "  forcelabels=true;",
        "  node [shape=box, style=\"filled,rounded\", fontname=\"Helvetica\", "
        f"fontsize=7, penwidth=0.9, margin=\"0.12,0.07\", fontcolor=\"{_TEXT_COLOR}\"];",
        "  edge [fontname=\"Helvetica\", fontsize=6, color=\"#4f7ea3\", "
        "fontcolor=\"#004f8a\", arrowsize=0.5, penwidth=0.9];",
    ]

    used_columns = sorted({n["column"] for n in nodes.values()})
    # Graphviz decides the vertical order of the bands to keep arrows short (shared
    # inputs end up between the bands that read them). Pinning the order was tried
    # with invisible anchors and made no difference, so it is left to the layout.
    for index, band in enumerate(bands):
        title = "Shared inputs" if band == "SHARED_INPUTS" else family_title(band)
        lines.append(f'  subgraph cluster_band{index} {{ label="{_escape(title)}"; '
                     'labeljust=l; fontname="Helvetica"; fontsize=9; fontcolor="#00285a"; '
                     'style="rounded,filled"; fillcolor="#f7fafd"; color="#c8dced";')
        for object_id, node in nodes.items():
            if node["band"] != band:
                continue
            layer = "input" if node["is_input"] else node["layer"]
            fill, border = _LAYER_STYLES.get(layer, _DEFAULT_STYLE)
            short = _short_name(node["technical_name"], node["object_name"])
            dashed = node["is_input"] or object_id in co_inputs or object_id in boundary
            style = "filled,rounded,dashed" if dashed else "filled,rounded"
            tooltip = f"{node['object_name']} ({_clean(node['module_name']) or 'no module'})"
            lines.append(f'    n{object_id} [label="{_box_label(short, "")}", style="{style}", '
                         f'fillcolor="{fill}", color="{border}", tooltip="{_escape(tooltip)}"];')
        lines.append("  }")

    # Column headings, chained invisibly so the columns keep their order.
    lines.append("  subgraph cluster_headings { style=invis;")
    for column in used_columns:
        title = LAYER_TITLES[LAYER_ORDER[column]]
        lines.append(f'    h{column} [shape=plaintext, style="", label="{title}", '
                     'fontsize=8, fontcolor="#004f8a"];')
    lines.append("  }")
    for left, right in zip(used_columns, used_columns[1:]):
        lines.append(f"  h{left} -> h{right} [style=invis];")

    for column in used_columns:
        members = " ".join(f"n{i}" for i, n in sorted(nodes.items()) if n["column"] == column)
        lines.append(f"  {{ rank=same; h{column} {members} }}")

    for hop in hops:
        attrs = [f'tooltip="{_escape(hop["tooltip"])}"']
        if hop["label"]:
            attrs.append(f'xlabel="{_escape(hop["label"])}"')
        if hop["backwards"]:
            # Reads from further down its own pipeline (the Item Location stage reads
            # its Core PIT). Drawn without constraining the layout, or it would drag
            # the Core PIT column back to the left.
            attrs += ['style=dotted', 'color="#9aa5ad"', 'constraint=false']
        elif not hop["same_band"]:
            attrs += ['style=dashed', 'color="#7fa8c6"']
        if hop["cross_module"]:
            attrs += ['color="#00aeef"', 'fontcolor="#0068b5"']
        lines.append(f'  n{hop["source"]} -> n{hop["target"]} [{", ".join(attrs)}];')

    lines.append("}")
    return "\n".join(lines)

