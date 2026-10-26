"""
Renders the FDD Command Center architecture diagram (see PRD.md, section 12)
to a PNG. Run once to regenerate architecture_diagram.png after the architecture changes.

Layout: strictly stacked horizontal bands, top to bottom. Every band spans the same
width and sits in its own y-range, so nothing overlaps regardless of how much text a
box holds. Cross-band arrows are vertical; same-band relationships (module -> Cortex)
are short diagonals within adjacent bands only.
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
from matplotlib.lines import Line2D

FIG_W, FIG_H = 15.0, 17.5
MARGIN = 0.6

BUILT = "#2e7d32"
PROPOSED = "#c77700"
FACT = "#8e8e8e"
FILL_UI = "#eaf2fb"
FILL_APP = "#fff7e6"
FILL_AI = "#f3eaff"
FILL_DATA = "#eafaf1"
FILL_EXT = "#f5f5f5"

fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
ax.set_xlim(0, FIG_W)
ax.set_ylim(0, FIG_H)
ax.axis("off")


def box(x, y, w, h, text, fill, edge, style="-", fontsize=9.0, fontweight="normal", lw=1.6):
    b = FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.09",
                        linewidth=lw, edgecolor=edge, facecolor=fill, linestyle=style, zorder=3)
    ax.add_patch(b)
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=fontsize,
            fontweight=fontweight, zorder=4, linespacing=1.4)
    return (x, y, w, h)


def band_label(x, y, text, color):
    ax.text(x, y, text, fontsize=9.5, fontweight="bold", color=color, va="bottom")


def v_arrow(b1, b2, color="#333", label=None, lw=1.5, style="-", label_dx=0.15):
    x1, y1, w1, h1 = b1
    x2, y2, w2, h2 = b2
    p1 = (x1 + w1 / 2, y1)
    p2 = (x2 + w2 / 2, y2 + h2)
    a = FancyArrowPatch(p1, p2, arrowstyle="-|>", mutation_scale=15, linewidth=lw,
                         color=color, linestyle=style, zorder=2)
    ax.add_patch(a)
    if label:
        ax.text((p1[0] + p2[0]) / 2 + label_dx, (p1[1] + p2[1]) / 2, label, fontsize=7.6,
                color=color, style="italic", ha="left", va="center", zorder=5,
                bbox=dict(boxstyle="round,pad=0.12", fc="white", ec="none", alpha=0.9))


def diag_arrow(b1, b2, color="#333", label=None, style="-", lw=1.1, rad=0.15, label_dy=0.0):
    x1, y1, w1, h1 = b1
    x2, y2, w2, h2 = b2
    c1 = (x1 + w1 / 2, y1 + h1 / 2)
    c2 = (x2 + w2 / 2, y2 + h2 / 2)
    a = FancyArrowPatch(c1, c2, arrowstyle="-|>", mutation_scale=13, linewidth=lw,
                         color=color, linestyle=style, zorder=2,
                         connectionstyle=f"arc3,rad={rad}", shrinkA=18, shrinkB=18)
    ax.add_patch(a)
    if label:
        ax.text((c1[0] + c2[0]) / 2, (c1[1] + c2[1]) / 2 + label_dy, label, fontsize=7.0,
                color=color, style="italic", ha="center", va="center", zorder=5,
                bbox=dict(boxstyle="round,pad=0.1", fc="white", ec="none", alpha=0.9))


CONTENT_W = FIG_W - 2 * MARGIN
y = FIG_H

# ------------------------------------------------------------------
# Title
# ------------------------------------------------------------------
y -= 0.55
ax.text(FIG_W / 2, y, "FDD Command Center — Application Architecture",
        ha="center", va="top", fontsize=17, fontweight="bold")
y -= 0.42
ax.text(FIG_W / 2, y, "Streamlit in Snowflake · Container runtime (SPCS)",
        ha="center", va="top", fontsize=10.5, color="#444")
y -= 0.55

# ------------------------------------------------------------------
# Band 1 - Personas
# ------------------------------------------------------------------
personas = ["BA / PO", "Developer", "Solution\nArchitect", "Manager /\nGovernance"]
n = len(personas)
gap = 0.3
pw = (CONTENT_W - (n - 1) * gap) / n
ph = 0.85
y_top = y
y -= ph
persona_boxes = []
for i, p in enumerate(personas):
    xx = MARGIN + i * (pw + gap)
    b = box(xx, y, pw, ph, p, "#ffffff", "#555", fontweight="bold")
    persona_boxes.append(b)
y -= 0.55

# ------------------------------------------------------------------
# Band 2 - Presentation (Streamlit pages)
# ------------------------------------------------------------------
band_top = y
ui_groups = [
    ("Browse", "Requirements · Publish FDD ·\nAsk · Object lineage ·\nDev pipeline"),
    ("Author", "Import an FDD · Write /\nUpdate a rule · Object\ncolumns · Teams & modules"),
    ("Govern", "Audit trail · Rollback ·\nRetire · Approval\nworkflow"),
    ("Validate", "Verify against Snowflake ·\nTest cases ·\nRepository health"),
    ("Attachments &\nContacts*", "Linked docs ·\nownership"),
]
n = len(ui_groups)
gap = 0.22
uw = (CONTENT_W - (n - 1) * gap) / n
uh = 1.55
band_h = uh + 0.55
outer = box(MARGIN - 0.15, y - band_h, CONTENT_W + 0.3, band_h, "", FILL_UI, BUILT, lw=1.2)
band_label(MARGIN, y - 0.28, "PRESENTATION  ·  Streamlit in Snowflake pages", "#1a4d1a")
y -= 0.45
y -= uh
ui_boxes = []
for i, (title, sub) in enumerate(ui_groups):
    xx = MARGIN + i * (uw + gap)
    proposed = title.endswith("*")
    edge, style = (PROPOSED, "--") if proposed else (BUILT, "-")
    b = box(xx, y, uw, uh, f"{title}\n\n{sub}", "#ffffff", edge, style=style, fontsize=8.2)
    ui_boxes.append(b)
y -= 0.55

# single aggregate arrow personas -> presentation band
v_arrow((MARGIN + CONTENT_W / 2 - 0.4, persona_boxes[0][1], 0.8, 0),
        (MARGIN + CONTENT_W / 2 - 0.4, band_top - band_h, 0.8, 0))

# ------------------------------------------------------------------
# Band 3 - Application modules
# ------------------------------------------------------------------
app_modules = [
    ("requirement_compiler.py", "NL \u2192 structured rule\n(LLM extract + parser)", BUILT, "-"),
    ("fdd_render.py", "Deterministic FDD\nEnglish renderer", BUILT, "-"),
    ("repository.py", "CRUD, audit, approvals,\nretire / rollback, hand-off", BUILT, "-"),
    ("catalog.py", "Entity resolution,\nlineage reads", BUILT, "-"),
    ("compliance.py", "Structural / data /\nAI verify", BUILT, "-"),
    ("test_generator.py", "Test case\ngeneration", BUILT, "-"),
    ("lineage_diagram.py", "Lineage graph\nrendering", BUILT, "-"),
    ("nlp_router.py", "Ask page intent\nrouting", BUILT, "-"),
    ("fdd_import.py", "FDD / xlsx import,\nsaved drafts", BUILT, "-"),
    ("fdd_publish.py", "Module FDD as\nself-contained HTML", BUILT, "-"),
    ("repository_health.py", "Repository-wide\nhealth checks", BUILT, "-"),
    ("sample_data.py", "Sample data,\ndraft from rules", BUILT, "-"),
    ("module_handoff_ui.py", "Dev hand-off,\nDev pipeline", BUILT, "-"),
    ("object_columns_builder.py", "Column spec\nbuilder", BUILT, "-"),
    ("attach./contact mgr *", "Linked docs +\nownership", PROPOSED, "--"),
]
cols = 5
rows = 3
gap_x, gap_y = 0.18, 0.2
aw = (CONTENT_W - (cols - 1) * gap_x) / cols
ah = 1.05
band_h = rows * ah + (rows - 1) * gap_y + 0.55
outer = box(MARGIN - 0.15, y - band_h, CONTENT_W + 0.3, band_h, "", FILL_APP, "#8a6d00", lw=1.2)
band_label(MARGIN, y - 0.28, "APPLICATION  ·  Python modules (Snowpark session)", "#6b5400")
y -= 0.45
app_top = y
app_boxes = {}
for i, (name, desc, edge, style) in enumerate(app_modules):
    row, col = divmod(i, cols)
    xx = MARGIN + col * (aw + gap_x)
    yy = app_top - ah - row * (ah + gap_y)
    b = box(xx, yy, aw, ah, f"{name}\n{desc}", "#ffffff", edge, style=style, fontsize=7.4)
    app_boxes[name] = b
app_bottom = app_top - rows * ah - (rows - 1) * gap_y
y = app_bottom - 0.5

v_arrow((MARGIN + CONTENT_W / 2 - 0.4, ui_boxes[0][1], 0.8, 0),
        (MARGIN + CONTENT_W / 2 - 0.4, app_top, 0.8, 0))

# ------------------------------------------------------------------
# Band 4 - Cortex AI
# ------------------------------------------------------------------
ai_w, ai_h = 8.5, 1.35
ai_x = MARGIN + (CONTENT_W - ai_w) / 2
band_h = ai_h + 0.55
outer = box(MARGIN - 0.15, y - band_h, CONTENT_W + 0.3, band_h, "", "#faf7ff", "#5c3d99", lw=1.0, style="-")
band_label(MARGIN, y - 0.28, "SNOWFLAKE CORTEX", "#3e2871")
y -= 0.45
ai_box = box(ai_x, y - ai_h, ai_w, ai_h,
             "AI_COMPLETE / COMPLETE  —  claude-sonnet-4-6 (writer) · claude-haiku-4-5 (fast)\n"
             "llama3.3-70b (fallback)  ·  Cortex Analyst (Ask page)", "#ffffff", "#5c3d99", fontsize=8.3)
for name in ("requirement_compiler.py", "test_generator.py", "compliance.py", "nlp_router.py",
             "fdd_import.py", "sample_data.py"):
    diag_arrow(app_boxes[name], ai_box, color="#5c3d99", lw=0.9, rad=0.08)
y = (y - ai_h) - 0.5

# ------------------------------------------------------------------
# Band 5 - Data (repository schema)
# ------------------------------------------------------------------
data_groups = [
    ("Rules", "RULE · RULE_CONDITION · RULE_ACTION\nRULE_JOIN · RULE_SET · RULE_SET_SOURCE\nRULE_REFERENCE · RULE_NOTE"),
    ("Objects & organization", "BUSINESS_OBJECT · BUSINESS_OBJECT_COLUMN\nOBJECT_SAMPLE_DATA · MODULE · TEAM\nTEAM_DATABASE · DATABASE_MAP"),
    ("Governance & hand-off", "RULE_CHANGE_HISTORY (+reason*) · AUDIT_TRAIL\nRULE_APPROVER · RULE_TEST_CASE\nMODULE_HANDOFF · FDD_IMPORT_DRAFT · ATTACHMENT*"),
]
n = len(data_groups)
gap = 0.3
dw = (CONTENT_W - (n - 1) * gap) / n
dh = 1.55
band_h = dh + 0.55
outer = box(MARGIN - 0.15, y - band_h, CONTENT_W + 0.3, band_h, "", FILL_DATA, BUILT, lw=1.2)
band_label(MARGIN, y - 0.28, "DATA  ·  IF_SUPPLY_CHAIN.PDH_SUPPLY_PLANNING_XFRM_BASE", "#1a4d1a")
y -= 0.45
data_top = y
data_boxes = []
for i, (title, sub) in enumerate(data_groups):
    xx = MARGIN + i * (dw + gap)
    b = box(xx, y - dh, dw, dh, f"{title}\n\n{sub}", "#ffffff", BUILT, fontsize=8.0)
    data_boxes.append(b)
y = data_top - dh
y -= 0.5

v_arrow((MARGIN + 1.1, app_bottom, 0.8, 0),
        (MARGIN + 1.1, data_top, 0.8, 0),
        label="read / write, via repository.py + catalog.py", label_dx=0.05)

# ------------------------------------------------------------------
# Band 6 - External systems (deployed fact + publish targets)
# ------------------------------------------------------------------
ext_groups = [
    ("Deployed Snowflake objects (fact)", "INFORMATION_SCHEMA · live tables & views\n"
     "Not part of the repository — read-only verification target", FACT, "--"),
    ("pdh-engineering-studio", "T_CMD_CODE_DEPLOYMENT ·\nT_CMD_REVIEW_REQUEST\n"
     "Read for dev hand-off status", FACT, "--"),
    ("Wiki / SharePoint / Confluence *", "Linked attachments\nAssembled FDD export target", PROPOSED, "--"),
]
n = len(ext_groups)
gap = 0.4
ew = (CONTENT_W - (n - 1) * gap) / n
eh = 1.5
band_h = eh + 0.55
outer = box(MARGIN - 0.15, y - band_h, CONTENT_W + 0.3, band_h, "", FILL_EXT, "#999", lw=1.0)
band_label(MARGIN, y - 0.28, "EXTERNAL  ·  outside the repository", "#555")
y -= 0.45
ext_top = y
ext_boxes = {}
for i, (title, sub, edge, style) in enumerate(ext_groups):
    xx = MARGIN + i * (ew + gap)
    b = box(xx, y - eh, ew, eh, f"{title}\n\n{sub}", "#ffffff", edge, style=style, fontsize=8.0)
    ext_boxes[title] = b
y = ext_top - eh

diag_arrow(data_boxes[2], ext_boxes["Deployed Snowflake objects (fact)"],
           color=FACT, style="--", lw=1.1, rad=-0.1, label="verify (compliance.py, read-only)")
diag_arrow(data_boxes[2], ext_boxes["pdh-engineering-studio"],
           color=FACT, style="--", lw=1.1, rad=0.0, label="hand-off status (read)")
diag_arrow(data_boxes[2], ext_boxes["Wiki / SharePoint / Confluence *"],
           color=PROPOSED, style="--", lw=1.1, rad=0.1, label="publish / link (proposed)")

y -= 0.7

# ------------------------------------------------------------------
# Legend
# ------------------------------------------------------------------
leg_x = MARGIN
ax.add_line(Line2D([leg_x, leg_x + 0.5], [y + 0.3, y + 0.3], color=BUILT, lw=2.2))
ax.text(leg_x + 0.62, y + 0.3, "Built", fontsize=8.6, va="center")
ax.add_line(Line2D([leg_x, leg_x + 0.5], [y - 0.02, y - 0.02], color=PROPOSED, lw=2.2, ls="--"))
ax.text(leg_x + 0.62, y - 0.02, "Proposed, not yet built (marked *)", fontsize=8.6, va="center")
ax.add_line(Line2D([leg_x, leg_x + 0.5], [y - 0.34, y - 0.34], color=FACT, lw=2.2, ls="--"))
ax.text(leg_x + 0.62, y - 0.34, "External / deployed fact — outside the repository", fontsize=8.6, va="center")

y -= 0.75
ax.set_ylim(y - 0.1, FIG_H)
fig.set_size_inches(FIG_W, FIG_H - y + 0.1)

plt.tight_layout()
out = "architecture_diagram.png"
plt.savefig(out, dpi=170, bbox_inches="tight", facecolor="white")
print(f"Wrote {out}")
