#!/usr/bin/env python3
"""
Laptop-side fake FC for bench-testing AgroDrone's MSP UART service.

Run this on your laptop connected to the main Pi over a USB-to-UART adapter:

  Laptop TX (adapter) -> Pi RX
  Laptop RX (adapter) -> Pi TX
  GND                 -> GND

The Pi's `msp_uart_service.py` will poll MSP_NAV_STATUS. This script
responds with a scripted sequence of NAV payloads so you can validate the
Pi side without a real flight controller.

If --port is not provided the script auto-detects USB serial adapters.

Examples:

  python3 fake_fc_laptop.py --scenario nominal
  python3 fake_fc_laptop.py --port /dev/cu.usbserial-1410 --scenario skip
  python3 fake_fc_laptop.py --scenario custom --waypoints 1,2,3,4
  python3 fake_fc_laptop.py --interactive
"""

import argparse
import struct
import sys
import threading
import time


MSP_NAV_STATUS = 121
MSP_SET_WP = 209
MSP_SAVE_NVRAM = 19

WAYPOINT_ACTION = 0x01


# ---------------------------------------------------------------------------
# MSP V2 helpers (protocol identical to fake_fc_uart.py)
# ---------------------------------------------------------------------------

def crc8_dvb_s2(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0xD5) if (crc & 0x80) else (crc << 1)
            crc &= 0xFF
    return crc


def build_msp_v2_response(function: int, payload: bytes = b"") -> bytes:
    flag = 0
    size = len(payload)
    msg = bytearray(9 + size)
    msg[0] = ord("$")
    msg[1] = ord("X")
    msg[2] = ord(">")
    msg[3] = flag
    msg[4] = function & 0xFF
    msg[5] = (function >> 8) & 0xFF
    msg[6] = size & 0xFF
    msg[7] = (size >> 8) & 0xFF
    if payload:
        msg[8:8 + size] = payload
    msg[-1] = crc8_dvb_s2(msg[3:-1])
    return bytes(msg)


