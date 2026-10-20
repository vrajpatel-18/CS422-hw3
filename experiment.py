#!/usr/bin/env python3
"""Run DART/CUBIC/Reno/BBR uploads (A3; A2 Q1-Q3 code) or regenerate their tables and PDF plots."""

import argparse
import csv
import json
import math
import platform
import random
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from iperf_client import ClientError, run_test
from reporting import generate_reports
from tcp_stats import StatsError, check_algorithms, check_support


def load_servers(path):
    """Read host,port CSV. A port can be an integer or an inclusive a-b range."""
    hosts = {}
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(line for line in stream if not line.lstrip().startswith("#"))
        if not reader.fieldnames or not {"host", "port"} <= set(reader.fieldnames):
            raise ValueError("server CSV must have host,port column headers")
        for number, row in enumerate(reader, 2):
            host = (row.get("host") or "").strip().lower().rstrip(".")
            port_text = (row.get("port") or "").strip()
            try:
                bounds = port_text.split("-")
                if len(bounds) not in (1, 2):
                    raise ValueError()
                first, last = int(bounds[0]), int(bounds[-1])
                if not host or any(char.isspace() for char in host) or not 1 <= first <= last <= 65535:
                    raise ValueError()
            except ValueError as exc:
                raise ValueError(f"invalid host or port at CSV row {number}: {host!r}, {port_text!r}") from exc
            hosts.setdefault(host, set()).update(range(first, last + 1))
    if not hosts:
        raise ValueError("server list is empty")
    return [{"host": host, "ports": sorted(ports)} for host, ports in sorted(hosts.items())]


def destination_plan(servers, seed):
    rng = random.Random(seed)
    shuffled = list(servers)
    rng.shuffle(shuffled)
    # Count each hostname once, even if several rows/ports describe the server.
    return [{"host": server["host"], "port": rng.choice(server["ports"])} for server in shuffled]


def save_json(path, value):
    # Preserve a readable manifest if the process stops between attempts.
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def host_metadata():
    result = {"platform": platform.platform(), "kernel": platform.release(),
              "python": platform.python_version()}
    for name in ("tcp_available_congestion_control", "tcp_allowed_congestion_control",
                 "tcp_congestion_control"):
        path = Path("/proc/sys/net/ipv4") / name
        result[name] = path.read_text().strip() if path.exists() else None
    return result


def algorithm_order(algorithms, repetition):
    """Rotate the order each repetition (a Latin square), so no algorithm always runs first."""
    shift = (repetition - 1) % len(algorithms)
    return algorithms[shift:] + algorithms[:shift]


