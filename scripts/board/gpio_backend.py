"""
GPIO backend with automatic Raspberry Pi / Mock detection.

- On a Raspberry Pi with RPi.GPIO: uses real GPIO.
- Elsewhere (Ubuntu dev machine): falls back to an in-memory mock
  that can be driven by the simulator.

Rows use a 3-state scheme:
    activate_row(pin)   -> drive LOW   (active)
    deactivate_row(pin) -> set INPUT   (high-Z, inactive)

This prevents shorts between two outputs when multiple reed
switches on the same column are closed at once (which can happen
during a game).

Columns are always INPUTs with internal pull-up. read_col() returns
0 if the line is LOW (a magnet has closed the reed switch on the
active row) and 1 otherwise.
"""

import threading

# ---- Try to import the real library ----
_BACKEND = "mock"
try:
    import RPi.GPIO as _RPI          # type: ignore
    _BACKEND = "rpi"
except (ImportError, RuntimeError):
    _BACKEND = "mock"


# ─────────────────────────────────────────────────────────────
#  Real Pi backend
# ─────────────────────────────────────────────────────────────
class _RPiBackend:
    name = "RPi.GPIO"

    def __init__(self):
        _RPI.setmode(_RPI.BCM)
        _RPI.setwarnings(False)
        self._row_pins = []
        self._col_pins = []

    def configure_matrix(self, row_pins, col_pins):
        self._row_pins = list(row_pins)
        self._col_pins = list(col_pins)
        # Rows: start as INPUT (high-Z, inactive)
        for p in self._row_pins:
            _RPI.setup(p, _RPI.IN)
        # Columns: INPUT with internal pull-up
        for p in self._col_pins:
            _RPI.setup(p, _RPI.IN, pull_up_down=_RPI.PUD_UP)

    def activate_row(self, pin):
        _RPI.setup(pin, _RPI.OUT)
        _RPI.output(pin, _RPI.LOW)

    def deactivate_row(self, pin):
        _RPI.setup(pin, _RPI.IN)

    def read_col(self, pin):
        return 0 if _RPI.input(pin) == _RPI.LOW else 1

    def cleanup(self):
        for p in self._row_pins:
            try: _RPI.setup(p, _RPI.IN)
            except Exception: pass
        _RPI.cleanup()


# ─────────────────────────────────────────────────────────────
#  Mock backend (Ubuntu / CI)
# ─────────────────────────────────────────────────────────────
class _MockBackend:
    name = "MOCK"

    def __init__(self):
        self._lock        = threading.Lock()
        self._active_row  = None              # only one active at a time
        self._closed_sw   = set()             # {(row_pin, col_pin) closed}
        self._row_pins    = []
        self._col_pins    = []

    def configure_matrix(self, row_pins, col_pins):
        with self._lock:
            self._row_pins   = list(row_pins)
            self._col_pins   = list(col_pins)
            self._active_row = None
            # Note: we DO NOT clear _closed_sw here, so a simulator
            # can pre-populate state before start.

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
                return 1   # pull-up wins
            return 0 if (self._active_row, pin) in self._closed_sw else 1

    def cleanup(self):
        with self._lock:
            self._active_row = None

    # --- simulator hooks (mock only) -------------------------
    def sim_close(self, row_pin, col_pin):
        with self._lock:
            self._closed_sw.add((row_pin, col_pin))

    def sim_open(self, row_pin, col_pin):
        with self._lock:
            self._closed_sw.discard((row_pin, col_pin))

    def sim_clear(self):
        with self._lock:
            self._closed_sw.clear()


# ─────────────────────────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────────────────────────
if _BACKEND == "rpi":
    _backend = _RPiBackend()
    IS_MOCK  = False
else:
    _backend = _MockBackend()
    IS_MOCK  = True

BACKEND_NAME = _backend.name


def configure(row_pins, col_pins):
    _backend.configure_matrix(row_pins, col_pins)

def activate_row(pin):
    _backend.activate_row(pin)

def deactivate_row(pin):
    _backend.deactivate_row(pin)

def read_col(pin):
    return _backend.read_col(pin)

def cleanup():
    _backend.cleanup()


# --- Mock-only helpers (no-ops on real Pi) ---
def mock_close(row_pin, col_pin):
    if IS_MOCK:
        _backend.sim_close(row_pin, col_pin)

def mock_open(row_pin, col_pin):
    if IS_MOCK:
        _backend.sim_open(row_pin, col_pin)

def mock_clear():
    if IS_MOCK:
        _backend.sim_clear()
