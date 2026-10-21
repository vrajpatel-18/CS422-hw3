# Live Linux demonstration: DART in eBPF

Run these commands inside your Ubuntu VM, from the project root (for example,
`cd ~/hw3`). Use the VM where you collected the measurements if possible.
The saved experiment used kernel `7.0.0-38-generic` on arm64. This guide does not
assume the Mac can compile or load the Linux eBPF program.

## Successful rehearsal

On October 21, 2026, the live demonstration completed successfully on Ubuntu
24.04 in UTM on an Apple Silicon Mac, using kernel `7.0.0-31-generic`.
DART compiled and loaded, and the loopback test (`127.0.0.1:55201`) completed
eight successful runs: two each for DART, CUBIC, Reno, and BBR. Validation
confirmed that every run used the requested algorithm, and all three plots
(time series, scatter, and comparison) were generated and opened.

This rehearsal verifies functionality in the demonstration environment. The
original public-path evaluation remains in `results/a3`, with its experiment
environment documented separately in `REPORT.md`.

## 1. Prepare before the session

Copy the updated project into the VM, including `dart/`, the Python source,
`requirements.txt`, and `results/a3/`. Keep `REPORT.pdf` available to show the
figures and analysis. Install dependencies while you have network access:

```sh
sudo apt update
sudo apt install clang llvm libbpf-dev bpftool make python3-venv iperf3
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Record and show the environment:

```sh
uname -a
clang --version
bpftool version
test -r /sys/kernel/btf/vmlinux && echo "Kernel BTF available"
grep -E 'CONFIG_(BPF_SYSCALL|BPF_JIT|DEBUG_INFO_BTF)=' /boot/config-$(uname -r)
```

The three kernel options should be enabled. The implementation also requires
BPF TCP congestion-control struct_ops and `.struct_ops.link` support; use the
compatible VM kernel from the experiment. Check that bpffs is mounted:

```sh
mountpoint /sys/fs/bpf
# Only if the previous command says it is not a mountpoint:
sudo mount -t bpf bpf /sys/fs/bpf
```

## 2. Compile and load in front of the grader

Finish any previous tests first. Unload an earlier pinned copy before registering
again; the clean step regenerates kernel-specific BTF headers and the object.

```sh
make -C dart unload
make -C dart clean
make -C dart
sudo modprobe tcp_bbr
sudo sysctl -w net.ipv4.tcp_allowed_congestion_control="reno cubic bbr"
make -C dart load
cat /proc/sys/net/ipv4/tcp_available_congestion_control
```

Expected: compilation succeeds and the final list includes `reno cubic bbr dart`
(order can vary). Do not unload DART until the demonstration finishes.

Show that an ordinary Python socket can actually select each algorithm:

```sh
.venv/bin/python - <<'PY'
from tcp_stats import check_algorithms
check_algorithms(['dart', 'cubic', 'reno', 'bbr'])
print('All four algorithms are selectable on TCP sockets.')
PY
```

## 3. Start a local server in a second VM terminal

```sh
iperf3 -s -B 127.0.0.1 -p 5201
```

Leave this terminal running. If port 5201 is occupied, stop your earlier server
or choose another unused port and change the CSV below to match. An installed
iperf3 service does not need to be used or stopped if you choose another port.

## 4. Run the Python client in the first terminal

The local server avoids public-server refusals during the demonstration. Loopback
proves build/load, socket selection, logging, and plotting; it does not reproduce
public-path congestion or replace the submitted evaluation.

```sh
mkdir -p results
printf 'host,port\n127.0.0.1,5201\n' > results/demo-servers.csv
demo_dir="results/demo-$(date +%Y%m%d-%H%M%S)"
.venv/bin/python experiment.py run \
    --servers results/demo-servers.csv --output "$demo_dir" \
    --algorithms dart,cubic,reno,bbr \
    -n 1 -t 10 --interval 0.2 -r 2 --seed 422 --pause 5
```

Allow about two minutes plus plot generation. There should be 8 successful runs,
with each run printing the selected data-socket algorithm. Repetition 1 uses
dart/cubic/reno/bbr; repetition 2 rotates to cubic/reno/bbr/dart. Every algorithm
uses the same destination, 10-second duration, and 0.2-second requested interval.

Validate the saved runs and show a measured sample from each algorithm:

```sh
.venv/bin/python - "$demo_dir" <<'PY'
import json
import sys
from collections import Counter
from pathlib import Path

root = Path(sys.argv[1])
manifest = json.loads((root / 'experiment.json').read_text())
runs = [json.loads((root / a['result_file']).read_text()) for a in manifest['attempts']]
assert manifest['status'] == 'complete', manifest['status']
assert manifest['shortfall'] == 0
assert manifest['failed_later_runs'] == 0
successes = [r for r in runs if r['status'] == 'success']
assert Counter(r['actual_algorithm'] for r in successes) == Counter(
    dart=2, cubic=2, reno=2, bbr=2)