def run_experiment(servers_path, output, count=10, duration=60, interval=0.2,
                   timeout=10, max_attempts=None, seed=None, algorithms=None, repetitions=1,
                   pause=5, retries=1):
    if count < 1 or type(duration) is not int or not 1 <= duration <= 86400:
        raise ValueError("count must be positive; duration must be an integer in 1..86400")
    if not all(math.isfinite(value) and value > 0 for value in (interval, timeout)):
        raise ValueError("interval and timeout must be finite positive seconds")
    if repetitions < 1:
        raise ValueError("repetitions must be at least 1")
    if not math.isfinite(pause) or pause < 0 or retries < 0:
        raise ValueError("pause must be finite and non-negative; retries must be non-negative")
    max_attempts = 2 * count if max_attempts is None else max_attempts
    if max_attempts < count:
        raise ValueError("max-attempts must be at least the requested destination count")
    servers_path, output = Path(servers_path), Path(output)
    servers = load_servers(servers_path)
    if algorithms:
        # Q3: every requested algorithm must be selectable before any traffic is sent.
        check_algorithms(algorithms)
        policy = "fixed list; order rotated each repetition"
    else:
        # Q1/Q2: read the OS default once, then pin that same baseline on every data socket.
        algorithms = [check_support()]
        policy = "OS default at experiment start"
    # Import before sending traffic, so a missing plotting dependency is caught early.
    import matplotlib
    seed = random.randrange(2**32) if seed is None else seed
    plan = destination_plan(servers, seed)[:max_attempts]
    output.mkdir(parents=True, exist_ok=False)
    (output / "runs").mkdir()
    source = servers_path.read_bytes()
    (output / "servers.csv").write_bytes(source)
    manifest = {
        "started_utc": datetime.now(timezone.utc).isoformat(), "status": "running",
        "requested_destinations": count, "successful_destinations": 0, "shortfall": count,
        "seed": seed, "max_attempts": max_attempts,
        "duration_seconds": duration, "sampling_interval_seconds": interval,
        "timeout_seconds": timeout, "host_metadata": host_metadata(),
        "algorithms": algorithms, "repetitions": repetitions,
        "pause_seconds": pause, "retries_per_run": retries,
        "algorithm_policy": policy,
        "matplotlib_version": matplotlib.__version__,
        "server_list_source": "https://iperf3serverlist.net/",
        "input_server_file": str(servers_path.resolve()),
        "initial_selection": plan[:count], "candidate_order": plan,
        "admitted_destinations": [], "failed_later_runs": 0,
        "attempts": [],
    }
    if policy.startswith("OS default"):
        manifest["baseline_algorithm"] = algorithms[0]
    save_json(output / "experiment.json", manifest)
    print(f"Seed: {seed}; requesting {count} distinct server hostnames; at most "
          f"{min(max_attempts, len(plan))} destinations tried; algorithms {', '.join(algorithms)}; "
          f"{repetitions} repetition(s).", flush=True)
    interrupted = False

    def run_one(destination, algorithm, repetition, order_index):
        """Run one cell of the schedule, retrying the same server a bounded number of times."""
        nonlocal interrupted
        for retry in range(retries + 1):
            # Many public servers reset a new control connection that arrives right after
            # the previous test ends, so wait before every test except the first.
            if manifest["attempts"] and pause:
                try:
                    time.sleep(pause)
                except KeyboardInterrupt:
                    interrupted = True
                    return False
            if attempt_once(destination, algorithm, repetition, order_index, retry) or interrupted:
                return not interrupted
        return False

    def attempt_once(destination, algorithm, repetition, order_index, retry):
        nonlocal interrupted
        index = len(manifest["attempts"]) + 1
        run_id = str(uuid.uuid4())
        attempt_started = datetime.now(timezone.utc).isoformat()
        label = f" [{algorithm}, repetition {repetition}]" if len(algorithms) > 1 or repetitions > 1 else ""
        label += f" (retry {retry})" if retry else ""
        print(f"Attempt {index}: {destination['host']}:{destination['port']}{label}", flush=True)
        try:
            result = run_test(**destination, duration=duration, timeout=timeout,
                              interval=interval, algorithm=algorithm)
        except ClientError as exc:
            result = {**exc.details, "status": "failed", "error": str(exc)}
        except KeyboardInterrupt as exc:
            interrupted = True
            result = {**getattr(exc, "details", {}), "status": "interrupted", "error": "Ctrl-C"}
        result.update(destination)
        result.update({"run_id": run_id, "attempt": index, "attempt_started_utc": attempt_started,
                       "requested_algorithm": algorithm, "repetition": repetition,
                       "order_index": order_index, "retry": retry})
        result_file = f"runs/attempt-{index:03d}.json"
        save_json(output / result_file, result)
        manifest["attempts"].append({**destination, "run_id": run_id, "attempt": index,
                                     "algorithm": algorithm, "repetition": repetition,
                                     "order_index": order_index, "retry": retry,
                                     "status": result["status"], "result_file": result_file,
                                     "error": result.get("error")})
        if result["status"] == "success":
            print(f"  Completed; data-socket algorithm: {result['actual_algorithm']}", flush=True)
        else:
            print(f"  {result['status']}: {result['error']}", file=sys.stderr, flush=True)
        save_json(output / "experiment.json", manifest)
        return result["status"] == "success"

    # Repetition 1 admits destinations: a server counts only if it completes a test with
    # every algorithm, so all algorithms are compared on the same destinations.
    for destination in plan:
        if manifest["successful_destinations"] >= count or interrupted:
            break
        admitted = True
        for order_index, algorithm in enumerate(algorithm_order(algorithms, 1), 1):
            if not run_one(destination, algorithm, 1, order_index):
                admitted = False
                break
        if admitted:
            manifest["admitted_destinations"].append(destination)
            manifest["successful_destinations"] += 1
        manifest["shortfall"] = max(0, count - manifest["successful_destinations"])
        save_json(output / "experiment.json", manifest)
    # Later repetitions sweep all admitted destinations, so repeats are minutes apart.
    # Failures here are recorded as missing runs, not retried or replaced.
    for repetition in range(2, repetitions + 1):
        for destination in manifest["admitted_destinations"]:
            for order_index, algorithm in enumerate(algorithm_order(algorithms, repetition), 1):
                if interrupted:
                    break
                if not run_one(destination, algorithm, repetition, order_index):
                    manifest["failed_later_runs"] += 1
    manifest["status"] = "interrupted" if interrupted else ("complete" if manifest["shortfall"] == 0 else "shortfall")
    manifest["finished_utc"] = datetime.now(timezone.utc).isoformat()
    save_json(output / "experiment.json", manifest)
    generate_reports(output)
    print(f"Completed {manifest['successful_destinations']}/{count} destinations; "
          f"shortfall: {manifest['shortfall']}; failed later runs: {manifest['failed_later_runs']}. "
          f"Results: {output}", flush=True)
    return manifest


