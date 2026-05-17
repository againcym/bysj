"""
ACOPOS 6D 滑块运动控制模块

提供单滑块的 6DOF 运动控制功能：
- 平面移动 (X/Y)
- Z 轴旋转
- 悬浮高度调节
- 梯形速度规划

使用方式：
    from shuttle_controller import ShuttleController
    ctrl = ShuttleController(sim, shuttle_handle, name='Shuttle_1')
    ctrl.move_to(0.3, 0.4, speed=1.0)
    ctrl.rotate_to(90)
    ctrl.set_levitation_height(0.003)
"""

import math
import time

# 默认渲染延迟（秒），让 CoppeliaSim GUI 有时间刷新画面
DEFAULT_RENDER_DELAY = 0.01


class ShuttleController:
    """ACOPOS 6D 滑块控制器（运动学模式）"""

    # 默认运动参数（基于ACOPOS 6D 规格调整）
    DEFAULT_MAX_SPEED = 2.0       # m/s
    DEFAULT_MAX_ACCEL = 20.0      # m/s²
    DEFAULT_LEVITATION = 0.002    # 2mm 默认悬浮高度（相对电机段顶面）
    DEFAULT_ROT_SPEED = 180.0     # deg/s 旋转速度

    def __init__(self, sim, handle, name='Shuttle', dt=0.05, render_delay=None):
        """
        Args:
            sim: CoppeliaSim sim 对象 (通过 RemoteAPIClient 获取)
            handle: 滑块在 CoppeliaSim 中的 object handle
            name: 滑块名称标识
            dt: 仿真步长 (秒), 默认 50ms（兼顾流畅性和性能）
            render_delay: 每步后的真实时间延迟 (秒), 让 GUI 有时间渲染
        """
        self.sim = sim
        self.handle = handle
        self.name = name
        self.dt = dt
        self.render_delay = render_delay if render_delay is not None else DEFAULT_RENDER_DELAY
        self.max_speed = self.DEFAULT_MAX_SPEED
        self.max_accel = self.DEFAULT_MAX_ACCEL
        self.levitation_height = self.DEFAULT_LEVITATION
        self.rot_speed = self.DEFAULT_ROT_SPEED
        self._collision_checker = None  # callback: (x, y) -> bool (True=安全)

    def set_collision_checker(self, checker):
        """设置碰撞检测回调。checker(x, y) 返回 True 表示安全。"""
        self._collision_checker = checker

    def _step_and_render(self):
        """执行一步仿真并等待 GUI 渲染"""
        self.sim.step()
        if self.render_delay > 0:
            time.sleep(self.render_delay)

    def get_pose(self):
        """获取当前 6DOF 位姿

        Returns:
            dict: {'x', 'y', 'z', 'alpha', 'beta', 'gamma'} (位置单位 m, 角度单位 deg)
        """
        pos = self.sim.getObjectPosition(self.handle, -1)
        euler = self.sim.getObjectOrientation(self.handle, -1)
        return {
            'x': pos[0],
            'y': pos[1],
            'z': pos[2],
            'alpha': math.degrees(euler[0]),
            'beta': math.degrees(euler[1]),
            'gamma': math.degrees(euler[2]),
        }

    def _set_pose(self, x, y, z, gamma_rad=None):
        """内部方法：设置位姿（保持当前旋转或指定 Z 轴旋转）"""
        self.sim.setObjectPosition(self.handle, -1, [x, y, z])
        if gamma_rad is not None:
            self.sim.setObjectOrientation(self.handle, -1, [0, 0, gamma_rad])

    def move_to(self, target_x, target_y, speed=None):
        """平面直线移动到目标位置 (梯形速度规划)

        Args:
            target_x: 目标 X 坐标 (m)
            target_y: 目标 Y 坐标 (m)
            speed: 最大速度 (m/s), 默认使用 max_speed
        """
        if speed is None:
            speed = self.max_speed

        pose = self.get_pose()
        start_x, start_y = pose['x'], pose['y']
        z = pose['z']

        dx = target_x - start_x
        dy = target_y - start_y
        dist = math.sqrt(dx * dx + dy * dy)

        if dist < 1e-6:
            return  # 已在目标位置

        # 方向单位向量
        ux = dx / dist
        uy = dy / dist

        # 梯形速度规划
        steps = self._trapezoidal_profile(dist, speed, self.max_accel)

        traveled = 0.0
        last_safe_x, last_safe_y = start_x, start_y
        blocked = False
        for v in steps:
            ds = v * self.dt
            traveled += ds
            if traveled > dist:
                traveled = dist
            x = start_x + ux * traveled
            y = start_y + uy * traveled
            # 底层碰撞拦截（这里你们可以关掉换成仿真器内的物理碰撞）
            if self._collision_checker and not self._collision_checker(x, y):
                print(f'    [WARN] [{self.name}] 拦截 @ '
                      f'({x:.3f},{y:.3f}), 停在 '
                      f'({last_safe_x:.3f},{last_safe_y:.3f})')
                blocked = True
                break
            self._set_pose(x, y, z)
            self._step_and_render()
            last_safe_x, last_safe_y = x, y

        if blocked:
            # 停在最后安全位置
            self._set_pose(last_safe_x, last_safe_y, z)
            self._step_and_render()
            return

        # 确保精确到达
        if self._collision_checker and not self._collision_checker(target_x, target_y):
            print(f'    [WARN] [{self.name}] 目标拦截 @ '
                  f'({target_x:.3f},{target_y:.3f})')
            return
        self._set_pose(target_x, target_y, z)
        self._step_and_render()

    def move_along_path(self, waypoints, speed=None):
        """沿路径点列表移动

        Args:
            waypoints: [(x1,y1), (x2,y2), ...] 路径点列表
            speed: 移动速度 (m/s)
        """
        for wx, wy in waypoints:
            self.move_to(wx, wy, speed)

    def move_arc(self, center_x, center_y, angle_deg, steps_count=100, speed=None):
        """沿圆弧运动

        Args:
            center_x, center_y: 圆弧中心坐标
            angle_deg: 旋转角度（正值=逆时针）
            steps_count: 插值步数
            speed: 未使用，通过 steps_count 控制速度
        """
        pose = self.get_pose()
        sx, sy, z = pose['x'], pose['y'], pose['z']

        # 计算初始半径和角度
        r = math.sqrt((sx - center_x)**2 + (sy - center_y)**2)
        start_angle = math.atan2(sy - center_y, sx - center_x)
        total_angle = math.radians(angle_deg)

        for i in range(1, steps_count + 1):
            frac = i / steps_count
            current_angle = start_angle + total_angle * frac
            x = center_x + r * math.cos(current_angle)
            y = center_y + r * math.sin(current_angle)
            self._set_pose(x, y, z)
            self._step_and_render()

    def rotate_to(self, target_angle_deg, rot_speed=None):
        """绕 Z 轴旋转到指定角度

        Args:
            target_angle_deg: 目标角度（度）
            rot_speed: 旋转速度 (deg/s)
        """
        if rot_speed is None:
            rot_speed = self.rot_speed

        pose = self.get_pose()
        current_gamma = pose['gamma']
        x, y, z = pose['x'], pose['y'], pose['z']

        # 计算角度差（最短路径）
        diff = target_angle_deg - current_gamma
        while diff > 180:
            diff -= 360
        while diff < -180:
            diff += 360

        total_steps = max(1, int(abs(diff) / (rot_speed * self.dt)))

        for i in range(1, total_steps + 1):
            frac = i / total_steps
            angle = current_gamma + diff * frac
            self._set_pose(x, y, z, math.radians(angle))
            self._step_and_render()

        # 确保精确到达
        self._set_pose(x, y, z, math.radians(target_angle_deg))
        self._step_and_render()

    def set_levitation_height(self, height_m, transition_speed=0.01):
        """设置悬浮高度（Z 方向升降）

        通过改变 Z 坐标增量实现悬浮高度变化。
        height_m 表示相对当前位置的目标悬浮增量调整：
        正值上升，负值下降。绝对值被限制在合理范围内。

        Args:
            height_m: 目标悬浮高度变化量 (m), 实际位移被限制在 ±3.5mm
            transition_speed: 升降速度 (m/s)
        """
        # 限制最大位移范围
        max_delta = 0.0035  # ±3.5mm
        height_m = max(-max_delta, min(max_delta, height_m))

        pose = self.get_pose()
        x, y = pose['x'], pose['y']
        current_z = pose['z']
        target_z = current_z + height_m

        dz = target_z - current_z
        if abs(dz) < 1e-6:
            return

        total_steps = max(1, int(abs(dz) / (transition_speed * self.dt)))

        for i in range(1, total_steps + 1):
            frac = i / total_steps
            z = current_z + dz * frac
            self._set_pose(x, y, z)
            self._step_and_render()

        self._set_pose(x, y, target_z)
        self._step_and_render()
        self.levitation_height += height_m

    def _trapezoidal_profile(self, distance, max_vel, max_acc):
        """梯形速度规划

        Returns:
            list[float]: 每步的速度序列
        """
        # 计算加速/减速所需距离
        t_acc = max_vel / max_acc
        d_acc = 0.5 * max_acc * t_acc * t_acc

        if 2 * d_acc >= distance:
            # 三角形速度曲线（达不到最大速度）
            t_acc = math.sqrt(distance / max_acc)
            peak_vel = max_acc * t_acc
            d_acc = distance / 2.0
            d_cruise = 0.0
        else:
            peak_vel = max_vel
            d_cruise = distance - 2 * d_acc

        velocities = []
        traveled = 0.0

        while traveled < distance - 1e-9:
            if traveled < d_acc:
                # 加速阶段
                v = math.sqrt(2 * max_acc * max(traveled, 0))
                v = min(v, peak_vel)
                if v < 0.01:
                    v = 0.01  # 防止速度过小导致步数爆炸
            elif traveled < d_acc + d_cruise:
                # 匀速阶段
                v = peak_vel
            else:
                # 减速阶段
                remaining = distance - traveled
                v = math.sqrt(2 * max_acc * max(remaining, 0))
                v = min(v, peak_vel)
                if v < 0.01:
                    v = 0.01

            velocities.append(v)
            traveled += v * self.dt

        return velocities
