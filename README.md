# Idira EPM Policy Splitter

A Python tool that splits Idira (formerly CyberArk) Endpoint Privilege Manager (EPM) policy exports so that no policy exceeds the importer's definition limit.

The EPM importer rejects any policy that contains more than 1000 application definitions. This script uses a safety margin of **900** by default.

> **Unverified:** the 1000-definition figure and how the importer resolves group references (by name or by Id) have not been confirmed against vendor documentation. See [Open questions](#open-questions).

## How the importer counts definitions

| Entry type | Count |
|------------|------:|
| Direct application definition | 1 |
| Application Group reference (`applicationType == 2`) | 1 + number of applications inside the group |

A policy that references a group of 500 applications is counted as 501.

## What the script does

1. **Splits oversized AppGroups.** A group with more than `limit - 1` apps (899 by default) is cut into equal pieces named `{Original}_1`, `{Original}_2`, … Each piece is a **new** group with a new Id.
2. **Rewrites references.** Every policy that referenced a split group now references all its pieces. Any field of the reference that held the original group's Id is set to the piece's Id. If the reference has its own `id` that is not the group's Id, the first piece keeps it and later pieces get new ones.
3. **Splits oversized policies.** A policy over the limit (including one that grew because a group it references was split) is spread evenly across the fewest new policies, named `{Original}_1`, `{Original}_2`, … with new Ids.
4. **Checks before writing.** If any output policy is still over the limit, nothing is written and the exit status is 1.

### The output contains only the split objects

The output `.epmp` contains **only**:

- the new AppGroup pieces;
- the new policies that replace oversized policies;
- the new copies of *associated policies*: policies under the limit that reference a split group. In practice these are rare, because a split group's pieces together always exceed the limit, so a policy that references one is normally split itself.

Policies and groups that did not need splitting are **not** in the output. They stay in the tenant untouched. The original oversized policies and groups are not in the output either. They stay in the tenant until the admin deletes them (see the checklist).

All other top-level keys are kept. Other list sections are written as empty lists, and non-list values (such as headers or metadata) are copied as-is. No fields are invented.

**Referenced groups.** Output policies may still reference existing groups that were not split. By default those groups are left out, because they already exist in the tenant, and the script lists their names. Use `--include-referenced-groups` to copy them into the output unchanged (same name and Id) if the importer needs them to resolve references.

**Links.** `LinkedAgentPolicies` is cleared on every new object, because the links belong to the original. The script lists every original that had links, so they can be re-applied by hand. Copying links was not done because what they point to and whether duplicates are allowed is unverified.

## Usage

```bash
python epm_policy_splitter.py <input.epmp> [output.epmp] [--limit N] [--include-referenced-groups] [--force]
```

| Option | Meaning |
|--------|---------|
| `output.epmp` | Optional. Defaults to `<input>_split.epmp` next to the input. |
| `--limit N` | Max definitions per policy (default 900). Groups are capped at `N - 1` apps. |
| `--include-referenced-groups` | Also include existing, unsplit groups referenced by the output policies. |
| `--force` | Allow overwriting an existing output file, or the input file. Without it, the script refuses. |

- Requires Python 3.8 or later. No third-party packages.
- Reads UTF-8 with or without a byte-order mark (BOM).
- If nothing exceeds the limit, no output file is written.

Exit status: `0` success (or nothing to do), `1` result still over the limit (nothing written), `2` bad input, bad arguments or refused overwrite.

### Sample output

```
Limit: 900 definitions per policy (groups hold at most 899 apps)
Groups split:          1
  Example Group A: 1200 apps -> Example Group A_1 (600), Example Group A_2 (600)
Policies split:        2
  Example Policy A: 1500 defs -> Example Policy A_1 (750), Example Policy A_2 (750)
  Example Policy B: 1210 defs -> Example Policy B_1 (605), Example Policy B_2 (605)
Associated policies:   0 (under the limit, but reference a split group)
Policies in output:    4
AppGroups in output:   2

Existing groups referenced by output policies (NOT in the output; they must already exist in the tenant):
  Example Group C

Verification:
  Max definitions in any output policy: 750
  All output policies are at or under 900 definitions.

Writing: example_split.epmp

Post-import checklist:
  1. Import the output file. It adds only new objects (new names and Ids).
  2. Delete the original policy 'Example Policy A' (replaced by the new copies).
  3. Delete the original policy 'Example Policy B' (replaced by the new copies).
  4. Delete the original AppGroup 'Example Group A' (replaced by the new copies).
  5. Re-apply links/assignments (computers, users, LinkedAgentPolicies) and policy order/priority on the new policies to match the originals.
  6. Confirm no policy in the tenant is over 900 definitions.
```

## After import

1. Delete each original oversized policy and AppGroup listed in the checklist. Their replacements are the `_1`, `_2`, … objects.
2. Re-apply assignments, links and policy order/priority on the new policies.
3. If the script warned about name collisions or missing referenced groups, fix those before importing.

## Tests

```bash
python3 -m unittest
```

Fixtures in `tests/fixtures/` are synthetic (fake names and Ids) and use `--limit 10` to stay small. Never add real customer exports to this public repo.

## Open questions

- Does the importer resolve group references by `displayName`, by Id, or both? Does it reject a policy that references a group missing from the file?
- Does importing an object with an existing name fail, duplicate, or overwrite?
- What exactly does `LinkedAgentPolicies` hold, and is copying it to the new objects safe?
- Does policy order or priority need to change for the new `_N` policies?
- Is 1000 the documented limit, and does the importer require a BOM on output?

## License

MIT. See [LICENSE](LICENSE).
