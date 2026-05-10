#!/usr/bin/env python3
import rospy
import moveit_commander
import numpy as np
import sys
import copy
import actionlib
import moveit_msgs.msg
from std_msgs.msg import String
from geometry_msgs.msg import Pose, Quaternion
from tf.transformations import quaternion_from_euler
from franka_gripper.msg import GraspAction, GraspGoal, MoveAction, MoveGoal
import json
# --- إعدادات الرقعة ---    
vc, ac = 1, 1
vg, ag = 0.6, 0.6
travel_h = 0.18
safe_h   = 0.1
pick_h   = 0.05
origin_x, origin_y, origin_z = 0.3981098174137588, -6.41297277950631e-05, 0
square_size = 0.045
square_positions = {}

hx=0
hy=0 
hz = origin_z  
graveyard_next_index = 0  
def update_orientation(color):
    global square_positions, hx, hy,graveyard_next_index
    graveyard_next_index = 0 
    square_positions = {}
    for col_idx, col in enumerate('abcdefgh'):
        for row in range(1, 9):
            if color == "white":
                # التوزيع الأصلي
                x = origin_x + (8 - row) * square_size
                y = origin_y - (7 - col_idx) * square_size
            else:
                # عكس الرقعة للأسود: الصف 8 يصبح الأقرب للروبوت
                x = origin_x + (row - 1) * square_size
                y = origin_y - col_idx * square_size
            square_positions[f"{col}{row}"] = (x, y, origin_z)
    
    # تحديث نقطة الـ Home لتناسب الاتجاه الجديد
    target_home = "h1" if color == "white" else "a8"
    hx_new, hy_new, _ = square_positions[target_home]
    hx = hx_new + 0.1 
    hy = hy_new + 0.3

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

# --- global publisher ---
status_pub = None


def init_gripper_clients():
    grasp_client = actionlib.SimpleActionClient('/panda2/franka_gripper/grasp', GraspAction)
    move_client  = actionlib.SimpleActionClient('/panda2/franka_gripper/move',  MoveAction)
    grasp_client.wait_for_server()
    move_client.wait_for_server()
    return grasp_client, move_client


def gripper_open(move_client, width=0.03, speed=0.1):
    goal = MoveGoal()
    goal.width = float(width)
    goal.speed = float(speed)
    move_client.send_goal(goal)
    move_client.wait_for_result()
    return move_client.get_result()


def gripper_close_full(move_client, speed=0.1):
    goal = MoveGoal()
    goal.width = 0.0
    goal.speed = float(speed)
    move_client.send_goal(goal)
    move_client.wait_for_result()
    return move_client.get_result()


def gripper_grasp(grasp_client, width=0.025, force=30, speed=0.1,
                  eps_inner=0.01, eps_outer=0.01):
    goal = GraspGoal()
    goal.width = float(width)
    goal.speed = float(speed)
    goal.force = float(force)
    goal.epsilon.inner = float(eps_inner)
    goal.epsilon.outer = float(eps_outer)
    grasp_client.send_goal(goal)
    grasp_client.wait_for_result()
    return grasp_client.get_result()


def move_to_pose(move_group, x, y, z, vf, af, cartesian=True):
    move_group.set_pose_reference_frame("panda1_link0")
    pose = Pose()
    pose.position.x = float(x)
    pose.position.y = float(y)
    pose.position.z = float(z)
    q = quaternion_from_euler(np.pi, 0.0, np.pi)
    pose.orientation = Quaternion(*q)

    if cartesian:
        waypoints = [copy.deepcopy(pose)]
        plan, fraction = move_group.compute_cartesian_path(waypoints, 0.05, False)
        if fraction > 0.9:
            current_state = move_group.get_current_state()
            plan = move_group.retime_trajectory(
                current_state, plan,
                velocity_scaling_factor=vf,
                acceleration_scaling_factor=af,
                algorithm="iterative_time_parameterization"
            )
            move_group.execute(plan, wait=True)
        else:
            move_group.set_pose_target(pose)
            move_group.go(wait=True)
    else:
        move_group.set_pose_target(pose)
        move_group.go(wait=True)

    move_group.stop()
    move_group.clear_pose_targets()


