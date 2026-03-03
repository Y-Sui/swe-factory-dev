# Benchmark Instance Analysis Report

**Date:** 2026-03-03
**Total instances:** 111 across 3 repositories
**Model used for classification:** `anthropic/claude-sonnet-4.5`

---

## Classification Schema

| Field | Values |
|---|---|
| **Type** | `bug_fix`, `feature`, `refactor`, `performance`, `docs_config`, `mixed` |
| **Difficulty** | `easy` (focused, targeted change), `medium` (moderate scope/complexity), `hard` (large or multi-system change) |

---

## Global Summary

### Type Distribution

| Type | Count | % | Avg ±Lines | Median ±Lines |
|---|---|---|---|---|
| feature | 53 | 48% | 778 | 364 |
| bug_fix | 24 | 22% | 120 | 37 |
| docs_config | 15 | 14% | 955 | 272 |
| mixed | 10 | 9% | 716 | 318 |
| refactor | 8 | 7% | 1,860 | 1,156 |
| performance | 1 | 1% | 458 | 458 |

### Difficulty Distribution

| Difficulty | Count | % |
|---|---|---|
| easy | 38 | 34% |
| medium | 28 | 25% |
| hard | 45 | 41% |

### Patch Statistics (Global)

| Metric | Value |
|---|---|
| Average lines changed | 729 |
| Median lines changed | 271 |
| Average lines added | 529 |
| Average lines removed | 201 |
| Average files changed | 10.1 |
| Min lines changed | 2 |
| Max lines changed | 8,876 |
| Max files changed | 124 |

---

## Per-Repository Breakdown

### MiroMindAI/miroflow (37 instances)

**Patch Statistics**

| Metric | Value |
|---|---|
| Average lines changed | 545 |
| Median lines changed | 222 |
| Average lines added | 442 |
| Average lines removed | 102 |
| Average files changed | 11.6 |
| Max lines changed | 7,339 |
| Max files changed | 124 |

**Type Distribution**

| Type | Count | % | Avg ±Lines | Median ±Lines |
|---|---|---|---|---|
| feature | 14 | 38% | 486 | 377 |
| docs_config | 12 | 32% | 995 | 272 |
| bug_fix | 8 | 22% | 34 | 13 |
| mixed | 2 | 5% | 73 | 105 |
| refactor | 1 | 3% | 991 | 991 |

**Difficulty Distribution**

| Difficulty | Count | % |
|---|---|---|
| easy | 13 | 35% |
| medium | 14 | 38% |
| hard | 10 | 27% |

**Instance Breakdown** (sorted by patch size)

| Instance | Type | Difficulty | ±Lines | Files |
|---|---|---|---|---|
| MiroMindAI__miroflow-57 | bug_fix | easy | 4 | 2 |
| MiroMindAI__miroflow-17 | bug_fix | easy | 4 | 1 |
| MiroMindAI__miroflow-96 | bug_fix | easy | 7 | 1 |
| MiroMindAI__miroflow-61 | docs_config | easy | 7 | 4 |
| MiroMindAI__miroflow-6 | bug_fix | easy | 10 | 1 |
| MiroMindAI__miroflow-28 | bug_fix | easy | 13 | 1 |
| MiroMindAI__miroflow-87 | bug_fix | easy | 26 | 2 |
| MiroMindAI__miroflow-13 | mixed | easy | 41 | 3 |
| MiroMindAI__miroflow-71 | bug_fix | medium | 91 | 1 |
| MiroMindAI__miroflow-67 | feature | easy | 98 | 3 |
| MiroMindAI__miroflow-15 | mixed | easy | 105 | 6 |
| MiroMindAI__miroflow-16 | bug_fix | medium | 119 | 5 |
| MiroMindAI__miroflow-42 | docs_config | easy | 120 | 5 |
| MiroMindAI__miroflow-37 | feature | medium | 133 | 8 |
| MiroMindAI__miroflow-38 | docs_config | easy | 133 | 5 |
| MiroMindAI__miroflow-74 | feature | medium | 144 | 2 |
| MiroMindAI__miroflow-45 | docs_config | easy | 147 | 4 |
| MiroMindAI__miroflow-92 | feature | medium | 151 | 5 |
| MiroMindAI__miroflow-68 | docs_config | medium | 222 | 4 |
| MiroMindAI__miroflow-81 | feature | medium | 231 | 7 |
| MiroMindAI__miroflow-70 | docs_config | medium | 241 | 18 |
| MiroMindAI__miroflow-63 | feature | medium | 253 | 6 |
| MiroMindAI__miroflow-73 | docs_config | medium | 272 | 13 |
| MiroMindAI__miroflow-69 | feature | medium | 299 | 12 |
| MiroMindAI__miroflow-20 | docs_config | medium | 349 | 6 |
| MiroMindAI__miroflow-76 | feature | medium | 377 | 6 |
| MiroMindAI__miroflow-2 | docs_config | hard | 536 | 18 |
| MiroMindAI__miroflow-52 | feature | hard | 659 | 11 |
| MiroMindAI__miroflow-48 | feature | hard | 743 | 11 |
| MiroMindAI__miroflow-51 | feature | hard | 874 | 15 |
| MiroMindAI__miroflow-60 | feature | hard | 891 | 10 |
| MiroMindAI__miroflow-86 | feature | hard | 922 | 7 |
| MiroMindAI__miroflow-58 | refactor | hard | 991 | 35 |
| MiroMindAI__miroflow-40 | feature | hard | 1,031 | 11 |
| MiroMindAI__miroflow-78 | docs_config | hard | 1,084 | 35 |
| MiroMindAI__miroflow-5 | docs_config | hard | 1,493 | 22 |
| MiroMindAI__miroflow-26 | docs_config | medium | 7,339 | 124 |

