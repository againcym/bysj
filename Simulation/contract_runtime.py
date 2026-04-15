from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class ContractRuntimeError(RuntimeError):
    pass


@dataclass
class RuntimeCondition:
    text: str
    subject: str = ""
    predicate: str = "is"
    object_value: str = ""
    signal: Optional[str] = None

    @classmethod
    def from_xml(cls, elem: ET.Element) -> Optional["RuntimeCondition"]:
        text = (elem.findtext("tokenDesc") or "").strip()
        if not text:
            return None

        subject = ""
        predicate = "is"
        object_value = ""
        signal = None
        for token in elem.findall("tokenGroup/token"):
            token_type = token.get("type") or ""
            token_text = (token.get("text") or "").strip()
            if token_type == "Variable" and not subject:
                subject = token_text
            elif token_type == "Operator" and token_text:
                predicate = token_text
            elif token_type == "Value" and not object_value:
                object_value = token_text
            elif token_type == "Condition" and token_text:
                signal = token_text

        return cls(
            text=text,
            subject=subject,
            predicate=predicate or "is",
            object_value=object_value,
            signal=signal,
        )

    def resolve_signal(self, interface_map: Dict[str, str]) -> Optional[str]:
        return self.signal or interface_map.get(self.subject)

    def to_dict(self, signal: Optional[str] = None) -> Dict[str, Any]:
        return {
            "text": self.text,
            "subject": self.subject,
            "predicate": self.predicate,
            "object": self.object_value,
            "signal": signal or self.signal,
        }


@dataclass
class RuntimeLink:
    index: int
    from_key: str
    to_key: str
    from_label: str
    to_label: str
    kind: str
    process_id: str
    assumptions: List[RuntimeCondition]
    guarantees: List[RuntimeCondition]
    interfaces: List[RuntimeCondition]
    from_context: Optional[Dict[str, Any]]
    to_context: Optional[Dict[str, Any]]

    def interface_map(self) -> Dict[str, str]:
        return {
            item.subject: item.signal
            for item in self.interfaces
            if item.subject and item.signal
        }

    def command_context(self) -> Optional[Dict[str, Any]]:
        return self.from_context or self.to_context

    def next_context(self) -> Optional[Dict[str, Any]]:
        return self.to_context


class OutputCommandAdapter:
    def __init__(self, signal_meta: Dict[str, Dict[str, Any]]):
        self.signal_meta = signal_meta
        self.points: Dict[str, Dict[str, Any]] = {}

    def reset(self) -> None:
        self.points = {}

    def issue_command(self, operation: Dict[str, Any], step_index: int) -> Dict[str, Any]:
        action_signal = (operation or {}).get("action_signal") or ""
        meta = self.signal_meta.get(action_signal, {})
        device = meta.get("device") or (operation or {}).get("device_name", "")

        if device:
            for signal_name, point in list(self.points.items()):
                if point.get("device") == device and signal_name != action_signal:
                    del self.points[signal_name]

        command = {
            "signal": action_signal,
            "output_signal": action_signal,
            "device": device,
            "output_device": device,
            "address": meta.get("address", ""),
            "output_address": meta.get("address", ""),
            "type": meta.get("type", ""),
            "output_type": meta.get("type", ""),
            "text": (operation or {}).get("display_text") or action_signal or "无输出命令",
            "step_desc": (operation or {}).get("step_desc", ""),
            "process_id": (operation or {}).get("process_id", ""),
            "step_id": (operation or {}).get("step_id", ""),
            "operation_node_key": (operation or {}).get("operation_node_key", ""),
            "payload": (operation or {}).get("payload") or {},
            "register_payload": (operation or {}).get("register_payload") or {},
            "step_index": step_index,
            "value": 1,
        }
        if action_signal:
            self.points[action_signal] = command
        return command

    def snapshot(self) -> List[Dict[str, Any]]:
        return [self.points[key] for key in sorted(self.points)]


