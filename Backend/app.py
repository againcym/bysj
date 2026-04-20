
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import traceback
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Generator, Iterable, List, Optional, Tuple

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONTRACTS_DIR = PROJECT_ROOT / "Transform" / "contracts"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(CONTRACTS_DIR) not in sys.path:
    sys.path.insert(0, str(CONTRACTS_DIR))

from Integration import FactoryIOModbusService, ModbusAdapterError

STATIC_DIR = PROJECT_ROOT / "Backend" / "static"
INDEX_HTML = STATIC_DIR / "index.html"
TOOL1118_DIR = Path(r"C:\Users\11769\Desktop\festo\tool1118-V3\tool1118")
TOOL1118_START_EXE = TOOL1118_DIR / "start.exe"
TOOL1118_SERVER_PUBLIC_RESULT_XML = TOOL1118_DIR / "server" / "public" / "result.xml"
TOOL1118_VUE_ASSET_RESULT_XML = TOOL1118_DIR / "vue-cbt-reconstruction" / "src" / "assets" / "result.xml"
FACTORYIO_DEFAULT_HOST = "127.0.0.1"
FACTORYIO_DEFAULT_PORT = 502
FACTORYIO_DEFAULT_UNIT_ID = 1
FACTORYIO_DEFAULT_TIMEOUT = 2.0

MODULE_IMPORT_ERROR: Optional[BaseException] = None

# ---------------------------------------------------------------------------
# Safe imports from project
# ---------------------------------------------------------------------------
try:
    from Config.paths import (
        CONTRACT_OUTPUT_LLMMAIN_XML,
        FACTORY_OWL,
        OUTPUTS_DIR,
        OUTPUT_SM_DIR,
        PPR_OUTPUT_XML,
        PIN_TABLE_XLSX,
        REQ_DOCX,
        RULES_CONFIG_JSON,
        SIGNAL_OUTPUT_XML,
        ensure_output_dirs,
    )
    try:
        from Config.paths import OUTPUT_CONTRACT_DIR  # type: ignore
    except Exception:
        OUTPUT_CONTRACT_DIR = OUTPUTS_DIR / "contract"
    try:
        from Config.paths import OPERATION_CONTEXT_JSON  # type: ignore
    except Exception:
        OPERATION_CONTEXT_JSON = OUTPUT_CONTRACT_DIR / "operation_context.json"
except Exception as exc:
    MODULE_IMPORT_ERROR = exc
    OUTPUTS_DIR = PROJECT_ROOT / "outputs"
    OUTPUT_SM_DIR = OUTPUTS_DIR / "Device_OWLs_demo_exact"
    OUTPUT_CONTRACT_DIR = OUTPUTS_DIR / "contract"
    REQ_DOCX = PROJECT_ROOT / "Requirement" / "requirement.docx"
    FACTORY_OWL = PROJECT_ROOT / "Requirement" / "factory_final_logic.owl"
    PPR_OUTPUT_XML = OUTPUTS_DIR / "PPR_Final_logic.xml"
    SIGNAL_OUTPUT_XML = OUTPUTS_DIR / "Signal_Definition.xml"
    PIN_TABLE_XLSX = PROJECT_ROOT / "Transform" / "signals" / "signal_pin_table_from_manual.xlsx"
    RULES_CONFIG_JSON = PROJECT_ROOT / "Transform" / "config" / "rules_config.json"
    CONTRACT_OUTPUT_LLMMAIN_XML = OUTPUT_CONTRACT_DIR / "output_contract_llmmain.xml"
    OPERATION_CONTEXT_JSON = OUTPUT_CONTRACT_DIR / "operation_context.json"

    def ensure_output_dirs():
        OUTPUTS_DIR.mkdir(exist_ok=True)
        OUTPUT_CONTRACT_DIR.mkdir(exist_ok=True)
        OUTPUT_SM_DIR.mkdir(exist_ok=True)

try:
    from rdflib import RDF, RDFS, URIRef  # type: ignore
except Exception:
    RDF = None  # type: ignore
    RDFS = None  # type: ignore
    URIRef = None  # type: ignore

try:
    from Main_Agent.main_agent import MainPlannerAgent  # type: ignore
    from Main_Agent.match_agent import LocalMaterialPlanner  # type: ignore
    from Transform.contracts.ppr_to_contract_converter_llm_primary import (  # type: ignore
        PPRToContractConverterDemoLLM,
    )
    from Transform.signals.signal_definition_generate import (  # type: ignore
        build_xml as build_signal_xml,
    )
    from Transform.signals.signal_definition_generate import (  # type: ignore
        indent as indent_signal_xml,
    )
    from Transform.signals.signal_definition_generate import (  # type: ignore
        parse_table as parse_signal_table,
    )
    from Transform.state_machines.state_machines_demo_exact_generate import (  # type: ignore
        TemplateChooser,
        generate_one,
    )
    from Transform.state_machines.state_machines_demo_exact_generate import (  # type: ignore
        indent as indent_owl,
    )
except Exception as exc:
    if MODULE_IMPORT_ERROR is None:
        MODULE_IMPORT_ERROR = exc

OWL_NS = {
    "owl": "http://www.w3.org/2002/07/owl#",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
}

DEFAULT_STAGE_META = [
    ("requirement_template", "需求模板解析"),
    ("physical_grounding", "物理资源匹配"),
    ("ontology_reasoning", "ontology 约束与语义校验"),
    ("ppr_generation", "PPR 生成"),
    ("state_machine_generation", "状态机生成"),
    ("contract_process_entries", "Contract / Process 层推理"),
    ("contract_step_mapping", "Contract / Step 映射"),
    ("contract_link_reasoning", "Contract / Operation Link 推理"),
]

# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class PipelineRequest(BaseModel):
    order: str = Field(..., min_length=1, description="用户订单文本")
    use_llm: bool = Field(True, description="若环境可用，允许调用 LLM 辅助")


class FactoryIOConnectionRequest(BaseModel):
    host: str = Field(FACTORYIO_DEFAULT_HOST, description="Factory I/O Modbus TCP host")
    port: int = Field(FACTORYIO_DEFAULT_PORT, ge=1, le=65535, description="Factory I/O Modbus TCP port")
    unit_id: int = Field(FACTORYIO_DEFAULT_UNIT_ID, ge=0, le=255, description="Modbus unit id")
    timeout: float = Field(FACTORYIO_DEFAULT_TIMEOUT, gt=0.1, le=30.0, description="Socket timeout in seconds")


class FactoryIOReadRequest(FactoryIOConnectionRequest):
    signal_names: List[str] = Field(default_factory=list, description="Signals to read; empty means read by type/all")
    signal_type: Optional[str] = Field(None, description="Optional filter: Input or Output")


