#!/usr/bin/env python3
"""Split Idira EPM policy exports so no policy exceeds the importer definition limit.

Input: an .epmp export (JSON). Output: an .epmp that contains ONLY the split
objects: new policies and AppGroups named Split_1_<name>, Split_2_<name>, ...
Everything that did not need splitting stays in the tenant untouched; the
originals that were split are deleted by the admin after import. See README.md.
"""

import argparse
import copy
import json
import sys
import uuid
from pathlib import Path

MAX_DEFINITIONS = 900  # fixed cap per policy; the importer's hard cap is 1000
GROUP_REF_TYPE = 2
KNOWN_POLICY_TYPES = {11: "Windows", 13: "macOS", 53: "other"}
KNOWN_GROUP_TYPES = {14: "Windows", 16: "macOS"}
SYNTHETIC_INTERNAL_ID_BASE = 99000000  # new InternalIds start at or above this

EXIT_OK = 0
EXIT_OVER_LIMIT = 1
EXIT_BAD_INPUT = 2


def split_name(index, name):
    return f"Split_{index}_{name}"


def get_import_count(app_entry, ag_counts):
    """Importer cost of one policy entry: 1, or 1 + group size for a group ref."""
    if app_entry.get("applicationType") == GROUP_REF_TYPE:
        return 1 + ag_counts.get(app_entry.get("displayName"), 0)
    return 1


def policy_count(policy, ag_counts):
    return sum(get_import_count(a, ag_counts) for a in (policy.get("Applications") or []))


def even_chunk_sizes(total, max_size):
    """ceil(total / max_size) sizes that differ by at most 1."""
    if total <= max_size:
        return [total]
    num_chunks = (total + max_size - 1) // max_size
    base, extra = divmod(total, num_chunks)
    return [base + 1 if i < extra else base for i in range(num_chunks)]


def contiguous_balanced_split(costs, limit):
    """Cut a list of entry costs into contiguous, order-preserving pieces.

    Uses ceil(total / limit) pieces when possible (more only if heavy entries
    make that impossible). Each piece aims at an equal share of the remaining
    cost and never exceeds the limit unless a single entry alone does.
    Returns a list of (start, end) index pairs.
    """
    total = sum(costs)
    if total <= limit:
        return [(0, len(costs))]
    k = (total + limit - 1) // limit
    while True:
        bounds, start, remaining = [], 0, total
        for piece in range(k):
            left = k - piece
            if left == 1:
                end = len(costs)
            else:
                target = (remaining + left - 1) // left
                end, cur = start, 0
                while end < len(costs):
                    nxt = cur + costs[end]
                    if cur and (nxt > limit or abs(nxt - target) > abs(cur - target)):
                        break
                    cur = nxt
                    end += 1
                    if cur >= target:
                        break
            bounds.append((start, end))
            remaining -= sum(costs[start:end])
            start = end
        fits = all(sum(costs[s:e]) <= limit or e - s == 1 for s, e in bounds)
        if (fits and start == len(costs)) or k >= len(costs):
            return [(s, e) for s, e in bounds if e > s]
        k += 1


class IdFactory:
    """New GUIDs and synthetic, unique InternalIds for the self-entries."""

    def __init__(self, data):
        existing = [0]
        for p in data.get("Policies") or []:
            for link in (p.get("LinkedAgentPolicies") or []) if isinstance(p, dict) else []:
                if isinstance(link, dict) and isinstance(link.get("InternalId"), int):
                    existing.append(link["InternalId"])
        self.next_internal = max(max(existing) + 1, SYNTHETIC_INTERNAL_ID_BASE)

    @staticmethod
    def guid(like=None):
        new = str(uuid.uuid4())
        if isinstance(like, str) and like.upper() == like and any(c.isalpha() for c in like):
            new = new.upper()
        return new

    def internal_id(self):
        value = self.next_internal
        self.next_internal += 1
        return value


