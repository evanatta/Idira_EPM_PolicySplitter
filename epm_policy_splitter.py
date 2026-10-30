#!/usr/bin/env python3
"""Split EPM policy exports so no policy exceeds the importer definition limit.

The output .epmp contains ONLY the objects created by the split (new AppGroup
pieces and new policies), so the import adds just those. Everything that did
not need splitting stays in the tenant untouched. See README.md.
"""

import argparse
import copy
import json
import sys
import uuid
from pathlib import Path

DEFAULT_LIMIT = 900
GROUP_REF_TYPE = 2

EXIT_OK = 0
EXIT_OVER_LIMIT = 1
EXIT_BAD_INPUT = 2


def get_import_count(app_entry, ag_counts):
    """Importer cost of one policy entry: 1, or 1 + group size for a group ref."""
    if app_entry.get("applicationType") == GROUP_REF_TYPE:
        return 1 + ag_counts.get(app_entry.get("displayName"), 0)
    return 1


def policy_count(policy, ag_counts):
    return sum(get_import_count(a, ag_counts) for a in (policy.get("Applications") or []))


def even_chunk_sizes(total, max_size):
    if total <= max_size:
        return [total]
    num_chunks = (total + max_size - 1) // max_size
    base = total // num_chunks
    extra = total % num_chunks
    return [base + 1 if i < extra else base for i in range(num_chunks)]


def new_id_like(old_id):
    """A fresh UUID formatted like the original Id (upper case / braces)."""
    new = str(uuid.uuid4())
    if isinstance(old_id, str):
        if old_id.upper() == old_id and any(c.isalpha() for c in old_id):
            new = new.upper()
        if old_id.startswith("{") and old_id.endswith("}"):
            new = "{" + new + "}"
    return new


def make_piece(obj, index, applications):
    """Copy of a policy/group as a NEW object: Name_<index>, new Id, given apps.

    LinkedAgentPolicies is cleared (if present) because the links belong to the
    original object; the summary tells the admin to re-link.
    """
    piece = copy.deepcopy(obj)
    piece["Name"] = f"{obj.get('Name', 'Unnamed')}_{index}"
    piece["Id"] = new_id_like(obj.get("Id"))
    piece["Applications"] = applications
    if "LinkedAgentPolicies" in piece:
        piece["LinkedAgentPolicies"] = []
    return piece


def split_appgroup(ag, limit):
    """Return the new pieces for an oversized group, or [] if it fits.

    A group may hold at most limit - 1 apps, because a policy that references
    it costs 1 + group size.
    """
    apps = ag.get("Applications") or []
    if len(apps) <= limit - 1:
        return []
    pieces = []
    idx = 0
    for i, size in enumerate(even_chunk_sizes(len(apps), limit - 1), start=1):
        pieces.append(make_piece(ag, i, apps[idx:idx + size]))
        idx += size
    return pieces


def rewrite_group_refs(apps, split_groups):
    """Replace each reference to a split group with one reference per piece.

    split_groups: {original name: (original Id, [pieces])}.
    ID handling: any field of the reference that held the original group's Id
    is set to the piece's Id. If the reference has its own `id` that is not the
    group Id, the first piece keeps it and later pieces get fresh ids.
    Returns (new_apps, changed).
    """
    new_apps = []
    changed = False
    for entry in apps:
        name = entry.get("displayName")
        if entry.get("applicationType") != GROUP_REF_TYPE or name not in split_groups:
            new_apps.append(entry)
            continue
        changed = True
        orig_id, pieces = split_groups[name]
        for i, piece in enumerate(pieces):
            ref = copy.deepcopy(entry)
            ref["displayName"] = piece["Name"]
            matched = False
            if isinstance(orig_id, str) and orig_id:
                for key, value in list(ref.items()):
                    if key != "displayName" and isinstance(value, str) and value.lower() == orig_id.lower():
                        ref[key] = piece["Id"]
                        matched = True
            if not matched and "id" in ref and i > 0:
                ref["id"] = new_id_like(ref["id"])
            new_apps.append(ref)
    return new_apps, changed