class FactoryIOWriteRequest(FactoryIOConnectionRequest):
    signal_name: str = Field(..., min_length=1, description="Output signal to write")
    value: bool = Field(True, description="Target output value")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Software-defined Automation Demo UI", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))


def _build_factoryio_service(req: Optional[FactoryIOConnectionRequest] = None) -> FactoryIOModbusService:
    config = req or FactoryIOConnectionRequest()
    return FactoryIOModbusService(
        signal_definition_path=Path(SIGNAL_OUTPUT_XML),
        host=config.host,
        port=config.port,
        unit_id=config.unit_id,
        timeout=config.timeout,
    )


@app.get("/api/examples")
def examples() -> Dict[str, List[str]]:
    return {
        "examples": [
            "我想生产一辆红色汽车",
            "我想生产一辆用蓝色写LUCKY字样的红色汽车",
            "我想生产一部手机",
            "我想生产一部红色手机并用黑色描边",
        ]
    }


@app.get("/api/download/{artifact_name}")
def download_artifact(artifact_name: str):
    artifact_map = {
        "ppr": PPR_OUTPUT_XML,
        "signal": SIGNAL_OUTPUT_XML,
        "contract": CONTRACT_OUTPUT_LLMMAIN_XML,
        "operation_context": OPERATION_CONTEXT_JSON,
    }
    path = artifact_map.get(artifact_name)
    if not path or not Path(path).exists():
        return JSONResponse(status_code=404, content={"ok": False, "error": f"未找到制品: {artifact_name}"})
    media_type = "application/octet-stream"
    suffix = Path(path).suffix.lower()
    if suffix == ".xml":
        media_type = "application/xml"
    elif suffix == ".json":
        media_type = "application/json"
    return FileResponse(path, media_type=media_type, filename=Path(path).name)


@app.get("/api/factoryio/config")
def factoryio_config() -> JSONResponse:
    try:
        service = _build_factoryio_service()
        return JSONResponse(
            {
                "ok": True,
                "config": service.describe(),
                "signal_definition_exists": Path(SIGNAL_OUTPUT_XML).exists(),
            }
        )
    except ModbusAdapterError as exc:
        return JSONResponse(status_code=404, content={"ok": False, "error": str(exc)})
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(exc), "traceback": traceback.format_exc()},
        )


@app.post("/api/factoryio/test-connection")
def factoryio_test_connection(req: FactoryIOConnectionRequest) -> JSONResponse:
    try:
        service = _build_factoryio_service(req)
        return JSONResponse({"ok": True, "result": service.test_connection(), "config": service.describe()})
    except ModbusAdapterError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(exc), "traceback": traceback.format_exc()},
        )


@app.post("/api/factoryio/signal-mappings")
def factoryio_signal_mappings(req: FactoryIOReadRequest) -> JSONResponse:
    try:
        service = _build_factoryio_service(req)
        return JSONResponse(
            {
                "ok": True,
                "config": service.describe(),
                "signals": service.list_signals(signal_type=req.signal_type),
            }
        )
    except ModbusAdapterError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(exc), "traceback": traceback.format_exc()},
        )


@app.post("/api/factoryio/read-signals")
def factoryio_read_signals(req: FactoryIOReadRequest) -> JSONResponse:
    try:
        service = _build_factoryio_service(req)
        return JSONResponse(
            {
                "ok": True,
                "config": service.describe(),
                "signals": service.read_signals(
                    signal_names=req.signal_names or None,
                    signal_type=req.signal_type,
                ),
            }
        )
    except ModbusAdapterError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(exc), "traceback": traceback.format_exc()},
        )


@app.post("/api/factoryio/write-signal")
def factoryio_write_signal(req: FactoryIOWriteRequest) -> JSONResponse:
    try:
        service = _build_factoryio_service(req)
        return JSONResponse(
            {
                "ok": True,
                "config": service.describe(),
                "signal": service.write_output_signal(req.signal_name, req.value),
            }
        )
    except ModbusAdapterError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(exc), "traceback": traceback.format_exc()},
        )


@app.post("/api/open-contract")
def open_contract_viewer() -> JSONResponse:
    contract_path = Path(CONTRACT_OUTPUT_LLMMAIN_XML)
    if not contract_path.exists():
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "error": f"未找到 Contract 文件: {contract_path}",
            },
        )
    if not TOOL1118_START_EXE.exists():
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "error": f"未找到启动程序: {TOOL1118_START_EXE}",
            },
        )

    synced_paths: List[str] = []
    sync_errors: List[str] = []
    for target in (TOOL1118_SERVER_PUBLIC_RESULT_XML, TOOL1118_VUE_ASSET_RESULT_XML):
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(contract_path, target)
            synced_paths.append(str(target))
        except Exception as exc:
            sync_errors.append(f"{target}: {exc}")

    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    try:
        subprocess.Popen(
            [str(TOOL1118_START_EXE)],
            cwd=str(TOOL1118_DIR),
            creationflags=creationflags,
        )
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": f"启动 start.exe 失败: {exc}",
            },
        )

    return JSONResponse(
        content={
            "ok": True,
            "message": "已启动 Contract 可视化工具。",
            "contract_path": str(contract_path),
            "synced_paths": synced_paths,
            "sync_errors": sync_errors,
        }
    )


@app.post("/api/pipeline")
def run_pipeline(req: PipelineRequest) -> JSONResponse:
    try:
        final_result = None
        for event in iter_pipeline_events(req.order.strip(), use_llm=req.use_llm):
            if event.get("event") == "done":
                final_result = event.get("result")
        if final_result is None:
            raise RuntimeError("流水线未返回最终结果。")
        return JSONResponse({"ok": True, **final_result})
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            },
        )


@app.post("/api/pipeline/stream")
def stream_pipeline(req: PipelineRequest) -> StreamingResponse:
    def event_stream() -> Generator[str, None, None]:
        try:
            for event in iter_pipeline_events(req.order.strip(), use_llm=req.use_llm):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as exc:  # pragma: no cover - surfaced to UI
            yield json.dumps(
                {
                    "event": "error",
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                },
                ensure_ascii=False,
            ) + "\n"

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")


