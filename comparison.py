"""Generate Q3 tables and PDF plots comparing congestion control algorithms."""

import csv
import math
import statistics
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.ticker import MaxNLocator

from reporting import measured_samples, percentile


# Fixed color, marker, and line style per algorithm, so identity never relies on color alone.
STYLES = {"dart": ("#8b3fd9", "D", "-"), "cubic": ("#2a78d6", "o", "-"), "reno": ("#eb6834", "s", "--"),
          "bbr": ("#1baf7a", "^", "-.")}
EXTRA_STYLES = [("#eda100", "D", ":"), ("#e87ba4", "v", "-"), ("#008300", "P", "--")]
METRICS = [
    ("Congestion window", "snd_cwnd (segments)", lambda s: s["snd_cwnd_segments"]),
    ("Smoothed RTT", "RTT (ms)", lambda s: s["rtt_us"] / 1000),
    ("Loss proxy", "Retransmitted segments / interval", lambda s: s["retrans_delta_segments"]),
    ("Acknowledged goodput", "Goodput (Mbit/s)", lambda s: s["goodput_bps"] / 1e6),
]
RUN_FIELDS = ["run_id", "attempt", "repetition", "order_index", "host", "port", "actual_algorithm",
              "intervals", "overall_goodput_mbps", "median_goodput_mbps", "min_rtt_ms",
              "min_rtt_source", "median_rtt_ms", "p95_rtt_ms", "queueing_delay_ms",
              "retrans_segments", "retrans_per_second", "median_cwnd_segments", "mss_bytes",
              "bottleneck_rate_mbps", "bdp_bytes", "cwnd_to_bdp"]
VARIABILITY_FIELDS = ["host", "port", "actual_algorithm", "runs", "mean_goodput_mbps",
                      "stdev_goodput_mbps", "cov_goodput", "mean_median_rtt_ms", "stdev_median_rtt_ms"]
ALGORITHM_FIELDS = ["actual_algorithm", "runs", "destinations", "median_run_goodput_mbps",
                    "mean_run_goodput_mbps", "stdev_run_goodput_mbps", "median_queueing_delay_ms",
                    "median_rtt_ms", "median_p95_rtt_ms",
                    "mean_retrans_per_second", "median_cwnd_to_bdp",
                    "destinations_best_goodput", "destinations_lowest_rtt"]


def style(algorithm, algorithms):
    if algorithm in STYLES:
        return STYLES[algorithm]
    others = [name for name in algorithms if name not in STYLES]
    return EXTRA_STYLES[others.index(algorithm) % len(EXTRA_STYLES)]


def run_metrics(run):
    """Per-run values for comparing algorithms against the path's BDP."""
    samples = measured_samples(run)
    rtts = [s["rtt_us"] / 1000 for s in samples if s["rtt_us"] > 0]
    # Prefer the kernel's raw windowed minimum; older runs only have smoothed RTT.
    kernel_min = [s["min_rtt_us"] / 1000 for s in samples if s.get("min_rtt_us")]
    min_rtt, source = (min(kernel_min), "tcpi_min_rtt") if kernel_min else (min(rtts), "min srtt sample")
    seconds = sum(s["interval_seconds"] for s in samples)
    acked = sum(s["acked_delta_bytes"] for s in samples)
    retrans = sum(s["retrans_delta_segments"] for s in samples)
    mss = statistics.median(s["snd_mss_bytes"] for s in samples)
    cwnd = statistics.median(s["snd_cwnd_segments"] for s in samples)
    # Bottleneck rate: highest kernel delivery-rate estimate, else the highest interval goodput.
    rates = [s["delivery_rate_bytes_per_second"] for s in samples if s.get("delivery_rate_bytes_per_second")]
    bottleneck = max(rates) if rates else max(s["goodput_bps"] for s in samples) / 8
    bdp = bottleneck * min_rtt / 1000
    return {
        **{name: run.get(name) for name in RUN_FIELDS[:7]},
        "repetition": run.get("repetition", 1),
        "intervals": len(samples), "overall_goodput_mbps": 8 * acked / seconds / 1e6,
        "median_goodput_mbps": statistics.median(s["goodput_bps"] / 1e6 for s in samples),
        "min_rtt_ms": min_rtt, "min_rtt_source": source,
        "median_rtt_ms": statistics.median(rtts), "p95_rtt_ms": percentile(rtts, 0.95),
        "queueing_delay_ms": max(0.0, statistics.median(rtts) - min_rtt),
        "retrans_segments": retrans, "retrans_per_second": retrans / seconds,
        "median_cwnd_segments": cwnd, "mss_bytes": mss,
        "bottleneck_rate_mbps": 8 * bottleneck / 1e6, "bdp_bytes": bdp,
        "cwnd_to_bdp": cwnd * mss / bdp if bdp > 0 else None,
    }


