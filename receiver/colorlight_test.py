"""
receiver/colorlight_test.py

Sends solid color frames in a continuous loop to the Colorlight 5A-75B.
A single frame is often ignored — the card needs a sustained stream.

Run as root:
    sudo python colorlight_test.py [--interface eth1] [--width 192] [--height 192]

Ctrl-C to stop and move to the next color.
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
    eth     = DST_MAC + SRC_MAC + bytes([0x0a, hw])
    payload = bytearray(63)
    payload[0] = hw
    payload[1] = hw
    payload[2] = 0xFF
    return eth + bytes(payload)


def row_pkt(row, bgr_row, width):
    eth    = DST_MAC + SRC_MAC + bytes([0x55, 0x00])
    header = bytes([row, 0x00, 0x00, width >> 8, width & 0xFF, 0x08, 0x88])
    return eth + header + bgr_row


def latch_pkt(pct):
    eth     = DST_MAC + SRC_MAC + bytes([0x01, 0x07])
    payload = bytearray(98)
    payload[21] = pct
    payload[22] = 5
    payload[24] = pct
    payload[25] = pct
    payload[26] = pct
    return eth + bytes(payload)


def send_frame(sock, row_pkts, brt_pkt, lat_pkt):
    sock.send(brt_pkt)
    for pkt in row_pkts:
        sock.send(pkt)
    time.sleep(0.001)
    sock.send(lat_pkt)


def build_solid(width, height, pct, color_bgr):
    b, g, r = color_bgr
    row_data = bytes([b, g, r] * width)
    row_pkts = [row_pkt(y, row_data, width) for y in range(height)]
    return row_pkts, brightness_pkt(pct), latch_pkt(pct)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--interface",  default="eth1")
    p.add_argument("--width",      type=int, default=192)
    p.add_argument("--height",     type=int, default=192)
    p.add_argument("--brightness", type=int, default=50,
                   help="Brightness 0-100 (default: 50)")
    return p.parse_args()


def main():
    args = parse_args()
    w, h, pct = args.width, args.height, args.brightness

    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
    sock.bind((args.interface, 0))

    colors = [
        ("RED",   (0,   0,   255)),
        ("GREEN", (0,   255, 0  )),
        ("BLUE",  (255, 0,   0  )),
        ("WHITE", (255, 255, 255)),
    ]

    stop = [False]

    def on_sigint(sig, frame):
        stop[0] = True

    for name, color in colors:
        stop[0] = False
        signal.signal(signal.SIGINT, on_sigint)

        row_pkts, brt_pkt, lat_pkt = build_solid(w, h, pct, color)

        print(f"Sending solid {name} continuously — press Ctrl-C to move to next color")
        count = 0
        t0 = time.monotonic()
        while not stop[0]:
            send_frame(sock, row_pkts, brt_pkt, lat_pkt)
            count += 1
            if count % 30 == 0:
                elapsed = time.monotonic() - t0
                print(f"  {count} frames sent ({count/elapsed:.1f} fps)")

        print()

    sock.close()
    print("Done.")


if __name__ == "__main__":
    main()
