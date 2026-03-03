# Model Agreement Analysis: GPT-4.1 vs Claude Sonnet 4.5

**Date:** 2026-03-03
**Instances:** 111 (miroflow: 37, mirothinker: 24, torchtune: 50)
**Models compared:**
- `openai/gpt-4.1` → `scripts/analysis_results.json`
- `anthropic/claude-sonnet-4.5` → `scripts/analysis_results_claude.json`

---

## Agreement Summary

| Metric | Count | Rate |
|---|---|---|
| Type agreement | 98 / 111 | **88.3%** |
| Difficulty agreement | 92 / 111 | **82.9%** |
| Both agree (type + difficulty) | 84 / 111 | **75.7%** |

Overall agreement is high: the two models agree on type for 9 in 10 instances, and on both dimensions simultaneously for 3 in 4. The core findings from the GPT-4.1 run are well-corroborated.

---

## Global Distribution Comparison

### Type Distribution

| Type | GPT-4.1 | Claude 4.5 | Delta |
|---|---|---|---|
| feature | 52 (47%) | 53 (48%) | +1 |
| bug_fix | 26 (23%) | 24 (22%) | -2 |
| mixed | 16 (14%) | 10 (9%) | **-6** |
| docs_config | 10 (9%) | 15 (14%) | **+5** |
| refactor | 7 (6%) | 8 (7%) | +1 |
| performance | 0 (0%) | 1 (1%) | +1 |

`feature` and `bug_fix` are nearly identical. The main divergence: **Claude resolves `mixed` labels into more specific types** — primarily `docs_config`, `refactor`, and `feature` — rather than leaving them as compound categories.

### Difficulty Distribution

| Difficulty | GPT-4.1 | Claude 4.5 | Delta |
|---|---|---|---|
| easy | 24 (22%) | 38 (34%) | **+14** |
| medium | 38 (34%) | 28 (25%) | **-10** |
| hard | 49 (44%) | 45 (41%) | -4 |

The most significant overall difference: **Claude Sonnet 4.5 rates instances easier than GPT-4.1**, collapsing many `medium` judgements into `easy`. The `hard` boundary is more stable between the two models.

---

## Type Disagreement Analysis (13 instances)

### Disagreement Transition Patterns

| GPT-4.1 → Claude 4.5 | Count | Interpretation |
|---|---|---|
| `mixed` → `feature` | 2 | Claude picks the dominant intent |
| `mixed` → `refactor` | 2 | Claude picks the dominant intent |
| `feature` → `docs_config` | 2 | Claude is stricter about "what really changed" |
| `bug_fix` → `docs_config` | 1 | Lint/config fix re-classified |
| `refactor` → `docs_config` | 1 | Code reorganisation re-classified as config |
| `bug_fix` → `mixed` | 1 | Claude sees additional intent |
| `mixed` → `docs_config` | 1 | Claude picks dominant intent |
| `mixed` → `performance` | 1 | Claude identifies perf optimization |
| `mixed` → `bug_fix` | 1 | Claude picks dominant intent |
| `bug_fix` → `feature` | 1 | Behavioural addition vs defect fix |

**Pattern**: GPT-4.1 is more likely to use `mixed` when multiple concerns are present; Claude prefers to commit to a single dominant type. GPT-4.1 also labels some config/lint work as `bug_fix`, while Claude routes it to `docs_config`.

### Per-Instance Type Disagreements

| Instance | ±Lines | GPT-4.1 | Claude 4.5 | Note |
|---|---|---|---|---|
| MiroMindAI__miroflow-61 | 7 | bug_fix | docs_config | Lint fix — classification boundary |
| MiroMindAI__miroflow-92 | 151 | mixed | feature | Token usage tracking; Claude sees one dominant intent |
| MiroMindAI__miroflow-58 | 991 | mixed | refactor | Config/param renaming; Claude sees structural change |
| MiroMindAI__miroflow-68 | 222 | feature | docs_config | GAIA validation docs/configs |
| MiroMindAI__miroflow-45 | 147 | refactor | docs_config | PR rules / config update |
| MiroMindAI__miroflow-15 | 105 | bug_fix | mixed | Multi-bug fix; Claude sees compound nature |
| MiroMindAI__miroflow-5 | 1,493 | mixed | docs_config | Large doc/config sweep |
| MiroMindAI__MiroThinker-15 | 106 | feature | docs_config | Debug config and run scripts |
| MiroMindAI__MiroThinker-34 | 1,373 | mixed | refactor | Large shared-logic extraction |
| MiroMindAI__sd-torchtune-64 | 364 | mixed | feature | Primarily new model config additions |
| MiroMindAI__sd-torchtune-63 | 458 | mixed | performance | Grouped GEMM / backend dispatch |
| MiroMindAI__sd-torchtune-39 | 26 | bug_fix | feature | Monitoring padding tokens — add vs fix |
| MiroMindAI__sd-torchtune-18 | 933 | mixed | bug_fix | QWEN3 tie-word-embedding fix |

