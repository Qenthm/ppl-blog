"""Generate PNG diagrams for the tdd-beyond-green blog post."""

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch
import numpy as np
from pathlib import Path

OUT = Path(__file__).parent
DPI = 150

# ──────────────────────────────────────────────
# 1. Red-Green-Refactor cycle
# ──────────────────────────────────────────────
def gen_rgr_cycle():
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 5)
    ax.axis("off")
    fig.patch.set_facecolor("#f8f9fa")
    ax.set_facecolor("#f8f9fa")

    nodes = [
        (2.0, 2.5, "WRITE\nFAILING TEST", "#e74c3c", "#fff"),
        (5.0, 2.5, "IMPLEMENT\nMINIMUM CODE", "#27ae60", "#fff"),
        (8.0, 2.5, "CLEAN UP\nREFACTOR",    "#2980b9", "#fff"),
    ]

    for x, y, label, bg, fg in nodes:
        circle = plt.Circle((x, y), 0.95, color=bg, zorder=3)
        ax.add_patch(circle)
        ax.text(x, y, label, ha="center", va="center",
                fontsize=9.5, fontweight="bold", color=fg,
                zorder=4, linespacing=1.5)

    arrow_kw = dict(arrowstyle="-|>", color="#555", lw=2,
                    mutation_scale=18, zorder=2)

    # RED → GREEN (straight)
    ax.annotate("", xy=(4.05, 2.5), xytext=(2.95, 2.5),
                arrowprops=dict(**arrow_kw))
    ax.text(3.5, 2.85, "all red", ha="center", fontsize=8, color="#555")

    # GREEN → REFACTOR (straight)
    ax.annotate("", xy=(7.05, 2.5), xytext=(5.95, 2.5),
                arrowprops=dict(**arrow_kw))
    ax.text(6.5, 2.85, "all green", ha="center", fontsize=8, color="#555")

    # REFACTOR → RED (arc below)
    ax.annotate("",
                xy=(2.0, 1.55), xytext=(8.0, 1.55),
                arrowprops=dict(arrowstyle="-|>", color="#555", lw=2,
                                mutation_scale=18, zorder=2,
                                connectionstyle="arc3,rad=-0.35"))
    ax.text(5.0, 0.75, "next failing test", ha="center",
            fontsize=8, color="#555")

    ax.set_title("The TDD Loop", fontsize=13, fontweight="bold",
                 color="#222", pad=10)
    fig.tight_layout()
    fig.savefig(OUT / "01-rgr-cycle.png", dpi=DPI, bbox_inches="tight")
    plt.close(fig)
    print("  ✓ 01-rgr-cycle.png")


# ──────────────────────────────────────────────
# 2. Risk score weights pie chart
# ──────────────────────────────────────────────
def gen_weights_pie():
    labels  = ["Delay Score\n(35%)", "Overdue Count\n(20%)",
               "Outstanding\nAmount (20%)", "Payment\nConsistency (15%)",
               "Invoice\nAge (10%)"]
    sizes   = [35, 20, 20, 15, 10]
    colors  = ["#e74c3c", "#e67e22", "#f1c40f", "#2ecc71", "#3498db"]
    explode = [0.05] * 5

    fig, ax = plt.subplots(figsize=(7, 5))
    fig.patch.set_facecolor("#f8f9fa")
    ax.set_facecolor("#f8f9fa")

    wedges, texts, autotexts = ax.pie(
        sizes, labels=labels, colors=colors, explode=explode,
        autopct="%1.0f%%", startangle=140,
        textprops={"fontsize": 9},
        wedgeprops={"edgecolor": "white", "linewidth": 2},
    )
    for at in autotexts:
        at.set_fontsize(9)
        at.set_fontweight("bold")
        at.set_color("white")

    ax.set_title("Risk Score Weights", fontsize=13,
                 fontweight="bold", color="#222", pad=12)
    fig.tight_layout()
    fig.savefig(OUT / "02-weights-pie.png", dpi=DPI, bbox_inches="tight")
    plt.close(fig)
    print("  ✓ 02-weights-pie.png")


