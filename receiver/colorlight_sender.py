"""
receiver/colorlight_sender.py

Receives JPEG frames from receiver/main.py over UDP multicast and sends
them directly to a Colorlight 5A-75B LED receiver card via raw Ethernet
(Layer 2). No IP address, no FPP required.

Pipeline:
  receiver/main.py ──UDP multicast──► colorlight_sender.py ──raw Ethernet 0x0107──► Colorlight 5A-75B

Protocol details:
  - Pure Layer 2 — no IP, no UDP, no TCP
  - Destination MAC: 11:22:33:44:55:66 (hardcoded in card firmware)
  - Source MAC:      22:22:33:44:55:66 (hardcoded)
  - EtherType:       0x0107 (image data)
  - Pixel byte order: BGR (not RGB)
  - One brightness packet per frame, followed by one Ethernet frame per row

Usage (root required for raw socket):
    sudo python colorlight_sender.py [--interface eth1] [--width 192] [--height 192]

Dependencies: see requirements.txt
"""

import argparse
import io
import logging
import signal
import socket
import struct
import threading
import time

from PIL import Image

# ---------------------------------------------------------------------------
# Fragment protocol  (must match receiver/main.py)
# ---------------------------------------------------------------------------

_FRAG_HDR_FMT = ">IHHI"
_FRAG_HDR_LEN = struct.calcsize(_FRAG_HDR_FMT)   # 12 bytes

# ---------------------------------------------------------------------------
# Colorlight L2 protocol
# ---------------------------------------------------------------------------

_DST_MAC   = bytes.fromhex('112233445566')
_SRC_MAC   = bytes.fromhex('222233445566')
_ETHERTYPE = bytes([0x01, 0x07])
_ETH_HDR   = _DST_MAC + _SRC_MAC + _ETHERTYPE   # 14 bytes


def _brightness_packet(brightness: int) -> bytes:
    """
    Setup packet sent once before each frame's row data.
    Payload is 22 bytes; brightness sits at payload byte 21
    (= full-frame byte 35).
    """
    payload = bytearray(22)
    payload[21] = brightness & 0xFF
    return _ETH_HDR + bytes(payload)


def _row_packet(row: int, bgr_row: bytes, width: int) -> bytes:
    """
    One Ethernet frame per row of pixels.

    Payload layout:
      [0]      command byte  (0x05 = write row)
      [1-2]    reserved
      [3-4]    row index     (uint16 big-endian)
      [5-6]    pixel count   (uint16 big-endian)
      [7+]     BGR pixel data (width × 3 bytes)
    """
    header = struct.pack('>BBBHH',
        0x05,   # command: write pixel row
        0x00,   # reserved
        0x00,   # reserved
        row,    # row index (0-based)
        width,  # pixels in this row
    )
    return _ETH_HDR + header + bgr_row


# ---------------------------------------------------------------------------
# FrameStore — single-slot, latest-frame-wins
# ---------------------------------------------------------------------------

class FrameStore:
    def __init__(self):
        self._lock  = threading.Condition(threading.Lock())
        self._frame = None
        self._seq   = 0

    def put(self, data: bytes) -> None:
        with self._lock:
            self._frame = data
            self._seq  += 1
            self._lock.notify_all()

    def get_latest(self, last_seq: int, timeout: float = 1.0):
        with self._lock:
            deadline = time.monotonic() + timeout
            while self._seq <= last_seq or self._frame is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._lock.wait(timeout=remaining)
            return self._seq, self._frame


# ---------------------------------------------------------------------------
# UDPReceiver — multicast → fragment reassembly → FrameStore
# ---------------------------------------------------------------------------

