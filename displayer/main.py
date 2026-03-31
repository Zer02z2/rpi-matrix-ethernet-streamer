"""
displayer/main.py

Receives JPEG frames from receiver/main.py over UDP multicast and displays
them on an RGB LED matrix.

Decoupled architecture:
  - ReceiverThread: joins UDP multicast group, reassembles fragments, stores
    latest decoded PIL Image in FrameStore
  - Display loop (main thread): swaps on vsync at matrix rate, always shows
    the latest frame

Run with Python 3.11+:
    sudo python main.py [--mcast-group 239.0.0.1] [--port 9002] [--led-*]

Dependencies:
    pip install pillow
    rgbmatrix Python bindings must be installed.
"""

import argparse
import io
import socket
import struct
import threading
import time

from PIL import Image
from rgbmatrix import RGBMatrix, RGBMatrixOptions

try:
    from sdnotify import SystemdNotifier
    _notifier = SystemdNotifier()
except ImportError:
    _notifier = None

# ---------------------------------------------------------------------------
# Fragment protocol  (must match receiver/main.py)
# ---------------------------------------------------------------------------

_FRAG_HDR_FMT = ">IHHI"   # frame_id(4) frag_idx(2) frag_total(2) frame_size(4)
_FRAG_HDR_LEN = struct.calcsize(_FRAG_HDR_FMT)   # 12 bytes


# ---------------------------------------------------------------------------
# FrameStore — single-slot, latest frame wins
# ---------------------------------------------------------------------------

class FrameStore:
    def __init__(self):
        self._lock  = threading.Condition(threading.Lock())
        self._frame = None
        self._seq   = 0

    def put(self, img: Image.Image) -> None:
        with self._lock:
            self._frame = img
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
# ReceiverThread — UDP multicast → fragment reassembly → FrameStore
# ---------------------------------------------------------------------------

class ReceiverThread(threading.Thread):
    def __init__(self, mcast_group: str, port: int,
                 store: FrameStore, matrix_w: int, matrix_h: int,
                 shutdown: threading.Event):
        super().__init__(name="Receiver", daemon=True)
        self.mcast_group = mcast_group
        self.port        = port
        self.store       = store
        self.matrix_w    = matrix_w
        self.matrix_h    = matrix_h
        self.shutdown    = shutdown

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", self.port))

        # Join the multicast group on all interfaces
        mreq = struct.pack("4s4s",
                           socket.inet_aton(self.mcast_group),
                           socket.inet_aton("0.0.0.0"))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.settimeout(1.0)
        print(f"UDP multicast receiver joined {self.mcast_group}:{self.port}")

        # Reassembly state for the current in-progress frame
        cur_id      = None   # frame_id being assembled
        cur_total   = None   # expected fragment count
        cur_size    = None   # expected total JPEG bytes
        cur_frags   = {}     # frag_idx → payload bytes

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

                # Drop stale fragments from older frames
                if cur_id is not None and frame_id != cur_id:
                    if frame_id < cur_id:
                        continue   # truly stale, discard
                    # newer frame started — abandon current reassembly
                    cur_frags.clear()

                cur_id    = frame_id
                cur_total = frag_total
                cur_size  = frame_size
                cur_frags[frag_idx] = payload

                if len(cur_frags) == cur_total:
                    # All fragments arrived — reassemble
                    jpeg_bytes = b"".join(
                        cur_frags[i] for i in range(cur_total)
                    )
                    cur_frags.clear()
                    cur_id = None
                    try:
                        img = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
                        img = img.resize(
                            (self.matrix_w, self.matrix_h), Image.LANCZOS
                        )
                        self.store.put(img)
                    except Exception as e:
                        print(f"Frame decode error: {e}")
        finally:
            sock.close()


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="RGB Matrix display — JPEG frames via UDP multicast."
    )
    parser.add_argument("--mcast-group", default="239.0.0.1",
                        help="UDP multicast group to join (default: 239.0.0.1)")
    parser.add_argument("--port",        type=int, default=9002,
                        help="UDP port (default: 9002)")

    parser.add_argument("--led-rows",                type=int,  default=64)
    parser.add_argument("--led-cols",                type=int,  default=64)
    parser.add_argument("--led-chain",               type=int,  default=3,   dest="led_chain")
    parser.add_argument("--led-parallel",            type=int,  default=3,   dest="led_parallel")
    parser.add_argument("--led-pwm-bits",            type=int,  default=7,   dest="led_pwm_bits")
    parser.add_argument("--led-pwm-dither-bits",     type=int,  default=1,   dest="led_pwm_dither_bits")
    parser.add_argument("--led-pwm-lsb-nanoseconds", type=int, default=50,   dest="led_pwm_lsb_nanoseconds")
    parser.add_argument("--led-slowdown-gpio",       type=int,  default=3,   dest="led_slowdown_gpio")
    parser.add_argument("--led-brightness",          type=int,  default=80,  dest="led_brightness")
    parser.add_argument("--led-hardware-mapping",    default="regular",      dest="led_hardware_mapping")
    parser.add_argument("--led-pixel-mapper",        default="",             dest="led_pixel_mapper")
    parser.add_argument("--led-show-refresh",        action="store_true",    default=False, dest="led_show_refresh")
    parser.add_argument("--led-limit-refresh",       type=int,  default=300, dest="led_limit_refresh",
                        help="Limit refresh rate in Hz, 0 = no limit (default: 300)")

    return parser.parse_args()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    options = RGBMatrixOptions()
    options.rows                  = args.led_rows
    options.cols                  = args.led_cols
    options.chain_length          = args.led_chain
    options.parallel              = args.led_parallel
    options.pwm_bits              = args.led_pwm_bits
    options.pwm_dither_bits       = args.led_pwm_dither_bits
    options.pwm_lsb_nanoseconds   = args.led_pwm_lsb_nanoseconds
    options.gpio_slowdown         = args.led_slowdown_gpio
    options.brightness            = args.led_brightness
    options.hardware_mapping      = args.led_hardware_mapping
    options.pixel_mapper_config   = args.led_pixel_mapper
    options.show_refresh_rate     = args.led_show_refresh
    options.limit_refresh_rate_hz = args.led_limit_refresh
    options.drop_privileges       = False

    matrix   = RGBMatrix(options=options)
    matrix_w = matrix.width
    matrix_h = matrix.height
    print(f"Matrix: {matrix_w}x{matrix_h}")

    shutdown = threading.Event()
    store    = FrameStore()

    receiver = ReceiverThread(
        args.mcast_group, args.port, store, matrix_w, matrix_h, shutdown
    )
    receiver.start()

    canvas   = matrix.CreateFrameCanvas()
    last_seq = 0

    try:
        while True:
            result = store.get_latest(last_seq, timeout=1.0)
            if result is None:
                continue
            last_seq, img = result

            canvas.SetImage(img)
            canvas = matrix.SwapOnVSync(canvas)
            canvas.SetImage(img)

            if _notifier:
                _notifier.notify("WATCHDOG=1")

    except KeyboardInterrupt:
        shutdown.set()
        matrix.Clear()
        print("\nShutting down.")


if __name__ == "__main__":
    main()