def stdev(values):
    return statistics.stdev(values) if len(values) > 1 else None


def write_csv(path, fields, rows):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def comparison_tables(directory, metrics, algorithms):
    write_csv(directory / "q3-runs.csv", RUN_FIELDS, metrics)
    groups = defaultdict(list)
    for row in metrics:
        groups[(row["host"], row["port"], row["actual_algorithm"])].append(row)
    variability = []
    for (host, port, algorithm), rows in sorted(groups.items()):
        goodput = [row["overall_goodput_mbps"] for row in rows]
        rtt = [row["median_rtt_ms"] for row in rows]
        spread = stdev(goodput)
        variability.append({
            "host": host, "port": port, "actual_algorithm": algorithm, "runs": len(rows),
            "mean_goodput_mbps": statistics.mean(goodput), "stdev_goodput_mbps": spread,
            "cov_goodput": spread / statistics.mean(goodput) if spread is not None and statistics.mean(goodput) else None,
            "mean_median_rtt_ms": statistics.mean(rtt), "stdev_median_rtt_ms": stdev(rtt),
        })
    write_csv(directory / "q3-variability.csv", VARIABILITY_FIELDS, variability)
    # Per destination, which algorithm had the highest mean goodput and lowest mean RTT.
    best_goodput, lowest_rtt = defaultdict(int), defaultdict(int)
    destinations = defaultdict(dict)
    for row in variability:
        destinations[(row["host"], row["port"])][row["actual_algorithm"]] = row
    for rows in destinations.values():
        if len(rows) == len(algorithms):
            best_goodput[max(rows.values(), key=lambda r: r["mean_goodput_mbps"])["actual_algorithm"]] += 1
            lowest_rtt[min(rows.values(), key=lambda r: r["mean_median_rtt_ms"])["actual_algorithm"]] += 1
    summary = []
    for algorithm in algorithms:
        rows = [row for row in metrics if row["actual_algorithm"] == algorithm]
        if not rows:
            continue
        ratios = [row["cwnd_to_bdp"] for row in rows if row["cwnd_to_bdp"] is not None]
        summary.append({
            "actual_algorithm": algorithm, "runs": len(rows),
            "destinations": len({(row["host"], row["port"]) for row in rows}),
            "median_run_goodput_mbps": statistics.median(row["overall_goodput_mbps"] for row in rows),
            "mean_run_goodput_mbps": statistics.mean(row["overall_goodput_mbps"] for row in rows),
            "stdev_run_goodput_mbps": stdev([row["overall_goodput_mbps"] for row in rows]),
            "median_queueing_delay_ms": statistics.median(row["queueing_delay_ms"] for row in rows),
            "median_rtt_ms": statistics.median(row["median_rtt_ms"] for row in rows),
            "median_p95_rtt_ms": statistics.median(row["p95_rtt_ms"] for row in rows),
            "mean_retrans_per_second": statistics.mean(row["retrans_per_second"] for row in rows),
            "median_cwnd_to_bdp": statistics.median(ratios) if ratios else None,
            "destinations_best_goodput": best_goodput[algorithm],
            "destinations_lowest_rtt": lowest_rtt[algorithm],
        })
    write_csv(directory / "q3-algorithms.csv", ALGORITHM_FIELDS, summary)
    return summary


