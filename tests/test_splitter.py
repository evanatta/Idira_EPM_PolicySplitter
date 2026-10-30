"""Tests for epm_policy_splitter. All fixtures are synthetic (fake names/Ids)."""

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import epm_policy_splitter as s  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LIMIT = 10  # small limit so fixtures stay tiny


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


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.data = load("mixed.epmp")
        self.out, self.report = s.process_epmp(self.data, LIMIT)

    def test_group_over_limit_split_into_new_pieces(self):
        self.assertEqual(names(self.out["AppGroups"]), ["Fake Group Big_1", "Fake Group Big_2"])
        sizes = [len(g["Applications"]) for g in self.out["AppGroups"]]
        self.assertEqual(sizes, [6, 6])
        self.assertTrue(all(sz <= LIMIT - 1 for sz in sizes))
        ids = {g["Id"] for g in self.out["AppGroups"]}
        self.assertNotIn("00000000-0000-4000-8000-000000000001", ids)
        self.assertTrue(all(g["LinkedAgentPolicies"] == [] for g in self.out["AppGroups"]))

    def test_policy_over_limit_split(self):
        big = [p for p in self.out["Policies"] if p["Name"].startswith("Fake Policy Big_")]
        self.assertEqual(len(big), 2)
        apps = [a for p in big for a in p["Applications"]]
        self.assertEqual(len(apps), 12)  # nothing lost or duplicated

    def test_untouched_objects_absent(self):
        all_names = names(self.out["Policies"]) + names(self.out["AppGroups"])
        for n in ("Fake Policy Untouched", "Fake Group Small", "Fake Group Unused",
                  "Fake Policy Big", "Fake Group Big"):
            self.assertNotIn(n, all_names)
        self.assertEqual(self.report["referenced_groups"], ["Fake Group Small"])

    def test_all_output_policies_within_limit(self):
        self.assertEqual(self.report["over_limit"], [])
        self.assertLessEqual(self.report["max_count"], LIMIT)

    def test_group_refs_carry_piece_ids(self):
        piece_ids = {g["Name"]: g["Id"] for g in self.out["AppGroups"]}
        refs = [a for p in self.out["Policies"] for a in p["Applications"]
                if a.get("applicationType") == 2 and a["displayName"] in piece_ids]
        self.assertEqual(sorted(a["displayName"] for a in refs), sorted(piece_ids))
        for a in refs:
            self.assertEqual(a["id"], piece_ids[a["displayName"]])

    def test_unsplit_group_ref_keeps_original_id(self):
        refs = [a for p in self.out["Policies"] for a in p["Applications"]
                if a.get("displayName") == "Fake Group Small"]
        self.assertEqual([a["id"] for a in refs], ["00000000-0000-4000-8000-000000000002"])

    def test_new_policy_ids_unique_and_new(self):
        ids = [p["Id"] for p in self.out["Policies"]]
        self.assertEqual(len(ids), len(set(ids)))
        orig = {p["Id"] for p in self.data["Policies"]}
        self.assertFalse(orig & set(ids))

    def test_top_level_keys_preserved_other_lists_emptied(self):
        self.assertEqual(list(self.out), list(self.data))
        self.assertEqual(self.out["FakeHeader"], self.data["FakeHeader"])
        self.assertEqual(self.out["FakeOtherSection"], [])

    def test_input_not_mutated(self):
        self.assertEqual(self.data, load("mixed.epmp"))

    def test_include_referenced_groups(self):
        out, report = s.process_epmp(load("mixed.epmp"), LIMIT, include_referenced_groups=True)
        self.assertIn("Fake Group Small", names(out["AppGroups"]))
        self.assertNotIn("Fake Group Unused", names(out["AppGroups"]))
        self.assertEqual(report["name_collisions"], [])

    def test_reference_own_id_kept_on_first_piece(self):
        data = load("mixed.epmp")
        ref = data["Policies"][1]["Applications"][0]
        ref["id"] = "ref-own-id"  # not the group's Id
        out, _ = s.process_epmp(data, LIMIT)
        refs = [a for p in out["Policies"] for a in p["Applications"]
                if a.get("displayName", "").startswith("Fake Group Big_")]
        by_name = {a["displayName"]: a["id"] for a in refs}
        self.assertEqual(by_name["Fake Group Big_1"], "ref-own-id")
        self.assertNotEqual(by_name["Fake Group Big_2"], "ref-own-id")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_bom_input(self):
        code, log = run([FIXTURES / "bom.epmp", self.tmp / "o.epmp", "--limit", LIMIT])
        self.assertEqual(code, 0, log)
        self.assertTrue((self.tmp / "o.epmp").exists())

    def test_refuses_existing_output(self):
        out = self.tmp / "o.epmp"
        out.write_text("keep me")
        code, _ = run([FIXTURES / "mixed.epmp", out, "--limit", LIMIT])
        self.assertEqual(code, 2)
        self.assertEqual(out.read_text(), "keep me")
        code, _ = run([FIXTURES / "mixed.epmp", out, "--limit", LIMIT, "--force"])
        self.assertEqual(code, 0)
        self.assertIn("Policies", json.loads(out.read_text()))

    def test_refuses_input_as_output(self):
        inp = self.tmp / "in.epmp"
        shutil.copy(FIXTURES / "mixed.epmp", inp)
        before = inp.read_text()
        code, _ = run([inp, inp, "--limit", LIMIT])
        self.assertEqual(code, 2)
        self.assertEqual(inp.read_text(), before)

    def test_nothing_to_split_writes_nothing(self):
        out = self.tmp / "o.epmp"
        code, log = run([FIXTURES / "nothing_to_split.epmp", out, "--limit", LIMIT])
        self.assertEqual(code, 0)
        self.assertFalse(out.exists())
        self.assertIn("No output written", log)

    def test_over_limit_exits_1_without_writing(self):
        data = load("mixed.epmp")
        # Force an unsplittable result: every entry alone costs more than the limit.
        orig = s.get_import_count
        s.get_import_count = lambda a, c: LIMIT + 5
        try:
            inp = self.tmp / "in.epmp"
            inp.write_text(json.dumps(data))
            out = self.tmp / "o.epmp"
            code, log = run([inp, out, "--limit", LIMIT])
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
        code, log = run([FIXTURES / "mixed.epmp", self.tmp / "o.epmp", "--limit", LIMIT])
        self.assertEqual(code, 0)
        self.assertIn("Post-import checklist", log)
        self.assertIn("Delete the original AppGroup 'Fake Group Big'", log)
        self.assertIn("LinkedAgentPolicies", log)


if __name__ == "__main__":
    unittest.main()