# ---------------------------------------------------------------------------
# Pipeline orchestration
# ---------------------------------------------------------------------------
def iter_pipeline_events(order: str, use_llm: bool = True) -> Generator[Dict[str, Any], None, None]:
    _require_modules()
    ensure_output_dirs()

    summary: Dict[str, Any] = {
        "order": order,
        "target_product": "待推理",
        "material_count": 0,
        "painting_operation_count": 0,
        "ppr_object_count": 0,
        "used_device_count": 0,
        "state_machine_count": 0,
        "process_contract_count": 0,
        "step_mapping_count": 0,
        "operation_link_count": 0,
        "contract_node_count": 0,
        "contract_link_count": 0,
    }
    visible_stages: List[Dict[str, Any]] = []

    yield {
        "event": "bootstrap",
        "summary": summary,
        "stage_order": [{"id": stage_id, "title": title} for stage_id, title in DEFAULT_STAGE_META],
    }

    yield {"event": "status", "stage_id": "requirement_template", "message": "正在解析需求模板中的工作站结构…"}
    planner = _make_planner(use_llm=use_llm)
    agent = _make_agent(planner)

    stations = agent.station_parser.parse(str(REQ_DOCX))
    agent.ontology.register_stations(stations)
    requirement_stage = build_requirement_stage(stations)
    visible_stages.append(requirement_stage)
    summary["station_count"] = len(requirement_stage["data"]["stations"])
    yield {"event": "stage", "stage": requirement_stage, "summary_patch": {"station_count": summary["station_count"]}}

    yield {"event": "status", "stage_id": "physical_grounding", "message": "正在匹配原料、喷涂末端与入库位置…"}
    physical_plan = planner.build_physical_plan(order)
    agent._validate_supported_order_semantics(physical_plan)
    physical_stage = build_physical_stage(physical_plan)
    visible_stages.append(physical_stage)
    summary.update(
        {
            "target_product": physical_plan.material_plan.target_product,
            "material_count": len(physical_plan.material_plan.selected_materials),
            "painting_operation_count": len(physical_plan.painting_plan),
        }
    )
    yield {
        "event": "stage",
        "stage": physical_stage,
        "summary_patch": {
            "target_product": summary["target_product"],
            "material_count": summary["material_count"],
            "painting_operation_count": summary["painting_operation_count"],
        },
    }

    yield {"event": "status", "stage_id": "ontology_reasoning", "message": "正在执行 ontology 约束检查与语义校验…"}
    tasks = agent._expand_tasks(stations, physical_plan)
    ontology_stage = build_ontology_stage(agent, tasks)
    visible_stages.append(ontology_stage)
    yield {"event": "stage", "stage": ontology_stage}

    yield {"event": "status", "stage_id": "ppr_generation", "message": "正在生成 PPR，并整理为可视化流程…"}
    ppr_path = Path(agent._build_ppr(tasks, physical_plan))
    ppr_objects = parse_ppr_xml(ppr_path)
    used_devices = extract_used_devices_from_ppr(ppr_objects)
    ppr_stage = build_ppr_stage(ppr_objects)
    visible_stages.append(ppr_stage)
    summary["ppr_object_count"] = len(ppr_objects)
    summary["used_device_count"] = len(used_devices)
    yield {
        "event": "stage",
        "stage": ppr_stage,
        "summary_patch": {
            "ppr_object_count": summary["ppr_object_count"],
            "used_device_count": summary["used_device_count"],
        },
    }

    # Internal-only stage: signal definition
    yield {"event": "status", "stage_id": "state_machine_generation", "message": "正在生成 Signal_Definition（内部依赖，不在主视图单独展示）…"}
    signal_xml_path = generate_signal_definition()

    yield {"event": "status", "stage_id": "state_machine_generation", "message": "正在根据设备模板生成状态机…"}
    decisions, state_machines = generate_state_machines(signal_xml_path)
    state_machine_index = {item["device_name"]: item for item in state_machines}
    state_machine_stage = build_state_machine_stage(decisions, state_machines, used_devices)
    visible_stages.append(state_machine_stage)
    summary["state_machine_count"] = len(state_machine_stage["data"]["devices"])
    yield {
        "event": "stage",
        "stage": state_machine_stage,
        "summary_patch": {"state_machine_count": summary["state_machine_count"]},
    }

    yield {"event": "status", "stage_id": "contract_process_entries", "message": "正在生成 Contract 的 Process 层入口语义…"}
    converter = PPRToContractConverterDemoLLM(
        enable_llm=use_llm,
        enable_llm_process=False,
        enable_llm_operation=use_llm,
    )
    process_entries = build_process_entries_trace(converter)
    process_views = build_contract_process_views(
        ppr_objects=ppr_objects,
        process_entries=process_entries,
        step_mappings=[],
        operation_links=[],
        state_machine_index=state_machine_index,
    )
    process_stage = build_contract_process_stage(process_views)
    visible_stages.append(process_stage)
    summary["process_contract_count"] = len(process_views)
    yield {
        "event": "stage",
        "stage": process_stage,
        "summary_patch": {"process_contract_count": summary["process_contract_count"]},
    }

    yield {"event": "status", "stage_id": "contract_step_mapping", "message": "正在做步骤到设备 / 动作的映射…"}
    step_mappings = build_step_mappings_trace(converter)
    process_views_with_steps = build_contract_process_views(
        ppr_objects=ppr_objects,
        process_entries=process_entries,
        step_mappings=step_mappings,
        operation_links=[],
        state_machine_index=state_machine_index,
    )
    step_stage = build_contract_step_stage(process_views_with_steps)
    visible_stages.append(step_stage)
    summary["step_mapping_count"] = len(step_mappings)
    yield {
        "event": "stage",
        "stage": step_stage,
        "summary_patch": {"step_mapping_count": summary["step_mapping_count"]},
    }

    yield {"event": "status", "stage_id": "contract_link_reasoning", "message": "正在基于状态机 target/source states 推理 Operation Link…"}
    operation_links = build_operation_links_trace(converter, step_mappings, state_machine_index)
    process_views_full = build_contract_process_views(
        ppr_objects=ppr_objects,
        process_entries=process_entries,
        step_mappings=step_mappings,
        operation_links=operation_links,
        state_machine_index=state_machine_index,
    )
    link_stage = build_contract_link_stage(process_views_full)
    visible_stages.append(link_stage)
    summary["operation_link_count"] = len(operation_links)
    yield {
        "event": "stage",
        "stage": link_stage,
        "summary_patch": {"operation_link_count": summary["operation_link_count"]},
    }

    yield {"event": "status", "stage_id": "contract_link_reasoning", "message": "正在落盘最终 Contract XML 与 operation_context…"}
    contract_path = Path(converter.convert())
    contract_summary = parse_contract_xml(contract_path)
    operation_context = _load_json_file(Path(OPERATION_CONTEXT_JSON))
    summary["contract_node_count"] = contract_summary.get("node_count", 0)
    summary["contract_link_count"] = contract_summary.get("link_count", 0)

    final_result = {
        "summary": summary,
        "stages": visible_stages,
        "views": {
            "ppr_objects": ppr_objects,
            "state_machine_index": state_machine_index,
            "contract_summary": contract_summary,
            "operation_context": operation_context,
        },
        "artifacts": {
            "ppr_xml": _safe_read_text(Path(PPR_OUTPUT_XML)),
            "signal_xml": _safe_read_text(Path(SIGNAL_OUTPUT_XML)),
            "contract_xml": _safe_read_text(Path(CONTRACT_OUTPUT_LLMMAIN_XML)),
            "operation_context_json": json.dumps(operation_context, ensure_ascii=False, indent=2),
        },
    }
    yield {"event": "done", "result": final_result}


