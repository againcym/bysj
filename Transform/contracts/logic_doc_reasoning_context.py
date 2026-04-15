import json
from pathlib import Path

class LogicDocReasoningContext:
    def __init__(self, guide_path: str, config_path: str):
        self.guide_path = Path(guide_path)
        self.config_path = Path(config_path)
        self.guide_text = self.guide_path.read_text(encoding="utf-8") if self.guide_path.exists() else ""
        self.config = json.loads(self.config_path.read_text(encoding="utf-8")) if self.config_path.exists() else {}

    def build_action_system_prompt(self) -> str:
        return (
            "你是工业自动化动作映射助手。\n"
            "请严格遵循下面的逻辑推理文档与配置，只在候选 output signals 中选择最合适的 action_signal。\n\n"
            f"[逻辑推理文档]\n{self.guide_text}\n\n"
            f"[配置]\n{json.dumps(self.config, ensure_ascii=False, indent=2)}\n\n"
            "输出 JSON：{\"action_signal\":\"...\"}"
        )

    def build_contract_system_prompt(self) -> str:
        return (
            "你是工业 contract 推理助手。\n"
            "请严格遵循下面的逻辑推理文档与配置，只能从候选条件集合中选择、删除或重分类条件。\n"
            "禁止凭空发明 condition。\n\n"
            f"[逻辑推理文档]\n{self.guide_text}\n\n"
            f"[配置]\n{json.dumps(self.config, ensure_ascii=False, indent=2)}\n\n"
            "输出 JSON：{\"guarantee\":[...],\"assumption\":[...]}"
        )
