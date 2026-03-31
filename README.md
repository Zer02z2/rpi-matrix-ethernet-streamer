# rpi-matrix-ethernet-streamer

Stream a Mac screen region to an RGB LED matrix **and/or** a Colorlight 5A-75B LED receiver card via a Raspberry Pi.

## Architecture

```
Mac                       Pi (eth0 ← NDI over switch)
┌──────────────────┐      ┌──────────────────────────────────────┐
│ sender/main.py   │─NDI─▶│ receiver/main.py                     │
│ (screen capture) │      │ (NDI → UDP multicast 239.0.0.1:9002) │
└──────────────────┘      └──────────┬───────────────────────────┘
                                     │ UDP multicast
                          ┌──────────┴───────────────────────────┐
                          │                                       │
                          ▼                                       ▼
              displayer/main.py                    receiver/fpp_bridge.py
              (RGB LED matrix)                     (DDP → FPP → Colorlight 5A-75B)
```

- **NDI** handles source discovery automatically — no IP addresses needed.
- `receiver/main.py` multicasts frames to `239.0.0.1:9002`; both `displayer` and `fpp_bridge` read from the same stream and can run simultaneously.

---

## Installation

### Mac — NDI Sender

Install the [NDI Tools runtime](https://ndi.video/tools/), then:

```bash
cd sender
pip install -r requirements.txt
```

### Raspberry Pi — Receiver, Displayer, FPP Bridge

**1. Install avahi (required for NDI discovery):**

```bash
sudo apt install avahi-daemon
```

**2. Install Python dependencies:**

```bash
cd receiver
pip install -r requirements.txt
```

**3. Install the rpi-rgb-led-matrix library** (required for `displayer/main.py` only):

```bash
git clone https://github.com/hzeller/rpi-rgb-led-matrix.git
cd rpi-rgb-led-matrix
make build-python PYTHON=$(which python3)
sudo make install-python PYTHON=$(which python3)
```

**4. Install Falcon Player** (required for `fpp_bridge.py` only):

Follow the [FPP install guide](https://github.com/FalconChristmas/fpp). After installing, configure it once in the FPP web UI:
1. Input/Output → Add Input → **DDP Pixels** (port 4048)
2. Input/Output → Add Output → **Colorlight** → select the direct-cable interface (e.g. `eth1`)
3. Map the DDP input channels to the Colorlight output.

---

## Usage

### Step 1 — Mac: start the NDI sender

```bash
cd sender
python main.py --ndi-name ScreenCapture --capture-fraction 0.5 --ndi-output-size 480
```

### Step 2 — Pi: start the NDI receiver

```bash
cd receiver
LC_ALL=C.UTF-8 python main.py --ndi-name ScreenCapture
```

### Step 3 — Pi: start whichever outputs you need

**RGB LED matrix:**

```bash
cd displayer
sudo python main.py --led-rows 64 --led-cols 64 --led-chain 3 --led-parallel 3
```

**Colorlight 5A-75B via FPP:**

```bash
cd receiver
python fpp_bridge.py --width 256 --height 128
```

Both outputs can run at the same time.

---

## Configuration Reference

### sender/main.py

| Flag | Default | Description |
|---|---|---|
| `--ndi-name` | `ScreenCapture` | NDI broadcast name |
| `--capture-fraction` | `0.5` | Capture region as fraction of monitor height |
| `--ndi-output-size` | `480` | NDI output frame size in pixels (square) |
| `--fps` | `30` | Target frame rate |

### receiver/main.py

| Flag | Default | Description |
|---|---|---|
| `--ndi-name` | `ScreenCapture` | NDI source name to search for |
| `--mcast-group` | `239.0.0.1` | UDP multicast group |
| `--port` | `9002` | UDP port |
| `--quality` | `85` | JPEG compression quality (1–95) |

### displayer/main.py

| Flag | Default | Description |
|---|---|---|
| `--mcast-group` | `239.0.0.1` | UDP multicast group to join |
| `--port` | `9002` | UDP port |
| `--led-rows` | `64` | Rows per panel |
| `--led-cols` | `64` | Columns per panel |
| `--led-chain` | `3` | Panels chained in series |
| `--led-parallel` | `3` | Parallel chains |
| `--led-brightness` | `80` | Brightness (0–100) |
| `--led-slowdown-gpio` | `3` | Increase to 4–5 if display glitches |

### receiver/fpp_bridge.py

| Flag | Default | Description |
|---|---|---|
| `--mcast-group` | `239.0.0.1` | UDP multicast group to join |
| `--port` | `9002` | UDP port |
| `--fpp-host` | `127.0.0.1` | Host running FPP |
| `--fpp-port` | `4048` | FPP DDP input port |
| `--width` | `256` | Colorlight display width in pixels |
| `--height` | `128` | Colorlight display height in pixels |

---

## Notes

- `displayer/main.py` must run with `sudo` (requires GPIO/DMA access).
- `receiver/main.py` must run with `LC_ALL=C.UTF-8` on some Pi OS versions to avoid NDI locale issues.
- If the LED matrix glitches, increase `--led-slowdown-gpio` to 4 or 5.
- Move the mouse on the Mac to pan the captured region.
