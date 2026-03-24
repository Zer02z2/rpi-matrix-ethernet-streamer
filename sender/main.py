"""
sender/main.py

Captures a screen region centered on the mouse cursor and streams it as an
NDI source over the local network.

Usage:
    python main.py [--ndi-name ScreenCapture] [--capture-fraction 0.5] [--fps 30]

Dependencies:
    pip install mss numpy opencv-python pynput ndi-python
    NDI SDK must be installed on the Mac: https://ndi.video/tools/
"""

import argparse
import logging
import signal
import time

import cv2
import mss
import numpy as np
import NDIlib as ndi
from pynput import mouse as pynput_mouse

NDI_OUTPUT_SIZE = 480  # width × height sent over NDI (square)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clamp_region(cx: int, cy: int, size: int, monitor: dict) -> dict:
    """Return an mss region dict of `size`×`size` centered on (cx, cy),
    clamped to the monitor bounds."""
    half = size // 2
    left = cx - half
    top  = cy - half

    # Clamp so the region stays within the monitor
    mon_right  = monitor["left"] + monitor["width"]
    mon_bottom = monitor["top"]  + monitor["height"]

    if left < monitor["left"]:
        left = monitor["left"]
    if top < monitor["top"]:
        top = monitor["top"]
    if left + size > mon_right:
        left = mon_right - size
    if top + size > mon_bottom:
        top = mon_bottom - size

    return {"left": left, "top": top, "width": size, "height": size}


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mac screen-capture NDI sender for RGB LED matrix."
    )
    parser.add_argument(
        "--ndi-name", default="ScreenCapture",
        help="NDI source name visible on the network (default: ScreenCapture)"
    )
    parser.add_argument(
        "--capture-fraction", type=float, default=0.5,
        metavar="FRACTION",
        help="Capture region as a fraction of the primary monitor's height "
             "(default: 0.5 — e.g. half of a 1080p screen = 540×540 px)"
    )
    parser.add_argument(
        "--fps", type=int, default=30,
        help="Target send rate in frames per second (default: 30)"
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)"
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("sender")

    # NDI init
    if not ndi.initialize():
        log.error("Failed to initialize NDI. Is the NDI SDK installed?")
        raise SystemExit(1)

    send_settings = ndi.SendCreate()
    send_settings.ndi_name = args.ndi_name
    send = ndi.send_create(send_settings)
    if not send:
        log.error("Failed to create NDI sender.")
        ndi.destroy()
        raise SystemExit(1)

    video_frame = ndi.VideoFrameV2()
    video_frame.FourCC = ndi.FOURCC_VIDEO_TYPE_BGRX
    video_frame.xres = NDI_OUTPUT_SIZE
    video_frame.yres = NDI_OUTPUT_SIZE

    log.info("NDI source '%s' created — visible on the local network.", args.ndi_name)

    # Graceful shutdown
    running = True

    def _stop(sig, frame):
        nonlocal running
        log.info("Signal %s received — shutting down.", sig)
        running = False

    signal.signal(signal.SIGINT,  _stop)
    signal.signal(signal.SIGTERM, _stop)

    mouse = pynput_mouse.Controller()
    frame_interval = 1.0 / args.fps

    with mss.mss() as sct:
        monitor = sct.monitors[1]  # primary display (index 0 = virtual all-monitors)
        capture_size = int(monitor["height"] * args.capture_fraction)
        log.info(
            "Primary monitor: %dx%d | Capture region: %dx%d px → NDI output: %dx%d px | FPS: %d",
            monitor["width"], monitor["height"],
            capture_size, capture_size,
            NDI_OUTPUT_SIZE, NDI_OUTPUT_SIZE,
            args.fps,
        )

        while running:
            t0 = time.monotonic()

            # 1. Mouse position
            mx, my = (int(v) for v in mouse.position)

            # 2. Clamp capture region to monitor bounds
            region = clamp_region(mx, my, capture_size, monitor)

            # 3. Capture → raw BGRA bytes
            raw = sct.grab(region)

            # 4. Zero-copy numpy view  (H × W × 4, BGRA uint8)
            img = np.frombuffer(raw.bgra, dtype=np.uint8).reshape(
                (raw.height, raw.width, 4)
            )

            # 5. Resize to NDI output resolution on the Mac
            #    (reduces NDI bandwidth and Pi decode work)
            if img.shape[:2] != (NDI_OUTPUT_SIZE, NDI_OUTPUT_SIZE):
                img = cv2.resize(
                    img,
                    (NDI_OUTPUT_SIZE, NDI_OUTPUT_SIZE),
                    interpolation=cv2.INTER_LINEAR,
                )

            # 6. NDI send — BGRX FourCC matches mss BGRA (alpha channel ignored)
            video_frame.data = img
            ndi.send_send_video_v2(send, video_frame)

            log.debug(
                "Frame sent | mouse=(%d,%d) region=%s | %.1f ms",
                mx, my, region, (time.monotonic() - t0) * 1000,
            )

            # 7. Rate limit
            elapsed = time.monotonic() - t0
            remaining = frame_interval - elapsed
            if remaining > 0:
                time.sleep(remaining)

    # Cleanup
    ndi.send_destroy(send)
    ndi.destroy()
    log.info("Sender stopped.")


if __name__ == "__main__":
    main()
