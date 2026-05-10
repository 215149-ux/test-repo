#!/usr/bin/env python3
"""
3x3 prototype live test for the chess-board HAL.

Real hardware (Raspberry Pi 3B):
    cd scripts
    sudo python3 demo_3x3.py          # sudo needed for WS2812 DMA

Dev machine (Ubuntu, no hardware):
    cd scripts
    python3 demo_3x3.py               # auto-detects, runs in simulator

Simulator commands (when no real GPIO is present):

    1..9              toggle cell. numbering is row-major:
                         1 2 3
                         4 5 6
                         7 8 9
    place <r> <c>     place a piece at (row, col), 0-indexed
    lift  <r> <c>     lift the piece at (row, col)
    fill              place all
    clear             lift all
    show              print current grid
    led  <r> <c> <color>   manually set an LED (for LED mapping test)
                           colors: off red green blue yellow white purple cyan
    leds <color>           fill all LEDs with one color
    help              this help
    quit / q          exit
"""

import argparse
import os
import shlex
import sys
import time

# allow "from board import ..." when running as ./demo_3x3.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from board import config, gpio_backend as gpio
from board.matrix_scanner    import MatrixScanner, CellEvent
from board.occupancy_tracker import OccupancyTracker
from board.led_controller    import LEDController


ROWS = len(config.ROW_PINS)
COLS = len(config.COL_PINS)


# ─── helpers ────────────────────────────────────────────────
def fmt_grid(grid):
    header = "     " + "  ".join(f"c{c}" for c in range(COLS))
    lines  = [header]
    for r, row in enumerate(grid):
        cells = ["●" if v else "·" for v in row]
        lines.append(f"  r{r}  " + "   ".join(cells))
    return "\n".join(lines)


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


# ─── main ───────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__)
    ap.add_argument("--sim",    action="store_true",
                    help="force simulator REPL (auto-on if no GPIO backend)")
    ap.add_argument("--no-led", action="store_true",
                    help="disable LED output")
    args = ap.parse_args()

    sim_mode = args.sim or gpio.IS_MOCK

    print(f"[backend ] {gpio.BACKEND_NAME}")
    print(f"[matrix  ] {ROWS} x {COLS}")
    print(f"[rows    ] BCM {config.ROW_PINS}")
    print(f"[cols    ] BCM {config.COL_PINS}")
    print(f"[scan    ] {int(1/config.SCAN_PERIOD_S)} Hz, debounce {config.DEBOUNCE_MS} ms")

    tracker = OccupancyTracker(ROWS, COLS)

    # LEDs
    leds = None
    if not args.no_led:
        try:
            leds = LEDController(ROWS, COLS)
            print(f"[leds    ] {leds.backend}, {leds.count} pixels on BCM{config.LED_PIN}")
            # quick boot flash
            leds.fill(LEDController.BLUE);  time.sleep(0.15)
            leds.fill(LEDController.GREEN); time.sleep(0.15)
            leds.clear()
        except Exception as e:
            print(f"[leds    ] init failed: {e}  (continuing without)")
            leds = None
    else:
        print("[leds    ] disabled")

    # Event handler: update tracker + LED feedback + print
    def on_event(ev: CellEvent):
        tracker.on_cell_event(ev)
        tag = "PLACE" if ev.state == 1 else "LIFT "
        print(f"  [{tag}] r{ev.row} c{ev.col}   (t={ev.timestamp:.3f})   occupied={tracker.count_occupied()}/{ROWS*COLS}")
        if leds:
            leds.set(ev.row, ev.col,
                     LEDController.GREEN if ev.state == 1 else LEDController.OFF)
            leds.show()

    scanner = MatrixScanner(on_event=on_event)
    scanner.start()

    try:
        if not sim_mode:
            print("\nscanning... put / lift a magnet on a square, or Ctrl-C to stop.\n")
            while True:
                time.sleep(1)
            return

        # ── simulator REPL ──
        print(
            "\nsimulator REPL (no real hardware).\n"
            "type `help` for commands, `quit` to exit.\n"
        )

        closed = set()

        def close_switch(r, c):
            gpio.mock_close(config.ROW_PINS[r], config.COL_PINS[c])
            closed.add((r, c))

        def open_switch(r, c):
            gpio.mock_open(config.ROW_PINS[r], config.COL_PINS[c])
            closed.discard((r, c))

        def toggle(r, c):
            if (r, c) in closed: open_switch(r, c)
            else:                close_switch(r, c)

        while True:
            try:
                raw = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not raw:
                continue
            parts = shlex.split(raw.lower())
            cmd   = parts[0]

            if cmd in ("q", "quit", "exit"):
                break

            elif cmd in ("h", "help", "?"):
                print(__doc__.split("Simulator commands", 1)[1]
                      if "Simulator commands" in __doc__ else __doc__)

            elif cmd == "show":
                print(fmt_grid(tracker.snapshot()))

            elif cmd == "clear":
                for (r, c) in list(closed):
                    open_switch(r, c)

            elif cmd == "fill":
                for r in range(ROWS):
                    for c in range(COLS):
                        if (r, c) not in closed:
                            close_switch(r, c)

            elif cmd == "place" and len(parts) == 3:
                r, c = int(parts[1]), int(parts[2])
                if 0 <= r < ROWS and 0 <= c < COLS:
                    close_switch(r, c)
                else:
                    print("out of range")

            elif cmd == "lift" and len(parts) == 3:
                r, c = int(parts[1]), int(parts[2])
                if 0 <= r < ROWS and 0 <= c < COLS:
                    open_switch(r, c)
                else:
                    print("out of range")

            elif cmd == "led" and len(parts) == 4 and leds:
                r, c = int(parts[1]), int(parts[2])
                color = COLOR_MAP.get(parts[3])
                if color is None:
                    print("colors: " + ", ".join(COLOR_MAP.keys()))
                elif 0 <= r < ROWS and 0 <= c < COLS:
                    leds.set(r, c, color); leds.show()
                else:
                    print("out of range")

            elif cmd == "leds" and len(parts) == 2 and leds:
                color = COLOR_MAP.get(parts[1])
                if color is None:
                    print("colors: " + ", ".join(COLOR_MAP.keys()))
                else:
                    leds.fill(color)

            elif cmd.isdigit():
                n = int(cmd) - 1
                if 0 <= n < ROWS * COLS:
                    toggle(n // COLS, n % COLS)
                else:
                    print(f"cell number out of range 1..{ROWS*COLS}")

            else:
                print("unknown command; type `help`")

            # let the debouncer commit between commands
            time.sleep((config.DEBOUNCE_MS + 15) / 1000.0)

    except KeyboardInterrupt:
        pass
    finally:
        scanner.stop()
        if leds:
            leds.clear()
        gpio.cleanup()
        print("\nstopped.")


if __name__ == "__main__":
    main()