class InputSignalAdapter:
    def __init__(self, signal_meta: Dict[str, Dict[str, Any]]):
        self.signal_meta = signal_meta
        self.points: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _key(signal: Optional[str], text: str) -> str:
        return signal or f"text::{text}"

    def reset(self) -> None:
        self.points = {}

    def lookup(self, signal: Optional[str], text: str) -> Optional[Dict[str, Any]]:
        return self.points.get(self._key(signal, text))

    def matches(self, condition: RuntimeCondition, signal: Optional[str]) -> bool:
        current = self.lookup(signal, condition.text)
        return bool(current and current.get("text") == condition.text)

    def observe_conditions(
        self,
        conditions: List[RuntimeCondition],
        link: RuntimeLink,
        phase: str,
        origin: str,
        step_index: int,
    ) -> List[Dict[str, Any]]:
        interface_map = link.interface_map()
        observed: List[Dict[str, Any]] = []
        for cond in conditions:
            signal = cond.resolve_signal(interface_map)
            meta = self.signal_meta.get(signal or "", {})
            point = {
                "signal": signal or "",
                "device": meta.get("device", ""),
                "address": meta.get("address", ""),
                "type": meta.get("type", ""),
                "subject": cond.subject,
                "text": cond.text,
                "expected_value": cond.object_value,
                "phase": phase,
                "origin": origin,
                "step_index": step_index,
            }
            self.points[self._key(signal, cond.text)] = point
            observed.append(point)
        return observed

    def snapshot(self) -> List[Dict[str, Any]]:
        return [self.points[key] for key in sorted(self.points)]


