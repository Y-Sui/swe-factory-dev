#!/usr/bin/env python3
"""
Generate classification figures from the annotated JSONL files.
Outputs PNG files to scripts/figures/.
"""

import json
import os
from collections import defaultdict, Counter
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

INSTANCE_FILES = {
    "miroflow": "internal-swe-bench-data/MiroMindAI__miroflow/instances_selected_36.jsonl",
    "mirothinker": "internal-swe-bench-data/MiroMindAI__MiroThinker/instances_selected_24.jsonl",
    "torchtune": "internal-swe-bench-data/MiroMindAI__sd-torchtune/instances_selected_50.jsonl",
}

REPO_LABELS = {
    "miroflow": "miroflow\n(n=37)",
    "mirothinker": "MiroThinker\n(n=24)",
    "torchtune": "sd-torchtune\n(n=50)",
}

TYPES = ["feature", "bug_fix", "docs_config", "mixed", "refactor", "performance"]
DIFFICULTIES = ["easy", "medium", "hard"]

TYPE_COLORS = {
    "feature":     "#4C72B0",
    "bug_fix":     "#DD8452",
    "docs_config": "#55A868",
    "mixed":       "#C44E52",
    "refactor":    "#8172B2",
    "performance": "#937860",
}

DIFF_COLORS = {
    "easy":   "#4CAF50",
    "medium": "#FF9800",
    "hard":   "#F44336",
}

OUT_DIR = Path("scripts/figures")
OUT_DIR.mkdir(parents=True, exist_ok=True)

FIGSIZE = (9, 6)

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.size": 11,
    "axes.spines.top": False,
    "axes.spines.right": False,
})

# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------

def load_all():
    all_rows, by_repo = [], {}
    for repo, path in INSTANCE_FILES.items():
        rows = [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]
        by_repo[repo] = rows
        all_rows.extend(rows)
    return all_rows, by_repo

def patch_size(row):
    ls = row.get("patch", "").splitlines()
    a = sum(1 for l in ls if l.startswith("+") and not l.startswith("+++"))
    r = sum(1 for l in ls if l.startswith("-") and not l.startswith("---"))
    return a + r

# ---------------------------------------------------------------------------
# Figure 1: Global type distribution — horizontal bar
# ---------------------------------------------------------------------------

def fig_global_type(all_rows):
    counts = Counter(r["analysis_type"] for r in all_rows)
    total = len(all_rows)
    types = [t for t in TYPES if counts[t] > 0]
    values = [counts[t] for t in types]
    colors = [TYPE_COLORS[t] for t in types]

    fig, ax = plt.subplots(figsize=FIGSIZE)
    bars = ax.barh(types[::-1], values[::-1], color=colors[::-1], height=0.6)
    for bar, val in zip(bars, values[::-1]):
        pct = 100 * val / total
        ax.text(bar.get_width() + 0.3, bar.get_y() + bar.get_height() / 2,
                f"{val}  ({pct:.0f}%)", va="center", fontsize=10)
    ax.set_xlabel("Number of instances")
    ax.set_title("Global Type Distribution  (n=111)", fontsize=13, fontweight="bold")
    ax.set_xlim(0, max(values) * 1.25)
    fig.tight_layout()
    fig.savefig(OUT_DIR / "01_global_type.png", dpi=150)
    plt.close(fig)
    print("  Saved 01_global_type.png")

# ---------------------------------------------------------------------------
# Figure 2: Global difficulty distribution — donut
# ---------------------------------------------------------------------------

