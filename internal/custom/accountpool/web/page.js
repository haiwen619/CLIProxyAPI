(function () {
  const API_BASE = "/v0/management/account-pool";
  const SECURE_PREFIX = "enc::v1::";
  const SECURE_STORAGE_KEY = "cli-proxy-api-webui::secure-storage";

  const state = {
    accounts: [],
    filePath: "",
    configPath: "",
    filter: "all",
    search: "",
    managementKey: readManagementKey(),
    runningId: "",
  };

  const els = {
    authBanner: document.getElementById("authBanner"),
    authForm: document.getElementById("authForm"),
    managementKeyInput: document.getElementById("managementKeyInput"),
    refreshBtn: document.getElementById("refreshBtn"),
    addBtn: document.getElementById("addBtn"),
    batchBtn: document.getElementById("batchBtn"),
    importConfigBtn: document.getElementById("importConfigBtn"),
    exportConfigBtn: document.getElementById("exportConfigBtn"),
    searchInput: document.getElementById("searchInput"),
    statusFilter: document.getElementById("statusFilter"),
    poolPath: document.getElementById("poolPath"),
    configPath: document.getElementById("configPath"),
    poolPathSidebar: document.getElementById("poolPathSidebar"),
    tableBody: document.getElementById("accountTableBody"),
    statTotal: document.getElementById("statTotal"),
    statEnabled: document.getElementById("statEnabled"),
    statTotp: document.getElementById("statTotp"),
    statMissingPassword: document.getElementById("statMissingPassword"),
    accountModal: document.getElementById("accountModal"),
    accountModalTitle: document.getElementById("accountModalTitle"),
    closeAccountModal: document.getElementById("closeAccountModal"),
    cancelAccountModal: document.getElementById("cancelAccountModal"),
    accountForm: document.getElementById("accountForm"),
    accountId: document.getElementById("accountId"),
    accountEmail: document.getElementById("accountEmail"),
    accountPassword: document.getElementById("accountPassword"),
    accountTotp: document.getElementById("accountTotp"),
    accountTags: document.getElementById("accountTags"),
    accountNotes: document.getElementById("accountNotes"),
    accountEnabled: document.getElementById("accountEnabled"),
    batchModal: document.getElementById("batchModal"),
    closeBatchModal: document.getElementById("closeBatchModal"),
    cancelBatchModal: document.getElementById("cancelBatchModal"),
    submitBatchBtn: document.getElementById("submitBatchBtn"),
    batchInput: document.getElementById("batchInput"),
    runModal: document.getElementById("runModal"),
    closeRunModal: document.getElementById("closeRunModal"),
    closeRunModalFooter: document.getElementById("closeRunModalFooter"),
    runSummary: document.getElementById("runSummary"),
    runAuthFiles: document.getElementById("runAuthFiles"),
    runOutput: document.getElementById("runOutput"),
    toast: document.getElementById("toast"),
  };

  function readManagementKey() {
    try {
      const raw = localStorage.getItem("managementKey");
      if (!raw) {
        return "";
      }
      return JSON.parse(decryptMaybe(raw));
    } catch (_) {
      return "";
    }
  }

  function writeManagementKey(value) {
    try {
      localStorage.setItem("managementKey", encrypt(JSON.stringify(value || "")));
      localStorage.setItem("isLoggedIn", "true");
    } catch (_) {
      localStorage.setItem("managementKey", JSON.stringify(value || ""));
    }
  }

  function encrypt(input) {
    const key = makeStorageKey();
    const bytes = xorBytes(new TextEncoder().encode(input), key);
    let out = "";
    bytes.forEach((item) => {
      out += String.fromCharCode(item);
    });
    return SECURE_PREFIX + btoa(out);
  }

  function decryptMaybe(input) {
    if (!input || !input.startsWith(SECURE_PREFIX)) {
      return input;
    }
    const encoded = input.slice(SECURE_PREFIX.length);
    const binary = atob(encoded);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) {
      bytes[i] = binary.charCodeAt(i);
    }
    return new TextDecoder().decode(xorBytes(bytes, makeStorageKey()));
  }

  function makeStorageKey() {
    return new TextEncoder().encode(`${SECURE_STORAGE_KEY}|${window.location.host}|${navigator.userAgent}`);
  }

  function xorBytes(bytes, key) {
    const out = new Uint8Array(bytes.length);
    for (let i = 0; i < bytes.length; i += 1) {
      out[i] = bytes[i] ^ key[i % key.length];
    }
    return out;
  }

  async function request(path, options) {
    const config = options || {};
    const headers = Object.assign({ "Content-Type": "application/json" }, config.headers || {});
    if (state.managementKey) {
      headers.Authorization = `Bearer ${state.managementKey}`;
    }

    const response = await fetch(`${API_BASE}${path}`, Object.assign({}, config, { headers }));
    const text = await response.text();
    let payload = {};
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch (_) {
        payload = { raw: text };
      }
    }

    if (!response.ok) {
      if (response.status === 401 || response.status === 403) {
        showAuth();
      }
      throw new Error(payload.error || `请求失败: ${response.status}`);
    }
    return payload;
  }

  function showAuth() {
    els.authBanner.classList.remove("hidden");
  }

  function hideAuth() {
    els.authBanner.classList.add("hidden");
  }

  function showToast(message, isError) {
    els.toast.textContent = message;
    els.toast.classList.remove("hidden");
    els.toast.style.background = isError ? "rgba(179, 56, 35, 0.95)" : "rgba(31, 36, 48, 0.92)";
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => els.toast.classList.add("hidden"), 2600);
  }

  function render() {
    const filtered = state.accounts.filter((item) => {
      const text = `${item.email} ${(item.tags || []).join(" ")} ${item.notes || ""}`.toLowerCase();
      if (state.search && !text.includes(state.search)) {
        return false;
      }
      switch (state.filter) {
        case "enabled":
          return item.enabled;
        case "disabled":
          return !item.enabled;
        case "totp":
          return Boolean(item.totp_secret);
        default:
          return true;
      }
    });

    els.tableBody.innerHTML = "";
    if (filtered.length === 0) {
      els.tableBody.innerHTML = '<tr><td colspan="7" class="empty-row">暂无匹配账号</td></tr>';
      return;
    }

    filtered.forEach((item) => {
      const row = document.createElement("tr");
      const tags = (item.tags || []).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("");
      const updatedAt = item.updated_at ? new Date(item.updated_at).toLocaleString("zh-CN") : "-";
      const isRunning = state.runningId === item.id;
      row.innerHTML = `
        <td>
          <div class="account-main">
            <span class="account-email">${escapeHtml(item.email)}</span>
            <span class="account-notes">${escapeHtml(item.notes || "无备注")}</span>
          </div>
        </td>
        <td><div class="tag-list">${tags || '<span class="account-notes">未设置</span>'}</div></td>
        <td>${item.password ? "已设置" : '<span class="account-notes">未设置</span>'}</td>
        <td>${item.totp_secret ? "已配置" : '<span class="account-notes">无</span>'}</td>
        <td><span class="status ${item.enabled ? "enabled" : "disabled"}">${item.enabled ? "启用" : "停用"}</span></td>
        <td>${escapeHtml(updatedAt)}</td>
        <td>
          <div class="row-actions">
            <button class="ghost mini" data-action="edit" data-id="${item.id}" type="button">编辑</button>
            <button class="ghost mini" data-action="login" data-id="${item.id}" type="button" ${isRunning ? "disabled" : ""}>${isRunning ? "执行中..." : "认证登录"}</button>
            <button class="ghost mini" data-action="toggle" data-id="${item.id}" type="button">${item.enabled ? "停用" : "启用"}</button>
            <button class="ghost mini danger" data-action="delete" data-id="${item.id}" type="button">删除</button>
          </div>
        </td>
      `;
      els.tableBody.appendChild(row);
    });
  }

  function renderSummary(summary) {
    els.statTotal.textContent = String(summary.total || 0);
    els.statEnabled.textContent = String(summary.enabled || 0);
    els.statTotp.textContent = String(summary.with_totp || 0);
    els.statMissingPassword.textContent = String(summary.missing_password || 0);
  }

  async function loadState() {
    try {
      const payload = await request("", { method: "GET" });
      state.accounts = payload.accounts || [];
      state.filePath = payload.file_path || "-";
      state.configPath = payload.config_path || "-";
      els.poolPath.textContent = state.filePath;
      els.configPath.textContent = state.configPath;
      els.poolPathSidebar.textContent = state.filePath;
      renderSummary(payload.summary || {});
      render();
      hideAuth();
    } catch (error) {
      render();
      showToast(error.message || "加载失败", true);
    }
  }

  function openAccountModal(account) {
    const item = account || {};
    els.accountModalTitle.textContent = item.id ? "编辑账号" : "添加账号";
    els.accountId.value = item.id || "";
    els.accountEmail.value = item.email || "";
    els.accountPassword.value = item.password || "";
    els.accountTotp.value = item.totp_secret || "";
    els.accountTags.value = (item.tags || []).join(", ");
    els.accountNotes.value = item.notes || "";
    els.accountEnabled.checked = item.id ? Boolean(item.enabled) : true;
    els.accountModal.classList.remove("hidden");
  }

  function closeAccountModal() {
    els.accountModal.classList.add("hidden");
  }

  function openBatchModal() {
    els.batchInput.value = "";
    els.batchModal.classList.remove("hidden");
  }

  function closeBatchModal() {
    els.batchModal.classList.add("hidden");
  }

  function openRunModal(result) {
    const authFiles = result.auth_files || [];
    const summaryLines = [
      result.message || (result.success ? "执行成功" : "执行失败"),
      result.python_executable ? `Python: ${result.python_executable}` : "",
    ].filter(Boolean);
    els.runSummary.textContent = summaryLines.join("\n");
    els.runAuthFiles.innerHTML = authFiles.length
      ? authFiles.map((item) => `
          <div class="run-auth-item">
            <strong>${escapeHtml(item.name || "未知文件")}</strong>
            <div class="run-auth-meta">${escapeHtml(item.email || "-")} | ${escapeHtml(item.type || "codex")} | ${escapeHtml(item.modtime || "-")}</div>
            <div class="run-auth-meta">${escapeHtml(item.path || "-")}</div>
          </div>
        `).join("")
      : '<div class="run-auth-item">未检测到新增或更新的 Codex 认证文件</div>';
    els.runOutput.textContent = result.output || "暂无输出";
    els.runModal.classList.remove("hidden");
  }

  function closeRunModal() {
    els.runModal.classList.add("hidden");
  }

  function resetRunModal(account) {
    els.runSummary.textContent = `准备执行 ${account.email} 的认证登录...`;
    els.runAuthFiles.innerHTML = '<div class="run-auth-item">执行中，等待结果...</div>';
    els.runOutput.textContent = "";
    els.runModal.classList.remove("hidden");
  }

  function appendRunOutput(chunk) {
    if (!chunk) {
      return;
    }
    els.runOutput.textContent += chunk;
    els.runOutput.scrollTop = els.runOutput.scrollHeight;
  }

  function updateRunSummary(message, pythonExecutable) {
    const summaryLines = [message || "", pythonExecutable ? `Python: ${pythonExecutable}` : ""].filter(Boolean);
    if (summaryLines.length > 0) {
      els.runSummary.textContent = summaryLines.join("\n");
    }
  }

  async function runCodexLoginStreaming(account) {
    resetRunModal(account);

    const response = await fetch(`${API_BASE}/accounts/${encodeURIComponent(account.id)}/run-codex-login-stream`, {
      method: "POST",
      headers: Object.assign(
        { "Content-Type": "application/json" },
        state.managementKey ? { Authorization: `Bearer ${state.managementKey}` } : {}
      ),
    });

    if (!response.ok) {
      const text = await response.text();
      let payload = {};
      try {
        payload = text ? JSON.parse(text) : {};
      } catch (_) {
        payload = { error: text };
      }
      throw new Error(payload.error || `请求失败: ${response.status}`);
    }

    if (!response.body) {
      throw new Error("浏览器不支持流式响应");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finalResult = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      for (const rawLine of lines) {
        const line = rawLine.trim();
        if (!line) {
          continue;
        }
        let event;
        try {
          event = JSON.parse(line);
        } catch (_) {
          appendRunOutput(`${rawLine}\n`);
          continue;
        }

        if (event.type === "meta") {
          updateRunSummary(event.message, event.python_executable);
        } else if (event.type === "output") {
          appendRunOutput(event.chunk || "");
        } else if (event.type === "result") {
          finalResult = event.result || null;
        }
      }
    }

    if (buffer.trim()) {
      try {
        const event = JSON.parse(buffer.trim());
        if (event.type === "result") {
          finalResult = event.result || null;
        }
      } catch (_) {
        appendRunOutput(buffer);
      }
    }

    if (!finalResult) {
      throw new Error("流式执行结束，但未收到结果事件");
    }
    openRunModal(finalResult);
    return finalResult;
  }

  async function saveAccount(event) {
    event.preventDefault();
    const id = els.accountId.value.trim();
    const payload = {
      email: els.accountEmail.value.trim(),
      password: els.accountPassword.value.trim(),
      totp_secret: els.accountTotp.value.trim(),
      enabled: els.accountEnabled.checked,
      tags: els.accountTags.value.split(",").map((item) => item.trim()).filter(Boolean),
      notes: els.accountNotes.value.trim(),
    };

    try {
      if (id) {
        await request(`/accounts/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(payload) });
        showToast("账号已更新");
      } else {
        await request("/accounts", { method: "POST", body: JSON.stringify(payload) });
        showToast("账号已添加");
      }
      closeAccountModal();
      await loadState();
    } catch (error) {
      showToast(error.message || "保存失败", true);
    }
  }

  async function handleRowAction(event) {
    const button = event.target.closest("button[data-action]");
    if (!button) {
      return;
    }

    const id = button.dataset.id;
    const account = state.accounts.find((item) => item.id === id);
    if (!account) {
      return;
    }

    try {
      if (button.dataset.action === "edit") {
        openAccountModal(account);
        return;
      }
      if (button.dataset.action === "login") {
        if (!window.confirm(`确认用账号 ${account.email} 执行 Codex 认证登录吗？`)) {
          return;
        }
        state.runningId = id;
        render();
        showToast(`开始执行 ${account.email} 的认证登录`);
        const result = await runCodexLoginStreaming(account);
        showToast(result.message || "执行完成", !result.success);
        return;
      }
      if (button.dataset.action === "toggle") {
        await request(`/accounts/${encodeURIComponent(id)}/status`, {
          method: "PATCH",
          body: JSON.stringify({ enabled: !account.enabled }),
        });
        showToast(account.enabled ? "账号已停用" : "账号已启用");
      }
      if (button.dataset.action === "delete") {
        if (!window.confirm(`确认删除账号 ${account.email} 吗？`)) {
          return;
        }
        await request(`/accounts/${encodeURIComponent(id)}`, { method: "DELETE" });
        showToast("账号已删除");
      }
      await loadState();
    } catch (error) {
      showToast(error.message || "操作失败", true);
    } finally {
      if (button.dataset.action === "login") {
        state.runningId = "";
        render();
      }
    }
  }

  function parseBatchInput(raw) {
    const text = raw.trim();
    if (!text) {
      throw new Error("请先输入导入内容");
    }
    if (text.startsWith("[")) {
      const payload = JSON.parse(text);
      if (!Array.isArray(payload)) {
        throw new Error("JSON 必须是数组");
      }
      return payload.map((item) => ({
        email: String(item.email || "").trim(),
        password: String(item.password || "").trim(),
        totp_secret: String(item.totp_secret || "").trim(),
        enabled: item.enabled !== false,
        tags: Array.isArray(item.tags) ? item.tags : [],
        notes: String(item.notes || "").trim(),
      }));
    }
    return text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).map((line) => {
      const parts = line.split(",").map((item) => item.trim());
      return {
        email: parts[0] || "",
        password: parts[1] || "",
        totp_secret: parts[2] || "",
        enabled: true,
        tags: [],
        notes: "",
      };
    });
  }

  async function submitBatchImport() {
    try {
      const accounts = parseBatchInput(els.batchInput.value);
      await request("/accounts", { method: "PUT", body: JSON.stringify({ accounts }) });
      closeBatchModal();
      await loadState();
      showToast(`已覆盖导入 ${accounts.length} 个账号`);
    } catch (error) {
      showToast(error.message || "批量导入失败", true);
    }
  }

  async function importFromConfig() {
    try {
      await request("/import-config", { method: "POST" });
      await loadState();
      showToast("已从 config.json 导入账号");
    } catch (error) {
      showToast(error.message || "导入失败", true);
    }
  }

  async function exportToConfig() {
    try {
      const result = await request("/export-config", { method: "POST" });
      await loadState();
      showToast(`已导出 ${result.exported || 0} 个账号到 config.json`);
    } catch (error) {
      showToast(error.message || "导出失败", true);
    }
  }

  function escapeHtml(value) {
    return String(value || "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  els.authForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    state.managementKey = els.managementKeyInput.value.trim();
    writeManagementKey(state.managementKey);
    await loadState();
  });
  els.refreshBtn.addEventListener("click", loadState);
  els.addBtn.addEventListener("click", () => openAccountModal());
  els.batchBtn.addEventListener("click", openBatchModal);
  els.importConfigBtn.addEventListener("click", importFromConfig);
  els.exportConfigBtn.addEventListener("click", exportToConfig);
  els.searchInput.addEventListener("input", (event) => {
    state.search = event.target.value.trim().toLowerCase();
    render();
  });
  els.statusFilter.addEventListener("change", (event) => {
    state.filter = event.target.value;
    render();
  });
  els.closeAccountModal.addEventListener("click", closeAccountModal);
  els.cancelAccountModal.addEventListener("click", closeAccountModal);
  els.accountForm.addEventListener("submit", saveAccount);
  els.tableBody.addEventListener("click", handleRowAction);
  els.closeBatchModal.addEventListener("click", closeBatchModal);
  els.cancelBatchModal.addEventListener("click", closeBatchModal);
  els.submitBatchBtn.addEventListener("click", submitBatchImport);
  els.closeRunModal.addEventListener("click", closeRunModal);
  els.closeRunModalFooter.addEventListener("click", closeRunModal);

  loadState();
})();