def arc_move(move_group,
             sx, sy, ex, ey,
             pick_h,
             h_travel,           
             arc_extra=0.2,
             vf=1, af=1,
             N_up=10, N_curve=40, N_down=10,
             eef_step=0.01):
    
    move_group.set_pose_reference_frame("panda1_link0")
    
    q = quaternion_from_euler(np.pi, 0.0, np.pi)
    ori = Quaternion(*q)

    sx, sy, ex, ey = float(sx), float(sy), float(ex), float(ey)
    pick_h = float(pick_h)
    h_travel = float(h_travel)
    arc_extra = float(arc_extra)

    def lerp(a, b, t):
        return a + (b - a) * t

    def bezier(p0, p1, p2, p3, t):
        u = 1.0 - t
        return (u*u*u)*p0 + 3*(u*u)*t*p1 + 3*u*(t*t)*p2 + (t*t*t)*p3

    waypoints = []

    # ❌ شيلنا: cur = move_group.get_current_pose().pose
    # ✅ بدل هيك، احسب z الحالي من pick_h مباشرة
    # لأن الروبوت دايماً بيكون على pick_h أو safe_h قبل ما تتصل بـ arc_move

    # Phase 1: رفع عمودي من sx,sy
    z_start = float(pick_h)  # الموقع الحالي معروف
    for i in range(max(2, int(N_up))):
        t = i / float(N_up - 1)
        p = Pose()
        p.position.x = sx
        p.position.y = sy
        p.position.z = lerp(z_start, h_travel, t)
        p.orientation = ori
        waypoints.append(copy.deepcopy(p))

    # Phase 2: Bezier curve
    P0 = (sx, sy, h_travel)
    P1 = (sx, sy, h_travel + arc_extra)
    P2 = (ex, ey, h_travel + arc_extra)
    P3 = (ex, ey, h_travel)

    for i in range(max(2, int(N_curve))):
        t = i / float(N_curve - 1)
        p = Pose()
        p.position.x = float(bezier(P0[0], P1[0], P2[0], P3[0], t))
        p.position.y = float(bezier(P0[1], P1[1], P2[1], P3[1], t))
        p.position.z = float(bezier(P0[2], P1[2], P2[2], P3[2], t))
        p.orientation = ori
        if i == 0:
            continue
        waypoints.append(copy.deepcopy(p))

    # Phase 3: نزول عمودي لـ ex,ey
    for i in range(max(2, int(N_down))):
        t = i / float(N_down - 1)
        p = Pose()
        p.position.x = ex
        p.position.y = ey
        p.position.z = lerp(h_travel, pick_h, t)
        p.orientation = ori
        if i == 0:
            continue
        waypoints.append(copy.deepcopy(p))

    move_group.set_start_state_to_current_state()
    plan, fraction = move_group.compute_cartesian_path(
        waypoints,
        float(eef_step),
        False
    )

    if fraction < 0.9:
        rospy.logwarn(f"Connected path incomplete: fraction={fraction:.2f}")
        return False

    current_state = move_group.get_current_state()
    plan = move_group.retime_trajectory(
        current_state,
        plan,
        velocity_scaling_factor=float(vf),
        acceleration_scaling_factor=float(af),
        algorithm="iterative_time_parameterization"
    )

    move_group.execute(plan, wait=True)
    move_group.stop()
    move_group.clear_pose_targets()
    return True


