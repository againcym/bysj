from pathlib import Path

# project 根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 顶层目录
BACKEND_DIR = PROJECT_ROOT / "Backend"
CONFIG_DIR = PROJECT_ROOT / "Config"
DEMO_DIR = PROJECT_ROOT / "demo"
DEMO_SM_DIR = DEMO_DIR / "state_machines"
MAIN_AGENT_DIR = PROJECT_ROOT / "Main_Agent"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
OUTPUT_CONTRACT_DIR = OUTPUTS_DIR / "contract"
OUTPUT_SM_DIR = OUTPUTS_DIR / "Device_OWLs_demo_exact"
REQUIREMENT_DIR = PROJECT_ROOT / "Requirement"
TRANSFORM_DIR = PROJECT_ROOT / "Transform"

# Transform 子目录
TRANSFORM_CONFIG_DIR = TRANSFORM_DIR / "config"
TRANSFORM_CONTRACTS_DIR = TRANSFORM_DIR / "contracts"
TRANSFORM_SIGNALS_DIR = TRANSFORM_DIR / "signals"
TRANSFORM_STATE_MACHINES_DIR = TRANSFORM_DIR / "state_machines"

# 常用文件
REQ_DOCX = REQUIREMENT_DIR / "requirement.docx"
FACTORY_OWL = REQUIREMENT_DIR / "factory_final_logic.owl"

PPR_OUTPUT_XML = OUTPUTS_DIR / "PPR_Final_logic.xml"
SIGNAL_OUTPUT_XML = OUTPUTS_DIR / "Signal_Definition.xml"

RULES_CONFIG_JSON = TRANSFORM_CONFIG_DIR / "rules_config.json"
PIN_TABLE_XLSX = TRANSFORM_SIGNALS_DIR / "signal_pin_table_from_manual.xlsx"

CONTRACT_OUTPUT_XML = OUTPUT_CONTRACT_DIR / "output_contract.xml"
CONTRACT_OUTPUT_LLMMAIN_XML = OUTPUT_CONTRACT_DIR / "output_contract_llmmain.xml"
# 新增：用于保存 step 级执行参数，不改变原 contract XML，仅并行输出 sidecar JSON
OPERATION_CONTEXT_JSON = OUTPUT_CONTRACT_DIR / "operation_context.json"

DEMO_CONTRACT_XML = DEMO_DIR / "contract_demo.xml"
DEMO_PPR_XML = DEMO_DIR / "ppr_demo.xml"
DEMO_SIGNAL_XML = DEMO_DIR / "Signal_Definition_demo.xml"

DEMO_OWL_FILES = {
    "ARM1": DEMO_SM_DIR / "ARM1_demo.owl",
    "ARM2": DEMO_SM_DIR / "ARM2_demo.owl",
    "ARM3": DEMO_SM_DIR / "ARM3_demo.owl",
    "ARM4": DEMO_SM_DIR / "ARM4_demo.owl",
    "ARM5": DEMO_SM_DIR / "ARM5_demo.owl",
    "ARM6": DEMO_SM_DIR / "ARM6_demo.owl",
    "ARM7": DEMO_SM_DIR / "ARM7_demo.owl",
    "Camera": DEMO_SM_DIR / "Camera_demo.owl",
    "ConveyorBelt1": DEMO_SM_DIR / "ConveyorBelt1_demo.owl",
    "ConveyorBelt2": DEMO_SM_DIR / "ConveyorBelt2_demo.owl",
}

GENERATED_OWL_FILES = {
    "ARM1": OUTPUT_SM_DIR / "ARM1_generated.owl",
    "ARM2": OUTPUT_SM_DIR / "ARM2_generated.owl",
    "ARM3": OUTPUT_SM_DIR / "ARM3_generated.owl",
    "ARM4": OUTPUT_SM_DIR / "ARM4_generated.owl",
    "ARM5": OUTPUT_SM_DIR / "ARM5_generated.owl",
    "ARM6": OUTPUT_SM_DIR / "ARM6_generated.owl",
    "ARM7": OUTPUT_SM_DIR / "ARM7_generated.owl",
    "Camera": OUTPUT_SM_DIR / "Camera_generated.owl",
    "ConveyorBelt1": OUTPUT_SM_DIR / "ConveyorBelt1_generated.owl",
    "ConveyorBelt2": OUTPUT_SM_DIR / "ConveyorBelt2_generated.owl",
    "Mover": OUTPUT_SM_DIR / "Mover_generated.owl",
}

CONTRACT_LOGIC_GUIDE_MD = TRANSFORM_CONFIG_DIR / "contract_logic_reasoning_guide.md"
CONTRACT_LOGIC_REASONING_CONFIG_JSON = TRANSFORM_CONFIG_DIR / "contract_logic_reasoning_config.json"
LOGIC_DOC_REASONING_CONTEXT_PY = TRANSFORM_CONTRACTS_DIR / "logic_doc_reasoning_context.py"


def ensure_output_dirs():
    OUTPUTS_DIR.mkdir(exist_ok=True)
    OUTPUT_CONTRACT_DIR.mkdir(exist_ok=True)
    OUTPUT_SM_DIR.mkdir(exist_ok=True)
