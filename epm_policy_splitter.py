#!/usr/bin/env python3

import argparse
import copy
import json
import sys
import uuid
from pathlib import Path

LIMIT = 900

def get_import_count(app_entry, ag_counts):
    if app_entry.get("applicationType") == 2:
        gname = app_entry.get("displayName")
        return 1 + ag_counts.get(gname, 0)
    return 1

def even_chunk_sizes(total, max_size):
    if total <= max_size:
        return [total]
    num_chunks = (total + max_size - 1) // max_size
    base = total // num_chunks
    extra = total % num_chunks
    return [base + 1 if i < extra else base for i in range(num_chunks)]

def split_appgroup(ag):
    apps = ag.get("Applications") or []
    max_per_group = LIMIT - 1
    if len(apps) <= max_per_group:
        return [ag]

    sizes = even_chunk_sizes(len(apps), max_per_group)
    chunks = []
    idx = 0
    for size in sizes:
        chunks.append(apps[idx:idx + size])
        idx += size

    results = []
    original_name = ag.get("Name", "Unnamed")

    first = copy.deepcopy(ag)
    first["Applications"] = chunks[0]
    results.append(first)

    for i, chunk in enumerate(chunks[1:], start=1):
        new_ag = copy.deepcopy(ag)
        new_ag["Name"] = f"{original_name}_{i}"
        new_ag["Id"] = str(uuid.uuid4())
        new_ag["Applications"] = chunk
        if "LinkedAgentPolicies" in new_ag:
            new_ag["LinkedAgentPolicies"] = []
        results.append(new_ag)

    return results

def update_policy_references(policy, name_map):
    new_apps = []
    for app_entry in (policy.get("Applications") or []):
        if app_entry.get("applicationType") == 2:
            gname = app_entry.get("displayName")
            if gname in name_map:
                for new_name in name_map[gname]:
                    ref = copy.deepcopy(app_entry)
                    ref["displayName"] = new_name
                    if "id" in ref:
                        ref["id"] = str(uuid.uuid4())
                    new_apps.append(ref)
                continue
        new_apps.append(app_entry)
    return new_apps

def even_split_entries(apps, ag_counts):
    costs = [get_import_count(a, ag_counts) for a in apps]
    total = sum(costs)
    if total <= LIMIT:
        return [apps]

    num_chunks = (total + LIMIT - 1) // LIMIT

    chunks = [[] for _ in range(num_chunks)]
    chunk_costs = [0] * num_chunks

    for entry, cost in zip(apps, costs):
        best_idx = None
        best_key = None
        for i in range(num_chunks):
            if chunk_costs[i] + cost <= LIMIT:
                key = (chunk_costs[i], i)
                if best_key is None or key < best_key:
                    best_key = key
                    best_idx = i
        if best_idx is None:
            chunks.append([entry])
            chunk_costs.append(cost)
            num_chunks += 1
        else:
            chunks[best_idx].append(entry)
            chunk_costs[best_idx] += cost

    return [c for c in chunks if c]

def split_policy(policy, ag_counts):
    apps = policy.get("Applications") or []
    if not apps:
        return [policy]

    total = sum(get_import_count(a, ag_counts) for a in apps)
    if total <= LIMIT:
        return [policy]

    chunks = even_split_entries(apps, ag_counts)

    results = []
    original_name = policy.get("Name", "Unnamed")

    first = copy.deepcopy(policy)
    first["Applications"] = chunks[0]
    results.append(first)

    for i, chunk in enumerate(chunks[1:], start=1):
        new_policy = copy.deepcopy(policy)
        new_policy["Name"] = f"{original_name}_{i}"
        new_policy["Id"] = str(uuid.uuid4())
        new_policy["Applications"] = chunk
        if "LinkedAgentPolicies" in new_policy:
            new_policy["LinkedAgentPolicies"] = []
        results.append(new_policy)

    return results