def representative_destination(completed, manifest):
    """First admitted destination with every algorithm x repetition run; else the most complete."""
    counts = defaultdict(int)
    for run in completed:
        counts[(run["host"], run["port"])] += 1
    order = [(d["host"], d["port"]) for d in manifest.get("admitted_destinations", [])]
    order += [key for key in counts if key not in order]
    expected = len(manifest["algorithms"]) * manifest.get("repetitions", 1)
    complete = [key for key in order if counts[key] >= expected]
    if complete:
        return complete[0]
    return max(order, key=lambda key: counts[key]) if counts else None


def repetition_line(run, first):
    bold = run.get("repetition", 1) == first
    return {"linewidth": 1.6 if bold else 0.9, "alpha": 1.0 if bold else 0.4,
            "label": f"repetition {run.get('repetition', 1)}"}


def plot_goodput_by_destination(directory, completed, manifest, algorithms):
    """Q1(c) for Q3 runs: one panel per destination, one curve per algorithm and repetition."""
    destinations = defaultdict(list)
    for run in completed:
        destinations[(run["host"], run["port"])].append(run)
    keys = list(destinations)
    with PdfPages(directory / "goodput.pdf") as pdf:
        for start in range(0, max(1, len(keys)), 4):
            batch = keys[start:start + 4]
            columns = 2 if len(batch) > 1 else 1
            rows = math.ceil(max(1, len(batch)) / columns)
            fig, axes = plt.subplots(rows, columns, figsize=(12, 4 * rows + 1.5), squeeze=False)
            for ax in axes.flat:
                ax.set_visible(False)
            if not keys:
                axes[0, 0].set_visible(True)
                axes[0, 0].axis("off")
                axes[0, 0].text(0, 0.5, "No completed runs. See experiment.json for failures.")
            for ax, key in zip(axes.flat, batch):
                ax.set_visible(True)
                runs = sorted(destinations[key], key=lambda r: (algorithms.index(r["actual_algorithm"]),
                                                                r.get("repetition", 1)))
                first = min(run.get("repetition", 1) for run in runs)
                for run in runs:
                    color, _, line = style(run["actual_algorithm"], algorithms)
                    samples = measured_samples(run)
                    width = repetition_line(run, first)
                    ax.plot([s["elapsed_seconds"] for s in samples], [s["goodput_bps"] / 1e6 for s in samples],
                            color=color, linestyle=line, linewidth=width["linewidth"], alpha=width["alpha"],
                            label=run["actual_algorithm"] if run.get("repetition", 1) == first else None)
                ax.set_title(f"{key[0]}:{key[1]}", fontsize=10)
                ax.set_xlabel("Elapsed sending time (s)")
                ax.set_ylabel("Acknowledged goodput (Mbit/s)")
                ax.set_ylim(bottom=0)
                ax.grid(alpha=0.25)
                ax.legend(fontsize=8, loc="lower right")
            fig.suptitle(f"A3 goodput: {manifest['successful_destinations']}/{manifest['requested_destinations']} "
                         f"destinations, {len(completed)} completed runs\n"
                         "first repetition bold; later repetitions faint")
            fig.subplots_adjust(left=0.09, right=0.98, bottom=0.1, top=0.84, hspace=0.65, wspace=0.28)
            pdf.savefig(fig)
            plt.close(fig)