def make_group_piece(ag, index, applications, ids):
    piece = copy.deepcopy(ag)  # keeps every field, incl. LinkedAgentPolicies as-is
    piece["Id"] = ids.guid(ag.get("Id"))
    piece["Name"] = split_name(index, ag.get("Name", "Unnamed"))
    piece["Applications"] = applications
    return piece


def make_policy_piece(policy, index, applications, ids):
    """Deep copy with new Id, prefixed Name, and a fresh LinkedAgentPolicies self-entry."""
    piece = copy.deepcopy(policy)
    piece["Id"] = ids.guid(policy.get("Id"))
    piece["Name"] = split_name(index, policy.get("Name", "Unnamed"))
    piece["Applications"] = applications
    links = policy.get("LinkedAgentPolicies")
    if isinstance(links, list) and links and isinstance(links[0], dict):
        entry = copy.deepcopy(links[0])
        entry["Id"] = piece["Id"]
        entry["InternalId"] = ids.internal_id()
        piece["LinkedAgentPolicies"] = [entry]
    return piece


def split_appgroup(ag, ids):
    """New pieces for a group with more than MAX_DEFINITIONS - 1 apps, else [].

    A policy that references a group pays 1 + group size, so a group may hold
    at most MAX_DEFINITIONS - 1 apps.
    """
    apps = ag.get("Applications") or []
    if len(apps) <= MAX_DEFINITIONS - 1:
        return []
    pieces, idx = [], 0
    for i, size in enumerate(even_chunk_sizes(len(apps), MAX_DEFINITIONS - 1), start=1):
        pieces.append(make_group_piece(ag, i, apps[idx:idx + size], ids))
        idx += size
    return pieces


def rewrite_group_refs(apps, split_groups):
    """Replace each reference to a split group with one reference per piece.

    split_groups maps original group Id and original group Name -> pieces.
    Each new reference is a copy of the original reference with id = piece Id
    and displayName = piece Name. Returns (new_apps, changed).
    """
    new_apps, changed = [], False
    for entry in apps:
        pieces = None
        if entry.get("applicationType") == GROUP_REF_TYPE:
            pieces = split_groups.get(("id", str(entry.get("id", "")).lower())) or \
                split_groups.get(("name", entry.get("displayName")))
        if not pieces:
            new_apps.append(entry)
            continue
        changed = True
        for piece in pieces:
            ref = copy.deepcopy(entry)
            ref["id"] = piece["Id"]
            ref["displayName"] = piece["Name"]
            new_apps.append(ref)
    return new_apps, changed


