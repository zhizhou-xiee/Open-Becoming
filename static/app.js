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

  function clearNativeSelection(settle = false) {
    const clear = () => {
      const selection = window.getSelection?.();
      if (selection && !selection.isCollapsed) selection.removeAllRanges();
    };
    clear();
    if (settle) {
      requestAnimationFrame(clear);
      setTimeout(clear, 80);
    }
  }

  function bindNativeLongPressGuard(element, targetSelector = null) {
    element?.addEventListener("contextmenu", event => {
      if (targetSelector && !event.target.closest(targetSelector)) return;
      event.preventDefault();
      clearNativeSelection();
    });
  }

  function makeLongPressEditable(el, keyOrGetter, onSave) {
    let timer;
    bindNativeLongPressGuard(el);
    el.addEventListener("touchstart", e => {
      timer = setTimeout(() => {
        e.preventDefault();
        clearNativeSelection();
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

  const _isStandalonePwa =
    window.matchMedia?.("(display-mode: standalone)")?.matches ||
    window.navigator.standalone === true;
  if (_isStandalonePwa) document.documentElement.classList.add("standalone-pwa");

  function setAppHeight() {
    const viewport = window.visualViewport;
    const keyboardOpen = viewport && window.innerHeight - viewport.height > 120;
    const height = _isStandalonePwa && !keyboardOpen
      ? "100vh"
      : `${viewport ? viewport.height : window.innerHeight}px`;
    document.documentElement.style.setProperty("--app-height", height);
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
      const musicScroll = document.getElementById('musicRoomScroll');
      if (musicScroll && document.activeElement?.id === 'musicMessageInput') {
        musicScroll.scrollTop = musicScroll.scrollHeight;
      }
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
    document.documentElement.classList.remove("chrome-shell");
  }

  // Any API request can be the first one made after APP_PASSWORD changes.
  // Reopen the login screen instead of leaving a restored iOS page looking usable.
  const nativeFetch = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const response = await nativeFetch(...args);
    const target = args[0];
    const targetUrl = typeof target === "string" ? target : target?.url;
    if (response.status === 401 && targetUrl) {
      const url = new URL(targetUrl, window.location.href);
      if (
        url.origin === window.location.origin
        && url.pathname.startsWith("/api/")
        && url.pathname !== "/api/login"
      ) showLoginOverlay();
    }
    return response;
  };

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
      await loadVoiceFeatureState();
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
  const imperialInputEl = document.getElementById("imperialInput");
  const groupInputEl = document.getElementById("groupInput");
  const imperialGroupInputEl = document.getElementById("imperialGroupInput");
  const sendBtn    = document.getElementById("send");
  const voiceRecordBtn = document.getElementById("voiceRecord");
  const voiceFileInput = document.getElementById("voiceFileInput");
  const charSubEl  = document.getElementById("char-sub");
  let voiceConfigState = null;
  const DEFAULT_COMPOSER_PLACEHOLDER = "小猫酝酿坏主意中…";
  const IMPERIAL_COMPOSER_PLACEHOLDER = "传朕旨意…";
  const IMPERIAL_DELIVERY_PLACEHOLDER = "旨意宣读中…";
  const GROUP_REPLY_PLACEHOLDER = "角色们正在回复…";
  const GROUP_CONTINUE_PLACEHOLDER = "祂们聊起来了…";
  const IMPERIAL_GROUP_REPLY_PLACEHOLDER = "群臣正在商议回奏…";

  function isImperialTheme() {
    return document.documentElement.dataset.theme === "imperial";
  }

  function imperialComposerPlaceholder(value) {
    if (value === DEFAULT_COMPOSER_PLACEHOLDER) {
      return IMPERIAL_COMPOSER_PLACEHOLDER;
    }
    if (value === GROUP_REPLY_PLACEHOLDER || value === GROUP_CONTINUE_PLACEHOLDER) {
      return IMPERIAL_GROUP_REPLY_PLACEHOLDER;
    }
    return value;
  }

  function fitImperialComposer(editor) {
    if (!editor || !isImperialTheme()) return;
    editor.style.height = "0px";
    const minHeight = 38;
    const maxHeight = 126;
    const nextHeight = Math.min(maxHeight, Math.max(minHeight, editor.scrollHeight));
    editor.style.height = `${nextHeight}px`;
    editor.style.overflowY = editor.scrollHeight > maxHeight ? "auto" : "hidden";
  }

  function syncComposer(source, imperialEditor) {
    if (!source || !imperialEditor) return;
    imperialEditor.value = source.value;
    imperialEditor.placeholder = imperialComposerPlaceholder(source.placeholder);
    imperialEditor.disabled = source.disabled;
    fitImperialComposer(imperialEditor);
  }

  function syncImperialComposers() {
    syncComposer(inputEl, imperialInputEl);
    syncComposer(groupInputEl, imperialGroupInputEl);
    requestAnimationFrame(() => {
      fitImperialComposer(imperialInputEl);
      fitImperialComposer(imperialGroupInputEl);
    });
  }

  function setSingleComposerValue(value) {
    inputEl.value = value;
    if (imperialInputEl) {
      imperialInputEl.value = value;
      fitImperialComposer(imperialInputEl);
    }
  }

  function setSingleComposerPlaceholder(value) {
    inputEl.placeholder = value;
    if (imperialInputEl) {
      imperialInputEl.placeholder = imperialComposerPlaceholder(value);
    }
  }

  function setSingleComposerDeliveryState(delivering) {
    if (!imperialInputEl) return;
    imperialInputEl.placeholder = delivering
      ? IMPERIAL_DELIVERY_PLACEHOLDER
      : imperialComposerPlaceholder(inputEl.placeholder);
  }

  function setSingleComposerDisabled(disabled) {
    inputEl.disabled = disabled;
    if (imperialInputEl) imperialInputEl.disabled = disabled;
  }

  function focusSingleComposer() {
    (isImperialTheme() ? imperialInputEl : inputEl)?.focus();
  }

  function setGroupComposerValue(value) {
    groupInputEl.value = value;
    if (imperialGroupInputEl) {
      imperialGroupInputEl.value = value;
      fitImperialComposer(imperialGroupInputEl);
    }
  }

  function setGroupComposerState(disabled, placeholder) {
    groupInputEl.disabled = disabled;
    groupInputEl.placeholder = placeholder;
    if (imperialGroupInputEl) {
      imperialGroupInputEl.disabled = disabled;
      imperialGroupInputEl.placeholder = imperialComposerPlaceholder(placeholder);
    }
  }

  function focusGroupComposer() {
    (isImperialTheme() ? imperialGroupInputEl : groupInputEl)?.focus();
  }

  function blurGroupComposer() {
    (isImperialTheme() ? imperialGroupInputEl : groupInputEl)?.blur();
  }

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
  let singleReplyTarget = null;
  let singleLatestPinToken = 0;
  let singleLatestPinActive = false;
  let singleLatestMediaCleanup = null;
  let singleHistoryRenderToken = 0;
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
  const charRows = {};
  const charAvatars = {
    char1: "/static/char1.svg",
    char2: "/static/char2.svg",
    char3: "/static/char3.svg",
    char4: "/static/char4.svg",
    char5: "/static/char5.svg",
    char6: "/static/char6.svg",
  };
  const IMPERIAL_CHARACTER_ART = {
    char1: "/static/imperial/portrait-char1.webp",
    char2: "/static/imperial/portrait-char2.webp",
    char3: "/static/imperial/portrait-char3.webp",
    char4: "/static/imperial/portrait-char4.webp",
    char5: "/static/imperial/portrait-char5.webp",
    char6: "/static/imperial/portrait-char6.webp",
  };
  const IMPERIAL_MODEL_MARKS = {
    openai: "/static/imperial/logo-openai.svg",
    claude: "/static/imperial/logo-claude.svg",
    gemini: "/static/imperial/logo-gemini.svg",
    grok: "/static/imperial/logo-grok.svg",
    deepseek: "/static/imperial/logo-deepseek.svg",
    openrouter: "/static/imperial/logo-openrouter.svg",
    generic: "/static/imperial/logo-generic.svg",
  };

  function getModelBrand(modelId) {
    const normalized = String(modelId || "").trim().toLowerCase();
    const hasPrefix = prefix => (
      normalized.startsWith(prefix) || normalized.includes(`/${prefix}`)
    );

    // Brand follows the actual model id, never the API transport/provider.
    if (hasPrefix("openai/") || /(^|[/_.:-])gpt(?:[-/_.:]|$)/.test(normalized)) {
      return { key: "openai", label: "OpenAI", logo: IMPERIAL_MODEL_MARKS.openai };
    }
    if (hasPrefix("anthropic/") || normalized.includes("claude")) {
      return { key: "claude", label: "Anthropic", logo: IMPERIAL_MODEL_MARKS.claude };
    }
    if (hasPrefix("google/") || normalized.includes("gemini")) {
      return { key: "gemini", label: "Google Gemini", logo: IMPERIAL_MODEL_MARKS.gemini };
    }
    if (hasPrefix("x-ai/") || hasPrefix("xai/") || normalized.includes("grok")) {
      return { key: "grok", label: "xAI Grok", logo: IMPERIAL_MODEL_MARKS.grok };
    }
    if (hasPrefix("deepseek/") || normalized.includes("deepseek")) {
      return { key: "deepseek", label: "DeepSeek", logo: IMPERIAL_MODEL_MARKS.deepseek };
    }
    if (normalized.includes("openrouter")) {
      return { key: "openrouter", label: "OpenRouter", logo: IMPERIAL_MODEL_MARKS.openrouter };
    }
    return { key: "generic", label: "自定义模型", logo: IMPERIAL_MODEL_MARKS.generic };
  }

  function applyImperialModelBrand(badge, modelId) {
    if (!badge) return;
    const brand = getModelBrand(modelId);
    badge.className = `imperial-model-badge imperial-model-${brand.key}`;
    badge.dataset.modelId = modelId || "";
    badge.title = `${brand.label} · ${modelId || "未配置模型"}`;
    badge.classList.remove("asset-missing");
    const logo = badge.querySelector("img");
    if (logo) {
      logo.src = brand.logo;
      logo.alt = brand.label;
      logo.style.display = "";
    }
  }

  function updateImperialModelBadge(characterId, modelId) {
    const badge = document.querySelector(
      `.char-list-row[data-character-id="${characterId}"] .imperial-model-badge`
    );
    applyImperialModelBrand(badge, modelId);
  }
  let userAvatar = "/static/user.svg";
  let appearanceState = null;

  const FRIENDSHIP_NORMAL = {
    state: "normal",
    reason: "",
    deleted_at: null,
    request_after: null,
    pending_request: null,
  };
  const _friendship = Object.fromEntries(
    Object.keys(histories).map(cid => [cid, { ...FRIENDSHIP_NORMAL }])
  );

  function friendshipState(cid = currentChar) {
    return _friendship[cid] || FRIENDSHIP_NORMAL;
  }

  function setFriendshipState(cid, state) {
    _friendship[cid] = { ...FRIENDSHIP_NORMAL, ...(state || {}) };
    updateFriendshipListItem(cid);
    if (cid === currentChar) updateFriendshipInputState();
    return _friendship[cid];
  }

  async function fetchFriendship(cid) {
    try {
      const response = await fetch(`/api/friendship/${cid}`);
      if (!response.ok) return friendshipState(cid);
      return setFriendshipState(cid, await response.json());
    } catch (error) {
      console.warn("friendship fetch failed for", cid, error);
      return friendshipState(cid);
    }
  }

  function updateFriendshipListItem(cid) {
    const state = friendshipState(cid);
    const row = charRows[cid];
    if (row) row.dataset.friendshipState = state.state;
    const preview = charPreviewEls[cid];
    if (preview && state.pending_request?.text) {
      preview.textContent = getPreviewText(`[好友申请] ${state.pending_request.text}`);
    }
    const dot = document.querySelector(`.unread-dot[data-cid="${cid}"]`);
    if (dot && state.pending_request) dot.classList.remove("hidden");
  }

  function updateFriendshipInputState() {
    const state = friendshipState();
    const inputbar = document.getElementById("inputbar");
    const lockbar = document.getElementById("friendshipLockBar");
    const userDeleted = state.state === "user_deleted";
    inputbar.classList.toggle("hidden", userDeleted);
    lockbar.classList.toggle("hidden", !userDeleted);
    if (userDeleted) {
      closePawMenu();
      setSingleReplyTarget(null);
    }
  }

  function resetSingleHistory(cid) {
    historyLoaded.delete(cid);
    histories[cid].length = 0;
    delete historyState[cid];
  }

  async function reloadSingleHistory(cid) {
    resetSingleHistory(cid);
    if (cid === currentChar) {
      messagesEl.innerHTML = "";
      await loadHistory(cid);
    }
  }

  async function refreshFriendships() {
    await Promise.all(Object.keys(_friendship).map(fetchFriendship));
    refreshCharPreviews();
    refreshUnread();
  }

  let friendshipActionCid = null;
  function openFriendshipActionSheet(cid) {
    friendshipActionCid = cid;
    const state = friendshipState(cid);
    const sheet = document.getElementById("friendshipActionSheet");
    const primary = document.getElementById("friendshipActionPrimary");
    primary.textContent = state.state === "normal" ? "删除好友" : "恢复好友";
    primary.classList.toggle("danger", state.state === "normal");
    sheet.classList.remove("hidden");
    sheet.setAttribute("aria-hidden", "false");
  }

  function closeFriendshipActionSheet() {
    const sheet = document.getElementById("friendshipActionSheet");
    sheet.classList.add("hidden");
    sheet.setAttribute("aria-hidden", "true");
    friendshipActionCid = null;
  }

  async function restoreFriendship(cid, greet = false) {
    const response = await fetch("/api/friendship/restore", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ character_id: cid, greet }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "恢复失败");
    setFriendshipState(cid, FRIENDSHIP_NORMAL);
    await reloadSingleHistory(cid);
    refreshCharPreviews();
    refreshUnread();
    return data;
  }

  function openFriendVerification(prefill = "") {
    const state = friendshipState();
    const reason = String(state.reason || "").trim();
    document.getElementById("friendVerifyReason").textContent = reason;
    document.getElementById("friendVerifyReasonWrap").classList.toggle("hidden", !reason);
    document.getElementById("friendVerifyText").value = prefill;
    const modal = document.getElementById("friendVerifyModal");
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    setTimeout(() => document.getElementById("friendVerifyText").focus(), 80);
  }

  function closeFriendVerification() {
    const modal = document.getElementById("friendVerifyModal");
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
  }

  function openPendingFriendRequest(cid) {
    const request = friendshipState(cid).pending_request;
    if (!request) return;
    const modal = document.getElementById("friendRequestModal");
    modal.dataset.cid = cid;
    document.getElementById("friendRequestAvatar").src = charAvatars[cid] || "";
    document.getElementById("friendRequestName").textContent = nickName(cid);
    document.getElementById("friendRequestText").textContent = request.text || "";
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
  }

  function closePendingFriendRequest() {
    const modal = document.getElementById("friendRequestModal");
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
    delete modal.dataset.cid;
  }

  function showFriendDeletedModal(reason) {
    document.getElementById("friendDeletedReason").textContent = reason || "";
    const modal = document.getElementById("friendDeletedModal");
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
  }

  function ensureNormalFriendship() {
    const state = friendshipState();
    if (state.state === "normal") return true;
    if (state.state === "char_deleted") openFriendVerification(inputEl.value.trim());
    else showToast("你已删除对方，可以先恢复好友");
    return false;
  }

  document.getElementById("friendshipActionCancel").addEventListener("click", closeFriendshipActionSheet);
  document.getElementById("friendshipActionSheet").addEventListener("click", event => {
    if (event.target === event.currentTarget) closeFriendshipActionSheet();
  });
  document.getElementById("friendshipActionPrimary").addEventListener("click", () => {
    const cid = friendshipActionCid;
    if (!cid) return;
    const state = friendshipState(cid);
    closeFriendshipActionSheet();
    if (state.state !== "normal") {
      restoreFriendship(cid).then(data => showToast(data.released > 0
        ? `已恢复好友，收到 ${data.released} 条积压消息`
        : "已恢复好友"))
        .catch(error => showToast(error.message));
      return;
    }
    showConfirmDialog("删除后祂将无法收到你的消息，确定吗？", async () => {
      try {
        const response = await fetch("/api/friendship/delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ character_id: cid }),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "删除失败");
        setFriendshipState(cid, data);
        showToast("已删除好友");
      } catch (error) {
        showToast(error.message);
      }
    });
  });

  document.getElementById("friendshipRestoreInline").addEventListener("click", () => {
    restoreFriendship(currentChar).then(data => showToast(data.released > 0
      ? `已恢复好友，收到 ${data.released} 条积压消息`
      : "已恢复好友"))
      .catch(error => showToast(error.message));
  });

  document.getElementById("friendVerifyClose").addEventListener("click", closeFriendVerification);
  document.getElementById("friendVerifyModal").addEventListener("click", event => {
    if (event.target === event.currentTarget) closeFriendVerification();
  });
  document.getElementById("friendVerifySubmit").addEventListener("click", async () => {
    const text = document.getElementById("friendVerifyText").value.trim();
    if (!text) return showToast("写一句申请验证再发送");
    const cid = currentChar;
    const button = document.getElementById("friendVerifySubmit");
    button.disabled = true;
    try {
      const response = await fetch("/api/friendship/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ character_id: cid, text }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "申请没送到");
      closeFriendVerification();
      setSingleComposerValue("");
      if (data.approved) {
        setFriendshipState(cid, FRIENDSHIP_NORMAL);
        await reloadSingleHistory(cid);
        refreshUnread();
        showToast(data.released > 0
          ? `对方通过了申请，收到 ${data.released} 条积压消息`
          : "对方通过了你的好友申请");
      } else {
        showToast("好友申请已发送，对方暂未通过");
      }
    } catch (error) {
      showToast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  document.getElementById("friendRequestLater").addEventListener("click", closePendingFriendRequest);
  document.getElementById("friendRequestApprove").addEventListener("click", async () => {
    const modal = document.getElementById("friendRequestModal");
    const cid = modal.dataset.cid;
    if (!cid) return;
    const button = document.getElementById("friendRequestApprove");
    button.disabled = true;
    try {
      const data = await restoreFriendship(cid, true);
      closePendingFriendRequest();
      showToast(data.released > 0
        ? `已通过申请，释放 ${data.released} 条积压消息`
        : "已通过好友申请");
    } catch (error) {
      showToast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  document.getElementById("friendDeletedKnow").addEventListener("click", () => {
    const modal = document.getElementById("friendDeletedModal");
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
  });

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
    syncImperialComposers();
    const backToList = document.getElementById("backToList");
    if (backToList) {
      backToList.setAttribute(
        "aria-label",
        theme.id === "imperial" ? "退朝" : "返回角色列表"
      );
    }
  }

  function replaceVisibleImageSources(oldUrl, newUrl) {
    if (!oldUrl || oldUrl === newUrl) return;
    const oldAbsolute = new URL(oldUrl, window.location.href).href;
    document.querySelectorAll("img").forEach(img => {
      if (img.src === oldAbsolute) img.src = newUrl;
    });
  }

  const WEATHER_EFFECTS = new Set(["off", "rain", "snow", "leaves"]);

  function applyWeatherEffect(effectId) {
    const layer = document.getElementById("weatherEffectLayer");
    if (!layer) return;
    const effect = WEATHER_EFFECTS.has(effectId) ? effectId : "off";
    layer.replaceChildren();
    layer.className = effect === "off" ? "" : `weather-effect weather-${effect}`;
    layer.dataset.effect = effect;
    if (effect === "off") return;

    const count = effect === "rain" ? 64 : effect === "snow" ? 34 : 24;
    const mobileDurationScale = window.matchMedia("(max-width: 700px)").matches ? 1.28 : 1;
    const fragment = document.createDocumentFragment();
    for (let index = 0; index < count; index += 1) {
      const particle = document.createElement("i");
      const baseDuration = effect === "rain"
        ? 2.8 + Math.random() * 1.4
        : effect === "snow"
          ? 12 + Math.random() * 8
          : 14 + Math.random() * 9;
      const duration = baseDuration * mobileDurationScale;
      particle.className = `weather-particle weather-variant-${index % 3}`;
      if (effect === "snow") particle.textContent = ["❄", "❅", "❆"][index % 3];
      particle.style.setProperty("--x", `${((index + Math.random()) / count * 100).toFixed(2)}%`);
      particle.style.setProperty("--delay", `${(-Math.random() * duration).toFixed(2)}s`);
      particle.style.setProperty("--duration", `${duration.toFixed(2)}s`);
      const size = effect === "rain"
        ? 18 + Math.random() * 20
        : effect === "snow"
          ? 12 + Math.random() * 13
          : 6 + Math.random() * 8;
      const opacity = effect === "rain"
        ? 0.48 + Math.random() * 0.34
        : 0.42 + Math.random() * 0.38;
      particle.style.setProperty("--size", `${size.toFixed(1)}px`);
      particle.style.setProperty("--opacity", `${opacity.toFixed(2)}`);
      particle.style.setProperty("--drift-mid", `${(-28 + Math.random() * 56).toFixed(1)}px`);
      particle.style.setProperty("--drift", `${(-42 + Math.random() * 84).toFixed(1)}px`);
      particle.style.setProperty("--drift-end", `${(-58 + Math.random() * 116).toFixed(1)}px`);
      particle.style.setProperty("--spin", `${(300 + Math.random() * 680).toFixed(0)}deg`);
      fragment.appendChild(particle);
    }
    layer.appendChild(fragment);
  }

  function applyAppearance(data) {
    if (!data) return;
    applyTheme(data);
    applyWeatherEffect(data.weather_effect || "off");
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

  function renderDesireScene(scene, enabled = true) {
    const wrap = document.getElementById("desireSceneWrap");
    const location = document.getElementById("desireSceneLocation");
    const detail = document.getElementById("desireSceneDetail");
    const clear = document.getElementById("desireSceneClear");
    wrap.classList.toggle("hidden", !enabled);
    if (!enabled) return;
    const hasScene = Boolean(scene?.location);
    location.textContent = hasScene ? scene.location : "还没落脚";
    const details = [scene?.activity, scene?.ambience].filter(Boolean);
    detail.textContent = hasScene
      ? (details.join(" · ") || "祂只是安静待在这里。")
      : "祂想好待在哪里时，这里会自己变化。";
    clear.classList.toggle("hidden", !hasScene);
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
    sheet.dataset.characterId = characterId;
    renderDesireScene(null, false);
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
      renderDesireScene(data.scene, data.scene_enabled !== false);
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
  document.getElementById("desireSceneClear").addEventListener("click", async event => {
    event.stopPropagation();
    const sheet = document.getElementById("desireSheet");
    const characterId = sheet.dataset.characterId;
    if (!characterId) return;
    try {
      const response = await fetch(`/api/scene/${characterId}/clear`, { method: "POST" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "清空失败");
      renderDesireScene(data.scene);
      showToast("当前位置已清空");
    } catch (error) {
      showToast(error.message || "暂时没能清空");
    }
  });
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
    const imageWidth = Number(data.width);
    const imageHeight = Number(data.height);
    if (imageWidth > 0 && imageHeight > 0) {
      bubble.style.aspectRatio = `${imageWidth} / ${imageHeight}`;
      bubble.classList.add("has-image-aspect");
    }
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
    const noteEl = document.createElement("div");
    noteEl.className = "transfer-note-text";
    noteEl.textContent = data.note || "\u00a0";
    noteEl.title = data.note || "";
    bubble.appendChild(noteEl);
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
          <span>${providerDisplayName(metrics.provider)}</span>
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
        send_voice: '🎙️ AI 语音',
        press_hug: '🤍 和好按钮',
        close_window: '🚪 封窗',
        delete_friend: '👤 删除好友',
        approve_friend_request: '🤝 通过好友申请',
        set_scene: '📍 切换场景',
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

  function appendSingleQuote(bubble, quote) {
    if (!quote) return;
    const quoted = document.createElement("div");
    quoted.className = "group-message-quote";
    const name = document.createElement("strong");
    name.textContent = quote.character_name || "引用";
    const content = document.createElement("span");
    content.textContent = quote.content || "";
    quoted.appendChild(name);
    quoted.appendChild(content);
    bubble.appendChild(quoted);
  }

  function setSingleBubbleContent(bubble, text, quote = null) {
    bubble.dataset.bubbleText = text;
    appendSingleQuote(bubble, quote);
    const content = document.createElement("span");
    content.textContent = text;
    bubble.appendChild(content);
  }

  function parseVoiceContent(content, messageId = null) {
    if (!content || !content.startsWith("__VOICE__")) return null;
    try {
      const data = JSON.parse(content.slice(9));
      return {
        ...data,
        id: messageId,
        message_id: messageId,
        url: data.url || (messageId ? `/api/voice/audio/${messageId}` : ""),
        ai_generated: true,
      };
    } catch (_) {
      return null;
    }
  }

  function buildVoiceBubble(data, messageId = null) {
    const bubble = document.createElement("div");
    bubble.className = "bubble ai voice-bubble";
    bubble.dataset.bubbleText = data.text || "语音消息";
    if (messageId) bubble.dataset.messageId = messageId;

    const main = document.createElement("div");
    main.className = "voice-bubble-main";
    const play = document.createElement("button");
    play.type = "button";
    play.className = "voice-play-btn";
    play.setAttribute("aria-label", "播放AI语音");
    play.innerHTML = '<span class="material-symbols-outlined">play_arrow</span>';
    const wave = document.createElement("span");
    wave.className = "voice-wave";
    [9, 17, 12, 23, 15, 20, 10, 18, 13].forEach((height, index) => {
      const bar = document.createElement("i");
      bar.style.setProperty("--wave-h", `${height}px`);
      bar.style.setProperty("--wave-i", index);
      wave.appendChild(bar);
    });
    const label = document.createElement("span");
    label.className = "voice-ai-label";
    label.textContent = "AI 语音";
    main.append(play, wave, label);
    bubble.appendChild(main);

    const details = document.createElement("details");
    details.className = "voice-transcript-details";
    const summary = document.createElement("summary");
    summary.textContent = "查看文字稿";
    const transcript = document.createElement("p");
    transcript.className = "voice-transcript";
    transcript.textContent = data.text || "（没有文字稿）";
    details.append(summary, transcript);
    bubble.appendChild(details);

    const audio = new Audio(data.url || `/api/voice/audio/${messageId}`);
    audio.preload = "none";
    const reset = () => {
      bubble.classList.remove("playing");
      play.querySelector("span").textContent = "play_arrow";
    };
    audio.addEventListener("ended", reset);
    audio.addEventListener("pause", reset);
    audio.addEventListener("error", () => {
      reset();
      showToast("语音暂时播放不了，再试一下");
    });
    play.addEventListener("click", event => {
      event.stopPropagation();
      if (audio.paused) {
        document.querySelectorAll(".voice-bubble.playing .voice-play-btn").forEach(btn => {
          if (btn !== play) btn.click();
        });
        audio.play().then(() => {
          bubble.classList.add("playing");
          play.querySelector("span").textContent = "pause";
        }).catch(() => showToast("浏览器没有允许播放这条语音"));
      } else {
        audio.pause();
      }
    });
    return bubble;
  }

  function buildSingleVoiceBlock(data, time, messageId) {
    const block = document.createElement("div");
    block.className = "single-msg-block from-ai";
    const avatarImg = document.createElement("img");
    avatarImg.src = charAvatars[currentChar] || "";
    avatarImg.style.cssText = "width:32px;height:32px;border-radius:50%;object-fit:cover;margin-bottom:4px;";
    avatarImg.onerror = function() { this.style.display = "none"; };
    decorateDesireAvatar(avatarImg, currentChar);
    block.appendChild(avatarImg);
    block.appendChild(buildVoiceBubble(data, messageId));
    const timeStr = formatMsgTime(time);
    if (timeStr) {
      const timeDiv = document.createElement("div");
      timeDiv.className = "msg-time";
      timeDiv.textContent = timeStr;
      block.appendChild(timeDiv);
    }
    return block;
  }

  function buildSingleBlock(text, who, time, messageId, toolsCalled, metrics, quote = null) {
    const voice = parseVoiceContent(text, messageId);
    if (voice) return buildSingleVoiceBlock(voice, time, messageId);
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
      const avatarWrap = document.createElement("div");
      avatarWrap.className = "msg-avatar-wrap";
      const avatarImg = document.createElement("img");
      avatarImg.src = charAvatars[currentChar] || "";
      avatarImg.style.cssText = "width:32px;height:32px;border-radius:50%;object-fit:cover;margin-bottom:4px;";
      avatarImg.onerror = function() { this.style.display = "none"; };
      decorateDesireAvatar(avatarImg, currentChar);
      avatarWrap.appendChild(avatarImg);
      block.appendChild(avatarWrap);
      block.appendChild(buildThinkBlock(toolsCalled, metrics));
      splitBubbleContent(text).forEach((part, index) => {
        const div = document.createElement("div");
        div.className = "bubble ai";
        setSingleBubbleContent(div, part, index === 0 ? quote : null);
        if (messageId) div.dataset.messageId = messageId;
        block.appendChild(div);
      });
    } else {
      const div = document.createElement("div");
      div.className = "bubble user";
      setSingleBubbleContent(div, text, quote);
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

  function addBubble(content, who, messageId, quote = null) {
    const time = new Date();
    histories[currentChar].push({ id: messageId, text: content, who, time, quote });
    messagesEl.appendChild(buildSingleBlock(content, who, time, messageId, null, null, quote));
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function addVoiceBubble(voice) {
    if (!voice) return;
    const time = new Date();
    const content = "__VOICE__" + JSON.stringify({
      text: voice.text,
      mime: voice.mime,
      from: "char",
      url: voice.url,
    });
    histories[currentChar].push({ id: voice.id, text: content, who: "ai", time });
    messagesEl.appendChild(buildSingleVoiceBlock(voice, time, voice.id));
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function scrollSingleToLatest({ settle = false, charId = currentChar } = {}) {
    if (singleLatestMediaCleanup) {
      singleLatestMediaCleanup();
      singleLatestMediaCleanup = null;
    }
    const token = ++singleLatestPinToken;
    singleLatestPinActive = true;
    let finishTimer = null;
    let maxTimer = null;
    const mediaListeners = [];
    const pin = () => {
      if (token !== singleLatestPinToken || !singleLatestPinActive || charId !== currentChar) return;
      const chatView = document.getElementById("singleChatView");
      if (chatView.style.display === "none") return;
      messagesEl.scrollTop = messagesEl.scrollHeight;
    };
    const teardown = () => {
      clearTimeout(finishTimer);
      clearTimeout(maxTimer);
      mediaListeners.forEach(({ image, done }) => {
        image.removeEventListener("load", done);
        image.removeEventListener("error", done);
      });
    };
    const finish = () => {
      pin();
      if (token === singleLatestPinToken) singleLatestPinActive = false;
      teardown();
      if (singleLatestMediaCleanup === teardown) singleLatestMediaCleanup = null;
    };
    requestAnimationFrame(() => {
      pin();
      requestAnimationFrame(pin);
    });
    if (document.fonts?.ready) document.fonts.ready.then(pin).catch(() => {});
    if (settle) {
      setTimeout(pin, 100);
      const pendingMedia = new Set(
        [...messagesEl.querySelectorAll("img")].filter(image => !image.complete)
      );
      pendingMedia.forEach(image => {
        const done = () => {
          pendingMedia.delete(image);
          pin();
          requestAnimationFrame(pin);
          if (!pendingMedia.size) finishTimer = setTimeout(finish, 180);
        };
        mediaListeners.push({ image, done });
        image.addEventListener("load", done, { once: true });
        image.addEventListener("error", done, { once: true });
        if (image.complete) done();
      });
      if (pendingMedia.size) {
        maxTimer = setTimeout(finish, 4500);
      } else {
        finishTimer = setTimeout(finish, 320);
      }
    } else {
      finishTimer = setTimeout(finish, 80);
    }
    singleLatestMediaCleanup = teardown;
  }

  async function revealSingleHistoryAtLatest(charId, renderToken) {
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    if (renderToken !== singleHistoryRenderToken || charId !== currentChar) return;
    messagesEl.scrollTop = messagesEl.scrollHeight;
    document.getElementById("singleChatView").classList.remove("history-positioning");
    scrollSingleToLatest({ settle: true, charId });
  }

  function renderFromCache(charId) {
    messagesEl.innerHTML = "";
    histories[charId].forEach(({ id, text, who, time, toolsCalled, metrics, quote }) => {
      messagesEl.appendChild(buildSingleBlock(text, who, time, id, toolsCalled, metrics, quote));
    });
    messagesEl.scrollTop = messagesEl.scrollHeight;
    scrollSingleToLatest({ settle: true, charId });
    refreshSleepStates();
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
        quote: m.quote,
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
        quote: m.quote,
      }));
      histories[charId].splice(0, 0, ...newEntries);
      if (charId !== currentChar) return;

      const prevHeight = messagesEl.scrollHeight;
      const prevTop    = messagesEl.scrollTop;
      const anchor     = messagesEl.firstChild;
      newEntries.forEach(({ id, text, who, time, toolsCalled, metrics, quote }) => messagesEl.insertBefore(
        buildSingleBlock(text, who, time, id, toolsCalled, metrics, quote), anchor
      ));
      messagesEl.scrollTop = prevTop + (messagesEl.scrollHeight - prevHeight);
    } catch (e) {
      console.warn("loadOlderMessages failed for", charId, e);
    } finally {
      st.loadingMore = false;
    }
  }
  messagesEl.addEventListener("scroll", () => {
    if (singleLatestPinActive) return;
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
    if (text && text.startsWith("__VOICE__")) {
      const voice = parseVoiceContent(text);
      return voice ? `[语音] ${voice.text}` : "[语音]";
    }
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
      const request = friendshipState(cid).pending_request;
      if (request?.text) {
        el.textContent = getPreviewText(`[好友申请] ${request.text}`);
        continue;
      }
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
      setSingleReplyTarget(null);
      listView.style.display = "flex";
      chatView.style.display = "none";
      refreshCharPreviews();
      refreshFriendships();
      refreshUnread();
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
      Object.entries(data).forEach(([cid, character]) => {
        if (!character?.name) return;
        GROUP_CHAR_NAMES[cid] = character.name;
        if (cid in CHAR_DISPLAY_NAMES) CHAR_DISPLAY_NAMES[cid] = character.name;
        CHAR_LIST.filter(item => item.id === cid).forEach(item => { item.name = character.name; });
        READING_CHAR_LIST.filter(item => item.id === cid).forEach(item => { item.name = character.name; });
      });
      const container = document.getElementById("charListContainer");
      container.innerHTML = "";
      const order = ["char1", "char2", "char3", "char4", "char5", "char6"];
      order.forEach(cid => {
        const c = data[cid];
        if (!c) return;
        const row = document.createElement("div");
        row.className = "char-list-row";
        row.dataset.characterId = cid;
        row.tabIndex = 0;
        row.setAttribute("role", "button");
        row.setAttribute("aria-label", `召见 ${nickName(cid)}`);

        const avatarWrap = document.createElement("div");
        avatarWrap.className = "char-list-avatar-wrap";
        const img = document.createElement("img");
        img.src = c.avatar;
        img.draggable = false;
        img.onerror = function() { this.style.display = "none"; };
        img.style.cssText = "width:48px;height:48px;border-radius:50%;object-fit:cover;display:block;";
        const dot = document.createElement("div");
        dot.className = "unread-dot hidden";
        dot.dataset.cid = cid;
        avatarWrap.appendChild(img);
        avatarWrap.appendChild(dot);

        const imperialArt = document.createElement("div");
        imperialArt.className = "imperial-character-art";
        imperialArt.setAttribute("aria-hidden", "true");
        const imperialPortrait = document.createElement("img");
        imperialPortrait.className = "imperial-character-figure";
        imperialPortrait.src = IMPERIAL_CHARACTER_ART[cid] || "";
        imperialPortrait.alt = "";
        imperialPortrait.draggable = false;
        imperialPortrait.onerror = function() {
          this.closest(".imperial-character-art")?.classList.add("asset-missing");
          console.warn("imperial portrait missing", this.src);
        };
        const imperialBadge = document.createElement("span");
        imperialBadge.className = "imperial-model-badge";
        const imperialLogo = document.createElement("img");
        imperialLogo.draggable = false;
        imperialLogo.onerror = function() {
          this.closest(".imperial-model-badge")?.classList.add("asset-missing");
          console.warn("imperial model mark missing", this.src);
        };
        imperialBadge.appendChild(imperialLogo);
        applyImperialModelBrand(imperialBadge, c.model);
        const imperialDot = document.createElement("div");
        imperialDot.className = "unread-dot imperial-unread-dot hidden";
        imperialDot.dataset.cid = cid;
        imperialArt.appendChild(imperialPortrait);
        imperialArt.appendChild(imperialBadge);
        imperialArt.appendChild(imperialDot);

        const info = document.createElement("div");
        info.className = "char-list-info";
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
        row.appendChild(imperialArt);
        row.appendChild(info);

        charPreviewEls[cid] = previewEl;
        charRows[cid] = row;
        charAvatars[cid] = c.avatar;

        let relationPressTimer = null;
        let relationPressStart = null;
        [avatarWrap, imperialArt].forEach(pressTarget => {
          pressTarget.addEventListener("pointerdown", event => {
            if (event.pointerType === "mouse" && event.button !== 0) return;
            relationPressStart = { x: event.clientX, y: event.clientY };
            relationPressTimer = setTimeout(() => {
              clearNativeSelection(true);
              row._friendshipSuppressClick = true;
              setTimeout(() => { row._friendshipSuppressClick = false; }, 900);
              openFriendshipActionSheet(cid);
            }, 600);
          });
          bindNativeLongPressGuard(pressTarget);
          pressTarget.addEventListener("pointermove", event => {
            if (!relationPressStart) return;
            if (
              Math.abs(event.clientX - relationPressStart.x) > 8
              || Math.abs(event.clientY - relationPressStart.y) > 8
            ) clearTimeout(relationPressTimer);
          });
          ["pointerup", "pointercancel", "pointerleave"].forEach(type => {
            pressTarget.addEventListener(type, () => {
              clearTimeout(relationPressTimer);
              relationPressStart = null;
            });
          });
        });
        const openCharacter = () => {
          if (row._friendshipSuppressClick) {
            row._friendshipSuppressClick = false;
            return;
          }
          if (friendshipState(cid).pending_request) {
            openPendingFriendRequest(cid);
            return;
          }
          dot.classList.add("hidden");
          imperialDot.classList.add("hidden");
          fetch(`/api/unread/${cid}/clear`, { method: "POST" }).catch(() => {});
          document.getElementById("char-name").textContent = nickName(cid);
          charSubEl.textContent = CHAR_META[cid]?.label ?? cid;
          showSingleSub("chat");
          switchChar(cid);
        };
        row.addEventListener("click", openCharacter);
        row.addEventListener("keydown", event => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            openCharacter();
          }
        });
        container.appendChild(row);
        updateFriendshipListItem(cid);
      });
      refreshCharPreviews();
      refreshUnread();
      refreshFriendships();
      refreshSleepStates();
      scheduleSecondaryViewsWarmup();
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
        const pending = !!friendshipState(dot.dataset.cid).pending_request;
        dot.classList.toggle("hidden", !pending && !unreadList.includes(dot.dataset.cid));
      });
    } catch(e) {}
  }

  async function refreshSleepStates() {
    try {
      const res = await fetch("/api/sleep_states");
      if (!res.ok) return;
      const states = await res.json();
      // 清除上一个 Z（可能在旧的最后一条消息上）
      document.querySelectorAll(".msg-avatar-zs").forEach(el => el.remove());
      // 如果当前角色在睡，把 Z 挂到最后一条 AI 消息的头像旁
      if (states[currentChar] === "asleep") {
        const allAi = messagesEl.querySelectorAll(".from-ai");
        const lastAi = allAi[allAi.length - 1];
        if (lastAi) {
          const wrap = lastAi.querySelector(".msg-avatar-wrap");
          if (wrap) {
            const zs = document.createElement("div");
            zs.className = "sleep-zs msg-avatar-zs";
            ["z","z","z"].forEach(t => {
              const s = document.createElement("span"); s.className = "z"; s.textContent = t;
              zs.appendChild(s);
            });
            wrap.appendChild(zs);
          }
        }
      }
    } catch(e) {}
  }

  // 每 60 秒轮询一次睡眠状态
  refreshSleepStates();
  setInterval(refreshSleepStates, 60000);
  setInterval(refreshFriendships, 30000);
  setInterval(refreshUnread, 15000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    refreshFriendships();
    refreshUnread();
  });
  window.addEventListener("pageshow", () => {
    refreshFriendships();
    refreshUnread();
  });

  async function switchChar(charId) {
    const renderToken = ++singleHistoryRenderToken;
    const chatView = document.getElementById("singleChatView");
    chatView.classList.add("history-positioning");
    setSingleReplyTarget(null);
    currentChar = charId;
    charSubEl.textContent = CHAR_META[charId]?.label ?? charId;
    messagesEl.innerHTML = "";
    try {
      await Promise.all([fetchFriendship(charId), loadHistory(charId)]);
      if (renderToken !== singleHistoryRenderToken || charId !== currentChar) return;
      updateFriendshipInputState();
      refreshSleepStates();
      await revealSingleHistoryAtLatest(charId, renderToken);
    } finally {
      if (renderToken === singleHistoryRenderToken && charId === currentChar) {
        chatView.classList.remove("history-positioning");
      }
    }
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
    if (data.voice) {
      await new Promise(r => setTimeout(r, 350));
      addVoiceBubble(data.voice);
    }
    if (data.window_closed && !data.friend_deleted) {
      await new Promise(r => setTimeout(r, 300));
      showCloseWindowModal(data.window_closed.reason || "");
    }
    if (data.friend_deleted) {
      setFriendshipState(currentChar, {
        state: "char_deleted",
        reason: data.friend_deleted.reason || "",
      });
      await new Promise(r => setTimeout(r, 300));
      showFriendDeletedModal(data.friend_deleted.reason || "");
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

  function setSingleReplyTarget(quote) {
    singleReplyTarget = quote || null;
    const bar = document.getElementById("singleQuoteBar");
    bar.classList.toggle("hidden", !singleReplyTarget);
    document.getElementById("singleQuoteName").textContent = singleReplyTarget
      ? `引用 ${singleReplyTarget.character_name}` : "";
    document.getElementById("singleQuoteText").textContent = singleReplyTarget?.content || "";
  }

  document.getElementById("singleQuoteClear").addEventListener("click", () => {
    setSingleReplyTarget(null);
    focusSingleComposer();
  });

  async function send() {
    const text = inputEl.value.trim();
    if (!text) return;
    const friendship = friendshipState();
    if (friendship.state === "char_deleted") {
      openFriendVerification(text);
      return;
    }
    if (friendship.state === "user_deleted") {
      updateFriendshipInputState();
      showToast("你已删除对方，可以先恢复好友");
      return;
    }
    setSingleComposerValue("");
    closePawMenu();
    sendBtn.disabled = true;
    setSingleComposerDeliveryState(true);
    const pendingQuote = singleReplyTarget;
    addBubble(text, "user", null, pendingQuote);
    const optimisticUserBlock = messagesEl.lastElementChild;
    const pending = addPendingAiBlock();

    try {
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: text,
          character_id: currentChar,
          session_id: "default",
          reply_to_id: pendingQuote?.message_id || null,
          reply_to_text: pendingQuote?.content || null,
        }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "发送失败");
      if (data.friendship_blocked) {
        optimisticUserBlock?.remove();
        pending.aiBlock?.remove();
        histories[currentChar].pop();
        setSingleComposerValue(text);
        const status = await fetchFriendship(currentChar);
        if (data.friendship_state === "char_deleted") {
          setFriendshipState(currentChar, { ...status, state: "char_deleted" });
          openFriendVerification(text);
        } else {
          setFriendshipState(currentChar, { ...status, state: "user_deleted" });
        }
        return;
      }
      setSingleReplyTarget(null);

      if (data.sleep) {
        // 角色睡着了，消息已攒，移除 pending AI 气泡，给用户消息加灰色提示
        if (pending.block && pending.block.parentNode) pending.block.parentNode.removeChild(pending.block);
        const chat = document.getElementById("chat");
        const lastUserBubble = chat.querySelector(".block:last-child");
        if (lastUserBubble) {
          const note = document.createElement("div");
          note.className = "sleep-queued-note";
          note.textContent = "已送达 · 对方睡着了 💤";
          note.style.cssText = "font-size:11px;color:var(--muted);text-align:right;margin-top:2px;margin-right:4px;";
          lastUserBubble.appendChild(note);
        }
      } else {
        await renderAiResponse(data, pending);
      }
    } catch (e) {
      renderAiError(pending, e);
    } finally {
      sendBtn.disabled = false;
      setSingleComposerDeliveryState(false);
      focusSingleComposer();
    }
  }

  document.getElementById("backToList").addEventListener("click", () => showSingleSub("list"));

  // ── 单聊左缘右划返回：列表在下，会话层跟手退场 ──
  const singleView = document.getElementById("singleView");
  const singleListView = document.getElementById("singleListView");
  const singleChatView = document.getElementById("singleChatView");
  const swipeBackHint = document.getElementById("swipeBackHint");
  const SWIPE_BACK_EDGE_WIDTH = 28;
  const SWIPE_BACK_LOCK_DISTANCE = 5;
  const SWIPE_BACK_READY_DISTANCE = 72;
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
    if (touch.clientX > SWIPE_BACK_EDGE_WIDTH) return;
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
    const rawDx = touch.clientX - swipeBackState.startX;
    const dx = Math.max(0, rawDx);
    const dy = touch.clientY - swipeBackState.startY;
    const absDy = Math.abs(dy);

    if (!swipeBackState.horizontal) {
      if (rawDx < -SWIPE_BACK_LOCK_DISTANCE) {
        resetSwipeBack();
        return;
      }
      if (absDy > 12 && absDy > dx * 1.35) {
        resetSwipeBack();
        return;
      }
      if (dx < SWIPE_BACK_LOCK_DISTANCE || dx < absDy * 0.72) return;
      swipeBackState.horizontal = true;
      singleListView.style.display = "flex";
      singleView.classList.add("swipe-peeking");
    }

    event.preventDefault();
    swipeBackState.dx = dx;
    swipeBackState.ready = dx >= SWIPE_BACK_READY_DISTANCE;
    const progress = Math.min(dx / 96, 1);
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
      && swipeBackState.dx >= 32
      && swipeBackState.dx / elapsed >= 0.3;
    const shouldReturn = swipeBackState.ready || fastFlick;
    settleSwipeBack(shouldReturn);
  });
  singleChatView.addEventListener("touchcancel", resetSwipeBack);

  // ── 发送键长按菜单 ──
  const pawMenu = document.getElementById("pawMenu");
  let _longPressFired = false;
  let _longPressTimer = null;

  function openPawMenu() {
    clearNativeSelection(true);
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
  bindNativeLongPressGuard(sendBtn);

  // 长按触发后拦掉紧随的 click，不执行 send()
  sendBtn.addEventListener("click", e => {
    if (_longPressFired) {
      _longPressFired = false;
      return;
    }
    send();
  });

  // ── 单聊气泡长按操作 ──
  let bubblePressTimer = null;

  messagesEl.addEventListener("pointerdown", e => {
    const bubble = e.target.closest(".bubble");
    if (!bubble) return;
    bubblePressTimer = setTimeout(() => handleBubbleLongPress(bubble), 500);
  });
  messagesEl.addEventListener("pointerup",   () => clearTimeout(bubblePressTimer));
  messagesEl.addEventListener("pointermove", () => clearTimeout(bubblePressTimer));
  bindNativeLongPressGuard(messagesEl, ".bubble");

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
    clearNativeSelection(true);
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
    const quoteButton = document.getElementById("bubbleQuoteBtn");
    const quoteDivider = document.getElementById("bubbleQuoteDivider");
    const messageId = Number(bubble.dataset.messageId);
    quoteButton.classList.toggle("hidden", !messageId);
    quoteDivider.classList.toggle("hidden", !messageId);
    document.getElementById("bubbleDeleteBtn").classList.remove("hidden");
    document.getElementById("bubbleDeleteDivider").classList.remove("hidden");
    menu.classList.remove("hidden");

    const closeMenu = () => menu.classList.add("hidden");
    menu.addEventListener("click", e => { if (e.target === menu) closeMenu(); }, { once: true });

    // 复制
    document.getElementById("bubbleCopyBtn").onclick = () => {
      closeMenu();
      const text = bubble.dataset.bubbleText || bubble.textContent || "";
      navigator.clipboard.writeText(text).then(() => showToast("已复制"));
    };

    quoteButton.onclick = () => {
      closeMenu();
      if (!messageId) return;
      setSingleReplyTarget({
        message_id: messageId,
        character_id: bubble.classList.contains("user") ? "user" : currentChar,
        character_name: bubble.classList.contains("user") ? GROUP_CHAR_NAMES.user : nickName(currentChar),
        content: bubble.dataset.bubbleText || bubble.textContent || "",
      });
      focusSingleComposer();
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
    setSingleComposerDisabled(true);
    sendBtn.disabled  = true;
    cwModal.classList.remove("hidden");
  }

  function hideCloseWindowModal() {
    cwModal.classList.add("hidden");
    setSingleComposerDisabled(false);
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
    if (!ensureNormalFriendship()) return;
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
    if (!ensureNormalFriendship()) {
      closeMakeupSheet();
      return;
    }
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
    if (!ensureNormalFriendship()) return;
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
    if (!ensureNormalFriendship()) {
      closeTransferPanel();
      return;
    }
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
    if (!ensureNormalFriendship()) return;
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
    if (!ensureNormalFriendship()) return;
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
    if (!ensureNormalFriendship()) return;
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

  async function compressChatImageFile(file) {
    if (file.type === "image/gif") return file;
    const browserReadyTypes = new Set(["image/jpeg", "image/png", "image/webp"]);
    if (file.size <= 1_800_000 && browserReadyTypes.has(file.type)) return file;

    const objectUrl = URL.createObjectURL(file);
    try {
      const image = new Image();
      image.src = objectUrl;
      if (typeof image.decode === "function") {
        await image.decode();
      } else {
        await new Promise((resolve, reject) => {
          image.onload = resolve;
          image.onerror = reject;
        });
      }
      const sourceWidth = image.naturalWidth || image.width;
      const sourceHeight = image.naturalHeight || image.height;
      if (!sourceWidth || !sourceHeight) return file;
      const scale = Math.min(1, 2048 / Math.max(sourceWidth, sourceHeight));
      const width = Math.max(1, Math.round(sourceWidth * scale));
      const height = Math.max(1, Math.round(sourceHeight * scale));
      const canvas = document.createElement("canvas");
      canvas.width = width;
      canvas.height = height;
      const context = canvas.getContext("2d", { alpha: false });
      if (!context) return file;
      context.fillStyle = "#fff";
      context.fillRect(0, 0, width, height);
      context.drawImage(image, 0, 0, width, height);
      const blob = await new Promise(resolve => canvas.toBlob(resolve, "image/jpeg", 0.82));
      if (!blob) return file;
      const baseName = (file.name || "image").replace(/\.[^.]+$/, "") || "image";
      return new File([blob], `${baseName}.jpg`, {
        type: "image/jpeg",
        lastModified: file.lastModified || Date.now(),
      });
    } catch (error) {
      console.warn("client image compression skipped", error);
      return file;
    } finally {
      URL.revokeObjectURL(objectUrl);
    }
  }

  async function sendImageFile(file) {
    if (!file) return;
    if (!ensureNormalFriendship()) return;
    if (!file.type.startsWith("image/")) {
      showToast("🐱 选一张图片嘛～");
      return;
    }
    if (file.size > 25 * 1024 * 1024) {
      showToast("🐱 原图有点大，换张 25MB 内的～");
      return;
    }
    sendBtn.disabled = true;
    setSingleComposerDeliveryState(true);
    const localUrl = URL.createObjectURL(file);
    const localImage = { url: localUrl, name: file.name, mime: file.type, from: "user" };
    addImageBubble(localImage, "user");
    const imageBlock = messagesEl.lastElementChild;
    const pending = addPendingAiBlock();
    try {
      const uploadFile = await compressChatImageFile(file);
      const form = new FormData();
      form.append("character_id", currentChar);
      form.append("session_id", "default");
      form.append("image", uploadFile, uploadFile.name);
      const res = await fetch("/api/image", { method: "POST", body: form });
      const data = await res.json();
      if (!res.ok || !data.image) throw new Error(data.error || "图片发送失败");

      const imageEl = imageBlock?.querySelector(".image-bubble img");
      if (imageEl) imageEl.src = data.image.url;
      const imageBubble = imageBlock?.querySelector(".image-bubble");
      if (imageBubble) {
        if (data.user_msg_id) imageBubble.dataset.messageId = data.user_msg_id;
        if (Number(data.image.width) > 0 && Number(data.image.height) > 0) {
          imageBubble.style.aspectRatio = `${data.image.width} / ${data.image.height}`;
          imageBubble.classList.add("has-image-aspect");
        }
      }
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
      setSingleComposerDeliveryState(false);
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

  function keepSingleComposerVisible() {
    setTimeout(() => {
      window.scrollTo(0, 0);
      const msgs = document.getElementById('messages');
      if (msgs) msgs.scrollTop = msgs.scrollHeight;
    }, 350);
  }

  inputEl.addEventListener("keydown", e => { if (e.key === "Enter") send(); });
  inputEl.addEventListener("focus", keepSingleComposerVisible);
  imperialInputEl?.addEventListener("input", () => {
    inputEl.value = imperialInputEl.value;
    fitImperialComposer(imperialInputEl);
  });
  imperialInputEl?.addEventListener("keydown", e => {
    if (e.key !== "Enter" || e.shiftKey || e.isComposing) return;
    e.preventDefault();
    inputEl.value = imperialInputEl.value;
    send();
  });
  imperialInputEl?.addEventListener("focus", keepSingleComposerVisible);

  async function loadVoiceFeatureState() {
    try {
      const response = await fetch("/api/voice/config", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      voiceConfigState = await response.json();
    } catch (_) {
      voiceConfigState = null;
    }
    const canReceive = !!(
      voiceConfigState?.enabled && voiceConfigState?.stt?.enabled
    );
    voiceRecordBtn.classList.toggle("hidden", !canReceive);
    return voiceConfigState;
  }

  let activeVoiceRecorder = null;
  let activeVoiceStream = null;
  let voiceRecordTimer = null;
  let voiceChunks = [];

  function stopVoiceTracks() {
    if (voiceRecordTimer) clearTimeout(voiceRecordTimer);
    voiceRecordTimer = null;
    activeVoiceStream?.getTracks?.().forEach(track => track.stop());
    activeVoiceStream = null;
  }

  function setVoiceRecording(active) {
    voiceRecordBtn.classList.toggle("recording", active);
    voiceRecordBtn.querySelector("span").textContent = active ? "stop" : "mic";
    voiceRecordBtn.setAttribute("aria-label", active ? "停止并发送录音" : "按下录音");
    voiceRecordBtn.title = active ? "停止并发送" : "收语音";
  }

  async function sendVoiceRecording(file) {
    if (!file || !file.size) return;
    if (!ensureNormalFriendship()) return;
    const maxMb = Number(voiceConfigState?.stt?.max_upload_mb || 20);
    if (file.size > maxMb * 1024 * 1024) {
      showToast(`录音不能超过 ${maxMb}MB`);
      return;
    }
    const form = new FormData();
    form.append("audio", file, file.name || "recording.webm");
    voiceRecordBtn.disabled = true;
    setSingleComposerPlaceholder("小猫正在听写录音…");
    try {
      const response = await fetch("/api/voice/transcribe", {
        method: "POST",
        body: form,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "没有听清这段录音");
      setSingleComposerValue(data.text || "");
      if (!inputEl.value.trim()) throw new Error("没有识别出文字");
      showToast("已经听写成文字，帮你发出去啦");
      await send();
    } catch (error) {
      showToast(error.message || "录音没有发送成功");
    } finally {
      voiceRecordBtn.disabled = false;
      setSingleComposerPlaceholder("小猫酝酿坏主意中…");
      voiceFileInput.value = "";
    }
  }

  function recordingExtension(mimeType) {
    const mime = String(mimeType || "").toLowerCase();
    if (mime.includes("mp4") || mime.includes("m4a")) return "m4a";
    if (mime.includes("ogg")) return "ogg";
    if (mime.includes("wav")) return "wav";
    return "webm";
  }

  async function startVoiceRecording() {
    if (!ensureNormalFriendship()) return;
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      voiceFileInput.click();
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const candidates = [
        "audio/mp4;codecs=mp4a.40.2",
        "audio/mp4",
        "audio/webm;codecs=opus",
        "audio/webm",
      ];
      const mimeType = candidates.find(type => MediaRecorder.isTypeSupported?.(type)) || "";
      const recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
      activeVoiceStream = stream;
      activeVoiceRecorder = recorder;
      voiceChunks = [];
      recorder.addEventListener("dataavailable", event => {
        if (event.data?.size) voiceChunks.push(event.data);
      });
      recorder.addEventListener("stop", () => {
        const finalType = recorder.mimeType || mimeType || "audio/webm";
        const blob = new Blob(voiceChunks, { type: finalType });
        const file = new File(
          [blob],
          `iphone-recording.${recordingExtension(finalType)}`,
          { type: finalType },
        );
        activeVoiceRecorder = null;
        voiceChunks = [];
        setVoiceRecording(false);
        stopVoiceTracks();
        sendVoiceRecording(file);
      });
      recorder.addEventListener("error", () => {
        activeVoiceRecorder = null;
        setVoiceRecording(false);
        stopVoiceTracks();
        showToast("录音被浏览器中断了");
      });
      recorder.start(500);
      setVoiceRecording(true);
      voiceRecordTimer = setTimeout(() => {
        if (activeVoiceRecorder?.state === "recording") activeVoiceRecorder.stop();
      }, 60_000);
      showToast("正在录音，再点一下就发送");
    } catch (_) {
      stopVoiceTracks();
      setVoiceRecording(false);
      showToast("没有拿到麦克风权限，可以改选录音文件");
      voiceFileInput.click();
    }
  }

  voiceRecordBtn.addEventListener("click", () => {
    if (activeVoiceRecorder?.state === "recording") activeVoiceRecorder.stop();
    else startVoiceRecording();
  });
  voiceFileInput.addEventListener("change", event => {
    sendVoiceRecording(event.target.files?.[0]);
  });

  // ════════════════════════════════════════════
  // 群聊
  // ════════════════════════════════════════════
  const groupMessagesEl = document.getElementById("groupMessages");
  const groupSendBtn    = document.getElementById("groupSend");
  const groupContinuePickerBtn = document.getElementById("charPickerContinue");
  bindNativeLongPressGuard(groupMessagesEl, ".bubble");

  function keepGroupComposerVisible() {
    setTimeout(() => {
      window.scrollTo(0, 0);
      groupMessagesEl.scrollTop = groupMessagesEl.scrollHeight;
    }, 350);
  }

  groupInputEl.addEventListener("focus", keepGroupComposerVisible);
  imperialGroupInputEl?.addEventListener("input", () => {
    groupInputEl.value = imperialGroupInputEl.value;
    fitImperialComposer(imperialGroupInputEl);
  });
  imperialGroupInputEl?.addEventListener("keydown", e => {
    if (e.key !== "Enter" || e.shiftKey || e.isComposing) return;
    e.preventDefault();
    groupInputEl.value = imperialGroupInputEl.value;
    sendGroup();
  });
  imperialGroupInputEl?.addEventListener("focus", keepGroupComposerVisible);

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
    const quietRoomPicker = mode.startsWith("reading") || mode.startsWith("music");
    const pickerCharacters = quietRoomPicker ? READING_CHAR_LIST : CHAR_LIST;
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
          if ((pickerMode.startsWith("reading") || pickerMode.startsWith("music")) && pickerSelected.size >= 2) {
            showToast(pickerMode.startsWith("music")
              ? "一起听喊一两位就刚刚好"
              : "共读一次喊一两位就刚刚好");
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
    } else if (pickerMode === "music_participants") {
      if (pickerSelected.size > 2) {
        showToast("最多选两位一起听");
        return;
      }
      document.getElementById("charPickerOverlay").classList.add("hidden");
      await saveMusicParticipants([...pickerSelected]);
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
  let groupLatestPinToken = 0;
  let groupLatestPinActive = false;

  function scrollGroupToLatest({ settle = false } = {}) {
    const token = ++groupLatestPinToken;
    groupLatestPinActive = true;
    const pin = () => {
      if (token !== groupLatestPinToken || !groupLatestPinActive) return;
      const view = document.getElementById("groupView");
      if (!view.classList.contains("active")) return;
      groupMessagesEl.scrollTop = groupMessagesEl.scrollHeight;
    };
    requestAnimationFrame(() => {
      pin();
      requestAnimationFrame(pin);
    });
    if (document.fonts?.ready) document.fonts.ready.then(pin).catch(() => {});
    if (settle) {
      setTimeout(pin, 100);
      setTimeout(() => {
        pin();
        if (token === groupLatestPinToken) groupLatestPinActive = false;
      }, 320);
    } else {
      setTimeout(() => {
        if (token === groupLatestPinToken) groupLatestPinActive = false;
      }, 80);
    }
  }

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
    focusGroupComposer();
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
    const voice = parseVoiceContent(content, messageId);
    if (voice && role !== "user") {
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
      block.appendChild(buildVoiceBubble(voice, messageId));
      const timeStr = formatMsgTime(time);
      if (timeStr) {
        const timeDiv = document.createElement("div");
        timeDiv.className = "msg-time";
        timeDiv.textContent = timeStr;
        block.appendChild(timeDiv);
      }
      return block;
    }
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
    if (parseVoiceContent(content, messageId)) {
      renderGroupMsg(character_id, character_name, role, content, time, toolsCalled, metrics, messageId, quote);
      return;
    }
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
    scrollGroupToLatest({ settle: true });
  }

  let groupOldestId    = null;
  let groupHasMore     = true;
  let groupLoadingMore = false;

  async function loadGroupHistory() {
    if (groupHistoryLoaded) {
      if (!groupMessagesEl.childElementCount) renderGroupFromCache();
      scrollGroupToLatest({ settle: true });
      return;
    }
    groupHistoryLoaded = true;
    try {
      const resp = await fetch(`/api/messages?session_id=group_chat&limit=${HISTORY_PAGE_SIZE}`);
      const data = await resp.json();
      groupMessagesEl.innerHTML = "";
      groupHistory.length = 0;
      const msgs = data.messages || [];
      msgs.forEach(m => {
        const charName = GROUP_CHAR_NAMES[m.character_id] || m.character_id;
        groupHistory.push({ id: m.id, character_id: m.character_id, character_name: charName, role: m.role, content: m.content, time: m.created_at, toolsCalled: m.tools_called || [], metrics: m.metrics, quote: m.quote });
        renderGroupMsg(m.character_id, charName, m.role, m.content, m.created_at, m.tools_called || [], m.metrics, m.id, m.quote);
      });
      groupOldestId = msgs.length ? msgs[0].id : null;
      groupHasMore  = !!data.has_more;
      scrollGroupToLatest({ settle: true });
      cacheGroupHistorySnapshot();
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
    if (groupLatestPinActive) return;
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
    clearNativeSelection(true);
    const messageId = Number(block.dataset.messageId);
    if (!messageId) return;
    const bubbleText = bubble.dataset.bubbleText || bubble.textContent || "";
    const menu = document.getElementById("bubbleActionMenu");
    const quoteButton = document.getElementById("bubbleQuoteBtn");
    const quoteDivider = document.getElementById("bubbleQuoteDivider");
    quoteButton.classList.remove("hidden");
    quoteDivider.classList.remove("hidden");
    document.getElementById("bubbleDeleteBtn").classList.remove("hidden");
    document.getElementById("bubbleDeleteDivider").classList.remove("hidden");
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
      focusGroupComposer();
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
    setGroupComposerState(busy, placeholder);
    groupSendBtn.disabled = busy;
    groupContinuePickerBtn.disabled = busy;
  }

  async function sendGroup() {
    if (groupBusy) return;
    const text = groupInputEl.value.trim();
    if (!text) return;
    const pendingQuote = groupReplyTarget ? { ...groupReplyTarget } : null;
    setGroupComposerValue("");
    setGroupBusy(true, GROUP_REPLY_PLACEHOLDER);

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
        groupHistory.push({ character_id: "user", character_name: GROUP_CHAR_NAMES.user, role: "user", content: text, time: userTime, quote: pendingQuote });
        renderGroupMsg("user", GROUP_CHAR_NAMES.user, "user", text, userTime, [], null, null, pendingQuote);
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
      focusGroupComposer();
    }
  }

  async function continueGroup() {
    if (groupBusy || onlineCharacters.size === 0) return;
    blurGroupComposer();
    setGroupBusy(true, GROUP_CONTINUE_PLACEHOLDER);
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

  // groupSend：单击立即发送，长按选择在线角色。
  let _groupSendPressTimer = null;
  let _groupSendSuppressClickUntil = 0;

  function cancelGroupSendPress() {
    clearTimeout(_groupSendPressTimer);
    _groupSendPressTimer = null;
  }

  groupSendBtn.addEventListener("pointerdown", () => {
    if (groupSendBtn.disabled) return;
    cancelGroupSendPress();
    _groupSendPressTimer = setTimeout(() => {
      clearNativeSelection(true);
      _groupSendPressTimer = null;
      _groupSendSuppressClickUntil = Date.now() + 800;
      navigator.vibrate?.(18);
      openCharPicker("online", null, [...onlineCharacters]);
    }, 500);
  });
  ["pointerup", "pointermove", "pointercancel", "pointerleave"].forEach(type => {
    groupSendBtn.addEventListener(type, cancelGroupSendPress);
  });
  bindNativeLongPressGuard(groupSendBtn);
  groupSendBtn.addEventListener("click", event => {
    if (Date.now() < _groupSendSuppressClickUntil) {
      event.preventDefault();
      return;
    }
    sendGroup();
  });
  groupInputEl.addEventListener("keydown", e => { if (e.key === "Enter") sendGroup(); });

  // ════════════════════════════════════════════
  // 记忆视图
  // ════════════════════════════════════════════
  const SECONDARY_VIEW_CACHE_PREFIX = "becoming-view-cache-v1:";

  function readSecondaryViewCache(key) {
    try {
      const cached = JSON.parse(localStorage.getItem(SECONDARY_VIEW_CACHE_PREFIX + key) || "null");
      return cached?.value ?? null;
    } catch (_) {
      return null;
    }
  }

  function writeSecondaryViewCache(key, value) {
    try {
      localStorage.setItem(
        SECONDARY_VIEW_CACHE_PREFIX + key,
        JSON.stringify({ saved_at: Date.now(), value }),
      );
    } catch (_) {}
  }

  function cacheGroupHistorySnapshot() {
    if (!groupHistory.length) return;
    writeSecondaryViewCache("group-history", groupHistory.slice(-40));
  }

  function hydrateSecondaryViewsFromCache() {
    const cachedGroup = readSecondaryViewCache("group-history");
    if (Array.isArray(cachedGroup) && cachedGroup.length && !groupHistory.length) {
      groupHistory.push(...cachedGroup);
      renderGroupFromCache();
    }
    const cachedMemory = readSecondaryViewCache("memory-overview");
    if (Array.isArray(cachedMemory) && cachedMemory.length) renderMemoryList(cachedMemory);
    const cachedUsage = readSecondaryViewCache("usage");
    if (cachedUsage && typeof cachedUsage === "object") renderUsage(cachedUsage);
  }

  async function fetchMemoryOverview() {
    const res = await fetch("/api/memory");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const characters = data.characters || [];
    writeSecondaryViewCache("memory-overview", characters);
    return data;
  }

  async function fetchUsage() {
    const res = await fetch("/api/usage");
    const data = await res.json();
    writeSecondaryViewCache("usage", data);
    return data;
  }

  const CHAR_DISPLAY_NAMES = {
    char1: "Char 1",
    char2:  "Char 2",
    char3:   "Char 3",
    char4:  "Char 4",
    char5:    "Char 5",
    char6:    "Char 6",
  };
  const PROVIDER_FALLBACK_LABELS = {
    openrouter: "OpenRouter",
    anthropic: "Anthropic 官方",
    deepseek: "DeepSeek 官方",
    custom_openai: "自定义 OpenAI-compatible",
  };
  // 内部总账统一使用 USD；供应商卡片可按实际结算习惯换算后展示。
  const PROVIDER_MONEY_DISPLAY = {
    deepseek: { symbol: "¥", rate_key: "cny_per_usd", fallback_rate: 6.78 },
  };

  function providerDisplayName(provider, providers = null) {
    return providers?.[provider]?.label || PROVIDER_FALLBACK_LABELS[provider] || provider || "未知供应商";
  }

  function renderUsage(data) {
    const panel = document.getElementById("usagePanel");
    if (!panel) return;
    panel.innerHTML = "";

    // 平台卡片
    const byPlatform = data.by_platform || {};
    const platformLimits = data.platform_limits || {};
    const platformCards = document.createElement("div");
    platformCards.className = "usage-platforms";
    const providers = data.providers || {};
    // 额度卡只展示当前真正配置了后端凭证的供应商；旧用量仍计入角色与总计。
    const providerKeys = Object.keys(providers)
      .filter(key => providers[key]?.configured);
    providerKeys.forEach(key => {
      const label = providerDisplayName(key, providers);
      const spent = byPlatform[key] || 0;
      const lim = platformLimits[key] || 0;
      const money = PROVIDER_MONEY_DISPLAY[key] || { symbol: "$", fallback_rate: 1 };
      const displayRate = Number(data[money.rate_key]) || money.fallback_rate;
      const displaySpent = spent * displayRate;
      const displayLimit = lim * displayRate;
      const card = document.createElement("div");
      card.className = "usage-platform-card";
      card.innerHTML = `
        <div class="usage-platform-name">${label}</div>
        <div class="usage-platform-amt">${money.symbol}${displaySpent.toFixed(2)}</div>
        <div class="usage-platform-name" style="margin-top:2px;">/ ${money.symbol}${displayLimit.toFixed(0)}</div>
      `;
      platformCards.appendChild(card);
    });
    if (providerKeys.length) panel.appendChild(platformCards);

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
        <span class="usage-label">${escapeReadingHtml(CHAR_DISPLAY_NAMES[cid] || cid)}</span>
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
  async function renderMemoryList(characters, backend = null) {
    const container = document.getElementById("memoryCardContainer");
    const renderVersion = ++memoryListRenderVersion;
    const fragment = document.createDocumentFragment();
    const hasAdmin = backend?.capabilities?.includes("admin") !== false;
    if (backend && (!backend.enabled || !hasAdmin)) {
      const notice = document.createElement("div");
      notice.className = "memory-backend-notice";
      notice.innerHTML = backend.enabled
        ? '<span class="material-symbols-outlined">cloud_sync</span><div><strong>正在使用外置记忆库</strong><small>聊天中的读取与写入照常进行；查看和编辑请到外置记忆库中完成。</small></div>'
        : '<span class="material-symbols-outlined">memory_off</span><div><strong>长期记忆目前已关闭</strong><small>聊天摘要仍保存在本机数据库，需要时可在服务器配置中重新开启。</small></div>';
      fragment.appendChild(notice);
    }
    characters.forEach(s => {
      const card = document.createElement("div");
      card.className = "memory-card" + (hasAdmin ? "" : " memory-card-external");
      if (hasAdmin) {
        card.tabIndex = 0;
        card.setAttribute("role", "button");
      }

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
      count.textContent = s.count == null
        ? (backend?.enabled ? "由外置记忆库管理" : "长期记忆已关闭")
        : (s.count ? `${s.count} 段记忆` : "还没有留下记忆");
      identity.appendChild(name);
      identity.appendChild(count);
      top.appendChild(avatar);
      top.appendChild(identity);

      const preview = document.createElement("div");
      preview.className = "memory-card-preview";
      preview.textContent = s.count == null
        ? (backend?.enabled
          ? "这部分内容不会从外置记忆库拉回管理页。"
          : "开启记忆后，这里会重新长出内容。")
        : (truncateText(s.latest?.content, 92) || "这里暂时安安静静。");
      card.appendChild(top);
      card.appendChild(preview);
      if (s.latest?.created) {
        const date = document.createElement("time");
        date.className = "memory-card-date";
        date.textContent = memoryDate(s.latest.created);
        card.appendChild(date);
      }

      if (hasAdmin) {
        const open = () => loadCharacterMemories(s.character_id, s.name);
        card.addEventListener("click", open);
        card.addEventListener("keydown", e => {
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
        });
      }
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
          await Promise.all([
            loadCharacterMemories(currentMemoryCharacter, currentMemoryName, false),
            loadMemoryView(),
          ]);
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
      const data = await fetchMemoryOverview();
      await renderMemoryList(data.characters || [], data.backend || null);
    } catch (e) {
      console.warn("loadMemoryView failed", e);
    }
  }

  let secondaryViewsWarmupStarted = false;
  function scheduleSecondaryViewsWarmup() {
    if (secondaryViewsWarmupStarted) return;
    secondaryViewsWarmupStarted = true;
    const warm = () => {
      Promise.allSettled([
        loadGroupHistory(),
        loadMemoryView(),
        loadUsagePanel(),
        loadCompressHealth(),
      ]);
    };
    if (typeof requestIdleCallback === "function") {
      requestIdleCallback(warm, { timeout: 1200 });
    } else {
      setTimeout(warm, 450);
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
        setTimeout(() => {
          splash.remove();
          document.documentElement.classList.remove("chrome-shell");
        }, 500);
      } else if (document.getElementById("loginOverlay").style.display === "none") {
        document.documentElement.classList.remove("chrome-shell");
      }
    }, delay);
  }

  // ── MCP 工具面板 ──
  let _toolsPanelOpen = false;

  async function openToolsPanel() {
    if (_voicePanelOpen) closeVoicePanel();
    if (_appearancePanelOpen) closeAppearancePanel();
    if (_personaPanelOpen) closePersonaPanel();
    if (_schedulerPanelOpen) closeSchedulerPanel();
    if (_memoryImportPanelOpen) closeMemoryImportPanel();
    if (_gestureHelpPanelOpen) closeGestureHelpPanel();
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
    if (_voicePanelOpen) closeVoicePanel();
    if (_appearancePanelOpen) closeAppearancePanel();
    if (_personaPanelOpen) closePersonaPanel();
    if (_toolsPanelOpen) closeToolsPanel();
    if (_memoryImportPanelOpen) closeMemoryImportPanel();
    if (_gestureHelpPanelOpen) closeGestureHelpPanel();
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
      desire_frequency: "low",
      desire_quiet_start: "23:30",
      desire_quiet_end: "08:30",
    };
    let limitCfg = {
      char1: 10, char2: 30, char3: 10,
      char4: 10, char5: 30, char6: 50,
    };
    let sleepCfg = {};
    let modelProviderCfg = { providers: {}, summary: {} };
    try {
      const [schedulerRes, limitsRes, sleepRes, providersRes] = await Promise.all([
        fetch("/api/scheduler/config"),
        fetch("/api/limits"),
        fetch("/api/sleep/config"),
        fetch("/api/model-providers"),
      ]);
      cfg = await schedulerRes.json();
      ({ limits: limitCfg } = await limitsRes.json());
      sleepCfg = await sleepRes.json();
      modelProviderCfg = await providersRes.json();
    } catch(e) {}
    const selMomSlots = new Set(cfg.moments_slots ? cfg.moments_slots.split(",").filter(Boolean) : []);
    let desireEnabled = cfg.desire_enabled !== false;
    let desireFrequency = ["low", "medium", "high"].includes(cfg.desire_frequency)
      ? cfg.desire_frequency
      : "low";

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

      const frequencyWrap = document.createElement("div");
      frequencyWrap.className = "scheduler-frequency-control";
      const frequencyHead = document.createElement("div");
      frequencyHead.className = "scheduler-frequency-head";
      const frequencyLabel = document.createElement("span");
      frequencyLabel.textContent = "主动频率";
      const frequencyValue = document.createElement("strong");
      const frequencyLevels = [
        { value: "low", label: "克制" },
        { value: "medium", label: "日常" },
        { value: "high", label: "热闹" },
      ];
      const frequencySlider = document.createElement("input");
      frequencySlider.type = "range";
      frequencySlider.min = "0";
      frequencySlider.max = "2";
      frequencySlider.step = "1";
      frequencySlider.className = "scheduler-frequency-slider";
      frequencySlider.setAttribute("aria-label", "主动频率");
      const frequencyScale = document.createElement("div");
      frequencyScale.className = "scheduler-frequency-scale";
      frequencyLevels.forEach(level => {
        const mark = document.createElement("span");
        mark.textContent = level.label;
        mark.dataset.frequency = level.value;
        frequencyScale.appendChild(mark);
      });
      const applyFrequency = () => {
        const index = Math.max(0, frequencyLevels.findIndex(level => level.value === desireFrequency));
        frequencySlider.value = String(index);
        frequencySlider.style.setProperty("--frequency-progress", `${index * 50}%`);
        frequencyValue.textContent = frequencyLevels[index].label;
        frequencyScale.querySelectorAll("span").forEach(mark => {
          mark.classList.toggle("is-active", mark.dataset.frequency === desireFrequency);
        });
      };
      frequencySlider.oninput = () => {
        desireFrequency = frequencyLevels[Number(frequencySlider.value)]?.value || "low";
        applyFrequency();
      };
      applyFrequency();
      frequencyHead.appendChild(frequencyLabel);
      frequencyHead.appendChild(frequencyValue);
      frequencyWrap.appendChild(frequencyHead);
      frequencyWrap.appendChild(frequencySlider);
      frequencyWrap.appendChild(frequencyScale);

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
      wrap.appendChild(frequencyWrap);
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

    function makeProviderStatusControls() {
      const wrap = document.createElement("div");
      wrap.className = "provider-status-list";
      Object.entries(modelProviderCfg.providers || {}).forEach(([key, info]) => {
        const row = document.createElement("div");
        row.className = "provider-status-row";
        const text = document.createElement("span");
        const name = document.createElement("strong");
        name.textContent = info.label || key;
        const envHint = document.createElement("small");
        envHint.textContent = info.configured ? "后端凭证已配置" : "尚未配置后端环境变量";
        text.append(name, envHint);
        const badge = document.createElement("span");
        badge.className = `provider-status-badge ${info.configured ? "is-ready" : ""}`;
        badge.textContent = info.configured ? "可用" : "未配置";
        row.append(text, badge);
        wrap.appendChild(row);
      });
      const note = document.createElement("p");
      note.className = "provider-status-note";
      note.textContent = "API Key 只从服务器环境变量读取，浏览器不会收到或回显。角色与摘要的供应商请到「换毛期°人设编辑」切换。";
      wrap.appendChild(note);
      return wrap;
    }

    function makeSleepControls() {
      const wrap = document.createElement("div");
      wrap.style.cssText = "display:flex;flex-direction:column;gap:12px;";
      const CHAR_ORDER = ["char1","char2","char3","char4","char5","char6"];
      CHAR_ORDER.forEach(cid => {
        const sc = sleepCfg[cid] || {};
        const card = document.createElement("div");
        card.style.cssText = "background:rgba(var(--dusky-rgb),.06);border-radius:12px;padding:12px 14px;display:flex;flex-direction:column;gap:8px;";

        // 名字 + 状态徽章
        const hdrRow = document.createElement("div");
        hdrRow.style.cssText = "display:flex;align-items:center;gap:8px;";
        const nameSpan = document.createElement("span");
        nameSpan.textContent = nickName(cid);
        nameSpan.style.cssText = "font-weight:600;font-size:14px;";
        const badge = document.createElement("span");
        badge.textContent = sc.current_state === "asleep" ? "😴 睡着" : "🌤 醒着";
        badge.style.cssText = "font-size:11px;padding:2px 8px;border-radius:20px;background:rgba(var(--dusky-rgb),.15);color:var(--dusky);";
        hdrRow.appendChild(nameSpan);
        hdrRow.appendChild(badge);
        card.appendChild(hdrRow);

        // 睡点 / 起床
        const timeRow = document.createElement("div");
        timeRow.style.cssText = "display:flex;gap:12px;flex-wrap:wrap;";
        [["bedtime","睡点","sleep_bedtime_"],["waketime","起床","sleep_waketime_"]].forEach(([field,label,prefix]) => {
          const lbl = document.createElement("label");
          lbl.style.cssText = "display:flex;flex-direction:column;gap:3px;font-size:12px;color:var(--muted);";
          lbl.textContent = label;
          const inp = document.createElement("input");
          inp.type = "time";
          inp.value = sc[field] || "";
          inp.dataset.sleepField = prefix + cid;
          inp.style.cssText = "border:1px solid var(--border);border-radius:8px;padding:4px 8px;background:var(--card);color:var(--text);font-size:13px;";
          lbl.appendChild(inp);
          timeRow.appendChild(lbl);
        });
        card.appendChild(timeRow);

        // resist_bias 滑条
        const sliderRow = document.createElement("div");
        sliderRow.style.cssText = "display:flex;flex-direction:column;gap:3px;";
        const sliderLbl = document.createElement("div");
        sliderLbl.style.cssText = "display:flex;justify-content:space-between;font-size:12px;color:var(--muted);";
        const sliderTxt = document.createElement("span");
        sliderTxt.textContent = "硬撑倾向";
        const sliderVal = document.createElement("span");
        const slider = document.createElement("input");
        slider.type = "range";
        slider.min = "0"; slider.max = "1"; slider.step = "0.05";
        slider.value = sc.resist_bias ?? 0.4;
        sliderVal.textContent = Number(slider.value).toFixed(2);
        slider.dataset.sleepField = "sleep_resist_" + cid;
        slider.style.cssText = "width:100%;accent-color:var(--dusky);";
        slider.oninput = () => { sliderVal.textContent = Number(slider.value).toFixed(2); };
        sliderLbl.appendChild(sliderTxt);
        sliderLbl.appendChild(sliderVal);
        sliderRow.appendChild(sliderLbl);
        sliderRow.appendChild(slider);
        card.appendChild(sliderRow);

        // 反向催睡开关
        const nagRow = document.createElement("div");
        nagRow.style.cssText = "display:flex;align-items:center;justify-content:space-between;";
        const nagLbl = document.createElement("span");
        nagLbl.style.cssText = "font-size:12px;color:var(--muted);";
        nagLbl.textContent = "到睡点自动催你睡";
        let nagOn = !!sc.nag_enabled;
        const nagBtn = document.createElement("button");
        const applyNag = () => {
          nagBtn.className = "tool-toggle" + (nagOn ? " tool-toggle-on" : "");
          nagBtn.textContent = nagOn ? "开" : "关";
          nagBtn.dataset.sleepNag = cid;
          nagBtn.dataset.sleepNagVal = nagOn ? "true" : "false";
        };
        applyNag();
        nagBtn.onclick = () => { nagOn = !nagOn; applyNag(); };
        nagRow.appendChild(nagLbl);
        nagRow.appendChild(nagBtn);
        card.appendChild(nagRow);

        // chronotype 文本
        const cTypeRow = document.createElement("div");
        cTypeRow.style.cssText = "display:flex;flex-direction:column;gap:3px;";
        const cTypeLbl = document.createElement("span");
        cTypeLbl.style.cssText = "font-size:11px;color:var(--muted);";
        cTypeLbl.textContent = "人设描述（注入困意时使用）";
        const cTypeInput = document.createElement("input");
        cTypeInput.type = "text";
        cTypeInput.value = sc.chronotype || "";
        cTypeInput.dataset.sleepField = "sleep_chron_" + cid;
        cTypeInput.style.cssText = "border:1px solid var(--border);border-radius:8px;padding:6px 10px;background:var(--card);color:var(--text);font-size:12px;width:100%;";
        cTypeRow.appendChild(cTypeLbl);
        cTypeRow.appendChild(cTypeInput);
        card.appendChild(cTypeRow);

        wrap.appendChild(card);
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
        // 收集睡眠配置
        const sleepPayload = {};
        const CHAR_ORDER_S = ["char1","char2","char3","char4","char5","char6"];
        CHAR_ORDER_S.forEach(cid => { sleepPayload[cid] = {}; });
        panel.querySelectorAll("[data-sleep-field]").forEach(el => {
          const key = el.dataset.sleepField;
          if (key.startsWith("sleep_bedtime_"))  sleepPayload[key.slice(14)].bedtime     = el.value;
          if (key.startsWith("sleep_waketime_")) sleepPayload[key.slice(15)].waketime    = el.value;
          if (key.startsWith("sleep_resist_"))   sleepPayload[key.slice(13)].resist_bias = el.value;
          if (key.startsWith("sleep_chron_"))    sleepPayload[key.slice(12)].chronotype  = el.value;
        });
        panel.querySelectorAll("[data-sleep-nag]").forEach(btn => {
          const cid = btn.dataset.sleepNag;
          sleepPayload[cid].nag_enabled = btn.dataset.sleepNagVal === "true";
        });

        const [schedulerSave, limitSave, sleepSave] = await Promise.all([
          fetch("/api/scheduler/config", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              moments_slots: [...selMomSlots].join(","),
              desire_enabled: desireEnabled,
              desire_frequency: desireFrequency,
              desire_quiet_start: document.getElementById("desireQuietStart")?.value || "23:30",
              desire_quiet_end: document.getElementById("desireQuietEnd")?.value || "08:30",
            }),
          }),
          fetch("/api/limits", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ limits }),
          }),
          fetch("/api/sleep/config", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(sleepPayload),
          }),
        ]);
        if (!schedulerSave.ok || !limitSave.ok || !sleepSave.ok) throw new Error("save failed");
        await loadUsagePanel();
        saveBtn.textContent = "已保存 🐾";
        setTimeout(() => { saveBtn.textContent = "保存配置 🐾"; }, 1500);
      } catch(e) {
        saveBtn.textContent = "保存失败，重试";
      }
    };

    function makeAccordion(title, bodyChildren, showSave = true) {
      const wrap = document.createElement("div");

      const hdr = document.createElement("button");
      hdr.textContent = title;
      hdr.style.cssText = "width:100%;padding:14px 20px;background:var(--cream);border:2px solid var(--dusky);border-radius:999px;color:var(--dusky);font-size:15px;font-weight:500;text-align:left;cursor:pointer;transition:border-radius .2s;appearance:none;-webkit-appearance:none;box-sizing:border-box;";

      const body = document.createElement("div");
      body.style.cssText = "display:none;flex-direction:column;gap:10px;padding:14px 16px;background:var(--cream);border:2px solid var(--dusky);border-top:none;border-radius:0 0 16px 16px;box-sizing:border-box;";
      body.dataset.schedBody = "true";
      body.dataset.schedSave = showSave ? "true" : "false";
      body.dataset.open = "false";
      bodyChildren.forEach(ch => body.appendChild(ch));

      let isOpen = false;
      hdr.onclick = () => {
        isOpen = !isOpen;
        body.style.display = isOpen ? "flex" : "none";
        body.dataset.open = isOpen ? "true" : "false";
        hdr.style.borderRadius = isOpen ? "16px 16px 0 0" : "999px";
        const anyOpen = [...panel.querySelectorAll("[data-sched-save='true']")]
          .some(b => b.dataset.open === "true");
        saveBtn.style.display = anyOpen ? "block" : "none";
      };

      wrap.appendChild(hdr);
      wrap.appendChild(body);
      return wrap;
    }

    const voiceLink = document.createElement("button");
    voiceLink.type = "button";
    voiceLink.className = "scheduler-feature-link";
    voiceLink.textContent = "说说喵°语音收发";
    voiceLink.onclick = openVoicePanel;

    panel.appendChild(makeAccordion("醒醒喵°欲望心跳", [makeDesireControls()]));
    panel.appendChild(makeAccordion("聊聊喵°自动发帖", [makeSlotRow(selMomSlots)]));
    panel.appendChild(voiceLink);
    panel.appendChild(makeAccordion("饭饭喵°月度额度", [makeLimitControls()]));
    panel.appendChild(makeAccordion("眠眠喵°睡眠节律", [makeSleepControls()]));
    panel.appendChild(makeAccordion("路由喵°模型供应商", [makeProviderStatusControls()], false));
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

  async function saveAppearanceWeather(effectId) {
    const res = await fetch("/api/appearance", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ weather_effect: effectId }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "天气效果没有换好");
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

    const weatherSection = document.createElement("section");
    weatherSection.className = "appearance-section";
    const weatherTitle = document.createElement("div");
    weatherTitle.className = "appearance-section-title";
    weatherTitle.innerHTML = '<span class="material-symbols-outlined">partly_cloudy_day</span><span>天气氛围</span>';
    const weatherGrid = document.createElement("div");
    weatherGrid.className = "appearance-weather-grid";
    [
      { id: "off", name: "关闭", icon: "clear_day" },
      { id: "rain", name: "下雨", icon: "rainy" },
      { id: "snow", name: "落雪", icon: "weather_snowy" },
      { id: "leaves", name: "落叶", icon: "eco" },
    ].forEach(weather => {
      const option = document.createElement("button");
      option.type = "button";
      option.className = "appearance-weather-option";
      option.classList.toggle("active", weather.id === data.weather_effect);
      option.setAttribute("aria-pressed", weather.id === data.weather_effect ? "true" : "false");
      option.innerHTML = `<span class="material-symbols-outlined">${weather.icon}</span><span>${weather.name}</span>`;
      option.addEventListener("click", async () => {
        if (weather.id === appearanceState?.weather_effect) return;
        weatherGrid.querySelectorAll("button").forEach(button => { button.disabled = true; });
        try {
          await saveAppearanceWeather(weather.id);
          showToast(weather.id === "off" ? "天气安静下来啦" : `${weather.name}落进来啦`);
          await renderAppearancePanel();
        } catch (e) {
          showToast(e.message);
          weatherGrid.querySelectorAll("button").forEach(button => { button.disabled = false; });
        }
      });
      weatherGrid.appendChild(option);
    });
    weatherSection.append(weatherTitle, weatherGrid);
    panel.appendChild(weatherSection);

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
      name.textContent = cid === "user" ? GROUP_CHAR_NAMES.user : nickName(cid);
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
    if (_voicePanelOpen) closeVoicePanel();
    if (_personaPanelOpen) closePersonaPanel();
    if (_toolsPanelOpen) closeToolsPanel();
    if (_schedulerPanelOpen) closeSchedulerPanel();
    if (_memoryImportPanelOpen) closeMemoryImportPanel();
    if (_gestureHelpPanelOpen) closeGestureHelpPanel();
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
  let _activeProviderPicker = null;
  let _providerPickerId = 0;

  function closeProviderPicker({ restoreFocus = true } = {}) {
    if (!_activeProviderPicker) return;
    const { overlay, trigger, keyHandler } = _activeProviderPicker;
    document.removeEventListener("keydown", keyHandler);
    overlay.remove();
    trigger.setAttribute("aria-expanded", "false");
    _activeProviderPicker = null;
    if (restoreFocus && document.body.contains(trigger)) {
      trigger.focus({ preventScroll: true });
    }
  }

  function makeProviderPicker(providers, selected, onChange) {
    const entries = Object.entries(providers);
    let value = providers[selected] ? selected : (entries[0]?.[0] || "");
    const pickerId = `providerPicker${++_providerPickerId}`;
    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "provider-picker-trigger";
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    trigger.setAttribute("aria-controls", `${pickerId}List`);

    const triggerLabel = document.createElement("span");
    triggerLabel.className = "provider-picker-trigger-label";
    const chevron = document.createElement("span");
    chevron.className = "material-symbols-outlined provider-picker-chevron";
    chevron.setAttribute("aria-hidden", "true");
    chevron.textContent = "expand_more";
    trigger.append(triggerLabel, chevron);

    const renderTrigger = () => {
      const info = providers[value] || {};
      triggerLabel.textContent = info.label || value || "选择供应商";
      trigger.classList.toggle("is-unconfigured", !info.configured);
      trigger.setAttribute(
        "aria-label",
        `模型供应商：${info.label || value || "未选择"}${info.configured ? "，后端凭证已配置" : "，尚未配置环境变量"}`,
      );
    };
    renderTrigger();

    trigger.addEventListener("click", () => {
      if (_activeProviderPicker?.trigger === trigger) {
        closeProviderPicker();
        return;
      }
      closeProviderPicker({ restoreFocus: false });

      const overlay = document.createElement("div");
      overlay.className = "provider-picker-overlay";
      overlay.setAttribute("aria-hidden", "false");
      const sheet = document.createElement("section");
      sheet.className = "provider-picker-sheet";
      sheet.setAttribute("role", "dialog");
      sheet.setAttribute("aria-modal", "true");
      sheet.setAttribute("aria-labelledby", `${pickerId}Title`);

      // 不使用 <header>：项目的会话顶栏有全局 header 主题样式。
      const header = document.createElement("div");
      header.className = "provider-picker-header";
      const headingIcon = document.createElement("span");
      headingIcon.className = "material-symbols-outlined provider-picker-heading-icon";
      headingIcon.setAttribute("aria-hidden", "true");
      headingIcon.textContent = "pets";
      const heading = document.createElement("div");
      heading.className = "provider-picker-heading";
      heading.id = `${pickerId}Title`;
      heading.innerHTML = "<strong>选择模型供应商</strong><small>密钥仍只保存在服务器里</small>";
      const closeButton = document.createElement("button");
      closeButton.type = "button";
      closeButton.className = "provider-picker-close";
      closeButton.setAttribute("aria-label", "关闭供应商选择");
      closeButton.innerHTML = '<span class="material-symbols-outlined" aria-hidden="true">close</span>';
      closeButton.addEventListener("click", () => closeProviderPicker());
      header.append(headingIcon, heading, closeButton);

      const list = document.createElement("div");
      list.className = "provider-picker-list";
      list.id = `${pickerId}List`;
      list.setAttribute("role", "listbox");
      list.setAttribute("aria-labelledby", `${pickerId}Title`);
      const optionButtons = [];
      entries.forEach(([key, info]) => {
        const option = document.createElement("button");
        option.type = "button";
        option.className = "provider-picker-option";
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", key === value ? "true" : "false");
        option.dataset.provider = key;
        const copy = document.createElement("span");
        copy.className = "provider-picker-option-copy";
        const label = document.createElement("strong");
        label.textContent = info.label || key;
        const state = document.createElement("small");
        state.className = `provider-picker-option-state${info.configured ? " is-ready" : ""}`;
        state.textContent = info.configured ? "后端凭证已配置" : "需先配置服务器环境变量";
        const check = document.createElement("span");
        check.className = "material-symbols-outlined provider-picker-check";
        check.setAttribute("aria-hidden", "true");
        check.textContent = "check";
        copy.append(label, state);
        option.append(copy, check);
        option.addEventListener("click", () => {
          value = key;
          renderTrigger();
          closeProviderPicker();
          onChange?.(value);
        });
        optionButtons.push(option);
        list.appendChild(option);
      });
      list.addEventListener("keydown", event => {
        const currentIndex = optionButtons.indexOf(document.activeElement);
        let nextIndex = currentIndex;
        if (event.key === "ArrowDown") nextIndex = (currentIndex + 1) % optionButtons.length;
        else if (event.key === "ArrowUp") nextIndex = (currentIndex - 1 + optionButtons.length) % optionButtons.length;
        else if (event.key === "Home") nextIndex = 0;
        else if (event.key === "End") nextIndex = optionButtons.length - 1;
        else return;
        event.preventDefault();
        optionButtons[nextIndex]?.focus();
      });
      sheet.append(header, list);
      overlay.appendChild(sheet);
      overlay.addEventListener("pointerdown", event => {
        if (event.target === overlay) closeProviderPicker();
      });
      const keyHandler = event => {
        if (event.key === "Escape") {
          event.preventDefault();
          closeProviderPicker();
        }
      };
      _activeProviderPicker = { overlay, trigger, keyHandler };
      document.addEventListener("keydown", keyHandler);
      document.body.appendChild(overlay);
      trigger.setAttribute("aria-expanded", "true");
      requestAnimationFrame(() => {
        (optionButtons.find(option => option.dataset.provider === value) || optionButtons[0])?.focus();
      });
    });

    return {
      element: trigger,
      get value() { return value; },
    };
  }

  async function openPersonaPanel() {
    if (_voicePanelOpen) closeVoicePanel();
    if (_appearancePanelOpen) closeAppearancePanel();
    if (_toolsPanelOpen) closeToolsPanel();
    if (_schedulerPanelOpen) closeSchedulerPanel();
    if (_memoryImportPanelOpen) closeMemoryImportPanel();
    if (_gestureHelpPanelOpen) closeGestureHelpPanel();
    const panel = document.getElementById("personaPanel");
    const placeholder = panel.nextElementSibling;
    panel.innerHTML = "";

    let personas;
    let characterConfig;
    let providerConfig;
    try {
      const [personaRes, configRes, providerRes] = await Promise.all([
        fetch("/api/personas"),
        fetch("/api/character-config"),
        fetch("/api/model-providers"),
      ]);
      personas = await personaRes.json();
      characterConfig = await configRes.json();
      providerConfig = await providerRes.json();
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

    const providers = providerConfig.providers || {};
    const updateProviderHint = (hint, provider) => {
      const info = providers[provider] || {};
      hint.textContent = info.configured
        ? "后端凭证已配置"
        : "需先在服务器环境变量中配置凭证";
      hint.classList.toggle("is-ready", !!info.configured);
    };

    const summaryCard = document.createElement("section");
    summaryCard.className = "persona-card provider-summary-card";
    const summaryTitle = document.createElement("div");
    summaryTitle.className = "persona-card-name";
    summaryTitle.textContent = "压缩与群聊摘要";
    const summaryNote = document.createElement("p");
    summaryNote.className = "provider-summary-note";
    summaryNote.textContent = "老对话压缩和群聊摘要走这一条，不再固定依赖 OpenRouter。";
    const summaryGrid = document.createElement("div");
    summaryGrid.className = "provider-config-grid";
    const summaryProviderWrap = document.createElement("div");
    summaryProviderWrap.className = "persona-model-wrap";
    summaryProviderWrap.appendChild(Object.assign(document.createElement("span"), { textContent: "供应商" }));
    const summaryModelWrap = document.createElement("label");
    summaryModelWrap.className = "persona-model-wrap";
    summaryModelWrap.appendChild(Object.assign(document.createElement("span"), { textContent: "摘要模型" }));
    const summaryModel = document.createElement("input");
    summaryModel.className = "persona-model-input";
    summaryModel.value = providerConfig.summary?.model || "";
    summaryModel.autocomplete = "off";
    summaryModelWrap.appendChild(summaryModel);
    summaryGrid.append(summaryProviderWrap, summaryModelWrap);
    const summaryActions = document.createElement("div");
    summaryActions.className = "provider-save-row";
    const summarySave = document.createElement("button");
    summarySave.className = "persona-save-btn";
    summarySave.textContent = "测试并保存";
    const summaryStatus = document.createElement("span");
    summaryStatus.className = "persona-saved-msg provider-config-status";
    const summaryProvider = makeProviderPicker(
      providers,
      providerConfig.summary?.provider || "openrouter",
      provider => {
        const defaultModel = providers[provider]?.default_model;
        if (defaultModel) summaryModel.value = defaultModel;
        updateProviderHint(summaryStatus, provider);
      },
    );
    summaryProviderWrap.appendChild(summaryProvider.element);
    updateProviderHint(summaryStatus, summaryProvider.value);
    summarySave.onclick = async () => {
      summarySave.disabled = true;
      summarySave.textContent = "连接测试中…";
      try {
        const response = await fetch("/api/model-providers/summary", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            provider: summaryProvider.value,
            model: summaryModel.value.trim(),
            verify_connection: true,
          }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || "连接失败");
        summaryStatus.textContent = "✓ 已连接并保存";
        summaryStatus.classList.add("is-ready");
      } catch (error) {
        summaryStatus.textContent = error.message || "保存失败";
        summaryStatus.classList.remove("is-ready");
      } finally {
        summarySave.disabled = false;
        summarySave.textContent = "测试并保存";
      }
    };
    summaryActions.append(summarySave, summaryStatus);
    summaryCard.append(summaryTitle, summaryNote, summaryGrid, summaryActions);
    panel.appendChild(summaryCard);

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

      let originalProvider = characterConfig[cid]?.provider || "openrouter";
      let originalModel = characterConfig[cid]?.model || "";
      const providerWrap = document.createElement("div");
      providerWrap.className = "persona-model-wrap";
      const providerCaption = document.createElement("span");
      providerCaption.textContent = "供应商";
      const providerHint = document.createElement("small");
      providerHint.className = "provider-config-status";

      const modelWrap = document.createElement("label");
      modelWrap.className = "persona-model-wrap";
      const modelCaption = document.createElement("span");
      modelCaption.textContent = "模型";
      const modelInput = document.createElement("input");
      modelInput.className = "persona-model-input";
      modelInput.type = "text";
      modelInput.value = characterConfig[cid]?.model || "";
      modelInput.autocomplete = "off";
      modelInput.spellcheck = false;
      modelWrap.appendChild(modelCaption);
      modelWrap.appendChild(modelInput);
      const providerPicker = makeProviderPicker(providers, originalProvider, provider => {
        const defaultModel = providers[provider]?.default_model;
        if (!modelInput.value.trim() || modelInput.value.trim() === originalModel) {
          if (defaultModel) modelInput.value = defaultModel;
        }
        updateProviderHint(providerHint, provider);
      });
      updateProviderHint(providerHint, providerPicker.value);
      providerWrap.append(providerCaption, providerPicker.element, providerHint);

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
        const connectionChanged = providerPicker.value !== originalProvider
          || modelInput.value.trim() !== originalModel;
        saveBtn.textContent = connectionChanged ? "测试连接中…" : "保存中…";
        try {
          const res = await fetch(`/api/character-config/${cid}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              persona: textarea.value,
              model: modelInput.value.trim(),
              provider: providerPicker.value,
              verify_connection: connectionChanged,
            }),
          });
          if (res.ok) {
            originalProvider = providerPicker.value;
            originalModel = modelInput.value.trim();
            updateImperialModelBadge(cid, originalModel);
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
          saveBtn.textContent = "保存";
        }
      });

      footer.appendChild(saveBtn);
      footer.appendChild(savedMsg);
      card.appendChild(header);
      card.appendChild(providerWrap);
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
    closeProviderPicker({ restoreFocus: false });
    const panel = document.getElementById("personaPanel");
    const placeholder = panel.nextElementSibling;
    panel.style.display = "none";
    panel.innerHTML = "";
    placeholder.style.display = "";
    _personaPanelOpen = false;
    document.getElementById("moreContent").scrollTop = 0;
  }

  // ── JSON / TXT 与旧 Ombre 记忆迁移 ──
  let _memoryImportPanelOpen = false;

  function openMemoryImportPanel() {
    if (_voicePanelOpen) closeVoicePanel();
    if (_appearancePanelOpen) closeAppearancePanel();
    if (_personaPanelOpen) closePersonaPanel();
    if (_toolsPanelOpen) closeToolsPanel();
    if (_schedulerPanelOpen) closeSchedulerPanel();
    if (_gestureHelpPanelOpen) closeGestureHelpPanel();
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

    const fileCard = document.createElement("section");
    fileCard.className = "memory-import-card";
    const fileTitle = document.createElement("div");
    fileTitle.className = "memory-import-title";
    fileTitle.innerHTML = '<span class="material-symbols-outlined">upload_file</span><strong>JSON / TXT</strong>';
    const fileHelp = document.createElement("p");
    fileHelp.className = "memory-import-help";
    fileHelp.textContent = "可一次选择多个文件。会优先识别 JSON 里的 char1–char6、每条记录的角色字段，或 char1.txt 这样的文件名。";

    const fallbackLabel = document.createElement("label");
    fallbackLabel.textContent = "认不出角色时放进";
    const fallbackSelect = document.createElement("select");
    const autoOption = document.createElement("option");
    autoOption.value = "";
    autoOption.textContent = "自动识别（不乱放）";
    fallbackSelect.appendChild(autoOption);
    Object.keys(histories).forEach(characterId => {
      const option = document.createElement("option");
      option.value = characterId;
      option.textContent = `${nickName(characterId)}（${characterId}）`;
      fallbackSelect.appendChild(option);
    });
    fallbackLabel.appendChild(fallbackSelect);

    const fileLabel = document.createElement("label");
    fileLabel.textContent = "选择记忆文件";
    const fileInput = document.createElement("input");
    fileInput.type = "file";
    fileInput.accept = ".json,.txt,application/json,text/plain";
    fileInput.multiple = true;
    fileLabel.appendChild(fileInput);

    const fileStatus = document.createElement("div");
    fileStatus.className = "memory-import-status";
    const importFiles = document.createElement("button");
    importFiles.className = "memory-import-submit";
    importFiles.textContent = "导入记忆文件";
    importFiles.onclick = async () => {
      if (!fileInput.files.length) {
        fileStatus.className = "memory-import-status error";
        fileStatus.textContent = "先选择 JSON 或 TXT 文件喵";
        return;
      }
      importFiles.disabled = true;
      importFiles.textContent = "正在分角色整理…";
      fileStatus.className = "memory-import-status";
      fileStatus.textContent = "";
      const form = new FormData();
      [...fileInput.files].forEach(file => form.append("files", file));
      form.append("fallback_character", fallbackSelect.value);
      try {
        const response = await fetch("/api/memory/import-files", {
          method: "POST",
          body: form,
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "导入失败");
        fileStatus.className = "memory-import-status ok";
        const extras = [];
        if (data.unassigned) extras.push(`未分配 ${data.unassigned}`);
        if (data.invalid) extras.push(`未识别 ${data.invalid}`);
        if (data.errors) extras.push(`失败 ${data.errors}`);
        fileStatus.textContent = `迁入 ${data.imported}，重复跳过 ${data.skipped}${extras.length ? `，${extras.join("，")}` : ""}`;
        fileInput.value = "";
        importFiles.textContent = "导入完成";
        await loadMemoryView();
      } catch (error) {
        fileStatus.className = "memory-import-status error";
        fileStatus.textContent = error.message;
        importFiles.textContent = "重新导入";
      } finally {
        importFiles.disabled = false;
      }
    };
    fileCard.appendChild(fileTitle);
    fileCard.appendChild(fileHelp);
    fileCard.appendChild(fallbackLabel);
    fileCard.appendChild(fileLabel);
    fileCard.appendChild(importFiles);
    fileCard.appendChild(fileStatus);

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
    panel.appendChild(fileCard);
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

  // ── 手势说明 ──
  let _gestureHelpPanelOpen = false;

  function openGestureHelpPanel() {
    if (_voicePanelOpen) closeVoicePanel();
    if (_appearancePanelOpen) closeAppearancePanel();
    if (_personaPanelOpen) closePersonaPanel();
    if (_toolsPanelOpen) closeToolsPanel();
    if (_schedulerPanelOpen) closeSchedulerPanel();
    if (_memoryImportPanelOpen) closeMemoryImportPanel();
    const panel = document.getElementById("gestureHelpPanel");
    panel.innerHTML = `
      <button type="button" class="persona-close-btn" id="gestureHelpClose">收起</button>
      <section class="gesture-help-card" aria-labelledby="gestureHelpTitle">
        <div class="gesture-help-title" id="gestureHelpTitle">
          <span class="material-symbols-outlined">swipe</span><strong>藏起来的猫爪手势</strong>
        </div>
        <div class="gesture-help-list">
          <div class="gesture-help-item"><span class="material-symbols-outlined">swipe_right</span><div><strong>从屏幕左边缘向右滑</strong><small>单聊里返回角色列表。</small></div></div>
          <div class="gesture-help-item"><span class="material-symbols-outlined">location_on</span><div><strong>点击聊天中的角色头像</strong><small>查看祂此刻的欲望状态与当前位置。</small></div></div>
          <div class="gesture-help-item"><span class="material-symbols-outlined">person_remove</span><div><strong>长按单聊列表头像</strong><small>删除好友、恢复好友，或处理对方发来的好友申请。</small></div></div>
          <div class="gesture-help-item"><span class="material-symbols-outlined">touch_app</span><div><strong>长按单聊发送爪</strong><small>打开图片、表情包、转账和补记入口。</small></div></div>
          <div class="gesture-help-item"><span class="material-symbols-outlined">group</span><div><strong>长按群聊发送爪</strong><small>调整群聊里当前在线的角色。</small></div></div>
          <div class="gesture-help-item"><span class="material-symbols-outlined">chat</span><div><strong>长按单聊消息</strong><small>删除这一条及它之后的本轮消息。</small></div></div>
          <div class="gesture-help-item"><span class="material-symbols-outlined">format_quote</span><div><strong>长按群聊消息</strong><small>复制、引用，或删除这一条及后续群聊。</small></div></div>
          <div class="gesture-help-item"><span class="material-symbols-outlined">ink_highlighter</span><div><strong>在共读正文长按文字并拖动选择</strong><small>松手后可以划线、写页边批注，或喊共读角色回应；也支持跨段选择。</small></div></div>
          <div class="gesture-help-item"><span class="material-symbols-outlined">swipe_left</span><div><strong>在共读书架的文件行上向左滑</strong><small>露出右侧删除按钮，可以移除已上传的共读文件。</small></div></div>
          <div class="gesture-help-item"><span class="material-symbols-outlined">edit</span><div><strong>长按聊天标题或角色名</strong><small>给角色或群聊改一个昵称。</small></div></div>
        </div>
      </section>`;
    panel.style.display = "flex";
    panel.style.flexDirection = "column";
    panel.style.padding = "12px 16px 16px";
    panel.style.gap = "12px";
    panel.querySelector("#gestureHelpClose").onclick = closeGestureHelpPanel;
    _gestureHelpPanelOpen = true;
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function closeGestureHelpPanel() {
    const panel = document.getElementById("gestureHelpPanel");
    panel.style.display = "none";
    panel.innerHTML = "";
    _gestureHelpPanelOpen = false;
    document.getElementById("moreContent").scrollTop = 0;
  }

  let _voicePanelOpen = false;

  function voiceField(label, input) {
    const wrap = document.createElement("label");
    wrap.className = "voice-settings-field";
    const title = document.createElement("span");
    title.textContent = label;
    wrap.append(title, input);
    return wrap;
  }

  function voiceInput(id, type = "text") {
    const input = document.createElement("input");
    input.id = id;
    input.type = type;
    input.autocomplete = "off";
    return input;
  }

  function voiceProviderSelect(id) {
    const select = document.createElement("select");
    select.id = id;
    [
      ["openai_compatible", "OpenAI-compatible"],
      ["custom_http", "自定义 HTTP"],
    ].forEach(([value, label]) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      select.appendChild(option);
    });
    return select;
  }

  function voiceSwitch(label, id, checked = false) {
    const row = document.createElement("label");
    row.className = "voice-switch-row";
    const text = document.createElement("span");
    text.textContent = label;
    const input = document.createElement("input");
    input.type = "checkbox";
    input.id = id;
    input.checked = checked;
    row.append(text, input);
    return row;
  }

  async function saveVoiceSettings({ announce = true } = {}) {
    const panel = document.getElementById("voicePanel");
    const voices = {};
    panel.querySelectorAll("[data-voice-character]").forEach(input => {
      voices[input.dataset.voiceCharacter] = input.value.trim();
    });
    const payload = {
      enabled: document.getElementById("voiceEnabled").checked,
      tts: {
        provider: document.getElementById("voiceTtsProvider").value,
        endpoint: document.getElementById("voiceTtsEndpoint").value.trim(),
        token: document.getElementById("voiceTtsToken").value.trim(),
        clear_token: document.getElementById("voiceTtsClear").checked,
        model: document.getElementById("voiceTtsModel").value.trim(),
        response_format: document.getElementById("voiceTtsFormat").value,
        voices,
      },
      stt: {
        enabled: document.getElementById("voiceSttEnabled").checked,
        provider: document.getElementById("voiceSttProvider").value,
        endpoint: document.getElementById("voiceSttEndpoint").value.trim(),
        token: document.getElementById("voiceSttToken").value.trim(),
        clear_token: document.getElementById("voiceSttClear").checked,
        model: document.getElementById("voiceSttModel").value.trim(),
        reuse_tts_credentials: document.getElementById("voiceSttReuse").checked,
        max_upload_mb: Number(document.getElementById("voiceSttMaxMb").value),
      },
      limits: {
        max_chars: Number(document.getElementById("voiceMaxChars").value),
        daily_count: Number(document.getElementById("voiceDailyCount").value),
        cost_per_1k_chars_usd: Number(document.getElementById("voiceRate").value),
        daily_cost_usd: Number(document.getElementById("voiceDailyCost").value),
      },
    };
    const response = await fetch("/api/voice/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "语音配置没有保存成功");
    voiceConfigState = data.config;
    document.getElementById("voiceTtsToken").value = "";
    document.getElementById("voiceSttToken").value = "";
    document.getElementById("voiceTtsClear").checked = false;
    document.getElementById("voiceSttClear").checked = false;
    document.getElementById("voiceTtsToken").placeholder = data.config.tts.token_configured
      ? "已保存在后端，留空不改" : "可留空（自托管服务可不需要）";
    document.getElementById("voiceSttToken").placeholder = data.config.stt.token_configured
      ? "已保存在后端，留空不改" : "可留空";
    await loadVoiceFeatureState();
    if (announce) showToast("语音收发配置保存好啦");
    return data.config;
  }

  async function openVoicePanel() {
    if (_appearancePanelOpen) closeAppearancePanel();
    if (_personaPanelOpen) closePersonaPanel();
    if (_toolsPanelOpen) closeToolsPanel();
    if (_schedulerPanelOpen) closeSchedulerPanel();
    if (_memoryImportPanelOpen) closeMemoryImportPanel();
    if (_gestureHelpPanelOpen) closeGestureHelpPanel();

    const panel = document.getElementById("voicePanel");
    panel.innerHTML = "";
    panel.style.display = "flex";
    panel.style.flexDirection = "column";
    panel.style.padding = "12px 16px 16px";
    panel.style.gap = "12px";
    const close = document.createElement("button");
    close.type = "button";
    close.className = "persona-close-btn";
    close.textContent = "收起 ×";
    close.onclick = closeVoicePanel;
    panel.appendChild(close);

    let config;
    try {
      config = await loadVoiceFeatureState();
      if (!config) throw new Error("load failed");
    } catch (_) {
      panel.insertAdjacentHTML("beforeend", '<p class="voice-settings-note">语音配置暂时没有加载出来，请收起后重试。</p>');
      _voicePanelOpen = true;
      return;
    }

    const intro = document.createElement("section");
    intro.className = "voice-settings-card";
    intro.innerHTML = `
      <h3>说说喵°语音收发</h3>
      <p class="voice-settings-note">默认关闭、随时可拔。发出的声音会明确标记为「AI 语音」；Token 只保存在后端，浏览器只能知道“是否已配置”，看不到原文。</p>`;
    intro.appendChild(voiceSwitch("语音总开关", "voiceEnabled", config.enabled));
    panel.appendChild(intro);

    const ttsCard = document.createElement("section");
    ttsCard.className = "voice-settings-card";
    const ttsTitle = document.createElement("h3");
    ttsTitle.textContent = "发语音 · TTS";
    const ttsNote = document.createElement("p");
    ttsNote.className = "voice-settings-note";
    ttsNote.textContent = "OpenAI-compatible 使用 model / input / voice；自定义 HTTP 使用 model / text / voice_id，并接收原始音频或 audio_base64。";
    const ttsGrid = document.createElement("div");
    ttsGrid.className = "voice-settings-grid";
    const ttsProvider = voiceProviderSelect("voiceTtsProvider");
    ttsProvider.value = config.tts.provider;
    const ttsEndpoint = voiceInput("voiceTtsEndpoint", "url");
    ttsEndpoint.value = config.tts.endpoint;
    const ttsModel = voiceInput("voiceTtsModel");
    ttsModel.value = config.tts.model;
    const ttsToken = voiceInput("voiceTtsToken", "password");
    ttsToken.placeholder = config.tts.token_configured
      ? "已保存在后端，留空不改" : "可留空（自托管服务可不需要）";
    ttsToken.autocomplete = "new-password";
    const ttsFormat = document.createElement("select");
    ttsFormat.id = "voiceTtsFormat";
    ["mp3", "opus", "aac", "flac", "wav"].forEach(value => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value.toUpperCase();
      ttsFormat.appendChild(option);
    });
    ttsFormat.value = config.tts.response_format;
    ttsGrid.append(
      voiceField("接口类型", ttsProvider),
      voiceField("模型", ttsModel),
      voiceField("接口地址", ttsEndpoint),
      voiceField("Token（不会回显）", ttsToken),
      voiceField("音频格式", ttsFormat),
    );
    const ttsClear = voiceSwitch("清除后端已保存的 TTS Token", "voiceTtsClear", false);
    const voiceIds = document.createElement("div");
    voiceIds.className = "voice-character-voices";
    config.characters.forEach(character => {
      const input = voiceInput(`voiceId-${character.id}`);
      input.dataset.voiceCharacter = character.id;
      input.value = config.tts.voices[character.id] || "";
      input.placeholder = "例如 alloy / 自定义 voice_id";
      voiceIds.appendChild(voiceField(`${character.name} · voice_id`, input));
    });
    ttsCard.append(ttsTitle, ttsNote, ttsGrid, ttsClear, voiceIds);

    const preview = document.createElement("div");
    preview.className = "voice-preview-row";
    const previewBox = document.createElement("div");
    previewBox.className = "voice-settings-field";
    const previewChar = document.createElement("select");
    previewChar.id = "voicePreviewCharacter";
    config.characters.forEach(character => {
      const option = document.createElement("option");
      option.value = character.id;
      option.textContent = character.name;
      previewChar.appendChild(option);
    });
    const previewText = document.createElement("textarea");
    previewText.id = "voicePreviewText";
    previewText.maxLength = Number(config.limits.max_chars || 180);
    previewText.value = "你好呀，这是一条AI语音试听。";
    previewBox.append(voiceField("试听角色", previewChar), voiceField("试听文字（试听也计入今日次数）", previewText));
    const previewBtn = document.createElement("button");
    previewBtn.type = "button";
    previewBtn.className = "voice-secondary-btn";
    previewBtn.textContent = "试听";
    previewBtn.onclick = async () => {
      previewBtn.disabled = true;
      previewBtn.textContent = "生成中…";
      try {
        await saveVoiceSettings({ announce: false });
        const response = await fetch("/api/voice/preview", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            character_id: previewChar.value,
            text: previewText.value.trim(),
          }),
        });
        if (!response.ok) {
          const error = await response.json();
          throw new Error(error.error || "试听失败");
        }
        const url = URL.createObjectURL(await response.blob());
        const audio = new Audio(url);
        audio.addEventListener("ended", () => URL.revokeObjectURL(url), { once: true });
        await audio.play();
        showToast("正在播放AI语音试听");
      } catch (error) {
        showToast(error.message || "试听失败");
      } finally {
        previewBtn.disabled = false;
        previewBtn.textContent = "试听";
      }
    };
    preview.append(previewBox, previewBtn);
    ttsCard.appendChild(preview);
    panel.appendChild(ttsCard);

    const sttCard = document.createElement("section");
    sttCard.className = "voice-settings-card";
    const sttTitle = document.createElement("h3");
    sttTitle.textContent = "收语音 · STT";
    const sttNote = document.createElement("p");
    sttNote.className = "voice-settings-note";
    sttNote.textContent = "iPhone 录音先上传到后端转成文字，再把文字作为普通消息交给角色；录音不会塞进模型上下文。";
    const sttEnabled = voiceSwitch("开启录音转文字", "voiceSttEnabled", config.stt.enabled);
    const sttReuse = voiceSwitch("复用 TTS 地址凭证中的 Token", "voiceSttReuse", config.stt.reuse_tts_credentials);
    const sttGrid = document.createElement("div");
    sttGrid.className = "voice-settings-grid";
    const sttProvider = voiceProviderSelect("voiceSttProvider");
    sttProvider.value = config.stt.provider;
    const sttEndpoint = voiceInput("voiceSttEndpoint", "url");
    sttEndpoint.value = config.stt.endpoint;
    const sttModel = voiceInput("voiceSttModel");
    sttModel.value = config.stt.model;
    const sttToken = voiceInput("voiceSttToken", "password");
    sttToken.autocomplete = "new-password";
    sttToken.placeholder = config.stt.token_configured ? "已保存在后端，留空不改" : "可留空";
    const sttMax = voiceInput("voiceSttMaxMb", "number");
    sttMax.min = "1";
    sttMax.max = "20";
    sttMax.value = config.stt.max_upload_mb;
    sttGrid.append(
      voiceField("接口类型", sttProvider),
      voiceField("模型", sttModel),
      voiceField("接口地址", sttEndpoint),
      voiceField("独立 Token（不会回显）", sttToken),
      voiceField("单段录音上限（MB）", sttMax),
    );
    const sttClear = voiceSwitch("清除后端已保存的独立 STT Token", "voiceSttClear", false);
    sttCard.append(sttTitle, sttNote, sttEnabled, sttReuse, sttGrid, sttClear);
    panel.appendChild(sttCard);

    const limitsCard = document.createElement("section");
    limitsCard.className = "voice-settings-card";
    const limitsTitle = document.createElement("h3");
    limitsTitle.textContent = "话唠保险丝";
    const limitsNote = document.createElement("p");
    limitsNote.className = "voice-settings-note";
    limitsNote.textContent = "费用按你填写的“每千字单价”估算；填 0 表示不估价，次数和单条字数仍会硬限制。";
    const limitsGrid = document.createElement("div");
    limitsGrid.className = "voice-settings-grid";
    const maxChars = voiceInput("voiceMaxChars", "number");
    maxChars.min = "20"; maxChars.max = "4000"; maxChars.value = config.limits.max_chars;
    const dailyCount = voiceInput("voiceDailyCount", "number");
    dailyCount.min = "1"; dailyCount.max = "1000"; dailyCount.value = config.limits.daily_count;
    const rate = voiceInput("voiceRate", "number");
    rate.min = "0"; rate.max = "100"; rate.step = "0.000001"; rate.value = config.limits.cost_per_1k_chars_usd;
    const dailyCost = voiceInput("voiceDailyCost", "number");
    dailyCost.min = "0"; dailyCost.max = "1000"; dailyCost.step = "0.01"; dailyCost.value = config.limits.daily_cost_usd;
    limitsGrid.append(
      voiceField("单条最多字数", maxChars),
      voiceField("每日生成/试听次数", dailyCount),
      voiceField("每千字费用（USD）", rate),
      voiceField("每日费用上限（USD）", dailyCost),
    );
    const usage = document.createElement("div");
    usage.className = "voice-usage-line";
    usage.textContent = `今天已生成 ${config.usage_today.count} 次 · ${config.usage_today.characters} 字 · 估算 $${Number(config.usage_today.estimated_cost_usd || 0).toFixed(4)}`;
    limitsCard.append(limitsTitle, limitsNote, limitsGrid, usage);
    panel.appendChild(limitsCard);

    const actions = document.createElement("div");
    actions.className = "voice-settings-actions";
    const save = document.createElement("button");
    save.type = "button";
    save.className = "voice-primary-btn";
    save.textContent = "保存语音配置 🐾";
    save.onclick = async () => {
      save.disabled = true;
      save.textContent = "保存中…";
      try {
        await saveVoiceSettings();
        save.textContent = "已保存 ✓";
      } catch (error) {
        showToast(error.message || "保存失败");
        save.textContent = "保存语音配置 🐾";
      } finally {
        save.disabled = false;
        setTimeout(() => { save.textContent = "保存语音配置 🐾"; }, 1200);
      }
    };
    actions.appendChild(save);
    panel.appendChild(actions);
    _voicePanelOpen = true;
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function closeVoicePanel() {
    const panel = document.getElementById("voicePanel");
    panel.style.display = "none";
    panel.innerHTML = "";
    _voicePanelOpen = false;
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
    } else if (action === "gesture-help") {
      if (_gestureHelpPanelOpen) closeGestureHelpPanel();
      else openGestureHelpPanel();
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
  const AUTHOR_NAMES = GROUP_CHAR_NAMES;

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
  let musicRoom = null;
  let musicLibrary = [];
  let musicQueue = [];
  let musicSearchItems = [];
  let musicOnlineSearchItems = [];
  let musicOnlineSearchBusy = false;
  let musicNeteaseStatus = null;
  let musicNeteasePlaylists = [];
  let musicNeteasePlaylistsLoaded = false;
  let musicNeteasePlaylistsBusy = false;
  let activeNeteasePlaylistId = "";
  let activeMusicTrack = null;
  let musicObjectUrl = "";
  let musicArtworkObjectUrl = "";
  let musicArtworkTrackKey = "";
  let musicArtworkRenderToken = 0;
  let musicLyricTrackKey = "";
  let musicLyricLines = [];
  let musicLyricIndex = null;
  let musicLibraryArtworkUrls = [];
  let musicDatabasePromise = null;
  let musicLibrarySyncPromise = null;
  let musicLibraryServerAvailable = true;
  let musicLibraryRefreshedAt = 0;
  const MUSIC_LIBRARY_RESET_KEY = "becoming-music-library-reset-20260718";
  const MUSIC_PLAYBACK_MODE_KEY = "becoming-music-playback-mode";
  const MUSIC_PLAYBACK_MODES = ["sequence", "shuffle", "repeat_one"];
  const MUSIC_PLAYBACK_MODE_META = {
    sequence: { label: "顺序播放", icon: "format_list_numbered" },
    shuffle: { label: "随机播放", icon: "shuffle" },
    repeat_one: { label: "单曲循环", icon: "repeat_one" },
  };
  let musicPlaybackMode = (() => {
    try {
      const saved = localStorage.getItem(MUSIC_PLAYBACK_MODE_KEY);
      return MUSIC_PLAYBACK_MODES.includes(saved) ? saved : "sequence";
    } catch (_) {
      return "sequence";
    }
  })();
  let musicLocalReady = false;
  let musicLoadingTrack = false;
  let musicRoomPollTimer = null;
  let musicRoomSyncTimer = null;
  const handledMusicCommands = new Set();
  let openSwipeRevealRow = null;

  function findMusicTrack(trackId) {
    return musicOnlineSearchItems.find(item => item.id === trackId)
      || (activeMusicTrack?.id === trackId ? activeMusicTrack : null);
  }

  function escapeReadingHtml(value) {
    const div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function setSwipeRevealOpen(row, open) {
    if (!row) return;
    row.classList.toggle("swipe-open", open);
    const deleteButton = row.querySelector(".swipe-delete-action");
    if (deleteButton) deleteButton.tabIndex = open ? 0 : -1;
    if (open) {
      if (openSwipeRevealRow && openSwipeRevealRow !== row) setSwipeRevealOpen(openSwipeRevealRow, false);
      openSwipeRevealRow = row;
    } else if (openSwipeRevealRow === row) {
      openSwipeRevealRow = null;
    }
  }

  function bindSwipeReveal(row, foregroundSelector, deleteSelector) {
    const foreground = row.querySelector(foregroundSelector);
    const deleteButton = row.querySelector(deleteSelector);
    if (!foreground || !deleteButton) return;
    row.classList.add("swipe-reveal-row");
    foreground.classList.add("swipe-reveal-foreground");
    deleteButton.classList.add("swipe-delete-action");
    deleteButton.tabIndex = -1;

    let tracking = false;
    let horizontal = false;
    let startX = 0;
    let startY = 0;
    let currentOffset = 0;
    const revealWidth = 72;

    row.addEventListener("pointerdown", event => {
      if (event.target.closest(deleteSelector)) return;
      if (event.pointerType === "mouse" && event.button !== 0) return;
      tracking = true;
      horizontal = false;
      startX = event.clientX;
      startY = event.clientY;
      currentOffset = row.classList.contains("swipe-open") ? -revealWidth : 0;
    });

    row.addEventListener("pointermove", event => {
      if (!tracking) return;
      const deltaX = event.clientX - startX;
      const deltaY = event.clientY - startY;
      if (!horizontal) {
        if (Math.abs(deltaY) > Math.abs(deltaX) && Math.abs(deltaY) > 7) {
          tracking = false;
          return;
        }
        if (Math.abs(deltaX) < 7) return;
        horizontal = true;
        row.classList.add("swiping");
        try { row.setPointerCapture(event.pointerId); } catch (_) {}
      }
      event.preventDefault();
      const base = row.classList.contains("swipe-open") ? -revealWidth : 0;
      currentOffset = Math.max(-revealWidth, Math.min(0, base + deltaX));
      foreground.style.transform = `translate3d(${currentOffset}px, 0, 0)`;
    });

    const finish = event => {
      if (!tracking && !horizontal) return;
      const moved = horizontal;
      tracking = false;
      horizontal = false;
      row.classList.remove("swiping");
      foreground.style.removeProperty("transform");
      setSwipeRevealOpen(row, currentOffset < -revealWidth / 2);
      try { row.releasePointerCapture(event.pointerId); } catch (_) {}
      if (moved) {
        row.dataset.suppressSwipeClick = "true";
        setTimeout(() => { delete row.dataset.suppressSwipeClick; }, 220);
      }
    };
    row.addEventListener("pointerup", finish);
    row.addEventListener("pointercancel", finish);
    row.addEventListener("click", event => {
      if (row.dataset.suppressSwipeClick === "true") {
        event.preventDefault();
        event.stopPropagation();
        return;
      }
      if (row.classList.contains("swipe-open") && !event.target.closest(deleteSelector)) {
        event.preventDefault();
        event.stopPropagation();
        setSwipeRevealOpen(row, false);
      }
    }, true);
    deleteButton.addEventListener("focus", () => setSwipeRevealOpen(row, true));
  }

  document.addEventListener("pointerdown", event => {
    if (openSwipeRevealRow && !openSwipeRevealRow.contains(event.target)) {
      setSwipeRevealOpen(openSwipeRevealRow, false);
    }
  }, true);

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
    document.getElementById("musicPane").classList.toggle("hidden", pane !== "music");
    syncMomentsFab();
    if (pane === "moments") loadMoments();
    else if (pane === "reading") loadReadingBooks();
    else loadMusicRoom();
  }

  document.querySelectorAll(".nest-tab").forEach(btn => {
    btn.addEventListener("click", () => setNestPane(btn.dataset.nestPane));
  });

  // ── 一起听 · 本地音乐 ──────────────────────────────────
  async function musicRequest(url, options = {}) {
    const requestOptions = { ...options };
    const method = String(requestOptions.method || "GET").toUpperCase();
    if (method === "GET" && !requestOptions.cache) requestOptions.cache = "no-store";
    const response = await fetch(url, requestOptions);
    let data = {};
    try { data = await response.json(); } catch (_) {}
    if (!response.ok) throw new Error(data.error || "一起听房间这次没接好");
    return data;
  }

  function formatMusicTime(seconds) {
    const numeric = Number(seconds);
    const safe = Number.isFinite(numeric) ? Math.max(0, Math.floor(numeric)) : 0;
    return `${Math.floor(safe / 60)}:${String(safe % 60).padStart(2, "0")}`;
  }

  function localMusicDuration(audio, track = activeMusicTrack) {
    const direct = Number(audio?.duration);
    if (Number.isFinite(direct) && direct >= 0) return direct;
    if (audio?.seekable?.length) {
      const seekableEnd = Number(audio.seekable.end(audio.seekable.length - 1));
      if (Number.isFinite(seekableEnd) && seekableEnd >= 0) return seekableEnd;
    }
    const saved = Number(track?.duration);
    return Number.isFinite(saved) && saved >= 0 ? saved : 0;
  }

  function parseTimedMusicLyrics(rawLyrics) {
    const source = String(rawLyrics || "");
    const offsetMatch = source.match(/\[offset:([+-]?\d+)\]/i);
    const offsetSeconds = offsetMatch ? Number(offsetMatch[1]) / 1000 : 0;
    const timeline = [];
    for (const rawLine of source.split(/\r?\n/)) {
      const timestamps = [...rawLine.matchAll(/\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]/g)];
      if (!timestamps.length) continue;
      const text = rawLine.replace(/\[[^\]]+\]/g, "").trim();
      if (!text) continue;
      for (const match of timestamps) {
        const fraction = match[3] ? Number(`0.${match[3]}`) : 0;
        timeline.push({
          time: Math.max(
            0,
            Number(match[1]) * 60 + Number(match[2]) + fraction + offsetSeconds,
          ),
          text,
        });
      }
    }
    timeline.sort((left, right) => left.time - right.time);
    return timeline.filter((line, index) => (
      !index
      || line.time !== timeline[index - 1].time
      || line.text !== timeline[index - 1].text
    ));
  }

  async function hydrateMusicTrackLyrics(track) {
    if (!track || track.lyrics || !track.has_lyrics || !track.lyrics_url) return track;
    try {
      const data = await musicRequest(track.lyrics_url);
      return {
        ...track,
        lyrics: data.lyrics || data.translated_lyrics || "",
        translated_lyrics: data.translated_lyrics || "",
      };
    } catch (_) {
      return track;
    }
  }

  function prepareMusicLyrics(track) {
    const key = track ? `${track.id || track.name}:${String(track.lyrics || "").length}` : "";
    if (key === musicLyricTrackKey) return;
    musicLyricTrackKey = key;
    musicLyricLines = parseTimedMusicLyrics(track?.lyrics);
    musicLyricIndex = null;
    renderMusicLyrics(0);
  }

  function renderMusicLyrics(positionSeconds) {
    const wrap = document.getElementById("musicLyrics");
    const current = document.getElementById("musicLyricCurrent");
    const next = document.getElementById("musicLyricNext");
    if (!musicLyricLines.length) {
      wrap.classList.add("hidden");
      current.textContent = "";
      next.textContent = "";
      return;
    }
    let low = 0;
    let high = musicLyricLines.length - 1;
    let index = -1;
    while (low <= high) {
      const middle = Math.floor((low + high) / 2);
      if (musicLyricLines[middle].time <= positionSeconds + 0.08) {
        index = middle;
        low = middle + 1;
      } else {
        high = middle - 1;
      }
    }
    const shownIndex = Math.max(0, index);
    if (shownIndex === musicLyricIndex && !wrap.classList.contains("hidden")) return;
    musicLyricIndex = shownIndex;
    current.textContent = musicLyricLines[shownIndex]?.text || "";
    next.textContent = musicLyricLines[shownIndex + 1]?.text || "";
    wrap.classList.toggle("is-upcoming", index < 0);
    wrap.classList.remove("is-changing");
    void wrap.offsetWidth;
    wrap.classList.add("is-changing");
    wrap.classList.remove("hidden");
  }

  function openMusicDatabase() {
    if (musicDatabasePromise) return musicDatabasePromise;
    musicDatabasePromise = new Promise((resolve, reject) => {
      const request = indexedDB.open("becoming-local-music", 1);
      request.onupgradeneeded = () => {
        const database = request.result;
        if (!database.objectStoreNames.contains("tracks")) {
          database.createObjectStore("tracks", { keyPath: "id" });
        }
        if (!database.objectStoreNames.contains("audio")) {
          database.createObjectStore("audio", { keyPath: "id" });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error("本机曲库没有打开"));
    });
    return musicDatabasePromise;
  }

  async function listLocalMusicTracks() {
    const database = await openMusicDatabase();
    return new Promise((resolve, reject) => {
      const request = database.transaction("tracks").objectStore("tracks").getAll();
      request.onsuccess = () => resolve((request.result || []).sort((a, b) => a.added_at - b.added_at));
      request.onerror = () => reject(request.error || new Error("本机曲库没有读出来"));
    });
  }

  async function getLocalMusicBlob(trackId) {
    const database = await openMusicDatabase();
    return new Promise((resolve, reject) => {
      const request = database.transaction("audio").objectStore("audio").get(trackId);
      request.onsuccess = () => resolve(request.result?.blob || null);
      request.onerror = () => reject(request.error || new Error("这首歌没有读出来"));
    });
  }

  async function saveLocalMusicTrack(track, blob) {
    const database = await openMusicDatabase();
    return new Promise((resolve, reject) => {
      const transaction = database.transaction(["tracks", "audio"], "readwrite");
      transaction.objectStore("tracks").put(track);
      transaction.objectStore("audio").put({ id: track.id, blob });
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error || new Error("这首歌没有存下来"));
      transaction.onabort = () => reject(transaction.error || new Error("设备空间不够了"));
    });
  }

  async function updateLocalMusicTrack(track) {
    const database = await openMusicDatabase();
    return new Promise((resolve, reject) => {
      const transaction = database.transaction("tracks", "readwrite");
      transaction.objectStore("tracks").put(track);
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error || new Error("歌曲资料没有存下来"));
    });
  }

  async function removeLocalMusicTrack(trackId) {
    const database = await openMusicDatabase();
    return new Promise((resolve, reject) => {
      const transaction = database.transaction(["tracks", "audio"], "readwrite");
      transaction.objectStore("tracks").delete(trackId);
      transaction.objectStore("audio").delete(trackId);
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error || new Error("这首歌没有删掉"));
    });
  }

  async function resetLocalMusicLibraryOnce() {
    if (localStorage.getItem(MUSIC_LIBRARY_RESET_KEY) === "done") return 0;
    const existingTracks = await listLocalMusicTracks();
    const database = await openMusicDatabase();
    await new Promise((resolve, reject) => {
      const transaction = database.transaction(["tracks", "audio"], "readwrite");
      transaction.objectStore("tracks").clear();
      transaction.objectStore("audio").clear();
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error || new Error("旧曲库没有清干净"));
      transaction.onabort = () => reject(transaction.error || new Error("旧曲库没有清干净"));
    });
    localStorage.setItem(MUSIC_LIBRARY_RESET_KEY, "done");
    return existingTracks.length;
  }

  async function fetchSyncedMusicTracks() {
    const data = await musicRequest(`/api/music/library?refresh=${Date.now()}`);
    return data.tracks || [];
  }

  async function uploadMusicTrack(track, blob) {
    const form = new FormData();
    form.append("track_id", track.id);
    form.append("name", track.name || "未命名歌曲");
    form.append("artist", track.artist || "本地音乐");
    form.append("album", track.album || "");
    form.append("duration", String(track.duration || 0));
    form.append("lyrics", String(track.lyrics || ""));
    const extensionByType = {
      "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/x-m4a": "m4a",
      "audio/aac": "aac", "audio/wav": "wav", "audio/x-wav": "wav",
      "audio/flac": "flac", "audio/ogg": "ogg", "audio/opus": "opus",
    };
    const fallbackExtension = extensionByType[blob.type || track.type] || "mp3";
    form.append("audio", blob, blob.name || track.filename || `${track.name || "music"}.${fallbackExtension}`);
    if (track.artwork instanceof Blob) {
      const artworkType = track.artwork.type || "image/jpeg";
      const artworkExtension = artworkType === "image/png" ? "png"
        : artworkType === "image/webp" ? "webp"
          : ["image/tiff", "image/x-tiff"].includes(artworkType) ? "tiff" : "jpg";
      form.append("artwork", track.artwork, `cover.${artworkExtension}`);
    }
    const data = await musicRequest("/api/music/library", { method: "POST", body: form });
    return data.track;
  }

  async function deleteSyncedMusicTrack(trackId) {
    await musicRequest(`/api/music/library/${encodeURIComponent(trackId)}`, { method: "DELETE" });
  }

  async function syncLocalMusicTracks(localTracks, serverTracks) {
    const serverById = new Map(serverTracks.map(track => [track.id, track]));
    const serverIds = new Set(serverTracks.map(track => track.id));
    const pending = localTracks.filter(track => {
      const synced = serverById.get(track.id);
      return !synced
        || (track.artwork instanceof Blob && !synced.artwork_url)
        || (Boolean(track.lyrics) && !synced.has_lyrics);
    });
    if (!pending.length) return { uploadedTracks: [], failures: [] };
    const hint = document.getElementById("musicConnectHint");
    const uploadedTracks = [];
    const failures = [];
    for (let index = 0; index < pending.length; index += 1) {
      hint.textContent = `正在同步 ${index + 1}/${pending.length}`;
      try {
        const blob = await getLocalMusicBlob(pending[index].id);
        if (!blob) throw new Error("本机音频文件不见了");
        const uploaded = await uploadMusicTrack(pending[index], blob);
        uploadedTracks.push(uploaded);
        serverIds.add(uploaded.id);
        await updateLocalMusicTrack({
          ...pending[index],
          audio_url: uploaded.audio_url || "",
          artwork_url: uploaded.artwork_url || "",
          lyrics: uploaded.lyrics || pending[index].lyrics || "",
          has_lyrics: Boolean(uploaded.has_lyrics || pending[index].lyrics),
          synced: true,
        });
      } catch (error) {
        failures.push({ track: pending[index], error });
        console.warn("music library sync failed", error);
      }
    }
    return { uploadedTracks, failures };
  }

  function localMusicTrackId(file) {
    const seed = `${file.name}\u0000${file.size}\u0000${file.lastModified}`;
    let hash = 2166136261;
    for (let index = 0; index < seed.length; index += 1) {
      hash ^= seed.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }
    return `local:${(hash >>> 0).toString(16)}:${file.size}`;
  }

  function localMusicDisplayName(filename) {
    return String(filename || "未命名歌曲").replace(/\.[^.]+$/, "").trim() || "未命名歌曲";
  }

  function localMusicImportKey(filename) {
    return localMusicDisplayName(filename)
      .replace(/(?:[._ -]cover|[._ -]artwork|封面)$/i, "")
      .trim()
      .toLocaleLowerCase();
  }

  function isMusicArtworkFile(file) {
    return String(file?.type || "").startsWith("image/")
      || /\.(?:png|jpe?g|webp)$/i.test(String(file?.name || ""));
  }

  function isMusicLyricsFile(file) {
    return /\.lrc$/i.test(String(file?.name || ""));
  }

  async function readMusicLyricsFile(file) {
    const bytes = new Uint8Array(await file.arrayBuffer());
    const encodings = ["utf-8", "gb18030", "utf-16le", "utf-16be"];
    for (const encoding of encodings) {
      try {
        const text = new TextDecoder(encoding).decode(bytes).replace(/^\uFEFF/, "").trim();
        if (text && !text.includes("�")) return text.slice(0, 50000);
      } catch (_) {}
    }
    return new TextDecoder("utf-8").decode(bytes).replace(/^\uFEFF/, "").trim().slice(0, 50000);
  }

  function localMusicFilenameMetadata(filename) {
    const stem = localMusicDisplayName(filename);
    const separators = [" - ", " – ", " — ", "-", "–", "—"];
    for (const separator of separators) {
      const at = stem.lastIndexOf(separator);
      if (at <= 0 || at >= stem.length - separator.length) continue;
      const artist = stem.slice(0, at).trim();
      const title = stem.slice(at + separator.length).trim();
      if (artist && title) return { artist, title };
    }
    return { artist: "", title: stem };
  }

  function musicBytesToText(bytes, encoding = 3) {
    if (!bytes?.length) return "";
    let label = "utf-8";
    let content = bytes;
    if (encoding === 0) label = "windows-1252";
    else if (encoding === 1) {
      if (bytes[0] === 0xfe && bytes[1] === 0xff) {
        label = "utf-16be";
        content = bytes.subarray(2);
      } else {
        label = "utf-16le";
        content = bytes[0] === 0xff && bytes[1] === 0xfe ? bytes.subarray(2) : bytes;
      }
    } else if (encoding === 2) label = "utf-16be";
    try {
      return new TextDecoder(label).decode(content).replace(/\0/g, "").trim();
    } catch (_) {
      return new TextDecoder("utf-8").decode(content).replace(/\0/g, "").trim();
    }
  }

  function musicImageMime(bytes, fallback = "image/jpeg") {
    if (bytes?.length >= 8 && bytes[0] === 0x89 && bytes[1] === 0x50 && bytes[2] === 0x4e && bytes[3] === 0x47) return "image/png";
    if (bytes?.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) return "image/jpeg";
    if (bytes?.length >= 4 && (
      (bytes[0] === 0x49 && bytes[1] === 0x49 && bytes[2] === 0x2a && bytes[3] === 0x00)
      || (bytes[0] === 0x4d && bytes[1] === 0x4d && bytes[2] === 0x00 && bytes[3] === 0x2a)
    )) return "image/tiff";
    if (bytes?.length >= 6 && String.fromCharCode(...bytes.subarray(0, 6)).startsWith("GIF8")) return "image/gif";
    if (bytes?.length >= 12 && String.fromCharCode(...bytes.subarray(8, 12)) === "WEBP") return "image/webp";
    return fallback && fallback.startsWith("image/") ? fallback : "image/jpeg";
  }

  function musicReadUint32(bytes, offset, littleEndian = false) {
    if (offset < 0 || offset + 4 > bytes.length) return 0;
    return new DataView(bytes.buffer, bytes.byteOffset + offset, 4).getUint32(0, littleEndian);
  }

  function musicReadSynchsafe(bytes, offset) {
    return ((bytes[offset] & 0x7f) << 21) | ((bytes[offset + 1] & 0x7f) << 14) |
      ((bytes[offset + 2] & 0x7f) << 7) | (bytes[offset + 3] & 0x7f);
  }

  function musicFindTerminator(bytes, start, wide = false) {
    if (wide) {
      for (let index = start; index + 1 < bytes.length; index += 2) {
        if (bytes[index] === 0 && bytes[index + 1] === 0) return index;
      }
    } else {
      const index = bytes.indexOf(0, start);
      if (index >= 0) return index;
    }
    return bytes.length;
  }

  function musicFindSequence(bytes, sequence) {
    for (let index = 0; index <= bytes.length - sequence.length; index += 1) {
      let match = true;
      for (let offset = 0; offset < sequence.length; offset += 1) {
        if (bytes[index + offset] !== sequence[offset]) { match = false; break; }
      }
      if (match) return index;
    }
    return -1;
  }

  function parseId3Picture(payload, legacy = false) {
    if (!payload?.length) return null;
    const encoding = payload[0];
    let cursor = 1;
    let mime = "image/jpeg";
    if (legacy) {
      const format = musicBytesToText(payload.subarray(cursor, cursor + 3), 0).toLowerCase();
      mime = format === "png" ? "image/png" : "image/jpeg";
      cursor += 4;
    } else {
      const mimeEnd = musicFindTerminator(payload, cursor);
      mime = musicBytesToText(payload.subarray(cursor, mimeEnd), 0) || mime;
      cursor = mimeEnd + 2;
    }
    const descriptionEnd = musicFindTerminator(payload, cursor, encoding === 1 || encoding === 2);
    cursor = descriptionEnd + (encoding === 1 || encoding === 2 ? 2 : 1);
    if (cursor >= payload.length) return null;
    const image = payload.subarray(cursor);
    return new Blob([image], { type: musicImageMime(image, mime) });
  }

  function parseId3Lyrics(payload) {
    if (!payload?.length || payload.length < 5) return "";
    const encoding = payload[0];
    const wide = encoding === 1 || encoding === 2;
    const descriptionStart = 4;
    const descriptionEnd = musicFindTerminator(payload, descriptionStart, wide);
    const lyricStart = Math.min(payload.length, descriptionEnd + (wide ? 2 : 1));
    return musicBytesToText(payload.subarray(lyricStart), encoding).slice(0, 50000);
  }

  function parseId3Metadata(bytes, start = 0) {
    const result = {};
    if (start + 10 > bytes.length || musicBytesToText(bytes.subarray(start, start + 3)) !== "ID3") return result;
    const version = bytes[start + 3];
    const tagEnd = Math.min(bytes.length, start + 10 + musicReadSynchsafe(bytes, start + 6));
    let cursor = start + 10;
    while (cursor + (version === 2 ? 6 : 10) <= tagEnd) {
      const idLength = version === 2 ? 3 : 4;
      const frameId = musicBytesToText(bytes.subarray(cursor, cursor + idLength), 0);
      if (!frameId || /^\x00+$/.test(frameId)) break;
      const size = version === 2
        ? (bytes[cursor + 3] << 16) | (bytes[cursor + 4] << 8) | bytes[cursor + 5]
        : version === 4
          ? musicReadSynchsafe(bytes, cursor + 4)
          : musicReadUint32(bytes, cursor + 4);
      const headerSize = version === 2 ? 6 : 10;
      const payloadStart = cursor + headerSize;
      const payloadEnd = Math.min(tagEnd, payloadStart + size);
      if (size <= 0 || payloadEnd <= payloadStart) break;
      const payload = bytes.subarray(payloadStart, payloadEnd);
      if (["TIT2", "TT2"].includes(frameId)) result.title = musicBytesToText(payload.subarray(1), payload[0]);
      else if (["TPE1", "TP1"].includes(frameId)) result.artist = musicBytesToText(payload.subarray(1), payload[0]);
      else if (["TALB", "TAL"].includes(frameId)) result.album = musicBytesToText(payload.subarray(1), payload[0]);
      else if (!result.lyrics && ["USLT", "ULT"].includes(frameId)) result.lyrics = parseId3Lyrics(payload);
      else if (!result.artwork && ["APIC", "PIC"].includes(frameId)) result.artwork = parseId3Picture(payload, frameId === "PIC");
      cursor = payloadStart + size;
    }
    return result;
  }

  function musicAtomType(bytes, offset) {
    return offset + 4 <= bytes.length ? String.fromCharCode(...bytes.subarray(offset, offset + 4)) : "";
  }

  function musicMp4Boxes(bytes, start, end) {
    const boxes = [];
    let cursor = start;
    while (cursor + 8 <= end) {
      let size = musicReadUint32(bytes, cursor);
      const type = musicAtomType(bytes, cursor + 4);
      let header = 8;
      if (size === 1 && cursor + 16 <= end) {
        const high = musicReadUint32(bytes, cursor + 8);
        const low = musicReadUint32(bytes, cursor + 12);
        size = high * 4294967296 + low;
        header = 16;
      } else if (size === 0) size = end - cursor;
      if (!type || size < header || cursor + size > end) break;
      boxes.push({ type, start: cursor + header, end: cursor + size });
      cursor += size;
    }
    return boxes;
  }

  function parseMp4Metadata(bytes) {
    const result = {};
    let containers = musicMp4Boxes(bytes, 0, bytes.length);
    let ilst = null;
    for (let depth = 0; depth < 8 && containers.length && !ilst; depth += 1) {
      const next = [];
      for (const box of containers) {
        if (box.type === "ilst") { ilst = box; break; }
        if (["moov", "udta", "meta"].includes(box.type)) {
          next.push(...musicMp4Boxes(bytes, box.start + (box.type === "meta" ? 4 : 0), box.end));
        }
      }
      containers = next;
    }
    if (!ilst) return result;
    for (const item of musicMp4Boxes(bytes, ilst.start, ilst.end)) {
      const data = musicMp4Boxes(bytes, item.start, item.end).find(box => box.type === "data");
      if (!data || data.start + 8 > data.end) continue;
      const payload = bytes.subarray(data.start + 8, data.end);
      if (item.type === "©nam") result.title = musicBytesToText(payload);
      else if (["©ART", "aART"].includes(item.type)) result.artist = musicBytesToText(payload);
      else if (item.type === "©alb") result.album = musicBytesToText(payload);
      else if (item.type === "©lyr") result.lyrics = musicBytesToText(payload).slice(0, 50000);
      else if (item.type === "covr" && !result.artwork && payload.length) {
        result.artwork = new Blob([payload], { type: musicImageMime(payload) });
      }
    }
    return result;
  }

  function parseFlacMetadata(bytes) {
    const result = {};
    if (bytes.length < 4 || musicBytesToText(bytes.subarray(0, 4)) !== "fLaC") return result;
    let cursor = 4;
    let last = false;
    while (!last && cursor + 4 <= bytes.length) {
      last = Boolean(bytes[cursor] & 0x80);
      const type = bytes[cursor] & 0x7f;
      const length = (bytes[cursor + 1] << 16) | (bytes[cursor + 2] << 8) | bytes[cursor + 3];
      const start = cursor + 4;
      const end = Math.min(bytes.length, start + length);
      if (type === 4 && start + 8 <= end) {
        let at = start;
        const vendorLength = musicReadUint32(bytes, at, true);
        at += 4 + vendorLength;
        const count = musicReadUint32(bytes, at, true);
        at += 4;
        for (let index = 0; index < count && at + 4 <= end; index += 1) {
          const itemLength = musicReadUint32(bytes, at, true);
          at += 4;
          const entry = musicBytesToText(bytes.subarray(at, Math.min(end, at + itemLength)));
          at += itemLength;
          const separator = entry.indexOf("=");
          if (separator < 0) continue;
          const key = entry.slice(0, separator).toUpperCase();
          const value = entry.slice(separator + 1).trim();
          if (key === "TITLE") result.title = value;
          else if (key === "ARTIST") result.artist = value;
          else if (key === "ALBUM") result.album = value;
          else if (["LYRICS", "UNSYNCEDLYRICS"].includes(key) && value) result.lyrics = value.slice(0, 50000);
        }
      } else if (type === 6 && !result.artwork && start + 32 <= end) {
        result.artwork = musicPictureFromBlock(bytes.subarray(start, end));
      }
      cursor = start + length;
    }
    return result;
  }

  function musicJoinByteParts(parts, length) {
    const joined = new Uint8Array(length);
    let offset = 0;
    parts.forEach(part => {
      joined.set(part, offset);
      offset += part.length;
    });
    return joined;
  }

  function musicOggPackets(bytes, limit = 4) {
    const packets = [];
    let packetParts = [];
    let packetLength = 0;
    let cursor = 0;
    while (cursor + 27 <= bytes.length && packets.length < limit) {
      if (musicBytesToText(bytes.subarray(cursor, cursor + 4), 0) !== "OggS") break;
      const segmentCount = bytes[cursor + 26];
      const lacingStart = cursor + 27;
      const payloadStart = lacingStart + segmentCount;
      if (payloadStart > bytes.length) break;
      let payloadCursor = payloadStart;
      for (let index = 0; index < segmentCount; index += 1) {
        const segmentLength = bytes[lacingStart + index];
        const segmentEnd = payloadCursor + segmentLength;
        if (segmentEnd > bytes.length) return packets;
        if (segmentLength) {
          const part = bytes.subarray(payloadCursor, segmentEnd);
          packetParts.push(part);
          packetLength += part.length;
        }
        payloadCursor = segmentEnd;
        if (segmentLength < 255) {
          packets.push(musicJoinByteParts(packetParts, packetLength));
          packetParts = [];
          packetLength = 0;
          if (packets.length >= limit) return packets;
        }
      }
      cursor = payloadCursor;
    }
    return packets;
  }

  function musicPictureFromBlock(bytes) {
    if (!bytes?.length || bytes.length < 32) return null;
    let cursor = 4;
    const mimeLength = musicReadUint32(bytes, cursor);
    cursor += 4;
    if (cursor + mimeLength + 4 > bytes.length) return null;
    const mime = musicBytesToText(bytes.subarray(cursor, cursor + mimeLength), 0);
    cursor += mimeLength;
    const descriptionLength = musicReadUint32(bytes, cursor);
    cursor += 4 + descriptionLength + 16;
    if (cursor + 4 > bytes.length) return null;
    const imageLength = musicReadUint32(bytes, cursor);
    cursor += 4;
    const image = bytes.subarray(cursor, Math.min(bytes.length, cursor + imageLength));
    return image.length ? new Blob([image], { type: musicImageMime(image, mime) }) : null;
  }

  function musicBytesFromBase64(value) {
    try {
      const raw = atob(String(value || "").replace(/\s/g, ""));
      const bytes = new Uint8Array(raw.length);
      for (let index = 0; index < raw.length; index += 1) bytes[index] = raw.charCodeAt(index);
      return bytes;
    } catch (_) {
      return new Uint8Array();
    }
  }

  function parseOggMetadata(bytes) {
    const result = {};
    const commentPacket = musicOggPackets(bytes, 4).find(packet => {
      const signature = musicBytesToText(packet.subarray(0, 8), 0);
      return signature.startsWith("\u0003vorbis") || signature.startsWith("OpusTags");
    });
    if (!commentPacket) return result;
    const signature = musicBytesToText(commentPacket.subarray(0, 8), 0);
    let cursor = signature.startsWith("OpusTags") ? 8 : 7;
    if (cursor + 4 > commentPacket.length) return result;
    const vendorLength = musicReadUint32(commentPacket, cursor, true);
    cursor += 4 + vendorLength;
    if (cursor + 4 > commentPacket.length) return result;
    const count = musicReadUint32(commentPacket, cursor, true);
    cursor += 4;
    let coverArt = "";
    let coverMime = "image/jpeg";
    for (let index = 0; index < count && cursor + 4 <= commentPacket.length; index += 1) {
      const length = musicReadUint32(commentPacket, cursor, true);
      cursor += 4;
      const entry = musicBytesToText(commentPacket.subarray(cursor, Math.min(commentPacket.length, cursor + length)));
      cursor += length;
      const separator = entry.indexOf("=");
      if (separator < 0) continue;
      const key = entry.slice(0, separator).toUpperCase();
      const value = entry.slice(separator + 1).trim();
      if (key === "TITLE" && value) result.title = value;
      else if (key === "ARTIST" && value) result.artist = value;
      else if (key === "ALBUM" && value) result.album = value;
      else if (["LYRICS", "UNSYNCEDLYRICS"].includes(key) && value) result.lyrics = value.slice(0, 50000);
      else if (key === "METADATA_BLOCK_PICTURE" && value && !result.artwork) {
        result.artwork = musicPictureFromBlock(musicBytesFromBase64(value));
      } else if (key === "COVERART" && value) coverArt = value;
      else if (key === "COVERARTMIME" && value) coverMime = value;
    }
    if (!result.artwork && coverArt) {
      const image = musicBytesFromBase64(coverArt);
      if (image.length) result.artwork = new Blob([image], { type: musicImageMime(image, coverMime) });
    }
    return result;
  }

  async function readEmbeddedMusicMetadata(file) {
    try {
      const bytes = new Uint8Array(await file.arrayBuffer());
      const signature = musicBytesToText(bytes.subarray(0, 12), 0);
      if (signature.startsWith("ID3")) return parseId3Metadata(bytes);
      if (signature.startsWith("fLaC")) return parseFlacMetadata(bytes);
      if (signature.startsWith("OggS")) return parseOggMetadata(bytes);
      if (musicAtomType(bytes, 4) === "ftyp") return parseMp4Metadata(bytes);
      const id3Offset = musicFindSequence(bytes, [0x49, 0x44, 0x33]);
      if (id3Offset >= 0 && musicBytesToText(bytes.subarray(id3Offset, id3Offset + 3), 0) === "ID3") {
        return parseId3Metadata(bytes, id3Offset);
      }
    } catch (error) {
      console.warn("music metadata read failed", error);
    }
    return {};
  }

  function inspectLocalAudio(file) {
    const durationPromise = new Promise((resolve, reject) => {
      const audio = document.createElement("audio");
      const url = URL.createObjectURL(file);
      const done = result => {
        URL.revokeObjectURL(url);
        audio.removeAttribute("src");
        result();
      };
      audio.preload = "metadata";
      audio.onloadedmetadata = () => {
        const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
        done(() => resolve(duration));
      };
      audio.onerror = () => done(() => reject(new Error(`${file.name} 不是这台设备能播放的音频`)));
      audio.src = url;
    });
    return Promise.all([durationPromise, readEmbeddedMusicMetadata(file)]).then(([duration, metadata]) => ({
      duration,
      ...metadata,
      metadata_scanned: true,
    }));
  }

  async function refreshLocalMusicLibrary({ syncLocal = true } = {}) {
    const localTracks = await listLocalMusicTracks();
    let serverTracks = [];
    let serverAvailable = true;
    let syncFailures = [];
    try {
      serverTracks = await fetchSyncedMusicTracks();
      if (syncLocal) {
        if (!musicLibrarySyncPromise) {
          musicLibrarySyncPromise = syncLocalMusicTracks(localTracks, serverTracks)
            .finally(() => { musicLibrarySyncPromise = null; });
        }
        const syncResult = await musicLibrarySyncPromise;
        syncFailures = syncResult.failures;
        if (syncResult.uploadedTracks.length) {
          const serverMap = new Map(serverTracks.map(track => [track.id, track]));
          syncResult.uploadedTracks.forEach(track => serverMap.set(track.id, track));
          serverTracks = [...serverMap.values()];
        }
      }
    } catch (error) {
      serverAvailable = false;
      console.warn("synced music library unavailable", error);
    }
    musicLibraryServerAvailable = serverAvailable;
    musicLibraryRefreshedAt = Date.now();
    const merged = new Map(serverTracks.map(track => [track.id, track]));
    localTracks.forEach(track => {
      const synced = merged.get(track.id) || {};
      merged.set(track.id, {
        ...synced,
        ...track,
        audio_url: synced.audio_url || track.audio_url || "",
        artwork_url: synced.artwork_url || track.artwork_url || "",
        synced: Boolean(synced.audio_url),
      });
    });
    musicLibrary = [...merged.values()].sort((a, b) => Number(a.added_at || 0) - Number(b.added_at || 0));
    if (!musicQueue.length) musicQueue = [...musicLibrary];
    const pendingCount = musicLibrary.filter(track => !track.synced).length;
    const hint = document.getElementById("musicConnectHint");
    return { serverAvailable, pendingCount, syncFailures };
  }

  function renderLocalMusicLibrary(query = "") {
    musicLibraryArtworkUrls.forEach(url => URL.revokeObjectURL(url));
    musicLibraryArtworkUrls = [];
    const normalized = String(query || "").trim().toLocaleLowerCase();
    musicSearchItems = musicLibrary.filter(track => {
      if (!normalized) return true;
      return `${track.name} ${track.artist || ""}`.toLocaleLowerCase().includes(normalized);
    });
    const wrap = document.getElementById("musicSearchResults");
    wrap.innerHTML = musicSearchItems.map(track => `
      <div class="music-search-row" data-track-id="${escapeReadingHtml(track.id)}">
        <button type="button" class="music-track-delete" aria-label="删除 ${escapeReadingHtml(track.name)}" title="删除这首歌">
          <span class="material-symbols-outlined">delete</span><span>删除</span>
        </button>
        <button type="button" class="music-track-play" aria-label="播放 ${escapeReadingHtml(track.name)}">
          ${track.artwork instanceof Blob || track.artwork_url
            ? `<img class="music-local-artwork" data-music-artwork-id="${escapeReadingHtml(track.id)}" alt="">`
            : `<span class="music-local-artwork material-symbols-outlined">music_note</span>`}
          <span class="music-local-meta"><strong>${escapeReadingHtml(track.name)}</strong><span>${escapeReadingHtml(track.artist || "本地音乐")}</span></span>
          <span class="material-symbols-outlined">play_arrow</span>
        </button>
      </div>`).join("") || `<div class="reading-annotation-empty">${musicLibrary.length ? "没有找到这首歌。" : "导入几首歌，曲库就会住在这里。"}</div>`;
    wrap.querySelectorAll(".music-search-row").forEach(row => {
      bindSwipeReveal(row, ".music-track-play", ".music-track-delete");
    });
    wrap.querySelectorAll("img[data-music-artwork-id]").forEach(image => {
      const track = musicLibrary.find(item => item.id === image.dataset.musicArtworkId);
      if (track?.artwork instanceof Blob) {
        const url = URL.createObjectURL(track.artwork);
        musicLibraryArtworkUrls.push(url);
        image.src = url;
      } else if (track?.artwork_url) {
        image.src = track.artwork_url;
      }
    });
  }

  function renderOnlineMusicLibrary(message = "") {
    const wrap = document.getElementById("musicSearchResults");
    if (message) {
      wrap.innerHTML = `<div class="reading-annotation-empty">${escapeReadingHtml(message)}</div>`;
      return;
    }
    wrap.innerHTML = musicOnlineSearchItems.map(track => `
      <div class="music-search-row online-track" data-track-id="${escapeReadingHtml(track.id)}">
        <button type="button" class="music-track-play" aria-label="播放 ${escapeReadingHtml(track.name)}">
          ${track.artwork_url
            ? `<img class="music-local-artwork" src="${escapeReadingHtml(track.artwork_url)}" alt="">`
            : `<span class="music-local-artwork material-symbols-outlined">music_note</span>`}
          <span class="music-local-meta"><strong>${escapeReadingHtml(track.name)}</strong><span>${escapeReadingHtml(track.artist || "网易云音乐")}${track.album ? ` · ${escapeReadingHtml(track.album)}` : ""}</span></span>
          <span class="material-symbols-outlined">play_arrow</span>
        </button>
      </div>`).join("") || '<div class="reading-annotation-empty">输入歌名或歌手，去网易云找一找。</div>';
  }

  function renderNeteaseAccount() {
    const profile = musicNeteaseStatus?.profile || {};
    const valid = Boolean(musicNeteaseStatus?.account_valid);
    const avatar = document.getElementById("musicAccountAvatar");
    document.getElementById("musicConnectTitle").textContent = valid && profile.nickname
      ? profile.nickname
      : "网易云音乐";
    document.getElementById("musicConnectHint").textContent = valid
      ? `${musicNeteasePlaylists.length || ""}${musicNeteasePlaylists.length ? " 个歌单 · " : ""}账号已接入`
      : (musicNeteaseStatus?.error || (musicNeteaseStatus?.account_configured
          ? "正在验证网易云账号"
          : "需要在后端接入 MUSIC_U"));
    if (valid && profile.avatar_url) {
      avatar.src = profile.avatar_url;
      avatar.hidden = false;
    } else {
      avatar.removeAttribute("src");
      avatar.hidden = true;
    }
  }

  function renderNeteasePlaylists(message = "") {
    const shelf = document.getElementById("musicPlaylistShelf");
    if (!musicNeteaseStatus?.account_valid) {
      shelf.classList.add("hidden");
      shelf.innerHTML = "";
      return;
    }
    shelf.classList.remove("hidden");
    if (message) {
      shelf.innerHTML = `<div class="music-playlist-title">我的歌单</div><div class="music-playlist-message">${escapeReadingHtml(message)}</div>`;
      return;
    }
    shelf.innerHTML = `
      <div class="music-playlist-title">我的歌单</div>
      <div class="music-playlist-scroll">
        ${musicNeteasePlaylists.map(playlist => `
          <button type="button" class="music-playlist-card${playlist.id === activeNeteasePlaylistId ? " active" : ""}" data-playlist-id="${escapeReadingHtml(playlist.id)}">
            ${playlist.cover_url
              ? `<img src="${escapeReadingHtml(playlist.cover_url)}" alt="">`
              : `<span class="music-playlist-cover material-symbols-outlined">queue_music</span>`}
            <span><strong>${escapeReadingHtml(playlist.name)}</strong><small>${playlist.track_count} 首</small></span>
          </button>`).join("") || '<span class="music-playlist-message">账号里还没有歌单。</span>'}
      </div>`;
  }

  async function loadNeteasePlaylists({ force = false } = {}) {
    if (musicNeteasePlaylistsBusy || (musicNeteasePlaylistsLoaded && !force)) return;
    if (!musicNeteaseStatus?.account_valid) return;
    musicNeteasePlaylistsBusy = true;
    renderNeteasePlaylists("正在把歌单抱过来……");
    try {
      const data = await musicRequest("/api/music/netease/playlists");
      musicNeteasePlaylists = Array.isArray(data.playlists) ? data.playlists : [];
      musicNeteaseStatus = { ...musicNeteaseStatus, account_valid: true, profile: data.profile || musicNeteaseStatus.profile };
      musicNeteasePlaylistsLoaded = true;
      renderNeteaseAccount();
      renderNeteasePlaylists();
    } catch (error) {
      renderNeteasePlaylists(error.message || "歌单这次没有接上。");
    } finally {
      musicNeteasePlaylistsBusy = false;
    }
  }

  async function openNeteasePlaylist(playlistId) {
    const playlist = musicNeteasePlaylists.find(item => item.id === playlistId);
    if (!playlist) return;
    activeNeteasePlaylistId = playlistId;
    renderNeteasePlaylists();
    renderOnlineMusicLibrary(`正在打开《${playlist.name}》……`);
    document.getElementById("musicSearchResults").classList.remove("hidden");
    try {
      const data = await musicRequest(`/api/music/netease/playlists/${encodeURIComponent(playlistId)}`);
      musicOnlineSearchItems = Array.isArray(data.playlist?.songs) ? data.playlist.songs : [];
      musicQueue = [...musicOnlineSearchItems];
      renderOnlineMusicLibrary(musicOnlineSearchItems.length ? "" : "这个歌单暂时是空的。");
    } catch (error) {
      renderOnlineMusicLibrary(error.message || "这个歌单没有打开。");
    }
  }

  async function searchOnlineMusic(query) {
    const keyword = String(query || "").trim();
    if (!keyword) {
      renderOnlineMusicLibrary("输入歌名或歌手，去网易云找一找。");
      return;
    }
    if (musicOnlineSearchBusy) return;
    activeNeteasePlaylistId = "";
    renderNeteasePlaylists();
    musicOnlineSearchBusy = true;
    renderOnlineMusicLibrary("正在网易云里找歌……");
    try {
      const data = await musicRequest(`/api/music/netease/search?q=${encodeURIComponent(keyword)}&limit=12`);
      musicOnlineSearchItems = Array.isArray(data.songs) ? data.songs : [];
      renderOnlineMusicLibrary(musicOnlineSearchItems.length ? "" : "没有找到这首歌，换个关键词试试。");
    } catch (error) {
      renderOnlineMusicLibrary(error.message || "网易云这次没有接上。");
    } finally {
      musicOnlineSearchBusy = false;
    }
  }

  function updateMusicMediaSession(track) {
    if (!("mediaSession" in navigator) || !window.MediaMetadata) return;
    navigator.mediaSession.metadata = new MediaMetadata({
      title: track.name,
      artist: track.artist || "音乐",
      album: track.album || "Becoming 一起听",
      artwork: musicArtworkObjectUrl ? [{ src: musicArtworkObjectUrl, type: track.artwork?.type || "image/jpeg" }] : [],
    });
  }

  function musicArtworkColor(image) {
    try {
      const canvas = document.createElement("canvas");
      canvas.width = 32;
      canvas.height = 32;
      const context = canvas.getContext("2d", { willReadFrequently: true });
      context.drawImage(image, 0, 0, 32, 32);
      const pixels = context.getImageData(0, 0, 32, 32).data;
      const buckets = new Map();
      for (let index = 0; index < pixels.length; index += 16) {
        if (pixels[index + 3] < 180) continue;
        const red = pixels[index];
        const green = pixels[index + 1];
        const blue = pixels[index + 2];
        const max = Math.max(red, green, blue);
        const min = Math.min(red, green, blue);
        const lightness = (max + min) / 510;
        if (lightness < .09 || lightness > .94) continue;
        const saturation = max === min ? 0 : (max - min) / (255 - Math.abs(max + min - 255));
        const key = `${Math.round(red / 32)}:${Math.round(green / 32)}:${Math.round(blue / 32)}`;
        const bucket = buckets.get(key) || { red: 0, green: 0, blue: 0, weight: 0 };
        const weight = .35 + saturation * 1.65;
        bucket.red += red * weight;
        bucket.green += green * weight;
        bucket.blue += blue * weight;
        bucket.weight += weight;
        buckets.set(key, bucket);
      }
      const winner = [...buckets.values()].sort((a, b) => b.weight - a.weight)[0];
      if (!winner) return "";
      return `rgb(${Math.round(winner.red / winner.weight)} ${Math.round(winner.green / winner.weight)} ${Math.round(winner.blue / winner.weight)})`;
    } catch (_) {
      return "";
    }
  }

  function renderMusicArtwork(track) {
    const image = document.getElementById("musicArtwork");
    const sleeve = document.getElementById("musicRecordSleeve");
    const header = document.getElementById("musicRoomHeader");
    const stage = document.getElementById("musicNowPlaying");
    const surfaces = [header, stage];
    const localArtwork = track?.artwork instanceof Blob ? track.artwork : null;
    const remoteArtwork = !localArtwork ? String(track?.artwork_url || "") : "";
    const key = track ? `${track.id || track.name}:${localArtwork?.size || remoteArtwork}` : "";
    if (key === musicArtworkTrackKey) return;
    musicArtworkTrackKey = key;
    const renderToken = ++musicArtworkRenderToken;
    if (musicArtworkObjectUrl.startsWith("blob:")) URL.revokeObjectURL(musicArtworkObjectUrl);
    musicArtworkObjectUrl = localArtwork ? URL.createObjectURL(localArtwork) : remoteArtwork;
    surfaces.forEach(surface => surface.classList.toggle("has-music-artwork", Boolean(musicArtworkObjectUrl)));
    sleeve.classList.toggle("has-music-artwork", Boolean(musicArtworkObjectUrl));
    surfaces.forEach(surface => {
      surface.style.removeProperty("--music-track-color");
      surface.style.setProperty("--music-track-image", musicArtworkObjectUrl ? `url("${musicArtworkObjectUrl}")` : "none");
    });
    image.removeAttribute("src");
    image.alt = "";
    if (!musicArtworkObjectUrl) return;
    image.onload = () => {
      if (renderToken !== musicArtworkRenderToken) return;
      const color = musicArtworkColor(image);
      if (color) surfaces.forEach(surface => surface.style.setProperty("--music-track-color", color));
    };
    image.onerror = () => {
      if (renderToken !== musicArtworkRenderToken) return;
      surfaces.forEach(surface => surface.classList.remove("has-music-artwork"));
      sleeve.classList.remove("has-music-artwork");
      surfaces.forEach(surface => surface.style.setProperty("--music-track-image", "none"));
      image.removeAttribute("src");
    };
    image.src = musicArtworkObjectUrl;
    image.alt = `${track.name || "歌曲"}封面`;
  }

  async function enrichLocalMusicTrack(track, blob) {
    if (track.metadata_scanned) return track;
    const metadata = await readEmbeddedMusicMetadata(blob);
    const enriched = {
      ...track,
      name: metadata.title || track.name,
      artist: metadata.artist || track.artist,
      album: metadata.album || track.album,
      artwork: metadata.artwork || track.artwork || null,
      lyrics: metadata.lyrics || track.lyrics || "",
      has_lyrics: Boolean(metadata.lyrics || track.lyrics),
      metadata_scanned: true,
    };
    await updateLocalMusicTrack(enriched);
    musicLibrary = musicLibrary.map(item => item.id === enriched.id ? enriched : item);
    musicQueue = musicQueue.map(item => item.id === enriched.id ? enriched : item);
    return enriched;
  }

  function currentLocalMusicState() {
    const audio = document.getElementById("musicAudio");
    const duration = localMusicDuration(audio);
    const position = Number(audio.currentTime);
    return {
      song_id: activeMusicTrack?.id || "",
      song_name: activeMusicTrack?.name || "",
      artist_name: activeMusicTrack?.artist || "",
      album_name: activeMusicTrack?.album || "",
      artwork_url: activeMusicTrack?.artwork_url || "",
      duration_ms: Math.round(duration * 1000),
      position_ms: Math.round((Number.isFinite(position) ? Math.max(0, position) : 0) * 1000),
      playback_state: activeMusicTrack ? (audio.paused ? "paused" : "playing") : "stopped",
    };
  }

  async function persistLocalMusicState() {
    if (!activeMusicTrack || musicLoadingTrack) return;
    const state = currentLocalMusicState();
    musicRoom = { ...(musicRoom || {}), ...state };
    try {
      const data = await musicRequest("/api/music/room", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(state),
      });
      musicRoom = data.room;
    } catch (_) {}
  }

  async function loadLocalMusicTrack(trackId, { autoplay = true, announce = true, positionMs = 0 } = {}) {
    let track = findMusicTrack(trackId);
    if (!track) throw new Error("曲库里没有找到这首歌");
    const isOnlineTrack = track.source === "netease" || String(track.id || "").startsWith("netease:");
    const blob = isOnlineTrack ? null : await getLocalMusicBlob(track.id);
    if (blob) track = await enrichLocalMusicTrack(track, blob);
    track = await hydrateMusicTrackLyrics(track);
    if (!blob && !track.audio_url) throw new Error("这首歌的音频文件不见了");

    const audio = document.getElementById("musicAudio");
    const previousId = activeMusicTrack?.id || musicRoom?.song_id || "";
    musicLoadingTrack = true;
    audio.pause();
    if (musicObjectUrl.startsWith("blob:")) URL.revokeObjectURL(musicObjectUrl);
    musicObjectUrl = blob ? URL.createObjectURL(blob) : track.audio_url;
    activeMusicTrack = { ...track, playback_unavailable: false };
    prepareMusicLyrics(activeMusicTrack);
    renderMusicRoom();
    try {
      if (isOnlineTrack) {
        await musicRequest(`/api/music/netease/audio/${encodeURIComponent(track.source_id)}/status`);
      }
      await new Promise((resolve, reject) => {
        const loaded = () => { cleanup(); resolve(); };
        const failed = () => {
          const code = audio.error?.code;
          const message = code === 2
            ? "网易云音频网络没有接上"
            : code === 3
              ? "这首歌的音频没有解码成功"
              : code === 4
                ? "这首歌暂时没有可播放的音源"
                : "这首歌没有放起来";
          cleanup();
          reject(new Error(message));
        };
        const cleanup = () => {
          audio.removeEventListener("loadedmetadata", loaded);
          audio.removeEventListener("error", failed);
        };
        audio.addEventListener("loadedmetadata", loaded);
        audio.addEventListener("error", failed);
        audio.src = musicObjectUrl;
        audio.load();
      });
      const duration = localMusicDuration(audio, track);
      if (positionMs > 0 && duration > 0) {
        audio.currentTime = Math.min(positionMs / 1000, Math.max(0, duration - 0.25));
      }
    } catch (error) {
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
      activeMusicTrack = { ...track, playback_unavailable: true };
      prepareMusicLyrics(activeMusicTrack);
      musicRoom = {
        ...(musicRoom || {}),
        song_id: track.id,
        song_name: track.name || "",
        artist_name: track.artist || "",
        album_name: track.album || "",
        artwork_url: track.artwork_url || "",
        duration_ms: Math.round(Number(track.duration || 0) * 1000),
        position_ms: 0,
        playback_state: "paused",
      };
      try {
        const saved = await musicRequest("/api/music/room", {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            song_id: track.id,
            song_name: track.name || "",
            artist_name: track.artist || "",
            album_name: track.album || "",
            artwork_url: track.artwork_url || "",
            duration_ms: Math.round(Number(track.duration || 0) * 1000),
            position_ms: 0,
            playback_state: "paused",
          }),
        });
        musicRoom = saved.room;
      } catch (_) {}
      renderMusicRoom();
      throw error;
    } finally {
      musicLoadingTrack = false;
    }
    renderMusicArtwork(track);
    updateMusicMediaSession(track);
    let autoplayBlocked = false;
    if (autoplay) {
      try {
        await audio.play();
      } catch (error) {
        if (error?.name !== "NotAllowedError") throw error;
        autoplayBlocked = true;
      }
    }
    await persistLocalMusicState();
    renderMusicRoom();

    if (announce && (musicRoom?.participants || []).length) {
      const reacted = await musicRequest("/api/music/room/react", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ event_type: previousId && previousId !== track.id ? "track_changed" : "track_started" }),
      });
      musicRoom = reacted.room;
      renderMusicRoom();
      await applyPendingMusicCommands();
    }
    return { track, autoplayBlocked };
  }

  async function importLocalMusic(files) {
    const selected = [...files].filter(file => file.size > 0);
    const artworkFiles = selected.filter(isMusicArtworkFile);
    const lyricsFiles = selected.filter(isMusicLyricsFile);
    const candidates = selected.filter(file => !isMusicArtworkFile(file) && !isMusicLyricsFile(file));
    if (!candidates.length && !artworkFiles.length && !lyricsFiles.length) return;
    if (navigator.storage?.persist) navigator.storage.persist().catch(() => {});
    const artworkByName = new Map(
      artworkFiles.map(file => [localMusicImportKey(file.name), file]),
    );
    const lyricsByName = new Map(await Promise.all(
      lyricsFiles.map(async file => [localMusicImportKey(file.name), await readMusicLyricsFile(file)]),
    ));
    let patchedArtwork = 0;
    let patchedLyrics = 0;
    if (artworkByName.size || lyricsByName.size) {
      const localTracks = await listLocalMusicTracks();
      for (const track of localTracks) {
        const artwork = artworkByName.get(localMusicImportKey(track.filename || track.name));
        const lyrics = lyricsByName.get(localMusicImportKey(track.filename || track.name));
        if (!artwork && !lyrics) continue;
        await updateLocalMusicTrack({
          ...track,
          ...(artwork ? { artwork, artwork_url: "" } : {}),
          ...(lyrics ? { lyrics, has_lyrics: true } : {}),
        });
        if (artwork) patchedArtwork += 1;
        if (lyrics) patchedLyrics += 1;
      }
    }
    let imported = 0;
    const importFailures = [];
    for (const file of candidates) {
      try {
        const inspected = await inspectLocalAudio(file);
        const filenameMetadata = localMusicFilenameMetadata(file.name);
        const track = {
          id: localMusicTrackId(file),
          name: inspected.title || filenameMetadata.title || localMusicDisplayName(file.name),
          artist: inspected.artist || filenameMetadata.artist || "本地音乐",
          album: inspected.album || "",
          artwork: inspected.artwork || artworkByName.get(localMusicImportKey(file.name)) || null,
          lyrics: inspected.lyrics || lyricsByName.get(localMusicImportKey(file.name)) || "",
          has_lyrics: Boolean(inspected.lyrics || lyricsByName.get(localMusicImportKey(file.name))),
          metadata_scanned: true,
          duration: inspected.duration,
          size: file.size,
          type: file.type || "audio/*",
          filename: file.name,
          added_at: Date.now() + imported,
        };
        await saveLocalMusicTrack(track, file);
        imported += 1;
      } catch (error) {
        importFailures.push({ file, error });
      }
    }
    musicQueue = [];
    const syncStatus = await refreshLocalMusicLibrary();
    document.getElementById("musicSearchResults").classList.remove("hidden");
    if (!candidates.length) {
      const patched = [];
      if (patchedArtwork) patched.push(`${patchedArtwork} 首封面`);
      if (patchedLyrics) patched.push(`${patchedLyrics} 首歌词`);
      const unmatched = artworkFiles.length + lyricsFiles.length - patchedArtwork - patchedLyrics;
      showToast(patched.length
        ? `补好了 ${patched.join("、")}${unmatched > 0 ? `，${unmatched} 个文件没有找到同名歌曲` : ""}`
        : "没有找到同名歌曲，封面或歌词名要和歌曲名一致");
    } else if (importFailures.length) {
      const firstName = importFailures[0].file?.name || "音频";
      showToast(`收下 ${imported} 首，${firstName} 没有认出来`);
    } else if (syncStatus.pendingCount) {
      const reason = syncStatus.syncFailures[0]?.error?.message;
      showToast(reason ? `已保存在电脑，云端同步失败：${reason}` : `收下 ${imported} 首，稍后继续同步`);
    } else {
      showToast(`收下并同步了 ${imported} 首歌`);
    }
  }

  async function initializeMusicPlayer() {
    if (musicLocalReady) return;
    const audio = document.getElementById("musicAudio");
    audio.addEventListener("timeupdate", () => {
      if (!activeMusicTrack) return;
      musicRoom = { ...(musicRoom || {}), ...currentLocalMusicState() };
      renderMusicPlaybackState();
    });
    audio.addEventListener("play", () => {
      if (musicLoadingTrack) return;
      musicRoom = { ...(musicRoom || {}), ...currentLocalMusicState() };
      renderMusicPlaybackState();
      persistLocalMusicState();
    });
    audio.addEventListener("pause", () => {
      if (musicLoadingTrack) return;
      musicRoom = { ...(musicRoom || {}), ...currentLocalMusicState() };
      renderMusicPlaybackState();
      persistLocalMusicState();
    });
    audio.addEventListener("ended", () => {
      runMusicAction("next", { automatic: true }).catch(() => {
        audio.pause();
        try { audio.currentTime = 0; } catch (_) {}
        musicRoom = { ...(musicRoom || {}), ...currentLocalMusicState() };
        renderMusicPlaybackState();
        persistLocalMusicState();
      });
    });
    if ("mediaSession" in navigator) {
      for (const [action, handler] of [
        ["play", () => runMusicAction("play")],
        ["pause", () => runMusicAction("pause")],
        ["previoustrack", () => runMusicAction("previous")],
        ["nexttrack", () => runMusicAction("next")],
      ]) {
        try { navigator.mediaSession.setActionHandler(action, handler); } catch (_) {}
      }
    }
    musicRoomSyncTimer = setInterval(() => {
      if (activeNestPane === "music" && activeMusicTrack && !audio.paused) persistLocalMusicState();
    }, 8000);
    renderMusicPlaybackMode();
    musicLocalReady = true;
  }

  function renderMusicPlaybackMode() {
    const button = document.getElementById("musicPlaybackModeBtn");
    const meta = MUSIC_PLAYBACK_MODE_META[musicPlaybackMode];
    button.dataset.mode = musicPlaybackMode;
    button.setAttribute("aria-label", meta.label);
    button.title = meta.label;
    button.querySelector(".material-symbols-outlined").textContent = meta.icon;
  }

  function cycleMusicPlaybackMode() {
    const currentIndex = MUSIC_PLAYBACK_MODES.indexOf(musicPlaybackMode);
    musicPlaybackMode = MUSIC_PLAYBACK_MODES[(currentIndex + 1) % MUSIC_PLAYBACK_MODES.length];
    try { localStorage.setItem(MUSIC_PLAYBACK_MODE_KEY, musicPlaybackMode); } catch (_) {}
    renderMusicPlaybackMode();
    showToast(MUSIC_PLAYBACK_MODE_META[musicPlaybackMode].label);
  }

  function musicQueueIndex(queue, action, automatic = false) {
    let currentIndex = queue.findIndex(track => track.id === activeMusicTrack?.id);
    if (currentIndex < 0) currentIndex = 0;

    if (automatic && musicPlaybackMode === "repeat_one") return currentIndex;
    if (musicPlaybackMode === "shuffle" && queue.length > 1) {
      const choices = queue.map((_, index) => index).filter(index => index !== currentIndex);
      return choices[Math.floor(Math.random() * choices.length)];
    }
    if (automatic && action === "next" && currentIndex >= queue.length - 1) return -1;
    return action === "previous"
      ? (currentIndex - 1 + queue.length) % queue.length
      : (currentIndex + 1) % queue.length;
  }

  function renderMusicPlaybackState() {
    const audio = document.getElementById("musicAudio");
    const hasTrack = Boolean(activeMusicTrack);
    const canPlay = hasTrack && !activeMusicTrack.playback_unavailable;
    const durationSeconds = hasTrack ? localMusicDuration(audio) : (musicRoom?.duration_ms || 0) / 1000;
    const rawPosition = Number(audio.currentTime);
    const positionSeconds = hasTrack
      ? (Number.isFinite(rawPosition) ? Math.max(0, rawPosition) : 0)
      : (musicRoom?.position_ms || 0) / 1000;
    const progress = document.getElementById("musicProgress");
    progress.value = durationSeconds ? Math.min(1000, positionSeconds / durationSeconds * 1000) : 0;
    progress.disabled = !canPlay;
    document.getElementById("musicCurrentTime").textContent = formatMusicTime(positionSeconds);
    document.getElementById("musicDuration").textContent = formatMusicTime(durationSeconds);
    const isPlaying = canPlay && !audio.paused;
    document.getElementById("musicRecordSleeve").classList.toggle("is-playing", isPlaying);
    document.querySelector("#musicPlayBtn .material-symbols-outlined").textContent = isPlaying ? "pause" : "play_arrow";
    document.getElementById("musicPlayBtn").setAttribute("aria-label", isPlaying ? "暂停" : "播放");
    document.getElementById("musicPreviousBtn").disabled = !canPlay;
    document.getElementById("musicPlayBtn").disabled = !canPlay;
    document.getElementById("musicNextBtn").disabled = !canPlay;
    renderMusicLyrics(positionSeconds);
  }

  function renderMusicRoom() {
    if (!musicRoom) return;
    const avatarWrap = document.getElementById("musicAvatarPair");
    const people = [
      ...(musicRoom.participants || []),
      { name: GROUP_CHAR_NAMES.user, avatar: userAvatar },
    ];
    const curImgs = avatarWrap.querySelectorAll("img");
    const avatarChanged = curImgs.length !== people.length ||
      [...curImgs].some((img, i) => img.getAttribute("src") !== people[i].avatar);
    if (avatarChanged) {
      avatarWrap.innerHTML = people.map(person =>
        `<img class="music-room-avatar" src="${escapeReadingHtml(person.avatar)}" alt="${escapeReadingHtml(person.name)}">`
      ).join("");
    }
    const elapsedMinutes = Math.floor((musicRoom.together_seconds || 0) / 60);
    const hasCompanions = Boolean((musicRoom.participants || []).length);
    document.getElementById("musicTogetherTime").textContent = elapsedMinutes
      ? `${hasCompanions ? "一起" : "自己"}听了 ${elapsedMinutes} 分钟`
      : (hasCompanions ? "刚刚坐下" : "一个人也很好");
    document.getElementById("musicDistanceBtn").hidden = !hasCompanions;
    document.getElementById("musicDistanceBtn").textContent = musicRoom.distance_km == null
      ? "相距多远" : `相距 ${Number(musicRoom.distance_km).toLocaleString()} 公里`;

    const shownTrack = activeMusicTrack || (musicRoom.song_id ? {
      id: musicRoom.song_id,
      name: musicRoom.song_name,
      artist: musicRoom.artist_name,
      album: musicRoom.album_name,
      artwork_url: musicRoom.artwork_url,
    } : null);
    const hasTrack = Boolean(shownTrack);
    document.getElementById("musicNowPlaying").classList.toggle("music-now-empty", !hasTrack);
    if (!hasTrack) prepareMusicLyrics(null);
    renderMusicArtwork(shownTrack);
    document.getElementById("musicTrackName").textContent = hasTrack
      ? shownTrack.name : "房间还安安静静的";
    document.getElementById("musicArtistName").textContent = hasTrack
      ? shownTrack.artist || "音乐" : (hasCompanions ? "挑一首歌，再喊祂们坐过来" : "挑一首歌，自己听也很好");
    renderMusicPlaybackState();
    renderMusicMessages();
  }

  function renderMusicMessages() {
    const wrap = document.getElementById("musicRoomMessages");
    const messages = musicRoom?.messages || [];
    const visibleMessages = messages.slice(-2);
    const renderKey = JSON.stringify(visibleMessages.map(message => [
      message.id,
      message.author_id,
      message.author_name,
      message.author_id === "user" ? userAvatar : message.avatar,
      message.content,
      message.details,
    ]));
    if (wrap.dataset.renderKey === renderKey && wrap.childElementCount) return;
    wrap.dataset.renderKey = renderKey;
    if (!messages.length) {
      wrap.innerHTML = '<div class="reading-annotation-empty">同一副耳机里还没有人说话。</div>';
      return;
    }
    wrap.innerHTML = visibleMessages.map(message => {
      const isUser = message.author_id === "user";
      const messageAvatar = isUser ? userAvatar : message.avatar;
      const details = message.details || {};
      const traces = Array.isArray(details.tools) && details.tools.length
        ? details.tools
        : (details.tool ? [{ name: details.tool, arguments: details.input, output: details.output }] : []);
      const toolLabels = {
        music_player_control: "播放器",
        music_search: "搜歌",
        music_play_track: "点歌",
      };
      const tool = traces.map(trace => `
        <details class="music-tool-detail">
          <summary>${escapeReadingHtml(toolLabels[trace.name] || trace.name || "音乐工具")}</summary>
          <pre>${escapeReadingHtml(JSON.stringify({ input: trace.arguments || {}, output: trace.output, status: trace.status }, null, 2))}</pre>
        </details>`).join("");
      const avatarImg = `<img class="music-message-avatar" src="${escapeReadingHtml(messageAvatar)}" alt="">`;
      return `
        <div class="music-room-message${isUser ? " from-user" : ""}">
          ${isUser ? "" : avatarImg}
          <div class="music-message-body">
            ${isUser ? "" : `<strong>${escapeReadingHtml(message.author_name)}</strong>`}
            <p class="music-message-bubble">${escapeReadingHtml(message.content)}</p>${tool}
          </div>
          ${isUser ? avatarImg : ""}
        </div>`;
    }).join("");
  }

  async function loadMusicRoom({ quiet = false } = {}) {
    try {
      const data = await musicRequest("/api/music/room");
      musicRoom = data.room;
      await initializeMusicPlayer();
      if (!musicNeteaseStatus) {
        try { musicNeteaseStatus = await musicRequest("/api/music/netease/status"); }
        catch (error) {
          musicNeteaseStatus = { available: false, account_configured: false, account_valid: false, error: error.message };
        }
        renderNeteaseAccount();
        renderNeteasePlaylists();
        await loadNeteasePlaylists();
      }
      if (!activeMusicTrack && musicRoom.song_id) {
        let savedTrack = findMusicTrack(musicRoom.song_id);
        if (!savedTrack && String(musicRoom.song_id).startsWith("netease:")) {
          const sourceId = String(musicRoom.song_id).split(":", 2)[1];
          try {
            const onlineData = await musicRequest(`/api/music/netease/tracks/${encodeURIComponent(sourceId)}`);
            savedTrack = onlineData.track;
            if (savedTrack) {
              musicOnlineSearchItems = [savedTrack, ...musicOnlineSearchItems.filter(track => track.id !== savedTrack.id)];
              musicQueue = [savedTrack];
            }
          } catch (error) {
            if (!quiet) showToast(error.message || "在线歌曲暂时没有接上");
          }
        }
        if (savedTrack) {
          await loadLocalMusicTrack(savedTrack.id, {
            autoplay: false,
            announce: false,
            positionMs: musicRoom.position_ms || 0,
          });
        } else if (!String(musicRoom.song_id).startsWith("netease:")) {
          const reset = await musicRequest("/api/music/room", {
            method: "PUT", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ reset: true }),
          });
          musicRoom = reset.room;
        }
      }
      renderMusicRoom();
      await applyPendingMusicCommands();
      if (!musicRoomPollTimer) {
        musicRoomPollTimer = setInterval(() => {
          if (activeNestPane === "music" && document.getElementById("momentsView").classList.contains("active")) {
            loadMusicRoom({ quiet: true });
          }
        }, 5000);
      }
    } catch (error) {
      if (!quiet) showToast(error.message);
    }
  }

  document.getElementById("musicLibraryPanel").addEventListener("toggle", event => {
    if (!event.target.open) return;
    renderNeteaseAccount();
    renderNeteasePlaylists();
    loadNeteasePlaylists();
  });

  document.getElementById("musicPlaylistShelf").addEventListener("click", event => {
    const button = event.target.closest("[data-playlist-id]");
    if (button) openNeteasePlaylist(button.dataset.playlistId);
  });

  document.getElementById("musicLibraryCollapseBtn").addEventListener("click", () => {
    document.getElementById("musicSearchResults").classList.add("hidden");
    document.getElementById("musicSearchInput").blur();
    document.getElementById("musicLibraryPanel").open = false;
  });

  document.getElementById("musicSearchForm").addEventListener("submit", async event => {
    event.preventDefault();
    const results = document.getElementById("musicSearchResults");
    const query = document.getElementById("musicSearchInput").value;
    await searchOnlineMusic(query);
    results.classList.remove("hidden");
  });

  document.getElementById("musicSearchInput").addEventListener("focus", () => {
    renderOnlineMusicLibrary();
    document.getElementById("musicSearchResults").classList.remove("hidden");
  });

  document.getElementById("musicSearchInput").addEventListener("input", () => {
    document.getElementById("musicSearchResults").classList.remove("hidden");
  });

  document.getElementById("musicSearchResults").addEventListener("click", async event => {
    const row = event.target.closest(".music-search-row");
    if (!row) return;
    let track = findMusicTrack(row.dataset.trackId);
    if (!track) return;
    try {
      const prepared = await musicRequest(`/api/music/netease/tracks/${encodeURIComponent(track.source_id)}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(track),
      });
      track = prepared.track;
      musicOnlineSearchItems = musicOnlineSearchItems.map(item => item.id === track.id ? track : item);
      musicQueue = [...musicOnlineSearchItems];
      document.getElementById("musicSearchResults").classList.add("hidden");
      const playback = await loadLocalMusicTrack(track.id);
      if (playback.autoplayBlocked) showToast("歌已经放进播放器，轻点一下就开唱");
    } catch (error) { showToast(error.message || "这首歌没有放起来"); }
  });

  async function runMusicAction(action, { announce = true, automatic = false } = {}) {
    const audio = document.getElementById("musicAudio");
    if (!activeMusicTrack) throw new Error("曲库里还没有正在播放的歌");
    if (action === "pause") audio.pause();
    else if (action === "play") await audio.play();
    else if (action === "previous" || action === "next") {
      const queue = musicQueue.length ? musicQueue : musicOnlineSearchItems;
      if (!queue.length) throw new Error("当前曲库还是空的");
      if (queue.length === 1 && !automatic) throw new Error("播放队列里暂时只有这一首");
      const index = musicQueueIndex(queue, action, automatic);
      if (index < 0) {
        audio.pause();
        try { audio.currentTime = 0; } catch (_) {}
        await persistLocalMusicState();
        renderMusicRoom();
        return;
      }
      if (automatic && musicPlaybackMode === "repeat_one") {
        try { audio.currentTime = 0; } catch (_) {}
        await audio.play();
        await persistLocalMusicState();
        renderMusicRoom();
        return;
      }
      await loadLocalMusicTrack(queue[index].id, { announce });
      return;
    }
    await persistLocalMusicState();
    renderMusicRoom();
  }

  document.getElementById("musicPreviousBtn").addEventListener("click", () => runMusicAction("previous").catch(error => showToast(error.message)));
  document.getElementById("musicNextBtn").addEventListener("click", () => runMusicAction("next").catch(error => showToast(error.message)));
  document.getElementById("musicPlaybackModeBtn").addEventListener("click", cycleMusicPlaybackMode);
  document.getElementById("musicPlayBtn").addEventListener("click", () => {
    const action = document.getElementById("musicAudio").paused ? "play" : "pause";
    runMusicAction(action).catch(error => showToast(error.message));
  });
  document.getElementById("musicProgress").addEventListener("change", async event => {
    const audio = document.getElementById("musicAudio");
    const duration = localMusicDuration(audio);
    if (!activeMusicTrack || !duration) return;
    try {
      audio.currentTime = duration * Number(event.target.value) / 1000;
      await persistLocalMusicState();
      renderMusicPlaybackState();
    } catch (error) { showToast(error.message || "没有跳到这里"); }
  });

  async function applyPendingMusicCommands() {
    for (const command of musicRoom?.pending_commands || []) {
      if (handledMusicCommands.has(command.id)) continue;
      handledMusicCommands.add(command.id);
      let status = "applied";
      let output = "播放器已执行";
      try {
        if (command.action === "play_online") {
          let track = command.arguments?.track;
          if (!track || track.source !== "netease" || !track.id || !track.audio_url) {
            throw new Error("在线点歌资料不完整");
          }
          const suggestedQueue = Array.isArray(command.arguments?.queue)
            ? command.arguments.queue.filter(item => (
              item && item.source === "netease" && item.id && item.audio_url
            )).slice(0, 6)
            : [];
          const nextQueue = [track, ...suggestedQueue.filter(item => item.id !== track.id)];
          musicOnlineSearchItems = [
            ...nextQueue,
            ...musicOnlineSearchItems.filter(item => !nextQueue.some(candidate => candidate.id === item.id)),
          ];
          musicQueue = [...musicOnlineSearchItems];
          const playback = await loadLocalMusicTrack(track.id, { announce: false });
          output = playback.autoplayBlocked
            ? `已选好《${track.name}》，需要你轻点播放`
            : `已开始播放《${track.name}》`;
        } else {
          await runMusicAction(command.action, { announce: false });
        }
      }
      catch (error) { status = "failed"; output = error.message || "播放器执行失败"; }
      try {
        await musicRequest(`/api/music/room/commands/${command.id}`, {
          method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status, output }),
        });
      } catch (_) {}
    }
  }

  document.getElementById("musicParticipantsBtn").addEventListener("click", () => {
    openCharPicker("music_participants", null, (musicRoom?.participants || []).map(person => person.id));
  });

  async function saveMusicParticipants(characterIds) {
    try {
      const data = await musicRequest("/api/music/room/participants", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ character_ids: characterIds }),
      });
      musicRoom = data.room || { ...(musicRoom || {}), participants: data.participants };
      renderMusicRoom();
      if (!characterIds.length) {
        showToast("那就自己安安静静听");
        return;
      }
      const reacted = await musicRequest("/api/music/room/react", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ event_type: "invite" }),
      });
      musicRoom = reacted.room;
      renderMusicRoom();
      await applyPendingMusicCommands();
    } catch (error) { showToast(error.message); }
  }

  document.getElementById("musicDistanceBtn").addEventListener("click", async () => {
    const current = musicRoom?.distance_km == null ? "" : String(musicRoom.distance_km);
    const raw = prompt("相距多少公里？留空就隐藏", current);
    if (raw === null) return;
    try {
      const body = { ...musicRoom, distance_km: raw.trim() === "" ? null : raw.trim() };
      delete body.messages;
      delete body.participants;
      delete body.pending_commands;
      const data = await musicRequest("/api/music/room", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      musicRoom = data.room;
      renderMusicRoom();
    } catch (error) { showToast(error.message); }
  });

  document.getElementById("musicMessageForm").addEventListener("submit", async event => {
    event.preventDefault();
    const input = document.getElementById("musicMessageInput");
    const content = input.value.trim();
    if (!content) return;
    input.disabled = true;
    try {
      const data = await musicRequest("/api/music/room/messages", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content }),
      });
      input.value = "";
      musicRoom = data.room;
      renderMusicRoom();
      await applyPendingMusicCommands();
    } catch (error) { showToast(error.message); }
    finally { input.disabled = false; input.focus(); }
  });

  document.getElementById("musicMessageInput").addEventListener("focus", () => {
    setTimeout(() => {
      window.scrollTo(0, 0);
      const musicScroll = document.getElementById("musicRoomScroll");
      musicScroll.scrollTop = musicScroll.scrollHeight;
      document.getElementById("musicMessageInput").scrollIntoView({ block: "nearest" });
    }, 350);
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
        <button type="button" class="reading-book-delete" aria-label="删除 ${escapeReadingHtml(book.title)}" title="删除这本书">
          <span class="material-symbols-outlined">delete</span><span>删除</span>
        </button>
        <button type="button" class="reading-book-open" aria-label="打开 ${escapeReadingHtml(book.title)}">
          <div class="reading-book-main">
            <div class="reading-book-title">${escapeReadingHtml(book.title)}</div>
            <div class="reading-book-meta">${book.total_chapters} 章 · ${book.progress.percent}% · ${book.encoding.toUpperCase()}</div>
          </div>
          <div class="reading-book-people">${people}</div>
          <div class="reading-book-progress" aria-label="已读 ${book.progress.percent}%"><span style="width:${book.progress.percent}%"></span></div>
        </button>
      `;
      row.querySelector(".reading-book-open").addEventListener("click", () => openReadingBook(book.id));
      row.querySelector(".reading-book-delete").addEventListener("click", () => {
        setSwipeRevealOpen(row, false);
        showConfirmDialog(`把《${book.title}》和它的划线批注一起移出书架？`, async () => {
          try {
            await readingRequest(`/api/reading/books/${book.id}`, { method: "DELETE" });
            readingBooks = readingBooks.filter(item => item.id !== book.id);
            renderReadingBooks();
            showToast("已经从书架移走啦");
          } catch (error) { showToast(error.message); }
        });
      });
      bindSwipeReveal(row, ".reading-book-open", ".reading-book-delete");
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

  function captureReadingSelection({ suppressNativeCallout = false } = {}) {
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
    // iOS may leave its Copy/Look Up/Translate menu above our own reading toolbar.
    // The offsets are safely stored above, so clearing only the native Range keeps
    // our highlight/note/ask actions intact while dismissing that system menu.
    if (suppressNativeCallout) selection.removeAllRanges();
  }

  const readingContentElement = document.getElementById("readingContent");
  let readingPointerType = "";
  readingContentElement.addEventListener("pointerdown", event => {
    readingPointerType = event.pointerType || "";
  });
  readingContentElement.addEventListener("contextmenu", event => {
    if (readingPointerType === "touch") event.preventDefault();
  });
  readingContentElement.addEventListener("pointerup", event => {
    const suppressNativeCallout = event.pointerType === "touch";
    setTimeout(() => captureReadingSelection({ suppressNativeCallout }), 20);
  });
  readingContentElement.addEventListener("keyup", () => captureReadingSelection());

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
        else if (activeNestPane === "reading") loadReadingBooks();
        else loadMusicRoom();
        syncMomentsFab();
      } else {
        momentsFab.classList.add("hidden");
      }
    });
  });

  // ── 页面加载：检查登录态，再初始化 ──
  document.addEventListener("DOMContentLoaded", async () => {
    hydrateSecondaryViewsFromCache();
    await loadSettings();
    await loadAppearance();
    await loadGroupConfig();
    await loadVoiceFeatureState();
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
  window.addEventListener("pagehide", cacheGroupHistorySnapshot);