def plot_timeseries(directory, runs, algorithms, destination):
    """Q2(b)(i) for each algorithm: rows are algorithms, columns share one y-scale per metric."""
    fig, axes = plt.subplots(len(algorithms), 4, figsize=(17, 2.9 * len(algorithms) + 1.4),
                             sharex=True, sharey="col", squeeze=False)
    tops = [0] * len(METRICS)
    for row, algorithm in enumerate(algorithms):
        group = sorted((r for r in runs if r["actual_algorithm"] == algorithm), key=lambda r: r.get("repetition", 1))
        color = style(algorithm, algorithms)[0]
        first = min((r.get("repetition", 1) for r in group), default=1)
        for run in group:
            samples = measured_samples(run)
            elapsed = [s["elapsed_seconds"] for s in samples]
            for column, (_, _, value) in enumerate(METRICS):
                values = [value(s) for s in samples]
                tops[column] = max(tops[column], *values)
                axes[row, column].plot(elapsed, values, color=color,
                                       drawstyle="steps-pre" if column == 2 else "default",
                                       **repetition_line(run, first))
        for column, (title, label, _) in enumerate(METRICS):
            ax = axes[row, column]
            ax.set_ylabel(f"{algorithm.upper()}\n{label}" if column == 0 else label, fontsize=9)
            ax.grid(alpha=0.25)
            if row == 0:
                ax.set_title(title)
            if row == len(algorithms) - 1:
                ax.set_xlabel("Elapsed sending time (s)")
            if column == 2:
                ax.yaxis.set_major_locator(MaxNLocator(integer=True))
        if not group:
            axes[row, 0].text(0.5, 0.5, "no completed run", transform=axes[row, 0].transAxes, ha="center")
        elif len(group) > 1:
            axes[row, 3].legend(fontsize=7, loc="lower right")
    # Shared y-limits must be set once, after every row is drawn, or row 0 freezes them.
    for column, top in enumerate(tops):
        axes[0, column].set_ylim(0, top * 1.05 if top else 1)
    fig.suptitle(f"A3: TCP statistics by algorithm, {destination[0]}:{destination[1]}\n"
                 "Each column shares one y-scale; first repetition bold, later repetitions faint")
    fig.text(0.5, 0.01, "cwnd/RTT are endpoint snapshots; goodput/loss cover the preceding interval.",
             ha="center", fontsize=9)
    fig.tight_layout(rect=(0, 0.03, 1, 0.93))
    fig.savefig(directory / "q3-timeseries.pdf")
    plt.close(fig)


def plot_scatter(directory, runs, algorithms, destination):
    """Q2(b)(ii) for each algorithm: cwnd, RTT, and loss against goodput on shared axes."""
    fig, axes = plt.subplots(len(algorithms), 3, figsize=(14, 3.3 * len(algorithms) + 1.2),
                             sharex=True, sharey="col", squeeze=False)
    tops, right = [0] * 3, 0
    for row, algorithm in enumerate(algorithms):
        color, marker, _ = style(algorithm, algorithms)
        samples = [s for r in runs if r["actual_algorithm"] == algorithm for s in measured_samples(r)]
        goodput = [s["goodput_bps"] / 1e6 for s in samples]
        right = max(right, *goodput, 0)
        for column, (title, label, value) in enumerate(METRICS[:3]):
            ax = axes[row, column]
            values = [value(s) for s in samples]
            tops[column] = max(tops[column], *values, 0)
            ax.scatter(goodput, values, s=12, color=color, marker=marker, alpha=0.45,
                       linewidths=0)
            ax.set_ylabel(f"{algorithm.upper()}\n{label}" if column == 0 else label, fontsize=9)
            ax.grid(alpha=0.25)
            if row == 0:
                ax.set_title(f"{title} vs goodput")
            if row == len(algorithms) - 1:
                ax.set_xlabel("Goodput (Mbit/s)")
            if column == 2:
                ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    axes[0, 0].set_xlim(0, right * 1.05 if right else 1)
    for column, top in enumerate(tops):
        axes[0, column].set_ylim(-0.05 * (top or 1), top * 1.05 if top else 1)
    fig.suptitle(f"A3: TCP metrics and goodput by algorithm, {destination[0]}:{destination[1]}\n"
                 "All repetitions pooled; each column shares axes. Correlation does not establish causation.")
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig.savefig(directory / "q3-scatter.pdf")
    plt.close(fig)