---

### MiroMindAI/MiroThinker (24 instances)

**Patch Statistics**

| Metric | Value |
|---|---|
| Average lines changed | 1,212 |
| Median lines changed | 271 |
| Average lines added | 577 |
| Average lines removed | 635 |
| Average files changed | 12.4 |
| Max lines changed | 8,876 |
| Max files changed | 49 |

> Note: high average lines removed (635) reflects large-scale refactors that delete substantial old code.

**Type Distribution**

| Type | Count | % | Avg ±Lines | Median ±Lines |
|---|---|---|---|---|
| feature | 12 | 50% | 1,079 | 304 |
| bug_fix | 5 | 21% | 47 | 40 |
| refactor | 4 | 17% | 2,666 | 1,373 |
| mixed | 2 | 8% | 2,565 | 4,658 |
| docs_config | 1 | 4% | 106 | 106 |

**Difficulty Distribution**

| Difficulty | Count | % |
|---|---|---|
| easy | 10 | 42% |
| medium | 5 | 21% |
| hard | 9 | 38% |

**Instance Breakdown** (sorted by patch size)

| Instance | Type | Difficulty | ±Lines | Files |
|---|---|---|---|---|
| MiroMindAI__MiroThinker-30 | bug_fix | easy | 2 | 1 |
| MiroMindAI__MiroThinker-19 | feature | easy | 9 | 3 |
| MiroMindAI__MiroThinker-60 | feature | easy | 25 | 1 |
| MiroMindAI__MiroThinker-111 | feature | easy | 33 | 4 |
| MiroMindAI__MiroThinker-54 | bug_fix | easy | 37 | 1 |
| MiroMindAI__MiroThinker-42 | bug_fix | easy | 40 | 2 |
| MiroMindAI__MiroThinker-22 | feature | easy | 40 | 3 |
| MiroMindAI__MiroThinker-43 | refactor | easy | 43 | 8 |
| MiroMindAI__MiroThinker-61 | bug_fix | easy | 53 | 1 |
| MiroMindAI__MiroThinker-51 | bug_fix | medium | 103 | 1 |
| MiroMindAI__MiroThinker-15 | docs_config | easy | 106 | 2 |
| MiroMindAI__MiroThinker-20 | feature | medium | 153 | 1 |
| MiroMindAI__MiroThinker-31 | feature | medium | 271 | 4 |
| MiroMindAI__MiroThinker-18 | feature | medium | 304 | 12 |
| MiroMindAI__MiroThinker-41 | refactor | hard | 374 | 33 |
| MiroMindAI__MiroThinker-11 | mixed | hard | 472 | 27 |
| MiroMindAI__MiroThinker-72 | feature | hard | 886 | 18 |
| MiroMindAI__MiroThinker-21 | feature | hard | 1,106 | 6 |
| MiroMindAI__MiroThinker-34 | refactor | medium | 1,373 | 49 |
| MiroMindAI__MiroThinker-62 | feature | hard | 2,736 | 36 |
| MiroMindAI__MiroThinker-28 | feature | hard | 2,909 | 20 |
| MiroMindAI__MiroThinker-8 | feature | hard | 4,480 | 11 |
| MiroMindAI__MiroThinker-6 | mixed | hard | 4,658 | 42 |
| MiroMindAI__MiroThinker-33 | refactor | hard | 8,876 | 12 |

