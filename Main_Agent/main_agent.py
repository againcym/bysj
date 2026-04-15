from __future__ import annotations

import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from docx import Document
from rdflib import Graph, Literal, Namespace, RDF, RDFS, URIRef

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from Config.paths import FACTORY_OWL, PPR_OUTPUT_XML, REQ_DOCX, ensure_output_dirs
from Main_Agent.match_agent import (
    LocalMaterialPlanner,
    PaintingOperationPlan,
    PhysicalPlan,
    PlanningError,
)


DTD_CONTENT = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE product_line [
    <!ELEMENT product_line (object+)>
    <!ELEMENT object (Resource,Process,Product,From,To)>
    <!ATTLIST object id ID #REQUIRED>
    <!ELEMENT Resource (Hardware_Resource, Material_Resource?)>
    <!ELEMENT Hardware_Resource (#PCDATA)>
    <!ELEMENT Material_Resource (#PCDATA)>
    <!ELEMENT Process (process_step+)>
    <!ELEMENT process_step (step_name, step_desc)>
    <!ATTLIST process_step id CDATA #REQUIRED>
    <!ELEMENT step_name (#PCDATA)>
    <!ELEMENT step_desc (#PCDATA)>
    <!ELEMENT Product (product_class,product_specific)>
    <!ELEMENT product_class (#PCDATA)>
    <!ELEMENT product_specific (#PCDATA)>
    <!ELEMENT From (From_id,From_condition)>
    <!ELEMENT From_id (#PCDATA)>
    <!ELEMENT From_condition (#PCDATA)>
    <!ELEMENT To (To_id,To_condition)>
    <!ELEMENT To_id (#PCDATA)>
    <!ELEMENT To_condition (#PCDATA)>
]>"""

RESOURCE_ID_MAP = {
    "仓库机器臂1": "ARM1",
    "转移机器臂2": "ARM2",
    "焊接机器臂3": "ARM3",
    "焊接机器臂4": "ARM4",
    "涂装机器臂5": "ARM5",
    "转移机器臂6": "ARM6",
    "仓库机器臂7": "ARM7",
    "传送带1": "ConveyorBelt1",
    "传送带2": "ConveyorBelt2",
    "相机": "Camera",
}

TASK_DEFAULTS = {
    "RAW_MATERIAL_HANDLING": "动子就位, 收到拿取原料信息",
    "WELDING": "动子就位, 收到焊接信息",
    "PAINTING": "动子就位, 收到涂装信息",
    "PHOTO_INSPECTION": "动子就位, 收到拍照检测信息",
    "FINISHED_PRODUCTS_WAREHOUSING": "动子就位, 收到成品入库信息",
}

COMPLETION_MAP = {
    "RAW_MATERIAL_HANDLING": "原料拿取完成",
    "WELDING": "焊接完成",
    "PAINTING": "喷涂完成",
    "PHOTO_INSPECTION": "拍照检测完成",
    "FINISHED_PRODUCTS_WAREHOUSING": "成品入库完成",
}

PRODUCT_SPEC_MAP = {
    "WELDING": "组件完成轨道焊接",
    "PAINTING": "已完成涂装",
    "PHOTO_INSPECTION": "已完成拍照检测",
    "FINISHED_PRODUCTS_WAREHOUSING": "已入库成品",
}

TITLE_TO_TASK = {
    "RAW MATERIAL HANDLING": "RAW_MATERIAL_HANDLING",
    "WELDING": "WELDING",
    "PAINTING": "PAINTING",
    "PHOTO INSPECTION": "PHOTO_INSPECTION",
    "FINISHED PRODUCTS WAREHOUSING": "FINISHED_PRODUCTS_WAREHOUSING",
}


@dataclass
class StationSpec:
    title: str
    base_id: str
    trigger_conditions: List[str] = field(default_factory=list)
    initial_positions: List[str] = field(default_factory=list)
    steps: List[str] = field(default_factory=list)
    resource_names: List[str] = field(default_factory=list)


@dataclass
class TaskSpec:
    base_id: str
    object_id: str
    material_name: Optional[str]
    station: StationSpec


class RequirementStationParser:
    TITLE_PATTERN = re.compile(r"^\s*([A-Za-z][A-Za-z\s]+?)\s*Station[:：]\s*$")

    def __init__(self, known_resources: Sequence[str]):
        self.known_resources = list(known_resources)

    def parse(self, doc_path: str) -> List[StationSpec]:
        doc = Document(doc_path)
        lines = [para.text.strip() for para in doc.paragraphs if para.text and para.text.strip()]

        stations: List[StationSpec] = []
        current_title: Optional[str] = None
        trigger_conditions: List[str] = []
        initial_positions: List[str] = []
        steps: List[str] = []
        current_section: Optional[str] = None

        def flush_current():
            nonlocal current_title, trigger_conditions, initial_positions, steps, current_section
            if not current_title:
                return
            base_id = self._title_to_task_id(current_title)
            station = StationSpec(
                title=current_title,
                base_id=base_id,
                trigger_conditions=trigger_conditions[:],
                initial_positions=initial_positions[:],
                steps=steps[:],
            )
            station.resource_names = self._extract_resource_names(station)
            stations.append(station)
            current_title = None
            trigger_conditions = []
            initial_positions = []
            steps = []
            current_section = None

        for line in lines:
            title_match = self.TITLE_PATTERN.match(line)
            if title_match:
                flush_current()
                current_title = title_match.group(1).strip()
                current_section = None
                continue

            if line.startswith("启动条件："):
                current_section = "trigger_conditions"
                extra = line[len("启动条件："):].strip()
                if extra:
                    trigger_conditions.append(extra)
                continue

            if line.startswith("初始位置："):
                current_section = "initial_positions"
                extra = line[len("初始位置："):].strip()
                if extra:
                    initial_positions.append(extra)
                continue

            if line.startswith("步骤："):
                current_section = "steps"
                extra = line[len("步骤："):].strip()
                if extra:
                    steps.append(extra)
                continue

            if current_section == "trigger_conditions":
                trigger_conditions.append(line)
            elif current_section == "initial_positions":
                initial_positions.append(line)
            elif current_section == "steps":
                steps.append(line)

        flush_current()
        return stations

    def _title_to_task_id(self, title: str) -> str:
        normalized = re.sub(r"\s+", " ", title.upper().strip())
        if normalized in TITLE_TO_TASK:
            return TITLE_TO_TASK[normalized]
        compact = normalized.replace("_", " ")
        if compact in TITLE_TO_TASK:
            return TITLE_TO_TASK[compact]
        raise ValueError(f"无法识别的 Station 标题: {title}")

    def _extract_resource_names(self, station: StationSpec) -> List[str]:
        ordered: List[str] = []
        for line in [*station.initial_positions, *station.steps]:
            for resource_name in self.known_resources:
                if resource_name in line and resource_name not in ordered:
                    ordered.append(resource_name)
        return ordered


class FactoryOntologyBridge:
    EX = Namespace("http://www.semanticweb.org/ontologies/factory#")

    PROCESS_CLASS_BY_TASK = {
        "RAW_MATERIAL_HANDLING": "PickupProcess",
        "WELDING": "WeldingProcess",
        "PAINTING": "PaintingProcess",
        "PHOTO_INSPECTION": "InspectionProcess",
        "FINISHED_PRODUCTS_WAREHOUSING": "WarehousingProcess",
    }

    PRODUCT_CLASS_BY_TASK = {
        "RAW_MATERIAL_HANDLING": "RawMaterial",
        "WELDING": "WeldingSemiProduct",
        "PAINTING": "PaintingSemiProduct",
        "PHOTO_INSPECTION": "InspectionSemiProduct",
        "FINISHED_PRODUCTS_WAREHOUSING": "FinishedProduct",
    }

    DEFAULT_PROCESS_BY_TASK = {
        "RAW_MATERIAL_HANDLING": "DefaultPickupProcess",
        "WELDING": "DefaultWeldingProcess",
        "PAINTING": "DefaultPaintingProcess",
        "PHOTO_INSPECTION": "DefaultInspectionProcess",
        "FINISHED_PRODUCTS_WAREHOUSING": "DefaultWarehousingProcess",
    }

    RESOURCE_LOCAL_BY_NAME = {
        resource_name: f"{resource_id}_Resource" for resource_name, resource_id in RESOURCE_ID_MAP.items()
    }

    def __init__(self, ontology_path: str):
        self.ontology_path = ontology_path
        self.graph = Graph()
        self.graph.parse(ontology_path)
        self.graph.bind("factory", self.EX)
        self.graph.bind("rdfs", RDFS)
        self.graph.bind("rdf", RDF)
        self._ensure_runtime_axioms()

    def _ensure_runtime_axioms(self):
        for task_id in self.DEFAULT_PROCESS_BY_TASK:
            self._ensure_process_individual(task_id)
            self._ensure_process_output(task_id)

    def _task_base(self, task_id: str) -> str:
        return "RAW_MATERIAL_HANDLING" if task_id.startswith("RAW_MATERIAL_HANDLING") else task_id

    def _uri(self, local_name: str) -> URIRef:
        return self.EX[local_name]

    def _ensure_process_individual(self, task_id: str) -> URIRef:
        task_base = self._task_base(task_id)
        local_name = self.DEFAULT_PROCESS_BY_TASK[task_base]
        process_uri = self._uri(local_name)
        process_class_uri = self._uri(self.PROCESS_CLASS_BY_TASK[task_base])
        if (process_uri, RDF.type, process_class_uri) not in self.graph:
            self.graph.add((process_uri, RDF.type, process_class_uri))
        return process_uri

    def _ensure_process_output(self, task_id: str):
        task_base = self._task_base(task_id)
        process_uri = self._ensure_process_individual(task_base)
        product_uri = self._uri(self.PRODUCT_CLASS_BY_TASK[task_base])
        predicate = self._uri("processToProduceProduct")
        if (process_uri, predicate, product_uri) not in self.graph:
            self.graph.add((process_uri, predicate, product_uri))

    def _ensure_resource_individual(self, resource_name: str) -> URIRef:
        local_name = self.RESOURCE_LOCAL_BY_NAME.get(resource_name)
        if not local_name:
            local_name = self._fallback_resource_local_name(resource_name)
        resource_uri = self._uri(local_name)
        resource_class_uri = self._uri("Resource")
        if (resource_uri, RDF.type, resource_class_uri) not in self.graph:
            self.graph.add((resource_uri, RDF.type, resource_class_uri))
        if not any(True for _ in self.graph.objects(resource_uri, RDFS.label)):
            self.graph.add((resource_uri, RDFS.label, Literal(resource_name, lang="zh")))
        return resource_uri

    def _fallback_resource_local_name(self, resource_name: str) -> str:
        compact = re.sub(r"[^A-Za-z0-9]+", "_", resource_name).strip("_")
        if compact:
            return f"Resource_{compact}"
        return f"Resource_{resource_name.encode('utf-8').hex()}"

    def register_station(self, station: StationSpec):
        process_uri = self._ensure_process_individual(station.base_id)
        enables_predicate = self._uri("resourceEnablesProcess")
        for resource_name in station.resource_names:
            resource_uri = self._ensure_resource_individual(resource_name)
            if (resource_uri, enables_predicate, process_uri) not in self.graph:
                self.graph.add((resource_uri, enables_predicate, process_uri))

    def register_stations(self, stations: Sequence[StationSpec]):
        for station in stations:
            self.register_station(station)

    def get_product_label_by_process(self, task_id: str) -> Optional[str]:
        task_base = self._task_base(task_id)
        process_uri = self._ensure_process_individual(task_base)
        predicate = self._uri("processToProduceProduct")
        for product_uri in self.graph.objects(process_uri, predicate):
            label = self._preferred_label(product_uri)
            if label:
                return label
        return None

    def validate_resources_for_task(self, task_id: str, candidate_resources: Sequence[str], station_resources: Sequence[str]) -> List[str]:
        task_base = self._task_base(task_id)
        process_uri = self._ensure_process_individual(task_base)
        enables_predicate = self._uri("resourceEnablesProcess")
        allowed_set = {
            self._preferred_label(resource_uri)
            for resource_uri in self.graph.subjects(enables_predicate, process_uri)
        }
        allowed_set.discard(None)

        ordered_source = list(candidate_resources) if candidate_resources else list(station_resources)
        validated = [resource for resource in ordered_source if resource in allowed_set]
        if validated:
            return validated

        fallback = [resource for resource in station_resources if resource in allowed_set]
        return fallback if fallback else list(station_resources)

    def _preferred_label(self, uri: URIRef) -> Optional[str]:
        zh_labels = [str(obj) for obj in self.graph.objects(uri, RDFS.label) if getattr(obj, "language", None) == "zh"]
        if zh_labels:
            return zh_labels[0]
        labels = [str(obj) for obj in self.graph.objects(uri, RDFS.label)]
        if labels:
            return labels[0]
        return self._local_name(uri)

    @staticmethod
    def _local_name(uri: URIRef) -> str:
        text = str(uri)
        return text.split("#")[-1].split("/")[-1]


class MainPlannerAgent:
    def __init__(self, planner: Optional[LocalMaterialPlanner] = None):
        self.planner = planner or LocalMaterialPlanner(enable_llm=True)
        self.req_path = str(REQ_DOCX)
        self.onto_path = str(FACTORY_OWL)
        self.station_parser = RequirementStationParser(known_resources=list(RESOURCE_ID_MAP.keys()))
        self.ontology = FactoryOntologyBridge(self.onto_path)

    def execute_workflow(self, user_order: str) -> str:
        print("\n" + "=" * 40 + f"\n🚀 启动全流程语义规划: {user_order}\n" + "=" * 40)

        stations = self.station_parser.parse(self.req_path)
        self.ontology.register_stations(stations)
        physical_plan = self.planner.build_physical_plan(user_order)
        self._validate_supported_order_semantics(physical_plan)
        tasks = self._expand_tasks(stations, physical_plan)
        output_path = self._build_ppr(tasks, physical_plan)
        print(f"\n✅ 生成成功：{output_path}")
        return output_path

    def _expand_tasks(self, stations: Sequence[StationSpec], physical_plan: PhysicalPlan) -> List[TaskSpec]:
        tasks: List[TaskSpec] = []
        materials = physical_plan.material_plan.selected_materials
        needs_painting = len(physical_plan.painting_plan) > 0
        for station in stations:
            if station.base_id == "PAINTING" and not needs_painting:
                continue
            if station.base_id == "RAW_MATERIAL_HANDLING":
                for material in materials:
                    object_id = f"{station.base_id}_{material.split('_')[-1].upper()}"
                    tasks.append(TaskSpec(
                        base_id=station.base_id,
                        object_id=object_id,
                        material_name=material,
                        station=station,
                    ))
            else:
                tasks.append(TaskSpec(
                    base_id=station.base_id,
                    object_id=station.base_id,
                    material_name=None,
                    station=station,
                ))
        return tasks

    def _build_ppr(self, tasks: Sequence[TaskSpec], physical_plan: PhysicalPlan) -> str:
        root = ET.Element("product_line")
        current_state_label = "原料"
        accumulated_materials: List[str] = []
        ware_coord_str = physical_plan.warehousing_plan.coord_str

        for task in tasks:
            station = task.station
            material_name = task.material_name
            material_coord_str = self._material_coord_str(material_name, physical_plan)
            validated_resources = self.ontology.validate_resources_for_task(
                task.base_id,
                candidate_resources=station.resource_names,
                station_resources=station.resource_names,
            )
            hardware_resource_text = ", ".join(validated_resources)

            if task.base_id == "RAW_MATERIAL_HANDLING":
                if not material_name:
                    raise PlanningError("原料任务缺少 material_name。")
                accumulated_materials.append(material_name)
                material_resource_text = f"{material_name} @ {material_coord_str}"
                product_class = self.ontology.get_product_label_by_process(task.base_id) or "原料"
                product_specific = ",".join(accumulated_materials)
            else:
                material_resource_text = current_state_label
                product_class = self.ontology.get_product_label_by_process(task.base_id) or current_state_label
                product_specific = PRODUCT_SPEC_MAP.get(task.base_id, "")

            from_condition = self._build_from_condition(station)
            process_steps = self._build_process_steps(
                task=task,
                physical_plan=physical_plan,
                material_coord_str=material_coord_str,
                ware_coord_str=ware_coord_str,
            )

            object_elem = ET.SubElement(root, "object", {"id": task.object_id})
            resource_elem = ET.SubElement(object_elem, "Resource")
            ET.SubElement(resource_elem, "Hardware_Resource").text = hardware_resource_text
            ET.SubElement(resource_elem, "Material_Resource").text = material_resource_text

            process_elem = ET.SubElement(object_elem, "Process")
            for step_index, step in enumerate(process_steps, start=1):
                process_step = ET.SubElement(process_elem, "process_step", {"id": str(step_index)})
                ET.SubElement(process_step, "step_name").text = step["name"]
                ET.SubElement(process_step, "step_desc").text = step["desc"]

            product_elem = ET.SubElement(object_elem, "Product")
            ET.SubElement(product_elem, "product_class").text = product_class
            ET.SubElement(product_elem, "product_specific").text = product_specific

            from_elem = ET.SubElement(object_elem, "From")
            ET.SubElement(from_elem, "From_id").text = "Mover"
            ET.SubElement(from_elem, "From_condition").text = from_condition

            to_elem = ET.SubElement(object_elem, "To")
            ET.SubElement(to_elem, "To_id").text = "Mover"
            ET.SubElement(to_elem, "To_condition").text = COMPLETION_MAP[task.base_id]

            current_state_label = product_class

        ensure_output_dirs()
        tree = ET.ElementTree(root)
        ET.indent(tree, space="    ")
        xml_body = ET.tostring(root, encoding="unicode")
        output_path = str(PPR_OUTPUT_XML)
        with open(output_path, "w", encoding="utf-8") as file:
            file.write(f"{DTD_CONTENT}\n{xml_body}")
        return output_path

    def _material_coord_str(self, material_name: Optional[str], physical_plan: PhysicalPlan) -> str:
        if not material_name:
            return ""
        slot = physical_plan.material_plan.primary_locations.get(material_name)
        if not slot:
            raise PlanningError(f"原料 '{material_name}' 没有关联的主仓位。")
        return slot.material_coord_str()

    def _validate_supported_order_semantics(self, physical_plan: PhysicalPlan):
        for item in physical_plan.painting_plan:
            if not item.instruction_text.strip():
                raise PlanningError("检测到 PAINTING 动作，但 painting instruction_text 为空。")
            if item.matched_tool.slot_index <= 0:
                raise PlanningError("检测到非法的末端槽位编号。")

    def _build_from_condition(self, station: StationSpec) -> str:
        conditions = [self._clean_condition_text(text) for text in station.trigger_conditions if self._clean_condition_text(text)]
        if conditions:
            return ", ".join(conditions)
        return TASK_DEFAULTS[station.base_id]

    @staticmethod
    def _clean_condition_text(text: str) -> str:
        cleaned = re.sub(r"[\n\r]+", ", ", str(text).strip())
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,")
        return cleaned

    def _build_process_steps(
        self,
        task: TaskSpec,
        physical_plan: PhysicalPlan,
        material_coord_str: str,
        ware_coord_str: str,
    ) -> List[Dict[str, str]]:
        if task.base_id == "PAINTING":
            return self._build_painting_process_steps(physical_plan.painting_plan)

        steps: List[Dict[str, str]] = []
        for step_index, raw_step in enumerate(task.station.steps, start=1):
            desc = self._normalize_step_desc(
                task.base_id,
                step_index,
                raw_step,
                material_coord_str,
                ware_coord_str,
            )
            name = self._summarize_step_name(task.base_id, task.material_name, step_index, desc)
            steps.append({"name": name, "desc": desc})
        return steps

    def _build_painting_process_steps(self, painting_plan: Sequence[PaintingOperationPlan]) -> List[Dict[str, str]]:
        if not painting_plan:
            raise PlanningError("PAINTING 已进入展开阶段，但 painting_plan 为空。")

        steps: List[Dict[str, str]] = []
        for item in painting_plan:
            tool_label = item.tool_label
            steps.append({
                "name": "更换工具",
                "desc": f"涂装机器臂5更换{tool_label}",
            })
            steps.append({
                "name": "执行涂装",
                "desc": f"涂装机器臂5根据{item.instruction_text}进行涂装",
            })
            steps.append({
                "name": "放回工具",
                "desc": f"涂装机器臂5放回{tool_label}",
            })
        steps.append({
            "name": "返回初始位",
            "desc": "涂装机器臂5回到初始位置",
        })
        return steps

    def _normalize_step_desc(
        self,
        task_base_id: str,
        step_index: int,
        raw_step: str,
        material_coord_str: str,
        ware_coord_str: str,
    ) -> str:
        desc = raw_step.strip().replace("\n", " ")

        if task_base_id == "PHOTO_INSPECTION" and desc == "相机对半成品拍照":
            desc = "相机对半成品进行拍照"

        if task_base_id == "RAW_MATERIAL_HANDLING" and step_index == 1 and material_coord_str:
            desc = desc.replace("原料", f"原料{material_coord_str}", 1)

        if task_base_id != "RAW_MATERIAL_HANDLING":
            replacement = "成品" if task_base_id == "FINISHED_PRODUCTS_WAREHOUSING" else "组件"
            desc = desc.replace("原料", replacement)

        if task_base_id == "FINISHED_PRODUCTS_WAREHOUSING" and "入库" in desc and ware_coord_str not in desc:
            desc = f"{desc}，至仓库位置{ware_coord_str}"

        return desc

    def _summarize_step_name(
        self,
        task_base_id: str,
        material_name: Optional[str],
        step_index: int,
        step_desc: str,
    ) -> str:
        text = step_desc

        if task_base_id == "RAW_MATERIAL_HANDLING" and step_index == 4 and material_name and material_name.endswith("body"):
            return "传送带停止"

        if "出库" in text and "传送带" in text:
            return "原料出库"
        if "转移至动子" in text:
            return "转移原料"
        if "将成品转移至传送带" in text or "转移至传送带2" in text:
            return "转移成品"
        if "回到初始位置" in text:
            if task_base_id == "FINISHED_PRODUCTS_WAREHOUSING":
                return "返回初始位" if step_index >= 6 else "返回初始"
            return "返回初始"
        if "正转启动" in text:
            return "传送带启动"
        if "反转启动" in text:
            return "启动传送带"
        if "被检测到时" in text and "停止" in text:
            if task_base_id == "FINISHED_PRODUCTS_WAREHOUSING":
                return "检测并停止"
            return "检测停止"
        if "轨道焊接" in text:
            return "轨道焊接"
        if "拍照" in text:
            return "拍照"
        if "入库" in text and "仓库" in text:
            return "仓库入库"
        return f"步骤{step_index}"


if __name__ == "__main__":
    agent = MainPlannerAgent(planner=LocalMaterialPlanner(enable_llm=False))
    try:
        agent.execute_workflow("我想生产一辆红色汽车")
    except Exception as exc:
        print(f"❌ {exc}")
        raise
