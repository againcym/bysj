# -*- coding: utf-8 -*-
"""
Run output_contract_llmmain.xml against the current CoppeliaSim scene.

This is an adapter layer, not a full contract verifier yet:
1. Parse Operation nodes from the contract XML.
2. Map contract names such as ARM1 / Mover to existing scene objects.
3. Execute visible demo motions with the current RobotArmController and
   ShuttleController.

Keep the CoppeliaSim scene layout unchanged. The contract names are translated
to the original scene object names through ARM_MAP / MOVER_NAME below.
"""

from __future__ import annotations

import argparse
import json
import math
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from conveyor_controller import ConveyorBeltController
from robot_arm import RobotArmController
from scene_config import (
    DT,
    IK_ANIM_STEPS_FAST,
    LINE_A_CENTER_X,
    LINE_B_CENTER_X,
    RENDER_DELAY,
    SHUTTLE_SPEED,
    Y_ASSEM,
    Y_CAMERA,
    Y_POLISH,
)
from shuttle_controller import ShuttleController


SIM_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = SIM_ROOT.parent
DEFAULT_SCENE_PATH = SIM_ROOT / "scenes" / "assembly_line.ttt"
# Default contract used when you run: python contract_runner.py
DEFAULT_CONTRACT_PATH = PROJECT_ROOT / "outputs" / "contract" / "output_contract_llmmain.xml"


# Contract ARM names -> existing CoppeliaSim object aliases.
# Do not rename scene objects unless you also update the controller code.
ARM_MAP = {
    "ARM1": "Robot_Put_A",
    "ARM2": "Robot_Assem1_A",
    "ARM3": "Robot_Assem1_B",
    "ARM4": "Robot_Assem2",
    "ARM5": "Robot_Polish_B",
    "ARM6": "Robot_Polish_A",
    "ARM7": "Robot_Put_B",
}

MOVER_NAME = "Shuttle_A1"
MOVER_HOME_POINT = ("Mover home", -0.357, 0.0)


# Logical workstation coordinates in the existing assembly_line.ttt scene.
# These are not object aliases; they are the route targets used by Mover.
WORKSTATIONS = {
    "WS1": ("WS1/raw material outbound", -0.357, 0.238),
    "WS2": ("WS2/welding", 0, 0.357),
    "WS3": ("WS3/welding spare", 0.238, 0.357),
    "WS4": ("WS4/painting", 0.357, 0),
    "WS5": ("WS5/photo inspection", 0.238, -0.357),
    "WS6": ("WS6/finished warehousing", -0.238, -0.357),
}

FALLBACK_MOVER_ROUTE = ["WS1", "WS1", "WS1", "WS2", "WS4", "WS5", "WS6"]


# End-effector demonstration height. The products are not moved; only the arm
# tip moves above process points.
ARM_POINT_Z = 0.130
MOVER_CORNER_OFFSET_X = 0.065
MOVER_CORNER_OFFSET_Y = 0.065

RAW_WAREHOUSE_POINTS = {
    "car_chassis": ("car chassis shelf", -0.720, 0.558, ARM_POINT_Z),
    "car_battery": ("car battery shelf", -0.680, 0.613, ARM_POINT_Z),
    "car_body": ("car body shelf", -0.610, 0.730, ARM_POINT_Z),
    "phone_back": ("phone back shelf", -1.046, 0.561, ARM_POINT_Z),
    "phone_battery": ("phone battery shelf", -1.046, 0.618, ARM_POINT_Z),
    "phone_screen": ("phone screen shelf", -1.046, 0.675, ARM_POINT_Z),
}
RAW_WAREHOUSE_SEQUENCE_BY_PRODUCT = {
    "car": ["car_chassis", "car_battery", "car_body"],
    "phone": ["phone_back", "phone_battery", "phone_screen"],
}

FINISHED_WAREHOUSE_POINTS = {
    "car": ("finished car shelf3", -0.618, -0.666, ARM_POINT_Z),
    "phone": ("finished phone shelf4", -0.618, -1.046, ARM_POINT_Z),
}
DEFAULT_PRODUCT_TYPE = "car"

CONVEYOR_POINTS = {
    "belt1_start": ("belt1 start", -0.926, 0.418, ARM_POINT_Z),
    "belt1_end": ("belt1 end", -0.506, 0.418, ARM_POINT_Z),
    "belt2_start": ("belt2 start", -0.418, -0.516, ARM_POINT_Z),
    "belt2_end": ("belt2 end", -0.418, -0.916, ARM_POINT_Z),
}

