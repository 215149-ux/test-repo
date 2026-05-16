#!/usr/bin/env python3
"""
board_tracker_node.py
=====================
ROS node يتعقّب حالة لوحة شطرنج فيزيائية مربوطة بمصفوفة ريد سويتشات 8x8،
ويتكامل مع نود الدسبلاي ونود ستوكفيش بطريقة نظيفة:

   /chess/game_start    ──┐
   /chess/board_state   ──┼──>  board_tracker_node  ──┬──> /chess/move
                          │                            ├──> /chess/promotion_request
                          │                            └──> /chess/status
                          │
       (Stockfish هو source of truth — نتزامن من board_state)

أوضاع التعقّب (Modes):
  ACTIVE   : دور اللاعب البشري — نراقب اللوحة ونصنّف وننشر الحركة.
  MONITOR  : دور الروبوت أو وضع Robot vs Robot — نراقب اللوحة فقط:
             نقارنها مع آخر board_state ونحذّر عند عدم التطابق المستقر
             (مفيد لاكتشاف خلل في الذراع الميكانيكية أو سقوط قطعة).
  LOCKED   : منشورة حركة وننتظر board_state للتأكيد. لا تصنيف جديد.

اكتشاف الحركة (في ACTIVE فقط) — يعتمد على chess.legal_moves:
  لكل دورة مسح بعد استقرار اللوحة:
    candidates = الحركات القانونية التي تنتج بالضبط نفس occupancy الحالية.
    - إن كانت واحدة فقط:
        - إذا حركة عادية: ننشر UCI على /chess/move.
        - إذا ترقية: ننشر /chess/promotion_request {"move": "e7e8"}
          (بدون حرف القطعة) ونسلّم القرار للدسبلاي.
    - إذا 4 خيارات بنفس from/to (q,r,b,n): ترقية → request.
    - إذا 0 وكانت اللوحة في حالة وسطية متّسقة مع حركة قانونية
      (مثلاً: المهاجم مرفوع، أو المأكول مرفوع، أو الاثنين معاً)
      → PENDING — صامت، نستنّى اللاعب يكمّل.
    - إذا 0 ولا حالة وسطية صالحة → ANOMALY — تحذير واحد.

استبدال المحاكاة بالراسبيري:
  استبدل SimulatedSensorBoard بكلاس يقرأ GPIO بنفس واجهة scan().
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
    ROS_AVAILABLE = False


# ============================================================================
#  الأدوات المساعدة
# ============================================================================
FILES = 'abcdefgh'
RANKS = '12345678'

# ترميز قطع الدسبلاي ('wP', 'bK', ...) لأي تحويل من board_2d إلى chess.Board
PIECE_FROM_DISP = {
    'wK': 'K', 'wQ': 'Q', 'wR': 'R', 'wB': 'B', 'wN': 'N', 'wP': 'P',
    'bK': 'k', 'bQ': 'q', 'bR': 'r', 'bB': 'b', 'bN': 'n', 'bP': 'p',
}


def sq_to_rc(sq: str):
    """'e2' → (row=6, col=4) حسب اتفاقية: row 0 = rank 8، col 0 = file a."""
    sq = sq.lower()
    return 8 - int(sq[1]), FILES.index(sq[0])


def rc_to_sq(row: int, col: int) -> str:
    return f"{FILES[col]}{8 - row}"


def occupancy_from_chess(board: chess.Board):
    """يستخرج مصفوفة 8x8 من 0/1 من kائن chess.Board."""
    occ = [[0] * 8 for _ in range(8)]
    for r in range(8):
        for c in range(8):
            sq = chess.square(c, 7 - r)
            if board.piece_at(sq) is not None:
                occ[r][c] = 1
    return occ


def diff_occupancy(prev, curr):
    """ترجع (disappeared, appeared) كأسماء خانات."""
    disappeared = []
    appeared = []
    for r in range(8):
        for c in range(8):
            if prev[r][c] == 1 and curr[r][c] == 0:
                disappeared.append(rc_to_sq(r, c))
            elif prev[r][c] == 0 and curr[r][c] == 1:
                appeared.append(rc_to_sq(r, c))
    return disappeared, appeared


def board_2d_to_fen(board_2d, turn='white'):
    """يحوّل تمثيل الدسبلاي (8x8 list of 'wP'/'bK'/None) إلى FEN كاملة."""
    rows = []
    for r in range(8):  # row 0 = rank 8
        empty = 0
        line = ''
        for c in range(8):
            cell = board_2d[r][c]
            if cell is None:
                empty += 1
            else:
                if empty > 0:
                    line += str(empty)
                    empty = 0
                line += PIECE_FROM_DISP.get(cell, '?')
        if empty > 0:
            line += str(empty)
        rows.append(line)
    placement = '/'.join(rows)
    side = 'w' if str(turn).lower() == 'white' else 'b'
    # لا نعرف castling/en-passant من 2D — نحاول نخمّن بشكل آمن
    return f"{placement} {side} KQkq - 0 1"


# ============================================================================
#  محاكي مصفوفة الحساسات (Drop-in replacement للـ GPIO)
# ============================================================================
class SimulatedSensorBoard:
    """
    في الإنتاج: استبدلها بكلاس يقرأ GPIO. الواجهة المهمة هي scan().
    """

    def __init__(self, initial_occupancy):
        self._board = [row[:] for row in initial_occupancy]
        self._lock = threading.Lock()

    def scan(self):
        with self._lock:
            return [row[:] for row in self._board]

    def set_square(self, sq: str, value: int):
        r, c = sq_to_rc(sq)
        with self._lock:
            self._board[r][c] = 1 if value else 0

    def apply_changes(self, changes: dict):
        with self._lock:
            for sq, v in changes.items():
                r, c = sq_to_rc(sq)
                self._board[r][c] = 1 if v else 0

    def load_from_chess_board(self, chess_board: chess.Board):
        occ = occupancy_from_chess(chess_board)
        with self._lock:
            self._board = occ


# ============================================================================
#  المصنّف — يعرف هوية القطع عبر chess.Board
# ============================================================================
class MoveDetector:
    """
    Stateless. كل استدعاء يستلم:
      - chess_board: لوحة الشطرنج المرجعية (بعد آخر مزامنة من ستوكفيش).
      - current_occ: مصفوفة 8x8 الحالية من الحساسات.
    ويرجع dict يصف الحالة.
    """

    @staticmethod
    def squares_touched_by_move(board: chess.Board, move: chess.Move):
        """
        يرجع set من أسماء الخانات التي تتأثر فيزيائياً بتنفيذ الحركة.
        يشمل: خانة الانطلاق، الهدف، خانة المأكول في en-passant،
        وخانتي الرخ في الكاستلنج.
        """
        sqs = {chess.square_name(move.from_square),
               chess.square_name(move.to_square)}
        if board.is_en_passant(move):
            ep_file = chess.square_file(move.to_square)
            ep_rank = chess.square_rank(move.from_square)
            sqs.add(chess.square_name(chess.square(ep_file, ep_rank)))
        if board.is_castling(move):
            rank = chess.square_rank(move.from_square)
            if chess.square_file(move.to_square) > chess.square_file(move.from_square):
                # كاستلنج قصير: الرخ من h إلى f
                sqs.add(chess.square_name(chess.square(7, rank)))
                sqs.add(chess.square_name(chess.square(5, rank)))
            else:
                # كاستلنج طويل: الرخ من a إلى d
                sqs.add(chess.square_name(chess.square(0, rank)))
                sqs.add(chess.square_name(chess.square(3, rank)))
        return sqs

    @classmethod
    def classify(cls, chess_board: chess.Board, current_occ, anchor_occ):
        """
        يرجع dict بحقل 'kind' وحقول إضافية حسب النوع:
          {'kind': 'clean'}  → اللوحة على anchor، لا تغيير.
          {'kind': 'complete', 'move': chess.Move, 'uci': '...'}  → حركة كاملة.
          {'kind': 'promotion', 'from_to': 'e7e8', 'candidates': [...]}
          {'kind': 'pending', 'partial_for': [...], 'disappeared': [...],
           'appeared': [...]}  → حالة وسطية صالحة، نستنّى.
          {'kind': 'anomaly', 'disappeared': [...], 'appeared': [...],
           'reason': '...'}
        """
        disappeared, appeared = diff_occupancy(anchor_occ, current_occ)

        if not disappeared and not appeared:
            return {'kind': 'clean'}

        # محاولة 1: هل اللوحة الحالية = نتيجة تنفيذ حركة قانونية؟
        legal = list(chess_board.legal_moves)
        complete_matches = []
        for m in legal:
            test = chess_board.copy(stack=False)
            test.push(m)
            if occupancy_from_chess(test) == current_occ:
                complete_matches.append(m)

        if complete_matches:
            # ترقية تنتج 4 حركات بنفس from→to و occupancy واحدة
            from_to_set = {(m.from_square, m.to_square) for m in complete_matches}
            if len(from_to_set) == 1 and any(m.promotion for m in complete_matches):
                fr, to = next(iter(from_to_set))
                from_to_uci = chess.square_name(fr) + chess.square_name(to)
                return {
                    'kind': 'promotion',
                    'from_to': from_to_uci,
                    'candidates': complete_matches,
                }
            if len(complete_matches) == 1:
                m = complete_matches[0]
                return {'kind': 'complete', 'move': m, 'uci': m.uci()}
            # حالة نظرية: عدة حركات قانونية مختلفة بنفس occupancy النهائي.
            # غير قابل للحدوث في شطرنج عادي، لكن نتعامل معها كـ ambiguous.
            return {
                'kind': 'anomaly',
                'disappeared': disappeared,
                'appeared': appeared,
                'reason': f'ambiguous_complete_matches={[m.uci() for m in complete_matches]}',
            }

        # محاولة 2: هل التغيير يطابق *حالة وسطية* لإحدى الحركات القانونية؟
        # حالة وسطية = كل خانة تغيّرت موجودة ضمن touched_squares لحركة قانونية.
        diff_set = set(disappeared) | set(appeared)
        partial_candidates = []
        for m in legal:
            touched = cls.squares_touched_by_move(chess_board, m)
            if diff_set.issubset(touched):
                # تحقق إضافي: التغييرات متّسقة مع اتجاه التنفيذ.
                # على سبيل المثال: لا يجب أن تظهر قطعة في خانة anchor=0
                # إذا الحركة لا تنتهي بقطعة هناك.
                if cls._consistent_with_move(chess_board, m, current_occ, anchor_occ):
                    partial_candidates.append(m)

        if partial_candidates:
            return {
                'kind': 'pending',
                'partial_for': partial_candidates,
                'disappeared': disappeared,
                'appeared': appeared,
            }

        # خلاف ذلك: anomaly
        return {
            'kind': 'anomaly',
            'disappeared': disappeared,
            'appeared': appeared,
            'reason': 'no_legal_move_matches',
        }

    @classmethod
    def _consistent_with_move(cls, board, move, current_occ, anchor_occ):
        """
        يتحقق من أن التغيرات الحالية تتفق مع تنفيذ جزئي للحركة.

        منطق التساهل: نسمح بحالتين انتقاليتين على أي خانة من خانات الحركة:
          1. خانة فرغت بشكل دائم (cur=0, final=0): سليم.
          2. خانة فارغة الآن لكن النهاية مشغولة (cur=0, final=1): "رفع
             مؤقت" — اللاعب رفع القطعة المأكولة ولسا ما حط مكانها. سليم.
          3. خانة امتلأت كما هو متوقع (cur=1, final=1): سليم.

        ما لا نقبله:
          - خانة امتلأت لكنها لا تنتهي مشغولة (تعني وضع قطعة في مكان خاطئ).
        وأيضاً: أي تغيير على خانة *خارج* خانات الحركة يعني التغيير لا يخص
        هذه الحركة (هذا مفحوص في classify عبر diff_set ⊆ touched).
        """
        test = board.copy(stack=False)
        test.push(move)
        final_occ = occupancy_from_chess(test)

        for r in range(8):
            for c in range(8):
                a = anchor_occ[r][c]
                f = final_occ[r][c]
                cur = current_occ[r][c]
                if cur == a:
                    continue  # ما تغيّرت
                # تغيّرت — نقبل (cur=f) أو (cur=0 و f=1) فقط.
                if cur == f:
                    continue
                if cur == 0 and f == 1:
                    continue  # رفع مؤقت لقطعة ستوضع لاحقاً
                return False
        return True


# ============================================================================
#  الأوضاع — Mode/State
# ============================================================================
class Mode:
    WAITING = 'WAITING'   # قبل وصول game_start
    ACTIVE = 'ACTIVE'     # دور اللاعب
    MONITOR = 'MONITOR'   # دور الروبوت (HvR) أو RvR
    LOCKED = 'LOCKED'     # منشورة حركة وننتظر board_state


# ============================================================================
#  ROS publishers/subscribers — wrappers تعمل بدون ROS أيضاً
# ============================================================================
class _DummyPub:
    def __init__(self, name):
        self.name = name

    def publish(self, msg):
        text = str(msg) if len(str(msg)) < 240 else str(msg)[:240] + '...'
        print(f"[ROS-sim PUB] {self.name}: {text}")


# ============================================================================
#  النود الرئيسية
# ============================================================================
class BoardTrackerNode:
    SCAN_RATE_HZ = 20
    STABILITY_CYCLES = 3      # عدد دورات استقرار قبل التصنيف
    LIFT_TIMEOUT_S = 30.0     # تنبيه إذا قطعة مرفوعة فترة طويلة
    # كم ننتظر board_state بعد نشر حركة قبل ما نعتبر إن نود ستوكفيش
    # غير متاحة ونعتمد على دفع الحركة المحلي. القيمة كبيرة بما يكفي
    # ليتمكن الذراع من تنفيذ الحركة المضادة وستوكفيش من الردّ.
    LOCKED_TIMEOUT_S = 30.0
    MISMATCH_GRACE_S = 3.0    # في MONITOR: لا نحذّر قبل هذه المهلة بعد آخر sync

    # التوبيكات — متوافقة مع disp3.2.py
    TOPIC_GAME_START = '/chess/game_start'
    TOPIC_BOARD_STATE = '/chess/board_state'
    TOPIC_MOVE = '/chess/move'
    TOPIC_PROMOTION_REQUEST = '/chess/promotion_request'
    TOPIC_STATUS = '/chess/status'
    TOPIC_OCCUPANCY = '/chess/board_occupancy'

    def __init__(self):
        # المصدر الذهني للحقيقة — يُحدَّث بشكل authoritative من board_state
        self.chess_board = chess.Board()

        # الحساسات — في الإنتاج: استبدل بكلاس GPIO
        self.sensor = SimulatedSensorBoard(occupancy_from_chess(self.chess_board))

        # حالة التشغيل
        self.mode = Mode.WAITING
        self.game_mode = None           # 'HvR' | 'RvR' | None
        self.human_color = None         # chess.WHITE | chess.BLACK | None
        self.last_board_state_time = 0.0

        # حالة LOCKED
        self.locked_uci = None
        self.locked_time = 0.0

        # حالة pending (في ACTIVE) — لطباعة تنبيهات
        self.pending_since = None
        self.last_pending_repr = None
        self.last_anomaly_repr = None
        self.last_mismatch_repr = None

        # الترقية المعلّقة
        self.pending_promotion_from_to = None   # 'e7e8' أو None

        # Anchor للتصنيف — ليش anchor منفصل عن chess_board؟
        # لأن chess_board قد يحتوي حركة "متفائلة" نحن دفعناها محلياً
        # قبل تأكيد ستوكفيش. anchor هي حالة الحساسات المتوقعة الآن.
        self._anchor_occ = occupancy_from_chess(self.chess_board)

        # مزامنة الخيوط
        self._shutdown = threading.Event()
        self._pending_event = threading.Event()
        self._move_processed_event = threading.Event()

        # السجلّ
        self._move_log = []

        # ROS
        if ROS_AVAILABLE:
            rospy.init_node('board_tracker', anonymous=False)
            self.pub_move = rospy.Publisher(self.TOPIC_MOVE, String, queue_size=10)
            self.pub_promo = rospy.Publisher(
                self.TOPIC_PROMOTION_REQUEST, String, queue_size=10)
            self.pub_status = rospy.Publisher(
                self.TOPIC_STATUS, String, queue_size=10)
            self.pub_occ = rospy.Publisher(
                self.TOPIC_OCCUPANCY, String, queue_size=10, latch=True)
            rospy.Subscriber(self.TOPIC_GAME_START, String, self._on_game_start)
            rospy.Subscriber(self.TOPIC_BOARD_STATE, String, self._on_board_state)
            self._log = rospy.loginfo
            self._warn = rospy.logwarn
            self._err = rospy.logerr
        else:
            self.pub_move = _DummyPub(self.TOPIC_MOVE)
            self.pub_promo = _DummyPub(self.TOPIC_PROMOTION_REQUEST)
            self.pub_status = _DummyPub(self.TOPIC_STATUS)
            self.pub_occ = _DummyPub(self.TOPIC_OCCUPANCY)
            self._log = lambda m: print(f"[INFO] {m}")
            self._warn = lambda m: print(f"[WARN] {m}")
            self._err = lambda m: print(f"[ERR ] {m}")

    # ========================================================================
    # ROS callbacks
    # ========================================================================
    def _on_game_start(self, msg):
        try:
            payload = json.loads(msg.data) if hasattr(msg, 'data') else json.loads(msg)
        except Exception as e:
            self._warn(f"game_start: invalid JSON ({e})")
            return

        mode = (payload.get('mode') or '').strip()
        if 'Robot vs Robot' in mode:
            self.game_mode = 'RvR'
            self.human_color = None
        elif 'Human vs Robot' in mode:
            self.game_mode = 'HvR'
            color = (payload.get('color') or 'white').strip().lower()
            self.human_color = chess.WHITE if color == 'white' else chess.BLACK
        else:
            self._warn(f"game_start: unknown mode {mode!r}")
            return

        # نبدأ بوضعية ابتدائية. لو وصلت board_state لاحقاً ستزامن.
        self.chess_board = chess.Board()
        self.sensor.load_from_chess_board(self.chess_board)
        self._anchor_occ = occupancy_from_chess(self.chess_board)
        self._move_log.clear()
        self.locked_uci = None
        self.pending_promotion_from_to = None
        self.pending_since = None
        self.last_pending_repr = None
        self.last_anomaly_repr = None
        self.last_mismatch_repr = None

        self._update_mode()
        self._publish_status(f"GAME_START mode={self.game_mode} human={self._color_name()}")
        self._log(f"game_start: mode={self.game_mode}, human_color={self._color_name()}, "
                  f"tracker_mode={self.mode}")
        self._publish_occupancy()

    def _on_board_state(self, msg):
        try:
            payload = json.loads(msg.data) if hasattr(msg, 'data') else json.loads(msg)
        except Exception as e:
            self._warn(f"board_state: invalid JSON ({e})")
            return

        # ندعم كلا الصيغتين: FEN مباشر، أو 2D board بتنسيق الدسبلاي.
        fen = payload.get('fen')
        if not fen and 'board' in payload:
            try:
                fen = board_2d_to_fen(payload['board'], payload.get('turn', 'white'))
            except Exception as e:
                self._warn(f"board_state: cannot convert 2D board ({e})")
                return

        if not fen:
            self._warn("board_state: no FEN/board info")
            return

        try:
            new_board = chess.Board(fen)
        except Exception as e:
            self._warn(f"board_state: invalid FEN {fen!r} ({e})")
            return

        self.chess_board = new_board
        self._anchor_occ = occupancy_from_chess(new_board)
        # في المحاكاة: نزامن الحساس مع board_state عشان نعكس
        # تأثير الذراع الميكانيكية (لأنه ما في ذراع حقيقية تنقل القطع).
        # على الراسبيري الحقيقي: الحساس قد عكس التغييرات تلقائياً قبل
        # وصول board_state، فهذا التحديث no-op (تطابق دائم).
        if isinstance(self.sensor, SimulatedSensorBoard):
            self.sensor.load_from_chess_board(new_board)
        self.last_board_state_time = time.time()
        self.locked_uci = None
        self.pending_promotion_from_to = None
        self.pending_since = None
        self.last_pending_repr = None
        self.last_anomaly_repr = None
        self.last_mismatch_repr = None
        self._update_mode()
        self._move_processed_event.set()
        self._publish_occupancy()
        self._log(f"board_state synced: turn={'w' if new_board.turn else 'b'}, "
                  f"mode={self.mode}, fen={new_board.fen()}")

    # ========================================================================
    # تحديد المود
    # ========================================================================
    def _update_mode(self):
        if self.game_mode is None:
            self.mode = Mode.WAITING
            return
        if self.game_mode == 'RvR':
            # Robot vs Robot: لا يوجد لاعب بشري يحرّك على اللوحة الفيزيائية.
            # الذراعان هما من يحرّكان القطع. لا حاجة لتعقّب الحساسات أبداً.
            # نبقى WAITING (inert كلياً) — لا تصنيف ولا mismatch ولا نشر.
            self.mode = Mode.WAITING
            return
        # HvR
        if self.locked_uci is not None or self.pending_promotion_from_to is not None:
            self.mode = Mode.LOCKED
            return
        if self.chess_board.turn == self.human_color:
            self.mode = Mode.ACTIVE
        else:
            self.mode = Mode.MONITOR

    def _color_name(self):
        if self.human_color is None:
            return 'none'
        return 'white' if self.human_color == chess.WHITE else 'black'

    # ========================================================================
    # نشر الرسائل
    # ========================================================================
    def _publish_status(self, text):
        self.pub_status.publish(text)

    def _publish_occupancy(self):
        payload = {
            'occupancy': self.sensor.scan(),
            'fen': self.chess_board.fen(),
            'mode': self.mode,
            'game_mode': self.game_mode,
            'human_color': self._color_name(),
            'moves': list(self._move_log),
        }
        self.pub_occ.publish(json.dumps(payload))

    def _publish_move(self, uci):
        self._log(f"PUBLISH /chess/move: {uci}")
        self.pub_move.publish(uci)
        self._move_log.append(uci)
        self.locked_uci = uci
        self.locked_time = time.time()
        # نطبّق محلياً بشكل متفائل — board_state سيؤكّد أو يعدّل لاحقاً.
        try:
            mv = chess.Move.from_uci(uci)
            self.chess_board.push(mv)
            self._anchor_occ = occupancy_from_chess(self.chess_board)
            # في المحاكاة فقط: نمنع الكاشف من اعتبار التغييرات الفيزيائية
            # السابقة "حركة جديدة" لأن chess_board تقدّم خطوة. الحساس بنفسه
            # قد عكس التغييرات (المستخدم حرّكها فعلاً)، فلا نلمسه.
        except Exception as e:
            self._warn(f"local push failed for {uci}: {e}")
        self._update_mode()
        self._publish_occupancy()

    def _publish_promotion_request(self, from_to):
        payload = {
            'move': from_to,
            'color': self._color_name() if self.human_color is not None else 'unknown',
        }
        self._log(f"PUBLISH /chess/promotion_request: {payload}")
        self.pub_promo.publish(json.dumps(payload))
        self.pending_promotion_from_to = from_to
        self._update_mode()

    # ========================================================================
    # حلقة المسح
    # ========================================================================
    def scan_loop(self):
        period = 1.0 / self.SCAN_RATE_HZ
        last_occ = self.sensor.scan()
        stable_count = 0

        while not self._shutdown.is_set():
            if ROS_AVAILABLE and rospy.is_shutdown():
                break

            current = self.sensor.scan()
            if current == last_occ:
                stable_count += 1
            else:
                stable_count = 0
                last_occ = current

            if stable_count == self.STABILITY_CYCLES:
                try:
                    self._dispatch_stable(current)
                except Exception as e:
                    self._err(f"dispatch error: {e}")

            # في LOCKED مع timeout: نعتبر التنفيذ المحلي كافي ونفك القفل.
            if (self.mode == Mode.LOCKED and self.locked_uci is not None
                    and time.time() - self.locked_time > self.LOCKED_TIMEOUT_S):
                self._warn(f"LOCKED timeout for {self.locked_uci} — "
                           f"no /chess/board_state arrived. Falling back to local state.")
                self.locked_uci = None
                self._update_mode()

            self._pending_event.wait(timeout=period)
            self._pending_event.clear()

    def _dispatch_stable(self, current_occ):
        if self.mode == Mode.WAITING:
            # لا game_start بعد — inert تماماً. لا تصنيف ولا تنبيهات.
            # اللاعب قد يكون يجهّز اللوحة أو يجرّب الحساسات.
            return
        if self.mode == Mode.LOCKED:
            return  # لا تصنيف، ننتظر board_state
        if self.mode == Mode.MONITOR:
            self._monitor_mismatch_check(current_occ)
            return
        if self.mode == Mode.ACTIVE:
            self._active_classify(current_occ)

    # ----------- ACTIVE: تصنيف الحركة وردّ الفعل -----------
    def _active_classify(self, current_occ):
        if self.pending_promotion_from_to is not None:
            return  # اللاعب طلب ترقية وننتظر الدسبلاي

        result = MoveDetector.classify(self.chess_board, current_occ, self._anchor_occ)
        kind = result['kind']

        if kind == 'clean':
            self.pending_since = None
            self.last_pending_repr = None
            self.last_anomaly_repr = None
            return

        if kind == 'complete':
            self._publish_move(result['uci'])
            self.pending_since = None
            return

        if kind == 'promotion':
            self._publish_promotion_request(result['from_to'])
            return

        if kind == 'pending':
            now = time.time()
            if self.pending_since is None:
                self.pending_since = now
            repr_ = (tuple(result['disappeared']), tuple(result['appeared']))
            if repr_ != self.last_pending_repr:
                self.last_pending_repr = repr_
                self._log(f"PENDING — disappeared={result['disappeared']} "
                          f"appeared={result['appeared']}")
            elapsed = now - self.pending_since
            if elapsed > self.LIFT_TIMEOUT_S and elapsed < self.LIFT_TIMEOUT_S + 0.5:
                self._publish_status(
                    f"WARN: pieces still off-board after {int(elapsed)}s "
                    f"(disappeared={result['disappeared']})")
            return

        if kind == 'anomaly':
            repr_ = (tuple(result['disappeared']), tuple(result['appeared']),
                     result.get('reason', ''))
            if repr_ != self.last_anomaly_repr:
                self.last_anomaly_repr = repr_
                msg = (f"ANOMALY: invalid board state "
                       f"(disappeared={result['disappeared']} "
                       f"appeared={result['appeared']} "
                       f"reason={result.get('reason')})")
                self._warn(msg)
                self._publish_status(msg)
            return

    # ----------- MONITOR: تنبيه عند عدم تطابق مستقر -----------
    def _monitor_mismatch_check(self, current_occ):
        expected = occupancy_from_chess(self.chess_board)
        if current_occ == expected:
            self.last_mismatch_repr = None
            return

        # لا نحذّر قبل grace period بعد آخر sync — الذراع قد تكون تتحرك.
        if (self.last_board_state_time > 0
                and time.time() - self.last_board_state_time < self.MISMATCH_GRACE_S):
            return

        disappeared, appeared = diff_occupancy(expected, current_occ)
        repr_ = (tuple(disappeared), tuple(appeared))
        if repr_ == self.last_mismatch_repr:
            return  # حذّرنا مسبقاً
        self.last_mismatch_repr = repr_
        msg = (f"BOARD MISMATCH ({self.mode}): physical board differs from "
               f"expected. missing={disappeared} extra={appeared}")
        self._warn(msg)
        self._publish_status(msg)

    # ========================================================================
    # واجهة التيرمينال — للاختبار
    # ========================================================================
    HELP_TEXT = """
