#!/usr/bin/env python3
"""
Fake FC UART responder for bench-testing AgroDrone's MSP UART service.

Run this on a second Raspberry Pi wired to the main Pi over 3.3V UART:

  Main Pi TX -> Fake Pi RX
  Main Pi RX -> Fake Pi TX
  GND        -> GND

The main Pi's `msp_uart_service.py` will poll MSP_NAV_STATUS. This script
responds with a scripted sequence of NAV payloads so you can validate the Pi
side without the real flight controller.

It also accepts MSP_SET_WP and MSP_SAVE_NVRAM writes so mission upload tests
do not crash immediately, though upload ACK behavior is intentionally minimal.

Examples:

  python3 fake_fc_uart.py --port /dev/serial0 --scenario nominal
  python3 fake_fc_uart.py --port /dev/ttyAMA0 --scenario skip
  python3 fake_fc_uart.py --port /dev/serial0 --scenario custom --waypoints 1,2,3,4
"""

import argparse
import struct
import time


MSP_NAV_STATUS = 121
MSP_SET_WP = 209
MSP_SAVE_NVRAM = 19

WAYPOINT_ACTION = 0x01


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


def nav_payload(nav_mode: int, nav_state: int, wp_num: int, wp_action: int) -> bytes:
    # INAV format: uint8 mode, uint8 state, uint8 action, uint8 wp_num,
    #              uint8 error, int16 heading  (7 bytes total)
    return struct.pack(
        "<BBBBBh",
        nav_mode,
        nav_state,
        wp_action,
        wp_num,
        0,   # nav_error
        0,   # nav_headingHoldTarget
    )


def build_sequence(name: str, custom_waypoints: list[int]) -> list[bytes]:
    # nav_state=0  → armed=False (pre-arm idle or post-disarm)
    # nav_state=6  → armed=True  (NAV_STATE_AUTO_WP: actively navigating)
    # nav_state=10 → armed=True  (mission complete, still flying / RTH)
    # Sequences must start disarmed and end with a disarm state so the
    # capture service sees the nav_state 0→>0 arm transition and the
    # >0→0 disarm transition that triggers image offload.

    if name == "nominal":
        return [
            nav_payload(0, 0, 0, 0),               # disarmed (pre-arm)
            nav_payload(2, 6, 1, WAYPOINT_ACTION),  # armed, WP 1
            nav_payload(2, 6, 2, WAYPOINT_ACTION),  # armed, WP 2
            nav_payload(2, 6, 3, WAYPOINT_ACTION),  # armed, WP 3
            nav_payload(2, 10, 3, WAYPOINT_ACTION), # mission complete (still armed)
            nav_payload(0, 0, 0, 0),               # disarmed → triggers offload
        ]

    if name == "duplicate":
        return [
            nav_payload(0, 0, 0, 0),               # disarmed (pre-arm)
            nav_payload(2, 6, 1, WAYPOINT_ACTION),  # armed, WP 1
            nav_payload(2, 6, 1, WAYPOINT_ACTION),  # duplicate WP 1 (no capture)
            nav_payload(2, 6, 2, WAYPOINT_ACTION),  # armed, WP 2
            nav_payload(2, 10, 2, WAYPOINT_ACTION), # mission complete
            nav_payload(0, 0, 0, 0),               # disarmed → triggers offload
        ]

    if name == "skip":
        return [
            nav_payload(0, 0, 0, 0),               # disarmed (pre-arm)
            nav_payload(2, 6, 1, WAYPOINT_ACTION),  # armed, WP 1
            nav_payload(2, 6, 3, WAYPOINT_ACTION),  # WP 3 — skipped WP 2, expect _MissionSyncError
            nav_payload(2, 10, 3, WAYPOINT_ACTION), # mission complete
            nav_payload(0, 0, 0, 0),               # disarmed
        ]

    if name == "backward":
        return [
            nav_payload(0, 0, 0, 0),               # disarmed (pre-arm)
            nav_payload(2, 6, 1, WAYPOINT_ACTION),  # armed, WP 1
            nav_payload(2, 6, 2, WAYPOINT_ACTION),  # WP 2
            nav_payload(2, 6, 1, WAYPOINT_ACTION),  # WP 1 — backward, expect _MissionSyncError
            nav_payload(2, 10, 1, WAYPOINT_ACTION), # mission complete
            nav_payload(0, 0, 0, 0),               # disarmed
        ]

    if name == "custom":
        sequence = [nav_payload(0, 0, 0, 0)]
        sequence += [nav_payload(2, 6, wp, WAYPOINT_ACTION) for wp in custom_waypoints]
        if custom_waypoints:
            sequence.append(nav_payload(2, 10, custom_waypoints[-1], WAYPOINT_ACTION))
        sequence.append(nav_payload(0, 0, 0, 0))
        return sequence

    raise ValueError(f"Unknown scenario: {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake MSP flight controller over UART")
    parser.add_argument("--port", default="/dev/serial0", help="UART port to open")
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
        help="How many MSP_NAV_STATUS requests each state is repeated for",
    )
    args = parser.parse_args()

    custom_waypoints = [int(x) for x in args.waypoints.split(",") if x.strip()]
    sequence = build_sequence(args.scenario, custom_waypoints)
    sequence_index = 0
    polls_at_current_state = 0

    import serial

    ser = serial.Serial(args.port, args.baud, timeout=1)
    print(
        f"[fake-fc] Listening on {args.port} @ {args.baud} | "
        f"scenario={args.scenario} | hold_polls={args.hold_polls}"
    )

    try:
        while True:
            request = read_msp_v2_request(ser)
            if request is None:
                continue

            function, payload = request

            if function == MSP_NAV_STATUS:
                current_payload = sequence[sequence_index]
                ser.write(build_msp_v2_response(MSP_NAV_STATUS, current_payload))

                nav_mode, nav_state, wp_action, wp_num, _, _ = struct.unpack(
                    "<BBBBBh", current_payload
                )
                print(
                    f"[fake-fc] NAV reply | mode={nav_mode} state={nav_state} "
                    f"wp={wp_num} action={wp_action}"
                )

                polls_at_current_state += 1
                if polls_at_current_state >= args.hold_polls and sequence_index < len(sequence) - 1:
                    sequence_index += 1
                    polls_at_current_state = 0

            elif function == MSP_SET_WP:
                print(f"[fake-fc] Received MSP_SET_WP payload ({len(payload)} bytes)")
                ser.write(build_msp_v2_response(MSP_SET_WP, b""))

            elif function == MSP_SAVE_NVRAM:
                print("[fake-fc] Received MSP_SAVE_NVRAM")
                ser.write(build_msp_v2_response(MSP_SAVE_NVRAM, b""))

            else:
                print(f"[fake-fc] Ignoring unsupported function {function}")

            time.sleep(0.01)
    finally:
        ser.close()


if __name__ == "__main__":
    main()