# ---------------------------------------------------------------------------
# Stage builders
# ---------------------------------------------------------------------------
def build_requirement_stage(stations: Iterable[Any]) -> Dict[str, Any]:
    station_cards = []
    for station in stations:
        station_cards.append(
            {
                "title": station.title,
                "task_id": station.base_id,
                "trigger_conditions": list(station.trigger_conditions),
                "initial_positions": list(station.initial_positions),
                "steps": list(station.steps),
                "resource_names": list(station.resource_names),
            }
        )
    return {
        "id": "requirement_template",
        "title": "需求模板解析",
        "phase": "需求 → PPR",
        "reasoning_badges": ["Station 模板", "步骤序列抽取", "工作站约束骨架"],
        "view_type": "requirement",
        "data": {
            "stations": station_cards,
            "requirement_doc": str(REQ_DOCX),
            "factory_ontology": str(FACTORY_OWL),
        },
    }




def build_physical_stage(physical_plan: Any) -> Dict[str, Any]:
    material_plan = physical_plan.material_plan
    painting_plan = list(getattr(physical_plan, "painting_plan", []) or [])
    warehousing_plan = physical_plan.warehousing_plan

    material_cards = []
    for material_name in material_plan.selected_materials:
        primary = _slot_to_dict(material_plan.primary_locations.get(material_name))
        material_cards.append(
            {
                "material_name": material_name,
                "chosen_location": primary,
                "chosen_label": humanize_slot(primary),
            }
        )

    painting_cards = []
    for plan in painting_plan:
        tool = _tool_to_dict(plan.matched_tool)
        tool_slot = tool.get("slot_index")
        fallback_tool_label = f"末端工具{tool_slot}" if tool_slot not in (None, "") else "末端工具"
        painting_cards.append(
            {
                "operation_kind": plan.operation_kind,
                "instruction_text": plan.instruction_text,
                "color": plan.color,
                "tool_type": plan.tool_type,
                "tool_station": tool.get("ts_id"),
                "tool_slot": tool_slot,
                "tool_desc": tool.get("description"),
                "tool_label": getattr(plan, "tool_label", fallback_tool_label),
            }
        )

    warehousing_data = {
        "item_type": warehousing_plan.item_type,
        "coord": list(warehousing_plan.coord),
        "coord_str": warehousing_plan.coord_str,
        "slot": _slot_to_dict(warehousing_plan.selected_slot),
        "slot_label": humanize_slot(_slot_to_dict(warehousing_plan.selected_slot)),
    }

    return {
        "id": "physical_grounding",
        "title": "物理资源匹配",
        "phase": "需求 → PPR",
        "reasoning_badges": ["Inventory", "Tool_Slots", "成品入库空位"],
        "view_type": "physical",
        "data": {
            "target_product": material_plan.target_product,
            "match_status": material_plan.match_status,
            "materials": material_cards,
            "painting_operations": painting_cards,
            "warehousing": warehousing_data,
        },
    }



def build_ontology_stage(agent: Any, tasks: Iterable[Any]) -> Dict[str, Any]:
    task_rows = []
    for task in tasks:
        validated_resources = list(
            agent.ontology.validate_resources_for_task(
                task.base_id,
                candidate_resources=task.station.resource_names,
                station_resources=task.station.resource_names,
            )
        )
        product_label = agent.ontology.get_product_label_by_process(task.base_id) or "未知产品"
        task_rows.append(
            {
                "task_id": task.base_id,
                "object_id": task.object_id,
                "station_title": task.station.title,
                "material_name": task.material_name,
                "validated_resources": validated_resources,
                "ontology_product_label": product_label,
            }
        )

    structure_graph = build_factory_owl_structure_graph()

    return {
        "id": "ontology_reasoning",
        "title": "ontology 约束与语义校验",
        "phase": "需求 → PPR",
        "reasoning_badges": ["OWL 结构", "resource 校验", "processToProduceProduct"],
        "view_type": "ontology",
        "data": {
            "tasks": task_rows,
            "structure_graph": structure_graph,
            "ontology_path": str(FACTORY_OWL),
        },
    }


