"""The Item Location FDD, imported the way a careful analyst reads it.

Reference case for the accurate import. Before this, the same document produced:

  * no Current -> Memories hop for Item, UPI or Hybrid - the Memories rows have an
    empty label cell and were read as more Transformation text, so every later rule
    landed one hop down the chain;
  * a Root View whose real input (T_BRD_ITEM_LOCATION_ROOT) was missing, because two
    Source rows sat between them;
  * the Substrate cell glued onto the Hybrid cell, so the substrate-only filter
    MATERIAL_TYPE_CD <> UNBW applied to every hybrid record;
  * rules saved with invented columns (LAYER, LOC, TYPE) and a reversed mapping,
    with nothing on screen to say so.

The model is stubbed: these tests pin down the structure the app builds and how it
grades what the model returns, not the model's wording.
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "streamlit" not in sys.modules:
    st = types.ModuleType("streamlit")
    st.session_state = {}

    def _cache_data(*dargs, **dkwargs):
        if dargs and callable(dargs[0]):
            return dargs[0]

        def wrap(fn):
            return fn
        return wrap

    st.cache_data = _cache_data
    st.cache_resource = _cache_data
    st.__getattr__ = lambda name: (lambda *a, **k: None)
    sys.modules["streamlit"] = st

if "snowflake.snowpark.context" not in sys.modules:
    for name in ("snowflake", "snowflake.snowpark", "snowflake.snowpark.context"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["snowflake.snowpark.context"].get_active_session = lambda: None

import pandas as pd

import fdd_import as fi
import requirement_compiler as rc
import rule_extractor as rx

HERE = os.path.dirname(os.path.abspath(__file__))
FDD = open(os.path.join(HERE, "fixtures", "item_location_fdd.txt"), encoding="utf-8").read()

X = "IF_SUPPLY_CHAIN.PDH_COMMON_XFRM."
XB = "IF_SUPPLY_CHAIN.PDH_COMMON_XFRM_BASE."
A = "IF_SUPPLY_CHAIN.PDH_COMMON_ANALYSIS."


def leaf(name):
    return str(name).rsplit(".", 1)[-1]


class TestDocumentStructure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.groups = fi.parse_document(FDD)["rule_groups"]
        cls.hops = [(leaf(g["source"]), leaf(g["target"])) for g in cls.groups]

    def group(self, target):
        return next(g for g in self.groups if leaf(g["target"]) == target)

    def test_the_whole_chain(self):
        self.assertEqual([
            ("INTELFOUNDRY_MATERIAL_PLANT", "T_BRD_ITEM_LOCATION_ROOT"),
            ("T_BRD_ITEM_LOCATION_ROOT", "V_BRD_ITEM_LOCATION_ROOT"),
            ("V_BRD_ITEM_LOCATION_ROOT", "V_BRD_ITEM_LOCATION_STAGE"),
            ("V_BRD_ITEM_LOCATION_STAGE", "T_BRD_ITEM_LOCATION_CORE_CURRENT"),
            ("T_BRD_ITEM_LOCATION_CORE_CURRENT", "T_BRD_ITEM_LOCATION_CORE_MEMORIES"),
            ("T_BRD_ITEM_LOCATION_CORE_MEMORIES", "V_BRD_ITEM_LOCATION_CORE_PIT"),
            ("V_BRD_UPI_BILL_OF_MATERIAL_LOCATION_CORE_PIT", "V_BRD_UPI_ITEM_LOCATION_STAGE"),
            ("V_BRD_UPI_ITEM_LOCATION_STAGE", "T_BRD_UPI_ITEM_LOCATION_CORE_CURRENT"),
            ("T_BRD_UPI_ITEM_LOCATION_CORE_CURRENT", "T_BRD_UPI_ITEM_LOCATION_CORE_MEMORIES"),
            ("T_BRD_UPI_ITEM_LOCATION_CORE_MEMORIES", "V_BRD_UPI_ITEM_LOCATION_CORE_PIT"),
            ("V_BRD_HYBRID_BILL_OF_MATERIAL_LOCATION_CORE_PIT", "V_BRD_HYBRID_ITEM_LOCATION_STAGE"),
            ("V_BRD_HYBRID_ITEM_LOCATION_STAGE", "T_BRD_HYBRID_ITEM_LOCATION_CORE_CURRENT"),
            ("T_BRD_HYBRID_ITEM_LOCATION_CORE_CURRENT", "T_BRD_HYBRID_ITEM_LOCATION_CORE_MEMORIES"),
            ("T_BRD_HYBRID_ITEM_LOCATION_CORE_MEMORIES", "V_BRD_HYBRID_ITEM_LOCATION_CORE_PIT"),
        ], self.hops)

    def test_time_travel_text_sits_on_its_own_hop(self):
        memories = self.group("T_BRD_ITEM_LOCATION_CORE_MEMORIES")
        self.assertIn("moves to memories", memories["text"])
        pit = self.group("V_BRD_ITEM_LOCATION_CORE_PIT")
        self.assertNotIn("moves to memories", pit["text"])

    def test_root_view_reads_the_root_table_and_both_declared_sources(self):
        root_view = self.group("V_BRD_ITEM_LOCATION_ROOT")
        self.assertEqual("T_BRD_ITEM_LOCATION_ROOT", leaf(root_view["source"]))
        self.assertEqual({"V_BRD_ITEM_ROOT", "V_BRD_PLANT_ROOT"},
                         {leaf(x) for x in root_view["extra_sources"]})
        self.assertEqual(["1", "2", "3"], [s["rule_number"] for s in root_view["statements"]])
        self.assertFalse(any(fi.is_no_transformation(s["text"])
                             for s in root_view["statements"]),
                         "'no major transformation' next to real rules says nothing")

    def test_substrate_cell_is_separate_from_the_hybrid_cell(self):
        stage = self.group("V_BRD_HYBRID_ITEM_LOCATION_STAGE")
        numbers = [s["rule_number"] for s in stage["statements"]]
        self.assertEqual(["1", "2.1", "2.2", "2.3", "2.4", "2.5"], numbers)
        hybrid, substrate_1 = stage["statements"][0], stage["statements"][1]
        self.assertNotIn("UNBW", hybrid["text"])
        self.assertIn("UNBW", substrate_1["text"])
        self.assertTrue(substrate_1["section"].startswith("Substrate locations"))
        self.assertNotIn("Substrate", substrate_1["text"])

    def test_substrate_objects_named_by_leaf_are_inputs(self):
        stage = self.group("V_BRD_HYBRID_ITEM_LOCATION_STAGE")
        inputs = {leaf(x) for x in [stage["source"], *stage["extra_sources"]]}
        self.assertTrue({"V_BRD_ITEM_LOCATION_ROOT", "V_BRD_ITEM_CORE_PIT"} <= inputs)
        self.assertEqual(["V_BRD_PLANT_CORE_PIT"], stage["unqualified_refs"],
                         "not declared in this FDD, so reported rather than guessed")

    def test_boilerplate_source_row_does_not_invent_an_edge(self):
        self.assertNotIn(("V_BRD_HYBRID_BILL_OF_MATERIAL_LOCATION_CORE_PIT",
                          "V_BRD_UPI_ITEM_LOCATION_STAGE"), self.hops)
        upi = self.group("V_BRD_UPI_ITEM_LOCATION_STAGE")
        self.assertEqual(["V_BRD_HYBRID_BILL_OF_MATERIAL_LOCATION_CORE_PIT"],
                         [leaf(x) for x in upi["declared_unused"]])

    def test_stage_keeps_nine_rules(self):
        stage = self.group("V_BRD_ITEM_LOCATION_STAGE")
        self.assertEqual([str(n) for n in range(1, 10)],
                         [s["rule_number"] for s in stage["statements"]])

    def test_time_travel_is_categorised(self):
        text = self.group("T_BRD_ITEM_LOCATION_CORE_MEMORIES")["statements"][0]["text"]
        self.assertEqual("TIME_TRAVEL", fi.no_transformation_proposal(text, "1")["rule_category"])
        plain = fi.no_transformation_proposal("We don't have any major transformation now.", "1")
        self.assertEqual("NO_TRANSFORMATION", plain["rule_category"])


class TestUnlabelledObjectRow(unittest.TestCase):
    def test_tab_then_name(self):
        self.assertEqual(("", XB + "T_X_CORE_MEMORIES"),
                         fi._split_label_row(f"\t{XB}T_X_CORE_MEMORIES"))

    def test_name_alone(self):
        self.assertEqual(("", XB + "T_X_CORE_MEMORIES"),
                         fi._split_label_row(f"{XB}T_X_CORE_MEMORIES"))

    def test_sentence_is_not_an_object_row(self):
        self.assertIsNone(fi._split_label_row(f"\tJoin {XB}T_X_CORE_MEMORIES with ..."))


# ------------------------------------------------------------------
# Grading what the model returns
# ------------------------------------------------------------------
COLUMNS = {
    X + "V_BRD_ITEM_LOCATION_ROOT": ["ITEM_ID", "PLANT_CD", "ITEM_CLASS_NM", "DELETE_IND"],
    X + "V_BRD_ITEM_LOCATION_STAGE": ["ITEM_ID", "PLANT_CD", "ITEM_LOCATION_TYPE_CD",
                                      "ITEM_ORDER_POINT", "TURNKEY_IND", "OWNER_CD"],
    A + "V_BRD_ITEM_CORE_PIT": ["ITEM_ID", "OWNER_CD", "MATERIAL_TYPE_CD"],
}


class Stub:
    """Plays the model: returns a fixed extraction and a fixed review."""

    def __init__(self, extraction, findings=()):
        self.extraction, self.findings, self.prompts = extraction, list(findings), []

    def __call__(self, prompt, schema, model=""):
        self.prompts.append(prompt)
        if schema is rx.REVIEW_SCHEMA:
            return {"findings": self.findings}
        return dict(self.extraction)


def base(**over):
    rule = {"rule_name": "Owner code", "rule_category": "METADATA_LOOKUP",
            "business_description": "Owner code from the item core view.",
            "conditions": [], "actions": [], "joins": [], "doubts": [], "fdd_issues": []}
    rule.update(over)
    return rule


OWNER_TEXT = (f"OWNER_CD: Join the identified items with {A}V_BRD_ITEM_CORE_PIT "
              "based on ITEM_ID to get the OWNER_CD")
OWNER_JOIN = {"left_object": X + "V_BRD_ITEM_LOCATION_ROOT",
              "right_object": A + "V_BRD_ITEM_CORE_PIT", "join_type": "INNER",
              "join_purpose": "owner code",
              "join_columns": [{"left_column": "ITEM_ID", "right_column": "ITEM_ID"}]}
OWNER_ACTION = {"target_field": "OWNER_CD", "action_type": "LOOKUP",
                "action_value": "V_BRD_ITEM_CORE_PIT.OWNER_CD", "action_description": ""}


class TestGrading(unittest.TestCase):
    def setUp(self):
        self._saved = (rc.get_object_columns, rc.resolve_object, rc.resolve_field,
                       rc.get_column_catalog)
        rc.get_object_columns = lambda name: COLUMNS.get(str(name).upper(), [])
        rc.resolve_object = lambda name, databases=None: (
            (str(name).upper(), 1) if str(name).upper() in COLUMNS else (None, None))
        rc.resolve_field = lambda text, limit=3: []
        rc.get_column_catalog = lambda: pd.DataFrame()

    def tearDown(self):
        (rc.get_object_columns, rc.resolve_object, rc.resolve_field,
         rc.get_column_catalog) = self._saved

    def run_rule(self, stub, text=OWNER_TEXT, review=True):
        return rc.compile_requirement(
            text, 0, "7", [X + "V_BRD_ITEM_LOCATION_ROOT"], X + "V_BRD_ITEM_LOCATION_STAGE",
            model="m", use_llm=True, review_rule=review, llm_fn=stub,
            context={"section": "", "neighbours": ["other rule"], "objects": list(COLUMNS)})

    def test_faithful_rule_is_green(self):
        result = self.run_rule(Stub(base(actions=[OWNER_ACTION], joins=[OWNER_JOIN])))
        self.assertEqual("green", result["status"], result["reasons"])
        self.assertEqual(1, len(result["proposal"]["joins"]))

    def test_prompt_carries_the_real_columns_of_every_object_involved(self):
        stub = Stub(base(actions=[OWNER_ACTION], joins=[OWNER_JOIN]))
        self.run_rule(stub)
        prompt = stub.prompts[0]
        self.assertIn("V_BRD_ITEM_CORE_PIT: ITEM_ID, OWNER_CD, MATERIAL_TYPE_CD", prompt)
        self.assertIn("V_BRD_ITEM_LOCATION_STAGE: ITEM_ID, PLANT_CD", prompt)
        self.assertIn("other rule", prompt)

    def test_invented_column_is_red(self):
        """Stage rule 1 was saved with LAYER != 'PDH'; there is no LAYER column."""
        rule = base(conditions=[{"field_name": "LAYER", "operator": "!=", "field_value": "PDH",
                                 "logical_operator": "AND"}],
                    actions=[{"target_field": "ITEM_ID", "action_type": "DERIVE",
                              "action_value": "LTRIM(ITEM_ID, '0')"}])
        result = self.run_rule(Stub(rule), text="Remove the leading zeros from the ITEM_ID.")
        self.assertEqual("red", result["status"])
        self.assertTrue(any("LAYER" in r for r in result["reasons"]))

    def test_unknown_column_is_red_and_explained(self):
        rule = base(actions=[{"target_field": rx.UNKNOWN, "action_type": "SET_VALUE",
                              "action_value": "VF"}],
                    doubts=["The FDD says 'location' but names no column; PLANT_CD is the "
                            "closest candidate."])
        result = self.run_rule(Stub(rule), text="new record with location as VF", review=False)
        self.assertEqual("red", result["status"])
        column = next(i for i in result["issues"] if i["kind"] == "column")
        self.assertEqual("PLANT_CD", column["suggestion"]["patch"]["value"],
                         "'location' in the text points at PLANT_CD on the target")
        self.assertIn("PLANT_CD", column["choices"]["options"])

    def test_join_described_but_not_captured_is_amber(self):
        """A join the model skipped is recovered from the sentence when it names the
        object; when it does not ("join with metadata"), the rule is flagged."""
        recovered = self.run_rule(Stub(base(actions=[OWNER_ACTION])))
        self.assertEqual(1, len(recovered["proposal"]["joins"]))
        result = self.run_rule(
            Stub(base(actions=[OWNER_ACTION])),
            text="Join with metadata based on ITEM_CLASS_NM to get the OWNER_CD")
        join = next(i for i in result["issues"] if i["kind"] == "join")
        self.assertEqual("Add join", join["suggestion"]["label"])
        self.assertEqual(rx.METADATA_VIEW,
                         join["suggestion"]["patch"]["join"]["right_object"])

    def test_fdd_contradiction_is_reported_not_resolved(self):
        rule = base(actions=[{"target_field": "TURNKEY_IND", "action_type": "SET_VALUE",
                              "action_value": "Y"}],
                    fdd_issues=["Both lines say 'If item is UNBW' but give Y and N."])
        result = self.run_rule(Stub(rule), text="If item is UNBW TURNKEY_IND = 'Y'. "
                                                "If item is UNBW TURNKEY_IND = 'N'", review=False)
        self.assertEqual("amber", result["status"])
        self.assertEqual(["Both lines say 'If item is UNBW' but give Y and N."],
                         result["fdd_issues"])

    def test_reviewer_catching_a_reversed_mapping_makes_it_red(self):
        stub = Stub(base(actions=[OWNER_ACTION], joins=[OWNER_JOIN]),
                    findings=[{"severity": "high", "kind": "wrong",
                               "detail": "PLANT_CD should take PARENT_ITEM_PLANT_CD, not the reverse."}])
        result = self.run_rule(stub)
        self.assertEqual("red", result["status"])
        self.assertTrue(any(i["kind"] == "review_wrong" for i in result["issues"]))

    def test_model_failure_falls_back_without_crashing(self):
        original = rc.extract_with_llm
        rc.extract_with_llm = lambda *a, **k: (None, "")
        try:
            result = self.run_rule(lambda prompt, schema, model="": None, review=False)
        finally:
            rc.extract_with_llm = original
        self.assertIn(result["status"], ("green", "amber", "red"))

    def test_metadata_view_is_given_when_the_rule_says_metadata(self):
        """Rules 8 and 9 say "join BOM view with metadata" but never name the view."""
        COLUMNS[rx.METADATA_VIEW] = ["SUBJECT_AREA_NM", "ENTITY_NM", "BUSINESS_RULE_NM",
                                     "COLUMN_VALUE_ONE_TXT", "COLUMN_VALUE_THREE_TXT"]
        try:
            stub = Stub(base(actions=[{"target_field": "ITEM_LOCATION_TYPE_CD",
                                       "action_type": "COPY",
                                       "action_value": "COLUMN_VALUE_THREE_TXT"}]))
            self.run_rule(stub, text="join BOM view with metadata where SUBJECT_AREA_NM = "
                                     "'Item_location'. COLUMN_VALUE_THREE_TXT as "
                                     "ITEM_LOCATION_TYPE_CD", review=False)
            self.assertIn(rx.METADATA_VIEW + ": SUBJECT_AREA_NM", stub.prompts[0])
        finally:
            COLUMNS.pop(rx.METADATA_VIEW, None)

    def test_schema_enums_are_the_repositorys_own(self):
        props = rc.EXTRACTION_SCHEMA["properties"]
        self.assertEqual(rc.RULE_CATEGORIES, props["rule_category"]["enum"])
        self.assertEqual(rc.ACTION_TYPES,
                         props["actions"]["items"]["properties"]["action_type"]["enum"])
        self.assertEqual(rc.OPERATORS,
                         props["conditions"]["items"]["properties"]["operator"]["enum"])


class TestOneClickFixes(TestGrading):
    """A suggested fix is applied in place and the rule re-graded without a model."""

    def regrade(self, proposal, text, model_issues=()):
        return rc.regrade(text, proposal, sources=[X + "V_BRD_ITEM_LOCATION_ROOT"],
                          target=X + "V_BRD_ITEM_LOCATION_STAGE",
                          context={"objects": list(COLUMNS)}, model_issues=model_issues)

    def test_applying_the_suggested_column_turns_the_rule_green(self):
        text = ("Check if ITEM is an ITEM_ORDER_POINT, then a new record with location "
                "as 'VF' and 'C' as ITEM_LOCATION_TYPE_CD")
        rule = base(actions=[{"target_field": "LOC", "action_type": "SET_VALUE",
                              "action_value": "VF"}])
        result = self.run_rule(Stub(rule), text=text, review=False)
        column = next(i for i in result["issues"] if i["kind"] == "column")
        self.assertEqual("LOC is not a real column", column["title"])
        fixed = rx.apply_patch(result["proposal"], column["suggestion"]["patch"])
        again = self.regrade(fixed, text)
        self.assertFalse([i for i in again["issues"] if i["kind"] == "column"])
        self.assertEqual("green", again["status"], again["issues"])

    def test_picking_another_column_from_the_dropdown(self):
        rule = base(actions=[{"target_field": "LOC", "action_type": "SET_VALUE",
                              "action_value": "VF"}])
        result = self.run_rule(Stub(rule), text="location as VF", review=False)
        column = next(i for i in result["issues"] if i["kind"] == "column")
        patch = dict(column["choices"]["patch"], value="ITEM_LOCATION_TYPE_CD")
        fixed = rx.apply_patch(result["proposal"], patch)
        self.assertEqual("ITEM_LOCATION_TYPE_CD", fixed["actions"][0]["target_field"])

    def test_removing_the_part_that_cannot_be_fixed(self):
        rule = base(actions=[{"target_field": "LOC", "action_type": "SET_VALUE",
                              "action_value": "VF"}, OWNER_ACTION])
        result = self.run_rule(Stub(rule), text="location as VF", review=False)
        column = next(i for i in result["issues"] if i["kind"] == "column")
        fixed = rx.apply_patch(result["proposal"], column["remove"])
        self.assertEqual(["OWNER_CD"], [a["target_field"] for a in fixed["actions"]])

    def test_reviewer_patch_is_one_click(self):
        finding = {"severity": "high", "kind": "wrong",
                   "detail": "PLANT_CD should take PARENT_ITEM_PLANT_CD.",
                   "suggested_fix": "Copy PARENT_ITEM_PLANT_CD into PLANT_CD",
                   "patch": {"section": "actions", "index": 0, "field": "action_value",
                             "value": "PARENT_ITEM_PLANT_CD"}}
        rule = base(actions=[{"target_field": "PLANT_CD", "action_type": "COPY",
                              "action_value": "PLANT_CD"}])
        result = self.run_rule(Stub(rule, findings=[finding]), text="PARENT_ITEM_PLANT_CD as PLANT_CD")
        issue = next(i for i in result["issues"] if i["kind"] == "review_wrong")
        fixed = rx.apply_patch(result["proposal"], issue["suggestion"]["patch"])
        self.assertEqual("PARENT_ITEM_PLANT_CD", fixed["actions"][0]["action_value"])
        resolved = {issue["id"]: {"how": "fixed"}}
        again = self.regrade(fixed, "PARENT_ITEM_PLANT_CD as PLANT_CD",
                             model_issues=result["model_issues"])
        self.assertEqual("green", rx.status_of(again["issues"], resolved))

    def test_accepting_anyway_clears_the_status(self):
        rule = base(actions=[{"target_field": "LOC", "action_type": "SET_VALUE",
                              "action_value": "VF"}])
        result = self.run_rule(Stub(rule), text="location as VF", review=False)
        resolved = {i["id"]: {"how": "accepted", "reason": "known gap"}
                    for i in result["issues"]}
        self.assertEqual("green", rx.status_of(result["issues"], resolved))

    def test_duplicate_problems_are_shown_once(self):
        issues = rx.merge_issues(rx.model_issues(
            doubts=["The rule does not name the column for location; PLANT_CD is likely."],
            fdd_issues=["The FDD does not name the location column; PLANT_CD assumed."],
            findings=[{"severity": "high", "kind": "missing",
                       "detail": "Location column missing; should be PLANT_CD."}]))
        self.assertEqual(1, len(issues))
        self.assertEqual("fix", issues[0]["level"])
        self.assertTrue(issues[0].get("also_fdd"))

    def test_a_fix_also_settles_the_doubt_it_answers(self):
        issues = rx.merge_issues(rx.model_issues(
            doubts=["PLANT_CD is the closest column for location."]))
        patch = {"op": "set", "section": "actions", "index": 0, "field": "target_field",
                 "value": "PLANT_CD"}
        self.assertEqual([issues[0]["id"]],
                         [i["id"] for i in rx.related_model_issues(issues, patch)])
        other = {"op": "set", "section": "actions", "index": 0, "field": "target_field",
                 "value": "OWNER_CD"}
        self.assertEqual([], rx.related_model_issues(issues, other))

    def test_accepted_and_noted_problems_become_review_comments(self):
        entry = {"rule_number": "4", "resolutions": {
            "column:x": {"how": "accepted", "title": "LOC is not a real column",
                         "reason": "agreed with author"},
            "fdd:y": {"how": "noted", "title": "Y/N", "detail": "Both lines say UNBW."},
            "join:z": {"how": "fixed", "title": "Join added"}}}
        comments = fi.resolution_comments(entry, 99, "Stage Transformation")
        self.assertEqual(2, len(comments), "fixed problems need no comment")
        self.assertIn("accepted at import despite: LOC is not a real column", comments[0])
        self.assertIn("(reason: agreed with author)", comments[0])
        self.assertIn("FDD note - Both lines say UNBW.", comments[1])
        self.assertTrue(all(c.startswith("[Open] Rule 4 (id 99, Stage Transformation)")
                            for c in comments))

    def test_one_comment_per_rule_not_per_problem(self):
        entry = {"rule_number": "4", "resolutions": {
            f"column:{i}": {"how": "accepted", "title": f"C{i} is not a real column"}
            for i in range(5)}}
        comments = fi.resolution_comments(entry, 99, "Stage")
        self.assertEqual(1, len(comments))
        self.assertEqual("5", str(comments[0].count("is not a real column")))
        note = fi.classify_comment(comments[0])
        self.assertEqual(("import", "4", "99"), (note["kind"], note["rule"], note["rule_id"]))
        self.assertEqual("typed", fi.classify_comment("Please check PLANT_CD")["kind"])
        self.assertEqual("fdd", fi.classify_comment("[Open] Do we filter MDG?")["kind"])

    def test_titles_are_short(self):
        long = ("The rule says location as VF but does not name the specific column that "
                "holds the location value in the target schema at all.")
        title = rx.model_issues(doubts=[long])[0]["title"]
        self.assertLessEqual(len(title.split()), 13)


class TestPlanPassesContext(unittest.TestCase):
    def test_rich_compile_fn_gets_sources_and_context(self):
        seen = []

        def compile_fn(text, rule_number="", source_object="", target_object="",
                       sources=None, context=None):
            seen.append((target_object, sources, context))
            return {"proposal": {"rule_name": "x"}, "status": "amber",
                    "reasons": ["Model doubt: d"], "errors": [], "warnings": []}

        plan = fi.build_import_plan(fi.parse_document(FDD), "Item Location",
                                    compile_fn=compile_fn)
        substrate = [c for t, s, c in seen if c.get("section")]
        self.assertEqual(5, len(substrate))
        stage = [s for t, s, c in seen if leaf(t) == "V_BRD_ITEM_LOCATION_STAGE"][0]
        self.assertIn(A + "V_BRD_ITEM_CORE_PIT", stage)
        self.assertEqual(len(seen), plan["summary"]["status_counts"]["amber"])
        upi = next(rs for rs in plan["rule_sets"]
                   if leaf(rs["target_technical_object"]) == "V_BRD_UPI_ITEM_LOCATION_STAGE")
        self.assertTrue(upi["declared_unused"])

    def test_legacy_compile_fn_still_works(self):
        def compile_fn(text, rule_number="", source_object="", target_object=""):
            return {"proposal": {"rule_name": "x"}, "errors": [], "warnings": []}

        plan = fi.build_import_plan(fi.parse_document(FDD), "Item Location",
                                    compile_fn=compile_fn)
        self.assertEqual(0, plan["summary"]["status_counts"]["red"])


if __name__ == "__main__":
    unittest.main()
