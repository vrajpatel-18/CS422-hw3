#!/usr/bin/env python3
"""One TCP upload using the iPerf3 protocol (reference: ESnet iPerf 3.17.1)."""

import json
import math
import os
import platform
import queue
import select
import socket
import struct
import threading
import time
import uuid
from datetime import datetime, timezone
from enum import IntEnum

from tcp_stats import (GoodputSampler, StatsError, check_support, congestion_algorithm,
                       read_tcp_info, select_congestion_algorithm, tcp_info_metadata)


class State(IntEnum):
    # Signed one-byte values from iperf_api.h.
    TEST_START = 1
    TEST_RUNNING = 2
    TEST_END = 4
    PARAM_EXCHANGE = 9
    CREATE_STREAMS = 10
    SERVER_TERMINATE = 11
    CLIENT_TERMINATE = 12
    EXCHANGE_RESULTS = 13
    DISPLAY_RESULTS = 14
    IPERF_DONE = 16
    ACCESS_DENIED = -1
    SERVER_ERROR = -2


BLOCK_SIZE = 128 * 1024
MAX_JSON_SIZE = 1024 * 1024


class ClientError(Exception):
    """A failed connection, protocol exchange, or upload."""

    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or {}


def remaining(deadline):
    """Return the remaining seconds of an absolute monotonic deadline."""
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("deadline expired")
    return seconds


def recv_exact(sock, size, deadline):
    """TCP may split any message; read all bytes within ONE total deadline."""
    data = bytearray()
    while len(data) < size:
        sock.settimeout(remaining(deadline))
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ClientError(
                f"premature EOF: received {len(data)} of {size} expected bytes"
            )
        data.extend(chunk)
    return bytes(data)


def send_bytes(sock, data, deadline):
    sock.settimeout(remaining(deadline))
    sock.sendall(data)


def send_json(sock, value, deadline):
    encoded = json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")
    send_bytes(sock, struct.pack("!I", len(encoded)) + encoded, deadline)


