"""
Matrix scanner with per-cell debouncing.

Runs a background thread that scans the full matrix at SCAN_PERIOD_S
and emits a CellEvent whenever a cell's *debounced* state changes.
"""

import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from . import config
from . import gpio_backend as gpio


@dataclass
class CellEvent:
    """A debounced transition on a single cell."""
    row: int
    col: int
    state: int          # 1 = occupied, 0 = empty
    timestamp: float    # time.monotonic() when the event was committed

    @property
    def kind(self) -> str:
        return "place" if self.state == 1 else "lift"


class MatrixScanner:
    def __init__(self,
                 row_pins: Optional[List[int]] = None,
                 col_pins: Optional[List[int]] = None,
                 scan_period_s: Optional[float] = None,
                 debounce_ms: Optional[int] = None,
                 on_event: Optional[Callable[[CellEvent], None]] = None):
        self.row_pins      = list(row_pins or config.ROW_PINS)
        self.col_pins      = list(col_pins or config.COL_PINS)
        self.rows          = len(self.row_pins)
        self.cols          = len(self.col_pins)
        self.scan_period_s = scan_period_s if scan_period_s is not None else config.SCAN_PERIOD_S
        self.debounce_ms   = debounce_ms   if debounce_ms   is not None else config.DEBOUNCE_MS
        self.on_event      = on_event

        # Per-cell debounce state
        self._stable = [[0] * self.cols for _ in range(self.rows)]
        self._last   = [[0] * self.cols for _ in range(self.rows)]
        self._since  = [[0.0] * self.cols for _ in range(self.rows)]

        self._thread  = None
        self._stop    = threading.Event()
        self._started = False

    # ── lifecycle ────────────────────────────────────────────
    def start(self):
        if self._started:
            return
        gpio.configure(self.row_pins, self.col_pins)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="MatrixScanner")
        self._thread.start()
        self._started = True

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        for p in self.row_pins:
            try: gpio.deactivate_row(p)
            except Exception: pass
        self._started = False

    # ── accessors ────────────────────────────────────────────
    def snapshot(self):
        """A copy of the current *stable* occupancy grid."""
        return [row[:] for row in self._stable]

    # ── core loop ────────────────────────────────────────────
    def _loop(self):
        settle_s = config.ROW_SETTLE_US / 1_000_000 if config.ROW_SETTLE_US > 0 else 0
        while not self._stop.is_set():
            t0 = time.monotonic()
            self._scan_once(settle_s)
            elapsed = time.monotonic() - t0
            sleep_for = self.scan_period_s - elapsed
            if sleep_for > 0:
                self._stop.wait(sleep_for)

    def _scan_once(self, settle_s: float):
        now = time.monotonic()

        for r, rpin in enumerate(self.row_pins):
            gpio.activate_row(rpin)
            if settle_s > 0:
                time.sleep(settle_s)

            for c, cpin in enumerate(self.col_pins):
                level = gpio.read_col(cpin)
                occupied = (level == 0) if config.ACTIVE_LOW else (level == 1)
                raw = 1 if occupied else 0

                # Raw change resets the "stable since" timer for this cell
                if raw != self._last[r][c]:
                    self._last[r][c]  = raw
                    self._since[r][c] = now

                age_ms = (now - self._since[r][c]) * 1000.0
                if raw != self._stable[r][c] and age_ms >= self.debounce_ms:
                    self._stable[r][c] = raw
                    ev = CellEvent(row=r, col=c, state=raw, timestamp=now)
                    if self.on_event:
                        try:
                            self.on_event(ev)
                        except Exception:
                            import traceback; traceback.print_exc()

            gpio.deactivate_row(rpin)
