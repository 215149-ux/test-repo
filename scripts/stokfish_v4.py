#!/usr/bin/env python3
"""
stokfish_v4.py — Chess Engine Node (PC)
═══════════════════════════════════════════════════════════════════
النود المسؤول عن محرك Stockfish والتحكم بالمنطق الأساسي للعبة.

ROS Topics:
  Publishers:
    /chess/board_state    → JSON {board, fen, last_move, turn, human_color, king_in_check, robot2_color}
    /chess/status         → text / __PROMOTION__:uci / __GAME_OVER__
    /chess/turn_signal    → JSON {active: true/false, turn: 'white'/'black', human_color: ...}
    robot_move_cmd        → UCI move string for Robot 1
    robot2_move_cmd       → UCI move string for Robot 2
    /panda1/color         → latch: robot1 color
    /panda2/color         → latch: robot2 color

  Subscribers:
    /chess/game_start      ← JSON settings from display_node
    /chess/move            ← UCI string from display_node OR board_tracker
    /chess/game_stop       ← 'stop' from display_node
    /chess/pause           ← 'pause'/'resume' from display_node
    /chess/promotion_request ← JSON {move: 'e7e8'} from board_tracker
    robot_status           ← 'panda1_ready'/'panda2_ready' from robot controllers
"""

import subprocess
import chess
import sys
import rospy
import json
import threading
import select
import time
from std_msgs.msg import String

# ══════════════════════════════════════════════════════════════════
#  Global State
# ══════════════════════════════════════════════════════════════════
robot_ready_flag = False
settings_received = threading.Event()
game_settings = {}
gui_move_ready = threading.Event()
final_gui_move = ""

game_paused = False
reset_game = False


# ══════════════════════════════════════════════════════════════════
#  ROS Callbacks
# ══════════════════════════════════════════════════════════════════
def status_callback(data):
    global robot_ready_flag
    if data.data in ["panda1_ready", "panda2_ready"]:
        robot_ready_flag = True


def wait_for_robot():
    global robot_ready_flag
    print("⏳ Waiting for robot feedback...")
    while not robot_ready_flag and not rospy.is_shutdown():
        if reset_game:
            return
        rospy.sleep(0.1)
    print("✅ Robot finished moving.")
    robot_ready_flag = False


def cb_game_start(msg):
    global game_settings, reset_game
    try:
        game_settings = json.loads(msg.data)
        reset_game = False
        settings_received.set()
    except Exception:
        pass


def cb_gui_move(msg):
    """يستقبل حركات من display_node أو board_tracker عبر /chess/move"""
    global final_gui_move
    final_gui_move = msg.data.strip()
    gui_move_ready.set()


def cb_game_stop(msg):
    global reset_game, game_paused
    if msg.data == "stop":
        reset_game = True
        game_paused = False
        print("🛑 Stop command received. Resetting...")


def cb_game_pause(msg):
    global game_paused
    if msg.data == "pause":
        game_paused = True
    elif msg.data == "resume":
        game_paused = False


def cb_promotion_request(msg):
    """
    يستقبل طلب ترقية من board_tracker.
    board_tracker بيبعث: {"move": "e7e8"}
    نحن نبعث __PROMOTION__:e7e8 للـ display ليعرض نافذة الاختيار.
    """
    try:
        data = json.loads(msg.data)
        from_to = data.get('move', '')
        if len(from_to) == 4:
            rospy.loginfo(f"[PROMO REQUEST] from board_tracker: {from_to}")
            pub_gui_status.publish(f"__PROMOTION__:{from_to}")
    except Exception as e:
        rospy.logerr(f"promotion_request parse error: {e}")


