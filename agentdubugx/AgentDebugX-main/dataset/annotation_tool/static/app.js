"use strict";

const state = {
  config: null,
  cases: [],
  current: null,
  annotation: null,
  original: null,
  selectedObservationId: null,
  checkedObservationIds: new Set(),
  payloadTab: "input",
  step: 1,
  readOnly: false,
  dirty: false,
  autosaveTimer: null,
  fieldErrors: {},
  pendingImport: null,
  pendingTransition: null,
  freezePreview: null,
};

const byId = (id) => document.getElementById(id);
const clone = (value) => JSON.parse(JSON.stringify(value));
const array = (value) => Array.isArray(value) ? value : [];
const unique = (values) => [...new Set(values.filter(Boolean))];
const text = (value) => value == null ? "" : String(value);

document.addEventListener("DOMContentLoaded", initialize);

async function initialize() {
  bindGlobalActions();
  byId("accessToken").value = sessionStorage.getItem("annotationToken") || "";
  try {
    state.config = await api("/api/config");
    byId("modeBadge").textContent = state.config.demo_mode ? "演示数据" : "生产只读数据";
    byId("modeBadge").title = `标注人：${state.config.annotator_id}`;
    await loadCases();
  } catch (error) {
    showToast(error.message, true);
  }
}

function bindGlobalActions() {
  byId("accessToken").addEventListener("change", async (event) => {
    sessionStorage.setItem("annotationToken", event.target.value.trim());
    try {
      state.config = await api("/api/config");
      await loadCases();
      showToast("访问令牌已更新");
    } catch (error) { showToast(error.message, true); }
  });
  byId("syncButton").addEventListener("click", syncSource);
  byId("refreshCasesButton").addEventListener("click", loadCases);
  byId("exportButton").addEventListener("click", exportAnnotations);
  byId("importButton").addEventListener("click", () => byId("importFile").click());
  byId("importFile").addEventListener("change", importAnnotations);
  byId("qualityButton").addEventListener("click", () => { state.pendingImport = null; openQualityReport(); });
  byId("freezeDatasetButton").addEventListener("click", freezeDataset);
  byId("statusFilter").addEventListener("change", renderCaseList);
  byId("caseSearch").addEventListener("input", renderCaseList);
  ["technicalFilter", "scopeFilter", "rootLabelFilter", "confidenceFilter"].forEach((id) => {
    byId(id).addEventListener("change", renderCaseList);
  });
  byId("observationSearch").addEventListener("input", renderObservationTree);
  byId("observationTypeFilter").addEventListener("change", renderObservationTree);
  byId("errorOnlyFilter").addEventListener("change", renderObservationTree);
  byId("clearSelectionButton").addEventListener("click", () => {
    state.checkedObservationIds.clear();
    renderObservationTree();
  });
  byId("locateFailureButton").addEventListener("click", locateFailure);
  byId("locateParentButton").addEventListener("click", locateParent);
  byId("locateNextButton").addEventListener("click", locateNext);
  document.querySelectorAll("[data-role]").forEach((button) => {
    button.addEventListener("click", () => assignRole(button.dataset.role));
  });
  document.querySelectorAll("[data-step]").forEach((button) => {
    button.addEventListener("click", () => showStep(Number(button.dataset.step)));
  });
  document.querySelectorAll("[data-payload]").forEach((button) => {
    button.addEventListener("click", () => {
      state.payloadTab = button.dataset.payload;
      renderObservationDetail();
    });
  });
  byId("rawWrapToggle").addEventListener("change", () => {
    byId("observationPayload").classList.toggle("no-wrap", !byId("rawWrapToggle").checked);
  });
  byId("previousStepButton").addEventListener("click", () => showStep(state.step - 1));
  byId("nextStepButton").addEventListener("click", () => showStep(state.step + 1));
  byId("resetButton").addEventListener("click", resetChanges);
  byId("saveDraftButton").addEventListener("click", () => saveAnnotation("draft", false));
  byId("submitNextButton").addEventListener("click", () => saveAnnotation("submitted", true));
  byId("excludeButton").addEventListener("click", excludeCase);
  byId("adjudicateButton").addEventListener("click", () => transitionCase("adjudicated", "裁决通过"));
  byId("freezeCaseButton").addEventListener("click", () => transitionCase("frozen", "冻结Case"));
  byId("newRevisionButton").addEventListener("click", beginRevision);
  byId("autosaveToggle").addEventListener("change", scheduleAutosave);
  byId("annotationForm").addEventListener("input", onFormChanged);
  byId("annotationForm").addEventListener("change", onFormChanged);
  byId("technicalLabel").addEventListener("change", onTechnicalLabelChanged);
  byId("toolSemantics").addEventListener("change", onSemanticsChanged);
  byId("rootScope").addEventListener("change", onRootScopeChanged);
  byId("recoveryStatus").addEventListener("change", () => {
    if (byId("recoveryStatus").value !== "recovered") {
      state.annotation.semantic_outcome.recovery_observation_id = null;
      byId("recoveryObservation").value = "";
    }
  });
  byId("closeRevisionDialog").addEventListener("click", () => byId("revisionDialog").close());
  byId("closeQualityDialog").addEventListener("click", () => { state.pendingImport = null; byId("qualityDialog").close(); });
  byId("applyImportButton").addEventListener("click", applyPendingImport);
  byId("closeTransitionDialog").addEventListener("click", () => byId("transitionDialog").close());
  byId("confirmTransitionButton").addEventListener("click", applyTransitionCase);
  byId("closeFreezeDialog").addEventListener("click", () => byId("freezeDialog").close());
  byId("previewFreezeButton").addEventListener("click", previewDatasetFreeze);
  byId("applyFreezeButton").addEventListener("click", applyDatasetFreeze);
  document.addEventListener("keydown", onShortcut);
  window.addEventListener("beforeunload", (event) => {
    if (state.dirty) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  const token = sessionStorage.getItem("annotationToken");
  if (token) headers.Authorization = `Bearer ${token}`;
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, headers });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("json") ? await response.json() : await response.text();
  if (!response.ok) {
    const error = new Error(payload.field_errors ? Object.values(payload.field_errors).join("；") : `请求失败 (${response.status})`);
    error.fieldErrors = payload.field_errors || {};
    error.status = response.status;
    throw error;
  }
  return payload;
}

async function loadCases() {
  const payload = await api("/api/cases");
  state.cases = payload.cases || [];
  renderCaseList();
}

