# UART Testing & Debugging Log
**Date:** 2026-04-16  
**Goal:** Verify end-to-end UART communication between laptop, Pi, and real flight controller (SpeedyBee F405V4 running INAV)

---

## Hardware Setup

```
Topology A (laptop → Pi, Pi → FC):
  Laptop USB-to-UART ──── Pi /dev/serial0 ──── FC UART
  (used by msp_uart_service in production)

Topology B (laptop → FC directly, for bench testing):
  Laptop USB-to-UART ──── FC UART
  /dev/cu.usbserial-BG044Z99
```

Both connections were active during this session:
- `/dev/cu.usbmodem0x80000001` — FC native USB (SpeedyBee direct USB)
- `/dev/cu.usbserial-BG044Z99` — USB-to-UART adapter wired to FC UART pins

---

## Scripts Created

### `fake_fc/fake_fc_laptop.py`
Laptop-side FC emulator — the inverse of the existing `fake_fc/fake_fc_uart.py`.  
Responds to MSP_NAV_STATUS polls with a scripted sequence of nav payloads so the Pi service can be tested without a real FC.

Key additions over the Pi version:
- Auto-detects macOS USB serial ports (`/dev/cu.usbserial-*`, `/dev/cu.usbmodem-*`)
- `--interactive` mode: manually step through scenario states with keyboard commands (`Enter`/`n`, `r`, `s`, `q`)
- Decodes and pretty-prints `MSP_SET_WP` payloads (lat/lon/alt) during mission uploads

### `fake_fc/fc_poller.py`
Diagnostic poller that acts as the Pi — sends MSP requests to the real FC and decodes the responses.

Features:
- Auto-detects USB serial ports
- `MSP_STATUS` probe at startup (connectivity check)
- Continuous `MSP_NAV_STATUS` polling (default 5 Hz, matches Pi service)
- `--raw` flag to dump hex bytes alongside decoded fields
- `--count N` to stop after N polls
- `--interval` to adjust poll rate

---

## Bug Found and Fixed: Wrong MSP_NAV_STATUS Payload Format

### Symptom
Running `fc_poller.py` against the real FC (via USB) returned:
```
[!] Payload too short: 7 bytes (expected 12)
```

### Root Cause
The codebase assumed `MSP_NAV_STATUS` (function 121) returns a 12-byte payload packed as six signed int16s:
```python
struct.unpack("<hhhhhh", payload[:12])  # WRONG
```

The actual INAV wire format is **7 bytes**: five uint8 fields followed by one int16:
```
uint8  nav_mode
uint8  nav_state
uint8  nav_activeWpAction   ← note: action comes BEFORE number
uint8  nav_activeWpNumber
uint8  nav_error
int16  nav_headingHoldTarget
```
```python
struct.unpack("<BBBBBh", payload[:7])  # CORRECT
```

### Why It Was Hidden
The fake FC scripts and the service parser were *consistently* using the wrong format with each other, so bench tests with the fake FC passed. Connecting to the real FC exposed the mismatch.

### Files Fixed
| File | Change |
|---|---|
| `msp-uart/msp_uart_service.py` | `_parse_nav_status()`: 12→7 bytes, `<hhhhhh`→`<BBBBBh`, fixed field order |
| `fake_fc/fc_poller.py` | `decode_nav_status()`: same format fix |
| `fake_fc/fake_fc_uart.py` | `nav_payload()`: now builds correct 7-byte payload |
| `fake_fc/fake_fc_laptop.py` | `nav_payload()`, `describe_state()`, internal unpacks |

---

## Debugging the UART Path (Topology B)

### Step 1 — Direct USB (ground truth)
Ran `fc_poller.py` without specifying a port. Auto-detected `/dev/cu.usbmodem0x80000001` (SpeedyBee native USB).

Result: clean decoded output after format fix — confirmed FC is alive and decode logic is correct.

### Step 2 — UART adapter, first attempt
```
python3 fc_poller.py --port /dev/cu.usbserial-BG044Z99
```
Result: no response on every poll.

**Ruled out:**
- Wrong port — confirmed this was the UART adapter
- Service conflict — `msp_uart_service` was not running on the Pi

**Candidates:**
1. MSP not enabled on the FC's UART port in INAV Configurator
2. TX/RX wires swapped

Used `--raw` flag to check if *anything* was coming back. No bytes at all → pointed to either MSP disabled or swapped wires (FC completely silent).

### Step 3 — Swapped TX/RX
Swapped the TX and RX wires on the adapter.

Result: immediate clean responses.

```
── MSP_NAV_STATUS polling ────────────────────────────
[12:40:53] poll #1
  nav_mode    = 0 (normal)
  nav_state   = 0 (idle)
  active_wp   = 1
  wp_action   = 0x01 (Fly here)
  nav_error   = 0
  heading_tgt = 109
```

---

## Final Verified State

| Check | Result |
|---|---|
| FC responds to MSP_STATUS | ✅ cycle_time=522µs, no i2c errors |
| FC responds to MSP_NAV_STATUS | ✅ clean 7-byte payload |
| Payload decodes correctly | ✅ all fields match expected idle state |
| UART path (laptop → FC) working | ✅ confirmed after TX/RX swap |
| MSP format fixed in all files | ✅ |

### Idle bench state (expected)
- `nav_mode = 0` — not in autonomous flight
- `nav_state = 0` — no mission executing
- `active_wp = 1, wp_action = 0x01` — WP 1 stored from a previous upload, not active
- `nav_error = 0` — no faults
- `heading_tgt = 109°` — current compass heading

---

## Next Steps
- Wire USB-to-UART adapter to Pi `/dev/serial0` (Topology A)
- Start `msp_uart_service` on the Pi
- Confirm events socket emits correct `mission_status` JSON matching what `fc_poller` reads directly
