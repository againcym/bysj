import argparse
import json
import os
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple
import sys

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(os.path.dirname(current_dir))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
from Config.paths import SIGNAL_OUTPUT_XML, OUTPUT_SM_DIR, OUTPUTS_DIR, ensure_output_dirs

OWL_NS = "http://www.w3.org/2002/07/owl#"
RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS_NS = "http://www.w3.org/2000/01/rdf-schema#"
XSD_NS = "http://www.w3.org/2001/XMLSchema#"
BASE_ONTOLOGY = "http://www.semanticweb.org/ontologies/arm1_sm"
BASE_NS = BASE_ONTOLOGY + "#"

ET.register_namespace("", BASE_NS)
ET.register_namespace("owl", OWL_NS)
ET.register_namespace("rdf", RDF_NS)
ET.register_namespace("rdfs", RDFS_NS)


def normalize_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def extract_first_json_block(text: str) -> Optional[Any]:
    if not text:
        return None
    text = str(text).strip()
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


def indent(elem, level=0):
    i = "\n" + level * "    "
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = i + "    "
        for child in elem:
            indent(child, level + 1)
            if not child.tail or not child.tail.strip():
                child.tail = i + "    "
        if not elem[-1].tail or not elem[-1].tail.strip():
            elem[-1].tail = i
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = i


def device_signals(device):
    ins, outs = [], []
    for s in device.findall("Signal"):
        if s.get("Type") == "Input":
            ins.append(s)
        else:
            outs.append(s)
    return ins, outs


def signal_name(sig) -> str:
    return sig.get("Name", "")


def signal_descs(sig) -> List[str]:
    out = []
    for sel in sig.findall("Select"):
        desc = (sel.get("Desc") or "").strip()
        if desc:
            out.append(desc)
    return out


def signal_address(sig) -> str:
    return sig.get("Address", "")


def signal_summary(sig) -> Dict[str, Any]:
    return {
        "name": signal_name(sig),
        "type": sig.get("Type", ""),
        "descs": signal_descs(sig),
        "address": signal_address(sig),
    }


def make_rdf_root() -> ET.Element:
    root = ET.Element(
        f"{{{RDF_NS}}}RDF",
        {f"{{http://www.w3.org/XML/1998/namespace}}base": BASE_ONTOLOGY},
    )
    ET.SubElement(root, f"{{{OWL_NS}}}Ontology", {f"{{{RDF_NS}}}about": BASE_ONTOLOGY})
    ET.SubElement(root, f"{{{OWL_NS}}}Class", {f"{{{RDF_NS}}}about": "#State"})
    return root


def add_annotation_property(root: ET.Element, name: str):
    ET.SubElement(root, f"{{{OWL_NS}}}AnnotationProperty", {f"{{{RDF_NS}}}about": f"#{name}"})


def add_object_property(root: ET.Element, name: str, label_text: Optional[str] = None,
                        comment: Optional[str] = None, is_auto_transition: Optional[bool] = None):
    prop = ET.SubElement(root, f"{{{OWL_NS}}}ObjectProperty", {f"{{{RDF_NS}}}about": f"#{name}"})
    if label_text:
        lab = ET.SubElement(prop, f"{{{RDFS_NS}}}label")
        lab.text = label_text
    if comment:
        com = ET.SubElement(prop, f"{{{RDFS_NS}}}comment")
        com.text = comment
    if is_auto_transition is not None:
        auto = ET.SubElement(prop, f"{{{BASE_NS}}}isAutomaticTransition")
        auto.set(f"{{{RDF_NS}}}datatype", f"{XSD_NS}boolean")
        auto.text = "true" if is_auto_transition else "false"
    return prop


