#!/usr/bin/env python3
"""
board_tracker_v2.py — Physical Board Tracker (Raspberry Pi)
═══════════════════════════════════════════════════════════════════
يتتبع الحركات الفيزيائية على رقعة الشطرنج عبر مصفوفة Hall-effect sensors.

ROS Topics:
  Publishers:
    /chess/move               → UCI string (حركة كاملة)
    /chess/promotion_request  → JSON {move: 'e7e8'} (حركة ترقية بدون حرف القطعة)

  Subscribers:
    /chess/game_start    ← JSON settings (لبدء التتبع)
    /chess/board_state   ← JSON {board, fen, ...} (لتزامن الحالة بعد حركة الروبوت)
    /chess/turn_signal   ← JSON {active, turn, human_color, mode, fen} (لمعرفة متى يتتبع)
    /chess/game_stop     ← 'stop' (لإيقاف التتبع)
    /chess/pause         ← 'pause'/'resume' (لإيقاف مؤقت)

Communication Flow (HvR):
  1. chess_engine ينشر turn_signal {active: true} → board_tracker يبدأ يتتبع
  2. الإنسان يحرّك قطعة → board_tracker يكتشف → ينشر /chess/move
  3. chess_engine يعالج الحركة → ينشر board_state + turn_signal {active: false}
  4. الروبوت يلعب → chess_engine ينشر board_state → board_tracker يتزامن
  5. chess_engine ينشر turn_signal {active: true} → يرجع لخطوة 2

Communication Flow (Promotion):
  1. board_tracker يكتشف بيدق وصل rank 1/8 → ينشر /chess/promotion_request
  2. chess_engine يبعث __PROMOTION__ للـ display
  3. display يعرض نافذة → المستخدم يختار → ينشر /chess/move (مع حرف الترقية)
"""

import time
import os
import sys
import json
import threading
import copy

try:
    import RPi.GPIO as GPIO
    HAS_GPIO = True
except ImportError:
    HAS_GPIO = False

import chess

try:
    import rospy
    from std_msgs.msg import String
    ROS_AVAILABLE = True
except ImportError:
    ROS_AVAILABLE = False

# ═════════════════════════════════════════════════════════════════
#  GPIO Configuration
# ═════════════════════════════════════════════════════════════════
ROW_PINS = [4, 17, 27, 22, 5, 6, 13, 19]
COL_PINS = [26, 21, 20, 16, 12, 25, 24, 23]

FILES = 'abcdefgh'
RANKS = '12345678'


def sq_to_rc(sq):
    return 8 - int(sq[1]), FILES.index(sq[0])


def rc_to_sq(row, col):
    return FILES[col] + str(8 - row)


def occupancy_from_chess(board):
    occ = [[0] * 8 for _ in range(8)]
    for r in range(8):
        for c in range(8):
            sq = chess.square(c, 7 - r)
            if board.piece_at(sq) is not None:
                occ[r][c] = 1
    return occ


def diff_occupancy(prev, curr):
    disappeared = []
    appeared = []
    for r in range(8):
        for c in range(8):
            if prev[r][c] == 1 and curr[r][c] == 0:
                disappeared.append(rc_to_sq(r, c))
            elif prev[r][c] == 0 and curr[r][c] == 1:
                appeared.append(rc_to_sq(r, c))
    return disappeared, appeared


# ═════════════════════════════════════════════════════════════════
#  GPIO Sensor Board
# ═════════════════════════════════════════════════════════════════
class GPIOSensorBoard:
    def __init__(self):
        if not HAS_GPIO:
            raise RuntimeError("RPi.GPIO not available")
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        for pin in ROW_PINS:
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        for pin in COL_PINS:
            GPIO.setup(pin, GPIO.OUT)
            GPIO.output(pin, GPIO.HIGH)

    def scan(self):
        board = [[0] * 8 for _ in range(8)]
        for c in range(8):
            GPIO.output(COL_PINS[c], GPIO.LOW)
            time.sleep(0.0003)
            for r in range(8):
                if GPIO.input(ROW_PINS[r]) == GPIO.LOW:
                    board[r][c] = 1
            GPIO.output(COL_PINS[c], GPIO.HIGH)
        return board

    def cleanup(self):
        GPIO.cleanup()


