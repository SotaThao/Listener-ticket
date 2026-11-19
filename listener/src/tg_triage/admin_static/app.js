(() => {
  "use strict";

  const $ = id => document.getElementById(id);
  const list = $("runs-list");
  const dashboard = $("dashboard");
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
    node.textContent = message;
    node.hidden = !message;
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

  function renderInputs(panel, inputs, run) {
    const messages = inputs.map(input => {
      const item = element("div", "msg");
      const reference = input.message_id == null ? "" : "Tin #" + input.message_id;
      const sender = join(time(input.received_at), input.sender_name || reference || "Không rõ người gửi");
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
        ? join(step.link, step.model !== step.link ? step.model : "", duration(step.latency_ms),
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
    const lastModel = steps.filter(step => step.kind === "model").pop();
    const context = join(
      lastModel ? "Lần gọi cuối " + lastModel.seq : run.model_calls ? number(run.model_calls) + " lần gọi" : "",
      run.fallback_reason ? "fallback: " + run.fallback_reason : run.proposal != null ? "Không fallback" : "",
      run.finish_reason ? "finish_reason: " + run.finish_reason : ""
    );
    panel.replaceChildren();
    if (context) panel.append(element("div", "sub", context));
    panel.append(run.proposal == null
      ? element("div", "sub", "Chưa có đề xuất.")
      : codeBlock(run.proposal, "Đề xuất", { json: true }));
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
    if (run.precheck_checks.length) {
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

  function renderRound({ run, inputs, steps }, index) {
    const round = roundTemplate.cloneNode(true);
    const panel = key => round.querySelector("#run-" + key);
    round.querySelector(".round-title").textContent = join("Vòng " + (index + 1), time(run.created_at));
    renderPrecheck(panel("precheck"), run);
    renderInputs(panel("inputs"), inputs, run);
    const orderedSteps = [...steps].sort(stepOrder);
    renderSteps(panel("steps"), orderedSteps);
    renderProposal(panel("proposal"), orderedSteps, run);
    renderDecision(panel("decision"), run);
    for (const disclosure of round.querySelectorAll("details")) {
      disclosure.dataset.key = run.run_id + "-" + keyForDisclosure(disclosure);
    }
    if (index) {
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
    detail.replaceChildren($("run-meta"), ...(rounds || [{ run, inputs, steps }]).map(renderRound));
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
    history.replaceState(null, "", location.pathname + location.search);
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
    status.dataset.status = paused ? "paused" : connected ? "live" : "offline";
    $("connection-label").textContent = paused
      ? join("Tạm dừng (" + number(pendingCount) + ")", connected ? "" : "Mất kết nối")
      : connected ? "Realtime" : everConnected ? "Đang kết nối lại" : "Đang kết nối";
    $("last-update").hidden = lastUpdate === null;
    $("update-time").textContent = time(lastUpdate);
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

  function showLogin(message) {
    session++;
    active = false;
    stream?.close();
    stream = null;
    clearTimeout(streamTimer);
    sessionCheck = null;
    pageRequest = null;
    detailRequest++;
    dashboard.hidden = true;
    $("live-status").hidden = true;
    $("pause-button").hidden = true;
    $("filters").hidden = true;
    $("startup-status").hidden = true;
    if (dialog.open) dialog.close();
    runs.clear();
    pending.clear();
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
    $("filters").hidden = false;
    dashboard.hidden = false;
    renderLive();
    updatePhoneView();
    selectRun(hashId());
    loadPage();
    connect();
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
    if (!paused && pendingCount) {
      applyChanges(pending.values());
      pending.clear();
      pendingCount = 0;
    }
    reconcileSnapshot();
    renderLive();
  });

  $("back-button").addEventListener("click", backToList);
  window.addEventListener("hashchange", () => {
    if (!active) return;
    updatePhoneView();
    selectRun(hashId() ?? latestId());
  });

  checkLogin().catch(() => showLogin("Không kiểm tra được phiên. Vui lòng thử đăng nhập."));
})();
