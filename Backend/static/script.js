const state = {
  stageOrder: [],
  stageMeta: {},
  stages: {},
  summary: {
    order: '',
    target_product: '待推理',
    material_count: 0,
    painting_operation_count: 0,
    ppr_object_count: 0,
    used_device_count: 0,
    state_machine_count: 0,
    process_contract_count: 0,
    step_mapping_count: 0,
    operation_link_count: 0,
    contract_node_count: 0,
    contract_link_count: 0,
    station_count: 0,
  },
  completedStages: new Set(),
  activeStageId: null,
  artifacts: {},
  finalResult: null,
  statusLines: [],
};

const examplesEl = document.getElementById('examples');
const chatLog = document.getElementById('chat-log');
const composer = document.getElementById('composer');
const orderInput = document.getElementById('order-input');
const runBtn = document.getElementById('run-btn');
const runtimeStatus = document.getElementById('runtime-status');
const progressSteps = document.getElementById('progress-steps');
const liveStatus = document.getElementById('live-status');
const summaryMetrics = document.getElementById('summary-metrics');
const stageSections = document.getElementById('stage-sections');
const rawViewer = document.getElementById('raw-viewer');
const openContractBtn = document.getElementById('open-contract-btn');

init();

async function init() {
  bindComposer();
  bindSourceToolbar();
  bindOpenContract();
  await loadExamples();
  renderMetrics();
}

async function loadExamples() {
  try {
    const res = await fetch('/api/examples');
    const data = await res.json();
    examplesEl.innerHTML = (data.examples || []).map(example => `
      <button class="example-chip" type="button">${escapeHtml(example)}</button>
    `).join('');
    examplesEl.querySelectorAll('.example-chip').forEach(btn => {
      btn.addEventListener('click', () => {
        orderInput.value = btn.textContent.trim();
        orderInput.focus();
      });
    });
  } catch (error) {
    examplesEl.innerHTML = '<div class="muted">示例加载失败</div>';
  }
}

function bindComposer() {
  composer.addEventListener('submit', async (event) => {
    event.preventDefault();
    const order = orderInput.value.trim();
    if (!order) return;

    resetRun(order);
    appendChat('user', order);
    appendChat('assistant', '已开始执行：阶段结果会逐步显示。');

    try {
      await runStreamingPipeline(order, document.getElementById('use-llm').checked);
      setRuntimeStatus('Done', 'done');
      appendChat('assistant', '整条链路已完成。');
    } catch (error) {
      setRuntimeStatus('Error', 'error');
      appendChat('error', error.message || '运行失败');
    } finally {
      setRunning(false);
    }
  });
}

function bindSourceToolbar() {
  document.querySelectorAll('.source-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.source-btn').forEach(x => x.classList.remove('active'));
      btn.classList.add('active');
      renderRawSource(btn.dataset.source);
    });
  });
}

function bindOpenContract() {
  if (!openContractBtn) return;
  openContractBtn.addEventListener('click', async () => {
    const originalText = openContractBtn.textContent;
    openContractBtn.disabled = true;
    openContractBtn.textContent = '正在打开...';

    try {
      const response = await fetch('/api/open-contract', { method: 'POST' });
      const data = await response.json();
      if (!response.ok || !data.ok) {
        throw new Error(data.error || '打开 Contract 失败');
      }
      const syncErrors = Array.isArray(data.sync_errors) ? data.sync_errors : [];
      if (syncErrors.length) {
        appendChat('assistant', `已启动 Contract 可视化工具，但 Contract 文件未能自动同步到 tool1118。\n${syncErrors.join('\n')}`);
      } else {
        appendChat('assistant', '已启动 Contract 可视化工具，并同步最新的 output_contract_llmmain.xml。');
      }
    } catch (error) {
      appendChat('error', error.message || '打开 Contract 失败');
    } finally {
      openContractBtn.disabled = false;
      openContractBtn.textContent = originalText;
    }
  });
}

function resetRun(order) {
  state.summary = {
    order,
    target_product: '待推理',
    material_count: 0,
    painting_operation_count: 0,
    ppr_object_count: 0,
    used_device_count: 0,
    state_machine_count: 0,
    process_contract_count: 0,
    step_mapping_count: 0,
    operation_link_count: 0,
    contract_node_count: 0,
    contract_link_count: 0,
    station_count: 0,
  };
  state.stageOrder = [];
  state.stageMeta = {};
  state.stages = {};
  state.completedStages = new Set();
  state.activeStageId = null;
  state.artifacts = {};
  state.finalResult = null;
  state.statusLines = [];
  stageSections.className = 'stage-sections';
  stageSections.innerHTML = '';
  liveStatus.innerHTML = '';
  progressSteps.innerHTML = '';
  rawViewer.textContent = '运行中，完成后可查看源文件。';
  renderMetrics();
  setRunning(true);
  setRuntimeStatus('Running', 'running');
}

async function runStreamingPipeline(order, useLlm) {
  const response = await fetch('/api/pipeline/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ order, use_llm: useLlm }),
  });

  if (!response.ok || !response.body) {
    const text = await response.text();
    throw new Error(text || '无法连接到后端流水线。');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    let index;
    while ((index = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, index).trim();
      buffer = buffer.slice(index + 1);
      if (!line) continue;
      handlePipelineEvent(JSON.parse(line));
    }
  }

  if (buffer.trim()) {
    handlePipelineEvent(JSON.parse(buffer.trim()));
  }
}

function handlePipelineEvent(event) {
  if (!event) return;

  if (event.event === 'bootstrap') {
    state.summary = { ...state.summary, ...(event.summary || {}) };
    state.stageOrder = event.stage_order || [];
    state.stageMeta = Object.fromEntries((state.stageOrder || []).map(item => [item.id, item]));
    renderProgress();
    renderMetrics();
    return;
  }

  if (event.event === 'status') {
    state.activeStageId = event.stage_id || null;
    state.statusLines.push({ stage_id: event.stage_id, message: event.message });
    renderProgress();
    renderLiveStatus();
    return;
  }

  if (event.event === 'stage') {
    const stage = event.stage;
    state.stages[stage.id] = stage;
    state.completedStages.add(stage.id);
    if (event.summary_patch) {
      state.summary = { ...state.summary, ...event.summary_patch };
      renderMetrics();
    }
    renderProgress();
    renderStage(stage);
    appendChat('assistant', `${stage.title} 已完成。`);
    return;
  }

  if (event.event === 'done') {
    state.finalResult = event.result || {};
    state.artifacts = state.finalResult.artifacts || {};
    state.summary = { ...state.summary, ...(state.finalResult.summary || {}) };
    renderMetrics();
    renderRawSource(document.querySelector('.source-btn.active')?.dataset.source || 'ppr_xml');
    return;
  }

  if (event.event === 'error') {
    appendChat('error', event.error || '运行失败');
    if (event.traceback) {
      const errorBlock = document.createElement('section');
      errorBlock.className = 'stage-shell error-shell';
      errorBlock.innerHTML = `
        <div class="stage-head-static">
          <div class="stage-head-main">
            <div class="phase-tag">运行失败</div>
            <h3>后端错误</h3>
          </div>
        </div>
        <div class="stage-body">
          <pre class="code-viewer">${escapeHtml(event.traceback)}</pre>
        </div>
      `;
      stageSections.appendChild(errorBlock);
    }
    throw new Error(event.error || '流水线异常终止');
  }
}

