(function () {
  "use strict";

  var MAX_PHOTO_BYTES = 8 * 1024 * 1024;
  var MAX_ENTRY_PHOTOS = 20;
  var app = {
    csrf: "",
    state: null,
    filters: { month: currentMonth() },
    editingEntry: null,
    entryPhotos: [],
    entrySaving: false,
    entryCommandState: {},
    editingExchange: null,
    editingInternalTransfer: null,
    editingCategory: null,
    deletingCategoryIds: new Set(),
    pendingCommand: null,
    toastTimer: null,
    amountFilterTimer: null,
    stateRequestId: 0,
    collapsedEntryDates: new Set()
  };

  function byId(id) {
    return document.getElementById(id);
  }

  function make(tag, className, label) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (label !== undefined && label !== null) {
      node.textContent = String(label);
    }
    return node;
  }

  function clear(node) {
    while (node && node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function setText(id, value) {
    var node = byId(id);
    if (node) {
      node.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
    }
  }

  function localDate() {
    var now = new Date(Date.now() + 8 * 60 * 60 * 1000);
    return now.toISOString().slice(0, 10);
  }

  function currentMonth() {
    return localDate().slice(0, 7);
  }

  function previousMonthEnd() {
    var month = currentMonth().split("-");
    var year = Number(month[0]);
    var monthNumber = Number(month[1]);
    return new Date(Date.UTC(year, monthNumber - 1, 0)).toISOString().slice(0, 10);
  }

  function newRequestId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    return "web-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
  }

  function amountString(value, signed) {
    var text = String(value === undefined || value === null ? "" : value).trim();
    if (!text || !/^[-+]?\d+(?:\.\d{0,2})?$/.test(text)) {
      throw new Error("请输入最多两位小数的金额。");
    }
    var negative = text.charAt(0) === "-";
    var positive = text.charAt(0) === "+";
    var unsigned = negative || positive ? text.slice(1) : text;
    var parts = unsigned.split(".");
    var integerPart = parts[0].replace(/^0+(?=\d)/, "");
    var fractionPart = (parts[1] || "").padEnd(2, "0");
    if (integerPart.length > 22 || (!signed && negative)) {
      throw new Error(signed ? "请输入有效余额。" : "金额必须大于 0。");
    }
    var isZero = /^0+$/.test(integerPart) && /^0+$/.test(fractionPart);
    if ((!signed && isZero) || (negative && isZero)) {
      throw new Error(signed ? "请输入有效余额。" : "金额必须大于 0。");
    }
    return (negative ? "-" : "") + integerPart + "." + fractionPart;
  }

  function displayAmount(amount, currency, showCurrency) {
    var text = amount === undefined || amount === null || amount === "" ? "—" : String(amount);
    return showCurrency === false ? text : (currency ? currency + " " + text : text);
  }

  function groupedAmount(amount) {
    var text = displayAmount(amount, null, false);
    var parts = /^([+-]?)(\d+)(\.\d+)?$/.exec(text);
    return parts ? parts[1] + parts[2].replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (parts[3] || "") : text;
  }

  function renderCardAmount(node, amount, currency) {
    if (!node) {
      return;
    }
    clear(node);
    node.classList.add("refined-money");
    node.appendChild(make("span", "money-unit", currency));
    node.appendChild(document.createTextNode(" "));
    node.appendChild(make("span", "money-number", groupedAmount(amount)));
  }

  function kindLabel(kind) {
    return kind === "income" ? "收入" : "支出";
  }

  function kindClass(kind) {
    return kind === "income" ? "income" : "expense";
  }

  function formatDateTime(value) {
    if (!value) {
      return "—";
    }
    var date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false });
  }

  function formatBytes(value) {
    var bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes < 0) {
      return "—";
    }
    if (bytes < 1024) {
      return bytes + " B";
    }
    if (bytes < 1024 * 1024) {
      return (bytes / 1024).toFixed(1) + " KiB";
    }
    return (bytes / (1024 * 1024)).toFixed(1) + " MiB";
  }

  function showToast(message, tone) {
    var region = byId("toast-region");
    clear(region);
    var toast = make("div", "toast " + (tone || ""), message);
    region.appendChild(toast);
    if (app.toastTimer) {
      window.clearTimeout(app.toastTimer);
    }
    app.toastTimer = window.setTimeout(function () {
      clear(region);
    }, 5000);
  }

  function showError(error) {
    var message = error && error.message ? error.message : "操作失败，请稍后重试。";
    showToast(message, "error");
  }

  function setSync(status, error) {
    var node = byId("sync-status");
    if (!node) {
      return;
    }
    node.textContent = status;
    node.classList.toggle("is-error", Boolean(error));
  }

  function ApiError(message, code, details, status) {
    Error.call(this, message);
    this.name = "ApiError";
    this.message = message;
    this.code = code || "request_failed";
    this.details = details;
    this.status = status || 0;
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;

  async function api(path, options) {
    var settings = options || {};
    var headers = new Headers(settings.headers || {});
    headers.set("Accept", "application/json");
    if (settings.body !== undefined && settings.body !== null && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    var response;
    try {
      response = await fetch(path, Object.assign({}, settings, { headers: headers, credentials: "same-origin" }));
    } catch (error) {
      throw new ApiError("无法连接本机账本服务。", "network_error", null, 0);
    }
    var data;
    try {
      data = await response.json();
    } catch (error) {
      throw new ApiError("本机服务返回了无法读取的响应。", "invalid_response", null, response.status);
    }
    if (!response.ok || data.ok === false) {
      var apiError = data.error || {};
      throw new ApiError(apiError.message || "请求未完成。", apiError.code, apiError.details, response.status);
    }
    return data;
  }

  async function command(action, payload, duplicateRetry, retryState) {
    var commandState = retryState || app;
    var key = action + "\u0000" + JSON.stringify(payload || {});
    if (commandState.confirmedCommand && commandState.confirmedCommand.originalKey === key) {
      payload = commandState.confirmedCommand.payload;
      duplicateRetry = true;
      key = action + "\u0000" + JSON.stringify(payload);
    } else if (!duplicateRetry) {
      commandState.confirmedCommand = null;
    }
    var requestId = commandState.pendingCommand && commandState.pendingCommand.key === key ? commandState.pendingCommand.requestId : newRequestId();
    commandState.pendingCommand = { key: key, requestId: requestId };
    var body = { action: action, payload: payload || {}, request_id: requestId };
    var headers = { "X-CSRF-Token": app.csrf };
    try {
      var data = await api("/api/command", { method: "POST", headers: headers, body: JSON.stringify(body) });
      if (commandState.pendingCommand && commandState.pendingCommand.key === key) {
        commandState.pendingCommand = null;
        commandState.confirmedCommand = null;
      }
      return data.result;
    } catch (error) {
      var duplicate = error && error.status === 409 && error.code && String(error.code).toLowerCase().indexOf("duplicate") >= 0;
      var transient = !error || error.status === 0 || error.status >= 500 || error.code === "network_error" || error.code === "invalid_response";
      if (!transient && commandState.pendingCommand && commandState.pendingCommand.key === key) {
        commandState.pendingCommand = null;
        commandState.confirmedCommand = null;
      }
      if (!duplicate || duplicateRetry) {
        throw error;
      }
      commandState.pendingCommand = null;
      var accepted = await askConfirm("发现相似记录", "账本检测到一笔可能重复的记录。仍然要保存吗？");
      if (!accepted) {
        throw new ApiError("已取消保存。", "cancelled", null, 0);
      }
      var confirmed = Object.assign({}, payload || {}, { confirm_duplicate: true });
      commandState.confirmedCommand = { originalKey: key, payload: confirmed };
      return command(action, confirmed, true, commandState);
    }
  }

  function askConfirm(title, message) {
    var dialog = byId("confirm-dialog");
    if (!dialog || typeof dialog.showModal !== "function") {
      return Promise.resolve(window.confirm(String(message)));
    }
    byId("confirm-title").textContent = title;
    byId("confirm-message").textContent = message;
    dialog.returnValue = "cancel";
    return new Promise(function (resolve) {
      dialog.addEventListener("close", function () {
        resolve(dialog.returnValue === "ok");
      }, { once: true });
      dialog.showModal();
    });
  }

  function selectedCategoryId() {
    var value = byId("entry-category").value;
    if (!value) {
      throw new Error("请选择一个类别。");
    }
    var id = Number(value);
    if (!Number.isInteger(id) || id <= 0) {
      throw new Error("请选择一个有效类别。");
    }
    return id;
  }

  function filterQuery() {
    var params = new URLSearchParams();
    Object.keys(app.filters).forEach(function (key) {
      var value = app.filters[key];
      if (value !== undefined && value !== null && value !== "") {
        params.set(key, String(value));
      }
    });
    return params.toString();
  }

  async function loadState(options) {
    var quiet = options && options.quiet;
    var requestId = ++app.stateRequestId;
    if (!quiet) {
      setSync("正在同步…", false);
    }
    var query = filterQuery();
    var data = await api("/api/state" + (query ? "?" + query : ""));
    if (requestId !== app.stateRequestId) {
      return data.result || {};
    }
    app.state = data.result || {};
    renderState();
    setSync("已同步", false);
    return app.state;
  }

  async function loadBackups() {
    var data = await api("/api/backups");
    renderBackups(data.result || []);
  }

  function renderState() {
    var state = app.state || {};
    var summary = state.summary || {};
    var expense = summary.expense_by_currency || {};
    var income = summary.income_by_currency || {};
    renderCardAmount(byId("summary-expense-cny"), expense.CNY, "CNY");
    renderCardAmount(byId("summary-expense-sgd"), expense.SGD, "SGD");
    renderCardAmount(byId("summary-income-cny"), income.CNY, "CNY");
    renderCardAmount(byId("summary-income-sgd"), income.SGD, "SGD");
    renderTodayRate(state.today_rate || {}, summary.pending_count);
    setText("category-month-label", summary.month ? summary.month + " · 月度统计" : "月度统计");
    setText("summary-expense-note", summary.month ? summary.month + " 月末 23:59 截止" : "月末 23:59 截止");
    renderWallets(state.wallets || []);
    renderCategorySummary(summary.categories || []);
    renderRecent(state.entries || []);
    renderEntries(state.entries || []);
    renderInternalTransfers(state.internal_transfers || []);
    renderSnapshots(state.snapshots || []);
    renderExchanges(state.exchanges || []);
    renderCategories(state.categories || []);
    populateCategorySelects(state.categories || [], state.historical_categories || []);
  }

  function renderTodayRate(rate, pendingCount) {
    var value = byId("today-rate");
    if (rate.status === "resolved" && rate.rate) {
      clear(value);
      value.classList.add("refined-money");
      var context = make("span", "rate-context");
      context.appendChild(make("span", "money-number", "1"));
      context.appendChild(document.createTextNode(" SGD ="));
      value.appendChild(context);
      value.appendChild(document.createTextNode(" "));
      value.appendChild(make("span", "money-number", rate.rate));
      value.appendChild(document.createTextNode(" "));
      value.appendChild(make("span", "money-unit", "CNY"));
      var note = "ECB 报价日 " + (rate.quote_date || "—");
      if (rate.quote_date && rate.as_of_date && rate.quote_date < rate.as_of_date) {
        note += " · 尚未取得今日报价，当前显示已有报价";
      }
      if (Number(pendingCount) > 0) {
        note += " · " + String(pendingCount) + " 笔记录待折算";
      }
      setText("today-rate-note", note);
      return;
    }
    value.classList.remove("refined-money");
    setText("today-rate", "暂未取得");
    setText("today-rate-note", Number(pendingCount) > 0 ? String(pendingCount) + " 笔记录待折算" : "联网后更新 ECB 报价");
  }

  function renderWallets(wallets) {
    ["wallet-cards", "wallet-detail-cards"].forEach(function (targetId) {
      var target = byId(targetId);
      if (!target) {
        return;
      }
      clear(target);
      if (!wallets.length) {
        var empty = make("div", "empty-card", "请在“钱包与余额”中录入首次余额快照。");
        target.appendChild(empty);
        return;
      }
      wallets.forEach(function (wallet) {
        var card = make("article", "wallet-card");
        var top = make("div", "wallet-card-top");
        var title = make("div");
        title.appendChild(make("span", "currency-chip " + String(wallet.currency || "").toLowerCase(), wallet.currency || "钱包"));
        title.appendChild(make("h3", "wallet-name", wallet.currency === "CNY" ? "人民币钱包" : "新加坡元钱包"));
        top.appendChild(title);
        if (wallet.initialized) {
          top.appendChild(make("span", wallet.negative ? "negative-badge" : "ready-badge", wallet.negative ? "余额为负" : "当前余额"));
        } else {
          top.appendChild(make("span", "pending-badge", "待初始化"));
        }
        card.appendChild(top);
        var balance = make("strong", "wallet-balance", "尚未录入");
        if (wallet.initialized) {
          renderCardAmount(balance, wallet.balance, wallet.currency);
        }
        card.appendChild(balance);
        var foot = make("div", "wallet-card-foot");
        var walletNote = "请先在钱包与余额中录入初始余额";
        if (wallet.initialized) {
          walletNote = targetId === "wallet-cards" ? "记账成功后立即更新 · 支出扣款，收入增加" : "初始余额基准：" + wallet.cutoff + " 23:59（非更新时间）";
        }
        foot.appendChild(make("span", "muted-text", walletNote));
        card.appendChild(foot);
        target.appendChild(card);
      });
    });
  }

  function renderCategorySummary(categories) {
    var target = byId("category-summary");
    clear(target);
    if (!categories.length) {
      target.appendChild(make("p", "empty-state", "本月还没有支出类别数据。"));
      return;
    }
    categories.forEach(function (item) {
      var row = make("div", "category-row");
      var head = make("div", "category-row-head");
      head.appendChild(make("span", "category-name", item.name || "未命名类别"));
      var amounts = item.amounts || {};
      var amountText = [];
      if (Number(amounts.CNY) !== 0) {
        amountText.push(displayAmount(amounts.CNY, "CNY"));
      }
      if (Number(amounts.SGD) !== 0) {
        amountText.push(displayAmount(amounts.SGD, "SGD"));
      }
      head.appendChild(make("span", "category-amount", amountText.length ? amountText.join(" · ") : "—"));
      row.appendChild(head);
      if (Number(item.pending_count) > 0) {
        row.appendChild(make("small", "pending-note", String(item.pending_count) + " 笔待汇率"));
      }
      target.appendChild(row);
    });
  }

  function renderRecent(entries) {
    var target = byId("recent-entries");
    clear(target);
    var latest = entries.slice(0, 12);
    if (!latest.length) {
      target.appendChild(make("p", "empty-state", "还没有记录。"));
      return;
    }
    latest.forEach(function (entry) {
      var row = make("div", "recent-item");
      var icon = make("span", "entry-dot " + kindClass(entry.kind), entry.kind === "income" ? "↑" : "↓");
      row.appendChild(icon);
      var detail = make("div", "recent-detail");
      detail.appendChild(make("strong", null, entry.name || "未命名记录"));
      detail.appendChild(make("small", null, [entry.date, entry.category_name || "未分类"].join(" · ")));
      row.appendChild(detail);
      row.appendChild(make("span", "recent-amount " + kindClass(entry.kind), (entry.kind === "income" ? "+" : "−") + displayAmount(entry.amount, entry.currency)));
      target.appendChild(row);
    });
  }

  function photoThumbnail(photoId, altText, previewUrl) {
    var link = make("a", "photo-thumb-link");
    link.href = previewUrl || "/api/photos/" + encodeURIComponent(String(photoId));
    link.target = "_blank";
    link.rel = "noreferrer noopener";
    link.title = "点击查看原图";
    link.setAttribute("aria-label", "查看" + altText + "原图");
    var image = make("img", "photo-thumb");
    image.src = link.href;
    image.alt = altText;
    image.loading = "lazy";
    image.decoding = "async";
    link.appendChild(image);
    return link;
  }

  function entryPhotoIds(entry) {
    return Array.isArray(entry.photo_ids) ? entry.photo_ids : (entry.photo_id ? [entry.photo_id] : []);
  }

  function entryAmountCents(amount) {
    var parts = String(amount).split(".");
    return BigInt(parts[0]) * 100n + BigInt((parts[1] || "").padEnd(2, "0"));
  }

  function formatEntryCents(cents) {
    return String(cents / 100n) + "." + String(cents % 100n).padStart(2, "0");
  }

  function groupEntriesByDate(entries) {
    var groups = new Map();
    entries.forEach(function (entry) {
      if (!groups.has(entry.date)) {
        groups.set(entry.date, {
          date: entry.date,
          entries: [],
          totals: { income: { CNY: 0n, SGD: 0n }, expense: { CNY: 0n, SGD: 0n } }
        });
      }
      var group = groups.get(entry.date);
      group.entries.push(entry);
      // Add original amounts in integer cents. Reference conversions never
      // enter the daily totals, and filtering uses precisely the shown rows.
      group.totals[entry.kind][entry.currency] += entryAmountCents(entry.amount);
    });
    return Array.from(groups.values()).sort(function (a, b) { return b.date.localeCompare(a.date); });
  }

  function entryCurrencySymbol(currency) {
    return currency === "SGD" ? "S$ " : "¥ ";
  }

  function renderEntryRow(entry) {
    var row = make("article", "dated-entry");
    row.setAttribute("role", "listitem");
    row.dataset.entryId = String(entry.id);
    var nameCell = make("div", "entry-description");
    var nameLine = make("div", "entry-name-line");
    nameLine.appendChild(make("strong", "entry-name-text", entry.name || "未命名记录"));
    var photoIds = entryPhotoIds(entry);
    if (photoIds.length) {
      var photos = make("div", "entry-photo-strip");
      photos.setAttribute("aria-label", photoIds.length + " 张照片，可横向滚动");
      photos.tabIndex = 0;
      photoIds.forEach(function (photoId, index) {
        photos.appendChild(photoThumbnail(photoId, (entry.name || "这笔账单") + "的第 " + (index + 1) + " 张照片"));
      });
      nameLine.appendChild(photos);
    }
    nameCell.appendChild(nameLine);
    if (entry.note) nameCell.appendChild(make("small", "entry-note", entry.note));
    row.appendChild(nameCell);
    row.appendChild(make("span", "entry-category", entry.category_name || "未分类"));
    var amountCell = make("div", "entry-amount");
    amountCell.setAttribute("aria-label", kindLabel(entry.kind) + " " + entry.currency + " " + entry.amount);
    amountCell.appendChild(make("strong", "amount-" + kindClass(entry.kind),
      (entry.kind === "income" ? "+" : "−") + entryCurrencySymbol(entry.currency) + entry.amount));
    if (entry.currency === "SGD" && entry.cny_amount) {
      amountCell.appendChild(make("small", "entry-conversion", "折算 ¥ " + entry.cny_amount));
    }
    if (entry.fx_status === "pending") {
      amountCell.appendChild(make("small", "pending-note", "人民币折算待更新"));
    }
    row.appendChild(amountCell);
    var actions = make("div", "entry-actions");
    var edit = make("button", "row-button", "编辑");
    edit.type = "button";
    edit.disabled = app.entrySaving;
    edit.setAttribute("aria-label", "编辑" + entry.name);
    edit.addEventListener("click", function () { beginEntryEdit(entry); });
    var remove = make("button", "row-button danger-text", "删除");
    remove.type = "button";
    remove.disabled = app.entrySaving;
    remove.setAttribute("aria-label", "删除" + entry.name);
    remove.addEventListener("click", function () { deleteEntry(entry); });
    actions.appendChild(edit);
    actions.appendChild(remove);
    row.appendChild(actions);
    return row;
  }

  function renderEntries(entries) {
    var target = byId("entries-groups");
    clear(target);
    byId("entries-empty").hidden = entries.length !== 0;
    byId("entries-columns").hidden = entries.length === 0;
    groupEntriesByDate(entries).forEach(function (group) {
      var section = make("section", "entry-day");
      var headingId = "entry-day-" + group.date;
      var listId = headingId + "-rows";
      section.setAttribute("aria-labelledby", headingId);
      var header = make("header", "entry-day-header");
      var heading = make("h4", "entry-day-heading");
      heading.id = headingId;
      var toggle = make("button", "entry-day-toggle");
      toggle.type = "button";
      toggle.setAttribute("aria-controls", listId);
      toggle.setAttribute("aria-expanded", String(!app.collapsedEntryDates.has(group.date)));
      toggle.appendChild(make("span", "entry-day-date", group.date));
      var weekday = new Date(group.date + "T12:00:00Z").getUTCDay();
      toggle.appendChild(make("span", "entry-day-weekday", ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][weekday]));
      var chevron = make("span", "entry-day-chevron", "▾");
      chevron.setAttribute("aria-hidden", "true");
      toggle.appendChild(chevron);
      heading.appendChild(toggle);
      header.appendChild(heading);
      var totals = make("div", "entry-day-totals");
      totals.setAttribute("aria-label", "当前筛选结果的每日原币收支");
      ["income", "expense"].forEach(function (kind) {
        var total = make("div", "entry-day-total " + kind);
        var label = make("span", "entry-total-label", kind === "income" ? "收" : "支");
        label.setAttribute("aria-label", kindLabel(kind));
        total.appendChild(label);
        var amounts = make("div", "entry-total-amounts");
        ["CNY", "SGD"].forEach(function (currency) {
          var amount = formatEntryCents(group.totals[kind][currency]);
          var value = make("span", null, entryCurrencySymbol(currency) + amount);
          value.setAttribute("aria-label", currency + " " + amount);
          amounts.appendChild(value);
        });
        total.appendChild(amounts);
        totals.appendChild(total);
      });
      header.appendChild(totals);
      section.appendChild(header);
      var list = make("div", "entry-day-rows");
      list.id = listId;
      list.setAttribute("role", "list");
      list.hidden = app.collapsedEntryDates.has(group.date);
      group.entries.forEach(function (entry) { list.appendChild(renderEntryRow(entry)); });
      toggle.addEventListener("click", function () {
        list.hidden = !list.hidden;
        toggle.setAttribute("aria-expanded", String(!list.hidden));
        if (list.hidden) app.collapsedEntryDates.add(group.date);
        else app.collapsedEntryDates.delete(group.date);
      });
      section.appendChild(list);
      target.appendChild(section);
    });
  }

  function renderSnapshots(snapshots) {
    var target = byId("snapshots-table-body");
    clear(target);
    byId("snapshots-empty").hidden = snapshots.length !== 0;
    snapshots.forEach(function (snapshot) {
      var row = make("tr");
      row.appendChild(make("td", null, snapshot.cutoff || snapshot.date || "—"));
      row.appendChild(make("td", null, snapshot.currency || "—"));
      row.appendChild(make("td", "align-right", displayAmount(snapshot.balance, snapshot.currency)));
      row.appendChild(make("td", null, snapshot.note || "—"));
      target.appendChild(row);
    });
  }

  function renderInternalTransfers(transfers) {
    var target = byId("internal-transfers-table-body");
    clear(target);
    byId("internal-transfers-empty").hidden = transfers.length !== 0;
    transfers.forEach(function (transfer) {
      var row = make("tr");
      row.appendChild(make("td", null, transfer.date || "—"));
      row.appendChild(make("td", null, transfer.from_account || "—"));
      row.appendChild(make("td", null, transfer.to_account || "—"));
      row.appendChild(make("td", "align-right", displayAmount(transfer.amount, transfer.currency)));
      row.appendChild(make("td", null, transfer.note || "—"));
      var actions = make("td", "row-actions");
      var edit = make("button", "row-button", "编辑");
      edit.type = "button";
      edit.addEventListener("click", function () { beginInternalTransferEdit(transfer); });
      var remove = make("button", "row-button danger-text", "删除");
      remove.type = "button";
      remove.addEventListener("click", function () { deleteInternalTransfer(transfer); });
      actions.appendChild(edit);
      actions.appendChild(remove);
      row.appendChild(actions);
      target.appendChild(row);
    });
  }

  function renderExchanges(exchanges) {
    var target = byId("exchanges-table-body");
    clear(target);
    byId("exchanges-empty").hidden = exchanges.length !== 0;
    exchanges.forEach(function (exchange) {
      var row = make("tr");
      row.appendChild(make("td", null, exchange.date || "—"));
      row.appendChild(make("td", null, "− " + displayAmount(exchange.from_amount, exchange.from_currency)));
      row.appendChild(make("td", null, "+ " + displayAmount(exchange.to_amount, exchange.to_currency)));
      row.appendChild(make("td", null, exchange.note || "—"));
      var actions = make("td", "row-actions");
      var edit = make("button", "row-button", "编辑");
      edit.type = "button";
      edit.addEventListener("click", function () { beginExchangeEdit(exchange); });
      var remove = make("button", "row-button danger-text", "删除");
      remove.type = "button";
      remove.addEventListener("click", function () { deleteExchange(exchange); });
      actions.appendChild(edit);
      actions.appendChild(remove);
      row.appendChild(actions);
      target.appendChild(row);
    });
  }

  function renderCategories(categories) {
    var target = byId("categories-table-body");
    clear(target);
    categories.filter(function (category) { return !category.deleted; }).forEach(function (category) {
      var row = make("tr");
      row.appendChild(make("td", null, category.name || "—"));
      row.appendChild(make("td", null, category.active ? "启用" : "已停用"));
      var actions = make("td", "row-actions");
      var edit = make("button", "row-button", "编辑");
      edit.type = "button";
      edit.addEventListener("click", function () { beginCategoryEdit(category); });
      var remove = make("button", "row-button danger-text", "删除");
      remove.type = "button";
      remove.id = "delete-category-" + String(category.id);
      remove.disabled = app.deletingCategoryIds.has(String(category.id));
      remove.setAttribute("aria-label", "删除类别“" + (category.name || "未命名类别") + "”");
      remove.addEventListener("click", function () { deleteCategory(category); });
      actions.appendChild(edit);
      actions.appendChild(remove);
      row.appendChild(actions);
      target.appendChild(row);
    });
  }

  function populateCategorySelects(categories, historicalCategories) {
    var entrySelect = byId("entry-category");
    var filterSelect = byId("filter-category");
    var current = entrySelect.value;
    var knownIds = new Set();
    var allCategories = categories.concat(historicalCategories || []).filter(function (category) {
      var id = String(category.id);
      if (knownIds.has(id)) {
        return false;
      }
      knownIds.add(id);
      return true;
    });
    function categoryLabel(category) {
      var status = category.deleted ? "（已删除）" : (category.active ? "" : "（已停用）");
      return status + (category.name || "未命名类别");
    }
    clear(entrySelect);
    var entryPlaceholder = make("option", null, "请选择类别");
    entryPlaceholder.value = "";
    entrySelect.appendChild(entryPlaceholder);
    allCategories.filter(function (category) {
      return (category.active && !category.deleted) || (app.editingEntry && String(category.id) === String(app.editingEntry.category_id));
    }).forEach(function (category) {
      var option = make("option", null, categoryLabel(category));
      option.value = String(category.id);
      if (String(category.id) === String(current)) {
        option.selected = true;
      }
      entrySelect.appendChild(option);
    });
    var filterCurrent = filterSelect.value;
    clear(filterSelect);
    var filterPlaceholder = make("option", null, "全部类别");
    filterPlaceholder.value = "";
    filterSelect.appendChild(filterPlaceholder);
    allCategories.forEach(function (category) {
      var filterOption = make("option", null, categoryLabel(category));
      filterOption.value = String(category.id);
      filterOption.selected = String(category.id) === String(filterCurrent);
      filterSelect.appendChild(filterOption);
    });
  }

  function renderBackups(backups) {
    var target = byId("backups-table-body");
    clear(target);
    byId("backups-empty").hidden = backups.length !== 0;
    backups.forEach(function (backup) {
      var row = make("tr");
      row.appendChild(make("td", "break-word", backup.name || "—"));
      row.appendChild(make("td", null, formatDateTime(backup.created)));
      row.appendChild(make("td", null, ({daily: "每日自动", manual: "手动", "pre-restore": "恢复前保护"})[backup.kind] || "未知"));
      row.appendChild(make("td", "align-right", formatBytes(backup.size)));
      var actions = make("td", "row-actions");
      var restore = make("button", "row-button danger-text", "恢复");
      restore.type = "button";
      restore.addEventListener("click", function () { restoreBackup(backup); });
      actions.appendChild(restore);
      row.appendChild(actions);
      target.appendChild(row);
    });
  }

  function switchView(viewId) {
    var title = "总览";
    document.querySelectorAll(".view").forEach(function (view) {
      var visible = view.id === viewId;
      view.hidden = !visible;
      view.classList.toggle("is-visible", visible);
      if (visible && view.querySelector("h2")) {
        title = view.querySelector("h2").textContent;
      }
    });
    document.querySelectorAll(".nav-item").forEach(function (button) {
      button.classList.toggle("is-active", button.dataset.view === viewId);
    });
    setText("page-title", title);
    if (viewId === "backup-view") {
      loadBackups().catch(showError);
    } else if ((viewId === "dashboard-view" || viewId === "wallets-view") && app.csrf) {
      loadState({ quiet: true }).catch(function () { setSync("同步失败", true); });
    }
  }

  function releaseEntryPhotos() {
    app.entryPhotos.forEach(function (photo) {
      if (photo.previewUrl) URL.revokeObjectURL(photo.previewUrl);
    });
    app.entryPhotos = [];
  }

  function renderEntryPhotos() {
    var current = byId("entry-photo-current");
    clear(current);
    current.hidden = app.entryPhotos.length === 0;
    byId("entry-photo-count").textContent = "已添加 " + app.entryPhotos.length + " / " + MAX_ENTRY_PHOTOS + " 张";
    byId("entry-photo").disabled = app.entrySaving || app.entryPhotos.length >= MAX_ENTRY_PHOTOS;
    app.entryPhotos.forEach(function (photo, index) {
      var item = make("div", "photo-preview-item");
      var name = photo.file ? photo.file.name : "已有照片";
      var link = photoThumbnail(photo.photoId, "第 " + (index + 1) + " 张照片：" + name, photo.previewUrl);
      item.appendChild(link);
      var detail = make("div", "photo-preview-detail");
      var label = make("span", "photo-preview-name", (index + 1) + ". " + name);
      label.title = name;
      detail.appendChild(label);
      detail.appendChild(make("small", null, photo.file ? (photo.photoId ? "已上传，待保存记录" : "待上传") : "已关联"));
      item.appendChild(detail);
      var remove = make("button", "row-button danger-text", "移除");
      remove.type = "button";
      remove.disabled = app.entrySaving;
      remove.setAttribute("aria-label", "移除第 " + (index + 1) + " 张照片：" + name);
      remove.addEventListener("click", function () {
        if (app.entrySaving) return;
        var position = app.entryPhotos.indexOf(photo);
        if (position < 0) return;
        if (photo.previewUrl) URL.revokeObjectURL(photo.previewUrl);
        app.entryPhotos.splice(position, 1);
        renderEntryPhotos();
      });
      item.appendChild(remove);
      current.appendChild(item);
    });
  }

  function addEntryPhotos() {
    var input = byId("entry-photo");
    var files = Array.from(input.files || []);
    input.value = "";
    if (app.entrySaving || !files.length) return;
    var added = [];
    try {
      if (app.entryPhotos.length + files.length > MAX_ENTRY_PHOTOS) {
        throw new Error("每笔最多添加 20 张照片，当前还能添加 " + (MAX_ENTRY_PHOTOS - app.entryPhotos.length) + " 张。本次选择未添加，请重新选择。");
      }
      files.forEach(function (file) {
        if (file.size > MAX_PHOTO_BYTES) throw new Error("“" + file.name + "”超过 8 MiB，本次选择未添加。");
        if (["image/jpeg", "image/png", "image/webp"].indexOf(file.type) < 0) {
          throw new Error("“" + file.name + "”不是 JPEG、PNG 或 WebP，本次选择未添加。");
        }
      });
      files.forEach(function (file) {
        added.push({ file: file, photoId: null, previewUrl: URL.createObjectURL(file), commandState: {} });
      });
      app.entryPhotos = app.entryPhotos.concat(added);
      renderEntryPhotos();
    } catch (error) {
      added.forEach(function (photo) { URL.revokeObjectURL(photo.previewUrl); });
      showError(error);
    }
  }

  function setEntrySaving(saving) {
    app.entrySaving = saving;
    var form = byId("entry-form");
    form.setAttribute("aria-busy", String(saving));
    form.querySelectorAll("input, select, textarea, button").forEach(function (control) { control.disabled = saving; });
    document.querySelectorAll("#cancel-entry, #quick-entry-button, [data-action='new-entry'], .entry-actions button").forEach(function (button) {
      button.disabled = saving;
    });
    form.querySelector("button[type='submit']").textContent = saving ? "正在保存…" : "保存记录";
    renderEntryPhotos();
  }

  function resetEntryForm(force) {
    if (app.entrySaving && force !== true) return;
    app.editingEntry = null;
    app.entryCommandState = {};
    releaseEntryPhotos();
    byId("entry-form").reset();
    byId("entry-id").value = "";
    byId("entry-date").value = localDate();
    byId("entry-kind").value = "expense";
    byId("entry-currency").value = "SGD";
    renderEntryPhotos();
    byId("entry-photo-status").textContent = "";
    byId("entry-form-eyebrow").textContent = "NEW ENTRY";
    byId("entry-form-title").textContent = "添加记录";
    byId("cancel-entry").hidden = true;
    populateCategorySelects((app.state && app.state.categories) || [], (app.state && app.state.historical_categories) || []);
    byId("entry-save-receipt").hidden = true;
  }

  function beginEntryEdit(entry) {
    if (app.entrySaving) return;
    releaseEntryPhotos();
    app.entryCommandState = {};
    byId("entry-save-receipt").hidden = true;
    switchView("records-view");
    app.editingEntry = entry;
    byId("entry-id").value = entry.id || "";
    byId("entry-kind").value = entry.kind || "expense";
    byId("entry-date").value = entry.date || localDate();
    byId("entry-currency").value = entry.currency || "CNY";
    byId("entry-amount").value = entry.amount || "";
    byId("entry-name").value = entry.name || "";
    byId("entry-note").value = entry.note || "";
    populateCategorySelects((app.state && app.state.categories) || [], (app.state && app.state.historical_categories) || []);
    byId("entry-category").value = entry.category_id ? String(entry.category_id) : "";
    byId("entry-photo").value = "";
    app.entryPhotos = entryPhotoIds(entry).map(function (photoId) { return { photoId: photoId }; });
    renderEntryPhotos();
    byId("entry-photo-status").textContent = "";
    byId("entry-form-eyebrow").textContent = "EDIT ENTRY";
    byId("entry-form-title").textContent = "编辑记录";
    byId("cancel-entry").hidden = false;
    byId("entry-name").focus();
  }

  async function saveEntry(event) {
    event.preventDefault();
    if (app.entrySaving) return;
    try {
      var payload = {
        date: byId("entry-date").value,
        kind: byId("entry-kind").value,
        currency: byId("entry-currency").value,
        amount: amountString(byId("entry-amount").value, false),
        name: byId("entry-name").value.trim(),
        category_id: selectedCategoryId(),
        note: byId("entry-note").value.trim()
      };
      if (!payload.name) {
        throw new Error("请填写名称。");
      }
      var id = byId("entry-id").value;
      if (id) {
        payload.id = Number(id);
      }
      setEntrySaving(true);
      for (var index = 0; index < app.entryPhotos.length; index += 1) {
        var photo = app.entryPhotos[index];
        if (photo.photoId) continue;
        byId("entry-photo-status").textContent = "正在上传第 " + (index + 1) + " / " + app.entryPhotos.length + " 张照片…";
        var dataUrl = await readFile(photo.file);
        photo.photoId = await savePhoto(dataUrl, photo.commandState);
        renderEntryPhotos();
      }
      payload.photo_ids = app.entryPhotos.map(function (photo) { return photo.photoId; });
      byId("entry-photo-status").textContent = "正在保存记录…";
      var saved = await command("entry.save", payload, false, app.entryCommandState);
      resetEntryForm(true);
      try {
        var updatedState = await loadState();
        var receipt = entryWalletReceipt(saved.entry, updatedState, Boolean(id));
        setText("entry-save-receipt", receipt);
        byId("entry-save-receipt").hidden = false;
        showToast(receipt, "success");
      } catch (refreshError) {
        setSync("余额显示待刷新", true);
        var savedNotice = "账目已保存成功，请勿重复提交。暂时未能读取新余额，请刷新页面查看。";
        setText("entry-save-receipt", savedNotice);
        byId("entry-save-receipt").hidden = false;
        showToast(savedNotice, "warning");
      }
    } catch (error) {
      if (app.entrySaving) {
        byId("entry-photo-status").textContent = "尚未确认保存。照片选择已保留，可再次点击保存重试；已上传的照片会继续使用。";
      }
      if (error.code !== "cancelled") {
        showError(error);
      }
    } finally {
      if (app.entrySaving) setEntrySaving(false);
    }
  }

  function entryWalletReceipt(entry, state, edited) {
    var prefix = "记录 #" + entry.id + (edited ? " 已更新。" : " 已保存。");
    var wallets = state.wallets || [];
    var wallet = wallets.find(function (item) { return item.currency === entry.currency; });
    if (!wallet || !wallet.initialized) {
      return prefix + "此币种钱包尚未初始化。";
    }
    if (edited) {
      return prefix + "钱包余额已重算：" + wallets.filter(function (item) { return item.initialized; }).map(function (item) {
        return displayAmount(item.balance, item.currency);
      }).join("；") + "。";
    }
    var walletName = entry.currency === "CNY" ? "人民币钱包" : "新加坡元钱包";
    var effect = entry.date <= wallet.cutoff ? "历史账目已包含在余额基准中，不重复增减钱包。" : walletName + (entry.kind === "income" ? "已增加 " : "已扣除 ") + displayAmount(entry.amount, entry.currency) + "。";
    return prefix + effect + "当前余额 " + displayAmount(wallet.balance, wallet.currency) + "。";
  }

  function readFile(file) {
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () { resolve(String(reader.result)); };
      reader.onerror = function () { reject(new Error("读取照片失败。")); };
      reader.onabort = function () { reject(new Error("读取照片已取消。")); };
      reader.readAsDataURL(file);
    });
  }

  async function savePhoto(dataUrl, retryState) {
    var comma = dataUrl.indexOf(",");
    if (comma < 0) {
      throw new Error("照片格式无效。");
    }
    var result = await command("photo.save", { content_base64: dataUrl.slice(comma + 1) }, false, retryState);
    var photoId = result && result.photo_id ? result.photo_id : result;
    if (typeof photoId !== "string" || !photoId) throw new Error("照片上传未返回有效编号，请重试。");
    return photoId;
  }

  async function deleteEntry(entry) {
    if (app.entrySaving) return;
    if (!await askConfirm("删除这条记录？", "删除后记录会从统计中移除，并保留审计可追踪性。")) {
      return;
    }
    try {
      await command("entry.delete", { id: Number(entry.id) });
      showToast("记录已删除。", "success");
      await loadState();
    } catch (error) {
      showError(error);
    }
  }

  function resetExchangeForm() {
    app.editingExchange = null;
    byId("exchange-form").reset();
    byId("exchange-id").value = "";
    byId("exchange-date").value = localDate();
    byId("exchange-from-currency").value = "CNY";
    byId("exchange-to-currency").value = "SGD";
    byId("exchange-form-eyebrow").textContent = "NEW EXCHANGE";
    byId("exchange-form-title").textContent = "添加换汇";
    byId("cancel-exchange").hidden = true;
  }

  function beginExchangeEdit(exchange) {
    switchView("exchange-view");
    app.editingExchange = exchange;
    byId("exchange-id").value = exchange.id || "";
    byId("exchange-date").value = exchange.date || localDate();
    byId("exchange-from-currency").value = exchange.from_currency || "CNY";
    byId("exchange-from-amount").value = exchange.from_amount || "";
    byId("exchange-to-currency").value = exchange.to_currency || "SGD";
    byId("exchange-to-amount").value = exchange.to_amount || "";
    byId("exchange-note").value = exchange.note || "";
    byId("exchange-form-eyebrow").textContent = "EDIT EXCHANGE";
    byId("exchange-form-title").textContent = "编辑换汇";
    byId("cancel-exchange").hidden = false;
  }

  async function saveExchange(event) {
    event.preventDefault();
    try {
      var payload = {
        date: byId("exchange-date").value,
        from_currency: byId("exchange-from-currency").value,
        from_amount: amountString(byId("exchange-from-amount").value, false),
        to_currency: byId("exchange-to-currency").value,
        to_amount: amountString(byId("exchange-to-amount").value, false),
        note: byId("exchange-note").value.trim()
      };
      if (payload.from_currency === payload.to_currency) {
        throw new Error("转出和到账币种必须不同。");
      }
      if (byId("exchange-id").value) {
        payload.id = Number(byId("exchange-id").value);
      }
      await command("exchange.save", payload);
      showToast(byId("exchange-id").value ? "换汇已更新。" : "换汇已保存。", "success");
      resetExchangeForm();
      await loadState();
    } catch (error) {
      if (error.code !== "cancelled") {
        showError(error);
      }
    }
  }

  async function deleteExchange(exchange) {
    if (!await askConfirm("删除这笔换汇？", "对应钱包余额会按账本规则重新计算。")) {
      return;
    }
    try {
      await command("exchange.delete", { id: Number(exchange.id) });
      showToast("换汇已删除。", "success");
      await loadState();
    } catch (error) {
      showError(error);
    }
  }

  function resetInternalTransferForm() {
    app.editingInternalTransfer = null;
    byId("internal-transfer-form").reset();
    byId("internal-transfer-id").value = "";
    byId("internal-transfer-date").value = localDate();
    byId("internal-transfer-currency").value = "SGD";
    byId("internal-transfer-form-eyebrow").textContent = "NEW TRANSFER";
    byId("internal-transfer-form-title").textContent = "添加内部转账";
    byId("cancel-internal-transfer").hidden = true;
  }

  function beginInternalTransferEdit(transfer) {
    switchView("records-view");
    app.editingInternalTransfer = transfer;
    byId("internal-transfer-id").value = transfer.id || "";
    byId("internal-transfer-date").value = transfer.date || localDate();
    byId("internal-transfer-currency").value = transfer.currency || "SGD";
    byId("internal-transfer-amount").value = transfer.amount || "";
    byId("internal-transfer-from").value = transfer.from_account || "";
    byId("internal-transfer-to").value = transfer.to_account || "";
    byId("internal-transfer-note").value = transfer.note || "";
    byId("internal-transfer-form-eyebrow").textContent = "EDIT TRANSFER";
    byId("internal-transfer-form-title").textContent = "编辑内部转账";
    byId("cancel-internal-transfer").hidden = false;
    byId("internal-transfer-from").focus();
  }

  async function saveInternalTransfer(event) {
    event.preventDefault();
    try {
      var payload = {
        date: byId("internal-transfer-date").value,
        currency: byId("internal-transfer-currency").value,
        amount: amountString(byId("internal-transfer-amount").value, false),
        from_account: byId("internal-transfer-from").value.trim(),
        to_account: byId("internal-transfer-to").value.trim(),
        note: byId("internal-transfer-note").value.trim()
      };
      if (!payload.from_account || !payload.to_account) {
        throw new Error("请填写转出账户和转入账户。");
      }
      if (byId("internal-transfer-id").value) {
        payload.id = Number(byId("internal-transfer-id").value);
      }
      await command("internal_transfer.save", payload);
      showToast(byId("internal-transfer-id").value ? "内部转账已更新。" : "内部转账已保存。", "success");
      resetInternalTransferForm();
      await loadState();
    } catch (error) {
      if (error.code !== "cancelled") {
        showError(error);
      }
    }
  }

  async function deleteInternalTransfer(transfer) {
    if (!await askConfirm("删除这笔内部转账？", "该记录不影响钱包余额或收支统计。")) {
      return;
    }
    try {
      await command("internal_transfer.delete", { id: Number(transfer.id) });
      showToast("内部转账已删除。", "success");
      await loadState();
    } catch (error) {
      showError(error);
    }
  }

  function resetCategoryForm() {
    app.editingCategory = null;
    byId("category-form").reset();
    byId("category-id").value = "";
    byId("category-active").checked = true;
    byId("category-form-eyebrow").textContent = "NEW CATEGORY";
    byId("category-form-title").textContent = "添加类别";
    byId("cancel-category").hidden = true;
  }

  function beginCategoryEdit(category) {
    switchView("categories-view");
    app.editingCategory = category;
    byId("category-id").value = category.id || "";
    byId("category-name").value = category.name || "";
    byId("category-active").checked = Boolean(category.active);
    byId("category-form-eyebrow").textContent = "EDIT CATEGORY";
    byId("category-form-title").textContent = "编辑类别";
    byId("cancel-category").hidden = false;
    byId("category-name").focus();
  }

  async function saveCategory(event) {
    event.preventDefault();
    try {
      var name = byId("category-name").value.trim();
      if (!name) {
        throw new Error("请填写类别名称。");
      }
      var payload = { name: name, active: byId("category-active").checked };
      if (byId("category-id").value) {
        payload.id = Number(byId("category-id").value);
      }
      await command("category.save", payload);
      showToast("类别已保存。", "success");
      resetCategoryForm();
      await loadState();
    } catch (error) {
      showError(error);
    }
  }

  async function deleteCategory(category) {
    var categoryId = String(category.id);
    if (app.deletingCategoryIds.has(categoryId)) {
      return;
    }
    app.deletingCategoryIds.add(categoryId);
    var button = byId("delete-category-" + categoryId);
    if (button) {
      button.disabled = true;
    }
    try {
      if (!await askConfirm("删除类别“" + (category.name || "未命名类别") + "”？", "从类别列表移除，历史账目及其原分类保留，不影响金额。")) {
        return;
      }
      await command("category.delete", { id: Number(category.id) });
      if (app.editingCategory && String(app.editingCategory.id) === categoryId) {
        resetCategoryForm();
      }
      showToast("类别已删除，历史账目及其原分类已保留。", "success");
      await loadState();
    } catch (error) {
      showError(error);
    } finally {
      app.deletingCategoryIds.delete(categoryId);
      button = byId("delete-category-" + categoryId);
      if (button) {
        button.disabled = false;
      }
    }
  }

  async function saveSnapshot(event) {
    event.preventDefault();
    try {
      var payload = {
        currency: byId("snapshot-currency").value,
        balance: amountString(byId("snapshot-balance").value, true),
        cutoff: byId("snapshot-cutoff").value,
        note: byId("snapshot-note").value.trim()
      };
      await command("snapshot.save", payload);
      showToast("余额快照已保存。", "success");
      byId("snapshot-note").value = "";
      await loadState();
    } catch (error) {
      showError(error);
    }
  }

  async function refreshFx() {
    try {
      var result = await command("fx.refresh", {});
      var updated = result && result.updated !== undefined ? result.updated : 0;
      var pending = result && result.pending !== undefined ? result.pending : 0;
      showToast("汇率更新完成：" + updated + " 笔已补齐，" + pending + " 笔仍待处理。", "success");
      await loadState();
    } catch (error) {
      showError(error);
    }
  }

  async function createBackup() {
    try {
      var result = await command("backup.create", {});
      showToast("备份已创建：" + (result && result.name ? result.name : "完成"), "success");
      await loadBackups();
    } catch (error) {
      showError(error);
    }
  }

  async function restoreBackup(backup) {
    if (!await askConfirm("恢复此备份？", "恢复会替换当前账本；服务会先自动备份当前状态。")) {
      return;
    }
    try {
      await command("backup.restore", { name: backup.name, confirm: true });
      showToast("备份已恢复，正在重新读取数据。", "success");
      await loadState();
      await loadBackups();
    } catch (error) {
      showError(error);
    }
  }

  async function exportCsv() {
    try {
      var result = await command("export.csv", app.filters);
      var name = result && result.name ? result.name : result;
      if (!name || !/^[A-Za-z0-9][A-Za-z0-9_.-]*\.csv$/.test(String(name))) {
        throw new Error("导出文件名无效。");
      }
      window.open("/api/exports/" + encodeURIComponent(String(name)), "_blank", "noopener");
      showToast("CSV 已生成。", "success");
    } catch (error) {
      showError(error);
    }
  }

  function applyFilters(event) {
    if (event) {
      event.preventDefault();
    }
    window.clearTimeout(app.amountFilterTimer);
    app.filters = {
      month: byId("dashboard-month").value || currentMonth(),
      date_from: byId("filter-date-from").value,
      date_to: byId("filter-date-to").value,
      category_id: byId("filter-category").value,
      currency: byId("filter-currency").value,
      amount: byId("filter-amount").value.trim(),
      q: byId("filter-query").value.trim()
    };
    loadState().catch(function (error) { setSync("同步失败", true); showError(error); });
  }

  function clearFilters() {
    window.clearTimeout(app.amountFilterTimer);
    byId("filter-date-from").value = "";
    byId("filter-date-to").value = "";
    byId("filter-category").value = "";
    byId("filter-currency").value = "";
    byId("filter-amount").value = "";
    byId("filter-query").value = "";
    app.filters = { month: byId("dashboard-month").value || currentMonth() };
    loadState().catch(function (error) { setSync("同步失败", true); showError(error); });
  }

  function wireEvents() {
    document.querySelectorAll(".nav-item").forEach(function (button) {
      button.addEventListener("click", function () { switchView(button.dataset.view); });
    });
    document.querySelectorAll("[data-view-target]").forEach(function (button) {
      button.addEventListener("click", function () { switchView(button.dataset.viewTarget); });
    });
    document.querySelectorAll("[data-action='new-entry']").forEach(function (button) {
      button.addEventListener("click", function () { switchView("records-view"); resetEntryForm(); byId("entry-name").focus(); });
    });
    byId("quick-entry-button").addEventListener("click", function () { switchView("records-view"); resetEntryForm(); byId("entry-name").focus(); });
    byId("refresh-button").addEventListener("click", refreshFx);
    byId("dashboard-month").addEventListener("change", function () { app.filters.month = byId("dashboard-month").value || currentMonth(); loadState().catch(showError); });
    byId("filter-form").addEventListener("submit", applyFilters);
    byId("filter-amount").addEventListener("input", function () {
      window.clearTimeout(app.amountFilterTimer);
      if (byId("filter-form").checkValidity()) {
        app.amountFilterTimer = window.setTimeout(function () { applyFilters(); }, 300);
      }
    });
    byId("filter-currency").addEventListener("change", function () {
      if (byId("filter-amount").value && byId("filter-form").checkValidity()) {
        applyFilters();
      }
    });
    byId("clear-filters").addEventListener("click", clearFilters);
    byId("entry-form").addEventListener("submit", saveEntry);
    byId("entry-photo").addEventListener("change", addEntryPhotos);
    byId("cancel-entry").addEventListener("click", resetEntryForm);
    window.addEventListener("pagehide", function (event) {
      if (!event.persisted) releaseEntryPhotos();
    });
    byId("snapshot-form").addEventListener("submit", saveSnapshot);
    byId("exchange-form").addEventListener("submit", saveExchange);
    byId("cancel-exchange").addEventListener("click", resetExchangeForm);
    byId("internal-transfer-form").addEventListener("submit", saveInternalTransfer);
    byId("cancel-internal-transfer").addEventListener("click", resetInternalTransferForm);
    byId("refresh-fx-button").addEventListener("click", refreshFx);
    byId("category-form").addEventListener("submit", saveCategory);
    byId("cancel-category").addEventListener("click", resetCategoryForm);
    byId("create-backup-button").addEventListener("click", createBackup);
    byId("refresh-backups-button").addEventListener("click", function () { loadBackups().catch(showError); });
    byId("export-button").addEventListener("click", exportCsv);
  }

  async function bootstrap() {
    byId("dashboard-month").value = currentMonth();
    byId("entry-date").max = localDate();
    byId("exchange-date").max = localDate();
    byId("internal-transfer-date").max = localDate();
    byId("snapshot-cutoff").max = localDate();
    byId("snapshot-cutoff").value = previousMonthEnd();
    resetEntryForm();
    resetExchangeForm();
    resetInternalTransferForm();
    resetCategoryForm();
    wireEvents();
    try {
      var session = await api("/api/session");
      app.csrf = session.csrf || "";
      if (!app.csrf) {
        throw new Error("本机会话没有返回写入凭据。");
      }
      if (session.auto_exit) {
        startPresence();
      }
      await loadState();
    } catch (error) {
      setSync("连接失败", true);
      showError(error);
    }
  }

  // A shortcut-started server stops once no ledger page is open, so each
  // page reports itself periodically and says goodbye when it closes.
  function sendPresence(closing) {
    return fetch("/api/presence", {
      method: "POST",
      keepalive: true,
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": app.csrf },
      body: JSON.stringify({ tab_id: app.tabId, closing: closing })
    });
  }

  function startPresence() {
    app.tabId = newRequestId();
    function beat() {
      sendPresence(false).then(function (response) {
        if (!response.ok) {
          throw new Error("presence rejected");
        }
      }).catch(function () {
        setSync("服务已停止", true);
        showToast("本机记账服务已停止，请重新双击桌面“记账”打开。", "error");
      });
    }
    beat();
    window.setInterval(beat, 30000);
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible") {
        beat();
      }
    });
    window.addEventListener("pageshow", function (event) {
      if (event.persisted) {
        beat();
      }
    });
    window.addEventListener("pagehide", function () {
      sendPresence(true).catch(function () {});
    });
  }

  document.addEventListener("DOMContentLoaded", bootstrap);
}());
