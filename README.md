# Idira EPM Policy Splitter

A Python tool that splits Idira (formerly CyberArk) Endpoint Privilege Manager (EPM) policy exports so that no policy exceeds the importer's definition limit.

The EPM importer rejects policies with too many application definitions (reported as 1000). This script keeps every policy at or under **900** by default.

## Full sequence

1. **Export** the policies (and their application groups) from the EPM console as an `.epmp` file.
2. **Run** the script on the export:
   ```bash
   python3 epm_policy_splitter.py Policies-export.epmp
   # writes Policies-export_split.epmp
   ```
3. **Import** the `_split.epmp` output into the same set. It contains only new `Split_N_` policies and groups.
4. **Delete the originals** that were split. The script prints their names in a post-import checklist.

Nothing else needs cleaning up. Policies and groups that did not need splitting are not in the output and stay untouched in the tenant.

## How definitions are counted

| Entry in a policy's `Applications` | Count |
|------------|------:|
| Direct application definition | 1 |
| Application group reference (`applicationType == 2`) | 1 + number of applications in the group |

A group may hold at most `limit - 1` apps (899 by default), so one reference to it still fits in a policy. The importer's real counting rule is **unverified** (see below).

## What the script does

1. **Splits oversized groups equally.** A group with more than 899 apps is cut into `ceil(apps / 899)` pieces. Piece sizes differ by at most 1, and app order is kept.
2. **Rewrites group references.** In every policy, a reference to a split group is replaced by one reference per piece, in order. Each new reference copies the original and sets `id` to the piece's Id and `displayName` to the piece's Name.
3. **Splits oversized policies equally.** A policy over the limit, including one that grew because a group it references was split, is cut into `ceil(total / limit)` pieces. The cuts are contiguous and keep the original order. Each piece aims at an equal share of the definitions. With only direct entries, sizes differ by at most 1. Heavy group references can make pieces less even, and can occasionally need one extra piece, but no piece exceeds the limit.
4. **Checks before writing.** If any output policy is still over the limit, nothing is written and the exit status is 1.

### Naming

Pieces are named with a prefix plus the original name:

```
My Policy  ->  Split_1_My Policy, Split_2_My Policy, ...
My Group   ->  Split_1_My Group,  Split_2_My Group,  ...
```

If a `Split_N_` name already exists in the input, the script warns.

### What each piece contains

- A deep copy of the original: every field is kept, whatever the policy type.
- A new GUID `Id` and the prefixed `Name`.
- Its share of `Applications`.
- **Policies:** a fresh self-entry, `LinkedAgentPolicies = [{Id: <new policy Id>, InternalId: <new number>, PolicyType: <same as the original's self-entry>}]`. New InternalIds are synthetic and unique. They start at 99000000, or after the highest InternalId in the file if that is higher.
- **Groups:** `LinkedAgentPolicies` is copied from the original (normally `[]`).

### What the output file contains

- `Policies`: only the `Split_N_` pieces. A policy under the limit that references a split group is also written, as one `Split_1_` copy. This is rare, because a split group's pieces together always exceed the limit.
- `AppGroups`: only the `Split_N_` group pieces. With `--include-referenced-groups`, existing unsplit groups that the new policies reference are also copied, unchanged.
- `EndUserUIs`: `null` stays `null`. Otherwise it keeps only the dialogs whose `id` the output policies reference.
- `TrustSoftwareDistributors`, `UserAccessTokens` and any other lists: written as empty lists. Other top-level keys are kept in the same order.
- Written as compact UTF-8 JSON without a BOM, like the export.

### Skipped and flagged

- Policies with no `Applications` key (for example PolicyType 53 policies) or an unsupported shape are skipped and listed. The script does not crash on them.
- Unknown `PolicyType` values are processed but flagged. The known values are policies 11 (Windows), 13 (macOS) and 53 (other), and groups 14 (Windows) and 16 (macOS).
- Other fields that still point at a split group's Id are listed for manual fixing, for example a `PARENT_PROCESS` pattern's `ApplicationGroupId`.