def build_factory_owl_structure_graph() -> Dict[str, Any]:
    if RDF is None or RDFS is None or URIRef is None:
        return {"nodes": [], "edges": [], "title": "OWL 结构图", "layout": "absolute"}

    try:
        from rdflib import Graph  # type: ignore
    except Exception:
        return {"nodes": [], "edges": [], "title": "OWL 结构图", "layout": "absolute"}

    try:
        g = Graph()
        g.parse(str(FACTORY_OWL))
    except Exception:
        return {"nodes": [], "edges": [], "title": "OWL 结构图", "layout": "absolute"}

    owl_class = URIRef("http://www.w3.org/2002/07/owl#Class")
    owl_object_property = URIRef("http://www.w3.org/2002/07/owl#ObjectProperty")

    def local_name(uri: Any) -> str:
        return str(uri).split("#")[-1]

    def label_of(uri: Any) -> str:
        return _preferred_label(g, uri) or local_name(uri)

    class_nodes = {s for s, _, _ in g.triples((None, RDF.type, owl_class))}
    class_by_name = {local_name(uri): uri for uri in class_nodes}

    process_root = class_by_name.get("Process")
    product_root = class_by_name.get("Product")
    resource_root = class_by_name.get("Resource")
    agent_root = class_by_name.get("Agent")

    process_subclasses = sorted(
        [s for s, _, o in g.triples((None, RDFS.subClassOf, process_root))] if process_root is not None else [],
        key=lambda x: local_name(x),
    )
    product_subclasses = sorted(
        [s for s, _, o in g.triples((None, RDFS.subClassOf, product_root))] if product_root is not None else [],
        key=lambda x: local_name(x),
    )

    property_nodes = {local_name(p): p for p, _, _ in g.triples((None, RDF.type, owl_object_property))}

    def relation_label(name: str, fallback: str) -> str:
        prop = property_nodes.get(name)
        return label_of(prop) if prop is not None else fallback

    width = 1380
    height = 940
    nodes: List[Dict[str, Any]] = [
        {"id": "schema::PPR", "label": "PPR", "subtitle": "需求 → PPR", "kind": "ppr", "x": 560, "y": 18, "w": 160, "h": 62},
        {"id": "schema::Process", "label": label_of(process_root) if process_root is not None else "Process", "subtitle": ":Process", "kind": "process-root", "x": 585, "y": 210, "w": 210, "h": 72},
        {"id": "schema::Agent", "label": label_of(agent_root) if agent_root is not None else "Agent", "subtitle": ":Agent", "kind": "agent-root", "x": 622, "y": 430, "w": 132, "h": 56},
        {"id": "schema::Product", "label": label_of(product_root) if product_root is not None else "Product", "subtitle": ":Product", "kind": "product-root", "x": 210, "y": 610, "w": 200, "h": 72},
        {"id": "schema::Resource", "label": label_of(resource_root) if resource_root is not None else "Resource", "subtitle": ":Resource", "kind": "resource-root", "x": 980, "y": 610, "w": 200, "h": 72},
    ]

    process_positions = [(70, 72), (322, 72), (574, 72), (826, 72), (1078, 72)]
    for cls, (x, y) in zip(process_subclasses, process_positions):
        nodes.append(
            {
                "id": f"process::{local_name(cls)}",
                "label": label_of(cls),
                "subtitle": local_name(cls),
                "kind": "process-child",
                "x": x,
                "y": y,
                "w": 152,
                "h": 54,
            }
        )

    product_positions = [(18, 770), (226, 770), (434, 770), (642, 770), (850, 770)]
    for cls, (x, y) in zip(product_subclasses, product_positions):
        nodes.append(
            {
                "id": f"product::{local_name(cls)}",
                "label": label_of(cls),
                "subtitle": local_name(cls),
                "kind": "product-child",
                "x": x,
                "y": y,
                "w": 184,
                "h": 56,
            }
        )

    edges: List[Dict[str, Any]] = [
        {"from": "schema::PPR", "to": "schema::Process", "label": "contains", "curve": 0, "style": "solid"},
        {"from": "schema::PPR", "to": "schema::Agent", "label": "contains", "curve": 0, "style": "solid"},
        {"from": "schema::PPR", "to": "schema::Product", "label": "contains", "curve": -90, "style": "solid"},
        {"from": "schema::PPR", "to": "schema::Resource", "label": "contains", "curve": 90, "style": "solid"},
        {"from": "schema::Agent", "to": "schema::Process", "label": relation_label("plansProcess", "plansProcess"), "curve": 0, "style": "solid"},
        {"from": "schema::Product", "to": "schema::Agent", "label": relation_label("productReportsToAgent", "productReportsToAgent"), "curve": 48, "style": "solid"},
        {"from": "schema::Agent", "to": "schema::Resource", "label": relation_label("perceivesResource", "perceivesResource"), "curve": 36, "style": "solid"},
        {"from": "schema::Resource", "to": "schema::Process", "label": relation_label("resourceEnablesProcess", "resourceEnablesProcess"), "curve": -26, "style": "solid"},
        {"from": "schema::Process", "to": "schema::Product", "label": relation_label("processToProduceProduct", "processToProduceProduct"), "curve": -44, "style": "solid"},
        {"from": "schema::Product", "to": "schema::Resource", "label": relation_label("productIsProducedOnResource", "productIsProducedOnResource"), "curve": 0, "style": "solid"},
    ]

    for cls in process_subclasses:
        edges.append(
            {
                "from": f"process::{local_name(cls)}",
                "to": "schema::Process",
                "label": "subClassOf",
                "curve": 0,
                "style": "dashed",
            }
        )
    for cls in product_subclasses:
        edges.append(
            {
                "from": f"product::{local_name(cls)}",
                "to": "schema::Product",
                "label": "subClassOf",
                "curve": 0,
                "style": "dashed",
            }
        )

    return {
        "title": "OWL 结构图",
        "layout": "absolute",
        "width": width,
        "height": height,
        "nodes": nodes,
        "edges": edges,
    }

def build_ppr_stage(ppr_objects: List[Dict[str, Any]]) -> Dict[str, Any]:
    flow_cards = []
    for idx, obj in enumerate(ppr_objects, start=1):
        flow_cards.append(
            {
                "seq": idx,
                "object_id": obj["id"],
                "hardware_resources": split_csv(obj["hardware_resource"]),
                "material_resource": obj["material_resource"],
                "product_class": obj["product_class"],
                "product_specific": obj["product_specific"],
                "from_conditions": split_csv(obj["from_condition"]),
                "to_condition": obj["to_condition"],
                "steps": obj["steps"],
            }
        )
    return {
        "id": "ppr_generation",
        "title": "PPR 生成",
        "phase": "需求 → PPR",
        "reasoning_badges": ["PPR DTD 结构", "顺序工艺展开", "对象 / 步骤流程图"],
        "view_type": "ppr",
        "data": {
            "objects": flow_cards,
            "count": len(flow_cards),
        },
    }


def build_state_machine_stage(
    decisions: List[Dict[str, Any]],
    state_machines: List[Dict[str, Any]],
    used_devices: List[str],
) -> Dict[str, Any]:
    decision_by_device = {item["device_name"]: item for item in decisions}
    cards = []
    for sm in state_machines:
        device_name = sm["device_name"]
        used = device_name in used_devices
        cards.append(
            {
                "device_name": device_name,
                "used_in_pipeline": used,
                "template": sm.get("template"),
                "reason": sm.get("reason"),
                "state_count": sm.get("state_count"),
                "action_count": sm.get("action_count"),
                "states": sm.get("states", []),
                "actions": sm.get("actions", []),
            }
        )

    cards.sort(key=lambda item: (not item["used_in_pipeline"], item["device_name"]))
    return {
        "id": "state_machine_generation",
        "title": "状态机生成",
        "phase": "PPR → Contract",
        "reasoning_badges": ["模板选择", "OWL 状态图", "动作 / 状态边界"],
        "view_type": "state_machine",
        "data": {
            "devices": cards,
            "used_devices": used_devices,
        },
    }


def build_contract_process_stage(process_views: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "id": "contract_process_entries",
        "title": "Contract / Process 层推理",
        "phase": "PPR → Contract",
        "reasoning_badges": ["From_condition → Guarantee", "Hardware_Resource → Assumption", "Interface 反推"],
        "view_type": "contract_process",
        "data": {"process_views": process_views},
    }


def build_contract_step_stage(process_views: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "id": "contract_step_mapping",
        "title": "Contract / Step 映射",
        "phase": "PPR → Contract",
        "reasoning_badges": ["step_desc → device", "candidate output signals", "action_signal 选择"],
        "view_type": "contract_step",
        "data": {"process_views": process_views},
    }