function renderProgress() {
  const active = state.activeStageId;
  progressSteps.innerHTML = (state.stageOrder || []).map(item => {
    let status = 'pending';
    if (state.completedStages.has(item.id)) status = 'done';
    else if (active === item.id) status = 'active';
    return `
      <div class="progress-step ${status}">
        <div class="progress-dot"></div>
        <div class="progress-title">${escapeHtml(item.title)}</div>
      </div>
    `;
  }).join('');
}

function renderLiveStatus() {
  liveStatus.innerHTML = state.statusLines.slice(-8).map(line => `
    <div class="status-line">
      <span class="status-badge">${escapeHtml(state.stageMeta[line.stage_id]?.title || line.stage_id || 'Pipeline')}</span>
      <span>${escapeHtml(line.message)}</span>
    </div>
  `).join('');
}

function renderMetrics() {
  const metrics = [
    ['目标产品', state.summary.target_product || '待推理'],
    ['模板站点数', state.summary.station_count || 0],
    ['原料数', state.summary.material_count || 0],
    ['涂装操作数', state.summary.painting_operation_count || 0],
    ['PPR 对象数', state.summary.ppr_object_count || 0],
    ['使用设备数', state.summary.used_device_count || 0],
    ['状态机数', state.summary.state_machine_count || 0],
    ['Process 合约数', state.summary.process_contract_count || 0],
    ['Step 映射数', state.summary.step_mapping_count || 0],
    ['Operation Link 数', state.summary.operation_link_count || 0],
    ['Contract 节点数', state.summary.contract_node_count || 0],
    ['Contract 边数', state.summary.contract_link_count || 0],
  ];
  summaryMetrics.innerHTML = metrics.map(([label, value]) => `
    <div class="metric-card">
      <div class="metric-label">${escapeHtml(label)}</div>
      <div class="metric-value">${escapeHtml(String(value ?? '-'))}</div>
    </div>
  `).join('');
}

function renderStage(stage) {
  const existing = document.getElementById(`stage-${stage.id}`);
  const wasOpen = existing ? existing.open : true;
  const html = renderStageBody(stage, wasOpen);
  if (existing) {
    existing.outerHTML = html;
  } else {
    stageSections.insertAdjacentHTML('beforeend', html);
  }
}

function renderStageBody(stage, open = true) {
  return `
    <details id="stage-${stage.id}" class="stage-shell" ${open ? 'open' : ''}>
      <summary class="stage-head">
        <div class="stage-head-main">
          <div class="phase-tag">${escapeHtml(stage.phase || '')}</div>
          <h3>${escapeHtml(stage.title || stage.id)}</h3>
        </div>
        <div class="chip-row">
          ${(stage.reasoning_badges || []).map(badge => `<span class="chip">${escapeHtml(badge)}</span>`).join('')}
        </div>
      </summary>
      <div class="stage-body">
        ${renderStageContent(stage)}
      </div>
    </details>
  `;
}

function renderStageContent(stage) {
  switch (stage.view_type) {
    case 'requirement':
      return renderRequirementStage(stage.data);
    case 'physical':
      return renderPhysicalStage(stage.data);
    case 'ontology':
      return renderOntologyStage(stage.data);
    case 'ppr':
      return renderPprStage(stage.data);
    case 'state_machine':
      return renderStateMachineStage(stage.data);
    case 'contract_process':
      return renderContractProcessStage(stage.data);
    case 'contract_step':
      return renderContractStepStage(stage.data);
    case 'contract_link':
      return renderContractLinkStage(stage.data);
    default:
      return `<div class="muted">暂无渲染器：${escapeHtml(stage.view_type || stage.id)}</div>`;
  }
}

function renderRequirementStage(data) {
  const stations = data.stations || [];
  return `
    <div class="card-grid station-grid">
      ${stations.map(station => `
        <article class="info-card station-card">
          <div class="card-title-row">
            <div>
              <h4>${escapeHtml(station.title)}</h4>
              <div class="muted small-text">${escapeHtml(station.task_id)}</div>
            </div>
            <span class="minor-chip">${escapeHtml(String((station.steps || []).length))} 步</span>
          </div>

          <div class="subblock compact-block">
            <div class="subblock-title">启动条件</div>
            <div class="pill-row">${(station.trigger_conditions || []).map(item => `<span class="tag">${escapeHtml(item)}</span>`).join('') || '<span class="muted">无</span>'}</div>
          </div>

          <div class="subblock compact-block">
            <div class="subblock-title">资源提示</div>
            <div class="pill-row">${(station.resource_names || []).map(item => `<span class="tag soft">${escapeHtml(item)}</span>`).join('') || '<span class="muted">无</span>'}</div>
          </div>

          ${renderDisclosureBlock('初始位置', `<ul class="bullet-list compact">${(station.initial_positions || []).map(item => `<li>${escapeHtml(item)}</li>`).join('')}</ul>`, false)}
          ${renderDisclosureBlock('步骤骨架', `<ol class="ordered-list compact">${(station.steps || []).map(item => `<li>${escapeHtml(item)}</li>`).join('')}</ol>`, false)}
        </article>
      `).join('')}
    </div>
  `;
}


