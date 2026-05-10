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

# 1. حساب مواقع المربعات مع عكس المحاور
square_positions = {}
for col_idx, col in enumerate('abcdefgh'):
    for row in range(1, 9):
        x = origin_x + (row-1)*square_size      
        y = origin_y - col_idx*square_size  
        z = origin_z
        square_positions[f"{col}{row}"] = (x, y, z)

# --- [تعديل] حساب إحداثيات الـ Home للروبوت الثاني ---
# استخراج إحداثيات a1 من القاموس وطرح الإزاحة المطلوبة
base_hx, base_hy, _ = square_positions["a8"]
hx = base_hx + 0.1
hy = base_hy + 0.3
hz = safe_h # استخدام الارتفاع الآمن لضمان عدم الاصطدام

original_keys = list(square_positions.keys())
reversed_keys = list(reversed(original_keys))
mirrored_squares = {original_keys[i]: square_positions[reversed_keys[i]] for i in range(len(original_keys))}

# 2. مواقع الترقية
_dist_from_board = 0.02
_gy_start_y = origin_y + square_size + _dist_from_board
_gy_start_x = origin_x + (7 * square_size) 

graveyard_positions = []
for col_g in range(3):
    for row_g in range(8):
        gx = _gy_start_x - (row_g * square_size)
        gy = _gy_start_y + (col_g * square_size)
        gz = origin_z
        graveyard_positions.append((gx, gy, gz))

_py = _gy_start_y + (3 * square_size)
promotion_positions = {
    'q': (_gy_start_x - 0 * square_size, _py, origin_z),
    'r': (_gy_start_x - 1 * square_size, _py, origin_z),
    'b': (_gy_start_x - 2 * square_size, _py, origin_z),
    'n': (_gy_start_x - 3 * square_size, _py, origin_z),
}

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
    move_group.set_pose_reference_frame("panda1_link0")
    pose = Pose()
    pose.position.x, pose.position.y, pose.position.z = float(x), float(y), float(z)
    q = quaternion_from_euler(np.pi, 0.0, np.pi)
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
    rospy.init_node('manual_robot_control_panda2', anonymous=True)
    arm = moveit_commander.MoveGroupCommander(
        "panda2_manipulator",
        robot_description="/panda2/robot_description",
        ns="/panda2")
    arm.set_pose_reference_frame("panda1_link0")
    gripper_client = actionlib.SimpleActionClient('/panda2/franka_gripper/move', MoveAction)
    gripper_client.wait_for_server()

    print(f"\n--- Panda 2 Manual Control (Open: {OPEN_WIDTH}, Close: {CLOSE_WIDTH}) ---")
    print(f"Custom Home Coords: X={hx:.3f}, Y={hy:.3f}, Z={hz:.3f}")

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
            
        # --- [إضافة] خيار الـ home للروبوت الثاني ---
        if cmd == 'home':
            rospy.loginfo(f"Panda 2 moving to custom Home: {hx}, {hy}, {hz}")
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
