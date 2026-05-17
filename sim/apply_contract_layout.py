# -*- coding: utf-8 -*-
"""
Rebuild the visible CoppeliaSim scene layout to match the 5-1 contract diagram.

This script edits the currently open scene:
- removes previous helper geometry, including the temporary white WS plates
- keeps the original dark platform/floor objects in the scene
- removes extra shuttles and keeps only Shuttle_A1 as Mover1
- keeps one camera station only
- repositions ARM1..ARM7 according to the diagram
- replaces the original 2x8 line platforms with one 4x4 central platform
- adds Belt1, Belt2, Shelf1..Shelf4 based on an OutputBin-style model,
  and TS_ARM_05

Run with CoppeliaSim open and simulation stopped:
    python apply_contract_layout.py --save-scene scenes/assembly_line_5_1.ttt
"""

from __future__ import annotations

import argparse
import math
import time
from pathlib import Path

from contract_runner import ARM_MAP, DEFAULT_SCENE_PATH, MOVER_NAME, WORKSTATIONS


LAYOUT_ROOT = "ContractLayout_5_1"

OBJECTS_TO_REMOVE = [
    "ContractLayout",
    "ContractLayout_5_1",
    "WorkFloor_WS1_Mover",
    "WorkFloor_WS2_WS3",
    "WorkFloor_WS4",
    "WorkFloor_WS5_WS6",
    "LineA_Platform",
    "LineB_Platform",
    "Shuttle_A2",
    "Shuttle_A3",
    "Shuttle_B1",
    "Shuttle_B2",
    "Shuttle_B3",
    "CameraStation_B",
    "Camera_B",
    "CameraPole_B",
    "CameraArm_B",
    "Assem2_Base",
    "OutputBin_A",
    "OutputBin_B",
    "Shelf_A",
    "Shelf_B",
    "Shelf1",
    "Shelf2",
    "Shelf3",
    "Shelf4",
    "Shelf5",
    "Shelf6",
    "Part_Bottom_A0",
    "Part_Top_A0",
    "Part_Bottom_A1",
    "Part_Top_A1",
    "Part_Bottom_A2",
    "Part_Top_A2",
    "Part_Bottom_B0",
    "Part_Top_B0",
    "Part_Bottom_B1",
    "Part_Top_B1",
    "Part_Bottom_B2",
    "Part_Top_B2",
]

ARM_POSES = {
    "ARM1": (-0.856, 0.598, 0.045, -math.pi / 2),
    "ARM2": (-0.576, 0.238, 0.045, 0.00),
    "ARM3": (0, 0.576, 0.045, -math.pi / 2),
    "ARM4": (0.238, 0.576, 0.045, -math.pi / 2),
    "ARM5": (0.576, 0, 0.045, math.pi),
    "ARM6": (-0.238, -0.576, 0.045, math.pi / 2),
    "ARM7": (-0.598, -0.856, 0.045, 0.00),
}

MOVER_HOME = (-0.357, 0, 0.080, 0.00)

PLATFORM_TILE_SIZE = 0.238
PLATFORM_PITCH = 0.241
PLATFORM_Z = 0.035
PLATFORM_COLOR = (0.19, 0.20, 0.20)
PLATFORM_ALT_COLOR = (0.23, 0.24, 0.24)

STATIC_VISUALS = {
    "Belt1": {
        "pos": (-0.716, 0.418, 0.03),
        "size": (0.48, 0.16, 0.045),
        "color": (0.03, 0.20, 0.05),
    },
    "Belt1_surface": {
        "pos": (-0.716, 0.418, 0.06),
        "size": (0.36, 0.09, 0.012),
        "color": (0.86, 0.90, 0.84),
    },
    "Belt1_left_roller": {
        "pos": (-0.926, 0.418, 0.067),
        "size": (0.045, 0.18, 0.045),
        "color": (0.01, 0.06, 0.02),
    },
    "Belt1_right_roller": {
        "pos": (-0.506, 0.418, 0.067),
        "size": (0.045, 0.18, 0.045),
        "color": (0.01, 0.06, 0.02),
    },
    "Belt2": {
        "pos": (-0.418, -0.716, 0.03),
        "size": (0.16, 0.44, 0.045),
        "color": (0.03, 0.20, 0.05),
    },
    "Belt2_surface": {
        "pos": (-0.418, -0.716, 0.06),
        "size": (0.09, 0.34, 0.012),
        "color": (0.86, 0.90, 0.84),
    },
    "Belt2_top_roller": {
        "pos": (-0.418, -0.516, 0.067),
        "size": (0.18, 0.045, 0.045),
        "color": (0.01, 0.06, 0.02),
    },
    "Belt2_bottom_roller": {
        "pos": (-0.418, -0.916, 0.067),
        "size": (0.18, 0.045, 0.045),
        "color": (0.01, 0.06, 0.02),
    },
}