# ══════════════════════════════════════════════════════════════════
#  Board Publishing (مع FEN + turn_signal)
# ══════════════════════════════════════════════════════════════════
def publish_to_gui(fen, last_move=None):
    """ينشر حالة اللوحة + FEN + turn_signal"""
    board = chess.Board(fen)
    board_2d = []
    for rank in range(7, -1, -1):
        row = []
        for file in range(8):
            piece = board.piece_at(chess.square(file, rank))
            if piece:
                c_prefix = 'w' if piece.color == chess.WHITE else 'b'
                row.append(f"{c_prefix}{piece.symbol().upper()}")
            else:
                row.append(None)
        board_2d.append(row)

    # King in check
    king_in_check_sq = None
    if board.is_check():
        king_sq = board.king(board.turn)
        if king_sq is not None:
            file_idx = chess.square_file(king_sq)
            rank_idx = chess.square_rank(king_sq)
            king_in_check_sq = [7 - rank_idx, file_idx]

    # Human color
    mode = game_settings.get('mode', 'Human vs Robot')
    if mode == "Robot vs Robot":
        h_color = 'none'
    else:
        h_color = game_settings.get('color', 'White').lower()

    # Robot2 color (for flip in RvR)
    r2_color = 'none'
    if mode == "Robot vs Robot":
        r2_color = game_settings.get('robot2', {}).get('color', 'Black').lower()

    payload = {
        'board': board_2d,
        'fen': fen,                      # ← الإضافة الرئيسية لـ board_tracker
        'last_move': last_move,
        'turn': 'white' if board.turn == chess.WHITE else 'black',
        'human_color': h_color,
        'king_in_check': king_in_check_sq,
        'robot2_color': r2_color,
    }
    pub_gui_board.publish(json.dumps(payload))

    # ── Publish turn_signal for board_tracker ──
    turn_str = 'white' if board.turn == chess.WHITE else 'black'
    is_human_turn = False
    if mode == "Human vs Robot":
        user_color = game_settings.get('color', 'White').lower()
        is_human_turn = (turn_str == user_color)

    turn_signal = {
        'active': is_human_turn,       # True = board_tracker should track
        'turn': turn_str,
        'human_color': h_color,
        'mode': mode,
        'fen': fen,
    }
    pub_turn_signal.publish(json.dumps(turn_signal))


# ══════════════════════════════════════════════════════════════════
#  Utility Functions
# ══════════════════════════════════════════════════════════════════
def fen_to_board(fen):
    rows = fen.split()[0].split('/')
    board = []
    for r in rows:
        row = []
        for c in r:
            if c.isdigit():
                row.extend(['.'] * int(c))
            else:
                row.append(c)
        board.append(row)
    return board


def print_board(board):
    print("\n    a b c d e f g h")
    print("  +" + "--" * 8 + "+")
    for i, row in enumerate(board):
        print(f"{8-i} |" + ' '.join(row) + f"| {8-i}")
    print("  +" + "--" * 8 + "+")
    print("    a b c d e f g h\n")


def get_board():
    proc.stdin.write("d\n")
    proc.stdin.flush()
    while True:
        line = proc.stdout.readline()
        if not line:
            continue
        line = line.strip()
        if line.startswith("Fen:"):
            fen = line.split("Fen:")[1].strip()
            board_disp = fen_to_board(fen)
            print_board(board_disp)
            return fen


def get_best_move():
    proc.stdin.write("go depth 10\n")
    proc.stdin.flush()
    while True:
        line = proc.stdout.readline()
        if not line:
            continue
        line = line.strip()
        if line.startswith("bestmove"):
            parts = line.split()
            return parts[1] if len(parts) >= 2 else None


def uci_format_ok(move):
    if len(move) not in (4, 5):
        return False
    if move[0] not in 'abcdefgh' or move[2] not in 'abcdefgh':
        return False
    if move[1] not in '12345678' or move[3] not in '12345678':
        return False
    return True


def check_game_over(fen, mode):
    board = chess.Board(fen)
    if board.is_checkmate():
        winner_color = "White" if board.turn == chess.BLACK else "Black"
        if mode == '1':
            user_c = game_settings.get('color', 'White')
            winner_name = "You win!" if winner_color.lower() == user_c.lower() else f"Robot 1 ({winner_color})"
        else:
            r1_c = game_settings.get('robot1', {}).get('color', 'White')
            winner_name = (
                f"Robot 1 ({winner_color} wins)"
                if winner_color.lower() == r1_c.lower()
                else f"Robot 2 ({winner_color} wins)"
            )
        status_text = f"Checkmate! {winner_name}"
        print(f"\n🏆 {status_text} 🏆")
        pub_gui_status.publish(status_text)
        pub_gui_status.publish("__GAME_OVER__")
        return True

    if board.is_stalemate() or board.is_insufficient_material():
        status_text = "Draw! Game Over."
        print(f"\n🤝 {status_text}")
        pub_gui_status.publish(status_text)
        pub_gui_status.publish("__GAME_OVER__")
        return True
    return False