# ═════════════════════════════════════════════════════════════════
#  Move Detection Logic
# ═════════════════════════════════════════════════════════════════
class MoveDetector:
    @staticmethod
    def squares_touched(board, move):
        sqs = {chess.square_name(move.from_square), chess.square_name(move.to_square)}
        if board.is_en_passant(move):
            ep_file = chess.square_file(move.to_square)
            ep_rank = chess.square_rank(move.from_square)
            sqs.add(chess.square_name(chess.square(ep_file, ep_rank)))
        if board.is_castling(move):
            rank = chess.square_rank(move.from_square)
            if chess.square_file(move.to_square) > chess.square_file(move.from_square):
                sqs.add(chess.square_name(chess.square(7, rank)))
                sqs.add(chess.square_name(chess.square(5, rank)))
            else:
                sqs.add(chess.square_name(chess.square(0, rank)))
                sqs.add(chess.square_name(chess.square(3, rank)))
        return sqs

    @classmethod
    def classify(cls, chess_board, current_occ, anchor_occ):
        disappeared, appeared = diff_occupancy(anchor_occ, current_occ)
        if not disappeared and not appeared:
            return {'kind': 'clean'}

        legal = list(chess_board.legal_moves)
        complete = []
        for m in legal:
            t = chess_board.copy(stack=False)
            t.push(m)
            if occupancy_from_chess(t) == current_occ:
                complete.append(m)

        if complete:
            ft = {(m.from_square, m.to_square) for m in complete}
            if len(ft) == 1 and any(m.promotion for m in complete):
                fr, to = next(iter(ft))
                return {
                    'kind': 'promotion',
                    'from_to': chess.square_name(fr) + chess.square_name(to),
                    'candidates': complete,
                }
            if len(complete) == 1:
                return {'kind': 'complete', 'move': complete[0], 'uci': complete[0].uci()}
            return {
                'kind': 'anomaly',
                'disappeared': disappeared,
                'appeared': appeared,
                'reason': 'ambiguous',
            }

        diff_set = set(disappeared) | set(appeared)
        partial = []
        for m in legal:
            touched = cls.squares_touched(chess_board, m)
            if diff_set.issubset(touched):
                if cls._consistent(chess_board, m, current_occ, anchor_occ):
                    partial.append(m)
        if partial:
            return {
                'kind': 'pending',
                'partial_for': partial,
                'disappeared': disappeared,
                'appeared': appeared,
            }
        return {
            'kind': 'anomaly',
            'disappeared': disappeared,
            'appeared': appeared,
            'reason': 'no_match',
        }

    @classmethod
    def _consistent(cls, board, move, current_occ, anchor_occ):
        t = board.copy(stack=False)
        t.push(move)
        final = occupancy_from_chess(t)
        for r in range(8):
            for c in range(8):
                a = anchor_occ[r][c]
                cur = current_occ[r][c]
                f = final[r][c]
                if cur == a:
                    continue
                if cur == f:
                    continue
                if cur == 0 and f == 1:
                    continue
                return False
        return True


# ═════════════════════════════════════════════════════════════════
#  Mode State Machine
# ═════════════════════════════════════════════════════════════════
class Mode:
    WAITING = 'WAITING'
    ACTIVE = 'ACTIVE'
    MONITOR = 'MONITOR'
    LOCKED = 'LOCKED'


