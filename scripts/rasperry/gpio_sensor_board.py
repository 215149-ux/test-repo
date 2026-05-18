#!/usr/bin/env python3
"""
gpio_sensor_board.py
====================
قارئ مصفوفة ريد سويتشات 8x8 من GPIO على Raspberry Pi 3 Model B.

التشغيل المباشر (اختبار بدون ROS):
    python3 gpio_sensor_board.py

    يعرض المصفوفة بشكل حيّ على التيرمينال كل 0.3 ثانية.
    Ctrl+C للإيقاف.

التوصيل:
  - 8 أعمدة (COLS) → Output pins: يُفعّل عمود واحد بـ LOW في كل مرة.
  - 8 صفوف (ROWS) → Input pins مع Pull-up داخلي:
    لما السويتش مغلق (مغناطيس = قطعة موجودة) → القراءة LOW = 1 (piece present).
    لما السويتش مفتوح (لا مغناطيس) → القراءة HIGH = 0 (empty).

الترتيب:
  COL 0 = file 'a'، COL 7 = file 'h'
  ROW 0 = rank 8 (أسود)، ROW 7 = rank 1 (أبيض)
"""

import time
import os
import sys

try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None
    print("[ERROR] RPi.GPIO not available!")
    print("        This script must run on a Raspberry Pi.")
    print("        Install: sudo apt install python3-rpi.gpio")
    sys.exit(1)


# ============================================================================
#  Pin Assignment — عدّل حسب توصيلتك
# ============================================================================

# الصفوف (ROWS) — Input pins مع Pull-up داخلي
# ROW 0 = rank 8 (أسود)، ROW 7 = rank 1 (أبيض)
ROW_PINS = [4, 17, 27, 22, 5, 6, 13, 19]

# الأعمدة (COLS) — Output pins
# COL 0 = file 'a'، COL 7 = file 'h'
COL_PINS = [26, 21, 20, 16, 12, 25, 24, 23]

# تأخير بعد تفعيل العمود (بالثواني)
SCAN_DELAY_S = 0.00025


# ============================================================================
#  GPIOSensorBoard
# ============================================================================
class GPIOSensorBoard:
    """
    يقرأ مصفوفة ريد سويتشات 8x8 من GPIO.
    واجهة scan() ترجع مصفوفة 8x8 (قوائم قوائم) من 0/1.
    """

    def __init__(self, row_pins=None, col_pins=None, scan_delay=None):
        self.row_pins = row_pins or ROW_PINS
        self.col_pins = col_pins or COL_PINS
        self.scan_delay = scan_delay or SCAN_DELAY_S

        if len(self.row_pins) != 8 or len(self.col_pins) != 8:
            raise ValueError("Need exactly 8 row pins and 8 column pins.")

        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)

        # الصفوف: Input مع Pull-up داخلي
        for pin in self.row_pins:
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

        # الأعمدة: Output — ابتدائياً كلها HIGH (معطّلة)
        for pin in self.col_pins:
            GPIO.setup(pin, GPIO.OUT)
            GPIO.output(pin, GPIO.HIGH)

    def scan(self):
        """
        يمسح المصفوفة 8x8 ويرجع occupancy:
          board[row][col] = 1 إذا القطعة موجودة (السويتش مغلق)
          board[row][col] = 0 إذا الخانة فاضية (السويتش مفتوح)
        """
        board = [[0] * 8 for _ in range(8)]

        for col_idx, col_pin in enumerate(self.col_pins):
            # تفعيل العمود الحالي (LOW)
            GPIO.output(col_pin, GPIO.LOW)
            time.sleep(self.scan_delay)

            # قراءة كل الصفوف
            for row_idx, row_pin in enumerate(self.row_pins):
                if GPIO.input(row_pin) == GPIO.LOW:
                    board[row_idx][col_idx] = 1

            # إطفاء العمود (HIGH)
            GPIO.output(col_pin, GPIO.HIGH)

        return board

    def cleanup(self):
        """يُنظّف GPIO عند الإغلاق."""
        GPIO.cleanup()

    # واجهة التوافق (لا تُستخدم — فقط لمنع AttributeError)
    def set_square(self, sq, value): pass
    def apply_changes(self, changes): pass
    def load_from_chess_board(self, chess_board): pass


# ============================================================================
#  عرض اللوحة بشكل واضح
# ============================================================================
def clear_screen():
    """مسح الشاشة."""
    os.system('clear')


