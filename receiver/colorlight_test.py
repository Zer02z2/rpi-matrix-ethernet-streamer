"""
receiver/colorlight_test.py

Standalone protocol test for the Colorlight 5A-75B.
First restores the red channel damaged by the previous incorrect test,
then sends solid color frames to verify the correct protocol works.

Run as root:
    sudo python colorlight_test.py [--interface eth1] [--width 192] [--height 192]
"""

import argparse
import socket
import time

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DST_MAC = bytes.fromhex('112233445566')
SRC_MAC = bytes.fromhex('222233445566')

BRIGHTNESS_MAP = [
    (0,   0x00), (1,  0x03), (2,  0x05), (4,  0x0a),
    (5,   0x0d), (6,  0x0f), (10, 0x1a), (25, 0x40),
    (50,  0x80), (75, 0xbf), (100, 0xff),
]


def hw_brightness(pct: int) -> int:
    val = 0x00
    for threshold, v in BRIGHTNESS_MAP:
        if pct >= threshold:
            val = v
    return val


def brightness_packet(pct: int) -> bytes:
    hw      = hw_brightness(pct)
    eth     = DST_MAC + SRC_MAC + bytes([0x0a, hw])
    payload = bytearray(63)
    payload[0] = hw
    payload[1] = hw
    payload[2] = 0xFF
    return eth + bytes(payload)


def row_packet(row: int, bgr_row: bytes, width: int) -> bytes:
    eth    = DST_MAC + SRC_MAC + bytes([0x55, 0x00])
    header = bytes([row, 0x00, 0x00, width >> 8, width & 0xFF, 0x08, 0x88])
    return eth + header + bgr_row


def latch_packet(pct: int) -> bytes:
    eth     = DST_MAC + SRC_MAC + bytes([0x01, 0x07])
    payload = bytearray(98)
    payload[21] = pct
    payload[22] = 5
    payload[24] = pct
    payload[25] = pct
    payload[26] = pct
    return eth + bytes(payload)


def send_solid(sock, width: int, height: int, pct: int, color_bgr: tuple):
    b, g, r = color_bgr
    row_data = bytes([b, g, r] * width)

    sock.send(brightness_packet(pct))
    for row in range(height):
        sock.send(row_packet(row, row_data, width))
    time.sleep(0.001)
    sock.send(latch_packet(pct))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--interface",  default="eth1")
    p.add_argument("--width",      type=int, default=192)
    p.add_argument("--height",     type=int, default=192)
    p.add_argument("--brightness", type=int, default=50,
                   help="Brightness percent 0-100 (default: 50)")
    return p.parse_args()


def main():
    args = parse_args()
    w, h, pct = args.width, args.height, args.brightness
    print(f"Interface: {args.interface}  |  {w}x{h}  |  brightness={pct}%\n")

    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
    sock.bind((args.interface, 0))

    # --- Step 1: Restore red channel ---
    # The previous test sent a malformed latch packet that zeroed out
    # color calibration. Send a correct latch packet to fix it.
    print("Step 1: Restoring color calibration (fixing red channel)...")
    correct_latch = latch_packet(pct)
    sock.send(correct_latch)
    time.sleep(0.1)
    print("  Done. Red should be restored now.\n")

    # --- Step 2: Solid color tests ---
    tests = [
        ("RED",     (0,   0,   255)),
        ("GREEN",   (0,   255, 0  )),
        ("BLUE",    (255, 0,   0  )),
        ("WHITE",   (255, 255, 255)),
        ("BLACK",   (0,   0,   0  )),
    ]

    for name, color in tests:
        print(f"Step 2: Sending solid {name} ...")
        send_solid(sock, w, h, pct, color)
        print(f"  >>> Does the display show solid {name}?")
        time.sleep(2)

    sock.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