def move_piece(move_group, grasp_client, move_client, move_text):
    global graveyard_next_index
    
    
    
    if move_text.startswith('{'):
          
            data = json.loads(move_text)
            if data.get("castling_flag") == 1:
                k_mv = data.get("king_move")
                r_mv = data.get("rook_move")
                start_king_move = k_mv[:2].lower()
                end_king_move  = k_mv[2:4].lower()
                start_rook_move = r_mv[:2].lower()
                end_rook_move   = r_mv[2:4].lower()
                
                skx, sky, _ = square_positions[start_king_move]
                srx, sry, _ = square_positions[start_rook_move]
                ekx, eky, _ = square_positions[end_king_move]
                erx, ery, _ = square_positions[end_rook_move]
                gripper_close_full(move_client)
                move_to_pose(move_group, hx, hy, safe_h, vc, ac, cartesian=True)
                arc_move(move_group, hx, hy, skx, sky,safe_h,travel_h )
                gripper_open(move_client)
                move_to_pose(move_group, skx, sky, pick_h, vg, ag)
                gripper_grasp(grasp_client)
                rospy.sleep(0.1)
                arc_move(move_group, skx, sky, ekx, eky, pick_h, travel_h)
                gripper_open(move_client)
                rospy.sleep(0.5)
                move_to_pose(move_group, ekx, eky, safe_h, vg, ag)
                gripper_close_full(move_client)
                arc_move(move_group, ekx, eky, srx, sry,safe_h,travel_h )
                gripper_open(move_client)
                move_to_pose(move_group, srx, sry, pick_h, vg, ag)
                gripper_grasp(grasp_client)
                rospy.sleep(0.1)
                arc_move(move_group, srx, sry, erx, ery, pick_h, travel_h)
                gripper_open(move_client)
                rospy.sleep(0.5)
                move_to_pose(move_group, erx, ery, safe_h, vg, ag)
                gripper_close_full(move_client)
                arc_move(move_group, erx, ery, hx, hy,safe_h,travel_h )
                move_group.clear_pose_targets()
                return    

    is_capture  = 'x' in move_text
    clean_move  = move_text.replace('x', '')
    start_square = clean_move[:2].lower()
    end_square   = clean_move[2:4].lower()
    promo_piece  = clean_move[4].lower() if len(clean_move) >= 5 else None
    is_promotion = promo_piece in ('q', 'r', 'b', 'n')

    sx, sy, _ = square_positions[start_square]
    ex, ey, _ = square_positions[end_square]

    rospy.loginfo(f"Move: {start_square}->{end_square} | capture={is_capture} | promotion={is_promotion}")

    # --- capture + promotion ---
    if is_capture and is_promotion:
            gx, gy, gz = graveyard_positions[graveyard_next_index]
            px, py, pz = promotion_positions[promo_piece]
            gripper_close_full(move_client)
            move_to_pose(move_group, hx, hy, safe_h, vc, ac, cartesian=True)
            arc_move(move_group, hx, hy, ex, ey,safe_h,travel_h )
        # 1. شيل القطعة المأكولة من end → graveyard 
        
           
            gripper_open(move_client)
            move_to_pose(move_group, ex, ey, pick_h, vg, ag)
            gripper_grasp(grasp_client)
            rospy.sleep(0.1)

            # 2. انقلها للـ graveyard بحركة arc
            arc_move(move_group, ex, ey, gx, gy, pick_h, travel_h)
            #move_to_pose(move_group, gx, gy, pick_h, vg, ag)
            gripper_open(move_client)
            rospy.sleep(0.5)

            # 3. ارتفع وأغلق الـ gripper
            move_to_pose(move_group, gx, gy, safe_h, vg, ag)
            gripper_close_full(move_client)
            arc_move(move_group, gx, gy, px, py, safe_h, travel_h)
            gripper_open(move_client)
            move_to_pose(move_group, px, py, pick_h, vg, ag)
            gripper_grasp(grasp_client)
            rospy.sleep(0.1)

            arc_move(move_group, px, py, ex, ey, pick_h, travel_h)
            gripper_open(move_client)
            rospy.sleep(0.5)

            move_to_pose(move_group, ex, ey, safe_h, vg, ag)
            gripper_close_full(move_client)
            
            graveyard_next_index += 1
            gx, gy, gz = graveyard_positions[graveyard_next_index]

            arc_move(move_group, ex, ey, sx, sy, safe_h, travel_h)
            gripper_open(move_client)
            move_to_pose(move_group, sx, sy, pick_h, vg, ag)
            gripper_grasp(grasp_client)
            arc_move(move_group, sx, sy, gx, gy, safe_h, travel_h)
            gripper_open(move_client)
            move_to_pose(move_group, gx, gy, pick_h, vg, ag)
            gripper_close_full(move_client)
            graveyard_next_index += 1
            arc_move(move_group, gx, gy, hx, hy,safe_h,travel_h )
            move_group.clear_pose_targets()
            return

    # --- capture عادي ---
    if is_capture:
        if graveyard_next_index >= len(graveyard_positions):
            rospy.logerr("Graveyard is full!")
        else:
            gx, gy, gz = graveyard_positions[graveyard_next_index]
            gripper_close_full(move_client)
            move_to_pose(move_group, hx, hy, safe_h, vc, ac, cartesian=True)
            arc_move(move_group, hx, hy, ex, ey,safe_h,travel_h )
            gripper_open(move_client)
            move_to_pose(move_group, ex, ey, pick_h, vg, ag)
            gripper_grasp(grasp_client)
            rospy.sleep(0.1)
            arc_move(move_group, ex, ey, gx, gy, pick_h, travel_h)
            gripper_open(move_client)
            rospy.sleep(0.5)
            move_to_pose(move_group, gx, gy, safe_h, vg, ag)
            gripper_close_full(move_client)
            arc_move(move_group, gx, gy, sx, sy, safe_h, travel_h)
            gripper_open(move_client)
            move_to_pose(move_group, sx, sy, pick_h, vg, ag)
            gripper_grasp(grasp_client)
            rospy.sleep(0.1)
            arc_move(move_group, sx, sy, ex, ey, pick_h, travel_h)
            gripper_open(move_client)
            rospy.sleep(0.5)
            move_to_pose(move_group, ex, ey, safe_h, vg, ag)
            gripper_close_full(move_client)
            arc_move(move_group, ex, ey, hx, hy,safe_h,travel_h )
            move_group.clear_pose_targets()
            graveyard_next_index += 1
            return

    # --- promotion عادي ---
    if is_promotion:
        if promo_piece not in promotion_positions:
            rospy.logerr(f"Unknown promotion piece: {promo_piece}")
        else:
            gx, gy, gz = graveyard_positions[graveyard_next_index]
            px, py, pz = promotion_positions[promo_piece]
            gripper_close_full(move_client)
            move_to_pose(move_group, hx, hy, safe_h, vc, ac, cartesian=True)
            arc_move(move_group, hx, hy, sx, sy,safe_h,travel_h )
            gripper_open(move_client)
            move_to_pose(move_group, sx, sy, pick_h, vg, ag)
            gripper_grasp(grasp_client)
            rospy.sleep(0.1)
            arc_move(move_group, sx, sy, gx, gy, pick_h, travel_h)
            gripper_open(move_client)
            rospy.sleep(0.5)
            move_to_pose(move_group, gx, gy, safe_h, vg, ag)
            gripper_close_full(move_client)
            arc_move(move_group, gx, gy, px, py, safe_h, travel_h)
            gripper_open(move_client)
            move_to_pose(move_group, px, py, pick_h, vg, ag)
            gripper_grasp(grasp_client)
            rospy.sleep(0.1)
            arc_move(move_group, px, py, ex, ey, pick_h, travel_h)
            gripper_open(move_client)
            rospy.sleep(0.5)
            move_to_pose(move_group, ex, ey, safe_h, vg, ag)
            gripper_close_full(move_client)
            arc_move(move_group, ex, ey, hx, hy,safe_h,travel_h )
            move_group.clear_pose_targets()
            graveyard_next_index += 1
            return

    # --- حركة عادية ---
    gripper_close_full(move_client)
    move_to_pose(move_group, hx, hy, safe_h, vc, ac, cartesian=True)
    arc_move(move_group, hx, hy, sx, sy,safe_h,travel_h )
    gripper_open(move_client)
    move_to_pose(move_group, sx, sy, pick_h, vg, ag)
    gripper_grasp(grasp_client)
    rospy.sleep(0.1)
    arc_move(move_group, sx, sy, ex, ey, pick_h, travel_h)
    gripper_open(move_client)
    rospy.sleep(0.5)
    move_to_pose(move_group, ex, ey, safe_h, vg, ag)
    gripper_close_full(move_client)
    arc_move(move_group, ex, ey, hx, hy,safe_h,travel_h )
    move_group.clear_pose_targets()

