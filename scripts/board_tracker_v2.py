#!/usr/bin/env python3
"""
board_tracker_v2.py — Physical Board Tracker (Raspberry Pi)
═══════════════════════════════════════════════════════════════════
يتتبع الحركات الفيزيائية على رقعة الشطرنج.

آلية العمل:
  - اللاعب يحرّك القطعة على الرقعة الفيزيائية
  - لمّا يخلص يكبس ENTER (محاكاة Push Button)
  - عندها النود يقرأ السينسورز ويقارن مع الحالة السابقة
  - لو الحركة قانونية → ينشرها على /chess/move

ROS Topics:
  Publishers:
    /chess/move               → UCI string
    /chess/promotion_request  → JSON {move: 'e7e8'}

  Subscribers:
    /chess/game_start    ← JSON settings
    /chess/board_state   ← JSON {board, fen, ...}
    /chess/turn_signal   ← JSON {active, turn, human_color, mode, fen}
    /chess/game_stop     ← 'stop'
    /chess/pause         ← 'pause'/'resume'
"""

import time
import sys
import json
import threading
import select

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


def print_matrix(occ, label=""):
    """يطبع مصفوفة 8x8 بشكل واضح"""
    print(f"\n{'═'*30} {label} {'═'*30}")
    print("     a  b  c  d  e  f  g  h")
    print("   ┌" + "───" * 8 + "┐")
    for r in range(8):
        rank_num = 8 - r
        row_str = f" {rank_num} │"
        for c in range(8):
            if occ[r][c] == 1:
                row_str += " ■ "
            else:
                row_str += " · "
        row_str += f"│ {rank_num}"
        print(row_str)
    print("   └" + "───" * 8 + "┘")
    print("     a  b  c  d  e  f  g  h")
    print(f"{'═'*66}")
    sys.stdout.flush()


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
    LOCKED_TIMEOUT_S = 10.0
    MISMATCH_GRACE_S = 3.0

    def __init__(self):
        self.chess_board = chess.Board()
        self.sensor = GPIOSensorBoard()
        self.mode = Mode.WAITING
        self.game_mode = None
        self.human_color = None
        self.game_active = False
        self.paused = False
        self.tracker_active = False
        self.last_board_state_time = 0.0
        self.locked_uci = None
        self.locked_time = 0.0
        self.pending_promotion_from_to = None
        self._anchor_occ = occupancy_from_chess(self.chess_board)
        self._shutdown = threading.Event()
        self._move_log = []
        self._button_pressed = threading.Event()  # محاكاة الـ Push Button

        if ROS_AVAILABLE:
            rospy.init_node('board_tracker', anonymous=False)
            # Publishers
            self.pub_move = rospy.Publisher('/chess/move', String, queue_size=10)
            self.pub_promo = rospy.Publisher('/chess/promotion_request', String, queue_size=10)

            # Subscribers
            rospy.Subscriber('/chess/game_start', String, self._on_game_start, queue_size=5)
            rospy.Subscriber('/chess/board_state', String, self._on_board_state, queue_size=5)
            rospy.Subscriber('/chess/turn_signal', String, self._on_turn_signal, queue_size=5)
            rospy.Subscriber('/chess/game_stop', String, self._on_game_stop, queue_size=5)
            rospy.Subscriber('/chess/pause', String, self._on_pause, queue_size=5)
        else:
            self.pub_move = None
            self.pub_promo = None

    # ─── Helpers ───────────────────────────────────────────────────
    def _pub(self, pub, msg):
        if pub:
            pub.publish(msg)

    def _print(self, msg):
        print(msg)
        sys.stdout.flush()

    # ─── ROS Callbacks ─────────────────────────────────────────────
    def _on_game_start(self, msg):
        self._print(f"\n{'*'*50}")
        self._print(f"📥 /chess/game_start: {msg.data[:100]}")
        self._print(f"{'*'*50}")
        try:
            p = json.loads(msg.data)
        except Exception:
            self._print("❌ JSON parse error!")
            return

        mode = p.get('mode', '')
        if 'Robot vs Robot' in mode:
            self.game_mode = 'RvR'
            self.human_color = None
            self.tracker_active = False
        elif 'Human vs Robot' in mode:
            self.game_mode = 'HvR'
            color = p.get('color', 'white').strip().lower()
            self.human_color = chess.WHITE if color == 'white' else chess.BLACK
            self.tracker_active = (color == 'white')
        else:
            self._print(f"⚠️  Unknown mode: '{mode}'")
            return

        self.chess_board = chess.Board()
        self._anchor_occ = occupancy_from_chess(self.chess_board)
        self._move_log = []
        self.locked_uci = None
        self.pending_promotion_from_to = None
        self.game_active = True
        self.paused = False
        self._update_mode()

        self._print(f"✅ Game started! mode={self.game_mode} tracker_active={self.tracker_active} → {self.mode}")
        print_matrix(self._anchor_occ, "ANCHOR (initial)")

    def _on_board_state(self, msg):
        self._print(f"📥 /chess/board_state received")
        try:
            p = json.loads(msg.data)
        except Exception:
            return

        # ── لو وصل board_state بس game_start ما وصل — فعّل اللعبة ──
        if not self.game_active:
            self._print(f"   ⚡ Auto-activating game from board_state")
            self.game_active = True
            if self.game_mode is None:
                self.game_mode = 'HvR'

        fen = p.get('fen')
        if fen:
            try:
                self.chess_board = chess.Board(fen)
                self._anchor_occ = occupancy_from_chess(self.chess_board)
                self.last_board_state_time = time.time()
                self.locked_uci = None
                self.pending_promotion_from_to = None
                self._print(f"   ✅ synced FEN: turn={'W' if self.chess_board.turn else 'B'}")
                print_matrix(self._anchor_occ, "ANCHOR (synced)")
            except Exception as e:
                self._print(f"   ❌ FEN error: {e}")
                return
        self._update_mode()
        self._print(f"   → mode={self.mode}")

    def _on_turn_signal(self, msg):
        self._print(f"📥 /chess/turn_signal: {msg.data[:60]}")
        try:
            p = json.loads(msg.data)
        except Exception:
            return

        self.tracker_active = p.get('active', False)

        # ── لو وصل turn_signal بس game_start ما وصل — فعّل اللعبة تلقائياً ──
        if not self.game_active and self.tracker_active:
            self._print(f"   ⚡ Auto-activating game (game_start missed)")
            self.game_active = True
            self.game_mode = 'HvR'
            mode_str = p.get('mode', 'Human vs Robot')
            if 'Robot' in mode_str and 'Robot' in mode_str.split('vs')[-1]:
                self.game_mode = 'RvR'
            h_color = p.get('human_color', 'white')
            if h_color and h_color != 'none':
                self.human_color = chess.WHITE if h_color == 'white' else chess.BLACK

        fen = p.get('fen')
        if fen:
            try:
                new_board = chess.Board(fen)
                if new_board.fen() != self.chess_board.fen():
                    self.chess_board = new_board
                    self._anchor_occ = occupancy_from_chess(self.chess_board)
                    self._print(f"   anchor updated from FEN")
                    print_matrix(self._anchor_occ, "ANCHOR (updated)")
            except Exception:
                pass

        if self.tracker_active:
            self.locked_uci = None
            self.pending_promotion_from_to = None

        self._update_mode()
        self._print(f"   → active={self.tracker_active} mode={self.mode}")

        # لو صار ACTIVE — نبّه المستخدم يحرّك ويكبس
        if self.mode == Mode.ACTIVE:
            self._print(f"\n{'─'*50}")
            self._print(f"👉 YOUR TURN! Move your piece then press [ENTER]")
            self._print(f"{'─'*50}")

    def _on_game_stop(self, msg):
        self._print(f"📥 /chess/game_stop: {msg.data}")
        if msg.data == "stop":
            self.game_active = False
            self.tracker_active = False
            self.paused = False
            self.mode = Mode.WAITING
            self._print("🛑 Game stopped.")

    def _on_pause(self, msg):
        self._print(f"📥 /chess/pause: {msg.data}")
        if msg.data == "pause":
            self.paused = True
            self._print("⏸  Paused.")
        elif msg.data == "resume":
            self.paused = False
            self._update_mode()
            self._print(f"▶  Resumed → mode={self.mode}")

    # ─── Mode Management ──────────────────────────────────────────
    def _update_mode(self):
        if not self.game_active:
            self.mode = Mode.WAITING
        elif self.paused:
            self.mode = Mode.WAITING
        elif self.game_mode == 'RvR':
            self.mode = Mode.WAITING
        elif self.locked_uci or self.pending_promotion_from_to:
            self.mode = Mode.LOCKED
        elif self.tracker_active:
            self.mode = Mode.ACTIVE
        else:
            self.mode = Mode.MONITOR

    # ─── Move Publishing ──────────────────────────────────────────
    def _publish_move(self, uci):
        self._print(f"\n🎯 PUBLISH /chess/move: {uci}")
        self._pub(self.pub_move, uci)
        self._move_log.append(uci)
        self.locked_uci = uci
        self.locked_time = time.time()
        try:
            self.chess_board.push(chess.Move.from_uci(uci))
            self._anchor_occ = occupancy_from_chess(self.chess_board)
        except Exception:
            pass
        self.tracker_active = False
        self._update_mode()
        self._print(f"   → LOCKED. Waiting for engine response...")

    def _publish_promo(self, from_to):
        payload = json.dumps({'move': from_to})
        self._print(f"\n♟  PUBLISH promotion_request: {payload}")
        self._pub(self.pub_promo, payload)
        self.pending_promotion_from_to = from_to
        self._update_mode()

    # ─── Button Listener (keyboard ENTER = push button) ──────────
    def _button_listener(self):
        """
        Thread يستمع لـ ENTER من الكيبورد.
        في الواقع رح يكون Push Button على GPIO — هون محاكاة.
        """
        while not self._shutdown.is_set():
            if ROS_AVAILABLE and rospy.is_shutdown():
                break
            # ننتظر ENTER (non-blocking مع timeout)
            if select.select([sys.stdin], [], [], 0.2)[0]:
                line = sys.stdin.readline().strip()
                # أي كبسة ENTER (فاضية أو مع نص) تعتبر "خلصت حركتي"
                self._button_pressed.set()

    # ─── Main Logic Loop ──────────────────────────────────────────
    def _main_loop(self):
        """
        الحلقة الرئيسية: تنتظر الـ button press ثم تقرأ وتحلل.
        """
        while not self._shutdown.is_set():
            if ROS_AVAILABLE and rospy.is_shutdown():
                break

            # لو مش ACTIVE — ننتظر بدون ما نعمل شي
            if self.mode != Mode.ACTIVE:
                time.sleep(0.1)
                continue

            # ── انتظر كبسة الزر ──
            self._button_pressed.clear()
            got_press = self._button_pressed.wait(timeout=0.3)

            if not got_press:
                continue  # ما حدا كبس — نرجع نفحص mode

            # ── كُبس الزر! نقرأ السينسورز ──
            if self.mode != Mode.ACTIVE:
                self._print("⚠️  Button pressed but not your turn anymore.")
                continue

            self._print(f"\n🔘 BUTTON PRESSED! Reading sensors...")

            # قراءة مستقرة (3 قراءات متتالية متطابقة)
            current_occ = self._stable_read()
            if current_occ is None:
                self._print("❌ Could not get stable reading. Try again.")
                continue

            # ── عرض المصفوفة الحالية ──
            print_matrix(current_occ, "CURRENT BOARD (after your move)")
            print_matrix(self._anchor_occ, "ANCHOR (before your move)")

            # ── الفرق ──
            disappeared, appeared = diff_occupancy(self._anchor_occ, current_occ)
            self._print(f"   disappeared = {disappeared}")
            self._print(f"   appeared    = {appeared}")

            if not disappeared and not appeared:
                self._print("⚠️  No change detected! Did you move a piece?")
                self._print("   Press [ENTER] again after moving.")
                continue

            # ── تصنيف الحركة ──
            result = MoveDetector.classify(self.chess_board, current_occ, self._anchor_occ)
            kind = result['kind']
            self._print(f"   classification: {kind}")

            if kind == 'complete':
                self._print(f"   ✅ Detected move: {result['uci']}")
                self._publish_move(result['uci'])

            elif kind == 'promotion':
                self._print(f"   ♟  Promotion detected: {result['from_to']}")
                self._publish_promo(result['from_to'])

            elif kind == 'pending':
                self._print(f"   ⏳ Incomplete move — you might still be moving.")
                self._print(f"      Finish your move and press [ENTER] again.")

            elif kind == 'anomaly':
                self._print(f"   ❌ INVALID MOVE! reason={result.get('reason')}")
                self._print(f"      disappeared={result.get('disappeared')}")
                self._print(f"      appeared={result.get('appeared')}")
                self._print(f"      Please fix the board and press [ENTER] again.")

            # Locked timeout check
            if self.mode == Mode.LOCKED and self.locked_uci:
                if time.time() - self.locked_time > self.LOCKED_TIMEOUT_S:
                    self._print(f"⏰ LOCKED timeout — unlocking.")
                    self.locked_uci = None
                    self._update_mode()

    def _stable_read(self, attempts=5, delay=0.05):
        """قراءة مستقرة: يقرأ عدة مرات ويأكد إنها متطابقة"""
        last = self.sensor.scan()
        stable_count = 0
        for _ in range(attempts * 3):
            time.sleep(delay)
            current = self.sensor.scan()
            if current == last:
                stable_count += 1
                if stable_count >= attempts - 1:
                    return current
            else:
                stable_count = 0
                last = current
        return last  # أرجع آخر قراءة حتى لو مش 100% مستقرة

    # ─── Initial Verification ─────────────────────────────────────
    def verify_initial(self):
        self._print("🔍 Checking initial board (30s timeout)...")
        expected = occupancy_from_chess(chess.Board())
        deadline = time.time() + 30.0

        while not self._shutdown.is_set():
            if ROS_AVAILABLE and rospy.is_shutdown():
                return
            current = self.sensor.scan()
            if current == expected:
                self._print("✅ All 32 pieces in place!")
                print_matrix(current, "INITIAL BOARD")
                return
            if time.time() > deadline:
                self._print("⚠️  TIMEOUT: Continuing without full board.")
                pieces = sum(sum(row) for row in current)
                self._print(f"   Detected {pieces}/32 pieces")
                print_matrix(current, f"CURRENT ({pieces} pieces)")
                return
            time.sleep(0.5)

    # ─── Main Entry Point ─────────────────────────────────────────
    def run(self):
        self._print("╔══════════════════════════════════════════════════════╗")
        self._print("║   Board Tracker v2 — BUTTON MODE (Press ENTER)      ║")
        self._print("╚══════════════════════════════════════════════════════╝")
        self._print(f"ROS={ROS_AVAILABLE} | GPIO={HAS_GPIO}")
        if ROS_AVAILABLE:
            self._print(f"ROS_MASTER_URI={rospy.get_param('/run_id', 'connected')}")
        self._print("")

        # ── شغّل threads أولاً حتى الـ callbacks تشتغل فوراً ──
        btn_thread = threading.Thread(target=self._button_listener, daemon=True)
        btn_thread.start()

        logic_thread = threading.Thread(target=self._main_loop, daemon=True)
        logic_thread.start()

        # ── verify_initial يشتغل بالخلفية (ما يحجز) ──
        verify_thread = threading.Thread(target=self.verify_initial, daemon=True)
        verify_thread.start()

        self._print(f"📡 Subscribed: game_start, board_state, turn_signal, game_stop, pause")
        self._print(f"📢 Publishing: /chess/move, /chess/promotion_request")
        self._print(f"")
        self._print(f"┌─────────────────────────────────────────────────┐")
        self._print(f"│  HOW TO USE:                                    │")
        self._print(f"│  1. Wait for 'YOUR TURN' message                │")
        self._print(f"│  2. Move your piece on the physical board       │")
        self._print(f"│  3. Press [ENTER] to confirm your move          │")
        self._print(f"│                                                 │")
        self._print(f"│  The system will read sensors and detect        │")
        self._print(f"│  your move automatically.                       │")
        self._print(f"└─────────────────────────────────────────────────┘")
        self._print(f"")
        self._print(f"⏳ Waiting for /chess/game_start...\n")

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
            btn_thread.join(timeout=2)
            logic_thread.join(timeout=2)
            verify_thread.join(timeout=2)
            self.sensor.cleanup()
            self._print("🔌 Shutdown complete.")


if __name__ == '__main__':
    node = BoardTrackerRPi()
    node.run()