# ──────────────────────────────────────────────
# 3. N+1 vs Batch comparison
# ──────────────────────────────────────────────
def gen_n1_vs_batch():
    fig, axes = plt.subplots(1, 2, figsize=(11, 5.5))
    fig.patch.set_facecolor("#f8f9fa")
    fig.suptitle("N+1 vs Batch: DB Round-Trips for 50 Invoices",
                 fontsize=13, fontweight="bold", color="#222", y=1.01)

    def draw_panel(ax, title, bg, entries, summary_text, summary_color):
        ax.set_facecolor(bg)
        ax.set_xlim(0, 10)
        ax.set_ylim(0, 10)
        ax.axis("off")
        ax.set_title(title, fontsize=11, fontweight="bold",
                     color="#222", pad=8)

        for i, (label, color) in enumerate(entries):
            y = 8.8 - i * 1.1
            box = FancyBboxPatch((0.5, y - 0.4), 9.0, 0.8,
                                 boxstyle="round,pad=0.1",
                                 facecolor=color, edgecolor="white",
                                 linewidth=1.5, zorder=2)
            ax.add_patch(box)
            ax.text(5.0, y, label, ha="center", va="center",
                    fontsize=9, color="#222", zorder=3)

        ax.text(5.0, 0.45, summary_text, ha="center", va="center",
                fontsize=10.5, fontweight="bold", color=summary_color)

    # N+1 panel
    n1_entries = [
        ("get_client(invoice_1.client_id)", "#ffd6d6"),
        ("get_client(invoice_2.client_id)", "#ffd6d6"),
        ("get_client(invoice_3.client_id)", "#ffd6d6"),
        ("  ···  47 more round-trips  ···",  "#ffe8cc"),
        ("get_client(invoice_50.client_id)", "#ffd6d6"),
    ]
    draw_panel(axes[0], "N+1 Approach",
               "#fff5f5", n1_entries,
               "50 DB calls  ≈  121 ms", "#c0392b")

    # Batch panel
    batch_entries = [
        ("collect all 50 client_ids", "#d6eaf8"),
        ("get_clients([id₁, id₂, …, id₅₀])", "#a9cce3"),
        ("← single query, all rows returned", "#d6eaf8"),
    ]
    draw_panel(axes[1], "Batch Approach",
               "#f0f8ff", batch_entries,
               "1 DB call  ≈  4 ms", "#1a5276")

    fig.tight_layout()
    fig.savefig(OUT / "03-n1-vs-batch.png", dpi=DPI, bbox_inches="tight")
    plt.close(fig)
    print("  ✓ 03-n1-vs-batch.png")


# ──────────────────────────────────────────────
# 4. Three testing layers
# ──────────────────────────────────────────────
def gen_three_layers():
    fig, ax = plt.subplots(figsize=(10, 5))
    fig.patch.set_facecolor("#f8f9fa")
    ax.set_facecolor("#f8f9fa")
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 6)
    ax.axis("off")
    ax.set_title("Three Test Layers — Three Failure Modes",
                 fontsize=13, fontweight="bold", color="#222", pad=10)

    columns = [
        {
            "x": 0.3, "width": 3.5,
            "header": "Unit Tests",
            "header_bg": "#2980b9", "body_bg": "#d6eaf8",
            "catches": "Logic Errors",
            "example": "delay weight 0.34\ninstead of 0.35\n→ fails instantly",
            "timing": "Fast  (ms)",
        },
        {
            "x": 4.25, "width": 3.5,
            "header": "Integration Tests",
            "header_bg": "#27ae60", "body_bg": "#d5f5e3",
            "catches": "Wiring Errors",
            "example": "audit_log writes to\nwrong table shape\nor wrong column",
            "timing": "Moderate  (seconds)",
        },
        {
            "x": 8.2, "width": 3.5,
            "header": "Benchmarks",
            "header_bg": "#e67e22", "body_bg": "#fdebd0",
            "catches": "Performance Regressions",
            "example": "N+1 query pattern\n121 ms vs 4 ms\nboth pass asserts",
            "timing": "Slow  (run separately)",
        },
    ]

    for col in columns:
        x, w = col["x"], col["width"]

        # Header bar
        hdr = FancyBboxPatch((x, 4.2), w, 1.3,
                             boxstyle="round,pad=0.15",
                             facecolor=col["header_bg"],
                             edgecolor="white", linewidth=2)
        ax.add_patch(hdr)
        ax.text(x + w / 2, 4.85, col["header"],
                ha="center", va="center",
                fontsize=11, fontweight="bold", color="white")

        # Body box
        body = FancyBboxPatch((x, 0.4), w, 3.6,
                              boxstyle="round,pad=0.15",
                              facecolor=col["body_bg"],
                              edgecolor=col["header_bg"], linewidth=1.5)
        ax.add_patch(body)

        ax.text(x + w / 2, 3.6, "Catches:", ha="center",
                fontsize=8.5, color="#555", style="italic")
        ax.text(x + w / 2, 3.1, col["catches"], ha="center",
                fontsize=10, fontweight="bold", color="#222")

        ax.axhline(y=2.6, xmin=(x + 0.1) / 12,
                   xmax=(x + w - 0.1) / 12,
                   color=col["header_bg"], lw=0.8, alpha=0.5)

        ax.text(x + w / 2, 2.3, col["example"], ha="center",
                va="top", fontsize=8.5, color="#444",
                linespacing=1.5)

        ax.text(x + w / 2, 0.7, col["timing"], ha="center",
                fontsize=8, color="#777", style="italic")

    fig.tight_layout()
    fig.savefig(OUT / "04-three-layers.png", dpi=DPI, bbox_inches="tight")
    plt.close(fig)
    print("  ✓ 04-three-layers.png")


if __name__ == "__main__":
    print("Generating diagrams...")
    gen_rgr_cycle()
    gen_weights_pie()
    gen_n1_vs_batch()
    gen_three_layers()
    print("Done.")
