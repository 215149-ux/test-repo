#!/usr/bin/env python3
"""
Chess board HAL — single-file edition.

Everything in one place for easy 3x3 prototyping:
  - GPIO backend (RPi.GPIO on the Pi, auto-falls back to a mock on Ubuntu)
  - Matrix scanner with per-cell debouncing
  - Occupancy tracker (current board state + event pub/sub)
  - WS2812 LED controller (rpi_ws281x with mock fallback)
  - Live REPL demo

Run:
    # Ubuntu dev machine  -> auto-mock + simulator REPL
    python3 chess_board.py

    # Raspberry Pi 3B     -> real GPIO + real WS2812 (sudo for DMA)
    sudo python3 chess_board.py

Scale to 8x8 later by editing ROW_PINS and COL_PINS below — nothing else.
"""

import argparse
import shlex
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple


# ═════════════════════════════════════════════════════════════════════
#  CONFIG — edit these to match your wiring
# ═════════════════════════════════════════════════════════════════════

# --- Matrix pins (BCM numbering) -----------------------------------
# Rows are driven (output LOW when active, INPUT/high-Z when inactive).
# Cols are read with internal pull-up.
#
# IMPORTANT for >3x3: put a diode in series with every reed switch
# (same direction for all of them), otherwise you will get "ghost"
# readings when 3 switches form an L in the matrix.
#
# 3x3 prototype:
ROW_PINS = [4, 17, 27]        # top -> bottom
COL_PINS = [22, 23, 24]       # left -> right

# 8x8 (use later):
# ROW_PINS = [4, 17, 27, 22, 5, 6, 13, 19]
# COL_PINS = [12, 16, 20, 21, 26, 25, 24, 23]

# --- Polarity ------------------------------------------------------
# True  => active row driven LOW, columns pulled-up, reed closing reads 0
# False => flip if your wiring is inverted
ACTIVE_LOW = True

# --- Scan / debounce -----------------------------------------------
SCAN_PERIOD_S = 0.010     # full matrix every 10 ms (100 Hz)
ROW_SETTLE_US = 50        # delay after switching active row
DEBOUNCE_MS   = 40        # reading must be stable this long

# --- WS2812 LEDs ---------------------------------------------------
LED_PIN        = 18       # BCM18 (PWM0) — required by rpi_ws281x
LED_BRIGHTNESS = 80       # 0..255 (keep low on bench)
LED_FREQ_HZ    = 800_000
LED_DMA        = 10
LED_INVERT     = False
LED_CHANNEL    = 0

# Serpentine wiring? (row 0 left->right, row 1 right->left, ...)
# True is the usual choice for LED strips folded under the board.
LED_SNAKE = True


# ═════════════════════════════════════════════════════════════════════
#  GPIO BACKEND (real RPi.GPIO or in-memory mock)
# ═════════════════════════════════════════════════════════════════════

try:
    import RPi.GPIO as _RPI               # type: ignore
    _HAS_RPI = True
except (ImportError, RuntimeError):
    _HAS_RPI = False


class _RPiBackend:
    """Real GPIO via RPi.GPIO."""
    name = "RPi.GPIO"

    def __init__(self):
        _RPI.setmode(_RPI.BCM)
        _RPI.setwarnings(False)
        self._rows: List[int] = []
        self._cols: List[int] = []

    def configure(self, rows, cols):
        self._rows = list(rows)
        self._cols = list(cols)
        for p in self._rows:
            _RPI.setup(p, _RPI.IN)
        for p in self._cols:
            _RPI.setup(p, _RPI.IN, pull_up_down=_RPI.PUD_UP)

    def activate_row(self, pin):
        _RPI.setup(pin, _RPI.OUT)
        _RPI.output(pin, _RPI.LOW)

    def deactivate_row(self, pin):
        _RPI.setup(pin, _RPI.IN)

    def read_col(self, pin):
        return 0 if _RPI.input(pin) == _RPI.LOW else 1

    def cleanup(self):
        for p in self._rows:
            try: _RPI.setup(p, _RPI.IN)
            except Exception: pass
        _RPI.cleanup()


