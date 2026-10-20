"""Generate summary tables and Q1/Q2 PDF plots; Q3 directories also use comparison.py."""

import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.ticker import MaxNLocator


SUMMARY_FIELDS = ["host", "port", "actual_algorithm", "runs", "intervals",
                  "min_goodput_mbps", "median_goodput_mbps", "mean_goodput_mbps",
                  "p95_goodput_mbps", "overall_goodput_mbps", "acknowledged_bytes",
                  "measured_seconds"]
SAMPLE_FIELDS = ["run_id", "attempt", "host", "port", "actual_algorithm", "status",
                 "sample_kind", "timestamp_utc", "monotonic_seconds", "elapsed_seconds",
                 "interval_seconds", "bytes_acked", "payload_bytes_acked",
                 "acked_delta_bytes", "goodput_bps", "tcp_info_length",
                 "snd_cwnd_segments", "snd_mss_bytes", "rtt_us", "rttvar_us",
                 "total_retrans_segments", "retrans_delta_segments",
                 "retrans_rate_segments_per_second", "pacing_rate_bytes_per_second",
                 "delivery_rate_bytes_per_second", "min_rtt_us"]


def measured_samples(run):
    # The initial baseline has no preceding interval. Keep all other samples.
    samples = [s for s in run["samples"] if s["goodput_bps"] is not None]
    if not samples:
        raise ValueError(f"Run {run['run_id']} has no measured intervals")
    for sample in samples:
        for field in ("elapsed_seconds", "interval_seconds", "acked_delta_bytes",
                      "goodput_bps", "snd_cwnd_segments", "rtt_us", "retrans_delta_segments"):
            value = sample[field]
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"Invalid {field} in run {run['run_id']}")
        if sample["interval_seconds"] == 0:
            raise ValueError("Sampling intervals must be positive")
    return samples


