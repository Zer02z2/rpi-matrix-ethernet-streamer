"""
sender/tcp_sender.py

Receives NDI video (from TouchDesigner or any NDI source) and serves
frames as JPEG over TCP. The Pi displayer connects to this script.

Usage:
    python tcp_sender.py [--port 9002] [--ndi-name ScreenCapture]

Dependencies:
    pip install ndi-python numpy opencv-python pillow
    NDI SDK must be installed on the Mac.
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
            for s in ndi.find_get_current_sources(finder):
                try:
                    name = s.ndi_name
                    if self.ndi_name.lower() in name.lower():
                        source = s
                        log.info("Matched: %s", name)
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
# TCP server — Pi displayer(s) connect to this
# ---------------------------------------------------------------------------

class ClientHandler(threading.Thread):
    def __init__(self, conn: socket.socket, addr, store: FrameStore,
                 shutdown: threading.Event):
        super().__init__(name=f"Client-{addr}", daemon=True)
        self.conn     = conn
        self.addr     = addr
        self.store    = store
        self.shutdown = shutdown

    def run(self):
        log = logging.getLogger(self.name)
        log.info("Connected: %s", self.addr)
        self.conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        last_seq = 0
        try:
            while not self.shutdown.is_set():
                result = self.store.get_latest(last_seq, timeout=1.0)
                if result is None:
                    continue
                last_seq, jpeg_bytes = result
                try:
                    self.conn.sendall(struct.pack(">I", len(jpeg_bytes)) + jpeg_bytes)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
        finally:
            self.conn.close()
            log.info("Disconnected: %s", self.addr)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="NDI → TCP bridge: receives NDI and serves frames to display Pi."
    )
    parser.add_argument("--port", type=int, default=9002,
                        help="TCP port to listen on (default: 9002)")
    parser.add_argument("--ndi-name", default="ScreenCapture",
                        help="NDI source name to receive (default: ScreenCapture)")
    parser.add_argument("--quality", type=int, default=85,
                        help="JPEG quality 1-95 (default: 85)")
    parser.add_argument("--log-level", default="INFO",
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
    receiver.start()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", args.port))
    server.listen(4)
    server.settimeout(1.0)
    log.info("TCP server listening on 0.0.0.0:%d — waiting for Pi to connect.", args.port)

    while not shutdown.is_set():
        try:
            conn, addr = server.accept()
            ClientHandler(conn, addr, store, shutdown).start()
        except socket.timeout:
            continue

    server.close()
    ndi.destroy()
    log.info("Stopped.")


if __name__ == "__main__":
    main()