## Usage

```bash
python3 epm_policy_splitter.py <input.epmp> [output.epmp] [--limit N] [--include-referenced-groups] [--force]
```

| Option | Meaning |
|--------|---------|
| `output.epmp` | Optional. Defaults to `<input>_split.epmp` next to the input. |
| `--limit N` | Max definitions per policy (default 900). Groups are capped at `N - 1` apps. |
| `--include-referenced-groups` | Also include existing, unsplit groups referenced by the output policies. |
| `--force` | Allow overwriting an existing output file, or the input. Without it, the script refuses. |

- Requires Python 3.8 or later. No third-party packages.
- Reads UTF-8 with or without a BOM.
- If nothing exceeds the limit, no output is written.

Exit status: `0` success or nothing to do, `1` still over the limit (nothing written), `2` bad input, bad arguments or a refused overwrite.

### Sample output

From `examples/test_oversized_input.epmp`:

```
Limit: 900 definitions per policy (groups hold at most 899 apps)
Groups split:          1
  Test Group Big: 1200 apps -> Split_1_Test Group Big (600), Split_2_Test Group Big (600)
Policies split:        3
  Test Policy Uses Big Group: 1203 defs -> Split_1_Test Policy Uses Big Group (601), Split_2_Test Policy Uses Big Group (603)
  Test Policy Big: 1000 defs -> Split_1_Test Policy Big (500), Split_2_Test Policy Big (500)
  Test Mac Policy Big: 1000 defs -> Split_1_Test Mac Policy Big (500), Split_2_Test Mac Policy Big (500)
Associated policies:   0 (under the limit, but reference a split group)
Policies in output:    6
AppGroups in output:   2

Verification:
  Max definitions in any output policy: 603
  All output policies are at or under 900 definitions.

Writing: examples/test_oversized_input_split.epmp

Post-import checklist:
  1. Import the output file. It adds only new Split_N_ objects (new names and Ids).
  2. Delete the original policy 'Test Policy Uses Big Group' (replaced by its Split_N_ copies).
  3. Delete the original policy 'Test Policy Big' (replaced by its Split_N_ copies).
  4. Delete the original policy 'Test Mac Policy Big' (replaced by its Split_N_ copies).
  5. Delete the original AppGroup 'Test Group Big' (replaced by its Split_N_ copies).
  6. Check targets/assignments, active state and priority of the Split_N_ policies against the originals before deleting them.
  7. Confirm no policy in the tenant is over 900 definitions.
```

## Examples

`examples/` holds synthetic test files with fake names, fake GUIDs and fake apps:

- `test_oversized_input.epmp`: contains
  - a 1,200-app Windows group referenced by a Windows Elevate policy;
  - a Windows Allow policy with 1,000 direct entries;
  - a macOS Block policy with 1,000 entries;
  - a small policy and group that must not be split.
  
  All policies are inactive (`IsActive: false`).
- `test_oversized_input_split.epmp`: the script's output for it.
- `make_examples.py` regenerates the input. `verify_example.py` checks the output.

## Tests

```bash
python3 -m unittest
```

Tests build synthetic documents in the confirmed export shape (`tests/synthetic.py`). Never add a real export to this public repo.

## Confirmed vs unverified

Confirmed by importing hand-built files into a lab tenant:
- Unsigned, hand-built `.epmp` files import.
- New GUIDs and made-up InternalIds import. The importer appears to assign or ignore InternalIds.
- `EndUserUIs: null` and empty `TrustSoftwareDistributors` / `UserAccessTokens` lists import.

Unverified:
- What the importer does with a policy over 900 (or over 1000) definitions, and the exact limit.
- Whether a group reference counts as 1 + group size, as assumed here.
- What happens on import when a name already exists.
- Whether a split-only file whose policies reference groups that already exist in the tenant (not in the file) resolves those references by Id or by name.
- Whether `EndUserUIs` filtered to the referenced dialogs import without creating duplicates.

## License

MIT. See [LICENSE](LICENSE).