function renderCaseList() {
  const list = byId("caseList");
  const query = byId("caseSearch").value.trim().toLowerCase();
  const status = byId("statusFilter").value;
  const technical = byId("technicalFilter").value;
  const scope = byId("scopeFilter").value;
  const rootLabel = byId("rootLabelFilter").value;
  const confidence = byId("confidenceFilter").value;
  const cases = state.cases.filter((item) => {
    const haystack = `${item.case_id} ${item.trace_id} ${item.scenario}`.toLowerCase();
    const summary = item.label_summary || {};
    return (!status || item.status === status)
      && (!technical || summary.technical_label === technical)
      && (!scope || summary.root_scope === scope)
      && (!rootLabel || summary.root_label === rootLabel)
      && (!confidence || summary.overall_confidence === confidence)
      && (!query || haystack.includes(query));
  });
  byId("caseCount").textContent = `${cases.length} / ${state.cases.length} 条`;
  list.replaceChildren();
  if (!cases.length) {
    const empty = document.createElement("p");
    empty.className = "form-help";
    empty.textContent = "没有匹配的Case。";
    list.append(empty);
    return;
  }
  cases.forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `case-item${state.current?.case?.case_id === item.case_id ? " active" : ""}`;
    button.setAttribute("role", "option");
    button.dataset.caseId = item.case_id;
    button.innerHTML = `<strong>${escapeHtml(item.case_id)}</strong><span>${escapeHtml(item.trace_id || "")}</span><span class="status-line"><em>${escapeHtml(item.scenario || "production")}</em><b>${escapeHtml(item.status)}</b></span>`;
    button.addEventListener("click", () => selectCase(item.case_id));
    list.append(button);
  });
}

async function selectCase(caseId, force = false) {
  if (!force && state.dirty && !window.confirm("当前修改尚未保存，确定切换Case吗？")) return;
  try {
    const payload = await api(`/api/cases/${encodeURIComponent(caseId)}`);
    state.current = payload;
    state.annotation = payload.latest_annotation ? clone(payload.latest_annotation) : defaultAnnotation(payload);
    state.original = clone(state.annotation);
    state.selectedObservationId = payload.case.tool_attempt?.tool_result_observation_id || payload.observations[0]?.observation_id || null;
    state.checkedObservationIds = new Set(state.selectedObservationId ? [state.selectedObservationId] : []);
    state.fieldErrors = {};
    state.dirty = false;
    state.step = 1;
    const latestStatus = payload.latest_annotation?.annotation_status;
    state.readOnly = Boolean(latestStatus && latestStatus !== "draft");
    renderWorkspace();
    renderCaseList();
  } catch (error) { showToast(error.message, true); }
}

function defaultAnnotation(envelope) {
  const failure = envelope.case.tool_attempt?.tool_result_observation_id || "";
  return {
    schema_version: "failure-attribution-annotation-v2",
    case_id: envelope.case.case_id,
    project_id: envelope.project_id,
    trace_id: envelope.trace.trace_id,
    annotation_status: "draft",
    technical_error_review: { human_label: "confirmed", correction_reason: null, comment: "", confidence: "medium" },
    semantic_outcome: { tool_result_semantics: "unknown", is_agent_failure: null, is_task_failure: null, recovery_status: "unknown", recovery_observation_id: null, outcome_reason: "", confidence: "medium" },
    failure_manifestation: { failure_onset_observation_id: failure, primary_failure_observation_id: failure, failure_observation_ids: failure ? [failure] : [], downstream_symptom_observation_ids: [] },
    attribution: { applicable: false, root_cause_scope: "unknown", primary_root_cause_observation_id: null, root_cause_observation_ids: [], root_cause_domain: null, root_cause_label: null, path_source_kind: null, path_source_observation_id: null, confidence: "medium" },
    current_trace_reference: { earliest_local_evidence_observation_id: null, local_trigger_observation_id: null, propagation_observation_ids: failure ? [failure] : [], evidence_observation_ids: failure ? [failure] : [], boundary_reason: null, reference_summary: "", confidence: "medium" },
    review: { evidence_observation_ids: failure ? [failure] : [], reasoning_summary: "", limitations: [], overall_confidence: "medium", notes: "" },
    source_snapshot: clone(envelope.source_snapshot),
  };
}

function renderWorkspace() {
  byId("emptyState").classList.add("hidden");
  byId("workspace").classList.remove("hidden");
  const envelope = state.current;
  byId("caseTitle").textContent = envelope.case.case_id;
  byId("caseStatus").textContent = state.annotation.annotation_status || "candidate";
  byId("truncatedWarning").classList.toggle("hidden", !envelope.source_snapshot.context_truncated);
  renderDefinitionList(byId("caseMetadata"), [
    ["Trace", envelope.trace.trace_id], ["Project", envelope.project_id],
    ["Rule", envelope.case.rule_evidence?.rule_id || "—"], ["命中字段", envelope.case.rule_evidence?.matched_field || "—"],
    ["Tool", envelope.case.tool_attempt?.tool_name || "read"], ["配对状态", envelope.case.tool_attempt?.pairing_status || "unknown"],
    ["快照时间", envelope.source_snapshot.snapshot_at], ["标注人", state.config?.annotator_id || "—"],
  ]);
  renderLabelSummary();
  byId("observationCount").textContent = `${envelope.observations.length} / ${envelope.source_snapshot.observation_count} 个节点`;
  populateObservationTypes();
  renderForm();
  renderObservationTree();
  renderObservationDetail();
  renderRevisionHistory();
  setReadOnly(state.readOnly);
  showStep(state.step);
  updateSaveState();
}

function renderLabelSummary() {
  const a = state.annotation || {};
  const technical = a.technical_error_review || {};
  const semantic = a.semantic_outcome || {};
  const attribution = a.attribution || {};
  const reference = a.current_trace_reference || {};
  renderDefinitionList(byId("labelSummary"), [
    ["技术错误", technical.human_label || "未标注"],
    ["Agent失败", triStateLabel(semantic.is_agent_failure)],
    ["任务失败", triStateLabel(semantic.is_task_failure)],
    ["恢复状态", semantic.recovery_status || "unknown"],
    ["根因范围", attribution.root_cause_scope || "unknown"],
    ["根因标签", attribution.root_cause_label || "—"],
    ["主要根因", attribution.primary_root_cause_observation_id || "—"],
    ["参考链", `${array(reference.propagation_observation_ids).length} 个节点`],
  ]);
}

