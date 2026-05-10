#!/usr/bin/env python3
 
import sys
import json
import threading
import rospy
from std_msgs.msg import String
 
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QGroupBox, QStackedWidget,
    QFrame, QScrollArea, QSizePolicy, QDialog
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont, QPainter, QColor
 
 
# ─────────────────────────────────────────────
#  Chess board widget (display-only)
# ─────────────────────────────────────────────
PIECE_UNICODE = {
    'wK': '♔', 'wQ': '♕', 'wR': '♖', 'wB': '♗', 'wN': '♘', 'wP': '♙',
    'bK': '♚', 'bQ': '♛', 'bR': '♜', 'bB': '♝', 'bN': '♞', 'bP': '♟',
}
 
INITIAL_POSITION = [
    ['bR','bN','bB','bQ','bK','bB','bN','bR'],
    ['bP','bP','bP','bP','bP','bP','bP','bP'],
    [None]*8, [None]*8, [None]*8, [None]*8,
    ['wP','wP','wP','wP','wP','wP','wP','wP'],
    ['wR','wN','wB','wQ','wK','wB','wN','wR'],
]
 
 


# ─────────────────────────────────────────────
#  Promotion Dialog
# ─────────────────────────────────────────────
class PromotionDialog(QDialog):
    """نافذة منبثقة أنيقة لاختيار قطعة الترقية"""
    def __init__(self, human_color, parent=None):
        super().__init__(parent)
        self.chosen = 'q'   # افتراضي: ملكة
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._build(human_color)

    def _build(self, human_color):
        color_prefix = 'w' if human_color == 'white' else 'b'
        pieces = [
            ('q', PIECE_UNICODE[f'{color_prefix}Q'], 'Queen'),
            ('r', PIECE_UNICODE[f'{color_prefix}R'], 'Rook'),
            ('b', PIECE_UNICODE[f'{color_prefix}B'], 'Bishop'),
            ('n', PIECE_UNICODE[f'{color_prefix}N'], 'Knight'),
        ]

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        card = QWidget()
        card.setObjectName("promoCard")
        card.setStyleSheet("""
            QWidget#promoCard {
                background-color: #141414;
                border: 1px solid #2a2a2a;
                border-radius: 14px;
            }
        """)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 18, 20, 18)
        card_layout.setSpacing(14)

        title = QLabel("♟  PROMOTION")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setFont(QFont('Consolas', 11, QFont.Weight.Bold))
        title.setStyleSheet("color: #4da6ff; letter-spacing: 3px;")
        card_layout.addWidget(title)

        sub = QLabel("Choose your piece")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setStyleSheet("color: #555; font-family: 'Consolas'; font-size: 10px; letter-spacing: 1px;")
        card_layout.addWidget(sub)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        for code, symbol, name in pieces:
            btn = QPushButton()
            btn.setFixedSize(72, 86)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: #1a1a1a;
                    border: 1px solid #333;
                    border-radius: 10px;
                    color: #e8e8e8;
                    font-family: 'Segoe UI Symbol';
                    font-size: 32px;
                    padding-bottom: 4px;
                }}
                QPushButton:hover {{
                    background-color: #1a3a5c;
                    border: 1px solid #4da6ff;
                    color: #ffffff;
                }}
                QPushButton:pressed {{
                    background-color: #0f2a40;
                }}
            """)
            btn.setText(symbol)
            btn.setToolTip(name)

            name_lbl = QLabel(name)
            name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            name_lbl.setStyleSheet("color: #555; font-family: 'Consolas'; font-size: 9px; letter-spacing: 1px;")

            col = QVBoxLayout()
            col.setSpacing(4)
            col.addWidget(btn)
            col.addWidget(name_lbl)
            btn_row.addLayout(col)

            def make_handler(c):
                def handler():
                    self.chosen = c
                    self.accept()
                return handler
            btn.clicked.connect(make_handler(code))

        card_layout.addLayout(btn_row)
        outer.addWidget(card)

    def get_choice(self):
        return self.chosen

class ChessBoardWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.board          = [row[:] for row in INITIAL_POSITION]
        self.last_move      = None
        self.flipped        = False
        self.king_in_check  = None   # [row, col] في إحداثيات board_2d أو None
        self.setMinimumSize(280, 280)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_position(self, board, last_move=None, king_in_check=None):
        self.board         = board
        self.last_move     = last_move
        self.king_in_check = king_in_check
        self.update()
 
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
 
        side     = min(self.width(), self.height())
        offset_x = (self.width()  - side) // 2
        offset_y = (self.height() - side) // 2
        cell     = side // 8
 
        light           = QColor('#F0D9B5')
        dark            = QColor('#B58863')
        last_move_color = QColor(205, 210, 106, 160)
        check_color     = QColor(220, 50, 50, 200)   # أحمر للكش
 

        for row in range(8):
            for col in range(8):
                r = 7 - row if self.flipped else row
                c = 7 - col if self.flipped else col
                x = offset_x + col * cell
                y = offset_y + row * cell

                color = light if (row + col) % 2 == 0 else dark
                painter.fillRect(x, y, cell, cell, color)

                # هايلايت آخر حركة
                if self.last_move:
                    (r1, c1), (r2, c2) = self.last_move
                    if (r, c) in [(r1, c1), (r2, c2)]:
                        painter.fillRect(x, y, cell, cell, last_move_color)

                # هايلايت مربع الملك عند الكش
                if self.king_in_check is not None:
                    kr, kc = self.king_in_check
                    if r == kr and c == kc:
                        painter.fillRect(x, y, cell, cell, check_color)

                piece = self.board[r][c]
                if piece:
                    symbol    = PIECE_UNICODE.get(piece, '?')
                    font      = QFont('Segoe UI Symbol', int(cell * 0.62))
                    font.setBold(False)
                    painter.setFont(font)
                    color_pen = QColor('#f5f5f5') if piece.startswith('w') else QColor('#1a1a1a')
                    painter.setPen(color_pen)
                    painter.drawText(x, y, cell, cell, Qt.AlignCenter, symbol)
 
        painter.end()
 
 
# ─────────────────────────────────────────────
#  Home Screen
# ─────────────────────────────────────────────
class HomeScreen(QWidget):
    def __init__(self, on_start):
        super().__init__()
        self.on_start = on_start
        # ── Robot vs Robot independent color selections ──
        self.robot1_color = 'White'
        self.robot2_color = 'Black'
        self._build()
 
    def _build(self):
        self.setStyleSheet("""
            QWidget { background-color: #0f0f0f; color: #e8e8e8; }
            QGroupBox {
                color: #888888;
                border: 1px solid #2a2a2a;
                border-radius: 8px;
                margin-top: 14px;
                padding: 14px 12px 12px 12px;
                font-size: 11px;
                letter-spacing: 1.5px;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; }
            QPushButton#toggleBtn {
                background-color: #181818;
                color: #bbbbbb;
                border: 1px solid #333333;
                border-radius: 6px;
                padding: 10px 14px;
                font-size: 12px;
                font-family: 'Consolas';
            }
            QPushButton#toggleBtn:hover   { background-color: #222222; }
            QPushButton#toggleBtn:checked {
                background-color: #1a3a5c;
                color: #4da6ff;
                border: 1px solid #2255aa;
            }
            QPushButton#startBtn {
                background-color: #1a3a5c;
                color: #4da6ff;
                border: 1px solid #2255aa;
                border-radius: 8px;
                padding: 16px;
                font-size: 14px;
                font-family: 'Consolas';
                letter-spacing: 2px;
            }
            QPushButton#startBtn:hover   { background-color: #224a6e; color: #66bbff; }
            QPushButton#startBtn:pressed { background-color: #0f2a40; }
        """)
 
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(0)
 
        header = QLabel("♟  CHESS ROBOT")
        header.setFont(QFont('Consolas', 17, QFont.Weight.Bold))
        header.setStyleSheet("color: #4da6ff; letter-spacing: 4px; margin-bottom: 4px;")
        layout.addWidget(header)
 
        sub = QLabel("CONTROL PANEL")
        sub.setFont(QFont('Consolas', 9))
        sub.setStyleSheet("color: #444; letter-spacing: 6px; margin-bottom: 16px;")
        layout.addWidget(sub)
 
        # ── Mode ──
        mode_group  = QGroupBox("PLAYING MODE")
        mode_layout = QHBoxLayout()
        mode_layout.setSpacing(10)
        self.btn_hvr = QPushButton("Human  vs  Robot")
        self.btn_rvr = QPushButton("Robot  vs  Robot")
        for b in (self.btn_hvr, self.btn_rvr):
            b.setObjectName("toggleBtn"); b.setCheckable(True)
        self._exclusive(self.btn_hvr, self.btn_rvr)
        self.btn_hvr.setChecked(True)
        # Connect mode switching — lambda لتجنب تمرير checked كـ argument
        self.btn_hvr.clicked.connect(lambda: self._on_mode_hvr())
        self.btn_rvr.clicked.connect(lambda: self._on_mode_rvr())
        mode_layout.addWidget(self.btn_hvr)
        mode_layout.addWidget(self.btn_rvr)
        mode_group.setLayout(mode_layout)
        layout.addWidget(mode_group)
        layout.addSpacing(10)

        # ── Stacked area: HvR settings vs RvR settings ──
        # This QStackedWidget replaces only the middle config section
        self.config_stack = QStackedWidget()
        self.config_stack.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        # ── Page 0: Human vs Robot settings (Difficulty + Color) ──
        hvr_page = QWidget()
        hvr_layout = QVBoxLayout(hvr_page)
        hvr_layout.setContentsMargins(0, 0, 0, 0)
        hvr_layout.setSpacing(10)

        # Difficulty
        diff_group  = QGroupBox("AI DIFFICULTY")
        diff_layout = QHBoxLayout()
        diff_layout.setSpacing(10)
        self.btn_easy   = QPushButton("EASY")
        self.btn_medium = QPushButton("MEDIUM")
        self.btn_hard   = QPushButton("HARD")
        for b in (self.btn_easy, self.btn_medium, self.btn_hard):
            b.setObjectName("toggleBtn"); b.setCheckable(True)
        self._exclusive(self.btn_easy, self.btn_medium, self.btn_hard)
        self.btn_medium.setChecked(True)
        diff_layout.addWidget(self.btn_easy)
        diff_layout.addWidget(self.btn_medium)
        diff_layout.addWidget(self.btn_hard)
        diff_group.setLayout(diff_layout)
        hvr_layout.addWidget(diff_group)

        # Color (Human vs Robot)
        self.selected_color = 'White'
        color_group  = QGroupBox("PLAYING COLOR")
        color_layout = QHBoxLayout()
        color_layout.setSpacing(10)
        self.btn_black = QPushButton("⬛  BLACK")
        self.btn_white = QPushButton("⬜  WHITE")
        for b in (self.btn_white, self.btn_black):
            b.setObjectName("toggleBtn"); b.setCheckable(True)
        self.btn_white.setChecked(True)
        self.btn_black.setChecked(False)
        self.btn_black.clicked.connect(lambda: self._select_black())
        self.btn_white.clicked.connect(lambda: self._select_white())
        color_layout.addWidget(self.btn_white)
        color_layout.addWidget(self.btn_black)
        color_group.setLayout(color_layout)
        hvr_layout.addWidget(color_group)

        self.config_stack.addWidget(hvr_page)   # index 0

        # ── Page 1: Robot vs Robot settings ──
        rvr_page = QWidget()
        rvr_outer = QVBoxLayout(rvr_page)
        rvr_outer.setContentsMargins(0, 0, 0, 0)
        rvr_outer.setSpacing(6)

        rvr_title = QLabel("ROBOT CONFIGURATION")
        rvr_title.setStyleSheet(
            "color: #555; font-family: 'Consolas'; font-size: 10px; "
            "letter-spacing: 2.5px; margin-top: 2px; margin-bottom: 2px;"
        )
        rvr_outer.addWidget(rvr_title)

        robots_row = QHBoxLayout()
        robots_row.setSpacing(10)

        # Robot 1 panel
        r1_group  = QGroupBox("🤖  ROBOT 1")
        r1_group.setStyleSheet(r1_group.styleSheet())   # inherit parent style
        r1_layout = QVBoxLayout()
        r1_layout.setSpacing(6)

        r1_diff_lbl = QLabel("DIFFICULTY")
        r1_diff_lbl.setStyleSheet(
            "color: #555; font-family: 'Consolas'; font-size: 9px; letter-spacing: 2px; margin-bottom: 1px;"
        )
        r1_layout.addWidget(r1_diff_lbl)
        r1_diff_row = QHBoxLayout(); r1_diff_row.setSpacing(5)
        self.r1_easy   = QPushButton("EASY");   self.r1_easy.setObjectName("toggleBtn");   self.r1_easy.setCheckable(True)
        self.r1_medium = QPushButton("MED");    self.r1_medium.setObjectName("toggleBtn"); self.r1_medium.setCheckable(True)
        self.r1_hard   = QPushButton("HARD");   self.r1_hard.setObjectName("toggleBtn");   self.r1_hard.setCheckable(True)
        self._exclusive(self.r1_easy, self.r1_medium, self.r1_hard)
        self.r1_medium.setChecked(True)
        r1_diff_row.addWidget(self.r1_easy)
        r1_diff_row.addWidget(self.r1_medium)
        r1_diff_row.addWidget(self.r1_hard)
        r1_layout.addLayout(r1_diff_row)

        r1_col_lbl = QLabel("COLOR")
        r1_col_lbl.setStyleSheet(
            "color: #555; font-family: 'Consolas'; font-size: 9px; letter-spacing: 2px; margin-top: 4px; margin-bottom: 1px;"
        )
        r1_layout.addWidget(r1_col_lbl)
        r1_col_row = QHBoxLayout(); r1_col_row.setSpacing(5)
        self.r1_white = QPushButton("⬜ WHITE"); self.r1_white.setObjectName("toggleBtn"); self.r1_white.setCheckable(True)
        self.r1_black = QPushButton("⬛ BLACK"); self.r1_black.setObjectName("toggleBtn"); self.r1_black.setCheckable(True)
        self.r1_white.setChecked(True)   # Robot 1 defaults to White
        self.r1_black.setChecked(False)
        self.r1_white.clicked.connect(lambda: self._r1_select_white())
        self.r1_black.clicked.connect(lambda: self._r1_select_black())
        r1_col_row.addWidget(self.r1_white)
        r1_col_row.addWidget(self.r1_black)
        r1_layout.addLayout(r1_col_row)

        r1_group.setLayout(r1_layout)
        robots_row.addWidget(r1_group)

        # Thin vertical separator
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setStyleSheet("color: #2a2a2a; margin: 8px 0px;")
        robots_row.addWidget(sep)

        # Robot 2 panel
        r2_group  = QGroupBox("🤖  ROBOT 2")
        r2_layout = QVBoxLayout()
        r2_layout.setSpacing(6)

        r2_diff_lbl = QLabel("DIFFICULTY")
        r2_diff_lbl.setStyleSheet(
            "color: #555; font-family: 'Consolas'; font-size: 9px; letter-spacing: 2px; margin-bottom: 1px;"
        )
        r2_layout.addWidget(r2_diff_lbl)
        r2_diff_row = QHBoxLayout(); r2_diff_row.setSpacing(5)
        self.r2_easy   = QPushButton("EASY");   self.r2_easy.setObjectName("toggleBtn");   self.r2_easy.setCheckable(True)
        self.r2_medium = QPushButton("MED");    self.r2_medium.setObjectName("toggleBtn"); self.r2_medium.setCheckable(True)
        self.r2_hard   = QPushButton("HARD");   self.r2_hard.setObjectName("toggleBtn");   self.r2_hard.setCheckable(True)
        self._exclusive(self.r2_easy, self.r2_medium, self.r2_hard)
        self.r2_medium.setChecked(True)
        r2_diff_row.addWidget(self.r2_easy)
        r2_diff_row.addWidget(self.r2_medium)
        r2_diff_row.addWidget(self.r2_hard)
        r2_layout.addLayout(r2_diff_row)

        r2_col_lbl = QLabel("COLOR")
        r2_col_lbl.setStyleSheet(
            "color: #555; font-family: 'Consolas'; font-size: 9px; letter-spacing: 2px; margin-top: 4px; margin-bottom: 1px;"
        )
        r2_layout.addWidget(r2_col_lbl)
        r2_col_row = QHBoxLayout(); r2_col_row.setSpacing(5)
        self.r2_white = QPushButton("⬜ WHITE"); self.r2_white.setObjectName("toggleBtn"); self.r2_white.setCheckable(True)
        self.r2_black = QPushButton("⬛ BLACK"); self.r2_black.setObjectName("toggleBtn"); self.r2_black.setCheckable(True)
        self.r2_white.setChecked(False)
        self.r2_black.setChecked(True)   # Robot 2 defaults to Black
        self.r2_white.clicked.connect(lambda: self._r2_select_white())
        self.r2_black.clicked.connect(lambda: self._r2_select_black())
        r2_col_row.addWidget(self.r2_white)
        r2_col_row.addWidget(self.r2_black)
        r2_layout.addLayout(r2_col_row)

        r2_group.setLayout(r2_layout)
        robots_row.addWidget(r2_group)

        rvr_outer.addLayout(robots_row)
        self.config_stack.addWidget(rvr_page)   # index 1

        layout.addWidget(self.config_stack)
        layout.addSpacing(16)
 
        # Start
        self.start_btn = QPushButton("▶   START GAME")
        self.start_btn.setObjectName("startBtn")
        self.start_btn.setMinimumHeight(44)
        self.start_btn.clicked.connect(self._on_start)
        layout.addWidget(self.start_btn)
        layout.addStretch()

        # Start on HvR page
        self.config_stack.setCurrentIndex(0)
 
    # ─── Mode switching ───────────────────────────────────────────────
    def _on_mode_hvr(self):
        self.btn_hvr.setChecked(True)
        self.btn_rvr.setChecked(False)
        self.config_stack.setCurrentIndex(0)

    def _on_mode_rvr(self):
        self.btn_rvr.setChecked(True)
        self.btn_hvr.setChecked(False)
        self.config_stack.setCurrentIndex(1)

    # ─── Exclusive helper ─────────────────────────────────────────────
    def _exclusive(self, *btns):
        def make_handler(clicked_btn, others):
            def handler(checked):
                if checked:
                    for b in others:
                        b.setChecked(False)
                else:
                    clicked_btn.setChecked(True)
            return handler
        for btn in btns:
            others = [b for b in btns if b is not btn]
            btn.clicked.connect(make_handler(btn, others))

    # ─── HvR color helpers ────────────────────────────────────────────
    def _select_white(self):
        self.selected_color = 'White'
        self.btn_white.setChecked(True)
        self.btn_black.setChecked(False)

    def _select_black(self):
        self.selected_color = 'Black'
        self.btn_black.setChecked(True)
        self.btn_white.setChecked(False)

    # ─── Robot 1 color helpers ────────────────────────────────────────
    def _r1_select_white(self):
        self.robot1_color = 'White'
        self.r1_white.setChecked(True)
        self.r1_black.setChecked(False)

    def _r1_select_black(self):
        self.robot1_color = 'Black'
        self.r1_black.setChecked(True)
        self.r1_white.setChecked(False)

    # ─── Robot 2 color helpers ────────────────────────────────────────
    def _r2_select_white(self):
        self.robot2_color = 'White'
        self.r2_white.setChecked(True)
        self.r2_black.setChecked(False)

    def _r2_select_black(self):
        self.robot2_color = 'Black'
        self.r2_black.setChecked(True)
        self.r2_white.setChecked(False)

    # ─── Settings collection ──────────────────────────────────────────
    def get_settings(self):
        mode = "Human vs Robot" if self.btn_hvr.isChecked() else "Robot vs Robot"

        if mode == "Human vs Robot":
            difficulty = "Easy" if self.btn_easy.isChecked() else ("Hard" if self.btn_hard.isChecked() else "Medium")
            color      = self.selected_color
            print("DEBUG get_settings: color =", color)
            return {"mode": mode, "difficulty": difficulty, "color": color}
        else:
            # Robot vs Robot — independent settings per robot
            r1_diff = "Easy" if self.r1_easy.isChecked() else ("Hard" if self.r1_hard.isChecked() else "Medium")
            r2_diff = "Easy" if self.r2_easy.isChecked() else ("Hard" if self.r2_hard.isChecked() else "Medium")
            return {
                "mode": mode,
                "robot1": {"difficulty": r1_diff, "color": self.robot1_color},
                "robot2": {"difficulty": r2_diff, "color": self.robot2_color},
            }
 
    def _on_start(self):
        self.on_start(self.get_settings())
 
 
# ─────────────────────────────────────────────
#  Game Screen
# ─────────────────────────────────────────────
class GameScreen(QWidget):
    def __init__(self, on_home, ros_publish_start, ros_publish_stop, ros_publish_move=None, ros_publish_pause=None):
        super().__init__()
        self.on_home            = on_home
        self.ros_publish_start  = ros_publish_start
        self.ros_publish_stop   = ros_publish_stop
        self.ros_publish_move   = ros_publish_move
        self.ros_publish_pause  = ros_publish_pause
        self.pending_promotion  = None   # UCI الأساسي بدون حرف الترقية
        self.white_secs        = 600
        self.black_secs        = 600
        self.active_player     = 'white'
        self.game_active       = False
        self.moves_list        = []
        self.move_number       = 1
        self.black_move_lbl    = None
        self.human_color       = 'white'
        self.pending_promotion  = None
        self._build()
 
        self.clock_timer = QTimer()
        self.clock_timer.timeout.connect(self._tick)
 
    def _build(self):
        self.setStyleSheet("""
            QWidget { background-color: #0f0f0f; color: #e8e8e8; }
            QLabel#statusBox {
                background-color: #141414;
                border: 1px solid #222;
                border-radius: 6px;
                padding: 7px 10px;
                color: #4da6ff;
                font-family: 'Consolas';
                font-size: 12px;
                letter-spacing: 1px;
            }
            QLabel#clockWhite, QLabel#clockBlack {
                border-radius: 8px;
                font-family: 'Consolas';
                font-size: 22px;
                font-weight: bold;
                padding: 8px 12px;
                min-width: 90px;
            }
            QLabel#clockWhite {
                background-color: #f0d9b5;
                color: #1a1a1a;
                border: 2px solid #c8aa82;
            }
            QLabel#clockBlack {
                background-color: #1a1a1a;
                color: #b0b0b0;
                border: 2px solid #333;
            }
            QLabel#clockWhite[active="true"] { border: 2px solid #4da6ff; }
            QLabel#clockBlack[active="true"] { border: 2px solid #4da6ff; }
            QLabel#playerNameWhite { color: #d4b483; font-family: 'Consolas'; font-size: 11px; letter-spacing: 2px; }
            QLabel#playerNameBlack { color: #888;    font-family: 'Consolas'; font-size: 11px; letter-spacing: 2px; }
            QPushButton#stopBtn {
                background-color: #3a1a1a; color: #ff6b6b;
                border: 1px solid #552222; border-radius: 6px;
                padding: 7px 14px; font-family: 'Consolas'; font-size: 11px;
            }
            QPushButton#stopBtn:hover { background-color: #4a2020; }
            QPushButton#pauseBtn {
                background-color: #1a2a3a; color: #66aaff;
                border: 1px solid #2a4a6a; border-radius: 6px;
                padding: 7px 14px; font-family: 'Consolas'; font-size: 11px;
            }
            QPushButton#pauseBtn:hover   { background-color: #223344; }
            QPushButton#pauseBtn:checked { background-color: #2a3a1a; color: #aaff66; border: 1px solid #4a6a2a; }
            QPushButton#homeBtn {
                background-color: #181818; color: #888;
                border: 1px solid #2a2a2a; border-radius: 6px;
                padding: 7px 14px; font-family: 'Consolas'; font-size: 11px;
            }
            QPushButton#homeBtn:hover { background-color: #222; color: #aaa; }
            QFrame#movePanel {
                background-color: #141414;
                border: 1px solid #222;
                border-radius: 8px;
            }
            QScrollArea { background: transparent; border: none; }
            QScrollArea > QWidget > QWidget { background: transparent; }
        """)
 
        outer = QHBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(10)
 
        # ── Left ──
        left = QVBoxLayout()
        left.setSpacing(6)
 
        top_bar = QHBoxLayout()
        title   = QLabel("♟  CHESS ROBOT")
        title.setStyleSheet("font-family:'Consolas'; font-size:13px; color:#4da6ff; letter-spacing:3px;")
        top_bar.addWidget(title)
        top_bar.addStretch()
        left.addLayout(top_bar)
 
        self.board_widget = ChessBoardWidget()
        left.addWidget(self.board_widget, stretch=1)
 
        self.status_label = QLabel("Waiting for game start...")
        self.status_label.setObjectName("statusBox")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setWordWrap(True)
        left.addWidget(self.status_label)
 
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.stop_btn  = QPushButton("⏹  STOP");   self.stop_btn.setObjectName("stopBtn");  self.stop_btn.setMinimumHeight(36)
        self.pause_btn = QPushButton("⏸  PAUSE");  self.pause_btn.setObjectName("pauseBtn"); self.pause_btn.setMinimumHeight(36); self.pause_btn.setCheckable(True)
        self.home_btn  = QPushButton("⌂  HOME");   self.home_btn.setObjectName("homeBtn");  self.home_btn.setMinimumHeight(36)
        self.stop_btn.clicked.connect(self._stop_game)
        self.pause_btn.clicked.connect(self._toggle_pause)
        self.home_btn.clicked.connect(self._go_home)
        btn_row.addWidget(self.stop_btn)
        btn_row.addWidget(self.pause_btn)
        btn_row.addWidget(self.home_btn)
        left.addLayout(btn_row)
 
        outer.addLayout(left, stretch=3)
 
        # ── Right ──
        right = QVBoxLayout()
        right.setSpacing(6)
 
        black_col = QVBoxLayout(); black_col.setSpacing(2)
        self.lbl_black_name = QLabel("● BLACK");   self.lbl_black_name.setObjectName("playerNameBlack")
        self.clock_black    = QLabel("10:00");      self.clock_black.setObjectName("clockBlack"); self.clock_black.setAlignment(Qt.AlignCenter)
        black_col.addWidget(self.lbl_black_name, alignment=Qt.AlignCenter)
        black_col.addWidget(self.clock_black)
        right.addLayout(black_col)
        right.addSpacing(4)
 
        move_frame        = QFrame(); move_frame.setObjectName("movePanel")
        mf_layout         = QVBoxLayout(move_frame); mf_layout.setContentsMargins(10,10,10,10); mf_layout.setSpacing(0)
        moves_title       = QLabel("MOVES"); moves_title.setStyleSheet("font-family:'Consolas'; font-size:10px; color:#444; letter-spacing:3px; margin-bottom:6px;")
        mf_layout.addWidget(moves_title)
        self.moves_scroll = QScrollArea(); self.moves_scroll.setWidgetResizable(True); self.moves_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.moves_container = QWidget()
        self.moves_layout    = QVBoxLayout(self.moves_container); self.moves_layout.setContentsMargins(0,0,0,0); self.moves_layout.setSpacing(1); self.moves_layout.addStretch()
        self.moves_scroll.setWidget(self.moves_container)
        mf_layout.addWidget(self.moves_scroll)
        right.addWidget(move_frame, stretch=1)
        right.addSpacing(4)
 
        white_col = QVBoxLayout(); white_col.setSpacing(2)
        self.clock_white    = QLabel("10:00");    self.clock_white.setObjectName("clockWhite"); self.clock_white.setAlignment(Qt.AlignCenter)
        self.lbl_white_name = QLabel("○ WHITE");  self.lbl_white_name.setObjectName("playerNameWhite")
        white_col.addWidget(self.clock_white)
        white_col.addWidget(self.lbl_white_name, alignment=Qt.AlignCenter)
        right.addLayout(white_col)
 
        outer.addLayout(right, stretch=1)
 
    def start_new_game(self, settings: dict):
        self.white_secs    = 600
        self.black_secs    = 600
        self.active_player = 'white'
        self.game_active   = True
        self.moves_list    = []
        self.move_number   = 1
        self.black_move_lbl = None
        self.is_rvr = (settings.get('mode') == 'Robot vs Robot')

        if self.is_rvr:
            self.human_color  = None
            self.rvr_r2_color = settings.get('robot2', {}).get('color', 'Black').strip().lower()
        else:
            self.human_color  = settings.get('color', 'White').lower()
            self.rvr_r2_color = 'none'

        for i in reversed(range(self.moves_layout.count())):
            w = self.moves_layout.itemAt(i).widget()
            if w:
                w.deleteLater()
        self.moves_layout.addStretch()
 
        self.board_widget.board         = [row[:] for row in INITIAL_POSITION]
        self.board_widget.last_move     = None
        self.board_widget.king_in_check = None

        if settings['mode'] == "Human vs Robot":
            self.board_widget.flipped = (self.human_color == 'black')
        else:
            # RvR: Robot 2 في الأسفل — نفس منطق HvR بالضبط بس بلون Robot 2
            r2_color = settings.get('robot2', {}).get('color', 'Black').strip().lower()
            self.board_widget.flipped = (r2_color == 'black')

        self.board_widget.update()
 
        if settings['mode'] == "Human vs Robot":
            if settings['color'] == 'White':
                self.lbl_white_name.setText("○ You  (White)")
                self.lbl_black_name.setText("● Robot  (Black)")
            else:
                self.lbl_white_name.setText("○ Robot  (White)")
                self.lbl_black_name.setText("● You  (Black)")
        else:
            r1 = settings.get('robot1', {})
            r2 = settings.get('robot2', {})
            r1_color = r1.get('color', 'White')
            r2_color = r2.get('color', 'Black')
            r1_diff  = r1.get('difficulty', 'Medium')
            r2_diff  = r2.get('difficulty', 'Medium')
            # Robot 2 دايماً في الأسفل (lbl_white_name = تحت)
            # Robot 1 دايماً في الأعلى (lbl_black_name = فوق)
            self.lbl_black_name.setText(f"● Robot 1  ({r1_color}) [{r1_diff}]")
            self.lbl_white_name.setText(f"○ Robot 2  ({r2_color}) [{r2_diff}]")
 
        if settings['mode'] == "Human vs Robot":
            if self.human_color == 'white':
                self.status_label.setText("Your turn  ●  black starts")
            else:
                self.status_label.setText("Robot thinking...  ●  black starts")
        else:
            self.status_label.setText("Robot vs Robot  ●  White starts")
        self._refresh_clocks()
        self._update_clock_active()
        self.clock_timer.start(1000)
 
        self.ros_publish_start(json.dumps(settings))
 
    def ros_update_board(self, board, last_move_uci, turn, human_color=None, king_in_check=None, robot2_color='none'):
        if human_color and human_color not in ('none', None):
            # HvR: اقلب حسب لون الإنسان
            self.human_color = human_color
            self.board_widget.flipped = (self.human_color == 'black')
        elif human_color == 'none' and robot2_color != 'none':
            # RvR: نفس منطق HvR — Robot 2 في الأسفل
            self.board_widget.flipped = (robot2_color == 'black')

        lm = None
        if last_move_uci and len(last_move_uci) >= 4:
            try:
                c1 = ord(last_move_uci[0]) - ord('a')
                r1 = 8 - int(last_move_uci[1])
                c2 = ord(last_move_uci[2]) - ord('a')
                r2 = 8 - int(last_move_uci[3])
                lm = ((r1, c1), (r2, c2))
            except (ValueError, IndexError):
                pass

        self.board_widget.set_position(board, last_move=lm, king_in_check=king_in_check)

        if last_move_uci:
            self._add_move_entry(last_move_uci)

        self.active_player = turn
        self._update_clock_active()

        # لو دور الإنسان ويعمل بروموشن — نافذة الاختيار
        # (chess_node بيبعت turn = 'white'/'black' بعد حركة الروبوت)
        # الإشارة بتجي من __PROMOTION__ في ros_update_status
 
    def ros_update_status(self, text):
        if text == "__GAME_OVER__":
            self.game_active = False
            self.clock_timer.stop()
            return
        if text.startswith("__PROMOTION__:"):
            # chess_node بيبعت '__PROMOTION__:e7e8' ← بيدق وصل للترقية
            # في RvR الروبوت يختار تلقائياً — ما نعرض النافذة
            if getattr(self, 'is_rvr', False) or self.human_color is None:
                return   # chess_node بيعالجها تلقائياً بـ Queen في _rvr_loop
            uci_base = text.split(":")[1]
            self._show_promotion_dialog(uci_base)
            return
        self.status_label.setText(text)
 
    def _tick(self):
        if not self.game_active:
            return
        if self.active_player == 'white':
            self.white_secs = max(0, self.white_secs - 1)
            if self.white_secs == 0:
                self._flag_fallen('white'); return
        else:
            self.black_secs = max(0, self.black_secs - 1)
            if self.black_secs == 0:
                self._flag_fallen('black'); return
        self._refresh_clocks()
 
    def _refresh_clocks(self):
        if self.is_rvr and hasattr(self, 'rvr_r2_color') and self.rvr_r2_color == 'black':
            # RvR + Robot2 أسود: clock_white (تحت) = Robot2 = وقت الأسود
            #                     clock_black (فوق)  = Robot1 = وقت الأبيض
            self.clock_white.setText(self._fmt(self.black_secs))
            self.clock_black.setText(self._fmt(self.white_secs))
        else:
            self.clock_white.setText(self._fmt(self.white_secs))
            self.clock_black.setText(self._fmt(self.black_secs))

    def _fmt(self, s):
        return f"{s//60:02d}:{s%60:02d}"

    def _update_clock_active(self):
        if self.is_rvr and hasattr(self, 'rvr_r2_color') and self.rvr_r2_color == 'black':
            # clock_white (تحت) = Robot2 = أسود
            # clock_black (فوق)  = Robot1 = أبيض
            bottom_active = (self.active_player == 'black')
            top_active    = (self.active_player == 'white')
        else:
            bottom_active = (self.active_player == 'white')
            top_active    = (self.active_player == 'black')

        self.clock_white.setProperty("active", "true" if bottom_active else "false")
        self.clock_black.setProperty("active", "true" if top_active    else "false")
        self.clock_white.style().unpolish(self.clock_white); self.clock_white.style().polish(self.clock_white)
        self.clock_black.style().unpolish(self.clock_black); self.clock_black.style().polish(self.clock_black)
 
    def _flag_fallen(self, player):
        self.game_active = False
        self.clock_timer.stop()
        winner = 'Black' if player == 'white' else 'White'
        self.status_label.setText(f"⏰  Time's up for {player.capitalize()}  —  {winner} wins!")
        # --- الإضافة الجديدة هنا ---
        # إرسال أمر stop آلياً إلى ملف stokfish_v3+.py
        self.ros_publish_stop("stop")
 
    def _add_move_entry(self, uci: str):
        is_white = (len(self.moves_list) % 2 == 0)
        self.moves_list.append(uci)
 
        if is_white:
            row_w  = QWidget()
            row_l  = QHBoxLayout(row_w); row_l.setContentsMargins(4,2,4,2); row_l.setSpacing(4)
            num_l  = QLabel(f"{self.move_number}."); num_l.setStyleSheet("color:#444; font-family:'Consolas'; font-size:11px; min-width:24px;")
            w_lbl  = QLabel(uci); w_lbl.setStyleSheet("color:#e8d5b0; font-family:'Consolas'; font-size:12px; font-weight:bold; min-width:52px;")
            self.black_move_lbl = QLabel("..."); self.black_move_lbl.setStyleSheet("color:#888; font-family:'Consolas'; font-size:12px; min-width:52px;")
            row_l.addWidget(num_l); row_l.addWidget(w_lbl); row_l.addWidget(self.black_move_lbl); row_l.addStretch()
            self.moves_layout.insertWidget(self.moves_layout.count() - 1, row_w)
            self.move_number += 1
        else:
            if self.black_move_lbl:
                self.black_move_lbl.setText(uci)
                self.black_move_lbl.setStyleSheet("color:#aaaaaa; font-family:'Consolas'; font-size:12px; min-width:52px;")
 
        QTimer.singleShot(50, lambda: self.moves_scroll.verticalScrollBar().setValue(
            self.moves_scroll.verticalScrollBar().maximum()))
 
    def _check_promotion_needed(self, uci_base):
        """
        يُستدعى من play_terminal أو أي مصدر حركة.
        لو الحركة ترقية بيدق → يعرض نافذة الاختيار.
        uci_base = 4 حروف مثل 'e7e8'
        """
        if self.ros_publish_move is None or self.human_color is None:
            return
        if len(uci_base) < 4:
            return
        # تحقق: البيدق وصل للصف الأخير
        dest_rank = uci_base[3]
        is_white_pawn = (self.human_color == 'white') and dest_rank == '8'
        is_black_pawn = (self.human_color == 'black') and dest_rank == '1'
        if is_white_pawn or is_black_pawn:
            self._show_promotion_dialog(uci_base)
        else:
            self.ros_publish_move(uci_base)

    def _show_promotion_dialog(self, uci_base):
        if self.human_color is None:
            # RvR — لا نافذة، chess_node يختار تلقائياً
            return
        dlg = PromotionDialog(self.human_color, parent=self)
        # نعرض النافذة في وسط الـ board widget
        board_center = self.board_widget.mapToGlobal(self.board_widget.rect().center())
        dlg.adjustSize()
        dlg.move(board_center.x() - dlg.width() // 2,
                 board_center.y() - dlg.height() // 2)
        self.status_label.setText("♟  Choose promotion piece...")
        dlg.exec()
        piece = dlg.get_choice()
        full_uci = uci_base + piece
        names = {'q': 'Queen', 'r': 'Rook', 'b': 'Bishop', 'n': 'Knight'}
        self.status_label.setText(f"♟  Promoted to {names[piece]}!")
        if self.ros_publish_move:
            self.ros_publish_move(full_uci)

    def _toggle_pause(self):
        if not self.game_active:
            self.pause_btn.setChecked(False); return
        if self.pause_btn.isChecked():
            self.clock_timer.stop()
            self.pause_btn.setText("▶  RESUME")
            self.status_label.setText("⏸  Game paused")
            if self.ros_publish_pause:
                self.ros_publish_pause("pause")
        else:
            self.clock_timer.start(1000)
            self.pause_btn.setText("⏸  PAUSE")
            self.status_label.setText("▶  Game resumed")
            if self.ros_publish_pause:
                self.ros_publish_pause("resume")
 
    def _stop_game(self):
        if not self.game_active:
            return
        self.game_active = False
        self.clock_timer.stop()
        self.status_label.setText("⏹  Game stopped")
        self.ros_publish_stop("stop")
 
    def _go_home(self):
        self.game_active = False
        self.clock_timer.stop()
        # ── reset pause button ──────────────────────────────────────────
        self.paused = False if hasattr(self, 'paused') else False
        self.pause_btn.setChecked(False)
        self.pause_btn.setText("⏸  PAUSE")
        if self.ros_publish_pause:
            self.ros_publish_pause("resume")   # تأكد chess_node مش locked
        self.ros_publish_stop("stop")
        self.on_home()
 
 
# ─────────────────────────────────────────────
#  Main Window
# ─────────────────────────────────────────────
class ChessRobotApp(QMainWindow):
    sig_board  = pyqtSignal(object, object, object, object, object, object)
    sig_status = pyqtSignal(str)
 
    def __init__(self, ros_node):
        super().__init__()
        self.ros_node = ros_node
        self.setWindowTitle("Chess Robot — display_node")
        self.setGeometry(100, 60, 740, 540)
        self.setMinimumSize(580, 460)
 
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
 
        self.game_screen = GameScreen(
            on_home            = self._go_home,
            ros_publish_start  = ros_node.publish_game_start,
            ros_publish_stop   = ros_node.publish_game_stop,
            ros_publish_move   = ros_node.publish_move,
            ros_publish_pause  = ros_node.publish_pause,
        )
        self.home_screen = HomeScreen(on_start=self._start_game)
 
        self.stack.addWidget(self.home_screen)    # index 0
        self.stack.addWidget(self.game_screen)    # index 1
        self.stack.setCurrentIndex(0)
 
        self.sig_board.connect(lambda b, lm, t, hc, kc, r2c: self.game_screen.ros_update_board(b, lm, t, hc, kc, r2c))
        self.sig_status.connect(self.game_screen.ros_update_status)
 
        self.setStyleSheet("""
            QMainWindow {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #0d1b2a,stop:1 #1b263b);
            }
            QStackedWidget { background: transparent; }
        """)
 
    def _start_game(self, settings):
        self.game_screen.start_new_game(settings)
        self.stack.setCurrentIndex(1)
 
    def _go_home(self):
        self.stack.setCurrentIndex(0)
 
    def update_board_from_ros(self, board, last_move, turn, human_color='none', king_in_check=None, robot2_color='none'):
        self.sig_board.emit(board, last_move, turn, human_color, king_in_check, robot2_color)

    def update_status_from_ros(self, text):
        self.sig_status.emit(text)
 
 
# ─────────────────────────────────────────────
#  ROS Node wrapper
# ─────────────────────────────────────────────
class DisplayROSNode:
    def __init__(self):
        rospy.init_node('display_node', anonymous=False)
 
        self.pub_start = rospy.Publisher('/chess/game_start', String, queue_size=10)
        self.pub_stop  = rospy.Publisher('/chess/game_stop',  String, queue_size=10)
        self.pub_move  = rospy.Publisher('/chess/move',       String, queue_size=10)
        self.pub_pause = rospy.Publisher('/chess/pause',      String, queue_size=10)
 
        self.gui: ChessRobotApp = None
 
        rospy.Subscriber('/chess/board_state', String, self._cb_board)
        rospy.Subscriber('/chess/status',      String, self._cb_status)
 
        rospy.loginfo("display_node initialized.")
 
    def publish_game_start(self, json_str: str):
        self.pub_start.publish(json_str)
        rospy.loginfo(f"Published game_start: {json_str}")
 
    def publish_game_stop(self, msg: str):
        self.pub_stop.publish(msg)
        rospy.loginfo("Published game_stop.")

    def publish_move(self, uci: str):
        self.pub_move.publish(uci)
        rospy.loginfo(f"Published move: {uci}")

    def publish_pause(self, state: str):
        """state = 'pause' أو 'resume'"""
        self.pub_pause.publish(state)
        rospy.loginfo(f"Published pause: {state}")
 
    def _cb_board(self, msg):
        if self.gui is None:
            return
        try:
            data       = json.loads(msg.data)
            board      = data['board']
            last_move  = data.get('last_move')
            turn       = data.get('turn', 'white')
        except (json.JSONDecodeError, KeyError):
            rospy.logerr("board_state: invalid JSON")
            return

        human_color    = data.get('human_color', 'none')
        king_in_check  = data.get('king_in_check', None)
        robot2_color   = data.get('robot2_color', 'none')
        self.gui.update_board_from_ros(board, last_move, turn, human_color, king_in_check, robot2_color)
 
    def _cb_status(self, msg):
        if self.gui is None:
            return
        self.gui.update_status_from_ros(msg.data)
 
    def spin_in_thread(self):
        t = threading.Thread(target=rospy.spin, daemon=True)
        t.start()
 
 
# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────
def main():
    ros_node = DisplayROSNode()
 
    app = QApplication(sys.argv)
    app.setApplicationName("Chess Robot Display")
 
    win = ChessRobotApp(ros_node)
    ros_node.gui = win
 
    ros_node.spin_in_thread()
    win.show()
 
    sys.exit(app.exec())
 
 
if __name__ == '__main__':
    main()