SHELF_POSES = {
    "Shelf1": (-0.666, 0.618, 0.0, math.pi / 2),
    "Shelf2": (-1.046, 0.618, 0.0, math.pi / 2),
    "Shelf3": (-0.618, -0.666, 0.0, 0.0),
    "Shelf4": (-0.618, -1.046, 0.0, 0.0),
}

CAMERA_POSES = {
    "CameraStation_A": (0.238, -0.5, 0.1),
    "CameraPole_A": (0.238, -0.5, 0.1),
    "CameraArm_A": (0.238, -0.5, 0.1),
    "Camera_A": (0.238, -0.357, 0.24),
}

TS_SLOTS = [
    # ARM5 faces negative X. Slots 1/2 are on ARM5's right side (+Y),
    # slots 3/4/5 are on its left side (-Y). Keep this list aligned with
    # contract_runner.py::TOOL_RACK_POINTS.
    (0.636, 0.150),
    (0.556, 0.150),
    (0.676, -0.150),
    (0.596, -0.150),
    (0.516, -0.150),
]

TS_SLOT_SIZE = (0.08, 0.08, 0.035)
TS_SLOT_BORDER_THICKNESS = 0.006
TS_SLOT_BORDER_HEIGHT = 0.006
TS_SLOT_LABEL_HEIGHT = 0.006

DIGIT_SEGMENTS = {
    1: ("b", "c"),
    2: ("a", "b", "g", "e", "d"),
    3: ("a", "b", "g", "c", "d"),
    4: ("f", "g", "b", "c"),
    5: ("a", "f", "g", "c", "d"),
}

CAR_MODEL_ALIASES = {
    "Part_Car_Body": [
        "Part_Car_Body",
        "car_body",
        "Car_Body",
        "新_车_身",
        "新 车 身",
    ],
    "Part_Car_Battery": [
        "Part_Car_Battery",
        "car_battery",
        "Car_Battery",
    ],
    "Part_Car_Chassis": [
        "Part_Car_Chassis",
        "car_chassis",
        "Car_Chassis",
    ],
    "Part_Phone_Back": [
        "Part_Phone_Back",
        "phone_back",
        "Phone_Back",
    ],
    "Part_Phone_Battery": [
        "Part_Phone_Battery",
        "phone_battery",
        "Phone_Battery",
    ],
    "Part_Phone_Screen": [
        "Part_Phone_Screen",
        "phone_screen",
        "Phone_Screen",
    ],
}

CAR_MODEL_POSES = {
    # alias in CAR_MODEL_ALIASES: (x, y, z, roll, pitch, yaw)
    # These three points are inside Shelf1 and arranged in parallel.
    "Part_Car_Body": (-0.61, 0.73, 0.030, 0.0, 0.0, 1.05*math.pi),
    "Part_Car_Battery": (-0.68, 0.613, 0.030, 0.0, 0.0, 0.0),
    "Part_Car_Chassis": (-0.72, 0.558, 0.030, math.pi / 2, 0.0, 0),
    # These three points are inside Shelf2 and arranged in parallel.
    "Part_Phone_Back": (-1.046, 0.561, 0.030, 0.0, math.pi, math.pi / 2),
    "Part_Phone_Battery": (-1.046, 0.618, 0.030, 0.0, 0.0, math.pi / 2),
    "Part_Phone_Screen": (-1.046, 0.675, 0.030, 0.0, math.pi, math.pi / 2),
}