def percentile(values, fraction):
    values = sorted(values)
    position = (len(values) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    return values[low] + (values[high] - values[low]) * (position - low)


def summary_rows(runs):
    groups = defaultdict(list)
    for run in runs:
        if run["status"] == "success":
            groups[(run["host"], run["port"], run["actual_algorithm"])].append(run)
    rows = []
    for (host, port, algorithm), group in sorted(groups.items()):
        samples = [s for run in group for s in measured_samples(run)]
        rates = [s["goodput_bps"] / 1e6 for s in samples]
        seconds = sum(s["interval_seconds"] for s in samples)
        acked = sum(s["acked_delta_bytes"] for s in samples)
        rows.append({
            "host": host, "port": port, "actual_algorithm": algorithm,
            "runs": len(group), "intervals": len(samples),
            "min_goodput_mbps": min(rates), "median_goodput_mbps": statistics.median(rates),
            "mean_goodput_mbps": statistics.mean(rates), "p95_goodput_mbps": percentile(rates, 0.95),
            "overall_goodput_mbps": 8 * acked / seconds / 1e6,
            "acknowledged_bytes": acked, "measured_seconds": seconds,
        })
    return rows


def write_tables(directory, runs, summaries):
    with (directory / "samples.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=SAMPLE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for run in runs:
            context = {name: run.get(name) for name in SAMPLE_FIELDS[:6]}
            for sample in run.get("samples", []):
                writer.writerow({**context, **sample})
    with (directory / "summary.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerows(summaries)


def plot_goodput(directory, runs, manifest):
    completed = [run for run in runs if run["status"] == "success"]
    with PdfPages(directory / "goodput.pdf") as pdf:
        for start in range(0, max(1, len(completed)), 4):
            batch = completed[start:start + 4]
            columns = 2 if len(batch) > 1 else 1
            rows = math.ceil(max(1, len(batch)) / columns)
            fig, axes = plt.subplots(rows, columns, figsize=(12, 4 * rows + 1), squeeze=False)
            for ax in axes.flat:
                ax.set_visible(False)
            if not completed:
                axes[0, 0].set_visible(True)
                axes[0, 0].axis("off")
                axes[0, 0].text(0, 0.5, "No completed runs. See experiment.json for failures.")
            for ax, run in zip(axes.flat, batch):
                ax.set_visible(True)
                samples = measured_samples(run)
                ax.plot([s["elapsed_seconds"] for s in samples],
                        [s["goodput_bps"] / 1e6 for s in samples], linewidth=1.2)
                ax.set_title(f"{run['host']}:{run['port']} | {run['actual_algorithm']}\n"
                             f"run {run['run_id'][:8]}", fontsize=10)
                ax.set_xlabel("Elapsed sending time (s)")
                ax.set_ylabel("Acknowledged goodput (Mbit/s)")
                ax.set_ylim(bottom=0)
                ax.grid(alpha=0.25)
            fig.suptitle(f"Q1: {len(completed)}/{manifest['requested_destinations']} destinations completed")
            fig.subplots_adjust(left=0.09, right=0.98, bottom=0.12, top=0.84,
                                hspace=0.65, wspace=0.28)
            pdf.savefig(fig)
            plt.close(fig)


def plot_tcp_metrics(directory, run):
    samples = measured_samples(run)
    elapsed = [s["elapsed_seconds"] for s in samples]
    goodput = [s["goodput_bps"] / 1e6 for s in samples]
    metrics = [
        ("Congestion window", "snd_cwnd (segments)", [s["snd_cwnd_segments"] for s in samples]),
        ("Smoothed RTT", "RTT (ms)", [s["rtt_us"] / 1000 for s in samples]),
        ("Loss proxy", "Retransmitted segments / interval", [s["retrans_delta_segments"] for s in samples]),
    ]
    identity = f"{run['host']}:{run['port']} | {run['actual_algorithm']} | run {run['run_id'][:8]}"
    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    for index, (ax, (title, label, values)) in enumerate(zip(
            axes.flat, metrics + [("Acknowledged goodput", "Goodput (Mbit/s)", goodput)])):
        if index == 2:
            ax.bar([s["elapsed_seconds"] - s["interval_seconds"] for s in samples], values,
                   width=[s["interval_seconds"] for s in samples], align="edge")
            ax.yaxis.set_major_locator(MaxNLocator(integer=True))
            if not any(values):
                ax.text(0.5, 0.5, "No retransmissions observed", transform=ax.transAxes, ha="center")
        else:
            ax.plot(elapsed, values, linewidth=1.2)
        ax.set(title=title, xlabel="Elapsed sending time (s)", ylabel=label, xlim=(0, elapsed[-1]))
        ax.set_ylim(0, max(values) * 1.05 if any(values) else 1)
        ax.grid(alpha=0.25)
    fig.suptitle(f"Q2: TCP statistics during upload\n{identity}")
    fig.text(0.5, 0.02, "cwnd/RTT are endpoint snapshots; goodput/loss cover the preceding interval.",
             ha="center", fontsize=9)
    fig.tight_layout(rect=(0, 0.04, 1, 0.91))
    fig.savefig(directory / "q2-timeseries.pdf")
    plt.close(fig)

    fig, axes = plt.subplots(1, 3, figsize=(13.5, 5))
    for index, (ax, (title, label, values)) in enumerate(zip(axes, metrics)):
        ax.scatter(goodput, values, s=18)
        ax.set(title=title + " vs goodput", xlabel="Goodput (Mbit/s)", ylabel=label)
        ax.set_xlim(left=0)
        ax.set_ylim(-0.05 if not any(values) else 0, max(values) * 1.05 if any(values) else 1)
        ax.grid(alpha=0.25)
        if index == 2:
            ax.yaxis.set_major_locator(MaxNLocator(integer=True))
            if not any(values):
                ax.text(0.5, 0.7, "Loss proxy is constant at zero", transform=ax.transAxes, ha="center", fontsize=9)
    fig.suptitle(f"Q2: TCP metrics and goodput\n{identity}")
    fig.text(0.5, 0.02, "Each point uses the same sampling interval. Correlation does not establish causation.",
             ha="center", fontsize=9)
    fig.tight_layout(rect=(0, 0.06, 1, 0.85))
    fig.savefig(directory / "q2-scatter.pdf")
    plt.close(fig)


def generate_reports(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "experiment.json").read_text(encoding="utf-8"))
    runs = [json.loads((directory / attempt["result_file"]).read_text(encoding="utf-8"))
            for attempt in manifest["attempts"]]
    completed = [run for run in runs if run["status"] == "success"]
    # Older Q1/Q2 manifests record only the baseline; Q3 manifests list every algorithm.
    algorithms = manifest.get("algorithms") or [manifest.get(
        "baseline_algorithm", completed[0]["actual_algorithm"] if completed else None)]
    if any(run["actual_algorithm"] not in algorithms for run in completed):
        raise ValueError(f"runs must use the recorded algorithms: {', '.join(map(str, algorithms))}")
    if len(algorithms) > 1 or manifest.get("repetitions", 1) > 1:
        from comparison import generate_comparison
        # Compare algorithms only on admitted destinations (complete first round). A rejected
        # destination's partial successes stay in samples.csv but not in summaries or plots.
        admitted = {(d["host"], d["port"]) for d in manifest.get("admitted_destinations", [])}
        compared = [run for run in runs if (run.get("host"), run.get("port")) in admitted] if admitted else runs
        summaries = summary_rows(compared)
        write_tables(directory, runs, summaries)
        for name in ("q2-timeseries.pdf", "q2-scatter.pdf"):
            (directory / name).unlink(missing_ok=True)
        generate_comparison(directory, compared, manifest)
        return summaries
    summaries = summary_rows(runs)
    write_tables(directory, runs, summaries)
    plot_goodput(directory, runs, manifest)
    # Q2 always uses the first success, regardless of how interesting its curve is.
    if completed:
        plot_tcp_metrics(directory, completed[0])
        print(f"Q2 representative: {completed[0]['run_id']} ({completed[0]['actual_algorithm']})")
    else:
        for name in ("q2-timeseries.pdf", "q2-scatter.pdf"):
            (directory / name).unlink(missing_ok=True)
    return summaries