ARM_DEFAULT_STATION = {
    "ARM2": "WS1",
    "ARM3": "WS2",
    "ARM4": "WS3",
    "ARM5": "WS4",
    "ARM6": "WS6",
}

TOOL_RACK_POINTS = {
    # Keep these target coordinates aligned with apply_contract_layout.py::TS_SLOTS.
    1: ("TS_ARM_05 slot 1", 0.636, 0.150, ARM_POINT_Z),
    2: ("TS_ARM_05 slot 2", 0.556, 0.150, ARM_POINT_Z),
    3: ("TS_ARM_05 slot 3", 0.676, -0.150, ARM_POINT_Z),
    4: ("TS_ARM_05 slot 4", 0.596, -0.150, ARM_POINT_Z),
    5: ("TS_ARM_05 slot 5", 0.516, -0.150, ARM_POINT_Z),
}
TOOL_RACK_SEQUENCE = tuple(TOOL_RACK_POINTS)


BELT_CONFIG = {
    "conveyor belt1": {
        "name": "ConveyorBelt1",
        "part_names": ["Part_Car_Body"],
        "axis": "x",
        "delta": 0.16,
    },
    "conveyor belt2": {
        "name": "ConveyorBelt2",
        "part_names": ["Part_Car_Body"],
        "axis": "y",
        "delta": -0.16,
    },
}


@dataclass(frozen=True)
class OperationNode:
    key: str
    group: str
    raw_text: str
    action: str
    target: str
    station: str | None = None
    material: str | None = None
    product: str | None = None
    context_id: str | None = None
    terminal_slot: int | None = None
    terminal_tool_type: str | None = None


def parse_contract_operations(
    contract_path: Path,
    operation_context_path: Path | None = None,
) -> list[OperationNode]:
    """Read Operation nodes in XML order."""
    tree = ET.parse(contract_path)
    root = tree.getroot()
    operation_context = load_operation_context(
        operation_context_path or infer_operation_context_path(contract_path)
    )

    nodes_by_key = {
        node.attrib.get("key", ""): node
        for node in root.findall("./NodeArray/Node")
    }
    outgoing_links = {
        link.attrib.get("from", ""): link.attrib.get("to", "")
        for link in root.findall("./LinkArray/Link")
    }
    contract_product = infer_contract_product(nodes_by_key.values())

    operations: list[OperationNode] = []
    for node in root.findall("./NodeArray/Node"):
        if node.attrib.get("category") != "Operation":
            continue

        raw_text = node.attrib.get("textt", "").strip()
        if "|" not in raw_text:
            print(f"[SKIP] Node {node.attrib.get('key')} has no action target: {raw_text}")
            continue

        action, target = raw_text.split("|", 1)
        normalized_action = normalize_name(action)
        normalized_target = normalize_name(target)
        group_key = node.attrib.get("group", "")
        group_node = nodes_by_key.get(group_key)
        group_text = (
            normalize_name(group_node.attrib.get("textt", ""))
            if group_node is not None
            else ""
        )
        product = infer_product_type(group_text) or contract_product
        station = None
        if action_key(normalized_target, normalized_action) == action_key("Mover", "move to"):
            next_key = outgoing_links.get(node.attrib.get("key", ""))
            station = infer_station_from_next_node(nodes_by_key.get(next_key))

        context = operation_context.get(node.attrib.get("key", ""))
        operations.append(
            OperationNode(
                key=node.attrib.get("key", ""),
                group=node.attrib.get("group", ""),
                raw_text=raw_text,
                action=normalized_action,
                target=normalized_target,
                station=station,
                material=infer_raw_material(group_text, product),
                product=product,
                context_id=(
                    str(context.get("context_id"))
                    if isinstance(context, dict) and context.get("context_id") is not None
                    else None
                ),
                terminal_slot=extract_terminal_slot(context),
                terminal_tool_type=extract_terminal_tool_type(context),
            )
        )

    return operations


