"""Linux TCP_INFO decoding and acknowledged-goodput sampling on the data socket."""

import math
import platform
import socket
import struct
import sys
import time
from datetime import datetime, timezone


# Linux UAPI struct tcp_info: native endian, explicit byte offsets, fixed widths.
# https://github.com/torvalds/linux/blob/v6.8/include/uapi/linux/tcp.h
TCP_INFO_SIZE = 256
LINUX_TCP_INFO = 11
LINUX_TCP_CONGESTION = 13
FIELDS = {
    "snd_mss_bytes": (16, "I"),
    "rtt_us": (68, "I"),
    "rttvar_us": (72, "I"),
    "snd_cwnd_segments": (80, "I"),
    "total_retrans_segments": (100, "I"),
    "pacing_rate_bytes_per_second": (104, "Q"),
    "bytes_acked": (120, "Q"),
    "min_rtt_us": (148, "I"),
    "delivery_rate_bytes_per_second": (160, "Q"),
}
REQUIRED_SIZE = 128  # Last required field is the 8-byte bytes_acked at offset 120.
FIELD_UNITS = {
    "snd_mss_bytes": "bytes",
    "rtt_us": "microseconds", "rttvar_us": "microseconds", "min_rtt_us": "microseconds (kernel windowed minimum)",
    "snd_cwnd_segments": "segments", "total_retrans_segments": "segments (cumulative)",
    "pacing_rate_bytes_per_second": "bytes/second", "bytes_acked": "bytes (cumulative)",
    "delivery_rate_bytes_per_second": "bytes/second",
}


class StatsError(Exception):
    pass


def decode_tcp_info(raw):
    if len(raw) < REQUIRED_SIZE:
        raise StatsError(
            f"TCP_INFO returned {len(raw)} bytes; at least {REQUIRED_SIZE} are "
            "required for tcpi_bytes_acked. Use a supported Linux/WSL2 kernel."
        )
    info = {"tcp_info_length": len(raw)}
    for name, (offset, fmt) in FIELDS.items():
        # Linux appends fields over time. Never decode a partial optional field.
        info[name] = (struct.unpack_from("=" + fmt, raw, offset)[0]
                      if offset + struct.calcsize("=" + fmt) <= len(raw) else None)
    # Unlimited pacing uses unsigned-long MAX (32 or 64 bits). A zero delivery
    # rate means the kernel has no valid delivery-rate estimate yet.
    if info["pacing_rate_bytes_per_second"] in (2**32 - 1, 2**64 - 1):
        info["pacing_rate_bytes_per_second"] = None
    if info["delivery_rate_bytes_per_second"] == 0:
        info["delivery_rate_bytes_per_second"] = None
    return info


def tcp_info_metadata():
    """Save the decoding contract with each run, including the execution kernel."""
    return {
        "structure": "Linux UAPI struct tcp_info", "kernel_release": platform.release(),
        "machine": platform.machine(), "byte_order": sys.byteorder,
        "alignment": "explicit offsets; =I is uint32, =Q is uint64",
        "requested_length_bytes": TCP_INFO_SIZE,
        "minimum_length_bytes": REQUIRED_SIZE,
        "fields": {name: {"offset_bytes": offset, "format": "=" + fmt, "unit": FIELD_UNITS[name]}
                   for name, (offset, fmt) in FIELDS.items()},
        "goodput_formula": "8 * delta(payload_bytes_acked) / delta(monotonic_seconds)",
        "loss_proxy": "delta(total_retrans_segments); a retransmission count, not a loss probability",
        "optional_values": "null if absent; pacing MAX means unlimited; delivery rate 0 means no estimate",
        "source": "https://github.com/torvalds/linux/blob/v6.8/include/uapi/linux/tcp.h",
    }


def read_tcp_info(sock):
    return decode_tcp_info(sock.getsockopt(socket.IPPROTO_TCP, LINUX_TCP_INFO, TCP_INFO_SIZE))


def congestion_algorithm(sock):
    return sock.getsockopt(socket.IPPROTO_TCP, LINUX_TCP_CONGESTION, 16).split(b"\0", 1)[0].decode("ascii")


def select_congestion_algorithm(sock, requested):
    """Pin the experiment's baseline on a fresh data socket before connect()."""
    sock.setsockopt(socket.IPPROTO_TCP, LINUX_TCP_CONGESTION, requested.encode("ascii"))
    actual = congestion_algorithm(sock)
    if actual != requested:
        raise StatsError(f"requested congestion algorithm {requested!r}, got {actual!r}")


def check_support():
    if platform.system() != "Linux":
        raise StatsError("Acknowledged-goodput measurements require Linux or WSL2; macOS is unsupported.")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        read_tcp_info(sock)
        return congestion_algorithm(sock)


def check_algorithms(names):
    """Fail before any traffic if this user cannot select every requested algorithm."""
    check_support()
    for name in names:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                select_congestion_algorithm(sock, name)
            except OSError as exc:
                raise StatsError(
                    f"cannot select congestion algorithm {name!r}: {exc}. It must appear in "
                    "/proc/sys/net/ipv4/tcp_available_congestion_control (e.g. sudo modprobe tcp_bbr, make -C dart load) "
                    "and, for non-root users, in tcp_allowed_congestion_control."
                ) from exc


def interval_values(previous, current):
    """Difference counters using actual time; reject resets rather than hiding them."""
    dt = current["monotonic_seconds"] - previous["monotonic_seconds"]
    delta = current["payload_bytes_acked"] - previous["payload_bytes_acked"]
    retrans = current["total_retrans_segments"] - previous["total_retrans_segments"]
    if (not math.isfinite(dt) or dt <= 0 or delta < 0 or retrans < 0
            or current["bytes_acked"] < previous["bytes_acked"]):
        raise StatsError("Invalid sampling interval or decreasing TCP counter")
    return {"interval_seconds": dt, "acked_delta_bytes": delta,
            "goodput_bps": 8 * delta / dt, "retrans_delta_segments": retrans,
            "retrans_rate_segments_per_second": retrans / dt}


class GoodputSampler:
    def __init__(self, sock, interval, cookie_ack_target):
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("sampling interval must be finite and positive")
        self.sock = sock
        self.interval = interval
        self.cookie_ack_target = cookie_ack_target
        self.samples = []

    def sample(self, kind="interval"):
        info = read_tcp_info(self.sock)
        now = time.monotonic()
        row = {
            **info, "sample_kind": kind,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "monotonic_seconds": now,
            "elapsed_seconds": now - self.samples[0]["monotonic_seconds"] if self.samples else 0,
            # Cookie ACKs can arrive after TEST_RUNNING. Exclude all cookie bytes,
            # even when they straddle the initial sampling boundary.
            "payload_bytes_acked": max(0, info["bytes_acked"] - self.cookie_ack_target),
            "interval_seconds": None, "acked_delta_bytes": None,
            "goodput_bps": None, "retrans_delta_segments": None,
            "retrans_rate_segments_per_second": None,
        }
        if self.samples:
            row.update(interval_values(self.samples[-1], row))
        self.samples.append(row)
        return now
