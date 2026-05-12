#!/usr/bin/env python3
"""
board_simulator.py
------------------
محاكي لوحة شطرنج 8x8 يعتمد على مصفوفة ريد سويتشات (reed switches).

الفكرة:
  - كل خانة على اللوحة عندها ريد سويتش.
  - لما تكون فيه قطعة فوق الخانة => السويتش مغلق => نعتبرها 1 (HIGH في منطق المحاكاة).
  - لما الخانة فاضية => السويتش مفتوح => 0.

هذا السكربت يحاكي الإشارات قبل الانتقال للراسبيري باي:
  - يعرض اللوحة بشكل 8x8 في التيرمينال.
  - تقدر "تحط" أو "تشيل" قطعة بكتابة اسم الخانة (مثل e2).
  - بعد كل تعديل، السكربت يقارن بين اللقطة السابقة واللقطة الحالية
    ويستنتج الحركة (from -> to) أو الأكل أو الكاستلنج.

استبدال لاحقاً بالراسبيري:
  يكفي استبدال الدالة scan_board() بالنسخة الحقيقية اللي تقرأ من GPIO.
  باقي المنطق (كشف الحركة) يبقى نفسه.

الاستخدام:
  python3 board_simulator.py

الأوامر داخل السكربت:
  e2          => بدّل حالة الخانة e2 (toggle)
  e2 1        => ضع قطعة على e2
  e2 0        => شل القطعة عن e2
  e2 e4       => نفذ حركة كاملة (شل من e2 وضع في e4)
  init        => ابدأ بالوضعية الابتدائية (كل الصف 1،2،7،8 ممتلئ)
  clear       => فرّغ اللوحة
  show        => أعد عرض اللوحة
  diff        => اعرض آخر حركة تم اكتشافها
  snapshot    => خزّن اللقطة الحالية كمرجع (previous) بدون اكتشاف حركة
  raw         => اطبع المصفوفة بشكل خام (قوائم داخل قوائم)
  help        => عرض الأوامر
  quit / q    => خروج
"""

import copy
import sys


# ------------------------------------------------------------------
# ثوابت اللوحة
# ------------------------------------------------------------------
FILES = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h']   # الأعمدة a..h
RANKS = [1, 2, 3, 4, 5, 6, 7, 8]                    # الصفوف 1..8

# طريقة التخزين:
#   board[row][col]
#   row 0  => rank 8 (الصف العلوي لما نعرض)
#   row 7  => rank 1 (الصف السفلي)
#   col 0  => file 'a'
#   col 7  => file 'h'
# هذه الاتفاقية مطابقة لكيفية عرض رقعة الشطرنج عادة.


# ------------------------------------------------------------------
# تحويل بين notation (مثلاً 'e2') وإحداثيات المصفوفة
# ------------------------------------------------------------------
def square_to_rc(square: str):
    """حوّل 'e2' => (row, col) حسب اتفاقية board[row][col]."""
    square = square.strip().lower()
    if len(square) != 2:
        return None
    f, r = square[0], square[1]
    if f not in FILES or not r.isdigit():
        return None
    rank = int(r)
    if rank not in RANKS:
        return None
    col = FILES.index(f)
    row = 8 - rank          # rank 8 -> row 0، rank 1 -> row 7
    return row, col


def rc_to_square(row: int, col: int) -> str:
    """حوّل (row, col) => 'e2'."""
    return f"{FILES[col]}{8 - row}"


# ------------------------------------------------------------------
# إنشاء اللوحة
# ------------------------------------------------------------------
def empty_board():
    """لوحة 8x8 كلها أصفار."""
    return [[0] * 8 for _ in range(8)]


def initial_board():
    """الوضعية الابتدائية: الصفوف 1،2 للأبيض و 7،8 للأسود مملوءة."""
    b = empty_board()
    # rank 1 و 2 => row 7 و 6
    for c in range(8):
        b[7][c] = 1
        b[6][c] = 1
    # rank 7 و 8 => row 1 و 0
    for c in range(8):
        b[0][c] = 1
        b[1][c] = 1
    return b


# ------------------------------------------------------------------
# محاكاة مسح اللوحة (نفس واجهة كود الراسبيري)
# ------------------------------------------------------------------
# في الراسبيري الحقيقي: هذه الدالة تقرأ من GPIO.
# هنا: فقط ترجع نسخة من الحالة الحالية.
_current_board = initial_board()


def scan_board():
    """
    مطابقة لواجهة كود الراسبيري: ترجع مصفوفة 8x8 (قوائم قوائم) من 0/1.
    في السيمولتور ترجع نسخة من الحالة الحالية.
    """
    return copy.deepcopy(_current_board)