def set_engine_strength(skill, elo):
    proc.stdin.write("setoption name UCI_LimitStrength value true\n")
    proc.stdin.write(f"setoption name Skill Level value {skill}\n")
    proc.stdin.write(f"setoption name UCI_Elo value {elo}\n")
    proc.stdin.flush()


# ══════════════════════════════════════════════════════════════════
#  ROS Node Initialization
# ══════════════════════════════════════════════════════════════════
rospy.init_node('chess_engine_node', anonymous=True)

# Publishers
move_pub1 = rospy.Publisher('robot_move_cmd', String, queue_size=10)
move_pub2 = rospy.Publisher('robot2_move_cmd', String, queue_size=10)
color_pub1 = rospy.Publisher('/panda1/color', String, queue_size=10, latch=True)
color_pub2 = rospy.Publisher('/panda2/color', String, queue_size=10, latch=True)
pub_gui_board = rospy.Publisher('/chess/board_state', String, queue_size=10)
pub_gui_status = rospy.Publisher('/chess/status', String, queue_size=10)
pub_turn_signal = rospy.Publisher('/chess/turn_signal', String, queue_size=10)

# Subscribers
rospy.Subscriber('robot_status', String, status_callback)
rospy.Subscriber('/chess/game_start', String, cb_game_start)
rospy.Subscriber('/chess/move', String, cb_gui_move)
rospy.Subscriber('/chess/game_stop', String, cb_game_stop)
rospy.Subscriber('/chess/pause', String, cb_game_pause)
rospy.Subscriber('/chess/promotion_request', String, cb_promotion_request)

# Start Stockfish
try:
    proc = subprocess.Popen(
        ['stockfish'],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True
    )
except FileNotFoundError:
    print("Stockfish not found.")
    sys.exit(1)

proc.stdin.write("uci\n")
proc.stdin.flush()
while "uciok" not in proc.stdout.readline():
    pass