def fig_global_difficulty(all_rows):
    counts = Counter(r["analysis_difficulty"] for r in all_rows)
    total = len(all_rows)
    values = [counts[d] for d in DIFFICULTIES]
    colors = [DIFF_COLORS[d] for d in DIFFICULTIES]
    labels = [f"{d}\n{counts[d]} ({100*counts[d]//total}%)" for d in DIFFICULTIES]

    fig, ax = plt.subplots(figsize=FIGSIZE)
    wedges, _ = ax.pie(values, colors=colors, startangle=90,
                       wedgeprops=dict(width=0.5), counterclock=False)
    ax.legend(wedges, labels, loc="lower center", bbox_to_anchor=(0.5, -0.08),
              ncol=3, frameon=False, fontsize=11)
    ax.set_title("Global Difficulty Distribution  (n=111)", fontsize=13, fontweight="bold")
    fig.tight_layout()
    fig.savefig(OUT_DIR / "02_global_difficulty.png", dpi=150)
    plt.close(fig)
    print("  Saved 02_global_difficulty.png")

# ---------------------------------------------------------------------------
# Figure 3: Per-repo type distribution — grouped bar
# ---------------------------------------------------------------------------

def fig_repo_type(by_repo):
    repos = list(by_repo.keys())
    active_types = [t for t in TYPES if any(
        Counter(r["analysis_type"] for r in by_repo[rp])[t] > 0 for rp in repos
    )]
    x = np.arange(len(repos))
    width = 0.13
    offsets = np.linspace(-(len(active_types)-1)/2, (len(active_types)-1)/2, len(active_types)) * width

    fig, ax = plt.subplots(figsize=FIGSIZE)
    for i, t in enumerate(active_types):
        vals = [Counter(r["analysis_type"] for r in by_repo[rp])[t] for rp in repos]
        bars = ax.bar(x + offsets[i], vals, width=width * 0.9,
                      color=TYPE_COLORS[t], label=t)
        for bar, v in zip(bars, vals):
            if v > 0:
                ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.2,
                        str(v), ha="center", va="bottom", fontsize=8)

    ax.set_xticks(x)
    ax.set_xticklabels([REPO_LABELS[r] for r in repos], fontsize=11)
    ax.set_ylabel("Number of instances")
    ax.set_title("Type Distribution by Repository", fontsize=13, fontweight="bold")
    ax.legend(loc="upper right", frameon=False, fontsize=10)
    ax.set_ylim(0, max(
        Counter(r["analysis_type"] for r in rows)[t]
        for rows in by_repo.values() for t in active_types
    ) * 1.3)
    fig.tight_layout()
    fig.savefig(OUT_DIR / "03_repo_type.png", dpi=150)
    plt.close(fig)
    print("  Saved 03_repo_type.png")

# ---------------------------------------------------------------------------
# Figure 4: Per-repo difficulty distribution — stacked bar
# ---------------------------------------------------------------------------

def fig_repo_difficulty(by_repo):
    repos = list(by_repo.keys())
    x = np.arange(len(repos))
    width = 0.22
    offsets = np.linspace(-(len(DIFFICULTIES)-1)/2, (len(DIFFICULTIES)-1)/2, len(DIFFICULTIES)) * width

    fig, ax = plt.subplots(figsize=FIGSIZE)
    for i, d in enumerate(DIFFICULTIES):
        vals = [Counter(r["analysis_difficulty"] for r in by_repo[rp])[d] for rp in repos]
        bars = ax.bar(x + offsets[i], vals, width * 0.9,
                      color=DIFF_COLORS[d], label=d)
        for bar, v in zip(bars, vals):
            if v > 0:
                ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.2,
                        str(v), ha="center", va="bottom", fontsize=9)

    ax.set_xticks(x)
    ax.set_xticklabels([REPO_LABELS[r] for r in repos], fontsize=11)
    ax.set_ylabel("Number of instances")
    ax.set_title("Difficulty Distribution by Repository", fontsize=13, fontweight="bold")
    ax.set_ylim(0, max(
        Counter(r["analysis_difficulty"] for r in rows)[d]
        for rows in by_repo.values() for d in DIFFICULTIES
    ) * 1.3)
    ax.legend(loc="upper right", frameon=False, fontsize=10)
    fig.tight_layout()
    fig.savefig(OUT_DIR / "04_repo_difficulty.png", dpi=150)
    plt.close(fig)
    print("  Saved 04_repo_difficulty.png")


