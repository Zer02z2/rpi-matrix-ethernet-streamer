"""
receiver/colorlight_detect.py

Sends a detection packet to the Colorlight 5A-75B and reads back the card's
firmware version and configured resolution.

Run as root:
    sudo python colorlight_detect.py [--interface eth1]
"""

import argparse
import socket

DST_MAC = bytes.fromhex('112233445566')
SRC_MAC = bytes.fromhex('222233445566')


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--interface", default="eth1")
    return p.parse_args()


def main():
    args = parse_args()

    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))  # ETH_P_ALL
    s.bind((args.interface, 0))
    s.settimeout(2.0)

    # Detection packet: EtherType 0x0700 + 270 zero bytes
    pkt = DST_MAC + SRC_MAC + bytes([0x07, 0x00]) + bytes(270)
    s.sendall(pkt)
    print(f"Sent detection packet on {args.interface}. Waiting for response...")

    try:
        while True:
            data = s.recv(1540)

            # Ignore our own outgoing packets
            if data[6:12] == SRC_MAC:
                continue

            print(f"\nReceived {len(data)} bytes:")
            print(f"  Src MAC:   {data[6:12].hex(':')}")
            print(f"  Dst MAC:   {data[0:6].hex(':')}")
            print(f"  EtherType: 0x{data[12]:02x}{data[13]:02x}")
            print(f"  Raw bytes (first 48): {data[:48].hex(' ')}")

            # Parse Colorlight 5A response
            if data[12] == 0x08 and data[13] == 0x05:
                print("\n  --> Colorlight card detected!")
                if len(data) > 16 and data[14] == 0x04:
                    fw_major = data[15]
                    fw_minor = data[16]
                    print(f"  Firmware: {fw_major}.{fw_minor}")
                if len(data) > 37:
                    res_x = data[34] * 256 + data[35]
                    res_y = data[36] * 256 + data[37]
                    print(f"  Configured resolution: {res_x} x {res_y}")
                    print()
                    if res_x == 0 and res_y == 0:
                        print("  WARNING: Resolution is 0x0 — card may not be configured yet.")
                    break
            else:
                print("  (Not a standard Colorlight detection response, continuing...)")

    except socket.timeout:
        print("\nNo response received within 2 seconds.")
        print("Possible causes:")
        print("  - Card is not powered or connected")
        print("  - Wrong interface (check 'ip link show')")
        print("  - Card firmware does not support detection")

    s.close()


if __name__ == "__main__":
    main()