class _MockBackend:
    """Software-driven GPIO, for Ubuntu / CI / simulation."""
    name = "MOCK"

    def __init__(self):
        self._lock = threading.Lock()
        self._active_row: Optional[int] = None
        self._closed: set = set()           # {(row_pin, col_pin)}
        self._rows: List[int] = []
        self._cols: List[int] = []

    def configure(self, rows, cols):
        with self._lock:
            self._rows = list(rows)
            self._cols = list(cols)
            self._active_row = None

    def activate_row(self, pin):
        with self._lock:
            self._active_row = pin

    def deactivate_row(self, pin):
        with self._lock:
            if self._active_row == pin:
                self._active_row = None

    def read_col(self, pin):
        with self._lock:
            if self._active_row is None:
                return 1                    # pull-up wins
            return 0 if (self._active_row, pin) in self._closed else 1

    def cleanup(self):
        with self._lock:
            self._active_row = None

    # ---- simulator hooks (mock only) ----
    def sim_close(self, row_pin, col_pin):
        with self._lock: self._closed.add((row_pin, col_pin))

    def sim_open(self, row_pin, col_pin):
        with self._lock: self._closed.discard((row_pin, col_pin))


# One global backend instance
_gpio = _RPiBackend() if _HAS_RPI else _MockBackend()
IS_MOCK = not _HAS_RPI


# ═════════════════════════════════════════════════════════════════════
#  MATRIX SCANNER + DEBOUNCE
# ═════════════════════════════════════════════════════════════════════

@dataclass
class CellEvent:
    """A debounced transition on a single cell."""
    row: int
    col: int
    state: int          # 1 = occupied, 0 = empty
    timestamp: float

    @property
    def kind(self) -> str:
        return "place" if self.state == 1 else "lift"


class MatrixScanner:
    """Background scanner that emits CellEvent on stable transitions."""

    def __init__(self,
                 row_pins: List[int],
                 col_pins: List[int],
                 on_event: Callable[[CellEvent], None],
                 scan_period_s: float = SCAN_PERIOD_S,
                 debounce_ms: int = DEBOUNCE_MS,
                 active_low: bool = ACTIVE_LOW):
        self.row_pins      = list(row_pins)
        self.col_pins      = list(col_pins)
        self.rows          = len(row_pins)
        self.cols          = len(col_pins)
        self.scan_period_s = scan_period_s
        self.debounce_ms   = debounce_ms
        self.active_low    = active_low
        self.on_event      = on_event

        self._stable = [[0] * self.cols for _ in range(self.rows)]
        self._last   = [[0] * self.cols for _ in range(self.rows)]
        self._since  = [[0.0] * self.cols for _ in range(self.rows)]

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self):
        _gpio.configure(self.row_pins, self.col_pins)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="MatrixScanner")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        for p in self.row_pins:
            try: _gpio.deactivate_row(p)
            except Exception: pass

    def snapshot(self) -> List[List[int]]:
        return [row[:] for row in self._stable]

    def _loop(self):
        settle_s = ROW_SETTLE_US / 1_000_000 if ROW_SETTLE_US > 0 else 0
        while not self._stop.is_set():
            t0 = time.monotonic()
            self._scan_once(settle_s)
            remaining = self.scan_period_s - (time.monotonic() - t0)
            if remaining > 0:
                self._stop.wait(remaining)

    def _scan_once(self, settle_s: float):
        now = time.monotonic()
        for r, rpin in enumerate(self.row_pins):
            _gpio.activate_row(rpin)
            if settle_s > 0:
                time.sleep(settle_s)

            for c, cpin in enumerate(self.col_pins):
                level = _gpio.read_col(cpin)
                occupied = (level == 0) if self.active_low else (level == 1)
                raw = 1 if occupied else 0

                if raw != self._last[r][c]:
                    self._last[r][c]  = raw
                    self._since[r][c] = now

                age_ms = (now - self._since[r][c]) * 1000.0
                if raw != self._stable[r][c] and age_ms >= self.debounce_ms:
                    self._stable[r][c] = raw
                    ev = CellEvent(r, c, raw, now)
                    try:
                        self.on_event(ev)
                    except Exception:
                        import traceback; traceback.print_exc()

            _gpio.deactivate_row(rpin)


# ═════════════════════════════════════════════════════════════════════
#  OCCUPANCY TRACKER
# ═════════════════════════════════════════════════════════════════════

class OccupancyTracker:
    """Holds the current 2D occupancy and fans out events to subscribers."""

    def __init__(self, rows: int, cols: int):
        self.rows = rows
        self.cols = cols
        self._grid: List[List[int]] = [[0] * cols for _ in range(rows)]
        self._subs: List[Callable[[CellEvent], None]] = []

    def on_cell_event(self, ev: CellEvent):
        self._grid[ev.row][ev.col] = ev.state
        for cb in self._subs:
            try: cb(ev)
            except Exception:
                import traceback; traceback.print_exc()

    def subscribe(self, cb: Callable[[CellEvent], None]):
        self._subs.append(cb)

    def snapshot(self) -> List[List[int]]:
        return [row[:] for row in self._grid]

    def at(self, row: int, col: int) -> int:
        return self._grid[row][col]

    def count_occupied(self) -> int:
        return sum(sum(row) for row in self._grid)