def even_split_entries(apps, ag_counts, limit):
    costs = [get_import_count(a, ag_counts) for a in apps]
    total = sum(costs)
    if total <= limit:
        return [apps]
    num_chunks = (total + limit - 1) // limit
    chunks = [[] for _ in range(num_chunks)]
    chunk_costs = [0] * num_chunks
    for entry, cost in zip(apps, costs):
        best_idx = None
        best_key = None
        for i in range(len(chunks)):
            if chunk_costs[i] + cost <= limit:
                key = (chunk_costs[i], i)
                if best_key is None or key < best_key:
                    best_key = key
                    best_idx = i
        if best_idx is None:
            chunks.append([entry])
            chunk_costs.append(cost)
        else:
            chunks[best_idx].append(entry)
            chunk_costs[best_idx] += cost
    return [c for c in chunks if c]


def process_epmp(data, limit=DEFAULT_LIMIT, include_referenced_groups=False):
    """Build the split-only output document and a report. Does not mutate data."""
    report = {
        "limit": limit,
        "groups_split": [],         # {name, count, pieces: [(name, size)], had_links}
        "policies_split": [],       # {name, count, pieces: [(name, size)], had_links}
        "policies_associated": [],  # {name, count, new_name, had_links}
        "referenced_groups": [],    # existing (unsplit) group names the output references
        "missing_groups": [],       # group names referenced but not found in AppGroups
        "included_groups": [],      # unsplit groups copied in (flag)
        "emptied_sections": [],     # other top-level lists emptied in the output
        "name_collisions": [],      # new names that already exist in the input
        "over_limit": [],           # (name, count) still over the limit
        "max_count": 0,
    }

    groups = data.get("AppGroups") or []
    policies = data.get("Policies") or []
    groups_by_name = {g.get("Name"): g for g in groups}

    # 1. Split oversized groups.
    split_groups = {}
    out_groups = []
    for ag in groups:
        pieces = split_appgroup(ag, limit)
        if not pieces:
            continue
        split_groups[ag.get("Name")] = (ag.get("Id"), pieces)
        out_groups.extend(pieces)
        report["groups_split"].append({
            "name": ag.get("Name", "Unnamed"),
            "count": len(ag.get("Applications") or []),
            "pieces": [(p["Name"], len(p["Applications"])) for p in pieces],
            "had_links": bool(ag.get("LinkedAgentPolicies")),
        })

    # Sizes as they will be after import: pieces replace their originals.
    ag_counts = {g.get("Name"): len(g.get("Applications") or []) for g in groups
                 if g.get("Name") not in split_groups}
    for _, pieces in split_groups.values():
        for p in pieces:
            ag_counts[p["Name"]] = len(p["Applications"])

    # 2. Policies: rewrite refs, then split. Keep only policies that changed.
    out_policies = []
    for policy in policies:
        apps = policy.get("Applications") or []
        orig_total = policy_count(policy, {**ag_counts, **{
            n: len(groups_by_name[n].get("Applications") or []) for n in split_groups}})
        new_apps, refs_changed = rewrite_group_refs(apps, split_groups)
        chunks = even_split_entries(new_apps, ag_counts, limit)
        if len(chunks) > 1:
            pieces = [make_piece(policy, i, c) for i, c in enumerate(chunks, start=1)]
            out_policies.extend(pieces)
            report["policies_split"].append({
                "name": policy.get("Name", "Unnamed"),
                "count": orig_total,
                "pieces": [(p["Name"], policy_count(p, ag_counts)) for p in pieces],
                "had_links": bool(policy.get("LinkedAgentPolicies")),
            })
        elif refs_changed:
            piece = make_piece(policy, 1, new_apps)
            out_policies.append(piece)
            report["policies_associated"].append({
                "name": policy.get("Name", "Unnamed"),
                "count": policy_count(piece, ag_counts),
                "new_name": piece["Name"],
                "had_links": bool(policy.get("LinkedAgentPolicies")),
            })

    # 3. Existing groups the output policies still reference.
    new_group_names = {g["Name"] for g in out_groups}
    referenced = []
    for p in out_policies:
        for a in p.get("Applications") or []:
            n = a.get("applicationType") == GROUP_REF_TYPE and a.get("displayName")
            if n and n not in new_group_names and n not in referenced:
                referenced.append(n)
    report["referenced_groups"] = [n for n in referenced if n in groups_by_name]
    report["missing_groups"] = [n for n in referenced if n not in groups_by_name]
    if include_referenced_groups:
        for n in report["referenced_groups"]:
            out_groups.append(copy.deepcopy(groups_by_name[n]))
            report["included_groups"].append(n)

    # 4. Checks.
    existing_names = {g.get("Name") for g in groups} | {p.get("Name") for p in policies}
    for obj in out_groups + out_policies:
        if obj["Name"] in existing_names and obj["Name"] not in report["included_groups"]:
            report["name_collisions"].append(obj["Name"])
    for p in out_policies:
        c = policy_count(p, ag_counts)
        report["max_count"] = max(report["max_count"], c)
        if c > limit:
            report["over_limit"].append((p["Name"], c))

    # 5. Output document: same top-level keys; other list sections emptied.
    out = {}
    for key, value in data.items():
        if key == "AppGroups":
            out[key] = out_groups
        elif key == "Policies":
            out[key] = out_policies
        elif isinstance(value, list):
            out[key] = []
            if value:
                report["emptied_sections"].append(key)
        else:
            out[key] = copy.deepcopy(value)
    if out_groups and "AppGroups" not in out:
        out["AppGroups"] = out_groups
    if out_policies and "Policies" not in out:
        out["Policies"] = out_policies

    return out, report


