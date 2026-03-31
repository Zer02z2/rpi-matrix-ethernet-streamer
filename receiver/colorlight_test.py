"""
receiver/colorlight_test.py

Diagnostic tests for the Colorlight 5A-75B.
Sends gradient and stripe patterns to reveal how rows and columns are mapped.

Run as root:
    sudo python colorlight_test.py [--interface eth1] [--width 192] [--height 192]

Ctrl-C moves to the next test.
"""

import argparse
import signal
import socket
import time

DST_MAC = bytes.fromhex('112233445566')
SRC_MAC = bytes.fromhex('222233445566')

BRIGHTNESS_MAP = [
    (0,   0x00), (1,  0x03), (2,  0x05), (4,  0x0a),
    (5,   0x0d), (6,  0x0f), (10, 0x1a), (25, 0x40),
    (50,  0x80), (75, 0xbf), (100, 0xff),
]


def hw_brightness(pct):
    val = 0x00
    for threshold, v in BRIGHTNESS_MAP:
        if pct >= threshold:
            val = v
    return val


def brightness_pkt(pct):
    hw      = hw_brightness(pct)
    payload = bytearray(63)
    payload[0] = hw
    payload[1] = hw
    payload[2] = 0xFF
    return DST_MAC + SRC_MAC + bytes([0x0a, hw]) + bytes(payload)


def row_pkt(row, bgr_row, width):
    header = bytes([row, 0x00, 0x00, width >> 8, width & 0xFF, 0x08, 0x88])
    return DST_MAC + SRC_MAC + bytes([0x55, 0x00]) + header + bgr_row


def latch_pkt(pct):
    payload = bytearray(98)
    payload[21] = pct
    payload[22] = 5
    payload[24] = pct
    payload[25] = pct
    payload[26] = pct
    return DST_MAC + SRC_MAC + bytes([0x01, 0x07]) + bytes(payload)


def send_frame(sock, rows_bgr, width, pct):
    sock.send(brightness_pkt(pct))
    for y, bgr_row in enumerate(rows_bgr):
        sock.send(row_pkt(y, bgr_row, width))
    time.sleep(0.001)
    sock.send(latch_pkt(pct))


# ---------------------------------------------------------------------------
# Frame generators
# ---------------------------------------------------------------------------

def solid_frame(width, height, b, g, r):
    row = bytes([b, g, r] * width)
    return [row] * height


def row_gradient_frame(width, height):
    """Each row has a unique hue based on row index.
    Row 0       = red
    Row h//3    = green
    Row 2*h//3  = blue
    Row h-1     = back to red
    This shows EXACTLY which physical row each index maps to."""
    rows = []
    for y in range(height):
        t = y / height
        if t < 1/3:
            r = int(255 * (1 - 3*t));      g = int(255 * 3*t);        b = 0
        elif t < 2/3:
            r = 0;                          g = int(255 * (2 - 3*t));  b = int(255 * (3*t - 1))
        else:
            r = int(255 * (3*t - 2));      g = 0;                     b = int(255 * (3 - 3*t))
        rows.append(bytes([b, g, r] * width))   # BGR
    return rows


def col_gradient_frame(width, height):
    """Each column has brightness based on x position.
    Left = dark, right = bright (white gradient).
    Shows which physical column each x index maps to."""
    row = bytes([int(255 * x / (width - 1))] * 3 * 1
                for x in range(width)).__class__(
                    b for x in range(width)
                    for v in [int(255 * x / (width - 1))]
                    for _ in [b for _ in range(3)]
                    for b in [v]
                )
    # Simpler version:
    row = bytes(
        v for x in range(width)
        for v in [int(255 * x / (width - 1))] * 3
    )
    return [row] * height


def thirds_frame(width, height):
    """Left third RED, middle third GREEN, right third BLUE.
    Reveals how horizontal pixels are distributed across panels."""
    t1, t2 = width // 3, 2 * (width // 3)
    row = (bytes([0, 0, 255]) * t1 +          # red
           bytes([0, 255, 0]) * (t2 - t1) +   # green
           bytes([255, 0, 0]) * (width - t2))  # blue
    return [row] * height


def bands_frame(width, height):
    """Horizontal bands: every 32 rows alternates RED/GREEN.
    Reveals how the 32-row strips map to row indices."""
    rows = []
    for y in range(height):
        band = (y // 32) % 2
        row = bytes([0, 0, 255] * width) if band == 0 else bytes([0, 255, 0] * width)
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Run loop
# ---------------------------------------------------------------------------

def run_test(sock, name, frame_rows, width, pct, description):
    stop = [False]

    def on_sigint(sig, f):
        stop[0] = True

    signal.signal(signal.SIGINT, on_sigint)
    print(f"\n{'='*60}")
    print(f"TEST: {name}")
    print(f"  {description}")
    print(f"  Ctrl-C to move to next test")
    print(f"{'='*60}")

    count = 0
    t0 = time.monotonic()
    while not stop[0]:
        send_frame(sock, frame_rows, width, pct)
        count += 1
        if count % 30 == 0:
            fps = count / (time.monotonic() - t0)
            print(f"  {count} frames  ({fps:.1f} fps)")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--interface",  default="eth1")
    p.add_argument("--width",      type=int, default=192)
    p.add_argument("--height",     type=int, default=192)
    p.add_argument("--brightness", type=int, default=50)
    return p.parse_args()


def main():
    args = parse_args()
    w, h, pct = args.width, args.height, args.brightness
    print(f"Interface: {args.interface}  |  {w}x{h}  |  brightness={pct}%")

    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
    sock.bind((args.interface, 0))

    tests = [
        ("SOLID RED",
         solid_frame(w, h, 0, 0, 255),
         "Whole display should be solid red. Note which areas light up."),

        ("HORIZONTAL THIRDS  [RED | GREEN | BLUE]",
         thirds_frame(w, h),
         "Left third=RED, middle=GREEN, right=BLUE. "
         "Shows how pixel columns map to physical panels."),

        ("COLUMN GRADIENT  [dark left → bright right]",
         col_gradient_frame(w, h),
         "Brightness increases left to right. "
         "Reveals column mapping and which panels receive data."),

        ("ROW GRADIENT  [red→green→blue top to bottom]",
         row_gradient_frame(w, h),
         "Color changes continuously top to bottom. "
         "Reveals exactly which row indices map to which physical rows."),

        ("32-ROW BANDS  [alternating RED / GREEN every 32 rows]",
         bands_frame(w, h),
         "Alternating red/green stripes every 32 rows. "
         "Shows whether 32-row grouping matches our row addressing."),
    ]

    for name, frame, desc in tests:
        run_test(sock, name, frame, w, pct, desc)

    sock.close()
    print("\nAll tests done.")


if __name__ == "__main__":
    main()
