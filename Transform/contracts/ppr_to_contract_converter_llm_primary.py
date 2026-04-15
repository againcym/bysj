import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(CURRENT_DIR))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from Config.paths import (
    PPR_OUTPUT_XML,
    SIGNAL_OUTPUT_XML,
    RULES_CONFIG_JSON,
    CONTRACT_OUTPUT_LLMMAIN_XML,
    OPERATION_CONTEXT_JSON,
    GENERATED_OWL_FILES,
    CONTRACT_LOGIC_GUIDE_MD,
    CONTRACT_LOGIC_REASONING_CONFIG_JSON,
    ensure_output_dirs,
)
from logic_doc_reasoning_context import LogicDocReasoningContext
from operation_context_builder import OperationContextBuilder

PPR_PATH = str(PPR_OUTPUT_XML)
SIGNAL_PATH = str(SIGNAL_OUTPUT_XML)
CONFIG_PATH = str(RULES_CONFIG_JSON)
OUTPUT_PATH = str(CONTRACT_OUTPUT_LLMMAIN_XML)
CONTEXT_OUTPUT_PATH = str(OPERATION_CONTEXT_JSON)
OWL_FILES = {k: str(v) for k, v in GENERATED_OWL_FILES.items() if k != "Mover"}

DISPLAY_DEVICE = {
    "ARM1": "ARM1",
    "ARM2": "ARM2",
    "ARM3": "ARM3",
    "ARM4": "ARM4",
    "ARM5": "ARM5",
    "ARM6": "ARM6",
    "ARM7": "ARM7",
    "Camera": "camera",
    "ConveyorBelt1": "conveyor belt1",
    "ConveyorBelt2": "conveyor belt2",
    "Mover": "Mover",
}

NS = {
    "owl": "http://www.w3.org/2002/07/owl#",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
}


def extract_first_json_block(text: str) -> Optional[Any]:
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    for pattern in (r"\{.*\}", r"\[.*\]"):
        m = re.search(pattern, text, re.DOTALL)
        if not m:
            continue
        try:
            return json.loads(m.group(0))
        except Exception:
            continue
    return None


def normalize_condition_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not item:
        return None
    subject = item.get("S") or item.get("subject")
    predicate = item.get("P") or item.get("predicate") or "is"
    obj = item.get("O") or item.get("object")
    signal = item.get("signal")
    if not subject or not obj:
        return None
    return {
        "S": str(subject).strip(),
        "P": str(predicate).strip() or "is",
        "O": str(obj).strip(),
        "signal": str(signal).strip() if signal else None,
    }