def infer_operation_context_path(contract_path: Path) -> Path | None:
    """Find operation_context_*.json next to output_contract_*.xml."""
    candidates: list[Path] = []
    name = contract_path.name.casefold()

    if name.startswith("output_contract_") and name.endswith(".xml"):
        suffix = contract_path.stem.removeprefix("output_contract_")
        candidates.append(contract_path.with_name(f"operation_context_{suffix}.json"))

    candidates.append(contract_path.with_name("operation_context.json"))

    for product in ("car", "phone"):
        if product in contract_path.stem.casefold():
            candidates.append(contract_path.with_name(f"operation_context_{product}.json"))
            candidates.append(PROJECT_ROOT / "outputs" / "contract" / f"operation_context_{product}.json")

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def load_operation_context(context_path: Path | None) -> dict[str, dict[str, Any]]:
    if context_path is None:
        return {}

    with context_path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    contexts = data.get("operation_context", {})
    index = data.get("operation_node_index") or data.get("node_key_index") or {}
    if not isinstance(contexts, dict) or not isinstance(index, dict):
        return {}

    by_node_key: dict[str, dict[str, Any]] = {}
    for node_key, context_id in index.items():
        context = contexts.get(context_id)
        if isinstance(context, dict):
            by_node_key[str(node_key)] = context
    return by_node_key


def extract_terminal_slot(context: dict[str, Any] | None) -> int | None:
    if not isinstance(context, dict):
        return None

    for container_key in ("register_payload", "payload", "parameters"):
        container = context.get(container_key)
        slot = find_terminal_slot(container)
        if slot is not None:
            return slot
    return find_terminal_slot(context)


def find_terminal_slot(value: Any) -> int | None:
    if isinstance(value, dict):
        if isinstance(value.get("terminal_slot"), int):
            return value["terminal_slot"]
        if isinstance(value.get("linked_terminal_slot"), int):
            return value["linked_terminal_slot"]

        terminal = value.get("terminal")
        if isinstance(terminal, dict) and isinstance(terminal.get("slot"), int):
            return terminal["slot"]

        for nested in value.values():
            slot = find_terminal_slot(nested)
            if slot is not None:
                return slot

    if isinstance(value, list):
        for nested in value:
            slot = find_terminal_slot(nested)
            if slot is not None:
                return slot

    return None


def extract_terminal_tool_type(context: dict[str, Any] | None) -> str | None:
    if not isinstance(context, dict):
        return None

    for container_key in ("register_payload", "payload", "parameters"):
        tool_type = find_terminal_tool_type(context.get(container_key))
        if tool_type:
            return tool_type
    return find_terminal_tool_type(context)


