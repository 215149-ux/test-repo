#!/usr/bin/env python3
"""
board_tracker_node.py
=====================
ROS node يحاكي لوحة شطرنج فيزيائية مربوطة بمصفوفة ريد سويتشات 8x8،
ويتعقّب هوية كل قطعة (مو فقط وجودها)، ويحوّل الحركات إلى UCI notation
لإرسالها لمحرك Stockfish.

المعمارية (Architecture):
    ┌──────────────────────────────┐
    │ Physical reed-switch matrix  │   (في الإنتاج — على الراسبيري باي)
    │        أو                     │
    │ Terminal-driven simulation   │   (هنا — للتطوير على الأوبونتو)
    └──────────────┬───────────────┘
                   │ scan_board() → 8x8 occupancy [0/1]
                   ▼
    ┌──────────────────────────────┐
    │      MoveDetector            │
    │  - يقارن prev vs curr        │
    │  - يصنّف الحركة               │
    │  - يستشير chess.Board لمعرفة │
    │    هوية القطعة والقانونية     │
    └──────────────┬───────────────┘
                   │ UCI move string
                   ▼
       /chess/human_move   (ROS topic)
                   │
                   ▼
         Stockfish engine node

التكامل مع stokfish.py:
    حالياً stokfish.py يقرأ حركات اللاعب من stdin. في المستقبل نضيف له
    Subscriber على '/chess/human_move' ليستقبلها من هذه النود بدل stdin.
    هذه النود فقط تنشر — stokfish.py يبقى كما هو حالياً.

كيف تستبدل المحاكاة بالراسبيري:
    فقط استبدل الكلاس SimulatedSensorBoard بكلاس مماثل يقرأ GPIO،
    مع الإبقاء على نفس الواجهة scan() → 8x8 list.
    كل باقي المنطق (MoveDetector, BoardTrackerNode) يبقى كما هو.

أوامر التيرمينال:
    e2e4                 ⇒ تنفيذ حركة (تحديث مصفوفة الحساسات)
    e2 e4                ⇒ نفس الشيء
    e7e8q                ⇒ حركة مع ترقية (q|r|b|n)
    promote q|r|b|n      ⇒ الترقية الافتراضية لما تحركها بدون تحديد
    show                 ⇒ عرض الوضعية الحالية
    occ                  ⇒ طباعة المصفوفة 8x8 الخام
    fen                  ⇒ طباعة FEN
    reset                ⇒ إعادة للوضعية الابتدائية
    lift <sq>            ⇒ رفع قطعة (خانة 1→0) — لمحاكاة رفع يدوي
    place <sq>           ⇒ وضع قطعة (خانة 0→1)
    help                 ⇒ عرض الأوامر
    quit                 ⇒ إنهاء النود

المواضيع (Topics):
    Published:
        /chess/human_move       std_msgs/String  — UCI للحركة المكتشفة
        /chess/board_occupancy  std_msgs/String  — JSON فيه occupancy و FEN
        /chess/detected_move    std_msgs/String  — JSON مفصّل عن الحركة
    Subscribed:
        /chess/game_start       std_msgs/String  — إعادة تعيين عند بداية لعبة

التشغيل:
    $ rosrun chessrobot_project board_tracker_node.py
    (أو مباشرةً للاختبار بدون roscore: الأوامر الداخلية تعمل، ورسائل ROS ترمى)
"""

import copy
import json
import threading
import time

import chess

try:
    import rospy
    from std_msgs.msg import String
    ROS_AVAILABLE = True
except ImportError:
    # للسماح بالاختبار على بيئة بدون ROS (dry-run).
    ROS_AVAILABLE = False


# ------------------------------------------------------------------
# أدوات تحويل notation
# ------------------------------------------------------------------
FILES = 'abcdefgh'


def sq_to_rc(sq: str):
    """'e2' → (row, col) حيث row 0 = rank 8 و col 0 = file a."""
    sq = sq.lower()
    col = FILES.index(sq[0])
    row = 8 - int(sq[1])
    return row, col


