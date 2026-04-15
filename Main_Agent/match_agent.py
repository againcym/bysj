from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from Config.factory_mcp_server import (
    InventorySummaryRecord,
    SlotRecord,
    ToolSlotRecord,
    get_inventory_summary_data,
    query_all_empty_slots_data,
    query_all_material_locations_data,
    query_tool_by_spec_data,
)


HARD_CODED_MODEL = "qwen3coder"
HARD_CODED_API_KEY = "sk-dQIpgr85q-E2l2Emr01uzw"
HARD_CODED_BASE_URL = "https://models.sjtu.edu.cn/api/v1/"

PRODUCT_KEYWORDS = {
    "car": ["汽车", "小车", "car", "vehicle"],
    "phone": ["手机", "电话", "phone", "mobile"],
}

STANDARD_MATERIAL_ORDER = {
    "car": ["car_chassis", "car_battery", "car_body"],
    "phone": ["phone_back", "phone_battery", "phone_screen"],
}

WAREHOUSING_ITEM_TYPE = {
    "car": "car_part",
    "phone": "phone_part",
}

COLOR_ALIASES = {
    "red": ["red", "红色", "红"],
    "blue": ["blue", "蓝色", "蓝"],
    "green": ["green", "绿色", "绿"],
    "yellow": ["yellow", "黄色", "黄"],
    "black": ["black", "黑色", "黑"],
    "white": ["white", "白色", "白"],
}

DISPLAY_COLOR = {
    "red": "红色",
    "blue": "蓝色",
    "green": "绿色",
    "yellow": "黄色",
    "black": "黑色",
    "white": "白色",
}

ALLOWED_TOOL_TYPES = {"Wide_Spray_Nozzle", "Fine_Point_Pen"}
SURFACE_KEYWORDS = (
    "喷涂", "涂装", "上色", "喷漆", "paint", "spray",
    "写", "字样", "文字", "logo", "标识",
    "描边", "勾边", "轮廓", "线条", "细节",
)

_ALL_COLOR_TOKENS = sorted({alias for aliases in COLOR_ALIASES.values() for alias in aliases}, key=len, reverse=True)
COLOR_TOKEN_PATTERN = "|".join(re.escape(token) for token in _ALL_COLOR_TOKENS)
PRODUCT_TOKEN_PATTERN = r"(?:汽车|小车|手机|car|phone|mobile|vehicle)"


class PlanningError(RuntimeError):
    """Raised when a physical plan cannot be grounded to available factory resources."""


@dataclass(frozen=True)
class MaterialPlan:
    target_product: str
    selected_materials: List[str]
    match_status: str
    all_locations: Dict[str, List[SlotRecord]] = field(default_factory=dict)
    primary_locations: Dict[str, SlotRecord] = field(default_factory=dict)


@dataclass(frozen=True)
class WarehousingPlan:
    item_type: str
    selected_slot: SlotRecord

    @property
    def coord(self) -> Tuple[int, int, int]:
        return self.selected_slot.coord_tuple()

    @property
    def coord_str(self) -> str:
        a, b, c = self.coord
        return f"({a}, {b}, {c})"


@dataclass(frozen=True)
class PaintingOperationPlan:
    color: str
    tool_type: str
    operation_kind: str
    instruction_text: str
    matched_tool: ToolSlotRecord

    @property
    def tool_label(self) -> str:
        return f"末端工具{self.matched_tool.slot_index}"

    def to_request_dict(self) -> Dict[str, str]:
        return {
            "color": self.color,
            "tool_type": self.tool_type,
            "operation_kind": self.operation_kind,
            "instruction_text": self.instruction_text,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "color": self.color,
            "tool_type": self.tool_type,
            "operation_kind": self.operation_kind,
            "instruction_text": self.instruction_text,
            "matched_tool": self.matched_tool.to_dict(),
        }


@dataclass(frozen=True)
class PhysicalPlan:
    material_plan: MaterialPlan
    tool_plan: List[ToolSlotRecord]
    warehousing_plan: WarehousingPlan
    painting_plan: List[PaintingOperationPlan]