def process_epmp(data):
    summary = {
        "appgroups_split": [],
        "policies_split": [],
    }

    new_appgroups = []
    name_map = {}

    for ag in data.get("AppGroups", []):
        original_name = ag.get("Name", "Unnamed")
        original_count = len(ag.get("Applications") or [])
        split_ags = split_appgroup(ag)
        new_appgroups.extend(split_ags)

        if len(split_ags) > 1:
            new_names = [g.get("Name") for g in split_ags]
            name_map[original_name] = new_names
            summary["appgroups_split"].append({
                "original": original_name,
                "original_count": original_count,
                "num_chunks": len(split_ags),
                "chunk_sizes": [len(g.get("Applications") or []) for g in split_ags],
            })

    data["AppGroups"] = new_appgroups

    if name_map:
        for policy in data.get("Policies", []):
            policy["Applications"] = update_policy_references(policy, name_map)

    ag_counts = {}
    for ag in data.get("AppGroups", []):
        name = ag.get("Name")
        ag_counts[name] = len(ag.get("Applications") or [])

    new_policies = []
    for policy in data.get("Policies", []):
        apps = policy.get("Applications") or []
        total = sum(get_import_count(a, ag_counts) for a in apps)

        split_policies = split_policy(policy, ag_counts)
        new_policies.extend(split_policies)

        if len(split_policies) > 1:
            chunk_sizes = [
                sum(get_import_count(a, ag_counts) for a in p.get("Applications") or [])
                for p in split_policies
            ]
            summary["policies_split"].append({
                "original": policy.get("Name", "Unnamed"),
                "original_total": total,
                "num_chunks": len(split_policies),
                "chunk_sizes": chunk_sizes,
            })

    data["Policies"] = new_policies
    return data, summary, ag_counts

def main():
    parser = argparse.ArgumentParser(
        description="Split EPM policies and application groups so no policy "
                    "exceeds 900 application definitions. Splits are even."
    )
    parser.add_argument("input", help="Input .epmp file")
    parser.add_argument("output", nargs="?", help="Output .epmp file (default: <input>_split.epmp)")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    if args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.with_name(f"{input_path.stem}_split{input_path.suffix}")

    print(f"Reading: {input_path}")
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    original_policies = len(data.get("Policies", []))
    original_appgroups = len(data.get("AppGroups", []))

    data, summary, ag_counts = process_epmp(data)

    new_policies = len(data["Policies"])
    new_appgroups = len(data["AppGroups"])

    print(f"Writing: {output_path}")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    print(f"\nLimit: {LIMIT} definitions per policy")
    print(f"Original policies:  {original_policies}")
    print(f"Policies after:     {new_policies}")
    print(f"Original AppGroups: {original_appgroups}")
    print(f"AppGroups after:    {new_appgroups}")

    if summary["appgroups_split"]:
        print(f"\nAppGroups split: {len(summary['appgroups_split'])}")
        for s in summary["appgroups_split"]:
            print(f"  {s['original']}: {s['original_count']} defs -> "
                  f"{s['num_chunks']} groups {s['chunk_sizes']}")
    else:
        print("\nNo AppGroups needed to be split.")

    if summary["policies_split"]:
        print(f"\nPolicies split: {len(summary['policies_split'])}")
        for s in summary["policies_split"]:
            print(f"  {s['original']}: {s['original_total']} defs -> "
                  f"{s['num_chunks']} policies {s['chunk_sizes']}")
    else:
        print("\nNo policies needed to be split.")

    max_found = 0
    over = []
    for p in data["Policies"]:
        total = sum(get_import_count(a, ag_counts) for a in (p.get("Applications") or []))
        if total > max_found:
            max_found = total
        if total > LIMIT:
            over.append((p.get("Name"), total))

    print(f"\nVerification:")
    print(f"  Max definitions in any policy: {max_found}")
    if over:
        print(f"  WARNING: {len(over)} policies still exceed the limit:")
        for name, count in over:
            print(f"    {name}: {count}")
        sys.exit(1)
    else:
        print(f"  All policies are at or under {LIMIT} definitions.")

if __name__ == "__main__":
    main()