def dedupe_conditions(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    seen = set()
    for item in items:
        norm = normalize_condition_item(item)
        if not norm:
            continue
        key = (norm["S"], norm["P"], norm["O"], norm.get("signal"))
        if key in seen:
            continue
        seen.add(key)
        out.append(norm)
    return out


def append_empty_condition(box: ET.Element):
    empty = ET.Element("Condition")
    ET.SubElement(empty, "tokenDesc").text = ""
    ET.SubElement(empty, "tokenGroup")
    box.append(empty)


def interface_signal_alias(signal_name: str, logic_ctx=None) -> str:
    if not signal_name:
        return signal_name
    if logic_ctx:
        alias_map = logic_ctx.config.get("interface_alias", {})
        if signal_name in alias_map:
            return alias_map[signal_name]
    if signal_name.startswith("TrackWelding_"):
        return "TrackWelding"
    if signal_name.startswith("TrackPainting_"):
        return "TrackPainting"
    return signal_name


def sort_conditions(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def prio(item: Dict[str, Any]) -> Tuple[int, str, str, str]:
        sig = item.get("signal") or ""
        subj = item.get("S") or ""
        obj = item.get("O") or ""
        if obj == "completed":
            p = 0
        elif obj in {
            "forward rotation", "backward rotation", "stopped", "picked up", "put down",
            "at start position", "not at start position", "ready", "at target position", "received"
        }:
            p = 1
        elif sig.startswith("CB"):
            p = 2
        elif sig.endswith("Mode"):
            p = 3
        else:
            p = 4
        return (p, subj, obj, sig)
    return sorted(dedupe_conditions(items), key=prio)


class JsonLLM:
    HARD_CODED_MODEL = "qwen3coder"
    HARD_CODED_API_KEY = "sk-dQIpgr85q-E2l2Emr01uzw"
    HARD_CODED_BASE_URL = "https://models.sjtu.edu.cn/api/v1/"

    def __init__(self, enabled: bool = True, timeout: int = 20, verbose: bool = True):
        self.enabled = enabled
        self.timeout = timeout
        self.verbose = verbose
        self.client = None
        self.cache: Dict[str, Any] = {}

        model = (self.HARD_CODED_MODEL or "").strip()
        api_key = (self.HARD_CODED_API_KEY or "").strip().strip("<>").strip()
        base_url = (self.HARD_CODED_BASE_URL or "").strip()

        if enabled and api_key and api_key != "YOUR_API_KEY_HERE" and api_key.startswith("sk-"):
            try:
                from langchain_openai import ChatOpenAI
                self.client = ChatOpenAI(
                    model=model,
                    api_key=api_key,
                    base_url=base_url,
                    temperature=0.1,
                    timeout=timeout,
                    max_retries=1,
                )
                if self.verbose:
                    print(f"[LLM] enabled=True model={model} timeout={timeout}s")
            except Exception as e:
                print(f"⚠️ ChatOpenAI 不可用，自动切到 fallback 模式: {e}")
                self.enabled = False
        else:
            if self.verbose:
                print("[LLM] disabled, invalid HARD_CODED_API_KEY in JsonLLM")
            self.enabled = False

    def ask_json(
        self,
        system_prompt: str,
        user_prompt: str,
        fallback: Any,
        cache_key: Optional[str] = None,
        label: str = ""
    ) -> Any:
        if cache_key and cache_key in self.cache:
            if self.verbose and label:
                print(f"[LLM cache hit] {label}")
            return self.cache[cache_key]

        if not self.enabled or self.client is None:
            return fallback

        try:
            if self.verbose and label:
                print(f"[LLM request] {label}")
            res = self.client.invoke([
                ("system", system_prompt),
                ("user", user_prompt),
            ])
            content = getattr(res, "content", str(res))
            parsed = extract_first_json_block(content)
            final = parsed if parsed is not None else fallback
            if cache_key:
                self.cache[cache_key] = final
            return final
        except Exception as e:
            print(f"⚠️ LLM 调用失败，回退确定性逻辑: {e}")
            return fallback


class ContractBuilder:
    @staticmethod
    def create_condition(subject: str, predicate: str, object_val: str):
        cond = ET.Element("Condition")
        ET.SubElement(cond, "tokenDesc").text = f"{subject} {predicate} {object_val}"
        tg = ET.SubElement(cond, "tokenGroup")
        ET.SubElement(tg, "token", {"type": "Variable", "text": str(subject)})
        ET.SubElement(tg, "token", {"type": "Operator", "text": str(predicate)})
        ET.SubElement(tg, "token", {"type": "Value", "text": str(object_val)})
        return cond, subject

    @staticmethod
    def create_interface(variable: str, signal_name: str, logic_ctx=None):
        display_signal = interface_signal_alias(signal_name, logic_ctx=logic_ctx)
        cond = ET.Element("Condition")
        ET.SubElement(cond, "tokenDesc").text = str(display_signal)
        tg = ET.SubElement(cond, "tokenGroup")
        ET.SubElement(tg, "token", {"type": "Variable", "text": str(variable)})
        ET.SubElement(tg, "token", {"type": "Condition", "text": str(display_signal)})
        return cond


class SignalRepository:
    def __init__(self, path: str):
        self.root = ET.parse(path).getroot()
        self.inputs: Dict[str, Dict[str, Any]] = {}
        self.outputs: Dict[str, Dict[str, Any]] = {}
        self.device_inputs: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.device_outputs: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

        for device in self.root.findall("Device"):
            dname = device.get("name")
            for sig in device.findall("Signal"):
                item = {
                    "device": dname,
                    "name": sig.get("Name"),
                    "type": sig.get("Type"),
                    "selects": [{"value": s.get("Value"), "desc": s.get("Desc")} for s in sig.findall("Select")],
                }
                if item["type"] == "Input":
                    self.inputs[item["name"]] = item
                    self.device_inputs[dname].append(item)
                else:
                    self.outputs[item["name"]] = item
                    self.device_outputs[dname].append(item)

    def get_device_input_summary(self, device_name: str) -> List[Dict[str, Any]]:
        return self.device_inputs.get(device_name, [])

    def get_device_output_summary(self, device_name: str) -> List[Dict[str, Any]]:
        return self.device_outputs.get(device_name, [])

    def guess_ready_condition(self, device_name: str) -> Optional[Dict[str, Any]]:
        for sig in self.device_inputs.get(device_name, []):
            for sel in sig["selects"]:
                desc = (sel.get("desc") or "").lower()
                if "at start position" in desc:
                    return {"S": self.subject_for_signal(sig["name"]), "P": "is", "O": "at start position", "signal": sig["name"]}
                if desc == "ready":
                    return {"S": self.subject_for_signal(sig["name"]), "P": "is", "O": "ready", "signal": sig["name"]}
                if desc == "stopped":
                    return {"S": self.subject_for_signal(sig["name"]), "P": "is", "O": "stopped", "signal": sig["name"]}
        return None

    def subject_for_signal(self, signal_name: str) -> str:
        mapping = {
            "MoverPosition": "Mover",
            "RMInformation": "Raw material information",
            "WeldingInformation": "Welding information",
            "PaintingInformation": "Painting information",
            "PIInformation": "Photo inspection information",
            "FPWInformation": "Finished Products Warehousing information",
            "TerminalController": "Terminal",
            "PhotoInspection": "Photo inspection",
            "Conveyorbelt1Motor": "ConveyorBelt1",
            "Conveyorbelt2Motor": "ConveyorBelt2",
        }
        if signal_name in mapping:
            return mapping[signal_name]
        if signal_name.startswith("TrackWelding"):
            return "Track welding"
        if signal_name.startswith("TrackPainting"):
            return "Track painting"
        if signal_name.endswith("Mode"):
            return signal_name[:-4]
        if "Motor" in signal_name:
            return signal_name.replace("Motor", "")
        if signal_name == "CB1Sensor1":
            return "Conveyorbelt1 first sensor"
        if signal_name == "CB1Sensor2":
            return "Conveyorbelt1 second sensor"
        if signal_name == "CB2Sensor1":
            return "Conveyorbelt2 first sensor"
        if signal_name == "CB2Sensor2":
            return "Conveyorbelt2 second sensor"
        m = re.match(r"(ARM\d+)(Outbound|Inbound|Moveout|Movein)$", signal_name)
        if m:
            arm, action = m.groups()
            action_text = {"Outbound": "outbound", "Inbound": "inbound", "Moveout": "move out", "Movein": "move in"}[action]
            return f"{arm} {action_text}"
        return signal_name


class StateMachineRepository:
    def __init__(self, signal_repo: SignalRepository):
        self.signal_repo = signal_repo
        self.states_by_device: Dict[str, Dict[str, Any]] = {}
        self.action_meta: Dict[str, Dict[str, Any]] = {}
        for device_name, path in OWL_FILES.items():
            if os.path.exists(path):
                self._load_one(device_name, path)

    @staticmethod
    def _local_name(tag: str) -> str:
        return tag.split("}")[-1]

    def _parse_state_label(self, label: str) -> List[Dict[str, Any]]:
        label = (label or "").replace("&amp;&amp;", "&&")
        parts = [p.strip() for p in label.split("&&") if p.strip()]
        conditions = []
        for part in parts:
            if "|" not in part:
                continue
            desc, signal_name = [x.strip() for x in part.split("|", 1)]
            conditions.append({
                "signal": signal_name,
                "subject": self.signal_repo.subject_for_signal(signal_name),
                "predicate": "is",
                "object": desc,
            })
        return conditions

    def _safe_lookup_state(self, states: Dict[str, Any], state_id: str) -> List[Dict[str, Any]]:
        if state_id in states:
            return states[state_id]["conditions"]
        for key, value in states.items():
            if key.lower() == state_id.lower():
                return value["conditions"]
        return []

    def _load_one(self, device_name: str, path: str):
        root = ET.parse(path).getroot()
        states = {}
        for ind in root.findall("owl:NamedIndividual", NS):
            about = ind.get(f"{{{NS['rdf']}}}about", "")
            sid = about.split("#")[-1]
            label = ind.findtext("rdfs:label", default="", namespaces=NS)
            conditions = self._parse_state_label(label)
            transitions = defaultdict(list)
            for child in ind:
                lname = self._local_name(child.tag)
                if lname in ("type", "label", "comment", "isAutomaticTransition"):
                    continue
                target = child.get(f"{{{NS['rdf']}}}resource")
                if target:
                    transitions[lname].append(target.split("#")[-1])
            states[sid] = {"label": label, "conditions": conditions, "transitions": dict(transitions)}

        self.states_by_device[device_name] = states
        action_sources = defaultdict(list)
        action_targets = defaultdict(list)
        for state_id, state_info in states.items():
            for action_name, target_state_ids in state_info["transitions"].items():
                for target_id in target_state_ids:
                    action_sources[action_name].append(state_id)
                    action_targets[action_name].append(target_id)

        for action_name in set(list(action_sources.keys()) + list(action_targets.keys())):
            self.action_meta[action_name] = {
                "device": device_name,
                "action": action_name,
                "source_states": [{"id": sid, "conditions": states[sid]["conditions"]} for sid in action_sources.get(action_name, []) if sid in states],
                "target_states": [{"id": tid, "conditions": self._safe_lookup_state(states, tid)} for tid in action_targets.get(action_name, [])],
            }

    def get_action_context(self, action_name: str) -> Dict[str, Any]:
        return self.action_meta.get(action_name, {})


class ProcessContractGenerator:
    def __init__(self, signal_repo: SignalRepository, config: Dict[str, Any]):
        self.signal_repo = signal_repo
        self.config = config

    def build_process_entry_contract(self, obj: ET.Element) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, str]]:
        from_text = obj.findtext("From/From_condition", default="")
        hardware_text = obj.findtext("Resource/Hardware_Resource", default="")

        g = []
        a = []

        for raw_cond in [x.strip() for x in from_text.split(",") if x.strip()]:
            g.append(self._fallback_process_condition(raw_cond))

        for cn_name in [x.strip() for x in hardware_text.split(",") if x.strip()]:
            device_name = self.config["resource_to_prefix"].get(cn_name)
            if not device_name:
                continue
            ready = self.signal_repo.guess_ready_condition(device_name)
            if ready:
                a.append(ready)

        g = sort_conditions(g)
        a = sort_conditions(a)

        signal_map = {}
        for item in g + a:
            if item.get("signal"):
                signal_map[item["S"]] = item["signal"]

        if obj.get("id") == "RAW_MATERIAL_HANDLING_CHASSIS" and "ARM2" in signal_map:
            signal_map["ARM2"] = "ARM2 is at start position"

        return g, a, signal_map

    def _fallback_process_condition(self, text: str) -> Dict[str, Any]:
        fallback_map = {
            "动子就位": {"S": "Mover", "P": "is", "O": "at target position", "signal": "MoverPosition"},
            "拿取原料信息": {"S": "Raw material information", "P": "is", "O": "received", "signal": "RMInformation"},
            "收到拿取原料信息": {"S": "Raw material information", "P": "is", "O": "received", "signal": "RMInformation"},
            "焊接信息": {"S": "Welding information", "P": "is", "O": "received", "signal": "WeldingInformation"},
            "收到焊接信息": {"S": "Welding information", "P": "is", "O": "received", "signal": "WeldingInformation"},
            "涂装信息": {"S": "Painting information", "P": "is", "O": "received", "signal": "PaintingInformation"},
            "收到涂装信息": {"S": "Painting information", "P": "is", "O": "received", "signal": "PaintingInformation"},
            "拍照检测信息": {"S": "Photo inspection information", "P": "is", "O": "received", "signal": "PIInformation"},
            "收到拍照检测信息": {"S": "Photo inspection information", "P": "is", "O": "received", "signal": "PIInformation"},
            "成品入库信息": {"S": "Finished Products Warehousing information", "P": "is", "O": "received", "signal": "FPWInformation"},
            "收到成品入库信息": {"S": "Finished Products Warehousing information", "P": "is", "O": "received", "signal": "FPWInformation"},
        }
        for key, value in fallback_map.items():
            if key in text:
                return value
        return {"S": text, "P": "is", "O": "completed", "signal": None}