function renderPhysicalStage(data) {
  const materials = data.materials || [];
  const paintingOps = data.painting_operations || [];
  const warehousing = data.warehousing || {};

  return `
    <div class="card-grid physical-grid refined-physical-grid">
      <article class="info-card physical-card order-card emphasis-card">
        <div class="card-title-row">
          <h4>订单识别</h4>
          <span class="minor-chip">${escapeHtml(data.match_status || '-')}</span>
        </div>
        <div class="hero-value">${escapeHtml(data.target_product || '-')}</div>
      </article>

      ${materials.map(item => `
        <article class="info-card physical-card match-card">
          <div class="card-title-row">
            <h4>${escapeHtml(item.material_name)}</h4>
            <span class="minor-chip">原料</span>
          </div>
          <div class="match-location-box">
            <div class="match-location-label">最终匹配位置</div>
            <div class="match-location-value">${escapeHtml(item.chosen_label || '未定位')}</div>
          </div>
        </article>
      `).join('')}

      <article class="info-card physical-card match-card">
        <div class="card-title-row">
          <h4>成品入库位</h4>
          <span class="minor-chip">${escapeHtml(warehousing.item_type || '-')}</span>
        </div>
        <div class="match-location-box">
          <div class="match-location-label">目标位置</div>
          <div class="match-location-value">${escapeHtml(warehousing.slot_label || warehousing.coord_str || '-')}</div>
        </div>
      </article>
    </div>

    <div class="section-mini-title">涂装 / 写字 / 描边对应的末端匹配</div>
    <div class="paint-ops-lane">
      ${paintingOps.length ? paintingOps.map(item => `
        <article class="info-card physical-card paint-op-card grouped-paint-card">
          <div class="card-title-row">
            <h4>${escapeHtml(formatOperationKind(item.operation_kind))}</h4>
            <span class="minor-chip">${escapeHtml(item.color || '-')}</span>
          </div>
          <div class="paint-op-strip">
            <div class="paint-mini-block wide">
              <div class="paint-mini-label">需求语义</div>
              <div class="paint-mini-value">${escapeHtml(item.instruction_text || '')}</div>
            </div>
            <div class="paint-mini-block">
              <div class="paint-mini-label">末端类型</div>
              <div class="paint-mini-value">${escapeHtml(item.tool_type || '-')}</div>
            </div>
            <div class="paint-mini-block">
              <div class="paint-mini-label">工具站</div>
              <div class="paint-mini-value">${escapeHtml(item.tool_station || '-')}</div>
            </div>
            <div class="paint-mini-block">
              <div class="paint-mini-label">槽位</div>
              <div class="paint-mini-value">${escapeHtml(String(item.tool_slot || '-'))}</div>
            </div>
            <div class="paint-mini-block wide">
              <div class="paint-mini-label">说明</div>
              <div class="paint-mini-value">${escapeHtml(item.tool_desc || '-')}</div>
            </div>
          </div>
        </article>
      `).join('') : '<div class="empty-inline">该订单没有触发喷涂 / 写字 / 描边末端匹配。</div>'}
    </div>
  `;
}


function renderOntologyStage(data) {
  return `
    <article class="info-card graph-card ontology-structure-card">
      <div class="card-title-row">
        <h4>Ontology 基础结构图</h4>
        <span class="minor-chip">OWL / RDF</span>
      </div>
      ${renderOntologySchemaGraph(data.structure_graph)}
    </article>

    <div class="section-mini-title">每个任务的 ontology 校验结果</div>
    <div class="card-grid ontology-validation-grid ontology-task-grid-refined">
      ${(data.tasks || []).map(item => `
        <article class="info-card ontology-task-card stable-task-card">
          <div class="card-title-row stable-title-row ontology-task-title-row">
            <div>
              <h4 class="ontology-task-object-id">${escapeHtml(item.object_id)}</h4>
              <div class="muted small-text">${escapeHtml(item.station_title || item.task_id || '')}</div>
            </div>
          </div>

          <div class="task-flow-box resource-box">
            <div class="flow-box-title">通过校验的资源</div>
            <div class="pill-row tight">${(item.validated_resources || []).map(r => `<span class="tag">${escapeHtml(r)}</span>`).join('') || '<span class="muted">无通过校验的资源</span>'}</div>
          </div>

          <div class="task-card-flow-column">
            <div class="flow-node task-node stretch-node compact-flow-node">${escapeHtml(item.task_id)}</div>
            <span class="flow-sep flow-down">↓</span>
            <div class="flow-node product-node stretch-node compact-flow-node">${escapeHtml(item.ontology_product_label || '-')}</div>
          </div>
        </article>
      `).join('')}
    </div>
  `;
}

function renderPprStage(data) {
  const objects = data.objects || [];
  return `
    <div class="flow-lane">
      ${objects.map((obj, index) => `
        <article class="flow-card">
          <div class="flow-card-head ppr-flow-card-head">
            <span class="minor-chip">#${escapeHtml(String(obj.seq))}</span>
            <h4 class="ppr-object-title">${escapeHtml(obj.object_id)}</h4>
          </div>
          <div class="pill-row tight">
            <span class="tag soft">${escapeHtml(obj.product_class || '-')}</span>
            ${(obj.hardware_resources || []).map(item => `<span class="tag">${escapeHtml(item)}</span>`).join('')}
          </div>
          <div class="info-strip">${escapeHtml(obj.material_resource || '-')}</div>
          <div class="kv-grid compact">
            <div class="kv-key">From</div><div class="kv-value">${escapeHtml((obj.from_conditions || []).join('，') || '-')}</div>
            <div class="kv-key">To</div><div class="kv-value">${escapeHtml(obj.to_condition || '-')}</div>
          </div>
          <div class="step-timeline">
            ${(obj.steps || []).map(step => `
              <div class="timeline-step">
                <div class="timeline-index">${escapeHtml(step.id)}</div>
                <div class="timeline-body">
                  <strong>${escapeHtml(step.name)}</strong>
                  <div>${escapeHtml(step.desc)}</div>
                </div>
              </div>
            `).join('')}
          </div>
        </article>
        ${index < objects.length - 1 ? '<div class="flow-sep">→</div>' : ''}
      `).join('')}
    </div>
  `;
}


