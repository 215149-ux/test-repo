#!/usr/bin/env python3
import subprocess
import chess
import sys
import rospy  
import json
import threading
import select  
from std_msgs.msg import String  

# --- متغيرات الـ Feedback للربط الفيزيائي ---
robot_ready_flag = False
settings_received = threading.Event()
game_settings = {}
gui_move_ready = threading.Event()
final_gui_move = ""

# --- متغيرات التحكم (Pause & Stop) ---
game_paused = False
reset_game = False

def status_callback(data):
    global robot_ready_flag
    if data.data in ["panda1_ready", "panda2_ready"]:
        robot_ready_flag = True

def wait_for_robot():
    global robot_ready_flag
    print("⏳ Waiting for robot feedback...")
    while not robot_ready_flag and not rospy.is_shutdown():
        if reset_game: return
        rospy.sleep(0.1)
    print("✅ Robot finished moving.")
    robot_ready_flag = False

def cb_game_start(msg):
    global game_settings, reset_game
    try:
        game_settings = json.loads(msg.data)
        reset_game = False
        settings_received.set()
    except: pass

# آخر حركة وصلت من /chess/move (إما من الدسبلاي بعد الترقية، أو من
# board_tracker بعد كشف حركة فعلية على اللوحة). نضعها في طابور لنقرأها
# في الحلقة الرئيسية بدل stdin.
import queue
move_queue = queue.Queue()

def cb_gui_move(msg):
    """تستقبل حركات UCI من:
       - الدسبلاي (بعد إكمال نافذة الترقية).
       - board_tracker_node (لما اللاعب يحرّك قطعة فيزيائياً على اللوحة).
       النوعان يصلان على نفس التوبيك /chess/move ولا نحتاج التمييز.
    """
    global final_gui_move
    final_gui_move = msg.data
    gui_move_ready.set()
    # نضع الحركة في الطابور حتى تلتقطها الحلقة الرئيسية بدل stdin.
    try:
        move_queue.put_nowait(msg.data)
    except queue.Full:
        pass

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

def publish_to_gui(fen, last_move=None):
    board = chess.Board(fen)
    board_2d = []
    for rank in range(7, -1, -1):
        row = []
        for file in range(8):
            piece = board.piece_at(chess.square(file, rank))
            if piece:
                c_prefix = 'w' if piece.color == chess.WHITE else 'b'
                row.append(f"{c_prefix}{piece.symbol().upper()}")
            else: row.append(None)
        board_2d.append(row)
    
    king_in_check_sq = None
    if board.is_check():
        king_sq = board.king(board.turn)
        if king_sq is not None:
            file_idx = chess.square_file(king_sq)
            rank_idx = chess.square_rank(king_sq)
            king_in_check_sq = [7 - rank_idx, file_idx]

    h_color = game_settings.get('color', 'White').lower()
    if game_settings.get('mode') == "Robot vs Robot":
        h_color = 'none'

    payload = {
        'board': board_2d, 'last_move': last_move,
        'turn': 'white' if board.turn == chess.WHITE else 'black',
        'human_color': h_color, 'king_in_check': king_in_check_sq,
        'robot2_color': game_settings.get('robot2', {}).get('color', 'black').lower() if game_settings.get('mode') == "Robot vs Robot" else 'none'
    }
    pub_gui_board.publish(json.dumps(payload))

def fen_to_board(fen):
    rows = fen.split()[0].split('/')
    board = []
    for r in rows:
        row = []
        for c in r:
            if c.isdigit(): row.extend(['.'] * int(c))
            else: row.append(c)
        board.append(row)
    return board

def print_board(board):
    print("\n    a b c d e f g h")
    print("  +" + "--"*8 + "+")
    for i, row in enumerate(board):
        print(f"{8-i} |" + ' '.join(row) + f"| {8-i}")
    print("  +" + "--"*8 + "+")
    print("    a b c d e f g h\n")

def get_board():
    proc.stdin.write("d\n")
    proc.stdin.flush()
    while True:
        line = proc.stdout.readline()
        if not line: continue
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
        if not line: continue
        line = line.strip()
        if line.startswith("bestmove"):
            parts = line.split()
            return parts[1] if len(parts) >= 2 else None