class JsonLLMHelper:
    def __init__(self, enabled: bool = True, temperature: float = 0.1):
        self.enabled = False
        self.temperature = temperature
        self.client = None

        if not enabled:
            return

        try:
            from langchain_openai import ChatOpenAI  # type: ignore

            self.client = ChatOpenAI(
                model=HARD_CODED_MODEL,
                api_key=HARD_CODED_API_KEY,
                base_url=HARD_CODED_BASE_URL,
                temperature=temperature,
                max_retries=1,
                timeout=20,
            )
            self.enabled = True
        except Exception as exc:  # pragma: no cover
            print(f"⚠️ LLM 客户端不可用，自动切换为确定性规划: {exc}")
            self.enabled = False
            self.client = None

    @staticmethod
    def extract_first_json_block(text: Any) -> Optional[Any]:
        text = str(text or "").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            pass
        for pattern in (r"\{.*?\}", r"\[.*?\]"):
            match = re.search(pattern, text, re.DOTALL)
            if not match:
                continue
            try:
                return json.loads(match.group(0))
            except Exception:
                continue
        return None

    def ask_json(self, system_prompt: str, user_prompt: str, fallback: Any) -> Any:
        if not self.enabled or self.client is None:
            return fallback
        try:
            res = self.client.invoke([
                ("system", system_prompt),
                ("user", user_prompt),
            ])
            parsed = self.extract_first_json_block(getattr(res, "content", str(res)))
            return parsed if parsed is not None else fallback
        except Exception as exc:  # pragma: no cover
            print(f"⚠️ LLM 调用失败，使用 fallback: {exc}")
            return fallback