function renderStateMachineStage(data) {
  const devices = data.devices || [];
  return `
    <div class="legend-row">
      <span class="legend-pill active-dot">当前订单实际使用的设备</span>
      <span class="legend-pill">已生成但本订单未使用</span>
    </div>
    <div class="card-grid state-machine-grid">
      ${devices.map(device => `
        <article class="info-card state-card ${device.used_in_pipeline ? 'highlight-card' : ''}">
          <div class="card-title-row">
            <div>
              <h4>${escapeHtml(device.device_name)}</h4>
              <div class="muted small-text">${escapeHtml(device.reason || '')}</div>
            </div>
            <span class="minor-chip">${escapeHtml(device.template || '-')}</span>
          </div>

          <div class="state-meta-row">
            <span>状态 ${escapeHtml(String(device.state_count || 0))}</span>
            <span>动作 ${escapeHtml(String(device.action_count || 0))}</span>
          </div>

          <div class="state-diagram-wrap">
            ${renderStateMachineSvg(device, { variant: 'card', showEdgeLabels: true })}
          </div>

          ${renderDisclosureBlock(
            '查看动作 / 转移关系',
            `
            <div class="subblock compact-block">
              <div class="subblock-title">动作标签</div>
              <div class="pill-row">${(device.actions || []).map(action => `<span class="tag">${escapeHtml(shortActionName(action.action))}</span>`).join('') || '<span class="muted">无</span>'}</div>
            </div>
            <div class="subblock compact-block">
              <div class="subblock-title">状态标签</div>
              <ul class="bullet-list compact">${(device.states || []).map(item => `<li><strong>${escapeHtml(formatStateId(item.state_id))}</strong>：${escapeHtml(stateSubtitleShort(item.label || ''))}</li>`).join('')}</ul>
            </div>
            <div class="subblock compact-block">
              <div class="subblock-title">转移关系</div>
              ${renderStateTransitionList(device.states || [])}
            </div>
            `,
            false
          )}
        </article>
      `).join('')}
    </div>
  `;
}


function renderContractProcessStage(data) {
  const views = data.process_views || [];
  return `
    <div class="process-stack compact-stack">
      ${views.map(view => `
        <details class="process-card process-entry-card foldable-process">
          <summary class="process-card-head process-summary-head">
            <div>
              <h4>${escapeHtml(view.process_id)}</h4>
              <div class="process-subtitle">${escapeHtml(view.product_class || '')} / ${escapeHtml(view.product_specific || '')}</div>
            </div>
          </summary>

          <div class="process-card-body entry-grid process-entry-grid">
            <div class="logic-panel">
              <div class="panel-mini-title">PPR 入口与资源</div>
              <div class="logic-stack">
                <div class="subblock compact-block">
                  <div class="subblock-title">入口条件</div>
                  ${renderTextPills(view.from_conditions || [], 'soft')}
                </div>
                <div class="subblock compact-block">
                  <div class="subblock-title">Hardware_Resource</div>
                  ${renderTextPills(view.hardware_resources || [], '')}
                </div>
              </div>
            </div>

            <div class="logic-panel">
              <div class="panel-mini-title">Guarantee / Assumption</div>
              <div class="logic-stack">
                <div class="subblock compact-block">
                  <div class="subblock-title">Guarantee</div>
                  ${renderConditionList(view.process_entry?.guarantee || [], true)}
                </div>
                <div class="subblock compact-block">
                  <div class="subblock-title">Assumption</div>
                  ${renderConditionList(view.process_entry?.assumption || [], true)}
                </div>
              </div>
            </div>

            <div class="logic-panel">
              <div class="panel-mini-title">Interface</div>
              ${renderInterfaceList(view.process_entry?.interface || [], true)}
            </div>
          </div>
        </details>
      `).join('')}
    </div>
  `;
}


function renderContractStepStage(data) {
  const views = data.process_views || [];
  return `
    <div class="process-stack compact-stack">
      ${views.map(view => `
        <details class="process-card foldable-process">
          <summary class="process-card-head process-summary-head">
            <div>
              <h4>${escapeHtml(view.process_id)}</h4>
              <div class="process-subtitle">按步骤做 device / action 映射</div>
            </div>
          </summary>
          <div class="process-card-body step-mapping-grid">
            ${(view.steps || []).map(step => `
              <article class="mapping-card unified-card">
                <div class="mapping-header unified-header">
                  <span class="mapping-index">${escapeHtml(step.step_id || '')}</span>
                  <div class="mapping-title-wrap">
                    <strong>${escapeHtml(step.step_name || '')}</strong>
                    <div class="muted">${escapeHtml(step.step_desc || '')}</div>
                  </div>
                </div>
                <div class="mapping-result">
                  <span class="tag">${escapeHtml(step.device_name || '未识别设备')}</span>
                  <span class="flow-sep inline">→</span>
                  <span class="tag strong">${escapeHtml(step.action_signal || '未识别动作')}</span>
                </div>
                ${renderDisclosureBlock(
                  '匹配依据',
                  `<ul class="bullet-list compact">${(step.reasoning_lines || []).map(line => `<li>${escapeHtml(line)}</li>`).join('')}</ul>`,
                  false
                )}
                ${renderDisclosureBlock(
                  '查看该设备的候选输出',
                  `
                  <div class="candidate-list">
                    ${(step.candidate_outputs || []).map(item => `
                      <div class="candidate-item">
                        <strong>${escapeHtml(item.name || '')}</strong>
                        <span>${escapeHtml(item.display_desc || '')}</span>
                        <code>${escapeHtml(item.address || '')}</code>
                      </div>
                    `).join('') || '<div class="muted">无候选输出</div>'}
                  </div>
                  `,
                  false
                )}
              </article>
            `).join('')}
          </div>
        </details>
      `).join('')}
    </div>
  `;
}


