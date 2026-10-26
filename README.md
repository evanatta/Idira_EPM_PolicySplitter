# Idira EPM Policy Splitter

A Python tool that splits CyberArk Endpoint Privilege Manager (EPM) policy exports so that no policy exceeds the importer's 1000-definition limit.

The EPM importer rejects any policy that contains more than 1000 application definitions. This script uses a safety margin of **900** and distributes definitions evenly so the resulting policies stay under that ceiling.

## How the importer counts definitions

| Entry type | Count |
|------------|------:|
| Direct application definition | 1 |
| Application Group reference (`applicationType == 2`) | 1 + number of applications inside the group |

A policy that references a group of 500 applications is counted as 501. A policy that references ten groups of 1000 is counted as 10,010.

## What the script does

1. **Splits oversized AppGroups.**  
   Any AppGroup that holds more than 899 definitions is divided into equal-sized groups (sizes differ by at most 1). Each new group receives a fresh UUID and a name of the form `{Original}_1`, `{Original}_2`, and so on. `LinkedAgentPolicies` is cleared on the new groups.

2. **Rewrites policy references.**  
   Every policy that pointed at a split group is updated to reference the new groups instead. Definitions are moved, never duplicated.

3. **Even-splits oversized policies.**  
   Any policy whose import-count exceeds 900 is distributed across the minimum number of policies needed. Group references and direct definitions are assigned so the resulting policies are as balanced as possible, each staying at or under 900. New policies keep the original name with a `_1`, `_2`, … suffix, receive a new UUID, and have `LinkedAgentPolicies` cleared.

4. **Verifies the result.**  
   After writing the output file the script re-counts every policy. If anything still exceeds 900 it prints a warning and exits with status 1.

## Usage

```bash
python epm_policy_splitter.py <input.epmp> [output.epmp]
```

- `input.epmp` is required.
- `output.epmp` is optional. If omitted, the script writes `<input>_split.epmp` next to the input file.
- Requires Python 3.8 or later. No third-party packages.

### Example

```bash
python epm_policy_splitter.py policies.epmp
# writes policies_split.epmp
```

### Sample output

```
Reading: policies.epmp
Writing: policies_split.epmp

Limit: 900 definitions per policy
Original policies:  50
Policies after:     58
Original AppGroups: 30
AppGroups after:    34

AppGroups split: 2
  Elevate Group - 01: 1200 defs -> 2 groups [600, 600]
  Allow Group - 04: 1800 defs -> 3 groups [600, 600, 600]

Policies split: 3
  Elevate Policy - Windows: 1500 defs -> 2 policies [750, 750]
  Allow Policy - Mac: 2400 defs -> 3 policies [800, 800, 800]
  Elevate Policy - MacOS: 1100 defs -> 2 policies [550, 550]

Verification:
  Max definitions in any policy: 800
  All policies are at or under 900 definitions.
```

## Behavior notes

- **Nothing is duplicated.** Each application definition ends up in exactly one AppGroup. Each group reference ends up in exactly one policy.
- **AppGroups under 899 definitions are left unchanged.**
- **Policies under 900 are left unchanged.**
- **New objects get new UUIDs** so the import does not collide with the originals.
- **`LinkedAgentPolicies` is cleared** on every new policy and AppGroup to avoid dangling links.
- The 900 limit is a safety margin under the documented 1000-definition importer ceiling. The script does not accept a custom limit.

## License

MIT. See [LICENSE](LICENSE).