# ═════════════════════════════════════════════════════════════════════
#  WS2812 LED CONTROLLER
# ═════════════════════════════════════════════════════════════════════

try:
    from rpi_ws281x import PixelStrip      # type: ignore
    _HAS_WS281X = True
except ImportError:
    _HAS_WS281X = False


class _MockStrip:
    def __init__(self, n):
        self.n      = n
        self.pixels = [(0, 0, 0)] * n

    def setPixelColorRGB(self, i, r, g, b):
        if 0 <= i < self.n:
            self.pixels[i] = (r, g, b)

    def show(self):
        chars = []
        for (r, g, b) in self.pixels:
            if   (r, g, b) == (0, 0, 0):          chars.append(".")
            elif r > max(g, b):                   chars.append("R")
            elif g > max(r, b):                   chars.append("G")
            elif b > max(r, g):                   chars.append("B")
            elif r > 40 and g > 40 and b < 40:    chars.append("Y")
            else:                                 chars.append("W")
        sys.stderr.write("\r[LEDs] " + " ".join(chars) + "   ")
        sys.stderr.flush()

    def begin(self):     pass
    def numPixels(self): return self.n


class LEDController:
    OFF    = (0, 0, 0)
    RED    = (255, 0, 0)
    GREEN  = (0, 255, 0)
    BLUE   = (0, 0, 255)
    YELLOW = (255, 180, 0)
    WHITE  = (100, 100, 100)
    PURPLE = (160, 0, 200)
    CYAN   = (0, 200, 200)

    def __init__(self, rows: int, cols: int, snake: bool = LED_SNAKE):
        self.rows  = rows
        self.cols  = cols
        self.count = rows * cols
        self.snake = snake

        if _HAS_WS281X:
            self._strip = PixelStrip(
                self.count, LED_PIN, LED_FREQ_HZ,
                LED_DMA, LED_INVERT, LED_BRIGHTNESS, LED_CHANNEL)
            self.backend = "rpi_ws281x"
        else:
            self._strip = _MockStrip(self.count)
            self.backend = "MOCK"

        self._strip.begin()
        self.clear()

    def index_of(self, row: int, col: int) -> int:
        if self.snake and (row % 2 == 1):
            return row * self.cols + (self.cols - 1 - col)
        return row * self.cols + col

    def set(self, row: int, col: int, color: Tuple[int, int, int]):
        r, g, b = color
        self._strip.setPixelColorRGB(self.index_of(row, col), r, g, b)

    def show(self):
        self._strip.show()

    def clear(self):
        for i in range(self.count):
            self._strip.setPixelColorRGB(i, 0, 0, 0)
        self._strip.show()

    def fill(self, color):
        for r in range(self.rows):
            for c in range(self.cols):
                self.set(r, c, color)
        self.show()


# ═════════════════════════════════════════════════════════════════════
#  DEMO / REPL
# ═════════════════════════════════════════════════════════════════════

HELP = """\
Simulator commands (mock mode only):

  1..N              toggle cell by number (row-major, 1-indexed)
  place <r> <c>     set cell (row, col) occupied  (0-indexed)
  lift  <r> <c>     set cell (row, col) empty
  fill              place on all cells
  clear             lift from all cells
  show              print current grid

  led <r> <c> <col> set one LED to a color
  leds <col>        fill all LEDs with a color
     colors: off red green blue yellow white purple cyan

  help              this help
  quit / q          exit
"""

COLOR_MAP = {
    "off":    LEDController.OFF,
    "red":    LEDController.RED,
    "green":  LEDController.GREEN,
    "blue":   LEDController.BLUE,
    "yellow": LEDController.YELLOW,
    "white":  LEDController.WHITE,
    "purple": LEDController.PURPLE,
    "cyan":   LEDController.CYAN,
}