function renderContractLinkStage(data) {
  const views = data.process_views || [];
  return `
    <div class="process-stack compact-stack">
      ${views.map(view => `
        <details class="process-card foldable-process">
          <summary class="process-card-head process-summary-head">
            <div>
              <h4>${escapeHtml(view.process_id)}</h4>
              <div class="process-subtitle">相邻步骤之间的 Contract link 推理</div>
            </div>
            <span class="minor-chip">${escapeHtml(String((view.links || []).length))} 条 link</span>
          </summary>

          <div class="process-card-body">
            ${(view.links || []).length ? (view.links || []).map(link => `
              <article class="reason-card structured-reason-card">
                <div class="reason-card-head">
                  <div class="reason-title-wrap">
                    <strong>${escapeHtml(link.from_step.step_name || '')}</strong>
                    <span class="flow-sep inline">→</span>
                    <strong>${escapeHtml(link.to_step.step_name || '')}</strong>
                  </div>
                  <span class="minor-chip">${escapeHtml(link.guide_rule_label || '状态机推理')}</span>
                </div>

                ${renderDisclosureBlock(
                  '推理说明',
                  `
                  <div class="reason-rule">${escapeHtml(link.guide_rule_desc || '')}</div>
                  <ul class="bullet-list compact">${(link.reasoning_lines || []).map(line => `<li>${escapeHtml(line)}</li>`).join('')}</ul>
                  `,
                  false
                )}

                <div class="reason-stage-stack">
                  <div class="reason-candidate-row">
                    <div class="logic-panel">
                      <div class="panel-mini-title">上一动作 target states 候选</div>
                      ${renderConditionList(link.prev_target_conditions || [], true)}
                    </div>
                    <div class="logic-panel">
                      <div class="panel-mini-title">下一动作 source states 候选</div>
                      ${renderConditionList(link.curr_source_conditions || [], true)}
                    </div>
                  </div>
                  <div class="machine-stage-row">
                    ${renderLinkStateMachinePanels(link)}
                  </div>
                </div>

                <div class="logic-panel final-contract-panel">
                  <div class="panel-mini-title">最终 Contract 结果</div>
                  <div class="final-contract-grid">
                    <div>
                      <div class="panel-mini-title muted-title">Guarantee</div>
                      ${renderConditionList(link.guarantee || [], true)}
                    </div>
                    <div>
                      <div class="panel-mini-title muted-title">Assumption</div>
                      ${renderConditionList(link.assumption || [], true)}
                    </div>
                    <div>
                      <div class="panel-mini-title muted-title">Interface</div>
                      ${renderInterfaceList(link.interface || [], true)}
                    </div>
                  </div>
                </div>
              </article>
            `).join('') : '<div class="empty-inline">该工作站没有相邻步骤 link 需要推理。</div>'}
          </div>
        </details>
      `).join('')}
    </div>
  `;
}


function renderLinkStateMachinePanels(link) {
  const prev = link.prev_state_machine;
  const curr = link.curr_state_machine;
  if (!prev && !curr) return '<div class="empty-inline">无状态机图</div>';

  if (prev && curr && prev.device_name === curr.device_name) {
    return `
      <article class="info-card compact-card machine-card single-machine-card">
        <div class="card-title-row">
          <h5>${escapeHtml(prev.device_name)} 状态机</h5>
          <span class="minor-chip">同一设备</span>
        </div>
        ${renderStateMachineSvg(prev, { variant: 'reason', showEdgeLabels: true })}
      </article>
    `;
  }

  return `
    <div class="state-compare-grid stacked-machine-grid">
      <article class="info-card compact-card machine-card">
        <div class="card-title-row">
          <h5>${escapeHtml(link.from_step.device_name || '-')} 状态机</h5>
          <span class="minor-chip">上一动作</span>
        </div>
        ${prev ? renderStateMachineSvg(prev, { variant: 'reason', showEdgeLabels: true }) : '<div class="empty-inline">无状态机图</div>'}
      </article>
      <article class="info-card compact-card machine-card">
        <div class="card-title-row">
          <h5>${escapeHtml(link.to_step.device_name || '-')} 状态机</h5>
          <span class="minor-chip">下一动作</span>
        </div>
        ${curr ? renderStateMachineSvg(curr, { variant: 'reason', showEdgeLabels: true }) : '<div class="empty-inline">无状态机图</div>'}
      </article>
    </div>
  `;
}

function renderDisclosureBlock(title, innerHtml, open = false) {
  return `
    <details class="inline-details" ${open ? 'open' : ''}>
      <summary>${escapeHtml(title)}</summary>
      <div class="details-body">${innerHtml}</div>
    </details>
  `;
}

function renderConditionColumns(guarantee, assumption) {
  return `
    <div class="condition-columns">
      <div>
        <div class="panel-mini-title">Guarantee</div>
        ${renderConditionList(guarantee, true)}
      </div>
      <div>
        <div class="panel-mini-title">Assumption</div>
        ${renderConditionList(assumption, true)}
      </div>
    </div>
  `;
}

function renderConditionList(items, compact = false) {
  if (!items || !items.length) {
    return '<div class="empty-inline">无</div>';
  }
  return `
    <ul class="condition-list ${compact ? 'compact' : ''}">
      ${items.map(item => `
        <li>
          <span>${escapeHtml(item.text || '')}</span>
          ${item.signal ? `<code>${escapeHtml(item.signal)}</code>` : ''}
        </li>
      `).join('')}
    </ul>
  `;
}

function renderInterfaceList(items, compact = false) {
  if (!items || !items.length) {
    return '<div class="empty-inline">无</div>';
  }
  return `
    <div class="interface-list ${compact ? 'compact' : ''}">
      ${items.map(item => `
        <div class="interface-item">
          <span>${escapeHtml(item.subject || '')}</span>
          <span class="flow-sep inline">→</span>
          <code>${escapeHtml(item.display_signal || item.signal || '')}</code>
        </div>
      `).join('')}
    </div>
  `;
}

function renderTextPills(items, mode = '') {
  if (!items || !items.length) return '<div class="empty-inline">无</div>';
  return `<div class="pill-row">${items.map(item => `<span class="tag ${mode}">${escapeHtml(item)}</span>`).join('')}</div>`;
}


function renderStateTransitionList(states) {
  const rows = [];
  (states || []).forEach(state => {
    (state.transitions || []).forEach(transition => {
      rows.push(`
        <li>
          <strong>${escapeHtml(formatStateId(state.state_id))}</strong>
          <span class="flow-sep inline">→</span>
          <strong>${escapeHtml(formatStateId(transition.target))}</strong>
          <code>${escapeHtml(shortActionName(transition.action))}</code>
        </li>
      `);
    });
  });
  if (!rows.length) return '<div class="empty-inline">无转移关系</div>';
  return `<ul class="bullet-list compact state-transition-list">${rows.join('')}</ul>`;
}

function renderRawSource(key) {
  const content = state.artifacts?.[key];
  rawViewer.textContent = content || '运行完成后可在这里查看源文件。';
}

function appendChat(role, text) {
  const message = document.createElement('div');
  message.className = `chat-msg ${role}`;
  message.innerHTML = `
    <div class="chat-meta">${role === 'user' ? '用户' : role === 'assistant' ? '系统' : '错误'}</div>
    <div class="chat-body">${escapeHtml(text).replace(/\n/g, '<br>')}</div>
  `;
  chatLog.appendChild(message);
  chatLog.scrollTop = chatLog.scrollHeight;
}

function setRunning(running) {
  runBtn.disabled = running;
  orderInput.disabled = running;
  document.getElementById('use-llm').disabled = running;
}

function setRuntimeStatus(text, cls = '') {
  runtimeStatus.textContent = text;
  runtimeStatus.className = 'status-pill';
  if (cls) runtimeStatus.classList.add(cls);
}

