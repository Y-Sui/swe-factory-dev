from datasets import load_dataset
import ast
import json

swebench = load_dataset("ScaleAI/SWE-bench_Pro", split="test")

f2p_counts = []
p2p_counts = []
skipped = 0

for i, case in enumerate(swebench):
    try:
        f2p = json.loads(case["fail_to_pass"])
    except (json.JSONDecodeError, TypeError):
        f2p = ast.literal_eval(case["fail_to_pass"])
    try:
        p2p = json.loads(case["pass_to_pass"])
    except (json.JSONDecodeError, TypeError):
        p2p = ast.literal_eval(case["pass_to_pass"])
    f2p_counts.append(len(f2p))
    p2p_counts.append(len(p2p))

import numpy as np

f2p_arr = np.array(f2p_counts)
p2p_arr = np.array(p2p_counts)

print(f"Total cases: {len(swebench)}")
print(f"\n--- Raw stats ---")
print(f"Avg F2P: {f2p_arr.mean():.2f}, Median F2P: {np.median(f2p_arr):.1f}, Range: [{f2p_arr.min()}, {f2p_arr.max()}]")
print(f"Avg P2P: {p2p_arr.mean():.2f}, Median P2P: {np.median(p2p_arr):.1f}, Range: [{p2p_arr.min()}, {p2p_arr.max()}]")

# Remove outliers using IQR
def trim_iqr(arr, k=1.5):
    q1, q3 = np.percentile(arr, [25, 75])
    iqr = q3 - q1
    mask = (arr >= q1 - k * iqr) & (arr <= q3 + k * iqr)
    return arr[mask]

f2p_trimmed = trim_iqr(f2p_arr)
p2p_trimmed = trim_iqr(p2p_arr)

print(f"\n--- After removing IQR outliers ---")
print(f"F2P: {len(f2p_trimmed)}/{len(f2p_arr)} cases, Avg: {f2p_trimmed.mean():.2f}, Median: {np.median(f2p_trimmed):.1f}, Range: [{f2p_trimmed.min()}, {f2p_trimmed.max()}]")
print(f"P2P: {len(p2p_trimmed)}/{len(p2p_arr)} cases, Avg: {p2p_trimmed.mean():.2f}, Median: {np.median(p2p_trimmed):.1f}, Range: [{p2p_trimmed.min()}, {p2p_trimmed.max()}]")

# Percentile breakdown
print(f"\n--- Percentiles ---")
for p in [25, 50, 75, 90, 95, 99]:
    print(f"  P{p}: F2P={np.percentile(f2p_arr, p):.0f}, P2P={np.percentile(p2p_arr, p):.0f}")
