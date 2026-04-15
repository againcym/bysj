import json
import re
import xml.etree.ElementTree as ET
from collections import OrderedDict
from typing import Any, Dict, Optional


class OperationContextBuilder:
    """
    从 PPR step 中提取“执行参数”并并行输出 operation_context.json。

    设计原则：
    1. 不改变现有 contract XML 结构；
    2. 不依赖 contract 的 Guarantee / Assumption 推理，只依赖 PPR 中稳定存在的业务信息；
    3. 同时提供稳定业务主键(context_id)和 contract 节点主键(operation_node_key)。
    """

    COLOR_MAP = OrderedDict([
        ("红色", "red"),
        ("蓝色", "blue"),
        ("黑色", "black"),
        ("白色", "white"),
        ("绿色", "green"),
        ("黄色", "yellow"),
        ("银色", "silver"),
        ("灰色", "gray"),
        ("紫色", "purple"),
        ("橙色", "orange"),
    ])

    def __init__(self, ppr_path: str, contract_output_path: str, context_output_path: str):
        self.ppr_path = ppr_path
        self.contract_output_path = contract_output_path
        self.context_output_path = context_output_path
        self.process_context: Dict[str, Dict[str, Any]] = OrderedDict()
        self.operation_context: Dict[str, Dict[str, Any]] = OrderedDict()
        self.node_key_index: Dict[str, str] = OrderedDict()
        self._runtime_state: Dict[str, Dict[str, Any]] = {}

    # ---------------------------------------------------------
    # 基础解析工具
    # ---------------------------------------------------------
    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        try:
            return int(str(value).strip())
        except Exception:
            return None

    @staticmethod
    def _clean_text(text: Optional[str]) -> str:
        return re.sub(r"\s+", " ", (text or "")).strip()

    @staticmethod
    def _split_csv(text: str):
        return [x.strip() for x in (text or "").split(",") if x and x.strip()]

    @staticmethod
    def _drop_none(obj: Any) -> Any:
        if isinstance(obj, dict):
            new_obj = {}
            for k, v in obj.items():
                cleaned = OperationContextBuilder._drop_none(v)
                if cleaned is None:
                    continue
                if cleaned == {}:
                    continue
                new_obj[k] = cleaned
            return new_obj
        if isinstance(obj, list):
            new_list = []
            for item in obj:
                cleaned = OperationContextBuilder._drop_none(item)
                if cleaned is None:
                    continue
                new_list.append(cleaned)
            return new_list
        return obj

    def _parse_location_tuple(self, raw_text: str) -> Optional[Dict[str, Any]]:
        if not raw_text:
            return None
        m = re.search(r"\(([^)]+)\)", raw_text)
        if not m:
            return None
        tokens = [x.strip() for x in m.group(1).split(",") if x.strip()]
        if len(tokens) != 3:
            return None

        first, second, third = tokens
        # 形如 (WS_01, 3, 1)
        if re.search(r"[A-Za-z_]", first):
            return {
                "warehouse": first,
                "level": self._safe_int(second),
                "slot": self._safe_int(third),
            }
        # 形如 (2, 2, 1)
        return {
            "x": self._safe_int(first),
            "y": self._safe_int(second),
            "z": self._safe_int(third),
        }

    def _parse_material_resource(self, material_resource_text: str) -> Dict[str, Any]:
        text = self._clean_text(material_resource_text)
        if not text:
            return {}

        if "@" in text:
            left, right = [x.strip() for x in text.split("@", 1)]
            parsed = {
                "material_name": left,
                "raw": text,
            }
            loc = self._parse_location_tuple(right)
            if loc:
                if "warehouse" in loc:
                    parsed["source_location"] = loc
                else:
                    parsed["location"] = loc
            return parsed

        return {
            "material_name": text,
            "raw": text,
        }

    def _parse_terminal_slot(self, step_desc: str) -> Optional[int]:
        m = re.search(r"末端工具\s*(\d+)", step_desc or "")
        if m:
            return self._safe_int(m.group(1))
        return None

    def _parse_painting_instruction(self, step_desc: str) -> Dict[str, Any]:
        step_desc = self._clean_text(step_desc)
        m = re.search(r"根据(.+?)信息进行涂装", step_desc)
        if not m:
            return {}

        phrase = self._clean_text(m.group(1))
        info: Dict[str, Any] = {
            "raw_instruction": phrase,
            "mode": "spray",
            "tool_type": "Wide_Spray_Nozzle",
        }

        for cn, en in self.COLOR_MAP.items():
            if cn in phrase:
                info["color_cn"] = cn
                info["color"] = en
                break

        if "整机" in phrase:
            info["scope"] = "whole_body"
        elif "局部" in phrase:
            info["scope"] = "partial"

        if "描边" in phrase:
            info["mode"] = "outline"
            info["outline"] = True
            info["tool_type"] = "Fine_Point_Pen"
        elif "写" in phrase or "字样" in phrase or "文字" in phrase:
            info["mode"] = "writing"
            info["tool_type"] = "Fine_Point_Pen"
            m_text = re.search(r"写(.+?)(?:字样|文字|内容)?$", phrase)
            if m_text:
                content = self._clean_text(m_text.group(1))
                if content:
                    info["text"] = content
        elif "喷涂" in phrase or "涂装" in phrase:
            info["mode"] = "spray"
            info["tool_type"] = "Wide_Spray_Nozzle"

        return info

    def _parse_warehousing_target(self, step_desc: str) -> Optional[Dict[str, Any]]:
        loc = self._parse_location_tuple(step_desc)
        if not loc:
            return None
        if all(k in loc for k in ("x", "y", "z")):
            return loc
        return None

    def _default_runtime(self) -> Dict[str, Any]:
        return {
            "arm5_current_slot": None,
            "arm5_current_tool_type": None,
        }

    # ---------------------------------------------------------
    # object / step context
    # ---------------------------------------------------------
    def ensure_process_context(self, obj: ET.Element) -> Dict[str, Any]:
        process_id = obj.get("id") or "UNKNOWN"
        if process_id in self.process_context:
            return self.process_context[process_id]

        hardware_text = self._clean_text(obj.findtext("Resource/Hardware_Resource", default=""))
        material_text = self._clean_text(obj.findtext("Resource/Material_Resource", default=""))
        product_class = self._clean_text(obj.findtext("Product/product_class", default=""))
        product_specific = self._clean_text(obj.findtext("Product/product_specific", default=""))
        from_cond = self._clean_text(obj.findtext("From/From_condition", default=""))
        to_cond = self._clean_text(obj.findtext("To/To_condition", default=""))

        context = {
            "process_id": process_id,
            "hardware_resource": self._split_csv(hardware_text),
            "material_resource": self._parse_material_resource(material_text),
            "product": {
                "product_class": product_class,
                "product_specific": product_specific,
            },
            "from_condition": from_cond,
            "to_condition": to_cond,
        }
        self.process_context[process_id] = context
        self._runtime_state[process_id] = self._default_runtime()
        return context

    def _build_payload(
        self,
        process_id: str,
        process_ctx: Dict[str, Any],
        step_id: str,
        step_name: str,
        step_desc: str,
        device_name: Optional[str],
        action_signal: Optional[str],
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        runtime = self._runtime_state.setdefault(process_id, self._default_runtime())
        material_ctx = process_ctx.get("material_resource", {}) or {}

        if process_id.startswith("RAW_MATERIAL_HANDLING"):
            if material_ctx:
                payload["material"] = {
                    "material_name": material_ctx.get("material_name"),
                    "source_location": material_ctx.get("source_location"),
                }

        if action_signal and action_signal.startswith("pickUpTerminal_"):
            slot = self._parse_terminal_slot(step_desc)
            painting_info = self._parse_painting_instruction(step_desc)
            tool_type = None
            if painting_info:
                tool_type = painting_info.get("tool_type")
            if not tool_type:
                tool_type = runtime.get("arm5_current_tool_type")
            runtime["arm5_current_slot"] = slot
            runtime["arm5_current_tool_type"] = tool_type
            payload["terminal"] = {
                "slot": slot,
                "tool_type": tool_type,
            }

        elif action_signal and action_signal.startswith("trackPainting_"):
            recipe = self._parse_painting_instruction(step_desc)
            if recipe:
                recipe["linked_terminal_slot"] = runtime.get("arm5_current_slot")
                if not recipe.get("tool_type") and runtime.get("arm5_current_tool_type"):
                    recipe["tool_type"] = runtime.get("arm5_current_tool_type")
                runtime["arm5_current_tool_type"] = recipe.get("tool_type")
                payload["painting_recipe"] = recipe

        elif action_signal and action_signal.startswith("putDownTerminal_"):
            slot = self._parse_terminal_slot(step_desc)
            if slot is None:
                slot = runtime.get("arm5_current_slot")
            payload["terminal"] = {
                "slot": slot,
                "tool_type": runtime.get("arm5_current_tool_type"),
            }
            runtime["arm5_current_slot"] = None
            runtime["arm5_current_tool_type"] = None

        if process_id == "FINISHED_PRODUCTS_WAREHOUSING" and (action_signal or "").startswith("inbound_"):
            target = self._parse_warehousing_target(step_desc)
            if target:
                payload["target_location"] = target

        if process_id == "WELDING" and (action_signal or "").startswith("trackWelding_"):
            payload["welding_recipe"] = {
                "raw_instruction": self._clean_text(step_desc),
                "mode": "track_welding",
            }

        return payload

    def _build_register_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        reg: Dict[str, Any] = {}

        material = payload.get("material") or {}
        if material.get("material_name"):
            reg["material_name"] = material["material_name"]
        src = material.get("source_location") or {}
        if "warehouse" in src:
            reg["source_warehouse"] = src.get("warehouse")
            reg["source_level"] = src.get("level")
            reg["source_slot"] = src.get("slot")

        terminal = payload.get("terminal") or {}
        if terminal.get("slot") is not None:
            reg["terminal_slot"] = terminal.get("slot")
        if terminal.get("tool_type"):
            reg["terminal_tool_type"] = terminal.get("tool_type")

        recipe = payload.get("painting_recipe") or {}
        if recipe:
            reg["paint_mode"] = recipe.get("mode")
            reg["paint_color"] = recipe.get("color")
            reg["paint_color_cn"] = recipe.get("color_cn")
            reg["paint_scope"] = recipe.get("scope")
            reg["paint_text"] = recipe.get("text")
            if recipe.get("outline") is not None:
                reg["paint_outline"] = recipe.get("outline")
            if recipe.get("linked_terminal_slot") is not None:
                reg["terminal_slot"] = recipe.get("linked_terminal_slot")
            if recipe.get("tool_type"):
                reg["terminal_tool_type"] = recipe.get("tool_type")

        target = payload.get("target_location") or {}
        if target:
            reg["target_x"] = target.get("x")
            reg["target_y"] = target.get("y")
            reg["target_z"] = target.get("z")

        welding = payload.get("welding_recipe") or {}
        if welding:
            reg["welding_mode"] = welding.get("mode")

        return self._drop_none(reg)

    def add_operation_context(
        self,
        obj: ET.Element,
        step: ET.Element,
        operation_node_key: str,
        device_name: Optional[str],
        action_signal: Optional[str],
        display_text: str,
    ) -> Optional[Dict[str, Any]]:
        process_ctx = self.ensure_process_context(obj)
        process_id = process_ctx["process_id"]
        step_id = str(step.get("id") or "")
        step_name = self._clean_text(step.findtext("step_name", default=""))
        step_desc = self._clean_text(step.findtext("step_desc", default=""))
        context_id = f"{process_id}__{step_id}"

        payload = self._build_payload(
            process_id=process_id,
            process_ctx=process_ctx,
            step_id=step_id,
            step_name=step_name,
            step_desc=step_desc,
            device_name=device_name,
            action_signal=action_signal,
        )
        register_payload = self._build_register_payload(payload)

        entry = {
            "context_id": context_id,
            "operation_node_key": str(operation_node_key),
            "process_id": process_id,
            "step_id": step_id,
            "step_name": step_name,
            "step_desc": step_desc,
            "device_name": device_name,
            "action_signal": action_signal,
            "display_text": display_text,
            "process_context": {
                "hardware_resource": process_ctx.get("hardware_resource", []),
                "product": process_ctx.get("product", {}),
                "from_condition": process_ctx.get("from_condition"),
                "to_condition": process_ctx.get("to_condition"),
            },
            "payload": payload,
            "register_payload": register_payload,
        }
        entry = self._drop_none(entry)
        self.operation_context[context_id] = entry
        self.node_key_index[str(operation_node_key)] = context_id
        return entry

    def save(self) -> str:
        data = {
            "schema_version": "1.0",
            "generator": "ppr_to_contract_converter_llm_primary.py",
            "ppr_source": self.ppr_path,
            "contract_output": self.contract_output_path,
            "process_context": self._drop_none(self.process_context),
            "operation_context": self._drop_none(self.operation_context),
            "node_key_index": self._drop_none(self.node_key_index),
        }
        with open(self.context_output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return self.context_output_path
