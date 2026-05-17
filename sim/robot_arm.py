"""
机械臂控制器 — 集成 IK 解算

核心工作流：
  目标末端位姿 → MirobotIK.solve() → 关节角度
  → sim.setJointPosition() 驱动仿真

提供高级 pick/place 接口，自动计算目标坐标并调用 IK。
"""

import math

from mirobot_ik import MirobotIK
from scene_config import (
    DT, RENDER_DELAY, SEGMENT_HEIGHT,
    IK_ANIM_STEPS, IK_ANIM_STEPS_FAST,
)


# 末端朝下的姿态 (欧拉角 rad)
TIP_DOWN_ORIENT = [0, math.pi / 2, 0]

# 抬起高度（pick/place 时的安全高度）
SAFE_Z = SEGMENT_HEIGHT + 0.05     # 安全高度（平台上方 50mm = 0.12m）
GRASP_CLEARANCE = 0.005            # 夹爪接近时的间距
MAX_PICK_DISTANCE = 0.03           # 最大抓取距离 30mm（超过则不执行）


class RobotArmController:
    """
    机械臂控制器 — 集成 IK 解算

    核心工作流：
      目标末端位姿 → MirobotIK.solve() → 关节角度
      → sim.setJointPosition() 驱动仿真

    提供高级 pick/place 接口，自动计算目标坐标并调用 IK。
    """

    def __init__(self, sim, name: str, dt: float = DT,
                 render_delay: float = RENDER_DELAY):
        self.sim = sim
        self.name = name
        self.dt = dt
        self.render_delay = render_delay

        # IK 解算器
        self.ik = MirobotIK(sim, name, dt, render_delay)

        # 末端句柄
        self.handle = self.ik.handle
        self.tip_handle = self.ik.tip_handle
        self.joints = self.ik.joints

        # 机械臂基座世界坐标
        self.base_pos = sim.getObjectPosition(self.handle, -1)

        # 当前吸附的工件
        self._attached_part = None

        print(f'  ArmCtrl [{name}]: base=({self.base_pos[0]:.3f}, '
              f'{self.base_pos[1]:.3f}, {self.base_pos[2]:.3f})')

    # ----------------------------------------------------------
    # 运动接口
    # ----------------------------------------------------------

    def move_tip_to(self, target_pos: list,
                    target_orient: list = None,
                    steps: int = IK_ANIM_STEPS):
        """
        IK 解算 + 动画。

        流程：
          1. 纯数学求解默认 IK 解
          2. 平滑动画到该解
          3. 检查到达精度
          4. 如偏差大 → 尝试其他 J1 偏移解，逐个动画尝试
        """
        original_angles = self.ik.get_joint_positions()

        # 默认求解 + 动画
        default_result = self.ik.solve(target_pos)
        self.ik.animate_to_angles(default_result, steps)

        # 检查到达精度
        tip_pos = self.get_tip_position()
        dist = sum((a - b) ** 2
                   for a, b in zip(tip_pos, target_pos)) ** 0.5

        if dist <= MAX_PICK_DISTANCE:
            return  # 一次就到位

        # 偏差大 → 逐个尝试其他 J1 偏移解
        for j1_offset in [math.pi/4, -math.pi/4,
                          math.pi/2, -math.pi/2,
                          3*math.pi/4, -3*math.pi/4,
                          math.pi]:
            trial_angles = list(original_angles)
            trial_angles[0] += j1_offset
            result = self.ik.solve(
                target_pos, initial_angles=trial_angles)
            self.ik.animate_to_angles(result, steps)

            tip_pos = self.get_tip_position()
            new_dist = sum((a - b) ** 2
                           for a, b in zip(tip_pos, target_pos)) ** 0.5
            if new_dist <= MAX_PICK_DISTANCE:
                return  # 找到好解

            # 如果这个也不行，继续试下一个

        # 全部试完仍偏差大
        tip_pos = self.get_tip_position()
        final_dist = sum((a - b) ** 2
                         for a, b in zip(tip_pos, target_pos)) ** 0.5
        if final_dist > MAX_PICK_DISTANCE:
            print(f'    [WARN] [{self.name}] IK 无法到达: '
                  f'{final_dist*1000:.0f}mm')

    def move_to_home(self, steps: int = 25):
        """回到初始姿态"""
        self.ik.move_to_home(anim_steps=steps)

    def get_tip_position(self) -> list:
        """获取末端世界坐标（直接从仿真读取，避免 FK 累积误差）"""
        return self.sim.getObjectPosition(self.tip_handle, -1)

    # ----------------------------------------------------------
    # 工件附着/分离（模拟夹爪）
    # ----------------------------------------------------------

    def pick(self, part_handle: int):
        """吸附工件到末端（带距离校验 + 自动补偿）"""
        # 检查末端到工件的距离
        tip_pos = self.get_tip_position()
        part_pos = self.sim.getObjectPosition(part_handle, -1)
        dist = sum((a - b) ** 2 for a, b in zip(tip_pos, part_pos)) ** 0.5

        if dist > MAX_PICK_DISTANCE:
            print(f'    [WARN] [{self.name}] 抓取距离过远: '
                  f'{dist*1000:.0f}mm > {MAX_PICK_DISTANCE*1000:.0f}mm, '
                  f'自动补偿移动到工件位置')
            # 补偿：先移到工件正上方，再下降到工件位置
            above = [part_pos[0], part_pos[1], SAFE_Z]
            self.move_tip_to(above, TIP_DOWN_ORIENT, IK_ANIM_STEPS_FAST)
            approach = [part_pos[0], part_pos[1],
                        part_pos[2] + GRASP_CLEARANCE]
            self.move_tip_to(approach, TIP_DOWN_ORIENT, IK_ANIM_STEPS_FAST)

            # 再次检查距离
            tip_pos = self.get_tip_position()
            dist = sum((a - b) ** 2
                       for a, b in zip(tip_pos, part_pos)) ** 0.5
            if dist > MAX_PICK_DISTANCE:
                print(f'    [WARN] [{self.name}] 补偿后仍超距: '
                      f'{dist*1000:.0f}mm -- 拒绝抓取')
                return  # 不抓取，不传送
        else:
            print(f'     [{self.name}] 抓取距离: {dist*1000:.1f}mm')

        self._attached_part = part_handle
        self.sim.setObjectParent(part_handle, self.tip_handle, True)

    def release(self, target_pos: list = None,
                parent_handle: int = -1):
        """释放工件"""
        if self._attached_part is not None:
            self.sim.setObjectParent(self._attached_part, parent_handle, True)
            if target_pos is not None:
                self.sim.setObjectPosition(
                    self._attached_part, -1, target_pos)
            self._attached_part = None

    def release_to_shuttle(self, shuttle_handle: int, z_offset: float):
        """释放工件到滑块上"""
        if self._attached_part is not None:
            self.sim.setObjectParent(
                self._attached_part, shuttle_handle, True)
            self.sim.setObjectPosition(
                self._attached_part, shuttle_handle, [0, 0, z_offset])
            self._attached_part = None

    @property
    def is_holding(self) -> bool:
        return self._attached_part is not None

    # ----------------------------------------------------------
    # 高级动作序列
    # ----------------------------------------------------------

    def pick_from_position(self, pos: list, part_handle: int):
        """
        完整的取件动作：
        1. 移到安全高度 → 2. 下降到目标上方 → 3. 吸附 → 4. 提起

        会在下降前刷新工件实时坐标（工件可能在滑块上移动了）。
        """
        # 先到目标上方（安全高度）
        above_pos = [pos[0], pos[1], SAFE_Z]
        self.move_tip_to(above_pos, TIP_DOWN_ORIENT, IK_ANIM_STEPS)

        # 刷新工件实时坐标（可能跟着滑块移动过）
        real_pos = self.sim.getObjectPosition(part_handle, -1)

        # 下降到工件实时位置上方
        approach_pos = [real_pos[0], real_pos[1],
                        real_pos[2] + GRASP_CLEARANCE]
        self.move_tip_to(approach_pos, TIP_DOWN_ORIENT, IK_ANIM_STEPS_FAST)

        # 吸附工件
        self.pick(part_handle)

        # 提起
        lift_pos = [real_pos[0], real_pos[1], SAFE_Z]
        self.move_tip_to(lift_pos, TIP_DOWN_ORIENT, IK_ANIM_STEPS_FAST)

    def place_at_position(self, pos: list,
                          parent_handle: int = -1,
                          z_offset: float = 0.0):
        """
        完整的放件动作：
        1. 移到目标上方 → 2. 下降 → 3. 释放到末端实际位置 → 4. 提起

        如果 IK 无法到达目标，就在末端实际位置释放（就地丢下）。
        """
        if self._attached_part is None:
            return

        # 目标上方（安全高度）
        above_pos = [pos[0], pos[1], SAFE_Z]
        self.move_tip_to(above_pos, TIP_DOWN_ORIENT, IK_ANIM_STEPS)

        # 下降到放置位置上方
        release_pos = [pos[0], pos[1], pos[2] + GRASP_CLEARANCE]
        self.move_tip_to(release_pos, TIP_DOWN_ORIENT, IK_ANIM_STEPS_FAST)

        # 检查末端实际位置
        tip_pos = self.get_tip_position()
        dist = sum((a - b) ** 2
                   for a, b in zip(tip_pos, release_pos)) ** 0.5

        if dist > MAX_PICK_DISTANCE:
            # 到不了 → 就地丢下（仅解除绑定，不移动工件）
            print(f'    [WARN] [{self.name}] 放置偏差 {dist*1000:.0f}mm, '
                  f'就地丢下')
            self.sim.setObjectParent(self._attached_part, -1, True)
            self._attached_part = None
        else:
            # 正常释放
            if parent_handle != -1:
                self.release_to_shuttle(parent_handle, z_offset)
            else:
                self.release(tip_pos)

        # 提起
        self.move_tip_to(above_pos, TIP_DOWN_ORIENT, IK_ANIM_STEPS_FAST)