for algorithm in ['dart', 'cubic', 'reno', 'bbr']:
    group = [r for r in successes if r['actual_algorithm'] == algorithm]
    for run in group:
        assert run['requested_algorithm'] == run['actual_algorithm']
        assert run['duration_seconds'] == 10
        assert run['sampling_interval_seconds'] == 0.2
        assert run['samples'][-1]['elapsed_seconds'] >= 10
    sample = next(s for s in group[0]['samples'] if s['goodput_bps'] is not None)
    print(algorithm, {
        'cwnd_segments': sample['snd_cwnd_segments'],
        'goodput_Mbit_s': round(sample['goodput_bps'] / 1e6, 2),
        'RTT_ms': round(sample['rtt_us'] / 1000, 3),
        'retransmitted_segments': sample['retrans_delta_segments'],
    })
print('PASS: eight runs, correct algorithms, duration, interval, and logged metrics.')
PY
```

Loopback can have no retransmissions; a zero loss proxy is a valid measurement.
Samples use actual elapsed time, so intervals may vary slightly with scheduling.

## 5. Show the generated figures and original evaluation

```sh
ls "$demo_dir"
.venv/bin/python experiment.py plot "$demo_dir"
```

Open `q3-timeseries.pdf`, `q3-scatter.pdf`, and `q3-compare.pdf` in that directory
with your VM's file manager. Show that rows are labeled by algorithm and scales
are consistent across algorithms. Then show `REPORT.pdf` and the original
`results/a3/` plots for the public-path results: 5 destinations, 3 repetitions,
60 successful runs, and two recorded failed attempts before server replacement.

To repeat the full public-server evaluation, use a fresh directory:

```sh
public_dir="results/public-demo-$(date +%Y%m%d-%H%M%S)"
.venv/bin/python experiment.py run --servers servers.csv --output "$public_dir" \
    --algorithms dart,cubic,reno,bbr \
    -n 5 -t 10 --interval 0.2 -r 3 --seed 422
```

Allow roughly 15–20 minutes or longer if servers fail. Public-server availability
and performance can differ from the recorded experiment.

## 6. Explain these points during the oral exam

- `dart/dart.bpf.c` registers TCP congestion-control callbacks through eBPF
  `struct_ops`. The Python client selects `dart` on its data socket before
  `connect()` and reads back the actual algorithm afterward.
- RTT and delivery-rate samples estimate the minimum RTT, bottleneck bandwidth,
  bandwidth-delay product, and queued packets. STARTUP grows quickly; CRUISE
  targets 2–4 queued packets; COMPETE grows when persistent delay suggests
  competing traffic; PROBE_RTT briefly reduces cwnd when the minimum becomes stale.
- Kernel loss detection calls `dart_ssthresh`: a full-queue classification targets
  70% of cwnd; otherwise it targets at least 90% or the BDP, bounded by the old cwnd.
  RTO restarts STARTUP. The experiment logs retransmissions as its loss proxy.
- Jitter adjustments were needed because a few packets of target queue correspond
  to much less delay than Wi-Fi/NAT jitter. Explain the per-round minimum RTT,
  RTT-variation allowance, and startup delay threshold.
- DART had fewer total retransmissions (1117 versus 2041–2600) but lower goodput
  on short paths. On the high-RTT path, early startup exit hurt DART and CUBIC.
  Lower retransmissions alone do not establish a better controller.
- The evaluation uses public paths, one flow at a time, and only three repetitions.
  It does not establish competing-flow fairness. The 10-second tests did not
  exercise the stale-minimum PROBE_RTT behavior.

## 7. Clean up after showing the results

Stop the foreground iperf3 server with Ctrl-C in its terminal. Once client sockets
have closed, unload DART:

```sh
make -C dart unload
cat /proc/sys/net/ipv4/tcp_available_congestion_control
```

The available list should no longer include `dart`.

## Troubleshooting

- **Already registered / file exists:** finish old tests, run `make -C dart unload`,
  then load again. Do not register a second copy of the same algorithm.
- **Missing BTF or unsupported struct_ops:** boot the compatible Ubuntu VM kernel;
  rebuilding on macOS will not resolve Linux kernel feature requirements.
- **Missing BBR:** run `sudo modprobe tcp_bbr`; if the module is unavailable, use
  a kernel that provides it. Do not silently omit the required baseline.
- **Cannot select algorithm:** confirm the load succeeded, the list contains all
  four names, and the sysctl step ran. `dart` itself is non-restricted.
- **Permission denied loading BPF:** use the provided make target, which invokes
  `sudo bpftool`; inspect the printed kernel/verifier error if it still fails.
- **Output directory exists:** choose a fresh timestamped directory. Existing
  measurements are deliberately protected.
- **Public server refuses/resets a test:** use the local demonstration first;
  the experiment logs failures and attempts replacements for the full evaluation.
- **Need debug traces:** after unloading, rebuild with `make -C dart clean` then
  `make -C dart DEBUG=1`, reload, and read `sudo cat /sys/kernel/tracing/trace_pipe`
  in another terminal. Stop trace reading with Ctrl-C when finished. Rebuilding
  without cleaning can leave the old object because flags are not dependencies.