def ensure_stopped(sim) -> None:
    if sim.getSimulationState() == sim.simulation_stopped:
        return
    print("[INFO] Stopping simulation before editing layout")
    sim.stopSimulation()
    for _ in range(80):
        if sim.getSimulationState() == sim.simulation_stopped:
            return
        time.sleep(0.05)


def get_constant(sim, name: str, fallback: int) -> int:
    value = getattr(sim, name, None)
    return fallback if value is None else value


def remove_object_tree_by_alias(sim, alias: str) -> None:
    try:
        root = sim.getObject(f"/{alias}")
    except Exception:
        return

    try:
        handles = sim.getObjectsInTree(root, sim.handle_all, 0)
    except Exception:
        handles = [root]

    for handle in reversed(handles):
        try:
            sim.removeObject(handle)
        except Exception:
            pass
    print(f"[REMOVE] /{alias}")


def remove_old_layout(sim) -> None:
    for alias in OBJECTS_TO_REMOVE:
        if alias == MOVER_NAME:
            continue
        remove_object_tree_by_alias(sim, alias)


def create_root(sim) -> int:
    root = sim.createDummy(0.01)
    sim.setObjectAlias(root, LAYOUT_ROOT)
    return root


def set_color(sim, handle: int, color) -> None:
    component = get_constant(sim, "colorcomponent_ambient_diffuse", 0)
    try:
        sim.setShapeColor(handle, None, component, list(color))
    except Exception:
        pass


def create_cuboid(sim, alias: str, pos, size, color, parent: int) -> int:
    cuboid_type = get_constant(sim, "primitiveshape_cuboid", 2)
    handle = sim.createPrimitiveShape(cuboid_type, list(size), 0)
    sim.setObjectAlias(handle, alias)
    sim.setObjectPosition(handle, -1, list(pos))
    sim.setObjectParent(handle, parent, True)
    set_color(sim, handle, color)
    return handle


def reposition_object(sim, alias: str, pose) -> None:
    x, y, z, yaw = pose
    try:
        handle = sim.getObject(f"/{alias}")
    except Exception:
        print(f"[WARN] Missing object /{alias}")
        return
    sim.setObjectPosition(handle, -1, [x, y, z])
    sim.setObjectOrientation(handle, -1, [0.0, 0.0, yaw])
    print(f"[MOVE] /{alias}: x={x:.3f}, y={y:.3f}, yaw={math.degrees(yaw):.0f}deg")


def reposition_existing_models(sim) -> None:
    for contract_name, pose in ARM_POSES.items():
        scene_alias = ARM_MAP[contract_name]
        reposition_object(sim, scene_alias, pose)

    reposition_object(sim, MOVER_NAME, MOVER_HOME)

    # Keep one camera station only and place it near WS5.
    for alias in ["CameraStation_A", "Camera_A", "CameraPole_A", "CameraArm_A"]:
        try:
            handle = sim.getObject(f"/{alias}")
        except Exception:
            continue
        pos = CAMERA_POSES[alias]
        sim.setObjectPosition(handle, -1, pos)


def add_outputbin_shelves(sim, parent: int) -> None:
    """Create all shelves with the same OutputBin-style geometry."""
    for alias, pose in SHELF_POSES.items():
        x, y, z, yaw = pose
        create_outputbin_fallback(sim, alias, (x, y, z), parent)
        try:
            handle = sim.getObject(f"/{alias}")
            sim.setObjectOrientation(handle, -1, [0.0, 0.0, yaw])
        except Exception:
            pass