---

### MiroMindAI/sd-torchtune (50 instances)

**Patch Statistics**

| Metric | Value |
|---|---|
| Average lines changed | 634 |
| Median lines changed | 364 |
| Average lines added | 569 |
| Average lines removed | 65 |
| Average files changed | 7.9 |
| Max lines changed | 4,386 |
| Max files changed | 39 |

> Note: low average lines removed (65) vs lines added (569) — most changes are additive new features rather than rewrites.

**Type Distribution**

| Type | Count | % | Avg ±Lines | Median ±Lines |
|---|---|---|---|---|
| feature | 27 | 54% | 795 | 487 |
| bug_fix | 11 | 22% | 216 | 39 |
| mixed | 6 | 12% | 314 | 318 |
| refactor | 3 | 6% | 1,075 | 1,156 |
| docs_config | 2 | 4% | 1,138 | 1,341 |
| performance | 1 | 2% | 458 | 458 |

**Difficulty Distribution**

| Difficulty | Count | % |
|---|---|---|
| easy | 15 | 30% |
| medium | 9 | 18% |
| hard | 26 | 52% |

**Instance Breakdown** (sorted by patch size)

| Instance | Type | Difficulty | ±Lines | Files |
|---|---|---|---|---|
| MiroMindAI__sd-torchtune-48 | bug_fix | easy | 4 | 2 |
| MiroMindAI__sd-torchtune-24 | bug_fix | easy | 7 | 1 |
| MiroMindAI__sd-torchtune-11 | feature | easy | 13 | 3 |
| MiroMindAI__sd-torchtune-78 | bug_fix | easy | 17 | 1 |
| MiroMindAI__sd-torchtune-59 | bug_fix | easy | 20 | 2 |
| MiroMindAI__sd-torchtune-58 | feature | easy | 24 | 1 |
| MiroMindAI__sd-torchtune-39 | feature | easy | 26 | 1 |
| MiroMindAI__sd-torchtune-66 | bug_fix | easy | 29 | 1 |
| MiroMindAI__sd-torchtune-65 | feature | easy | 37 | 2 |
| MiroMindAI__sd-torchtune-54 | feature | easy | 37 | 2 |
| MiroMindAI__sd-torchtune-44 | bug_fix | easy | 39 | 2 |
| MiroMindAI__sd-torchtune-32 | feature | easy | 43 | 1 |
| MiroMindAI__sd-torchtune-26 | bug_fix | easy | 56 | 1 |
| MiroMindAI__sd-torchtune-61 | bug_fix | medium | 89 | 1 |
| MiroMindAI__sd-torchtune-75 | mixed | medium | 95 | 6 |
| MiroMindAI__sd-torchtune-55 | mixed | medium | 110 | 9 |
| MiroMindAI__sd-torchtune-43 | feature | easy | 143 | 3 |
| MiroMindAI__sd-torchtune-53 | bug_fix | medium | 150 | 3 |
| MiroMindAI__sd-torchtune-30 | feature | medium | 155 | 2 |
| MiroMindAI__sd-torchtune-89 | feature | medium | 179 | 4 |
| MiroMindAI__sd-torchtune-69 | feature | medium | 228 | 4 |
| MiroMindAI__sd-torchtune-14 | feature | hard | 314 | 8 |
| MiroMindAI__sd-torchtune-19 | mixed | hard | 317 | 12 |
| MiroMindAI__sd-torchtune-22 | mixed | medium | 318 | 13 |
| MiroMindAI__sd-torchtune-40 | feature | hard | 329 | 2 |
| MiroMindAI__sd-torchtune-64 | feature | hard | 364 | 4 |
| MiroMindAI__sd-torchtune-57 | mixed | hard | 394 | 14 |
| MiroMindAI__sd-torchtune-63 | performance | hard | 458 | 16 |
| MiroMindAI__sd-torchtune-52 | feature | hard | 487 | 7 |
| MiroMindAI__sd-torchtune-28 | feature | hard | 518 | 9 |
| MiroMindAI__sd-torchtune-62 | feature | hard | 521 | 10 |
| MiroMindAI__sd-torchtune-17 | feature | medium | 619 | 9 |
| MiroMindAI__sd-torchtune-13 | refactor | easy | 650 | 2 |
| MiroMindAI__sd-torchtune-73 | mixed | hard | 651 | 15 |
| MiroMindAI__sd-torchtune-31 | feature | hard | 659 | 14 |
| MiroMindAI__sd-torchtune-88 | feature | hard | 910 | 8 |
| MiroMindAI__sd-torchtune-18 | bug_fix | hard | 933 | 23 |
| MiroMindAI__sd-torchtune-25 | docs_config | hard | 935 | 10 |
| MiroMindAI__sd-torchtune-38 | feature | hard | 943 | 8 |
| MiroMindAI__sd-torchtune-87 | bug_fix | hard | 1,037 | 4 |
| MiroMindAI__sd-torchtune-68 | feature | hard | 1,086 | 10 |
| MiroMindAI__sd-torchtune-41 | feature | hard | 1,106 | 6 |
| MiroMindAI__sd-torchtune-23 | refactor | hard | 1,156 | 10 |
| MiroMindAI__sd-torchtune-8 | docs_config | hard | 1,341 | 5 |
| MiroMindAI__sd-torchtune-9 | refactor | hard | 1,420 | 27 |
| MiroMindAI__sd-torchtune-36 | feature | hard | 1,742 | 7 |
| MiroMindAI__sd-torchtune-60 | feature | hard | 1,753 | 13 |
| MiroMindAI__sd-torchtune-70 | feature | hard | 2,024 | 23 |
| MiroMindAI__sd-torchtune-86 | feature | hard | 2,827 | 13 |
| MiroMindAI__sd-torchtune-20 | feature | hard | 4,386 | 39 |