# ═════════════════════════════════════════════════════════════════
#  Main Board Tracker Node
# ═════════════════════════════════════════════════════════════════
class BoardTrackerRPi:
    SCAN_RATE_HZ = 20
    STABILITY_CYCLES = 3
    LOCKED_TIMEOUT_S = 30.0
    LIFT_TIMEOUT_S = 30.0
    MISMATCH_GRACE_S = 3.0

    def __init__(self):
        self.chess_board = chess.Board()
        self.sensor = GPIOSensorBoard()
        self.mode = Mode.WAITING
        self.game_mode = None          # 'HvR' or 'RvR'
        self.human_color = None
        self.game_active = False       # هل اللعبة شغّالة
        self.paused = False            # هل اللعبة متوقفة مؤقتاً
        self.tracker_active = False    # هل دور الإنسان (يجب التتبع)
        self.last_board_state_time = 0.0
        self.locked_uci = None
        self.locked_time = 0.0
        self.pending_since = None
        self.last_pending_repr = None
        self.last_anomaly_repr = None
        self.last_mismatch_repr = None
        self.pending_promotion_from_to = None
        self._anchor_occ = occupancy_from_chess(self.chess_board)
        self._shutdown = threading.Event()
        self._move_log = []

        if ROS_AVAILABLE:
            rospy.init_node('board_tracker', anonymous=False)
            # Publishers
            self.pub_move = rospy.Publisher('/chess/move', String, queue_size=10)
            self.pub_promo = rospy.Publisher('/chess/promotion_request', String, queue_size=10)

            # Subscribers
            rospy.Subscriber('/chess/game_start', String, self._on_game_start)
            rospy.Subscriber('/chess/board_state', String, self._on_board_state)
            rospy.Subscriber('/chess/turn_signal', String, self._on_turn_signal)
            rospy.Subscriber('/chess/game_stop', String, self._on_game_stop)
            rospy.Subscriber('/chess/pause', String, self._on_pause)

            self._log = rospy.loginfo
            self._warn = rospy.logwarn
        else:
            self.pub_move = None
            self.pub_promo = None
            self._log = lambda m: print("[INFO] " + m)
            self._warn = lambda m: print("[WARN] " + m)

    # ─── Publishers ────────────────────────────────────────────────
    def _pub(self, pub, msg):
        if pub:
            pub.publish(msg)
        else:
            print("[PUB] " + str(msg)[:200])

    # ─── ROS Callbacks ─────────────────────────────────────────────
    def _on_game_start(self, msg):
        """بداية لعبة جديدة — reset كل شيء"""
        try:
            p = json.loads(msg.data)
        except Exception:
            return
        mode = p.get('mode', '')
        if 'Robot vs Robot' in mode:
            self.game_mode = 'RvR'
            self.human_color = None
            self.tracker_active = False  # في RvR لا نتتبع
        elif 'Human vs Robot' in mode:
            self.game_mode = 'HvR'
            color = p.get('color', 'white').strip().lower()
            self.human_color = chess.WHITE if color == 'white' else chess.BLACK
            # لو الإنسان أبيض — بنبدأ بالتتبع فوراً
            self.tracker_active = (color == 'white')
        else:
            return

        self.chess_board = chess.Board()
        self._anchor_occ = occupancy_from_chess(self.chess_board)
        self._move_log = []
        self.locked_uci = None
        self.pending_promotion_from_to = None
        self.game_active = True
        self.paused = False
        self._update_mode()
        self._log(f"game_start: mode={self.game_mode} human_color={color} tracker={self.mode}")

    def _on_board_state(self, msg):
        """
        استقبال حالة اللوحة من chess_engine.
        يُستخدم لتزامن الـ chess_board و anchor_occ بعد حركة الروبوت.
        """
        try:
            p = json.loads(msg.data)
        except Exception:
            return

        # نستخدم FEN للتزامن الدقيق
        fen = p.get('fen')
        if fen:
            try:
                self.chess_board = chess.Board(fen)
                self._anchor_occ = occupancy_from_chess(self.chess_board)
                self.last_board_state_time = time.time()
                self.locked_uci = None
                self.pending_promotion_from_to = None
                self._log(f"board_state synced via FEN: {fen.split()[0][:20]}...")
            except Exception as e:
                self._warn(f"FEN parse error: {e}")
                return
        else:
            # Fallback: لو ما في fen (توافق مع chess_node القديم)
            self._warn("board_state without FEN — cannot sync precisely!")

        self._update_mode()

    def _on_turn_signal(self, msg):
        """
        إشارة الدور من chess_engine.
        active=true يعني: دور الإنسان، ابدأ بالتتبع.
        active=false يعني: دور الروبوت، لا تتتبع.
        """
        try:
            p = json.loads(msg.data)
        except Exception:
            return

        self.tracker_active = p.get('active', False)

        # تزامن إضافي لو في fen
        fen = p.get('fen')
        if fen:
            try:
                new_board = chess.Board(fen)
                if new_board.fen() != self.chess_board.fen():
                    self.chess_board = new_board
                    self._anchor_occ = occupancy_from_chess(self.chess_board)
            except Exception:
                pass

        self._update_mode()
        self._log(f"turn_signal: active={self.tracker_active} mode={self.mode}")

    def _on_game_stop(self, msg):
        """إيقاف اللعبة"""
        if msg.data == "stop":
            self.game_active = False
            self.tracker_active = False
            self.paused = False
            self.mode = Mode.WAITING
            self._log("Game stopped — tracker WAITING.")

    def _on_pause(self, msg):
        """إيقاف مؤقت / استئناف"""
        if msg.data == "pause":
            self.paused = True
            self._log("Game paused — tracker paused.")
        elif msg.data == "resume":
            self.paused = False
            self._update_mode()
            self._log("Game resumed — tracker resumed.")

    # ─── Mode Management ──────────────────────────────────────────
    def _update_mode(self):
        if not self.game_active:
            self.mode = Mode.WAITING
        elif self.paused:
            self.mode = Mode.WAITING
        elif self.game_mode == 'RvR':
            self.mode = Mode.WAITING    # في RvR لا نتتبع
        elif self.locked_uci or self.pending_promotion_from_to:
            self.mode = Mode.LOCKED
        elif self.tracker_active:
            self.mode = Mode.ACTIVE
        else:
            self.mode = Mode.MONITOR

    # ─── Move Publishing ──────────────────────────────────────────
    def _publish_move(self, uci):
        self._log(f"PUBLISH /chess/move: {uci}")
        self._pub(self.pub_move, uci)
        self._move_log.append(uci)
        self.locked_uci = uci
        self.locked_time = time.time()
        # نحدّث اللوحة المحلية بعد الإرسال
        try:
            self.chess_board.push(chess.Move.from_uci(uci))
            self._anchor_occ = occupancy_from_chess(self.chess_board)
        except Exception:
            pass
        self.tracker_active = False  # الدور انتقل للروبوت
        self._update_mode()

    def _publish_promo(self, from_to):
        payload = json.dumps({'move': from_to})
        self._log(f"PUBLISH /chess/promotion_request: {payload}")
        self._pub(self.pub_promo, payload)
        self.pending_promotion_from_to = from_to
        self._update_mode()

    # ─── Scan Loop ────────────────────────────────────────────────
    def scan_loop(self):
        period = 1.0 / self.SCAN_RATE_HZ
        last_occ = self.sensor.scan()
        stable = 0

        while not self._shutdown.is_set():
            if ROS_AVAILABLE and rospy.is_shutdown():
                break

            current = self.sensor.scan()
            if current == last_occ:
                stable += 1
            else:
                stable = 0
                last_occ = current

            if stable == self.STABILITY_CYCLES:
                self._dispatch(current)

            # Locked timeout
            if self.mode == Mode.LOCKED and self.locked_uci:
                if time.time() - self.locked_time > self.LOCKED_TIMEOUT_S:
                    self._warn(f"LOCKED timeout for {self.locked_uci}")
                    self.locked_uci = None
                    self._update_mode()

            time.sleep(period)

    def _dispatch(self, occ):
        if self.mode == Mode.WAITING:
            return
        if self.mode == Mode.LOCKED:
            return
        if self.mode == Mode.MONITOR:
            self._check_mismatch(occ)
            return
        if self.mode == Mode.ACTIVE:
            self._classify(occ)

    def _classify(self, occ):
        if self.pending_promotion_from_to:
            return
        result = MoveDetector.classify(self.chess_board, occ, self._anchor_occ)
        kind = result['kind']

        if kind == 'clean':
            self.pending_since = None
            return
        if kind == 'complete':
            self._publish_move(result['uci'])
            self.pending_since = None
            return
        if kind == 'promotion':
            self._publish_promo(result['from_to'])
            return
        if kind == 'pending':
            if self.pending_since is None:
                self.pending_since = time.time()
                self._log(f"PENDING: {result.get('disappeared', [])}")
            return
        if kind == 'anomaly':
            r = (tuple(result.get('disappeared', [])), tuple(result.get('appeared', [])))
            if r != self.last_anomaly_repr:
                self.last_anomaly_repr = r
                self._warn(f"ANOMALY: {result}")

    def _check_mismatch(self, occ):
        expected = occupancy_from_chess(self.chess_board)
        if occ == expected:
            self.last_mismatch_repr = None
            return
        if time.time() - self.last_board_state_time < self.MISMATCH_GRACE_S:
            return
        d, a = diff_occupancy(expected, occ)
        r = (tuple(d), tuple(a))
        if r != self.last_mismatch_repr:
            self.last_mismatch_repr = r
            self._warn(f"MISMATCH: missing={d} extra={a}")

    # ─── Initial Verification ─────────────────────────────────────
    def verify_initial(self):
        self._log("Waiting for 32 pieces...")
        expected = occupancy_from_chess(chess.Board())
        warned = False
        while not self._shutdown.is_set():
            if ROS_AVAILABLE and rospy.is_shutdown():
                return False
            if self.sensor.scan() == expected:
                self._log("OK - All 32 pieces in place!")
                return True
            if not warned:
                self._warn("Board not ready. Place all pieces.")
                warned = True
            time.sleep(0.5)
        return False

    # ─── Main Entry Point ─────────────────────────────────────────
    def run(self):
        self._log("Board Tracker RPi v2 starting...")
        if not self.verify_initial():
            return
        self._log("Scan loop running. Waiting for /chess/game_start...")
        t = threading.Thread(target=self.scan_loop, daemon=True)
        t.start()
        try:
            if ROS_AVAILABLE:
                rospy.spin()
            else:
                while not self._shutdown.is_set():
                    time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown.set()
            t.join(timeout=2)
            self.sensor.cleanup()
            self._log("Shutdown complete.")


if __name__ == '__main__':
    node = BoardTrackerRPi()
    node.run()
