"""
仿真场景全局配置与常量

坐标系、尺寸参数、仿真参数等。
所有数值与预构建场景文件 (assembly_line.ttt) 保持同步。
"""

import math

# ============================================================
# 仿真参数
# ============================================================
DT = 0.05                  # 仿真步长 50ms
RENDER_DELAY = 0.01         # GUI 渲染延迟
SHUTTLE_SPEED = 0.3         # 滑块移动速度 m/s（降低以便观察）
INSPECT_PASS_RATE = 0.9     # 质检通过率
INSPECT_STEPS = 20          # 质检等待步数

# IK 动画步数
IK_ANIM_STEPS = 30          # IK 运动动画步数
IK_ANIM_STEPS_FAST = 15     # 快速运动

# ============================================================
# 坐标常量 (与预构建场景同步)
# ============================================================
SEGMENT_SIZE_X = 0.240
SEGMENT_SIZE_Y = 0.240
SEGMENT_HEIGHT = 0.070
SEGMENT_GAP = 0.001

LINE_GRID_COLS = 2
LINE_GRID_ROWS = 8

LINE_WORKSPACE_X = (LINE_GRID_COLS * SEGMENT_SIZE_X
                    + (LINE_GRID_COLS - 1) * SEGMENT_GAP)
LINE_WORKSPACE_Y = (LINE_GRID_ROWS * SEGMENT_SIZE_Y
                    + (LINE_GRID_ROWS - 1) * SEGMENT_GAP)

LINE_GAP = 0.18
LINE_SPACING_X = LINE_WORKSPACE_X + LINE_GAP

LINE_A_CENTER_X = -LINE_SPACING_X / 2
LINE_B_CENTER_X = LINE_SPACING_X / 2

BELT_SPACING = SEGMENT_SIZE_Y + SEGMENT_GAP

Y_PUT    =  LINE_WORKSPACE_Y / 2 - 0.5 * BELT_SPACING
Y_ASSEM  =  LINE_WORKSPACE_Y / 2 - 4.5 * BELT_SPACING
Y_CAMERA = -LINE_WORKSPACE_Y / 2 + 1.5 * BELT_SPACING
Y_POLISH = -LINE_WORKSPACE_Y / 2 + 0.5 * BELT_SPACING

# 工件尺寸
PART_BOTTOM_SIZE = [0.025, 0.035, 0.010]
PART_TOP_SIZE = [0.020, 0.028, 0.008]

# ============================================================
# 滑块物理尺寸
# ============================================================
SHUTTLE_SIZE_X = 0.160             # 滑块宽 160mm
SHUTTLE_SIZE_Y = 0.160             # 滑块长 160mm
SHUTTLE_SAFE_MARGIN = 0.02         # 安全边距 20mm

# 机械臂底座近似尺寸
ROBOT_BASE_SIZE = 0.10             # 底座近似直径 100mm

# 机械臂臂展
ARM_REACH = 0.250  # Mirobot 臂展 250mm