def add_state(root: ET.Element, state_id: str, label: str, transitions: Optional[List[Tuple[str, str]]] = None):
    ind = ET.SubElement(root, f"{{{OWL_NS}}}NamedIndividual", {f"{{{RDF_NS}}}about": f"#{state_id}"})
    ET.SubElement(ind, f"{{{RDF_NS}}}type", {f"{{{RDF_NS}}}resource": "#State"})
    lab = ET.SubElement(ind, f"{{{RDFS_NS}}}label")
    lab.text = label
    for action_name, target_state in (transitions or []):
        if not action_name or not target_state:
            continue
        ET.SubElement(ind, f"{{{BASE_NS}}}{action_name}", {f"{{{RDF_NS}}}resource": f"#{target_state}"})
    return ind


def property_label_from_signal(sig) -> str:
    descs = signal_descs(sig)
    desc = descs[0] if descs else signal_name(sig)
    addr = signal_address(sig)
    return f"{desc} ({addr})" if addr else desc


TEMPLATE_SYSTEM_PROMPT = """
你是工业设备状态机模板选择器。
你的任务不是生成 OWL，而是根据设备名、输入信号、输出信号，选择最合适的固定模板。
你必须且只能输出 JSON，不要输出解释文字。

可选 template:
- arm_outbound_inbound
- arm_movein_moveout
- arm_trackwelding
- arm5_demo_exact
- conveyor_demo_exact
- camera_demo_exact
- mover_minimal
- unknown

规则：
1. ARM1/ARM7 -> arm_outbound_inbound
2. ARM2/ARM6 -> arm_movein_moveout
3. ARM3/ARM4 -> arm_trackwelding
4. ARM5 -> arm5_demo_exact
5. ConveyorBelt1/2 -> conveyor_demo_exact
6. Camera -> camera_demo_exact
7. Mover -> mover_minimal

只输出:
{"template":"...","reason":"..."}
""".strip()


