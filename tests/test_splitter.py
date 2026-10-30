"""Tests for epm_policy_splitter. All data is synthetic (see tests/synthetic.py)."""

import contextlib
import copy
import io
import json
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import epm_policy_splitter as s  # noqa: E402
import synthetic as syn  # noqa: E402

FIXTURES = HERE / "fixtures"
SMALL = 10  # cap patched in for the small fixtures (the real cap is fixed at 900)


def load(name):
    with open(FIXTURES / name, encoding="utf-8-sig") as f:
        return json.load(f)


def run(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = s.main([str(a) for a in argv])
    return code, out.getvalue()


def names(objs):
    return [o["Name"] for o in objs]


def counts_after(out, data):
    """Group sizes as the importer would see them: output groups, else input groups."""
    sizes = {g["Name"]: len(g["Applications"]) for g in data["AppGroups"]}
    sizes.update({g["Name"]: len(g["Applications"]) for g in out["AppGroups"]})
    return sizes


def cost(policy, sizes):
    """Real counting rule: entry = 1, group reference = 1 + apps in the group."""
    return sum(1 + sizes[a["displayName"]] if a["applicationType"] == 2 else 1
               for a in policy["Applications"])


def small_cap():
    """Patch the fixed cap down to SMALL so tiny fixtures exercise the splitting."""
    return mock.patch.object(s, "MAX_DEFINITIONS", SMALL)


class SmallFixtureTests(unittest.TestCase):
    """mixed.epmp with the cap patched to 10."""

    def setUp(self):
        self.data = load("mixed.epmp")
        with small_cap():
            self.out, self.report = s.process_epmp(copy.deepcopy(self.data))

    def test_group_split_with_prefix_and_balanced(self):
        self.assertEqual(names(self.out["AppGroups"]),
                         ["Split_1_Test Group Big", "Split_2_Test Group Big"])
        self.assertEqual([len(g["Applications"]) for g in self.out["AppGroups"]], [6, 6])
        for g in self.out["AppGroups"]:
            self.assertEqual(g["PolicyType"], 14)
            self.assertEqual(g["LinkedAgentPolicies"], [])
            self.assertNotEqual(g["Id"], syn.guid(1, 1))

    def test_policy_pieces_prefixed_and_order_preserved(self):
        pieces = [p for p in self.out["Policies"] if p["Name"].endswith("_Test Policy Big")]
        self.assertEqual(names(pieces), ["Split_1_Test Policy Big", "Split_2_Test Policy Big"])
        orig = [a["id"] for a in self.data["Policies"][0]["Applications"]]
        self.assertEqual([a["id"] for p in pieces for a in p["Applications"]], orig)

    def test_mac_policy_split(self):
        pieces = [p for p in self.out["Policies"] if p["Name"].endswith("_Test Mac Policy Big")]
        self.assertEqual([len(p["Applications"]) for p in pieces], [6, 6])
        for p in pieces:
            self.assertEqual(p["PolicyType"], 13)
            self.assertEqual(p["LinkedAgentPolicies"][0]["PolicyType"], 1003)

    def test_untouched_and_unsupported_absent(self):
        all_names = names(self.out["Policies"]) + names(self.out["AppGroups"])
        for n in ("Test Policy Untouched", "Test Group Small", "Test Group Unused",
                  "Test Other Policy No Apps", "Test Policy Big", "Test Group Big"):
            self.assertNotIn(n, all_names)
        self.assertEqual(self.report["skipped_policies"],
                         [("Test Other Policy No Apps", "no Applications key")])
        self.assertEqual(self.report["referenced_groups"], ["Test Group Small"])

    def test_every_output_policy_within_limit(self):
        sizes = counts_after(self.out, self.data)
        for p in self.out["Policies"]:
            self.assertLessEqual(cost(p, sizes), SMALL, p["Name"])
        self.assertEqual(self.report["over_limit"], [])

    def test_self_entry(self):
        orig_types = {p["Name"]: p["LinkedAgentPolicies"][0]["PolicyType"]
                      for p in self.data["Policies"]}
        internal = []
        for p in self.out["Policies"]:
            (entry,) = p["LinkedAgentPolicies"]
            self.assertEqual(entry["Id"], p["Id"])
            original = p["Name"].split("_", 2)[2]
            self.assertEqual(entry["PolicyType"], orig_types[original])
            internal.append(entry["InternalId"])
        self.assertEqual(len(internal), len(set(internal)))
        self.assertTrue(all(i >= s.SYNTHETIC_INTERNAL_ID_BASE for i in internal))

    def test_group_refs_point_at_pieces(self):
        piece_ids = {g["Name"]: g["Id"] for g in self.out["AppGroups"]}
        refs = [a for p in self.out["Policies"] for a in p["Applications"]
                if a["applicationType"] == 2 and a["displayName"] in piece_ids]
        self.assertEqual(sorted(a["displayName"] for a in refs), sorted(piece_ids))
        for a in refs:
            self.assertEqual(a["id"], piece_ids[a["displayName"]])
            self.assertEqual(a["patterns"], {})

    def test_unsplit_group_ref_unchanged(self):
        refs = [a for p in self.out["Policies"] for a in p["Applications"]
                if a.get("displayName") == "Test Group Small"]
        self.assertEqual([a["id"] for a in refs], [syn.guid(1, 2)])

    def test_ids_new_and_unique(self):
        ids = [o["Id"] for o in self.out["Policies"] + self.out["AppGroups"]]
        self.assertEqual(len(ids), len(set(ids)))
        orig = {o["Id"] for o in self.data["Policies"] + self.data["AppGroups"]}
        self.assertFalse(orig & set(ids))

    def test_top_level_keys_and_null_end_user_uis(self):
        self.assertEqual(list(self.out), list(self.data))
        self.assertIsNone(self.out["EndUserUIs"])
        self.assertEqual(self.out["TrustSoftwareDistributors"], [])

    def test_all_fields_preserved_on_pieces(self):
        orig = {p["Name"]: p for p in self.data["Policies"]}
        for p in self.out["Policies"]:
            self.assertEqual(set(p), set(orig[p["Name"].split("_", 2)[2]]))

    def test_include_referenced_groups(self):
        with small_cap():
            out, report = s.process_epmp(load("mixed.epmp"), include_referenced_groups=True)
        self.assertIn("Test Group Small", names(out["AppGroups"]))
        self.assertNotIn("Test Group Unused", names(out["AppGroups"]))
        self.assertEqual(report["name_collisions"], [])


class RealSizeTests(unittest.TestCase):
    """The examples/ input: 1,200-app group and 1,000-entry policies, fixed 900 cap."""

    @classmethod
    def setUpClass(cls):
        cls.data = syn.oversized_example()
        cls.out, cls.report = s.process_epmp(copy.deepcopy(cls.data))

    def test_group_split_equally(self):
        self.assertEqual([(g["Name"], len(g["Applications"])) for g in self.out["AppGroups"]],
                         [("Split_1_Test Group Big", 600), ("Split_2_Test Group Big", 600)])

    def test_policies_split_equally(self):
        sizes = counts_after(self.out, self.data)
        got = {p["Name"]: cost(p, sizes) for p in self.out["Policies"]}
        self.assertEqual(got, {
            "Split_1_Test Policy Uses Big Group": 601, "Split_2_Test Policy Uses Big Group": 603,
            "Split_1_Test Policy Big": 500, "Split_2_Test Policy Big": 500,
            "Split_1_Test Mac Policy Big": 500, "Split_2_Test Mac Policy Big": 500,
        })
        self.assertTrue(all(c <= 900 for c in got.values()))

    def test_untouched_absent(self):
        self.assertNotIn("Test Policy Small", names(self.out["Policies"]))
        self.assertNotIn("Test Group Small", names(self.out["AppGroups"]))

    def test_name_collision_warning(self):
        data = syn.oversized_example()
        data["Policies"].append(syn.win_policy(9, "Split_1_Test Policy Big", 1, [], 90000009))
        _, report = s.process_epmp(data)
        self.assertIn("Split_1_Test Policy Big", report["name_collisions"])

    def test_unknown_policy_type_warned(self):
        data = syn.oversized_example()
        data["Policies"][3]["PolicyType"] = 77
        _, report = s.process_epmp(data)
        self.assertIn(("policy", "Test Policy Small", 77), report["unknown_types"])

    def test_stale_parent_process_reference_warned(self):
        data = syn.oversized_example()
        app = data["Policies"][2]["Applications"][0]
        app["patterns"]["PARENT_PROCESS"] = {"@type": "ParentProcess", "AllAncestors": True,
                                             "ApplicationGroupId": syn.guid(1, 1),
                                             "ApplicationGroupName": "Test Group Big",
                                             "isEmpty": False}
        _, report = s.process_epmp(data)
        self.assertEqual(len(report["stale_group_refs"]), 1)


class SplitAlgorithmTests(unittest.TestCase):
    def test_balanced_unit_costs(self):
        for total in (901, 1000, 1801, 2700, 2701):
            bounds = s.contiguous_balanced_split([1] * total, 900)
            sizes = [e - st for st, e in bounds]
            self.assertEqual(len(sizes), -(-total // 900))
            self.assertLessEqual(max(sizes) - min(sizes), 1)
            self.assertEqual(sum(sizes), total)

    def test_heavy_entries_never_exceed(self):
        costs = [600, 1, 1, 600, 1, 300, 300]
        bounds = s.contiguous_balanced_split(costs, 900)
        self.assertTrue(all(sum(costs[a:b]) <= 900 for a, b in bounds))
        self.assertEqual(bounds[-1][1], len(costs))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cap = small_cap()
        self.cap.start()

    def tearDown(self):
        self.cap.stop()
        shutil.rmtree(self.tmp)

    def test_no_limit_option(self):
        with self.assertRaises(SystemExit) as cm:
            run([FIXTURES / "mixed.epmp", self.tmp / "o.epmp", "--limit", 50])
        self.assertEqual(cm.exception.code, 2)
        self.assertFalse((self.tmp / "o.epmp").exists())
        self.assertEqual(s.process_epmp.__code__.co_varnames[:2], ("data", "include_referenced_groups"))

    def test_bom_input_and_compact_output(self):
        bom = self.tmp / "bom.epmp"
        bom.write_bytes(b"\xef\xbb\xbf" + (FIXTURES / "mixed.epmp").read_bytes())
        out = self.tmp / "o.epmp"
        code, log = run([bom, out])
        self.assertEqual(code, 0, log)
        text = out.read_text(encoding="utf-8")
        self.assertNotIn(", ", text[:200])
        self.assertIn('"EndUserUIs":null', text)

    def test_refuses_existing_output(self):
        out = self.tmp / "o.epmp"
        out.write_text("keep me")
        code, _ = run([FIXTURES / "mixed.epmp", out])
        self.assertEqual(code, 2)
        self.assertEqual(out.read_text(), "keep me")
        code, _ = run([FIXTURES / "mixed.epmp", out, "--force"])
        self.assertEqual(code, 0)
        self.assertIn("Policies", json.loads(out.read_text()))

    def test_refuses_input_as_output(self):
        inp = self.tmp / "in.epmp"
        shutil.copy(FIXTURES / "mixed.epmp", inp)
        before = inp.read_text()
        code, _ = run([inp, inp])
        self.assertEqual(code, 2)
        self.assertEqual(inp.read_text(), before)

    def test_nothing_to_split_writes_nothing(self):
        out = self.tmp / "o.epmp"
        code, log = run([FIXTURES / "nothing_to_split.epmp", out])
        self.assertEqual(code, 0)
        self.assertFalse(out.exists())
        self.assertIn("No output written", log)

    def test_over_limit_exits_1_without_writing(self):
        orig = s.get_import_count
        s.get_import_count = lambda a, c: SMALL + 5  # every entry alone is over the limit
        try:
            out = self.tmp / "o.epmp"
            code, log = run([FIXTURES / "mixed.epmp", out])
        finally:
            s.get_import_count = orig
        self.assertEqual(code, 1, log)
        self.assertFalse(out.exists())

    def test_malformed_json(self):
        bad = self.tmp / "bad.epmp"
        bad.write_text("{not json")
        code, _ = run([bad, self.tmp / "o.epmp"])
        self.assertEqual(code, 2)

    def test_summary_has_checklist(self):
        code, log = run([FIXTURES / "mixed.epmp", self.tmp / "o.epmp"])
        self.assertEqual(code, 0)
        self.assertIn("Post-import checklist", log)
        self.assertIn("Delete the original AppGroup 'Test Group Big'", log)
        self.assertIn("Delete the original policy 'Test Mac Policy Big'", log)
        self.assertIn("Skipped policies", log)

    def test_example_files_match_generator(self):
        ex = HERE.parent / "examples" / "test_oversized_input.epmp"
        if not ex.exists():
            self.skipTest("examples not generated")
        self.assertEqual(json.loads(ex.read_text(encoding="utf-8")), syn.oversized_example())


if __name__ == "__main__":
    unittest.main()