# ------------------------------------------------------------------
# عرض اللوحة
# ------------------------------------------------------------------
# ألوان ANSI بسيطة لتمييز الخانات (تعمل على أوبونتو).
ANSI_RESET = "\033[0m"
ANSI_DIM   = "\033[2m"
ANSI_BOLD  = "\033[1m"
ANSI_GREEN = "\033[32m"
ANSI_RED   = "\033[31m"
ANSI_CYAN  = "\033[36m"
ANSI_YELLOW = "\033[33m"


def print_board(board, highlight=None):
    """
    اطبع اللوحة بشكل مقروء.
    highlight: قائمة اختيارية من الخانات (squares) يتم تمييزها.
    """
    highlight = set(highlight or [])
    print()
    print("    " + "   ".join(f.upper() for f in FILES))
    print("  +" + "----" * 8 + "+")

    for row in range(8):
        rank = 8 - row
        line = f"{rank} |"
        for col in range(8):
            sq = rc_to_square(row, col)
            val = board[row][col]
            cell = " 1 " if val == 1 else " . "
            if sq in highlight:
                cell = f"{ANSI_YELLOW}{ANSI_BOLD}{cell}{ANSI_RESET}"
            elif val == 1:
                cell = f"{ANSI_GREEN}{cell}{ANSI_RESET}"
            else:
                cell = f"{ANSI_DIM}{cell}{ANSI_RESET}"
            line += cell + "|"
        line += f" {rank}"
        print(line)
        print("  +" + "----" * 8 + "+")

    print("    " + "   ".join(f.upper() for f in FILES))
    print()


# ------------------------------------------------------------------
# اكتشاف الحركة من خلال المقارنة بين لقطتين
# ------------------------------------------------------------------
def diff_boards(prev, curr):
    """
    قارن بين لقطتين وأرجع قائمة بالتغييرات:
      disappeared => خانات كانت 1 وصارت 0 (رُفعت منها القطعة)
      appeared    => خانات كانت 0 وصارت 1 (وُضعت فيها قطعة)
    """
    disappeared = []
    appeared = []
    for r in range(8):
        for c in range(8):
            if prev[r][c] == 1 and curr[r][c] == 0:
                disappeared.append(rc_to_square(r, c))
            elif prev[r][c] == 0 and curr[r][c] == 1:
                appeared.append(rc_to_square(r, c))
    return disappeared, appeared


def classify_move(prev, curr):
    """
    من خلال الفرق بين لقطتين، استنتج نوع الحركة.
    ترجع dict فيها:
      type: 'none' | 'move' | 'capture' | 'castling' | 'lifted' | 'placed' | 'unknown'
      details: شرح إضافي
      squares: الخانات المتأثرة (للعرض)
    """
    disappeared, appeared = diff_boards(prev, curr)
    info = {
        "type": "none",
        "details": "",
        "squares": [],
        "disappeared": disappeared,
        "appeared": appeared,
    }

    # لا تغيير
    if not disappeared and not appeared:
        return info

    # حالة: قطعة واحدة اتشالت بس (ما انحطت مكانها)
    # هذا عادةً وسط الحركة: اللاعب رفع القطعة ولسا ما حطها.
    if len(disappeared) == 1 and len(appeared) == 0:
        info["type"] = "lifted"
        info["details"] = f"قطعة رُفعت من {disappeared[0]} (لسا ما حُطّت)"
        info["squares"] = disappeared
        return info

    # حالة: قطعة انحطت بدون ما شيء يتشال (قطعة جديدة أضيفت)
    if len(appeared) == 1 and len(disappeared) == 0:
        info["type"] = "placed"
        info["details"] = f"قطعة وُضعت في {appeared[0]} (بدون رفع)"
        info["squares"] = appeared
        return info

    # حالة: حركة عادية (from -> to)
    #   بدون أكل: واحدة اختفت وواحدة ظهرت (في خانة ثانية).
    if len(disappeared) == 1 and len(appeared) == 1:
        info["type"] = "move"
        info["details"] = f"حركة من {disappeared[0]} إلى {appeared[0]}"
        info["squares"] = disappeared + appeared
        return info

    # حالة: حركة أكل
    #   قطعتين اختفت (اللي اتحركت + اللي انأكلت) وواحدة ظهرت
    #   (لأن المهاجم راح مكان المأكول).
    if len(disappeared) == 2 and len(appeared) == 1:
        to_sq = appeared[0]
        from_candidates = [s for s in disappeared if s != to_sq]
        # القطعة اللي اتحركت هي اللي ما كانتش هي نفس خانة الهدف
        if len(from_candidates) == 1:
            info["type"] = "capture"
            info["details"] = f"أكل: {from_candidates[0]} x {to_sq}"
            info["squares"] = [from_candidates[0], to_sq]
            return info

    # حالة: الكاستلنج
    #   أربع خانات تغيرت (ملك + قلعة).
    #   مثلاً (كاستلنج قصير أبيض): e1 -> فاضي، h1 -> فاضي، g1 -> ممتلئ، f1 -> ممتلئ
    if len(disappeared) == 2 and len(appeared) == 2:
        # تحقق من أنماط الكاستلنج المعروفة
        castling_patterns = [
            # (disappeared_set, appeared_set, label)
            ({"e1", "h1"}, {"g1", "f1"}, "كاستلنج قصير أبيض O-O"),
            ({"e1", "a1"}, {"c1", "d1"}, "كاستلنج طويل أبيض O-O-O"),
            ({"e8", "h8"}, {"g8", "f8"}, "كاستلنج قصير أسود O-O"),
            ({"e8", "a8"}, {"c8", "d8"}, "كاستلنج طويل أسود O-O-O"),
        ]
        d_set = set(disappeared)
        a_set = set(appeared)
        for ds, as_, label in castling_patterns:
            if d_set == ds and a_set == as_:
                info["type"] = "castling"
                info["details"] = label
                info["squares"] = list(d_set | a_set)
                return info

    # أي شيء آخر = غير معروف
    info["type"] = "unknown"
    info["details"] = (
        f"تغيير غير معروف: اختفى {disappeared}، ظهر {appeared}"
    )
    info["squares"] = disappeared + appeared
    return info


