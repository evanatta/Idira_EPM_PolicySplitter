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
PARENT_PROCESS = "PARENT_PROCESS"
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


def make_group_copy(ag, applications, ids):
    """Single Split_1_ copy of a group whose entries were rewritten (not oversized)."""
    return make_group_piece(ag, 1, applications, ids)


def lookup_pieces(replaced, group_id, group_name):
    """Pieces of a replaced group, matched by Id, or by Name when there is no Id."""
    if isinstance(group_id, str) and group_id:
        return replaced.get(("id", group_id.lower()))
    if isinstance(group_name, str) and group_name:
        return replaced.get(("name", group_name))
    return None


def parent_process_pattern(entry):
    """The PARENT_PROCESS pattern dict of an Applications entry, or None.

    In exports it sits at entry["patterns"]["PARENT_PROCESS"] and points at a
    group with ApplicationGroupId / ApplicationGroupName.
    """
    pats = entry.get("patterns") if isinstance(entry, dict) else None
    pp = pats.get(PARENT_PROCESS) if isinstance(pats, dict) else None
    return pp if isinstance(pp, dict) else None


def parent_process_target(entry, replaced):
    pp = parent_process_pattern(entry)
    if pp is None:
        return None
    return lookup_pieces(replaced, pp.get("ApplicationGroupId"), pp.get("ApplicationGroupName"))