def _fmt_grid(grid):
    cols = len(grid[0])
    hdr  = "     " + "  ".join(f"c{c}" for c in range(cols))
    out  = [hdr]
    for r, row in enumerate(grid):
        out.append(f"  r{r}  " + "   ".join("●" if v else "·" for v in row))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__)
    ap.add_argument("--sim",    action="store_true",
                    help="force REPL (auto-on when no real GPIO)")
    ap.add_argument("--no-led", action="store_true",
                    help="disable LED output")
    args = ap.parse_args()

    rows, cols = len(ROW_PINS), len(COL_PINS)
    sim_mode = args.sim or IS_MOCK

    print(f"[backend ] {_gpio.name}")
    print(f"[matrix  ] {rows} x {cols}")
    print(f"[rows    ] BCM {ROW_PINS}")
    print(f"[cols    ] BCM {COL_PINS}")
    print(f"[scan    ] {int(1/SCAN_PERIOD_S)} Hz, debounce {DEBOUNCE_MS} ms")

    # LEDs
    leds = None
    if not args.no_led:
        try:
            leds = LEDController(rows, cols)
            print(f"[leds    ] {leds.backend}, {leds.count} pixels on BCM{LED_PIN}")
            leds.fill(LEDController.BLUE);  time.sleep(0.15)
            leds.fill(LEDController.GREEN); time.sleep(0.15)
            leds.clear()
        except Exception as e:
            print(f"[leds    ] init failed: {e}")
            leds = None
    else:
        print("[leds    ] disabled")

    tracker = OccupancyTracker(rows, cols)

    def on_event(ev: CellEvent):
        tracker.on_cell_event(ev)
        tag = "PLACE" if ev.state == 1 else "LIFT "
        print(f"  [{tag}] r{ev.row} c{ev.col}   occupied={tracker.count_occupied()}/{rows*cols}")
        if leds:
            leds.set(ev.row, ev.col,
                     LEDController.GREEN if ev.state == 1 else LEDController.OFF)
            leds.show()

    scanner = MatrixScanner(ROW_PINS, COL_PINS, on_event)
    scanner.start()

    try:
        if not sim_mode:
            print("\nscanning real GPIO... move a magnet on/off a square. Ctrl-C to stop.\n")
            while True:
                time.sleep(1)
            return

        print("\nsimulator REPL (no real hardware). type `help` for commands.\n")
        closed: set = set()

        def close(r, c):
            _gpio.sim_close(ROW_PINS[r], COL_PINS[c])
            closed.add((r, c))

        def open_(r, c):
            _gpio.sim_open(ROW_PINS[r], COL_PINS[c])
            closed.discard((r, c))

        while True:
            try:
                line = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            parts = shlex.split(line.lower())
            cmd   = parts[0]

            if cmd in ("q", "quit", "exit"):
                break
            elif cmd in ("h", "help", "?"):
                print(HELP)
            elif cmd == "show":
                print(_fmt_grid(tracker.snapshot()))
            elif cmd == "clear":
                for (r, c) in list(closed):
                    open_(r, c)
            elif cmd == "fill":
                for r in range(rows):
                    for c in range(cols):
                        if (r, c) not in closed:
                            close(r, c)
            elif cmd == "place" and len(parts) == 3:
                r, c = int(parts[1]), int(parts[2])
                if 0 <= r < rows and 0 <= c < cols: close(r, c)
                else: print("out of range")
            elif cmd == "lift" and len(parts) == 3:
                r, c = int(parts[1]), int(parts[2])
                if 0 <= r < rows and 0 <= c < cols: open_(r, c)
                else: print("out of range")
            elif cmd == "led" and len(parts) == 4 and leds:
                r, c = int(parts[1]), int(parts[2])
                color = COLOR_MAP.get(parts[3])
                if color is None:
                    print("colors: " + ", ".join(COLOR_MAP))
                elif 0 <= r < rows and 0 <= c < cols:
                    leds.set(r, c, color); leds.show()
                else:
                    print("out of range")
            elif cmd == "leds" and len(parts) == 2 and leds:
                color = COLOR_MAP.get(parts[1])
                if color is None: print("colors: " + ", ".join(COLOR_MAP))
                else:             leds.fill(color)
            elif cmd.isdigit():
                n = int(cmd) - 1
                if 0 <= n < rows * cols:
                    r, c = n // cols, n % cols
                    if (r, c) in closed: open_(r, c)
                    else:                close(r, c)
                else:
                    print(f"cell number out of range 1..{rows*cols}")
            else:
                print("unknown command. type `help`.")

            time.sleep((DEBOUNCE_MS + 15) / 1000.0)

    except KeyboardInterrupt:
        pass
    finally:
        scanner.stop()
        if leds: leds.clear()
        _gpio.cleanup()
        print("\nstopped.")


if __name__ == "__main__":
    main()
