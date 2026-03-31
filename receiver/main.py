"""
receiver/main.py

Receives NDI video from the Mac sender and broadcasts JPEG frames over
UDP multicast to all listeners on the local Pi (displayer, fpp_bridge, etc.).

Pipeline:
  Mac sender/main.py  ──NDI──►  receiver/main.py  ──UDP multicast──►  displayer/main.py
                                                                    └──►  colorlight/fpp_bridge.py

Fragment protocol (12-byte header per datagram):
  frame_id(4)  frag_idx(2)  frag_total(2)  frame_size(4)  [payload]

Run with Python 3.9+:
    python main.py [--ndi-name ScreenCapture] [--mcast-group 239.0.0.1] [--port 9002]

Dependencies:
    pip install ndi-python numpy opencv-python pillow
    avahi-daemon must be running for NDI discovery.
"""

import argparse
import io
import logging
import signal
import socket
import struct
import threading
import time

import cv2
import numpy as np
import NDIlib as ndi
from PIL import Image

# ---------------------------------------------------------------------------
# Fragment protocol
# ---------------------------------------------------------------------------

_FRAG_HDR_FMT  = ">IHHI"   # frame_id(4) frag_idx(2) frag_total(2) frame_size(4)
_FRAG_HDR_LEN  = struct.calcsize(_FRAG_HDR_FMT)   # 12 bytes
_CHUNK_SIZE    = 60_000    # bytes of JPEG data per datagram (safe for loopback)


def fragment(jpeg_bytes: bytes, frame_id: int) -> list:
    """Split jpeg_bytes into UDP datagrams with fragment headers."""
    total = len(jpeg_bytes)
    chunks = [jpeg_bytes[i: i + _CHUNK_SIZE] for i in range(0, total, _CHUNK_SIZE)]
    frag_total = len(chunks)
    pkts = []
    for idx, chunk in enumerate(chunks):
        hdr = struct.pack(_FRAG_HDR_FMT, frame_id, idx, frag_total, total)
        pkts.append(hdr + chunk)
    return pkts


# ---------------------------------------------------------------------------
# FrameStore — single-slot, latest frame wins
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
# NDI receive loop
# ---------------------------------------------------------------------------

class NDIReceiver(threading.Thread):
    def __init__(self, ndi_name: str, store: FrameStore,
                 shutdown: threading.Event, jpeg_quality: int):
        super().__init__(name="NDIReceiver", daemon=True)
        self.ndi_name     = ndi_name
        self.store        = store
        self.shutdown     = shutdown
        self.jpeg_quality = jpeg_quality

    def run(self):
        log = logging.getLogger(self.name)
        while not self.shutdown.is_set():
            recv = self._connect(log)
            if recv is None:
                continue
            log.info("Receiving NDI frames...")
            self._recv_loop(recv, log)
            ndi.recv_destroy(recv)
            if not self.shutdown.is_set():
                log.warning("NDI stream lost — reconnecting in 2s...")
                time.sleep(2)

    def _connect(self, log):
        finder = ndi.find_create_v2()
        if not finder:
            log.error("Failed to create NDI finder.")
            time.sleep(2)
            return None

        log.info("Searching for NDI source '%s'...", self.ndi_name)
        source = None
        while source is None and not self.shutdown.is_set():
            ndi.find_wait_for_sources(finder, 1000)
            sources = ndi.find_get_current_sources(finder)
            for s in sources:
                try:
                    if self.ndi_name.lower() in s.ndi_name.lower():
                        source = s
                        log.info("Matched: %s", s.ndi_name)
                        break
                except Exception:
                    pass
            if source is None:
                log.debug("Not found yet, retrying...")

        if source is None:
            ndi.find_destroy(finder)
            return None

        recv_settings = ndi.RecvCreateV3()
        recv_settings.color_format = ndi.RECV_COLOR_FORMAT_BGRX_BGRA
        recv = ndi.recv_create_v3(recv_settings)
        if not recv:
            log.error("Failed to create NDI receiver.")
            ndi.find_destroy(finder)
            return None

        ndi.recv_connect(recv, source)
        ndi.find_destroy(finder)
        return recv

    def _recv_loop(self, recv, log):
        while not self.shutdown.is_set():
            frame_type, video, _, _ = ndi.recv_capture_v3(recv, 1000)

            if frame_type == ndi.FRAME_TYPE_VIDEO:
                img = np.copy(video.data)
                ndi.recv_free_video_v2(recv, video)

                img = cv2.cvtColor(img, cv2.COLOR_BGRA2RGB)
                pil_img = Image.fromarray(img)
                buf = io.BytesIO()
                pil_img.save(buf, format="JPEG", quality=self.jpeg_quality)
                self.store.put(buf.getvalue())

            elif frame_type == ndi.FRAME_TYPE_ERROR:
                log.warning("NDI frame error — reconnecting.")
                break


# ---------------------------------------------------------------------------
# UDP multicast sender
# ---------------------------------------------------------------------------

class UDPSender(threading.Thread):
    def __init__(self, mcast_group: str, port: int,
                 store: FrameStore, shutdown: threading.Event):
        super().__init__(name="UDPSender", daemon=True)
        self.mcast_group = mcast_group
        self.port        = port
        self.store       = store
        self.shutdown    = shutdown

    def run(self):
        log = logging.getLogger(self.name)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Enable loopback so processes on the same host receive their own multicast
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        # TTL 1 keeps multicast local to the machine / single subnet
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        log.info("UDP multicast sender → %s:%d", self.mcast_group, self.port)

        last_seq  = 0
        frame_id  = 0
        dest      = (self.mcast_group, self.port)
        try:
            while not self.shutdown.is_set():
                result = self.store.get_latest(last_seq, timeout=1.0)
                if result is None:
                    continue
                last_seq, jpeg_bytes = result
                for pkt in fragment(jpeg_bytes, frame_id):
                    sock.sendto(pkt, dest)
                log.debug("Frame %d sent | %d bytes | %d fragment(s)",
                          frame_id, len(jpeg_bytes),
                          -(-len(jpeg_bytes) // _CHUNK_SIZE))
                frame_id = (frame_id + 1) & 0xFFFFFFFF
        finally:
            sock.close()


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="NDI receiver → UDP multicast JPEG broadcaster."
    )
    parser.add_argument("--ndi-name",    default="ScreenCapture",
                        help="NDI source name to search for (default: ScreenCapture)")
    parser.add_argument("--mcast-group", default="239.0.0.1",
                        help="UDP multicast group address (default: 239.0.0.1)")
    parser.add_argument("--port",        type=int, default=9002,
                        help="UDP port (default: 9002)")
    parser.add_argument("--quality",     type=int, default=85,
                        help="JPEG compression quality 1-95 (default: 85)")
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

    if not ndi.initialize():
        raise RuntimeError("Failed to initialize NDI.")

    shutdown = threading.Event()

    def _stop(sig, frame):
        log.info("Shutting down...")
        shutdown.set()

    signal.signal(signal.SIGINT,  _stop)
    signal.signal(signal.SIGTERM, _stop)

    store    = FrameStore()
    receiver = NDIReceiver(args.ndi_name, store, shutdown, args.quality)
    sender   = UDPSender(args.mcast_group, args.port, store, shutdown)

    receiver.start()
    sender.start()
    shutdown.wait()

    ndi.destroy()
    log.info("Stopped.")


if __name__ == "__main__":
    main()