function formatSlot(slot) {
  if (!slot) return '未定位';
  const warehouse = slot.parent_id || slot.ts_id || '-';
  const level = slot.level_index !== undefined && slot.level_index !== null && slot.level_index !== '' ? `${slot.level_index}层` : '';
  const slotIndex = slot.slot_index !== undefined && slot.slot_index !== null && slot.slot_index !== '' ? `${slot.slot_index}槽` : '';
  return [warehouse, level, slotIndex].filter(Boolean).join(' · ');
}

function renderOntologyRuntimeBoard(tasks) {
  if (!tasks || !tasks.length) {
    return '<div class="empty-inline">无运行时校验信息。</div>';
  }
  return `
    <div class="runtime-board">
      ${tasks.map(item => `
        <article class="runtime-task-card">
          <div class="runtime-task-title">${escapeHtml(item.object_id)}</div>
          <div class="runtime-task-subtitle">${escapeHtml(item.task_id || '')}</div>
          <div class="runtime-task-flow">
            <div class="runtime-task-group resources">${(item.validated_resources || []).map(r => `<span class="tag">${escapeHtml(r)}</span>`).join('') || '<span class="muted">无资源</span>'}</div>
            <span class="flow-sep">→</span>
            <div class="flow-node task-node">${escapeHtml(item.task_id)}</div>
            <span class="flow-sep">→</span>
            <div class="flow-node product-node">${escapeHtml(item.ontology_product_label || '-')}</div>
          </div>
        </article>
      `).join('')}
    </div>
  `;
}

function renderOntologySchemaGraph(graph) {
  const nodes = (graph?.nodes || []).filter(node => node.id !== 'schema::PPR');
  const edges = (graph?.edges || []).filter(
    edge => edge.from !== 'schema::PPR' && edge.to !== 'schema::PPR'
  );
  if (!nodes.length) {
    return '<div class="empty-inline">当前没有可展示的图结构。</div>';
  }

  const width = graph?.width || 1280;
  const height = graph?.height || 960;
  const positions = Object.fromEntries(nodes.map(node => [node.id, node]));
  const prefix = `schema-${Math.random().toString(36).slice(2, 8)}`;

  const svgEdges = edges.map((edge, idx) => {
    const from = positions[edge.from];
    const to = positions[edge.to];
    if (!from || !to) return '';
    const markerId = `${prefix}-arrow-${idx}`;
    const toCenterX = to.x + (to.w || 180) / 2;
    const toCenterY = to.y + (to.h || 68) / 2;
    const fromCenterX = from.x + (from.w || 180) / 2;
    const fromCenterY = from.y + (from.h || 68) / 2;
    let start = getRectEdgePoint(from, toCenterX, toCenterY);
    let end = getRectEdgePoint(to, fromCenterX, fromCenterY);
    const dx = end.x - start.x;
    const dy = end.y - start.y;
    const len = Math.hypot(dx, dy) || 1;
    const nx = -dy / len;
    const ny = dx / len;
    let curve = Number(edge.curve || 0);
    if (edge.from === 'schema::Product' && edge.to === 'schema::Resource') curve = curve || 32;
    if (edge.from === 'schema::Process' && edge.to === 'schema::Product') curve = curve || -72;
    const ctrl = {
      x: (start.x + end.x) / 2 + nx * curve,
      y: (start.y + end.y) / 2 + ny * curve,
    };
    const midPoint = quadraticBezierPoint(start, ctrl, end, 0.5);
    const hideSubclassLabel = (
      (String(edge.from || '').startsWith('product::') && edge.to === 'schema::Product') ||
      (String(edge.from || '').startsWith('process::') && edge.to === 'schema::Process')
    );
    const label = hideSubclassLabel ? '' : escapeHtml(edge.label || '');
    return `
      <defs>
        <marker id="${markerId}" markerWidth="10" markerHeight="10" refX="8" refY="5" orient="auto">
          <path d="M0,0 L0,10 L10,5 z" fill="#8fa2c9"></path>
        </marker>
      </defs>
      <path d="M ${start.x} ${start.y} Q ${ctrl.x} ${ctrl.y} ${end.x} ${end.y}" class="graph-edge ontology-edge ${edge.style === 'dashed' ? 'dashed' : ''}" marker-end="url(#${markerId})"></path>
      ${label ? renderSvgLabelBadge(midPoint.x, midPoint.y - (curve < 0 ? 18 : 12), label, 'graph-label schema-label') : ''}
    `;
  }).join('');

  const svgNodes = nodes.map(node => {
    const x = node.x || 0;
    const y = node.y || 0;
    const w = node.w || 180;
    const h = node.h || 68;
    const titleLines = wrapSvgText(node.label || '', maxCharsForWidth(w, 7.4, 18), 2);
    const subLines = wrapSvgText(node.subtitle || '', maxCharsForWidth(w, 7.2, 18), 2);
    return `
      <g transform="translate(${x}, ${y})">
        <rect width="${w}" height="${h}" rx="18" class="graph-node schema-node ${node.kind || 'default'}"></rect>
        ${renderSvgLines(w / 2, 26, titleLines, `graph-node-title schema-title ${node.kind || 'default'}`, 14)}
        ${renderSvgLines(w / 2, 48, subLines, `graph-node-subtitle schema-subtitle ${node.kind || 'default'}`, 12)}
      </g>
    `;
  }).join('');

  return `
    <div class="svg-scroll schema-scroll">
      <svg viewBox="0 0 ${width} ${height}" class="logic-graph schema-graph">
        ${svgEdges}
        ${svgNodes}
      </svg>
    </div>
  `;
}

function renderLayeredGraph(graph, options = {}) {
  return renderOntologySchemaGraph(graph);
}

function getRectEdgePoint(node, targetX, targetY) {
  const w = node.w || 180;
  const h = node.h || 68;
  const cx = (node.x || 0) + w / 2;
  const cy = (node.y || 0) + h / 2;
  const dx = targetX - cx;
  const dy = targetY - cy;
  const absDx = Math.abs(dx);
  const absDy = Math.abs(dy);

  if (absDx * h > absDy * w) {
    const sign = dx >= 0 ? 1 : -1;
    return {
      x: cx + sign * w / 2,
      y: cy + (absDx ? dy * (w / 2) / absDx : 0),
    };
  }

  const sign = dy >= 0 ? 1 : -1;
  return {
    x: cx + (absDy ? dx * (h / 2) / absDy : 0),
    y: cy + sign * h / 2,
  };
}