def color_cb(msg):
    color = msg.data.strip().lower()
    rospy.loginfo(f"Panda 2 is now playing as: {color}")
    update_orientation(color)
    
   
def move_callback(msg, args):
    move_group, grasp_client, move_client = args
    move_text = msg.data.strip()
    rospy.loginfo(f"Move received: {move_text}")
    try:
        move_piece(move_group, grasp_client, move_client, move_text)
        status_pub.publish("panda2_ready")  # ← إشارة الانتهاء
    except Exception as e:
        rospy.logerr(f"Error executing move: {e}")


def main():
    global status_pub
    moveit_commander.roscpp_initialize(sys.argv)
    rospy.init_node('chessrobot2_moveit', anonymous=False)

    arm = moveit_commander.MoveGroupCommander(
        "panda2_manipulator",
        robot_description="/panda2/robot_description",
        ns="/panda2"
    )
    arm.set_max_velocity_scaling_factor(1)
    arm.set_max_acceleration_scaling_factor(1)
    arm.set_pose_reference_frame("panda1_link0")

    status_pub = rospy.Publisher('robot_status', String, queue_size=10)
    grasp_client, move_client = init_gripper_clients()

    rospy.loginfo("Panda2 Ready. Waiting for moves...")
    rospy.Subscriber('robot2_move_cmd', String, move_callback,
                     (arm, grasp_client, move_client))
    rospy.Subscriber('/panda2/color', String, color_cb)                 
    rospy.spin()


if __name__ == '__main__':
    main()