function renderForm() {
  const a = state.annotation;
  setValue("technicalLabel", a.technical_error_review?.human_label || "confirmed");
  setValue("correctionReason", a.technical_error_review?.correction_reason || "");
  setValue("technicalConfidence", a.technical_error_review?.confidence || "medium");
  setValue("technicalComment", a.technical_error_review?.comment || "");
  setValue("toolSemantics", a.semantic_outcome?.tool_result_semantics || "unknown");
  setValue("isAgentFailure", triStateValue(a.semantic_outcome?.is_agent_failure));
  setValue("isTaskFailure", triStateValue(a.semantic_outcome?.is_task_failure));
  setValue("recoveryStatus", a.semantic_outcome?.recovery_status || "unknown");
  setValue("recoveryObservation", a.semantic_outcome?.recovery_observation_id || "");
  setValue("semanticConfidence", a.semantic_outcome?.confidence || "medium");
  setValue("outcomeReason", a.semantic_outcome?.outcome_reason || "");
  setValue("failureOnset", a.failure_manifestation?.failure_onset_observation_id || "");
  setValue("primaryFailure", a.failure_manifestation?.primary_failure_observation_id || "");
  setValue("rootScope", a.attribution?.root_cause_scope || "unknown");
  setValue("attributionApplicable", String(Boolean(a.attribution?.applicable)));
  setValue("attributionConfidence", a.attribution?.confidence || "medium");
  setValue("primaryRoot", a.attribution?.primary_root_cause_observation_id || "");
  setValue("rootDomain", a.attribution?.root_cause_domain || "");
  setValue("rootLabel", a.attribution?.root_cause_label || "");
  setValue("pathSourceKind", a.attribution?.path_source_kind || "");
  setValue("pathSourceObservation", a.attribution?.path_source_observation_id || "");
  setValue("earliestEvidence", a.current_trace_reference?.earliest_local_evidence_observation_id || "");
  setValue("localTrigger", a.current_trace_reference?.local_trigger_observation_id || "");
  setValue("referenceConfidence", a.current_trace_reference?.confidence || "medium");
  setValue("boundaryReason", a.current_trace_reference?.boundary_reason || "");
  setValue("referenceSummary", a.current_trace_reference?.reference_summary || "");
  setValue("reasoningSummary", a.review?.reasoning_summary || "");
  setValue("overallConfidence", a.review?.overall_confidence || "medium");
  setValue("reviewNotes", a.review?.notes || "");
  document.querySelectorAll("input[name=limitations]").forEach((checkbox) => {
    checkbox.checked = array(a.review?.limitations).includes(checkbox.value);
  });
  updateConditionalFields();
  renderRoleFields();
  renderErrors();
}

function collectAnnotation(status = state.annotation.annotation_status || "draft") {
  const a = clone(state.annotation);
  a.annotation_status = status;
  a.technical_error_review = { human_label: value("technicalLabel"), correction_reason: value("correctionReason") || null, comment: value("technicalComment"), confidence: value("technicalConfidence") };
  a.semantic_outcome = { ...a.semantic_outcome, tool_result_semantics: value("toolSemantics"), is_agent_failure: parseTriState(value("isAgentFailure")), is_task_failure: parseTriState(value("isTaskFailure")), recovery_status: value("recoveryStatus"), recovery_observation_id: value("recoveryObservation") || null, outcome_reason: value("outcomeReason"), confidence: value("semanticConfidence") };
  a.failure_manifestation = { ...a.failure_manifestation, failure_onset_observation_id: value("failureOnset") || null, primary_failure_observation_id: value("primaryFailure") || null };
  a.attribution = { ...a.attribution, applicable: value("attributionApplicable") === "true", root_cause_scope: value("rootScope"), primary_root_cause_observation_id: value("primaryRoot") || null, root_cause_domain: value("rootDomain") || null, root_cause_label: value("rootLabel") || null, path_source_kind: value("pathSourceKind") || null, path_source_observation_id: value("pathSourceObservation") || null, confidence: value("attributionConfidence") };
  a.current_trace_reference = { ...a.current_trace_reference, earliest_local_evidence_observation_id: value("earliestEvidence") || null, local_trigger_observation_id: value("localTrigger") || null, boundary_reason: value("boundaryReason") || null, reference_summary: value("referenceSummary"), confidence: value("referenceConfidence") };
  a.review = { ...a.review, reasoning_summary: value("reasoningSummary"), limitations: [...document.querySelectorAll("input[name=limitations]:checked")].map((item) => item.value), overall_confidence: value("overallConfidence"), notes: value("reviewNotes") };
  return a;
}

function onFormChanged(event) {
  if (!state.current || state.readOnly) return;
  state.annotation = collectAnnotation("draft");
  markDirty();
  if (["technicalLabel", "toolSemantics", "rootScope", "recoveryStatus"].includes(event.target.id)) updateConditionalFields();
}

function onTechnicalLabelChanged() {
  if (value("technicalLabel") === "rule_false_positive") {
    const a = state.annotation;
    a.semantic_outcome = { tool_result_semantics: "unknown", is_agent_failure: false, is_task_failure: false, recovery_status: "unknown", recovery_observation_id: null, outcome_reason: "", confidence: "medium" };
    a.failure_manifestation = { failure_onset_observation_id: null, primary_failure_observation_id: null, failure_observation_ids: [], downstream_symptom_observation_ids: [] };
    a.attribution = { applicable: false, root_cause_scope: "unknown", primary_root_cause_observation_id: null, root_cause_observation_ids: [], root_cause_domain: null, root_cause_label: null, path_source_kind: null, path_source_observation_id: null, confidence: "medium" };
    a.current_trace_reference = { earliest_local_evidence_observation_id: null, local_trigger_observation_id: null, propagation_observation_ids: [], evidence_observation_ids: [], boundary_reason: null, reference_summary: "", confidence: "medium" };
    a.review.evidence_observation_ids = [];
    renderForm();
  }
  updateConditionalFields();
}

function onSemanticsChanged() {
  if (["validation_probe", "control_flow_signal", "expected_negative_result"].includes(value("toolSemantics"))) {
    setValue("isAgentFailure", "false");
    setValue("isTaskFailure", "false");
  }
}

function onRootScopeChanged() {
  const scope = value("rootScope");
  if (scope === "current_trace") setValue("attributionApplicable", "true");
  else {
    setValue("attributionApplicable", "false");
    state.annotation.attribution.primary_root_cause_observation_id = null;
    state.annotation.attribution.root_cause_observation_ids = [];
    setValue("primaryRoot", ""); setValue("rootDomain", ""); setValue("rootLabel", "");
  }
  updateConditionalFields(); renderRoleFields(); renderObservationTree();
}

function updateConditionalFields() {
  const falsePositive = value("technicalLabel") === "rule_false_positive";
  byId("correctionReasonField").classList.toggle("hidden", !falsePositive);
  document.querySelectorAll("[data-step-panel]").forEach((panel) => {
    if (Number(panel.dataset.stepPanel) >= 2 && Number(panel.dataset.stepPanel) <= 5) panel.disabled = falsePositive || state.readOnly;
  });
  const outside = value("rootScope") === "outside_current_trace";
  byId("outsideExplanation").classList.toggle("hidden", !outside);
  ["primaryRoot", "rootDomain", "rootLabel"].forEach((id) => { byId(id).disabled = value("rootScope") !== "current_trace" || state.readOnly; });
  byId("recoveryObservation").closest("label").classList.toggle("hidden", value("recoveryStatus") !== "recovered");
}