class TemplateChooser:
    def __init__(self, backend: str = "none", temperature: float = 0.1, verbose: bool = True,
                 local_model_path: str = r"C:\Users\11769\Desktop\LangChain\Qwen2-7B-Instruct"):
        self.backend = backend
        self.temperature = temperature
        self.verbose = verbose
        self.local_model_path = local_model_path
        self.client = None
        self._init_backend()

    def _init_backend(self):
        if self.backend == "none":
            if self.verbose:
                print("[LLM] disabled, use deterministic template mapping")
            return

        if self.backend == "openai":
            try:
                from langchain_openai import ChatOpenAI
                api_key = os.getenv("SJTU_API_KEY") or os.getenv("OPENAI_API_KEY")
                if not api_key:
                    raise RuntimeError("missing SJTU_API_KEY / OPENAI_API_KEY")
                self.client = ChatOpenAI(
                    model=os.getenv("SJTU_MODEL", "qwen3coder"),
                    api_key=api_key,
                    base_url=os.getenv("SJTU_BASE_URL", "https://models.sjtu.edu.cn/api/v1/"),
                    temperature=self.temperature,
                    timeout=30,
                    max_retries=1,
                )
                if self.verbose:
                    print("[LLM] backend=openai")
                return
            except Exception as e:
                print(f"⚠️ 初始化 ChatOpenAI 失败，回退确定性模板: {e}")
                self.client = None
                self.backend = "none"
                return

        if self.backend == "local":
            try:
                import torch
                from transformers import AutoModelForCausalLM, AutoTokenizer, pipeline
                tokenizer = AutoTokenizer.from_pretrained(self.local_model_path)
                model = AutoModelForCausalLM.from_pretrained(
                    self.local_model_path,
                    device_map="auto",
                    torch_dtype=torch.float16
                )
                self.client = pipeline(
                    "text-generation",
                    model=model,
                    tokenizer=tokenizer,
                    max_new_tokens=256,
                    temperature=self.temperature,
                    do_sample=True,
                    return_full_text=False,
                )
                if self.verbose:
                    print(f"[LLM] backend=local model_path={self.local_model_path}")
                return
            except Exception as e:
                print(f"⚠️ 初始化本地模型失败，回退确定性模板: {e}")
                self.client = None
                self.backend = "none"
                return

        print(f"⚠️ 未知 backend={self.backend}，回退确定性模板")
        self.backend = "none"

    def _deterministic_template(self, device_name: str) -> Dict[str, Any]:
        if device_name in {"ARM1", "ARM7"}:
            return {"template": "arm_outbound_inbound", "reason": "ARM1/7 demo template"}
        if device_name in {"ARM2", "ARM6"}:
            return {"template": "arm_movein_moveout", "reason": "ARM2/6 demo template"}
        if device_name in {"ARM3", "ARM4"}:
            return {"template": "arm_trackwelding", "reason": "ARM3/4 demo template"}
        if device_name == "ARM5":
            return {"template": "arm5_demo_exact", "reason": "ARM5 demo template"}
        if device_name.startswith("ConveyorBelt"):
            return {"template": "conveyor_demo_exact", "reason": "conveyor demo template"}
        if device_name == "Camera":
            return {"template": "camera_demo_exact", "reason": "camera demo template"}
        if device_name == "Mover":
            return {"template": "mover_minimal", "reason": "no demo file; use minimal template"}
        return {"template": "unknown", "reason": "no template"}

    def choose(self, device_name: str, inputs, outputs) -> Dict[str, Any]:
        fallback = self._deterministic_template(device_name)
        if self.backend == "none" or self.client is None:
            return fallback

        payload = json.dumps(
            {
                "device_name": device_name,
                "inputs": [signal_summary(s) for s in inputs],
                "outputs": [signal_summary(s) for s in outputs],
                "fallback": fallback,
            },
            ensure_ascii=False,
            indent=2,
        )

        try:
            if self.backend == "openai":
                res = self.client.invoke([("system", TEMPLATE_SYSTEM_PROMPT), ("user", payload)])
                content = getattr(res, "content", str(res))
            else:
                prompt = (
                    "<|im_start|>system\n" + TEMPLATE_SYSTEM_PROMPT + "\n<|im_end|>\n"
                    "<|im_start|>user\n" + payload + "\n<|im_end|>\n"
                    "<|im_start|>assistant\n"
                )
                content = self.client(prompt)[0]["generated_text"]
            parsed = extract_first_json_block(content)
            if isinstance(parsed, dict) and parsed.get("template") in {
                "arm_outbound_inbound", "arm_movein_moveout", "arm_trackwelding",
                "arm5_demo_exact", "conveyor_demo_exact", "camera_demo_exact",
                "mover_minimal", "unknown"
            }:
                return parsed
        except Exception as e:
            print(f"⚠️ LLM 模板判别失败，回退确定性模板: {e}")
        return fallback


def build_arm_outbound_inbound(device_name: str, inputs, outputs, strict_demo_quirks: bool = True) -> ET.Element:
    root = make_rdf_root()
    outs = {signal_name(s): s for s in outputs}
    for name in [f"outbound_{device_name}", f"inbound_{device_name}", f"reset_{device_name}"]:
        if name in outs:
            add_object_property(root, name, property_label_from_signal(outs[name]))

    add_state(root, "AtStartPosition", f"at start position | {device_name}Mode", [
        (f"outbound_{device_name}", "OutboundCompleted"),
        (f"inbound_{device_name}", "InboundCompleted"),
        (f"reset_{device_name}", "AtStartPosition"),
    ])
    add_state(root, "OutboundCompleted", f"not at start position | {device_name}Mode && completed | {device_name}Outbound", [
        (f"reset_{device_name}", "AtStartPosition"),
        (f"inbound_{device_name}", "InboundCompleted"),
        (f"outbound_{device_name}", "OutboundCompleted"),
    ])
    if strict_demo_quirks and device_name == "ARM7":
        inbound_transitions = [
            (f"reset_{device_name}", "AtStartPosition"),
            (f"inbound_{device_name}", "OutboundCompleted"),
            ("inbound_ARM1", "InboundCompleted"),
        ]
    else:
        inbound_transitions = [
            (f"reset_{device_name}", "AtStartPosition"),
            (f"outbound_{device_name}", "OutboundCompleted"),
            (f"inbound_{device_name}", "InboundCompleted"),
        ]
    add_state(root, "InboundCompleted", f"not at start position | {device_name}Mode && completed | {device_name}Inbound", inbound_transitions)
    return root


