#!/usr/bin/env python3
"""Check examples/test_oversized_input_split.epmp against the input (synthetic)."""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
inp = json.loads((HERE / "test_oversized_input.epmp").read_text(encoding="utf-8"))
out = json.loads((HERE / "test_oversized_input_split.epmp").read_text(encoding="utf-8"))
ok = True


def check(cond, msg):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + msg)


sizes = {g["Name"]: len(g["Applications"]) for g in inp["AppGroups"]}
sizes.update({g["Name"]: len(g["Applications"]) for g in out["AppGroups"]})
group_ids = {g["Id"]: g["Name"] for g in out["AppGroups"]}
orig = {p["Name"]: p for p in inp["Policies"]}
orig_ids = {o["Id"] for o in inp["Policies"] + inp["AppGroups"]}

check(list(out) == list(inp), "same top-level keys in the same order")
check(out["EndUserUIs"] is None, "EndUserUIs null preserved")
check(all(g["Name"].startswith("Split_") for g in out["AppGroups"]), "all groups Split_ prefixed")
check(all(p["Name"].startswith("Split_") for p in out["Policies"]), "all policies Split_ prefixed")
check("Test Policy Small" not in [p["Name"] for p in out["Policies"]], "untouched policy absent")
check("Test Group Small" not in [g["Name"] for g in out["AppGroups"]], "untouched group absent")
for p in out["Policies"]:
    c = sum(1 + sizes[a["displayName"]] if a["applicationType"] == 2 else 1
            for a in p["Applications"])
    src = orig[p["Name"].split("_", 2)[2]]
    (self_entry,) = p["LinkedAgentPolicies"]
    check(c <= 900, f"{p['Name']}: {c} definitions <= 900")
    check(self_entry["Id"] == p["Id"] and
          self_entry["PolicyType"] == src["LinkedAgentPolicies"][0]["PolicyType"],
          f"{p['Name']}: self-entry Id/PolicyType ok ({self_entry['PolicyType']}, "
          f"InternalId {self_entry['InternalId']})")
    check(set(p) == set(src), f"{p['Name']}: same field set as original")
    for a in p["Applications"]:
        if a["applicationType"] == 2 and a["displayName"].startswith("Split_"):
            check(group_ids.get(a["id"]) == a["displayName"],
                  f"{p['Name']}: ref {a['displayName']} resolves to piece Id")
ids = [o["Id"] for o in out["Policies"] + out["AppGroups"]]
check(len(ids) == len(set(ids)) and not set(ids) & orig_ids, "all Ids new and unique")
internal = [p["LinkedAgentPolicies"][0]["InternalId"] for p in out["Policies"]]
check(len(internal) == len(set(internal)), "InternalIds unique")
pieces = [(g["Id"], g["Name"]) for g in out["AppGroups"]]
(pp_policy,) = [p for p in out["Policies"] if p["Name"].endswith("_Test Policy Parent Process")]
targets = [(a["patterns"]["PARENT_PROCESS"]["ApplicationGroupId"],
            a["patterns"]["PARENT_PROCESS"]["ApplicationGroupName"])
           for a in pp_policy["Applications"] if "PARENT_PROCESS" in a["patterns"]]
check(targets == pieces * 3, "Test Policy Parent Process: each PARENT_PROCESS entry "
      "cloned once per Test Group Big piece, in order")
check(not any(i in json.dumps(out) for i in orig_ids), "no original Id left in the output")
for name in ("Test Policy Uses Big Group", "Test Policy Big", "Test Mac Policy Big"):
    src_ids = [a["id"] for a in orig[name]["Applications"] if a["applicationType"] != 2]
    got = [a["id"] for p in out["Policies"] if p["Name"].endswith("_" + name)
           for a in p["Applications"] if a["applicationType"] != 2]
    check(src_ids == got, f"{name}: direct entries kept once, in order")
apps = [a["id"] for g in out["AppGroups"] for a in g["Applications"]]
check(apps == [a["id"] for a in inp["AppGroups"][0]["Applications"]],
      "Test Group Big: 1200 apps kept once, in order")
print("OVERALL:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