def rewrite_entries(apps, replaced, ids):
    """Point an Applications list at the pieces of replaced groups.

    replaced maps ("id", original group Id) and ("name", original group Name)
    -> list of piece groups.
    - A group reference (applicationType 2) to a replaced group becomes one
      reference per piece (id = piece Id, displayName = piece Name).
    - An entry whose PARENT_PROCESS pattern points at a replaced group becomes
      one clone of the entry per piece, with ApplicationGroupId/Name set to
      the piece. An entry holds only one PARENT_PROCESS pattern, so the entry
      itself is cloned. The first clone keeps the entry's id, later clones
      get a new one. Matching "parent in any piece" is the same as "parent in
      the original group", because a policy's entries are alternatives.
    Order and all other fields are kept. Returns (new_apps, group_refs, pp)
    where group_refs and pp count the rewritten original entries.
    """
    new_apps, n_refs, n_pp = [], 0, 0
    for entry in apps:
        if not isinstance(entry, dict):
            new_apps.append(entry)
            continue
        if entry.get("applicationType") == GROUP_REF_TYPE:
            pieces = lookup_pieces(replaced, entry.get("id"), entry.get("displayName"))
            if pieces:
                n_refs += 1
                for piece in pieces:
                    ref = copy.deepcopy(entry)
                    ref["id"] = piece["Id"]
                    ref["displayName"] = piece["Name"]
                    new_apps.append(ref)
                continue
        pieces = parent_process_target(entry, replaced)
        if pieces:
            n_pp += 1
            for i, piece in enumerate(pieces):
                clone = copy.deepcopy(entry)
                if i and "id" in clone:
                    clone["id"] = ids.guid(entry.get("id"))
                pp = clone["patterns"][PARENT_PROCESS]
                pp["ApplicationGroupId"] = piece["Id"]
                pp["ApplicationGroupName"] = piece["Name"]
                new_apps.append(clone)
            continue
        new_apps.append(entry)
    return new_apps, n_refs, n_pp


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
        "groups_associated": [],    # {name, count, new_name}: copied only to rewrite PARENT_PROCESS
        "parent_process_rewritten": [],  # (kind, object name, entries rewritten)
        "skipped_policies": [],     # (name, reason)
        "unknown_types": [],        # (kind, name, PolicyType)
        "referenced_groups": [],
        "missing_groups": [],
        "included_groups": [],
        "stale_group_refs": [],     # (object name, path) still pointing at a replaced group's Id
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

    # 1. Groups to replace: oversized ones, plus (transitively) any group with an
    #    entry whose PARENT_PROCESS pattern points at a group being replaced.
    def key_of(g):
        return str(g.get("Id", "")).lower()

    def pp_targets(g):
        """Keys of groups in this file that g's PARENT_PROCESS patterns point at."""
        found = []
        for e in g.get("Applications") or []:
            pp = parent_process_pattern(e)
            if pp is None:
                continue
            gid, gname = pp.get("ApplicationGroupId"), pp.get("ApplicationGroupName")
            target = groups_by_id.get(gid.lower()) if isinstance(gid, str) and gid else \
                groups_by_name.get(gname)
            if target is not None:
                found.append(key_of(target))
        return found

    to_replace = {key_of(g) for g in groups
                  if len(g.get("Applications") or []) > limit - 1}
    grew = True
    while grew:
        grew = False
        for g in groups:
            if key_of(g) not in to_replace and any(t in to_replace for t in pp_targets(g)):
                to_replace.add(key_of(g))
                grew = True

    # Build pieces so that a group is built after the groups it points at;
    # a PARENT_PROCESS cycle between replaced groups cannot be resolved and is
    # left to the stale-reference warning below.
    replaced, out_groups, built = {}, [], set()
    pending = [g for g in groups if key_of(g) in to_replace]
    while pending:
        ready = [g for g in pending
                 if all(t in built or t == key_of(g) or t not in to_replace
                        for t in pp_targets(g))] or pending[:1]
        g = ready[0]
        pending.remove(g)
        apps, _, n_pp = rewrite_entries(g.get("Applications") or [], replaced, ids)
        if n_pp:
            report["parent_process_rewritten"].append(("AppGroup", g.get("Name", "Unnamed"), n_pp))
        if len(apps) > limit - 1:
            pieces, idx = [], 0
            for i, size in enumerate(even_chunk_sizes(len(apps), limit - 1), start=1):
                pieces.append(make_group_piece(g, i, apps[idx:idx + size], ids))
                idx += size
            report["groups_split"].append({
                "name": g.get("Name", "Unnamed"),
                "count": len(apps),
                "pieces": [(p["Name"], len(p["Applications"])) for p in pieces],
            })
        else:
            pieces = [make_group_copy(g, apps, ids)]
            report["groups_associated"].append({
                "name": g.get("Name", "Unnamed"), "count": len(apps),
                "new_name": pieces[0]["Name"]})
        replaced[("id", key_of(g))] = pieces
        replaced[("name", g.get("Name"))] = pieces
        built.add(key_of(g))
        out_groups.extend(pieces)
    replaced_ids = set(built)

    # Group sizes as they will be after import: pieces replace their originals.
    orig_counts = {g.get("Name"): len(g.get("Applications") or []) for g in groups}
    ag_counts = {n: c for n, c in orig_counts.items()
                 if ("name", n) not in replaced}
    for p in out_groups:
        ag_counts[p["Name"]] = len(p["Applications"])

    # 2. Policies: rewrite refs and PARENT_PROCESS patterns that point at
    #    replaced groups, then split equally (clones count 1 each).
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
        new_apps, n_refs, n_pp = rewrite_entries(apps, replaced, ids)
        if n_pp:
            report["parent_process_rewritten"].append(("policy", policy.get("Name", "Unnamed"), n_pp))
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
        elif n_refs or n_pp:
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

    # 4. Anything that still carries a replaced group's original Id: fields the
    #    rewrite does not know, PARENT_PROCESS cycles between replaced groups,
    #    and skipped policies (which are not in the output).
    if replaced_ids:
        skipped = {n for n, _ in report["skipped_policies"]}
        scan = out_policies + out_groups + \
            [p for p in policies if isinstance(p, dict) and p.get("Name", "Unnamed") in skipped]
        for obj in scan:
            for path, _ in find_strings(obj, replaced_ids):
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
    if r["groups_associated"]:
        print(f"Groups copied:         {len(r['groups_associated'])} "
              f"(under the limit, but their PARENT_PROCESS patterns point at a split group)")
        for g in r["groups_associated"]:
            print(f"  {g['name']} -> {g['new_name']} ({g['count']} apps)")
    print(f"Policies split:        {len(r['policies_split'])}")
    for p in r["policies_split"]:
        print(f"  {p['name']}: {p['count']} defs -> "
              + ", ".join(f"{n} ({s})" for n, s in p["pieces"]))
    print(f"Associated policies:   {len(r['policies_associated'])} "
          f"(under the limit, but reference a split group)")
    for p in r["policies_associated"]:
        print(f"  {p['name']} -> {p['new_name']} ({p['count']} defs)")
    n_pol = sum(len(p["pieces"]) for p in r["policies_split"]) + len(r["policies_associated"])
    n_grp = sum(len(g["pieces"]) for g in r["groups_split"]) + \
        len(r["groups_associated"]) + len(r["included_groups"])
    print(f"Policies in output:    {n_pol}")
    print(f"AppGroups in output:   {n_grp}")
    if r["parent_process_rewritten"]:
        print("PARENT_PROCESS patterns pointing at a split group were rewritten "
              "(one entry per piece):")
        for kind, n, k in r["parent_process_rewritten"]:
            print(f"  {kind} {n}: {k} entr{'y' if k == 1 else 'ies'}")
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
        print("\nWARNING: these fields still point at the Id of a group that is replaced by "
              "Split_N_ pieces and could not be rewritten; fix by hand after import:")
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
    for g in r["groups_split"] + r["groups_associated"]:
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