class ContractSimulationRuntime:
    def __init__(
        self,
        contract_path: Path,
        operation_context_path: Optional[Path] = None,
        signal_definition_path: Optional[Path] = None,
    ):
        self.contract_path = Path(contract_path)
        self.operation_context_path = Path(operation_context_path) if operation_context_path else None
        self.signal_definition_path = Path(signal_definition_path) if signal_definition_path else None
        self.signal_meta = self._load_signal_meta()
        self.output_signal_index = self._build_output_signal_index()
        self.output_adapter = OutputCommandAdapter(self.signal_meta)
        self.input_adapter = InputSignalAdapter(self.signal_meta)
        self.links: List[RuntimeLink] = []
        self.current_pointer = 0
        self.executed_link_indices: List[int] = []
        self.recent_events: List[Dict[str, Any]] = []
        self.last_transition: Optional[Dict[str, Any]] = None
        self.pending_command: Optional[Dict[str, Any]] = None
        self.awaiting_feedback = False
        self.auto_apply_assumptions = True
        self.reload()

    def _load_signal_meta(self) -> Dict[str, Dict[str, Any]]:
        if not self.signal_definition_path or not self.signal_definition_path.exists():
            return {}

        signal_meta: Dict[str, Dict[str, Any]] = {}
        root = ET.parse(self.signal_definition_path).getroot()
        for device in root.findall("Device"):
            device_name = device.get("name") or ""
            for signal in device.findall("Signal"):
                signal_name = signal.get("Name") or ""
                if not signal_name:
                    continue
                signal_meta[signal_name] = {
                    "device": device_name,
                    "address": signal.get("Address") or "",
                    "type": signal.get("Type") or "",
                }
        return signal_meta

    def _build_output_signal_index(self) -> Dict[str, List[str]]:
        index: Dict[str, List[str]] = {}
        for signal_name, meta in self.signal_meta.items():
            if meta.get("type") != "Output":
                continue
            device_key = self._normalize_text(meta.get("device", ""))
            index.setdefault(device_key, []).append(signal_name)
        return index

    def reload(self) -> None:
        if not self.contract_path.exists():
            raise ContractRuntimeError(f"未找到 Contract 文件: {self.contract_path}")

        node_map = self._load_nodes()
        operation_context = self._load_operation_context()
        root = ET.parse(self.contract_path).getroot()
        links: List[RuntimeLink] = []
        for index, link_elem in enumerate(root.findall("LinkArray/Link"), start=1):
            from_key = link_elem.get("from") or ""
            to_key = link_elem.get("to") or ""
            from_node = node_map.get(from_key, {})
            to_node = node_map.get(to_key, {})
            from_context = operation_context.get(from_key)
            to_context = operation_context.get(to_key)

            assumptions = self._parse_condition_box(link_elem.find("Contract/Assumption"))
            guarantees = self._parse_condition_box(link_elem.find("Contract/Guarantee"))
            interfaces = self._parse_condition_box(link_elem.find("Contract/Interface"))

            kind = self._infer_link_kind(from_context, to_context, from_node, to_node)
            process_id = (
                (to_context or {}).get("process_id")
                or (from_context or {}).get("process_id")
                or (to_node.get("label") if to_node.get("type") == "Process" else "")
                or (from_node.get("label") if from_node.get("type") == "Process" else "")
            )
            links.append(
                RuntimeLink(
                    index=index,
                    from_key=from_key,
                    to_key=to_key,
                    from_label=from_node.get("label", from_key),
                    to_label=to_node.get("label", to_key),
                    kind=kind,
                    process_id=process_id,
                    assumptions=assumptions,
                    guarantees=guarantees,
                    interfaces=interfaces,
                    from_context=from_context,
                    to_context=to_context,
                )
            )

        self.links = links
        self.reset(auto_apply_assumptions=self.auto_apply_assumptions)

    def _load_nodes(self) -> Dict[str, Dict[str, str]]:
        root = ET.parse(self.contract_path).getroot()
        node_map: Dict[str, Dict[str, str]] = {}
        for node in root.findall("NodeArray/Node"):
            key = node.get("key") or ""
            node_map[key] = {
                "label": (node.get("textt") or node.get("text") or key),
                "type": node.get("type") or "",
            }
        return node_map

    def _load_operation_context(self) -> Dict[str, Dict[str, Any]]:
        if not self.operation_context_path or not self.operation_context_path.exists():
            return {}
        data = json.loads(self.operation_context_path.read_text(encoding="utf-8"))
        op_map = data.get("operation_context") or {}
        return {
            str(item.get("operation_node_key")): item
            for item in op_map.values()
            if item.get("operation_node_key") is not None
        }

    @staticmethod
    def _parse_condition_box(box: Optional[ET.Element]) -> List[RuntimeCondition]:
        if box is None:
            return []
        items: List[RuntimeCondition] = []
        for elem in box.findall("Condition"):
            condition = RuntimeCondition.from_xml(elem)
            if condition is not None:
                items.append(condition)
        return items

    @staticmethod
    def _infer_link_kind(
        from_context: Optional[Dict[str, Any]],
        to_context: Optional[Dict[str, Any]],
        from_node: Dict[str, str],
        to_node: Dict[str, str],
    ) -> str:
        if from_context and to_context:
            return "operation_transition"
        if to_context:
            return "process_entry"
        if from_context:
            return "process_exit"
        if from_node.get("type") == "Process" or to_node.get("type") == "Process":
            return "process_transition"
        return "transition"

    @staticmethod
    def _normalize_text(text: str) -> str:
        return "".join(ch.lower() for ch in str(text or "") if ch.isalnum())

    def _guess_output_signal(self, action_label: str, device_label: str) -> str:
        device_key = self._normalize_text(device_label)
        action_key = self._normalize_text(action_label)
        candidates = self.output_signal_index.get(device_key, [])
        if not candidates:
            return ""

        for signal_name in candidates:
            signal_action = signal_name
            device_name = self.signal_meta.get(signal_name, {}).get("device", "")
            suffix = f"_{device_name}"
            if device_name and signal_name.endswith(suffix):
                signal_action = signal_name[: -len(suffix)]
            if self._normalize_text(signal_action) == action_key:
                return signal_name

        for signal_name in candidates:
            signal_action = signal_name
            device_name = self.signal_meta.get(signal_name, {}).get("device", "")
            suffix = f"_{device_name}"
            if device_name and signal_name.endswith(suffix):
                signal_action = signal_name[: -len(suffix)]
            normalized = self._normalize_text(signal_action)
            if action_key in normalized or normalized in action_key:
                return signal_name
        return ""

    def _fallback_operation_from_label(self, label: str, process_id: str) -> Dict[str, Any]:
        if "|" not in (label or ""):
            return {}
        action_label, device_label = [part.strip() for part in label.split("|", 1)]
        signal_name = self._guess_output_signal(action_label, device_label)
        device_meta = self.signal_meta.get(signal_name or "", {})
        return {
            "operation_node_key": "",
            "process_id": process_id,
            "step_id": "",
            "device_name": device_meta.get("device", device_label),
            "action_signal": signal_name,
            "display_text": label,
            "step_desc": label,
            "payload": {},
            "register_payload": {},
        }

    def _operation_context_for_link(self, link: RuntimeLink) -> Dict[str, Any]:
        if link.from_context:
            return link.from_context
        fallback = self._fallback_operation_from_label(link.from_label, link.process_id)
        if fallback:
            return fallback
        if link.to_context:
            return link.to_context
        return self._fallback_operation_from_label(link.to_label, link.process_id)

    def _next_operation_context_for_link(self, link: RuntimeLink) -> Dict[str, Any]:
        if link.to_context:
            return link.to_context
        return self._fallback_operation_from_label(link.to_label, link.process_id)

    def reset(self, auto_apply_assumptions: bool = True) -> Dict[str, Any]:
        self.auto_apply_assumptions = auto_apply_assumptions
        self.current_pointer = 0
        self.executed_link_indices = []
        self.recent_events = []
        self.last_transition = None
        self.pending_command = None
        self.awaiting_feedback = False
        self.output_adapter.reset()
        self.input_adapter.reset()
        self._append_event(kind="reset", message="仿真已重置。", link_index=None)
        return self.get_state()

    def _current_link(self) -> Optional[RuntimeLink]:
        if self.current_pointer >= len(self.links):
            return None
        return self.links[self.current_pointer]

    def _resolve_condition_signal(self, condition: RuntimeCondition, link: RuntimeLink) -> Optional[str]:
        return condition.resolve_signal(link.interface_map())

    def _build_check_entry(self, condition: RuntimeCondition, link: RuntimeLink, phase: str) -> Dict[str, Any]:
        signal = self._resolve_condition_signal(condition, link)
        meta = self.signal_meta.get(signal or "", {})
        observed = self.input_adapter.lookup(signal, condition.text)
        matched = bool(observed and observed.get("text") == condition.text)
        status = "ready" if matched and phase == "precondition" else "observed" if matched else "missing" if phase == "precondition" else "pending"
        return {
            "text": condition.text,
            "subject": condition.subject,
            "expected_value": condition.object_value,
            "signal": signal or "",
            "device": meta.get("device", ""),
            "address": meta.get("address", ""),
            "type": meta.get("type", ""),
            "phase": phase,
            "status": status,
            "origin": (observed or {}).get("origin", ""),
            "observed_text": (observed or {}).get("text", ""),
        }

    def _evaluate_link_checks(self, link: RuntimeLink) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[RuntimeCondition], List[RuntimeCondition]]:
        preconditions = [self._build_check_entry(item, link, "precondition") for item in link.assumptions]
        feedback = [self._build_check_entry(item, link, "feedback") for item in link.guarantees]
        missing_preconditions = [item for item, check in zip(link.assumptions, preconditions) if check["status"] != "ready"]
        pending_feedback = [item for item, check in zip(link.guarantees, feedback) if check["status"] != "observed"]
        return preconditions, feedback, missing_preconditions, pending_feedback

    def _build_operation_view(self, link: RuntimeLink) -> Dict[str, Any]:
        operation = self._operation_context_for_link(link)
        action_signal = operation.get("action_signal") or ""
        meta = self.signal_meta.get(action_signal, {})
        return {
            "operation_node_key": operation.get("operation_node_key", ""),
            "process_id": operation.get("process_id", link.process_id),
            "step_id": operation.get("step_id", ""),
            "device_name": operation.get("device_name", ""),
            "action_signal": action_signal,
            "display_text": operation.get("display_text") or action_signal or link.from_label,
            "step_desc": operation.get("step_desc", ""),
            "output_signal": action_signal,
            "output_device": meta.get("device", operation.get("device_name", "")),
            "output_address": meta.get("address", ""),
            "output_type": meta.get("type", ""),
            "payload": operation.get("payload") or {},
            "register_payload": operation.get("register_payload") or {},
        }

    def _build_next_operation_view(self, link: RuntimeLink) -> Dict[str, Any]:
        operation = self._next_operation_context_for_link(link)
        return {
            "operation_node_key": operation.get("operation_node_key", ""),
            "device_name": operation.get("device_name", ""),
            "action_signal": operation.get("action_signal", ""),
            "display_text": operation.get("display_text") or link.to_label,
            "step_desc": operation.get("step_desc", ""),
        }

    def _build_interface_view(self, link: RuntimeLink) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for cond in link.interfaces:
            signal = cond.signal or ""
            meta = self.signal_meta.get(signal, {})
            items.append(
                {
                    "subject": cond.subject,
                    "signal": signal,
                    "device": meta.get("device", ""),
                    "address": meta.get("address", ""),
                    "type": meta.get("type", ""),
                }
            )
        return items

    def _build_transition_view(
        self,
        link: RuntimeLink,
        blocked_phase: Optional[str] = None,
        command: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        preconditions, feedback, missing_preconditions, pending_feedback = self._evaluate_link_checks(link)
        if blocked_phase == "precondition":
            status = "blocked-precondition"
        elif blocked_phase == "feedback":
            status = "blocked-feedback"
        elif not missing_preconditions and not pending_feedback:
            status = "passed"
        elif self.current_pointer >= len(self.executed_link_indices):
            status = "running"
        else:
            status = "pending"

        return {
            "index": link.index,
            "from_key": link.from_key,
            "to_key": link.to_key,
            "from_label": link.from_label,
            "to_label": link.to_label,
            "title": f"{link.from_label} -> {link.to_label}",
            "kind": link.kind,
            "process_id": link.process_id,
            "status": status,
            "operation": self._build_operation_view(link),
            "next_operation": self._build_next_operation_view(link),
            "command": command or self._build_operation_view(link),
            "contract": {
                "preconditions": preconditions,
                "feedback": feedback,
                "interfaces": self._build_interface_view(link),
                "precondition_total": len(preconditions),
                "precondition_ready": len(preconditions) - len(missing_preconditions),
                "feedback_total": len(feedback),
                "feedback_observed": len(feedback) - len(pending_feedback),
            },
        }

    def _available_manual_conditions(self, link: RuntimeLink, phase: str) -> List[RuntimeCondition]:
        preconditions, feedback, missing_preconditions, pending_feedback = self._evaluate_link_checks(link)
        if phase == "precondition":
            return missing_preconditions
        if phase == "feedback":
            return pending_feedback
        raise ContractRuntimeError(f"不支持的注入阶段: {phase}")

    def inject_inputs(self, phase: str, items: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        link = self._current_link()
        if link is None:
            raise ContractRuntimeError("仿真已完成，没有可注入的链路。")

        selectable = self._available_manual_conditions(link, phase)
        if not selectable:
            self._append_event(
                kind="manual-input",
                message=f"第 {link.index} 条链路当前没有可手动注入的{ '前置输入' if phase == 'precondition' else '反馈输入' }。",
                link_index=link.index,
                payload={"phase": phase},
            )
            return self.get_state()

        selected: List[RuntimeCondition] = []
        if items:
            wanted = {
                ((item or {}).get("text") or "", (item or {}).get("signal") or "")
                for item in items
            }
            for cond in selectable:
                signal = self._resolve_condition_signal(cond, link) or ""
                if (cond.text, signal) in wanted or (cond.text, "") in wanted:
                    selected.append(cond)
        else:
            selected = selectable

        if not selected:
            raise ContractRuntimeError("没有匹配到可注入的输入信号。")

        observed = self.input_adapter.observe_conditions(
            selected,
            link=link,
            phase=phase,
            origin="manual",
            step_index=link.index,
        )
        self._append_event(
            kind="manual-input",
            message=f"第 {link.index} 条链路手动注入了 {len(observed)} 条{ '前置输入' if phase == 'precondition' else '反馈输入' }。",
            link_index=link.index,
            payload={"phase": phase, "inputs": observed},
        )
        blocked_phase = "feedback" if self.awaiting_feedback else None
        return self.get_state(blocked=False, blocked_link=link, blocked_phase=blocked_phase)

    def _append_event(
        self,
        kind: str,
        message: str,
        link_index: Optional[int],
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.recent_events.append(
            {
                "kind": kind,
                "message": message,
                "link_index": link_index,
                "payload": payload or {},
            }
        )
        self.recent_events = self.recent_events[-40:]

    def get_state(
        self,
        blocked: bool = False,
        blocked_link: Optional[RuntimeLink] = None,
        blocked_phase: Optional[str] = None,
    ) -> Dict[str, Any]:
        done = self.current_pointer >= len(self.links)
        current_link = self._current_link()
        current_transition = None
        if blocked_link is not None:
            current_transition = self._build_transition_view(
                blocked_link,
                blocked_phase=blocked_phase,
                command=self.pending_command if blocked_link == current_link else None,
            )
        elif current_link is not None:
            live_phase = blocked_phase or ("feedback" if self.awaiting_feedback else None)
            current_transition = self._build_transition_view(
                current_link,
                blocked_phase=live_phase,
                command=self.pending_command,
            )

        return {
            "summary": {
                "total_links": len(self.links),
                "executed_links": len(self.executed_link_indices),
                "pending_links": max(0, len(self.links) - len(self.executed_link_indices)),
                "current_step": None if done else (self.current_pointer + 1),
                "auto_complete_inputs": self.auto_apply_assumptions,
                "active_output_count": len(self.output_adapter.snapshot()),
                "observed_input_count": len(self.input_adapter.snapshot()),
                "awaiting_feedback": self.awaiting_feedback,
            },
            "done": done,
            "blocked": blocked,
            "blocked_phase": blocked_phase,
            "current_transition": current_transition,
            "last_transition": self.last_transition,
            "output_snapshot": self.output_adapter.snapshot(),
            "input_snapshot": self.input_adapter.snapshot(),
            "recent_events": self.recent_events[-12:],
        }

    def step(self, auto_apply_assumptions: Optional[bool] = None) -> Dict[str, Any]:
        auto_apply = self.auto_apply_assumptions if auto_apply_assumptions is None else auto_apply_assumptions
        link = self._current_link()
        if link is None:
            self._append_event(kind="done", message="仿真已完成。", link_index=None)
            return self.get_state()

        preconditions, _feedback, missing_preconditions, _pending_feedback = self._evaluate_link_checks(link)
        if missing_preconditions and not auto_apply and not self.awaiting_feedback:
            self._append_event(
                kind="blocked",
                message=f"第 {link.index} 条链路等待前置输入，不允许继续执行输出命令。",
                link_index=link.index,
                payload={"phase": "precondition", "missing": [item.to_dict(self._resolve_condition_signal(item, link)) for item in missing_preconditions]},
            )
            return self.get_state(blocked=True, blocked_link=link, blocked_phase="precondition")

        if missing_preconditions and auto_apply and not self.awaiting_feedback:
            injected = self.input_adapter.observe_conditions(
                missing_preconditions,
                link=link,
                phase="precondition",
                origin="mocked",
                step_index=link.index,
            )
            self._append_event(
                kind="precondition",
                message=f"第 {link.index} 条链路自动注入了 {len(injected)} 条前置输入。",
                link_index=link.index,
                payload={"inputs": injected},
            )

        command = self.pending_command
        if not self.awaiting_feedback:
            command = self.output_adapter.issue_command(self._operation_context_for_link(link), link.index)
            self.pending_command = command
            self.awaiting_feedback = True
            if command.get("signal"):
                self._append_event(
                    kind="command",
                    message=f"已下发输出命令 {command['signal']}。",
                    link_index=link.index,
                    payload={"command": command},
                )
            else:
                self._append_event(
                    kind="command",
                    message=f"第 {link.index} 条链路没有可下发的输出命令，仅执行 contract 检查。",
                    link_index=link.index,
                )

        _preconditions, feedback, _missing_preconditions, pending_feedback = self._evaluate_link_checks(link)
        if pending_feedback and not auto_apply:
            self.last_transition = self._build_transition_view(link, blocked_phase="feedback", command=command)
            self._append_event(
                kind="blocked",
                message=f"第 {link.index} 条链路已下发输出，正在等待反馈输入。",
                link_index=link.index,
                payload={"phase": "feedback", "pending": [item.to_dict(self._resolve_condition_signal(item, link)) for item in pending_feedback]},
            )
            return self.get_state(blocked=True, blocked_link=link, blocked_phase="feedback")

        if pending_feedback and auto_apply:
            observed = self.input_adapter.observe_conditions(
                pending_feedback,
                link=link,
                phase="feedback",
                origin="mocked",
                step_index=link.index,
            )
            self._append_event(
                kind="feedback",
                message=f"第 {link.index} 条链路自动注入了 {len(observed)} 条反馈输入。",
                link_index=link.index,
                payload={"inputs": observed},
            )

        self.executed_link_indices.append(link.index)
        self.last_transition = self._build_transition_view(link, command=command)
        self.pending_command = None
        self.awaiting_feedback = False
        self.current_pointer += 1
        self._append_event(
            kind="step",
            message=f"已执行第 {link.index} 条链路：{link.from_label} -> {link.to_label}",
            link_index=link.index,
            payload={"transition": self.last_transition},
        )

        if self.current_pointer >= len(self.links):
            self._append_event(kind="done", message="仿真已完成。", link_index=None)
        return self.get_state()

    def run(self, max_steps: int = 256, auto_apply_assumptions: Optional[bool] = None) -> Dict[str, Any]:
        result = self.get_state()
        for _ in range(max_steps):
            if result.get("done") or result.get("blocked"):
                break
            result = self.step(auto_apply_assumptions=auto_apply_assumptions)
        return result