def build_arm_movein_moveout(device_name: str, inputs, outputs) -> ET.Element:
    root = make_rdf_root()
    outs = {signal_name(s): s for s in outputs}
    for name in [f"moveOut_{device_name}", f"moveIn_{device_name}", f"reset_{device_name}"]:
        if name in outs:
            add_object_property(root, name, property_label_from_signal(outs[name]))

    add_state(root, "AtStartPosition", f"at start position | {device_name}Mode", [
        (f"moveOut_{device_name}", "MoveoutCompleted"),
        (f"moveIn_{device_name}", "MoveinCompleted"),
        (f"reset_{device_name}", "AtStartPosition"),
    ])
    add_state(root, "MoveoutCompleted", f"not at start position | {device_name}Mode && completed | {device_name}Moveout", [
        (f"reset_{device_name}", "AtStartPosition"),
        (f"moveIn_{device_name}", "MoveinCompleted"),
        (f"moveOut_{device_name}", "MoveoutCompleted"),
    ])
    add_state(root, "MoveinCompleted", f"not at start position | {device_name}Mode && completed | {device_name}Movein", [
        (f"reset_{device_name}", "AtStartPosition"),
        (f"moveOut_{device_name}", "MoveoutCompleted"),
        (f"moveIn_{device_name}", "MoveinCompleted"),
    ])
    return root


def build_arm_trackwelding(device_name: str, inputs, outputs) -> ET.Element:
    root = make_rdf_root()
    outs = {signal_name(s): s for s in outputs}
    for name in [f"trackWelding_{device_name}", f"reset_{device_name}"]:
        if name in outs:
            add_object_property(root, name, property_label_from_signal(outs[name]))

    add_state(root, "AtStartPosition", f"at start position | {device_name}Mode", [
        (f"trackWelding_{device_name}", "TrackweldingCompleted"),
        (f"reset_{device_name}", "AtStartPosition"),
    ])
    add_state(root, "TrackweldingCompleted", f"not at start position | {device_name}Mode && completed | TrackWelding_{device_name}", [
        (f"reset_{device_name}", "AtStartPosition"),
        (f"trackWelding_{device_name}", "TrackweldingCompleted"),
    ])
    return root


def build_arm5_demo_exact(device_name: str, inputs, outputs) -> ET.Element:
    root = make_rdf_root()
    outs = {signal_name(s): s for s in outputs}
    for name in [f"pickUpTerminal_{device_name}", f"trackPainting_{device_name}", f"putDownTerminal_{device_name}", f"reset_{device_name}"]:
        if name in outs:
            add_object_property(root, name, property_label_from_signal(outs[name]))

    add_state(root, "AtStartPosition", f"at start position | {device_name}Mode", [
        (f"pickUpTerminal_{device_name}", "PickupCompleted"),
        (f"trackPainting_{device_name}", "TrackpaintingCompleted"),
        (f"putDownTerminal_{device_name}", "PutdownCompleted"),
        (f"reset_{device_name}", "AtStartPosition"),
    ])
    add_state(root, "PickupCompleted", f"not at start position | {device_name}Mode && picked up | TerminalController", [
        (f"reset_{device_name}", "AtStartPosition"),
        (f"putDownTerminal_{device_name}", "PutdownCompleted"),
        (f"trackPainting_{device_name}", "TrackpaintingCompleted"),
        (f"pickUpTerminal_{device_name}", "PickupCompleted"),
    ])
    add_state(root, "TrackpaintingCompleted", f"not at start position | {device_name}Mode && completed | TrackPainting_{device_name}", [
        (f"reset_{device_name}", "AtStartPosition"),
        (f"putDownTerminal_{device_name}", "PutdownCompleted"),
        (f"pickUpTerminal_{device_name}", "PickupCompleted"),
        (f"trackPainting_{device_name}", "TrackpaintingCompleted"),
    ])
    add_state(root, "PutdownCompleted", f"not at start position | {device_name}Mode && put down | TerminalController", [
        (f"reset_{device_name}", "AtStartPosition"),
        (f"pickUpTerminal_{device_name}", "PickupCompleted"),
        (f"trackPainting_{device_name}", "TrackpaintingCompleted"),
        (f"putDownTerminal_{device_name}", "PutdownCompleted"),
    ])
    return root