class UDPReceiver(threading.Thread):
    def __init__(self, mcast_group: str, port: int,
                 store: FrameStore, shutdown: threading.Event):
        super().__init__(name="UDPReceiver", daemon=True)
        self.mcast_group = mcast_group
        self.port        = port
        self.store       = store
        self.shutdown    = shutdown

    def run(self):
        log = logging.getLogger(self.name)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", self.port))
        mreq = struct.pack("4s4s",
                           socket.inet_aton(self.mcast_group),
                           socket.inet_aton("0.0.0.0"))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.settimeout(1.0)
        log.info("UDP multicast receiver joined %s:%d", self.mcast_group, self.port)

        cur_id = cur_total = None
        cur_frags = {}

        try:
            while not self.shutdown.is_set():
                try:
                    data, _ = sock.recvfrom(65536)
                except socket.timeout:
                    continue
                if len(data) < _FRAG_HDR_LEN:
                    continue

                frame_id, frag_idx, frag_total, frame_size = struct.unpack_from(
                    _FRAG_HDR_FMT, data
                )
                payload = data[_FRAG_HDR_LEN:]

                if cur_id is not None and frame_id != cur_id:
                    if frame_id < cur_id:
                        continue
                    cur_frags.clear()

                cur_id    = frame_id
                cur_total = frag_total
                cur_frags[frag_idx] = payload

                if len(cur_frags) == cur_total:
                    jpeg_bytes = b"".join(cur_frags[i] for i in range(cur_total))
                    cur_frags.clear()
                    cur_id = None
                    self.store.put(jpeg_bytes)
        finally:
            sock.close()


# ---------------------------------------------------------------------------
# ColorlightSender — FrameStore → raw Ethernet → Colorlight card
# ---------------------------------------------------------------------------

class ColorlightSender(threading.Thread):
    def __init__(self, interface: str, width: int, height: int, brightness: int,
                 store: FrameStore, shutdown: threading.Event):
        super().__init__(name="ColorlightSender", daemon=True)
        self.interface  = interface
        self.width      = width
        self.height     = height
        self.brightness = brightness
        self.store      = store
        self.shutdown   = shutdown

    def run(self):
        log = logging.getLogger(self.name)

        # AF_PACKET + SOCK_RAW = raw Layer-2 socket; requires root
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
        sock.bind((self.interface, 0))
        log.info("Raw L2 socket bound to %s | display=%dx%d | brightness=%d",
                 self.interface, self.width, self.height, self.brightness)

        brightness_pkt = _brightness_packet(self.brightness)
        row_stride     = self.width * 3   # bytes per row (3 channels)
        last_seq       = 0

        try:
            while not self.shutdown.is_set():
                result = self.store.get_latest(last_seq, timeout=1.0)
                if result is None:
                    continue
                last_seq, jpeg_bytes = result

                try:
                    img = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
                    if img.size != (self.width, self.height):
                        img = img.resize((self.width, self.height), Image.LANCZOS)
                    # Colorlight expects BGR, not RGB
                    r, g, b = img.split()
                    bgr_bytes = Image.merge("RGB", (b, g, r)).tobytes()
                except Exception as exc:
                    log.warning("Frame decode error: %s", exc)
                    continue

                sock.send(brightness_pkt)
                for row in range(self.height):
                    row_data = bgr_bytes[row * row_stride: (row + 1) * row_stride]
                    sock.send(_row_packet(row, row_data, self.width))

                log.debug("Frame sent | seq=%d | %d rows", last_seq, self.height)
        finally:
            sock.close()


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="UDP multicast → raw Ethernet → Colorlight 5A-75B (no IP, no FPP)"
    )
    parser.add_argument("--mcast-group", default="239.0.0.1",
                        help="UDP multicast group to join (default: 239.0.0.1)")
    parser.add_argument("--port",        type=int, default=9002,
                        help="UDP port (default: 9002)")
    parser.add_argument("--interface",   default="eth1",
                        help="Interface connected to Colorlight card (default: eth1)")
    parser.add_argument("--width",       type=int, default=192,
                        help="Display width in pixels (default: 192)")
    parser.add_argument("--height",      type=int, default=192,
                        help="Display height in pixels (default: 192)")
    parser.add_argument("--brightness",  type=int, default=255,
                        help="Brightness 0-255 (default: 255)")
    parser.add_argument("--log-level",   default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("main")
    log.info("colorlight_sender | mcast=%s:%d | interface=%s | display=%dx%d",
             args.mcast_group, args.port, args.interface, args.width, args.height)

    shutdown = threading.Event()

    def _stop(sig, frame):
        log.info("Shutting down...")
        shutdown.set()

    signal.signal(signal.SIGINT,  _stop)
    signal.signal(signal.SIGTERM, _stop)

    store    = FrameStore()
    receiver = UDPReceiver(args.mcast_group, args.port, store, shutdown)
    sender   = ColorlightSender(
        args.interface, args.width, args.height, args.brightness, store, shutdown
    )

    receiver.start()
    sender.start()
    shutdown.wait()
    log.info("Stopped.")


if __name__ == "__main__":
    main()