def build_contract_link_stage(process_views: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "id": "contract_link_reasoning",
        "title": "Contract / Operation Link 推理",
        "phase": "PPR → Contract",
        "reasoning_badges": ["prev target states", "curr source states", "Guarantee / Assumption / Interface"],
        "view_type": "contract_link",
        "data": {"process_views": process_views},
    }


# ---------------------------------------------------------------------------
# Artifact generators
# ---------------------------------------------------------------------------
def generate_signal_definition() -> Path:
    devices = parse_signal_table(str(PIN_TABLE_XLSX), "Signal Pin Table")
    root = build_signal_xml(devices)
    indent_signal_xml(root)
    signal_path = Path(SIGNAL_OUTPUT_XML)
    ET.ElementTree(root).write(signal_path, encoding="utf-8", xml_declaration=True)
    return signal_path


def generate_state_machines(signal_xml_path: Path) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    ensure_output_dirs()
    signal_root = ET.parse(signal_xml_path).getroot()
    chooser = TemplateChooser(backend="none", verbose=False)

    decisions: List[Dict[str, Any]] = []
    summaries: List[Dict[str, Any]] = []

    for device in signal_root.findall("Device"):
        owl_root, decision = generate_one(device, chooser, strict_demo_quirks=True)
        device_name = device.get("name") or "Unknown"
        record = {
            "device_name": device_name,
            "template": decision.get("template"),
            "reason": decision.get("reason"),
        }
        decisions.append(record)
        if owl_root is None:
            continue
        indent_owl(owl_root)
        out_path = Path(OUTPUT_SM_DIR) / f"{device_name}_generated.owl"
        ET.ElementTree(owl_root).write(out_path, encoding="utf-8", xml_declaration=True)
        summaries.append(parse_state_machine_summary(out_path, record))

    return decisions, summaries


# ---------------------------------------------------------------------------
# Summaries / parsers
# ---------------------------------------------------------------------------
def parse_ppr_xml(path: Path) -> List[Dict[str, Any]]:
    root = ET.parse(path).getroot()
    objects: List[Dict[str, Any]] = []
    for obj in root.findall("object"):
        steps = []
        for step in obj.findall("Process/process_step"):
            steps.append(
                {
                    "id": step.get("id"),
                    "name": (step.findtext("step_name", default="") or "").strip(),
                    "desc": (step.findtext("step_desc", default="") or "").strip(),
                }
            )
        objects.append(
            {
                "id": obj.get("id"),
                "hardware_resource": (obj.findtext("Resource/Hardware_Resource", default="") or "").strip(),
                "material_resource": (obj.findtext("Resource/Material_Resource", default="") or "").strip(),
                "product_class": (obj.findtext("Product/product_class", default="") or "").strip(),
                "product_specific": (obj.findtext("Product/product_specific", default="") or "").strip(),
                "from_condition": (obj.findtext("From/From_condition", default="") or "").strip(),
                "to_condition": (obj.findtext("To/To_condition", default="") or "").strip(),
                "steps": steps,
            }
        )
    return objects


def parse_state_machine_summary(path: Path, decision: Dict[str, Any]) -> Dict[str, Any]:
    root = ET.parse(path).getroot()
    states = []
    actions = []

    for ind in root.findall("owl:NamedIndividual", OWL_NS):
        about = ind.get(f"{{{OWL_NS['rdf']}}}about", "")
        state_id = about.split("#")[-1]
        label = ind.findtext("rdfs:label", default="", namespaces=OWL_NS)
        transitions = []
        for child in ind:
            tag = child.tag.split("}")[-1]
            if tag in {"type", "label", "comment", "isAutomaticTransition"}:
                continue
            target = child.get(f"{{{OWL_NS['rdf']}}}resource")
            if target:
                transitions.append({"action": tag, "target": target.split("#")[-1]})
        states.append({"state_id": state_id, "label": label, "transitions": transitions})

    for prop in root.findall("owl:ObjectProperty", OWL_NS):
        about = prop.get(f"{{{OWL_NS['rdf']}}}about", "")
        label = prop.findtext("rdfs:label", default="", namespaces=OWL_NS)
        actions.append({"action": about.split("#")[-1], "label": label})

    return {
        "device_name": decision.get("device_name"),
        "template": decision.get("template"),
        "reason": decision.get("reason"),
        "state_count": len(states),
        "action_count": len(actions),
        "states": states,
        "actions": actions,
        "output_path": str(path),
    }


def parse_contract_xml(path: Path) -> Dict[str, Any]:
    root = ET.parse(path).getroot()
    nodes = root.findall("NodeArray/Node")
    links = root.findall("LinkArray/Link")
    return {
        "node_count": len(nodes),
        "link_count": len(links),
    }


# ---------------------------------------------------------------------------
# Contract trace builders
# ---------------------------------------------------------------------------
def build_process_entries_trace(converter: Any) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for obj in converter.ppr_root.findall("object"):
        process_id = obj.get("id") or "UNKNOWN"
        hardware_text = obj.findtext("Resource/Hardware_Resource", default="") or ""
        g_data, a_data, signal_map = converter.process_generator.build_process_entry_contract(obj)
        entries.append(
            {
                "process_id": process_id,
                "hardware_resource": split_csv(hardware_text),
                "from_condition": split_csv(obj.findtext("From/From_condition", default="") or ""),
                "guarantee": decorate_conditions(g_data),
                "assumption": decorate_conditions(a_data),
                "interface": decorate_interface_map(signal_map, converter),
            }
        )
    return entries


def build_step_mappings_trace(converter: Any) -> List[Dict[str, Any]]:
    step_mappings: List[Dict[str, Any]] = []
    resource_map = _load_resource_prefix_map()
    reverse_resource_map = {v: k for k, v in resource_map.items()}

    for obj in converter.ppr_root.findall("object"):
        process_id = obj.get("id") or "UNKNOWN"
        hardware_text = obj.findtext("Resource/Hardware_Resource", default="") or ""
        transport_pattern = converter.operation_reasoner.infer_transport_pattern(process_id, hardware_text)

        for step in obj.findall("Process/process_step"):
            step_id = step.get("id") or ""
            step_name = (step.findtext("step_name", default="") or "").strip()
            step_desc = (step.findtext("step_desc", default="") or "").strip()
            device_name = converter.operation_reasoner.resolve_device_from_step(step_desc)
            action_signal = converter.operation_reasoner.resolve_action_signal(
                step_desc,
                device_name,
                process_id=process_id,
                hardware_text=hardware_text,
            )
            display_text = converter.operation_reasoner.display_text(action_signal, device_name) if device_name and action_signal else ""
            candidate_outputs = []
            if device_name:
                for item in converter.signal_repo.get_device_output_summary(device_name):
                    desc = ""
                    selects = item.get("selects") or []
                    if selects:
                        desc = selects[0].get("desc") or ""
                    candidate_outputs.append(
                        {
                            "name": item.get("name"),
                            "display_desc": desc,
                            "address": item.get("address"),
                        }
                    )

            reasoning_lines = build_step_mapping_reasoning_lines(
                step_desc=step_desc,
                process_id=process_id,
                hardware_text=hardware_text,
                device_name=device_name,
                action_signal=action_signal,
                transport_pattern=transport_pattern,
                candidate_outputs=candidate_outputs,
                reverse_resource_map=reverse_resource_map,
            )

            step_mappings.append(
                {
                    "process_id": process_id,
                    "step_id": step_id,
                    "step_name": step_name,
                    "step_desc": step_desc,
                    "device_name": device_name,
                    "device_cn": reverse_resource_map.get(device_name, device_name),
                    "action_signal": action_signal,
                    "display_text": display_text,
                    "candidate_outputs": candidate_outputs,
                    "transport_pattern": transport_pattern,
                    "reasoning_lines": reasoning_lines,
                }
            )
    return step_mappings


