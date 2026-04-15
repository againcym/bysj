# Contract Logic Reasoning Guide

## 目标
本文件不是硬编码动作结果，而是给 LLM 一套稳定的工业 contract 推理语义，使其在不同 step_desc 表达下仍按统一逻辑推理。

## 一、总流程
1. 从 step_desc 识别设备（device）
2. 结合 process_id、Hardware_Resource 和物流单元模式，推断当前动作角色
3. 在该设备的候选 output signal 中选择最合适的 action_signal
4. 根据 action_signal 查询设备状态机
5. 从状态机提取候选条件：
   - prev_action 的 target states -> 更偏 guarantee 候选
   - curr_action 的 source states -> 更偏 assumption 候选
6. 在候选条件中做筛选、排序和重分类
7. 最后由最终保留的 condition 反推 interface

## 二、设备识别原则
- 设备识别优先依赖 PPR 文本中的设备名
- 中文设备名必须先映射到统一设备前缀
- 一旦设备已确定，动作选择必须仅在该设备 outputs 中进行
- 不允许跨设备猜测动作

## 三、action_signal 选择原则
LLM 不自由生成 action，而是在该设备候选 output signals 中做选择。

### 通用优先级
1. reset 类：返回初始、回到原位、复位
2. motion 类：forward / backward / stop
3. transport 类：outbound / inbound / moveOut / moveIn
4. process 类：trackWelding / trackPainting / photoInspection
5. tool 类：pickUpTerminal / putDownTerminal

### 语义区分
- outbound：仓库臂把物料/成品从仓库侧送出到输送侧
- inbound：仓库臂把物料/成品从输送侧收回/入库到仓库侧
- moveOut：转移臂把物料从传送带末端/转移侧送到 mover 或下游承载体
- moveIn：转移臂把物料从 mover 或上游承载体送上传送带
- forward：传送带朝末端方向输送
- backward：传送带朝始端方向输送
- stop：传送带停止
- pickUpTerminal：拿起末端工具
- putDownTerminal：放下末端工具
- trackWelding：执行焊接
- trackPainting：执行涂装
- photoInspection：执行拍照检测

## 四、对称物流单元推理规则
对于由“仓库机器臂 + 传送带 + 转移机器臂”组成的物流单元，必须先判断该 process 的物流方向。

### 4.1 warehouse_to_mover（原料出库）
典型流程：
- 仓库臂把原料放上传送带 -> outbound
- 传送带向末端输送 -> forward
- 转移臂把原料从传送带末端取到 mover -> moveOut

典型设备组合：
- ARM1 + ConveyorBelt1 + ARM2

### 4.2 mover_to_warehouse（成品入库）
典型流程：
- 转移臂把成品放上传送带 -> moveIn
- 传送带向始端输送 -> backward
- 仓库臂把成品从传送带始端取回并入库 -> inbound

典型设备组合：
- ARM6 + ConveyorBelt2 + ARM7

### 4.3 传感器对应关系
- outbound / inbound 更接近仓库侧（始端）传感器
- moveOut / moveIn 更接近转移侧（末端）传感器
- conveyor stop 对应当前流向的目标端传感器
- 对 ConveyorBelt1:
  - first_sensor = CB1Sensor1
  - second_sensor = CB1Sensor2
- 对 ConveyorBelt2:
  - first_sensor = CB2Sensor1
  - second_sensor = CB2Sensor2

### 4.4 文本提示优先级
当 process 模式已知时：
- “出库至传送带” 对仓库臂优先映射 outbound
- “入库至仓库” 对仓库臂优先映射 inbound
- “转移至动子” 对转移臂优先映射 moveOut
- “转移至传送带” 对转移臂优先映射 moveIn
- “正转启动” 优先映射 forward
- “反转启动” 优先映射 backward
- “检测到时停止” 优先映射 stop

## 五、condition 候选提取原则
- prev_action.target_states.conditions -> guarantee 候选池
- curr_action.source_states.conditions -> assumption 候选池
- 不要凭空发明状态
- 不要把不存在于候选池中的 condition 加入最终输出，除非配置中允许补充 side effect

## 六、Guarantee / Assumption 分类规则
### 一般规则
- prev_action 完成后成立的结果，更偏 Guarantee
- curr_action 执行前必须满足的前置状态，更偏 Assumption

### reset link
- Guarantee 通常保留 prev_action 的 completion/effect
- Assumption 通常保留 curr_device 的 not at start position

### conveyor motion -> stop
- motion 状态（forward rotation / backward rotation）更偏 Guarantee
- sensor triggered 更偏 Assumption

### stop -> next arm
- stopped 更偏 Guarantee
- arm at start position 更偏 Assumption

### ARM5 painting chain
- pickUpTerminal -> trackPainting：picked up 更偏 Guarantee，Assumption 通常清空或最小化
- trackPainting -> putDownTerminal：completed 更偏 Guarantee，Assumption 通常清空或最小化
- putDownTerminal -> pickUpTerminal：put down 更偏 Guarantee，Assumption 通常清空，用于连续多次末端拿取/放下链路
- putDownTerminal -> reset：put down 更偏 Guarantee，not at start position 更偏 Assumption

### 对称物流单元补充
- warehouse_to_mover:
  - outbound -> reset：应保留 outbound completed，且可补始端传感器 side effect
  - forward -> stop：应保留 forward rotation，并优先 second sensor 作为 assumption
  - stop -> moveOut：应保留 stopped，并要求 transfer arm at start position
  - moveOut -> reset：应保留 moveOut completed，并可补末端传感器 side effect
- mover_to_warehouse:
  - moveIn -> reset：应保留 moveIn completed，并可补末端传感器 side effect
  - backward -> stop：应保留 backward rotation，并优先 first sensor 作为 assumption
  - stop -> inbound：应保留 stopped，并要求 warehouse arm at start position
  - inbound -> reset：应保留 inbound completed，并可补始端传感器 side effect

## 七、Interface 生成原则
- Interface 只能从最终保留的 Guarantee / Assumption 条件反推
- 一个主体通常只保留一个最核心 signal
- 若 signal 需要 alias，则按 alias 表映射
- 不要把自然语言中文说明直接当成 interface tokenDesc，除非配置明确允许

## 八、LLM 的职责边界
### LLM 负责
- 自然语言 step_desc 到候选 action_signal 的语义排序
- 结合 process 模式对 transport 动作做角色推理
- 候选 condition 的保留 / 删除 / 重分类
- 在候选集中选择最合理的 contract 语义表达

### 规则负责
- 中文设备名到设备前缀映射
- 候选 output signals 的边界
- 候选 state conditions 的来源边界
- 对称物流单元模式
- reset / conveyor / ARM5 等强约束归一化
- interface alias
- side effect 补充

原则：LLM 只在“受限候选空间”里推理，而不是自由生成整个 contract。

## 九、输出要求
对于 action_signal 选择：
{"action_signal":"..."}

对于 contract 分类：
{"guarantee":[{"S":"...","P":"is","O":"...","signal":"..."}],"assumption":[...]}