def print_board_live(board, scan_count, changes):
    """
    يطبع اللوحة بشكل مصفوفة 8x8 واضحة مع:
    - أرقام الصفوف والأعمدة
    - عدّ القطع
    - إظهار التغييرات عن آخر قراءة
    """
    FILES = 'abcdefgh'
    piece_count = sum(cell for row in board for cell in row)

    # ألوان ANSI
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

    clear_screen()

    print(f"{BOLD}{CYAN}╔══════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}{CYAN}║   Reed-Switch Matrix 8x8 — Live Monitor     ║{RESET}")
    print(f"{BOLD}{CYAN}╠══════════════════════════════════════════════╣{RESET}")
    print(f"{CYAN}║  1 = piece detected    . = empty             ║{RESET}")
    print(f"{CYAN}║  Ctrl+C to stop                              ║{RESET}")
    print(f"{CYAN}╚══════════════════════════════════════════════╝{RESET}")
    print()

    # Header
    print(f"       {BOLD}", end="")
    for f in FILES:
        print(f"  {f} ", end="")
    print(f"{RESET}")
    print(f"     ┌{'────┬' * 7}────┐")

    # Board
    for row_idx in range(8):
        rank = 8 - row_idx
        print(f"  {BOLD}{rank}{RESET}  │", end="")
        for col_idx in range(8):
            val = board[row_idx][col_idx]
            sq_name = f"{FILES[col_idx]}{rank}"

            if val == 1:
                # قطعة موجودة
                if sq_name in changes and changes[sq_name] == 'appeared':
                    # ظهرت الآن (جديدة)
                    print(f" {YELLOW}{BOLD}1{RESET} │", end="")
                else:
                    print(f" {GREEN}{BOLD}1{RESET} │", end="")
            else:
                # فاضية
                if sq_name in changes and changes[sq_name] == 'disappeared':
                    # اختفت الآن
                    print(f" {RED}{BOLD}0{RESET} │", end="")
                else:
                    print(f" {DIM}.{RESET} │", end="")

        print(f"  {BOLD}{rank}{RESET}")

        if row_idx < 7:
            print(f"     ├{'────┼' * 7}────┤")
        else:
            print(f"     └{'────┴' * 7}────┘")

    # Footer
    print(f"       {BOLD}", end="")
    for f in FILES:
        print(f"  {f} ", end="")
    print(f"{RESET}")
    print()

    # إحصائيات
    status_color = GREEN if piece_count == 32 else (YELLOW if piece_count > 0 else RED)
    print(f"  Pieces detected: {status_color}{BOLD}{piece_count}/32{RESET}")
    print(f"  Scan count:      {scan_count}")

    if changes:
        change_list = [f"{sq}({'+'if v=='appeared' else '-'})"
                       for sq, v in changes.items()]
        print(f"  Last change:     {YELLOW}{' '.join(change_list[:8])}{RESET}")
    else:
        print(f"  Last change:     {DIM}(none){RESET}")

    # حالة التوصيل
    if piece_count == 0:
        print(f"\n  {RED}⚠  No pieces detected! Check wiring.{RESET}")
    elif piece_count == 32:
        print(f"\n  {GREEN}✓  All 32 pieces in place — ready to play!{RESET}")
    print()

    # عرض المصفوفة الخام (للتشخيص)
    print(f"  {DIM}Raw matrix:{RESET}")
    for row in board:
        print(f"  {DIM}{row}{RESET}")


# ============================================================================
#  Main — اختبار مستقل
# ============================================================================
if __name__ == '__main__':
    print("Initializing GPIO...")
    sensor = GPIOSensorBoard()
    print("GPIO initialized. Starting scan loop...")
    time.sleep(0.5)

    scan_count = 0
    prev_board = [[0] * 8 for _ in range(8)]
    FILES = 'abcdefgh'

    try:
        while True:
            board = sensor.scan()
            scan_count += 1

            # اكتشاف التغييرات عن الدورة السابقة
            changes = {}
            for r in range(8):
                for c in range(8):
                    sq = f"{FILES[c]}{8 - r}"
                    if prev_board[r][c] == 0 and board[r][c] == 1:
                        changes[sq] = 'appeared'
                    elif prev_board[r][c] == 1 and board[r][c] == 0:
                        changes[sq] = 'disappeared'

            # عرض
            print_board_live(board, scan_count, changes)

            # تخزين للمقارنة
            prev_board = [row[:] for row in board]

            time.sleep(0.3)

    except KeyboardInterrupt:
        print("\n\nStopping...")
    finally:
        sensor.cleanup()
        print("GPIO cleaned up. Done.")
