#!/usr/bin/env python3
"""
board_tracker_node.py
=====================
ROS node that tracks physical chess board state via 8x8 reed switch matrix,
integrates with display node and Stockfish node.

Topics:
   /chess/game_start    --> board_tracker_node --> /chess/move
   /chess/board_state   -->                   --> /chess/promotion_request
                                              --> /chess/status

Modes:
  WAITING  : Before game_start - completely inert, no classification.
  ACTIVE   : Human's turn - detect moves and publish.
  MONITOR  : Robot's turn or RvR - watch for mismatches only.
  LOCKED   : Move published, waiting for board_state confirmation.
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
#  Utilities
# ============================================================================
FILES = 'abcdefgh'
RANKS = '12345678'

PIECE_FROM_DISP = {
    'wK': 'K', 'wQ': 'Q', 'wR': 'R', 'wB': 'B', 'wN': 'N', 'wP': 'P',
    'bK': 'k', 'bQ': 'q', 'bR': 'r', 'bB': 'b', 'bN': 'n', 'bP': 'p',
}



def sq_to_rc(sq: str):
    """'e2' -> (row=6, col=4). row 0 = rank 8, col 0 = file a."""
    sq = sq.lower()
    return 8 - int(sq[1]), FILES.index(sq[0])


def rc_to_sq(row: int, col: int) -> str:
    return f"{FILES[col]}{8 - row}"


def occupancy_from_chess(board: chess.Board):
    """Extract 8x8 occupancy matrix from chess.Board."""
    occ = [[0] * 8 for _ in range(8)]
    for r in range(8):
        for c in range(8):
            sq = chess.square(c, 7 - r)
            if board.piece_at(sq) is not None:
                occ[r][c] = 1
    return occ


def diff_occupancy(prev, curr):
    """Returns (disappeared, appeared) as square names."""
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
    """Convert display 2D board representation to FEN."""
    rows = []
    for r in range(8):
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
    return f"{placement} {side} KQkq - 0 1"



# ============================================================================
#  Simulated Sensor Board (replace with GPIO class for production)
# ============================================================================
class SimulatedSensorBoard:
    """
    Drop-in replacement for GPIO reader. Interface: scan() returns 8x8 list.
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
#  Move Detector - stateless classifier
# ============================================================================
class MoveDetector:

    @staticmethod
    def squares_touched_by_move(board: chess.Board, move: chess.Move):
        """Returns set of square names physically affected by a move."""
        sqs = {chess.square_name(move.from_square),
               chess.square_name(move.to_square)}
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
    def classify(cls, chess_board: chess.Board, current_occ, anchor_occ):
        """
        Returns dict with 'kind' field:
          'clean'     - no change from anchor
          'complete'  - exactly one legal move matches
          'promotion' - pawn promotion detected
          'pending'   - intermediate state consistent with a legal move
          'anomaly'   - invalid state
        """
        disappeared, appeared = diff_occupancy(anchor_occ, current_occ)

        if not disappeared and not appeared:
            return {'kind': 'clean'}

        # Try 1: Does current occupancy match a completed legal move?
        legal = list(chess_board.legal_moves)
        complete_matches = []
        for m in legal:
            test = chess_board.copy(stack=False)
            test.push(m)
            if occupancy_from_chess(test) == current_occ:
                complete_matches.append(m)


        if complete_matches:
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
            return {
                'kind': 'anomaly',
                'disappeared': disappeared,
                'appeared': appeared,
                'reason': f'ambiguous_complete_matches={[m.uci() for m in complete_matches]}',
            }

        # Try 2: Intermediate state matching a legal move in progress?
        diff_set = set(disappeared) | set(appeared)
        partial_candidates = []
        for m in legal:
            touched = cls.squares_touched_by_move(chess_board, m)
            if diff_set.issubset(touched):
                if cls._consistent_with_move(chess_board, m, current_occ, anchor_occ):
                    partial_candidates.append(m)

        if partial_candidates:
            return {
                'kind': 'pending',
                'partial_for': partial_candidates,
                'disappeared': disappeared,
                'appeared': appeared,
            }

        return {
            'kind': 'anomaly',
            'disappeared': disappeared,
            'appeared': appeared,
            'reason': 'no_legal_move_matches',
        }

    @classmethod
    def _consistent_with_move(cls, board, move, current_occ, anchor_occ):
        """Check if current changes are consistent with partial execution of move."""
        test = board.copy(stack=False)
        test.push(move)
        final_occ = occupancy_from_chess(test)

        for r in range(8):
            for c in range(8):
                a = anchor_occ[r][c]
                f = final_occ[r][c]
                cur = current_occ[r][c]
                if cur == a:
                    continue
                if cur == f:
                    continue
                if cur == 0 and f == 1:
                    continue  # temporary lift
                return False
        return True



