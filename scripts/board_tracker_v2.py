#!/usr/bin/env python3
"""
board_tracker_v2.py — Physical Board Tracker (Raspberry Pi)
═══════════════════════════════════════════════════════════════════
يتتبع الحركات الفيزيائية على رقعة الشطرنج عبر مصفوفة Hall-effect sensors.

ROS Topics:
  Publishers:
    /chess/move               → UCI string (حركة كاملة)
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
    print(f"{'═'*66}\n")


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
    LOCKED_TIMEOUT_S = 10.0
    MISMATCH_GRACE_S = 3.0
    PRINT_INTERVAL_S = 2.0       # طباعة المصفوفة كل ثانيتين عند التغيير

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
        self.pending_since = None
        self.last_anomaly_repr = None
        self.last_mismatch_repr = None
        self.pending_promotion_from_to = None
        self._anchor_occ = occupancy_from_chess(self.chess_board)
        self._last_printed_occ = None
        self._last_print_time = 0.0
        self._shutdown = threading.Event()
        self._move_log = []

        if ROS_AVAILABLE:
            rospy.init_node('board_tracker', anonymous=False)
            # Publishers
            self.pub_move = rospy.Publisher('/chess/move', String, queue_size=10)
            self.pub_promo = rospy.Publisher('/chess/promotion_request', String, queue_size=10)

            # Subscribers — queue_size=1 لضمان أحدث رسالة
            rospy.Subscriber('/chess/game_start', String, self._on_game_start, queue_size=5)
            rospy.Subscriber('/chess/board_state', String, self._on_board_state, queue_size=5)
            rospy.Subscriber('/chess/turn_signal', String, self._on_turn_signal, queue_size=5)
            rospy.Subscriber('/chess/game_stop', String, self._on_game_stop, queue_size=5)
            rospy.Subscriber('/chess/pause', String, self._on_pause, queue_size=5)

            self._log = rospy.loginfo
            self._warn = rospy.logwarn
        else:
            self.pub_move = None
            self.pub_promo = None
            self._log = lambda m: print("[INFO] " + m)
            self._warn = lambda m: print("[WARN] " + m)

    # ─── Helpers ───────────────────────────────────────────────────
    def _pub(self, pub, msg):
        if pub:
            pub.publish(msg)
        else:
            print(f"[PUB] {str(msg)[:200]}")

    def _print(self, msg):
        """طباعة مباشرة + rospy.loginfo"""
        print(msg)
        sys.stdout.flush()
        if ROS_AVAILABLE and not rospy.is_shutdown():
            rospy.loginfo(msg)

    # ─── ROS Callbacks ─────────────────────────────────────────────
    def _on_game_start(self, msg):
        self._print(f"\n{'*'*50}")
        self._print(f"📥 [RECEIVED] /chess/game_start: {msg.data[:100]}")
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

        self._print(f"✅ Game started! mode={self.game_mode} tracker_active={self.tracker_active} → mode={self.mode}")
        print_matrix(self._anchor_occ, "INITIAL ANCHOR")

    def _on_board_state(self, msg):
        self._print(f"📥 [RECEIVED] /chess/board_state (len={len(msg.data)})")
        try:
            p = json.loads(msg.data)
        except Exception:
            self._print("❌ JSON parse error on board_state!")
            return

        fen = p.get('fen')
        if fen:
            try:
                self.chess_board = chess.Board(fen)
                self._anchor_occ = occupancy_from_chess(self.chess_board)
                self.last_board_state_time = time.time()
                self.locked_uci = None
                self.pending_promotion_from_to = None
                self.pending_since = None
                self._print(f"✅ board_state synced: {fen.split()[0][:30]}... turn={'W' if self.chess_board.turn else 'B'}")
                print_matrix(self._anchor_occ, "SYNCED ANCHOR")
            except Exception as e:
                self._print(f"❌ FEN parse error: {e}")
                return
        else:
            self._print("⚠️  board_state without FEN — no sync!")

        self._update_mode()
        self._print(f"   → mode={self.mode}")

    def _on_turn_signal(self, msg):
        self._print(f"📥 [RECEIVED] /chess/turn_signal: {msg.data[:80]}")
        try:
            p = json.loads(msg.data)
        except Exception:
            self._print("❌ JSON parse error on turn_signal!")
            return

        self.tracker_active = p.get('active', False)

        fen = p.get('fen')
        if fen:
            try:
                new_board = chess.Board(fen)
                if new_board.fen() != self.chess_board.fen():
                    self.chess_board = new_board
                    self._anchor_occ = occupancy_from_chess(self.chess_board)
                    self._print(f"   anchor updated from turn_signal FEN")
                    print_matrix(self._anchor_occ, "UPDATED ANCHOR")
            except Exception:
                pass

        if self.tracker_active:
            self.locked_uci = None
            self.pending_promotion_from_to = None
            self.pending_since = None

        self._update_mode()
        self._print(f"   → tracker_active={self.tracker_active} mode={self.mode}")

    def _on_game_stop(self, msg):
        self._print(f"📥 [RECEIVED] /chess/game_stop: {msg.data}")
        if msg.data == "stop":
            self.game_active = False
            self.tracker_active = False
            self.paused = False
            self.mode = Mode.WAITING
            self._print("🛑 Game stopped — tracker WAITING.")

    def _on_pause(self, msg):
        self._print(f"📥 [RECEIVED] /chess/pause: {msg.data}")
        if msg.data == "pause":
            self.paused = True
            self._print("⏸  Game paused.")
        elif msg.data == "resume":
            self.paused = False
            self._update_mode()
            self._print(f"▶  Game resumed → mode={self.mode}")

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
        self._print(f"   → LOCKED. Waiting for board_state/turn_signal...")

    def _publish_promo(self, from_to):
        payload = json.dumps({'move': from_to})
        self._print(f"\n♟  PUBLISH /chess/promotion_request: {payload}")
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
                # ── طباعة المصفوفة عند التغيير ──
                now = time.time()
                if current != self._last_printed_occ and (now - self._last_print_time) > self.PRINT_INTERVAL_S:
                    self._last_printed_occ = [row[:] for row in current]
                    self._last_print_time = now
                    print_matrix(current, f"SENSOR SCAN [mode={self.mode}]")
                    # أظهر الفرق مع الـ anchor
                    if self.mode == Mode.ACTIVE:
                        disappeared, appeared = diff_occupancy(self._anchor_occ, current)
                        if disappeared or appeared:
                            print(f"   ↑ disappeared={disappeared}  appeared={appeared}")
                            sys.stdout.flush()

                self._dispatch(current)

            # Locked timeout
            if self.mode == Mode.LOCKED and self.locked_uci:
                if time.time() - self.locked_time > self.LOCKED_TIMEOUT_S:
                    self._print(f"⏰ LOCKED timeout for {self.locked_uci} — unlocking.")
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
                self._print(f"⏳ PENDING: {result.get('disappeared', [])}")
            return
        if kind == 'anomaly':
            r = (tuple(result.get('disappeared', [])), tuple(result.get('appeared', [])))
            if r != self.last_anomaly_repr:
                self.last_anomaly_repr = r
                self._print(f"⚠️  ANOMALY: disappeared={result.get('disappeared')} appeared={result.get('appeared')} reason={result.get('reason')}")

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
            self._print(f"⚠️  MISMATCH: missing={d} extra={a}")

    # ─── Initial Verification ─────────────────────────────────────
    def verify_initial(self):
        """
        تحقق من القطع — مع timeout 30 ثانية.
        لو ما اكتملت → يطبع تحذير ويكمّل (ما يحجز النود).
        """
        self._print("🔍 Checking initial board state (30s timeout)...")
        expected = occupancy_from_chess(chess.Board())
        deadline = time.time() + 30.0

        while not self._shutdown.is_set():
            if ROS_AVAILABLE and rospy.is_shutdown():
                return False
            current = self.sensor.scan()
            if current == expected:
                self._print("✅ All 32 pieces in place!")
                print_matrix(current, "INITIAL BOARD")
                return True
            if time.time() > deadline:
                # ── Timeout — اطبع الحالة وكمّل ──
                self._print("⚠️  TIMEOUT: Board not fully set up. Continuing anyway...")
                print_matrix(current, "CURRENT (incomplete)")
                print_matrix(expected, "EXPECTED (32 pieces)")
                return True  # نكمّل بدل ما نحجز
            time.sleep(0.5)
        return False

    # ─── Main Entry Point ─────────────────────────────────────────
    def run(self):
        self._print("╔══════════════════════════════════════════════╗")
        self._print("║   Board Tracker RPi v2 — Starting...        ║")
        self._print("╚══════════════════════════════════════════════╝")
        self._print(f"ROS_AVAILABLE={ROS_AVAILABLE}")
        self._print(f"HAS_GPIO={HAS_GPIO}")

        if not self.verify_initial():
            return

        self._print(f"\n🚀 Scan loop running.")
        self._print(f"📡 Subscribed to: /chess/game_start, /chess/board_state,")
        self._print(f"   /chess/turn_signal, /chess/game_stop, /chess/pause")
        self._print(f"📢 Publishing to: /chess/move, /chess/promotion_request")
        self._print(f"⏳ Waiting for /chess/game_start...\n")

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
            self._print("🔌 Shutdown complete.")


if __name__ == '__main__':
    node = BoardTrackerRPi()
    node.run()