function setReadOnly(readOnly) {
  state.readOnly = readOnly;
  byId("annotationForm").querySelectorAll("input, select, textarea").forEach((control) => { control.disabled = readOnly; });
  document.querySelectorAll("[data-role]").forEach((button) => { button.disabled = readOnly; });
  ["saveDraftButton", "submitNextButton", "excludeButton", "resetButton", "autosaveToggle"].forEach((id) => { byId(id).disabled = readOnly; });
  const latestStatus = state.current?.latest_annotation?.annotation_status;
  byId("newRevisionButton").classList.toggle("hidden", !readOnly || latestStatus === "frozen");
  byId("adjudicateButton").classList.toggle("hidden", !readOnly || !["submitted", "needs_review"].includes(latestStatus));
  byId("freezeCaseButton").classList.toggle("hidden", !readOnly || latestStatus !== "adjudicated");
  updateConditionalFields();
}

function beginRevision() {
  if (state.current.latest_annotation?.annotation_status === "frozen") return;
  state.annotation = clone(state.current.latest_annotation || state.annotation);
  state.annotation.annotation_status = "draft";
  state.original = clone(state.annotation);
  state.readOnly = false;
  setReadOnly(false);
  setValue("changeReason", "人工修订");
  markDirty();
  showToast("已创建可编辑副本，保存时将生成新revision");
}

function populateObservationTypes() {
  const select = byId("observationTypeFilter");
  const current = select.value;
  const types = unique(state.current.observations.map((item) => text(item.type))).sort();
  select.replaceChildren(new Option("全部类型", ""), ...types.map((item) => new Option(item, item)));
  select.value = types.includes(current) ? current : "";
}

function renderObservationTree() {
  if (!state.current) return;
  const container = byId("observationTree");
  const search = value("observationSearch").toLowerCase();
  const typeFilter = value("observationTypeFilter");
  const errorOnly = byId("errorOnlyFilter").checked;
  const observations = state.current.observations.filter((item) => {
    const payload = JSON.stringify(item).toLowerCase();
    const isError = text(item.level).toUpperCase() === "ERROR" || /file not found|enoent|no such file/i.test(payload);
    return (!search || payload.includes(search)) && (!typeFilter || item.type === typeFilter) && (!errorOnly || isError);
  });
  container.replaceChildren();
  observations.forEach((item) => {
    const id = observationId(item);
    const row = document.createElement("button");
    row.type = "button";
    row.className = `tree-node${state.selectedObservationId === id ? " selected" : ""}${state.checkedObservationIds.has(id) ? " checked" : ""}`;
    row.setAttribute("role", "treeitem");
    row.style.paddingLeft = `${8 + observationDepth(item) * 13}px`;
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox"; checkbox.checked = state.checkedObservationIds.has(id); checkbox.setAttribute("aria-label", `选择${id}`);
    checkbox.addEventListener("click", (event) => { event.stopPropagation(); toggleChecked(id, checkbox.checked); });
    const main = document.createElement("span"); main.className = "node-main";
    main.innerHTML = `<strong>${escapeHtml(item.name || item.type || "Observation")}</strong><span>${escapeHtml(id)} · ${escapeHtml(formatTime(item.start_time))}</span>`;
    const badges = document.createElement("span"); badges.className = "role-badges";
    rolesFor(id).forEach((role) => { const badge = document.createElement("span"); badge.className = `role-badge ${role}`; badge.textContent = roleLetter(role); badge.title = role; badges.append(badge); });
    row.append(checkbox, main, badges);
    row.addEventListener("click", () => { state.selectedObservationId = id; renderObservationTree(); renderObservationDetail(); });
    container.append(row);
  });
  if (!observations.length) container.textContent = "没有符合筛选条件的Observation。";
}

function observationDepth(item) {
  const map = new Map(state.current.observations.map((value) => [observationId(value), value]));
  let depth = 0; let parent = item.parent_observation_id; const seen = new Set();
  while (parent && map.has(parent) && depth < 8 && !seen.has(parent)) { seen.add(parent); depth += 1; parent = map.get(parent).parent_observation_id; }
  return depth;
}

function renderObservationDetail() {
  if (!state.current) return;
  const item = findObservation(state.selectedObservationId);
  document.querySelectorAll("[data-payload]").forEach((button) => button.classList.toggle("active", button.dataset.payload === state.payloadTab));
  if (!item) { byId("selectedObservationLabel").textContent = "请选择节点"; byId("observationPayload").textContent = ""; return; }
  byId("selectedObservationLabel").textContent = observationId(item);
  renderDefinitionList(byId("observationMetadata"), [["Name", item.name], ["Type", item.type], ["Level", item.level], ["Parent", item.parent_observation_id || "—"], ["Start", item.start_time], ["End", item.end_time || "—"]]);
  const keys = {
    input: ["input_redacted", "input"], output: ["output_redacted", "output"],
    status: ["status_message_redacted", "status_message"], metadata: ["metadata_redacted", "metadata"],
  }[state.payloadTab];
  const payload = keys.map((key) => item[key]).find((candidate) => candidate !== undefined);
  byId("observationPayload").textContent = typeof payload === "string" ? payload : JSON.stringify(payload ?? null, null, 2);
}

function toggleChecked(id, checked) {
  if (checked) state.checkedObservationIds.add(id); else state.checkedObservationIds.delete(id);
  renderObservationTree();
}

function selectedRoleIds() {
  const checked = [...state.checkedObservationIds];
  return checked.length ? checked : (state.selectedObservationId ? [state.selectedObservationId] : []);
}

