#!/usr/bin/env python3
"""
Diagnostic poller: connect your laptop to the real FC via USB-to-UART
and verify it's sending the expected MSP data.

Sends MSP_NAV_STATUS requests and decodes the responses. Also sends a
quick MSP_STATUS probe at startup to confirm the link is alive.

Usage:
  python3 fc_poller.py                        # auto-detect port
  python3 fc_poller.py --port /dev/cu.usbserial-1410
  python3 fc_poller.py --interval 0.5         # poll every 500ms
  python3 fc_poller.py --raw                  # also dump raw hex bytes
"""

import argparse
import struct
import sys
import time


MSP_IDENT      = 100
MSP_STATUS     = 101
MSP_NAV_STATUS = 121


# ---------------------------------------------------------------------------
# MSP V2 framing
# ---------------------------------------------------------------------------

def crc8_dvb_s2(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0xD5) if (crc & 0x80) else (crc << 1)
            crc &= 0xFF
    return crc


def build_request(function: int, payload: bytes = b"") -> bytes:
    size = len(payload)
    msg = bytearray(9 + size)
    msg[0] = ord("$")
    msg[1] = ord("X")
    msg[2] = ord("<")
    msg[3] = 0  # flag
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


def read_response(ser):
    """
    Read one MSP V2 response frame.
    Returns (function, payload) or None on error/timeout.
    """
    header = read_exact(ser, 3)
    if len(header) < 3:
        return None
    if header != b"$X>":
        # drain garbage byte by byte until we find the preamble or give up
        return None

    rest = read_exact(ser, 5)
    if len(rest) < 5:
        return None

    flag     = rest[0]
    function = rest[1] | (rest[2] << 8)
    size     = rest[3] | (rest[4] << 8)

    body = read_exact(ser, size + 1)
    if len(body) < size + 1:
        return None

    payload      = body[:size]
    received_crc = body[size]
    crc_data     = bytes([flag, rest[1], rest[2], rest[3], rest[4]]) + payload

    if crc8_dvb_s2(crc_data) != received_crc:
        print(f"  [!] CRC mismatch on function {function} — got 0x{received_crc:02X}, "
              f"expected 0x{crc8_dvb_s2(crc_data):02X}")
        return None

    return function, payload


# ---------------------------------------------------------------------------
# Payload decoders
# ---------------------------------------------------------------------------

def decode_nav_status(payload: bytes, raw: bool) -> None:
    if raw:
        print(f"  raw ({len(payload)}B): {payload.hex(' ')}")
    if len(payload) < 7:
        print(f"  [!] Payload too short: {len(payload)} bytes (expected 7)")
        return

    # INAV format: uint8 mode, uint8 state, uint8 action, uint8 wp_num,
    #              uint8 error, int16 heading  (7 bytes total)
    nav_mode, nav_state, wp_action, wp_num, nav_error, heading_hold = struct.unpack(
        "<BBBBBh", payload[:7]
    )

    complete = (nav_mode == 2 and nav_state == 10)

    print(f"  nav_mode    = {nav_mode}"
          + (" (RTH/land)" if nav_mode == 2 else " (normal)" if nav_mode == 0 else ""))
    print(f"  nav_state   = {nav_state}"
          + (" (landed/idle)" if nav_state == 10 else " (in-flight)" if nav_state != 0 else " (idle)"))
    print(f"  active_wp   = {wp_num}")
    print(f"  wp_action   = 0x{wp_action:02X}"
          + (" (Fly here)" if wp_action == 0x01 else ""))
    print(f"  nav_error   = {nav_error}")
    print(f"  heading_tgt = {heading_hold}")
    if complete:
        print("  ** MISSION COMPLETE **")


def decode_status(payload: bytes, raw: bool) -> None:
    if raw:
        print(f"  raw ({len(payload)}B): {payload.hex(' ')}")
    if len(payload) < 11:
        print(f"  [!] Status payload short: {len(payload)} bytes")
        return
    cycle_time, i2c_errors, sensors, flight_mode, profile = struct.unpack(
        "<HHIHB", payload[:11]
    )
    print(f"  cycle_time  = {cycle_time} µs")
    print(f"  i2c_errors  = {i2c_errors}")
    print(f"  sensors     = 0x{sensors:08X}")
    print(f"  flight_mode = 0x{flight_mode:08X}")
    print(f"  profile     = {profile}")


# ---------------------------------------------------------------------------
# Port detection
# ---------------------------------------------------------------------------

def find_port() -> str | None:
    from serial.tools import list_ports
    candidates = [
        p for p in list_ports.comports()
        if any(p.device.startswith(pfx) for pfx in (
            "/dev/cu.usbserial",
            "/dev/cu.usbmodem",
            "/dev/ttyUSB",
            "/dev/ttyACM",
        ))
    ]
    if not candidates:
        print("[poller] No USB serial ports found. Connect adapter or use --port.")
        return None
    if len(candidates) == 1:
        print(f"[poller] Auto-detected: {candidates[0].device}  ({candidates[0].description})")
        return candidates[0].device
    print("[poller] Multiple ports found — use --port to specify one:")
    for p in candidates:
        print(f"    {p.device}  ({p.description})")
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Poll real FC over UART and decode MSP responses")
    parser.add_argument("--port",     default=None,  help="Serial port (auto-detected if omitted)")
    parser.add_argument("--baud",     type=int, default=115200)
    parser.add_argument("--interval", type=float, default=0.2, help="Poll interval in seconds (default 0.2 = 5 Hz)")
    parser.add_argument("--raw",      action="store_true", help="Also print raw hex payload bytes")
    parser.add_argument("--count",    type=int, default=0, help="Stop after N polls (0 = run forever)")
    args = parser.parse_args()

    port = args.port or find_port()
    if port is None:
        sys.exit(1)

    import serial
    ser = serial.Serial(port, args.baud, timeout=1)
    print(f"[poller] Opened {port} @ {args.baud}bps  (Ctrl-C to stop)\n")

    # --- connectivity check: MSP_STATUS ---
    print("── MSP_STATUS probe ──────────────────────────────────")
    ser.write(build_request(MSP_STATUS))
    resp = read_response(ser)
    if resp is None:
        print("  [!] No response to MSP_STATUS — check wiring and baud rate")
    else:
        fn, payload = resp
        if fn == MSP_STATUS:
            decode_status(payload, args.raw)
        else:
            print(f"  [!] Expected function {MSP_STATUS}, got {fn}")
    print()

    # --- continuous MSP_NAV_STATUS polling ---
    print("── MSP_NAV_STATUS polling ────────────────────────────")
    poll_num = 0
    try:
        while True:
            poll_num += 1
            ts = time.strftime("%H:%M:%S")
            print(f"[{ts}] poll #{poll_num}")

            ser.write(build_request(MSP_NAV_STATUS))
            resp = read_response(ser)

            if resp is None:
                print("  [!] No response (timeout or framing error)")
            else:
                fn, payload = resp
                if fn == MSP_NAV_STATUS:
                    decode_nav_status(payload, args.raw)
                else:
                    print(f"  [!] Expected {MSP_NAV_STATUS}, got function {fn}")
                    if args.raw:
                        print(f"  raw: {payload.hex(' ')}")

            if args.count and poll_num >= args.count:
                break

            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()
        print(f"\n[poller] Done. {poll_num} poll(s).")


if __name__ == "__main__":
    main()