# ------------------------------------------------------------------
# عمليات تعديل الحالة الحالية (بديل الإشارات من GPIO في الحقيقة)
# ------------------------------------------------------------------
def set_square(square: str, value: int) -> bool:
    rc = square_to_rc(square)
    if rc is None:
        return False
    r, c = rc
    _current_board[r][c] = 1 if value else 0
    return True


def toggle_square(square: str) -> bool:
    rc = square_to_rc(square)
    if rc is None:
        return False
    r, c = rc
    _current_board[r][c] = 0 if _current_board[r][c] == 1 else 1
    return True


def apply_move(from_sq: str, to_sq: str) -> bool:
    """نفذ حركة: شل من from_sq وضع في to_sq (لو فيه قطعة في to_sq بتنأكل)."""
    f_rc = square_to_rc(from_sq)
    t_rc = square_to_rc(to_sq)
    if f_rc is None or t_rc is None:
        return False
    fr, fc = f_rc
    tr, tc = t_rc
    if _current_board[fr][fc] != 1:
        print(f"{ANSI_RED}تحذير: الخانة {from_sq} فاضية أصلاً.{ANSI_RESET}")
        return False
    _current_board[fr][fc] = 0
    _current_board[tr][tc] = 1
    return True


# ------------------------------------------------------------------
# الحلقة التفاعلية
# ------------------------------------------------------------------
HELP_TEXT = """
الأوامر المتاحة:
  init              ابدأ بالوضعية الابتدائية (كل الصف 1,2,7,8 ممتلئ)
  clear             فرّغ اللوحة
  show              أعد عرض اللوحة
  raw               اطبع المصفوفة 8x8 الخام (زي سكربت الراسبيري)
  snapshot          خزّن الحالة الحالية كمرجع (previous) بدون اكتشاف حركة
  diff              اعرض آخر حركة تم اكتشافها

  e2                بدّل حالة الخانة e2 (toggle)
  e2 1 | e2 on      ضع قطعة على e2
  e2 0 | e2 off     شل القطعة عن e2
  e2 e4             نفّذ حركة من e2 إلى e4

  batch             ابدأ وضع الدفعة (توقف الكشف التلقائي)
  commit            أنهِ الدفعة واكشف الحركة مرة واحدة
                    (مفيد للكاستلنج: batch, e1 g1, h1 f1, commit)

  help              عرض هذه القائمة
  quit / q / exit   خروج
"""


# علم يحدد إن كنا داخل دفعة (لا نكشف تلقائياً بعد كل تعديل)
_batch_mode = False


