(() => {
  "use strict";

  const $ = id => document.getElementById(id);
  const list = $("runs-list");
  const dashboard = $("dashboard");
  const batchDashboard = $("batch-dashboard");
  const detail = $("run-detail");
  const roundTemplate = $("run-round").cloneNode(true);
  const dialog = $("filter-dialog");
  const filterButtons = [...document.querySelectorAll("[data-filter]")];
  const phone = matchMedia("(max-width: 767px)");
  const timeFormat = new Intl.DateTimeFormat("vi-VN", {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23"
  });
  const numberFormat = new Intl.NumberFormat("vi-VN");
  const decimalFormat = new Intl.NumberFormat("vi-VN", {
    minimumFractionDigits: 1, maximumFractionDigits: 1
  });
  const badgeClasses = new Map([
    ["READY", "READY"], ["APPEND", "APPEND"], ["NEED_INFO", "NEED"],
    ["HOLD", "HOLD"], ["CHAT", "CHAT"], ["ESCALATED", "ESC"]
  ]);
  const actions = new Map([
    ["READY", "Tạo ticket mới, trả lời đúng tin gốc"],
    ["APPEND", "Bổ sung vào ticket hiện có"],
    ["NEED_INFO", "Hỏi thêm thông tin"],
    ["MEDIA_ONLY", "Chờ mô tả cho ảnh"],
    ["CHAT", "Bỏ qua tin nhắn"],
    ["HOLD", "Tạm giữ lượt xử lý"],
    ["ESCALATED", "Báo cho nhóm vận hành"],
    ["MULTI", "Xử lý theo quyết định của từng nhóm tin"]
  ]);
  const batchStates = new Map([
    ["collecting", "Đang gom"], ["queued", "Chờ xử lý"], ["processing", "Đang xử lý"],
    ["finished", "Đã xử lý"], ["interrupted", "Bị gián đoạn"]
  ]);
  const messageDecisions = new Map([
    ["queued", "Chờ xử lý"], ["classifying", "Đang phân loại"], ["in_case", "Đã đưa vào hồ sơ"],
    ["pending", "Chờ kết quả ghi ticket"], ["pending_review", "Chờ xem xét"], ["needs_human", "Cần người xử lý"],
    ["issue", "Đã tạo ticket"], ["followup", "Đã ghi bổ sung"], ["chat", "Bỏ qua tin nhắn"],
    ["noise", "Bỏ qua tin nhiễu"], ["duplicate", "Tin trùng"], ["error", "Lỗi xử lý"],
    ["context_pending", "Chờ thêm ngữ cảnh"], ["rate_limited", "Tạm giới hạn xử lý"]
  ]);
  const batchPollDelay = 2000;
  const savedTraceRefreshDelay = 30000;
  const batches = new Map();
  const batchRoundCache = new Map();
  const renderedBatchRounds = new WeakMap();
  let view = null;
  let batchId = null;
  let batchData = null;
  let batchDetailUpdated = 0;
  let messageId = null;
  let batchListOnPhone = false;
  let batchTimer;
  let batchPoll = null;
  let batchRefreshPending = false;
  let batchManualPending = false;
  let batchConnected = false;
  let batchUnavailable = false;
  let batchLastUpdate = null;

  const filters = { state: "", model: "", chat: "" };
  const runs = new Map();
  const rows = new Map();
  const pending = new Map();
  let pendingCount = 0;
  let active = false;
  let session = 0;
  let pageRequest = null;
  let needsSnapshot = false;
  let nextBefore = null;
  let selectedId = null;
  let detailRequest = 0;
  let filterKey = "";
  let follow = true;
  let paused = false;
  let stream = null;
  let streamTimer;
  let sessionCheck = null;
  let connected = false;
  let everConnected = false;
  let lastUpdate = null;

  function element(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value != null) node.textContent = value;
    return node;
  }

  function setStatus(node, message) {
    setText(node, message);
    node.hidden = !message;
  }

  function setText(node, value) {
    if (node.textContent !== value) node.textContent = value;
  }

  function time(seconds) {
    return Number.isFinite(seconds) ? timeFormat.format(seconds * 1000) : "—";
  }

  function number(value) {
    return Number.isFinite(value) ? numberFormat.format(value) : "—";
  }

  function duration(ms) {
    return Number.isFinite(ms) ? decimalFormat.format(ms / 1000) + " s" : "";
  }

  function join(...parts) {
    return parts.filter(value => value !== "" && value != null).join(" · ");
  }

  function badge(run) {
    const color = run.in_progress ? "RUN" : badgeClasses.get(run.final_state);
    return element("span", color ? "badge " + color : "badge",
      run.in_progress ? "ĐANG CHẠY" : run.final_state || "—");
  }

  function chatLabel(run) {
    return run.chat_label || (run.chat_id == null ? "Chat —" : "Chat " + run.chat_id);
  }

  const batchCode = batch => /^[a-f0-9]{32}$/i.test(batch.batch_id) ? batch.batch_id.slice(0, 8) : batch.batch_id;
  const batchLabel = batch => "Batch #" + batchCode(batch);
  const batchState = batch => batchStates.get(batch.status) || "Trạng thái chưa rõ";
  const batchIsOpen = batch => ["collecting", "queued", "processing"].includes(batch.status);

  function formatTime(node, seconds) {
    setText(node, time(seconds));
    if (Number.isFinite(seconds)) node.dateTime = new Date(seconds * 1000).toISOString();
    else node.removeAttribute("datetime");
  }

  function reconcileRows(container, entries, keyOf, update, tag = "li") {
    const focused = document.activeElement;
    const existing = new Map([...container.children].map(node => [node.dataset.key, node]));
    let position = container.firstElementChild;
    for (const entry of entries) {
      const key = keyOf(entry);
      const node = existing.get(key) || element(tag);
      node.dataset.key = key;
      existing.delete(key);
      update(node, entry, key);
      if (node !== position) container.insertBefore(node, position);
      position = node.nextElementSibling;
    }
    for (const node of existing.values()) node.remove();
    if (container.contains(focused) && document.activeElement !== focused) focused.focus({ preventScroll: true });
  }

  function renderBatches() {
    reconcileRows($("batches-list"), batches.values(), batch => batch.batch_id, (row, batch, id) => {
      if (!row.firstChild) {
        const button = element("button", "batch-item");
        button.type = "button";
        button.append(element("strong"), element("span", "batch-note"));
        button.addEventListener("click", () => selectBatch(id, true));
        row.append(button);
      }
      const button = row.firstChild;
      button.setAttribute("aria-current", String(id === batchId));
      button.title = "Batch #" + id;
      setText(button.firstChild, chatLabel(batch));
      setText(button.lastChild, join("#" + batchCode(batch), batchState(batch), number(batch.message_count) + " tin"));
    });
  }

  function messageState(message, batch) {
    if (batch.status === "collecting" && (!message.decision || message.decision === "queued")) return batchState(batch);
    if (batch.status === "interrupted" && (!message.decision || ["queued", "classifying"].includes(message.decision))) {
      return batchState(batch);
    }
    if (message.decision) return messageDecisions.get(message.decision) ?? message.decision;
    if (batchIsOpen(batch)) return batchState(batch);
    return "Chưa có quyết định";
  }

  function renderBatchDetail() {
    const { batch, messages } = batchData;
    const title = batchLabel(batch);
    setText($("batch-crumb"), "Theo dõi batch / " + chatLabel(batch));
    setText($("batch-title"), title);
    $("batch-title").title = "Batch #" + batch.batch_id;
    setText($("batch-badge"), batchState(batch));
    $("batch-badge").dataset.state = batch.status;
    setText($("batch-meta"), join(chatLabel(batch), "Bắt đầu " + time(batch.started_at), number(batch.message_count) + " tin"));
    setText($("batch-messages-title"), "Tin trong batch (" + number(messages.length) + ")");
    reconcileRows($("batch-messages"), messages, message => String(message.message_id), (row, message, id) => {
      if (!row.firstChild) {
        const button = element("button", "batch-message-grid batch-message");
        button.type = "button";
        button.setAttribute("aria-controls", "batch-trace");
        const body = element("span", "batch-message-body");
        body.append(element("strong"), element("span", "message-text"));
        const status = element("span", "batch-message-state");
        status.append(element("span"), element("span", "batch-note", "Xem chi tiết ↓"));
        button.append(element("time"), body, status);
        button.addEventListener("click", () => setBatchScope(id, true));
        row.append(button);
      }
      const [when, body, status] = row.firstChild.children;
      formatTime(when, message.received_at);
      setText(body.firstChild, join(message.sender_name || "Không rõ người gửi", "#" + id));
      setText(body.lastChild, message.text || "Không có văn bản");
      setText(status.firstChild, messageState(message, batch));
    });
    const picker = $("batch-scope");
    const choices = [
      ["", "Toàn batch #" + batchCode(batch)],
      ...messages.map(message => [String(message.message_id), join("Tin #" + message.message_id, message.sender_name)])
    ];
    reconcileRows(picker, choices, ([id]) => id, (option, [, label], id) => {
      option.value = id;
      setText(option, label);
    }, "option");
    $("batch-content").hidden = false;
    renderBatchTrace();
  }

  function batchScope() {
    const { messages } = batchData;
    const selected = messages.find(message => String(message.message_id) === messageId);
    const scoped = selected ? [selected] : messages;
    const ids = [...new Set(scoped.flatMap(message => message.run_ids))].sort((a, b) => a - b);
    return { selected, scoped, ids };
  }

  function traceStatus(batch, scoped, ids) {
    if (!ids.length) {
      if (batch.status === "collecting") return "Đang gom · Chưa bắt đầu lượt xử lý.";
      if (batch.status === "queued") return "Chờ xử lý · Chưa có nhật ký lượt.";
      if (batch.status === "processing") return "Đang xử lý · Chưa có nhật ký cho phạm vi này.";
      return "Không có nhật ký lượt cho phạm vi này.";
    }
    const records = ids.map(id => batchRoundCache.get(id));
    const missing = records.filter(record => record?.result === null).length;
    const failed = records.some(record => record?.failed);
    const available = records.some(record => record?.result);
    const loading = records.some(record => !record || record.result === undefined);
    const unresolved = scoped.filter(message => !message.run_ids.length).length;
    const incomplete = records.some(record => record?.result && (!record.result.run.final_state || record.result.run.in_progress));
    return join(
      batch.status === "interrupted" ? "Batch đã bị gián đoạn. Nhật ký của các tin có thể gồm lượt xử lý lại sau đó." : "",
      loading && !failed ? "Đang tải nhật ký lượt…" : "",
      failed ? (available ? "Không tải được một phần nhật ký lượt." : "Không tải được nhật ký lượt. Đang thử lại.") : "",
      missing ? number(missing) + " lượt chưa có nhật ký lưu lại." : "",
      unresolved ? number(unresolved) + " tin chưa có nhật ký lượt." : "",
      incomplete ? "Một số lượt chưa ghi đủ kết quả." : ""
    );
  }

  function renderBatchTrace() {
    const { batch, messages } = batchData;
    const { selected, scoped, ids } = batchScope();
    messageId = selected ? String(selected.message_id) : null;
    if ($("batch-scope").value !== (messageId ?? "")) $("batch-scope").value = messageId ?? "";
    for (const row of $("batch-messages").children) {
      row.firstChild.setAttribute("aria-pressed", String(row.dataset.key === messageId));
    }
    const title = selected ? "tin #" + selected.message_id : "batch #" + batchCode(batch);
    setText($("batch-trace-title"), "Chi tiết xử lý " + title);
    setStatus($("batch-trace-status"), traceStatus(batch, scoped, ids));
    const shared = selected ? messages.filter(message => message !== selected && message.run_ids.some(id => ids.includes(id))) : [];
    setStatus($("batch-trace-shared"), shared.length
      ? "Xử lý chung với " + shared.map(message => "tin #" + message.message_id).join(", ") + "."
      : "");
    reconcileRows($("batch-receipts"), batch.status === "collecting" ? scoped : [],
      message => String(message.message_id), (row, message) => setText(row, join(time(message.received_at),
        "Đã nhận tin #" + message.message_id, message.sender_name)));
    const recorded = ids.flatMap(id => {
      const round = batchRoundCache.get(id);
      return round?.result ? [{ id, round }] : [];
    });
    renderBatchRounds(recorded);
  }

  function renderBatchRounds(recorded) {
    reconcileRows($("batch-rounds"), recorded, ({ id }) => String(id), (wrapper, { id, round }) => {
      const rendered = renderedBatchRounds.get(wrapper);
      if (rendered?.scope === messageId && rendered.revision === round.revision) return;
      // Only replace rounds whose data or message highlight changed.
      renderedBatchRounds.set(wrapper, { scope: messageId, revision: round.revision });
      const focused = document.activeElement;
      const expanded = new Set([...wrapper.querySelectorAll("details[open]")].map(keyForDisclosure));
      const focusedLink = wrapper.querySelector(".batch-run-title a") === focused;
      const focusedKey = focused.tagName === "SUMMARY" && wrapper.contains(focused)
        ? keyForDisclosure(focused.parentElement) : null;
      const { run } = round.result;
      const heading = element("h4", "batch-run-title");
      const link = element("a", "", "Lượt #" + id);
      link.href = "#run-" + id;
      heading.append(link, badge(run));
      const meta = element("p", "batch-note", join(time(run.created_at), number(run.message_count) + " tin",
        run.model || run.provider, number(run.model_calls) + " lần gọi", duration(run.latency_ms)));
      const entry = renderRound(round.result, 0, { batch: true, scope: messageId, chatId: batchData.batch.chat_id });
      wrapper.replaceChildren(heading, meta, entry);
      for (const disclosure of entry.querySelectorAll("details")) {
        const key = keyForDisclosure(disclosure);
        disclosure.open = expanded.has(key);
        if (key === focusedKey) disclosure.firstChild.focus({ preventScroll: true });
      }
      if (focusedLink) link.focus({ preventScroll: true });
    }, "article");
  }

  function setBatchScope(id, focus = false) {
    messageId = id;
    renderBatchTrace();
    if (focus) {
      $("batch-trace-title").focus({ preventScroll: true });
      $("batch-trace").scrollIntoView({ block: "start" });
    }
    if (batchScope().ids.some(runId => !batchRoundCache.has(runId))) refreshBatches({ manual: true });
  }

  function selectBatch(id, explicit = false) {
    batchListOnPhone = false;
    document.body.classList.remove("show-batch-list");
    if (id !== batchId) {
      batchId = id;
      messageId = null;
      batchData = null;
      batchDetailUpdated = 0;
      batchRoundCache.clear();
      $("batch-rounds").replaceChildren();
      $("batch-content").hidden = true;
      $("batch-empty").hidden = true;
      setStatus($("batch-detail-status"), "Đang tải batch…");
      $("batch-detail").scrollTop = 0;
      renderBatches();
    }
    if (explicit) {
      if (phone.matches) window.scrollTo(0, 0);
      if (batchData) $("batch-title").focus({ preventScroll: true });
      else $("batch-detail-status").focus({ preventScroll: true });
      refreshBatches({ cancel: true, manual: true });
    }
  }

  async function fetchBatchJson(url, signal) {
    const response = await request(url, { signal: AbortSignal.any([signal, AbortSignal.timeout(10000)]) });
    if (!response || signal.aborted) return null;
    if (response.status === 404) return null;
    if (!response.ok) throw new Error();
    return response.json();
  }

  async function updateBatchList(signal) {
    const payload = await fetchBatchJson("/api/batches", signal);
    if (signal.aborted) return;
    if (!payload) throw new Error();
    batches.clear();
    for (const batch of payload.batches) batches.set(batch.batch_id, batch);
    if (batchId === null && batches.size) selectBatch(batches.keys().next().value);
    else renderBatches();
    setStatus($("batches-status"), batches.size ? "" : "Chưa có batch nào được ghi lại.");
    $("batch-empty").hidden = Boolean(batches.size || batchId !== null);
  }

  async function updateBatchDetail(signal) {
    const id = batchId;
    if (id === null) return;
    const summary = batches.get(id);
    // A closed batch may receive run links or message outcomes after its collector work ends.
    const recentlyRead = Date.now() - batchDetailUpdated < savedTraceRefreshDelay;
    if (recentlyRead && batchData && !batchIsOpen(batchData.batch) && (!summary || (
      summary.status === batchData.batch.status && summary.message_count === batchData.batch.message_count
      && chatLabel(summary) === chatLabel(batchData.batch)
    ))) {
      setStatus($("batch-detail-status"), "");
      return;
    }
    const payload = await fetchBatchJson("/api/batches/" + encodeURIComponent(id), signal);
    if (signal.aborted || id !== batchId) return;
    if (!payload) {
      batchData = null;
      $("batch-content").hidden = true;
      setStatus($("batch-detail-status"), "Không tìm thấy batch này.");
      return;
    }
    const wasLoading = document.activeElement === $("batch-detail-status");
    batchData = payload;
    batchDetailUpdated = Date.now();
    setStatus($("batch-detail-status"), "");
    renderBatchDetail();
    if (wasLoading) $("batch-title").focus({ preventScroll: true });
  }

  async function updateBatchRounds(signal) {
    const { batch } = batchData;
    const { ids } = batchScope();
    const open = batchIsOpen(batch);
    const scheduled = ids.filter(id => {
      const cached = batchRoundCache.get(id);
      if (!cached || cached.failed) return true;
      const elapsed = Date.now() - cached.attemptedAt;
      const incomplete = !cached.result || cached.result.run.in_progress;
      if (open && incomplete) return true;
      if (cached.open && !open && incomplete) return true;
      return elapsed >= savedTraceRefreshDelay;
    });
    // Cap the number of exact requests in a cycle and load unseen rounds before retrying partial ones.
    scheduled.sort((a, b) => (batchRoundCache.get(a)?.attemptedAt ?? 0) - (batchRoundCache.get(b)?.attemptedAt ?? 0));
    for (const id of scheduled.slice(0, 8)) {
      if (signal.aborted || !batchScope().ids.includes(id)) return;
      try {
        const result = await fetchBatchJson("/api/rounds/" + id, signal);
        if (signal.aborted) return;
        if (result && result.run.run_id !== id) throw new Error();
        const previous = batchRoundCache.get(id);
        const signature = JSON.stringify(result);
        const revision = signature === previous?.signature ? previous.revision : (previous?.revision ?? 0) + 1;
        batchRoundCache.set(id, { result, signature, revision, open, attemptedAt: Date.now() });
      } catch {
        if (signal.aborted) return;
        batchRoundCache.set(id, { ...batchRoundCache.get(id), failed: true, attemptedAt: Date.now() });
      }
      renderBatchTrace();
    }
  }

  function stopBatchUpdates() {
    clearTimeout(batchTimer);
    batchRefreshPending = false;
    batchManualPending = false;
    batchPoll?.abort();
  }

  function canPollBatches() {
    return active && view === "batches" && !paused && !document.hidden;
  }

  async function refreshBatches({ cancel = false, manual = false } = {}) {
    clearTimeout(batchTimer);
    if (!active || view !== "batches" || document.hidden || (paused && !manual)) return;
    if (batchPoll) {
      batchRefreshPending = true;
      batchManualPending ||= manual;
      if (cancel) batchPoll.abort();
      return;
    }
    const controller = new AbortController();
    const { signal } = controller;
    batchPoll = controller;
    try {
      if (!manual) await updateBatchList(signal);
      if (signal.aborted) return;
      if (!manual || !batchData) await updateBatchDetail(signal);
      if (signal.aborted) return;
      if (!manual) {
        batchConnected = true;
        batchUnavailable = false;
        batchLastUpdate = Date.now() / 1000;
      }
      renderLive();
      if (batchData && !(phone.matches && batchListOnPhone)) await updateBatchRounds(signal);
    } catch {
      if (signal.aborted) return;
      batchConnected = false;
      batchUnavailable = true;
      $("batch-empty").hidden = true;
      if (!batches.size) setStatus($("batches-status"), "Không tải được danh sách batch.");
      if (batchId !== null) setStatus($("batch-detail-status"), batchData
        ? "Mất kết nối · Đang hiển thị dữ liệu lần cập nhật trước."
        : "Không tải được batch. Đang thử kết nối lại.");
      renderLive();
    } finally {
      batchPoll = null;
      const pending = batchRefreshPending;
      const manualPending = batchManualPending;
      batchRefreshPending = false;
      batchManualPending = false;
      if (pending && (manualPending || canPollBatches())) {
        batchTimer = setTimeout(() => refreshBatches({ manual: manualPending }), 0);
      } else if (canPollBatches()) {
        batchTimer = setTimeout(refreshBatches, batchPollDelay);
      }
    }
  }

  function codeBlock(value, label, { json = false, collapsed = false, prose = false } = {}) {
    const text = json ? JSON.stringify(value, null, 2) : String(value);
    const block = element(prose ? "div" : "pre", prose ? "message-text" : "", text);
    if (!collapsed && text.length <= 1200) return block;
    const details = element("details");
    details.append(element("summary", "", label), block);
    return details;
  }

  function ticketLink(url, label) {
    try {
      const target = new URL(url);
      if (target.protocol === "https:" && !target.username && !target.password) {
        const link = element("a", "", label || target.href);
        link.href = target.href;
        return link;
      }
    } catch {
      // Some historical runs have a ticket reference without a URL.
    }
    return element("span", "", label || "—");
  }

  function renderInputs(panel, inputs, run, { batch, scope, chatId }) {
    const messages = inputs.map(input => {
      const item = element("div", "msg");
      const reference = input.message_id == null ? "" : "Tin #" + input.message_id;
      const selected = scope != null && String(input.message_id) === scope
        && String(input.chat_id ?? chatId) === String(chatId);
      if (selected) item.classList.add("selected-input");
      const sender = batch
        ? join(time(input.received_at), input.sender_name || "Không rõ người gửi", reference, selected ? "Tin đang xem" : "")
        : join(time(input.received_at), input.sender_name || reference || "Không rõ người gửi");
      const body = element("div");
      if (input.text) body.append(codeBlock(input.text, "Nội dung " + reference, { prose: true }));
      else body.append(element("span", "sub", "Không có văn bản"));
      if (input.issue_url) body.append(ticketLink(input.issue_url, "Ticket của tin nhắn"));
      item.append(element("span", "who", sender), body);
      return item;
    });
    if (!messages.length && run.message_refs?.length) {
      messages.push(codeBlock(run.message_refs, "Mã tham chiếu tin nhắn", { json: true }));
    }
    if (!messages.length) messages.push(element("div", "sub", "Không có dữ liệu tin nhắn."));
    panel.replaceChildren(...messages);
  }

  function stepOrder(a, b) {
    const byTime = (a.started_at ?? Infinity) - (b.started_at ?? Infinity);
    return byTime || (a.kind === b.kind ? a.seq - b.seq : a.kind === "model" ? -1 : 1);
  }

  function renderSteps(panel, steps) {
    const timeline = element("ol", "steps");
    for (const step of steps) {
      const isModel = step.kind === "model";
      const item = element("li", isModel ? "step think" : "step tool");
      const metadata = isModel
        ? join(step.model || "Chưa ghi tên model", duration(step.latency_ms),
          step.reasoning_tokens == null ? "" : "suy luận " + number(step.reasoning_tokens) + " token",
          step.prompt_tokens == null ? "" : number(step.prompt_tokens) + " → " + number(step.completion_tokens) + " token",
          step.timeout_s == null ? "" : "giới hạn " + number(step.timeout_s) + " s",
          step.finish_reason, step.error_class, step.schema_error_kind)
        : join(duration(step.latency_ms), step.result_bytes == null ? "" : number(step.result_bytes) + " B", step.error);
      item.append(
        element("span", "kind", isModel ? "Thinking" : "Công cụ"),
        element("span", "lbl", isModel ? "Lần gọi " + step.seq : step.tool || "—"),
        element("span", "sub", " " + metadata)
      );
      if (isModel) {
        if (step.reasoning_text) item.append(codeBlock(step.reasoning_text, "Nội dung suy luận"));
        if (step.content_text) item.append(codeBlock(step.content_text, "Đầu ra thô", { collapsed: true }));
        if (step.tool_calls != null) item.append(codeBlock(step.tool_calls, "Yêu cầu gọi công cụ", { json: typeof step.tool_calls !== "string", collapsed: true }));
      } else {
        if (step.args != null) item.append(codeBlock(step.args, "Tham số", { json: true }));
        if (step.result != null) {
          item.append(codeBlock(step.result, "Kết quả", {
            json: typeof step.result !== "string", collapsed: true
          }));
        }
      }
      for (const disclosure of item.querySelectorAll("details")) {
        disclosure.dataset.key = step.kind + "-" + step.seq + "-" + disclosure.firstChild.textContent;
      }
      timeline.append(item);
    }
    panel.replaceChildren(timeline.childElementCount ? timeline : element("div", "sub", "Chưa có bước được ghi lại."));
  }

  function renderProposal(panel, steps, run) {
    const modelSteps = steps.filter(step => step.kind === "model");
    const lastModel = modelSteps.at(-1);
    const context = join(
      lastModel ? "Lần gọi cuối " + lastModel.seq : run.model_calls ? number(run.model_calls) + " lần gọi" : "",
      run.fallback_reason ? "fallback: " + run.fallback_reason : run.proposal != null ? "Không fallback" : "",
      run.finish_reason ? "finish_reason: " + run.finish_reason : ""
    );
    panel.replaceChildren();
    if (context) panel.append(element("div", "sub", context));
    if (run.proposal != null) {
      panel.append(codeBlock(run.proposal, "Đề xuất", { json: true }));
      return;
    }
    const finalOutput = run.phase === "assistant_decide" && modelSteps.findLast(step =>
      step.finish_reason !== "tool_calls" && step.content_text?.trim());
    if (finalOutput) {
      panel.append(element("div", "sub", "Đầu ra thô của lần gọi " + finalOutput.seq),
        codeBlock(finalOutput.content_text, "Xem đầu ra thô"));
      return;
    }
    panel.append(element("div", "sub", "Chưa có đầu ra được ghi lại."));
  }

  function legacyRuleLabel(code, text) {
    return code ? join(code, "Quy tắc xử lý", text || "Không có nguyên văn trong nhật ký") : null;
  }

  function renderPrecheck(panel, run) {
    if (!run.precheck_summary) {
      panel.replaceChildren(element("div", "sub", "Chưa ghi dữ liệu lọc trước."));
      return;
    }
    panel.replaceChildren(element("div", "", run.precheck_summary));
    if (run.precheck_rule) {
      panel.append(codeBlock(run.precheck_rule, "Nguyên văn luật", { prose: true }));
    }
    if (run.precheck_checks?.length) {
      const disclosure = element("details");
      const list = element("ul");
      for (const check of run.precheck_checks) {
        list.append(element("li", "", join(check.rule_display,
          check.match ? "Khớp" : "Không khớp", check.why)));
      }
      disclosure.append(element("summary", "", "Các bước kiểm tra"), list);
      panel.append(disclosure);
    }
  }

  function renderDecision(panel, run) {
    const action = run.in_progress ? "Đang xử lý" : actions.get(run.final_state);
    const outcomes = Array.isArray(run.outcomes) ? run.outcomes.filter(group => group && typeof group === "object") : [];
    const matching = outcomes.find(group => group.rule === run.decision_rule);
    const fields = [
      ["Trạng thái", badge(run)], ["Luật", run.rule_display || legacyRuleLabel(run.decision_rule, matching?.rule_text)],
      ["Template", run.template], ["Hành động", action],
      ["Ticket", ticketLink(run.ticket_url, run.ticket_ref)], ["Lượt thử", run.attempt_no]
    ];
    if (run.error_class) fields.push(["Lỗi", run.error_class]);
    if (run.retry_at != null) fields.push(["Thử lại lúc", time(run.retry_at)]);
    if (run.circuit_state) fields.push(["Circuit", run.circuit_state]);
    const definitions = element("dl", "kv");
    for (const [label, value] of fields) {
      const description = element("dd");
      if (value instanceof Node) description.append(value);
      else description.textContent = value ?? "—";
      definitions.append(element("dt", "", label), description);
    }
    panel.replaceChildren(definitions);
    if (outcomes.length) {
      const groups = outcomes.map(({ rule, rule_text, ...group }, index) => rule ? {
        ...group, rule: run.outcome_rule_displays?.[index] || legacyRuleLabel(rule, rule_text)
      } : group);
      panel.append(codeBlock(groups, "Quyết định từng nhóm tin", { json: true, collapsed: true }));
    }
  }

  function renderRound({ run, inputs, steps, round_index }, index, { batch = false, scope = null, chatId = null } = {}) {
    const round = roundTemplate.cloneNode(true);
    const panel = key => round.querySelector("#run-" + key);
    const roundNumber = round_index ?? (index + 1);
    round.querySelector(".round-title").textContent = join("Vòng " + roundNumber, time(run.created_at));
    renderPrecheck(panel("precheck"), run);
    renderInputs(panel("inputs"), inputs, run, { batch, scope, chatId });
    const orderedSteps = [...steps].sort(stepOrder);
    renderSteps(panel("steps"), orderedSteps);
    renderProposal(panel("proposal"), orderedSteps, run);
    renderDecision(panel("decision"), run);
    for (const disclosure of round.querySelectorAll("details")) {
      disclosure.dataset.key = run.run_id + "-" + keyForDisclosure(disclosure);
    }
    if (batch || index) {
      round.removeAttribute("id");
      for (const node of round.querySelectorAll("[id]")) node.removeAttribute("id");
    }
    return round;
  }

  function renderDetail({ run, inputs, steps, rounds, truncated }) {
    const expanded = new Set([...detail.querySelectorAll("details[open]")].map(keyForDisclosure));
    const focused = document.activeElement;
    const focusedKey = focused?.tagName === "SUMMARY" && detail.contains(focused)
      ? keyForDisclosure(focused.parentElement) : null;
    $("run-badge").replaceChildren(badge(run));
    const when = element("li");
    when.append(element("b", "", time(run.created_at)), " · " + chatLabel(run));
    const calls = element("li");
    calls.append(number(run.model_calls) + " lần gọi");
    if (duration(run.latency_ms)) calls.append(" · ", element("b", "", duration(run.latency_ms)));
    $("run-meta").replaceChildren(
      when, element("li", "", number(run.message_count) + " tin, " + number(run.image_count) + " ảnh"),
      ...(run.model || run.provider ? [element("li", "", run.model || run.provider)] : []), calls
    );
    if (run.tokens_in != null || run.tokens_out != null) {
      $("run-meta").append(element("li", "", join(
        number(run.tokens_in) + " → " + number(run.tokens_out) + " token",
        run.reasoning_tokens == null ? "" : "suy luận " + number(run.reasoning_tokens),
        run.cache_hit_tokens ? "cache " + number(run.cache_hit_tokens) : ""
      )));
    }
    detail.replaceChildren($("run-meta"), ...(rounds || [{ run, inputs, steps }]).map((entry, index) => renderRound(entry, index)));
    if (truncated) {
      const notice = document.createElement("p");
      notice.textContent = "Đang hiện 50 vòng gần nhất";
      detail.insertBefore(notice, detail.children[1]);
    }
    setStatus($("detail-status"), "");
    detail.hidden = false;
    for (const disclosure of detail.querySelectorAll("details")) {
      const key = keyForDisclosure(disclosure);
      disclosure.open = expanded.has(key);
      if (key === focusedKey) disclosure.firstChild.focus({ preventScroll: true });
    }
  }

  function keyForDisclosure(disclosure) {
    return disclosure.dataset.key || disclosure.firstChild.textContent;
  }

  function matchesFilters(run) {
    const stateMatches = filters.state === "hold-errors"
      ? run.final_state === "HOLD" || Boolean(run.error_class)
      : !filters.state || run.final_state === filters.state;
    return stateMatches
      && (!filters.model || String(run.model ?? "").toLowerCase().includes(filters.model.toLowerCase()))
      && (!filters.chat || String(run.chat_id) === filters.chat);
  }

  function renderList() {
    const sorted = [...runs.values()].sort(newestFirst);
    for (const [id, row] of rows) {
      if (!runs.has(id)) {
        row.remove();
        rows.delete(id);
      }
    }
    let position = list.firstElementChild;
    for (const run of sorted) {
      const id = run.run_id;
      let item = rows.get(id);
      if (!item) {
        item = element("li");
        const button = element("button", "row");
        button.type = "button";
        button.addEventListener("click", () => showRun(id, true));
        item.append(button);
        rows.set(id, item);
      }
      const button = item.firstElementChild;
      button.setAttribute("aria-current", String(id === selectedId));
      const updated = run.updated_at ?? run.created_at;
      const when = element("time", "", time(updated));
      if (Number.isFinite(updated)) when.dateTime = new Date(updated * 1000).toISOString();
      button.replaceChildren(
        badge(run), element("span", "title", "#" + id + " · " + chatLabel(run)), when,
        element("span", "sub", join(
          run.decided_by, number(run.round_count) + " vòng", number(run.message_count) + " tin",
          run.template, run.model || run.provider,
          run.tool_calls == null ? "" : number(run.tool_calls) + " công cụ",
          duration(run.latency_ms), run.ticket_ref, run.error_class,
          run.fallback_reason ? "fallback: " + run.fallback_reason : ""
        ))
      );
      if (item !== position) list.insertBefore(item, position);
      position = item.nextElementSibling;
    }
  }

  function refreshSelection() {
    for (const [id, item] of rows) {
      item.firstElementChild.setAttribute("aria-current", String(id === selectedId));
    }
  }

  function hashId() {
    const match = /^#run-([1-9]\d*)$/.exec(location.hash);
    const id = match ? Number(match[1]) : null;
    return Number.isSafeInteger(id) ? id : null;
  }

  function updatePhoneView() {
    document.body.classList.toggle("show-detail", hashId() !== null);
  }

  function showRun(id, explicit = false) {
    const wasDetail = hashId() !== null;
    const url = "#run-" + id;
    if (explicit && location.hash !== url) history.pushState(null, "", url);
    else if (wasDetail && location.hash !== url) history.replaceState(null, "", url);
    updatePhoneView();
    selectRun(id);
    if (explicit && phone.matches) {
      window.scrollTo(0, 0);
      $("run-title").focus({ preventScroll: true });
    }
  }

  function backToList() {
    history.replaceState(null, "", "#runs");
    updatePhoneView();
    const row = rows.get(selectedId);
    if (row) row.firstElementChild.focus();
    else filterButtons[0].focus();
  }

  async function request(path, options) {
    const current = session;
    const response = await fetch(path, options);
    if (current !== session) return null;
    if (response.status === 401 && path !== "/api/login") {
      showLogin(active ? "Phiên đăng nhập đã hết hạn. Vui lòng đăng nhập lại." : "");
      return null;
    }
    return response;
  }

  async function selectRun(id, refresh = false) {
    const ticket = ++detailRequest;
    selectedId = id;
    if (!refresh) {
      refreshSelection();
      $("run-title").textContent = id === null ? "Chi tiết lượt" : "Lượt #" + id;
      $("run-badge").replaceChildren();
      detail.hidden = true;
      for (const panel of detail.querySelectorAll(".meta, .c")) panel.replaceChildren();
      $("detail-pane").scrollTop = 0;
      setStatus($("detail-status"), id === null ? "Chọn một lượt để xem chi tiết." : "Đang tải chi tiết…");
    }
    if (id === null) return;
    try {
      const response = await request("/api/runs/" + id);
      if (!response || ticket !== detailRequest) return;
      if (response.status === 404) {
        detail.hidden = true;
        setStatus($("detail-status"), "Không tìm thấy lượt này.");
        return;
      }
      if (!response.ok) throw new Error();
      const payload = await response.json();
      if (ticket === detailRequest) renderDetail(payload);
    } catch {
      if (ticket === detailRequest) setStatus($("detail-status"), "Không tải được chi tiết lượt.");
    }
  }

  async function fetchPage(before) {
    const query = new URLSearchParams({ limit: "50" });
    if (before !== null) query.set("before", before);
    if (filters.model) query.set("model", filters.model);
    if (filters.chat) query.set("chat", filters.chat);
    async function fetchRuns(params) {
      const response = await request("/api/runs?" + params);
      if (!response) return null;
      if (!response.ok) throw new Error();
      return response.json();
    }
    if (filters.state !== "hold-errors") {
      if (filters.state) query.set("state", filters.state);
      return fetchRuns(query);
    }
    const holds = new URLSearchParams(query);
    const errors = new URLSearchParams(query);
    holds.set("state", "HOLD");
    errors.set("errors", "1");
    const pages = await Promise.all([fetchRuns(holds), fetchRuns(errors)]);
    if (pages.some(page => page === null)) return null;
    // Both pages start at the same cursor. The newest 50 unique rows form the union page.
    const combined = new Map();
    for (const page of pages) for (const run of page.runs) combined.set(run.run_id, run);
    const sorted = [...combined.values()].sort(newestFirst);
    const union = sorted.slice(0, 50);
    const hasMore = sorted.length > union.length || pages.some(page => page.next_before != null);
    return {
      runs: union,
      next_before: hasMore ? latestRound(union.at(-1)) : null
    };
  }

  const latestRound = run => run?.latest_run_id ?? run?.run_id ?? null;
  const newestFirst = (a, b) => latestRound(b) - latestRound(a);

  function latestId() {
    return [...runs.values()].sort(newestFirst)[0]?.run_id ?? null;
  }

  async function loadPage({ append = false, reconcile = false } = {}) {
    const before = append ? nextBefore : null;
    const job = { changes: new Map() };
    pageRequest = job;
    if (!reconcile) setStatus($("list-status"), "Đang tải danh sách…");
    $("load-more").disabled = true;
    try {
      const page = await fetchPage(before);
      if (!page || pageRequest !== job) return;
      if (reconcile) {
        const freshIds = new Set(page.runs.map(run => run.run_id));
        const floor = page.next_before ?? 0;
        for (const [id, run] of runs) {
          if (latestRound(run) >= floor && !freshIds.has(id) && !job.changes.has(id)) runs.delete(id);
        }
      }
      for (const run of page.runs) {
        const updated = job.changes.get(run.run_id);
        if (!job.changes.has(run.run_id)) runs.set(run.run_id, run);
        else if (updated && matchesFilters(updated)) runs.set(run.run_id, updated);
      }
      if (!reconcile || nextBefore === null) nextBefore = page.next_before;
      renderList();
      setStatus($("list-status"), runs.size ? "" : "Không có lượt phù hợp.");
      const newest = latestId();
      if (hashId() === null && (selectedId === null || (!append && !reconcile && !runs.has(selectedId)))) {
        selectRun(newest);
      } else if (reconcile && hashId() === null && follow && newest !== null && newest !== selectedId) {
        showRun(newest);
      } else if (reconcile && selectedId !== null) {
        selectRun(selectedId, true);
      }
    } catch {
      if (pageRequest === job) setStatus($("list-status"), "Không tải được danh sách. Vui lòng thử lại.");
    } finally {
      if (pageRequest === job) {
        pageRequest = null;
        $("load-more").disabled = false;
        $("load-more").hidden = nextBefore === null;
        reconcileSnapshot();
      }
    }
  }

  function changeFilters() {
    runs.clear();
    nextBefore = null;
    $("load-more").hidden = true;
    renderList();
    loadPage();
  }

  function renderLive() {
    const status = $("live-status");
    const batchView = view === "batches";
    const online = batchView ? batchConnected : connected;
    const updated = batchView ? batchLastUpdate : lastUpdate;
    status.dataset.status = paused ? "paused" : online ? "live" : "offline";
    setText($("connection-label"), paused
      ? batchView ? "Tạm dừng" : join("Tạm dừng (" + number(pendingCount) + ")", connected ? "" : "Mất kết nối")
      : online ? "Realtime" : batchView && batchUnavailable ? "Mất kết nối · Đang thử lại"
      : !batchView && everConnected ? "Đang kết nối lại" : "Đang kết nối");
    $("last-update").hidden = updated === null;
    formatTime($("update-time"), updated);
    const button = $("pause-button");
    button.setAttribute("aria-pressed", String(paused));
    button.setAttribute("aria-label", paused ? "Tiếp tục cập nhật realtime" : "Tạm dừng cập nhật realtime");
    button.title = paused ? "Tiếp tục" : "Tạm dừng";
    $("pause-icon").toggleAttribute("hidden", paused);
    $("resume-icon").toggleAttribute("hidden", !paused);
  }

  function applyChanges(changes) {
    let createdId = null;
    let selectedChanged = false;
    for (const { run, created } of changes) {
      const id = run.run_id;
      if (run.replaced_thread_id != null) {
        runs.delete(run.replaced_thread_id);
        pageRequest?.changes.set(run.replaced_thread_id, null);
        if (selectedId === run.replaced_thread_id) showRun(id);
      }
      if (matchesFilters(run)) {
        if (created || nextBefore === null || latestRound(run) >= nextBefore || runs.has(id)) runs.set(id, run);
        if (created && (createdId === null || newestFirst(run, runs.get(createdId)) < 0)) createdId = id;
      } else {
        runs.delete(id);
      }
      pageRequest?.changes.set(id, run);
      if (id === selectedId) selectedChanged = true;
    }
    renderList();
    if (!pageRequest) setStatus($("list-status"), runs.size ? "" : "Không có lượt phù hợp.");
    if (follow && createdId !== null && latestId() === createdId && selectedId !== createdId) showRun(createdId);
    else if (selectedChanged) selectRun(selectedId, true);
    lastUpdate = Date.now() / 1000;
  }

  function handleEvent(event) {
    let run;
    try {
      run = JSON.parse(event.data);
    } catch {
      return;
    }
    if (!Number.isSafeInteger(run?.run_id)) return;
    const created = event.type === "run.created";
    if (paused) {
      const earlier = pending.get(run.run_id);
      pending.set(run.run_id, { run, created: created || Boolean(earlier?.created) });
      pendingCount++;
    } else {
      applyChanges([{ run, created }]);
    }
    renderLive();
  }

  function reconcileSnapshot() {
    if (needsSnapshot && !pageRequest && !paused) {
      needsSnapshot = false;
      loadPage({ reconcile: true });
    }
  }

  function connect() {
    const source = new EventSource("/api/stream");
    stream = source;
    source.addEventListener("hello", event => {
      if (source !== stream) return;
      connected = true;
      everConnected = true;
      if (!paused && lastUpdate === null) {
        try {
          lastUpdate = JSON.parse(event.data).server_time ?? null;
        } catch {
          lastUpdate = null;
        }
      }
      // Refresh the first page after opening or reconnecting the stream.
      needsSnapshot = true;
      reconcileSnapshot();
      renderLive();
    });
    for (const type of ["run.created", "run.updated"]) {
      source.addEventListener(type, event => {
        if (source === stream) handleEvent(event);
      });
    }
    source.onerror = () => {
      if (source !== stream) return;
      connected = false;
      renderLive();
      // EventSource exposes no HTTP status. Probe the session before retrying a closed stream.
      if (!sessionCheck) {
        const probe = request("/api/me").catch(() => null).finally(() => {
          if (sessionCheck === probe) sessionCheck = null;
        });
        sessionCheck = probe;
      }
      if (source.readyState === EventSource.CLOSED) {
        clearTimeout(streamTimer);
        streamTimer = setTimeout(() => {
          if (source === stream && active) connect();
        }, 3000);
      }
    };
  }

  function stopRunUpdates() {
    stream?.close();
    stream = null;
    clearTimeout(streamTimer);
    pageRequest = null;
    detailRequest++;
    connected = false;
    everConnected = false;
    lastUpdate = null;
    needsSnapshot = false;
    pending.clear();
    pendingCount = 0;
    if (dialog.open) dialog.close();
  }

  function syncView() {
    const id = hashId();
    const nextView = id !== null || location.hash === "#runs" ? "runs" : "batches";
    if (nextView === view) {
      if (view === "runs") {
        updatePhoneView();
        selectRun(id ?? latestId());
      }
      return;
    }
    view = nextView;
    const batchView = view === "batches";
    dashboard.hidden = batchView;
    batchDashboard.hidden = !batchView;
    $("filters").hidden = batchView;
    $("batch-view-link").setAttribute("aria-current", String(batchView));
    $("run-view-link").setAttribute("aria-current", String(!batchView));
    document.body.classList.toggle("show-batch-list", batchListOnPhone);
    if (batchView) {
      stopRunUpdates();
      batchConnected = false;
      batchUnavailable = false;
      batchLastUpdate = null;
      refreshBatches();
    } else {
      stopBatchUpdates();
      updatePhoneView();
      selectRun(id);
      if (runs.size) loadPage({ reconcile: true });
      else loadPage();
      connect();
    }
    renderLive();
  }

  function showLogin(message) {
    session++;
    active = false;
    view = null;
    stopBatchUpdates();
    stopRunUpdates();
    sessionCheck = null;
    dashboard.hidden = true;
    batchDashboard.hidden = true;
    $("live-status").hidden = true;
    $("pause-button").hidden = true;
    $("view-nav").hidden = true;
    $("filters").hidden = true;
    $("startup-status").hidden = true;
    if (dialog.open) dialog.close();
    runs.clear();
    batches.clear();
    batchRoundCache.clear();
    batchId = null;
    batchData = null;
    batchDetailUpdated = 0;
    messageId = null;
    batchListOnPhone = false;
    for (const id of ["batches-list", "batch-messages", "batch-scope", "batch-receipts", "batch-rounds"]) {
      $(id).replaceChildren();
    }
    $("batch-content").hidden = true;
    $("batch-empty").hidden = true;
    setStatus($("batches-status"), "Đang tải danh sách batch…");
    setStatus($("batch-detail-status"), "");
    renderList();
    selectRun(null);
    $("login-view").hidden = false;
    showLoginError(message);
    $("password").focus();
  }

  function showLoginError(message, invalid = false) {
    setStatus($("login-error"), message);
    $("password").setAttribute("aria-invalid", String(invalid));
    if (invalid) $("password").focus();
  }

  function showDashboard() {
    active = true;
    session++;
    connected = false;
    everConnected = false;
    paused = false;
    pendingCount = 0;
    lastUpdate = null;
    nextBefore = null;
    needsSnapshot = false;
    $("load-more").hidden = true;
    $("login-view").hidden = true;
    $("startup-status").hidden = true;
    $("live-status").hidden = false;
    $("pause-button").hidden = false;
    $("view-nav").hidden = false;
    syncView();
  }

  async function checkLogin() {
    const response = await request("/api/me");
    if (!response) return;
    if (!response.ok) throw new Error();
    showDashboard();
  }

  $("login-form").addEventListener("submit", async event => {
    event.preventDefault();
    const password = $("password");
    const submit = $("login-submit");
    const body = JSON.stringify({ password: password.value });
    password.value = "";
    submit.disabled = true;
    showLoginError("");
    try {
      const response = await request("/api/login", {
        method: "POST", headers: { "Content-Type": "application/json" }, body
      });
      if (!response) return;
      if (response.status === 401) showLoginError("Mật khẩu không đúng.", true);
      else if (response.status === 429) {
        const { retry_after: seconds } = await response.json();
        const retryAt = time(Date.now() / 1000 + seconds);
        showLoginError("Đăng nhập tạm khóa. Thử lại lúc " + retryAt + " (sau " + number(seconds) + " giây).", true);
      } else if (response.status === 204) await checkLogin();
      else throw new Error();
    } catch {
      showLoginError("Không thể đăng nhập. Vui lòng thử lại.");
    } finally {
      submit.disabled = false;
    }
  });

  for (const button of filterButtons) {
    button.addEventListener("click", () => {
      filters.state = button.dataset.filter;
      for (const item of filterButtons) item.setAttribute("aria-pressed", String(item === button));
      changeFilters();
    });
  }

  function openFilter(key, label) {
    filterKey = key;
    $("filter-label").textContent = label;
    $("filter-value").value = filters[key];
    $("filter-value").inputMode = key === "chat" ? "text" : "search";
    dialog.showModal();
    $("filter-value").select();
  }

  function setFilter(value) {
    filters[filterKey] = value;
    const button = $(filterKey + "-filter");
    const label = filterKey === "model" ? "Model" : "Chat";
    button.textContent = (value ? label + ": " + value : label) + " ▾";
    button.setAttribute("aria-pressed", String(Boolean(value)));
    dialog.close();
    changeFilters();
  }

  $("model-filter").addEventListener("click", () => openFilter("model", "Tên model chứa"));
  $("chat-filter").addEventListener("click", () => openFilter("chat", "Mã chat"));
  $("filter-form").addEventListener("submit", event => {
    event.preventDefault();
    setFilter($("filter-value").value.trim());
  });
  $("clear-filter").addEventListener("click", () => setFilter(""));
  $("cancel-filter").addEventListener("click", () => dialog.close());
  $("load-more").addEventListener("click", () => {
    if (!pageRequest && nextBefore !== null) loadPage({ append: true });
  });

  $("follow-button").addEventListener("click", () => {
    follow = !follow;
    $("follow-button").setAttribute("aria-pressed", String(follow));
    const newest = latestId();
    if (follow && !paused && newest !== null) showRun(newest);
  });

  $("pause-button").addEventListener("click", () => {
    paused = !paused;
    if (view === "batches") {
      if (paused) stopBatchUpdates();
      else refreshBatches();
      renderLive();
      return;
    }
    if (!paused && pendingCount) {
      applyChanges(pending.values());
      pending.clear();
      pendingCount = 0;
    }
    reconcileSnapshot();
    renderLive();
  });

  $("back-button").addEventListener("click", backToList);
  $("choose-batch").addEventListener("click", () => {
    batchListOnPhone = true;
    document.body.classList.add("show-batch-list");
    $("batches-title").focus({ preventScroll: true });
    window.scrollTo(0, 0);
  });
  $("batch-trace-jump").addEventListener("click", () => setBatchScope(null, true));
  $("batch-scope").addEventListener("change", event => setBatchScope(event.target.value || null));
  document.addEventListener("visibilitychange", () => {
    if (view !== "batches") return;
    if (document.hidden) stopBatchUpdates();
    else refreshBatches();
  });
  window.addEventListener("hashchange", () => {
    if (active) syncView();
  });

  checkLogin().catch(() => showLogin("Không kiểm tra được phiên. Vui lòng thử đăng nhập."));
})();
