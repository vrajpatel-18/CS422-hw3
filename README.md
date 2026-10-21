# Assignment 3: DART congestion control in eBPF (Option 1)

DART (the controller designed in Assignment 2, Q3(b)) is implemented as an eBPF
`struct_ops` TCP congestion control in [dart/dart.bpf.c](dart/dart.bpf.c). The
Python iPerf3 client from Assignment 2 selects it on the data socket with
`TCP_CONGESTION` and compares it with CUBIC, Reno, and BBR. See [REPORT.md](REPORT.md), the submission PDF [REPORT.pdf](REPORT.pdf), and
the [live Linux demonstration guide](DEMO.md).

## Environment

| | Version |
| --- | --- |
| OS | Ubuntu 26.04.1 LTS (arm64), UTM VM on an Apple M3 Mac, Shared Network (NAT) |
| Kernel | `7.0.0-38-generic` |
| Toolchain | clang 21.1.8, bpftool 7.7.0 (libbpf 1.7), libbpf-dev 1.6.3 |
| Python | 3.14.4, matplotlib 3.11.2 |

Required kernel features (all on in Ubuntu's kernel): `CONFIG_BPF_SYSCALL`,
`CONFIG_BPF_JIT`, `CONFIG_DEBUG_INFO_BTF` (`/sys/kernel/btf/vmlinux`), and BPF
`struct_ops` for `tcp_congestion_ops` (Linux 5.6+; `.struct_ops.link` needs 6.4+).
BBR is the `tcp_bbr` module.

## Setup, build, load

```sh
sudo apt install clang llvm libbpf-dev bpftool make python3-venv
sudo modprobe tcp_bbr
sudo sysctl -w net.ipv4.tcp_allowed_congestion_control="reno cubic bbr"   # lets non-root select bbr
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

make -C dart          # generates dart/vmlinux.h from the running kernel, builds dart.bpf.o
make -C dart load     # registers "dart"; prints the available list, which must include dart
```

`dart` is registered with `TCP_CONG_NON_RESTRICTED`, so any user can select it.
Loading does not survive a reboot. After all tests have finished, run
`make -C dart unload` to remove the pinned link `/sys/fs/bpf/dart`.
`make -C dart clean && make -C dart DEBUG=1` adds per-round
`bpf_printk` output (`sudo cat /sys/kernel/tracing/trace_pipe`).

Quick check that a socket really uses DART:

```sh
python3 -c "import socket; s=socket.socket(); s.setsockopt(6, 13, b'dart'); print(s.getsockopt(6, 13, 16))"
```

## Run

```sh
run_dir="results/a3-$(date +%Y%m%d-%H%M%S)"
.venv/bin/python experiment.py run --servers servers.csv --output "$run_dir" \
    -n 5 -t 10 --interval 0.2 -r 3 --seed 422
.venv/bin/python experiment.py plot "$run_dir"    # rebuild tables/PDFs from this run
```

Defaults are now `--algorithms dart,cubic,reno,bbr` and `-t 10`; other options are
unchanged from Assignment 2 (`experiment.py --help`). Each repetition rotates the
algorithm order; `experiment.json` records every attempt, its order, and failures.

The submitted run repeats all Assignment 2 Q2(b) plots for DART, CUBIC, Reno,
and BBR at the same A2 representative destination,
`speedtest.lax12.us.leaseweb.net:5207` (three successful runs per algorithm).
`q3-timeseries.pdf` shows cwnd, RTT, retransmissions per interval, and goodput
over time; `q3-scatter.pdf` shows cwnd, RTT, and retransmissions against goodput.
Both use labeled algorithm rows and shared axis scales across algorithms.
`experiment.py plot` also refreshes the report PNGs in the run's `png/` directory;
rebuild `REPORT.pdf` afterward to embed them. A3 uses 10-second runs as required
by its handout; the A2 Q2 trace used 20 seconds.

## Files

| File | Purpose |
| --- | --- |
| `dart/dart.bpf.c`, `dart/Makefile` | DART implementation; build/load/unload |
| `experiment.py`, `iperf_client.py`, `tcp_stats.py` | A2 client: iPerf3 upload, `TCP_INFO` sampling |
| `reporting.py`, `comparison.py` | Tables and PDF plots |
| `results/a3/` | Raw runs (`runs/*.json`, `samples.csv`), tables (`*.csv`), plots (`*.pdf`) |

Changes from the Assignment 2 code: default algorithms and duration, a DART
plot style, two extra columns in `q3-algorithms.csv` (run-goodput stdev, p95 RTT),
and plot titles.

## Submission PDF

`REPORT.pdf` is the single report for Brightspace. To rebuild it from `REPORT.md`:

```sh
.venv/bin/pip install -r requirements-report.txt
.venv/bin/python tools/build_report.py
```

The PDF uses the saved plot PNGs, embeds the analysis, and converts repository-relative
links to GitHub links so they remain usable outside the checkout. Report generation
uses additional dependencies; the experiment itself still needs only `requirements.txt`.