function getRectEdgePointBiased(node, targetX, targetY, bias = 0) {
  const point = getRectEdgePoint(node, targetX, targetY);
  const w = node.w || 180;
  const h = node.h || 68;
  const cx = (node.x || 0) + w / 2;
  const cy = (node.y || 0) + h / 2;

  if (Math.abs(point.x - cx) >= w / 2 - 1) {
    return {
      x: point.x,
      y: Math.max(node.y + 12, Math.min(node.y + h - 12, point.y + bias)),
    };
  }

  return {
    x: Math.max(node.x + 16, Math.min(node.x + w - 16, point.x + bias)),
    y: point.y,
  };
}

function quadraticBezierPoint(start, control, end, t = 0.5) {
  const mt = 1 - t;
  return {
    x: mt * mt * start.x + 2 * mt * t * control.x + t * t * end.x,
    y: mt * mt * start.y + 2 * mt * t * control.y + t * t * end.y,
  };
}

function maxCharsForWidth(width, pxPerChar = 8, horizontalPadding = 20) {
  return Math.max(6, Math.floor((width - horizontalPadding) / pxPerChar));
}

function renderSvgLabelBadge(x, y, text, className = 'graph-label', paddingX = 6, minWidth = 28) {
  const width = Math.max(minWidth, text.length * 7 + paddingX * 2);
  const height = 18;
  return `
    <g transform="translate(${x - width / 2}, ${y - height / 2})" class="svg-label-group">
      <rect width="${width}" height="${height}" rx="9" class="svg-label-bg"></rect>
      <text x="${width / 2}" y="12" text-anchor="middle" class="${className}">${escapeHtml(text)}</text>
    </g>
  `;
}

function renderStateEdgeBadge(x, y, text) {
  return renderSvgLabelBadge(x, y, text, 'graph-label state-edge-label', 7, 52);
}

function renderStateMachineSvg(machine, options = {}) {
  const states = machine?.states || [];
  if (!states.length) {
    return '<div class="empty-inline">无状态图</div>';
  }

  const variant = options.variant || 'card';
  const showEdgeLabels = options.showEdgeLabels !== false;
  const layout = computeStateLayout(states.length, variant);
  const width = layout.width;
  const height = layout.height;
  const nodeWidth = variant === 'reason' ? 208 : 220;
  const nodeHeight = variant === 'reason' ? 92 : 98;
  const pos = {};
  states.forEach((state, index) => {
    pos[state.state_id] = { x: layout.points[index].x, y: layout.points[index].y, w: nodeWidth, h: nodeHeight };
  });

  const stateIndexMap = Object.fromEntries(states.map((state, index) => [state.state_id, index]));
  const transitions = [];
  states.forEach((state) => {
    (state.transitions || []).forEach((transition, idx) => {
      transitions.push({
        from: state.state_id,
        to: transition.target,
        action: transition.action,
        label: shortActionName(transition.action),
        ordinal: idx,
      });
    });
  });

  const directedPairs = new Set(transitions.map(edge => `${edge.from}::${edge.to}`));
  const uid = `sm-${Math.random().toString(36).slice(2, 8)}`;

  const svgEdges = transitions.map((edge, edgeIndex) => {
    const from = pos[edge.from];
    const to = pos[edge.to];
    if (!from || !to) return '';
    const markerId = `${uid}-${edgeIndex}`;
    if (edge.from === edge.to) {
      const loopStartX = from.x + nodeWidth * 0.72;
      const loopStartY = from.y + 10;
      const loopEndX = from.x + nodeWidth * 0.28;
      const loopEndY = from.y + 10;
      const loopCtrlX1 = from.x + nodeWidth + 26;
      const loopCtrlY1 = from.y - 28;
      const loopCtrlX2 = from.x - 26;
      const loopCtrlY2 = from.y - 28;
      const loopLabelX = from.x + nodeWidth / 2;
      const loopLabelY = from.y - 18;
      return `
        <defs>
          <marker id="${markerId}" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto">
            <path d="M0,0 L0,8 L8,4 z" fill="#8fa2c9"></path>
          </marker>
        </defs>
        <path d="M ${loopStartX} ${loopStartY} C ${loopCtrlX1} ${loopCtrlY1}, ${loopCtrlX2} ${loopCtrlY2}, ${loopEndX} ${loopEndY}" class="graph-edge state-edge" marker-end="url(#${markerId})"></path>
        ${showEdgeLabels ? renderStateEdgeBadge(loopLabelX, loopLabelY, edge.label) : ''}
      `;
    }

    const fromCenterX = from.x + nodeWidth / 2;
    const fromCenterY = from.y + nodeHeight / 2;
    const toCenterX = to.x + nodeWidth / 2;
    const toCenterY = to.y + nodeHeight / 2;
    let start = getRectEdgePoint(from, toCenterX, toCenterY);
    let end = getRectEdgePoint(to, fromCenterX, fromCenterY);
    const dx = end.x - start.x;
    const dy = end.y - start.y;
    const len = Math.hypot(dx, dy) || 1;
    const nx = -dy / len;
    const ny = dx / len;
    const hasOpposite = directedPairs.has(`${edge.to}::${edge.from}`);
    let curve = 0;
    if (hasOpposite) {
      const bend = stateIndexMap[edge.from] < stateIndexMap[edge.to] ? -74 : 74;
      const anchorBias = bend < 0 ? -20 : 20;
      start = getRectEdgePointBiased(from, toCenterX, toCenterY, anchorBias);
      end = getRectEdgePointBiased(to, fromCenterX, fromCenterY, anchorBias);
      start = { x: start.x + nx * bend * 0.16, y: start.y + ny * bend * 0.16 };
      end = { x: end.x + nx * bend * 0.16, y: end.y + ny * bend * 0.16 };
      curve = bend;
    } else if (Math.abs(dy) < 18) {
      curve = (edge.ordinal % 2 === 0 ? -18 : 18);
    }
    const ctrl = {
      x: (start.x + end.x) / 2 + nx * curve,
      y: (start.y + end.y) / 2 + ny * curve,
    };
    const midPoint = quadraticBezierPoint(start, ctrl, end, 0.5);
    const labelX = midPoint.x;
    const labelY = midPoint.y;
    return `
      <defs>
        <marker id="${markerId}" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto">
          <path d="M0,0 L0,8 L8,4 z" fill="#8fa2c9"></path>
        </marker>
      </defs>
      <path d="M ${start.x} ${start.y} Q ${ctrl.x} ${ctrl.y} ${end.x} ${end.y}" class="graph-edge state-edge" marker-end="url(#${markerId})"></path>
      ${showEdgeLabels ? renderStateEdgeBadge(labelX, labelY, edge.label) : ''}
    `;
  }).join('');

  const svgNodeRects = states.map((state) => {
    const p = pos[state.state_id];
    return `
      <g transform="translate(${p.x}, ${p.y})">
        <rect width="${nodeWidth}" height="${nodeHeight}" rx="18" class="graph-node state-node"></rect>
      </g>
    `;
  }).join('');

  const svgNodeTexts = states.map((state) => {
    const p = pos[state.state_id];
    const titleLines = wrapSvgText(
      formatStateId(state.state_id),
      maxCharsForWidth(nodeWidth, variant === 'reason' ? 7.1 : 7.4, 6),
      2
    );
    const conditionLines = stateConditionLines(state.label || '', nodeWidth, 2);
    return `
      <g transform="translate(${p.x}, ${p.y})">
        ${renderSvgLines(nodeWidth / 2, 28, titleLines, 'graph-node-title state-node-title', 14)}
        ${renderSvgLines(nodeWidth / 2, 60, conditionLines, 'graph-node-subtitle state-node-condition', 12)}
      </g>
    `;
  }).join('');

  return `
    <div class="svg-scroll state-scroll ${variant === 'reason' ? 'reason-state-scroll' : ''}">
      <svg viewBox="0 0 ${width} ${height}" class="state-graph ${variant}">
        ${svgNodeRects}
        ${svgEdges}
        ${svgNodeTexts}
      </svg>
    </div>
  `;
}