def print_report(r, include_referenced_groups):
    print(f"\nLimit: {r['limit']} definitions per policy "
          f"(groups hold at most {r['limit'] - 1} apps)")
    print(f"Groups split:          {len(r['groups_split'])}")
    for g in r["groups_split"]:
        print(f"  {g['name']}: {g['count']} apps -> {', '.join(f'{n} ({s})' for n, s in g['pieces'])}")
    print(f"Policies split:        {len(r['policies_split'])}")
    for p in r["policies_split"]:
        print(f"  {p['name']}: {p['count']} defs -> {', '.join(f'{n} ({s})' for n, s in p['pieces'])}")
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
    if r["name_collisions"]:
        print("\nWARNING: these new names already exist in the input; rename before import:")
        for n in r["name_collisions"]:
            print(f"  {n}")
    linked = [x["name"] for x in r["groups_split"] + r["policies_split"] + r["policies_associated"]
              if x["had_links"]]
    if linked:
        print("\nWARNING: LinkedAgentPolicies is cleared on every new object. These originals had links;")
        print("re-create the links/assignments on their replacements after import:")
        for n in linked:
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
    step = 1
    print(f"  {step}. Import the output file. It adds only new objects (new names and Ids).")
    step += 1
    for kind, items in (("policy", r["policies_split"]), ("policy", r["policies_associated"]),
                        ("AppGroup", r["groups_split"])):
        for x in items:
            print(f"  {step}. Delete the original {kind} '{x['name']}' (replaced by the new copies).")
            step += 1
    print(f"  {step}. Re-apply links/assignments (computers, users, LinkedAgentPolicies) "
          f"and policy order/priority on the new policies to match the originals.")
    step += 1
    print(f"  {step}. Confirm no policy in the tenant is over {r['limit']} definitions.")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Split oversized EPM policies and AppGroups. The output contains only "
                    "the new split objects (and policies that reference split groups).")
    parser.add_argument("input", help="Input .epmp file")
    parser.add_argument("output", nargs="?", help="Output .epmp file (default: <input>_split.epmp)")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"Max definitions per policy (default {DEFAULT_LIMIT})")
    parser.add_argument("--include-referenced-groups", action="store_true",
                        help="Also copy existing (unsplit) groups that the output policies reference")
    parser.add_argument("--force", action="store_true",
                        help="Allow overwriting an existing output file (or the input)")
    args = parser.parse_args(argv)

    if args.limit < 2:
        print("Error: --limit must be at least 2", file=sys.stderr)
        return EXIT_BAD_INPUT
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

    out, report = process_epmp(data, args.limit, args.include_referenced_groups)
    print(f"Input policies:  {len(data.get('Policies') or [])}")
    print(f"Input AppGroups: {len(data.get('AppGroups') or [])}")

    if not report["groups_split"] and not report["policies_split"]:
        print("\nNothing exceeds the limit. No output written.")
        return EXIT_OK

    print_report(report, args.include_referenced_groups)
    if report["over_limit"]:
        print("\nNo output written because the result is still over the limit.")
        return EXIT_OVER_LIMIT

    print(f"\nWriting: {output_path}")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print_checklist(report)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