def create_outputbin_fallback(sim, alias: str, pos, parent: int) -> None:
    x, y, z = pos
    base = create_cuboid(
        sim,
        alias,
        (x, y, z),
        (0.24, 0.18, 0.035),
        (0.58, 0.58, 0.62),
        parent,
    )
    create_cuboid(
        sim,
        f"{alias}_rim_front",
        (x, y - 0.095, z + 0.030),
        (0.25, 0.018, 0.050),
        (0.35, 0.35, 0.40),
        base,
    )
    create_cuboid(
        sim,
        f"{alias}_rim_back",
        (x, y + 0.095, z + 0.030),
        (0.25, 0.018, 0.050),
        (0.35, 0.35, 0.40),
        base,
    )
    create_cuboid(
        sim,
        f"{alias}_rim_left",
        (x - 0.125, y, z + 0.030),
        (0.018, 0.18, 0.050),
        (0.35, 0.35, 0.40),
        base,
    )
    create_cuboid(
        sim,
        f"{alias}_rim_right",
        (x + 0.125, y, z + 0.030),
        (0.018, 0.18, 0.050),
        (0.35, 0.35, 0.40),
        base,
    )


def add_central_platform(sim, parent: int) -> None:
    start = -1.5 * PLATFORM_PITCH
    for row in range(4):
        for col in range(4):
            x = start + col * PLATFORM_PITCH
            y = start + row * PLATFORM_PITCH
            color = PLATFORM_COLOR if (row + col) % 2 == 0 else PLATFORM_ALT_COLOR
            create_cuboid(
                sim,
                f"CentralPlatform_{row}_{col}",
                (x, y, PLATFORM_Z),
                (PLATFORM_TILE_SIZE, PLATFORM_TILE_SIZE, 0.070),
                color,
                parent,
            )
    print("[ADD] CentralPlatform 4x4")


def print_workstation_points() -> None:
    for station, (label, x, y) in WORKSTATIONS.items():
        print(f"[WS] {station}: {label} at x={x:.3f}, y={y:.3f}")


def add_ts_slot_border(sim, alias: str, x: float, y: float, parent: int) -> None:
    width, depth, height = TS_SLOT_SIZE
    t = TS_SLOT_BORDER_THICKNESS
    z = height / 2 + TS_SLOT_BORDER_HEIGHT / 2 + 0.001

    create_cuboid(
        sim,
        f"{alias}_border_top",
        (x, y + depth / 2, z),
        (width + t, t, TS_SLOT_BORDER_HEIGHT),
        (0.0, 0.0, 0.0),
        parent,
    )
    create_cuboid(
        sim,
        f"{alias}_border_bottom",
        (x, y - depth / 2, z),
        (width + t, t, TS_SLOT_BORDER_HEIGHT),
        (0.0, 0.0, 0.0),
        parent,
    )
    create_cuboid(
        sim,
        f"{alias}_border_left",
        (x - width / 2, y, z),
        (t, depth + t, TS_SLOT_BORDER_HEIGHT),
        (0.0, 0.0, 0.0),
        parent,
    )
    create_cuboid(
        sim,
        f"{alias}_border_right",
        (x + width / 2, y, z),
        (t, depth + t, TS_SLOT_BORDER_HEIGHT),
        (0.0, 0.0, 0.0),
        parent,
    )


def add_ts_slot_number(sim, index: int, x: float, y: float, parent: int) -> None:
    z = TS_SLOT_SIZE[2] / 2 + TS_SLOT_LABEL_HEIGHT / 2 + 0.006
    dx = 0.012
    dy = 0.017
    horizontal_size = (0.026, 0.004, TS_SLOT_LABEL_HEIGHT)
    vertical_size = (0.004, 0.020, TS_SLOT_LABEL_HEIGHT)
    segment_specs = {
        "a": ((x, y + dy, z), horizontal_size),
        "b": ((x + dx, y + dy / 2, z), vertical_size),
        "c": ((x + dx, y - dy / 2, z), vertical_size),
        "d": ((x, y - dy, z), horizontal_size),
        "e": ((x - dx, y - dy / 2, z), vertical_size),
        "f": ((x - dx, y + dy / 2, z), vertical_size),
        "g": ((x, y, z), horizontal_size),
    }

    for segment in DIGIT_SEGMENTS[index]:
        pos, size = segment_specs[segment]
        create_cuboid(
            sim,
            f"TS_ARM_05_slot_{index}_label_{segment}",
            pos,
            size,
            (0.0, 0.0, 0.0),
            parent,
        )