function assignRole(role) {
  if (state.readOnly || !state.annotation) return;
  const ids = selectedRoleIds();
  if (!ids.length) { showToast("请先选择Observation", true); return; }
  const a = state.annotation;
  if (role === "failure") {
    a.failure_manifestation.failure_observation_ids = unique([...a.failure_manifestation.failure_observation_ids, ...ids]);
    const ordered = sortObservationIds(a.failure_manifestation.failure_observation_ids);
    a.failure_manifestation.failure_onset_observation_id = ordered[0];
    a.failure_manifestation.primary_failure_observation_id ||= ids[0];
    a.current_trace_reference.propagation_observation_ids = sortObservationIds(unique([...a.current_trace_reference.propagation_observation_ids, ...ids]));
  } else if (role === "root") {
    if (value("rootScope") !== "current_trace") { showToast("只有current_trace范围可以选择根因", true); return; }
    a.attribution.root_cause_observation_ids = unique([...a.attribution.root_cause_observation_ids, ...ids]);
    a.attribution.primary_root_cause_observation_id ||= ids[0];
    a.attribution.applicable = true;
  } else if (role === "evidence") {
    a.review.evidence_observation_ids = unique([...a.review.evidence_observation_ids, ...ids]);
    a.current_trace_reference.evidence_observation_ids = unique([...a.current_trace_reference.evidence_observation_ids, ...ids]);
  } else if (role === "context") {
    a.current_trace_reference.earliest_local_evidence_observation_id = sortObservationIds(ids)[0];
    a.attribution.path_source_observation_id = sortObservationIds(ids)[0];
    addToReferenceChain(ids);
  } else if (role === "trigger") {
    a.current_trace_reference.local_trigger_observation_id = sortObservationIds(ids).at(-1);
    addToReferenceChain(ids);
  } else if (role === "symptom") {
    a.failure_manifestation.downstream_symptom_observation_ids = unique([...a.failure_manifestation.downstream_symptom_observation_ids, ...ids]);
  } else if (role === "recovery") {
    a.semantic_outcome.recovery_status = "recovered";
    a.semantic_outcome.recovery_observation_id = ids[0];
  } else if (role === "clear") {
    ids.forEach(removeObservationFromRoles);
  }
  state.annotation = a;
  renderForm(); renderObservationTree(); markDirty();
}

function addToReferenceChain(ids) {
  const ref = state.annotation.current_trace_reference;
  ref.propagation_observation_ids = sortObservationIds(unique([...ref.propagation_observation_ids, ...ids]));
  ref.evidence_observation_ids = unique([...ref.evidence_observation_ids, ...ids]);
  state.annotation.review.evidence_observation_ids = unique([...state.annotation.review.evidence_observation_ids, ...ids]);
}

function removeObservationFromRoles(id) {
  const a = state.annotation;
  const remove = (values) => array(values).filter((value) => value !== id);
  a.failure_manifestation.failure_observation_ids = remove(a.failure_manifestation.failure_observation_ids);
  a.failure_manifestation.downstream_symptom_observation_ids = remove(a.failure_manifestation.downstream_symptom_observation_ids);
  a.attribution.root_cause_observation_ids = remove(a.attribution.root_cause_observation_ids);
  a.current_trace_reference.propagation_observation_ids = remove(a.current_trace_reference.propagation_observation_ids);
  a.current_trace_reference.evidence_observation_ids = remove(a.current_trace_reference.evidence_observation_ids);
  a.review.evidence_observation_ids = remove(a.review.evidence_observation_ids);
  const scalarPaths = [[a.failure_manifestation, "failure_onset_observation_id"], [a.failure_manifestation, "primary_failure_observation_id"], [a.attribution, "primary_root_cause_observation_id"], [a.attribution, "path_source_observation_id"], [a.current_trace_reference, "earliest_local_evidence_observation_id"], [a.current_trace_reference, "local_trigger_observation_id"], [a.semantic_outcome, "recovery_observation_id"]];
  scalarPaths.forEach(([section, key]) => { if (section[key] === id) section[key] = null; });
}

function rolesFor(id) {
  if (!state.annotation) return [];
  const a = state.annotation; const result = [];
  if (array(a.failure_manifestation?.failure_observation_ids).includes(id)) result.push("failure");
  if (array(a.attribution?.root_cause_observation_ids).includes(id)) result.push("root");
  if (array(a.review?.evidence_observation_ids).includes(id)) result.push("evidence");
  if (a.current_trace_reference?.earliest_local_evidence_observation_id === id) result.push("context");
  if (a.current_trace_reference?.local_trigger_observation_id === id) result.push("trigger");
  if (array(a.failure_manifestation?.downstream_symptom_observation_ids).includes(id)) result.push("symptom");
  if (a.semantic_outcome?.recovery_observation_id === id) result.push("recovery");
  return result;
}

function renderRoleFields() {
  if (!state.annotation) return;
  const a = state.annotation;
  renderChips("failureChips", a.failure_manifestation.failure_observation_ids, "failure");
  renderChips("symptomChips", a.failure_manifestation.downstream_symptom_observation_ids, "symptom");
  renderChips("rootChips", a.attribution.root_cause_observation_ids, "root");
  renderChips("referenceEvidenceChips", a.current_trace_reference.evidence_observation_ids, "referenceEvidence");
  renderChips("reviewEvidenceChips", a.review.evidence_observation_ids, "reviewEvidence");
  renderPropagation();
  setValue("failureOnset", a.failure_manifestation.failure_onset_observation_id || "");
  setValue("primaryFailure", a.failure_manifestation.primary_failure_observation_id || "");
  setValue("primaryRoot", a.attribution.primary_root_cause_observation_id || "");
  setValue("recoveryObservation", a.semantic_outcome.recovery_observation_id || "");
  setValue("earliestEvidence", a.current_trace_reference.earliest_local_evidence_observation_id || "");
  setValue("localTrigger", a.current_trace_reference.local_trigger_observation_id || "");
  setValue("pathSourceObservation", a.attribution.path_source_observation_id || "");
}

function renderChips(containerId, ids, role) {
  const container = byId(containerId); container.replaceChildren();
  array(ids).forEach((id) => {
    const chip = document.createElement("span"); chip.className = "chip";
    const label = document.createElement("span"); label.textContent = id; label.title = id;
    const remove = document.createElement("button"); remove.type = "button"; remove.textContent = "×"; remove.disabled = state.readOnly; remove.setAttribute("aria-label", `移除${id}`);
    remove.addEventListener("click", () => removeChip(role, id));
    chip.append(label, remove); container.append(chip);
  });
  if (!array(ids).length) container.textContent = "尚未选择";
}

function removeChip(role, id) {
  const a = state.annotation;
  if (role === "failure") a.failure_manifestation.failure_observation_ids = a.failure_manifestation.failure_observation_ids.filter((value) => value !== id);
  if (role === "symptom") a.failure_manifestation.downstream_symptom_observation_ids = a.failure_manifestation.downstream_symptom_observation_ids.filter((value) => value !== id);
  if (role === "root") a.attribution.root_cause_observation_ids = a.attribution.root_cause_observation_ids.filter((value) => value !== id);
  if (role === "referenceEvidence") a.current_trace_reference.evidence_observation_ids = a.current_trace_reference.evidence_observation_ids.filter((value) => value !== id);
  if (role === "reviewEvidence") a.review.evidence_observation_ids = a.review.evidence_observation_ids.filter((value) => value !== id);
  renderRoleFields(); renderObservationTree(); markDirty();
}