def build_operation_links_trace(
    converter: Any,
    step_mappings: List[Dict[str, Any]],
    state_machine_index: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    operation_links: List[Dict[str, Any]] = []
    mappings_by_process: Dict[str, List[Dict[str, Any]]] = {}
    for item in step_mappings:
        mappings_by_process.setdefault(item["process_id"], []).append(item)

    for process_id, process_steps in mappings_by_process.items():
        prev_step = None
        for current in process_steps:
            if prev_step is None:
                prev_step = current
                continue
            if not prev_step.get("device_name") or not prev_step.get("action_signal") or not current.get("device_name") or not current.get("action_signal"):
                prev_step = current
                continue

            prev_action = prev_step["action_signal"]
            curr_action = current["action_signal"]
            prev_ctx = converter.state_repo.get_action_context(prev_action)
            curr_ctx = converter.state_repo.get_action_context(curr_action)

            prev_target = decorate_conditions(
                converter.operation_reasoner._collect_conditions_from_states(prev_ctx.get("target_states", []))
            )
            curr_source = decorate_conditions(
                converter.operation_reasoner._collect_conditions_from_states(curr_ctx.get("source_states", []))
            )
            final_g, final_a, final_signal_map = converter.operation_reasoner.reason_link_contract(
                prev_step_desc=prev_step["step_desc"],
                prev_device=prev_step["device_name"],
                prev_action=prev_action,
                curr_step_desc=current["step_desc"],
                curr_device=current["device_name"],
                curr_action=curr_action,
            )

            rule_label, rule_desc = infer_link_rule(prev_action, curr_action, prev_step["device_name"], current["device_name"])
            reasoning_lines = [
                f"上一动作 {prev_action} 的 target states 提供了 Guarantee 候选。",
                f"下一动作 {curr_action} 的 source states 提供了 Assumption 候选。",
            ]
            if rule_label:
                reasoning_lines.append(f"本条 link 命中规则：{rule_label}。{rule_desc}")
            reasoning_lines.append("最终 Interface 只从保留下来的 Guarantee / Assumption 条件反推。")

            operation_links.append(
                {
                    "process_id": process_id,
                    "from_step": prev_step,
                    "to_step": current,
                    "guide_rule_label": rule_label,
                    "guide_rule_desc": rule_desc,
                    "prev_target_conditions": prev_target,
                    "curr_source_conditions": curr_source,
                    "guarantee": decorate_conditions(final_g),
                    "assumption": decorate_conditions(final_a),
                    "interface": decorate_interface_map(final_signal_map, converter),
                    "reasoning_lines": reasoning_lines,
                    "prev_state_machine": state_machine_index.get(prev_step["device_name"]),
                    "curr_state_machine": state_machine_index.get(current["device_name"]),
                }
            )
            prev_step = current
    return operation_links


def build_contract_process_views(
    ppr_objects: List[Dict[str, Any]],
    process_entries: List[Dict[str, Any]],
    step_mappings: List[Dict[str, Any]],
    operation_links: List[Dict[str, Any]],
    state_machine_index: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    ppr_index = {item["id"]: item for item in ppr_objects}
    entry_index = {item["process_id"]: item for item in process_entries}

    steps_by_process: Dict[str, List[Dict[str, Any]]] = {}
    for item in step_mappings:
        steps_by_process.setdefault(item["process_id"], []).append(item)

    links_by_process: Dict[str, List[Dict[str, Any]]] = {}
    for item in operation_links:
        links_by_process.setdefault(item["process_id"], []).append(item)

    process_views: List[Dict[str, Any]] = []
    for ppr_object in ppr_objects:
        process_id = ppr_object["id"]
        entry = entry_index.get(process_id)
        process_views.append(
            {
                "process_id": process_id,
                "hardware_resources": split_csv(ppr_object["hardware_resource"]),
                "material_resource": ppr_object["material_resource"],
                "product_class": ppr_object["product_class"],
                "product_specific": ppr_object["product_specific"],
                "from_conditions": split_csv(ppr_object["from_condition"]),
                "to_condition": ppr_object["to_condition"],
                "steps": steps_by_process.get(process_id, []),
                "links": links_by_process.get(process_id, []),
                "process_entry": entry,
            }
        )
    return process_views


# ---------------------------------------------------------------------------
# UI helpers / explanation
# ---------------------------------------------------------------------------
def build_step_mapping_reasoning_lines(
    step_desc: str,
    process_id: str,
    hardware_text: str,
    device_name: Optional[str],
    action_signal: Optional[str],
    transport_pattern: Optional[Dict[str, Any]],
    candidate_outputs: List[Dict[str, Any]],
    reverse_resource_map: Dict[str, str],
) -> List[str]:
    lines: List[str] = []
    if device_name:
        cn_name = reverse_resource_map.get(device_name, device_name)
        lines.append(f"步骤文本中出现设备名「{cn_name}」，因此先锁定设备为 {device_name}。")
    else:
        lines.append("步骤文本中未能稳定识别设备名，需要人工检查。")

    if transport_pattern:
        flow = transport_pattern.get("flow_direction")
        if flow == "warehouse_to_mover":
            lines.append("该 process 落在 warehouse_to_mover 物流模式，动作会优先按 原料出库 单元解释。")
        elif flow == "mover_to_warehouse":
            lines.append("该 process 落在 mover_to_warehouse 物流模式，动作会优先按 成品入库 单元解释。")

    keyword_hits = [kw for kw in ("出库", "入库", "转移至动子", "转移至传送带", "正转", "反转", "停止", "更换工具", "放回工具", "焊接", "涂装", "拍照") if kw in step_desc]
    if keyword_hits:
        lines.append(f"动作语义主要由关键词触发：{' / '.join(keyword_hits)}。")

    if candidate_outputs:
        names = "、".join(item["name"] for item in candidate_outputs)
        lines.append(f"最终只在该设备的候选输出 [{names}] 内进行动作选择。")

    if action_signal:
        lines.append(f"最终映射结果：{action_signal}。")
    return lines


def infer_link_rule(prev_action: str, curr_action: str, prev_device: str, curr_device: str) -> Tuple[str, str]:
    if curr_action.startswith("reset_"):
        return (
            "reset link",
            "保持上一动作的完成效果作为 Guarantee，同时把“未回初始位”保留为当前 reset 的 Assumption。",
        )
    if (prev_action.startswith("forward_") or prev_action.startswith("backward_")) and curr_action.startswith("stop_"):
        return (
            "conveyor motion → stop",
            "把传送带正在运动视为 Guarantee，把目标端传感器被触发视为 Assumption。",
        )
    if prev_action.startswith("stop_") and (curr_action.startswith("moveOut_") or curr_action.startswith("inbound_")):
        return (
            "stop → next arm",
            "先保证传送带已停止，再要求下一个机械臂处于初始位。",
        )
    if prev_action.startswith("pickUpTerminal_") and curr_action.startswith("trackPainting_"):
        return (
            "ARM5 pickUp → trackPainting",
            "工具已拿起是最核心的 Guarantee，Assumption 会被最小化。",
        )
    if prev_action.startswith("trackPainting_") and curr_action.startswith("putDownTerminal_"):
        return (
            "ARM5 trackPainting → putDown",
            "喷涂完成是最核心的 Guarantee，Assumption 会被最小化。",
        )
    if prev_action.startswith("putDownTerminal_") and curr_action.startswith("reset_"):
        return (
            "ARM5 putDown → reset",
            "工具已放下作为 Guarantee，同时保留“ARM5 尚未回初始位”作为 Assumption。",
        )
    return ("通用 state target/source 推理", "上一动作 target states 更偏 Guarantee，下一动作 source states 更偏 Assumption。")


def decorate_conditions(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    decorated = []
    for item in items or []:
        subject = item.get("S") or item.get("subject")
        predicate = item.get("P") or item.get("predicate") or "is"
        obj = item.get("O") or item.get("object")
        signal = item.get("signal")
        if not subject or not obj:
            continue
        decorated.append(
            {
                "subject": subject,
                "predicate": predicate,
                "object": obj,
                "signal": signal,
                "text": f"{subject} {predicate} {obj}",
            }
        )
    return decorated


def decorate_interface_map(signal_map: Dict[str, str], converter: Any) -> List[Dict[str, Any]]:
    alias_map = {}
    if getattr(converter, "logic_ctx", None) is not None:
        alias_map = converter.logic_ctx.config.get("interface_alias", {})
    items = []
    for subject, signal in (signal_map or {}).items():
        display_signal = alias_map.get(signal, signal)
        items.append(
            {
                "subject": subject,
                "signal": signal,
                "display_signal": display_signal,
                "text": f"{subject} → {display_signal}",
            }
        )
    return items


# ---------------------------------------------------------------------------
# Generic utils
# ---------------------------------------------------------------------------
def _require_modules():
    if MODULE_IMPORT_ERROR is not None:
        raise RuntimeError(f"项目依赖导入失败，请检查 Backend 与项目根目录的相对位置是否正确。原始错误：{MODULE_IMPORT_ERROR}")


def _make_planner(use_llm: bool):
    try:
        return LocalMaterialPlanner(enable_llm=use_llm)
    except TypeError:
        return LocalMaterialPlanner()


def _make_agent(planner: Any):
    try:
        return MainPlannerAgent(planner=planner)
    except TypeError:
        return MainPlannerAgent()


def _safe_read_text(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception:
        return ""


def _load_json_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def split_csv(text: str) -> List[str]:
    return [item.strip() for item in (text or "").split(",") if item.strip()]


def _slot_to_dict(slot: Any) -> Dict[str, Any]:
    if slot is None:
        return {}
    if isinstance(slot, dict):
        return slot
    if hasattr(slot, "to_dict"):
        return slot.to_dict()
    return {
        "parent_id": getattr(slot, "parent_id", ""),
        "level_index": getattr(slot, "level_index", ""),
        "slot_index": getattr(slot, "slot_index", ""),
        "item_type": getattr(slot, "item_type", ""),
        "status": getattr(slot, "status", ""),
    }


def _tool_to_dict(tool: Any) -> Dict[str, Any]:
    if tool is None:
        return {}
    if isinstance(tool, dict):
        return tool
    if hasattr(tool, "to_dict"):
        return tool.to_dict()
    return {
        "ts_id": getattr(tool, "ts_id", ""),
        "slot_index": getattr(tool, "slot_index", ""),
        "tool_type": getattr(tool, "tool_type", ""),
        "color": getattr(tool, "color", ""),
        "description": getattr(tool, "description", ""),
    }


def humanize_slot(slot: Dict[str, Any]) -> str:
    if not slot:
        return "未定位"
    warehouse = slot.get("parent_id") or slot.get("ts_id") or "-"
    level = slot.get("level_index")
    slot_index = slot.get("slot_index")
    pieces = [str(warehouse)]
    if level not in (None, ""):
        pieces.append(f"{level}层")
    if slot_index not in (None, ""):
        pieces.append(f"{slot_index}槽")
    return " · ".join(pieces)


def extract_used_devices_from_ppr(ppr_objects: List[Dict[str, Any]]) -> List[str]:
    resource_map = _load_resource_prefix_map()
    used: List[str] = []
    seen = set()
    for obj in ppr_objects:
        for resource_name in split_csv(obj.get("hardware_resource", "")):
            prefix = resource_map.get(resource_name)
            if prefix and prefix not in seen:
                seen.add(prefix)
                used.append(prefix)
    return used


def _load_resource_prefix_map() -> Dict[str, str]:
    default_map = {
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
    try:
        data = json.loads(Path(RULES_CONFIG_JSON).read_text(encoding="utf-8"))
        return data.get("resource_to_prefix", default_map)
    except Exception:
        return default_map


def _preferred_label(graph: Any, uri: Any) -> str:
    if graph is None or RDFS is None:
        return str(uri).split("#")[-1]
    zh_labels = [str(obj) for obj in graph.objects(uri, RDFS.label) if getattr(obj, "language", None) == "zh"]
    if zh_labels:
        return zh_labels[0]
    labels = [str(obj) for obj in graph.objects(uri, RDFS.label)]
    if labels:
        return labels[0]
    return str(uri).split("#")[-1]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("Backend.app:app", host="127.0.0.1", port=8000, reload=False)