def uci_format_ok(move):
    if len(move) not in (4,5): return False
    if move[0] not in 'abcdefgh' or move[2] not in 'abcdefgh': return False
    if move[1] not in '12345678' or move[3] not in '12345678': return False
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
            winner_name = f"Robot 1 ({winner_color} wins )" if winner_color.lower() == r1_c.lower() else f"Robot 2 ({winner_color} wins)"
        
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

rospy.init_node('chess_engine_node', anonymous=True)
move_pub1 = rospy.Publisher('robot_move_cmd', String, queue_size=10)
move_pub2 = rospy.Publisher('robot2_move_cmd', String, queue_size=10)

# --- ناشري الألوان للروبوتات ---
color_pub1 = rospy.Publisher('/panda1/color', String, queue_size=10, latch=True)
color_pub2 = rospy.Publisher('/panda2/color', String, queue_size=10, latch=True)

pub_gui_board = rospy.Publisher('/chess/board_state', String, queue_size=10)
pub_gui_status = rospy.Publisher('/chess/status', String, queue_size=10)

rospy.Subscriber('robot_status', String, status_callback)
rospy.Subscriber('/chess/game_start', String, cb_game_start)
rospy.Subscriber('/chess/move', String, cb_gui_move)
rospy.Subscriber('/chess/game_stop', String, cb_game_stop)
rospy.Subscriber('/chess/pause', String, cb_game_pause)