class OperationContractReasoner:
    def __init__(
        self,
        signal_repo: SignalRepository,
        sm_repo: StateMachineRepository,
        config: Dict[str, Any],
        llm: JsonLLM,
        logic_ctx: Optional["LogicDocReasoningContext"] = None,
        use_llm: bool = True,
    ):
        self.signal_repo = signal_repo
        self.sm_repo = sm_repo
        self.config = config
        self.llm = llm
        self.logic_ctx = logic_ctx
        self.use_llm = use_llm

    def resolve_device_from_step(self, step_desc: str) -> Optional[str]:
        for cn_name, prefix in self.config["resource_to_prefix"].items():
            if cn_name in step_desc:
                return prefix
        return None

    def infer_transport_pattern(self, process_id: str, hardware_text: str) -> Optional[Dict[str, Any]]:
        if not self.logic_ctx:
            return None
        patterns = self.logic_ctx.config.get("transport_unit_patterns", {})
        for _, pattern in patterns.items():
            if process_id in pattern.get("process_ids", []):
                return pattern
        return None

    def _pick_signal_by_keyword(self, signal_names: List[str], keyword: str) -> Optional[str]:
        if not keyword:
            return None
        for name in signal_names:
            if keyword.lower() in name.lower():
                return name
        return None

    def infer_action_by_transport_pattern(self, step_desc: str, device_name: str, pattern: Dict[str, Any]) -> Optional[str]:
        outputs = self.signal_repo.get_device_output_summary(device_name)
        names = [x["name"] for x in outputs]
        roles = pattern.get("roles", {})
        action_roles = pattern.get("action_roles", {})

        if device_name in roles.get("warehouse_arm", []):
            if "出库" in step_desc:
                return self._pick_signal_by_keyword(names, action_roles.get("warehouse_arm_load", "outbound"))
            if "入库" in step_desc:
                return self._pick_signal_by_keyword(names, action_roles.get("warehouse_arm_unload", "inbound"))

        if device_name in roles.get("transfer_arm", []):
            if "转移至动子" in step_desc or "移到动子" in step_desc:
                return self._pick_signal_by_keyword(names, action_roles.get("transfer_arm_unload", "moveOut"))
            if "转移至传送带" in step_desc or "放上传送带" in step_desc or "将成品转移至传送带" in step_desc:
                return self._pick_signal_by_keyword(names, action_roles.get("transfer_arm_load", "moveIn"))

        if device_name in roles.get("conveyor", []):
            if "正转" in step_desc:
                return self._pick_signal_by_keyword(names, "forward")
            if "反转" in step_desc:
                return self._pick_signal_by_keyword(names, "backward")
            if "停止" in step_desc:
                return self._pick_signal_by_keyword(names, "stop")

        return None

    def resolve_action_signal(self, step_desc: str, device_name: str, process_id: str = "", hardware_text: str = "") -> Optional[str]:
        if not device_name:
            return None
        outputs = self.signal_repo.get_device_output_summary(device_name)
        if not outputs:
            return None

        pattern = self.infer_transport_pattern(process_id, hardware_text)
        pattern_fallback = self.infer_action_by_transport_pattern(step_desc, device_name, pattern) if pattern else None
        fallback = pattern_fallback or self._fallback_match_action(step_desc, device_name)

        if not self.use_llm or not self.logic_ctx:
            return fallback

        llm_result = self.llm.ask_json(
            system_prompt=self.logic_ctx.build_action_system_prompt(),
            user_prompt=(
                f"process_id: {process_id}\n"
                f"hardware_resource: {hardware_text}\n"
                f"transport_pattern: {json.dumps(pattern, ensure_ascii=False) if pattern else 'null'}\n"
                f"step_desc: {step_desc}\n"
                f"device: {device_name}\n"
                f"candidate_output_signals: {json.dumps(outputs, ensure_ascii=False)}\n"
                f"fallback: {json.dumps({'action_signal': fallback}, ensure_ascii=False)}\n"
                f"请注意：只能从 candidate_output_signals 中选择 action_signal。"
            ),
            fallback={"action_signal": fallback},
            cache_key=f"action::{process_id}::{device_name}::{step_desc}",
            label=f"action match {device_name} {step_desc[:24]}",
        )
        if isinstance(llm_result, dict):
            action_signal = llm_result.get("action_signal")
            valid_signals = {x['name'] for x in outputs}
            if action_signal in valid_signals:
                return action_signal
        return fallback

    def _fallback_match_action(self, step_desc: str, device_name: str) -> Optional[str]:
        outputs = self.signal_repo.get_device_output_summary(device_name)
        names = [x["name"] for x in outputs]
        lower_names = {name.lower(): name for name in names}

        reset_keywords = ("返回初始", "回到初始", "初始位置", "返回初始位", "复位")
        if any(k in step_desc for k in reset_keywords):
            for lower_name, original in lower_names.items():
                if "reset" in lower_name:
                    return original

        keyword_map = {
            "正转": ["forward"],
            "反转": ["backward"],
            "停止": ["stop"],
            "更换末端工具": ["pickUpTerminal", "pickUp"],
            "更换工具": ["pickUpTerminal", "pickUp"],
            "放回末端工具": ["putDownTerminal", "putDown"],
            "放回工具": ["putDownTerminal", "putDown"],
            "焊接": ["trackWelding"],
            "涂装": ["trackPainting"],
            "拍照": ["photoInspection"],
            "出库": ["outbound"],
            "入库": ["inbound"],
            "转移至动子": ["moveOut"],
            "转移至传送带": ["moveIn"],
            "转移": ["moveOut", "moveIn", "outbound", "inbound"],
        }
        for keyword, candidates in keyword_map.items():
            if keyword not in step_desc:
                continue
            for candidate in candidates:
                for lower_name, original in lower_names.items():
                    if candidate.lower() in lower_name:
                        return original
        return names[0] if names else None

    def display_text(self, action_signal: str, device_name: str) -> str:
        output_meta = self.signal_repo.outputs.get(action_signal, {})
        selects = output_meta.get("selects", [])
        display_action = selects[0].get("desc") if selects else action_signal.split("_")[0]
        return f"{display_action}|{DISPLAY_DEVICE.get(device_name, device_name)}"

    def reason_link_contract(
        self,
        prev_step_desc: str,
        prev_device: str,
        prev_action: str,
        curr_step_desc: str,
        curr_device: str,
        curr_action: str,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, str]]:
        prev_ctx = self.sm_repo.get_action_context(prev_action)
        curr_ctx = self.sm_repo.get_action_context(curr_action)

        guarantee_candidates = self._collect_conditions_from_states(prev_ctx.get("target_states", []))
        assumption_candidates = self._collect_conditions_from_states(curr_ctx.get("source_states", []))

        fallback_g, fallback_a = self._fallback_reason_link(
            prev_step_desc, prev_device, prev_action,
            curr_step_desc, curr_device, curr_action,
            prev_ctx, curr_ctx
        )
        guarantee_candidates = dedupe_conditions(guarantee_candidates + fallback_g)
        assumption_candidates = dedupe_conditions(assumption_candidates + fallback_a)

        if self.use_llm and self.logic_ctx:
            llm_result = self.llm.ask_json(
                system_prompt=self.logic_ctx.build_contract_system_prompt(),
                user_prompt=(
                    f"prev_step_desc: {prev_step_desc}\n"
                    f"prev_device: {prev_device}\n"
                    f"prev_action: {prev_action}\n"
                    f"prev_target_conditions: {json.dumps(guarantee_candidates, ensure_ascii=False)}\n\n"
                    f"curr_step_desc: {curr_step_desc}\n"
                    f"curr_device: {curr_device}\n"
                    f"curr_action: {curr_action}\n"
                    f"curr_source_conditions: {json.dumps(assumption_candidates, ensure_ascii=False)}\n\n"
                    f"要求：\n"
                    f"1. guarantee 优先从 prev_target_conditions 中选\n"
                    f"2. assumption 优先从 curr_source_conditions 中选\n"
                    f"3. 不允许凭空发明新 condition\n"
                    f"4. 可以删除、保留、重分类候选 condition，但必须保持 JSON 结构"
                ),
                fallback={"guarantee": guarantee_candidates, "assumption": assumption_candidates},
                cache_key=f"link::{prev_device}::{prev_action}::{curr_device}::{curr_action}::{prev_step_desc}::{curr_step_desc}",
                label=f"link {prev_action} -> {curr_action}",
            )
            if isinstance(llm_result, dict):
                guarantee = dedupe_conditions(llm_result.get("guarantee", guarantee_candidates))
                assumption = dedupe_conditions(llm_result.get("assumption", assumption_candidates))
            else:
                guarantee, assumption = guarantee_candidates, assumption_candidates
        else:
            guarantee, assumption = guarantee_candidates, assumption_candidates

        guarantee, assumption = self._normalize_reset_link_contract(
            prev_action=prev_action,
            prev_device=prev_device,
            prev_ctx=prev_ctx,
            curr_action=curr_action,
            curr_device=curr_device,
            guarantee=guarantee,
            assumption=assumption,
            curr_ctx=curr_ctx,
        )

        guarantee, assumption = self._normalize_special_link_contract(
            prev_step_desc=prev_step_desc,
            prev_device=prev_device,
            prev_action=prev_action,
            curr_step_desc=curr_step_desc,
            curr_device=curr_device,
            curr_action=curr_action,
            guarantee=guarantee,
            assumption=assumption,
        )

        guarantee = sort_conditions(guarantee)
        assumption = sort_conditions(assumption)

        signal_map = {}
        for item in guarantee + assumption:
            if item.get("signal"):
                signal_map[item["S"]] = item["signal"]

        return guarantee, assumption, signal_map

    def _collect_conditions_from_states(self, states: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for state in states:
            for cond in state.get("conditions", []):
                out.append({"S": cond["subject"], "P": cond["predicate"], "O": cond["object"], "signal": cond["signal"]})
        return dedupe_conditions(out)

    def _get_mode_signal(self, device_name: str) -> Optional[str]:
        if not device_name:
            return None
        direct = f"{device_name}Mode"
        if direct in self.signal_repo.inputs:
            return direct
        for sig in self.signal_repo.get_device_input_summary(device_name):
            if sig["name"].endswith("Mode"):
                return sig["name"]
        return None

    def _expected_completion_signals(self, action_signal: str, device_name: str) -> List[str]:
        if not action_signal:
            return []
        expected: List[str] = []
        if action_signal.startswith("outbound_"):
            expected.append(f"{device_name}Outbound")
        elif action_signal.startswith("inbound_"):
            expected.append(f"{device_name}Inbound")
        elif action_signal.startswith("moveOut_"):
            expected.append(f"{device_name}Moveout")
        elif action_signal.startswith("moveIn_"):
            expected.append(f"{device_name}Movein")
        elif action_signal.startswith("trackWelding_"):
            expected.extend([f"TrackWelding_{device_name}", "TrackWelding"])
        elif action_signal.startswith("trackPainting_"):
            expected.extend([f"TrackPainting_{device_name}", "TrackPainting"])
        elif action_signal.startswith("pickUpTerminal_") or action_signal.startswith("putDownTerminal_"):
            expected.append("TerminalController")
        elif action_signal.startswith("photoInspection_"):
            expected.append("PhotoInspection")
        valid = []
        for sig in expected:
            if sig in self.signal_repo.inputs:
                valid.append(sig)
        return valid or expected

    def _normalize_reset_link_contract(
        self,
        prev_action: str,
        prev_device: str,
        prev_ctx: Dict[str, Any],
        curr_action: str,
        curr_device: str,
        guarantee: List[Dict[str, Any]],
        assumption: List[Dict[str, Any]],
        curr_ctx: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        if not curr_action or not curr_action.startswith("reset_"):
            return dedupe_conditions(guarantee), dedupe_conditions(assumption)

        mode_signal = self._get_mode_signal(curr_device)
        if not mode_signal:
            return dedupe_conditions(guarantee), dedupe_conditions(assumption)

        device_subject = self.signal_repo.subject_for_signal(mode_signal)
        expected_signals = set(self._expected_completion_signals(prev_action, prev_device))

        normalized_assumption: List[Dict[str, Any]] = []
        for state in curr_ctx.get("source_states", []):
            for cond in state.get("conditions", []):
                if cond.get("signal") == mode_signal and cond.get("object") != "at start position":
                    normalized_assumption.append({"S": cond["subject"], "P": cond["predicate"], "O": cond["object"], "signal": cond["signal"]})
        if not normalized_assumption:
            normalized_assumption = [{"S": device_subject, "P": "is", "O": "not at start position", "signal": mode_signal}]

        normalized_guarantee: List[Dict[str, Any]] = []
        for state in prev_ctx.get("target_states", []):
            for cond in state.get("conditions", []):
                sig = cond.get("signal")
                if sig == mode_signal:
                    continue
                if expected_signals:
                    if sig in expected_signals or (sig and sig.startswith("CB")):
                        normalized_guarantee.append({"S": cond["subject"], "P": cond["predicate"], "O": cond["object"], "signal": cond["signal"]})
                else:
                    if sig and sig != mode_signal:
                        normalized_guarantee.append({"S": cond["subject"], "P": cond["predicate"], "O": cond["object"], "signal": cond["signal"]})

        return dedupe_conditions(normalized_guarantee), dedupe_conditions(normalized_assumption)

    def _normalize_special_link_contract(
        self,
        prev_step_desc: str,
        prev_device: str,
        prev_action: str,
        curr_step_desc: str,
        curr_device: str,
        curr_action: str,
        guarantee: List[Dict[str, Any]],
        assumption: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        guarantee = dedupe_conditions(guarantee)
        assumption = dedupe_conditions(assumption)

        def only_obj(items: List[Dict[str, Any]], *objs: str) -> List[Dict[str, Any]]:
            want = set(objs)
            return [x for x in items if x.get("O") in want]

        def only_signal(items: List[Dict[str, Any]], *signals: str) -> List[Dict[str, Any]]:
            want = set(signals)
            return [x for x in items if x.get("signal") in want]

        if curr_action.startswith("forward_") or curr_action.startswith("backward_"):
            assumption = only_obj(assumption, "stopped")

        if prev_action.startswith("forward_") and curr_action.startswith("stop_"):
            guarantee = only_obj(guarantee, "forward rotation")
            assumption = only_signal(assumption, "CB1Sensor2")
        if prev_action.startswith("backward_") and curr_action.startswith("stop_"):
            guarantee = only_obj(guarantee, "backward rotation")
            assumption = only_signal(assumption, "CB2Sensor1")

        if prev_action.startswith("stop_") and curr_action.startswith("moveOut_"):
            guarantee = only_obj(guarantee, "stopped")
            assumption = [x for x in assumption if x.get("signal") == f"{curr_device}Mode" and x.get("O") == "at start position"]

        if prev_action.startswith("stop_") and curr_action.startswith("inbound_"):
            guarantee = only_obj(guarantee, "stopped")
            assumption = [x for x in assumption if x.get("signal") == f"{curr_device}Mode" and x.get("O") == "at start position"]

        if prev_action.startswith("pickUpTerminal_") and curr_action.startswith("trackPainting_"):
            guarantee = only_obj(guarantee, "picked up")
            assumption = []

        if prev_action.startswith("trackPainting_") and curr_action.startswith("putDownTerminal_"):
            guarantee = only_obj(guarantee, "completed")
            assumption = []

        if prev_action.startswith("putDownTerminal_") and curr_action.startswith("reset_"):
            guarantee = only_obj(guarantee, "put down")
            assumption = [x for x in assumption if x.get("O") == "not at start position"]

        if prev_action.startswith("trackWelding_") and curr_action.startswith("reset_"):
            guarantee = [x for x in guarantee if x.get("O") == "completed"]
            assumption = [x for x in assumption if x.get("O") == "not at start position"]

        if prev_action.startswith("outbound_") and curr_action.startswith("reset_") and prev_device == "ARM1":
            if not any(x.get("signal") == "CB1Sensor1" for x in guarantee):
                guarantee.append({"S": "Conveyorbelt1 first sensor", "P": "is", "O": "triggered", "signal": "CB1Sensor1"})
        if prev_action.startswith("moveOut_") and curr_action.startswith("reset_") and prev_device == "ARM2":
            if not any(x.get("signal") == "CB1Sensor2" for x in guarantee):
                guarantee.append({"S": "Conveyorbelt1 second sensor", "P": "is", "O": "not triggered", "signal": "CB1Sensor2"})
        if prev_action.startswith("moveIn_") and curr_action.startswith("reset_") and prev_device == "ARM6":
            if not any(x.get("signal") == "CB2Sensor2" for x in guarantee):
                guarantee.append({"S": "Conveyorbelt2 second sensor", "P": "is", "O": "triggered", "signal": "CB2Sensor2"})
        if prev_action.startswith("inbound_") and curr_action.startswith("reset_") and prev_device == "ARM7":
            if not any(x.get("signal") == "CB2Sensor1" for x in guarantee):
                guarantee.append({"S": "Conveyorbelt2 first sensor", "P": "is", "O": "not triggered", "signal": "CB2Sensor1"})

        gset = {(x["S"], x["P"], x["O"], x.get("signal")) for x in guarantee}
        assumption = [x for x in assumption if (x["S"], x["P"], x["O"], x.get("signal")) not in gset]
        return dedupe_conditions(guarantee), dedupe_conditions(assumption)

    def _fallback_reason_link(
        self,
        prev_step_desc: str,
        prev_device: str,
        prev_action: str,
        curr_step_desc: str,
        curr_device: str,
        curr_action: str,
        prev_ctx: Dict[str, Any],
        curr_ctx: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        guarantee: List[Dict[str, Any]] = []
        assumption: List[Dict[str, Any]] = []

        for state in prev_ctx.get("target_states", []):
            for cond in state.get("conditions", []):
                guarantee.append({"S": cond["subject"], "P": cond["predicate"], "O": cond["object"], "signal": cond["signal"]})
        for state in curr_ctx.get("source_states", []):
            for cond in state.get("conditions", []):
                assumption.append({"S": cond["subject"], "P": cond["predicate"], "O": cond["object"], "signal": cond["signal"]})

        if "出库至传送带1" in prev_step_desc and prev_device == "ARM1":
            guarantee.append({"S": "Conveyorbelt1 first sensor", "P": "is", "O": "triggered", "signal": "CB1Sensor1"})
        if "转移至动子" in prev_step_desc and prev_device == "ARM2":
            guarantee.append({"S": "Conveyorbelt1 second sensor", "P": "is", "O": "not triggered", "signal": "CB1Sensor2"})
        if ("转移至传送带2" in prev_step_desc or "将成品转移至传送带" in prev_step_desc) and prev_device == "ARM6":
            guarantee.append({"S": "Conveyorbelt2 second sensor", "P": "is", "O": "triggered", "signal": "CB2Sensor2"})
        if "入库" in prev_step_desc and prev_device == "ARM7":
            guarantee.append({"S": "Conveyorbelt2 first sensor", "P": "is", "O": "not triggered", "signal": "CB2Sensor1"})

        if "传送带1" in curr_step_desc and "停止" in curr_step_desc:
            assumption.append({"S": "Conveyorbelt1 second sensor", "P": "is", "O": "triggered", "signal": "CB1Sensor2"})
        if "传送带2" in curr_step_desc and "停止" in curr_step_desc:
            assumption.append({"S": "Conveyorbelt2 first sensor", "P": "is", "O": "triggered", "signal": "CB2Sensor1"})

        return dedupe_conditions(guarantee), dedupe_conditions(assumption)


class PPRToContractConverterDemoLLM:
    def __init__(self, enable_llm: bool = True, enable_llm_process: bool = False, enable_llm_operation: bool = True):
        self.ppr_root = ET.parse(PPR_PATH).getroot()
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            self.config = json.load(f)

        self.llm = JsonLLM(enabled=enable_llm, timeout=20, verbose=True)
        self.signal_repo = SignalRepository(SIGNAL_PATH)
        self.state_repo = StateMachineRepository(self.signal_repo)

        self.logic_ctx = LogicDocReasoningContext(
            guide_path=str(CONTRACT_LOGIC_GUIDE_MD),
            config_path=str(CONTRACT_LOGIC_REASONING_CONFIG_JSON),
        )

        self.process_generator = ProcessContractGenerator(self.signal_repo, self.config)
        self.operation_reasoner = OperationContractReasoner(
            self.signal_repo,
            self.state_repo,
            self.config,
            self.llm,
            self.logic_ctx,
            use_llm=(enable_llm and enable_llm_operation),
        )

        self.root = ET.Element("Data")
        self.oblib = ET.SubElement(self.root, "Oblib")
        self.node_array = ET.SubElement(self.root, "NodeArray")
        self.link_array = ET.SubElement(self.root, "LinkArray")
        self.current_key = 100
        self.operation_context_builder = OperationContextBuilder(
            ppr_path=PPR_PATH,
            contract_output_path=OUTPUT_PATH,
            context_output_path=CONTEXT_OUTPUT_PATH,
        )

    def build_oblib(self):
        ET.SubElement(self.oblib, "Item", {"key": "1", "text": "Instance Group", "type": "namespace", "parent": "NaN"})
        groups = {"ARM": "12", "ConveyorBelt": "39", "Mover": "2", "Camera": "83", "System": "99"}
        for g_text, g_key in groups.items():
            ET.SubElement(self.oblib, "Item", {"key": g_key, "text": g_text, "type": "instanceGroup", "parent": "1"})

        item_idx = 200
        for device in self.signal_repo.root.findall("Device"):
            dname = device.get("name")
            parent_key = next((v for k, v in groups.items() if k in dname), "99")
            obj_key = str(item_idx)
            ET.SubElement(self.oblib, "Item", {"key": obj_key, "text": dname, "type": "object", "parent": parent_key})
            for sig in device.findall("Signal"):
                sig_type = "status" if sig.get("Type") == "Input" else "action"
                display_text = sig.get("Name").split("_")[0] if "_" in sig.get("Name") else sig.get("Name")
                ET.SubElement(self.oblib, "Item", {"key": str(item_idx + 1), "text": display_text, "type": sig_type, "parent": obj_key})
                item_idx += 1
            item_idx += 100

    def add_node(self, textt: str, node_type: str = "Operation", group: str = "NaN", is_group: bool = False) -> str:
        key = str(self.current_key)
        ET.SubElement(self.node_array, "Node", {
            "key": key,
            "text": "null" if not is_group else textt,
            "textt": textt,
            "type": node_type,
            "category": node_type,
            "group": str(group),
            "isGroup": "true" if is_group else "false",
            "nodeSignal": "[]",
            "isSubGraphExpanded": "false",
        })
        self.current_key += 10
        return key

    def fill_contract(self, link_elem: ET.Element, g_data=None, a_data=None, signal_map=None, skip_interface_for_subjects=None):
        contract = ET.SubElement(link_elem, "Contract")
        g_box = ET.SubElement(contract, "Guarantee")
        a_box = ET.SubElement(contract, "Assumption")
        i_box = ET.SubElement(contract, "Interface")
        inv = ET.SubElement(contract, "Invariant")

        subjects = []
        for box, data in ((g_box, g_data or []), (a_box, a_data or [])):
            if not data:
                append_empty_condition(box)
                continue
            for item in data:
                cond, sub = ContractBuilder.create_condition(item["S"], item["P"], item["O"])
                box.append(cond)
                subjects.append(sub)

        used = set()
        skip_set = set(skip_interface_for_subjects or [])
        for subject in subjects:
            if subject in used or subject in skip_set:
                continue
            signal = (signal_map or {}).get(subject)
            if signal:
                i_box.append(ContractBuilder.create_interface(subject, signal, logic_ctx=self.logic_ctx))
                used.add(subject)
        if not used:
            append_empty_condition(i_box)
        append_empty_condition(inv)

    def convert(self):
        self.build_oblib()
        last_process_key = None
        last_process_subject = None

        objects = self.ppr_root.findall("object")
        print(f"[convert] total objects = {len(objects)}")

        for idx, obj in enumerate(objects, start=1):
            process_id = obj.get("id")
            hardware_text = obj.findtext("Resource/Hardware_Resource", default="")
            print(f"[convert] ({idx}/{len(objects)}) process = {process_id}")

            mover_key = self.add_node("move_to|Mover")
            if last_process_key:
                process_to_mover = ET.SubElement(self.link_array, "Link", {"from": last_process_key, "to": mover_key})
                process_done = [{"S": last_process_subject, "P": "is", "O": "completed"}] if last_process_subject else [{"S": "Process", "P": "is", "O": "completed"}]
                if last_process_subject == "Photo inspection":
                    self.fill_contract(process_to_mover, g_data=process_done, signal_map={"Photo inspection": "PhotoInspection"}, skip_interface_for_subjects=None)
                else:
                    self.fill_contract(process_to_mover, g_data=process_done, skip_interface_for_subjects=[last_process_subject] if last_process_subject else None)

            if process_id == "PHOTO_INSPECTION":
                steps = obj.findall("Process/process_step")
                if steps:
                    step_desc = steps[0].findtext("step_desc", default="")
                    curr_device = self.operation_reasoner.resolve_device_from_step(step_desc)
                    curr_action = self.operation_reasoner.resolve_action_signal(step_desc, curr_device, process_id=process_id, hardware_text=hardware_text)
                    if curr_device and curr_action:
                        display_text = self.operation_reasoner.display_text(curr_action, curr_device)
                        curr_key = self.add_node(display_text)
                        self.operation_context_builder.add_operation_context(
                            obj=obj,
                            step=steps[0],
                            operation_node_key=curr_key,
                            device_name=curr_device,
                            action_signal=curr_action,
                            display_text=display_text,
                        )
                        g_data, a_data, signal_map = self.process_generator.build_process_entry_contract(obj)
                        photo_link = ET.SubElement(self.link_array, "Link", {"from": mover_key, "to": curr_key})
                        self.fill_contract(photo_link, g_data=g_data, a_data=a_data, signal_map=signal_map)
                        last_process_key = curr_key
                        last_process_subject = "Photo inspection"
                        continue

            process_key = self.add_node(process_id, node_type="Process", is_group=True)
            last_process_subject = self._format_process_completion(process_id)

            entry_link = ET.SubElement(self.link_array, "Link", {"from": mover_key, "to": process_key})
            g_data, a_data, signal_map = self.process_generator.build_process_entry_contract(obj)
            self.fill_contract(entry_link, g_data=g_data, a_data=a_data, signal_map=signal_map)

            prev_op = None
            steps = obj.findall("Process/process_step")
            print(f"[convert] process steps = {len(steps)}")
            for sidx, step in enumerate(steps, start=1):
                step_desc = step.findtext("step_desc", default="")
                curr_device = self.operation_reasoner.resolve_device_from_step(step_desc)
                curr_action = self.operation_reasoner.resolve_action_signal(
                    step_desc,
                    curr_device,
                    process_id=process_id,
                    hardware_text=hardware_text,
                )
                print(f"  [step {sidx}] device={curr_device} action={curr_action} desc={step_desc[:40]}")
                if not curr_device or not curr_action:
                    continue

                display_text = self.operation_reasoner.display_text(curr_action, curr_device)
                curr_key = self.add_node(display_text, group=process_key)
                self.operation_context_builder.add_operation_context(
                    obj=obj,
                    step=step,
                    operation_node_key=curr_key,
                    device_name=curr_device,
                    action_signal=curr_action,
                    display_text=display_text,
                )
                curr_op = {"key": curr_key, "device": curr_device, "action": curr_action, "desc": step_desc}

                if prev_op is not None:
                    op_link = ET.SubElement(self.link_array, "Link", {"from": prev_op["key"], "to": curr_key})
                    g_items, a_items, op_signal_map = self.operation_reasoner.reason_link_contract(
                        prev_step_desc=prev_op["desc"],
                        prev_device=prev_op["device"],
                        prev_action=prev_op["action"],
                        curr_step_desc=curr_op["desc"],
                        curr_device=curr_op["device"],
                        curr_action=curr_op["action"],
                    )
                    self.fill_contract(op_link, g_data=g_items, a_data=a_items, signal_map=op_signal_map)

                prev_op = curr_op

            last_process_key = process_key

        if last_process_key:
            release_key = self.add_node("release|Mover")
            release_link = ET.SubElement(self.link_array, "Link", {"from": last_process_key, "to": release_key})
            self.fill_contract(release_link, g_data=[{"S": last_process_subject, "P": "is", "O": "completed"}], skip_interface_for_subjects=[last_process_subject])

        ensure_output_dirs()
        tree = ET.ElementTree(self.root)
        ET.indent(tree, space="    ")
        tree.write(OUTPUT_PATH, encoding="utf-8", xml_declaration=True)
        context_path = self.operation_context_builder.save()
        print(f"[convert] operation context saved: {context_path}")
        return OUTPUT_PATH

    @staticmethod
    def _format_process_completion(process_id: str) -> str:
        special = {
            "RAW_MATERIAL_HANDLING_CHASSIS": "Raw material hanlding",
            "RAW_MATERIAL_HANDLING_BATTERY": "Raw material hanlding",
            "RAW_MATERIAL_HANDLING_BODY": "Raw material hanlding",
            "WELDING": "Welding",
            "PAINTING": "Painting",
            "FINISHED_PRODUCTS_WAREHOUSING": "Finished Products Warehousing",
            "PHOTO_INSPECTION": "Photo inspection",
        }
        return special.get(process_id, " ".join(word.capitalize() for word in process_id.lower().replace("_", " ").split()))


if __name__ == "__main__":
    converter = PPRToContractConverterDemoLLM(
        enable_llm=True,
        enable_llm_process=False,
        enable_llm_operation=True,
    )
    output_path = converter.convert()
    print(f"done: {output_path}")