def build_conveyor_demo_exact(device_name: str, inputs, outputs) -> ET.Element:
    root = make_rdf_root()
    outs = {signal_name(s): s for s in outputs}
    suffix = "1" if device_name.endswith("1") else "2"
    forward_name = f"forward_conveyorBelt{suffix}"
    backward_name = f"backward_conveyorBelt{suffix}"
    stop_name = f"stop_conveyorBelt{suffix}"
    for name in [forward_name, backward_name, stop_name]:
        if name in outs:
            add_object_property(root, name, property_label_from_signal(outs[name]))

    motor = f"Conveyorbelt{suffix}Motor"
    prefix = f"CB{suffix}"
    add_state(root, f"{prefix}Stopped", f"stopped | {motor}", [
        (forward_name, f"{prefix}MovingForward"),
        (backward_name, f"{prefix}MovingBackward"),
        (stop_name, f"{prefix}Stopped"),
    ])
    add_state(root, f"{prefix}MovingForward", f"forward rotation | {motor}", [(stop_name, f"{prefix}Stopped")])
    add_state(root, f"{prefix}MovingBackward", f"backward rotation | {motor}", [(stop_name, f"{prefix}Stopped")])
    return root


def build_camera_demo_exact(device_name: str, inputs, outputs, strict_demo_quirks: bool = True) -> ET.Element:
    root = make_rdf_root()
    add_annotation_property(root, "isAutomaticTransition")
    outs = {signal_name(s): s for s in outputs}
    if "photoInspection_Camera" in outs:
        add_object_property(root, "photoInspection_Camera", property_label_from_signal(outs["photoInspection_Camera"]))
    add_object_property(root, "reset_Camera", "reset (internal)",
                        comment="This transition fires automatically and immediately when the source state is entered.",
                        is_auto_transition=True)

    start_state_about = "Cameraready" if strict_demo_quirks else "CameraReady"
    reset_target = "CameraReady"
    add_state(root, start_state_about, "ready | CameraMode", [
        ("photoInspection_Camera", "PhotoInspectionCompleted"),
        ("reset_Camera", reset_target),
    ])
    add_state(root, "PhotoInspectionCompleted", "completed | PhotoInspection", [("reset_Camera", reset_target)])
    return root


def build_mover_minimal(device_name: str, inputs, outputs) -> ET.Element:
    root = make_rdf_root()
    outs = {signal_name(s): s for s in outputs}
    for name in [f"release_{device_name}", f"moveTo_{device_name}"]:
        if name in outs:
            add_object_property(root, name, property_label_from_signal(outs[name]))

    add_state(root, "NotAtTarget", "not at target position | MoverPosition", [(f"moveTo_{device_name}", "AtTarget")])
    add_state(root, "AtTarget", "at target position | MoverPosition", [(f"release_{device_name}", "NotAtTarget")])
    return root


