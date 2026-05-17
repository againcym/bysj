"""
Mirobot 数值逆运动学 (IK) 解算器 v3

改进：使用 getObjectPosition + getObjectQuaternion 构建变换矩阵
（避免 getObjectMatrix 的格式歧义问题）

运动链缓存 + 纯 Python 正运动学：
  - 初始化时从 CoppeliaSim 读取运动链几何（一次性）
  - FK/Jacobian 完全在 numpy 中计算，IK 迭代零 ZMQ 调用
  - 仅最终设置关节角度时使用 ZMQ

注：如果你们觉得麻烦，可以去掉这个模块换成sim内置的ik求解器。

使用方式：
    ik = MirobotIK(sim, 'Robot_Put_A', dt=0.05)
    angles = ik.solve([0.1, 0.2, 0.15])
    ik.move_tip_to([0.1, 0.2, 0.15], anim_steps=30)
"""

import math
import time
import numpy as np

DEFAULT_RENDER_DELAY = 0.01


class MirobotIK:
    """
    数值 IK 解算器 v3

    运动链模型：
      T_tip = T_base · L_0 · Rz(θ₀) · L_1 · Rz(θ₁) · … · L_tip
    """

    MAX_ITERATIONS = 500
    POSITION_TOL = 0.001       # 1mm
    DAMPING = 0.005
    STEP_ALPHA = 1.0

    # 关节限位 (rad) — 宽松设置确保 IK 可达
    JOINT_LIMITS = [
        (-math.pi,     math.pi),       # J1: ±180°
        (-2.35,        2.35),          # J2: ±135°
        (-2.35,        2.35),          # J3: ±135°
        (-math.pi,     math.pi),       # J4: ±180°
        (-2.35,        2.35),          # J5: ±135°
        (-math.pi,     math.pi),       # J6: ±180°
    ]

    def __init__(self, sim, robot_name: str, dt: float = 0.05,
                 render_delay: float = DEFAULT_RENDER_DELAY):
        self.sim = sim
        self.robot_name = robot_name
        self.dt = dt
        self.render_delay = render_delay

        self.handle = sim.getObject(f'/{robot_name}')
        self.joints = sim.getObjectsInTree(
            self.handle, sim.object_joint_type, 0)
        self.n_joints = len(self.joints)
        self.tip_handle = self._find_tip()

        self._home_angles = self.get_joint_positions()

        # 缓存运动链
        self._cache_kinematics()

        # 验证
        fk_pos = self.fk(self._home_angles)
        actual_pos = np.array(sim.getObjectPosition(self.tip_handle, -1))
        fk_err = np.linalg.norm(fk_pos - actual_pos)
        status = '[PASS]' if fk_err < 0.005 else '[WARN]'
        print(f'  MirobotIK [{robot_name}]: {self.n_joints}J, '
              f'FK误差={fk_err*1000:.2f}mm {status}')

    # ----------------------------------------------------------
    # 初始化
    # ----------------------------------------------------------

    def _find_tip(self) -> int:
        all_objs = self.sim.getObjectsInTree(
            self.handle, self.sim.handle_all, 0)
        for obj in reversed(all_objs):
            try:
                alias = self.sim.getObjectAlias(obj, 0).lower()
                if 'tip' in alias or 'connection' in alias:
                    return obj
            except Exception:
                pass
        for obj in reversed(all_objs):
            try:
                if self.sim.getObjectType(obj) == self.sim.object_dummy_type:
                    return obj
            except Exception:
                pass
        return self.joints[-1] if self.joints else self.handle

    @staticmethod
    def _quat_to_rot(q):
        """四元数 [x, y, z, w] → 3×3 旋转矩阵"""
        x, y, z, w = q[0], q[1], q[2], q[3]
        return np.array([
            [1-2*(y*y+z*z), 2*(x*y-w*z),   2*(x*z+w*y)  ],
            [2*(x*y+w*z),   1-2*(x*x+z*z), 2*(y*z-w*x)  ],
            [2*(x*z-w*y),   2*(y*z+w*x),   1-2*(x*x+y*y)],
        ])

    def _get_transform(self, handle: int) -> np.ndarray:
        """
        从 CoppeliaSim 读取 4×4 齐次变换矩阵

        使用 getObjectPosition + getObjectQuaternion，
        避免 getObjectMatrix 的格式歧义。
        """
        pos = self.sim.getObjectPosition(handle, -1)
        quat = self.sim.getObjectQuaternion(handle, -1)
        R = self._quat_to_rot(quat)
        T = np.eye(4)
        T[:3, :3] = R
        T[:3, 3] = pos
        return T

    def _cache_kinematics(self):
        """从 CoppeliaSim 一次性读取运动链变换"""
        saved = self.get_joint_positions()

        # 设置所有关节到 0
        for j in self.joints:
            self.sim.setJointPosition(j, 0.0)
        self.sim.step()
        if self.render_delay > 0:
            time.sleep(self.render_delay)

        # 读取基座世界变换
        self._T_base = self._get_transform(self.handle)

        # 读取各 link 变换
        self._link_transforms = []

        # 读取所有关节和 tip 的世界变换
        T_frames = []
        for j in self.joints:
            T_frames.append(self._get_transform(j))
        T_tip_world = self._get_transform(self.tip_handle)

        # Base → Joint 0
        self._link_transforms.append(
            np.linalg.inv(self._T_base) @ T_frames[0])

        # Joint i → Joint i+1
        for i in range(1, self.n_joints):
            self._link_transforms.append(
                np.linalg.inv(T_frames[i-1]) @ T_frames[i])

        # Last Joint → Tip
        self._tip_transform = np.linalg.inv(T_frames[-1]) @ T_tip_world

        # 恢复关节角度
        self.set_joint_positions(saved)
        self.sim.step()
        if self.render_delay > 0:
            time.sleep(self.render_delay)

    # ----------------------------------------------------------
    # FK — 纯 Python
    # ----------------------------------------------------------

    @staticmethod
    def _rot_z(angle: float) -> np.ndarray:
        c, s = math.cos(angle), math.sin(angle)
        return np.array([
            [c, -s, 0, 0],
            [s,  c, 0, 0],
            [0,  0, 1, 0],
            [0,  0, 0, 1],
        ])

    def fk(self, angles) -> np.ndarray:
        """正运动学 — 返回 tip 世界坐标 [x, y, z]"""
        T = self._T_base.copy()
        for i in range(self.n_joints):
            T = T @ self._link_transforms[i] @ self._rot_z(angles[i])
        T = T @ self._tip_transform
        return T[:3, 3]

    def fk_full(self, angles) -> np.ndarray:
        """返回完整 4×4 变换"""
        T = self._T_base.copy()
        for i in range(self.n_joints):
            T = T @ self._link_transforms[i] @ self._rot_z(angles[i])
        T = T @ self._tip_transform
        return T

    # ----------------------------------------------------------
    # Jacobian — 纯 Python
    # ----------------------------------------------------------

    def _jacobian(self, angles) -> np.ndarray:
        delta = 1e-4
        pos0 = self.fk(angles)
        J = np.zeros((3, self.n_joints))
        for i in range(self.n_joints):
            perturbed = list(angles)
            perturbed[i] += delta
            J[:, i] = (self.fk(perturbed) - pos0) / delta
        return J

    # ----------------------------------------------------------
    # IK 求解 — 纯 Python
    # ----------------------------------------------------------

    def solve(self, target_pos: list,
              initial_angles: list = None,
              max_iter: int = None,
              tolerance: float = None) -> list:
        """逆运动学求解 (DLS)"""
        if max_iter is None:
            max_iter = self.MAX_ITERATIONS
        if tolerance is None:
            tolerance = self.POSITION_TOL

        target = np.array(target_pos, dtype=float)

        if initial_angles is not None:
            angles = np.array(initial_angles, dtype=float)
        else:
            angles = np.array(self.get_joint_positions(), dtype=float)

        # J1 初始猜测策略：
        # （指定了 initial_angles 时保留外部提供的值，用于多角度重试）
        if initial_angles is None:
            # 双尝试策略：先用当前角度求解，再用 j1_guess 求解
            # 取残差更小的结果。这避免了 j1_guess 在 ±π 边界发散。
            current_angles = angles.copy()

            base_pos = self._T_base[:3, 3]
            dx = target[0] - base_pos[0]
            dy = target[1] - base_pos[1]
            target_angle_world = math.atan2(dy, dx)
            base_yaw = math.atan2(self._T_base[1, 0], self._T_base[0, 0])
            j1_guess = target_angle_world - base_yaw
            j1_guess = (j1_guess + math.pi) % (2 * math.pi) - math.pi

            # 用当前角度先算一次 FK 误差
            current_fk_err = np.linalg.norm(self.fk(current_angles) - target)

            # 用 j1_guess 先算一次 FK 误差
            guess_angles = current_angles.copy()
            guess_angles[0] = j1_guess
            guess_fk_err = np.linalg.norm(self.fk(guess_angles) - target)

            # 选更接近目标的作为初始角度
            if guess_fk_err < current_fk_err * 0.5:
                angles[0] = j1_guess

        best_angles = angles.copy()
        best_error = float('inf')

        for iteration in range(max_iter):
            pos = self.fk(angles)
            error = target - pos
            error_norm = np.linalg.norm(error)

            if error_norm < best_error:
                best_error = error_norm
                best_angles = angles.copy()

            if error_norm < tolerance:
                return angles.tolist()

            J = self._jacobian(angles)
            damping = self.DAMPING * max(1.0, error_norm * 10)
            JJT = J @ J.T + (damping ** 2) * np.eye(3)

            try:
                d_theta = J.T @ np.linalg.solve(JJT, error)
            except np.linalg.LinAlgError:
                d_theta = np.linalg.pinv(J) @ error

            # 步长限制
            max_step = 0.3
            step_norm = np.linalg.norm(d_theta)
            if step_norm > max_step:
                d_theta *= max_step / step_norm

            angles = angles + self.STEP_ALPHA * d_theta
            angles = self._clamp_joints(angles)

        if best_error > 0.01:
            print(f'    [WARN] IK [{self.robot_name}]: '
                  f'残差={best_error*1000:.1f}mm ({max_iter}次)')
        return best_angles.tolist()

    def _clamp_joints(self, angles: np.ndarray) -> np.ndarray:
        for i in range(min(len(angles), len(self.JOINT_LIMITS))):
            lo, hi = self.JOINT_LIMITS[i]
            angles[i] = np.clip(angles[i], lo, hi)
        return angles

    # ----------------------------------------------------------
    # 关节读写
    # ----------------------------------------------------------

    def get_joint_positions(self) -> list:
        return [self.sim.getJointPosition(j) for j in self.joints]

    def set_joint_positions(self, angles: list):
        for j, a in zip(self.joints, angles):
            self.sim.setJointPosition(j, float(a))

    # ----------------------------------------------------------
    # 运动接口
    # ----------------------------------------------------------

    def animate_to_angles(self, target_angles: list,
                          anim_steps: int = 30):
        """直接动画到指定关节角度（不重新求解 IK）"""
        current = np.array(self.get_joint_positions())
        target_np = np.array(target_angles)

        for step in range(1, anim_steps + 1):
            t = step / anim_steps
            t_smooth = t * t * (3 - 2 * t)
            interp = current + (target_np - current) * t_smooth
            self.set_joint_positions(interp.tolist())
            self.sim.step()
            if self.render_delay > 0:
                time.sleep(self.render_delay)

    def move_tip_to(self, target_pos: list,
                    target_orient=None,
                    anim_steps: int = 30):
        """IK 解算 + 动画"""
        target_angles = self.solve(target_pos)
        self.animate_to_angles(target_angles, anim_steps)

    def move_to_home(self, anim_steps: int = 25):
        current = np.array(self.get_joint_positions())
        target = np.array(self._home_angles)

        for step in range(1, anim_steps + 1):
            t = step / anim_steps
            t_smooth = t * t * (3 - 2 * t)
            interp = current + (target - current) * t_smooth
            self.set_joint_positions(interp.tolist())
            self.sim.step()
            if self.render_delay > 0:
                time.sleep(self.render_delay)
