"""
displayer/main.py

Receives JPEG frames from receiver/main.py over TCP and displays them
on an RGB LED matrix.

Run with Python 3.11+:
    sudo python main.py [--port 9002] [--led-*]

Dependencies:
    pip install pillow
    rgbmatrix Python bindings must be installed.
"""

import argparse
import io
import socket
import struct
import time

from PIL import Image
from rgbmatrix import RGBMatrix, RGBMatrixOptions

try:
    from sdnotify import SystemdNotifier
    _notifier = SystemdNotifier()
except ImportError:
    _notifier = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _recvall(sock: socket.socket, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def iter_frames(port: int):
    """Yields PIL Images from receiver's TCP stream."""
    while True:
        try:
            print(f"Connecting to receiver on port {port}...")
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect(("127.0.0.1", port))
                print("Connected.")
                while True:
                    raw = _recvall(s, 4)
                    if not raw:
                        break
                    length = struct.unpack(">I", raw)[0]
                    jpeg   = _recvall(s, length)
                    if not jpeg:
                        break
                    yield Image.open(io.BytesIO(jpeg)).convert("RGB")
        except Exception as e:
            print(f"Connection lost: {e} — retrying in 2s...")
            time.sleep(2)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="RGB Matrix display — JPEG frames via TCP.")

    parser.add_argument("--port", type=int, default=9002)

    parser.add_argument("--led-rows",                type=int,  default=64)
    parser.add_argument("--led-cols",                type=int,  default=64)
    parser.add_argument("--led-chain",               type=int,  default=3,   dest="led_chain")
    parser.add_argument("--led-parallel",            type=int,  default=3,   dest="led_parallel")
    parser.add_argument("--led-pwm-bits",            type=int,  default=7,   dest="led_pwm_bits")
    parser.add_argument("--led-pwm-dither-bits",     type=int,  default=1,   dest="led_pwm_dither_bits")
    parser.add_argument("--led-pwm-lsb-nanoseconds", type=int, default=50,   dest="led_pwm_lsb_nanoseconds")
    parser.add_argument("--led-slowdown-gpio",       type=int,  default=3,   dest="led_slowdown_gpio")
    parser.add_argument("--led-brightness",          type=int,  default=100, dest="led_brightness")
    parser.add_argument("--led-hardware-mapping",    default="regular",      dest="led_hardware_mapping")
    parser.add_argument("--led-pixel-mapper",        default="",             dest="led_pixel_mapper")
    parser.add_argument("--led-show-refresh",        action="store_true", default=False, dest="led_show_refresh")

    return parser.parse_args()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    options = RGBMatrixOptions()
    options.rows                = args.led_rows
    options.cols                = args.led_cols
    options.chain_length        = args.led_chain
    options.parallel            = args.led_parallel
    options.pwm_bits            = args.led_pwm_bits
    options.pwm_dither_bits     = args.led_pwm_dither_bits
    options.pwm_lsb_nanoseconds = args.led_pwm_lsb_nanoseconds
    options.gpio_slowdown       = args.led_slowdown_gpio
    options.brightness          = args.led_brightness
    options.hardware_mapping    = args.led_hardware_mapping
    options.pixel_mapper_config = args.led_pixel_mapper
    options.show_refresh_rate   = args.led_show_refresh
    options.drop_privileges     = False

    matrix   = RGBMatrix(options=options)
    matrix_w = matrix.width
    matrix_h = matrix.height
    print(f"Matrix: {matrix_w}x{matrix_h}")

    canvas = matrix.CreateFrameCanvas()

    try:
        for img in iter_frames(args.port):
            img = img.resize((matrix_w, matrix_h), Image.LANCZOS)
            canvas.SetImage(img)
            canvas = matrix.SwapOnVSync(canvas)
            canvas.SetImage(img)
            if _notifier:
                _notifier.notify("WATCHDOG=1")
    except KeyboardInterrupt:
        matrix.Clear()
        print("\nShutting down.")


if __name__ == "__main__":
    main()