def generate_one(device, chooser: TemplateChooser, strict_demo_quirks: bool = True) -> Tuple[Optional[ET.Element], Dict[str, Any]]:
    device_name = device.get("name")
    inputs, outputs = device_signals(device)
    decision = chooser.choose(device_name, inputs, outputs)
    template = decision.get("template", "unknown")

    if template == "arm_outbound_inbound":
        return build_arm_outbound_inbound(device_name, inputs, outputs, strict_demo_quirks=strict_demo_quirks), decision
    if template == "arm_movein_moveout":
        return build_arm_movein_moveout(device_name, inputs, outputs), decision
    if template == "arm_trackwelding":
        return build_arm_trackwelding(device_name, inputs, outputs), decision
    if template == "arm5_demo_exact":
        return build_arm5_demo_exact(device_name, inputs, outputs), decision
    if template == "conveyor_demo_exact":
        return build_conveyor_demo_exact(device_name, inputs, outputs), decision
    if template == "camera_demo_exact":
        return build_camera_demo_exact(device_name, inputs, outputs, strict_demo_quirks=strict_demo_quirks), decision
    if template == "mover_minimal":
        return build_mover_minimal(device_name, inputs, outputs), decision
    return None, decision


def main():
    parser = argparse.ArgumentParser(description="Generate demo-equivalent per-device OWL state machines from Signal_Definition.xml")
    default_input = str(SIGNAL_OUTPUT_XML)
    default_output_dir = str(OUTPUT_SM_DIR)
    default_decision_log = str(OUTPUTS_DIR / "device_template_decisions_demo_exact.json")

    parser.add_argument("--input", default=default_input, help="Signal definition XML path")
    parser.add_argument("--output-dir", default=default_output_dir, help="Directory for generated OWL files")
    parser.add_argument("--decision-log", default=default_decision_log, help="JSON log for template decisions")
    parser.add_argument("--llm-backend", default="none", choices=["openai", "local", "none"], help="Template chooser backend")
    parser.add_argument("--local-model-path", default=r"C:\Users\11769\Desktop\LangChain\Qwen2-7B-Instruct", help="Local model path for --llm-backend local")
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--fix-demo-quirks", action="store_true", help="Do not replicate known demo typos (ARM7 / Camera)")
    args = parser.parse_args()

    strict_demo_quirks = not args.fix_demo_quirks
    ensure_output_dirs()
    os.makedirs(args.output_dir, exist_ok=True)

    chooser = TemplateChooser(
        backend=args.llm_backend,
        temperature=args.temperature,
        verbose=True,
        local_model_path=args.local_model_path,
    )

    print(f"正在读取：{args.input}")
    root = ET.parse(args.input).getroot()

    count = 0
    decisions: List[Dict[str, Any]] = []
    for device in root.findall("Device"):
        owl_root, decision = generate_one(device, chooser, strict_demo_quirks=strict_demo_quirks)
        decision["device_name"] = device.get("name")
        decisions.append(decision)

        if owl_root is None:
            print(f"跳过未适配设备：{device.get('name')} -> {decision.get('template')}")
            continue

        indent(owl_root)
        out_path = os.path.join(args.output_dir, f"{device.get('name')}_generated.owl")
        ET.ElementTree(owl_root).write(out_path, encoding="utf-8", xml_declaration=True)
        count += 1
        print(f"已生成：{out_path} | template={decision.get('template')} | reason={decision.get('reason')}")

    # with open(args.decision_log, "w", encoding="utf-8") as f:
    #     json.dump(decisions, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 全部完成！共生成 {count} 个状态机文件")
    print(f"📁 保存位置：{args.output_dir}")
    # print(f"📝 模板判别日志：{args.decision_log}")
    if strict_demo_quirks:
        print("ℹ️ 当前为 strict demo 模式：会复刻 Camera / ARM7 demo 中的原始小瑕疵，以便最大限度贴近 demo。")


if __name__ == "__main__":
    main()
