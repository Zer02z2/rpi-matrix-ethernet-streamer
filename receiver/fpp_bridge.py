"""
receiver/fpp_bridge.py

Receives JPEG frames from receiver/main.py over UDP multicast and forwards
them to Falcon Player (FPP) as DDP (Distributed Display Protocol) UDP packets.
FPP then outputs the pixel data to a Colorlight 5A-75B LED receiver card over
the dedicated point-to-point Ethernet connection.

Full pipeline on the Pi:
  receiver/main.py ──UDP multicast──► fpp_bridge.py ──DDP UDP──► FPP :4048 ──Ethernet──► Colorlight 5A-75B

Usage:
    python fpp_bridge.py [--mcast-group 239.0.0.1] [--port 9002]
                         [--fpp-host 127.0.0.1] [--fpp-port 4048]
                         [--width 256] [--height 128]

One-time FPP setup (FPP web UI):
    1. Input/Output → Add Input  → DDP Pixels  (port 4048)
    2. Input/Output → Add Output → Colorlight  → select direct-cable interface (e.g. eth1)
    3. Map DDP input channels → Colorlight output channels.

Dependencies:
    pip install pillow
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

_FRAG_HDR_FMT = ">IHHI"   # frame_id(4) frag_idx(2) frag_total(2) frame_size(4)
_FRAG_HDR_LEN = struct.calcsize(_FRAG_HDR_FMT)   # 12 bytes


# ---------------------------------------------------------------------------
# DDP v1 protocol constants
# ---------------------------------------------------------------------------

_DDP_VER1        = 0x40   # version bits (bits 7-6 = 01)
_DDP_PUSH        = 0x01   # push flag — receiver displays when this arrives
_DDP_TYPE_RGB8   = 0x02   # 8-bit RGB
_DDP_ID_DEFAULT  = 0x01   # output device 1

_DDP_HEADER_FMT = ">BBBBIH"   # flags1(1) flags2(1) type(1) id(1) offset(4) length(2)
_DDP_HEADER_LEN = struct.calcsize(_DDP_HEADER_FMT)   # 10 bytes

# Stay within standard 1500-byte Ethernet MTU:
# 1500 - 20 (IP) - 8 (UDP) - 10 (DDP) = 1462 → use 1440
_DDP_MAX_DATA = 1440


def build_ddp_packets(rgb_bytes: bytes) -> list:
    """Split raw RGB bytes into DDP push datagrams."""
    packets = []
    total = len(rgb_bytes)
    byte_offset = 0
    while byte_offset < total:
        chunk = rgb_bytes[byte_offset: byte_offset + _DDP_MAX_DATA]
        is_last = (byte_offset + len(chunk)) >= total
        flags1 = _DDP_VER1 | (_DDP_PUSH if is_last else 0)
        header = struct.pack(
            _DDP_HEADER_FMT,
            flags1,
            0x00,
            _DDP_TYPE_RGB8,
            _DDP_ID_DEFAULT,
            byte_offset,
            len(chunk),
        )
        packets.append(header + chunk)
        byte_offset += len(chunk)
    return packets


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
# UDPReceiver — multicast → fragment reassembly → FrameStore (raw JPEG bytes)
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

        cur_id    = None
        cur_total = None
        cur_size  = None
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
                cur_size  = frame_size
                cur_frags[frag_idx] = payload

                if len(cur_frags) == cur_total:
                    jpeg_bytes = b"".join(
                        cur_frags[i] for i in range(cur_total)
                    )
                    cur_frags.clear()
                    cur_id = None
                    self.store.put(jpeg_bytes)
                    log.debug("Frame reassembled | %d bytes", len(jpeg_bytes))
        finally:
            sock.close()


# ---------------------------------------------------------------------------
# DDPSender — decodes JPEG frames and fires DDP datagrams at FPP
# ---------------------------------------------------------------------------

class DDPSender(threading.Thread):
    def __init__(self, fpp_host: str, fpp_port: int,
                 width: int, height: int,
                 store: FrameStore, shutdown: threading.Event):
        super().__init__(name="DDPSender", daemon=True)
        self.fpp_host = fpp_host
        self.fpp_port = fpp_port
        self.width    = width
        self.height   = height
        self.store    = store
        self.shutdown = shutdown

    def run(self):
        log = logging.getLogger(self.name)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        log.info(
            "DDP sender ready → %s:%d  display=%dx%d px  (%d bytes/frame, %d DDP packets/frame)",
            self.fpp_host, self.fpp_port,
            self.width, self.height,
            self.width * self.height * 3,
            -(-self.width * self.height * 3 // _DDP_MAX_DATA),
        )
        last_seq = 0
        try:
            while not self.shutdown.is_set():
                result = self.store.get_latest(last_seq, timeout=1.0)
                if result is None:
                    continue
                last_seq, jpeg_bytes = result

                try:
                    img = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
                    if img.size != (self.width, self.height):
                        img = img.resize(
                            (self.width, self.height), Image.LANCZOS
                        )
                    rgb_bytes = img.tobytes()
                except Exception as exc:
                    log.warning("Failed to decode frame: %s", exc)
                    continue

                for pkt in build_ddp_packets(rgb_bytes):
                    sock.sendto(pkt, (self.fpp_host, self.fpp_port))

                log.debug("DDP frame sent | seq=%d | %d bytes", last_seq, len(rgb_bytes))
        finally:
            sock.close()


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Bridge: receiver/main.py UDP multicast → FPP DDP → Colorlight 5A-75B\n\n"
            "Run alongside receiver/main.py on the same Pi.\n"
            "FPP must be installed and configured with a DDP input and Colorlight output."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--mcast-group", default="239.0.0.1",
        metavar="GROUP",
        help="UDP multicast group to join (default: 239.0.0.1)"
    )
    parser.add_argument(
        "--port", type=int, default=9002,
        metavar="PORT",
        help="UDP multicast port (default: 9002)"
    )
    parser.add_argument(
        "--fpp-host", default="127.0.0.1",
        metavar="HOST",
        help="Host running Falcon Player — usually the same Pi (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--fpp-port", type=int, default=4048,
        metavar="PORT",
        help="FPP DDP input UDP port (default: 4048)"
    )
    parser.add_argument(
        "--width", type=int, default=256,
        metavar="PIXELS",
        help="Colorlight display width in pixels (default: 256)"
    )
    parser.add_argument(
        "--height", type=int, default=128,
        metavar="PIXELS",
        help="Colorlight display height in pixels (default: 128)"
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)"
    )
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
    log.info(
        "fpp_bridge | mcast=%s:%d | fpp=%s:%d | display=%dx%d",
        args.mcast_group, args.port,
        args.fpp_host, args.fpp_port,
        args.width, args.height,
    )

    shutdown = threading.Event()

    def _stop(sig, frame):
        log.info("Signal %s — shutting down...", sig)
        shutdown.set()

    signal.signal(signal.SIGINT,  _stop)
    signal.signal(signal.SIGTERM, _stop)

    store    = FrameStore()
    receiver = UDPReceiver(args.mcast_group, args.port, store, shutdown)
    sender   = DDPSender(
        args.fpp_host, args.fpp_port,
        args.width, args.height,
        store, shutdown,
    )

    receiver.start()
    sender.start()
    shutdown.wait()
    log.info("Stopped.")


if __name__ == "__main__":
    main()
