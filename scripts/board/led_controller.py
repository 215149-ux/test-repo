"""
WS2812 LED controller with mock fallback.

- If rpi_ws281x is available (Pi): drives the real LED strip via DMA.
  Running usually requires root (sudo).
- Otherwise: prints a one-line ASCII representation of the strip to
  stderr, so you can see what would light up on a dev machine.
"""

import sys
from typing import Tuple

from . import config


# ─── rpi_ws281x optional import ─────────────────────────────
try:
    from rpi_ws281x import PixelStrip
    _HAS_WS281X = True
except ImportError:
    _HAS_WS281X = False


# ─── Mock strip ─────────────────────────────────────────────
class _MockStrip:
    def __init__(self, n):
        self.n       = n
        self.pixels  = [(0, 0, 0)] * n

    def setPixelColorRGB(self, i, r, g, b):
        if 0 <= i < self.n:
            self.pixels[i] = (r, g, b)

    def show(self):
        chars = []
        for (r, g, b) in self.pixels:
            if (r, g, b) == (0, 0, 0):          chars.append(".")
            elif r > max(g, b):                  chars.append("R")
            elif g > max(r, b):                  chars.append("G")
            elif b > max(r, g):                  chars.append("B")
            elif r > 40 and g > 40 and b < 40:   chars.append("Y")
            else:                                chars.append("W")
        sys.stderr.write("\r[LEDs] " + " ".join(chars) + "   ")
        sys.stderr.flush()

    def begin(self):     pass
    def numPixels(self): return self.n


# ─── Controller ─────────────────────────────────────────────
class LEDController:
    # Palette
    OFF    = (0, 0, 0)
    RED    = (255, 0, 0)
    GREEN  = (0, 255, 0)
    BLUE   = (0, 0, 255)
    YELLOW = (255, 180, 0)
    WHITE  = (100, 100, 100)
    PURPLE = (160, 0, 200)
    CYAN   = (0, 200, 200)

    def __init__(self, rows=None, cols=None, snake=None):
        self.rows  = rows  if rows  is not None else len(config.ROW_PINS)
        self.cols  = cols  if cols  is not None else len(config.COL_PINS)
        self.count = self.rows * self.cols
        self.snake = snake if snake is not None else config.LED_SNAKE

        if _HAS_WS281X:
            self._strip = PixelStrip(
                self.count,
                config.LED_PIN,
                config.LED_FREQ_HZ,
                config.LED_DMA,
                config.LED_INVERT,
                config.LED_BRIGHTNESS,
                config.LED_CHANNEL,
            )
            self.backend = "rpi_ws281x"
        else:
            self._strip = _MockStrip(self.count)
            self.backend = "MOCK"

        self._strip.begin()
        self.clear()

    # ── mapping ──
    def index_of(self, row: int, col: int) -> int:
        if self.snake and (row % 2 == 1):
            return row * self.cols + (self.cols - 1 - col)
        return row * self.cols + col

    # ── primitives ──
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
