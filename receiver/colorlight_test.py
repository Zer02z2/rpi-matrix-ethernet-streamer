"""
receiver/colorlight_test.py

Tries several known Colorlight 5A-75B packet header formats in sequence.
Each format sends a different solid color — whichever one lights up the
display tells us which header format is correct.

Run as root:
    sudo python colorlight_test.py [--interface eth1] [--width 192] [--height 192]
"""

import argparse
import select
import socket
import struct
import time

# ---------------------------------------------------------------------------
# Ethernet constants
# ---------------------------------------------------------------------------

DST_MAC    = bytes.fromhex('112233445566')
SRC_MAC    = bytes.fromhex('222233445566')
ETHERTYPE  = bytes([0x01, 0x07])
ETH_HDR    = DST_MAC + SRC_MAC + ETHERTYPE   # 14 bytes

ETH_MIN_PAYLOAD = 46   # minimum payload for a 60-byte Ethernet frame


def pad(payload: bytes) -> bytes:
    """Pad payload to Ethernet minimum if needed."""
    if len(payload) < ETH_MIN_PAYLOAD:
        return payload + b'\x00' * (ETH_MIN_PAYLOAD - len(payload))
    return payload


# ---------------------------------------------------------------------------
# Brightness / config packet — sent once before each frame
# (22-byte payload, brightness at byte 21)
# ---------------------------------------------------------------------------

def brightness_packet(brightness: int = 0xFF) -> bytes:
    payload = bytearray(max(22, ETH_MIN_PAYLOAD))
    payload[21] = brightness
    return ETH_HDR + bytes(payload)


# ---------------------------------------------------------------------------
# Row packet header formats to try
# Each lambda returns a 7-byte header given (row_index, pixel_count)
# ---------------------------------------------------------------------------

FORMATS = {

    "A  cmd=0x05, reserved, row, width": lambda row, w:
        struct.pack('>BBBHH', 0x05, 0x00, 0x00, row, w),

    "B  row, width, zeros": lambda row, w:
        struct.pack('>HH', row, w) + b'\x00\x00\x00',

    "C  row, byte_count, zeros": lambda row, w:
        struct.pack('>HH', row, w * 3) + b'\x00\x00\x00',

    "D  row, zeros, width, zero": lambda row, w:
        struct.pack('>H', row) + b'\x00\x00' + struct.pack('>H', w) + b'\x00',

    "E  zeros, row, zeros": lambda row, w:
        b'\x00\x00' + struct.pack('>H', row) + b'\x00\x00\x00',

    "F  row only, rest zeros": lambda row, w:
        struct.pack('>H', row) + b'\x00\x00\x00\x00\x00',

}

# Solid colors in BGR order: (B, G, R)
COLORS = [
    (0,   0,   255),   # red
    (0,   255, 0),     # green
    (255, 0,   0),     # blue
    (0,   255, 255),   # yellow
    (255, 0,   255),   # magenta
    (255, 255, 0),     # cyan
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def recv_all(sock: socket.socket, timeout: float = 0.4) -> list:
    """Collect any frames the card sends back within timeout seconds."""
    out = []
    deadline = time.monotonic() + timeout
    while True:
        rem = deadline - time.monotonic()
        if rem <= 0:
            break
        r, _, _ = select.select([sock], [], [], rem)
        if not r:
            break
        data = sock.recv(65536)
        if data[6:12] != SRC_MAC:   # ignore our own frames
            out.append(data)
    return out


def send_frame(sock, width, height, header_fn, color_bgr):
    """Send brightness packet + all rows using the given header format."""
    sock.send(brightness_packet(0xFF))

    b, g, r = color_bgr
    row_data = bytes([b, g, r] * width)
    for row in range(height):
        header = header_fn(row, width)
        sock.send(ETH_HDR + header + row_data)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--interface", default="eth1")
    p.add_argument("--width",     type=int, default=192)
    p.add_argument("--height",    type=int, default=192)
    return p.parse_args()


def main():
    args = parse_args()
    w, h = args.width, args.height
    print(f"Interface: {args.interface}  |  {w}x{h}\n")
    print("Each format sends a different color. Watch the display.")
    print("Note which color appears — that tells us which format is correct.\n")

    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
    sock.bind((args.interface, 0))

    for (fmt_name, header_fn), color in zip(FORMATS.items(), COLORS):
        b, g, r = color
        color_name = {
            (0,0,255):   "RED",
            (0,255,0):   "GREEN",
            (255,0,0):   "BLUE",
            (0,255,255): "YELLOW",
            (255,0,255): "MAGENTA",
            (255,255,0): "CYAN",
        }.get(color, str(color))

        print(f"Format {fmt_name}")
        print(f"  Sending solid {color_name} ...")
        send_frame(sock, w, h, header_fn, color)

        responses = recv_all(sock, timeout=0.5)
        if responses:
            for resp in responses:
                hex_str = resp[:32].hex(' ')
                print(f"  Card responded: {hex_str}{'...' if len(resp) > 32 else ''}")
        else:
            print(f"  No response from card.")

        print(f"  >>> Does the display show solid {color_name}? (wait 2 seconds)")
        time.sleep(2)
        print()

    sock.close()
    print("Done. Report which color (if any) appeared on the display.")


if __name__ == "__main__":
    main()