def find_terminal_tool_type(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("terminal_tool_type", "tool_type"):
            if isinstance(value.get(key), str):
                return value[key]

        terminal = value.get("terminal")
        if isinstance(terminal, dict) and isinstance(terminal.get("tool_type"), str):
            return terminal["tool_type"]

        for nested in value.values():
            tool_type = find_terminal_tool_type(nested)
            if tool_type:
                return tool_type

    if isinstance(value, list):
        for nested in value:
            tool_type = find_terminal_tool_type(nested)
            if tool_type:
                return tool_type

    return None


def normalize_name(value: str) -> str:
    """Normalize spacing while preserving case."""
    return " ".join(value.replace("_", " ").strip().split())


def action_key(target: str, action: str) -> tuple[str, str]:
    return (target.casefold(), action.casefold())


def infer_station_from_next_node(node) -> str | None:
    """Infer Mover workstation from the node reached by move_to|Mover."""
    if node is None:
        return None

    category = node.attrib.get("category", "")
    raw_text = normalize_name(node.attrib.get("textt", ""))
    text = raw_text.casefold()

    if category == "Process":
        if text.startswith("raw material handling"):
            return "WS1"
        if text == "welding":
            return "WS2"
        if text == "painting":
            return "WS4"
        if text == "finished products warehousing":
            return "WS6"

    if category == "Operation" and action_key("camera", "photo inspection") == action_key(
        *raw_text.split("|", 1)[::-1]
    ):
        return "WS5"

    return None


def infer_contract_product(nodes) -> str | None:
    text = " ".join(normalize_name(node.attrib.get("textt", "")) for node in nodes)
    return infer_product_type(text)


def infer_product_type(text_value: str) -> str | None:
    text = text_value.casefold()
    if (
        has_word(text, "phone")
        or has_word(text, "screen")
        or has_word(text, "display")
        or has_word(text, "back")
        or "backplate" in text
        or "back panel" in text
        or any(token in text for token in ("手机", "屏幕", "背板", "后盖"))
    ):
        return "phone"
    if (
        has_word(text, "car")
        or has_word(text, "vehicle")
        or has_word(text, "chassis")
        or has_word(text, "body")
        or any(token in text for token in ("汽车", "底盘", "车身"))
    ):
        return "car"
    return None


def infer_raw_material(process_text: str, product: str | None = None) -> str | None:
    text = process_text.casefold()
    product = product or DEFAULT_PRODUCT_TYPE

    if has_word(text, "screen") or has_word(text, "display") or "屏幕" in text:
        return "phone_screen"
    if (
        has_word(text, "back")
        or "backplate" in text
        or "back panel" in text
        or "背板" in text
        or "后盖" in text
    ):
        return "phone_back"
    if has_word(text, "chassis") or "底盘" in text:
        return "car_chassis"
    if has_word(text, "battery"):
        return f"{product}_battery"
    if "电池" in text:
        return f"{product}_battery"
    if has_word(text, "body") or "车身" in text:
        return "car_body"
    return None


def has_word(text: str, word: str) -> bool:
    normalized = f" {text.replace('_', ' ')} "
    return f" {word} " in normalized


class ContractRunner:
    def __init__(
        self,
        sim,
        *,
        dry_run: bool = False,
        speed: float = SHUTTLE_SPEED,
        belt_speed: float = 0.10,
        move_products: bool = False,
    ):
        self.sim = sim
        self.dry_run = dry_run
        self.speed = speed
        self.belt_speed = belt_speed
        self.move_products = move_products

        self.mover: ShuttleController | None = None
        self.arms: dict[str, RobotArmController] = {}
        self.belts: dict[str, ConveyorBeltController] = {}
        self.move_index = 0
        self.current_station: str | None = None
        self.current_product_type = DEFAULT_PRODUCT_TYPE
        self.raw_warehouse_index = 0
        self.tool_slot_index = 0
        self.current_tool_slot = TOOL_RACK_POINTS[TOOL_RACK_SEQUENCE[0]]

        self.actions: dict[tuple[str, str], Callable[[OperationNode], None]] = {
            action_key("Mover", "move to"): self.move_mover,
            action_key("Mover", "release"): self.release_mover,

            action_key("ARM1", "outbound"): self.arm_transfer,
            action_key("ARM1", "inbound"): self.arm_transfer,
            action_key("ARM1", "reset"): self.arm_reset,

            action_key("ARM2", "move out"): self.arm_transfer,
            action_key("ARM2", "move in"): self.arm_transfer,
            action_key("ARM2", "reset"): self.arm_reset,

            action_key("ARM3", "track welding"): self.arm_track,
            action_key("ARM3", "reset"): self.arm_reset,

            action_key("ARM4", "track welding"): self.arm_track,
            action_key("ARM4", "reset"): self.arm_reset,

            action_key("ARM5", "pick up the terminal"): self.arm_transfer,
            action_key("ARM5", "track painting"): self.arm_track,
            action_key("ARM5", "put down the terminal"): self.arm_transfer,
            action_key("ARM5", "reset"): self.arm_reset,

            action_key("ARM6", "move out"): self.arm_transfer,
            action_key("ARM6", "move in"): self.arm_transfer,
            action_key("ARM6", "reset"): self.arm_reset,

            action_key("ARM7", "outbound"): self.arm_transfer,
            action_key("ARM7", "inbound"): self.arm_transfer,
            action_key("ARM7", "reset"): self.arm_reset,

            action_key("conveyor belt1", "forward"): self.belt_forward,
            action_key("conveyor belt1", "backward"): self.belt_backward,
            action_key("conveyor belt1", "stop"): self.belt_stop,
            action_key("conveyor belt2", "forward"): self.belt_forward,
            action_key("conveyor belt2", "backward"): self.belt_backward,
            action_key("conveyor belt2", "stop"): self.belt_stop,

            action_key("camera", "photo inspection"): self.camera_photo_inspection,
        }

    def run(self, operations: list[OperationNode], *, limit: int | None = None) -> None:
        count = len(operations) if limit is None else min(limit, len(operations))
        print(f"[INFO] Executing {count}/{len(operations)} operation nodes")

        for index, op in enumerate(operations[:count], start=1):
            metadata = []
            if op.terminal_slot is not None:
                metadata.append(f"terminal_slot={op.terminal_slot}")
            if op.terminal_tool_type:
                metadata.append(f"terminal_tool={op.terminal_tool_type}")
            metadata_text = f" ({', '.join(metadata)})" if metadata else ""
            print(f"\n[{index:02d}/{count:02d}] node={op.key} action={op.raw_text}{metadata_text}")
            if self.dry_run:
                continue

            if op.product:
                self.current_product_type = op.product

            handler = self.actions.get(action_key(op.target, op.action))
            if handler is None:
                self.unknown_action(op)
                continue
            handler(op)

    # ------------------------------------------------------------------
    # Controller creation
    # ------------------------------------------------------------------

    def get_mover(self) -> ShuttleController:
        if self.mover is None:
            handle = self.sim.getObject(f"/{MOVER_NAME}")
            self.mover = ShuttleController(
                self.sim,
                handle,
                name="Mover1",
                dt=DT,
                render_delay=RENDER_DELAY,
            )
        return self.mover

    def get_arm(self, contract_arm_name: str) -> RobotArmController:
        scene_name = ARM_MAP.get(contract_arm_name.upper())
        if scene_name is None:
            raise KeyError(f"No ARM_MAP entry for {contract_arm_name}")

        if contract_arm_name not in self.arms:
            print(f"  [INIT] {contract_arm_name} -> {scene_name}")
            self.arms[contract_arm_name] = RobotArmController(
                self.sim,
                scene_name,
                dt=DT,
                render_delay=RENDER_DELAY,
            )
        return self.arms[contract_arm_name]

    def get_belt(self, contract_belt_name: str) -> ConveyorBeltController:
        belt_key = contract_belt_name.casefold()
        config = BELT_CONFIG.get(belt_key)
        if config is None:
            raise KeyError(f"No BELT_CONFIG entry for {contract_belt_name}")

        if belt_key not in self.belts:
            print(f"  [INIT] {contract_belt_name} -> {config['name']}")
            self.belts[belt_key] = ConveyorBeltController(
                self.sim,
                config["name"],
                config["part_names"],
                axis=config["axis"],
                delta=config["delta"],
                speed=self.belt_speed,
                dt=DT,
                render_delay=RENDER_DELAY,
            )
        return self.belts[belt_key]

    # ------------------------------------------------------------------
    # Action handlers
    # ------------------------------------------------------------------

    def move_mover(self, op: OperationNode) -> None:
        label, x, y = self.resolve_mover_target(op)
        print(f"  [Mover] move_to {label}: x={x:.4f}, y={y:.4f}")
        self.get_mover().move_to(x, y, speed=self.speed)

    def release_mover(self, op: OperationNode) -> None:
        label, x, y = MOVER_HOME_POINT
        print(f"  [Mover] release -> {label}: x={x:.4f}, y={y:.4f}")
        self.get_mover().move_to(x, y, speed=self.speed)
        self.current_station = None

    def arm_reset(self, op: OperationNode) -> None:
        arm_name = op.target.upper()
        print(f"  [{arm_name}] reset -> home")
        self.get_arm(arm_name).move_to_home(steps=IK_ANIM_STEPS_FAST)

    def arm_transfer(self, op: OperationNode) -> None:
        arm_name = op.target.upper()
        print(f"  [{arm_name}] {op.action} point-to-point route")
        arm = self.get_arm(arm_name)
        route = self.resolve_arm_transfer_route(arm_name, op.action, op)
        if route:
            self.move_arm_through_points(arm, route)
            return

        print(f"  [WARN] [{arm_name}] no point route for {op.action}; using joint gesture")
        self.arm_joint_gesture(arm, op.action)

    def arm_track(self, op: OperationNode) -> None:
        arm_name = op.target.upper()
        station = self.station_for_arm(arm_name)
        print(f"  [{arm_name}] {op.action} around {station} mover corners")
        arm = self.get_arm(arm_name)
        self.move_arm_through_points(arm, self.mover_corner_points(station))

    def belt_forward(self, op: OperationNode) -> None:
        if not self.move_products:
            print(f"  [{op.target}] forward skipped; products stay on shelves")
            self.wait_steps(8)
            return
        print(f"  [{op.target}] forward")
        self.get_belt(op.target).forward()

    def belt_backward(self, op: OperationNode) -> None:
        if not self.move_products:
            print(f"  [{op.target}] backward skipped; products stay on shelves")
            self.wait_steps(8)
            return
        print(f"  [{op.target}] backward")
        self.get_belt(op.target).backward()

    def belt_stop(self, op: OperationNode) -> None:
        if not self.move_products:
            print(f"  [{op.target}] stop; products stay on shelves")
            self.wait_steps(5)
            return
        print(f"  [{op.target}] stop")
        self.get_belt(op.target).stop()

    def camera_photo_inspection(self, op: OperationNode) -> None:
        print("  [camera] photo inspection simulated")
        self.wait_steps(20)

    def unknown_action(self, op: OperationNode) -> None:
        print(f"  [WARN] No handler for {op.raw_text}; waiting instead")
        self.wait_steps(8)

    # ------------------------------------------------------------------
    # Motion helpers
    # ------------------------------------------------------------------

    def resolve_mover_target(self, op: OperationNode) -> tuple[str, float, float]:
        station = op.station
        if station is None:
            route_index = min(self.move_index, len(FALLBACK_MOVER_ROUTE) - 1)
            station = FALLBACK_MOVER_ROUTE[route_index]
        self.move_index += 1

        if station not in WORKSTATIONS:
            raise KeyError(f"No WORKSTATIONS entry for {station}")
        self.current_station = station
        return WORKSTATIONS[station]

    def station_for_arm(self, arm_name: str) -> str:
        if self.current_station in WORKSTATIONS:
            return self.current_station
        return ARM_DEFAULT_STATION.get(arm_name, "WS1")

    def workstation_point(self, station: str) -> tuple[str, float, float, float]:
        label, x, y = WORKSTATIONS[station]
        return label, x, y, ARM_POINT_Z

    def next_raw_warehouse_point(
        self, product: str | None = None
    ) -> tuple[str, float, float, float]:
        product = product or self.current_product_type or DEFAULT_PRODUCT_TYPE
        sequence = RAW_WAREHOUSE_SEQUENCE_BY_PRODUCT.get(
            product, RAW_WAREHOUSE_SEQUENCE_BY_PRODUCT[DEFAULT_PRODUCT_TYPE]
        )
        material = sequence[
            self.raw_warehouse_index % len(sequence)
        ]
        self.raw_warehouse_index += 1
        return RAW_WAREHOUSE_POINTS[material]

    def raw_warehouse_point_for_operation(
        self, op: OperationNode
    ) -> tuple[str, float, float, float]:
        if op.material in RAW_WAREHOUSE_POINTS:
            return RAW_WAREHOUSE_POINTS[op.material]
        return self.next_raw_warehouse_point(op.product)

    def finished_warehouse_point_for_operation(
        self, op: OperationNode
    ) -> tuple[str, float, float, float]:
        product = op.product or self.current_product_type or DEFAULT_PRODUCT_TYPE
        return FINISHED_WAREHOUSE_POINTS.get(
            product, FINISHED_WAREHOUSE_POINTS[DEFAULT_PRODUCT_TYPE]
        )

    def tool_rack_point_for_operation(
        self, op: OperationNode
    ) -> tuple[str, float, float, float]:
        if op.terminal_slot is not None:
            point = TOOL_RACK_POINTS.get(op.terminal_slot)
            if point is None:
                print(f"    [WARN] Unknown terminal slot {op.terminal_slot}; using fallback slot")
                return self.next_tool_rack_point()
            self.current_tool_slot = point
            return point
        return self.next_tool_rack_point()

    def next_tool_rack_point(self) -> tuple[str, float, float, float]:
        slot = TOOL_RACK_SEQUENCE[self.tool_slot_index % len(TOOL_RACK_SEQUENCE)]
        point = TOOL_RACK_POINTS[slot]
        self.tool_slot_index += 1
        self.current_tool_slot = point
        return point

    def resolve_arm_transfer_route(
        self, arm_name: str, action: str, op: OperationNode
    ) -> list[tuple[str, float, float, float]]:
        key = action.casefold()

        if key == "outbound":
            if arm_name == "ARM1":
                return [self.raw_warehouse_point_for_operation(op), CONVEYOR_POINTS["belt1_start"]]
            if arm_name == "ARM7":
                return [self.finished_warehouse_point_for_operation(op), CONVEYOR_POINTS["belt2_end"]]

        if key == "inbound":
            if arm_name == "ARM7":
                return [CONVEYOR_POINTS["belt2_end"], self.finished_warehouse_point_for_operation(op)]
            if arm_name == "ARM1":
                return [CONVEYOR_POINTS["belt1_start"], self.raw_warehouse_point_for_operation(op)]

        if key == "move out":
            station = self.station_for_arm(arm_name)
            if arm_name == "ARM2":
                return [CONVEYOR_POINTS["belt1_end"], self.workstation_point(station)]
            if arm_name == "ARM6":
                return [CONVEYOR_POINTS["belt2_start"], self.workstation_point(station)]

        if key == "move in":
            station = self.station_for_arm(arm_name)
            if arm_name == "ARM2":
                return [self.workstation_point(station), CONVEYOR_POINTS["belt1_end"]]
            if arm_name == "ARM6":
                return [self.workstation_point(station), CONVEYOR_POINTS["belt2_start"]]

        if key == "pick up the terminal":
            return [self.tool_rack_point_for_operation(op)]

        if key == "put down the terminal":
            if op.terminal_slot is not None:
                return [self.tool_rack_point_for_operation(op)]
            return [self.current_tool_slot]

        return []

    def mover_corner_points(self, station: str) -> list[tuple[str, float, float, float]]:
        label, x, y = WORKSTATIONS[station]
        return [
            (f"{label} corner front-left", x - MOVER_CORNER_OFFSET_X, y + MOVER_CORNER_OFFSET_Y, ARM_POINT_Z),
            (f"{label} corner front-right", x + MOVER_CORNER_OFFSET_X, y + MOVER_CORNER_OFFSET_Y, ARM_POINT_Z),
            (f"{label} corner rear-right", x + MOVER_CORNER_OFFSET_X, y - MOVER_CORNER_OFFSET_Y, ARM_POINT_Z),
            (f"{label} corner rear-left", x - MOVER_CORNER_OFFSET_X, y - MOVER_CORNER_OFFSET_Y, ARM_POINT_Z),
            (f"{label} center", x, y, ARM_POINT_Z),
        ]

    def move_arm_through_points(
        self,
        arm: RobotArmController,
        points: list[tuple[str, float, float, float]],
    ) -> None:
        for label, x, y, z in points:
            print(f"    -> {label}: x={x:.3f}, y={y:.3f}, z={z:.3f}")
            arm.move_tip_to([x, y, z], steps=IK_ANIM_STEPS_FAST)

    def arm_joint_gesture(self, arm: RobotArmController, action: str) -> None:
        current = arm.ik.get_joint_positions()
        direction = -1.0 if "in" in action.casefold() else 1.0
        target = list(current)

        if len(target) >= 3:
            target[0] += direction * 0.35
            target[1] += direction * 0.18
            target[2] -= direction * 0.16
        elif target:
            target[0] += direction * 0.35

        arm.ik.animate_to_angles(target, IK_ANIM_STEPS_FAST)

    def arm_sweep_gesture(self, arm: RobotArmController) -> None:
        base = arm.ik.get_joint_positions()
        if not base:
            self.wait_steps(10)
            return

        for offset in (0.25, -0.25, 0.0):
            target = list(base)
            target[0] = base[0] + offset
            if len(target) >= 5:
                target[4] = base[4] + 0.20 * math.sin(offset * math.pi)
            arm.ik.animate_to_angles(target, max(8, IK_ANIM_STEPS_FAST // 2))

    def wait_steps(self, steps: int) -> None:
        for _ in range(steps):
            self.sim.step()
            if RENDER_DELAY > 0:
                time.sleep(RENDER_DELAY)


def ensure_stopped(sim) -> None:
    if sim.getSimulationState() == sim.simulation_stopped:
        return

    print("[INFO] Stopping previous simulation state")
    sim.stopSimulation()
    for _ in range(80):
        if sim.getSimulationState() == sim.simulation_stopped:
            return
        time.sleep(0.05)


def ensure_scene_loaded(sim, scene_path: Path, *, load_scene: bool) -> None:
    if load_scene:
        print(f"[INFO] Loading scene: {scene_path}")
        sim.loadScene(str(scene_path).replace("\\", "/"))
        return

    try:
        sim.getObject(f"/{MOVER_NAME}")
    except Exception as exc:
        raise RuntimeError(
            f"Scene object /{MOVER_NAME} was not found. "
            f"Open {scene_path} in CoppeliaSim first, or run with --load-scene."
        ) from exc


def remove_legacy_robot_scripts(sim) -> None:
    """Remove old scene child scripts that conflict with Python control."""
    for scene_name in ARM_MAP.values():
        script_path = f"/{scene_name}/Script"
        try:
            script_handle = sim.getObject(script_path)
        except Exception:
            continue

        try:
            sim.removeObject(script_handle)
            print(f"[REMOVE] legacy robot script {script_path}")
        except Exception as exc:
            print(f"[WARN] Could not remove legacy robot script {script_path}: {exc}")


def configure_simulation(sim) -> None:
    sim.setBoolParam(sim.boolparam_dynamics_handling_enabled, False)
    sim.setStepping(True)
    sim.setFloatParam(sim.floatparam_simulation_time_step, DT)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Execute contract XML operations in CoppeliaSim."
    )
    parser.add_argument(
        "--contract",
        type=Path,
        default=DEFAULT_CONTRACT_PATH,
        help="Path to output_contract_llmmain.xml.",
    )
    parser.add_argument(
        "--operation-context",
        type=Path,
        default=None,
        help=(
            "Path to operation_context_*.json. If omitted, it is inferred from "
            "--contract, e.g. output_contract_car.xml -> operation_context_car.json."
        ),
    )
    parser.add_argument(
        "--scene",
        type=Path,
        default=DEFAULT_SCENE_PATH,
        help="Scene path used when --load-scene is provided.",
    )
    parser.add_argument(
        "--load-scene",
        action="store_true",
        help="Load the scene from --scene before running. Otherwise use the open scene.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and print operations without moving simulation objects.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Execute only the first N operation nodes.",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=SHUTTLE_SPEED,
        help="Mover speed in m/s.",
    )
    parser.add_argument(
        "--belt-speed",
        type=float,
        default=0.10,
        help="Logical conveyor speed in m/s.",
    )
    parser.add_argument(
        "--move-products",
        action="store_true",
        help="Also move configured product objects during conveyor belt operations.",
    )
    parser.add_argument(
        "--keep-running",
        action="store_true",
        help="Do not stop the simulation after the contract finishes.",
    )
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()

    if not args.contract.exists():
        raise FileNotFoundError(f"Contract XML not found: {args.contract}")
    if args.operation_context is not None and not args.operation_context.exists():
        raise FileNotFoundError(
            f"Operation context JSON not found: {args.operation_context}"
        )
    if args.load_scene and not args.scene.exists():
        raise FileNotFoundError(f"Scene file not found: {args.scene}")

    context_path = args.operation_context or infer_operation_context_path(args.contract)
    operations = parse_contract_operations(args.contract, context_path)
    if not operations:
        raise RuntimeError(f"No Operation nodes found in {args.contract}")

    print(f"[INFO] Contract XML: {args.contract}")
    if context_path is None:
        print("[WARN] Operation context JSON not found; terminal slots will use fallback order")
    else:
        print(f"[INFO] Operation context: {context_path}")

    if args.dry_run:
        count = len(operations) if args.limit is None else min(args.limit, len(operations))
        print(f"[DRY-RUN] Parsed {len(operations)} operation nodes from:")
        print(f"          {args.contract}")
        if context_path is not None:
            print(f"          context: {context_path}")
        for index, op in enumerate(operations[:count], start=1):
            suffix = f" -> {op.station}" if op.station else ""
            if op.product:
                suffix += f" product={op.product}"
            if op.material:
                suffix += f" material={op.material}"
            if op.terminal_slot is not None:
                suffix += f" terminal_slot={op.terminal_slot}"
            if op.terminal_tool_type:
                suffix += f" terminal_tool={op.terminal_tool_type}"
            print(f"[{index:02d}/{count:02d}] node={op.key} action={op.raw_text}{suffix}")
        return

    from coppeliasim_zmqremoteapi_client import RemoteAPIClient

    client = RemoteAPIClient()
    sim = client.require("sim")

    ensure_stopped(sim)
    ensure_scene_loaded(sim, args.scene, load_scene=args.load_scene)
    remove_legacy_robot_scripts(sim)
    configure_simulation(sim)

    try:
        if not args.dry_run:
            print("[INFO] Starting simulation")
            sim.startSimulation()

        runner = ContractRunner(
            sim,
            dry_run=False,
            speed=args.speed,
            belt_speed=args.belt_speed,
            move_products=args.move_products,
        )
        runner.run(operations, limit=args.limit)

    finally:
        if not args.keep_running and not args.dry_run:
            print("\n[INFO] Stopping simulation")
            sim.stopSimulation()


if __name__ == "__main__":
    main()