# ══════════════════════════════════════════════════════════════════
#  Main Game Loop
# ══════════════════════════════════════════════════════════════════
while not rospy.is_shutdown():
    print("📡 Waiting for GUI to press START...")
    settings_received.wait()

    # ── Distribute colors ──
    mode = game_settings.get("mode", "Human vs Robot")
    if mode == "Robot vs Robot":
        r1_color = game_settings.get('robot1', {}).get('color', 'White').lower()
        r2_color = game_settings.get('robot2', {}).get('color', 'Black').lower()
        color_pub1.publish(r1_color)
        color_pub2.publish(r2_color)
        print(f"📢 [SETUP] Colors Sent -> Panda1: {r1_color}, Panda2: {r2_color}")
    else:
        h_color = game_settings.get('color', 'White').lower()
        r1_color = "black" if h_color == "white" else "white"
        color_pub1.publish(r1_color)
        print(f"📢 [SETUP] Color Sent -> Panda1: {r1_color}")

    settings_received.clear()

    reset_game = False
    moves_list = []
    proc.stdin.write("position startpos\n")
    proc.stdin.flush()
    current_fen = get_board()
    publish_to_gui(current_fen)
    pub_gui_status.publish("White's turn")

    game_mode = '2' if mode == "Robot vs Robot" else '1'
    user_color = game_settings.get('color', 'White').lower()
    difficulty_map = {
        'Easy': {'skill': 0, 'elo': 1350},
        'Medium': {'skill': 10, 'elo': 1500},
        'Hard': {'skill': 20, 'elo': 2850},
    }

    if game_mode == '2':
        r1_set = game_settings.get('robot1')
        r2_set = game_settings.get('robot2')
        d1 = r1_set.get('difficulty')
        conf1 = difficulty_map[d1]
        skill1, elo1 = conf1['skill'], conf1['elo']
        d2 = r2_set.get('difficulty')
        conf2 = difficulty_map[d2]
        skill2, elo2 = conf2['skill'], conf2['elo']
        print(f"🔥 MODE 2: Panda 1 [{d1}:{elo1}] vs Panda 2 [{d2}:{elo2}]")
    else:
        d_name = game_settings.get('difficulty')
        conf1 = difficulty_map[d_name]
        skill1, elo1 = conf1['skill'], conf1['elo']
        print(f"🔥 MODE 1: Opponent Panda 1 set to [{d_name}:{elo1}]")

    # ══════════════════════════════════════════════════════════════
    #  Game Turn Loop
    # ══════════════════════════════════════════════════════════════
    while not reset_game and not rospy.is_shutdown():
        # Handle pause
        while game_paused and not reset_game and not rospy.is_shutdown():
            rospy.sleep(0.1)
        if reset_game:
            break

        board_obj = chess.Board(current_fen)
        is_human_turn = (
            game_mode == '1'
            and (
                (board_obj.turn == chess.WHITE and user_color == 'white')
                or (board_obj.turn == chess.BLACK and user_color == 'black')
            )
        )

        # ────────────────────────────────────────────────────────
        #  Human's Turn (HvR mode)
        # ────────────────────────────────────────────────────────
        if is_human_turn:
            print(f"👉 Your move ({user_color.upper()}): waiting for /chess/move...")

            # ── انتظار الحركة (من board_tracker أو terminal) ──
            mv = None
            while mv is None and not reset_game and not rospy.is_shutdown():
                # 1) Check ROS move (from board_tracker or display)
                if gui_move_ready.wait(timeout=0.15):
                    mv = final_gui_move.strip().lower()
                    gui_move_ready.clear()
                    break

                # 2) Check terminal input (debugging only)
                if select.select([sys.stdin], [], [], 0.0)[0]:
                    line_in = sys.stdin.readline().strip().lower()
                    if uci_format_ok(line_in):
                        mv = line_in
                        break
                    else:
                        pub_gui_status.publish("Invalid format! Use e2e4")
                        print(">>> Invalid format!")

            if reset_game or rospy.is_shutdown():
                break
            if mv is None:
                continue

            # ── Validate the move ──
            try:
                temp_board = chess.Board(current_fen)
                from_sq = chess.parse_square(mv[0:2])
                piece = temp_board.piece_at(from_sq)
                to_rank = chess.square_rank(chess.parse_square(mv[2:4]))
                is_promo_potential = (
                    piece and piece.piece_type == chess.PAWN
                    and to_rank in (0, 7)
                    and len(mv) == 4
                )
                check_mv = mv + 'q' if is_promo_potential else mv

                if chess.Move.from_uci(check_mv) not in temp_board.legal_moves:
                    pub_gui_status.publish(f"Illegal move: {mv}")
                    print(f">>> ILLEGAL MOVE: {mv}")
                    continue  # يرجع لأول الـ game loop → يدخل human turn تاني

                if is_promo_potential:
                    # اطلب من الـ GUI يعرض نافذة الترقية
                    print("♟  Opening Promotion Dialog on GUI...")
                    pub_gui_status.publish(f"__PROMOTION__:{mv}")
                    gui_move_ready.clear()
                    # انتظر رد الـ GUI بحرف الترقية
                    while not gui_move_ready.is_set() and not reset_game:
                        time.sleep(0.1)
                    if reset_game:
                        break
                    mv = final_gui_move.strip().lower()
                    gui_move_ready.clear()

                move_obj = chess.Move.from_uci(mv)
                move_to_send = mv

                # Castling handling
                if temp_board.is_castling(move_obj):
                    castling_data = {"king_move": mv, "rook_move": "", "castling_flag": 1}
                    if mv == "e1g1":
                        castling_data["rook_move"] = "h1f1"
                    elif mv == "e1c1":
                        castling_data["rook_move"] = "a1d1"
                    elif mv == "e8g8":
                        castling_data["rook_move"] = "h8f8"
                    elif mv == "e8c8":
                        castling_data["rook_move"] = "a8d8"
                    move_to_send = json.dumps(castling_data)
                    print(f"🏰 [HUMAN CASTLING]: {move_to_send}")
                elif temp_board.is_capture(move_obj):
                    print(f"\n🔥 [CAPTURE] You took a piece at {mv[2:4]}!")

                moves_list.append(mv)
                proc.stdin.write(f"position startpos moves {' '.join(moves_list)}\n")
                proc.stdin.flush()
                current_fen = get_board()
                publish_to_gui(current_fen, last_move=mv)

                if not reset_game:
                    next_color = "Black" if board_obj.turn == chess.WHITE else "White"
                    pub_gui_status.publish(f"{next_color}'s turn")

            except Exception as e:
                print(f">>> Error in move processing: {e}")
                pub_gui_status.publish(f"Error: {e}")
                continue

        # ────────────────────────────────────────────────────────
        #  Robot vs Robot
        # ────────────────────────────────────────────────────────
        elif game_mode == '2':
            is_p1_turn = (
                (board_obj.turn == chess.WHITE and r1_set.get('color', '').lower() == 'white')
                or (board_obj.turn == chess.BLACK and r1_set.get('color', '').lower() == 'black')
            )
            current_pub = move_pub1 if is_p1_turn else move_pub2
            current_skill = skill1 if is_p1_turn else skill2
            current_elo = elo1 if is_p1_turn else elo2

            robot_name = "Robot 1" if is_p1_turn else "Robot 2"
            robot_color = r1_set.get('color') if is_p1_turn else r2_set.get('color')

            if not reset_game:
                pub_gui_status.publish(f"{robot_name} ({robot_color}) thinking...")
                print(f"\nStockfish ({robot_name}) thinking...")

            set_engine_strength(current_skill, current_elo)
            best = get_best_move()
            if not best or reset_game:
                break

            engine_board = chess.Board(current_fen)
            move_obj = chess.Move.from_uci(best)
            move_to_send = best

            if engine_board.is_castling(move_obj):
                castling_data = {"king_move": best, "rook_move": "", "castling_flag": 1}
                if best == "e1g1":
                    castling_data["rook_move"] = "h1f1"
                elif best == "e1c1":
                    castling_data["rook_move"] = "a1d1"
                elif best == "e8g8":
                    castling_data["rook_move"] = "h8f8"
                elif best == "e8c8":
                    castling_data["rook_move"] = "a8d8"
                move_to_send = json.dumps(castling_data)
                print(f"🏰 [ROBOT CASTLING]: {move_to_send}")
            elif engine_board.is_capture(move_obj):
                print(f"\n🔥 [CAPTURE] {robot_name} took a piece at {best[2:4]}!")
                move_to_send = best + "x"

            if len(best) == 5:
                print(f"✨ [PROMOTION] {robot_name} promoted to {best[4]}!")

            moves_list.append(best)
            proc.stdin.write(f"position startpos moves {' '.join(moves_list)}\n")
            proc.stdin.flush()
            current_fen = get_board()
            print(f"{robot_name} plays: {best}")

            robot_ready_flag = False
            current_pub.publish(move_to_send)
            wait_for_robot()

            if not reset_game:
                next_color = "Black" if board_obj.turn == chess.WHITE else "White"
                pub_gui_status.publish(f"{next_color}'s turn")
            publish_to_gui(current_fen, last_move=best)

        # ────────────────────────────────────────────────────────
        #  Robot's Turn (HvR mode)
        # ────────────────────────────────────────────────────────
        elif game_mode == '1' and not is_human_turn:
            robot_color = "Black" if user_color == "white" else "White"
            if not reset_game:
                pub_gui_status.publish(f"Robot 1 ({robot_color}) thinking...")
                print("\nStockfish (Panda 1) thinking...")

            set_engine_strength(skill1, elo1)
            best = get_best_move()
            if not best or reset_game:
                break

            engine_board = chess.Board(current_fen)
            move_obj = chess.Move.from_uci(best)
            move_to_send = best

            if engine_board.is_castling(move_obj):
                castling_data = {"king_move": best, "rook_move": "", "castling_flag": 1}
                if best == "e1g1":
                    castling_data["rook_move"] = "h1f1"
                elif best == "e1c1":
                    castling_data["rook_move"] = "a1d1"
                elif best == "e8g8":
                    castling_data["rook_move"] = "h8f8"
                elif best == "e8c8":
                    castling_data["rook_move"] = "a8d8"
                move_to_send = json.dumps(castling_data)
                print(f"🏰 [ROBOT CASTLING]: {move_to_send}")
            elif engine_board.is_capture(move_obj):
                print(f"\n🔥 [CAPTURE] Panda 1 took a piece at {best[2:4]}!")
                move_to_send = best + "x"

            if len(best) == 5:
                print(f"✨ [PROMOTION] Panda 1 promoted to {best[4]}!")

            moves_list.append(best)
            proc.stdin.write(f"position startpos moves {' '.join(moves_list)}\n")
            proc.stdin.flush()
            current_fen = get_board()
            print(f"Panda 1 plays: {best}")

            robot_ready_flag = False
            move_pub1.publish(move_to_send)
            wait_for_robot()

            if not reset_game:
                pub_gui_status.publish("Your turn")
            publish_to_gui(current_fen, last_move=best)

        # ── Check game over ──
        if check_game_over(current_fen, game_mode):
            reset_game = True

    print("♻ Game loop terminated. Preparing for new session...")

proc.terminate()