def rc_to_sq(row: int, col: int) -> str:
    return f"{FILES[col]}{8 - row}"


def occupancy_from_chess(board: chess.Board):
    """استخرج مصفوفة 8x8 من 0/1 من كائن chess.Board."""
    occ = [[0] * 8 for _ in range(8)]
    for r in range(8):
        for c in range(8):
            sq = chess.square(c, 7 - r)  # python-chess: (file, rank) 0-7
            if board.piece_at(sq) is not None:
                occ[r][c] = 1
    return occ


def diff_occupancy(prev, curr):
    """Returns (disappeared_list, appeared_list) كـ notation (مثل 'e2')."""
    disappeared = []
    appeared = []
    for r in range(8):
        for c in range(8):
            if prev[r][c] == 1 and curr[r][c] == 0:
                disappeared.append(rc_to_sq(r, c))
            elif prev[r][c] == 0 and curr[r][c] == 1:
                appeared.append(rc_to_sq(r, c))
    return disappeared, appeared


# ------------------------------------------------------------------
# محاكي مصفوفة الحساسات (Drop-in replacement للـ GPIO)
# ------------------------------------------------------------------
class SimulatedSensorBoard:
    """
    محاكي لوحة الريد سويتشات. في الإنتاج: استبدلها بكلاس يقرأ GPIO.
    الواجهة العامة المهمة هي scan() التي ترجع مصفوفة 8x8 [0/1].
    """

    def __init__(self, initial_occupancy):
        self._board = [row[:] for row in initial_occupancy]
        self._lock = threading.Lock()

    def scan(self):
        """المكافئ لـ scan_board() في كود الراسبيري."""
        with self._lock:
            return [row[:] for row in self._board]

    def set_square(self, sq: str, value: int):
        r, c = sq_to_rc(sq)
        with self._lock:
            self._board[r][c] = 1 if value else 0

    def apply_changes(self, changes: dict):
        """changes: dict من sq→0/1. تطبيق دفعة واحدة (ذرّي atomic)."""
        with self._lock:
            for sq, v in changes.items():
                r, c = sq_to_rc(sq)
                self._board[r][c] = 1 if v else 0

    def load_from_chess_board(self, chess_board: chess.Board):
        occ = occupancy_from_chess(chess_board)
        with self._lock:
            self._board = occ