function renderPropagation() {
  const container = byId("propagationList"); container.replaceChildren();
  const chain = state.annotation.current_trace_reference.propagation_observation_ids;
  chain.forEach((id, index) => {
    const row = document.createElement("div"); row.className = "ordered-chip";
    const order = document.createElement("b"); order.textContent = String(index + 1);
    const code = document.createElement("code"); code.textContent = id;
    const up = document.createElement("button"); up.type = "button"; up.textContent = "↑"; up.disabled = state.readOnly || index === 0; up.title = "上移";
    const down = document.createElement("button"); down.type = "button"; down.textContent = "↓"; down.disabled = state.readOnly || index === chain.length - 1; down.title = "下移";
    const remove = document.createElement("button"); remove.type = "button"; remove.textContent = "×"; remove.disabled = state.readOnly; remove.title = "移除";
    up.addEventListener("click", () => movePropagation(index, index - 1)); down.addEventListener("click", () => movePropagation(index, index + 1)); remove.addEventListener("click", () => { chain.splice(index, 1); renderPropagation(); markDirty(); });
    row.append(order, code, up, down, remove); container.append(row);
  });
  if (!chain.length) container.textContent = "尚未构建传播链";
}

function movePropagation(from, to) {
  const chain = state.annotation.current_trace_reference.propagation_observation_ids;
  [chain[from], chain[to]] = [chain[to], chain[from]];
  renderPropagation(); markDirty();
}

function locateFailure() {
  const id = state.current?.case.tool_attempt?.tool_result_observation_id;
  if (id) locateObservation(id);
}
function locateParent() {
  const current = findObservation(state.selectedObservationId); if (current?.parent_observation_id) locateObservation(current.parent_observation_id); else showToast("当前节点没有可见父节点", true);
}
function locateNext() {
  const ordered = state.current?.observations || []; const index = ordered.findIndex((item) => observationId(item) === state.selectedObservationId);
  if (index >= 0 && index < ordered.length - 1) locateObservation(observationId(ordered[index + 1])); else showToast("当前节点没有后续节点", true);
}
function locateObservation(id) {
  state.selectedObservationId = id; state.checkedObservationIds = new Set([id]);
  byId("observationSearch").value = ""; byId("observationTypeFilter").value = ""; byId("errorOnlyFilter").checked = false;
  renderObservationTree(); renderObservationDetail();
  requestAnimationFrame(() => document.querySelector(".tree-node.selected")?.scrollIntoView({ block: "center" }));
}

function showStep(step) {
  state.step = Math.max(1, Math.min(6, step));
  document.querySelectorAll("[data-step-panel]").forEach((panel) => panel.classList.toggle("hidden", Number(panel.dataset.stepPanel) !== state.step));
  document.querySelectorAll("[data-step]").forEach((button) => button.classList.toggle("active", Number(button.dataset.step) === state.step));
  byId("previousStepButton").disabled = state.step === 1;
  byId("nextStepButton").disabled = state.step === 6;
  byId("stepIndicator").textContent = `第 ${state.step} / 6 步`;
}

async function saveAnnotation(status, next) {
  if (!state.current || state.readOnly) return;
  clearTimeout(state.autosaveTimer);
  try {
    const annotation = collectAnnotation(status);
    const payload = await api(`/api/cases/${encodeURIComponent(annotation.case_id)}/annotations`, {
      method: "POST",
      body: JSON.stringify({ annotation, expected_source_hash: state.current.source_snapshot.source_hash, change_reason: value("changeReason") || null }),
    });
    state.annotation = payload.annotation; state.original = clone(payload.annotation); state.dirty = false; state.fieldErrors = {};
    showToast(status === "draft" ? `草稿revision ${payload.annotation.annotation_revision}已保存` : `已提交为${payload.annotation.annotation_status}`);
    await loadCases();
    if (next) {
      const index = state.cases.findIndex((item) => item.case_id === annotation.case_id);
      const nextCase = state.cases.slice(index + 1).find((item) => ["candidate", "draft"].includes(item.status));
      if (nextCase) await selectCase(nextCase.case_id, true); else await selectCase(annotation.case_id, true);
    } else await selectCase(annotation.case_id, true);
  } catch (error) {
    state.fieldErrors = error.fieldErrors || {};
    renderErrors();
    const firstStep = stepForError(Object.keys(state.fieldErrors)[0]);
    if (firstStep) showStep(firstStep);
    byId("validationSummary").focus();
    showToast(error.message, true);
  }
}

async function excludeCase() {
  if (!state.current || state.readOnly) return;
  if (!window.confirm("确定排除此Case吗？该操作会保留revision历史。")) return;
  await saveAnnotation("excluded", true);
}

function transitionCase(status, label) {
  const latest = state.current?.latest_annotation;
  if (!latest) return;
  state.pendingTransition = { status, label };
  byId("transitionDialogTitle").textContent = label;
  byId("transitionDialogHelp").textContent = status === "frozen"
    ? "冻结后该Case不能创建新revision；正式数据集冻结还需要使用顶部的冻结数据集操作。"
    : "裁决会基于当前最新标签创建新的不可变revision。若仍有未知结果、低置信度或截断上下文，服务端会保留needs_review。";
  setValue("transitionReason", label);
  byId("transitionDialog").showModal();
}

async function applyTransitionCase() {
  const pending = state.pendingTransition;
  const latest = state.current?.latest_annotation;
  if (!pending || !latest) return;
  const reason = value("transitionReason").trim();
  if (!reason) { showToast("请填写变更原因", true); return; }
  try {
    const annotation = clone(latest);
    annotation.annotation_status = pending.status;
    const payload = await api(`/api/cases/${encodeURIComponent(annotation.case_id)}/annotations`, {
      method: "POST",
      body: JSON.stringify({ annotation, expected_source_hash: state.current.source_snapshot.source_hash, change_reason: reason.trim() }),
    });
    byId("transitionDialog").close();
    state.pendingTransition = null;
    await loadCases();
    await selectCase(annotation.case_id, true);
    if (payload.annotation.annotation_status !== pending.status) {
      showToast(`未进入${pending.status}：仍有字段需要复核`, true);
      return;
    }
    showToast(`${pending.label}完成，revision ${payload.annotation.annotation_revision}`);
  } catch (error) {
    state.fieldErrors = error.fieldErrors || {};
    renderErrors();
    showToast(error.message, true);
  }
}

function resetChanges() {
  if (!state.current || state.readOnly) return;
  if (state.dirty && !window.confirm("确定丢弃当前未保存修改吗？")) return;
  state.annotation = clone(state.original); state.fieldErrors = {}; state.dirty = false;
  renderForm(); renderObservationTree(); updateSaveState(); showToast("已恢复到最近保存状态");
}