try:
    proc = subprocess.Popen(['stockfish'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
except FileNotFoundError:
    print("Stockfish not found."); sys.exit(1)

proc.stdin.write("uci\n"); proc.stdin.flush()
while "uciok" not in proc.stdout.readline(): pass

while not rospy.is_shutdown():
    print("📡 Waiting for GUI to press START...")
    settings_received.wait()
    
    # --- توزيع وإرسال الألوان فور استلام الإعدادات ---
    mode = game_settings.get("mode", "Human vs Robot")
    if mode == "Robot vs Robot":
        r1_color = game_settings.get('robot1', {}).get('color', 'White').lower()
        r2_color = game_settings.get('robot2', {}).get('color', 'Black').lower()
        color_pub1.publish(r1_color)
        color_pub2.publish(r2_color)
        print(f"📢 [SETUP] Colors Sent -> Panda1: {r1_color}, Panda2: {r2_color}")
    else:
        # في وضع HvR، الروبوت دائماً يأخذ اللون المعاكس للإنسان
        h_color = game_settings.get('color', 'White').lower()
        r1_color = "black" if h_color == "white" else "white"
        color_pub1.publish(r1_color)
        print(f"📢 [SETUP] Color Sent -> Panda1: {r1_color}")

    settings_received.clear() 
    
    reset_game = False
    moves_list = []
    proc.stdin.write("position startpos\n"); proc.stdin.flush()
    current_fen = get_board()
    publish_to_gui(current_fen)
    pub_gui_status.publish("White's turn") 

    game_mode = '2' if mode == "Robot vs Robot" else '1'
    user_color = game_settings.get('color', 'White').lower()
    difficulty_map = {'Easy': {'skill': 0, 'elo': 1350}, 'Medium': {'skill': 10, 'elo': 1500}, 'Hard': {'skill': 20, 'elo': 2850}}

    if game_mode == '2':
        r1_set = game_settings.get('robot1')
        r2_set = game_settings.get('robot2')
        d1 = r1_set.get('difficulty'); conf1 = difficulty_map[d1]; skill1, elo1 = conf1['skill'], conf1['elo']
        d2 = r2_set.get('difficulty'); conf2 = difficulty_map[d2]; skill2, elo2 = conf2['skill'], conf2['elo']
        print(f"🔥 MODE 2: Panda 1 [{d1}:{elo1}] vs Panda 2 [{d2}:{elo2}]")
    else:
        d_name = game_settings.get('difficulty'); conf1 = difficulty_map[d_name]; skill1, elo1 = conf1['skill'], conf1['elo']
        print(f"🔥 MODE 1: Opponent Panda 1 set to [{d_name}:{elo1}]")

    while not reset_game and not rospy.is_shutdown():
        while game_paused and not reset_game and not rospy.is_shutdown():
            rospy.sleep(0.1)
        if reset_game: break

        board_obj = chess.Board(current_fen)
        is_human_turn = (game_mode == '1' and ((board_obj.turn == chess.WHITE and user_color == 'white') or (board_obj.turn == chess.BLACK and user_color == 'black')))

        if is_human_turn:
            print(f"👉 Your move ({user_color.upper()}): "
                  f"(type on terminal, or play physically on the board — both work)")
            # نفرّغ الطابور من أي حركات قديمة قبل بدء انتظار حركة اللاعب
            while not move_queue.empty():
                try: move_queue.get_nowait()
                except queue.Empty: break
            while not reset_game and not rospy.is_shutdown():
                mv = None
                # 1. أولاً نشيك على الطابور (حركة من board_tracker أو الدسبلاي).
                try:
                    mv = move_queue.get_nowait()
                except queue.Empty:
                    pass
                # 2. ثم نشيك على stdin (للاختبار اليدوي).
                if mv is None and select.select([sys.stdin], [], [], 0.1)[0]:
                    mv = sys.stdin.readline().strip().lower()
                if mv is None:
                    if reset_game: break
                    continue
                if not uci_format_ok(mv):
                    pub_gui_status.publish("Invalid format! Use e2e4")
                    print(">>> Invalid format!"); continue
                try:
                    temp_board = chess.Board(current_fen)
                    from_sq = chess.parse_square(mv[0:2])
                    piece = temp_board.piece_at(from_sq)
                    to_rank = chess.square_rank(chess.parse_square(mv[2:4]))
                    is_promo_potential = (piece and piece.piece_type == chess.PAWN and to_rank in (0,7) and len(mv) == 4)
                    check_mv = mv + 'q' if is_promo_potential else mv

                    if chess.Move.from_uci(check_mv) in temp_board.legal_moves:
                        if is_promo_potential:
                            print("♟  Opening Promotion Dialog on GUI...")
                            pub_gui_status.publish(f"__PROMOTION__:{mv}")
                            gui_move_ready.clear(); gui_move_ready.wait()
                            mv = final_gui_move

                        move_obj = chess.Move.from_uci(mv)
                        move_to_send = mv
                        if temp_board.is_castling(move_obj):
                            castling_data = {"king_move": mv, "rook_move": "", "castling_flag": 1}
                            if mv == "e1g1": castling_data["rook_move"] = "h1f1"
                            elif mv == "e1c1": castling_data["rook_move"] = "a1d1"
                            elif mv == "e8g8": castling_data["rook_move"] = "h8f8"
                            elif mv == "e8c8": castling_data["rook_move"] = "a8d8"
                            move_to_send = json.dumps(castling_data)
                            print(f"🏰 [HUMAN CASTLING]: {move_to_send}")
                        elif temp_board.is_capture(move_obj):
                            print(f"\n🔥 [CAPTURE] You took a piece at {mv[2:4]}!")

                        moves_list.append(mv)
                        if not reset_game:
                            next_color = "Black" if board_obj.turn == chess.WHITE else "White"
                            pub_gui_status.publish(f"{next_color}'s turn")
                        break
                    else:
                        pub_gui_status.publish(f"Illegal move: {mv}")
                        print(">>> ILLEGAL MOVE!")
                except: print(">>> Error in move detection.")
                if reset_game: break

            if reset_game: break
            proc.stdin.write(f"position startpos moves {' '.join(moves_list)}\n"); proc.stdin.flush()
            current_fen = get_board(); publish_to_gui(current_fen, last_move=moves_list[-1])

        elif game_mode == '2':
            is_p1_turn = (board_obj.turn == chess.WHITE and r1_set.get('color','').lower() == 'white') or \
                         (board_obj.turn == chess.BLACK and r1_set.get('color','').lower() == 'black')
            current_pub = move_pub1 if is_p1_turn else move_pub2
            current_skill = skill1 if is_p1_turn else skill2
            current_elo = elo1 if is_p1_turn else elo2
            
            robot_name = "Robot 1" if is_p1_turn else "Robot 2"
            robot_color = r1_set.get('color') if is_p1_turn else r2_set.get('color')
            
            if not reset_game:
                pub_gui_status.publish(f"{robot_name} ({robot_color}) turn")
                print(f"\nStockfish ({robot_name}) thinking...")
            
            set_engine_strength(current_skill, current_elo)
            best = get_best_move()
            if not best or reset_game: break
            
            engine_board = chess.Board(current_fen)
            move_obj = chess.Move.from_uci(best)
            move_to_send = best

            if engine_board.is_castling(move_obj):
                castling_data = {"king_move": best, "rook_move": "", "castling_flag": 1}
                if best == "e1g1": castling_data["rook_move"] = "h1f1"
                elif best == "e1c1": castling_data["rook_move"] = "a1d1"
                elif best == "e8g8": castling_data["rook_move"] = "h8f8"
                elif best == "e8c8": castling_data["rook_move"] = "a8d8"
                move_to_send = json.dumps(castling_data)
                print(f"🏰 [ROBOT CASTLING]: {move_to_send}")
            elif engine_board.is_capture(move_obj):
                print(f"\n🔥 [CAPTURE] {robot_name} took a piece at {best[2:4]}!")
                move_to_send = best + "x"
            
            if len(best) == 5: print(f"✨ [PROMOTION] {robot_name} promoted to {best[4]}!")
            moves_list.append(best)
            proc.stdin.write(f"position startpos moves {' '.join(moves_list)}\n"); proc.stdin.flush()
            current_fen = get_board()
            print(f"{robot_name} plays: {best}")
            
            robot_ready_flag = False
            current_pub.publish(move_to_send)
            wait_for_robot()
            
            if not reset_game:
                next_color = "Black" if board_obj.turn == chess.WHITE else "White"
                pub_gui_status.publish(f"{next_color}'s turn")
            publish_to_gui(current_fen, last_move=best)

        board_obj = chess.Board(current_fen)
        if not is_human_turn and game_mode == '1' and not reset_game:
            robot_color = "Black" if user_color == "white" else "White"
            if not reset_game:
                pub_gui_status.publish(f"Robot 1 ({robot_color}) turn")
                print("\nStockfish (Panda 1) thinking...")
            
            set_engine_strength(skill1, elo1)
            best = get_best_move()
            if best:
                engine_board = chess.Board(current_fen)
                move_obj = chess.Move.from_uci(best)
                move_to_send = best

                if engine_board.is_castling(move_obj):
                    castling_data = {"king_move": best, "rook_move": "", "castling_flag": 1}
                    if best == "e1g1": castling_data["rook_move"] = "h1f1"
                    elif best == "e1c1": castling_data["rook_move"] = "a1d1"
                    elif best == "e8g8": castling_data["rook_move"] = "h8f8"
                    elif best == "e8c8": castling_data["rook_move"] = "a8d8"
                    move_to_send = json.dumps(castling_data)
                    print(f"🏰 [ROBOT CASTLING]: {move_to_send}")
                elif engine_board.is_capture(move_obj):
                    print(f"\n🔥 [CAPTURE] Panda 1 took a piece at {best[2:4]}!")
                    move_to_send = best + "x"

                if len(best) == 5: print(f"✨ [PROMOTION] Panda 1 promoted to {best[4]}!")
                moves_list.append(best)
                proc.stdin.write(f"position startpos moves {' '.join(moves_list)}\n"); proc.stdin.flush()
                current_fen = get_board()
                print(f"Panda 1 plays: {best}")
                
                robot_ready_flag = False
                move_pub1.publish(move_to_send)
                wait_for_robot()
                
                if not reset_game:
                    next_color = "Black" if board_obj.turn == chess.WHITE else "White"
                    pub_gui_status.publish(f"{next_color}'s turn")
                publish_to_gui(current_fen, last_move=best)

        if check_game_over(current_fen, game_mode): reset_game = True

    print("♻ Game loop terminated. Preparing for new session...")

proc.terminate()
