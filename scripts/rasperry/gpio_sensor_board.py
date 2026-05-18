#!/usr/bin/env python3
"""
gpio_sensor_board.py
====================
كلاس يقرأ مصفوفة ريد سويتشات 8x8 من GPIO على Raspberry Pi 3 Model B.

التوصيل:
  - 8 أعمدة (COLS) → Output pins: يُفعّل عمود واحد بـ LOW في كل مرة.
  - 8 صفوف (ROWS) → Input pins مع Pull-up داخلي:
    لما السويتش مغلق (مغناطيس = قطعة موجودة) → القراءة LOW = 1 (piece present).
    لما السويتش مفتوح (لا مغناطيس) → القراءة HIGH = 0 (empty).

  هذا مسح صفّي/عمودي (scanning matrix) — نفس مبدأ كيبورد المصفوفات.
  في أي لحظة، عمود واحد فقط مفعّل (LOW)، ونقرأ الصفوف كلها.
  ثم ننتقل للعمود التالي.

الأعمدة والصفوف:
  الترتيب يتبع ترتيب رقعة الشطرنج:
    COL 0 = file 'a' (العمود الأيسر من جهة الأبيض)
    COL 7 = file 'h'
    ROW 0 = rank 8 (الصف الأعلى — قطع الأسود)
    ROW 7 = rank 1 (الصف الأسفل — قطع الأبيض)

  هذا يطابق اتفاقية board_tracker_node.py:
    board[row][col] حيث row 0 = rank 8.

الدائرة:
  كل ريد سويتش يربط بين pin الصف و pin العمود:
    COL_pin ─── [Reed Switch] ─── ROW_pin
  لما COL = LOW و السويتش مغلق: ROW يقرأ LOW (لأنه مسحوب لـ GND عبر السويتش).
  لما السويتش مفتوح: ROW يبقى HIGH (Pull-up الداخلي).

ملاحظات مهمة:
  1. الراسبيري باي 3B عندها 26 GPIO pin قابل للاستخدام. نحتاج 16 (8 صفوف + 8 أعمدة).
  2. المسح ياخد ~2ms (8 أعمدة × 250μs لكل عمود) — أسرع بكثير من حركة اللاعب.
  3. debouncing: نعتمد على STABILITY_CYCLES في board_tracker_node (3 دورات = 150ms)
     فلا حاجة لـ debounce إضافي هنا.
  4. لازم تكون الـ magnets في قاعدة كل قطعة (neodymium N35 أو أقوى).

التوصيلات الافتراضية (قابلة للتعديل):
  ROWS (Input + PUD_UP): GPIO 4, 17, 27, 22, 5, 6, 13, 19
  COLS (Output):         GPIO 26, 21, 20, 16, 12, 25, 24, 23

  (يمكنك تغييرها من الثوابت بالأسفل)

الاستخدام:
  from gpio_sensor_board import GPIOSensorBoard
  sensor = GPIOSensorBoard()
  occupancy = sensor.scan()  # → 8x8 list of 0/1
  sensor.cleanup()

  أو في board_tracker_node.py:
  بدل SimulatedSensorBoard، استخدم GPIOSensorBoard بنفس الواجهة.
"""

import time

try:
    import RPi.GPIO as GPIO
except ImportError:
    # للسماح بالـ import على بيئة بدون RPi (مثل PC للاختبار)
    GPIO = None
    print("[WARN] RPi.GPIO not available — GPIOSensorBoard will not function.")


# ============================================================================
#  Pin Assignment — عدّل حسب توصيلتك
# ============================================================================

# الصفوف (ROWS) — Input pins مع Pull-up داخلي
# ROW 0 = rank 8 (أسود)، ROW 7 = rank 1 (أبيض)
ROW_PINS = [4, 17, 27, 22, 5, 6, 13, 19]

# الأعمدة (COLS) — Output pins
# COL 0 = file 'a'، COL 7 = file 'h'
COL_PINS = [26, 21, 20, 16, 12, 25, 24, 23]

# تأخير بعد تفعيل العمود (بالثواني) — يسمح للإشارة بالاستقرار.
# 250μs كافية للريد سويتشات العادية.
SCAN_DELAY_S = 0.00025


