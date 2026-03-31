"""
receiver/colorlight_sender.py

Receives JPEG frames from receiver/main.py over UDP multicast and sends
them directly to a Colorlight 5A-75B LED receiver card via raw Ethernet
(Layer 2). No IP address, no FPP required.

Protocol (3 EtherTypes per frame):
  0x0a00+brt  Brightness packet   — sent first
  0x5500      Row pixel data      — one packet per row, BGR order
  0x0107      Latch/display       — sent last to flip the display buffer

Credit: protocol reverse-engineered by haraldkubota
        https://github.com/haraldkubota/colorlight

Usage (root required for raw socket):
    sudo python colorlight_sender.py [--interface eth1] [--width 192] [--height 192] [--brightness 50]

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

_DST_MAC = bytes.fromhex('112233445566')
_SRC_MAC = bytes.fromhex('222233445566')

# Brightness percent → hardware value (from haraldkubota reference)
_BRIGHTNESS_MAP = [
    (0,   0x00), (1,  0x03), (2,  0x05), (4,  0x0a),
    (5,   0x0d), (6,  0x0f), (10, 0x1a), (25, 0x40),
    (50,  0x80), (75, 0xbf), (100, 0xff),
]


def _hw_brightness(pct: int) -> int:
    """Convert brightness percent (0-100) to hardware value (0-255)."""
    val = 0x00
    for threshold, v in _BRIGHTNESS_MAP:
        if pct >= threshold:
            val = v
    return val


def _brightness_packet(pct: int) -> bytes:
    """
    EtherType = 0x0a00 + hw_brightness.
    63-byte payload: [hw_brt, hw_brt, 0xFF, 0, 0, ...]
    """
    hw  = _hw_brightness(pct)
    eth = _DST_MAC + _SRC_MAC + bytes([0x0a, hw])
    payload = bytearray(63)
    payload[0] = hw
    payload[1] = hw
    payload[2] = 0xFF
    return eth + bytes(payload)


def _row_packet(row: int, bgr_row: bytes, width: int) -> bytes:
    """
    EtherType = 0x5500.
    7-byte header: [row, 0, 0, width_hi, width_lo, 0x08, 0x88]
    Followed by BGR pixel data (width × 3 bytes).
    """
    eth    = _DST_MAC + _SRC_MAC + bytes([0x55, 0x00])
    header = bytes([
        row,
        0x00,
        0x00,
        width >> 8,
        width & 0xFF,
        0x08,
        0x88,
    ])
    return eth + header + bgr_row


def _latch_packet(pct: int) -> bytes:
    """
    EtherType = 0x0107.
    98-byte payload with color calibration at specific offsets.
    Flips the display buffer — must be sent after all row packets.
    """
    eth     = _DST_MAC + _SRC_MAC + bytes([0x01, 0x07])
    payload = bytearray(98)
    payload[21] = pct
    payload[22] = 5
    payload[24] = pct
    payload[25] = pct
    payload[26] = pct
    return eth + bytes(payload)


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
        self.brightness = brightness   # 0-100 percent
        self.store      = store
        self.shutdown   = shutdown

    def run(self):
        log = logging.getLogger(self.name)

        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
        sock.bind((self.interface, 0))
        log.info("Raw L2 socket bound to %s | display=%dx%d | brightness=%d%%",
                 self.interface, self.width, self.height, self.brightness)

        brt_pkt   = _brightness_packet(self.brightness)
        latch_pkt = _latch_packet(self.brightness)
        row_stride = self.width * 3
        last_seq   = 0

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
                    # Colorlight expects BGR
                    r, g, b = img.split()
                    bgr_bytes = Image.merge("RGB", (b, g, r)).tobytes()
                except Exception as exc:
                    log.warning("Frame decode error: %s", exc)
                    continue

                # 1. Brightness
                sock.send(brt_pkt)

                # 2. Row data
                for row in range(self.height):
                    row_data = bgr_bytes[row * row_stride: (row + 1) * row_stride]
                    sock.send(_row_packet(row, row_data, self.width))

                # 3. Small delay then latch (haraldkubota notes this prevents
                #    flickering on the last row module)
                time.sleep(0.001)
                sock.send(latch_pkt)

                log.debug("Frame sent | seq=%d", last_seq)
        finally:
            sock.close()


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="UDP multicast → raw Ethernet → Colorlight 5A-75B (no IP, no FPP)"
    )
    parser.add_argument("--mcast-group", default="239.0.0.1")
    parser.add_argument("--port",        type=int, default=9002)
    parser.add_argument("--interface",   default="eth1",
                        help="Interface connected to Colorlight card (default: eth1)")
    parser.add_argument("--width",       type=int, default=192)
    parser.add_argument("--height",      type=int, default=192)
    parser.add_argument("--brightness",  type=int, default=50,
                        help="Brightness 0-100 percent (default: 50)")
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
    log.info("colorlight_sender | mcast=%s:%d | interface=%s | display=%dx%d | brightness=%d%%",
             args.mcast_group, args.port, args.interface,
             args.width, args.height, args.brightness)

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