class LocalMaterialPlanner:
    """
    保留 LocalMaterialPlanner 名称，继续作为主流程入口使用。

    这一版的增强点：
    1. MCP 与 planner 全程结构化耦合；
    2. ontology 上游需要的 painting 语义在这里先做稳定提取；
    3. “喷涂 / 写字 / 描边”统一归到 PAINTING 站，但会保留不同末端与不同指令文本；
    4. __main__ 提供覆盖面更广的回归测试集。
    """

    def __init__(self, enable_llm: bool = True):
        self.llm = JsonLLMHelper(enabled=enable_llm, temperature=0.1)
        self._material_cache: Dict[str, MaterialPlan] = {}
        self._painting_cache: Dict[str, List[PaintingOperationPlan]] = {}
        self._warehousing_cache: Dict[str, WarehousingPlan] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def build_physical_plan(self, user_order: str) -> PhysicalPlan:
        material_plan = self.get_ai_material_plan(user_order)
        painting_plan = self.get_ai_painting_plan(user_order)
        warehousing_plan = self.get_ai_warehousing_plan(user_order)
        tool_plan = [item.matched_tool for item in painting_plan]
        return PhysicalPlan(
            material_plan=material_plan,
            tool_plan=tool_plan,
            warehousing_plan=warehousing_plan,
            painting_plan=painting_plan,
        )

    def get_ai_material_plan(self, user_order: str) -> MaterialPlan:
        if user_order in self._material_cache:
            return self._material_cache[user_order]

        print(f"--- 接收订单: {user_order} ---")
        inventory = get_inventory_summary_data()
        counts = {item.status: item.count for item in inventory}
        fallback_target = self._detect_target_product(user_order)
        fallback_selected = [m for m in STANDARD_MATERIAL_ORDER[fallback_target] if counts.get(m, 0) > 0]
        fallback_missing = [m for m in STANDARD_MATERIAL_ORDER[fallback_target] if counts.get(m, 0) <= 0]
        fallback_status = "OK" if not fallback_missing else f"缺少{', '.join(fallback_missing)}"
        fallback = {
            "target_product": fallback_target,
            "selected_materials": fallback_selected,
            "match_status": fallback_status,
        }

        llm_result = self.llm.ask_json(
            system_prompt=(
                "你是一个严谨的工业调度专家。"
                "请根据用户订单和实时库存，确定目标产品类型，并从标准物料序列中挑选可用物料。"
                "只允许 target_product 为 car 或 phone。"
                "只允许 selected_materials 为该 target_product 对应的标准物料子集。"
                "输出 JSON："
                '{"target_product":"car|phone","selected_materials":[...],"match_status":"..."}'
            ),
            user_prompt=(
                f"订单：{user_order}\n"
                f"库存：{json.dumps([x.to_dict() for x in inventory], ensure_ascii=False)}\n"
                f"标准物料序列：{json.dumps(STANDARD_MATERIAL_ORDER, ensure_ascii=False)}"
            ),
            fallback=fallback,
        )

        target_product = str((llm_result or {}).get("target_product", fallback_target)).strip().lower()
        if target_product not in STANDARD_MATERIAL_ORDER:
            target_product = fallback_target

        selected_materials = [
            material
            for material in STANDARD_MATERIAL_ORDER[target_product]
            if material in set((llm_result or {}).get("selected_materials", [])) and counts.get(material, 0) > 0
        ]
        if not selected_materials:
            selected_materials = fallback_selected

        if not selected_materials:
            raise PlanningError(f"无法为订单 '{user_order}' 匹配到可用原料。")

        missing_materials = [m for m in STANDARD_MATERIAL_ORDER[target_product] if counts.get(m, 0) <= 0]
        match_status = "OK" if not missing_materials else f"缺少{', '.join(missing_materials)}"

        all_locations: Dict[str, List[SlotRecord]] = {}
        primary_locations: Dict[str, SlotRecord] = {}
        for material in selected_materials:
            locations = query_all_material_locations_data(material)
            if not locations:
                raise PlanningError(f"库存统计显示存在 '{material}'，但未找到其具体仓位。")
            all_locations[material] = locations
            primary_locations[material] = locations[0]

        print("\n--- 结构化物料规划结果 ---")
        print(f"🎯 目标成品: {target_product}")
        print(f"📋 选定物料: {', '.join(selected_materials)}")
        print(f"🚩 状态反馈: {match_status}")

        result = MaterialPlan(
            target_product=target_product,
            selected_materials=selected_materials,
            match_status=match_status,
            all_locations=all_locations,
            primary_locations=primary_locations,
        )
        self._material_cache[user_order] = result
        return result

    def get_ai_painting_plan(self, user_order: str) -> List[PaintingOperationPlan]:
        if user_order in self._painting_cache:
            return self._painting_cache[user_order]

        print(f"\n--- 正在分析 PAINTING 工位需求: {user_order} ---")
        grounded_requests = self._infer_painting_requests_rule(user_order)

        if not grounded_requests:
            if self._has_surface_request(user_order):
                raise PlanningError(
                    "检测到表面处理需求，但未能从订单中解析出明确颜色。"
                    "请在需求中明确写成例如“红色喷涂”“蓝色写LUCKY字样”“黑色描边”。"
                )
            print("✅ 未检测到喷涂 / 写字 / 描边需求，无需生成 PAINTING 工位动作。")
            self._painting_cache[user_order] = []
            return []

        plans: List[PaintingOperationPlan] = []
        for request in grounded_requests:
            candidates = query_tool_by_spec_data(tool_type=request["tool_type"], color=request["color"])
            if not candidates:
                raise PlanningError(
                    f"未找到匹配工具: 类型={request['tool_type']}, 颜色={request['color']}"
                )
            matched_tool = candidates[0]
            plan = PaintingOperationPlan(
                color=request["color"],
                tool_type=request["tool_type"],
                operation_kind=request["operation_kind"],
                instruction_text=request["instruction_text"],
                matched_tool=matched_tool,
            )
            plans.append(plan)
            print(
                f"✅ 需求落地成功: [{request['instruction_text']}] -> "
                f"{matched_tool.ts_id} 槽位 {matched_tool.slot_index} ({matched_tool.tool_type}, {matched_tool.color})"
            )

        print(f"\n--- 结构化 PAINTING 规划结果 (共 {len(plans)} 项) ---")
        for idx, plan in enumerate(plans, start=1):
            print(
                f"[{idx}] 🧩 指令: {plan.instruction_text} | "
                f"🔧 工具: {plan.tool_type} | 🎨 颜色: {plan.color} | "
                f"📍 槽位: {plan.matched_tool.ts_id}:{plan.matched_tool.slot_index}"
            )

        self._painting_cache[user_order] = plans
        return plans

    def get_ai_tool_plan(self, user_order: str) -> List[ToolSlotRecord]:
        return [item.matched_tool for item in self.get_ai_painting_plan(user_order)]

    def get_ai_warehousing_plan(self, user_order: str) -> WarehousingPlan:
        if user_order in self._warehousing_cache:
            return self._warehousing_cache[user_order]

        print(f"\n--- 正在分析成品入库需求: {user_order} ---")
        fallback_product = self._detect_target_product(user_order)
        fallback_item_type = WAREHOUSING_ITEM_TYPE[fallback_product]
        llm_result = self.llm.ask_json(
            system_prompt=(
                "你是一个工业分类专家。"
                "请判断用户订单对应的成品入库类别。只允许 car_part 或 phone_part。"
                "输出 JSON：{'item_type':'car_part'}"
            ),
            user_prompt=f"订单：{user_order}",
            fallback={"item_type": fallback_item_type},
        )
        item_type = str((llm_result or {}).get("item_type", fallback_item_type)).strip()
        if item_type not in {"car_part", "phone_part"}:
            item_type = fallback_item_type

        empty_slots = query_all_empty_slots_data(item_type)
        if not empty_slots:
            raise PlanningError(f"类型 '{item_type}' 的存储区域已满，无法生成正确的入库坐标。")

        selected_slot = empty_slots[0]
        result = WarehousingPlan(item_type=item_type, selected_slot=selected_slot)
        print(f"✅ 入库匹配成功: {item_type} -> 坐标数组: {result.coord}")
        self._warehousing_cache[user_order] = result
        return result

    # ------------------------------------------------------------------
    # Rule-based helpers
    # ------------------------------------------------------------------
    def _detect_target_product(self, user_order: str) -> str:
        text = user_order.lower()
        for product, keywords in PRODUCT_KEYWORDS.items():
            for keyword in keywords:
                if keyword.lower() in text:
                    return product
        return "car"

    def _has_surface_request(self, user_order: str) -> bool:
        text = user_order.lower()
        if any(keyword in text for keyword in SURFACE_KEYWORDS):
            return True
        return self._find_product_color(user_order) is not None

    def _infer_painting_requests_rule(self, user_order: str) -> List[Dict[str, str]]:
        requests: List[Dict[str, str]] = []

        product_color = self._find_product_color(user_order)
        if product_color:
            requests.append({
                "color": product_color,
                "tool_type": "Wide_Spray_Nozzle",
                "operation_kind": "spray",
                "instruction_text": f"{DISPLAY_COLOR[product_color]}整机喷涂信息",
            })

        requests.extend(self._find_writing_requests(user_order))
        requests.extend(self._find_outline_requests(user_order))

        deduped: List[Dict[str, str]] = []
        seen = set()
        for item in requests:
            key = (item["color"], item["tool_type"], item["operation_kind"], item["instruction_text"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    def _find_product_color(self, user_order: str) -> Optional[str]:
        text = str(user_order or "")
        patterns = [
            re.compile(rf"(?P<color>{COLOR_TOKEN_PATTERN})(?:的)?\s*{PRODUCT_TOKEN_PATTERN}", re.IGNORECASE),
            re.compile(rf"{PRODUCT_TOKEN_PATTERN}\s*(?:喷成|涂成|上色为|颜色为|painted|sprayed)\s*(?P<color>{COLOR_TOKEN_PATTERN})", re.IGNORECASE),
            re.compile(rf"(?:喷涂|涂装|上色|喷漆|paint|spray)\s*(?:成|为)?\s*(?P<color>{COLOR_TOKEN_PATTERN})", re.IGNORECASE),
        ]
        for pattern in patterns:
            match = pattern.search(text)
            if not match:
                continue
            color = self._normalize_color(match.group("color"))
            if color:
                return color
        return None

    def _find_writing_requests(self, user_order: str) -> List[Dict[str, str]]:
        text = str(user_order or "")
        results: List[Dict[str, str]] = []
        pattern = re.compile(
            rf"(?:用)?(?P<color>{COLOR_TOKEN_PATTERN})(?:的)?(?:[\s\-]{0,2}|[\u4e00-\u9fa5A-Za-z0-9_]{{0,4}})?"
            rf"(?:写|写上|写出)(?P<content>[\u4e00-\u9fa5A-Za-z0-9_\-]{{1,24}}?)"
            rf"(?:字样|文字|logo|标识)?(?=的|在|并|，|。|,|\.|$)",
            re.IGNORECASE,
        )
        for match in pattern.finditer(text):
            color = self._normalize_color(match.group("color"))
            if not color:
                continue
            content = str(match.group("content") or "").strip(" _-，。,.的")
            instruction = (
                f"{DISPLAY_COLOR[color]}写{content}字样信息"
                if content else f"{DISPLAY_COLOR[color]}写字信息"
            )
            results.append({
                "color": color,
                "tool_type": "Fine_Point_Pen",
                "operation_kind": "writing",
                "instruction_text": instruction,
            })
        return results

    def _find_outline_requests(self, user_order: str) -> List[Dict[str, str]]:
        text = str(user_order or "")
        results: List[Dict[str, str]] = []
        pattern = re.compile(
            rf"(?:用)?(?P<color>{COLOR_TOKEN_PATTERN})(?:的)?(?:[\s\-]{{0,2}}|[\u4e00-\u9fa5A-Za-z0-9_]{{0,4}})?"
            rf"(?P<kind>描边|勾边|轮廓|线条|细节)",
            re.IGNORECASE,
        )
        kind_map = {
            "描边": "描边",
            "勾边": "描边",
            "轮廓": "轮廓描边",
            "线条": "线条细节",
            "细节": "细节描边",
        }
        for match in pattern.finditer(text):
            color = self._normalize_color(match.group("color"))
            kind = str(match.group("kind") or "").strip()
            if not color:
                continue
            kind_text = kind_map.get(kind, kind)
            results.append({
                "color": color,
                "tool_type": "Fine_Point_Pen",
                "operation_kind": "outline",
                "instruction_text": f"{DISPLAY_COLOR[color]}{kind_text}信息",
            })
        return results

    def _normalize_color(self, raw_color: str) -> Optional[str]:
        if not raw_color:
            return None
        lowered = raw_color.lower().strip()
        for normalized, aliases in COLOR_ALIASES.items():
            for alias in aliases:
                alias_lower = alias.lower()
                if lowered == alias_lower or alias_lower in lowered:
                    return normalized
        return None


# ----------------------------------------------------------------------
# Direct-run regression tests
# ----------------------------------------------------------------------
def _assert_equal(actual: Any, expected: Any, label: str):
    if actual != expected:
        raise AssertionError(f"{label} | expected={expected!r}, actual={actual!r}")


def _assert_tool_signature(actual_tools: Sequence[ToolSlotRecord], expected: Sequence[Tuple[str, str, int]], label: str):
    actual = [(tool.tool_type, tool.color.lower(), tool.slot_index) for tool in actual_tools]
    expected_norm = [(tool_type, color.lower(), slot_index) for tool_type, color, slot_index in expected]
    if actual != expected_norm:
        raise AssertionError(f"{label} | expected={expected_norm!r}, actual={actual!r}")


def _assert_painting_signature(actual_plan: Sequence[PaintingOperationPlan], expected: Sequence[Tuple[str, str, int, str]], label: str):
    actual = [
        (item.tool_type, item.color.lower(), item.matched_tool.slot_index, item.instruction_text)
        for item in actual_plan
    ]
    expected_norm = [(tool_type, color.lower(), slot_index, instruction) for tool_type, color, slot_index, instruction in expected]
    if actual != expected_norm:
        raise AssertionError(f"{label} | expected={expected_norm!r}, actual={actual!r}")


def _expect_planning_error(label: str, fn):
    try:
        fn()
    except PlanningError:
        print(f"✅ {label}")
        return
    raise AssertionError(f"{label} | expected PlanningError but no exception was raised")


def run_demo_tests():
    planner = LocalMaterialPlanner(enable_llm=False)
    passed = 0
    failed = 0

    def run_case(label: str, fn):
        nonlocal passed, failed
        try:
            fn()
            print(f"✅ {label}")
            passed += 1
        except Exception as exc:
            print(f"❌ {label}: {exc}")
            failed += 1

    run_case(
        "材料规划：普通汽车",
        lambda: (
            _assert_equal(planner.get_ai_material_plan("我想生产一辆汽车").target_product, "car", "target_product"),
            _assert_equal(planner.get_ai_material_plan("我想生产一辆汽车").selected_materials, ["car_chassis", "car_battery", "car_body"], "materials"),
        ),
    )

    run_case(
        "材料规划：红色手机",
        lambda: (
            _assert_equal(planner.get_ai_material_plan("我想生产一部红色手机").target_product, "phone", "target_product"),
            _assert_equal(planner.get_ai_material_plan("我想生产一部红色手机").selected_materials, ["phone_back", "phone_battery", "phone_screen"], "materials"),
        ),
    )

    run_case(
        "PAINTING：红色汽车 -> 红色喷涂头 2 号位",
        lambda: _assert_painting_signature(
            planner.get_ai_painting_plan("我想生产一辆红色汽车"),
            [("Wide_Spray_Nozzle", "red", 2, "红色整机喷涂信息")],
            "red car painting",
        ),
    )

    run_case(
        "PAINTING：黑色汽车 -> 黑色喷涂头 1 号位",
        lambda: _assert_painting_signature(
            planner.get_ai_painting_plan("我想生产一辆黑色汽车"),
            [("Wide_Spray_Nozzle", "black", 1, "黑色整机喷涂信息")],
            "black car painting",
        ),
    )

    run_case(
        "PAINTING：蓝色写字汽车 -> 蓝色细笔 5 号位",
        lambda: _assert_painting_signature(
            planner.get_ai_painting_plan("我想生产一辆用蓝色写LUCKY字样的汽车"),
            [("Fine_Point_Pen", "blue", 5, "蓝色写LUCKY字样信息")],
            "blue writing car painting",
        ),
    )

    run_case(
        "PAINTING：黑色描边汽车 -> 黑色细笔 3 号位",
        lambda: _assert_painting_signature(
            planner.get_ai_painting_plan("我想生产一辆用黑色描边的汽车"),
            [("Fine_Point_Pen", "black", 3, "黑色描边信息")],
            "black outline car painting",
        ),
    )

    run_case(
        "PAINTING：红色汽车 + 蓝色写字 -> 两套末端",
        lambda: _assert_painting_signature(
            planner.get_ai_painting_plan("我想生产一辆用蓝色写LUCKY字样的红色汽车"),
            [
                ("Wide_Spray_Nozzle", "red", 2, "红色整机喷涂信息"),
                ("Fine_Point_Pen", "blue", 5, "蓝色写LUCKY字样信息"),
            ],
            "mixed painting ops",
        ),
    )

    run_case(
        "PAINTING：红色手机 + 黑色描边 -> 两套末端",
        lambda: _assert_painting_signature(
            planner.get_ai_painting_plan("我想生产一部红色手机并用黑色描边"),
            [
                ("Wide_Spray_Nozzle", "red", 2, "红色整机喷涂信息"),
                ("Fine_Point_Pen", "black", 3, "黑色描边信息"),
            ],
            "phone mixed painting ops",
        ),
    )

    run_case(
        "工具接口：普通手机 -> 无末端工具",
        lambda: _assert_tool_signature(
            planner.get_ai_tool_plan("我想生产一部手机"),
            [],
            "plain phone tools",
        ),
    )

    run_case(
        "工具接口：红色汽车 + 蓝色写字",
        lambda: _assert_tool_signature(
            planner.get_ai_tool_plan("我想生产一辆用蓝色写LUCKY字样的红色汽车"),
            [
                ("Wide_Spray_Nozzle", "red", 2),
                ("Fine_Point_Pen", "blue", 5),
            ],
            "mixed tools",
        ),
    )

    run_case(
        "入库规划：汽车成品 -> (2, 2, 1)",
        lambda: _assert_equal(
            planner.get_ai_warehousing_plan("我想生产一辆红色汽车").coord,
            (2, 2, 1),
            "car warehousing coord",
        ),
    )

    run_case(
        "入库规划：手机成品 -> (1, 1, 3)",
        lambda: _assert_equal(
            planner.get_ai_warehousing_plan("我想生产一部红色手机").coord,
            (1, 1, 3),
            "phone warehousing coord",
        ),
    )

    run_case(
        "整体验证：红色汽车 + 蓝色写字",
        lambda: (
            _assert_equal(planner.build_physical_plan("我想生产一辆用蓝色写LUCKY字样的红色汽车").material_plan.selected_materials, ["car_chassis", "car_battery", "car_body"], "physical materials"),
            _assert_tool_signature(planner.build_physical_plan("我想生产一辆用蓝色写LUCKY字样的红色汽车").tool_plan, [("Wide_Spray_Nozzle", "red", 2), ("Fine_Point_Pen", "blue", 5)], "physical tools"),
            _assert_equal(planner.build_physical_plan("我想生产一辆用蓝色写LUCKY字样的红色汽车").warehousing_plan.coord, (2, 2, 1), "physical coord"),
        ),
    )

    run_case(
        "整体验证：普通手机无 PAINTING 动作",
        lambda: (
            _assert_equal(len(planner.build_physical_plan("我想生产一部手机").painting_plan), 0, "painting_plan length"),
            _assert_equal(planner.build_physical_plan("我想生产一部手机").warehousing_plan.coord, (1, 1, 3), "plain phone coord"),
        ),
    )

    run_case(
        "异常验证：只有喷涂但未写颜色",
        lambda: _expect_planning_error(
            "surface missing color - spray",
            lambda: planner.get_ai_painting_plan("我想生产一辆需要喷涂的汽车"),
        ),
    )

    run_case(
        "异常验证：只有写字但未写颜色",
        lambda: _expect_planning_error(
            "surface missing color - writing",
            lambda: planner.get_ai_painting_plan("我想生产一辆写LUCKY字样的汽车"),
        ),
    )

    print("\n" + "=" * 68)
    print(f"match_agent 回归测试完成：通过 {passed} 项 | 失败 {failed} 项")
    print("=" * 68)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    run_demo_tests()
