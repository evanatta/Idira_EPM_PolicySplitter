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
            "Split_1_Test Policy Parent Process": 7,
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

    def test_parent_process_policy_follows_pieces(self):
        (p,) = [p for p in self.out["Policies"] if p["Name"].endswith("Test Policy Parent Process")]
        self.assertEqual(p["Name"], "Split_1_Test Policy Parent Process")
        pieces = [(g["Id"], g["Name"]) for g in self.out["AppGroups"]]
        got = [(a["patterns"]["PARENT_PROCESS"]["ApplicationGroupId"],
                a["patterns"]["PARENT_PROCESS"]["ApplicationGroupName"])
               for a in p["Applications"] if "PARENT_PROCESS" in a["patterns"]]
        self.assertEqual(got, pieces * 3)
        self.assertEqual(self.report["stale_group_refs"], [])
        self.assertIn(("policy", "Test Policy Parent Process", 3),
                      self.report["parent_process_rewritten"])


def pp_of(entry):
    return entry["patterns"].get("PARENT_PROCESS")


class ParentProcessTests(unittest.TestCase):
    """PARENT_PROCESS patterns that point at a split group (cap patched to 10)."""

    def setUp(self):
        self.cap = small_cap()
        self.cap.start()
        # 12 apps > 9 -> two pieces of 6.
        self.big = syn.group(1, "Test Group Parents", [syn.win_app(i) for i in range(1, 13)])
        self.other = syn.group(2, "Test Group Other", [syn.win_app(i) for i in range(20, 23)])

    def tearDown(self):
        self.cap.stop()

    def run_doc(self, policies, groups=None):
        data = syn.document(policies, groups or [self.big, self.other])
        before = copy.deepcopy(data)
        out, report = s.process_epmp(data)
        self.assertEqual(data, before, "input must not be mutated")
        return out, report

    def piece_targets(self, out):
        return [(g["Id"], g["Name"]) for g in out["AppGroups"]
                if g["Name"].endswith("_Test Group Parents")]

    def test_entry_cloned_per_piece_in_place(self):
        apps = [syn.win_app(100), syn.with_parent(syn.win_app(101), self.big, False),
                syn.with_parent(syn.win_app(102), self.other), syn.win_app(103)]
        out, report = self.run_doc([syn.win_policy(1, "Test PP Policy", 2, apps, 1)])
        (p,) = out["Policies"]
        self.assertEqual(p["Name"], "Split_1_Test PP Policy")
        got = p["Applications"]
        self.assertEqual(len(got), 5)
        self.assertEqual(got[0], apps[0])
        self.assertEqual(got[3], apps[2], "PARENT_PROCESS to an unsplit group is untouched")
        self.assertEqual(got[4], apps[3])
        targets = self.piece_targets(out)
        for clone, (gid, gname) in zip(got[1:3], targets):
            self.assertEqual((pp_of(clone)["ApplicationGroupId"],
                              pp_of(clone)["ApplicationGroupName"]), (gid, gname))
            self.assertFalse(pp_of(clone)["AllAncestors"])
            rest = copy.deepcopy(clone)
            orig = copy.deepcopy(apps[1])
            for e in (rest, orig):
                e.pop("id")
                del e["patterns"]["PARENT_PROCESS"]["ApplicationGroupId"]
                del e["patterns"]["PARENT_PROCESS"]["ApplicationGroupName"]
            self.assertEqual(rest, orig, "all other fields kept")
        self.assertEqual(got[1]["id"], apps[1]["id"])
        self.assertNotEqual(got[2]["id"], apps[1]["id"])
        self.assertEqual(report["policies_associated"][0]["count"], 5)
        self.assertEqual(report["stale_group_refs"], [])

    def test_split_policy_rewritten_then_split_equally(self):
        # 9 direct + 1 PP entry = 10, at the cap; the rewrite makes 11 -> 6 + 5.
        apps = [syn.win_app(100 + i) for i in range(9)]
        apps.insert(4, syn.with_parent(syn.win_app(200), self.big))
        out, report = self.run_doc([syn.win_policy(1, "Test PP Policy", 1, apps, 1)])
        self.assertEqual([len(p["Applications"]) for p in out["Policies"]], [6, 5])
        self.assertEqual(names(out["Policies"]),
                         ["Split_1_Test PP Policy", "Split_2_Test PP Policy"])
        flat = [a for p in out["Policies"] for a in p["Applications"]]
        self.assertEqual([pp_of(a)["ApplicationGroupId"] for a in flat if pp_of(a)],
                         [gid for gid, _ in self.piece_targets(out)])
        self.assertEqual(report["policies_split"][0]["count"], 10)
        self.assertEqual(report["over_limit"], [])

    def test_name_only_pattern_matched_by_name(self):
        app = syn.with_parent(syn.win_app(100), self.big)
        app["patterns"]["PARENT_PROCESS"]["ApplicationGroupId"] = ""
        out, _ = self.run_doc([syn.win_policy(1, "Test PP Policy", 2, [app], 1)])
        self.assertEqual([pp_of(a)["ApplicationGroupName"] for a in out["Policies"][0]["Applications"]],
                         [n for _, n in self.piece_targets(out)])

    def test_group_with_pattern_is_copied_and_refs_follow(self):
        holder = syn.group(3, "Test Group Holder",
                           [syn.with_parent(syn.win_app(300), self.big), syn.win_app(301)])
        policy = syn.win_policy(1, "Test Uses Holder", 1, [syn.group_ref(holder)], 1)
        out, report = self.run_doc([policy], [self.big, self.other, holder])
        (copy_,) = [g for g in out["AppGroups"] if g["Name"].endswith("Test Group Holder")]
        self.assertEqual(copy_["Name"], "Split_1_Test Group Holder")
        self.assertEqual(len(copy_["Applications"]), 3)
        self.assertEqual([pp_of(a)["ApplicationGroupId"] for a in copy_["Applications"] if pp_of(a)],
                         [gid for gid, _ in self.piece_targets(out)])
        (p,) = out["Policies"]
        self.assertEqual([(a["id"], a["displayName"]) for a in p["Applications"]],
                         [(copy_["Id"], copy_["Name"])])
        self.assertEqual(report["groups_associated"][0]["name"], "Test Group Holder")
        self.assertEqual(report["stale_group_refs"], [])
        code_log = io.StringIO()
        with contextlib.redirect_stdout(code_log):
            s.print_checklist(report)
        self.assertIn("Delete the original AppGroup 'Test Group Holder'", code_log.getvalue())

    def test_unknown_field_still_warned(self):
        policy = syn.win_policy(1, "Test PP Policy", 2,
                                [syn.with_parent(syn.win_app(100), self.big)], 1)
        policy["SomeFutureField"] = {"GroupId": self.big["Id"]}
        _, report = self.run_doc([policy])
        self.assertEqual(report["stale_group_refs"],
                         [("Split_1_Test PP Policy", "SomeFutureField.GroupId")])

    def test_cycle_between_split_groups_warned(self):
        a = syn.group(5, "Test Group A", [syn.win_app(i) for i in range(400, 412)])
        b = syn.group(6, "Test Group B", [syn.win_app(i) for i in range(500, 512)])
        a["Applications"][0] = syn.with_parent(a["Applications"][0], b)
        b["Applications"][0] = syn.with_parent(b["Applications"][0], a)
        _, report = self.run_doc([], [a, b])
        self.assertEqual(len(report["stale_group_refs"]), 1)

    def test_chain_of_groups_resolved_in_order(self):
        # Holder listed before the big group it points at.
        holder = syn.group(3, "Test Group Holder", [syn.with_parent(syn.win_app(300), self.big)])
        out, report = self.run_doc([], [holder, self.big])
        self.assertEqual(report["stale_group_refs"], [])
        (copy_,) = [g for g in out["AppGroups"] if g["Name"] == "Split_1_Test Group Holder"]
        self.assertEqual(len(copy_["Applications"]), 2)


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
