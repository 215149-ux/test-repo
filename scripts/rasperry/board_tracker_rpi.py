#!/usr/bin/env python3
"""
board_tracker_rpi.py
====================
نسخة الإنتاج من board_tracker_node.py — تعمل على Raspberry Pi 3 Model B.

الفرق عن النسخة المحاكية:
  - تستخدم GPIOSensorBoard (يقرأ من الريد سويتشات الفعلية) بدل SimulatedSensorBoard.
  - لا واجهة terminal (لا أوامر يدوية) — النود تعمل headless.
  - الإدخال الوحيد: إشارات الحساسات + ROS topics.

التشغيل:
  $ rosrun chess_robot board_tracker_rpi.py

  أو مباشرة:
  $ python3 board_tracker_rpi.py

ملاحظات:
  - لازم يكون python-chess مثبّت: pip3 install python-chess
  - لازم RPi.GPIO مثبّت (عادة مثبّت تلقائياً على Raspbian/Pi OS)
  - عدّل ROW_PINS و COL_PINS في gpio_sensor_board.py حسب توصيلتك الفعلية.
"""

import sys
import os

# نضيف المسار الحالي عشان يلاقي gpio_sensor_board
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# نستورد GPIOSensorBoard من نفس المجلد
from gpio_sensor_board import GPIOSensorBoard

# نستورد كل شيء من board_tracker_node (المحاكي) ونستبدل الـ sensor فقط
# لكن بما أن board_tracker_node قد لا يكون في نفس المجلد، نعيد تعريف
# الأجزاء الضرورية هنا.

# --- الحل: نضيف مسار scripts/ أيضاً ---
scripts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, scripts_dir)

from board_tracker_node import (
    BoardTrackerNode, SimulatedSensorBoard, occupancy_from_chess,
    print_chess_board, Mode
)

import chess


class BoardTrackerRPi(BoardTrackerNode):
    """
    نفس BoardTrackerNode بالضبط، مع فرقين:
    1. يستخدم GPIOSensorBoard بدل SimulatedSensorBoard.
    2. لا واجهة terminal — يعمل headless.
    """

    def __init__(self):
        # نبني الـ parent أولاً (يبني SimulatedSensorBoard)
        super().__init__()

        # نستبدل الحساس بالنسخة الحقيقية
        self._log("Initializing GPIO sensor board (8x8 reed-switch matrix)...")
        try:
            self.sensor = GPIOSensorBoard()
            self._log("GPIO sensor board initialized successfully.")
        except Exception as e:
            self._err(f"Failed to initialize GPIO sensor: {e}")
            self._err("Falling back to SimulatedSensorBoard (for debugging only).")
            # نبقى على SimulatedSensorBoard كـ fallback

    def run(self):
        """نسخة headless — بدون terminal_loop."""
        self._publish_occupancy()

        # التحقق من الوضعية الابتدائية
        self._log("="*50)
        self._log("  Chess Board Tracker — Raspberry Pi Edition")
        self._log("  Waiting for all 32 pieces to be placed...")
        self._log("="*50)

        if not self.verify_initial_setup():
            self._err("Initial setup not confirmed. Exiting.")
            if hasattr(self.sensor, 'cleanup'):
                self.sensor.cleanup()
            return

        print_chess_board(self.chess_board)
        self._log("Board tracker running. Waiting for /chess/game_start...")

        import threading
        scan_thread = threading.Thread(target=self.scan_loop, daemon=True)
        scan_thread.start()

        # بدل terminal_loop: ننتظر ROS shutdown أو Ctrl+C
        try:
            if hasattr(self, '_ros_available') and self._ros_available:
                import rospy
                rospy.spin()
            else:
                # بدون ROS: ننتظر بلا نهاية
                import time
                while not self._shutdown.is_set():
                    time.sleep(1.0)
        except KeyboardInterrupt:
            self._log("Ctrl+C received.")
        finally:
            self._shutdown.set()
            self._pending_event.set()
            scan_thread.join(timeout=2.0)
            if hasattr(self.sensor, 'cleanup'):
                self.sensor.cleanup()
                self._log("GPIO cleaned up.")
            self._log("board_tracker_rpi shutting down.")


# ============================================================================
#  Entry point
# ============================================================================
def main():
    node = BoardTrackerRPi()
    node.run()


if __name__ == '__main__':
    try:
        import rospy
        ROS_OK = True
    except ImportError:
        ROS_OK = False
        print("[WARN] rospy not available — running standalone (no ROS publishing).")

    if ROS_OK:
        try:
            main()
        except Exception as e:
            print(f"[FATAL] {e}")
            import traceback
            traceback.print_exc()
    else:
        main()
