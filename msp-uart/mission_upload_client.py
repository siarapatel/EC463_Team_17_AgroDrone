#!/usr/bin/env python3
"""
AgroDrone Mission Upload Client
--------------------------------
Connects to the msp_uart_service control socket, sends an upload_request,
and waits for the upload_response. Triggered by systemd .path unit when
waypoints.json changes.

The control socket is exclusively for request/response commands; it does not
receive nav events, so the first response line is always the upload reply.

Exit codes: 0 = success, 1 = failure
"""

import json
import os
import socket
import sys
import time

CONTROL_SOCKET_PATH = os.environ.get("MSP_CONTROL_SOCKET_PATH",
                                      "/run/agrodrone/msp-control.sock")
TIMEOUT_S = 120  # generous timeout — large missions can take ~30 s to upload
CONNECT_RETRIES = int(os.environ.get("MSP_CONTROL_CONNECT_RETRIES", "5"))
CONNECT_DELAY_S = float(os.environ.get("MSP_CONTROL_CONNECT_DELAY_S", "2"))


def main() -> None:
    sock = None
    for attempt in range(1, CONNECT_RETRIES + 1):
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(TIMEOUT_S)
            sock.connect(CONTROL_SOCKET_PATH)
            break
        except (FileNotFoundError, ConnectionRefusedError) as e:
            if sock is not None:
                sock.close()
                sock = None
            print(
                f"[upload-client] cannot connect to {CONTROL_SOCKET_PATH} "
                f"(attempt {attempt}/{CONNECT_RETRIES}): {e}"
            )
            if attempt < CONNECT_RETRIES:
                time.sleep(CONNECT_DELAY_S)

    if sock is None:
        print(f"[upload-client] ERROR: cannot connect to {CONTROL_SOCKET_PATH}")
        sys.exit(1)

    try:
        sock.sendall((json.dumps({"type": "upload_request"}) + "\n").encode())

        line = sock.makefile("r").readline()
        if not line:
            print("[upload-client] ERROR: connection closed before response")
            sys.exit(1)

        response = json.loads(line)
        if response.get("success"):
            print("[upload-client] Mission upload succeeded.")
            sys.exit(0)
        else:
            print(f"[upload-client] Mission upload FAILED: {response.get('error')}")
            sys.exit(1)

    except socket.timeout:
        print("[upload-client] ERROR: timed out waiting for upload response")
        sys.exit(1)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[upload-client] ERROR: malformed response: {e}")
        sys.exit(1)
    finally:
        sock.close()


if __name__ == "__main__":
    main()