def recv_json(sock, deadline):
    size = struct.unpack("!I", recv_exact(sock, 4, deadline))[0]
    if not 0 < size <= MAX_JSON_SIZE:
        raise ClientError(f"invalid JSON frame length: {size}")
    encoded = recv_exact(sock, size, deadline)
    try:
        value = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ClientError(f"invalid server JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ClientError("expected a JSON object from server")
    if "error" in value:
        raise ClientError(f"server rejected test: {value['error']}")
    return value


def recv_state(sock, deadline):
    state = struct.unpack("!b", recv_exact(sock, 1, deadline))[0]
    if state == State.ACCESS_DENIED:
        raise ClientError("ACCESS_DENIED: server is busy or refused the test")
    if state == State.SERVER_TERMINATE:
        raise ClientError("SERVER_TERMINATE: server ended the test prematurely")
    if state == State.SERVER_ERROR:
        code = struct.unpack("!I", recv_exact(sock, 4, deadline))[0]
        # Most errors carry errno too; authorization failures can send only code.
        try:
            remote_errno = struct.unpack("!I", recv_exact(sock, 4, deadline))[0]
        except (OSError, ClientError):
            remote_errno = None
        raise ClientError(
            f"SERVER_ERROR: server rejected test (iperf error={code}, "
            f"remote errno={remote_errno})"
        )
    try:
        return State(state)
    except ValueError as exc:
        raise ClientError(f"unknown iPerf3 state: {state}") from exc


def connect_control(host, port, timeout):
    """Bound DNS resolution and all candidate connection attempts together."""
    deadline = time.monotonic() + timeout
    resolved = queue.Queue(maxsize=1)

    def resolve():
        try:
            resolved.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError as exc:
            resolved.put(exc)

    # getaddrinfo has no timeout. A daemon lets the caller exit if DNS stalls.
    threading.Thread(target=resolve, daemon=True).start()
    try:
        addresses = resolved.get(timeout=remaining(deadline))
    except queue.Empty as exc:
        raise TimeoutError("DNS resolution timed out") from exc
    if isinstance(addresses, OSError):
        raise addresses
    last_error = "DNS returned no addresses"
    for index, (family, kind, protocol, _, address) in enumerate(addresses):
        sock = socket.socket(family, kind, protocol)
        try:
            # Reserve time for other addresses when a host has IPv4 and IPv6.
            sock.settimeout(remaining(deadline) / (len(addresses) - index))
            sock.connect(address)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
        except BaseException:
            sock.close()
            raise
    raise ClientError(f"could not connect to {host}:{port}: {last_error}")


def transmit(control, data, duration, timeout, sampler):
    """Keep sending while watching control errors, data EOF, and deadlines."""
    payload = memoryview(os.urandom(BLOCK_SIZE))
    data.setblocking(False)
    started = last_progress = sampler.sample("baseline")
    deadline = started + duration
    next_sample = started + sampler.interval
    sent = offset = 0
    cpu_start = os.times()
    while True:
        now = time.monotonic()
        if now >= deadline:
            break
        if now >= next_sample:
            now = sampler.sample()
            next_sample = now + sampler.interval
            if now >= deadline:
                break
        if now - last_progress >= timeout:
            raise TimeoutError(f"data socket made no send progress for {timeout:g}s")
        readable, writable, _ = select.select(
            [control, data], [data], [],
            min(deadline - now, last_progress + timeout - now, next_sample - now),
        )
        # Check control first: the server may close both sockets on rejection.
        if control in readable:
            state = recv_state(control, time.monotonic() + timeout)
            raise ClientError(f"unexpected {state.name} during upload")
        if time.monotonic() >= deadline:
            break
        if data in readable:
            try:
                reply = data.recv(1)
            except BlockingIOError:
                reply = None
            if reply == b"":
                raise ClientError("data connection closed before the duration elapsed")
            if reply:
                raise ClientError("unexpected data from server during upload")
        if data in writable:
            try:
                count = data.send(payload[offset:])
            except BlockingIOError:
                continue
            if count == 0:
                raise ClientError("data connection accepted zero bytes")
            sent += count
            offset = (offset + count) % len(payload)
            last_progress = time.monotonic()
    elapsed = sampler.sample("final") - started
    cpu_end = os.times()
    user_percent = 100 * (cpu_end.user - cpu_start.user) / elapsed
    system_percent = 100 * (cpu_end.system - cpu_start.system) / elapsed
    retrans = (sampler.samples[-1]["total_retrans_segments"]
               - sampler.samples[0]["total_retrans_segments"])
    results = {
        "cpu_util_total": user_percent + system_percent,
        "cpu_util_user": user_percent,
        "cpu_util_system": system_percent,
        "sender_has_retransmits": 1,
        "streams": [{
            "id": 1, "bytes": sent, "retransmits": retrans,
            "jitter": 0, "errors": 0, "packets": 0,
            "omitted_errors": 0, "omitted_packets": 0,
            "start_time": 0, "end_time": elapsed,
        }],
    }
    return results, elapsed


def validate_results(results):
    """Require the receiver result for our single stream before claiming success."""
    def finite_number(value):
        try:
            return type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            return False

    for field in ("cpu_util_total", "cpu_util_user", "cpu_util_system",
                  "sender_has_retransmits"):
        value = results.get(field)
        if not finite_number(value):
            raise ClientError(f"missing or invalid server result field: {field}")
    streams = results.get("streams")
    if (not isinstance(streams, list) or len(streams) != 1
            or not isinstance(streams[0], dict) or streams[0].get("id") != 1):
        raise ClientError("server results do not contain the expected stream (id=1)")
    count = streams[0].get("bytes")
    if not finite_number(count) or not 0 <= count < 2**64:
        raise ClientError("invalid receiver byte count in server results")


def run_test(host, port=5201, duration=60, timeout=10.0, interval=0.2, algorithm=None):
    """Complete one standard iPerf3 upload or raise ClientError with its stage."""
    if not host or not 1 <= port <= 65535:
        raise ValueError("host must be nonempty and port must be in 1..65535")
    if type(duration) is not int or not 1 <= duration <= 86400:
        raise ValueError("duration must be a whole number of seconds in 1..86400")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a finite positive number of seconds")
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("sampling interval must be finite and positive")
    if algorithm is None:
        algorithm = check_support()
    control = data = None
    sampler = None
    actual_algorithm = None
    complete = False
    stage = "control connection"
    events = []
    run_started = time.monotonic()
    started_utc = datetime.now(timezone.utc).isoformat()
    decoding = tcp_info_metadata()

    def measurements():
        return {"requested_algorithm": algorithm, "actual_algorithm": actual_algorithm,
                "sampling_interval_seconds": interval, "tcp_info_decoding": decoding,
                "cookie_ack_target": sampler.cookie_ack_target if sampler else None,
                "samples": sampler.samples if sampler else []}

    def record(direction, state):
        events.append({"elapsed_seconds": time.monotonic() - run_started,
                       "direction": direction, "state": state.name})

    def expect(expected):
        state = recv_state(control, time.monotonic() + timeout)
        record("receive", state)
        if state != expected:
            raise ClientError(f"expected {expected.name}, received {state.name}")

    def send_state(state):
        send_bytes(control, struct.pack("!b", state), time.monotonic() + timeout)
        record("send", state)

    try:
        control = connect_control(host, port, timeout)
        peer = control.getpeername()
        # iPerf3 COOKIE_SIZE is 37, including the terminating NUL byte.
        cookie = str(uuid.uuid4()).encode("ascii") + b"\0"
        send_bytes(control, cookie, time.monotonic() + timeout)

        stage = "parameter negotiation"
        expect(State.PARAM_EXCHANGE)
        parameters = {
            "tcp": True, "omit": 0, "time": duration,
            "num": 0, "blockcount": 0, "parallel": 1, "len": BLOCK_SIZE,
            "client_version": "cs422-python-1.0",
        }
        send_json(control, parameters, time.monotonic() + timeout)
        expect(State.CREATE_STREAMS)

        stage = "data connection"
        data = socket.socket(control.family, socket.SOCK_STREAM)
        data.settimeout(timeout)
        select_congestion_algorithm(data, algorithm)
        # Reuse the exact control peer; a second DNS lookup could pick another server.
        data.connect(peer)
        actual_algorithm = congestion_algorithm(data)
        if actual_algorithm != algorithm:
            raise StatsError(f"data socket uses {actual_algorithm!r}; expected {algorithm!r}")
        # Exclude the cookie even if its ACK arrives after payload transmission starts.
        cookie_ack_target = read_tcp_info(data)["bytes_acked"] + len(cookie)
        sampler = GoodputSampler(data, interval, cookie_ack_target)
        send_bytes(data, cookie, time.monotonic() + timeout)
        expect(State.TEST_START)
        expect(State.TEST_RUNNING)

        stage = "upload"
        client_results, elapsed = transmit(control, data, duration, timeout, sampler)
        send_state(State.TEST_END)

        stage = "result exchange"
        # Keep data open until DISPLAY_RESULTS, just as the official client does.
        expect(State.EXCHANGE_RESULTS)
        deadline = time.monotonic() + timeout
        send_json(control, client_results, deadline)
        server_results = recv_json(control, deadline)
        validate_results(server_results)
        expect(State.DISPLAY_RESULTS)

        stage = "test termination"
        data.close()
        data = None
        send_state(State.IPERF_DONE)
        complete = True
        sent = client_results["streams"][0]["bytes"]
        return {
            "status": "success", "host": host, "port": port,
            "peer": list(peer), "started_utc": started_utc,
            "platform": platform.platform(), "duration_seconds": duration,
            "send_seconds": elapsed, "payload_bytes_sent": sent,
            "application_send_rate_bps": 8 * sent / elapsed,
            "client_results": client_results, "server_results": server_results,
            "protocol_events": events,
            **measurements(),
        }
    except (OSError, ClientError, StatsError) as exc:
        raise ClientError(f"{stage}: {exc}", {
            **measurements(), "protocol_events": events,
        }) from exc
    except KeyboardInterrupt as exc:
        exc.details = {**measurements(), "protocol_events": events}
        raise
    finally:
        if control is not None:
            if not complete:
                # Best effort only: never let abort cleanup hang a failed test.
                try:
                    send_bytes(control, struct.pack("!b", State.CLIENT_TERMINATE),
                               time.monotonic() + min(timeout, 0.2))
                except OSError:
                    pass
            control.close()
        if data is not None:
            data.close()