def find_strings(obj, wanted, path=""):
    """Yield (path, value) for every string in obj whose lower-case is in wanted."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from find_strings(v, wanted, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from find_strings(v, wanted, f"{path}[{i}]")
    elif isinstance(obj, str) and obj.lower() in wanted:
        yield path, obj


def unsupported_reason(policy):
    if not isinstance(policy, dict):
        return "not a JSON object"
    if "Applications" not in policy:
        return "no Applications key"
    apps = policy["Applications"]
    if apps is None:
        return "Applications is null"
    if not isinstance(apps, list):
        return "Applications is not a list"
    if any(not isinstance(a, dict) for a in apps):
        return "Applications has non-object entries"
    return None


def process_epmp(data, include_referenced_groups=False, ids=None):
    """Build the split-only output document and a report. Does not mutate data."""
    ids = ids or IdFactory(data)
    limit = MAX_DEFINITIONS
    report = {
        "limit": limit,
        "groups_split": [],         # {name, count, pieces: [(name, size)]}
        "policies_split": [],       # {name, count, pieces: [(name, size)]}
        "policies_associated": [],  # {name, count, new_name}
        "skipped_policies": [],     # (name, reason)
        "unknown_types": [],        # (kind, name, PolicyType)
        "referenced_groups": [],
        "missing_groups": [],
        "included_groups": [],
        "stale_group_refs": [],     # (object name, path) still pointing at a split group's Id
        "emptied_sections": [],
        "name_collisions": [],
        "over_limit": [],
        "max_count": 0,
    }

    groups = [g for g in (data.get("AppGroups") or []) if isinstance(g, dict)]
    policies = data.get("Policies") or []
    groups_by_name = {g.get("Name"): g for g in groups}
    groups_by_id = {str(g.get("Id", "")).lower(): g for g in groups}

    for g in groups:
        if g.get("PolicyType") not in KNOWN_GROUP_TYPES:
            report["unknown_types"].append(("AppGroup", g.get("Name"), g.get("PolicyType")))

    # 1. Split oversized groups.
    split_groups, out_groups, split_group_ids = {}, [], set()
    for ag in groups:
        pieces = split_appgroup(ag, ids)
        if not pieces:
            continue
        split_groups[("id", str(ag.get("Id", "")).lower())] = pieces
        split_groups[("name", ag.get("Name"))] = pieces
        split_group_ids.add(str(ag.get("Id", "")).lower())
        out_groups.extend(pieces)
        report["groups_split"].append({
            "name": ag.get("Name", "Unnamed"),
            "count": len(ag.get("Applications") or []),
            "pieces": [(p["Name"], len(p["Applications"])) for p in pieces],
        })

    # Group sizes as they will be after import: pieces replace their originals.
    orig_counts = {g.get("Name"): len(g.get("Applications") or []) for g in groups}
    ag_counts = {n: c for n, c in orig_counts.items()
                 if ("name", n) not in split_groups}
    for p in out_groups:
        ag_counts[p["Name"]] = len(p["Applications"])

    # 2. Policies: rewrite refs to split groups, then split equally.
    out_policies = []
    for policy in policies:
        reason = unsupported_reason(policy)
        if reason:
            name = policy.get("Name", "Unnamed") if isinstance(policy, dict) else "?"
            report["skipped_policies"].append((name, reason))
            continue
        if policy.get("PolicyType") not in KNOWN_POLICY_TYPES:
            report["unknown_types"].append(("policy", policy.get("Name"), policy.get("PolicyType")))
        apps = policy["Applications"]
        orig_total = policy_count(policy, orig_counts)
        new_apps, refs_changed = rewrite_group_refs(apps, split_groups)
        costs = [get_import_count(a, ag_counts) for a in new_apps]
        bounds = contiguous_balanced_split(costs, limit)
        if len(bounds) > 1:
            pieces = [make_policy_piece(policy, i, new_apps[s:e], ids)
                      for i, (s, e) in enumerate(bounds, start=1)]
            out_policies.extend(pieces)
            report["policies_split"].append({
                "name": policy.get("Name", "Unnamed"),
                "count": orig_total,
                "pieces": [(p["Name"], policy_count(p, ag_counts)) for p in pieces],
            })
        elif refs_changed:
            piece = make_policy_piece(policy, 1, new_apps, ids)
            out_policies.append(piece)
            report["policies_associated"].append({
                "name": policy.get("Name", "Unnamed"),
                "count": policy_count(piece, ag_counts),
                "new_name": piece["Name"],
            })

    # 3. Existing groups the output policies still reference.
    new_group_names = {g["Name"] for g in out_groups}
    referenced = []
    for p in out_policies:
        for a in p.get("Applications") or []:
            if a.get("applicationType") != GROUP_REF_TYPE:
                continue
            g = groups_by_id.get(str(a.get("id", "")).lower()) or groups_by_name.get(a.get("displayName"))
            name = g.get("Name") if g else a.get("displayName")
            if name in new_group_names or name in referenced:
                continue
            referenced.append(name)
            (report["referenced_groups"] if g else report["missing_groups"]).append(name)
    if include_referenced_groups:
        for n in report["referenced_groups"]:
            out_groups.append(copy.deepcopy(groups_by_name[n]))
            report["included_groups"].append(n)

    # 4. Other places (e.g. PARENT_PROCESS patterns) that point at a split group's Id.
    if split_group_ids:
        for obj in [p for p in policies if isinstance(p, dict)] + groups:
            for path, _ in find_strings(obj, split_group_ids):
                if not (path.startswith("Applications[") and path.endswith("].id")) and path != "Id":
                    report["stale_group_refs"].append((obj.get("Name", "Unnamed"), path))

    # 5. Checks.
    existing_names = {g.get("Name") for g in groups} | \
        {p.get("Name") for p in policies if isinstance(p, dict)}
    for obj in out_groups + out_policies:
        if obj["Name"] in existing_names and obj["Name"] not in report["included_groups"]:
            report["name_collisions"].append(obj["Name"])
    for p in out_policies:
        c = policy_count(p, ag_counts)
        report["max_count"] = max(report["max_count"], c)
        if c > limit:
            report["over_limit"].append((p["Name"], c))

    # 6. Output: same top-level keys. EndUserUIs keeps only dialogs the output
    #    policies reference (null stays null); other list sections are emptied.
    out_text = json.dumps(out_policies).lower()
    out = {}
    for key, value in data.items():
        if key == "AppGroups":
            out[key] = out_groups
        elif key == "Policies":
            out[key] = out_policies
        elif key == "EndUserUIs" and isinstance(value, list):
            out[key] = [copy.deepcopy(u) for u in value if isinstance(u, dict) and
                        isinstance(u.get("id"), str) and u["id"].lower() in out_text]
        elif isinstance(value, list):
            out[key] = []
            if value:
                report["emptied_sections"].append(key)
        else:
            out[key] = copy.deepcopy(value)
    return out, report


def print_report(r, include_referenced_groups):
    print(f"\nLimit: {r['limit']} definitions per policy "
          f"(groups hold at most {r['limit'] - 1} apps)")
    print(f"Groups split:          {len(r['groups_split'])}")
    for g in r["groups_split"]:
        print(f"  {g['name']}: {g['count']} apps -> "
              + ", ".join(f"{n} ({s})" for n, s in g["pieces"]))
    print(f"Policies split:        {len(r['policies_split'])}")
    for p in r["policies_split"]:
        print(f"  {p['name']}: {p['count']} defs -> "
              + ", ".join(f"{n} ({s})" for n, s in p["pieces"]))
    print(f"Associated policies:   {len(r['policies_associated'])} "
          f"(under the limit, but reference a split group)")
    for p in r["policies_associated"]:
        print(f"  {p['name']} -> {p['new_name']} ({p['count']} defs)")
    n_pol = sum(len(p["pieces"]) for p in r["policies_split"]) + len(r["policies_associated"])
    n_grp = sum(len(g["pieces"]) for g in r["groups_split"]) + len(r["included_groups"])
    print(f"Policies in output:    {n_pol}")
    print(f"AppGroups in output:   {n_grp}")
    if r["emptied_sections"]:
        print(f"Other sections left empty in output: {', '.join(r['emptied_sections'])}")

    if r["skipped_policies"]:
        print(f"\nSkipped policies (not split, not in output): {len(r['skipped_policies'])}")
        for n, why in r["skipped_policies"]:
            print(f"  {n}: {why}")
    if r["unknown_types"]:
        print("\nWARNING: unknown PolicyType values (processed anyway):")
        for kind, n, t in r["unknown_types"]:
            print(f"  {kind} {n}: PolicyType {t}")
    if r["referenced_groups"]:
        verb = "INCLUDED in the output (--include-referenced-groups)" if include_referenced_groups \
            else "NOT in the output; they must already exist in the tenant"
        print(f"\nExisting groups referenced by output policies ({verb}):")
        for n in r["referenced_groups"]:
            print(f"  {n}")
    if r["missing_groups"]:
        print("\nWARNING: output policies reference groups not found in the input file:")
        for n in r["missing_groups"]:
            print(f"  {n}")
    if r["stale_group_refs"]:
        print("\nWARNING: these fields still point at the Id of a group that was split "
              "(not rewritten; fix by hand after import):")
        for n, path in r["stale_group_refs"]:
            print(f"  {n}: {path}")
    if r["name_collisions"]:
        print("\nWARNING: these new names already exist in the input; rename before import:")
        for n in r["name_collisions"]:
            print(f"  {n}")

    print(f"\nVerification:\n  Max definitions in any output policy: {r['max_count']}")
    if r["over_limit"]:
        print(f"  ERROR: {len(r['over_limit'])} policies still exceed the limit:")
        for n, c in r["over_limit"]:
            print(f"    {n}: {c}")
    else:
        print(f"  All output policies are at or under {r['limit']} definitions.")


def print_checklist(r):
    print("\nPost-import checklist:")
    lines = ["Import the output file. It adds only new Split_N_ objects (new names and Ids)."]
    for p in r["policies_split"] + r["policies_associated"]:
        lines.append(f"Delete the original policy '{p['name']}' (replaced by its Split_N_ copies).")
    for g in r["groups_split"]:
        lines.append(f"Delete the original AppGroup '{g['name']}' (replaced by its Split_N_ copies).")
    lines.append("Check targets/assignments, active state and priority of the Split_N_ policies "
                 "against the originals before deleting them.")
    lines.append(f"Confirm no policy in the tenant is over {r['limit']} definitions.")
    for i, line in enumerate(lines, start=1):
        print(f"  {i}. {line}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Split oversized EPM policies and AppGroups equally. The output contains "
                    "only the new Split_N_ objects.")
    parser.add_argument("input", help="Input .epmp file (an EPM export)")
    parser.add_argument("output", nargs="?", help="Output .epmp file (default: <input>_split.epmp)")
    parser.add_argument("--include-referenced-groups", action="store_true",
                        help="Also copy existing (unsplit) groups that the output policies reference")
    parser.add_argument("--force", action="store_true",
                        help="Allow overwriting an existing output file (or the input)")
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    if not input_path.is_file():
        print(f"Error: input file not found: {input_path}", file=sys.stderr)
        return EXIT_BAD_INPUT
    output_path = Path(args.output) if args.output else \
        input_path.with_name(f"{input_path.stem}_split{input_path.suffix}")
    if not args.force:
        if output_path.resolve() == input_path.resolve():
            print("Error: output is the input file; refusing to overwrite (use --force)",
                  file=sys.stderr)
            return EXIT_BAD_INPUT
        if output_path.exists():
            print(f"Error: output file exists: {output_path} (use --force to overwrite)",
                  file=sys.stderr)
            return EXIT_BAD_INPUT

    print(f"Reading: {input_path}")
    try:
        with open(input_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        print(f"Error: input is not valid UTF-8 JSON: {e}", file=sys.stderr)
        return EXIT_BAD_INPUT
    if not isinstance(data, dict):
        print("Error: input JSON must be an object with Policies/AppGroups", file=sys.stderr)
        return EXIT_BAD_INPUT

    out, report = process_epmp(data, args.include_referenced_groups)
    print(f"Input policies:  {len(data.get('Policies') or [])}")
    print(f"Input AppGroups: {len(data.get('AppGroups') or [])}")

    if not report["groups_split"] and not report["policies_split"]:
        for n, why in report["skipped_policies"]:
            print(f"  skipped policy {n}: {why}")
        print("\nNothing exceeds the limit. No output written.")
        return EXIT_OK

    print_report(report, args.include_referenced_groups)
    if report["over_limit"]:
        print("\nNo output written because the result is still over the limit.")
        return EXIT_OVER_LIMIT

    print(f"\nWriting: {output_path}")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"), ensure_ascii=False)
    print_checklist(report)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
