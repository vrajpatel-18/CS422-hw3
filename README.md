# Assignment 3: DART congestion control in eBPF (Option 1)

DART (the controller designed in Assignment 2, Q3(b)) is implemented as an eBPF
`struct_ops` TCP congestion control in [dart/dart.bpf.c](dart/dart.bpf.c). The
Python iPerf3 client from Assignment 2 selects it on the data socket with
`TCP_CONGESTION` and compares it with CUBIC, Reno, and BBR. See [REPORT.md](REPORT.md).

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
make -C dart unload   # removes the pinned link /sys/fs/bpf/dart
```

`dart` is registered with `TCP_CONG_NON_RESTRICTED`, so any user can select it.
Loading does not survive a reboot. `make -C dart DEBUG=1` adds per-round
`bpf_printk` output (`sudo cat /sys/kernel/tracing/trace_pipe`).

Quick check that a socket really uses DART:

```sh
python3 -c "import socket; s=socket.socket(); s.setsockopt(6, 13, b'dart'); print(s.getsockopt(6, 13, 16))"
```

## Run

```sh
.venv/bin/python experiment.py run --servers servers.csv --output results/a3 \
    -n 5 -t 10 --interval 0.2 -r 3 --seed 422
.venv/bin/python experiment.py plot results/a3    # rebuild tables/PDFs from saved data
```

Defaults are now `--algorithms dart,cubic,reno,bbr` and `-t 10`; other options are
unchanged from Assignment 2 (`experiment.py --help`). Each repetition rotates the
algorithm order; `experiment.json` records every attempt, its order, and failures.

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