---

## Cross-Repository Insights

### Patch Size by Type (Global)

| Type | n | Avg ±Lines | Median ±Lines | Notes |
|---|---|---|---|---|
| bug_fix | 24 | 120 | 37 | Targeted fixes; typically single-file |
| feature | 53 | 778 | 364 | Wide spread; smaller additions to large multi-module additions |
| mixed | 10 | 716 | 318 | Feature bundled with fixes or config changes |
| docs_config | 15 | 955 | 272 | Often sweeping config/doc overhauls; high variance |
| refactor | 8 | 1,860 | 1,156 | Largest on average; reorganise entire subsystems |
| performance | 1 | 458 | 458 | Single instance (grouped GEMM backend dispatch) |

### Difficulty vs. Patch Size

- **easy** instances are predominantly `bug_fix` and small focused `feature` additions — clear, well-scoped changes with a single intent.
- **medium** spans all types but concentrates around focused `feature` additions and moderate `docs_config` sweeps.
- **hard** is dominated by large `feature` and `refactor` changes touching 10+ files, often with compound or vague problem statements.

### Repository Character

| Repo | Dominant Type | Dominant Difficulty | Avg ±Lines | Notable Pattern |
|---|---|---|---|---|
| miroflow | feature (38%) | medium (38%) | 545 | High docs_config share (32%); lock-file outlier inflates avg |
| mirothinker | feature (50%) | easy (42%) | 1,212 | High avg removal (635) — refactors delete substantial old code |
| torchtune | feature (54%) | hard (52%) | 634 | Additive ML model/module additions; very low avg removal (65) |

### Benchmark Composition Implications

- **34% easy instances** (predominantly `bug_fix` and small `feature`) are the most tractable for SWE agents — minimal context, clear failure signal, highest F2P probability.
- **41% hard instances** will challenge agents the most: large multi-file patches, vague or compound problem statements, often requiring deep understanding of the domain.
- **`docs_config` and `refactor`** instances are poor F2P candidates — behaviour-observable test signals are weak. Consider deprioritising or treating separately from `bug_fix`/`feature`.
- **`feature` instances** dominate (48%) and span all difficulties. The smaller ones (easy/medium) are viable for test generation; the large hard ones require agents to understand intent beyond the diff.