def read_exact(ser, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = ser.read(n - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def read_msp_v2_request(ser):
    header = read_exact(ser, 3)
    if len(header) < 3 or header != b"$X<":
        return None

    rest = read_exact(ser, 5)
    if len(rest) < 5:
        return None

    flag = rest[0]
    function = rest[1] | (rest[2] << 8)
    payload_size = rest[3] | (rest[4] << 8)

    body = read_exact(ser, payload_size + 1)
    if len(body) < payload_size + 1:
        return None

    payload = body[:payload_size]
    received_crc = body[payload_size]
    crc_data = bytes([flag, rest[1], rest[2], rest[3], rest[4]]) + payload
    if crc8_dvb_s2(crc_data) != received_crc:
        return None

    return function, payload


# ---------------------------------------------------------------------------
# NAV payload / scenario construction
# ---------------------------------------------------------------------------

def nav_payload(nav_mode: int, nav_state: int, wp_num: int, wp_action: int) -> bytes:
    # INAV format: uint8 mode, uint8 state, uint8 action, uint8 wp_num,
    #              uint8 error, int16 heading  (7 bytes total)
    return struct.pack(
        "<BBBBBh",
        nav_mode,
        nav_state,
        wp_action,
        wp_num,
        0,  # nav_error
        0,  # nav_headingHoldTarget
    )


def build_sequence(name: str, custom_waypoints: list[int]) -> list[bytes]:
    if name == "nominal":
        return [
            nav_payload(0, 0, 0, 0),
            nav_payload(0, 0, 1, WAYPOINT_ACTION),
            nav_payload(0, 0, 2, WAYPOINT_ACTION),
            nav_payload(0, 0, 3, WAYPOINT_ACTION),
            nav_payload(2, 10, 3, WAYPOINT_ACTION),
        ]

    if name == "duplicate":
        return [
            nav_payload(0, 0, 1, WAYPOINT_ACTION),
            nav_payload(0, 0, 1, WAYPOINT_ACTION),
            nav_payload(0, 0, 2, WAYPOINT_ACTION),
            nav_payload(2, 10, 2, WAYPOINT_ACTION),
        ]

    if name == "skip":
        return [
            nav_payload(0, 0, 1, WAYPOINT_ACTION),
            nav_payload(0, 0, 3, WAYPOINT_ACTION),
            nav_payload(2, 10, 3, WAYPOINT_ACTION),
        ]

    if name == "backward":
        return [
            nav_payload(0, 0, 1, WAYPOINT_ACTION),
            nav_payload(0, 0, 2, WAYPOINT_ACTION),
            nav_payload(0, 0, 1, WAYPOINT_ACTION),
            nav_payload(2, 10, 1, WAYPOINT_ACTION),
        ]

    if name == "custom":
        sequence = [nav_payload(0, 0, wp, WAYPOINT_ACTION) for wp in custom_waypoints]
        if custom_waypoints:
            sequence.append(nav_payload(2, 10, custom_waypoints[-1], WAYPOINT_ACTION))
        return sequence

    raise ValueError(f"Unknown scenario: {name}")


def describe_state(payload: bytes, index: int, total: int) -> str:
    nav_mode, nav_state, wp_action, wp_num, _, _ = struct.unpack("<BBBBBh", payload)
    complete = nav_mode == 2 and nav_state == 10
    status = "COMPLETE" if complete else f"wp={wp_num}"
    return f"state {index + 1}/{total} | mode={nav_mode} state={nav_state} {status}"


# ---------------------------------------------------------------------------
# WP payload decoder
# ---------------------------------------------------------------------------

def decode_set_wp(payload: bytes) -> str:
    if len(payload) < 18:
        return f"(payload too short: {len(payload)} bytes)"
    index, action, lat_int, lon_int, alt_cm, p1, p2, p3, flag = struct.unpack(
        "<BBiiihhhB", payload[:19]
    )
    lat = lat_int / 1e7
    lon = lon_int / 1e7
    alt_m = alt_cm / 100.0
    return (
        f"index={index} action=0x{action:02X} "
        f"lat={lat:.7f} lon={lon:.7f} alt={alt_m:.1f}m "
        f"p1={p1} p2={p2} p3={p3} flag=0x{flag:02X}"
    )


# ---------------------------------------------------------------------------
# Port auto-detection
# ---------------------------------------------------------------------------

def find_usb_serial_port() -> str | None:
    from serial.tools import list_ports

    candidates = [
        p for p in list_ports.comports()
        if any(p.device.startswith(prefix) for prefix in (
            "/dev/cu.usbserial",
            "/dev/cu.usbmodem",
            "/dev/ttyUSB",
            "/dev/ttyACM",
        ))
    ]

    if not candidates:
        print("[fake-fc] No USB serial ports found.")
        print("         Connect your USB-to-UART adapter and try again,")
        print("         or specify --port explicitly.")
        return None

    if len(candidates) == 1:
        port = candidates[0].device
        print(f"[fake-fc] Auto-detected port: {port} ({candidates[0].description})")
        return port

    print("[fake-fc] Multiple USB serial ports found — specify one with --port:")
    for p in candidates:
        print(f"    {p.device}  ({p.description})")
    return None


# ---------------------------------------------------------------------------
# Serial responder loop (runs in its own thread for interactive mode)
# ---------------------------------------------------------------------------

class Responder:
    def __init__(self, ser, sequence: list[bytes], hold_polls: int, interactive: bool):
        self._ser = ser
        self._sequence = sequence
        self._hold_polls = hold_polls
        self._interactive = interactive
        self._lock = threading.Lock()
        self._index = 0
        self._polls_at_state = 0
        self._stop = threading.Event()

    # Public API for interactive commands
    def advance(self):
        with self._lock:
            if self._index < len(self._sequence) - 1:
                self._index += 1
                self._polls_at_state = 0
                print(f"[fake-fc] >> advanced to {describe_state(self._sequence[self._index], self._index, len(self._sequence))}")
            else:
                print("[fake-fc] >> already at last state")

    def restart(self):
        with self._lock:
            self._index = 0
            self._polls_at_state = 0
            print(f"[fake-fc] >> restarted to {describe_state(self._sequence[0], 0, len(self._sequence))}")

    def status(self):
        with self._lock:
            print(f"[fake-fc] >> current: {describe_state(self._sequence[self._index], self._index, len(self._sequence))}")

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            request = read_msp_v2_request(self._ser)
            if request is None:
                continue

            function, payload = request

            if function == MSP_NAV_STATUS:
                with self._lock:
                    current_payload = self._sequence[self._index]
                    nav_mode, nav_state, wp_action, wp_num, _, _ = struct.unpack(
                        "<BBBBBh", current_payload
                    )
                    print(
                        f"[fake-fc] NAV reply | mode={nav_mode} state={nav_state} "
                        f"wp={wp_num} action={wp_action}"
                    )
                    self._ser.write(build_msp_v2_response(MSP_NAV_STATUS, current_payload))

                    if not self._interactive:
                        self._polls_at_state += 1
                        if (
                            self._polls_at_state >= self._hold_polls
                            and self._index < len(self._sequence) - 1
                        ):
                            self._index += 1
                            self._polls_at_state = 0

            elif function == MSP_SET_WP:
                info = decode_set_wp(payload)
                print(f"[fake-fc] WP upload  | {info}")
                self._ser.write(build_msp_v2_response(MSP_SET_WP, b""))

            elif function == MSP_SAVE_NVRAM:
                print("[fake-fc] SAVE_NVRAM | waypoints committed")
                self._ser.write(build_msp_v2_response(MSP_SAVE_NVRAM, b""))

            else:
                print(f"[fake-fc] Unknown function {function}, ignoring")

            time.sleep(0.01)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Laptop-side fake MSP flight controller over UART"
    )
    parser.add_argument(
        "--port",
        default=None,
        help="Serial port (auto-detected if omitted)",
    )
    parser.add_argument("--baud", type=int, default=115200, help="UART baud rate")
    parser.add_argument(
        "--scenario",
        default="nominal",
        choices=["nominal", "duplicate", "skip", "backward", "custom"],
        help="Predefined nav-status scenario to replay",
    )
    parser.add_argument(
        "--waypoints",
        default="1,2,3",
        help="Comma-separated waypoint list for --scenario custom",
    )
    parser.add_argument(
        "--hold-polls",
        type=int,
        default=5,
        help="Polls before auto-advancing to next state (ignored with --interactive)",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Manually step through states with keyboard commands",
    )
    args = parser.parse_args()

    port = args.port or find_usb_serial_port()
    if port is None:
        sys.exit(1)

    custom_waypoints = [int(x) for x in args.waypoints.split(",") if x.strip()]
    sequence = build_sequence(args.scenario, custom_waypoints)

    import serial

    ser = serial.Serial(port, args.baud, timeout=1)
    print(
        f"[fake-fc] Opened {port} @ {args.baud}bps | "
        f"scenario={args.scenario} | states={len(sequence)}"
    )
    if args.interactive:
        print("[fake-fc] Interactive mode — commands:")
        print("          Enter / n  advance to next state")
        print("          r          restart sequence")
        print("          s          print current state")
        print("          q          quit")
    else:
        print(f"[fake-fc] Auto-advance after {args.hold_polls} polls per state")

    responder = Responder(ser, sequence, args.hold_polls, args.interactive)
    thread = threading.Thread(target=responder.run, daemon=True)
    thread.start()

    try:
        if args.interactive:
            while True:
                try:
                    cmd = input().strip().lower()
                except EOFError:
                    break
                if cmd in ("", "n"):
                    responder.advance()
                elif cmd == "r":
                    responder.restart()
                elif cmd == "s":
                    responder.status()
                elif cmd == "q":
                    break
                else:
                    print("[fake-fc] Unknown command. Use: Enter/n, r, s, q")
        else:
            thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        responder.stop()
        ser.close()
        print("\n[fake-fc] Closed.")


if __name__ == "__main__":
    main()
