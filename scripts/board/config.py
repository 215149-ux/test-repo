"""
Board HAL configuration.

Edit the pin numbers here to match your wiring.
All numbers are BCM GPIO numbers (not physical pin numbers).

Current setup: 3x3 prototype.
To scale to the full 8x8 board later, simply extend ROW_PINS and COL_PINS
to 8 entries each. Everything else adapts automatically.
"""

# ============================================================
# Matrix
# ============================================================
# Row pins are DRIVEN by the Pi (outputs when active, INPUT/high-Z
# when inactive). Column pins are READ with internal pull-ups.
#
# IMPORTANT: put a diode in series with every reed switch
# (anode on the row side, cathode on the column side, consistent
# across all 64 switches) to prevent ghosting. For 3x3 it may
# accidentally work without diodes; for 8x8 it is mandatory.
#
# --- 3x3 prototype (adjust to your actual wiring) -----------
ROW_PINS = [4, 17, 27]        # top -> bottom
COL_PINS = [22, 23, 24]       # left -> right

# --- 8x8 (example, use later) -------------------------------
# ROW_PINS = [4, 17, 27, 22, 5, 6, 13, 19]
# COL_PINS = [12, 16, 20, 21, 26, 25, 24, 23]

# ============================================================
# Polarity
# ============================================================
# With the wiring above:
#   - active row is driven LOW
#   - inactive rows are high-Z (INPUT)
#   - columns have internal pull-ups
#   => column reads LOW when a magnet closes the reed switch
#
# Set ACTIVE_LOW = False only if your wiring is inverted.
ACTIVE_LOW = True

# ============================================================
# Timing
# ============================================================
SCAN_PERIOD_S   = 0.010    # full-matrix scan period (100 Hz)
ROW_SETTLE_US   = 50       # settle time after switching row (microseconds)
DEBOUNCE_MS     = 40       # reading must be stable this long before emitting

# ============================================================
# WS2812 LEDs
# ============================================================
# One LED per square, under the board.
LED_PIN        = 18        # BCM18 (PWM0) – required by rpi_ws281x
LED_BRIGHTNESS = 80        # 0..255  (keep low for bench testing)
LED_FREQ_HZ    = 800_000
LED_DMA        = 10
LED_INVERT     = False
LED_CHANNEL    = 0

# How LEDs are physically wired under the grid.
# If you daisy-chain row-by-row with every other row reversed
# (the common way), set True. If each row starts fresh from the
# same side, set False.
#
#   SNAKE = True:          SNAKE = False:
#   row 0:  0  1  2        row 0:  0  1  2
#   row 1:  5  4  3        row 1:  3  4  5
#   row 2:  6  7  8        row 2:  6  7  8
LED_SNAKE = True