---

## Difficulty Disagreement Analysis (19 instances)

### Disagreement Transition Patterns

| GPT-4.1 → Claude 4.5 | Count | Interpretation |
|---|---|---|
| `medium` → `easy` | 13 | Claude has a more lenient medium/easy boundary |
| `hard` → `medium` | 4 | Claude is slightly more lenient on large patches |
| `medium` → `hard` | 1 | Claude is occasionally stricter |
| `hard` → `easy` | 1 | Claude sees a clear, targeted change despite size |

**Pattern**: Claude Sonnet 4.5 applies a more lenient threshold between `easy` and `medium`. It tends to call focused single-file or single-function changes `easy` even when line count falls in the 30–150 range. GPT-4.1 more strictly applies the ≤30-line rule for `easy`.

The `hard` boundary is more stable: only 5 total disagreements there, suggesting both models agree on what constitutes a genuinely complex change.

### Notable Difficulty Disagreements

| Instance | ±Lines | GPT-4.1 | Claude 4.5 | Note |
|---|---|---|---|---|
| MiroMindAI__miroflow-26 | 7,339 | hard | medium | GPT penalises scale; Claude notes it's a lock file update |
| MiroMindAI__miroflow-76 | 377 | hard | medium | Claude sees a focused feature, not multi-system |
| MiroMindAI__sd-torchtune-13 | 650 | hard | easy | 2-file delete of old monkey-patch files — trivial despite size |
| MiroMindAI__sd-torchtune-64 | 364 | medium | hard | Claude harder here — sees complex model config dependency |
| MiroMindAI__MiroThinker-34 | 1,373 | hard | medium | Large refactor but well-scoped to one module |

---

## Per-Repository Agreement

| Repo | Type agreement | Difficulty agreement | Both agree |
|---|---|---|---|
| miroflow (37) | 30/37 (81.1%) | 30/37 (81.1%) | 24/37 (64.9%) |
| mirothinker (24) | 22/24 (91.7%) | 19/24 (79.2%) | 18/24 (75.0%) |
| torchtune (50) | 46/50 (92.0%) | 43/50 (86.0%) | 42/50 (84.0%) |

**miroflow** has the most disagreement, driven by the high proportion of config/doc-heavy PRs where the `docs_config` vs `mixed`/`refactor`/`bug_fix` boundary is blurry. **torchtune** has the highest agreement — ML model PRs have clearer intent signals.

---

## Consistency of Core Findings

Despite the disagreements above, the **headline findings are consistent across both models**:

| Finding | GPT-4.1 | Claude 4.5 | Consistent? |
|---|---|---|---|
| feature dominates (~47–48%) | 52 (47%) | 53 (48%) | ✓ |
| bug_fix is ~22–23% | 26 (23%) | 24 (22%) | ✓ |
| torchtune is hardest (>50% hard) | 27/50 (54%) | 26/50 (52%) | ✓ |
| mirothinker skews hard (~38–42%) | 10/24 (42%) | 9/24 (38%) | ✓ |
| miroflow is most balanced | 32% hard | 27% hard | ✓ |
| bug_fix patches are small (median ~29 lines) | median 29 | — | ✓ |
| refactor/docs_config patches are large | avg 1,235–1,809 | — | ✓ |
| easy instances are mostly bug_fix | yes | yes | ✓ |

---

## Key Takeaways

1. **88% type agreement** and **83% difficulty agreement** confirm the classification taxonomy is stable and well-defined. Both models reach the same conclusions on the vast majority of instances.

2. **Main type divergence**: GPT-4.1 uses `mixed` more freely (16 instances) while Claude prefers to pick a single dominant type (10 mixed). Neither is wrong — it reflects a judgment call on whether compound intent should be collapsed. For benchmark purposes, the specific type matters less than the distinction between `bug_fix` (strong F2P signal) vs `feature`/`refactor`/`docs_config` (weaker signal).

3. **Main difficulty divergence**: Claude rates 13 `medium` instances as `easy` — largely single-file changes in the 30–150 line range. GPT applies the stated ≤30-line threshold more literally. The `hard` boundary is stable: both models agree that >300-line multi-system changes are genuinely hard.

4. **Benchmark composition is robust**: The high-level insight — ~22% of instances are tractable `easy` bug_fixes, ~44% are `hard` and will challenge any agent — is confirmed by both models and is not an artefact of any single model's bias.

5. **Actionable recommendation**: Instances classified as `bug_fix / easy` by both models (strong consensus) are the best starting point for the pipeline — they have the clearest F2P signal, smallest patch scope, and highest probability of yielding good test cases.