def add_static_visuals(sim, parent: int) -> None:
    for alias, cfg in STATIC_VISUALS.items():
        create_cuboid(sim, alias, cfg["pos"], cfg["size"], cfg["color"], parent)
        x, y, _ = cfg["pos"]
        print(f"[ADD] {alias}: x={x:.3f}, y={y:.3f}")

    for index, (x, y) in enumerate(TS_SLOTS, start=1):
        alias = f"TS_ARM_05_slot_{index}"
        create_cuboid(
            sim,
            alias,
            (x, y, 0.0),
            TS_SLOT_SIZE,
            (0.45, 0.20, 0.04),
            parent,
        )
        add_ts_slot_border(sim, alias, x, y, parent)
        add_ts_slot_number(sim, index, x, y, parent)
    print("[ADD] TS_ARM_05 slots")


def find_object_by_aliases(sim, aliases) -> int | None:
    for alias in aliases:
        try:
            return sim.getObject(f"/{alias}")
        except Exception:
            pass
    return None


def unpack_car_pose(pose):
    if len(pose) == 4:
        x, y, z, yaw = pose
        return x, y, z, 0.0, 0.0, yaw
    if len(pose) == 6:
        return pose
    raise ValueError("CAR_MODEL_POSES entries must be (x, y, z, yaw) or (x, y, z, roll, pitch, yaw)")


def reposition_car_models(sim) -> None:
    for canonical_alias, pose in CAR_MODEL_POSES.items():
        handle = find_object_by_aliases(
            sim, CAR_MODEL_ALIASES.get(canonical_alias, [canonical_alias])
        )
        if handle is None:
            print(
                f"[WARN] Missing car model for {canonical_alias}. "
                f"Rename the imported STL to /{canonical_alias}."
            )
            continue

        x, y, z, roll, pitch, yaw = unpack_car_pose(pose)
        sim.setObjectAlias(handle, canonical_alias)
        sim.setObjectParent(handle, -1, True)
        sim.setObjectPosition(handle, -1, [x, y, z])
        sim.setObjectOrientation(handle, -1, [roll, pitch, yaw])
        print(
            f"[MOVE] /{canonical_alias}: "
            f"x={x:.3f}, y={y:.3f}, z={z:.3f}, "
            f"roll={math.degrees(roll):.0f}deg, "
            f"pitch={math.degrees(pitch):.0f}deg, "
            f"yaw={math.degrees(yaw):.0f}deg"
        )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rebuild scene layout according to the 5-1 contract diagram."
    )
    parser.add_argument(
        "--load-scene",
        action="store_true",
        help="Load assembly_line.ttt before rebuilding the layout.",
    )
    parser.add_argument(
        "--scene",
        type=Path,
        default=DEFAULT_SCENE_PATH,
        help="Scene path used with --load-scene.",
    )
    parser.add_argument(
        "--save-scene",
        type=Path,
        default=Path("scenes") / "assembly_line_5_1.ttt",
        help="Path to save the rebuilt scene.",
    )
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()

    from coppeliasim_zmqremoteapi_client import RemoteAPIClient

    client = RemoteAPIClient()
    sim = client.require("sim")

    ensure_stopped(sim)

    if args.load_scene:
        if not args.scene.exists():
            raise FileNotFoundError(f"Scene file not found: {args.scene}")
        print(f"[INFO] Loading scene: {args.scene}")
        sim.loadScene(str(args.scene).replace("\\", "/"))

    remove_old_layout(sim)
    root = create_root(sim)

    reposition_existing_models(sim)
    reposition_car_models(sim)
    add_central_platform(sim, root)
    print_workstation_points()
    add_static_visuals(sim, root)
    add_outputbin_shelves(sim, root)

    if args.save_scene is not None:
        print(f"[INFO] Saving scene: {args.save_scene}")
        sim.saveScene(str(args.save_scene.resolve()).replace("\\", "/"))

    print("[DONE] 5-1 layout rebuilt. Keep /Shuttle_A1 as Mover1.")


if __name__ == "__main__":
    main()
