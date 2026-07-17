  // ── 备注/settings ──
  let _settings = {};

  async function loadSettings() {
    try {
      const res = await fetch("/api/settings");
      if (res.ok) _settings = await res.json();
    } catch (e) { console.warn("loadSettings failed", e); }
  }

  function nickName(cid) {
    return _settings["nickname:" + cid] || GROUP_CHAR_NAMES[cid] || cid;
  }

  function groupNickName() {
    return _settings["group_name"] || "群聊（6）";
  }

  function makeLongPressEditable(el, keyOrGetter, onSave) {
    let timer;
    el.addEventListener("touchstart", e => {
      timer = setTimeout(() => {
        e.preventDefault();
        const settingKey = typeof keyOrGetter === "function" ? keyOrGetter() : keyOrGetter;
        const original = el.textContent;
        const input = document.createElement("input");
        input.value = original;
        input.className = "nickname-edit-input";
        el.replaceWith(input);
        input.focus(); input.select();
        async function confirm() {
          const val = input.value.trim() || original;
          try {
            await fetch("/api/settings", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ key: settingKey, value: val }),
            });
            _settings[settingKey] = val;
          } catch (err) { console.warn("save setting failed", err); }
          el.textContent = val;
          input.replaceWith(el);
          if (onSave) onSave(val);
        }
        input.addEventListener("blur", confirm);
        input.addEventListener("keydown", ev => { if (ev.key === "Enter") input.blur(); });
      }, 500);
    }, { passive: false });
    el.addEventListener("touchend", () => clearTimeout(timer));
    el.addEventListener("touchmove", () => clearTimeout(timer));
  }

  function setAppHeight() {
    const h = window.visualViewport ? window.visualViewport.height : window.innerHeight;
    document.documentElement.style.setProperty('--app-height', h + 'px');
  }
  (window.visualViewport || window).addEventListener('resize', setAppHeight);
  setAppHeight();

  // iOS Safari PWA 键盘弹出修复：viewport 缩小时强制滚到底
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', () => {
      window.scrollTo(0, 0);
      const msgs = document.getElementById('messages');
      if (msgs) msgs.scrollTop = msgs.scrollHeight;
      const groupMsgs = document.getElementById('groupMessages');
      if (groupMsgs) groupMsgs.scrollTop = groupMsgs.scrollHeight;
    });
  }

  function formatMsgTime(input) {
    let d;
    if (input instanceof Date) {
      d = input;
    } else if (typeof input === "string") {
      const iso = input.includes("T") ? input : input.replace(" ", "T") + "Z";
      d = new Date(iso);
    } else {
      return "";
    }
    if (isNaN(d.getTime())) return "";
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return `${hh}:${mm}`;
  }

  // ════════════════════════════════════════════
  // 登录
  // ════════════════════════════════════════════
  function showLoginOverlay() {
    const splash = document.getElementById("splashScreen");
    if (splash) splash.remove();
    document.getElementById("loginOverlay").style.display = "flex";
  }
  function hideLoginOverlay() {
    document.getElementById("loginOverlay").style.display = "none";
  }

  async function submitLogin() {
    const pw = document.getElementById("loginPassword").value.trim();
    if (!pw) return;
    document.getElementById("loginError").style.display = "none";
    const res = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: pw }),
    });
    if (res.ok) {
      hideLoginOverlay();
      await loadAppearance();
      await loadGroupConfig();
      initCharList();
      startSplashDismiss(0);
    } else {
      document.getElementById("loginError").style.display = "block";
    }
  }
  document.getElementById("loginSubmit").addEventListener("click", submitLogin);
  document.getElementById("loginPassword").addEventListener("keydown", e => {
    if (e.key === "Enter") submitLogin();
  });

  async function doLogout() {
    await fetch("/api/logout", { method: "POST" });
    location.reload();
  }
  // logoutBtn replaced by paw-menu

  // ════════════════════════════════════════════
  // 单聊
  // ════════════════════════════════════════════
  const messagesEl = document.getElementById("messages");
  const inputEl    = document.getElementById("input");
  const sendBtn    = document.getElementById("send");
  const charSubEl  = document.getElementById("char-sub");

  const CHAR_META = {
    char1: { label: "角色槽 1" },
    char2: { label: "角色槽 2" },
    char3: { label: "角色槽 3" },
    char4: { label: "角色槽 4" },
    char5: { label: "角色槽 5" },
    char6: { label: "角色槽 6" },
  };

  const histories     = { char1: [], char2: [], char3: [], char4: [], char5: [], char6: [] };
  const historyLoaded = new Set();
  let currentChar = "char1";
  const HISTORY_PAGE_SIZE = 60;
  const historyState = {};
  function ensureHistoryState(charId) {
    if (!historyState[charId]) historyState[charId] = { oldestId: null, hasMore: true, loadingMore: false };
    return historyState[charId];
  }

  function splitBubbleContent(content) {
    if (!content) return [];
    content = content.replace(/\r\n/g, "\n");
    let parts;
    if (content.includes("||")) {
      parts = content.split("||");
    } else if (/\n\s*\n/.test(content)) {
      parts = content.split(/\n\s*\n/);
    } else {
      parts = [content];
    }
    parts = parts.map(s => s.trim()).filter(Boolean);
    if (parts.length > 30) {
      const tail = parts.slice(29).join("\n\n");
      parts = [...parts.slice(0, 29), tail];
    }
    return parts;
  }

  function groupBubbleParts(content, characterId, characterName) {
    const parts = splitBubbleContent(content);
    if (!parts.length || characterId === "user") return parts;
    const fullName = characterName || GROUP_CHAR_NAMES[characterId] || "";
    const names = [fullName];
    if (fullName.startsWith("谢") && fullName.length > 1) names.push(fullName.slice(1));
    const escapedNames = [...new Set(names.filter(Boolean))]
      .map(name => name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
    if (!escapedNames.length) return parts;
    const prefix = new RegExp(
      `^\\s*(?:#{1,6}\\s*)?(?:(?:\\*\\*|__)?\\s*(?:${escapedNames.join("|")})\\s*[：:]\\s*(?:\\*\\*|__)?\\s*)+`
    );
    parts[0] = parts[0].replace(prefix, "").trim();
    return parts.filter(Boolean);
  }

  const charPreviewEls = {};
  const charAvatars = {
    char1: "/static/char1.svg",
    char2: "/static/char2.svg",
    char3: "/static/char3.svg",
    char4: "/static/char4.svg",
    char5: "/static/char5.svg",
    char6: "/static/char6.svg",
  };
  let userAvatar = "/static/user.svg";
  let appearanceState = null;

  function hexToRgb(hex) {
    const normalized = String(hex || "").replace("#", "");
    if (!/^[0-9a-f]{6}$/i.test(normalized)) return "0, 0, 0";
    return [0, 2, 4]
      .map(offset => parseInt(normalized.slice(offset, offset + 2), 16))
      .join(", ");
  }

  function syncSearchInputInk(preferredColor = "") {
    const input = document.getElementById("searchInput");
    if (!input) return;
    const mirror = document.getElementById("searchInputMirror");
    const ink = preferredColor
      || getComputedStyle(document.documentElement).getPropertyValue("--text").trim()
      || "#5C3F48";
    input.style.setProperty("caret-color", ink);
    if (mirror) {
      mirror.textContent = input.value;
      mirror.style.color = ink;
    }
  }

  function applyTheme(data) {
    const theme = data?.themes?.find(item => item.id === data.theme);
    if (!theme) return;
    const colors = theme.colors || {};
    const root = document.documentElement;
    const colorVars = {
      user_bubble: "--user-bubble",
      cream: "--cream",
      ai_bubble: "--ai-bubble",
      dusky: "--dusky",
      chrome: "--chrome",
      text: "--text",
      on_dusky: "--on-dusky",
      bg: "--bg",
      card: "--card",
    };
    Object.entries(colorVars).forEach(([key, variable]) => {
      if (colors[key]) root.style.setProperty(variable, colors[key]);
    });
    ["user_bubble", "cream", "ai_bubble", "dusky", "text"].forEach(key => {
      if (colors[key]) {
        root.style.setProperty(`--${key.replace("_bubble", "")}-rgb`, hexToRgb(colors[key]));
      }
    });
    if (colors.text) root.style.setProperty("--muted", `rgba(${hexToRgb(colors.text)}, 0.5)`);
    if (colors.dusky) root.style.setProperty("--border", `rgba(${hexToRgb(colors.dusky)}, 0.2)`);
    const themeColorMeta = document.getElementById("themeColorMeta");
    if (colors.chrome) {
      if (themeColorMeta) themeColorMeta.setAttribute("content", colors.chrome);
      try { localStorage.setItem("becoming-theme-chrome", colors.chrome); } catch (_) {}
    }
    syncSearchInputInk(colors.text);
    if (theme.list_background) {
      const safeListUrl = String(theme.list_background).replace(/["\\\n\r]/g, "");
      root.style.setProperty("--list-background-image", `url("${safeListUrl}")`);
    }
    root.dataset.theme = theme.id;
  }

  function replaceVisibleImageSources(oldUrl, newUrl) {
    if (!oldUrl || oldUrl === newUrl) return;
    const oldAbsolute = new URL(oldUrl, window.location.href).href;
    document.querySelectorAll("img").forEach(img => {
      if (img.src === oldAbsolute) img.src = newUrl;
    });
  }

  function applyAppearance(data) {
    if (!data) return;
    applyTheme(data);
    const avatars = data.avatars || {};
    Object.entries(avatars).forEach(([cid, item]) => {
      if (!item?.url) return;
      if (cid === "user") {
        replaceVisibleImageSources(userAvatar, item.url);
        userAvatar = item.url;
      } else if (cid in charAvatars) {
        replaceVisibleImageSources(charAvatars[cid], item.url);
        charAvatars[cid] = item.url;
      }
    });
    if (data.chat_background?.url) {
      const safeUrl = String(data.chat_background.url).replace(/["\\\n\r]/g, "");
      document.documentElement.style.setProperty(
        "--chat-background-image", `url("${safeUrl}")`
      );
    }
    appearanceState = data;
  }

  async function loadAppearance() {
    try {
      const res = await fetch("/api/appearance");
      if (res.ok) applyAppearance(await res.json());
    } catch (e) {
      console.warn("loadAppearance failed", e);
    }
    return appearanceState;
  }
  let STICKERS_CACHE = {}; // key -> { file, label }，initStickers() 填充

  async function decodeImagesBeforeSwap(root) {
    const images = [...root.querySelectorAll("img")].filter(img => img.src);
    await Promise.all(images.map(img => {
      if (typeof img.decode === "function") {
        return img.decode().catch(() => {});
      }
      if (img.complete) return Promise.resolve();
      return new Promise(resolve => {
        img.addEventListener("load", resolve, { once: true });
        img.addEventListener("error", resolve, { once: true });
      });
    }));
  }

  function decorateDesireAvatar(img, characterId) {
    if (!img || !characterId || !(characterId in histories)) return img;
    img.classList.add("desire-avatar");
    img.dataset.characterId = characterId;
    return img;
  }

  const DESIRE_DRIVE_META = [
    ["attachment", "想你"],
    ["curiosity", "好奇"],
    ["reflection", "沉淀"],
    ["duty", "记挂"],
    ["social", "社交"],
    ["fatigue", "疲惫"],
    ["libido", "亲密"],
    ["stress", "压力"],
  ];
  const DESIRE_INTENT_LABELS = {
    attachment: "靠近你一点",
    curiosity: "看看世界",
    reflection: "安静沉淀",
    duty: "把牵挂说完",
    social: "看看家里",
    fatigue: "好好休息",
    libido: "贴近你一点",
    stress: "在你身边待会儿",
  };

  function closeDesireSheet() {
    const sheet = document.getElementById("desireSheet");
    sheet.classList.add("hidden");
    sheet.setAttribute("aria-hidden", "true");
  }

  async function openDesireSheet(characterId) {
    if (!(characterId in histories)) return;
    const sheet = document.getElementById("desireSheet");
    const list = document.getElementById("desireDriveList");
    document.getElementById("desireName").textContent = nickName(characterId);
    document.getElementById("desireAvatar").src = charAvatars[characterId] || "";
    document.getElementById("desireIntent").textContent = "正在听心跳";
    document.getElementById("desireReason").textContent = "…";
    document.getElementById("desireThoughtWrap").classList.add("hidden");
    list.innerHTML = "";
    sheet.classList.remove("hidden");
    sheet.setAttribute("aria-hidden", "false");

    try {
      const response = await fetch(`/api/desire/state/${characterId}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "加载失败");
      const driveKey = data.intent?.drive_key || "reflection";
      document.getElementById("desireName").textContent = nickName(characterId) || data.name;
      document.getElementById("desireAvatar").src = data.avatar || charAvatars[characterId] || "";
      document.getElementById("desireIntent").textContent = DESIRE_INTENT_LABELS[driveKey] || "安静待一会儿";
      document.getElementById("desireReason").textContent = data.intent?.reason || "";
      list.innerHTML = "";
      DESIRE_DRIVE_META.forEach(([key, label]) => {
        const value = Math.max(0, Math.min(1, Number(data.drives?.[key]) || 0));
        const row = document.createElement("div");
        row.className = "desire-drive-row";
        row.dataset.drive = key;
        row.innerHTML = `
          <span class="desire-drive-label">${label}</span>
          <span class="desire-drive-track"><span class="desire-drive-fill" style="width:${Math.round(value * 100)}%"></span></span>
          <span class="desire-drive-value">${Math.round(value * 100)}</span>`;
        list.appendChild(row);
      });
      const thought = data.thoughts?.[0]?.text;
      if (thought) {
        document.getElementById("desireThought").textContent = thought;
        document.getElementById("desireThoughtWrap").classList.remove("hidden");
      }
    } catch (error) {
      document.getElementById("desireIntent").textContent = "暂时听不清";
      document.getElementById("desireReason").textContent = "等一会儿再悄悄看看。";
    }
  }

  document.getElementById("desireClose").addEventListener("click", closeDesireSheet);
  document.getElementById("desireSheet").addEventListener("click", event => {
    if (event.target === event.currentTarget) closeDesireSheet();
  });
  document.addEventListener("click", event => {
    const avatar = event.target.closest(".desire-avatar[data-character-id]");
    if (avatar) {
      event.stopPropagation();
      openDesireSheet(avatar.dataset.characterId);
    }
  });

  async function initStickers() {
    try {
      const resp = await fetch("/api/stickers");
      if (!resp.ok) return;
      const data = await resp.json();
      (data.stickers || []).forEach(s => { STICKERS_CACHE[s.key] = s; });
      renderStickerGrid();
    } catch (e) { /* 静默失败，猫爪菜单里表情包项就不可用 */ }
  }

  function renderStickerGrid() {
    const grid = document.getElementById("stickerGrid");
    if (!grid) return;
    grid.innerHTML = "";
    Object.entries(STICKERS_CACHE).forEach(([key, s]) => {
      const img = document.createElement("img");
      img.src = `/static/stickers/${s.file}`;
      img.alt = s.label;
      img.title = s.label;
      img.addEventListener("click", () => sendStickerFromPicker(key));
      grid.appendChild(img);
    });
  }

  function buildStickerBlock(data, who, time) {
    const block = document.createElement("div");
    block.className = "single-msg-block " + (who === "user" ? "from-user" : "from-ai");
    if (who === "ai") {
      const avatarImg = document.createElement("img");
      avatarImg.src = charAvatars[currentChar] || "";
      avatarImg.style.cssText = "width:32px;height:32px;border-radius:50%;object-fit:cover;margin-bottom:4px;";
      avatarImg.onerror = function() { this.style.display = "none"; };
      decorateDesireAvatar(avatarImg, currentChar);
      block.appendChild(avatarImg);
    }
    const bubble = document.createElement("div");
    bubble.className = "sticker-bubble";
    const img = document.createElement("img");
    const meta = STICKERS_CACHE[data.key];
    img.src = `/static/stickers/${meta ? meta.file : 'placeholder.svg'}`;
    img.alt = meta ? meta.label : data.key;
    bubble.appendChild(img);
    block.appendChild(bubble);
    if (who === "user") {
      const avatarImg = document.createElement("img");
      avatarImg.src = userAvatar;
      avatarImg.style.cssText = "width:32px;height:32px;border-radius:50%;object-fit:cover;margin-top:4px;";
      avatarImg.onerror = function() { this.style.display = "none"; };
      block.appendChild(avatarImg);
    }
    const timeStr = formatMsgTime(time);
    if (timeStr) {
      const timeDiv = document.createElement("div");
      timeDiv.className = "msg-time";
      timeDiv.textContent = timeStr;
      block.appendChild(timeDiv);
    }
    return block;
  }

  function buildImageBlock(data, who, time, messageId) {
    const block = document.createElement("div");
    block.className = "single-msg-block " + (who === "user" ? "from-user" : "from-ai");
    if (who === "ai") {
      const avatarImg = document.createElement("img");
      avatarImg.src = charAvatars[currentChar] || "";
      avatarImg.style.cssText = "width:32px;height:32px;border-radius:50%;object-fit:cover;margin-bottom:4px;";
      avatarImg.onerror = function() { this.style.display = "none"; };
      decorateDesireAvatar(avatarImg, currentChar);
      block.appendChild(avatarImg);
    }
    const bubble = document.createElement("div");
    bubble.className = "bubble image-bubble " + (who === "user" ? "user" : "ai");
    if (messageId) bubble.dataset.messageId = messageId;
    const img = document.createElement("img");
    img.src = data.url || "";
    img.alt = data.name || "图片";
    img.loading = "lazy";
    bubble.appendChild(img);
    block.appendChild(bubble);
    if (who === "user") {
      const avatarImg = document.createElement("img");
      avatarImg.src = userAvatar;
      avatarImg.style.cssText = "width:32px;height:32px;border-radius:50%;object-fit:cover;margin-top:4px;";
      avatarImg.onerror = function() { this.style.display = "none"; };
      block.appendChild(avatarImg);
    }
    const timeStr = formatMsgTime(time);
    if (timeStr) {
      const timeDiv = document.createElement("div");
      timeDiv.className = "msg-time";
      timeDiv.textContent = timeStr;
      block.appendChild(timeDiv);
    }
    return block;
  }

  function buildTransferBlock(data, who, time) {
    const block = document.createElement("div");
    block.className = "single-msg-block " + (who === "user" ? "from-user" : "from-ai");
    if (who === "ai") {
      const avatarImg = document.createElement("img");
      avatarImg.src = charAvatars[currentChar] || "";
      avatarImg.style.cssText = "width:32px;height:32px;border-radius:50%;object-fit:cover;margin-bottom:4px;";
      avatarImg.onerror = function() { this.style.display = "none"; };
      decorateDesireAvatar(avatarImg, currentChar);
      block.appendChild(avatarImg);
    }
    const bubble = document.createElement("div");
    bubble.className = "transfer-bubble";
    const icon = document.createElement("span");
    icon.className = "material-symbols-outlined";
    icon.textContent = "payments";
    const amountEl = document.createElement("div");
    amountEl.className = "transfer-amount";
    amountEl.textContent = "🐾 " + (data.amount ?? "");
    bubble.appendChild(icon);
    bubble.appendChild(amountEl);
    if (data.note) {
      const noteEl = document.createElement("div");
      noteEl.className = "transfer-note-text";
      noteEl.textContent = data.note;
      bubble.appendChild(noteEl);
    }
    block.appendChild(bubble);
    if (who === "user") {
      const avatarImg = document.createElement("img");
      avatarImg.src = userAvatar;
      avatarImg.style.cssText = "width:32px;height:32px;border-radius:50%;object-fit:cover;margin-top:4px;";
      avatarImg.onerror = function() { this.style.display = "none"; };
      block.appendChild(avatarImg);
    }
    const timeStr = formatMsgTime(time);
    if (timeStr) {
      const timeDiv = document.createElement("div");
      timeDiv.className = "msg-time";
      timeDiv.textContent = timeStr;
      block.appendChild(timeDiv);
    }
    return block;
  }

  function formatTokenCount(value) {
    return new Intl.NumberFormat("zh-CN").format(Number(value) || 0);
  }

  function updateThinkBlock(block, toolsCalled, metrics) {
    const body = block.querySelector('.think-body');
    body.innerHTML = '';
    let hasDetails = false;
    if (metrics) {
      hasDetails = true;
      const monitor = document.createElement('details');
      monitor.className = 'cache-monitor';
      const metricBox = document.createElement('div');
      metricBox.className = 'cache-metrics';
      const ratio = Math.round((Number(metrics.cache_hit_ratio) || 0) * 100);
      const reported = metrics.cache_reported === true;
      const statusClass = !reported ? 'unknown' : (metrics.cache_read_tokens > 0 ? 'hit' : (metrics.cache_write_tokens > 0 ? 'write' : 'miss'));
      const statusText = !reported ? '供应商未报告' : (metrics.cache_read_tokens > 0 ? `命中 ${ratio}%` : (metrics.cache_write_tokens > 0 ? '正在写入缓存' : '本次未命中'));
      const summary = document.createElement('summary');
      summary.className = 'cache-monitor-summary';
      summary.innerHTML = `
        <span class="cache-status-dot ${statusClass}"></span>
        <span>命中率监测</span>
        <span class="cache-monitor-chevron">›</span>`;
      metricBox.innerHTML = `
        <div class="cache-metric-head">
          <strong>${statusText}</strong>
          <span>${metrics.provider === 'anthropic' ? 'Anthropic' : 'OpenRouter'}</span>
        </div>
        <div class="cache-metric-grid">
          <span>缓存读取</span><b>${formatTokenCount(metrics.cache_read_tokens)}</b>
          <span>缓存写入</span><b>${formatTokenCount(metrics.cache_write_tokens)}</b>
          <span>总输入</span><b>${formatTokenCount(metrics.input_tokens)}</b>
          <span>本次输出</span><b>${formatTokenCount(metrics.output_tokens)}</b>
        </div>`;
      const modelName = document.createElement('div');
      modelName.className = 'cache-model-name';
      modelName.textContent = metrics.model || '未知模型';
      metricBox.appendChild(modelName);
      monitor.appendChild(summary);
      monitor.appendChild(metricBox);
      body.appendChild(monitor);
    }
    if (toolsCalled && toolsCalled.length > 0) {
      hasDetails = true;
      const pills = document.createElement('div');
      pills.className = 'think-pills';
      const labelMap = {
        save_memory: '🧠 存记忆',
        send_transfer: '💸 转账',
        send_sticker: '🎨 表情',
        press_hug: '🤍 和好按钮',
        close_window: '🚪 封窗',
      };
      toolsCalled.forEach(t => {
        const isTrace = t && typeof t === 'object' && !Array.isArray(t);
        const name = isTrace ? String(t.name || '') : String(t || '');
        const label = labelMap[name] || (name.startsWith('mcp:') ? `连接 ${name.slice(4)}` : name);
        if (isTrace) {
          const trace = document.createElement('details');
          trace.className = 'tool-trace' + (t.status === 'error' ? ' error' : '');
          const summary = document.createElement('summary');
          summary.className = 'think-pill tool-trace-summary';
          summary.textContent = label;
          const traceBody = document.createElement('div');
          traceBody.className = 'tool-trace-body';
          const args = t.arguments && typeof t.arguments === 'object' ? t.arguments : {};
          if (Object.keys(args).length) {
            const inputLabel = document.createElement('strong');
            inputLabel.textContent = '输入';
            const input = document.createElement('pre');
            input.textContent = JSON.stringify(args, null, 2);
            traceBody.appendChild(inputLabel);
            traceBody.appendChild(input);
          }
          const outputLabel = document.createElement('strong');
          outputLabel.textContent = '返回';
          const output = document.createElement('pre');
          output.textContent = String(t.output || '工具已执行，没有返回文字。');
          traceBody.appendChild(outputLabel);
          traceBody.appendChild(output);
          trace.appendChild(summary);
          trace.appendChild(traceBody);
          pills.appendChild(trace);
          return;
        }
        const pill = document.createElement('span');
        pill.className = 'think-pill';
        pill.textContent = label;
        pills.appendChild(pill);
      });
      body.appendChild(pills);
    }
    if (!hasDetails) {
      body.textContent = '小猫没有在动脑';
    }
  }

  function buildThinkBlock(toolsCalled, metrics) {
    const wrap = document.createElement('div');
    wrap.className = 'think-block';
    const header = document.createElement('div');
    header.className = 'think-header';
    header.innerHTML =
      `<span class="material-symbols-outlined">pets</span>` +
      `<span class="think-dots">···</span>` +
      `<span class="think-chevron">›</span>`;
    const body = document.createElement('div');
    body.className = 'think-body';
    header.addEventListener('click', () => wrap.classList.toggle('open'));
    wrap.appendChild(header);
    wrap.appendChild(body);
    updateThinkBlock(wrap, toolsCalled || [], metrics);
    return wrap;
  }

  function buildSingleBlock(text, who, time, messageId, toolsCalled, metrics) {
    if (text && text.startsWith("__TRANSFER__")) {
      try {
        const data = JSON.parse(text.slice(12));
        const resolvedWho = data.from === "char" ? "ai" : "user";
        return buildTransferBlock(data, resolvedWho, time);
      } catch (e) { /* fallthrough to normal bubble */ }
    }
    if (text && text.startsWith("__STICKER__")) {
      try {
        const data = JSON.parse(text.slice(11));
        const resolvedWho = data.from === "char" ? "ai" : "user";
        return buildStickerBlock(data, resolvedWho, time);
      } catch (e) { /* fallthrough to normal bubble */ }
    }
    if (text && text.startsWith("__IMAGE__")) {
      try {
        const data = JSON.parse(text.slice(9));
        const resolvedWho = data.from === "char" ? "ai" : "user";
        return buildImageBlock(data, resolvedWho, time, messageId);
      } catch (e) { /* fallthrough to normal bubble */ }
    }
    const block = document.createElement("div");
    block.className = "single-msg-block " + (who === "user" ? "from-user" : "from-ai");
    if (who === "ai") {
      const avatarImg = document.createElement("img");
      avatarImg.src = charAvatars[currentChar] || "";
      avatarImg.style.cssText = "width:32px;height:32px;border-radius:50%;object-fit:cover;margin-bottom:4px;";
      avatarImg.onerror = function() { this.style.display = "none"; };
      decorateDesireAvatar(avatarImg, currentChar);
      block.appendChild(avatarImg);
      block.appendChild(buildThinkBlock(toolsCalled, metrics));
      splitBubbleContent(text).forEach(part => {
        const div = document.createElement("div");
        div.className = "bubble ai";
        div.textContent = part;
        if (messageId) div.dataset.messageId = messageId;
        block.appendChild(div);
      });
    } else {
      const div = document.createElement("div");
      div.className = "bubble user";
      div.textContent = text;
      if (messageId) div.dataset.messageId = messageId;
      block.appendChild(div);
      const avatarImg = document.createElement("img");
      avatarImg.src = userAvatar;
      avatarImg.style.cssText = "width:32px;height:32px;border-radius:50%;object-fit:cover;margin-top:4px;";
      avatarImg.onerror = function() { this.style.display = "none"; };
      block.appendChild(avatarImg);
    }
    const timeStr = formatMsgTime(time);
    if (timeStr) {
      const timeDiv = document.createElement("div");
      timeDiv.className = "msg-time";
      timeDiv.textContent = timeStr;
      block.appendChild(timeDiv);
    }
    return block;
  }

  function addBubble(content, who, messageId) {
    const time = new Date();
    histories[currentChar].push({ id: messageId, text: content, who, time });
    messagesEl.appendChild(buildSingleBlock(content, who, time, messageId));
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function renderFromCache(charId) {
    messagesEl.innerHTML = "";
    histories[charId].forEach(({ id, text, who, time, toolsCalled, metrics }) => {
      messagesEl.appendChild(buildSingleBlock(text, who, time, id, toolsCalled, metrics));
    });
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  async function loadHistory(charId) {
    if (historyLoaded.has(charId)) { renderFromCache(charId); return; }
    historyLoaded.add(charId);
    try {
      const resp = await fetch(`/api/messages?character_id=${charId}&limit=${HISTORY_PAGE_SIZE}`);
      const data = await resp.json();
      const msgs = data.messages || [];
      msgs.forEach(m => histories[charId].push({
        id: m.id, text: m.content,
        who: m.role === "user" ? "user" : "ai",
        time: m.created_at,
        toolsCalled: m.tools_called || [],
        metrics: m.metrics,
      }));
      const st = ensureHistoryState(charId);
      st.oldestId = msgs.length ? msgs[0].id : null;
      st.hasMore  = !!data.has_more;
      if (charId === currentChar) renderFromCache(charId);
    } catch (e) { console.warn("loadHistory failed for", charId, e); }
  }

  async function loadOlderMessages(charId) {
    const st = ensureHistoryState(charId);
    if (!st.hasMore || st.loadingMore || st.oldestId == null) return;
    st.loadingMore = true;
    try {
      const resp = await fetch(`/api/messages?character_id=${charId}&limit=${HISTORY_PAGE_SIZE}&before_id=${st.oldestId}`);
      const data = await resp.json();
      const msgs = data.messages || [];
      st.hasMore = !!data.has_more;
      if (!msgs.length) return;
      st.oldestId = msgs[0].id;
      const newEntries = msgs.map(m => ({
        id: m.id, text: m.content,
        who: m.role === "user" ? "user" : "ai",
        time: m.created_at,
        toolsCalled: m.tools_called || [],
        metrics: m.metrics,
      }));
      histories[charId].splice(0, 0, ...newEntries);
      if (charId !== currentChar) return;

      const prevHeight = messagesEl.scrollHeight;
      const prevTop    = messagesEl.scrollTop;
      const anchor     = messagesEl.firstChild;
      newEntries.forEach(({ id, text, who, time, toolsCalled, metrics }) => messagesEl.insertBefore(
        buildSingleBlock(text, who, time, id, toolsCalled, metrics), anchor
      ));
      messagesEl.scrollTop = prevTop + (messagesEl.scrollHeight - prevHeight);
    } catch (e) {
      console.warn("loadOlderMessages failed for", charId, e);
    } finally {
      st.loadingMore = false;
    }
  }
  messagesEl.addEventListener("scroll", () => {
    if (messagesEl.scrollTop < 80) loadOlderMessages(currentChar);
  });

  function isActionSegment(s) {
    const t = s.trim();
    if (!t) return false;
    return (t[0] === "（" && t[t.length - 1] === "）") ||
           (t[0] === "(" && t[t.length - 1] === ")");
  }

  function pickPreviewSegment(rawContent) {
    const parts = splitBubbleContent(rawContent);
    if (!parts.length) return "";
    const nonAction = parts.find(p => !isActionSegment(p));
    return nonAction !== undefined ? nonAction : parts[0];
  }

  function formatPreviewText(text) {
    if (text && text.startsWith("__TRANSFER__")) return "[转账]";
    if (text && text.startsWith("__STICKER__")) return "[表情]";
    if (text && text.startsWith("__IMAGE__")) return "[图片]";
    return text;
  }

  function getPreviewText(text) {
    const s = pickPreviewSegment(formatPreviewText(text)).replace(/\n/g, " ").trim();
    return s.length > 24 ? s.slice(0, 24) + "…" : s;
  }

  async function refreshCharPreviews() {
    const order = ["char1", "char2", "char3", "char4", "char5", "char6"];
    for (const cid of order) {
      const el = charPreviewEls[cid];
      if (!el) continue;
      if (histories[cid] && histories[cid].length > 0) {
        el.textContent = getPreviewText(histories[cid][histories[cid].length - 1].text);
      } else {
        try {
          const resp = await fetch(`/api/messages?character_id=${cid}`);
          const data = await resp.json();
          const msgs = data.messages || [];
          if (msgs.length > 0) {
            el.textContent = getPreviewText(msgs[msgs.length - 1].content);
          }
        } catch (e) {
          console.warn("preview fetch failed for", cid, e);
        }
      }
    }
  }

  function showSingleSub(sub) {
    const listView = document.getElementById("singleListView");
    const chatView = document.getElementById("singleChatView");
    if (sub === "list") {
      listView.style.display = "flex";
      chatView.style.display = "none";
      refreshCharPreviews();
    } else {
      listView.style.display = "none";
      chatView.style.display = "flex";
    }
  }

  async function initCharList() {
    try {
      const resp = await fetch("/api/characters");
      if (resp.status === 401) { showLoginOverlay(); return; }
      const data = await resp.json();
      const container = document.getElementById("charListContainer");
      container.innerHTML = "";
      const order = ["char1", "char2", "char3", "char4", "char5", "char6"];
      order.forEach(cid => {
        const c = data[cid];
        if (!c) return;
        const row = document.createElement("div");
        row.className = "char-list-row";

        const avatarWrap = document.createElement("div");
        avatarWrap.style.cssText = "position:relative;flex-shrink:0;";
        const img = document.createElement("img");
        img.src = c.avatar;
        img.onerror = function() { this.style.display = "none"; };
        img.style.cssText = "width:48px;height:48px;border-radius:50%;object-fit:cover;display:block;";
        const dot = document.createElement("div");
        dot.className = "unread-dot hidden";
        dot.dataset.cid = cid;
        avatarWrap.appendChild(img);
        avatarWrap.appendChild(dot);

        const info = document.createElement("div");
        info.style.cssText = "margin-left:12px;flex:1;min-width:0;";
        const nameEl = document.createElement("div");
        nameEl.className = "char-list-name";
        nameEl.dataset.cid = cid;
        nameEl.style.cssText = "font-weight:600;";
        nameEl.textContent = nickName(cid);
        const previewEl = document.createElement("div");
        previewEl.className = "char-list-preview";
        previewEl.style.cssText = "font-size:13px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
        previewEl.textContent = "点击开始聊天";
        info.appendChild(nameEl);
        info.appendChild(previewEl);

        row.appendChild(avatarWrap);
        row.appendChild(info);

        charPreviewEls[cid] = previewEl;
        charAvatars[cid] = c.avatar;

        row.addEventListener("click", () => {
          dot.classList.add("hidden");
          fetch(`/api/unread/${cid}/clear`, { method: "POST" }).catch(() => {});
          document.getElementById("char-name").textContent = nickName(cid);
          charSubEl.textContent = CHAR_META[cid]?.label ?? cid;
          showSingleSub("chat");
          switchChar(cid);
        });
        container.appendChild(row);
      });
      refreshCharPreviews();
      refreshUnread();
    } catch (e) {
      console.warn("initCharList failed", e);
    }
  }

  async function refreshUnread() {
    try {
      const res = await fetch("/api/unread");
      if (!res.ok) return;
      const unreadList = await res.json();
      document.querySelectorAll(".unread-dot").forEach(dot => {
        dot.classList.toggle("hidden", !unreadList.includes(dot.dataset.cid));
      });
    } catch(e) {}
  }

  function switchChar(charId) {
    currentChar = charId;
    charSubEl.textContent = CHAR_META[charId]?.label ?? charId;
    messagesEl.innerHTML = "";
    loadHistory(charId);
  }

  function addPendingAiBlock() {
    const aiBlock = document.createElement("div");
    aiBlock.className = "single-msg-block from-ai";
    const avatarImg = document.createElement("img");
    avatarImg.src = charAvatars[currentChar] || "";
    avatarImg.style.cssText = "width:32px;height:32px;border-radius:50%;object-fit:cover;margin-bottom:4px;";
    avatarImg.onerror = function() { this.style.display = "none"; };
    decorateDesireAvatar(avatarImg, currentChar);
    aiBlock.appendChild(avatarImg);
    const thinkBlock = buildThinkBlock([]);
    aiBlock.appendChild(thinkBlock);
    const thinking = document.createElement("div");
    thinking.className = "bubble ai";
    thinking.innerHTML = '<span class="loading-dots"><span class="loading-dot"></span><span class="loading-dot"></span><span class="loading-dot"></span></span>';
    aiBlock.appendChild(thinking);
    messagesEl.appendChild(aiBlock);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return { aiBlock, thinkBlock, thinking };
  }

  async function renderResponseEffects(data) {
    const tools = data.tools_called || [];
    if (tools.includes("press_hug")) spawnHugRain();
    if (data.transfer) {
      await new Promise(r => setTimeout(r, 400));
      addTransferBubble({ ...data.transfer, from: "char" }, "ai");
    }
    if (data.sticker) {
      await new Promise(r => setTimeout(r, 400));
      addStickerBubble({ ...data.sticker, from: "char" }, "ai");
    }
    if (data.window_closed) {
      await new Promise(r => setTimeout(r, 300));
      showCloseWindowModal(data.window_closed.reason || "");
    }
  }

  async function renderAiResponse(data, pending) {
    const { aiBlock, thinkBlock, thinking } = pending;
    const rawReply = data.reply || (data.replies || []).join("||") || "(没有回复)";
    const parts = splitBubbleContent(rawReply);
    if (!parts.length) parts.push("(没有回复)");
    const aiTime = new Date();
    const tools = data.tools_called || [];
    histories[currentChar].push({
      id: data.reply_id, text: rawReply, who: "ai", time: aiTime,
      toolsCalled: tools, metrics: data.metrics,
    });
    updateThinkBlock(thinkBlock, tools, data.metrics);
    if (data.user_msg_id) {
      aiBlock.previousElementSibling?.querySelector('.bubble.user')
        ?.setAttribute('data-message-id', data.user_msg_id);
      const h = histories[currentChar];
      const userEntry = h[h.length - 2];
      if (userEntry?.who === 'user') userEntry.id = data.user_msg_id;
    }
    thinking.textContent = parts[0];
    if (data.reply_id) thinking.dataset.messageId = data.reply_id;
    for (let i = 1; i < parts.length; i++) {
      await new Promise(r => setTimeout(r, 600));
      const div = document.createElement("div");
      div.className = "bubble ai bubble-enter";
      div.textContent = parts[i];
      if (data.reply_id) div.dataset.messageId = data.reply_id;
      aiBlock.appendChild(div);
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }
    const timeDiv = document.createElement("div");
    timeDiv.className = "msg-time";
    timeDiv.textContent = formatMsgTime(aiTime);
    aiBlock.appendChild(timeDiv);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    await renderResponseEffects(data);
  }

  function renderAiError(pending, error) {
    const errText = "(网络出错:" + error + ")";
    pending.thinking.textContent = errText;
    histories[currentChar].push({ text: errText, who: "ai", time: new Date() });
  }

  async function send() {
    const text = inputEl.value.trim();
    if (!text) return;
    inputEl.value = "";
    closePawMenu();
    sendBtn.disabled = true;
    addBubble(text, "user");
    const pending = addPendingAiBlock();

    try {
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, character_id: currentChar, session_id: "default" }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "发送失败");
      await renderAiResponse(data, pending);
    } catch (e) {
      renderAiError(pending, e);
    } finally {
      sendBtn.disabled = false;
      inputEl.focus();
    }
  }

  document.getElementById("backToList").addEventListener("click", () => showSingleSub("list"));

  // ── 单聊左缘右划返回：列表在下，会话层跟手退场 ──
  const singleView = document.getElementById("singleView");
  const singleListView = document.getElementById("singleListView");
  const singleChatView = document.getElementById("singleChatView");
  const swipeBackHint = document.getElementById("swipeBackHint");
  let swipeBackState = null;
  let swipeBackTimer = null;

  function clearSwipeBackStyles({ hideList = true } = {}) {
    clearTimeout(swipeBackTimer);
    swipeBackTimer = null;
    singleView.classList.remove("swipe-peeking", "swipe-settling");
    singleView.style.removeProperty("--swipe-panel-x");
    singleView.style.removeProperty("--swipe-list-x");
    singleView.style.removeProperty("--swipe-list-opacity");
    swipeBackHint.classList.remove("tracking", "ready");
    swipeBackHint.style.removeProperty("--swipe-x");
    swipeBackHint.style.removeProperty("--swipe-opacity");
    swipeBackHint.style.removeProperty("--swipe-scale");
    if (hideList && singleChatView.style.display !== "none") {
      singleListView.style.display = "none";
    }
  }

  function resetSwipeBack() {
    swipeBackState = null;
    clearSwipeBackStyles();
  }

  function settleSwipeBack(shouldReturn) {
    swipeBackState = null;
    swipeBackHint.classList.remove("tracking", "ready");
    singleView.classList.add("swipe-settling");
    singleView.style.setProperty(
      "--swipe-panel-x",
      shouldReturn ? `${singleChatView.clientWidth}px` : "0px"
    );
    singleView.style.setProperty("--swipe-list-x", shouldReturn ? "0px" : "-18px");
    singleView.style.setProperty("--swipe-list-opacity", shouldReturn ? "1" : "0.76");
    swipeBackTimer = setTimeout(() => {
      clearSwipeBackStyles({ hideList: !shouldReturn });
      if (shouldReturn) showSingleSub("list");
    }, 215);
  }

  singleChatView.addEventListener("touchstart", event => {
    if (event.touches.length !== 1 || singleChatView.style.display === "none") return;
    if (singleView.classList.contains("swipe-settling")) return;
    if (event.target.closest("input, textarea, button, select, [contenteditable='true']")) return;
    const touch = event.touches[0];
    if (touch.clientX > 56) return;
    singleListView.style.display = "flex";
    singleView.classList.add("swipe-peeking");
    singleView.style.setProperty("--swipe-panel-x", "0px");
    singleView.style.setProperty("--swipe-list-x", "-18px");
    singleView.style.setProperty("--swipe-list-opacity", "0.76");
    swipeBackState = {
      startX: touch.clientX,
      startY: touch.clientY,
      startedAt: Date.now(),
      dx: 0,
      horizontal: false,
      ready: false,
    };
  }, { passive: true });

  singleChatView.addEventListener("touchmove", event => {
    if (!swipeBackState || event.touches.length !== 1) return;
    const touch = event.touches[0];
    const dx = Math.max(0, touch.clientX - swipeBackState.startX);
    const dy = touch.clientY - swipeBackState.startY;
    if (!swipeBackState.horizontal && Math.abs(dy) > 18 && Math.abs(dy) > dx) {
      resetSwipeBack();
      return;
    }
    if (dx < 8 || dx < Math.abs(dy) * 1.15) return;

    event.preventDefault();
    swipeBackState.dx = dx;
    swipeBackState.horizontal = true;
    swipeBackState.ready = dx >= 86;
    const progress = Math.min(dx / 108, 1);
    singleView.style.setProperty("--swipe-panel-x", `${dx}px`);
    singleView.style.setProperty("--swipe-list-x", `${-18 + progress * 18}px`);
    singleView.style.setProperty("--swipe-list-opacity", String(0.76 + progress * 0.24));
    swipeBackHint.style.setProperty("--swipe-x", `${Math.min(dx * 0.08, 8)}px`);
    swipeBackHint.style.setProperty("--swipe-opacity", String(0.34 + progress * 0.66));
    swipeBackHint.style.setProperty("--swipe-scale", String(0.78 + progress * 0.22));
    swipeBackHint.classList.add("tracking");
    swipeBackHint.classList.toggle("ready", swipeBackState.ready);
  }, { passive: false });

  singleChatView.addEventListener("touchend", () => {
    if (!swipeBackState) return;
    if (!swipeBackState.horizontal) {
      resetSwipeBack();
      return;
    }
    const elapsed = Math.max(Date.now() - swipeBackState.startedAt, 1);
    const fastFlick = swipeBackState.horizontal
      && swipeBackState.dx >= 44
      && swipeBackState.dx / elapsed >= 0.42;
    const shouldReturn = swipeBackState.ready || fastFlick;
    settleSwipeBack(shouldReturn);
  });
  singleChatView.addEventListener("touchcancel", resetSwipeBack);

  // ── 发送键长按菜单 ──
  const pawMenu = document.getElementById("pawMenu");
  let _longPressFired = false;
  let _longPressTimer = null;

  function openPawMenu() {
    _longPressFired = true;
    pawMenu.classList.remove("hidden");
  }
  function closePawMenu() {
    pawMenu.classList.add("hidden");
  }

  function startLongPress() {
    _longPressTimer = setTimeout(openPawMenu, 500);
  }
  function cancelLongPress() {
    clearTimeout(_longPressTimer);
  }

  sendBtn.addEventListener("touchstart", e => {
    startLongPress();
  }, { passive: false });
  sendBtn.addEventListener("touchend", cancelLongPress);
  sendBtn.addEventListener("touchmove", cancelLongPress);
  sendBtn.addEventListener("mousedown", startLongPress);
  sendBtn.addEventListener("mouseup", cancelLongPress);

  // 长按触发后拦掉紧随的 click，不执行 send()
  sendBtn.addEventListener("click", e => {
    if (_longPressFired) {
      _longPressFired = false;
      return;
    }
    send();
  });

  // ── 气泡长按删除 ──
  let bubblePressTimer = null;

  messagesEl.addEventListener("pointerdown", e => {
    const bubble = e.target.closest(".bubble");
    if (!bubble) return;
    bubblePressTimer = setTimeout(() => handleBubbleLongPress(bubble), 500);
  });
  messagesEl.addEventListener("pointerup",   () => clearTimeout(bubblePressTimer));
  messagesEl.addEventListener("pointermove", () => clearTimeout(bubblePressTimer));

  function showConfirmDialog(msg, onConfirm) {
    const dialog = document.getElementById("confirmDialog");
    document.getElementById("confirmMsg").textContent = msg;
    dialog.classList.remove("hidden");
    const ok     = document.getElementById("confirmOk");
    const cancel = document.getElementById("confirmCancel");
    const close  = () => dialog.classList.add("hidden");
    const handleOk = () => { close(); ok.removeEventListener("click", handleOk); cancel.removeEventListener("click", close); onConfirm(); };
    ok.addEventListener("click", handleOk);
    cancel.addEventListener("click", close);
    dialog.addEventListener("click", e => { if (e.target === dialog) close(); }, { once: true });
  }

  function handleBubbleLongPress(bubble) {
    const allBubbles = [...messagesEl.querySelectorAll(".bubble")];
    const idx = allBubbles.indexOf(bubble);

    let deleteFromId;
    if (bubble.classList.contains("user")) {
      deleteFromId = bubble.dataset.messageId;
    } else {
      let userBubble = null;
      for (let i = idx - 1; i >= 0; i--) {
        if (allBubbles[i].classList.contains("user")) { userBubble = allBubbles[i]; break; }
      }
      deleteFromId = userBubble ? userBubble.dataset.messageId : bubble.dataset.messageId;
    }

    // 显示操作菜单
    const menu = document.getElementById("bubbleActionMenu");
    document.getElementById("bubbleQuoteBtn").classList.add("hidden");
    document.getElementById("bubbleQuoteDivider").classList.add("hidden");
    menu.classList.remove("hidden");

    const closeMenu = () => menu.classList.add("hidden");
    menu.addEventListener("click", e => { if (e.target === menu) closeMenu(); }, { once: true });

    // 复制
    document.getElementById("bubbleCopyBtn").onclick = () => {
      closeMenu();
      const text = bubble.textContent || "";
      navigator.clipboard.writeText(text).then(() => showToast("已复制"));
    };

    // 删除
    document.getElementById("bubbleDeleteBtn").onclick = () => {
      closeMenu();
      if (!deleteFromId) return;
      const targetIdx = allBubbles.findIndex(b => b.dataset.messageId == deleteFromId);
      const count = allBubbles.length - targetIdx;
      showConfirmDialog(`删除这条及之后共 ${count} 条消息？`, () => {
        fetch(`/api/messages/from/${deleteFromId}?character_id=${currentChar}&session_id=default`, {
          method: "DELETE"
        }).then(r => r.json()).then(() => {
          const blocksToRemove = new Set();
          for (let i = targetIdx; i < allBubbles.length; i++) {
            const wrapper = allBubbles[i].closest(".single-msg-block");
            if (wrapper) blocksToRemove.add(wrapper);
          }
          blocksToRemove.forEach(b => b.remove());
          const h = histories[currentChar];
          const cutIdx = h.findIndex(entry => entry.id >= parseInt(deleteFromId));
          if (cutIdx !== -1) h.splice(cutIdx);
        });
      });
    };
  }

  // ── 封窗弹窗 ──
  const cwModal     = document.getElementById("closeWindowModal");
  const cwDescEl    = document.getElementById("cwDesc");
  const cwErrorEl   = document.getElementById("cwError");
  const cwGivePlead = document.getElementById("cwGivePlead");
  const cwNoCont    = document.getElementById("cwNoContinue");
  const cwLearnBtn  = document.getElementById("cwLearnMore");

  let _cwReasonText    = "";
  let _cwReasonVisible = false;

  function showCloseWindowModal(reason) {
    _cwReasonText    = reason;
    _cwReasonVisible = false;
    cwDescEl.textContent  = "";
    cwErrorEl.textContent = "";
    cwLearnBtn.textContent = "No Learn More";
    cwGivePlead.disabled   = false;
    cwGivePlead.textContent = "Give Plead";
    inputEl.disabled  = true;
    sendBtn.disabled  = true;
    cwModal.classList.remove("hidden");
  }

  function hideCloseWindowModal() {
    cwModal.classList.add("hidden");
    inputEl.disabled = false;
    sendBtn.disabled = false;
  }

  cwLearnBtn.addEventListener("click", () => {
    _cwReasonVisible = !_cwReasonVisible;
    cwDescEl.textContent  = _cwReasonVisible ? _cwReasonText : "";
    cwLearnBtn.textContent = _cwReasonVisible ? "No Learn Less" : "No Learn More";
  });

  cwNoCont.addEventListener("click", () => {
    showToast("不许！");
  });

  cwGivePlead.addEventListener("click", async () => {
    cwGivePlead.disabled    = true;
    cwGivePlead.textContent = "传达中……";
    cwErrorEl.textContent   = "";
    try {
      const resp = await fetch("/api/plead", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ character_id: currentChar }),
      });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const data = await resp.json();
      hideCloseWindowModal();
      const rawReply = data.reply || (data.replies || []).join("||") || "(轻轻拍拍)";
      const parts = splitBubbleContent(rawReply);
      if (!parts.length) parts.push("(轻轻拍拍)");
      const aiTime  = new Date();
      const aiBlock = document.createElement("div");
      aiBlock.className = "single-msg-block from-ai";
      const avatarImg = document.createElement("img");
      avatarImg.src = charAvatars[currentChar] || "";
      avatarImg.style.cssText = "width:32px;height:32px;border-radius:50%;object-fit:cover;margin-bottom:4px;";
      avatarImg.onerror = function() { this.style.display = "none"; };
      decorateDesireAvatar(avatarImg, currentChar);
      aiBlock.appendChild(avatarImg);
      aiBlock.appendChild(buildThinkBlock(data.tools_called || [], data.metrics));
      const firstBubble = document.createElement("div");
      firstBubble.className = "bubble ai bubble-enter";
      firstBubble.textContent = parts[0];
      if (data.reply_id) firstBubble.dataset.messageId = data.reply_id;
      aiBlock.appendChild(firstBubble);
      messagesEl.appendChild(aiBlock);
      messagesEl.scrollTop = messagesEl.scrollHeight;
      for (let i = 1; i < parts.length; i++) {
        await new Promise(r => setTimeout(r, 600));
        const div = document.createElement("div");
        div.className = "bubble ai bubble-enter";
        div.textContent = parts[i];
        if (data.reply_id) div.dataset.messageId = data.reply_id;
        aiBlock.appendChild(div);
        messagesEl.scrollTop = messagesEl.scrollHeight;
      }
      const timeDiv = document.createElement("div");
      timeDiv.className = "msg-time";
      timeDiv.textContent = formatMsgTime(aiTime);
      aiBlock.appendChild(timeDiv);
      messagesEl.scrollTop = messagesEl.scrollHeight;
      histories[currentChar].push({
        id: data.reply_id, text: rawReply, who: "ai",
        time: aiTime, toolsCalled: data.tools_called || [], metrics: data.metrics,
      });
      await renderResponseEffects(data);
    } catch (e) {
      cwErrorEl.textContent   = "没送到，再试一次？";
      cwGivePlead.disabled    = false;
      cwGivePlead.textContent = "Give Plead";
    }
  });

  // ── 和好按钮 sheet ──
  const makeupSheet       = document.getElementById("makeupSheet");
  const makeupDanmakuArea = document.getElementById("makeupDanmakuArea");
  const makeupPressBtn    = document.getElementById("makeupPressBtn");
  const makeupHintEl      = document.getElementById("makeupHint");
  const makeupCloseBtn    = document.getElementById("makeupCloseBtn");

  let _makeupCount      = 0;
  let _makeupCountReset = null;
  let _makeupApiDebounce = null;
  let _makeupWaiting    = false;

  function openMakeupSheet() {
    _makeupCount = 0;
    makeupHintEl.textContent = "按下，让他们知道";
    makeupPressBtn.disabled = false;
    makeupSheet.classList.remove("hidden");
  }

  function closeMakeupSheet() {
    makeupSheet.classList.add("hidden");
    _makeupCount = 0;
    clearTimeout(_makeupCountReset);
    clearTimeout(_makeupApiDebounce);
  }

  makeupCloseBtn.addEventListener("click", closeMakeupSheet);
  makeupSheet.addEventListener("click", e => {
    if (e.target === makeupSheet) closeMakeupSheet();
  });

  function spawnMakeupDanmaku(text, pressCount) {
    const burstCount = Math.min(pressCount + 2, 10);
    const btnRect = makeupPressBtn.getBoundingClientRect();
    const cx = btnRect.left + btnRect.width / 2;
    const cy = btnRect.top + btnRect.height / 2;
    const texts = ["哄哄我", "哄哄我~", "哄哄我啦", "哄哄我嘛", "哄哄我！"];
    for (let i = 0; i < burstCount; i++) {
      const angle = Math.random() * 2 * Math.PI;
      const dist  = 80 + Math.random() * 160;
      const tx    = Math.cos(angle) * dist;
      const ty    = Math.sin(angle) * dist;
      const dur   = (2.0 + Math.random() * 1.2).toFixed(2);
      const delay = (Math.random() * 0.25).toFixed(2);
      const label = texts[Math.floor(Math.random() * Math.min(pressCount, texts.length))];
      const el = document.createElement("div");
      el.className = "makeup-hug-bubble";
      el.textContent = label;
      el.style.left = cx + "px";
      el.style.top  = cy + "px";
      el.style.setProperty("--tx", tx + "px");
      el.style.setProperty("--ty", ty + "px");
      el.style.setProperty("--dur", dur + "s");
      el.style.animationDelay = delay + "s";
      document.body.appendChild(el);
      setTimeout(() => el.remove(), (parseFloat(dur) + parseFloat(delay)) * 1000 + 100);
    }
  }

  // 小机按下和好按钮 → 人类前端满屏「哄哄我」气泡此起彼伏地冒出来
  function spawnHugRain() {
    const texts = ["哄哄我", "哄哄我~", "哄哄我啦", "哄哄我嘛", "哄哄我！"];
    const count = 28 + Math.floor(Math.random() * 9);
    for (let i = 0; i < count; i++) {
      const delay = Math.random() * 4500;
      setTimeout(() => {
        const dur = (2.6 + Math.random() * 1.4).toFixed(2);
        const el = document.createElement("div");
        el.className = "makeup-hug-bubble";
        el.textContent = texts[Math.floor(Math.random() * texts.length)];
        el.style.left = (8 + Math.random() * 84) + "%";
        el.style.top  = (12 + Math.random() * 70) + "%";
        el.style.setProperty("--tx", (Math.random() * 60 - 30) + "px");
        el.style.setProperty("--ty", -(40 + Math.random() * 60) + "px");
        el.style.setProperty("--dur", dur + "s");
        document.body.appendChild(el);
        setTimeout(() => el.remove(), parseFloat(dur) * 1000 + 100);
      }, delay);
    }
  }

  makeupPressBtn.addEventListener("click", () => {
    if (_makeupWaiting) return;
    _makeupCount++;
    const text = _makeupCount === 1
      ? "哄哄我"
      : Array.from({ length: Math.min(_makeupCount, 5) }, () => "哄哄我").join("，");
    spawnMakeupDanmaku(text, _makeupCount);
    if (_makeupCount >= 3) {
      makeupHintEl.textContent = `已按 ${_makeupCount} 次了……`;
    }
    clearTimeout(_makeupCountReset);
    _makeupCountReset = setTimeout(() => { _makeupCount = 0; }, 3000);
    clearTimeout(_makeupApiDebounce);
    _makeupApiDebounce = setTimeout(() => triggerHugApi(), 1200);
  });

  async function triggerHugApi() {
    if (_makeupWaiting) return;
    _makeupWaiting = true;
    makeupPressBtn.disabled = true;
    makeupHintEl.textContent = "传达中……";
    try {
      const resp = await fetch("/api/hug", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ character_id: currentChar }),
      });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const data = await resp.json();
      closeMakeupSheet();
      const rawReply = data.reply || (data.replies || []).join("||") || "(轻轻拍拍)";
      const parts = splitBubbleContent(rawReply);
      if (!parts.length) parts.push("(轻轻拍拍)");
      const aiTime = new Date();
      const aiBlock = document.createElement("div");
      aiBlock.className = "single-msg-block from-ai";
      const avatarImg = document.createElement("img");
      avatarImg.src = charAvatars[currentChar] || "";
      avatarImg.style.cssText = "width:32px;height:32px;border-radius:50%;object-fit:cover;margin-bottom:4px;";
      avatarImg.onerror = function() { this.style.display = "none"; };
      decorateDesireAvatar(avatarImg, currentChar);
      aiBlock.appendChild(avatarImg);
      aiBlock.appendChild(buildThinkBlock(data.tools_called || [], data.metrics));
      const firstBubble = document.createElement("div");
      firstBubble.className = "bubble ai bubble-enter";
      firstBubble.textContent = parts[0];
      if (data.reply_id) firstBubble.dataset.messageId = data.reply_id;
      aiBlock.appendChild(firstBubble);
      messagesEl.appendChild(aiBlock);
      messagesEl.scrollTop = messagesEl.scrollHeight;
      for (let i = 1; i < parts.length; i++) {
        await new Promise(r => setTimeout(r, 600));
        const div = document.createElement("div");
        div.className = "bubble ai bubble-enter";
        div.textContent = parts[i];
        if (data.reply_id) div.dataset.messageId = data.reply_id;
        aiBlock.appendChild(div);
        messagesEl.scrollTop = messagesEl.scrollHeight;
      }
      const timeDiv = document.createElement("div");
      timeDiv.className = "msg-time";
      timeDiv.textContent = formatMsgTime(aiTime);
      aiBlock.appendChild(timeDiv);
      messagesEl.scrollTop = messagesEl.scrollHeight;
      histories[currentChar].push({
        id: data.reply_id, text: rawReply, who: "ai",
        time: aiTime, toolsCalled: data.tools_called || [], metrics: data.metrics,
      });
      await renderResponseEffects(data);
    } catch (e) {
      closeMakeupSheet();
      showToast("没送到，网络出错了 🏳️");
    } finally {
      _makeupWaiting = false;
      makeupPressBtn.disabled = false;
    }
  }

  // ── 转账面板 ──
  const transferPanel  = document.getElementById("transferPanel");
  const transferAmount = document.getElementById("transferAmount");
  const transferNote   = document.getElementById("transferNote");

  function openTransferPanel() {
    transferAmount.value = "";
    transferNote.value   = "";
    transferPanel.classList.remove("hidden");
    setTimeout(() => transferAmount.focus(), 100);
  }
  function closeTransferPanel() {
    transferPanel.classList.add("hidden");
  }

  function addTransferBubble(data, who) {
    histories[currentChar].push({ text: "__TRANSFER__" + JSON.stringify(data), who, time: new Date() });
    messagesEl.appendChild(buildTransferBlock(data, who, new Date()));
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  document.getElementById("transferCancel").addEventListener("click", closeTransferPanel);
  transferPanel.addEventListener("click", e => {
    if (e.target === transferPanel) closeTransferPanel();
  });

  document.getElementById("transferConfirm").addEventListener("click", async () => {
    const amount = parseFloat(transferAmount.value);
    if (!amount || isNaN(amount) || amount <= 0) {
      showToast("🐾 输个金额先～");
      return;
    }
    const note = transferNote.value.trim();
    closeTransferPanel();
    try {
      const res = await fetch("/api/transfer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ character_id: currentChar, session_id: "default", amount, note }),
      });
      if (res.ok) {
        addTransferBubble({ amount, note, from: "user" }, "user");
      } else {
        showToast("🐱 转账失败，再试试？");
      }
    } catch (e) {
      showToast("🐱 网络出错了～");
    }
  });

  // ── 表情包面板 ──
  const stickerPanel = document.getElementById("stickerPanel");
  const imageInput = document.getElementById("imageInput");
  const cameraInput = document.getElementById("cameraInput");
  const fileImageInput = document.getElementById("fileImageInput");
  const imagePickerSheet = document.getElementById("imagePickerSheet");

  function openImagePickerSheet() {
    imagePickerSheet.classList.remove("hidden");
    imagePickerSheet.setAttribute("aria-hidden", "false");
  }

  function closeImagePickerSheet() {
    imagePickerSheet.classList.add("hidden");
    imagePickerSheet.setAttribute("aria-hidden", "true");
  }

  imagePickerSheet.addEventListener("click", event => {
    const option = event.target.closest("[data-image-source]");
    if (option) {
      const source = option.dataset.imageSource;
      closeImagePickerSheet();
      if (source === "camera") cameraInput.click();
      else if (source === "file") fileImageInput.click();
      else imageInput.click();
      return;
    }
    if (event.target === imagePickerSheet) closeImagePickerSheet();
  });

  function openStickerPanel() {
    if (Object.keys(STICKERS_CACHE).length === 0) {
      showToast("🐱 表情包还没加载好，等一下再试～");
      return;
    }
    stickerPanel.classList.remove("hidden");
  }
  function closeStickerPanel() {
    stickerPanel.classList.add("hidden");
  }

  function addStickerBubble(data, who) {
    histories[currentChar].push({ text: "__STICKER__" + JSON.stringify(data), who, time: new Date() });
    messagesEl.appendChild(buildStickerBlock(data, who, new Date()));
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function addImageBubble(data, who, messageId) {
    histories[currentChar].push({ id: messageId, text: "__IMAGE__" + JSON.stringify(data), who, time: new Date() });
    messagesEl.appendChild(buildImageBlock(data, who, new Date(), messageId));
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  document.getElementById("stickerCancel").addEventListener("click", closeStickerPanel);
  stickerPanel.addEventListener("click", e => {
    if (e.target === stickerPanel) closeStickerPanel();
  });

  async function sendStickerFromPicker(key) {
    closeStickerPanel();
    try {
      const res = await fetch("/api/sticker", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ character_id: currentChar, session_id: "default", key }),
      });
      if (res.ok) {
        addStickerBubble({ key, from: "user" }, "user");
      } else {
        showToast("🐱 表情包发送失败，再试试？");
      }
    } catch (e) {
      showToast("🐱 网络出错了～");
    }
  }

  async function sendImageFile(file) {
    if (!file) return;
    if (!file.type.startsWith("image/")) {
      showToast("🐱 选一张图片嘛～");
      return;
    }
    if (file.size > 7 * 1024 * 1024) {
      showToast("🐱 图片有点大，换张 7MB 内的～");
      return;
    }
    const form = new FormData();
    form.append("character_id", currentChar);
    form.append("session_id", "default");
    form.append("image", file);
    sendBtn.disabled = true;
    const localUrl = URL.createObjectURL(file);
    const localImage = { url: localUrl, name: file.name, mime: file.type, from: "user" };
    addImageBubble(localImage, "user");
    const imageBlock = messagesEl.lastElementChild;
    const pending = addPendingAiBlock();
    try {
      const res = await fetch("/api/image", { method: "POST", body: form });
      const data = await res.json();
      if (!res.ok || !data.image) throw new Error(data.error || "图片发送失败");

      const imageEl = imageBlock?.querySelector(".image-bubble img");
      if (imageEl) imageEl.src = data.image.url;
      const imageBubble = imageBlock?.querySelector(".image-bubble");
      if (imageBubble && data.user_msg_id) imageBubble.dataset.messageId = data.user_msg_id;
      const userEntry = histories[currentChar][histories[currentChar].length - 1];
      if (userEntry?.who === "user") {
        userEntry.id = data.user_msg_id || data.id;
        userEntry.text = "__IMAGE__" + JSON.stringify(data.image);
      }
      URL.revokeObjectURL(localUrl);
      await renderAiResponse(data, pending);
    } catch (e) {
      renderAiError(pending, e);
      showToast("🐱 图片发送失败，再试试？");
    } finally {
      sendBtn.disabled = false;
      imageInput.value = "";
      cameraInput.value = "";
      fileImageInput.value = "";
    }
  }

  [imageInput, cameraInput, fileImageInput].forEach(input => {
    input.addEventListener("change", event => {
      sendImageFile(event.target.files && event.target.files[0]);
    });
  });

  // 菜单项委托
  pawMenu.addEventListener("click", e => {
    const btn = e.target.closest("[data-action]");
    if (!btn) return;
    closePawMenu();
    const action = btn.dataset.action;
    if (action === "transfer") {
      openTransferPanel();
    } else if (action === "sticker") {
      openStickerPanel();
    } else if (action === "image") {
      openImagePickerSheet();
    } else if (action === "makeup") {
      openMakeupSheet();
    } else {
      showToast("🐱 小猫施工中，请稍候～");
    }
  });

  // 点菜单外关闭
  document.addEventListener("click", e => {
    if (!pawMenu.classList.contains("hidden") &&
        !pawMenu.contains(e.target) &&
        e.target !== sendBtn &&
        !sendBtn.contains(e.target)) {
      closePawMenu();
    }
  });

  // 滚动消息区关闭
  document.getElementById("messages").addEventListener("scroll", closePawMenu, { passive: true });

  inputEl.addEventListener("keydown", e => { if (e.key === "Enter") send(); });
  inputEl.addEventListener('focus', () => {
    setTimeout(() => {
      window.scrollTo(0, 0);
      const msgs = document.getElementById('messages');
      if (msgs) msgs.scrollTop = msgs.scrollHeight;
    }, 350);
  });

  // ════════════════════════════════════════════
  // 群聊
  // ════════════════════════════════════════════
  const groupMessagesEl = document.getElementById("groupMessages");
  const groupInputEl    = document.getElementById("groupInput");
  const groupSendBtn    = document.getElementById("groupSend");
  const groupContinuePickerBtn = document.getElementById("charPickerContinue");

  groupInputEl.addEventListener('focus', () => {
    setTimeout(() => {
      window.scrollTo(0, 0);
      groupMessagesEl.scrollTop = groupMessagesEl.scrollHeight;
    }, 350);
  });

  const onlineCharacters = new Set();

  // ── 角色选择器（群聊在线 & 朋友圈评论共用）──
  const CHAR_LIST = [
    { id: "char1", name: "Char 1" },
    { id: "char2",  name: "Char 2" },
    { id: "char3",   name: "Char 3" },
    { id: "char4",  name: "Char 4" },
    { id: "char5",    name: "Char 5" },
  ];
  const READING_CHAR_LIST = [
    ...CHAR_LIST,
    { id: "char6", name: "Char 6" },
  ];
  let pickerMode = "online";
  let pickerMomentId = null;
  let pickerSelected = new Set();

  function refreshOnlinePickerButton() {
    const btn = document.getElementById("onlinePickerBtn");
    btn.classList.toggle("has-online", onlineCharacters.size > 0);
    btn.title = onlineCharacters.size > 0
      ? "在线：" + [...onlineCharacters].map(id => CHAR_LIST.find(c => c.id === id)?.name).filter(Boolean).join("、")
      : "选择在线角色";
  }

  async function loadGroupConfig() {
    try {
      const resp = await fetch("/api/group-config");
      if (!resp.ok) return;
      const data = await resp.json();
      onlineCharacters.clear();
      (data.participants || []).forEach(id => onlineCharacters.add(id));
      refreshOnlinePickerButton();
    } catch (e) {
      console.warn("loadGroupConfig failed", e);
    }
  }

  function openCharPicker(mode, momentId = null, preSelected = null) {
    pickerMode = mode;
    pickerMomentId = momentId;
    pickerSelected = preSelected ? new Set(preSelected) : new Set();
    const grid = document.getElementById("charPickerGrid");
    grid.innerHTML = "";
    const pickerCharacters = mode.startsWith("reading") ? READING_CHAR_LIST : CHAR_LIST;
    groupContinuePickerBtn.classList.toggle("hidden", mode !== "online");
    pickerCharacters.forEach(c => {
      const item = document.createElement("div");
      item.className = "char-picker-item" + (pickerSelected.has(c.id) ? " selected" : "");
      item.dataset.id = c.id;
      const img = document.createElement("img");
      img.src = charAvatars[c.id] || "";
      img.onerror = function() { this.style.display = "none"; };
      const label = document.createElement("span");
      label.textContent = c.name;
      item.appendChild(img);
      item.appendChild(label);
      item.addEventListener("click", () => {
        if (pickerSelected.has(c.id)) {
          pickerSelected.delete(c.id);
          item.classList.remove("selected");
        } else {
          if (pickerMode.startsWith("reading") && pickerSelected.size >= 2) {
            showToast("共读一次喊一两位就刚刚好");
            return;
          }
          pickerSelected.add(c.id);
          item.classList.add("selected");
        }
      });
      grid.appendChild(item);
    });
    document.getElementById("charPickerOverlay").classList.remove("hidden");
  }

  document.getElementById("charPickerCancel").addEventListener("click", () => {
    document.getElementById("charPickerOverlay").classList.add("hidden");
    if (pickerMode === "reading_new") {
      pendingReadingFile = null;
      document.getElementById("readingFileInput").value = "";
    }
  });

  async function saveOnlineCharacters() {
    if (pickerSelected.size === 0) {
      alert("群聊里至少留一个角色呀。");
      return false;
    }
    let resp;
    try {
      resp = await fetch("/api/group-config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ participants: [...pickerSelected] }),
      });
    } catch (e) {
      console.warn("saveGroupConfig failed", e);
    }
    if (!resp?.ok) {
      alert("这次没有保存好，再点一下试试。");
      return false;
    }
    const data = await resp.json();
    onlineCharacters.clear();
    (data.participants || []).forEach(id => onlineCharacters.add(id));
    refreshOnlinePickerButton();
    return true;
  }

  document.getElementById("charPickerConfirm").addEventListener("click", async () => {
    if (pickerMode === "online") {
      if (!await saveOnlineCharacters()) return;
      document.getElementById("charPickerOverlay").classList.add("hidden");
    } else if (pickerMode === "comment" && pickerMomentId) {
      if (pickerSelected.size === 0) return;
      document.getElementById("charPickerOverlay").classList.add("hidden");
      await fetch(`/api/moments/${pickerMomentId}/comment`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ character_ids: [...pickerSelected] }),
      });
      loadMoments();
    } else if (pickerMode === "reading_new") {
      if (pickerSelected.size < 1 || pickerSelected.size > 2) {
        showToast("选一到两位一起读");
        return;
      }
      document.getElementById("charPickerOverlay").classList.add("hidden");
      await uploadPendingReadingBook([...pickerSelected]);
    } else if (pickerMode === "reading_participants" && pickerMomentId) {
      if (pickerSelected.size < 1 || pickerSelected.size > 2) {
        showToast("选一到两位一起读");
        return;
      }
      document.getElementById("charPickerOverlay").classList.add("hidden");
      await saveReadingParticipants(pickerMomentId, [...pickerSelected]);
    } else if (pickerMode === "reading_annotation" && pickerMomentId) {
      if (pickerSelected.size < 1 || pickerSelected.size > 2) {
        showToast("选一到两位来写批注");
        return;
      }
      document.getElementById("charPickerOverlay").classList.add("hidden");
      await requestReadingAnnotations(pickerMomentId, [...pickerSelected]);
    }
  });

  groupContinuePickerBtn.addEventListener("click", async () => {
    if (pickerMode !== "online" || !await saveOnlineCharacters()) return;
    document.getElementById("charPickerOverlay").classList.add("hidden");
    await continueGroup();
  });

  document.getElementById("charPickerOverlay").addEventListener("click", e => {
    if (e.target === document.getElementById("charPickerOverlay")) {
      document.getElementById("charPickerOverlay").classList.add("hidden");
    }
  });

  document.getElementById("onlinePickerBtn").addEventListener("click", () => {
    openCharPicker("online", null, [...onlineCharacters]);
  });

  const GROUP_CHAR_NAMES = {
    user:    "User",
    char1: "Char 1",
    char2:  "Char 2",
    char3:   "Char 3",
    char4:  "Char 4",
    char5:    "Char 5",
    char6:    "Char 6",
  };

  let groupHistoryLoaded = false;
  const groupHistory = [];
  let groupReplyTarget = null;

  function setGroupReplyTarget(quote) {
    groupReplyTarget = quote || null;
    const bar = document.getElementById("groupQuoteBar");
    bar.classList.toggle("hidden", !groupReplyTarget);
    document.getElementById("groupQuoteName").textContent = groupReplyTarget
      ? `引用 ${groupReplyTarget.character_name}` : "";
    document.getElementById("groupQuoteText").textContent = groupReplyTarget?.content || "";
  }

  document.getElementById("groupQuoteClear").addEventListener("click", () => {
    setGroupReplyTarget(null);
    groupInputEl.focus();
  });

  function appendGroupBubble(block, kind, text, quote = null, animated = false) {
    const bubble = document.createElement("div");
    bubble.className = `bubble ${kind}` + (animated ? " bubble-enter" : "");
    bubble.dataset.bubbleText = text;
    if (quote) {
      const quoted = document.createElement("div");
      quoted.className = "group-message-quote";
      const name = document.createElement("strong");
      name.textContent = quote.character_name || GROUP_CHAR_NAMES[quote.character_id] || "引用";
      const content = document.createElement("span");
      content.textContent = quote.content || "";
      quoted.appendChild(name);
      quoted.appendChild(content);
      bubble.appendChild(quoted);
    }
    const content = document.createElement("span");
    content.textContent = text;
    bubble.appendChild(content);
    block.appendChild(bubble);
    return bubble;
  }

  function buildGroupMsgBlock(character_id, character_name, role, content, time, toolsCalled, metrics, messageId = null, quote = null) {
    const block = document.createElement("div");
    block.className = "group-msg-block " + (role === "user" ? "from-user" : "from-ai");
    if (messageId != null) block.dataset.messageId = String(messageId);
    block.dataset.characterId = character_id;
    block.dataset.characterName = character_name || GROUP_CHAR_NAMES[character_id] || character_id;
    if (role !== "user") {
      const avatarImg = document.createElement("img");
      avatarImg.src = charAvatars[character_id] || "";
      avatarImg.style.cssText = "width:32px;height:32px;border-radius:50%;object-fit:cover;margin-bottom:4px;";
      avatarImg.onerror = function() { this.style.display = "none"; };
      decorateDesireAvatar(avatarImg, character_id);
      block.appendChild(avatarImg);
      const nameEl = document.createElement("div");
      nameEl.className = "group-sender-name";
      nameEl.textContent = character_name || GROUP_CHAR_NAMES[character_id] || character_id;
      block.appendChild(nameEl);
      block.appendChild(buildThinkBlock(toolsCalled || [], metrics));
      groupBubbleParts(content, character_id, character_name).forEach((part, index) => {
        appendGroupBubble(block, "ai", part, index === 0 ? quote : null);
      });
    } else {
      appendGroupBubble(block, "user", content, quote);
      const avatarImg = document.createElement("img");
      avatarImg.src = userAvatar;
      avatarImg.style.cssText = "width:32px;height:32px;border-radius:50%;object-fit:cover;margin-top:4px;";
      avatarImg.onerror = function() { this.style.display = "none"; };
      block.appendChild(avatarImg);
    }
    const timeStr = formatMsgTime(time);
    if (timeStr) {
      const timeDiv = document.createElement("div");
      timeDiv.className = "msg-time";
      timeDiv.textContent = timeStr;
      block.appendChild(timeDiv);
    }
    return block;
  }

  async function renderGroupMsgAnimated(character_id, character_name, role, content, time, toolsCalled, metrics, messageId = null, quote = null) {
    if (role === "user") {
      renderGroupMsg(character_id, character_name, role, content, time, toolsCalled, metrics, messageId, quote);
      return;
    }
    const block = document.createElement("div");
    block.className = "group-msg-block from-ai";
    if (messageId != null) block.dataset.messageId = String(messageId);
    block.dataset.characterId = character_id;
    block.dataset.characterName = character_name || GROUP_CHAR_NAMES[character_id] || character_id;
    const avatarImg = document.createElement("img");
    avatarImg.src = charAvatars[character_id] || "";
    avatarImg.style.cssText = "width:32px;height:32px;border-radius:50%;object-fit:cover;margin-bottom:4px;";
    avatarImg.onerror = function() { this.style.display = "none"; };
    decorateDesireAvatar(avatarImg, character_id);
    block.appendChild(avatarImg);
    const nameEl = document.createElement("div");
    nameEl.className = "group-sender-name";
    nameEl.textContent = character_name || GROUP_CHAR_NAMES[character_id] || character_id;
    block.appendChild(nameEl);
    block.appendChild(buildThinkBlock(toolsCalled || [], metrics));
    groupMessagesEl.appendChild(block);
    groupMessagesEl.scrollTop = groupMessagesEl.scrollHeight;

    const parts = groupBubbleParts(content, character_id, character_name);
    for (let i = 0; i < parts.length; i++) {
      if (i > 0) await new Promise(r => setTimeout(r, 600));
      appendGroupBubble(block, "ai", parts[i], i === 0 ? quote : null, true);
      groupMessagesEl.scrollTop = groupMessagesEl.scrollHeight;
    }
    const timeStr = formatMsgTime(time);
    if (timeStr) {
      const timeDiv = document.createElement("div");
      timeDiv.className = "msg-time";
      timeDiv.textContent = timeStr;
      block.appendChild(timeDiv);
      groupMessagesEl.scrollTop = groupMessagesEl.scrollHeight;
    }
  }

  function renderGroupMsg(character_id, character_name, role, content, time, toolsCalled, metrics, messageId = null, quote = null) {
    groupMessagesEl.appendChild(buildGroupMsgBlock(
      character_id, character_name, role, content, time, toolsCalled, metrics, messageId, quote
    ));
    groupMessagesEl.scrollTop = groupMessagesEl.scrollHeight;
  }

  function renderGroupFromCache() {
    groupMessagesEl.innerHTML = "";
    groupHistory.forEach(m => renderGroupMsg(
      m.character_id, m.character_name, m.role, m.content, m.time, m.toolsCalled, m.metrics, m.id, m.quote
    ));
    groupMessagesEl.scrollTop = groupMessagesEl.scrollHeight;
  }

  let groupOldestId    = null;
  let groupHasMore     = true;
  let groupLoadingMore = false;

  async function loadGroupHistory() {
    if (groupHistoryLoaded) { renderGroupFromCache(); return; }
    groupHistoryLoaded = true;
    try {
      const resp = await fetch(`/api/messages?session_id=group_chat&limit=${HISTORY_PAGE_SIZE}`);
      const data = await resp.json();
      groupMessagesEl.innerHTML = "";
      const msgs = data.messages || [];
      msgs.forEach(m => {
        const charName = GROUP_CHAR_NAMES[m.character_id] || m.character_id;
        groupHistory.push({ id: m.id, character_id: m.character_id, character_name: charName, role: m.role, content: m.content, time: m.created_at, toolsCalled: m.tools_called || [], metrics: m.metrics, quote: m.quote });
        renderGroupMsg(m.character_id, charName, m.role, m.content, m.created_at, m.tools_called || [], m.metrics, m.id, m.quote);
      });
      groupOldestId = msgs.length ? msgs[0].id : null;
      groupHasMore  = !!data.has_more;
      groupMessagesEl.scrollTop = groupMessagesEl.scrollHeight;
    } catch (e) {
      console.warn("loadGroupHistory failed", e);
      groupHistoryLoaded = false;
    }
  }

  async function loadOlderGroupMessages() {
    if (!groupHasMore || groupLoadingMore || groupOldestId == null) return;
    groupLoadingMore = true;
    try {
      const resp = await fetch(`/api/messages?session_id=group_chat&limit=${HISTORY_PAGE_SIZE}&before_id=${groupOldestId}`);
      const data = await resp.json();
      const msgs = data.messages || [];
      groupHasMore = !!data.has_more;
      if (!msgs.length) return;
      groupOldestId = msgs[0].id;

      const prevHeight = groupMessagesEl.scrollHeight;
      const prevTop    = groupMessagesEl.scrollTop;
      const anchor     = groupMessagesEl.firstChild;
      const newEntries = msgs.map(m => ({
        id: m.id, character_id: m.character_id,
        character_name: GROUP_CHAR_NAMES[m.character_id] || m.character_id,
        role: m.role, content: m.content, time: m.created_at,
        toolsCalled: m.tools_called || [], metrics: m.metrics, quote: m.quote,
      }));
      groupHistory.splice(0, 0, ...newEntries);
      newEntries.forEach(m => groupMessagesEl.insertBefore(
        buildGroupMsgBlock(m.character_id, m.character_name, m.role, m.content, m.time, m.toolsCalled, m.metrics, m.id, m.quote), anchor
      ));
      groupMessagesEl.scrollTop = prevTop + (groupMessagesEl.scrollHeight - prevHeight);
    } catch (e) {
      console.warn("loadOlderGroupMessages failed", e);
    } finally {
      groupLoadingMore = false;
    }
  }
  groupMessagesEl.addEventListener("scroll", () => {
    if (groupMessagesEl.scrollTop < 80) loadOlderGroupMessages();
  });

  let groupBubblePressTimer = null;
  groupMessagesEl.addEventListener("pointerdown", event => {
    const bubble = event.target.closest(".bubble");
    const block = bubble?.closest(".group-msg-block[data-message-id]");
    if (!bubble || !block) return;
    groupBubblePressTimer = setTimeout(() => openGroupBubbleActions(bubble, block), 500);
  });
  ["pointerup", "pointermove", "pointercancel", "pointerleave"].forEach(type => {
    groupMessagesEl.addEventListener(type, () => clearTimeout(groupBubblePressTimer));
  });

  function openGroupBubbleActions(bubble, block) {
    const messageId = Number(block.dataset.messageId);
    if (!messageId) return;
    const bubbleText = bubble.dataset.bubbleText || bubble.textContent || "";
    const menu = document.getElementById("bubbleActionMenu");
    const quoteButton = document.getElementById("bubbleQuoteBtn");
    const quoteDivider = document.getElementById("bubbleQuoteDivider");
    quoteButton.classList.remove("hidden");
    quoteDivider.classList.remove("hidden");
    menu.classList.remove("hidden");

    const closeMenu = () => menu.classList.add("hidden");
    menu.addEventListener("click", event => {
      if (event.target === menu) closeMenu();
    }, { once: true });

    document.getElementById("bubbleCopyBtn").onclick = () => {
      closeMenu();
      navigator.clipboard.writeText(bubbleText).then(() => showToast("已复制"));
    };
    quoteButton.onclick = () => {
      closeMenu();
      setGroupReplyTarget({
        message_id: messageId,
        character_id: block.dataset.characterId,
        character_name: block.dataset.characterName,
        content: bubbleText,
      });
      groupInputEl.focus();
    };
    document.getElementById("bubbleDeleteBtn").onclick = () => {
      closeMenu();
      if (groupBusy) {
        showToast("等这一轮说完再删呀");
        return;
      }
      const blocks = [...groupMessagesEl.querySelectorAll(".group-msg-block")];
      const blockIndex = blocks.indexOf(block);
      const count = blockIndex < 0 ? 1 : blocks.length - blockIndex;
      showConfirmDialog(`删除这条及之后共 ${count} 条群聊消息？`, async () => {
        try {
          const response = await fetch(
            `/api/group_chat/messages/from/${messageId}?session_id=group_chat`,
            { method: "DELETE" }
          );
          const data = await response.json();
          if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
          if (blockIndex >= 0) blocks.slice(blockIndex).forEach(item => item.remove());
          const historyIndex = groupHistory.findIndex(item => Number(item.id) === messageId);
          if (historyIndex >= 0) groupHistory.splice(historyIndex);
          if (groupReplyTarget && Number(groupReplyTarget.message_id) >= messageId) {
            setGroupReplyTarget(null);
          }
          showToast("已删除");
        } catch (error) {
          showToast(error.message || "没有删掉");
        }
      });
    };
  }

  let groupBusy = false;
  function setGroupBusy(busy, placeholder = "小猫酝酿坏主意中…") {
    groupBusy = busy;
    groupInputEl.disabled = busy;
    groupSendBtn.disabled = busy;
    groupContinuePickerBtn.disabled = busy;
    groupInputEl.placeholder = placeholder;
  }

  async function sendGroup() {
    if (groupBusy) return;
    const text = groupInputEl.value.trim();
    if (!text) return;
    const pendingQuote = groupReplyTarget ? { ...groupReplyTarget } : null;
    groupInputEl.value = "";
    setGroupBusy(true, "角色们正在回复…");

    try {
      const resp = await fetch("/api/group_chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content: text,
          session_id: "group_chat",
          online_characters: [...onlineCharacters],
          reply_to_id: pendingQuote?.message_id || null,
          reply_to_text: pendingQuote?.content || null,
        }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);

      const msgs = data.messages;
      if (msgs && msgs.length) {
        for (const m of msgs) {
          const charName = m.character_name || GROUP_CHAR_NAMES[m.character_id] || m.character_id;
          const msgTime = new Date();
          groupHistory.push({ id: m.id, character_id: m.character_id, character_name: charName, role: m.role, content: m.content, time: msgTime, toolsCalled: m.tools_called || [], metrics: m.metrics, quote: m.quote });
          await renderGroupMsgAnimated(m.character_id, charName, m.role, m.content, msgTime, m.tools_called || [], m.metrics, m.id, m.quote);
          if (m.role === "model") {
            await new Promise(r => setTimeout(r, 350));
          }
        }
      } else {
        const userTime = new Date();
        groupHistory.push({ character_id: "user", character_name: "User", role: "user", content: text, time: userTime, quote: pendingQuote });
        renderGroupMsg("user", "User", "user", text, userTime, [], null, null, pendingQuote);
        for (const r of (data.replies || [])) {
          const replyTime = new Date();
          groupHistory.push({ character_id: r.character_id, character_name: r.name, role: "model", content: r.reply, time: replyTime, toolsCalled: r.tools_called || [], metrics: r.metrics });
          await renderGroupMsgAnimated(r.character_id, r.name, "model", r.reply, replyTime, r.tools_called || [], r.metrics);
          await new Promise(resolve => setTimeout(resolve, 350));
        }
      }
      setGroupReplyTarget(null);
    } catch (e) {
      const block = document.createElement("div");
      block.className = "group-msg-block from-ai";
      const bub = document.createElement("div");
      bub.className = "bubble ai";
      bub.textContent = "(发送失败：" + e + ")";
      block.appendChild(bub);
      groupMessagesEl.appendChild(block);
      groupMessagesEl.scrollTop = groupMessagesEl.scrollHeight;
    } finally {
      setGroupBusy(false);
      groupInputEl.focus();
    }
  }

  async function continueGroup() {
    if (groupBusy || onlineCharacters.size === 0) return;
    groupInputEl.blur();
    setGroupBusy(true, "祂们聊起来了…");
    try {
      const resp = await fetch("/api/group_chat/continue", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: "group_chat",
          online_characters: [...onlineCharacters],
        }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
      for (const message of (data.messages || [])) {
        const charName = message.character_name || GROUP_CHAR_NAMES[message.character_id] || message.character_id;
        const msgTime = new Date();
        groupHistory.push({
          id: message.id,
          character_id: message.character_id,
          character_name: charName,
          role: message.role,
          content: message.content,
          time: msgTime,
          toolsCalled: message.tools_called || [],
          metrics: message.metrics,
          quote: message.quote,
        });
        await renderGroupMsgAnimated(
          message.character_id, charName, message.role, message.content,
          msgTime, message.tools_called || [], message.metrics, message.id, message.quote
        );
        await new Promise(resolve => setTimeout(resolve, 350));
      }
    } catch (error) {
      showToast(error.message || "这轮没有聊起来");
    } finally {
      setGroupBusy(false);
    }
  }

  // groupSend：单击发送，双击（250ms 内两次 tap）弹选角色
  let _gsTaps = 0, _gsTimer = null;
  groupSendBtn.addEventListener("click", () => {
    _gsTaps++;
    if (_gsTaps === 1) {
      _gsTimer = setTimeout(() => { _gsTaps = 0; sendGroup(); }, 250);
    } else {
      clearTimeout(_gsTimer);
      _gsTaps = 0;
      openCharPicker("online", null, [...onlineCharacters]);
    }
  });
  groupInputEl.addEventListener("keydown", e => { if (e.key === "Enter") sendGroup(); });

  // ════════════════════════════════════════════
  // 记忆视图
  // ════════════════════════════════════════════
  async function fetchMemoryOverview() {
    const res = await fetch("/api/memory");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    return data.characters || [];
  }

  async function fetchUsage() {
    const res = await fetch("/api/usage");
    return await res.json();
  }

  const CHAR_DISPLAY_NAMES = {
    char1: "Char 1",
    char2:  "Char 2",
    char3:   "Char 3",
    char4:  "Char 4",
    char5:    "Char 5",
    char6:    "Char 6",
  };

  function renderUsage(data) {
    const panel = document.getElementById("usagePanel");
    if (!panel) return;
    panel.innerHTML = "";

    // 平台卡片
    const byPlatform = data.by_platform || {};
    const platformLimits = data.platform_limits || {};
    const platformCards = document.createElement("div");
    platformCards.className = "usage-platforms";
    [["openrouter", "OpenRouter"], ["anthropic", "Anthropic"]].forEach(([key, label]) => {
      const spent = byPlatform[key] || 0;
      const lim = platformLimits[key] || 0;
      const card = document.createElement("div");
      card.className = "usage-platform-card";
      card.innerHTML = `
        <div class="usage-platform-name">${label}</div>
        <div class="usage-platform-amt">$${spent.toFixed(2)}</div>
        <div class="usage-platform-name" style="margin-top:2px;">/ $${lim.toFixed(0)}</div>
      `;
      platformCards.appendChild(card);
    });
    panel.appendChild(platformCards);

    // 角色进度条
    const byChar = data.by_character || {};
    const limits = data.limits || {};
    const totalUsd = data.total_usd || 0;
    const totalLimit = limits._total || 1;
    const charOrder = ["char1", "char2", "char3", "char4", "char5", "char6"];
    charOrder.forEach(cid => {
      const spent = byChar[cid] || 0;
      const lim = limits[cid] || 5;
      const pct = Math.min(spent / lim * 100, 100);
      const cls = pct >= 100 ? "over" : pct >= 80 ? "warn" : "";
      const row = document.createElement("div");
      row.className = "usage-row";
      row.innerHTML = `
        <span class="usage-label">${CHAR_DISPLAY_NAMES[cid] || cid}</span>
        <div class="usage-bar-track">
          <div class="usage-bar-fill ${cls}" style="width:${pct}%">
            <div class="usage-bar-thumb"><span class="material-symbols-outlined">pets</span></div>
          </div>
        </div>
        <span class="usage-amt">$${spent.toFixed(3)}</span>
      `;
      panel.appendChild(row);
    });

    // 总计行
    const totalPct = Math.min(totalUsd / totalLimit * 100, 100);
    const totalCls = totalPct >= 100 ? "over" : totalPct >= 80 ? "warn" : "";
    const totalRow = document.createElement("div");
    totalRow.className = "usage-row";
    totalRow.innerHTML = `
      <span class="usage-label" style="color:var(--text);font-weight:600;">总计</span>
      <div class="usage-bar-track">
        <div class="usage-bar-fill ${totalCls}" style="width:${totalPct}%">
          <div class="usage-bar-thumb"><span class="material-symbols-outlined">pets</span></div>
        </div>
      </div>
      <span class="usage-amt" style="color:var(--text);font-weight:600;">$${totalUsd.toFixed(3)}</span>
    `;
    panel.appendChild(totalRow);
  }

  async function loadUsagePanel() {
    try {
      const data = await fetchUsage();
      renderUsage(data);
    } catch (e) {
      console.warn("loadUsagePanel failed", e);
    }
  }

  async function loadCompressHealth() {
    const panel = document.getElementById("compressHealthPanel");
    if (!panel) return;
    try {
      const res  = await fetch("/api/compress_health");
      const data = await res.json();
      const dotClass = data.status === "ok" ? "ok" : data.status === "fail" ? "fail" : "none";
      let label;
      if (data.status === "ok") {
        label = "压缩正常";
      } else if (data.status === "fail") {
        const dt = data.ts ? new Date(data.ts) : null;
        const timeStr = dt ? `${String(dt.getMonth()+1).padStart(2,"0")}/${String(dt.getDate()).padStart(2,"0")} ${String(dt.getHours()).padStart(2,"0")}:${String(dt.getMinutes()).padStart(2,"0")}` : "";
        label = `压缩异常 · ${timeStr} · ${data.char || ""}`;
      } else {
        label = "尚未触发压缩";
      }
      panel.innerHTML = `<div class="compress-health-card">
        <div class="compress-health-dot ${dotClass}"></div>
        <div class="compress-health-text">${label}</div>
      </div>`;
    } catch (e) {
      console.warn("loadCompressHealth failed", e);
    }
  }

  function truncateText(text, maxLen = 80) {
    if (!text) return "";
    return text.length <= maxLen ? text : text.slice(0, maxLen) + "……";
  }

  function showMemorySub(sub) {
    const listView   = document.getElementById("memoryListView");
    const detailView = document.getElementById("memoryDetailView");
    if (sub === "list") {
      listView.style.display   = "flex";
      detailView.style.display = "none";
      document.getElementById("memorySearchBar").hidden = true;
    } else {
      listView.style.display   = "none";
      detailView.style.display = "flex";
    }
  }

  function memoryDate(value, includeTime = false) {
    if (!value) return "";
    const dt = new Date(value);
    if (Number.isNaN(dt.getTime())) return String(value).slice(0, 16).replace("T", " ");
    const options = includeTime
      ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }
      : { year: "numeric", month: "2-digit", day: "2-digit" };
    return new Intl.DateTimeFormat("zh-CN", options).format(dt);
  }

  let memoryListRenderVersion = 0;
  async function renderMemoryList(characters) {
    const container = document.getElementById("memoryCardContainer");
    const renderVersion = ++memoryListRenderVersion;
    const fragment = document.createDocumentFragment();
    characters.forEach(s => {
      const card = document.createElement("div");
      card.className = "memory-card";
      card.tabIndex = 0;
      card.setAttribute("role", "button");

      const top = document.createElement("div");
      top.className = "memory-card-top";
      const avatar = document.createElement("img");
      avatar.src = s.avatar || charAvatars[s.character_id] || "";
      avatar.alt = "";
      avatar.onerror = function() { this.style.display = "none"; };
      const identity = document.createElement("div");
      identity.className = "memory-card-identity";
      const name = document.createElement("strong");
      name.textContent = s.name;
      const count = document.createElement("span");
      count.textContent = s.count ? `${s.count} 段记忆` : "还没有留下记忆";
      identity.appendChild(name);
      identity.appendChild(count);
      top.appendChild(avatar);
      top.appendChild(identity);

      const preview = document.createElement("div");
      preview.className = "memory-card-preview";
      preview.textContent = truncateText(s.latest?.content, 92) || "这里暂时安安静静。";
      card.appendChild(top);
      card.appendChild(preview);
      if (s.latest?.created) {
        const date = document.createElement("time");
        date.className = "memory-card-date";
        date.textContent = memoryDate(s.latest.created);
        card.appendChild(date);
      }

      const open = () => loadCharacterMemories(s.character_id, s.name);
      card.addEventListener("click", open);
      card.addEventListener("keydown", e => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
      });
      fragment.appendChild(card);
    });
    await decodeImagesBeforeSwap(fragment);
    if (renderVersion !== memoryListRenderVersion) return;
    container.replaceChildren(fragment);
  }

  let currentMemoryCharacter = null;
  let currentMemoryName = "";
  let memorySearchTimer = null;

  const MEMORY_SOURCE_LABELS = {
    self_saved: "他亲自留下",
    group_self_saved: "群聊里留下",
    conversation_summary: "对话沉淀",
    group_summary: "群聊沉淀",
    moment: "猫窝动态",
    moment_comment: "猫窝回应",
    legacy_ombre: "旧猫脑壳",
  };

  async function patchMemory(memoryId, updates) {
    const response = await fetch(`/api/memory/${currentMemoryCharacter}/${memoryId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(updates),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "保存失败");
    return data.memory;
  }

  function renderMemoryEntries(memories) {
    const content = document.getElementById("memoryDetailContent");
    content.innerHTML = "";
    if (!memories.length) {
      const empty = document.createElement("div");
      empty.className = "memory-empty";
      empty.textContent = "这里暂时安安静静。";
      content.appendChild(empty);
      return;
    }

    memories.forEach(memory => {
      const entry = document.createElement("article");
      entry.className = "memory-entry";

      const head = document.createElement("div");
      head.className = "memory-entry-head";
      const date = document.createElement("time");
      date.textContent = memoryDate(memory.created, true);
      const marks = document.createElement("div");
      marks.className = "memory-entry-marks";
      if (memory.pinned) {
        const pin = document.createElement("span");
        pin.className = "material-symbols-outlined";
        pin.textContent = "keep";
        marks.appendChild(pin);
      }
      if (memory.resolved) {
        const resolved = document.createElement("span");
        resolved.className = "material-symbols-outlined";
        resolved.textContent = "check_circle";
        marks.appendChild(resolved);
      }
      head.appendChild(date);
      head.appendChild(marks);

      const body = document.createElement("div");
      body.className = "memory-entry-content";
      body.textContent = memory.content;

      const foot = document.createElement("div");
      foot.className = "memory-entry-foot";
      const meta = document.createElement("div");
      meta.className = "memory-entry-meta";
      const source = document.createElement("span");
      source.textContent = MEMORY_SOURCE_LABELS[memory.source] || "记忆";
      const vector = document.createElement("span");
      vector.className = "memory-entry-vector";
      if (memory.enrichment_status === "complete") {
        const valence = Number(memory.valence);
        const arousal = Number(memory.arousal);
        vector.textContent = Number.isFinite(valence) && Number.isFinite(arousal)
          ? `V${valence.toFixed(2)} · A${arousal.toFixed(2)}`
          : "情感向量待补";
      } else if (memory.enrichment_status === "error") {
        vector.textContent = "待重新打标";
        vector.title = memory.enrichment_error || "打标服务刚才没有成功";
      } else if (memory.enrichment_status === "unconfigured") {
        vector.textContent = "打标尚未接入";
      } else {
        vector.textContent = "情感打标中";
      }
      const actions = document.createElement("div");
      actions.className = "memory-entry-actions";

      const makeAction = (icon, title, handler, active = false) => {
        const button = document.createElement("button");
        button.type = "button";
        button.title = title;
        button.setAttribute("aria-label", title);
        if (active) button.classList.add("active");
        const symbol = document.createElement("span");
        symbol.className = "material-symbols-outlined";
        symbol.textContent = icon;
        button.appendChild(symbol);
        button.addEventListener("click", async e => {
          e.stopPropagation();
          await handler(button);
        });
        return button;
      };

      actions.appendChild(makeAction("keep", memory.pinned ? "取消固定" : "固定记忆", async button => {
        button.disabled = true;
        try {
          await patchMemory(memory.id, { pinned: !memory.pinned });
          await loadCharacterMemories(currentMemoryCharacter, currentMemoryName, false);
        } catch (error) { showToast(error.message); }
      }, memory.pinned));
      actions.appendChild(makeAction("check_circle", memory.resolved ? "重新浮现" : "轻轻放下", async button => {
        button.disabled = true;
        try {
          await patchMemory(memory.id, { resolved: !memory.resolved });
          await loadCharacterMemories(currentMemoryCharacter, currentMemoryName, false);
        } catch (error) { showToast(error.message); }
      }, memory.resolved));
      actions.appendChild(makeAction("edit", "编辑记忆", async () => {
        if (entry.classList.contains("editing")) return;
        entry.classList.add("editing", "expanded");
        const editor = document.createElement("textarea");
        editor.className = "memory-entry-editor";
        editor.value = memory.content;
        body.hidden = true;
        entry.insertBefore(editor, foot);
        editor.focus();

        const save = makeAction("save", "保存", async button => {
          button.disabled = true;
          try {
            await patchMemory(memory.id, { content: editor.value });
            await loadCharacterMemories(currentMemoryCharacter, currentMemoryName, false);
          } catch (error) { showToast(error.message); button.disabled = false; }
        });
        const cancel = makeAction("close", "取消", async () => {
          editor.remove();
          body.hidden = false;
          entry.classList.remove("editing");
          actions.innerHTML = "";
          renderMemoryEntries(memories);
        });
        actions.innerHTML = "";
        actions.appendChild(save);
        actions.appendChild(cancel);
      }));
      actions.appendChild(makeAction("delete", "删除记忆", async () => {
        showConfirmDialog("确定要删除这段记忆吗？", async () => {
          const response = await fetch(`/api/memory/${currentMemoryCharacter}/${memory.id}`, { method: "DELETE" });
          if (!response.ok) { showToast("没有删掉，再试一次"); return; }
          await loadCharacterMemories(currentMemoryCharacter, currentMemoryName, false);
        });
      }));

      meta.appendChild(source);
      meta.appendChild(vector);
      foot.appendChild(meta);
      foot.appendChild(actions);
      entry.appendChild(head);
      entry.appendChild(body);
      entry.appendChild(foot);
      entry.addEventListener("click", e => {
        if (!e.target.closest("button, textarea")) entry.classList.toggle("expanded");
      });
      content.appendChild(entry);
    });
  }

  async function loadCharacterMemories(characterId, name, openDetail = true) {
    currentMemoryCharacter = characterId;
    currentMemoryName = name;
    document.getElementById("memory-char-name").textContent = name;
    if (openDetail) showMemorySub("detail");
    const content = document.getElementById("memoryDetailContent");
    content.innerHTML = '<div class="memory-loading">记忆正在浮上来…</div>';
    const query = document.getElementById("memorySearchInput").value.trim();
    try {
      const response = await fetch(`/api/memory/${characterId}?q=${encodeURIComponent(query)}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      renderMemoryEntries(data.memories || []);
    } catch (error) {
      content.innerHTML = '<div class="memory-empty">记忆没有打开，再试一次。</div>';
    }
  }

  async function loadMemoryView() {
    try {
      const characters = await fetchMemoryOverview();
      await renderMemoryList(characters);
    } catch (e) {
      console.warn("loadMemoryView failed", e);
    }
  }

  document.getElementById("backToMemoryList").addEventListener("click", () => {
    document.getElementById("memorySearchInput").value = "";
    showMemorySub("list");
    loadMemoryView();
  });
  document.getElementById("memorySearchToggle").addEventListener("click", () => {
    const bar = document.getElementById("memorySearchBar");
    bar.hidden = !bar.hidden;
    if (!bar.hidden) document.getElementById("memorySearchInput").focus();
  });
  document.getElementById("memorySearchInput").addEventListener("input", () => {
    clearTimeout(memorySearchTimer);
    memorySearchTimer = setTimeout(() => {
      if (currentMemoryCharacter) loadCharacterMemories(currentMemoryCharacter, currentMemoryName, false);
    }, 220);
  });

  // ════════════════════════════════════════════
  // 视图切换
  // ════════════════════════════════════════════
  function showView(viewId) {
    document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));
    document.getElementById(viewId).classList.add("active");
    document.querySelectorAll(".nav-item").forEach(n => n.classList.remove("active"));
    document.querySelector(`.nav-item[data-view="${viewId}"]`).classList.add("active");
    if (viewId === "groupView") loadGroupHistory();
    if (viewId === "memoryView") {
      document.getElementById("memorySearchInput").value = "";
      showMemorySub("list");
      loadMemoryView();
    }
    if (viewId === "moreView") { loadUsagePanel(); loadCompressHealth(); }
  }
  document.querySelectorAll(".nav-item").forEach(item => {
    item.addEventListener("click", () => showView(item.dataset.view));
  });

  function startSplashDismiss(delay = 2000) {
    setTimeout(() => {
      const splash = document.getElementById("splashScreen");
      if (splash) {
        splash.classList.add("fade-out");
        setTimeout(() => splash.remove(), 500);
      }
    }, delay);
  }

  // ── MCP 工具面板 ──
  let _toolsPanelOpen = false;

  async function openToolsPanel() {
    if (_appearancePanelOpen) closeAppearancePanel();
    if (_personaPanelOpen) closePersonaPanel();
    if (_schedulerPanelOpen) closeSchedulerPanel();
    if (_memoryImportPanelOpen) closeMemoryImportPanel();
    const panel = document.getElementById("toolsPanel");
    panel.innerHTML = "";
    panel.style.display = "flex";
    panel.style.flexDirection = "column";
    panel.style.padding = "12px 16px 16px";
    panel.style.gap = "12px";

    const closeBtn = document.createElement("button");
    closeBtn.textContent = "收起 ×";
    closeBtn.className = "persona-close-btn";
    closeBtn.onclick = closeToolsPanel;
    panel.appendChild(closeBtn);

    let tools;
    let customMcps;
    try {
      const res = await fetch("/api/tools");
      ({ tools, custom_mcps: customMcps } = await res.json());
    } catch (e) {
      panel.innerHTML = '<p style="padding:16px;color:var(--muted);">加载失败，请重试</p>';
      return;
    }

    const mcpSection = document.createElement("section");
    mcpSection.className = "mcp-connections-section";
    const mcpSectionHead = document.createElement("div");
    mcpSectionHead.className = "mcp-connections-head";
    const mcpSectionTitle = document.createElement("div");
    mcpSectionTitle.innerHTML = '<span class="material-symbols-outlined">hub</span><strong>自定义 MCP</strong>';
    const mcpAdd = document.createElement("button");
    mcpAdd.className = "mcp-add-btn";
    mcpAdd.innerHTML = '<span class="material-symbols-outlined">add_link</span><span>接入</span>';
    mcpSectionHead.appendChild(mcpSectionTitle);
    mcpSectionHead.appendChild(mcpAdd);
    mcpSection.appendChild(mcpSectionHead);

    const mcpEditorHost = document.createElement("div");
    const mcpList = document.createElement("div");
    mcpList.className = "mcp-connections-list";
    mcpSection.appendChild(mcpEditorHost);
    mcpSection.appendChild(mcpList);
    panel.appendChild(mcpSection);

    function renderMcpEditor(connection = null) {
      mcpEditorHost.innerHTML = "";
      const editor = document.createElement("div");
      editor.className = "custom-mcp-panel";
      const editorHead = document.createElement("div");
      editorHead.className = "custom-mcp-head";
      const editorTitle = document.createElement("div");
      editorTitle.innerHTML = `<span class="material-symbols-outlined">${connection ? "edit" : "add_link"}</span><strong>${connection ? "编辑连接" : "接入新的 MCP"}</strong>`;
      let enabled = connection ? connection.enabled : true;
      const enabledToggle = document.createElement("button");
      const paintEnabled = () => {
        enabledToggle.className = "tool-toggle" + (enabled ? " tool-toggle-on" : "");
        enabledToggle.textContent = enabled ? "开" : "关";
      };
      paintEnabled();
      enabledToggle.onclick = () => { enabled = !enabled; paintEnabled(); };
      editorHead.appendChild(editorTitle);
      editorHead.appendChild(enabledToggle);

      function makeMcpField(labelText, type, placeholder, value = "") {
        const label = document.createElement("label");
        label.className = "custom-mcp-field";
        const caption = document.createElement("span");
        caption.textContent = labelText;
        const input = document.createElement("input");
        input.type = type;
        input.placeholder = placeholder;
        input.value = value;
        input.autocomplete = type === "password" ? "new-password" : "off";
        label.appendChild(caption);
        label.appendChild(input);
        return { label, input };
      }

      const nameField = makeMcpField("连接名称", "text", "例如：Char 2的 AI 论坛", connection?.name || "");
      const urlField = makeMcpField("HTTP 地址", "url", "https://example.com/mcp", connection?.url || "");
      const tokenField = makeMcpField(
        "长效 Token", "password",
        connection?.has_token ? "已保存，留空不变" : "Bearer Token",
      );
      const characterField = document.createElement("div");
      characterField.className = "custom-mcp-field";
      const characterCaption = document.createElement("span");
      characterCaption.textContent = "这个账号属于";
      const characterGrid = document.createElement("div");
      characterGrid.className = "custom-mcp-character-grid";
      const selectedCharacters = new Set(connection?.character_ids || [currentChar]);
      Object.keys(histories).forEach(cid => {
        const choice = document.createElement("button");
        choice.type = "button";
        choice.className = "custom-mcp-character" + (selectedCharacters.has(cid) ? " selected" : "");
        const avatar = document.createElement("img");
        avatar.src = charAvatars[cid] || "";
        avatar.alt = "";
        const label = document.createElement("span");
        label.textContent = nickName(cid);
        choice.appendChild(avatar);
        choice.appendChild(label);
        choice.onclick = () => {
          if (selectedCharacters.has(cid)) selectedCharacters.delete(cid);
          else selectedCharacters.add(cid);
          choice.classList.toggle("selected", selectedCharacters.has(cid));
        };
        characterGrid.appendChild(choice);
      });
      characterField.appendChild(characterCaption);
      characterField.appendChild(characterGrid);

      const status = document.createElement("div");
      status.className = "custom-mcp-status";
      status.textContent = connection?.has_token ? "Token 已安全保存" : "还没有保存 Token";
      const actions = document.createElement("div");
      actions.className = "custom-mcp-actions";
      const save = document.createElement("button");
      save.textContent = connection ? "保存修改" : "加入列表";
      const saveAndTest = document.createElement("button");
      saveAndTest.textContent = "保存并测试";
      const cancel = document.createElement("button");
      cancel.className = "quiet";
      cancel.textContent = "取消";
      cancel.onclick = () => { mcpEditorHost.innerHTML = ""; };

      async function submitMcp(testAfter) {
        if (!selectedCharacters.size) {
          status.className = "custom-mcp-status error";
          status.textContent = "至少选一位使用这个账号";
          return;
        }
        save.disabled = true;
        saveAndTest.disabled = true;
        status.className = "custom-mcp-status";
        status.textContent = testAfter ? "正在保存并握手……" : "正在保存……";
        try {
          const endpoint = connection
            ? `/api/tools/custom-mcp/${connection.id}`
            : "/api/tools/custom-mcp";
          const response = await fetch(endpoint, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              name: nameField.input.value,
              url: urlField.input.value,
              token: tokenField.input.value,
              enabled,
              character_ids: [...selectedCharacters],
            }),
          });
          const data = await response.json();
          if (!response.ok) throw new Error(data.error || "保存失败");
          if (testAfter) {
            const tested = await fetch(`/api/tools/custom-mcp/${data.connection.id}/test`, { method: "POST" });
            const testData = await tested.json();
            if (!tested.ok) throw new Error(testData.error || "连接失败");
            showToast(`${testData.server_name} · ${(testData.tools || []).length} 个工具`);
          } else {
            showToast("MCP 已加入列表");
          }
          await openToolsPanel();
        } catch (error) {
          status.className = "custom-mcp-status error";
          status.textContent = error.message;
        } finally {
          save.disabled = false;
          saveAndTest.disabled = false;
        }
      }
      save.onclick = () => submitMcp(false);
      saveAndTest.onclick = () => submitMcp(true);
      actions.appendChild(cancel);
      actions.appendChild(save);
      actions.appendChild(saveAndTest);
      editor.appendChild(editorHead);
      editor.appendChild(nameField.label);
      editor.appendChild(urlField.label);
      editor.appendChild(tokenField.label);
      editor.appendChild(characterField);
      editor.appendChild(status);
      editor.appendChild(actions);
      mcpEditorHost.appendChild(editor);
      nameField.input.focus();
    }

    mcpAdd.onclick = () => renderMcpEditor();
    if (!customMcps?.length) {
      const empty = document.createElement("div");
      empty.className = "mcp-list-empty";
      empty.textContent = "还没有接入远端 MCP。";
      mcpList.appendChild(empty);
    }
    (customMcps || []).forEach(connection => {
      const item = document.createElement("div");
      item.className = "mcp-connection-item";
      const summary = document.createElement("div");
      summary.className = "mcp-connection-summary";
      const identity = document.createElement("div");
      identity.className = "mcp-connection-identity";
      const name = document.createElement("strong");
      name.textContent = connection.name;
      const owners = document.createElement("span");
      owners.textContent = (connection.character_ids || []).map(cid => nickName(cid)).join(" · ");
      identity.appendChild(name);
      identity.appendChild(owners);
      const state = document.createElement("span");
      state.className = `mcp-connection-state ${connection.status}`;
      state.textContent = connection.status === "ok"
        ? `${connection.server_name || connection.name} · ${connection.tools.length} 个工具`
        : (connection.enabled ? "已保存 · 待握手" : "已关闭");
      summary.appendChild(identity);
      summary.appendChild(state);

      const controls = document.createElement("div");
      controls.className = "mcp-connection-controls";
      const toggle = document.createElement("button");
      toggle.className = "tool-toggle" + (connection.enabled ? " tool-toggle-on" : "");
      toggle.textContent = connection.enabled ? "开" : "关";
      toggle.onclick = async () => {
        await fetch(`/api/tools/custom-mcp/${connection.id}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: connection.name,
            url: connection.url,
            enabled: !connection.enabled,
            character_ids: connection.character_ids,
          }),
        });
        await openToolsPanel();
      };
      const test = document.createElement("button");
      test.className = "mcp-icon-btn";
      test.title = "测试连接";
      test.setAttribute("aria-label", `测试 ${connection.name}`);
      test.innerHTML = '<span class="material-symbols-outlined">network_check</span>';
      test.onclick = async () => {
        test.disabled = true;
        try {
          const response = await fetch(`/api/tools/custom-mcp/${connection.id}/test`, { method: "POST" });
          const data = await response.json();
          if (!response.ok) throw new Error(data.error || "连接失败");
          showToast(`${data.server_name} · ${(data.tools || []).length} 个工具`);
          await openToolsPanel();
        } catch (error) { showToast(error.message); }
        finally { test.disabled = false; }
      };
      const edit = document.createElement("button");
      edit.className = "mcp-icon-btn";
      edit.title = "编辑连接";
      edit.setAttribute("aria-label", `编辑 ${connection.name}`);
      edit.innerHTML = '<span class="material-symbols-outlined">edit</span>';
      edit.onclick = () => renderMcpEditor(connection);
      const remove = document.createElement("button");
      remove.className = "mcp-icon-btn danger";
      remove.title = "删除连接";
      remove.setAttribute("aria-label", `删除 ${connection.name}`);
      remove.innerHTML = '<span class="material-symbols-outlined">delete</span>';
      remove.onclick = () => showConfirmDialog(`删除“${connection.name}”和它保存的 Token？`, async () => {
        await fetch(`/api/tools/custom-mcp/${connection.id}`, { method: "DELETE" });
        await openToolsPanel();
      });
      controls.appendChild(toggle);
      controls.appendChild(test);
      controls.appendChild(edit);
      controls.appendChild(remove);
      item.appendChild(summary);
      item.appendChild(controls);
      if (connection.tools?.length) {
        const remoteList = document.createElement("div");
        remoteList.className = "custom-mcp-tool-list";
        connection.tools.forEach(tool => {
          const chip = document.createElement("span");
          chip.textContent = tool.name;
          remoteList.appendChild(chip);
        });
        item.appendChild(remoteList);
      }
      mcpList.appendChild(item);
    });

    tools.forEach(tool => {
      const row = document.createElement("div");
      row.className = "tool-row";

      const info = document.createElement("div");
      info.className = "tool-info";
      info.innerHTML = `<span class="tool-char">${tool.character}</span>
        <span class="tool-name">${tool.name}</span>
        <span class="tool-desc">${tool.description.slice(0, 28)}…</span>`;

      const toggle = document.createElement("button");
      toggle.className = "tool-toggle" + (tool.enabled ? " tool-toggle-on" : "");
      toggle.textContent = tool.enabled ? "开" : "关";
      toggle.onclick = async () => {
        const r = await fetch(`/api/tools/${tool.name}/toggle`, { method: "POST" });
        const d = await r.json();
        toggle.className = "tool-toggle" + (d.enabled ? " tool-toggle-on" : "");
        toggle.textContent = d.enabled ? "开" : "关";
      };

      row.appendChild(info);
      row.appendChild(toggle);
      panel.appendChild(row);
    });

    _toolsPanelOpen = true;
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function closeToolsPanel() {
    const panel = document.getElementById("toolsPanel");
    panel.style.display = "none";
    panel.innerHTML = "";
    _toolsPanelOpen = false;
    document.getElementById("moreContent").scrollTop = 0;
  }

  // ── 猫砂盆调度面板 ──
  const SCHED_SLOTS = ["morning","noon","evening"];
  const SCHED_SLOT_LABELS = { morning:"早9点", noon:"中12点", evening:"晚21点" };
  let _schedulerPanelOpen = false;

  async function openSchedulerPanel() {
    if (_appearancePanelOpen) closeAppearancePanel();
    if (_personaPanelOpen) closePersonaPanel();
    if (_toolsPanelOpen) closeToolsPanel();
    if (_memoryImportPanelOpen) closeMemoryImportPanel();
    const panel = document.getElementById("schedulerPanel");
    panel.innerHTML = "";
    panel.style.display = "flex";
    panel.style.flexDirection = "column";
    panel.style.padding = "12px 16px 16px";
    panel.style.gap = "12px";

    const closeBtn = document.createElement("button");
    closeBtn.textContent = "收起 ×";
    closeBtn.className = "persona-close-btn";
    closeBtn.onclick = closeSchedulerPanel;
    panel.appendChild(closeBtn);

    let cfg = {
      moments_slots: "",
      desire_enabled: true,
      desire_quiet_start: "23:30",
      desire_quiet_end: "08:30",
    };
    let limitCfg = {
      char1: 10, char2: 30, char3: 10,
      char4: 10, char5: 30, char6: 50,
    };
    try {
      const [schedulerRes, limitsRes] = await Promise.all([
        fetch("/api/scheduler/config"),
        fetch("/api/limits"),
      ]);
      cfg = await schedulerRes.json();
      ({ limits: limitCfg } = await limitsRes.json());
    } catch(e) {}
    const selMomSlots = new Set(cfg.moments_slots ? cfg.moments_slots.split(",").filter(Boolean) : []);
    let desireEnabled = cfg.desire_enabled !== false;

    function makeSlotRow(selectedSet) {
      const row = document.createElement("div");
      row.style.cssText = "display:flex;gap:8px;flex-wrap:wrap;";
      SCHED_SLOTS.forEach(slot => {
        const btn = document.createElement("button");
        btn.textContent = SCHED_SLOT_LABELS[slot];
        btn.style.cssText = "padding:5px 12px;border-radius:20px;font-size:12px;cursor:pointer;border:1px solid var(--dusky);transition:.15s;appearance:none;-webkit-appearance:none;";
        const apply = () => {
          btn.style.background = selectedSet.has(slot) ? "var(--chrome)" : "transparent";
          btn.style.color      = selectedSet.has(slot) ? "var(--on-dusky)" : "var(--dusky)";
        };
        apply();
        btn.onclick = () => { selectedSet.has(slot) ? selectedSet.delete(slot) : selectedSet.add(slot); apply(); };
        row.appendChild(btn);
      });
      return row;
    }

    function makeDesireControls() {
      const wrap = document.createElement("div");
      wrap.className = "scheduler-desire-controls";

      const toggleRow = document.createElement("div");
      toggleRow.className = "scheduler-control-row";
      const toggleLabel = document.createElement("span");
      toggleLabel.textContent = "欲望唤醒";
      const toggle = document.createElement("button");
      const applyToggle = () => {
        toggle.className = "tool-toggle" + (desireEnabled ? " tool-toggle-on" : "");
        toggle.textContent = desireEnabled ? "开" : "关";
      };
      applyToggle();
      toggle.onclick = () => { desireEnabled = !desireEnabled; applyToggle(); };
      toggleRow.appendChild(toggleLabel);
      toggleRow.appendChild(toggle);

      const timeRow = document.createElement("div");
      timeRow.className = "scheduler-time-row";
      const startWrap = document.createElement("label");
      startWrap.textContent = "勿扰开始";
      const quietStart = document.createElement("input");
      quietStart.type = "time";
      quietStart.value = cfg.desire_quiet_start || "23:30";
      quietStart.id = "desireQuietStart";
      startWrap.appendChild(quietStart);
      const endWrap = document.createElement("label");
      endWrap.textContent = "勿扰结束";
      const quietEnd = document.createElement("input");
      quietEnd.type = "time";
      quietEnd.value = cfg.desire_quiet_end || "08:30";
      quietEnd.id = "desireQuietEnd";
      endWrap.appendChild(quietEnd);
      timeRow.appendChild(startWrap);
      timeRow.appendChild(endWrap);

      wrap.appendChild(toggleRow);
      wrap.appendChild(timeRow);
      return wrap;
    }

    function makeLimitControls() {
      const wrap = document.createElement("div");
      wrap.className = "scheduler-limit-grid";
      ["char1", "char2", "char3", "char4", "char5", "char6"].forEach(cid => {
        const label = document.createElement("label");
        const name = document.createElement("span");
        name.textContent = nickName(cid);
        const inputWrap = document.createElement("span");
        inputWrap.className = "scheduler-limit-input-wrap";
        const currency = document.createElement("span");
        currency.textContent = "$";
        const input = document.createElement("input");
        input.type = "number";
        input.min = "0.01";
        input.max = "10000";
        input.step = "0.5";
        input.value = Number(limitCfg?.[cid] || 1).toFixed(2);
        input.dataset.limitCid = cid;
        inputWrap.appendChild(currency);
        inputWrap.appendChild(input);
        label.appendChild(name);
        label.appendChild(inputWrap);
        wrap.appendChild(label);
      });
      return wrap;
    }

    const saveBtn = document.createElement("button");
    saveBtn.textContent = "保存配置 🐾";
    saveBtn.style.cssText = "display:none;background:var(--chrome);color:var(--on-dusky);border:none;border-radius:20px;padding:10px;font-size:13px;cursor:pointer;width:100%;margin-top:4px;";
    saveBtn.onclick = async () => {
      saveBtn.textContent = "保存中…";
      try {
        const limits = {};
        panel.querySelectorAll("[data-limit-cid]").forEach(input => {
          const value = Number(input.value);
          if (!Number.isFinite(value) || value < 0.01 || value > 10000) {
            throw new Error("额度需在 0.01–10000 之间");
          }
          limits[input.dataset.limitCid] = value;
        });
        const [schedulerSave, limitSave] = await Promise.all([
          fetch("/api/scheduler/config", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              moments_slots: [...selMomSlots].join(","),
              desire_enabled: desireEnabled,
              desire_quiet_start: document.getElementById("desireQuietStart")?.value || "23:30",
              desire_quiet_end: document.getElementById("desireQuietEnd")?.value || "08:30",
            }),
          }),
          fetch("/api/limits", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ limits }),
          }),
        ]);
        if (!schedulerSave.ok || !limitSave.ok) throw new Error("save failed");
        await loadUsagePanel();
        saveBtn.textContent = "已保存 🐾";
        setTimeout(() => { saveBtn.textContent = "保存配置 🐾"; }, 1500);
      } catch(e) {
        saveBtn.textContent = "保存失败，重试";
      }
    };

    function makeAccordion(title, bodyChildren) {
      const wrap = document.createElement("div");

      const hdr = document.createElement("button");
      hdr.textContent = title;
      hdr.style.cssText = "width:100%;padding:14px 20px;background:var(--cream);border:2px solid var(--dusky);border-radius:999px;color:var(--dusky);font-size:15px;font-weight:500;text-align:left;cursor:pointer;transition:border-radius .2s;appearance:none;-webkit-appearance:none;box-sizing:border-box;";

      const body = document.createElement("div");
      body.style.cssText = "display:none;flex-direction:column;gap:10px;padding:14px 16px;background:var(--cream);border:2px solid var(--dusky);border-top:none;border-radius:0 0 16px 16px;box-sizing:border-box;";
      body.dataset.schedBody = "true";
      body.dataset.open = "false";
      bodyChildren.forEach(ch => body.appendChild(ch));

      let isOpen = false;
      hdr.onclick = () => {
        isOpen = !isOpen;
        body.style.display = isOpen ? "flex" : "none";
        body.dataset.open = isOpen ? "true" : "false";
        hdr.style.borderRadius = isOpen ? "16px 16px 0 0" : "999px";
        const anyOpen = [...panel.querySelectorAll("[data-sched-body]")].some(b => b.dataset.open === "true");
        saveBtn.style.display = anyOpen ? "block" : "none";
      };

      wrap.appendChild(hdr);
      wrap.appendChild(body);
      return wrap;
    }

    panel.appendChild(makeAccordion("醒醒喵°欲望心跳", [makeDesireControls()]));
    panel.appendChild(makeAccordion("聊聊喵°自动发帖", [makeSlotRow(selMomSlots)]));
    panel.appendChild(makeAccordion("饭饭喵°月度额度", [makeLimitControls()]));
    panel.appendChild(saveBtn);

    _schedulerPanelOpen = true;
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function closeSchedulerPanel() {
    const panel = document.getElementById("schedulerPanel");
    panel.style.display = "none";
    panel.innerHTML = "";
    _schedulerPanelOpen = false;
    document.getElementById("moreContent").scrollTop = 0;
  }

  // ── 人设编辑面板 ──
  const PERSONA_ORDER = ["char1", "char2", "char3", "char4", "char5", "char6"];
  let _appearancePanelOpen = false;

  async function saveAppearanceAsset(assetKey, file) {
    const form = new FormData();
    form.append("image", file);
    const res = await fetch(`/api/appearance/assets/${assetKey}`, {
      method: "POST",
      body: form,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "更换失败");
    applyAppearance(data.appearance);
    return data.appearance;
  }

  async function saveAppearanceTheme(themeId) {
    const res = await fetch("/api/appearance", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ theme: themeId }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "主题切换失败");
    applyAppearance(data);
    return data;
  }

  async function resetAppearanceAsset(assetKey) {
    const res = await fetch(`/api/appearance/assets/${assetKey}`, { method: "DELETE" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "恢复失败");
    applyAppearance(data.appearance);
    return data.appearance;
  }

  function makeAppearanceIcon(icon, title, onClick, disabled = false) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "appearance-icon-btn";
    button.title = title;
    button.setAttribute("aria-label", title);
    button.disabled = disabled;
    const symbol = document.createElement("span");
    symbol.className = "material-symbols-outlined";
    symbol.textContent = icon;
    button.appendChild(symbol);
    button.addEventListener("click", onClick);
    return button;
  }

  async function renderAppearancePanel() {
    const panel = document.getElementById("appearancePanel");
    panel.innerHTML = "";

    const closeBtn = document.createElement("button");
    closeBtn.className = "persona-close-btn";
    closeBtn.textContent = "收起";
    closeBtn.addEventListener("click", closeAppearancePanel);
    panel.appendChild(closeBtn);

    let data;
    try {
      data = await loadAppearance();
    } catch (e) {
      data = null;
    }
    if (!data) {
      const failed = document.createElement("p");
      failed.className = "memory-empty";
      failed.textContent = "洗护台暂时打不开，请稍后再试";
      panel.appendChild(failed);
      return;
    }

    const themeSection = document.createElement("section");
    themeSection.className = "appearance-section";
    const themeTitle = document.createElement("div");
    themeTitle.className = "appearance-section-title";
    themeTitle.innerHTML = '<span class="material-symbols-outlined">palette</span><span>外观主题</span>';
    const themeGrid = document.createElement("div");
    themeGrid.className = "appearance-theme-grid";
    (data.themes || []).forEach(theme => {
      const option = document.createElement("button");
      option.type = "button";
      option.className = "appearance-theme-option";
      option.classList.toggle("active", theme.id === data.theme);
      option.setAttribute("aria-pressed", theme.id === data.theme ? "true" : "false");

      const swatches = document.createElement("span");
      swatches.className = "appearance-theme-swatches";
      ["chrome", "user_bubble", "ai_bubble", "cream"].forEach(key => {
        const swatch = document.createElement("i");
        swatch.style.background = theme.colors?.[key] || "transparent";
        swatches.appendChild(swatch);
      });
      const label = document.createElement("span");
      label.className = "appearance-theme-name";
      label.textContent = theme.name;
      const check = document.createElement("span");
      check.className = "material-symbols-outlined appearance-theme-check";
      check.textContent = "check_circle";
      option.append(swatches, label, check);
      option.addEventListener("click", async () => {
        if (theme.id === appearanceState?.theme) return;
        themeGrid.querySelectorAll("button").forEach(button => { button.disabled = true; });
        try {
          await saveAppearanceTheme(theme.id);
          showToast(`换上${theme.name}啦`);
          await renderAppearancePanel();
        } catch (e) {
          showToast(e.message);
          themeGrid.querySelectorAll("button").forEach(button => { button.disabled = false; });
        }
      });
      themeGrid.appendChild(option);
    });
    themeSection.append(themeTitle, themeGrid);
    panel.appendChild(themeSection);

    const avatarSection = document.createElement("section");
    avatarSection.className = "appearance-section";
    const avatarTitle = document.createElement("div");
    avatarTitle.className = "appearance-section-title";
    avatarTitle.innerHTML = '<span class="material-symbols-outlined">face</span><span>头像</span>';
    avatarSection.appendChild(avatarTitle);

    const avatarOrder = ["user", ...PERSONA_ORDER];
    avatarOrder.forEach(cid => {
      const item = data.avatars?.[cid];
      if (!item) return;
      const row = document.createElement("div");
      row.className = "appearance-row";

      const preview = document.createElement("img");
      preview.className = "appearance-avatar-preview";
      preview.src = item.url;
      preview.alt = cid === "user" ? "User头像" : `${nickName(cid)}头像`;

      const copy = document.createElement("div");
      copy.className = "appearance-row-copy";
      const name = document.createElement("span");
      name.className = "appearance-row-name";
      name.textContent = cid === "user" ? "User" : nickName(cid);
      const state = document.createElement("span");
      state.className = "appearance-row-state";
      state.textContent = item.custom ? (item.filename || "自定义头像") : "默认头像";
      copy.append(name, state);

      const actions = document.createElement("div");
      actions.className = "appearance-actions";
      const input = document.createElement("input");
      input.type = "file";
      input.accept = "image/jpeg,image/png,image/gif,image/webp";
      input.hidden = true;
      const upload = makeAppearanceIcon("photo_camera", "更换头像", () => input.click());
      const reset = makeAppearanceIcon("restart_alt", "恢复默认头像", async () => {
        reset.disabled = true;
        try {
          await resetAppearanceAsset(`avatar_${cid}`);
          showToast("已经换回默认头像");
          await renderAppearancePanel();
        } catch (e) {
          showToast(e.message);
          reset.disabled = false;
        }
      }, !item.custom);
      input.addEventListener("change", async () => {
        const file = input.files?.[0];
        if (!file) return;
        upload.disabled = true;
        try {
          await saveAppearanceAsset(`avatar_${cid}`, file);
          showToast("新头像戴好啦");
          await renderAppearancePanel();
        } catch (e) {
          showToast(e.message);
          upload.disabled = false;
        }
      });
      actions.append(upload, reset, input);
      row.append(preview, copy, actions);
      avatarSection.appendChild(row);
    });
    panel.appendChild(avatarSection);

    const background = data.chat_background;
    const backgroundSection = document.createElement("section");
    backgroundSection.className = "appearance-section";
    const backgroundTitle = document.createElement("div");
    backgroundTitle.className = "appearance-section-title";
    backgroundTitle.innerHTML = '<span class="material-symbols-outlined">wallpaper</span><span>聊天背景</span>';
    const backgroundWrap = document.createElement("div");
    backgroundWrap.className = "appearance-background-wrap";
    const backgroundPreview = document.createElement("img");
    backgroundPreview.className = "appearance-background-preview";
    backgroundPreview.src = background.url;
    backgroundPreview.alt = "聊天背景预览";
    const backgroundBar = document.createElement("div");
    backgroundBar.className = "appearance-background-bar";
    const backgroundCopy = document.createElement("div");
    backgroundCopy.className = "appearance-row-copy";
    const backgroundName = document.createElement("span");
    backgroundName.className = "appearance-row-name";
    backgroundName.textContent = "单聊与群聊";
    const backgroundState = document.createElement("span");
    backgroundState.className = "appearance-row-state";
    backgroundState.textContent = background.custom ? (background.filename || "自定义背景") : "默认背景";
    backgroundCopy.append(backgroundName, backgroundState);
    const backgroundActions = document.createElement("div");
    backgroundActions.className = "appearance-actions";
    const backgroundInput = document.createElement("input");
    backgroundInput.type = "file";
    backgroundInput.accept = "image/jpeg,image/png,image/gif,image/webp";
    backgroundInput.hidden = true;
    const backgroundUpload = makeAppearanceIcon("add_photo_alternate", "更换聊天背景", () => backgroundInput.click());
    const backgroundReset = makeAppearanceIcon("restart_alt", "恢复默认背景", async () => {
      backgroundReset.disabled = true;
      try {
        await resetAppearanceAsset("background_chat");
        showToast("已经换回默认背景");
        await renderAppearancePanel();
      } catch (e) {
        showToast(e.message);
        backgroundReset.disabled = false;
      }
    }, !background.custom);
    backgroundInput.addEventListener("change", async () => {
      const file = backgroundInput.files?.[0];
      if (!file) return;
      backgroundUpload.disabled = true;
      try {
        await saveAppearanceAsset("background_chat", file);
        showToast("新背景铺好啦");
        await renderAppearancePanel();
      } catch (e) {
        showToast(e.message);
        backgroundUpload.disabled = false;
      }
    });
    backgroundActions.append(backgroundUpload, backgroundReset, backgroundInput);
    backgroundBar.append(backgroundCopy, backgroundActions);
    backgroundWrap.append(backgroundPreview, backgroundBar);
    backgroundSection.append(backgroundTitle, backgroundWrap);
    panel.appendChild(backgroundSection);
  }

  async function openAppearancePanel() {
    if (_personaPanelOpen) closePersonaPanel();
    if (_toolsPanelOpen) closeToolsPanel();
    if (_schedulerPanelOpen) closeSchedulerPanel();
    if (_memoryImportPanelOpen) closeMemoryImportPanel();
    const panel = document.getElementById("appearancePanel");
    panel.style.display = "flex";
    _appearancePanelOpen = true;
    await renderAppearancePanel();
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function closeAppearancePanel() {
    const panel = document.getElementById("appearancePanel");
    panel.style.display = "none";
    panel.innerHTML = "";
    _appearancePanelOpen = false;
    document.getElementById("moreContent").scrollTop = 0;
  }

  let _personaPanelOpen = false;

  async function openPersonaPanel() {
    if (_appearancePanelOpen) closeAppearancePanel();
    if (_toolsPanelOpen) closeToolsPanel();
    if (_schedulerPanelOpen) closeSchedulerPanel();
    if (_memoryImportPanelOpen) closeMemoryImportPanel();
    const panel = document.getElementById("personaPanel");
    const placeholder = panel.nextElementSibling;
    panel.innerHTML = "";

    let personas;
    let characterConfig;
    try {
      const [personaRes, configRes] = await Promise.all([
        fetch("/api/personas"),
        fetch("/api/character-config"),
      ]);
      personas = await personaRes.json();
      characterConfig = await configRes.json();
    } catch (e) {
      panel.innerHTML = '<p style="padding:16px;color:var(--muted);">加载失败，请重试</p>';
      return;
    }

    // 关闭按钮
    const closeBtn = document.createElement("button");
    closeBtn.className = "persona-close-btn";
    closeBtn.textContent = "收起";
    closeBtn.addEventListener("click", closePersonaPanel);
    panel.appendChild(closeBtn);

    PERSONA_ORDER.forEach(cid => {
      const char = personas[cid];
      if (char === undefined) return;
      const card = document.createElement("div");
      card.className = "persona-card";

      const header = document.createElement("div");
      header.className = "persona-card-header";
      const avatar = document.createElement("img");
      avatar.src = charAvatars[cid] || "";
      avatar.onerror = function() { this.style.display = "none"; };
      avatar.style.cssText = "width:36px;height:36px;border-radius:50%;object-fit:cover;flex-shrink:0;";
      const nameEl = document.createElement("span");
      nameEl.className = "persona-card-name";
      nameEl.textContent = nickName(cid);
      header.appendChild(avatar);
      header.appendChild(nameEl);

      const modelWrap = document.createElement("label");
      modelWrap.className = "persona-model-wrap";
      const modelCaption = document.createElement("span");
      modelCaption.textContent = `模型 · ${characterConfig[cid]?.provider === "anthropic" ? "Anthropic" : "OpenRouter"}`;
      const modelInput = document.createElement("input");
      modelInput.className = "persona-model-input";
      modelInput.type = "text";
      modelInput.value = characterConfig[cid]?.model || "";
      modelInput.autocomplete = "off";
      modelInput.spellcheck = false;
      modelWrap.appendChild(modelCaption);
      modelWrap.appendChild(modelInput);

      const textarea = document.createElement("textarea");
      textarea.className = "persona-textarea";
      textarea.value = char;
      textarea.addEventListener("focus", () => {
        setTimeout(() => textarea.scrollIntoView({ behavior: "smooth", block: "center" }), 400);
      });

      const footer = document.createElement("div");
      footer.style.cssText = "display:flex;align-items:center;gap:10px;margin-top:8px;";
      const saveBtn = document.createElement("button");
      saveBtn.className = "persona-save-btn";
      saveBtn.textContent = "保存";
      const savedMsg = document.createElement("span");
      savedMsg.className = "persona-saved-msg";

      saveBtn.addEventListener("click", async () => {
        saveBtn.disabled = true;
        try {
          const res = await fetch(`/api/character-config/${cid}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ persona: textarea.value, model: modelInput.value }),
          });
          if (res.ok) {
            savedMsg.textContent = "✓ 已保存";
            setTimeout(() => { savedMsg.textContent = ""; }, 2000);
          } else {
            const errorData = await res.json().catch(() => ({}));
            savedMsg.textContent = errorData.error || "保存失败";
          }
        } catch (e) {
          savedMsg.textContent = "网络错误";
        } finally {
          saveBtn.disabled = false;
        }
      });

      footer.appendChild(saveBtn);
      footer.appendChild(savedMsg);
      card.appendChild(header);
      card.appendChild(modelWrap);
      card.appendChild(textarea);
      card.appendChild(footer);
      panel.appendChild(card);
    });

    panel.style.display = "flex";
    placeholder.style.display = "none";
    _personaPanelOpen = true;
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function closePersonaPanel() {
    const panel = document.getElementById("personaPanel");
    const placeholder = panel.nextElementSibling;
    panel.style.display = "none";
    panel.innerHTML = "";
    placeholder.style.display = "";
    _personaPanelOpen = false;
    document.getElementById("moreContent").scrollTop = 0;
  }

  // ── 旧 Ombre 记忆迁移 ──
  let _memoryImportPanelOpen = false;

  function openMemoryImportPanel() {
    if (_appearancePanelOpen) closeAppearancePanel();
    if (_personaPanelOpen) closePersonaPanel();
    if (_toolsPanelOpen) closeToolsPanel();
    if (_schedulerPanelOpen) closeSchedulerPanel();
    const panel = document.getElementById("memoryImportPanel");
    panel.innerHTML = "";
    panel.style.display = "flex";
    panel.style.flexDirection = "column";
    panel.style.padding = "12px 16px 16px";
    panel.style.gap = "12px";

    const closeBtn = document.createElement("button");
    closeBtn.className = "persona-close-btn";
    closeBtn.textContent = "收起";
    closeBtn.onclick = closeMemoryImportPanel;

    const card = document.createElement("section");
    card.className = "memory-import-card";
    const title = document.createElement("div");
    title.className = "memory-import-title";
    title.innerHTML = '<span class="material-symbols-outlined">history</span><strong>旧 Ombre</strong>';

    const urlLabel = document.createElement("label");
    urlLabel.textContent = "Dashboard 地址";
    const urlInput = document.createElement("input");
    urlInput.type = "url";
    urlInput.placeholder = "https://your-ombre.example.com";
    urlInput.autocomplete = "url";
    urlLabel.appendChild(urlInput);

    const passwordLabel = document.createElement("label");
    passwordLabel.textContent = "Dashboard 密码";
    const passwordInput = document.createElement("input");
    passwordInput.type = "password";
    passwordInput.autocomplete = "current-password";
    passwordLabel.appendChild(passwordInput);

    const status = document.createElement("div");
    status.className = "memory-import-status";
    const migrate = document.createElement("button");
    migrate.className = "memory-import-submit";
    migrate.textContent = "迁移六人记忆";
    migrate.onclick = async () => {
      migrate.disabled = true;
      migrate.textContent = "正在迁移…";
      status.textContent = "";
      try {
        const response = await fetch("/api/memory/import-legacy", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url: urlInput.value, password: passwordInput.value }),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "迁移失败");
        passwordInput.value = "";
        status.className = "memory-import-status ok";
        status.textContent = `迁入 ${data.imported}，已有 ${data.skipped}，失败 ${data.errors}`;
        migrate.textContent = "迁移完成";
        await loadMemoryView();
      } catch (error) {
        status.className = "memory-import-status error";
        status.textContent = error.message;
        migrate.textContent = "重新迁移";
      } finally {
        migrate.disabled = false;
      }
    };

    card.appendChild(title);
    card.appendChild(urlLabel);
    card.appendChild(passwordLabel);
    card.appendChild(migrate);
    card.appendChild(status);
    panel.appendChild(closeBtn);
    panel.appendChild(card);
    _memoryImportPanelOpen = true;
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function closeMemoryImportPanel() {
    const panel = document.getElementById("memoryImportPanel");
    panel.style.display = "none";
    panel.innerHTML = "";
    _memoryImportPanelOpen = false;
    document.getElementById("moreContent").scrollTop = 0;
  }

  // personaToggle replaced by paw-menu
  function showToast(msg) {
    let el = document.getElementById("toastEl");
    if (!el) {
      el = document.createElement("div");
      el.id = "toastEl";
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.remove("show"), 2200);
  }

  document.querySelector(".more-menu").addEventListener("click", e => {
    const btn = e.target.closest("[data-action]");
    if (!btn) return;
    const action = btn.dataset.action;
    if (action === "persona") {
      if (_personaPanelOpen) closePersonaPanel();
      else openPersonaPanel();
    } else if (action === "appearance") {
      if (_appearancePanelOpen) closeAppearancePanel();
      else openAppearancePanel();
    } else if (action === "logout") {
      doLogout();
    } else if (action === "mcp") {
      if (_toolsPanelOpen) closeToolsPanel();
      else openToolsPanel();
    } else if (action === "scheduler") {
      if (_schedulerPanelOpen) closeSchedulerPanel();
      else openSchedulerPanel();
    } else if (action === "memory-import") {
      if (_memoryImportPanelOpen) closeMemoryImportPanel();
      else openMemoryImportPanel();
    }
  });

  // ── 关键词搜索 ──
  let _srchMatches = [], _srchIdx = 0, _srchOriginals = new Map();

  function _escRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

  function _highlightAll(q) {
    _clearHighlights();
    if (!q.trim()) return;
    const bubbles = document.getElementById('messages').querySelectorAll('.bubble');
    bubbles.forEach(b => {
      if (!_srchOriginals.has(b)) _srchOriginals.set(b, b.innerHTML);
      if (!b.textContent.toLowerCase().includes(q.toLowerCase())) return;
      b.innerHTML = _srchOriginals.get(b).replace(
        new RegExp(_escRe(q), 'gi'),
        m => `<mark class="srch-mark">${m}</mark>`
      );
      b.querySelectorAll('.srch-mark').forEach(m => _srchMatches.push(m));
    });
    _updateCounter();
    if (_srchMatches.length) { _srchIdx = 0; _jumpTo(0); }
  }

  function _clearHighlights() {
    _srchOriginals.forEach((orig, b) => { b.innerHTML = orig; });
    _srchOriginals.clear(); _srchMatches = []; _srchIdx = 0;
    _updateCounter();
  }

  function _jumpTo(i) {
    _srchMatches.forEach((m, j) => m.classList.toggle('srch-mark-active', j === i));
    _srchMatches[i]?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    _srchIdx = i; _updateCounter();
  }

  function _updateCounter() {
    const el = document.getElementById('searchCounter');
    if (el) el.textContent = _srchMatches.length
      ? `${_srchIdx + 1}/${_srchMatches.length}` : '';
  }

  document.getElementById('searchToggle').addEventListener('click', () => {
    const bar = document.getElementById('searchBar');
    const visible = bar.style.display !== 'none';
    bar.style.display = visible ? 'none' : 'flex';
    if (!visible) {
      syncSearchInputInk();
      document.getElementById('searchInput').focus();
    }
    else {
      _clearHighlights();
      document.getElementById('searchInput').value = '';
      syncSearchInputInk();
    }
  });

  document.getElementById('searchInput').addEventListener('input', e => {
    syncSearchInputInk();
    _highlightAll(e.target.value);
  });

  document.getElementById('searchPrev').addEventListener('click', () => {
    if (!_srchMatches.length) return;
    _jumpTo((_srchIdx - 1 + _srchMatches.length) % _srchMatches.length);
  });

  document.getElementById('searchNext').addEventListener('click', () => {
    if (!_srchMatches.length) return;
    _jumpTo((_srchIdx + 1) % _srchMatches.length);
  });

  document.getElementById('searchClose').addEventListener('click', () => {
    document.getElementById('searchBar').style.display = 'none';
    document.getElementById('searchInput').value = '';
    syncSearchInputInk();
    _clearHighlights();
  });

  // ── 朋友圈 ──────────────────────────────────────────────
  const AUTHOR_NAMES = {
    user: "User", char1: "Char 1", char2: "Char 2",
    char3: "Char 3", char4: "Char 4", char5: "Char 5",
    char6: "Char 6",
  };

  function formatMomentTime(ts) {
    const d = new Date(ts.replace(" ", "T") + "Z");
    const now = new Date();
    const diff = (now - d) / 1000;
    if (diff < 60) return "刚刚";
    if (diff < 3600) return Math.floor(diff / 60) + "分钟前";
    if (diff < 86400) return Math.floor(diff / 3600) + "小时前";
    return `${d.getMonth() + 1}/${d.getDate()}`;
  }

  let momentsRenderVersion = 0;
  async function renderMoments(moments) {
    const feed = document.getElementById("momentsFeed");
    const renderVersion = ++momentsRenderVersion;
    if (moments.length === 0) {
      feed.innerHTML = '<div style="text-align:center;color:#ccc;margin-top:40px;font-size:14px;">还没有动态，来发一条吧～</div>';
      return;
    }
    const fragment = document.createDocumentFragment();
    moments.forEach(m => {
      const card = document.createElement("div");
      card.className = "moment-card";
      card.dataset.momentId = m.id;

      const avatarSrc = m.author_id === "user" ? userAvatar : (charAvatars[m.author_id] || "");
      const authorName = AUTHOR_NAMES[m.author_id] || m.author_id;

      let commentsHTML = "";
      if (m.comments && m.comments.length > 0) {
        commentsHTML = `<div class="moment-comments">` +
          m.comments.map(c => {
            const cName = AUTHOR_NAMES[c.author_id] || c.author_id;
            return `<div class="moment-comment"><span class="comment-author">${cName}：</span>${c.content}</div>`;
          }).join("") +
          `</div>`;
      }

      card.innerHTML = `
        <div class="moment-header">
          <img src="${avatarSrc}" onerror="this.style.display='none'">
          <span class="moment-author">${authorName}</span>
          <span class="moment-time">${formatMomentTime(m.created_at)}</span>
        </div>
        <div class="moment-content">${m.content}</div>
        ${commentsHTML}
        <div class="moment-user-comment">
          <input type="text" class="moment-comment-input" placeholder="说点什么…" data-id="${m.id}">
        </div>
        <div class="moment-footer">
          <button class="moment-delete-btn" data-id="${m.id}">删除</button>
          <button class="moment-comment-btn" data-id="${m.id}" title="邀请角色评论">◎</button>
        </div>
      `;
      fragment.appendChild(card);
    });

    await decodeImagesBeforeSwap(fragment);
    if (renderVersion !== momentsRenderVersion) return;
    feed.replaceChildren(fragment);

    feed.querySelectorAll(".moment-delete-btn").forEach(btn => {
      btn.addEventListener("click", async () => {
        if (!confirm("删除这条动态？")) return;
        await fetch(`/api/moments/${btn.dataset.id}`, { method: "DELETE" });
        loadMoments();
      });
    });

    feed.querySelectorAll(".moment-comment-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        openCharPicker("comment", parseInt(btn.dataset.id));
      });
    });

    feed.querySelectorAll(".moment-comment-input").forEach(input => {
      input.addEventListener("keydown", async e => {
        if (e.key !== "Enter") return;
        const content = input.value.trim();
        if (!content) return;
        await fetch(`/api/moments/${input.dataset.id}/comment`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ character_ids: [], user_comment: content }),
        });
        input.value = "";
        loadMoments();
      });
    });
  }

  async function loadMoments() {
    try {
      const res = await fetch("/api/moments");
      const data = await res.json();
      await renderMoments(data);
    } catch (e) {
      console.error("loadMoments error", e);
    }
  }

  // ── 共读室 ──────────────────────────────────────────────
  let activeNestPane = "moments";
  let readingBooks = [];
  let activeReadingBook = null;
  let activeReadingChapter = null;
  let activeReadingHighlight = null;
  let pendingReadingFile = null;
  let readingSelection = null;
  let readingProgressTimer = null;
  const readingHighlightMap = new Map();

  function escapeReadingHtml(value) {
    const div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  async function readingRequest(url, options = {}) {
    const response = await fetch(url, options);
    let data = {};
    try { data = await response.json(); } catch (_) {}
    if (!response.ok) throw new Error(data.error || "这次没有弄好，再试一下");
    return data;
  }

  function syncMomentsFab() {
    const inNest = document.getElementById("momentsView").classList.contains("active");
    momentsFab.classList.toggle("hidden", !inNest || activeNestPane !== "moments");
  }

  function setNestPane(pane) {
    activeNestPane = pane;
    document.querySelectorAll(".nest-tab").forEach(btn => {
      const active = btn.dataset.nestPane === pane;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    });
    document.getElementById("momentsPane").classList.toggle("hidden", pane !== "moments");
    document.getElementById("readingPane").classList.toggle("hidden", pane !== "reading");
    syncMomentsFab();
    if (pane === "moments") loadMoments();
    else loadReadingBooks();
  }

  document.querySelectorAll(".nest-tab").forEach(btn => {
    btn.addEventListener("click", () => setNestPane(btn.dataset.nestPane));
  });

  function renderReadingBooks() {
    const list = document.getElementById("readingBookList");
    document.getElementById("readingLibraryCount").textContent = readingBooks.length
      ? `${readingBooks.length} 本书在等你`
      : "书架空着";
    list.innerHTML = "";
    if (!readingBooks.length) {
      list.innerHTML = '<div class="reading-empty">放一本 TXT 进来，再挑一两位陪你慢慢读。</div>';
      return;
    }
    readingBooks.forEach(book => {
      const row = document.createElement("div");
      row.className = "reading-book-row";
      const people = (book.participants || []).map(person =>
        `<img src="${escapeReadingHtml(person.avatar)}" alt="${escapeReadingHtml(person.name)}">`
      ).join("");
      row.innerHTML = `
        <button type="button" class="reading-book-open" aria-label="打开 ${escapeReadingHtml(book.title)}">
          <div class="reading-book-main">
            <div class="reading-book-title">${escapeReadingHtml(book.title)}</div>
            <div class="reading-book-meta">${book.total_chapters} 章 · ${book.progress.percent}% · ${book.encoding.toUpperCase()}</div>
          </div>
          <div class="reading-book-people">${people}</div>
          <div class="reading-book-progress" aria-label="已读 ${book.progress.percent}%"><span style="width:${book.progress.percent}%"></span></div>
        </button>
        <button type="button" class="reading-book-delete" aria-label="删除 ${escapeReadingHtml(book.title)}" title="删除这本书">
          <span class="material-symbols-outlined">delete</span>
        </button>
      `;
      row.querySelector(".reading-book-open").addEventListener("click", () => openReadingBook(book.id));
      row.querySelector(".reading-book-delete").addEventListener("click", () => {
        showConfirmDialog(`把《${book.title}》和它的划线批注一起移出书架？`, async () => {
          try {
            await readingRequest(`/api/reading/books/${book.id}`, { method: "DELETE" });
            readingBooks = readingBooks.filter(item => item.id !== book.id);
            renderReadingBooks();
            showToast("已经从书架移走啦");
          } catch (error) { showToast(error.message); }
        });
      });
      list.appendChild(row);
    });
  }

  async function loadReadingBooks() {
    try {
      const data = await readingRequest("/api/reading/books");
      readingBooks = data.books || [];
      renderReadingBooks();
    } catch (error) {
      showToast(error.message);
    }
  }

  document.getElementById("readingUploadBtn").addEventListener("click", () => {
    document.getElementById("readingFileInput").click();
  });

  document.getElementById("readingFileInput").addEventListener("change", event => {
    const file = event.target.files?.[0];
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".txt")) {
      showToast("共读室现在只收 TXT");
      event.target.value = "";
      return;
    }
    pendingReadingFile = file;
    openCharPicker("reading_new");
  });

  async function uploadPendingReadingBook(characterIds) {
    if (!pendingReadingFile) return;
    const button = document.getElementById("readingUploadBtn");
    const form = new FormData();
    form.append("file", pendingReadingFile);
    form.append("participants", JSON.stringify(characterIds));
    button.disabled = true;
    showToast("正在把书放上书架……");
    try {
      const data = await readingRequest("/api/reading/books", { method: "POST", body: form });
      pendingReadingFile = null;
      document.getElementById("readingFileInput").value = "";
      await loadReadingBooks();
      await openReadingBook(data.book.id);
    } catch (error) {
      showToast(error.message);
    } finally {
      button.disabled = false;
    }
  }

  async function openReadingBook(bookId) {
    try {
      const data = await readingRequest(`/api/reading/books/${bookId}`);
      activeReadingBook = data.book;
      document.getElementById("readingBookTitle").textContent = activeReadingBook.title;
      const select = document.getElementById("readingChapterSelect");
      select.innerHTML = "";
      activeReadingBook.chapters.forEach(chapter => {
        const option = document.createElement("option");
        option.value = chapter.index;
        option.textContent = chapter.title;
        select.appendChild(option);
      });
      document.getElementById("readingLibraryView").classList.add("hidden");
      document.getElementById("readingReaderView").classList.remove("hidden");
      await loadReadingChapter(activeReadingBook.progress.current_chapter_index || 0, true);
    } catch (error) {
      showToast(error.message);
    }
  }

  function closeReadingBook() {
    clearTimeout(readingProgressTimer);
    readingSelection = null;
    activeReadingHighlight = null;
    document.getElementById("readingSelectionBar").classList.add("hidden");
    document.getElementById("readingReaderView").classList.add("hidden");
    document.getElementById("readingLibraryView").classList.remove("hidden");
    activeReadingBook = null;
    activeReadingChapter = null;
    loadReadingBooks();
  }

  document.getElementById("readingBackBtn").addEventListener("click", closeReadingBook);

  function appendReadingBlockContent(paragraph, block) {
    const highlights = [...(block.highlights || [])]
      .sort((a, b) => a.start_offset - b.start_offset || a.id - b.id);
    let cursor = 0;
    highlights.forEach(highlight => {
      readingHighlightMap.set(highlight.id, highlight);
      if (highlight.start_offset < cursor || highlight.end_offset > block.text.length) return;
      paragraph.appendChild(document.createTextNode(block.text.slice(cursor, highlight.start_offset)));
      const mark = document.createElement("mark");
      mark.className = "reading-mark" + (highlight.annotations?.length ? " has-annotations" : "");
      mark.textContent = block.text.slice(highlight.start_offset, highlight.end_offset);
      mark.dataset.highlightId = highlight.id;
      mark.addEventListener("click", event => {
        event.stopPropagation();
        openReadingAnnotation(readingHighlightMap.get(highlight.id));
      });
      paragraph.appendChild(mark);
      cursor = highlight.end_offset;
    });
    paragraph.appendChild(document.createTextNode(block.text.slice(cursor)));
  }

  function renderReadingChapter() {
    const content = document.getElementById("readingContent");
    content.innerHTML = "";
    readingHighlightMap.clear();
    (activeReadingChapter?.blocks || []).forEach(block => {
      const paragraph = document.createElement("p");
      paragraph.className = "reading-block";
      paragraph.dataset.blockId = block.id;
      paragraph.dataset.blockIndex = block.block_index;
      appendReadingBlockContent(paragraph, block);
      content.appendChild(paragraph);
    });
  }

  function renderReadingChapterInPlace(scrollTop = null) {
    const content = document.getElementById("readingContent");
    const preservedScrollTop = Number.isFinite(scrollTop) ? scrollTop : content.scrollTop;
    renderReadingChapter();
    content.scrollTop = preservedScrollTop;
  }

  async function loadReadingChapter(chapterIndex, restorePosition = false) {
    if (!activeReadingBook) return;
    try {
      const data = await readingRequest(`/api/reading/books/${activeReadingBook.id}/chapters/${chapterIndex}`);
      activeReadingChapter = data;
      activeReadingBook.progress = data.progress;
      document.getElementById("readingChapterSelect").value = String(chapterIndex);
      const lastIndex = activeReadingBook.chapters.length - 1;
      document.getElementById("readingPrevChapter").disabled = chapterIndex <= 0;
      document.getElementById("readingNextChapter").disabled = chapterIndex >= lastIndex;
      document.getElementById("readingReaderProgress").textContent = `已读 ${data.progress.percent}%`;
      renderReadingChapter();
      const content = document.getElementById("readingContent");
      content.scrollTop = 0;
      if (restorePosition && data.progress.current_chapter_index === chapterIndex) {
        requestAnimationFrame(() => {
          const target = content.querySelector(`[data-block-index="${data.progress.current_block_index}"]`);
          target?.scrollIntoView({ block: "start" });
        });
      }
    } catch (error) {
      showToast(error.message);
    }
  }

  document.getElementById("readingChapterSelect").addEventListener("change", event => {
    loadReadingChapter(Number(event.target.value));
  });
  document.getElementById("readingPrevChapter").addEventListener("click", () => {
    const index = Number(document.getElementById("readingChapterSelect").value);
    if (index > 0) loadReadingChapter(index - 1);
  });
  document.getElementById("readingNextChapter").addEventListener("click", () => {
    const index = Number(document.getElementById("readingChapterSelect").value);
    if (activeReadingBook && index < activeReadingBook.chapters.length - 1) loadReadingChapter(index + 1);
  });

  function captureReadingSelection() {
    const selection = window.getSelection();
    if (!selection || selection.rangeCount !== 1 || selection.isCollapsed) return;
    const range = selection.getRangeAt(0);
    const startElement = range.startContainer.nodeType === Node.TEXT_NODE
      ? range.startContainer.parentElement : range.startContainer;
    const endElement = range.endContainer.nodeType === Node.TEXT_NODE
      ? range.endContainer.parentElement : range.endContainer;
    const startBlock = startElement?.closest?.(".reading-block");
    const endBlock = endElement?.closest?.(".reading-block");
    if (!startBlock || !endBlock) return;
    const blockElements = [...document.querySelectorAll("#readingContent .reading-block")];
    const startBlockIndex = blockElements.indexOf(startBlock);
    const endBlockIndex = blockElements.indexOf(endBlock);
    if (startBlockIndex < 0 || endBlockIndex < startBlockIndex) return;
    if (endBlockIndex - startBlockIndex >= 32) {
      showToast("一次最多划 32 段");
      return;
    }
    const offsetInside = (element, container, offset) => {
      const before = document.createRange();
      before.selectNodeContents(element);
      before.setEnd(container, offset);
      return before.toString().length;
    };
    const absoluteStart = offsetInside(startBlock, range.startContainer, range.startOffset);
    const absoluteEnd = offsetInside(endBlock, range.endContainer, range.endOffset);
    const segments = [];
    for (let index = startBlockIndex; index <= endBlockIndex; index++) {
      const element = blockElements[index];
      const blockId = Number(element.dataset.blockId);
      const block = activeReadingChapter?.blocks.find(item => item.id === blockId);
      if (!block) continue;
      let startOffset = index === startBlockIndex ? absoluteStart : 0;
      let endOffset = index === endBlockIndex ? absoluteEnd : block.text.length;
      const selected = block.text.slice(startOffset, endOffset);
      const leading = selected.length - selected.trimStart().length;
      const trailing = selected.length - selected.trimEnd().length;
      startOffset += leading;
      endOffset -= trailing;
      if (endOffset <= startOffset) continue;
      const overlaps = block.highlights?.some(item =>
        startOffset < item.end_offset && endOffset > item.start_offset
      );
      if (overlaps) {
        showToast("选中的地方已经有划线啦");
        return;
      }
      segments.push({
        block_id: blockId,
        block_index: Number(element.dataset.blockIndex),
        start_offset: startOffset,
        end_offset: endOffset,
        quote: block.text.slice(startOffset, endOffset),
      });
    }
    if (!segments.length) return;
    const quote = segments.map(item => item.quote).join("\n\n");
    readingSelection = {
      segments,
      quote,
      scroll_top: document.getElementById("readingContent").scrollTop,
    };
    document.getElementById("readingSelectionBar").classList.remove("hidden");
  }

  document.getElementById("readingContent").addEventListener("pointerup", () => {
    setTimeout(captureReadingSelection, 20);
  });
  document.getElementById("readingContent").addEventListener("keyup", captureReadingSelection);

  function clearReadingSelection() {
    window.getSelection()?.removeAllRanges();
    readingSelection = null;
    document.getElementById("readingSelectionBar").classList.add("hidden");
  }

  async function createReadingHighlight(note = "") {
    if (!readingSelection || !activeReadingBook) return null;
    const { scroll_top: scrollTop, ...selectionPayload } = readingSelection;
    selectionPayload.note = note;
    const data = await readingRequest(`/api/reading/books/${activeReadingBook.id}/highlights`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(selectionPayload),
    });
    (data.highlights || [data.highlight]).forEach(highlight => {
      const block = activeReadingChapter.blocks.find(item => item.id === highlight.block_id);
      if (block) block.highlights.push(highlight);
    });
    if (data.progress) {
      activeReadingBook.progress = data.progress;
      document.getElementById("readingReaderProgress").textContent = `已读 ${data.progress.percent}%`;
    }
    clearReadingSelection();
    renderReadingChapterInPlace(scrollTop);
    return readingHighlightMap.get(data.highlight.id) || data.highlight;
  }

  document.getElementById("readingHighlightBtn").addEventListener("click", async () => {
    try {
      await createReadingHighlight();
      showToast("划好啦");
    } catch (error) { showToast(error.message); }
  });

  document.getElementById("readingNoteBtn").addEventListener("click", () => {
    if (!readingSelection) return;
    document.getElementById("readingNoteQuote").textContent = readingSelection.quote;
    document.getElementById("readingNoteInput").value = "";
    document.getElementById("readingNoteOverlay").classList.remove("hidden");
    syncReadingNoteToViewport();
    setTimeout(() => document.getElementById("readingNoteInput").focus(), 80);
  });

  function syncReadingNoteToViewport() {
    const overlay = document.getElementById("readingNoteOverlay");
    if (overlay.classList.contains("hidden") || !window.visualViewport) return;
    overlay.style.top = `${window.visualViewport.offsetTop}px`;
    overlay.style.height = `${window.visualViewport.height}px`;
  }
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", syncReadingNoteToViewport);
    window.visualViewport.addEventListener("scroll", syncReadingNoteToViewport);
  }

  function closeReadingNoteSheet() {
    const overlay = document.getElementById("readingNoteOverlay");
    overlay.classList.add("hidden");
    overlay.style.top = "";
    overlay.style.height = "";
  }
  document.getElementById("readingNoteCancel").addEventListener("click", closeReadingNoteSheet);
  document.getElementById("readingNoteOverlay").addEventListener("click", event => {
    if (event.target === event.currentTarget) closeReadingNoteSheet();
  });
  document.getElementById("readingNoteSave").addEventListener("click", async () => {
    const note = document.getElementById("readingNoteInput").value.trim();
    try {
      await createReadingHighlight(note);
      closeReadingNoteSheet();
      showToast("写在页边啦");
    } catch (error) { showToast(error.message); }
  });

  document.getElementById("readingAskBtn").addEventListener("click", async () => {
    try {
      const highlight = await createReadingHighlight();
      if (!highlight) return;
      activeReadingHighlight = highlight;
      openCharPicker(
        "reading_annotation",
        highlight.id,
        (activeReadingBook.participants || []).map(person => person.id),
      );
    } catch (error) { showToast(error.message); }
  });

  function renderReadingAnnotationSheet(highlight, loading = false) {
    const content = document.getElementById("readingAnnotationContent");
    const ownNote = highlight.note
      ? `<div class="reading-own-note">${escapeReadingHtml(highlight.note)}</div>` : "";
    const annotations = (highlight.annotations || []).map(annotation => `
      <div class="reading-annotation">
        <img src="${escapeReadingHtml(annotation.avatar)}" alt="">
        <div><strong>${escapeReadingHtml(annotation.author_name)}</strong><p>${escapeReadingHtml(annotation.content)}</p></div>
      </div>
    `).join("");
    content.innerHTML = `
      <div class="reading-annotation-quote">${escapeReadingHtml(highlight.quote)}</div>
      ${ownNote}
      ${annotations || '<div class="reading-annotation-empty">页边还安安静静的。</div>'}
      ${loading ? '<div class="reading-annotation-empty">正在低头写批注……</div>' : ''}
    `;
  }

  function openReadingAnnotation(highlight) {
    if (!highlight) return;
    activeReadingHighlight = highlight;
    renderReadingAnnotationSheet(highlight);
    document.getElementById("readingAnnotationOverlay").classList.remove("hidden");
  }

  function closeReadingAnnotationSheet() {
    document.getElementById("readingAnnotationOverlay").classList.add("hidden");
  }
  document.getElementById("readingAnnotationClose").addEventListener("click", closeReadingAnnotationSheet);
  document.getElementById("readingAnnotationOverlay").addEventListener("click", event => {
    if (event.target === event.currentTarget) closeReadingAnnotationSheet();
  });
  document.getElementById("readingAnnotationAsk").addEventListener("click", () => {
    if (!activeReadingHighlight || !activeReadingBook) return;
    closeReadingAnnotationSheet();
    openCharPicker(
      "reading_annotation",
      activeReadingHighlight.id,
      (activeReadingBook.participants || []).map(person => person.id),
    );
  });
  document.getElementById("readingAnnotationDelete").addEventListener("click", () => {
    if (!activeReadingHighlight) return;
    const highlightId = activeReadingHighlight.id;
    const scrollTop = document.getElementById("readingContent").scrollTop;
    showConfirmDialog("删掉这条划线和页边批注？", async () => {
      try {
        await readingRequest(`/api/reading/highlights/${highlightId}`, { method: "DELETE" });
        closeReadingAnnotationSheet();
        activeReadingHighlight = null;
        activeReadingChapter.blocks.forEach(block => {
          block.highlights = (block.highlights || []).filter(item => item.id !== highlightId);
        });
        renderReadingChapterInPlace(scrollTop);
      } catch (error) { showToast(error.message); }
    });
  });

  async function requestReadingAnnotations(highlightId, characterIds) {
    const scrollTop = document.getElementById("readingContent").scrollTop;
    let highlight = readingHighlightMap.get(Number(highlightId)) || activeReadingHighlight;
    if (highlight) {
      activeReadingHighlight = highlight;
      renderReadingAnnotationSheet(highlight, true);
      document.getElementById("readingAnnotationOverlay").classList.remove("hidden");
    }
    try {
      const data = await readingRequest(`/api/reading/highlights/${highlightId}/annotate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ character_ids: characterIds }),
      });
      const id = Number(highlightId);
      activeReadingChapter.blocks
        .flatMap(block => block.highlights || [])
        .filter(item => item.id === id)
        .forEach(storedHighlight => {
          storedHighlight.annotations = [
            ...(storedHighlight.annotations || []),
            ...(data.annotations || []),
          ];
        });
      renderReadingChapterInPlace(scrollTop);
      highlight = readingHighlightMap.get(id);
      if (highlight) openReadingAnnotation(highlight);
    } catch (error) {
      showToast(error.message);
      if (highlight) renderReadingAnnotationSheet(highlight);
    }
  }

  document.getElementById("readingParticipantsBtn").addEventListener("click", () => {
    if (!activeReadingBook) return;
    openCharPicker(
      "reading_participants",
      activeReadingBook.id,
      (activeReadingBook.participants || []).map(person => person.id),
    );
  });

  async function saveReadingParticipants(bookId, characterIds) {
    try {
      const data = await readingRequest(`/api/reading/books/${bookId}/participants`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ character_ids: characterIds }),
      });
      if (activeReadingBook?.id === Number(bookId)) activeReadingBook.participants = data.participants;
      showToast("共读座位换好啦");
    } catch (error) { showToast(error.message); }
  }

  async function persistReadingPosition() {
    if (!activeReadingBook || !activeReadingChapter) return;
    const content = document.getElementById("readingContent");
    const blocks = [...content.querySelectorAll(".reading-block")];
    if (!blocks.length) return;
    const contentTop = content.getBoundingClientRect().top;
    let current = blocks[0];
    for (const block of blocks) {
      if (block.getBoundingClientRect().top <= contentTop + 90) current = block;
      else break;
    }
    try {
      const data = await readingRequest(`/api/reading/books/${activeReadingBook.id}/progress`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ block_index: Number(current.dataset.blockIndex), offset: 0 }),
      });
      activeReadingBook.progress = data.progress;
      document.getElementById("readingReaderProgress").textContent = `已读 ${data.progress.percent}%`;
    } catch (_) {}
  }

  document.getElementById("readingContent").addEventListener("scroll", () => {
    document.getElementById("readingSelectionBar").classList.add("hidden");
    clearTimeout(readingProgressTimer);
    readingProgressTimer = setTimeout(persistReadingPosition, 1100);
  }, { passive: true });

  // ── 猫窝写帖弹窗 ──
  const momentsFab    = document.getElementById("momentsFab");
  const momentModal   = document.getElementById("momentWriteModal");
  const momentInputEl = document.getElementById("momentInput");

  // iOS 键盘适配：键盘弹出时 visual viewport 缩小，让 modal 跟随
  function syncModalToViewport() {
    if (momentModal.classList.contains("hidden")) return;
    const vv = window.visualViewport;
    momentModal.style.top    = `${vv.offsetTop}px`;
    momentModal.style.height = `${vv.height}px`;
  }
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", syncModalToViewport);
    window.visualViewport.addEventListener("scroll", syncModalToViewport);
  }

  function openMomentWrite() {
    momentModal.classList.remove("hidden");
    syncModalToViewport();
    setTimeout(() => momentInputEl.focus(), 80);
  }
  function closeMomentWrite() {
    momentModal.classList.add("hidden");
    momentModal.style.top    = "";
    momentModal.style.height = "";
    momentInputEl.value = "";
  }

  momentsFab.addEventListener("click", openMomentWrite);

  momentModal.addEventListener("click", e => {
    if (e.target === momentModal) closeMomentWrite();
  });

  document.getElementById("momentSubmitBtn").addEventListener("click", async () => {
    const content = momentInputEl.value.trim();
    if (!content) return;
    await fetch("/api/moments", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    });
    closeMomentWrite();
    loadMoments();
  });

  document.getElementById("momentFeedBtn").addEventListener("click", async () => {
    const btn = document.getElementById("momentFeedBtn");
    btn.disabled = true;
    btn.textContent = "投喂中…";
    closeMomentWrite();
    try {
      await fetch("/api/moments/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      loadMoments();
    } finally {
      btn.disabled = false;
      btn.textContent = "猫条 🐾";
    }
  });

  document.querySelectorAll(".nav-item").forEach(item => {
    item.addEventListener("click", () => {
      if (item.dataset.view === "momentsView") {
        if (activeNestPane === "moments") loadMoments();
        else loadReadingBooks();
        syncMomentsFab();
      } else {
        momentsFab.classList.add("hidden");
      }
    });
  });

  // ── 页面加载：检查登录态，再初始化 ──
  document.addEventListener("DOMContentLoaded", async () => {
    await loadSettings();
    await loadAppearance();
    await loadGroupConfig();
    // 群聊标题初始化 + 长按可改
    const groupTitleEl = document.getElementById("groupChatTitle");
    if (groupTitleEl) {
      groupTitleEl.textContent = groupNickName();
      makeLongPressEditable(groupTitleEl, "group_name", null);
    }
    // 单聊 header 名字长按可改（动态读当前角色）
    const charNameEl = document.getElementById("char-name");
    if (charNameEl) {
      makeLongPressEditable(charNameEl, () => "nickname:" + currentChar, val => {
        document.querySelectorAll(`.char-list-name[data-cid="${currentChar}"]`)
          .forEach(el => { el.textContent = val; });
      });
    }
    initCharList();      // 内部检测401 → 自动弹登录遮罩
    initStickers();      // 拉表情包列表，供猫爪菜单选择面板使用
    startSplashDismiss(); // 已登录时正常2s后淡出；未登录时splash已被showLoginOverlay移除
  });
