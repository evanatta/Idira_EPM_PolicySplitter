#!/usr/bin/env python3
"""Regenerate examples/test_oversized_input.epmp (synthetic data only).

Then produce the split output with the CLI:
    python3 epm_policy_splitter.py examples/test_oversized_input.epmp --force
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tests"))
import synthetic  # noqa: E402

out = HERE / "test_oversized_input.epmp"
with open(out, "w", encoding="utf-8") as f:
    json.dump(synthetic.oversized_example(), f, separators=(",", ":"), ensure_ascii=False)
print(f"Wrote {out}")