# ------------------------------------------------------------------
# كاشف الحركة — يستشير chess.Board لمعرفة هوية القطع
# ------------------------------------------------------------------
class MoveDetector:
    """
    يستنتج الحركة بمقارنة اللقطة الحالية (بعد استقرار اللوحة) مع اللقطة المرجعية
    (حالة اللعب المستقرة الأخيرة). يستشير chess.Board لمعرفة هوية القطع وقانونية
    الحركات — وهذا ضروري لأن مصفوفة الريد سويتشات لا تفرّق بين القطع المختلفة،
    لذا فإن الأكل قد يظهر ظاهرياً كـ "قطعة واحدة اختفت" (خانة الهدف كانت مشغولة
    وما زالت مشغولة، لكن بقطعة مختلفة).

    منطق التصنيف (بعد استقرار اللوحة):
      disappeared=0, appeared=0  → لا تغيير.
      disappeared=1, appeared=1  → حركة بسيطة (from→to) أو أكل الـ en-passant
                                    (نفحص قانونياً).
      disappeared=1, appeared=0  → أكل مباشر (عدو كان على خانة الهدف، المهاجم
                                    استبدله فلم يتغير occupancy هناك).
                                    نستخدم chess.legal_moves لاستنتاج الهدف.
      disappeared=2, appeared=1  → أكل مرّ بحالة وسيطة ظهرت فيها خانة الهدف
                                    فارغة ثم امتلأت.
      disappeared=2, appeared=2  → كاستلنج (ملك + رخ).
      غير ذلك                    → غير معروف / خلل.
    """

    CASTLING_PATTERNS = [
        ({"e1", "h1"}, {"g1", "f1"}, "e1g1"),
        ({"e1", "a1"}, {"c1", "d1"}, "e1c1"),
        ({"e8", "h8"}, {"g8", "f8"}, "e8g8"),
        ({"e8", "a8"}, {"c8", "d8"}, "e8c8"),
    ]

    def __init__(self, chess_board: chess.Board):
        self.board = chess_board
        self.last_snapshot = occupancy_from_chess(chess_board)

    def reset_snapshot(self):
        self.last_snapshot = occupancy_from_chess(self.board)

    def detect(self, current_occupancy, default_promotion: str = 'q'):
        """
        يستنتج حركة من المقارنة بين current_occupancy و self.last_snapshot.
        يرجع (uci, description) أو (None, reason).
        """
        disappeared, appeared = diff_occupancy(self.last_snapshot, current_occupancy)

        if not disappeared and not appeared:
            return None, "no_change"

        # --- كاستلنج: أربع خانات تتغير ---
        if len(disappeared) == 2 and len(appeared) == 2:
            d_set, a_set = set(disappeared), set(appeared)
            for ds, as_, uci in self.CASTLING_PATTERNS:
                if d_set == ds and a_set == as_:
                    try:
                        mv = chess.Move.from_uci(uci)
                    except ValueError:
                        return None, f"invalid_castling_uci_{uci}"
                    if mv in self.board.legal_moves:
                        return uci, f"castling {uci}"
                    return None, f"illegal_castling_{uci}"
            return None, f"unknown_4sq disappeared={disappeared} appeared={appeared}"

        # --- حركة بسيطة ---
        if len(disappeared) == 1 and len(appeared) == 1:
            return self._build_uci(disappeared[0], appeared[0], default_promotion)

        # --- حركة مرت بخانة هدف فارغة وسيطة (أكل أو en-passant واضح) ---
        if len(disappeared) == 2 and len(appeared) == 1:
            to_sq = appeared[0]
            from_candidates = [s for s in disappeared if s != to_sq]
            if len(from_candidates) == 1:
                return self._build_uci(from_candidates[0], to_sq, default_promotion)
            # en-passant: الهدف (to_sq) ليس ضمن disappeared
            # والمأكول (xsq) + المنطلق (from_sq) كلاهما ضمنها.
            return self._resolve_en_passant(disappeared, to_sq, default_promotion)

        # --- خانة واحدة "اختفت" والباقي لم يتغير ---
        #   * إما lifted (اللاعب رفع قطعة ولسا ما حطها) — غير مستقرة.
        #   * أو أكل استبدالي (المأكولة والمهاجم في نفس الخانة فـ occupancy لم يتغير).
        # يعتمد المتصل على الاستقرار — هذا التابع يُستدعى فقط عند استقرار اللوحة،
        # لذا نفترض الحالة الثانية ونبحث عن أكل قانوني من تلك الخانة.
        if len(disappeared) == 1 and len(appeared) == 0:
            return self._resolve_in_place_capture(disappeared[0])

        if len(disappeared) == 0 and len(appeared) == 1:
            return None, f"placed_only_{appeared[0]}"

        return None, (f"unknown disappeared={disappeared} appeared={appeared}")

    # ----- منطق الـ UCI القياسي -----
    def _build_uci(self, from_sq: str, to_sq: str, default_promotion: str):
        piece = self.board.piece_at(chess.parse_square(from_sq))
        if piece is None:
            return None, f"no_piece_at_{from_sq}"

        uci = from_sq + to_sq
        if piece.piece_type == chess.PAWN:
            to_rank = int(to_sq[1])
            if (piece.color == chess.WHITE and to_rank == 8) or \
               (piece.color == chess.BLACK and to_rank == 1):
                p = default_promotion if default_promotion in ('q', 'r', 'b', 'n') else 'q'
                uci += p

        try:
            mv = chess.Move.from_uci(uci)
        except ValueError:
            return None, f"invalid_uci_{uci}"

        if mv not in self.board.legal_moves:
            return None, f"illegal_{uci}"

        return uci, self._describe(mv)

    def _resolve_en_passant(self, disappeared_list, to_sq, default_promotion):
        """حالة: خانتان اختفتا وخانة هدف واحدة ظهرت، ولا أحد منهما هو to_sq."""
        for from_sq in disappeared_list:
            try:
                mv = chess.Move.from_uci(from_sq + to_sq)
            except ValueError:
                continue
            if mv in self.board.legal_moves and self.board.is_en_passant(mv):
                return from_sq + to_sq, f"en_passant {from_sq + to_sq}"
        return None, f"unresolved_enpassant d={disappeared_list} a=[{to_sq}]"

    def _resolve_in_place_capture(self, from_sq: str):
        """
        خانة واحدة اختفت ولا أحد ظهر.
        نبحث في chess.Board عن حركات أكل قانونية من هذه الخانة.
        """
        piece = self.board.piece_at(chess.parse_square(from_sq))
        if piece is None:
            return None, f"no_piece_at_{from_sq}_unexpected"
        # لا نعرف إن كانت الحركة قد اكتملت ما لم نتأكد من الاستقرار —
        # نرجع إشارة lifted_or_capture والمتصل يقرر بحسب استقرار اللوحة.
        captures_from = [
            m for m in self.board.legal_moves
            if m.from_square == chess.parse_square(from_sq)
            and self.board.is_capture(m)
        ]
        if len(captures_from) == 0:
            return None, f"lifted_{from_sq}"
        if len(captures_from) == 1:
            mv = captures_from[0]
            return mv.uci(), f"capture_in_place {mv.uci()}"
        # أكثر من احتمال — غامض. نعيد lifted لأن الحركة لم تُحسم بعد
        # (قد يكون رفع قطعة ولسا ما حط).
        options = [m.uci() for m in captures_from]
        return None, f"ambiguous_capture_from_{from_sq} options={options}"

    def _describe(self, mv: chess.Move) -> str:
        uci = mv.uci()
        if self.board.is_en_passant(mv):
            return f"en_passant {uci}"
        if self.board.is_capture(mv):
            return f"capture {uci}"
        if self.board.is_castling(mv):
            return f"castling {uci}"
        if len(uci) == 5:
            return f"promotion {uci}"
        return f"move {uci}"

    def commit(self, uci: str):
        mv = chess.Move.from_uci(uci)
        self.board.push(mv)
        self.last_snapshot = occupancy_from_chess(self.board)