def algorithm_list(text):
    """Parse --algorithms; 'default' (None) means the OS default, as in Q1/Q2."""
    names = [name.strip().lower() for name in text.split(",") if name.strip()]
    if names == ["default"]:
        return None
    if not names or len(set(names)) != len(names) or not all(name.isalnum() for name in names):
        raise argparse.ArgumentTypeError("expected distinct names such as cubic,reno,bbr")
    return names


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="measure destinations and generate reports (Linux/WSL2)")
    run.add_argument("--servers", type=Path, default=Path(__file__).with_name("servers.csv"))
    run.add_argument("--output", type=Path, required=True, help="new output directory; existing directories are protected")
    run.add_argument("-n", "--count", type=int, default=10)
    run.add_argument("-t", "--duration", type=int, default=10)
    run.add_argument("--interval", type=float, default=0.2)
    run.add_argument("--timeout", type=float, default=10)
    run.add_argument("--max-attempts", type=int, help="total destinations tried, including replacements (default: 2*n)")
    run.add_argument("--algorithms", type=algorithm_list, default="dart,cubic,reno,bbr",
                     help="comma-separated algorithms, or 'default' for the OS default only (default: dart,cubic,reno,bbr)")
    run.add_argument("-r", "--repetitions", type=int, default=3,
                     help="runs per destination and algorithm (default: 3)")
    run.add_argument("--pause", type=float, default=5,
                     help="seconds to wait before each test; public servers often reset back-to-back tests (default: 5)")
    run.add_argument("--retries", type=int, default=1,
                     help="extra tries of the same server after a failed test, each logged (default: 1)")
    run.add_argument("--seed", type=int, help="random seed; generated and recorded if omitted")
    plot = commands.add_parser("plot", help="rebuild tables/PDF from saved measurements on any supported Python host")
    plot.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "plot":
            generate_reports(args.directory)
            print(f"Rebuilt reports in {args.directory}")
            return 0
        manifest = run_experiment(args.servers, args.output, args.count, args.duration,
                                  args.interval, args.timeout, args.max_attempts, args.seed,
                                  args.algorithms, args.repetitions, args.pause, args.retries)
        return 130 if manifest["status"] == "interrupted" else (1 if manifest["shortfall"] else 0)
    except (ValueError, OSError, StatsError, ImportError) as exc:
        print(f"experiment: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