# ============================================================================
#  GPIOSensorBoard — نفس واجهة SimulatedSensorBoard
# ============================================================================
class GPIOSensorBoard:
    """
    يقرأ مصفوفة ريد سويتشات 8x8 من GPIO.
    واجهة scan() ترجع مصفوفة 8x8 (قوائم قوائم) من 0/1.
    متوافق مع board_tracker_node.py كـ drop-in replacement لـ SimulatedSensorBoard.
    """

    def __init__(self, row_pins=None, col_pins=None, scan_delay=None):
        """
        row_pins: list of 8 GPIO pins for rows (Input, PUD_UP)
        col_pins: list of 8 GPIO pins for columns (Output)
        scan_delay: delay in seconds after activating each column
        """
        if GPIO is None:
            raise RuntimeError("RPi.GPIO is not available. Cannot use GPIOSensorBoard.")

        self.row_pins = row_pins or ROW_PINS
        self.col_pins = col_pins or COL_PINS
        self.scan_delay = scan_delay or SCAN_DELAY_S

        if len(self.row_pins) != 8 or len(self.col_pins) != 8:
            raise ValueError("Need exactly 8 row pins and 8 column pins.")

        # إعداد GPIO
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

        الترتيب:
          row 0 = rank 8 (أسود)
          row 7 = rank 1 (أبيض)
          col 0 = file 'a'
          col 7 = file 'h'
        """
        board = [[0] * 8 for _ in range(8)]

        for col_idx, col_pin in enumerate(self.col_pins):
            # تفعيل العمود الحالي (LOW)
            GPIO.output(col_pin, GPIO.LOW)

            # انتظار استقرار الإشارة
            time.sleep(self.scan_delay)

            # قراءة كل الصفوف
            for row_idx, row_pin in enumerate(self.row_pins):
                # السويتش مغلق → ROW = LOW → قطعة موجودة = 1
                if GPIO.input(row_pin) == GPIO.LOW:
                    board[row_idx][col_idx] = 1
                else:
                    board[row_idx][col_idx] = 0

            # إطفاء العمود (HIGH)
            GPIO.output(col_pin, GPIO.HIGH)

        return board

    def cleanup(self):
        """يُنظّف GPIO عند الإغلاق."""
        GPIO.cleanup()

    # ========================================================================
    # واجهة التوافق مع SimulatedSensorBoard
    # (هذه الدوال لا تُستخدم في الإنتاج — فقط لمنع AttributeError
    #  لو الكود حاول يناديها عن طريق الخطأ)
    # ========================================================================
    def set_square(self, sq, value):
        """لا معنى لها في GPIO — الحساس يقرأ الواقع مباشرة."""
        pass

    def apply_changes(self, changes):
        """لا معنى لها في GPIO — الحساس يقرأ الواقع مباشرة."""
        pass

    def load_from_chess_board(self, chess_board):
        """لا معنى لها في GPIO — الحساس يقرأ الواقع مباشرة."""
        pass


# ============================================================================
#  اختبار مستقل (شغّله مباشرة على الراسبيري لفحص التوصيلات)
# ============================================================================
if __name__ == '__main__':
    FILES = 'abcdefgh'

    print("=" * 50)
    print("  GPIO Reed-Switch Matrix 8x8 — Live Test")
    print("=" * 50)
    print(f"  ROW pins (rank 8→1): {ROW_PINS}")
    print(f"  COL pins (file a→h): {COL_PINS}")
    print("=" * 50)
    print()
    print("  1 = piece detected (switch closed)")
    print("  . = empty (switch open)")
    print()
    print("  Press Ctrl+C to stop.")
    print()

    sensor = GPIOSensorBoard()

    try:
        while True:
            board = sensor.scan()

            # عدّ القطع
            piece_count = sum(cell for row in board for cell in row)

            # طباعة اللوحة
            print("    a   b   c   d   e   f   g   h")
            print("  +---+---+---+---+---+---+---+---+")
            for row_idx in range(8):
                rank = 8 - row_idx
                line = f"{rank} |"
                for col_idx in range(8):
                    cell = " 1 " if board[row_idx][col_idx] == 1 else " . "
                    line += cell + "|"
                line += f" {rank}"
                print(line)
                print("  +---+---+---+---+---+---+---+---+")
            print(f"    a   b   c   d   e   f   g   h    pieces: {piece_count}/32")
            print()

            # طباعة خام
            # for row in board:
            #     print(row)

            time.sleep(0.5)

    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        sensor.cleanup()
        print("GPIO cleaned up. Done.")