# ------------------------------------------------------------------
# طباعة اللوحة بشكل مقروء (للتيرمينال)
# ------------------------------------------------------------------
def print_chess_board(board: chess.Board):
    print()
    print("    a   b   c   d   e   f   g   h")
    print("  +---+---+---+---+---+---+---+---+")
    for rank in range(7, -1, -1):
        row_str = f"{rank + 1} |"
        for file in range(8):
            p = board.piece_at(chess.square(file, rank))
            cell = f" {p.symbol()} " if p else " . "
            row_str += cell + "|"
        row_str += f" {rank + 1}"
        print(row_str)
        print("  +---+---+---+---+---+---+---+---+")
    print("    a   b   c   d   e   f   g   h")
    turn = 'white' if board.turn == chess.WHITE else 'black'
    print(f"  Turn: {turn}   FEN: {board.fen()}\n")


# ------------------------------------------------------------------
# النود الرئيسية
# ------------------------------------------------------------------
class BoardTrackerNode:
    SCAN_RATE_HZ = 20

    TOPIC_HUMAN_MOVE = '/chess/human_move'
    TOPIC_OCCUPANCY = '/chess/board_occupancy'
    TOPIC_DETECTED = '/chess/detected_move'
    TOPIC_GAME_START = '/chess/game_start'

    def __init__(self):
        # اللوحة الذهنية (source of truth لهوية القطع)
        self.chess_board = chess.Board()

        # محاكي الحساسات (في الراسبيري: استبدله بـ GPIOSensorBoard)
        self.sensor = SimulatedSensorBoard(occupancy_from_chess(self.chess_board))

        # الكاشف
        self.detector = MoveDetector(self.chess_board)

        # خيارات
        self.default_promotion = 'q'

        # أعلام حكم
        self._shutdown = threading.Event()
        self._move_log = []        # قائمة الـ UCI moves المنشورة
        self._pending_event = threading.Event()  # لإيقاظ حلقة المسح فوراً بعد أمر

        # ROS setup
        if ROS_AVAILABLE:
            rospy.init_node('board_tracker', anonymous=False)
            self.pub_move = rospy.Publisher(self.TOPIC_HUMAN_MOVE, String, queue_size=10)
            self.pub_occ = rospy.Publisher(self.TOPIC_OCCUPANCY, String, queue_size=10, latch=True)
            self.pub_detected = rospy.Publisher(self.TOPIC_DETECTED, String, queue_size=10)
            rospy.Subscriber(self.TOPIC_GAME_START, String, self._on_game_start)
            self._log = rospy.loginfo
            self._warn = rospy.logwarn
            self._err = rospy.logerr
        else:
            self.pub_move = _DummyPub(self.TOPIC_HUMAN_MOVE)
            self.pub_occ = _DummyPub(self.TOPIC_OCCUPANCY)
            self.pub_detected = _DummyPub(self.TOPIC_DETECTED)
            self._log = lambda m: print(f"[INFO] {m}")
            self._warn = lambda m: print(f"[WARN] {m}")
            self._err = lambda m: print(f"[ERR ] {m}")

    # -------- ROS callbacks --------
    def _on_game_start(self, msg):
        self._log(f"game_start received ({msg.data!r}) — resetting board.")
        self._reset_board()

    # -------- Board operations --------
    def _reset_board(self):
        self.chess_board.reset()
        self.sensor.load_from_chess_board(self.chess_board)
        self.detector = MoveDetector(self.chess_board)
        self._move_log.clear()
        self._publish_occupancy()

    def _publish_occupancy(self):
        payload = {
            'occupancy': self.sensor.scan(),
            'fen': self.chess_board.fen(),
            'turn': 'white' if self.chess_board.turn == chess.WHITE else 'black',
            'moves': list(self._move_log),
        }
        self.pub_occ.publish(json.dumps(payload))

    # -------- Startup verification --------
    def verify_initial_setup(self, timeout_sec: float = None) -> bool:
        """ينتظر حتى تكون اللوحة في الوضعية الابتدائية (32 قطعة كاملة)."""
        self._log("Verifying initial setup — checking all 32 pieces are present...")
        expected = occupancy_from_chess(chess.Board())
        start = time.time()
        warned = False
        while not self._shutdown.is_set():
            if ROS_AVAILABLE and rospy.is_shutdown():
                return False
            current = self.sensor.scan()
            if current == expected:
                self._log("OK — All 32 pieces in place. Ready to track moves.")
                print_chess_board(self.chess_board)
                return True

            # تشخيص الفروقات
            issues = []
            for r in range(8):
                for c in range(8):
                    if current[r][c] != expected[r][c]:
                        sq = rc_to_sq(r, c)
                        if expected[r][c] == 1:
                            issues.append(f"missing@{sq}")
                        else:
                            issues.append(f"unexpected@{sq}")
            if not warned:
                self._warn(f"Board NOT in initial position. Issues: {issues[:16]}"
                           f"{' ...' if len(issues) > 16 else ''}")
                print("   ضع القطع في الوضعية الابتدائية (كل الصف 1,2 للأبيض و 7,8 للأسود).")
                print("   في المحاكاة: اكتب 'reset' في التيرمينال لإعادة ضبط اللوحة.")
                warned = True
            if timeout_sec is not None and (time.time() - start) > timeout_sec:
                self._err("Initial setup verification timed out.")
                return False
            time.sleep(0.3)
        return False

    # -------- Scan loop (الأساس) --------
    # عدد دورات المسح المتتالية التي يجب أن تبقى اللوحة فيها مستقرة قبل
    # قبول حركة. هذا يمنع التصنيف الخاطئ أثناء الحركات متعددة المراحل
    # (رفع قطعة ← تحريكها ← وضعها).
    STABILITY_CYCLES = 3

    def scan_loop(self):
        """حلقة مستمرة تراقب مصفوفة الحساسات وتنشر الحركات المكتشفة عند الاستقرار."""
        period = 1.0 / self.SCAN_RATE_HZ
        last_occ = self.sensor.scan()
        stable_count = 0
        while not self._shutdown.is_set():
            if ROS_AVAILABLE and rospy.is_shutdown():
                break

            current = self.sensor.scan()

            # نقيس استقرار اللوحة: كم دورة مسح متتالية بنفس الـ occupancy
            if current == last_occ:
                stable_count += 1
            else:
                stable_count = 0
                last_occ = current

            # نصنّف فقط لما تستقر اللوحة لمدة كافية بعد تغيير
            if stable_count == self.STABILITY_CYCLES:
                uci, info = self.detector.detect(current, self.default_promotion)
                if uci is not None:
                    self._handle_detected_move(uci, info)
                elif info and info != "no_change" and not info.startswith("lifted_"):
                    # نطبع التغييرات غير المحسومة للتشخيص (مرة واحدة بعد الاستقرار)
                    self._warn(f"Unresolved state after stabilization: {info}")

            # انتظر الدورة التالية أو حدث "أمر جديد"
            self._pending_event.wait(timeout=period)
            self._pending_event.clear()

    def _handle_detected_move(self, uci: str, info: str):
        self._log(f"DETECTED {uci}  ({info})  — publishing on {self.TOPIC_HUMAN_MOVE}")

        # تحديث اللوحة الذهنية
        try:
            self.detector.commit(uci)
        except Exception as e:
            self._err(f"Failed to commit detected move {uci}: {e}")
            return

        self._move_log.append(uci)

        # نشر على التوبيكات
        self.pub_move.publish(uci)
        self.pub_detected.publish(json.dumps({
            'uci': uci,
            'info': info,
            'fen_after': self.chess_board.fen(),
            'turn_after': 'white' if self.chess_board.turn == chess.WHITE else 'black',
        }))
        self._publish_occupancy()

        # عرض اللوحة الجديدة
        print_chess_board(self.chess_board)

        # تنبيهات نهاية اللعبة
        if self.chess_board.is_checkmate():
            self._log("CHECKMATE")
        elif self.chess_board.is_stalemate():
            self._log("STALEMATE (draw)")
        elif self.chess_board.is_check():
            self._log("CHECK")

    # -------- Terminal interface --------
    HELP_TEXT = """
Commands:
  e2e4 | e2 e4      تنفيذ حركة كاملة (تحديث الحساسات فيتكشفها الـ scan loop)
  e7e8q             ترقية (q|r|b|n)
  promote q         تعيين قطعة الترقية الافتراضية
  show              طباعة الوضعية الحالية
  occ               طباعة مصفوفة الحساسات 8x8 الخام
  fen               طباعة FEN
  moves             قائمة كل الحركات المنفّذة
  lift e2           محاكاة "رفع" قطعة يدوياً (خانة 1→0)
  place e4          محاكاة "وضع" قطعة يدوياً (خانة 0→1)
  reset             إعادة الوضعية الابتدائية
  help              عرض هذه الرسالة
  quit              إنهاء النود
"""

    def terminal_loop(self):
        print(self.HELP_TEXT)
        while not self._shutdown.is_set():
            if ROS_AVAILABLE and rospy.is_shutdown():
                break
            try:
                prompt = f"[{self._turn_symbol()}#{len(self._move_log)+1}] board> "
                line = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                self._shutdown.set()
                break
            if not line:
                continue
            try:
                self._dispatch(line)
            except Exception as e:
                self._err(f"Terminal command error: {e}")

            # أعطي حلقة المسح لحظة للاستجابة قبل الطلب التالي
            self._pending_event.set()
            time.sleep(0.1)

    def _turn_symbol(self):
        return 'w' if self.chess_board.turn == chess.WHITE else 'b'

    def _dispatch(self, line: str):
        low = line.lower()
        if low in ('q', 'quit', 'exit'):
            self._shutdown.set()
            return
        if low == 'help':
            print(self.HELP_TEXT)
            return
        if low == 'show':
            print_chess_board(self.chess_board)
            return
        if low == 'occ':
            for row in self.sensor.scan():
                print(row)
            return
        if low == 'fen':
            print(self.chess_board.fen())
            return
        if low == 'moves':
            print(' '.join(self._move_log) if self._move_log else '(no moves yet)')
            return
        if low == 'reset':
            self._reset_board()
            print("Board reset to initial position.")
            print_chess_board(self.chess_board)
            return
        if low.startswith('promote '):
            p = low.split(None, 1)[1].strip()
            if p in ('q', 'r', 'b', 'n'):
                self.default_promotion = p
                print(f"Default promotion piece set to '{p}'.")
            else:
                print("Invalid promotion — use q, r, b, or n.")
            return
        if low.startswith('lift '):
            sq = low.split(None, 1)[1].strip()
            if not self._is_valid_square(sq):
                print(f"Invalid square: {sq}")
                return
            self.sensor.set_square(sq, 0)
            print(f"Lifted {sq} (sensor set to 0).")
            return
        if low.startswith('place '):
            sq = low.split(None, 1)[1].strip()
            if not self._is_valid_square(sq):
                print(f"Invalid square: {sq}")
                return
            self.sensor.set_square(sq, 1)
            print(f"Placed at {sq} (sensor set to 1).")
            return

        # محاولة تفسير كحركة
        from_sq, to_sq, promo = self._parse_move_tokens(line)
        if from_sq is None:
            print(f"Unknown command: {line!r}. Type 'help'.")
            return
        self._simulate_player_move(from_sq, to_sq, promo)

    @staticmethod
    def _is_valid_square(sq):
        if not isinstance(sq, str) or len(sq) != 2:
            return False
        return sq[0] in FILES and sq[1] in '12345678'

    def _parse_move_tokens(self, line):
        """يفهم 'e2e4' أو 'e2 e4' أو 'e7e8q' أو 'e7 e8 q'."""
        parts = line.lower().split()
        if len(parts) == 1:
            tok = parts[0]
            if len(tok) == 4:
                return tok[:2], tok[2:4], None
            if len(tok) == 5:
                return tok[:2], tok[2:4], tok[4]
        elif len(parts) == 2:
            a, b = parts
            if self._is_valid_square(a) and self._is_valid_square(b):
                return a, b, None
        elif len(parts) == 3:
            a, b, p = parts
            if self._is_valid_square(a) and self._is_valid_square(b) and p in ('q', 'r', 'b', 'n'):
                return a, b, p
        return None, None, None

    def _simulate_player_move(self, from_sq, to_sq, promo):
        """
        يحاكي تنفيذ حركة فيزيائياً: يحدّث مصفوفة الحساسات لتعكس النتيجة بعد
        إتمام الحركة. حلقة المسح ستكشفها وتصنفها وتنشرها.
        """
        if not (self._is_valid_square(from_sq) and self._is_valid_square(to_sq)):
            print(f"Invalid squares: {from_sq}, {to_sq}")
            return

        from_idx = chess.parse_square(from_sq)
        to_idx = chess.parse_square(to_sq)
        piece = self.chess_board.piece_at(from_idx)
        if piece is None:
            print(f"No piece at {from_sq}.")
            return

        # ترقية تلقائية إذا ما حُدّدت
        if piece.piece_type == chess.PAWN:
            to_rank = int(to_sq[1])
            if (piece.color == chess.WHITE and to_rank == 8) or \
               (piece.color == chess.BLACK and to_rank == 1):
                if promo is None:
                    promo = self.default_promotion

        uci = from_sq + to_sq + (promo if promo else '')
        try:
            mv = chess.Move.from_uci(uci)
        except ValueError:
            print(f"Invalid UCI: {uci}")
            return

        if mv not in self.chess_board.legal_moves:
            legal_sample = [m.uci() for m in list(self.chess_board.legal_moves)[:6]]
            print(f"Illegal move: {uci}. Sample legal moves: {legal_sample}")
            return

        # نحاكي التغييرات الفيزيائية بحيث تمر اللوحة بنفس اللحظات الوسيطة
        # التي ستراها مصفوفة الريد سويتشات الحقيقية. هذا ضروري عشان حلقة
        # المسح تصنّف الحركة بدقة (خاصةً في الأكل: رفع المأكولة ثم وضع
        # المهاجم مكانها — بدون هذا يظهر الأكل كأنه "lifted" فقط).
        scan_period = 1.0 / self.SCAN_RATE_HZ
        wait_step = max(scan_period * 2, 0.05)

        is_capture = self.chess_board.is_capture(mv)
        is_ep = self.chess_board.is_en_passant(mv)
        is_castling = self.chess_board.is_castling(mv)

        if is_ep:
            # en-passant: البيدق المأكول على نفس ملف الهدف، لكن على رتبة from.
            captured_sq = to_sq[0] + from_sq[1]
            self.sensor.apply_changes({captured_sq: 0})
            time.sleep(wait_step)
            self.sensor.apply_changes({from_sq: 0})
            time.sleep(wait_step)
            self.sensor.apply_changes({to_sq: 1})
        elif is_capture:
            # أكل عادي: ارفع المأكولة أولاً، ثم حرّك المهاجم.
            self.sensor.apply_changes({to_sq: 0})
            time.sleep(wait_step)
            self.sensor.apply_changes({from_sq: 0, to_sq: 1})
        elif is_castling:
            # كاستلنج: الأربع خانات تتغير كلقطة واحدة، كاشفنا يعرفه بالنمط.
            rook_map = {
                'e1g1': ('h1', 'f1'), 'e1c1': ('a1', 'd1'),
                'e8g8': ('h8', 'f8'), 'e8c8': ('a8', 'd8'),
            }
            rf, rt = rook_map[from_sq + to_sq]
            self.sensor.apply_changes({from_sq: 0, to_sq: 1, rf: 0, rt: 1})
        else:
            # حركة عادية
            self.sensor.apply_changes({from_sq: 0, to_sq: 1})
        # scan loop يكتشف ويصنف وينشر في الدورة التالية

    # -------- Run --------
    def run(self):
        self._publish_occupancy()

        if not self.verify_initial_setup():
            self._log("Exiting — initial setup not confirmed.")
            return

        scan_thread = threading.Thread(target=self.scan_loop, daemon=True)
        scan_thread.start()

        try:
            self.terminal_loop()
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown.set()
            self._pending_event.set()
            scan_thread.join(timeout=1.0)
            self._log("Shutting down board_tracker.")


# ------------------------------------------------------------------
# Dummy publisher للاختبار بدون ROS
# ------------------------------------------------------------------
class _DummyPub:
    def __init__(self, name):
        self.name = name

    def publish(self, msg):
        print(f"[ROS-sim] {self.name}: {msg if len(str(msg)) < 200 else str(msg)[:200] + '...'}")


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------
def main():
    node = BoardTrackerNode()
    try:
        node.run()
    except Exception as e:
        print(f"[FATAL] {e}")
        raise


if __name__ == '__main__':
    if ROS_AVAILABLE:
        try:
            main()
        except rospy.ROSInterruptException:
            pass
    else:
        print("[WARN] rospy not available — running in dry-run mode (publishes are printed).")
        main()