# ---------------------------------------------------------------------------
# Figure 6: Patch size distribution by type — box plot
# ---------------------------------------------------------------------------

def fig_patch_size_by_type(all_rows):
    active_types = [t for t in TYPES if any(r["analysis_type"] == t for r in all_rows)]
    data = {t: [patch_size(r) for r in all_rows if r["analysis_type"] == t]
            for t in active_types}

    fig, ax = plt.subplots(figsize=FIGSIZE)
    bp = ax.boxplot(
        [data[t] for t in active_types],
        patch_artist=True,
        medianprops=dict(color="black", linewidth=2),
        flierprops=dict(marker="o", markersize=4, alpha=0.5),
        widths=0.5,
    )
    for patch, t in zip(bp["boxes"], active_types):
        patch.set_facecolor(TYPE_COLORS[t])
        patch.set_alpha(0.8)
    ax.set_yscale("log")
    ax.set_xticks(range(1, len(active_types) + 1))
    ax.set_xticklabels(active_types, fontsize=11)
    ax.set_ylabel("Lines changed (log scale)")
    ax.set_title("Patch Size Distribution by Type", fontsize=13, fontweight="bold")
    # Annotate medians
    for i, t in enumerate(active_types):
        med = int(np.median(data[t]))
        ax.text(i + 1, med * 1.15, f"med={med}", ha="center", va="bottom", fontsize=8)
    fig.tight_layout()
    fig.savefig(OUT_DIR / "06_patch_size_by_type.png", dpi=150)
    plt.close(fig)
    print("  Saved 06_patch_size_by_type.png")

# ---------------------------------------------------------------------------
# Figure 7: Patch size distribution by difficulty — violin
# ---------------------------------------------------------------------------

def fig_patch_size_by_difficulty(all_rows):
    data = {d: [patch_size(r) for r in all_rows if r["analysis_difficulty"] == d]
            for d in DIFFICULTIES}

    fig, ax = plt.subplots(figsize=FIGSIZE)
    parts = ax.violinplot(
        [data[d] for d in DIFFICULTIES],
        positions=range(len(DIFFICULTIES)),
        showmedians=True,
        showextrema=True,
    )
    for i, (body, d) in enumerate(zip(parts["bodies"], DIFFICULTIES)):
        body.set_facecolor(DIFF_COLORS[d])
        body.set_alpha(0.7)
    parts["cmedians"].set_color("black")
    parts["cmedians"].set_linewidth(2)

    ax.set_yscale("log")
    ax.set_xticks(range(len(DIFFICULTIES)))
    ax.set_xticklabels(
        [f"{d}\n(n={len(data[d])})" for d in DIFFICULTIES], fontsize=11
    )
    ax.set_ylabel("Lines changed (log scale)")
    ax.set_title("Patch Size Distribution by Difficulty", fontsize=13, fontweight="bold")
    for i, d in enumerate(DIFFICULTIES):
        med = int(np.median(data[d]))
        ax.text(i, med * 1.2, f"med={med}", ha="center", va="bottom", fontsize=9)
    fig.tight_layout()
    fig.savefig(OUT_DIR / "07_patch_size_by_difficulty.png", dpi=150)
    plt.close(fig)
    print("  Saved 07_patch_size_by_difficulty.png")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Loading data from JSONL files...")
    all_rows, by_repo = load_all()
    print(f"  {len(all_rows)} instances loaded\n")

    print("Generating figures...")
    fig_global_type(all_rows)
    fig_global_difficulty(all_rows)
    fig_repo_type(by_repo)
    fig_repo_difficulty(by_repo)
    fig_patch_size_by_type(all_rows)
    fig_patch_size_by_difficulty(all_rows)

    print(f"\nAll figures saved to {OUT_DIR.resolve()}/")
