#!/usr/bin/env python3

import rospy
import chess
import chess.engine
import json
import threading
from std_msgs.msg import String


class ChessNode:
    def __init__(self):
        rospy.init_node('chess_node_v13', anonymous=False)

        STOCKFISH_PATH = "/usr/games/stockfish"
        self.engine = chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH)
        rospy.loginfo("Stockfish engine started.")

        self.board            = chess.Board()
        self.game_active      = False
        self.paused           = False
        self.human_color      = chess.WHITE
        self.difficulty       = 10
        self.think_time       = 1.0
        self.rvr_mode         = False
        self.rvr_white_skill  = 10
        self.rvr_black_skill  = 10
        self.rvr_robot2_color = 'black'

        self.pub_board       = rospy.Publisher('/chess/board_state',  String, queue_size=10)
        self.pub_status      = rospy.Publisher('/chess/status',       String, queue_size=10)
        self.pub_clock       = rospy.Publisher('/chess/clock',        String, queue_size=10)
        self.pub_turn_signal = rospy.Publisher('/chess/turn_signal',  String, queue_size=10)

        rospy.Subscriber('/chess/game_start', String, self.cb_game_start)
        rospy.Subscriber('/chess/move',       String, self.cb_human_move)
        rospy.Subscriber('/chess/game_stop',  String, self.cb_game_stop)
        rospy.Subscriber('/chess/pause',      String, self.cb_pause)
        rospy.Subscriber('/chess/promotion_request', String, self.cb_promotion_request)

        rospy.loginfo("chess_node ready. Waiting for game_start...")

    # ══════════════════════════════════════════════════════════════
    #  Callbacks
    # ══════════════════════════════════════════════════════════════
    def cb_game_start(self, msg):
        try:
            settings = json.loads(msg.data)
        except json.JSONDecodeError:
            rospy.logerr("game_start: invalid JSON")
            return

        mode = settings.get('mode', 'Human vs Robot')
        difficulty_map = {'Easy': 0, 'Medium': 10, 'Hard': 18}

        self.board       = chess.Board()
        self.game_active = False
        self.paused      = False

        if mode == 'Robot vs Robot':
            self.rvr_mode = True
            r1 = settings.get('robot1', {})
            r2 = settings.get('robot2', {})
            r1_color = r1.get('color', 'White').strip().lower()
            r2_color = r2.get('color', 'Black').strip().lower()
            self.rvr_robot2_color = r2_color
            r1_skill = difficulty_map.get(r1.get('difficulty', 'Medium'), 10)
            r2_skill = difficulty_map.get(r2.get('difficulty', 'Medium'), 10)

            if r1_color == 'white':
                self.rvr_white_skill = r1_skill
                self.rvr_black_skill = r2_skill
            else:
                self.rvr_white_skill = r2_skill
                self.rvr_black_skill = r1_skill

            self.human_color = None
            self.game_active = True
            rospy.loginfo(f"RvR started | White skill={self.rvr_white_skill} | Black skill={self.rvr_black_skill}")
            self._publish_board()
            self._publish_status("Robot vs Robot — White starts")
            t = threading.Thread(target=self._rvr_loop, daemon=True)
            t.start()

        else:
            self.rvr_mode    = False
            difficulty       = settings.get('difficulty', 'Medium')
            color            = settings.get('color', 'White')
            self.difficulty  = difficulty_map.get(difficulty, 10)
            self.engine.configure({"Skill Level": self.difficulty})
            self.human_color = chess.WHITE if color.strip().lower() == 'white' else chess.BLACK
            self.game_active = True
            rospy.loginfo(f"HvR started | Difficulty: {difficulty} | Human: {'White' if self.human_color == chess.WHITE else 'Black'}")
            self._publish_board()
            if self.human_color == chess.BLACK:
                self._publish_status("Robot thinking...  (White starts)")
                self._do_robot_move()
            else:
                self._publish_status("Your turn  (You are White)")

    def cb_human_move(self, msg):
        """
        يستقبل حركة من board_tracker (الراسبيري) عبر /chess/move
        أو من display_node (للترقية)
        """
        if not self.game_active:
            rospy.logwarn("No active game.")
            return
        if self.rvr_mode:
            rospy.logwarn("Robot vs Robot mode — human moves ignored.")
            return
        if self.paused:
            self._publish_status("⏸  Game is paused. Press RESUME first.")
            return

        uci_str = msg.data.strip().lower()
        rospy.loginfo(f"[RECEIVED] /chess/move: {uci_str}")

        # ── ترقية بيدق: 4 حروف بدون حرف قطعة ──
        if len(uci_str) == 4:
            try:
                test_move_q = chess.Move.from_uci(uci_str + 'q')
                dest_rank   = int(uci_str[3])
                is_promo    = (
                    test_move_q in self.board.legal_moves and
                    self.board.piece_at(chess.square(ord(uci_str[0]) - ord('a'),
                                                     int(uci_str[1]) - 1)) is not None and
                    self.board.piece_at(chess.square(ord(uci_str[0]) - ord('a'),
                                                     int(uci_str[1]) - 1)).piece_type == chess.PAWN and
                    ((self.human_color == chess.WHITE and dest_rank == 8) or
                     (self.human_color == chess.BLACK and dest_rank == 1))
                )
                if is_promo:
                    self._publish_status(f"__PROMOTION__:{uci_str}")
                    return
            except Exception:
                pass

        # ── تحقق من الدور ──
        if self.board.turn != self.human_color:
            current = "White" if self.board.turn == chess.WHITE else "Black"
            rospy.logwarn(f"Not human's turn! It's {current}'s turn.")
            self._publish_status(f"Not your turn! Waiting for {current}...")
            return

        # ── تحقق من صيغة الحركة ──
        try:
            move = chess.Move.from_uci(uci_str)
        except ValueError:
            self._publish_status(f"Invalid move format: {uci_str}")
            return

        # ── تحقق من شرعية الحركة ──
        if move not in self.board.legal_moves:
            self._publish_status(f"Illegal move: {uci_str}. Try again.")
            return

        # ── نفّذ الحركة ──
        self.board.push(move)
        rospy.loginfo(f"Human played: {uci_str}")
        self._publish_board(last_move=uci_str)

        if self._check_game_over():
            return

        # ── دور الروبوت ──
        self._publish_status("Robot thinking...")
        rospy.sleep(0.2)
        self._do_robot_move()

    def cb_promotion_request(self, msg):
        """
        يستقبل طلب ترقية من board_tracker.
        {"move": "e7e8"} → يبعث __PROMOTION__ للـ display
        """
        try:
            data = json.loads(msg.data)
            from_to = data.get('move', '')
            if len(from_to) == 4:
                rospy.loginfo(f"[PROMO REQUEST] from tracker: {from_to}")
                self._publish_status(f"__PROMOTION__:{from_to}")
        except Exception as e:
            rospy.logerr(f"promotion_request error: {e}")

    def cb_game_stop(self, msg):
        self.game_active = False
        self.paused      = False
        self._publish_status("Game stopped.")
        rospy.loginfo("Game stopped.")

    def cb_pause(self, msg):
        if msg.data == "pause":
            self.paused = True
            rospy.loginfo("Game paused.")
        elif msg.data == "resume":
            self.paused = False
            rospy.loginfo("Game resumed.")

    # ══════════════════════════════════════════════════════════════
    #  Robot vs Robot Loop
    # ══════════════════════════════════════════════════════════════
    def _rvr_loop(self):
        import time
        while self.game_active:
            while self.paused and self.game_active:
                time.sleep(0.2)
            if not self.game_active:
                break

            skill = self.rvr_white_skill if self.board.turn == chess.WHITE else self.rvr_black_skill
            self.engine.configure({"Skill Level": skill})

            try:
                result = self.engine.play(self.board, chess.engine.Limit(time=self.think_time))
            except Exception as e:
                rospy.logerr(f"Stockfish error in RvR: {e}")
                break

            move    = result.move
            uci_str = move.uci()

            if len(uci_str) == 4:
                piece = self.board.piece_at(chess.square(ord(uci_str[0]) - ord('a'), int(uci_str[1]) - 1))
                dest_rank = int(uci_str[3])
                if piece and piece.piece_type == chess.PAWN and dest_rank in (1, 8):
                    uci_str += 'q'
                    move = chess.Move.from_uci(uci_str)

            self.board.push(move)
            rospy.loginfo(f"RvR {'White' if self.board.turn == chess.BLACK else 'Black'} played: {uci_str}")
            self._publish_board(last_move=uci_str)

            if self._check_game_over():
                break

            color_next = "White" if self.board.turn == chess.WHITE else "Black"
            self._publish_status(f"Robot thinking...  ({color_next} to move)")
            time.sleep(0.3)

    # ══════════════════════════════════════════════════════════════
    #  Robot Move (HvR)
    # ══════════════════════════════════════════════════════════════
    def _do_robot_move(self):
        if not self.game_active:
            return
        import time
        while self.paused and self.game_active:
            time.sleep(0.2)
        if not self.game_active:
            return
        try:
            result = self.engine.play(self.board, chess.engine.Limit(time=self.think_time))
        except Exception as e:
            rospy.logerr(f"Stockfish error: {e}")
            return

        move = result.move
        self.board.push(move)
        uci_str = move.uci()
        rospy.loginfo(f"Robot played: {uci_str}")
        self._publish_board(last_move=uci_str)

        if self._check_game_over():
            return

        self._publish_status("Your turn")

    # ══════════════════════════════════════════════════════════════
    #  Game Over Check
    # ══════════════════════════════════════════════════════════════
    def _check_game_over(self):
        if self.board.is_checkmate():
            if self.rvr_mode:
                loser_color  = "White" if self.board.turn == chess.WHITE else "Black"
                winner_color = "Black" if self.board.turn == chess.WHITE else "White"
                r2_color = self.rvr_robot2_color
                winner_name = "Robot 2" if winner_color.lower() == r2_color else "Robot 1"
                self._publish_status(f"Checkmate! {winner_name} ({winner_color}) wins!")
            else:
                winner = "Robot" if self.board.turn == self.human_color else "You"
                self._publish_status(f"Checkmate! {winner} win!")
            self._publish_status_game_over()
            self.game_active = False
            return True
        if self.board.is_stalemate():
            self._publish_status("Stalemate! Draw.")
            self._publish_status_game_over()
            self.game_active = False
            return True
        if self.board.is_insufficient_material():
            self._publish_status("Draw — insufficient material.")
            self._publish_status_game_over()
            self.game_active = False
            return True
        if self.board.is_check():
            if self.rvr_mode:
                color = "White" if self.board.turn == chess.WHITE else "Black"
                self._publish_status(f"Check! ({color} to move)")
            else:
                self._publish_status("Check! Your turn.")
        return False

    def _publish_status_game_over(self):
        import time
        time.sleep(0.1)
        self.pub_status.publish("__GAME_OVER__")

    # ══════════════════════════════════════════════════════════════
    #  Board Publishing (مع FEN + turn_signal)
    # ══════════════════════════════════════════════════════════════
    def _publish_board(self, last_move=None):
        board_2d = []
        for rank in range(7, -1, -1):
            row = []
            for file in range(8):
                sq    = chess.square(file, rank)
                piece = self.board.piece_at(sq)
                if piece is None:
                    row.append(None)
                else:
                    c = 'w' if piece.color == chess.WHITE else 'b'
                    row.append(f"{c}{piece.symbol().upper()}")
            board_2d.append(row)

        king_in_check_sq = None
        if self.board.is_check():
            king_sq = self.board.king(self.board.turn)
            if king_sq is not None:
                file_idx = chess.square_file(king_sq)
                rank_idx = chess.square_rank(king_sq)
                king_in_check_sq = [7 - rank_idx, file_idx]

        h_color = 'none' if self.human_color is None else ('white' if self.human_color == chess.WHITE else 'black')
        turn_str = 'white' if self.board.turn == chess.WHITE else 'black'

        payload = {
            'board':           board_2d,
            'fen':             self.board.fen(),
            'last_move':       last_move,
            'turn':            turn_str,
            'human_color':     h_color,
            'king_in_check':   king_in_check_sq,
            'robot2_color':    self.rvr_robot2_color if self.rvr_mode else 'none',
        }
        self.pub_board.publish(json.dumps(payload))

        # ── turn_signal for board_tracker ──
        is_human_turn = False
        if not self.rvr_mode and self.human_color is not None:
            is_human_turn = (self.board.turn == self.human_color)

        turn_signal = {
            'active':      is_human_turn,
            'turn':        turn_str,
            'human_color': h_color,
            'mode':        'Robot vs Robot' if self.rvr_mode else 'Human vs Robot',
            'fen':         self.board.fen(),
        }
        self.pub_turn_signal.publish(json.dumps(turn_signal))

    def _publish_status(self, text):
        self.pub_status.publish(text)
        rospy.loginfo(f"Status: {text}")

    def shutdown(self):
        rospy.loginfo("Shutting down chess_node...")
        self.engine.quit()


if __name__ == '__main__':
    node = ChessNode()
    rospy.on_shutdown(node.shutdown)
    rospy.spin()
