# rpi-matrix-ethernet-streamer

Stream your Mac screen live to an RGB LED matrix connected to a Raspberry Pi. The system captures a region of your screen (centered on the mouse cursor), compresses it as JPEG frames, and displays it in real time on a chained LED matrix panel array.

## Architecture

```
Mac                              Network                  Raspberry Pi
┌─────────────────────┐          ┌──────────────────┐     ┌──────────────────────┐
│ sender/main.py      │──NDI────▶│ receiver/main.py │─TCP▶│ displayer/main.py    │
│ (screen capture)    │          │ (NDI → TCP)      │     │ (LED matrix display) │
└─────────────────────┘          └──────────────────┘     └──────────────────────┘
```

**Alternative (tcp-native branch):** The `sender/tcp_sender.py` script combines the NDI receiver and TCP server into one process that runs on the Mac, so the Pi only needs to run the displayer.

```
Mac                                              Raspberry Pi
┌──────────────────────────────────┐             ┌──────────────────────┐
│ sender/main.py + tcp_sender.py   │────TCP─────▶│ displayer/main.py    │
│ (screen capture + TCP server)    │             │ (LED matrix display) │
└──────────────────────────────────┘             └──────────────────────┘
```

## Prerequisites

### Mac

- Python 3.10+
- [NDI Tools / SDK](https://ndi.video/tools/) — install the runtime so `ndi-python` can find the shared library

### Raspberry Pi

- Python 3.9+
- [rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix) — compile the library and install the Python bindings (see below)
- `avahi-daemon` running (required for NDI source discovery over mDNS)

```bash
sudo apt-get install avahi-daemon
```

## Installation

### Mac — Sender

```bash
cd sender
pip install mss numpy opencv-python pynput ndi-python pillow
```

### Mac — TCP Sender (tcp-native branch only)

```bash
cd sender
pip install ndi-python numpy opencv-python pillow
```

### Raspberry Pi — NDI Receiver

```bash
cd receiver
pip install ndi-python numpy opencv-python pillow
```

### Raspberry Pi — LED Matrix Displayer

First, build and install the `rpi-rgb-led-matrix` C++ library and its Python bindings:

```bash
git clone https://github.com/hzeller/rpi-rgb-led-matrix.git
cd rpi-rgb-led-matrix
make build-python PYTHON=$(which python3)
sudo make install-python PYTHON=$(which python3)
```

Then install the remaining Python dependency:

```bash
cd displayer
pip install pillow
# Optional: systemd watchdog support
pip install sdnotify
```

## Usage

### Option A — NDI Pipeline (main branch)

**Step 1: Start the screen capture sender on the Mac**

```bash
cd sender
python main.py --ndi-name ScreenCapture --capture-fraction 0.5 --fps 30
```

**Step 2: Start the NDI receiver on the Raspberry Pi**

```bash
cd receiver
LC_ALL=C.UTF-8 python main.py --ndi-name ScreenCapture --port 9002
```

**Step 3: Start the LED matrix displayer on the Raspberry Pi**

```bash
cd displayer
sudo python main.py \
  --host 127.0.0.1 --port 9002 \
  --led-rows 64 --led-cols 64 \
  --led-chain 3 --led-parallel 3 \
  --led-brightness 100
```

---

### Option B — TCP Native (tcp-native branch)

**Step 1: Start the screen capture sender on the Mac**

```bash
cd sender
python main.py --ndi-name ScreenCapture --capture-fraction 0.5 --fps 30
```

**Step 2: Start the TCP sender (NDI → TCP bridge) on the Mac**

```bash
cd sender
python tcp_sender.py --ndi-name ScreenCapture --port 9002 --quality 85
```

**Step 3: Start the LED matrix displayer on the Raspberry Pi** (pointing at the Mac's IP)

```bash
cd displayer
sudo python main.py \
  --host <mac-ip> --port 9002 \
  --led-rows 64 --led-cols 64 \
  --led-chain 3 --led-parallel 3 \
  --led-brightness 100
```

## Configuration Reference

### sender/main.py

| Flag | Default | Description |
|---|---|---|
| `--ndi-name` | `ScreenCapture` | NDI source name to broadcast |
| `--capture-fraction` | `0.5` | Capture region as a fraction of monitor height |
| `--fps` | `30` | Target frame rate |
| `--log-level` | `INFO` | Logging verbosity |

### sender/tcp_sender.py

| Flag | Default | Description |
|---|---|---|
| `--ndi-name` | `ScreenCapture` | NDI source name to receive |
| `--port` | `9002` | TCP port to serve frames on |
| `--quality` | `85` | JPEG compression quality (1–95) |
| `--log-level` | `INFO` | Logging verbosity |

### receiver/main.py

| Flag | Default | Description |
|---|---|---|
| `--ndi-name` | `ScreenCapture` | NDI source name to receive |
| `--port` | `9002` | TCP port to serve frames on |
| `--quality` | `85` | JPEG compression quality (1–95) |
| `--log-level` | `INFO` | Logging verbosity |

### displayer/main.py

| Flag | Default | Description |
|---|---|---|
| `--host` | `127.0.0.1` | TCP server host |
| `--port` | `9002` | TCP server port |
| `--led-rows` | `64` | Rows per matrix panel |
| `--led-cols` | `64` | Columns per matrix panel |
| `--led-chain` | `3` | Number of panels chained in series |
| `--led-parallel` | `3` | Number of parallel chains |
| `--led-brightness` | `100` | Brightness (0–100) |
| `--led-pwm-bits` | `7` | PWM bit depth |
| `--led-pwm-dither-bits` | `1` | PWM dither bits |
| `--led-pwm-lsb-nanoseconds` | `50` | LSB pulse width in nanoseconds |
| `--led-slowdown-gpio` | `3` | GPIO slowdown (increase if display glitches) |
| `--led-hardware-mapping` | `regular` | Pin mapping name |
| `--led-pixel-mapper` | | Pixel mapper string |
| `--led-show-refresh` | | Print refresh rate to stdout |
| `--led-limit-refresh` | `0` | Cap refresh rate in Hz (0 = unlimited) |

## Notes

- The displayer must be run with `sudo` because the `rpi-rgb-led-matrix` library requires direct GPIO/DMA access.
- Move the mouse on the Mac to pan the captured region across your screen.
- Reduce `--capture-fraction` for a tighter crop or increase `--fps` for smoother motion (at the cost of bandwidth).
- If the display shows glitches, increase `--led-slowdown-gpio` (try 4 or 5).
