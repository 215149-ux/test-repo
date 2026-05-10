#!/usr/bin/env python3
import rospy
import moveit_commander
import numpy as np
import sys
import copy
import actionlib
from std_msgs.msg import String
from geometry_msgs.msg import Pose, Quaternion
from tf.transformations import quaternion_from_euler
from franka_gripper.msg import MoveAction, MoveGoal

# --- متغيرات التحكم العالمية ---
OPEN_WIDTH = 0.04
CLOSE_WIDTH = 0.025
GRIPPER_SPEED = 0.1

v_slow, a_slow = 0.7, 0.7
safe_h = 0.055

# ------------------------------------------------

origin_x, origin_y, origin_z = 0.3981098174137588, -6.41297277950631e-05, 0
square_size = 0.045

# 1. حساب مواقع المربعات
square_positions = {}
for col_idx, col in enumerate('abcdefgh'):
    for row in range(1, 9):
        x = origin_x + (8 - row) * square_size
        y = origin_y - (7 - col_idx) * square_size
        square_positions[f"{col}{row}"] = (x, y, origin_z)

# --- [تعديل] حساب إحداثيات الـ Home المطلوبة ---
# استخراج إحداثيات a1 وتطبيق الإزاحة
base_hx, base_hy, _ = square_positions["h8"]
hx = base_hx - 0.1
hy = base_hy + 0.3
hz = safe_h # استخدام الارتفاع الآمن المعرف في الكود

original_keys = list(square_positions.keys())
reversed_keys = list(reversed(original_keys))
mirrored_squares = {original_keys[i]: square_positions[reversed_keys[i]] for i in range(len(original_keys))}

# 2. مواقع الترقية
_gy_start = origin_y - 8 * square_size - 0.01
_py = _gy_start - 3 * square_size
promotion_positions = {'q': (origin_x, _py, origin_z), 'r': (origin_x + square_size, _py, origin_z), 
                       'b': (origin_x + 2*square_size, _py, origin_z), 'n': (origin_x + 3*square_size, _py, origin_z)}

# 3. مواقع المقبرة
graveyard_positions = []
for col_g in range(3):
    for row_g in range(8):
        graveyard_positions.append((origin_x + row_g * square_size, _gy_start - col_g * square_size, origin_z))

def gripper_control(move_client, action_type):
    goal = MoveGoal()
    if action_type == "open":
        goal.width = float(OPEN_WIDTH)
        rospy.loginfo(f"Opening to: {OPEN_WIDTH}m")
    else:
        goal.width = float(CLOSE_WIDTH)
        rospy.loginfo(f"Closing to: {CLOSE_WIDTH}m")
    
    goal.speed = float(GRIPPER_SPEED)
    move_client.send_goal(goal)
    move_client.wait_for_result()

def move_to_pose(move_group, x, y, z, vf, af):
    pose = Pose()
    pose.position.x, pose.position.y, pose.position.z = float(x), float(y), float(z)
    q = quaternion_from_euler(np.pi, 0.0, 0.0)
    pose.orientation = Quaternion(*q)
    waypoints = [copy.deepcopy(pose)]
    plan, fraction = move_group.compute_cartesian_path(waypoints, 0.05, False)
    if fraction > 0.9:
        plan = move_group.retime_trajectory(move_group.get_current_state(), plan, vf, af, "iterative_time_parameterization")
        move_group.execute(plan, wait=True)
    move_group.stop()
    move_group.clear_pose_targets()

def manual_control():
    moveit_commander.roscpp_initialize(sys.argv)
    rospy.init_node('manual_robot_control_globals', anonymous=True)
    arm = moveit_commander.MoveGroupCommander(
        "panda1_manipulator",
        robot_description="/panda1/robot_description",
        ns="/panda1")
    gripper_client = actionlib.SimpleActionClient('/panda1/franka_gripper/move', MoveAction)
    gripper_client.wait_for_server()

    print(f"\n--- Manual Control (Open: {OPEN_WIDTH}, Close: {CLOSE_WIDTH}) ---")
    print(f"Home Coords: X={hx:.3f}, Y={hy:.3f}, Z={hz:.3f}")

    while not rospy.is_shutdown():
        cmd = input("\nEnter Target/Action: ").strip().lower()
        if cmd == 'exit': break
        if cmd == 'open':
            gripper_control(gripper_client, "open")
            continue
        if cmd == 'close':
            gripper_control(gripper_client, "close")
            continue
        if cmd == 'ready':
            arm.set_named_target('ready')
            arm.go(wait=True)
            continue
            
        # --- [إضافة] خيار الـ home الجديد باستخدام الإحداثيات المحسوبة ---
        if cmd == 'home':
            rospy.loginfo(f"Moving to custom Home: {hx}, {hy}, {hz}")
            move_to_pose(arm, hx, hy, hz, v_slow, a_slow)
            continue

        target_pos = None
        if cmd in mirrored_squares: target_pos = mirrored_squares[cmd]
        elif cmd.startswith('p') and len(cmd) == 2 and cmd[1] in promotion_positions: target_pos = promotion_positions[cmd[1]]
        elif cmd.startswith('x'):
            try:
                idx = int(cmd[1:])
                if 0 <= idx < len(graveyard_positions): target_pos = graveyard_positions[idx]
            except: pass

        if target_pos: move_to_pose(arm, target_pos[0], target_pos[1], safe_h, v_slow, a_slow)
        else: print("Invalid Input!")

if __name__ == '__main__':
    try: manual_control()
    except rospy.ROSInterruptException: pass