Commands (testing/simulation):
  e2e4 | e2 e4 | e7e8q     يطبّق التغييرات الفيزيائية على المحاكي
                            (يحاكي حركة لاعب على اللوحة الحقيقية)
  lift e2                  يرفع قطعة (sensor 1→0)
  place e4                 يضع قطعة (sensor 0→1)
  toggle e2                يقلب حالة خانة
  show                     يعرض اللوحة الذهنية الحالية
  occ                      يعرض مصفوفة الحساسات الخام 8x8
  fen                      يعرض FEN الحالية
  moves                    يعرض الحركات المنشورة
  state                    يعرض mode + game_mode + human_color
  start_white              يحاكي game_start: HvR + اللاعب أبيض
  start_black              يحاكي game_start: HvR + اللاعب أسود
  start_rvr                يحاكي game_start: Robot vs Robot
  sync_fen <fen>           يحاكي board_state بـ FEN معطاة
  pubpromo q|r|b|n         يحاكي رد الدسبلاي على ترقية معلّقة
  reset                    يعيد الوضعية الابتدائية ويلغي game info
  help / quit
"""

    def terminal_loop(self):
        print(self.HELP_TEXT)
        # في وضع البدء بدون game_start، نفترض HvR/أبيض للاختبار
        if self.game_mode is None:
            print("(no game_start received yet — type 'start_white' to begin testing)")
        while not self._shutdown.is_set():
            if ROS_AVAILABLE and rospy.is_shutdown():
                break
            try:
                prompt = f"[{self.mode}|{self._color_name()}#{len(self._move_log)+1}] > "
                line = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                self._shutdown.set()
                break
            if not line:
                continue
            try:
                self._dispatch_terminal(line)
            except Exception as e:
                self._err(f"terminal error: {e}")
            self._pending_event.set()
            time.sleep(0.05)

    def _dispatch_terminal(self, line):
        low = line.lower()
        parts = low.split()

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
            print(' '.join(self._move_log) or '(no moves)')
            return
        if low == 'state':
            print(f"mode={self.mode}  game_mode={self.game_mode}  "
                  f"human={self._color_name()}  turn={'white' if self.chess_board.turn else 'black'}  "
                  f"locked={self.locked_uci}  promo_pending={self.pending_promotion_from_to}")
            return
        if low == 'reset':
            self.chess_board = chess.Board()
            self.sensor.load_from_chess_board(self.chess_board)
            self._anchor_occ = occupancy_from_chess(self.chess_board)
            self._move_log.clear()
            self.game_mode = None
            self.human_color = None
            self.locked_uci = None
            self.pending_promotion_from_to = None
            self._update_mode()
            print("Board reset.")
            return

        if low.startswith('start_'):
            return self._handle_start_command(low)

        if low.startswith('sync_fen '):
            fen = line.split(None, 1)[1]
            self._on_board_state(_FakeMsg(json.dumps({'fen': fen})))
            return

        if low.startswith('pubpromo '):
            piece = parts[1] if len(parts) >= 2 else ''
            if piece not in ('q', 'r', 'b', 'n'):
                print("usage: pubpromo q|r|b|n")
                return
            if not self.pending_promotion_from_to:
                print("no pending promotion.")
                return
            full_uci = self.pending_promotion_from_to + piece
            print(f"(simulating display publishing {full_uci} on /chess/move)")
            # نتعامل معها كأنها حركة من الدسبلاي: نطبّقها محلياً.
            self.pending_promotion_from_to = None
            try:
                mv = chess.Move.from_uci(full_uci)
                if mv not in self.chess_board.legal_moves:
                    print(f"illegal promotion: {full_uci}")
                    return
                self.chess_board.push(mv)
                self.sensor.load_from_chess_board(self.chess_board)
                self._anchor_occ = occupancy_from_chess(self.chess_board)
                self._move_log.append(full_uci)
                self._update_mode()
                self._publish_occupancy()
                print_chess_board(self.chess_board)
            except Exception as e:
                print(f"error: {e}")
            return

        if low.startswith('lift '):
            sq = parts[1]
            if not _is_valid_square(sq):
                print(f"bad square: {sq}"); return
            self.sensor.set_square(sq, 0)
            return
        if low.startswith('place '):
            sq = parts[1]
            if not _is_valid_square(sq):
                print(f"bad square: {sq}"); return
            self.sensor.set_square(sq, 1)
            return
        if low.startswith('toggle '):
            sq = parts[1]
            if not _is_valid_square(sq):
                print(f"bad square: {sq}"); return
            r, c = sq_to_rc(sq)
            cur = self.sensor.scan()[r][c]
            self.sensor.set_square(sq, 0 if cur else 1)
            return

        # محاولة تفسيرها كحركة (e2e4, e7e8q, e2 e4)
        from_sq, to_sq, promo = _parse_move_tokens(line)
        if from_sq is None:
            print(f"unknown command: {line!r}. Type 'help'.")
            return
        self._simulate_physical_move(from_sq, to_sq, promo)

    def _handle_start_command(self, low):
        if low == 'start_white':
            payload = {'mode': 'Human vs Robot', 'color': 'White', 'difficulty': 'Medium'}
        elif low == 'start_black':
            payload = {'mode': 'Human vs Robot', 'color': 'Black', 'difficulty': 'Medium'}
        elif low == 'start_rvr':
            payload = {'mode': 'Robot vs Robot',
                       'robot1': {'color': 'White', 'difficulty': 'Medium'},
                       'robot2': {'color': 'Black', 'difficulty': 'Medium'}}
        else:
            print(f"unknown: {low}"); return
        self._on_game_start(_FakeMsg(json.dumps(payload)))

    def _simulate_physical_move(self, from_sq, to_sq, promo):
        """يحاكي تحريك قطعة فيزيائياً على المحاكي. ينعكس عبر حلقة المسح."""
        try:
            piece = self.chess_board.piece_at(chess.parse_square(from_sq))
        except Exception:
            print(f"bad squares: {from_sq}, {to_sq}"); return
        if piece is None:
            print(f"no piece at {from_sq}"); return

        # في وضع MONITOR/RvR، تطبيق التغييرات الفيزيائية يعني محاكاة الذراع.
        # نطبّقها مباشرة كحركة كاملة بدون انتظار تصنيف من الكاشف.
        if self.mode != Mode.ACTIVE:
            mode_before = self.mode
            # نحتاج promo في الترقية
            if piece.piece_type == chess.PAWN:
                rk = int(to_sq[1])
                if (piece.color == chess.WHITE and rk == 8) or \
                   (piece.color == chess.BLACK and rk == 1):
                    promo = promo or 'q'
            uci = from_sq + to_sq + (promo or '')
            try:
                mv = chess.Move.from_uci(uci)
            except Exception:
                print(f"invalid uci: {uci}"); return
            if mv not in self.chess_board.legal_moves:
                print(f"illegal: {uci}"); return
            self.chess_board.push(mv)
            self.sensor.load_from_chess_board(self.chess_board)
            self._anchor_occ = occupancy_from_chess(self.chess_board)
            self._move_log.append(uci)
            self._update_mode()
            self._publish_occupancy()
            print(f"(simulated {mode_before}-side physical move {uci} — "
                  f"NOT published on /chess/move)")
            print_chess_board(self.chess_board)
            return

        # ACTIVE: نطبّق التغييرات الفيزيائية بنفس ترتيب اللاعب الواقعي.
        # حلقة المسح ستكشف وتنشر.
        # لاحظ: لا نمرّر `promo` هنا — في الترقية نحرّك البيدق فقط ثم
        # ننتظر الدسبلاي يبعت /chess/move بحرف القطعة.
        try:
            mv_attempt = chess.Move.from_uci(from_sq + to_sq + (promo or 'q'))
        except Exception:
            mv_attempt = None
        is_capture = bool(mv_attempt and self.chess_board.is_capture(mv_attempt))
        is_ep = bool(mv_attempt and self.chess_board.is_en_passant(mv_attempt))
        is_castling = bool(mv_attempt and self.chess_board.is_castling(mv_attempt))

        scan_period = 1.0 / self.SCAN_RATE_HZ
        wait_step = max(scan_period * 2, 0.05)

        self._move_processed_event.clear()

        if is_ep:
            cap_sq = to_sq[0] + from_sq[1]
            self.sensor.apply_changes({cap_sq: 0}); time.sleep(wait_step)
            self.sensor.apply_changes({from_sq: 0}); time.sleep(wait_step)
            self.sensor.apply_changes({to_sq: 1})
        elif is_capture:
            self.sensor.apply_changes({to_sq: 0}); time.sleep(wait_step)
            self.sensor.apply_changes({from_sq: 0, to_sq: 1})
        elif is_castling:
            rmap = {'e1g1': ('h1', 'f1'), 'e1c1': ('a1', 'd1'),
                    'e8g8': ('h8', 'f8'), 'e8c8': ('a8', 'd8')}
            rf, rt = rmap[from_sq + to_sq]
            self.sensor.apply_changes({from_sq: 0, to_sq: 1, rf: 0, rt: 1})
        else:
            self.sensor.apply_changes({from_sq: 0, to_sq: 1})

        # نستنّى scan loop يكتشف وينشر / يطلب ترقية.
        self._pending_event.set()
        self._move_processed_event.wait(timeout=1.5)

    # ========================================================================
    # تشغيل
    # ========================================================================
    def verify_initial_setup(self, timeout=None):
        self._log("Verifying initial setup (32 pieces in starting position)...")
        expected = occupancy_from_chess(chess.Board())
        start = time.time()
        warned = False
        while not self._shutdown.is_set():
            if ROS_AVAILABLE and rospy.is_shutdown():
                return False
            if self.sensor.scan() == expected:
                self._log("OK — initial position confirmed.")
                return True
            if not warned:
                self._warn("Board NOT in initial position. "
                           "Place all 32 pieces, then send /chess/game_start.")
                warned = True
            if timeout and (time.time() - start) > timeout:
                return False
            time.sleep(0.3)
        return False

    def run(self):
        self._publish_occupancy()
        if not self.verify_initial_setup():
            return
        print_chess_board(self.chess_board)

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
            self._log("board_tracker shutting down.")


# ============================================================================
#  أدوات التيرمينال
# ============================================================================
def print_chess_board(board: chess.Board):
    print()
    print("    a   b   c   d   e   f   g   h")
    print("  +---+---+---+---+---+---+---+---+")
    for rank in range(7, -1, -1):
        line = f"{rank+1} |"
        for file in range(8):
            p = board.piece_at(chess.square(file, rank))
            cell = f" {p.symbol()} " if p else " . "
            line += cell + "|"
        line += f" {rank+1}"
        print(line)
        print("  +---+---+---+---+---+---+---+---+")
    print("    a   b   c   d   e   f   g   h")
    turn = 'white' if board.turn == chess.WHITE else 'black'
    print(f"  Turn: {turn}   FEN: {board.fen()}\n")


def _is_valid_square(sq):
    return isinstance(sq, str) and len(sq) == 2 and sq[0] in FILES and sq[1] in RANKS


def _parse_move_tokens(line):
    parts = line.lower().split()
    if len(parts) == 1:
        tok = parts[0]
        if len(tok) == 4:
            return tok[:2], tok[2:4], None
        if len(tok) == 5:
            return tok[:2], tok[2:4], tok[4]
    elif len(parts) == 2 and _is_valid_square(parts[0]) and _is_valid_square(parts[1]):
        return parts[0], parts[1], None
    elif len(parts) == 3 and _is_valid_square(parts[0]) and _is_valid_square(parts[1]) \
            and parts[2] in ('q', 'r', 'b', 'n'):
        return parts[0], parts[1], parts[2]
    return None, None, None


class _FakeMsg:
    """يحاكي std_msgs/String للنداءات الداخلية."""
    def __init__(self, data):
        self.data = data


# ============================================================================
#  Entry point
# ============================================================================
def main():
    node = BoardTrackerNode()
    node.run()


if __name__ == '__main__':
    if ROS_AVAILABLE:
        try:
            main()
        except rospy.ROSInterruptException:
            pass
    else:
        print("[WARN] rospy not available — running in dry-run / simulation mode.")
        main()
