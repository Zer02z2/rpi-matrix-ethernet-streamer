"""
receiver/colorlight_test.py

Minimal standalone test for the Colorlight 5A-75B protocol.
Sends a solid red frame and prints any response the card sends back.

Run as root (raw socket required):
    sudo python colorlight_test.py [--interface eth1] [--width 192] [--height 192]
"""

import argparse
import select
import socket
import struct
import time

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DST_MAC    = bytes.fromhex('112233445566')
SRC_MAC    = bytes.fromhex('222233445566')
ETH_DATA   = bytes([0x01, 0x07])
ETH_DETECT = bytes([0x07, 0x00])

ETH_HDR      = DST_MAC + SRC_MAC + ETH_DATA
ETH_HDR_DET  = DST_MAC + SRC_MAC + ETH_DETECT


# ---------------------------------------------------------------------------
# Packet builders
# ---------------------------------------------------------------------------

def brightness_packet(brightness: int = 0xFF) -> bytes:
    payload = bytearray(22)
    payload[21] = brightness
    return ETH_HDR + bytes(payload)


def row_packet(row: int, bgr_row: bytes, width: int) -> bytes:
    header = struct.pack('>BBBHH',
        0x05,   # command byte
        0x00,
        0x00,
        row,
        width,
    )
    return ETH_HDR + header + bgr_row


def detection_packet() -> bytes:
    return ETH_HDR_DET + bytes(4)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def hex_dump(data: bytes, label: str = "") -> None:
    if label:
        print(f"\n{label} ({len(data)} bytes):")
    for i in range(0, len(data), 16):
        chunk = data[i:i+16]
        hex_part = ' '.join(f'{b:02x}' for b in chunk)
        asc_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        print(f"  {i:04x}  {hex_part:<48}  {asc_part}")


def recv_responses(sock: socket.socket, timeout: float = 0.3) -> list:
    """Collect all frames the card sends back within timeout seconds."""
    responses = []
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        ready, _, _ = select.select([sock], [], [], remaining)
        if not ready:
            break
        data = sock.recv(65536)
        # Ignore frames we sent ourselves
        if data[6:12] != SRC_MAC:
            responses.append(data)
    return responses


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_detection(sock: socket.socket) -> None:
    print("\n=== TEST: Detection packet (EtherType 0x0700) ===")
    pkt = detection_packet()
    hex_dump(pkt, "Sending")
    sock.send(pkt)
    responses = recv_responses(sock)
    if responses:
        for r in responses:
            hex_dump(r, "Card responded")
    else:
        print("  No response from card.")


def test_solid_frame(sock: socket.socket, width: int, height: int,
                     send_brightness: bool, color_bgr: tuple) -> None:
    label = "with brightness packet" if send_brightness else "WITHOUT brightness packet"
    r, g, b = color_bgr
    print(f"\n=== TEST: Solid color BGR=({b},{g},{r}) frame, {label} ===")

    if send_brightness:
        bp = brightness_packet(0xFF)
        sock.send(bp)
        print(f"  Sent brightness packet ({len(bp)} bytes)")

    row_data = bytes([b, g, r] * width)   # BGR order
    for row in range(height):
        pkt = row_packet(row, row_data, width)
        sock.send(pkt)

    print(f"  Sent {height} row packets ({width} pixels each)")

    responses = recv_responses(sock, timeout=0.5)
    if responses:
        for resp in responses:
            hex_dump(resp, "Card responded")
    else:
        print("  No response from card.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Colorlight 5A-75B protocol test — sends a solid red frame."
    )
    parser.add_argument("--interface", default="eth1")
    parser.add_argument("--width",     type=int, default=192)
    parser.add_argument("--height",    type=int, default=192)
    return parser.parse_args()


def main():
    args = parse_args()
    print(f"Interface: {args.interface}  |  Display: {args.width}x{args.height}")

    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
    sock.bind((args.interface, 0))

    # 1. Detection handshake — see if card responds and what it says
    test_detection(sock)
    time.sleep(0.2)

    # 2. Solid red, no brightness packet
    test_solid_frame(sock, args.width, args.height,
                     send_brightness=False, color_bgr=(0, 0, 255))
    time.sleep(0.5)

    # 3. Solid red, with brightness packet
    test_solid_frame(sock, args.width, args.height,
                     send_brightness=True, color_bgr=(0, 0, 255))
    time.sleep(0.5)

    # 4. Solid green, no brightness packet
    test_solid_frame(sock, args.width, args.height,
                     send_brightness=False, color_bgr=(0, 255, 0))

    sock.close()
    print("\nDone. Share the output above to help diagnose the issue.")


if __name__ == "__main__":
    main()