function computeStateLayout(count, variant = 'card') {
  if (variant === 'reason') {
    if (count <= 1) return { width: 360, height: 210, points: [{ x: 84, y: 62 }] };
    if (count === 2) return { width: 760, height: 250, points: [{ x: 88, y: 78 }, { x: 464, y: 78 }] };
    if (count === 3) return { width: 760, height: 430, points: [{ x: 78, y: 76 }, { x: 486, y: 76 }, { x: 282, y: 276 }] };
    if (count === 4) return { width: 840, height: 500, points: [{ x: 76, y: 68 }, { x: 562, y: 68 }, { x: 76, y: 320 }, { x: 562, y: 320 }] };
  }

  if (count <= 1) return { width: 380, height: 220, points: [{ x: 88, y: 64 }] };
  if (count === 2) return { width: 780, height: 260, points: [{ x: 102, y: 82 }, { x: 458, y: 82 }] };
  if (count === 3) return { width: 780, height: 460, points: [{ x: 82, y: 82 }, { x: 492, y: 82 }, { x: 286, y: 296 }] };
  if (count === 4) return { width: 860, height: 520, points: [{ x: 78, y: 78 }, { x: 576, y: 78 }, { x: 78, y: 340 }, { x: 576, y: 340 }] };

  const width = variant === 'reason' ? 880 : 940;
  const height = variant === 'reason' ? 560 : 620;
  const centerX = width / 2 - 99;
  const centerY = height / 2 - 45;
  const radius = variant === 'reason' ? 190 : 220;
  const points = [];
  for (let i = 0; i < count; i++) {
    const angle = (Math.PI * 2 * i) / count - Math.PI / 2;
    points.push({
      x: centerX + Math.cos(angle) * radius,
      y: centerY + Math.sin(angle) * radius,
    });
  }
  return { width, height, points };
}

function renderSvgLines(x, y, lines, className, lineHeight = 15) {
  const safeLines = lines && lines.length ? lines : [''];
  return `
    <text x="${x}" y="${y}" text-anchor="middle" class="${className}">
      ${safeLines.map((line, idx) => `<tspan x="${x}" dy="${idx === 0 ? 0 : lineHeight}">${escapeHtml(line)}</tspan>`).join('')}
    </text>
  `;
}

function wrapSvgText(text, maxChars = 12, maxLines = 2) {
  const raw = String(text || '').trim();
  if (!raw) return [''];
  const chunks = [];
  let buffer = '';
  for (const char of raw) {
    buffer += char;
    if (buffer.length >= maxChars) {
      chunks.push(buffer);
      buffer = '';
      if (chunks.length >= maxLines) break;
    }
  }
  if (buffer && chunks.length < maxLines) chunks.push(buffer);
  if (!chunks.length) chunks.push(raw.slice(0, maxChars));
  const truncated = raw.length > chunks.join('').length;
  if (truncated && chunks.length) {
    chunks[chunks.length - 1] = chunks[chunks.length - 1].replace(/.$/, '…');
  }
  return chunks.slice(0, maxLines);
}

function formatStateId(stateId) {
  return String(stateId || '')
    .replace(/([a-z])([A-Z])/g, '$1 $2')
    .replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2')
    .replace(/_/g, ' ');
}

function stateSubtitleShort(label) {
  if (!label) return '';
  const firstBlock = String(label).split('&&')[0].trim();
  if (!firstBlock) return '';
  const desc = firstBlock.split('|')[0].trim();
  return desc
    .replace('not at start position', 'not at start')
    .replace('at start position', 'at start')
    .replace('forward rotation', 'forward')
    .replace('backward rotation', 'backward');
}

function stateConditionLines(label, boxWidth = 180, maxLines = 2) {
  const maxChars = maxCharsForWidth(boxWidth, 6.9, 6);
  const parts = String(label || '')
    .split('&&')
    .map(part => part.trim())
    .filter(Boolean)
    .map(part => part
      .replace('not at start position', 'not at start')
      .replace('at start position', 'at start')
      .replace('forward rotation', 'forward')
      .replace('backward rotation', 'backward')
    );

  if (!parts.length) return [''];
  return parts.slice(0, maxLines).map(part => wrapSvgText(part, maxChars, 1)[0]);
}

function shortActionName(action) {
  if (!action) return '';
  return String(action)
    .replace(/_(ARM\d+|Camera|Mover|conveyorBelt\d+)/gi, '')
    .replace(/([a-z])([A-Z])/g, '$1 $2')
    .replace(/_/g, ' ')
    .trim();
}

function formatOperationKind(kind) {
  const mapping = {
    spray: '整体喷涂',
    writing: '写字 / 细节绘制',
    outline: '描边',
  };
  return mapping[kind] || kind || 'painting';
}

function sign(num) {
  return num >= 0 ? 1 : -1;
}

function escapeHtml(text) {
  return String(text ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