# ============================================================================
#  Modes
# ============================================================================
class Mode:
    WAITING = 'WAITING'
    ACTIVE = 'ACTIVE'
    MONITOR = 'MONITOR'
    LOCKED = 'LOCKED'


# ============================================================================
#  Dummy ROS publisher for non-ROS testing
# ============================================================================
class _DummyPub:
    def __init__(self, name):
        self.name = name

    def publish(self, msg):
        text = str(msg) if len(str(msg)) < 240 else str(msg)[:240] + '...'
        print(f"[ROS-sim PUB] {self.name}: {text}")


# ============================================================================
#  Main Node
# ============================================================================
class BoardTrackerNode:
    SCAN_RATE_HZ = 20
    STABILITY_CYCLES = 3
    LIFT_TIMEOUT_S = 30.0
    LOCKED_TIMEOUT_S = 5.0
    MISMATCH_GRACE_S = 3.0

    TOPIC_GAME_START = '/chess/game_start'
    TOPIC_BOARD_STATE = '/chess/board_state'
    TOPIC_MOVE = '/chess/move'
    TOPIC_PROMOTION_REQUEST = '/chess/promotion_request'
    TOPIC_STATUS = '/chess/status'
    TOPIC_OCCUPANCY = '/chess/board_occupancy'

    def __init__(self):
        self.chess_board = chess.Board()
        self.sensor = SimulatedSensorBoard(occupancy_from_chess(self.chess_board))

        self.mode = Mode.WAITING
        self.game_mode = None
        self.human_color = None
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
        self._pending_event = threading.Event()
        self._move_processed_event = threading.Event()

        self._move_log = []

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

        # Always start with standard initial position.
        # The physical board is ALWAYS oriented the same way (a1 bottom-left).
        # Whether human plays White or Black, the chess logic starts identically.
        # Pieces are swapped physically by the player, but sensor reads real positions.
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
        # In simulation: sync sensor with board_state to reflect robot arm moves.
        # On real hardware: sensor already reflects physical changes (no-op).
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
    # Mode determination
    # ========================================================================
    def _update_mode(self):
        if self.game_mode is None:
            self.mode = Mode.WAITING
            return
        if self.game_mode == 'RvR':
            self.mode = Mode.MONITOR
            return
        # HvR mode
        if self.locked_uci is not None or self.pending_promotion_from_to is not None:
            self.mode = Mode.LOCKED
            return
        # KEY FIX: Correctly determine whose turn it is.
        # chess_board.turn == chess.WHITE means it's White's turn.
        # If human_color == chess.BLACK, then when chess_board.turn == BLACK
        # it's the human's turn (ACTIVE), otherwise MONITOR.
        if self.chess_board.turn == self.human_color:
            self.mode = Mode.ACTIVE
        else:
            self.mode = Mode.MONITOR

    def _color_name(self):
        if self.human_color is None:
            return 'none'
        return 'white' if self.human_color == chess.WHITE else 'black'


    # ========================================================================
    # Publishing
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
        # Optimistic local push - board_state will confirm or override later.
        try:
            mv = chess.Move.from_uci(uci)
            self.chess_board.push(mv)
            self._anchor_occ = occupancy_from_chess(self.chess_board)
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
    # Scan loop
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

            # LOCKED timeout fallback
            if (self.mode == Mode.LOCKED and self.locked_uci is not None
                    and time.time() - self.locked_time > self.LOCKED_TIMEOUT_S):
                self._warn(f"LOCKED timeout for {self.locked_uci} — "
                           f"no /chess/board_state arrived. Falling back to local state.")
                self.locked_uci = None
                self._update_mode()

            self._pending_event.wait(timeout=period)
            self._pending_event.clear()

    def _dispatch_stable(self, current_occ):
        # FIX: WAITING mode is completely inert - no mismatch checks.
        # Player may be setting up pieces or testing sensors.
        if self.mode == Mode.WAITING:
            return
        if self.mode == Mode.LOCKED:
            return
        if self.mode == Mode.MONITOR:
            self._monitor_mismatch_check(current_occ)
            return
        if self.mode == Mode.ACTIVE:
            self._active_classify(current_occ)


    # ----------- ACTIVE: classify move and react -----------
    def _active_classify(self, current_occ):
        if self.pending_promotion_from_to is not None:
            return

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


    # ----------- MONITOR: warn on stable mismatch -----------
    def _monitor_mismatch_check(self, current_occ):
        expected = occupancy_from_chess(self.chess_board)
        if current_occ == expected:
            self.last_mismatch_repr = None
            return

        # Grace period after last sync - robot arm may still be moving.
        if (self.last_board_state_time > 0
                and time.time() - self.last_board_state_time < self.MISMATCH_GRACE_S):
            return

        disappeared, appeared = diff_occupancy(expected, current_occ)
        repr_ = (tuple(disappeared), tuple(appeared))
        if repr_ == self.last_mismatch_repr:
            return
        self.last_mismatch_repr = repr_
        msg = (f"BOARD MISMATCH ({self.mode}): physical board differs from "
               f"expected. missing={disappeared} extra={appeared}")
        self._warn(msg)
        self._publish_status(msg)


    # ========================================================================
    # Terminal interface - for testing
    # ========================================================================
    HELP_TEXT = """
Commands (testing/simulation):
  e2e4 | e2 e4 | e7e8q     Apply physical changes on simulator
  lift e2                  Lift piece (sensor 1->0)
  place e4                 Place piece (sensor 0->1)
  toggle e2                Toggle square
  show                     Show mental board
  occ                      Show raw 8x8 sensor matrix
  fen                      Show current FEN
  moves                    Show published moves
  state                    Show mode + game_mode + human_color
  start_white              Simulate game_start: HvR + human White
  start_black              Simulate game_start: HvR + human Black
  start_rvr                Simulate game_start: Robot vs Robot
  sync_fen <fen>           Simulate board_state with given FEN
  pubpromo q|r|b|n         Simulate display promotion response
  reset                    Reset to initial position
  help / quit
"""

    def terminal_loop(self):
        print(self.HELP_TEXT)
        if self.game_mode is None:
            print("(no game_start received yet — type 'start_white' or 'start_black' to begin)")
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
                  f"human={self._color_name()}  "
                  f"turn={'white' if self.chess_board.turn else 'black'}  "
                  f"locked={self.locked_uci}  "
                  f"promo_pending={self.pending_promotion_from_to}")
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

        # Try to parse as a move
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
        """Simulate a physical move on the sensor board."""
        try:
            piece = self.chess_board.piece_at(chess.parse_square(from_sq))
        except Exception:
            print(f"bad squares: {from_sq}, {to_sq}"); return
        if piece is None:
            print(f"no piece at {from_sq}"); return

        # In MONITOR/RvR: apply as complete move directly (simulates robot arm)
        if self.mode != Mode.ACTIVE:
            mode_before = self.mode
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


        # ACTIVE: apply physical changes in realistic order, let scan loop detect.
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

        self._pending_event.set()
        self._move_processed_event.wait(timeout=1.5)


    # ========================================================================
    # Run
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
#  Terminal utilities
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
    """Simulates std_msgs/String for internal calls."""
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
