import sqlite3
import xml.etree.ElementTree as ET
import os

def convert_xml_to_db(xml_file="FactoryLayout.xml", db_name="FactoryData.db"):
    current_dir = os.path.dirname(os.path.abspath(__file__))
    xml_path = os.path.join(current_dir, xml_file)
    db_path = os.path.join(current_dir, db_name)
    
    if not os.path.exists(xml_path):
        print(f"错误: 找不到 XML 文件 {xml_path}")
        return

    if os.path.exists(db_path):
        os.remove(db_path)
        
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 1. 创建表结构
    cursor.executescript('''
        -- 物理实体 (仓库、工具站)
        CREATE TABLE Entities (id TEXT PRIMARY KEY, category TEXT, x REAL, y REAL, z REAL);

        -- 物料尺寸定义表
        CREATE TABLE Item_Specs (
            item_type TEXT PRIMARY KEY,
            length REAL, width REAL, height REAL
        );

        -- 库存与槽位信息表
        CREATE TABLE Inventory (
            item_type TEXT, 
            parent_id TEXT,
            level_index INTEGER, 
            slot_index INTEGER,
            offset_x REAL, offset_y REAL, offset_z REAL,
            status TEXT
        );

        -- 工具槽位表 (增加了 color 列)
        CREATE TABLE Tool_Slots (
            ts_id TEXT, 
            slot_index INTEGER, 
            tool_type TEXT, 
            color TEXT,
            description TEXT, 
            offset_x REAL, offset_y REAL, offset_z REAL
        );
    ''')

    tree = ET.parse(xml_path)
    root = tree.getroot()

    # 2. 解析物料定义
    for d in root.findall(".//ItemDefinition"):
        cursor.execute("INSERT INTO Item_Specs VALUES (?, ?, ?, ?)",
                       (d.get("type"), float(d.get("length")), float(d.get("width")), float(d.get("height"))))

    # 3. 处理仓库与库存
    for wh in root.findall(".//Warehouse"):
        wh_id = wh.get("id")
        cursor.execute("INSERT INTO Entities VALUES (?, ?, ?, ?, ?)", 
                       (wh_id, "Warehouse", float(wh.get("x")), float(wh.get("y")), float(wh.get("z"))))
        
        for layer in wh.findall(".//Layer"):
            lvl = int(layer.get("level", 0))
            layer_z = float(layer.get("offset_z", 0))
            layer_type = layer.get("type")
            
            if layer_type and not layer.findall("Slot"):
                status = layer.get("status")
                cursor.execute("INSERT INTO Inventory VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (layer_type, wh_id, lvl, 1, 0.0, 0.0, layer_z, status))
            
            for slot in layer.findall("Slot"):
                slot_type = slot.get("type")
                s_idx = int(slot.get("index", 0))
                status = slot.get("status")
                cursor.execute("INSERT INTO Inventory VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (slot_type, wh_id, lvl, s_idx, 
                     float(slot.get("offset_x", 0)), float(slot.get("offset_y", 0)), 
                     layer_z, status))

    # 4. 处理工具站 (增加 color 属性提取)
    for ts in root.findall(".//ToolStation"):
        ts_id = ts.get("id")
        cursor.execute("INSERT INTO Entities VALUES (?, ?, ?, ?, ?)", 
                       (ts_id, "ToolStation", float(ts.get("x")), float(ts.get("y")), float(ts.get("z"))))
        for s in ts.findall(".//Slot"):
            cursor.execute("INSERT INTO Tool_Slots VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (ts_id, 
                            int(s.get("index")), 
                            s.get("type"), 
                            s.get("color"), # 提取 XML 中的 color 属性
                            s.get("desc"), 
                            float(s.get("offset_x", 0)), 
                            float(s.get("offset_y", 0)), 
                            float(s.get("offset_z", 0))))

    conn.commit()
    conn.close()

if __name__ == "__main__":
    convert_xml_to_db()