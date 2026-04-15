from __future__ import annotations

import os
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Sequence, Tuple

try:
    from mcp.server.fastmcp import FastMCP  # type: ignore
except Exception:
    class FastMCP:  # pragma: no cover - local fallback for environments without MCP
        def __init__(self, name: str):
            self.name = name

        def tool(self):
            def decorator(func):
                return func
            return decorator

        def run(self):
            raise RuntimeError("FastMCP is unavailable in the current environment.")


mcp = FastMCP("SmartFactory_Pro")

current_dir = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(current_dir, "FactoryData.db")


@dataclass(frozen=True)
class InventorySummaryRecord:
    status: str
    count: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SlotRecord:
    parent_id: str
    level_index: int
    slot_index: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "parent_id": self.parent_id,
            "level_index": self.level_index,
            "slot_index": self.slot_index,
        }

    def material_coord_tokens(self) -> Tuple[str, str, str]:
        return (str(self.parent_id), str(self.level_index), str(self.slot_index))

    def material_coord_str(self) -> str:
        a, b, c = self.material_coord_tokens()
        return f"({a}, {b}, {c})"

    def coord_tuple(self) -> Tuple[int, int, int]:
        return (_safe_int(self.parent_id), int(self.level_index), int(self.slot_index))


@dataclass(frozen=True)
class ToolSlotRecord:
    ts_id: str
    slot_index: int
    tool_type: str
    color: str
    description: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)



def _safe_int(value: Any) -> int:
    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        digits = "".join(ch for ch in text if ch.isdigit())
        return int(digits) if digits else 0



def _connect_db() -> sqlite3.Connection:
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f"未找到数据库文件: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn



def execute_query(query: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
    conn = _connect_db()
    try:
        cur = conn.cursor()
        cur.execute(query, tuple(params))
        return cur.fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Structured data accessors
# ---------------------------------------------------------------------------
def get_inventory_summary_data() -> List[InventorySummaryRecord]:
    query = """
        SELECT status, COUNT(*) AS count
        FROM Inventory
        WHERE status != 'empty'
        GROUP BY status
        ORDER BY status
    """
    rows = execute_query(query)
    return [InventorySummaryRecord(status=str(row["status"]), count=int(row["count"])) for row in rows]



def query_all_material_locations_data(material_name: str) -> List[SlotRecord]:
    query = """
        SELECT parent_id, level_index, slot_index
        FROM Inventory
        WHERE status = ?
        ORDER BY parent_id, level_index, slot_index
    """
    rows = execute_query(query, (material_name,))
    return [
        SlotRecord(
            parent_id=str(row["parent_id"]),
            level_index=int(row["level_index"]),
            slot_index=int(row["slot_index"]),
        )
        for row in rows
    ]



def query_all_empty_slots_data(item_type: str) -> List[SlotRecord]:
    query = """
        SELECT parent_id, level_index, slot_index
        FROM Inventory
        WHERE item_type = ? AND status = 'empty'
        ORDER BY parent_id, level_index, slot_index
    """
    rows = execute_query(query, (item_type,))
    return [
        SlotRecord(
            parent_id=str(row["parent_id"]),
            level_index=int(row["level_index"]),
            slot_index=int(row["slot_index"]),
        )
        for row in rows
    ]



def query_tool_by_spec_data(tool_type: str, color: str) -> List[ToolSlotRecord]:
    query = """
        SELECT ts_id, slot_index, tool_type, color, description
        FROM Tool_Slots
        WHERE tool_type = ? AND color = ? COLLATE NOCASE
        ORDER BY ts_id, slot_index
    """
    rows = execute_query(query, (tool_type, color.strip().lower()))
    return [
        ToolSlotRecord(
            ts_id=str(row["ts_id"]),
            slot_index=int(row["slot_index"]),
            tool_type=str(row["tool_type"]),
            color=str(row["color"]),
            description=str(row["description"] or ""),
        )
        for row in rows
    ]



def query_all_tools_data() -> List[ToolSlotRecord]:
    query = """
        SELECT ts_id, slot_index, tool_type, color, description
        FROM Tool_Slots
        ORDER BY ts_id, slot_index
    """
    rows = execute_query(query)
    return [
        ToolSlotRecord(
            ts_id=str(row["ts_id"]),
            slot_index=int(row["slot_index"]),
            tool_type=str(row["tool_type"]),
            color=str(row["color"]),
            description=str(row["description"] or ""),
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Human-readable formatters kept for MCP tool compatibility
# ---------------------------------------------------------------------------
def _format_inventory_summary(records: Iterable[InventorySummaryRecord]) -> str:
    records = list(records)
    if not records:
        return "当前仓库为空，没有任何原料。"

    output = ["--- 当前库存物料汇总 ---"]
    total_items = 0
    for record in records:
        output.append(f"- {record.status}: {record.count} 个")
        total_items += record.count
    output.append("-----------------------")
    output.append(f"总计物料种类: {len(records)} | 总计数量: {total_items}")
    return "\n".join(output)



def _format_slot_records(records: Iterable[SlotRecord], header: str) -> str:
    records = list(records)
    if not records:
        return header
    lines = [header]
    for record in records:
        lines.append(
            f"- 仓库: {record.parent_id}, 层数: L{record.level_index}, 槽位: {record.slot_index}"
        )
    return "\n".join(lines)



def _format_tool_records(records: Iterable[ToolSlotRecord], empty_message: str, title: str | None = None) -> str:
    records = list(records)
    if not records:
        return empty_message
    lines = [title] if title else []
    if title:
        lines.append(f"共查询到 {len(records)} 个末端工具:")
    else:
        lines.append(f"找到 {len(records)} 个匹配工具:")
    for record in records:
        lines.append(
            f"- 工具站: {record.ts_id}, 槽位: {record.slot_index}, 类型: {record.tool_type}, 颜色: {record.color}, 描述: {record.description}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Existing MCP tool surface, now implemented on top of structured accessors
# ---------------------------------------------------------------------------
@mcp.tool()
def get_inventory_summary() -> str:
    return _format_inventory_summary(get_inventory_summary_data())


@mcp.tool()
def query_all_material_locations(material_name: str) -> str:
    records = query_all_material_locations_data(material_name)
    if not records:
        return f"库存中没有找到物料: {material_name}"
    return _format_slot_records(records, f"找到 {len(records)} 个 '{material_name}' 物料点:")


@mcp.tool()
def query_all_empty_slots(item_type: str) -> str:
    records = query_all_empty_slots_data(item_type)
    if not records:
        return f"类型 '{item_type}' 的存储区域已满（无空位）。"
    return _format_slot_records(records, f"找到 {len(records)} 个可用空位 (类型: {item_type}):")


@mcp.tool()
def query_tool_by_spec(tool_type: str, color: str) -> str:
    records = query_tool_by_spec_data(tool_type=tool_type, color=color)
    return _format_tool_records(
        records,
        empty_message=f"未找到匹配工具: 类型={tool_type}, 颜色={color}",
    )


@mcp.tool()
def query_all_tools() -> str:
    records = query_all_tools_data()
    return _format_tool_records(
        records,
        empty_message="系统中未配置任何末端工具信息。",
        title="=== 系统全部末端工具信息汇总 ===",
    )


if __name__ == "__main__":
    mcp.run()