def strip(ax, metrics, algorithms, field, label):
    """Box per algorithm with each run drawn as a point."""
    present = [a for a in algorithms if any(m["actual_algorithm"] == a and m[field] is not None for m in metrics)]
    values = [[m[field] for m in metrics if m["actual_algorithm"] == a and m[field] is not None] for a in present]
    if not present:
        ax.text(0.5, 0.5, "no data", transform=ax.transAxes, ha="center")
        return
    ax.boxplot(values, widths=0.5, showfliers=False, medianprops={"color": "#333333"})
    for index, (algorithm, points) in enumerate(zip(present, values), 1):
        color, marker, _ = style(algorithm, algorithms)
        # Deterministic jitter keeps the PDF reproducible.
        offsets = [((i * 0.618) % 1 - 0.5) * 0.3 for i in range(len(points))]
        ax.scatter([index + o for o in offsets], points, s=22, color=color, marker=marker, alpha=0.8, zorder=3)
    ax.set_xticks(range(1, len(present) + 1), [a.upper() for a in present])
    ax.set_ylabel(label)
    ax.grid(alpha=0.25, axis="y")


def plot_compare(directory, metrics, algorithms):
    fig, axes = plt.subplots(2, 2, figsize=(12, 9))
    strip(axes[0, 0], metrics, algorithms, "overall_goodput_mbps", "Overall goodput per run (Mbit/s)")
    axes[0, 0].set_title("(a) Throughput")
    axes[0, 0].set_ylim(bottom=0)
    strip(axes[0, 1], metrics, algorithms, "queueing_delay_ms", "Median RTT − min RTT (ms)")
    axes[0, 1].set_title("(b) Queueing delay")
    axes[0, 1].set_ylim(bottom=0)
    strip(axes[1, 0], metrics, algorithms, "cwnd_to_bdp", "Median cwnd·MSS / BDP estimate")
    axes[1, 0].set_title("(c) Window relative to bandwidth-delay product")
    axes[1, 0].axhline(1, color="#666666", linewidth=1, linestyle=":")
    axes[1, 0].text(0.99, 1, " cwnd = BDP", color="#666666", fontsize=8, ha="right", va="bottom",
                    transform=axes[1, 0].get_yaxis_transform())
    ratios = [m["cwnd_to_bdp"] for m in metrics if m["cwnd_to_bdp"]]
    if ratios and max(ratios) / min(ratios) > 20:
        axes[1, 0].set_yscale("log")
    ax = axes[1, 1]
    for algorithm in algorithms:
        rows = [m for m in metrics if m["actual_algorithm"] == algorithm]
        if rows:
            color, marker, _ = style(algorithm, algorithms)
            ax.scatter([m["min_rtt_ms"] for m in rows], [m["overall_goodput_mbps"] for m in rows], s=26,
                       color=color, marker=marker, alpha=0.8, label=algorithm.upper())
    ax.set(title="(d) Goodput vs path base RTT", xlabel="Min RTT of run (ms)", ylabel="Overall goodput (Mbit/s)")
    ax.set_xlim(left=0)
    ax.set_ylim(bottom=0)
    ax.grid(alpha=0.25)
    ax.legend(fontsize=9)
    fig.suptitle(f"A3: algorithm comparison across all destinations ({len(metrics)} completed runs; one point per run)")
    fig.text(0.5, 0.01, "BDP estimate = max kernel delivery rate × min RTT. Public paths change between runs.",
             ha="center", fontsize=9)
    fig.tight_layout(rect=(0, 0.03, 1, 0.95))
    fig.savefig(directory / "q3-compare.pdf")
    plt.close(fig)


def generate_comparison(directory, runs, manifest):
    algorithms = manifest["algorithms"]
    completed = [run for run in runs if run["status"] == "success"]
    metrics = [run_metrics(run) for run in completed]
    summary = comparison_tables(directory, metrics, algorithms)
    plot_goodput_by_destination(directory, completed, manifest, algorithms)
    destination = representative_destination(completed, manifest)
    names = ("q3-timeseries.pdf", "q3-scatter.pdf", "q3-compare.pdf")
    if destination is None:
        for name in names:
            (directory / name).unlink(missing_ok=True)
        return summary
    chosen = [run for run in completed if (run["host"], run["port"]) == destination]
    plot_timeseries(directory, chosen, algorithms, destination)
    plot_scatter(directory, chosen, algorithms, destination)
    plot_compare(directory, metrics, algorithms)
    print(f"Q3 representative destination: {destination[0]}:{destination[1]} ({len(chosen)} runs)")
    return summary