function markDirty() {
  state.dirty = true; updateSaveState(); scheduleAutosave();
}
function updateSaveState(message) { byId("saveState").textContent = message || (state.dirty ? "有未保存修改" : "已保存"); }
function scheduleAutosave() {
  clearTimeout(state.autosaveTimer);
  if (!state.dirty || state.readOnly || !byId("autosaveToggle").checked) return;
  updateSaveState("等待自动保存…");
  state.autosaveTimer = setTimeout(() => saveAnnotation("draft", false), 1400);
}

function renderErrors() {
  document.querySelectorAll(".field-error").forEach((item) => item.classList.remove("field-error"));
  document.querySelectorAll("[data-step]").forEach((item) => item.classList.remove("has-error"));
  const entries = Object.entries(state.fieldErrors);
  const summary = byId("validationSummary");
  summary.classList.toggle("hidden", !entries.length);
  summary.replaceChildren();
  if (!entries.length) return;
  const title = document.createElement("strong"); title.textContent = `请修正 ${entries.length} 个问题`;
  const list = document.createElement("ul");
  entries.forEach(([path, message]) => {
    const item = document.createElement("li"); const button = document.createElement("button"); button.type = "button"; button.className = "mini-button"; button.textContent = `${path}: ${message}`;
    button.addEventListener("click", () => { showStep(stepForError(path)); document.querySelector(`[data-field="${CSS.escape(path)}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" }); });
    item.append(button); list.append(item);
    document.querySelector(`[data-field="${CSS.escape(path)}"]`)?.classList.add("field-error");
    document.querySelector(`[data-step="${stepForError(path)}"]`)?.classList.add("has-error");
  });
  summary.append(title, list);
}

function stepForError(path) {
  if (path?.startsWith("technical")) return 1; if (path?.startsWith("semantic")) return 2;
  if (path?.startsWith("failure")) return 3; if (path?.startsWith("attribution")) return 4;
  if (path?.startsWith("current_trace")) return 5; return 6;
}

function renderRevisionHistory() {
  const container = byId("revisionHistory"); container.replaceChildren();
  const history = state.current.annotation_history || [];
  history.forEach((item) => {
    const row = document.createElement("button"); row.type = "button"; row.className = "history-row history-button";
    row.innerHTML = `<b>rev ${item.revision}</b><span>${escapeHtml(item.status)}</span><span>${escapeHtml(item.annotator_id)}</span><time>${escapeHtml(formatTime(item.created_at))}</time>`;
    row.addEventListener("click", () => openRevision(item));
    container.append(row);
  });
  if (!history.length) container.textContent = "尚无历史revision";
}

async function openRevision(item) {
  try {
    const caseId = state.current.case.case_id;
    const payload = await api(`/api/cases/${encodeURIComponent(caseId)}/annotations/${item.revision}`);
    const annotation = payload.annotation;
    byId("revisionDialogTitle").textContent = `${caseId} · revision ${item.revision}`;
    renderDefinitionList(byId("revisionDialogMetadata"), [
      ["状态", annotation.annotation_status],
      ["标注人", annotation.audit?.annotator_id || item.annotator_id],
      ["创建时间", annotation.audit?.created_at || item.created_at],
      ["修改原因", annotation.audit?.change_reason || item.change_reason || "—"],
    ]);
    renderRevisionDiff(annotation, state.current.latest_annotation || state.annotation);
    byId("revisionPayload").textContent = JSON.stringify(annotation, null, 2);
    byId("revisionDialog").showModal();
  } catch (error) { showToast(error.message, true); }
}

function renderRevisionDiff(historical, current) {
  const container = byId("revisionDiff"); container.replaceChildren();
  const oldValues = flattenObject(historical);
  const currentValues = flattenObject(current || {});
  const paths = unique([...Object.keys(oldValues), ...Object.keys(currentValues)])
    .filter((path) => !path.startsWith("audit.") && oldValues[path] !== currentValues[path])
    .sort();
  paths.slice(0, 80).forEach((path) => {
    const row = document.createElement("div"); row.className = "diff-row";
    const key = document.createElement("code"); key.textContent = path;
    const value = document.createElement("span"); value.textContent = `${oldValues[path] ?? "∅"} → ${currentValues[path] ?? "∅"}`;
    row.append(key, value); container.append(row);
  });
  if (!paths.length) container.textContent = "与当前 revision 无差异。";
  else if (paths.length > 80) container.append(`另有 ${paths.length - 80} 项差异未展开。`);
}

function flattenObject(value, prefix = "", result = {}) {
  if (Array.isArray(value)) {
    result[prefix] = JSON.stringify(value);
    return result;
  }
  if (value && typeof value === "object") {
    Object.entries(value).forEach(([key, item]) => flattenObject(item, prefix ? `${prefix}.${key}` : key, result));
    return result;
  }
  result[prefix] = value == null ? null : String(value);
  return result;
}

async function syncSource() {
  byId("syncButton").disabled = true;
  try { const result = await api("/api/sync", { method: "POST", body: "{}" }); await loadCases(); showToast(`已同步 ${result.case_count} 条Case`); }
  catch (error) { showToast(error.message, true); }
  finally { byId("syncButton").disabled = false; }
}

async function exportAnnotations() {
  try {
    const headers = {}; const token = sessionStorage.getItem("annotationToken"); if (token) headers.Authorization = `Bearer ${token}`;
    const response = await fetch("/api/export", { headers }); if (!response.ok) throw new Error(`导出失败 (${response.status})`);
    const blob = await response.blob(); const url = URL.createObjectURL(blob); const anchor = document.createElement("a");
    anchor.href = url; anchor.download = "annotations.jsonl"; document.body.append(anchor); anchor.click(); anchor.remove(); URL.revokeObjectURL(url);
    showToast("annotations.jsonl 已导出");
  } catch (error) { showToast(error.message, true); }
}

async function importAnnotations(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  try {
    const lines = (await file.text()).split(/\r?\n/).filter((line) => line.trim());
    const annotations = lines.map((line, index) => {
      try { return JSON.parse(line); }
      catch { throw new Error(`第 ${index + 1} 行不是有效JSON`); }
    });
    if (!annotations.length) throw new Error("JSONL文件没有Annotation");
    const preview = await api("/api/import", {
      method: "POST",
      body: JSON.stringify({ annotations, dry_run: true }),
    });
    if (!preview.can_apply) {
      state.pendingImport = null;
      openQualityReport(preview);
      throw new Error(`导入预检失败：${preview.errors.length} 个问题`);
    }
    state.pendingImport = annotations;
    openQualityReport(preview);
    showToast(`预检通过 ${preview.importable_count} 条Annotation，请确认导入`);
  } catch (error) { showToast(error.message, true); }
  finally { event.target.value = ""; }
}

async function applyPendingImport() {
  const annotations = state.pendingImport;
  if (!annotations) return;
  byId("applyImportButton").disabled = true;
  try {
    const result = await api("/api/import", {
      method: "POST",
      body: JSON.stringify({ annotations, dry_run: false }),
    });
    if (!result.can_apply) {
      openQualityReport(result);
      throw new Error("导入状态在预检后发生变化，请重新选择文件");
    }
    state.pendingImport = null;
    byId("qualityDialog").close();
    await loadCases();
    showToast(`已导入 ${result.imported_count} 条Annotation`);
  } catch (error) { showToast(error.message, true); }
  finally { byId("applyImportButton").disabled = false; }
}

async function openQualityReport(provided = null) {
  try {
    const report = provided || await api("/api/quality");
    renderDefinitionList(byId("qualitySummary"), [
      ["Case总数", report.case_count ?? report.total_count ?? "—"],
      ["已标注", report.annotated_case_count ?? report.importable_count ?? "—"],
      ["可导出", report.exportable_count ?? "—"],
      ["待复核", report.needs_review_count ?? "—"],
      ["已冻结", report.frozen_count ?? report.frozen_case_count ?? "—"],
      ["可执行", report.can_apply ?? report.can_freeze ?? "—"],
    ]);
    byId("qualityPayload").textContent = JSON.stringify(report, null, 2);
    byId("applyImportButton").classList.toggle("hidden", !state.pendingImport || !report.can_apply);
    if (!byId("qualityDialog").open) byId("qualityDialog").showModal();
  } catch (error) { showToast(error.message, true); }
}

function freezeDataset() {
  const suggested = `fnf-${new Date().toISOString().slice(0, 10).replaceAll("-", "")}-v1`;
  setValue("datasetVersion", suggested);
  setValue("datasetTestRatio", "0.4");
  state.freezePreview = null;
  byId("freezePreview").textContent = "尚未预检";
  byId("applyFreezeButton").disabled = true;
  byId("freezeDialog").showModal();
}

async function previewDatasetFreeze() {
  const version = value("datasetVersion").trim();
  const testRatio = Number(value("datasetTestRatio"));
  try {
    const request = { dataset_version: version, dry_run: true, test_ratio: testRatio };
    const preview = await api("/api/freeze", { method: "POST", body: JSON.stringify(request) });
    state.freezePreview = { ...preview, request };
    byId("freezePreview").textContent = JSON.stringify(preview, null, 2);
    byId("applyFreezeButton").disabled = !preview.can_freeze;
    if (!preview.can_freeze) {
      showToast(`冻结被 ${preview.blockers.length} 条未完成Case阻止`, true);
      return;
    }
    showToast(`预检通过：可冻结 ${preview.frozen_case_count} 条Case`);
  } catch (error) {
    state.freezePreview = null;
    byId("applyFreezeButton").disabled = true;
    showToast(error.message, true);
  }
}

async function applyDatasetFreeze() {
  const preview = state.freezePreview;
  if (!preview?.can_freeze) return;
  byId("applyFreezeButton").disabled = true;
  try {
    const request = { ...preview.request, dry_run: false };
    const headers = { "Content-Type": "application/json" };
    const token = sessionStorage.getItem("annotationToken"); if (token) headers.Authorization = `Bearer ${token}`;
    const response = await fetch("/api/freeze", { method: "POST", headers, body: JSON.stringify(request) });
    const contentType = response.headers.get("content-type") || "";
    if (!response.ok || contentType.includes("json")) {
      const payload = await response.json();
      byId("freezePreview").textContent = JSON.stringify(payload, null, 2);
      throw new Error(payload.field_errors ? Object.values(payload.field_errors).join("；") : "冻结失败");
    }
    const blob = await response.blob(); const url = URL.createObjectURL(blob); const anchor = document.createElement("a");
    anchor.href = url; anchor.download = `${request.dataset_version}.zip`; document.body.append(anchor); anchor.click(); anchor.remove(); URL.revokeObjectURL(url);
    byId("freezeDialog").close();
    state.freezePreview = null;
    await loadCases();
    if (state.current) await selectCase(state.current.case.case_id, true);
    showToast(`数据集 ${request.dataset_version} 已冻结并下载`);
  } catch (error) { showToast(error.message, true); }
  finally { byId("applyFreezeButton").disabled = false; }
}

function onShortcut(event) {
  if (event.ctrlKey || event.metaKey || event.altKey || /INPUT|SELECT|TEXTAREA/.test(event.target.tagName)) return;
  const role = { f: "failure", r: "root", e: "evidence", c: "context", t: "trigger" }[event.key.toLowerCase()];
  if (role) { event.preventDefault(); assignRole(role); }
}

function renderDefinitionList(container, entries) {
  container.replaceChildren(); entries.forEach(([label, value]) => { const wrapper = document.createElement("div"); const dt = document.createElement("dt"); const dd = document.createElement("dd"); dt.textContent = label; dd.textContent = text(value === null || value === undefined || value === "" ? "—" : value); wrapper.append(dt, dd); container.append(wrapper); });
}
function observationId(item) { return text(item?.observation_id || item?.id); }
function findObservation(id) { return state.current?.observations.find((item) => observationId(item) === id); }
function sortObservationIds(ids) { const order = new Map(state.current.observations.map((item, index) => [observationId(item), index])); return unique(ids).sort((left, right) => (order.get(left) ?? 1e9) - (order.get(right) ?? 1e9)); }
function roleLetter(role) { return { failure: "F", root: "R", evidence: "E", context: "C", trigger: "T", symptom: "S", recovery: "Y" }[role] || "?"; }
function value(id) { return byId(id).value; }
function setValue(id, next) { byId(id).value = text(next); }
function triStateValue(value) { return value === true ? "true" : value === false ? "false" : "null"; }
function triStateLabel(value) { return value === true ? "true" : value === false ? "false" : "unknown"; }
function parseTriState(value) { return value === "true" ? true : value === "false" ? false : null; }
function formatTime(value) { if (!value) return "—"; const date = new Date(value); return Number.isNaN(date.getTime()) ? text(value) : date.toLocaleString(); }
function escapeHtml(value) { return text(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]); }
let toastTimer;
function showToast(message, error = false) { clearTimeout(toastTimer); const toast = byId("toast"); toast.textContent = message; toast.classList.toggle("error", error); toast.classList.add("visible"); toastTimer = setTimeout(() => toast.classList.remove("visible"), 3600); }