def handle_command(cmd: str, previous_snapshot):
    """
    نفّذ أمر واحد.
    ترجع tuple: (should_exit, should_rescan)
      should_rescan=True يعني نعمل scan + diff بعد الأمر.
    """
    cmd = cmd.strip().lower()
    if not cmd:
        return False, False

    if cmd in ("q", "quit", "exit"):
        return True, False

    if cmd == "help":
        print(HELP_TEXT)
        return False, False

    if cmd == "init":
        global _current_board
        _current_board = initial_board()
        print(f"{ANSI_CYAN}تم ضبط الوضعية الابتدائية.{ANSI_RESET}")
        return False, True

    if cmd == "clear":
        _current_board[:] = empty_board()
        print(f"{ANSI_CYAN}اللوحة فُرغت.{ANSI_RESET}")
        return False, True

    if cmd == "show":
        print_board(scan_board())
        return False, False

    if cmd == "raw":
        b = scan_board()
        print("Raw 8x8 matrix (row0 = rank 8, col0 = file a):")
        for row in b:
            print(row)
        return False, False

    if cmd == "snapshot":
        # نحدث السنابشوت السابق ليطابق الحالي، فلا يُكشف تغيير
        previous_snapshot[:] = copy.deepcopy(_current_board)
        print(f"{ANSI_CYAN}تم تخزين اللقطة الحالية كمرجع.{ANSI_RESET}")
        return False, False

    if cmd == "diff":
        # فقط اعرض الفرق دون تحديث المرجع
        curr = scan_board()
        info = classify_move(previous_snapshot, curr)
        print_detection(info)
        return False, False

    if cmd == "batch":
        global _batch_mode
        _batch_mode = True
        print(f"{ANSI_CYAN}دخلت وضع الدفعة: عدّل اللوحة كما تشاء ثم اكتب 'commit'.{ANSI_RESET}")
        return False, False

    if cmd == "commit":
        if not _batch_mode:
            print(f"{ANSI_YELLOW}لست داخل دفعة.{ANSI_RESET}")
            return False, False
        _batch_mode = False
        print(f"{ANSI_CYAN}تم إنهاء الدفعة. جاري الكشف...{ANSI_RESET}")
        return False, True

    # صيغة: "e2" / "e2 1" / "e2 0" / "e2 e4"
    parts = cmd.split()

    if len(parts) == 1:
        sq = parts[0]
        if square_to_rc(sq) is None:
            print(f"{ANSI_RED}أمر غير معروف: {cmd}{ANSI_RESET}")
            return False, False
        toggle_square(sq)
        return False, True

    if len(parts) == 2:
        a, b = parts
        # e2 e4 => حركة
        if square_to_rc(a) is not None and square_to_rc(b) is not None:
            if apply_move(a, b):
                return False, True
            return False, False

        # e2 1 / e2 0 / e2 on / e2 off
        if square_to_rc(a) is not None:
            if b in ("1", "on", "high"):
                set_square(a, 1)
                return False, True
            if b in ("0", "off", "low"):
                set_square(a, 0)
                return False, True

        print(f"{ANSI_RED}أمر غير مفهوم: {cmd}{ANSI_RESET}")
        return False, False

    print(f"{ANSI_RED}أمر غير مفهوم: {cmd}{ANSI_RESET}")
    return False, False


def print_detection(info):
    """اطبع نتيجة كشف الحركة بشكل ملوّن."""
    t = info["type"]
    if t == "none":
        print(f"{ANSI_DIM}لا يوجد تغيير.{ANSI_RESET}")
        return
    if t == "move":
        print(f"{ANSI_GREEN}[MOVE]     {info['details']}{ANSI_RESET}")
    elif t == "capture":
        print(f"{ANSI_RED}[CAPTURE]  {info['details']}{ANSI_RESET}")
    elif t == "castling":
        print(f"{ANSI_CYAN}[CASTLING] {info['details']}{ANSI_RESET}")
    elif t == "lifted":
        print(f"{ANSI_YELLOW}[LIFTED]   {info['details']}{ANSI_RESET}")
    elif t == "placed":
        print(f"{ANSI_YELLOW}[PLACED]   {info['details']}{ANSI_RESET}")
    else:
        print(f"{ANSI_RED}[UNKNOWN]  {info['details']}{ANSI_RESET}")


def main():
    print(f"{ANSI_BOLD}Chess Board Simulator (8x8 reed-switch emulation){ANSI_RESET}")
    print("اكتب 'help' لرؤية الأوامر، و 'quit' للخروج.")
    previous_snapshot = scan_board()
    print_board(previous_snapshot)

    while True:
        try:
            cmd = input("board> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break

        should_exit, should_rescan = handle_command(cmd, previous_snapshot)
        if should_exit:
            break

        # داخل وضع الدفعة: لا نكشف ولا نطبع اللوحة إلا عند commit
        if _batch_mode and cmd.strip().lower() != "commit":
            continue

        if should_rescan:
            curr = scan_board()
            info = classify_move(previous_snapshot, curr)
            highlight = info["squares"]
            print_board(curr, highlight=highlight)
            print_detection(info)
            # حدّث المرجع بعد الكشف
            previous_snapshot[:] = curr

    print("مع السلامة.")


if __name__ == "__main__":
    main()
