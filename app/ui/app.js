/* Owlet dashboard.
 *
 * Talks only to the local FastAPI server on the same origin. All state-changing
 * calls carry the X-Owlet header required by the backend's CSRF guard.
 * Rendered model output is treated as untrusted: Markdown is escaped before a
 * small allow-listed formatter runs, and links are limited to http(s).
 */
(() => {
  "use strict";

  // ---------- theme (before anything paints) ----------

  const THEME_KEY = "owlet-theme";
  const root = document.documentElement;
  const media = window.matchMedia("(prefers-color-scheme: dark)");

  function applyTheme(theme) {
    root.dataset.theme = theme;
    const btn = document.getElementById("themeBtn");
    if (btn) {
      btn.textContent = theme === "dark" ? "☀" : "☾";
      btn.title = theme === "dark" ? "Switch to light theme" : "Switch to dark theme";
      btn.setAttribute("aria-label", btn.title);
    }
  }

  function initialTheme() {
    try {
      const saved = localStorage.getItem(THEME_KEY);
      if (saved === "light" || saved === "dark") return saved;
    } catch (_) {
      /* storage may be disabled */
    }
    return media.matches ? "dark" : "light";
  }

  applyTheme(initialTheme());

  // ---------- element refs ----------

  const $ = (id) => document.getElementById(id);

  const els = {
    modelBadge: $("modelBadge"),
    browserBadge: $("browserBadge"),
    agentBadge: $("agentBadge"),
    stopBtn: $("stopBtn"),
    themeBtn: $("themeBtn"),
    moodleUrl: $("moodleUrl"),
    connectBtn: $("connectBtn"),
    httpUser: $("httpUser"),
    httpPass: $("httpPass"),
    loginHint: $("loginHint"),
    connState: $("connState"),
    pageTitle: $("pageTitle"),
    pageUrl: $("pageUrl"),
    checkLoginBtn: $("checkLoginBtn"),
    syncBtn: $("syncBtn"),
    syncState: $("syncState"),
    disconnectBtn: $("disconnectBtn"),
    refreshCourses: $("refreshCourses"),
    refreshAssignments: $("refreshAssignments"),
    courseList: $("courseList"),
    assignmentList: $("assignmentList"),
    courseCount: $("courseCount"),
    assignmentCount: $("assignmentCount"),
    chatTitle: $("chatTitle"),
    chatSubtitle: $("chatSubtitle"),
    newChatBtn: $("newChatBtn"),
    messages: $("messages"),
    approvalBar: $("approvalBar"),
    composer: $("composer"),
    messageInput: $("messageInput"),
    sendBtn: $("sendBtn"),
    agentActivity: $("agentActivity"),
    modelMeta: $("modelMeta"),
    refreshFiles: $("refreshFiles"),
    fileTree: $("fileTree"),
    activityLog: $("activityLog"),
    activityCount: $("activityCount"),
    approvalModal: $("approvalModal"),
    approvalHeading: $("approvalHeading"),
    approvalMessage: $("approvalMessage"),
    approvalDetails: $("approvalDetails"),
    approveBtn: $("approveBtn"),
    denyBtn: $("denyBtn"),
    draftModal: $("draftModal"),
    draftHeading: $("draftHeading"),
    draftStatus: $("draftStatus"),
    draftMeta: $("draftMeta"),
    draftHint: $("draftHint"),
    draftFiles: $("draftFiles"),
    draftCloseBtn: $("draftCloseBtn"),
    draftReviewBtn: $("draftReviewBtn"),
    draftStartBtn: $("draftStartBtn"),
    toast: $("toast"),
    toolCount: $("toolCount"),
    brandModel: $("brandModel"),
    chatCount: $("chatCount"),
    conversationList: $("conversationList"),
    chatPanel: $("chatPanel"),
    attachments: $("attachments"),
    attachBtn: $("attachBtn"),
    imageInput: $("imageInput"),
    dropOverlay: $("dropOverlay"),
    scrollBtn: $("scrollBtn"),
    modelNotice: $("modelNotice"),
    modelNoticeTitle: $("modelNoticeTitle"),
    modelNoticeBody: $("modelNoticeBody"),
    modelNoticeCommand: $("modelNoticeCommand"),
  };

  const welcomeMarkup = els.messages ? els.messages.innerHTML : "";
  const defaultChatTitle = els.chatTitle ? els.chatTitle.textContent : "New conversation";
  const defaultChatSubtitle = els.chatSubtitle ? els.chatSubtitle.textContent : "";

  // ---------- state ----------

  const state = {
    conversationId: null,
    conversations: [],
    running: false,
    sending: false,
    conversationStarted: false,
    // The assistant message currently being streamed:
    // { id, msg, bubble, body, text, frame, done }.
    stream: null,
    waitingEl: null,
    lastAssistantEl: null,
    lastUserMessageEl: null,
    lastUserText: "",
    attachmentSeq: 0,
    pendingTools: [],
    approvals: new Map(),
    currentApprovalId: null,
    activity: [],
    files: { workspace: [], generated: [] },
    openDirs: new Set(),
    syncing: false,
    courses: [],
    assignments: [],
    activeTab: "workspace",
    tools: [],
    connected: false,
    sseFailed: false,
    lastBrowser: null,
    draftId: null,
    draft: null,
    attachments: [],
    chatConfig: { max_images: 4, max_image_mb: 8 },
    ollama: null,
  };

  let toastTimer = 0;
  let dataRefreshTimer = 0;
  let browserRefreshTimer = 0;
  let browserPollBusy = false;
  let resetEchoUntil = 0;

  // ---------- helpers ----------

  const ACTIVITY_KINDS = new Set([
    "tool",
    "reading",
    "navigate",
    "download",
    "file",
    "error",
    "warning",
    "browser",
    "approval_required",
    "approval_timeout",
    "stopped",
    "done",
    "reset",
  ]);

  const ACTIVITY_ICONS = {
    tool: "🔧",
    reading: "📖",
    navigate: "🧭",
    download: "⬇",
    file: "📄",
    error: "⚠",
    warning: "⚠",
    browser: "🌐",
    approval_required: "🔒",
    approval_timeout: "⏱",
    stopped: "■",
    done: "✓",
    reset: "🧹",
  };

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => {
      switch (char) {
        case "&":
          return "&amp;";
        case "<":
          return "&lt;";
        case ">":
          return "&gt;";
        case '"':
          return "&quot;";
        default:
          return "&#39;";
      }
    });
  }

  function escapeAttr(value) {
    return escapeHtml(value).replace(/`/g, "&#96;");
  }

  function truncate(text, max) {
    const value = String(text ?? "");
    return value.length > max ? `${value.slice(0, max - 1)}…` : value;
  }

  function formatBytes(bytes) {
    const size = Number(bytes) || 0;
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  }

  function formatClock(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  }

  function relativeTime(value) {
    if (!value) return "";
    const date = new Date(String(value).replace(" ", "T"));
    if (Number.isNaN(date.getTime())) return "";
    const seconds = Math.round((Date.now() - date.getTime()) / 1000);
    if (seconds < 45) return "just now";
    if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))}m ago`;
    if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
    if (seconds < 604800) return `${Math.round(seconds / 86400)}d ago`;
    return date.toLocaleDateString([], { month: "short", day: "numeric" });
  }

  function modelName() {
    return state.chatConfig?.model || state.ollama?.model || "local model";
  }

  function maxImages() {
    const value = Number(state.chatConfig?.max_images);
    return Number.isFinite(value) && value > 0 ? value : 4;
  }

  function maxImageBytes() {
    const value = Number(state.chatConfig?.max_image_mb);
    return (Number.isFinite(value) && value > 0 ? value : 8) * 1024 * 1024;
  }

  function humanize(value) {
    const text = String(value ?? "").replace(/[_-]+/g, " ").trim();
    return text ? text.charAt(0).toUpperCase() + text.slice(1) : "this action";
  }

  function plural(count, one, many) {
    return `${count} ${count === 1 ? one : many || `${one}s`}`;
  }

  function showToast(message, tone) {
    if (!els.toast) return;
    window.clearTimeout(toastTimer);
    els.toast.textContent = message;
    els.toast.classList.remove("hidden", "ok", "bad");
    if (tone) els.toast.classList.add(tone);
    toastTimer = window.setTimeout(() => els.toast.classList.add("hidden"), 4200);
  }

  function setText(el, text) {
    if (el) el.textContent = text;
  }

  function setBadge(el, label, kind, title) {
    if (!el) return;
    el.classList.remove("pill-ok", "pill-warn", "pill-bad", "pill-busy", "pill-idle", "pill-muted");
    el.classList.add(`pill-${kind}`);
    const span = el.querySelector("span");
    if (span) span.textContent = label;
    else el.textContent = label;
    el.title = title || label;
  }

  async function api(path, options = {}) {
    const method = (options.method || "GET").toUpperCase();
    const headers = {};
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    if (method !== "GET") headers["X-Owlet"] = "1";

    let response;
    try {
      response = await fetch(path, {
        method,
        headers,
        body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
        cache: "no-store",
      });
    } catch (error) {
      throw new Error(
        "Cannot reach the local Owlet server. Is `python main.py` still running?"
      );
    }

    const type = response.headers.get("content-type") || "";
    let payload = null;
    if (type.includes("application/json")) {
      payload = await response.json().catch(() => null);
    } else {
      payload = await response.text().catch(() => "");
    }

    if (!response.ok) {
      const detail =
        (payload && (payload.detail || payload.error || payload.message)) ||
        (typeof payload === "string" && payload) ||
        `${response.status} ${response.statusText}`;
      const error = new Error(String(detail));
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  async function withLoading(button, task) {
    if (!button) return task();
    if (button.dataset.loading === "1") return undefined;
    button.dataset.loading = "1";
    button.classList.add("loading");
    button.disabled = true;
    try {
      return await task();
    } finally {
      button.dataset.loading = "";
      button.classList.remove("loading");
      button.disabled = false;
    }
  }

  // ---------- safe markdown ----------

  function safeHref(raw) {
    const value = String(raw || "").replace(/&amp;/g, "&");
    try {
      const url = new URL(value);
      if (url.protocol !== "http:" && url.protocol !== "https:") return null;
      return escapeAttr(url.href);
    } catch (_) {
      return null;
    }
  }

  function inlineMarkdown(text) {
    const codes = [];
    let out = text.replace(/`([^`\n]+)`/g, (_, code) => {
      codes.push(`<code>${code}</code>`);
      return `\u0000${codes.length - 1}\u0000`;
    });

    // Models sometimes emit escaped markers (\*\*bold\*\*) or names with
    // underscores. Unescape the punctuation so the intent renders instead of
    // showing literal backslashes.
    out = out.replace(/\\([\\`*_{}\[\]()#+\-.!>~|])/g, "$1");

    out = out.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/__([^_\n]+)__/g, "<strong>$1</strong>");
    out = out.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
    out = out.replace(/(^|[^\w_])_([^_\n]+)_(?![\w_])/g, "$1<em>$2</em>");
    out = out.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");

    out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (match, label, url) => {
      const href = safeHref(url);
      if (!href) return match;
      return `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`;
    });

    out = out.replace(/\u0000(\d+)\u0000/g, (_, index) => codes[Number(index)] || "");
    return out;
  }

  // Small, dependency-free syntax highlighter for fenced code blocks. It runs on
  // the raw text and escapes every token, so model output can never inject HTML.
  const CODE_KEYWORDS =
    "abstract|async|await|break|case|catch|class|const|continue|def|default|del|elif|else|" +
    "except|export|extends|False|false|finally|float|for|from|function|global|if|implements|" +
    "import|in|int|interface|is|lambda|let|match|new|None|none|null|of|or|pass|private|" +
    "protected|public|raise|return|self|static|str|switch|throw|True|true|try|type|typeof|" +
    "undefined|var|void|while|with|yield";

  const CODE_TOKEN = new RegExp(
    [
      "(\\/\\/[^\\n]*|#[^\\n]*|\\/\\*[\\s\\S]*?\\*\\/)",
      "(\"(?:[^\"\\\\\\n]|\\\\.)*\"|'(?:[^'\\\\\\n]|\\\\.)*'|`(?:[^`\\\\]|\\\\.)*`)",
      "(\\b(?:0x[0-9a-fA-F]+|\\d+(?:\\.\\d+)?)\\b)",
      `(\\b(?:${CODE_KEYWORDS})\\b)`,
    ].join("|"),
    "g"
  );

  function highlightCode(raw) {
    const text = String(raw ?? "");
    let out = "";
    let last = 0;
    CODE_TOKEN.lastIndex = 0;
    let match = CODE_TOKEN.exec(text);
    while (match) {
      out += escapeHtml(text.slice(last, match.index));
      const kind = match[1]
        ? "comment"
        : match[2]
          ? "string"
          : match[3]
            ? "number"
            : "keyword";
      out += `<span class="tok-${kind}">${escapeHtml(match[0])}</span>`;
      last = match.index + match[0].length;
      match = CODE_TOKEN.exec(text);
    }
    out += escapeHtml(text.slice(last));
    return out;
  }

  function renderCodeBlock(raw, lang) {
    const cls = lang ? ` class="language-${escapeAttr(lang)}"` : "";
    return `<pre><code${cls}>${highlightCode(raw)}</code></pre>`;
  }

  // A compact block parser. It is deliberately dependency-free and runs
  // safely on partial text, so an unfinished response can be rendered on every
  // animation frame without waiting for the closing markers.
  const LIST_ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
  const TABLE_SEPARATOR = /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?\s*$/;

  function tableCells(line) {
    let text = String(line || "").trim();
    if (text.startsWith("|")) text = text.slice(1);
    if (text.endsWith("|")) text = text.slice(0, -1);
    const cells = [];
    let current = "";
    for (let i = 0; i < text.length; i += 1) {
      const char = text[i];
      if (char === "\\" && text[i + 1] === "|") {
        current += "|";
        i += 1;
        continue;
      }
      if (char === "|") {
        cells.push(current.trim());
        current = "";
        continue;
      }
      current += char;
    }
    cells.push(current.trim());
    return cells;
  }

  function tableAlignment(cell) {
    const left = cell.startsWith(":");
    const right = cell.endsWith(":");
    if (left && right) return "center";
    if (right) return "right";
    if (left) return "left";
    return "";
  }

  function renderTable(lines, start) {
    const header = tableCells(lines[start]);
    const alignments = tableCells(lines[start + 1]).map(tableAlignment);
    let i = start + 2;
    const rows = [];
    while (i < lines.length && lines[i].trim() && lines[i].includes("|")) {
      rows.push(tableCells(lines[i]));
      i += 1;
    }

    const alignClass = (index) =>
      alignments[index] ? ` class="ta-${alignments[index]}"` : "";
    const head = header
      .map((cell, index) => `<th${alignClass(index)}>${inlineMarkdown(cell)}</th>`)
      .join("");
    const body = rows
      .map(
        (row) =>
          `<tr>${header
            .map(
              (_cell, index) =>
                `<td${alignClass(index)}>${inlineMarkdown(row[index] || "")}</td>`
            )
            .join("")}</tr>`
      )
      .join("");

    return {
      html: `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`,
      next: i,
    };
  }

  function renderListBlock(lines, start) {
    const items = [];
    let i = start;
    let previous = null;

    while (i < lines.length) {
      const line = lines[i];
      const match = line.match(LIST_ITEM);
      if (match) {
        const indent = match[1].replace(/\t/g, "  ").length;
        previous = {
          indent,
          ordered: /^\d/.test(match[2]),
          text: match[3].trim(),
        };
        items.push(previous);
        i += 1;
        continue;
      }
      if (!line.trim()) break;
      // Indented continuation text belongs to the item above it.
      if (
        previous &&
        /^\s{2,}/.test(line) &&
        !/^\s*(```+|~~~+|>|#{1,6}\s)/.test(line) &&
        !TABLE_SEPARATOR.test(line)
      ) {
        previous.text += ` ${line.trim()}`;
        i += 1;
        continue;
      }
      break;
    }

    function build(from, indent) {
      if (from >= items.length) return { html: "", next: from };
      const type = items[from].ordered ? "ol" : "ul";
      let out = `<${type}>`;
      let cursor = from;

      while (cursor < items.length && items[cursor].indent === indent) {
        if ((items[cursor].ordered ? "ol" : "ul") !== type) break;
        out += `<li>${inlineMarkdown(items[cursor].text)}`;
        cursor += 1;
        while (cursor < items.length && items[cursor].indent > indent) {
          const child = build(cursor, items[cursor].indent);
          if (!child.html || child.next <= cursor) break;
          out += child.html;
          cursor = child.next;
        }
        out += "</li>";
      }

      out += `</${type}>`;
      return { html: out, next: cursor };
    }

    let html = "";
    let cursor = 0;
    while (cursor < items.length) {
      const part = build(cursor, items[cursor].indent);
      if (!part.html || part.next <= cursor) break;
      html += part.html;
      cursor = part.next;
    }
    return { html, next: i };
  }

  function renderMarkdown(source) {
    const normalized = String(source ?? "").replace(/\r\n?/g, "\n");
    const rawLines = normalized.split("\n");
    const lines = escapeHtml(normalized).split("\n");
    const html = [];
    let paragraph = [];

    function flushParagraph() {
      if (!paragraph.length) return;
      html.push(`<p>${inlineMarkdown(paragraph.join(" "))}</p>`);
      paragraph = [];
    }

    let i = 0;
    while (i < lines.length) {
      const line = lines[i];

      const fence = line.match(/^\s*(`{3,}|~{3,})\s*(.*)$/);
      if (fence) {
        flushParagraph();
        const marker = fence[1][0];
        const lang = fence[2].trim().replace(/[^a-zA-Z0-9+#.-]/g, "");
        const closeFence = marker === "`" ? /^\s*`{3,}\s*$/ : /^\s*~{3,}\s*$/;
        const codeLines = [];
        i += 1;
        while (i < lines.length && !closeFence.test(lines[i])) {
          codeLines.push(rawLines[i]);
          i += 1;
        }
        i += 1; // consume the closing fence (or the end of the text)
        html.push(renderCodeBlock(codeLines.join("\n"), lang));
        continue;
      }

      if (!line.trim()) {
        flushParagraph();
        i += 1;
        continue;
      }

      const heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        flushParagraph();
        const level =
          heading[1].length === 1 ? "h3" : heading[1].length === 2 ? "h4" : "h5";
        html.push(`<${level}>${inlineMarkdown(heading[2].trim())}</${level}>`);
        i += 1;
        continue;
      }

      if (/^\s*([-*_])\s*(?:\1\s*){2,}$/.test(line)) {
        flushParagraph();
        html.push("<hr>");
        i += 1;
        continue;
      }

      if (rawLines[i] && /^>\s?/.test(rawLines[i])) {
        flushParagraph();
        const quote = [];
        while (i < rawLines.length && /^>\s?/.test(rawLines[i])) {
          quote.push(rawLines[i].replace(/^>\s?/, ""));
          i += 1;
        }
        html.push(`<blockquote>${renderMarkdown(quote.join("\n"))}</blockquote>`);
        continue;
      }

      if (line.includes("|") && i + 1 < lines.length && TABLE_SEPARATOR.test(lines[i + 1])) {
        flushParagraph();
        const table = renderTable(lines, i);
        html.push(table.html);
        i = table.next;
        continue;
      }

      if (LIST_ITEM.test(line)) {
        flushParagraph();
        const list = renderListBlock(lines, i);
        html.push(list.html);
        i = list.next;
        continue;
      }

      paragraph.push(line.trim());
      i += 1;
    }

    flushParagraph();
    return html.join("");
  }

  // ---------- chat rendering ----------

  const IMAGE_ONLY_TEXT = "Please look at the attached image and tell me what matters.";

  function nearBottom() {
    if (!els.messages) return true;
    const el = els.messages;
    return el.scrollHeight - el.scrollTop - el.clientHeight < 140;
  }

  function updateScrollButton() {
    if (!els.messages || !els.scrollBtn) return;
    const el = els.messages;
    const overflow = el.scrollHeight - el.clientHeight > 60;
    els.scrollBtn.classList.toggle("hidden", !overflow || nearBottom());
  }

  function scrollToBottom(force) {
    if (!els.messages) return;
    if (force || nearBottom()) {
      els.messages.scrollTop = els.messages.scrollHeight;
    }
    updateScrollButton();
  }

  function removeWelcome() {
    const welcome = els.messages?.querySelector(".welcome");
    if (welcome) welcome.remove();
  }

  function restoreWelcome() {
    if (!els.messages) return;
    resetStream();
    els.messages.innerHTML = welcomeMarkup;
    els.messages.scrollTop = 0;
    state.lastUserMessageEl = null;
    state.lastAssistantEl = null;
    renderSuggestions();
    updateScrollButton();
  }

  function formatMessageTime(value) {
    if (!value) return "";
    const date = new Date(String(value).replace(" ", "T"));
    if (Number.isNaN(date.getTime())) return "";
    const sameDay = date.toDateString() === new Date().toDateString();
    return date.toLocaleString([], {
      month: sameDay ? undefined : "short",
      day: sameDay ? undefined : "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function messageImages(images) {
    const usable = (Array.isArray(images) ? images : []).filter(
      (image) => image && image.url
    );
    if (!usable.length) return null;

    const grid = document.createElement("div");
    grid.className = "msg-images";
    usable.forEach((image) => {
      const link = document.createElement("a");
      link.href = image.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.title = image.name || "Attached image";
      const img = document.createElement("img");
      img.src = image.url;
      img.alt = image.name || "Attached image";
      img.loading = "lazy";
      link.appendChild(img);
      grid.appendChild(link);
    });
    return grid;
  }

  function messageActions(msg, options = {}) {
    const main = msg?.querySelector(".msg-main");
    if (!main) return null;
    let row = main.querySelector(".msg-actions");
    if (!row) {
      row = document.createElement("div");
      row.className = "msg-actions";
      main.appendChild(row);
    }

    // Idempotent: a repeated final event must not grow a second Copy button on
    // a message that already has one.
    const addAction = (act, label, title) => {
      if (row.querySelector(`.msg-action[data-act="${act}"]`)) return;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "msg-action";
      button.dataset.act = act;
      button.textContent = label;
      if (title) button.title = title;
      row.appendChild(button);
    };

    if (options.copy) addAction("copy", "Copy");
    if (options.regenerate) {
      addAction(
        "regenerate",
        "Regenerate",
        "Ask the model to answer the last question again"
      );
    }
    row.classList.toggle("hidden", !row.children.length);
    return row;
  }

  function clearRegenerateActions() {
    els.messages?.querySelectorAll('.msg-action[data-act="regenerate"]').forEach((button) => {
      const row = button.closest(".msg-actions");
      button.remove();
      if (row && !row.children.length) row.classList.add("hidden");
    });
  }

  function ensureMessageMeta(msg, createdAt) {
    if (!msg) return null;
    const main = msg.querySelector(".msg-main");
    if (!main) return null;
    let meta = main.querySelector(".msg-meta");
    if (!meta) {
      meta = document.createElement("div");
      meta.className = "msg-meta";
      main.appendChild(meta);
    }
    if (!meta.querySelector(".msg-time") && createdAt) {
      const time = document.createElement("span");
      time.className = "msg-time";
      time.textContent = formatMessageTime(createdAt);
      time.title = new Date(String(createdAt).replace(" ", "T")).toLocaleString();
      meta.prepend(time);
    }
    return meta;
  }

  let messageSeq = 0;

  function newMessageId() {
    messageSeq += 1;
    return `m-${Date.now().toString(36)}-${messageSeq}`;
  }

  function messageById(id) {
    if (!id || !els.messages) return null;
    return (
      Array.from(els.messages.querySelectorAll(".msg[data-message-id]")).find(
        (msg) => msg.dataset.messageId === id
      ) || null
    );
  }

  // Every assistant bubble owns exactly one body element. Streaming re-renders
  // that same element; it must never append a second copy of the answer.
  function ensureBubbleBody(bubble) {
    if (!bubble) return null;
    let body = bubble.querySelector(".bubble-text");
    if (!body) {
      body = document.createElement("div");
      body.className = "bubble-text";
    }
    Array.from(bubble.childNodes).forEach((node) => {
      if (node === body) return;
      // A bare text node is what the old streaming path left behind next to the
      // rendered body, which displayed every answer twice.
      if (node.nodeType === Node.TEXT_NODE) node.remove();
    });
    bubble.querySelectorAll(".bubble-text").forEach((candidate) => {
      if (candidate !== body) candidate.remove();
    });
    if (!body.parentNode) bubble.appendChild(body);
    return body;
  }

  function setAssistantBody(msg, text) {
    if (!msg) return null;
    const bubble = msg.querySelector(".bubble");
    if (!bubble) return null;
    bubble.classList.remove("streaming");
    const body = ensureBubbleBody(bubble);
    body.innerHTML = renderMarkdown(text);
    msg.dataset.rawText = String(text ?? "");
    state.lastAssistantEl = msg;
    ensureMessageMeta(msg, new Date().toISOString());
    clearRegenerateActions();
    messageActions(msg, { copy: true, regenerate: true });
    return body;
  }

  // True only while the same turn is still on screen: a newer user message
  // means a new question, so an identical answer is legitimate there.
  function duplicatedAssistant(text, exclude) {
    let last = state.lastAssistantEl;
    if (last === exclude) {
      let node = last?.previousElementSibling || null;
      last = null;
      while (node) {
        if (node.classList?.contains("msg")) {
          if (node.classList.contains("user")) return null;
          if (node.classList.contains("assistant")) {
            last = node;
            break;
          }
        }
        node = node.previousElementSibling;
      }
    }
    if (!last || !els.messages?.contains(last)) return null;
    let node = last.nextElementSibling;
    while (node) {
      if (node.classList?.contains("msg") && node.classList.contains("user")) return null;
      node = node.nextElementSibling;
    }
    return last.dataset.rawText === String(text) ? last : null;
  }

  function resetStream() {
    const stream = state.stream;
    if (stream) {
      if (stream.frame) window.cancelAnimationFrame(stream.frame);
      stream.frame = 0;
      stream.done = true;
    }
    state.stream = null;
  }

  function createMessage(role, text, options = {}) {
    const msg = document.createElement("div");
    msg.className = `msg ${role}`;
    msg.dataset.messageId = options.messageId || newMessageId();
    if (role === "assistant" && !options.error && !options.notice) {
      msg.dataset.rawText = String(text ?? "");
    }

    if (role === "assistant") {
      const avatar = document.createElement("div");
      avatar.className = "msg-avatar";
      avatar.setAttribute("aria-hidden", "true");
      avatar.textContent = "M";
      msg.appendChild(avatar);
    }

    const main = document.createElement("div");
    main.className = "msg-main";

    const author = document.createElement("div");
    author.className = "msg-author";
    author.textContent =
      role === "user" ? "You" : options.error ? "Owlet · error" : "Owlet";
    main.appendChild(author);

    const bubble = document.createElement("div");
    bubble.className = "bubble";
    if (options.error) bubble.classList.add("error");
    if (options.notice) bubble.classList.add("notice");
    if (options.streaming) bubble.classList.add("streaming");

    const images = messageImages(options.images);
    if (images) bubble.appendChild(images);

    if (text) {
      if (options.plain) {
        const p = document.createElement("div");
        p.className = "bubble-text";
        p.textContent = text;
        bubble.appendChild(p);
      } else {
        const body = document.createElement("div");
        body.className = "bubble-text";
        body.innerHTML = renderMarkdown(text);
        bubble.appendChild(body);
      }
    }

    main.appendChild(bubble);
    ensureMessageMeta(msg, options.createdAt || new Date().toISOString());
    if (role === "assistant" && options.actions) messageActions(msg, options.actions);
    msg.appendChild(main);
    els.messages.appendChild(msg);
    scrollToBottom(true);
    return { msg, bubble };
  }

  function appendUser(text, images, createdAt, messageId) {
    removeWelcome();
    const shown = String(text ?? "");
    const { msg } = createMessage("user", shown, {
      plain: true,
      images,
      createdAt: createdAt || new Date().toISOString(),
      messageId,
    });
    state.lastUserMessageEl = msg;
    state.lastUserText = shown;
    state.conversationStarted = true;
    return msg;
  }

  function appendAssistant(text, options = {}) {
    removeWelcome();
    const streaming = Boolean(options.streaming);
    const actions = streaming
      ? null
      : options.actions ||
        (options.error || options.notice
          ? { copy: true }
          : { copy: true, regenerate: Boolean(options.regenerate) });
    const { msg, bubble } = createMessage("assistant", text, {
      ...options,
      actions,
      createdAt: options.createdAt || new Date().toISOString(),
    });
    if (options.regenerate) {
      // Exactly one Regenerate target: the newest answer.
      els.messages
        ?.querySelectorAll('.msg-action[data-act="regenerate"]')
        .forEach((button) => {
          if (!msg.contains(button)) button.remove();
        });
    }
    if (!streaming) state.lastAssistantEl = msg;
    return { msg, bubble };
  }

  function appendError(text) {
    appendAssistant(text, { error: true, plain: true });
  }

  function appendNotice(text) {
    appendAssistant(text, { notice: true });
  }

  function showWaiting(label) {
    hideWaiting();
    if (!els.messages) return;
    removeWelcome();

    const msg = document.createElement("div");
    msg.className = "msg assistant waiting";

    const avatar = document.createElement("div");
    avatar.className = "msg-avatar";
    avatar.setAttribute("aria-hidden", "true");
    avatar.textContent = "M";

    const main = document.createElement("div");
    main.className = "msg-main";

    const author = document.createElement("div");
    author.className = "msg-author";
    author.textContent = "Owlet";

    const bubble = document.createElement("div");
    bubble.className = "bubble thinking";
    bubble.innerHTML =
      '<span class="thinking-dots" aria-hidden="true"><i></i><i></i><i></i></span><span class="thinking-label"></span>';
    const text = bubble.querySelector(".thinking-label");
    if (text) text.textContent = label || `Thinking with ${modelName()}…`;

    main.appendChild(author);
    main.appendChild(bubble);
    msg.appendChild(avatar);
    msg.appendChild(main);
    els.messages.appendChild(msg);
    state.waitingEl = msg;
    scrollToBottom(true);
  }

  function hideWaiting() {
    if (state.waitingEl) {
      state.waitingEl.remove();
      state.waitingEl = null;
    }
  }

  function startStreaming(messageId) {
    hideWaiting();
    removeWelcome();
    const { msg, bubble } = createMessage("assistant", "", {
      streaming: true,
      messageId: messageId || newMessageId(),
    });
    const body = ensureBubbleBody(bubble);
    const stream = {
      id: msg.dataset.messageId,
      msg,
      bubble,
      body,
      text: "",
      frame: 0,
      done: false,
    };
    state.stream = stream;
    state.lastAssistantEl = msg;
    scrollToBottom(true);
    return stream;
  }

  function scheduleStreamPreview(stream) {
    if (!stream || stream.done || stream.frame) return;
    stream.frame = window.requestAnimationFrame(() => {
      stream.frame = 0;
      if (!stream.done) stream.body.innerHTML = renderMarkdown(stream.text);
    });
  }

  function appendAssistantDelta(chunk, messageId) {
    const text = String(chunk ?? "");
    if (!text) return;

    let stream = state.stream;
    if (stream && stream.done) stream = null;

    if (!stream) {
      // A replay of a message that already finished must not open a new bubble.
      if (messageId && messageById(messageId)) return;
      stream = startStreaming(messageId);
    } else if (messageId && stream.id !== messageId) {
      // A new model round began without a final event for the previous one.
      finalizeStreaming(undefined, { messageId: stream.id });
      stream = state.stream;
      if (messageId && messageById(messageId)) return;
      if (!stream) stream = startStreaming(messageId);
    }

    stream.text += text;
    scheduleStreamPreview(stream);
    scrollToBottom(false);
  }

  function upsertAssistantMessage(messageId, text, options = {}) {
    const existing =
      (messageId ? messageById(messageId) : null) || duplicatedAssistant(text);
    if (existing) {
      // A repeated final event (same id) or the same answer arriving again in
      // one turn is an update, never a second message with the same content.
      setAssistantBody(existing, text);
      return existing;
    }
    removeWelcome();
    const { msg } = appendAssistant(text, {
      ...options,
      messageId: messageId || newMessageId(),
      regenerate: options.regenerate !== false,
    });
    return msg;
  }

  function finalizeStreaming(finalText, options = {}) {
    hideWaiting();
    const stream = state.stream;
    const text =
      finalText === undefined || finalText === null
        ? stream
          ? stream.text
          : ""
        : String(finalText);

    if (!stream) {
      // Nothing is streaming: an event without deltas still creates exactly one
      // message, and a repeat updates that same message.
      if (!text.trim()) return null;
      return upsertAssistantMessage(options.messageId, text, options);
    }

    if (stream.frame) {
      window.cancelAnimationFrame(stream.frame);
      stream.frame = 0;
    }
    stream.done = true;
    state.stream = null;

    const msg = stream.msg;
    const content = text.trim() ? text : stream.text;

    if (!content.trim()) {
      msg.remove();
      if (state.lastAssistantEl === msg) state.lastAssistantEl = null;
      scrollToBottom(false);
      return null;
    }

    // If this round repeats the answer already on screen in the same turn,
    // keep the existing bubble and drop the duplicate instead of showing it
    // twice.
    const duplicate = duplicatedAssistant(content, msg);
    if (duplicate) {
      msg.remove();
      state.lastAssistantEl = duplicate;
      setAssistantBody(duplicate, content);
      scrollToBottom(false);
      return duplicate;
    }

    // Finalise the same element that streamed; no second body is ever added.
    setAssistantBody(msg, content);
    scrollToBottom(false);
    return msg;
  }

  function addMessageMeta(msg, text) {
    if (!msg || !text) return;
    const meta = ensureMessageMeta(msg, null);
    if (!meta) return;
    let stats = meta.querySelector(".msg-stats");
    if (!stats) {
      stats = document.createElement("span");
      stats.className = "msg-stats";
      meta.appendChild(stats);
    }
    stats.textContent = text;
  }

  function appendToolRow(name, args) {
    hideWaiting();
    const row = document.createElement("div");
    row.className = "tool-row";

    const status = document.createElement("span");
    status.className = "tool-state";
    const spinner = document.createElement("span");
    spinner.className = "tool-spinner";
    status.appendChild(spinner);

    const label = document.createElement("span");
    label.className = "tool-name";
    label.textContent = name;

    row.appendChild(status);
    row.appendChild(label);

    if (args && typeof args === "object" && Object.keys(args).length) {
      row.title = truncate(JSON.stringify(args), 500);
    }

    els.messages.appendChild(row);
    state.pendingTools.push(row);
    scrollToBottom(true);
    return row;
  }

  function finishToolRow(payload, message) {
    const row = state.pendingTools.pop();
    if (!row) return;

    const ok = !(payload && payload.ok === false);
    row.classList.add(ok ? "ok" : "fail");

    const status = row.querySelector(".tool-state");
    if (status) status.textContent = ok ? "✓" : "✕";

    const text = message || (payload && (payload.message || payload.error)) || (ok ? "Done" : "Failed");
    if (text) {
      const detail = document.createElement("span");
      detail.className = "tool-msg";
      detail.textContent = text;
      detail.title = text;
      row.appendChild(detail);
    }
    scrollToBottom(false);
  }

  function setRunning(running) {
    state.running = Boolean(running);
    if (els.stopBtn) els.stopBtn.disabled = !state.running;
    if (els.messageInput) {
      els.messageInput.placeholder = state.running
        ? "The agent is working… press Stop to interrupt"
        : "Ask Owlet… (attach an image with the paperclip)";
    }
    updateComposerState();
  }

  function setAgentStatus(status, message) {
    const map = {
      idle: { label: "Idle", kind: "idle", title: "Agent idle" },
      running: { label: "Working", kind: "busy", title: "Agent is working" },
      waiting_approval: { label: "Approval", kind: "warn", title: "Waiting for your approval" },
      stopping: { label: "Stopping", kind: "warn", title: "Stopping the agent" },
    };
    const info = map[status] || map.idle;
    setBadge(els.agentBadge, info.label, info.kind, info.title);

    if (message) setText(els.agentActivity, message);
    else setText(els.agentActivity, info.label);

    if (status === "idle") setRunning(false);
    else if (status === "running" || status === "waiting_approval" || status === "stopping") {
      setRunning(true);
    }
  }

  // ---------- courses & assignments ----------

  function fillComposer(text) {
    if (!els.messageInput) return;
    els.messageInput.value = text;
    autosize();
    els.messageInput.focus();
    showToast("Prompt ready — press Send.");
  }

  // ---------- collapsible cards ----------

  // Per-viewer convenience only: a blocked or empty storage just means every
  // card starts in its default state.
  const CARDS_KEY = "owlet-collapsed";
  const DEFAULT_COLLAPSED = { activity: true };

  function loadCardPrefs() {
    try {
      const parsed = JSON.parse(localStorage.getItem(CARDS_KEY) || "{}");
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (_) {
      return {};
    }
  }

  const cardPrefs = loadCardPrefs();

  function setCardCollapsed(card, collapsed) {
    card.classList.toggle("collapsed", collapsed);
    const toggle = card.querySelector(".card-toggle");
    if (toggle) {
      toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
      toggle.title = collapsed ? "Expand" : "Collapse";
    }
  }

  function initCards() {
    document.querySelectorAll(".card[data-card]").forEach((card) => {
      const key = card.dataset.card;
      const saved = cardPrefs[key];
      setCardCollapsed(card, typeof saved === "boolean" ? saved : Boolean(DEFAULT_COLLAPSED[key]));
      card.querySelector(".card-toggle")?.addEventListener("click", () => {
        const collapsed = !card.classList.contains("collapsed");
        setCardCollapsed(card, collapsed);
        cardPrefs[key] = collapsed;
        try {
          localStorage.setItem(CARDS_KEY, JSON.stringify(cardPrefs));
        } catch (_) {
          /* ignore */
        }
      });
    });
  }

  // Once logged in, the connection form is just noise. Fold it away unless the
  // user has chosen a state for that card themselves.
  function autoCollapseConnection(loggedIn) {
    if (typeof cardPrefs.connection === "boolean") return;
    const card = document.querySelector('.card[data-card="connection"]');
    if (card) setCardCollapsed(card, loggedIn === true);
  }

  function setCount(el, value) {
    if (!el) return;
    el.textContent = value ? String(value) : "";
  }

  // ---------- suggestions (built from the synced data) ----------

  function suggestionButton(label, prompt, title) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "suggestion";
    button.dataset.prompt = prompt;
    button.textContent = label;
    button.title = title || prompt;
    return button;
  }

  function quoted(name, max) {
    return `“${truncate(String(name || ""), max)}”`;
  }

  // "Artificial Intelligence Lab(BS-CYS-5A)" → "Artificial Intelligence Lab":
  // section codes make every label start the same and truncate identically.
  function shortCourse(name) {
    const text = String(name || "").trim();
    return text.replace(/\s*\([^()]*\)\s*$/, "").trim() || text;
  }

  function renderSuggestions() {
    const holder = els.messages?.querySelector("#suggestions");
    if (!holder) return;
    holder.replaceChildren();

    const pending = (state.assignments || []).filter((row) => row.pending);
    const courses = state.courses || [];
    const items = [];

    if (pending.length) {
      const lines = pending.slice(0, 12).map((row) => {
        const due = row.due_text ? ` — due ${row.due_text}` : "";
        const course = row.course_name ? ` (${row.course_name})` : "";
        return `- ${row.name}${course}${due}`;
      });
      items.push([
        "What should I do first?",
        `These are my pending Moodle assignments:\n${lines.join("\n")}\n\nWhich should I work on first, and why? Keep it short.`,
        "Prioritise your pending assignments",
      ]);

      const next = pending[0];
      const inCourse = next.course_name ? ` in "${next.course_name}"` : "";
      items.push([
        `Explain ${quoted(next.name, 26)}`,
        `Open assignment "${next.name}"${inCourse} and explain what I have to do, step by step, in simple language.`,
      ]);
      items.push([
        `Plan ${quoted(next.name, 26)}`,
        `Open assignment "${next.name}"${inCourse}, read its attached files, and give me a plan with steps to finish it before the deadline.`,
      ]);
    }

    const [first, second] = courses;
    if (first) {
      items.push([
        `Summarise ${quoted(shortCourse(first.name), 28)}`,
        `Open my "${first.name}" course and summarise it: the topics, the materials posted, and any upcoming work.`,
      ]);
    }
    if (second && !pending.length) {
      items.push([
        `Study materials in ${quoted(shortCourse(second.name), 24)}`,
        `Open my "${second.name}" course and list the lecture slides and files I should study first, newest first.`,
      ]);
    }
    if (courses.length > 1 && !pending.length) {
      items.push([
        "Any new announcements?",
        "Check my Moodle dashboard for recent announcements or course updates and summarise anything important.",
      ]);
    }

    if (!items.length) {
      items.push([
        "List my courses",
        "List my Moodle courses.",
        "Works once you are logged in to Moodle in Chrome",
      ]);
      items.push([
        "What can you help with?",
        "In two or three sentences, what can you help me with in my Moodle courses?",
      ]);
    }

    items.slice(0, 4).forEach(([label, prompt, title]) => {
      holder.appendChild(suggestionButton(label, prompt, title));
    });
  }

  function courseItem(course) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "list-item";
    button.title = `Ask the agent to open ${course.name}`;

    const glyph = document.createElement("span");
    glyph.className = "glyph";
    glyph.textContent = (course.name || "?").trim().charAt(0).toUpperCase() || "?";

    const body = document.createElement("span");
    body.className = "body";
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = course.name || "Untitled course";
    body.appendChild(name);

    const sub = document.createElement("span");
    sub.className = "sub";
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = course.moodle_id ? `Moodle #${course.moodle_id}` : "Course";
    sub.appendChild(chip);
    body.appendChild(sub);

    button.appendChild(glyph);
    button.appendChild(body);
    button.addEventListener("click", () => {
      fillComposer(`Open my "${course.name}" course`);
    });
    return button;
  }

  const STATUS_STYLES = {
    graded: "chip chip-ok",
    submitted: "chip chip-ok",
    submitted_for_grading: "chip chip-ok",
    no_submission_required: "chip",
    draft: "chip chip-warn",
    reopened: "chip chip-warn",
    not_submitted: "chip",
    unknown: "chip",
  };

  const URGENCY_RANK = { overdue: 0, urgent: 1, soon: 2, upcoming: 3, no_due_date: 4 };

  function hoursLabel(hours) {
    if (hours === null || hours === undefined) return "";
    const value = Number(hours);
    if (!Number.isFinite(value)) return "";
    const magnitude = Math.abs(value);
    if (magnitude < 1) return "under an hour";
    if (magnitude < 48) return `${Math.round(magnitude)}h`;
    return `${Math.round(magnitude / 24)}d`;
  }

  // The badge is built from the backend's urgency, never from Date parsing the
  // Moodle display text: the stored instant is the only trustworthy input.
  function dueBadge(assignment) {
    const urgency = assignment.urgency || "no_due_date";
    const relative = hoursLabel(assignment.due_in_hours);
    let className = "chip";
    let label = "";

    if (urgency === "overdue") {
      className = "chip chip-danger";
      label = relative ? `Overdue by ${relative}` : "Overdue";
    } else if (urgency === "urgent") {
      className = "chip chip-danger";
      label = relative ? `Due in ${relative}` : "Due soon";
    } else if (urgency === "soon") {
      className = "chip chip-warn";
      label = relative ? `Due in ${relative}` : "Due soon";
    } else if (assignment.due_text) {
      label = truncate(assignment.due_text, 40);
    }

    if (!label) return null;
    const chip = document.createElement("span");
    chip.className = className;
    chip.textContent = label;
    chip.title = assignment.due_text ? `Due: ${assignment.due_text}` : label;
    return chip;
  }

  function statusBadge(assignment) {
    const status = assignment.status || "unknown";
    const chip = document.createElement("span");
    chip.className = STATUS_STYLES[status] || "chip";
    chip.textContent = truncate(
      assignment.status_label || assignment.status_text || "Status unknown",
      40
    );
    chip.title = assignment.status_text || chip.textContent;
    return chip;
  }

  function assignmentItem(assignment) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "list-item";
    button.title = "Ask the agent to open this assignment";

    const glyph = document.createElement("span");
    glyph.className = "glyph";
    glyph.textContent = "✎";

    const body = document.createElement("span");
    body.className = "body";

    const name = document.createElement("span");
    name.className = "name";
    name.textContent = assignment.name || "Untitled assignment";
    body.appendChild(name);

    const sub = document.createElement("span");
    sub.className = "sub sub-chips";

    if (assignment.course_name) {
      const course = document.createElement("span");
      course.textContent = assignment.course_name;
      sub.appendChild(course);
    }

    if (assignment.due_text || assignment.urgency) {
      const due = dueBadge(assignment);
      if (due) sub.appendChild(due);
    }

    sub.appendChild(statusBadge(assignment));

    body.appendChild(sub);
    button.appendChild(glyph);
    button.appendChild(body);

    button.addEventListener("click", () => {
      const inCourse = assignment.course_name ? ` in "${assignment.course_name}"` : "";
      fillComposer(`Open assignment "${assignment.name}"${inCourse}`);
    });

    // A sibling button, not a nested one: buttons cannot contain buttons.
    const row = document.createElement("div");
    row.className = "assignment-row";
    row.appendChild(button);
    if (assignment.id !== undefined && assignment.id !== null) {
      const draft = document.createElement("button");
      draft.type = "button";
      draft.className = "btn btn-ghost small row-action";
      draft.textContent = "Draft";
      draft.title = "Draft this assignment with the configured model, then review it";
      draft.addEventListener("click", () => openDraft(assignment.id));
      row.appendChild(draft);
    }
    return row;
  }

  // ---------- assignment drafts ----------

  const DRAFT_STATUS = {
    none: ["No draft yet", "chip"],
    draft: ["Needs review", "chip chip-warn"],
    changed: ["Changed since review", "chip chip-warn"],
    reviewed: ["Reviewed", "chip chip-ok"],
  };

  function draftFileItem(file, extraClass) {
    const item = document.createElement("li");
    item.className = `draft-file${extraClass ? ` ${extraClass}` : ""}`;
    const link = document.createElement("a");
    link.href = file.download_url;
    link.textContent = file.name || file.path;
    link.title = file.path;
    link.setAttribute("download", "");
    item.appendChild(link);
    const meta = document.createElement("span");
    meta.className = "draft-file-meta";
    const bits = [];
    if (file.folder) bits.push(`${file.folder}/`);
    if (file.size !== undefined) bits.push(formatBytes(file.size));
    if (file.changed) bits.push("changed");
    meta.textContent = bits.join(" · ");
    item.appendChild(meta);
    return item;
  }

  function renderDraft(data) {
    state.draft = data;
    const info = data?.assignment || {};
    setText(els.draftHeading, info.name || "Assignment draft");
    const metaParts = [];
    if (info.course_name) metaParts.push(info.course_name);
    if (info.due_text) metaParts.push(`Due ${info.due_text}`);
    setText(els.draftMeta, metaParts.join(" · "));

    const [label, className] = DRAFT_STATUS[data?.status] || DRAFT_STATUS.none;
    if (els.draftStatus) {
      els.draftStatus.className = className;
      els.draftStatus.textContent = label;
    }

    const files = Array.isArray(data?.files) ? data.files : [];
    let hint = "";
    if (state.running) {
      hint = "The agent is working. Files appear here when the run finishes.";
    } else if (data?.status === "none") {
      hint = "Draft with AI reads the saved instructions and teacher files, then writes every deliverable into this assignment's folder.";
    } else if (data?.status === "changed") {
      hint = "Files changed after your last review. Check them, then mark the draft reviewed again.";
    } else if (data?.status === "reviewed") {
      hint = `Reviewed ${data.reviewed_at ? new Date(data.reviewed_at).toLocaleString() : ""}. Upload these files in Moodle yourself.`;
    } else {
      hint = "Open each file and check it against REVIEW.md before marking it reviewed.";
    }
    setText(els.draftHint, hint);

    if (els.draftFiles) {
      els.draftFiles.replaceChildren();
      if (data?.review_note) {
        els.draftFiles.appendChild(
          draftFileItem(
            {
              name: "REVIEW.md",
              path: data.review_note,
              folder: "explanations",
              download_url: `/api/files/download?root=workspace&path=${encodeURIComponent(data.review_note)}`,
            },
            "draft-note"
          )
        );
      }
      files.forEach((file) => els.draftFiles.appendChild(draftFileItem(file)));
      els.draftFiles.classList.toggle("hidden", !files.length && !data?.review_note);
    }

    if (els.draftStartBtn) {
      els.draftStartBtn.textContent = files.length ? "Redraft" : "Draft with AI";
      els.draftStartBtn.disabled = state.running;
    }
    if (els.draftReviewBtn) {
      els.draftReviewBtn.disabled = state.running || !files.length || data?.status === "reviewed";
    }
  }

  async function loadDraft(assignmentId) {
    const data = await api(`/api/assignments/${encodeURIComponent(assignmentId)}/draft`);
    if (state.draftId === assignmentId) renderDraft(data);
    return data;
  }

  async function openDraft(assignmentId) {
    state.draftId = assignmentId;
    renderDraft({ assignment: (state.assignments || []).find((row) => row.id === assignmentId), status: "none", files: [] });
    els.draftModal?.classList.remove("hidden");
    try {
      await loadDraft(assignmentId);
    } catch (error) {
      showToast(error.message, "bad");
    }
  }

  function closeDraft() {
    state.draftId = null;
    els.draftModal?.classList.add("hidden");
  }

  async function startDraft() {
    const assignmentId = state.draftId;
    if (assignmentId === null || state.running) return;
    const hasFiles = Boolean(state.draft?.files?.length);
    if (
      hasFiles &&
      !window.confirm("Redraft this assignment? Existing files are only replaced if you approve each overwrite.")
    ) {
      return;
    }
    const name = state.draft?.assignment?.name || "assignment";
    await withLoading(els.draftStartBtn, async () => {
      // The run is an ordinary agent conversation shown in the chat. Clear the
      // chat before the request so the run's first live event is not wiped.
      restoreWelcome();
      state.lastAssistantEl = null;
      state.lastUserText = "";
      try {
        const response = await api(`/api/assignments/${encodeURIComponent(assignmentId)}/draft`, {
          method: "POST",
          body: {},
        });
        state.conversationId = response.conversation_id;
        state.conversationStarted = true;
        setText(els.chatTitle, truncate(`Draft: ${name}`, 70));
        setText(els.chatSubtitle, `Drafting · ${modelName()} · nothing is submitted`);
        setRunning(true);
        closeDraft();
        showToast("Drafting started. Approve any overwrite the agent asks for.");
      } catch (error) {
        showToast(error.message, "bad");
      }
    });
  }

  async function markReviewed() {
    const assignmentId = state.draftId;
    if (assignmentId === null) return;
    let data = null;
    await withLoading(els.draftReviewBtn, async () => {
      try {
        data = await api(`/api/assignments/${encodeURIComponent(assignmentId)}/review`, {
          method: "POST",
          body: {},
        });
        showToast("Marked reviewed. Any later edit will show as changed.", "ok");
      } catch (error) {
        showToast(error.message, "bad");
      }
    });
    // Rendered after withLoading re-enables the button, so "reviewed" can disable it.
    if (data && state.draftId === assignmentId) renderDraft(data);
  }

  function renderCourses(courses) {
    state.courses = Array.isArray(courses) ? courses : [];
    setCount(els.courseCount, state.courses.length);
    renderSuggestions();
    if (!els.courseList) return;
    els.courseList.replaceChildren();
    if (!Array.isArray(courses) || !courses.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "No courses yet. Connect to Moodle and log in to sync them.";
      els.courseList.appendChild(empty);
      return;
    }
    courses.forEach((course) => els.courseList.appendChild(courseItem(course)));
  }

  function renderAssignments(assignments) {
    const rows = Array.isArray(assignments) ? assignments.slice() : [];
    state.assignments = rows;
    setCount(els.assignmentCount, rows.filter((row) => row.pending).length);
    renderSuggestions();
    if (!els.assignmentList) return;
    els.assignmentList.replaceChildren();
    if (!rows.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "Assignments appear here after you log in and the sync runs.";
      els.assignmentList.appendChild(empty);
      return;
    }

    rows.sort((a, b) => {
      const pendingDelta = Number(!a.pending) - Number(!b.pending);
      if (pendingDelta) return pendingDelta;
      const rankA = URGENCY_RANK[a.urgency] ?? 9;
      const rankB = URGENCY_RANK[b.urgency] ?? 9;
      if (rankA !== rankB) return rankA - rankB;
      const hoursA = a.due_in_hours === null || a.due_in_hours === undefined ? 1e9 : Number(a.due_in_hours);
      const hoursB = b.due_in_hours === null || b.due_in_hours === undefined ? 1e9 : Number(b.due_in_hours);
      if (hoursA !== hoursB) return hoursA - hoursB;
      return String(a.name || "").localeCompare(String(b.name || ""));
    });
    const pending = rows.filter((row) => row.pending);
    const rest = rows.filter((row) => !row.pending);

    pending.slice(0, 60).forEach((row) => {
      els.assignmentList.appendChild(assignmentItem(row));
    });

    if (!pending.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "Nothing pending. Nice.";
      els.assignmentList.appendChild(empty);
    }

    // Submitted, graded and unknown-status work stays reachable but folded away.
    if (rest.length) {
      const group = document.createElement("details");
      group.className = "list-group";
      const summary = document.createElement("summary");
      summary.textContent = `${rest.length} submitted or no status`;
      summary.title = "Submitted, graded, or no status reported by Moodle";
      group.appendChild(summary);
      rest.slice(0, 60).forEach((row) => group.appendChild(assignmentItem(row)));
      els.assignmentList.appendChild(group);
    }
  }

  function renderSync(info) {
    const syncing = Boolean(info && (info.running || info.state === "running" || info.phase || info.step));
    if (syncing !== state.syncing) {
      state.syncing = syncing;
      if (state.lastBrowser) updateBrowser(state.lastBrowser);
    }
    if (!els.syncState) return;
    if (!info) {
      setText(els.syncState, "Not synced yet");
      return;
    }
    if (info.phase === "files" && info.assignment) {
      const where = info.step && info.total ? ` · course ${info.step}/${info.total}` : "";
      setText(els.syncState, `Saving ${info.assignment}${where}`);
      return;
    }
    if (info.step && info.total) {
      setText(els.syncState, `Reading ${info.course || "course"} (${info.step}/${info.total})`);
      return;
    }
    const message = info.message || (info.running ? "Syncing…" : "Not synced yet");
    setText(els.syncState, message);
    els.syncState.title = (info.errors && info.errors.length ? info.errors.join("\n") : message) || "";
    els.syncState.classList.toggle("bad", info.state === "failed");
    els.syncState.classList.toggle("ok", info.state === "done");
    els.syncState.classList.toggle("warn", info.state === "running");
  }

  // ---------- files ----------

  function buildDownloadUrl(file) {
    const path = String(file.path || "")
      .split("/")
      .map((segment) => encodeURIComponent(segment))
      .join("/");
    return `/api/files/download?root=${encodeURIComponent(file.root || state.activeTab)}&path=${path}`;
  }

  function fileIcon(name) {
    const ext = String(name).split(".").pop().toLowerCase();
    if (ext === "pdf") return "📕";
    if (["doc", "docx", "odt", "rtf"].includes(ext)) return "📘";
    if (["xls", "xlsx", "csv", "ods"].includes(ext)) return "📊";
    if (["png", "jpg", "jpeg", "gif", "webp", "svg"].includes(ext)) return "🖼";
    if (["zip", "7z", "rar", "tar", "gz"].includes(ext)) return "📦";
    if (["py", "js", "ts", "tsx", "jsx", "java", "c", "cpp", "cs", "go", "rs", "php", "rb", "sh", "html", "css", "json", "xml", "yaml", "yml", "sql"].includes(ext)) {
      return "📜";
    }
    if (["md", "txt", "log"].includes(ext)) return "📝";
    return "📄";
  }

  function hasFiles(node) {
    if (node.type === "file") return true;
    return Array.isArray(node.children) && node.children.some(hasFiles);
  }

  // Course folders (depth 0) always show, so every course has its place. Below
  // that, empty scaffolding folders (code/, pdfs/, submission/…) are hidden
  // until something is saved in them. Folders start closed and remember what
  // the user opened across refreshes.
  function treeFiles(nodes, container, depth = 0) {
    nodes.forEach((node) => {
      if (node.type === "notice") {
        const note = document.createElement("p");
        note.className = "empty";
        note.textContent = node.name || "More files not shown.";
        container.appendChild(note);
        return;
      }

      if (node.type === "dir") {
        if (depth > 0 && !hasFiles(node)) return;
        const key = `${state.activeTab}:${node.path || node.name}`;
        const details = document.createElement("details");
        details.className = "tree-dir";
        details.open = state.openDirs.has(key);
        details.addEventListener("toggle", () => {
          if (details.open) state.openDirs.add(key);
          else state.openDirs.delete(key);
        });

        const summary = document.createElement("summary");
        const folder = document.createElement("span");
        folder.className = "folder";
        folder.textContent = "📁";
        const label = document.createElement("span");
        label.textContent = node.name || "folder";
        summary.appendChild(folder);
        summary.appendChild(label);
        details.appendChild(summary);

        const children = document.createElement("div");
        children.className = "tree-children";
        if (hasFiles(node)) {
          treeFiles(node.children, children, depth + 1);
        } else {
          const empty = document.createElement("p");
          empty.className = "empty";
          empty.textContent = "Empty";
          children.appendChild(empty);
        }
        details.appendChild(children);
        container.appendChild(details);
        return;
      }

      if (node.type === "file") {
        const link = document.createElement("a");
        link.className = "tree-file";
        link.href = buildDownloadUrl(node);
        link.download = node.name || "";
        link.title = `Download ${node.name}`;

        const icon = document.createElement("span");
        icon.className = "file-icon";
        icon.textContent = fileIcon(node.name);

        const name = document.createElement("span");
        name.className = "file-name";
        name.textContent = node.name || "file";

        const size = document.createElement("span");
        size.className = "file-size";
        size.textContent = formatBytes(node.size);

        link.appendChild(icon);
        link.appendChild(name);
        link.appendChild(size);
        container.appendChild(link);
      }
    });
  }

  function renderFiles() {
    if (!els.fileTree) return;
    els.fileTree.replaceChildren();
    const nodes = state.files[state.activeTab] || [];
    const root = document.createElement("div");
    root.className = "tree";

    if (!Array.isArray(nodes) || !nodes.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent =
        state.activeTab === "workspace"
          ? "Course folders appear here after the first sync."
          : "Files made outside any course appear here.";
      els.fileTree.appendChild(empty);
      return;
    }

    treeFiles(nodes, root);
    els.fileTree.appendChild(root);
  }

  // ---------- activity ----------

  async function refreshActivity() {
    try {
      const data = await api("/api/activity");
      renderActivity(data.activity);
    } catch (_) {
      renderActivity([]);
    }
  }

  function activityItem(item) {
    const row = document.createElement("div");
    row.className = "activity-item";
    row.dataset.kind = item.kind || "other";
    row.dataset.level = item.level === "error" ? "error" : "info";

    const time = document.createElement("time");
    time.textContent = formatClock(item.created_at || item.ts);

    const icon = document.createElement("span");
    icon.className = "act-icon";
    icon.textContent = ACTIVITY_ICONS[item.kind] || "•";

    const text = document.createElement("span");
    text.className = "act-text";
    text.textContent = item.message || "";
    text.title = item.message || "";

    row.appendChild(icon);
    row.appendChild(text);
    row.appendChild(time);
    return row;
  }

  function addActivity(item, live) {
    if (!els.activityLog) return;
    const kind = item.kind || "other";
    if (!ACTIVITY_KINDS.has(kind)) return;

    if (!state.activity.length) {
      els.activityLog.replaceChildren();
    }

    state.activity.push(item);
    if (state.activity.length > 250) state.activity.shift();

    const empty = els.activityLog.querySelector(".empty");
    if (empty) empty.remove();

    els.activityLog.appendChild(activityItem(item));
    while (els.activityLog.children.length > 250) {
      els.activityLog.removeChild(els.activityLog.firstElementChild);
    }
    if (!live || nearBottom()) {
      els.activityLog.scrollTop = els.activityLog.scrollHeight;
    }
    setText(els.activityCount, String(state.activity.length));
  }

  function renderActivity(items) {
    if (!els.activityLog) return;
    els.activityLog.replaceChildren();
    state.activity = [];
    const rows = (Array.isArray(items) ? items : []).filter((item) => ACTIVITY_KINDS.has(item.kind));
    if (!rows.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "Agent activity will appear here.";
      els.activityLog.appendChild(empty);
      setText(els.activityCount, "0");
      return;
    }
    rows.slice(-180).forEach((item) => {
      state.activity.push(item);
      els.activityLog.appendChild(activityItem(item));
    });
    els.activityLog.scrollTop = els.activityLog.scrollHeight;
    setText(els.activityCount, String(state.activity.length));
  }

  // ---------- browser ----------

  function updateOllama(ollama) {
    if (!ollama) return;
    state.ollama = ollama;
    const model = ollama.model || modelName();
    if (ollama.reachable === false) {
      setBadge(els.modelBadge, "Ollama offline", "bad", ollama.error || "Ollama is not reachable");
    } else if (ollama.installed === false) {
      setBadge(els.modelBadge, `${model} missing`, "warn", ollama.error || "Model not installed");
    } else {
      const latency = ollama.latency_ms ? ` · ${ollama.latency_ms} ms` : "";
      const vision = ollama.vision === false ? " · no vision" : "";
      setBadge(els.modelBadge, model, "ok", `Ollama ready${latency}${vision}`);
    }
    setText(els.brandModel, model);
    const caps = [];
    if (ollama.vision) caps.push("vision");
    if (ollama.thinking) caps.push("thinking");
    setText(
      els.modelMeta,
      `${model} · ${ollama.remote ? "remote" : "local"}${
        ollama.reachable === false ? " · offline" : ""
      }${caps.length ? ` · ${caps.join(" + ")}` : ""}`
    );
    updateModelNotice(ollama);
  }

  function updateModelNotice(ollama) {
    if (!els.modelNotice) return;
    if (ollama.reachable === false) {
      els.modelNotice.classList.remove("hidden");
      setText(els.modelNoticeTitle, "Ollama is not running");
      setText(
        els.modelNoticeBody,
        ollama.error ||
          (ollama.remote
            ? "Check that Ollama is running on the Tailscale peer, then reload this page."
            : "Start the Ollama app, then reload this page. Nothing leaves your laptop.")
      );
      setText(els.modelNoticeCommand, "ollama serve");
      return;
    }
    if (ollama.installed === false) {
      els.modelNotice.classList.remove("hidden");
      setText(els.modelNoticeTitle, `${ollama.model} is not installed`);
      setText(
        els.modelNoticeBody,
        ollama.remote
          ? "Pull the model once on the Tailscale peer. It is a large download, but it only happens once."
          : "Pull the model once in a terminal. It is a large download, but it only happens once."
      );
      setText(els.modelNoticeCommand, `ollama pull ${ollama.model}`);
      return;
    }
    els.modelNotice.classList.add("hidden");
  }

  function updateBrowser(browser) {
    if (!browser) return;
    state.lastBrowser = browser;
    const browserState = browser.state || "disconnected";
    const page = browser.page || null;
    const loggedIn = browser.logged_in;

    state.connected = Boolean(browser.connected);
    autoCollapseConnection(loggedIn);

    const badgeMap = {
      disconnected: { label: "Browser off", kind: "muted", title: "Browser disconnected" },
      starting: { label: "Starting", kind: "warn", title: "Launching visible Chrome" },
      login_required: { label: "Login needed", kind: "warn", title: "Log in manually in Chrome" },
      ready: { label: "Moodle ready", kind: "ok", title: "Logged in and ready" },
      unknown: { label: "Browser open", kind: "warn", title: "Login state unknown" },
      error: { label: "Browser error", kind: "bad", title: browser.error || "Browser error" },
    };
    const info = badgeMap[browserState] || badgeMap.unknown;
    setBadge(els.browserBadge, info.label, info.kind, browser.error || info.title);

    const connMap = {
      disconnected: { label: "disconnected", cls: "state-off" },
      starting: { label: "starting…", cls: "state-warn" },
      login_required: { label: "login required", cls: "state-warn" },
      ready: { label: "logged in", cls: "state-on" },
      unknown: { label: "check login", cls: "state-warn" },
      error: { label: "error", cls: "state-bad" },
    };
    let conn = connMap[browserState] || connMap.unknown;
    if (state.syncing && browserState === "ready") conn = { label: "syncing…", cls: "state-warn" };
    if (els.connState) {
      els.connState.className = `state ${conn.cls}`;
      els.connState.textContent = conn.label;
    }

    setText(els.pageTitle, page?.title || browser.title || (browserState === "disconnected" ? "No page open" : "Moodle"));
    setText(els.pageUrl, page?.url || browser.url || "—");

    const hint = els.loginHint;
    if (hint) {
      hint.classList.remove("warn", "ok", "bad");
      switch (browserState) {
        case "ready":
          hint.textContent =
            "Logged in. The agent can now read your courses and assignments in the visible window.";
          hint.classList.add("ok");
          break;
        case "login_required":
          hint.textContent =
            "Chrome is open. Log in to Moodle manually (including SSO/MFA). Owlet never sees or stores your password.";
          hint.classList.add("warn");
          break;
        case "starting":
          hint.textContent = "Launching visible Chrome with your persistent profile…";
          break;
        case "error":
          hint.textContent = browser.error || "The browser could not start. Check Chrome and try again.";
          hint.classList.add("bad");
          break;
        case "unknown":
          hint.textContent =
            browser.error ||
            "Chrome is open. If you are logged in, the agent will confirm it on the next read.";
          if (browser.error) hint.classList.add("warn");
          break;
        default:
          hint.textContent =
            "A visible Chrome window will open. Log in manually — this app never asks for or stores your password.";
          break;
      }
    }

    if (browser.error && browserState === "error") {
      els.browserBadge.title = browser.error;
    }

    if (browser.moodle_url && !els.moodleUrl.value) {
      els.moodleUrl.value = browser.moodle_url;
    }
  }

  async function refreshBrowserStatus() {
    if (browserPollBusy || document.hidden) return;
    browserPollBusy = true;
    try {
      const status = await api("/api/browser/status");
      updateBrowser(status);
      if (status?.moodle_url) {
        state.moodleUrl = status.moodle_url;
        if (!els.moodleUrl.value) els.moodleUrl.value = status.moodle_url;
      }
    } catch (_) {
      /* the SSE stream remains the primary channel */
    } finally {
      browserPollBusy = false;
    }
  }

  function scheduleBrowserRefresh() {
    window.clearTimeout(browserRefreshTimer);
    browserRefreshTimer = window.setTimeout(refreshBrowserStatus, 280);
  }

  function scheduleDataRefresh() {
    window.clearTimeout(dataRefreshTimer);
    dataRefreshTimer = window.setTimeout(refreshData, 400);
  }

  async function refreshData() {
    const results = await Promise.allSettled([
      api("/api/courses"),
      api("/api/assignments"),
      api("/api/workspace"),
    ]);
    if (results[0].status === "fulfilled") renderCourses(results[0].value.courses);
    if (results[1].status === "fulfilled") renderAssignments(results[1].value.assignments);
    if (results[2].status === "fulfilled") {
      state.files = results[2].value || state.files;
      renderFiles();
    }
  }

  // ---------- approvals ----------

  function renderApprovalBar() {
    if (!els.approvalBar) return;
    if (!state.currentApprovalId) {
      els.approvalBar.classList.add("hidden");
      els.approvalBar.replaceChildren();
      return;
    }
    const meta = state.approvals.get(state.currentApprovalId);
    els.approvalBar.replaceChildren();
    const text = document.createElement("span");
    text.textContent = `Approval required: ${humanize(meta?.action)}`;
    els.approvalBar.appendChild(text);
    els.approvalBar.classList.remove("hidden");
    setText(els.agentActivity, "Waiting for your approval");
  }

  function openApproval(meta) {
    if (!meta || !meta.approval_id) return;
    state.approvals.set(meta.approval_id, meta);
    if (state.currentApprovalId) {
      renderApprovalBar();
      return;
    }

    state.currentApprovalId = meta.approval_id;
    setText(els.approvalHeading, "Approval required");
    setText(
      els.approvalMessage,
      `The agent wants to ${humanize(meta.action).toLowerCase()}. Review the details before approving.`
    );
    if (els.approvalDetails) {
      els.approvalDetails.textContent = meta.details
        ? JSON.stringify(meta.details, null, 2)
        : "No extra details.";
    }
    els.approvalModal?.classList.remove("hidden");
    renderApprovalBar();
    setAgentStatus("waiting_approval", "Waiting for your approval");
    els.approveBtn?.focus();
  }

  function hideApprovalModal() {
    els.approvalModal?.classList.add("hidden");
    if (els.approvalDetails) els.approvalDetails.textContent = "";
  }

  function showNextApproval() {
    const next = state.approvals.keys().next();
    if (next.done) {
      state.currentApprovalId = null;
      hideApprovalModal();
      renderApprovalBar();
      return;
    }
    const meta = state.approvals.get(next.value);
    state.currentApprovalId = next.value;
    state.approvals.delete(next.value);
    openApproval(meta);
  }

  async function resolveApproval(approvalId, approved) {
    if (!approvalId) return;
    if (els.approveBtn) els.approveBtn.disabled = true;
    if (els.denyBtn) els.denyBtn.disabled = true;
    try {
      await api(`/api/approvals/${encodeURIComponent(approvalId)}`, {
        method: "POST",
        body: { approved },
      });
      state.approvals.delete(approvalId);
      if (state.currentApprovalId === approvalId) {
        state.currentApprovalId = null;
        hideApprovalModal();
      }
      showNextApproval();
      showToast(approved ? "Action approved." : "Action denied.", approved ? "ok" : undefined);
    } catch (error) {
      showToast(error.message, "bad");
    } finally {
      if (els.approveBtn) els.approveBtn.disabled = false;
      if (els.denyBtn) els.denyBtn.disabled = false;
    }
  }

  // ---------- conversations ----------

  function renderConversations(rows) {
    state.conversations = Array.isArray(rows) ? rows : [];
    setCount(els.chatCount, state.conversations.length);
    if (!els.conversationList) return;
    els.conversationList.replaceChildren();
    if (!state.conversations.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "No conversations yet. Ask something to start one.";
      els.conversationList.appendChild(empty);
      return;
    }
    state.conversations.forEach((row) =>
      els.conversationList.appendChild(conversationRow(row))
    );
  }

  function conversationRow(row) {
    const wrap = document.createElement("div");
    wrap.className = "conv-row";
    wrap.dataset.id = String(row.id);
    if (row.id === state.conversationId) wrap.classList.add("active");

    const open = document.createElement("button");
    open.type = "button";
    open.className = "conv-item";
    open.title = row.title || "Conversation";

    const title = document.createElement("span");
    title.className = "conv-title";
    title.textContent = row.title || "Untitled conversation";

    const meta = document.createElement("span");
    meta.className = "conv-meta";
    const count = Number(row.message_count) || 0;
    meta.textContent = [count ? plural(count, "message") : "empty", relativeTime(row.updated_at)]
      .filter(Boolean)
      .join(" · ");

    open.appendChild(title);
    open.appendChild(meta);
    open.addEventListener("click", () => openConversation(Number(row.id)));

    const actions = document.createElement("div");
    actions.className = "conv-actions";

    const rename = document.createElement("button");
    rename.type = "button";
    rename.className = "icon-btn conv-action";
    rename.dataset.act = "rename";
    rename.title = "Rename conversation";
    rename.setAttribute("aria-label", `Rename ${row.title || "conversation"}`);
    rename.textContent = "✎";
    rename.addEventListener("click", () => renameConversation(Number(row.id)));

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "icon-btn conv-action";
    remove.dataset.act = "delete";
    remove.title = "Delete conversation";
    remove.setAttribute("aria-label", `Delete ${row.title || "conversation"}`);
    remove.textContent = "🗑";
    remove.addEventListener("click", () => deleteConversation(Number(row.id)));

    actions.appendChild(rename);
    actions.appendChild(remove);
    wrap.appendChild(open);
    wrap.appendChild(actions);
    return wrap;
  }

  async function refreshConversations() {
    try {
      const data = await api("/api/conversations");
      renderConversations(data.conversations);
    } catch (_) {
      /* the list is convenience only; the chat itself still works */
    }
  }

  function renderHistory(messages) {
    if (!els.messages) return;
    els.messages.replaceChildren();
    state.lastAssistantEl = null;
    state.lastUserMessageEl = null;
    state.lastUserText = "";
    resetStream();
    state.pendingTools = [];
    hideWaiting();

    const rows = (Array.isArray(messages) ? messages : []).filter(
      (row) => row && (row.role === "user" || row.role === "assistant")
    );
    let lastAssistant = null;
    let lastRole = null;
    let shown = 0;
    rows.forEach((row) => {
      const content = String(row.content || "");
      const messageId = row.id !== undefined && row.id !== null ? `db-${row.id}` : undefined;
      if (row.role === "user") {
        appendUser(content, row.images || [], row.created_at, messageId);
        lastRole = "user";
        shown += 1;
      } else if (content.trim()) {
        lastAssistant = appendAssistant(content, {
          createdAt: row.created_at,
          messageId,
        }).msg;
        lastRole = "assistant";
        shown += 1;
      }
    });

    if (!shown) {
      restoreWelcome();
      return;
    }
    if (lastAssistant && lastRole === "assistant") {
      clearRegenerateActions();
      messageActions(lastAssistant, { regenerate: true });
    }
    scrollToBottom(true);
  }

  async function openConversation(conversationId) {
    if (state.running) {
      showToast("Stop the agent before switching conversations.");
      return;
    }
    const row = state.conversations.find((item) => item.id === conversationId);
    if (state.conversationId === conversationId && state.conversationStarted) return;

    state.conversationId = conversationId;
    state.conversationStarted = true;
    state.lastUserText = "";
    clearAttachments();
    hideWaiting();
    setText(els.chatTitle, row?.title || "Conversation");
    setText(els.chatSubtitle, `${modelName()} · loading history…`);
    if (els.messages) {
      els.messages.replaceChildren();
      const loading = document.createElement("p");
      loading.className = "empty";
      loading.textContent = "Loading messages…";
      els.messages.appendChild(loading);
    }
    renderConversations(state.conversations);
    updateScrollButton();

    try {
      const data = await api(
        `/api/conversations/${encodeURIComponent(conversationId)}/messages`
      );
      if (state.conversationId !== conversationId) return;
      renderHistory(data.messages);
      setText(
        els.chatSubtitle,
        `${modelName()} · ${plural((data.messages || []).length, "message")}`
      );
      setText(els.agentActivity, "Ready");
    } catch (error) {
      if (state.conversationId !== conversationId) return;
      if (els.messages) els.messages.replaceChildren();
      appendError(error.message);
      setText(els.chatSubtitle, "Conversation unavailable");
    }
  }

  async function renameConversation(conversationId) {
    const row = state.conversations.find((item) => item.id === conversationId);
    const proposed = window.prompt("Rename this conversation", row?.title || "");
    if (proposed === null) return;
    const title = proposed.trim().slice(0, 120);
    if (!title) {
      showToast("A conversation needs a name.", "bad");
      return;
    }
    try {
      const result = await api(
        `/api/conversations/${encodeURIComponent(conversationId)}`,
        { method: "PATCH", body: { title } }
      );
      const updated = result?.conversation;
      if (updated) {
        state.conversations = state.conversations.map((item) =>
          item.id === updated.id ? { ...item, ...updated } : item
        );
        renderConversations(state.conversations);
        if (state.conversationId === conversationId) setText(els.chatTitle, updated.title);
      }
      showToast("Conversation renamed.", "ok");
    } catch (error) {
      showToast(error.message, "bad");
    }
  }

  async function deleteConversation(conversationId) {
    if (state.running) {
      showToast("Stop the agent before deleting a conversation.");
      return;
    }
    const row = state.conversations.find((item) => item.id === conversationId);
    const label = row?.title || "this conversation";
    if (!window.confirm(`Delete “${label}”? Its messages and attached images are removed.`)) {
      return;
    }
    try {
      await api(`/api/conversations/${encodeURIComponent(conversationId)}`, {
        method: "DELETE",
      });
      state.conversations = state.conversations.filter((item) => item.id !== conversationId);
      renderConversations(state.conversations);
      if (state.conversationId === conversationId) newChat();
      showToast("Conversation deleted.", "ok");
    } catch (error) {
      showToast(error.message, "bad");
    }
  }

  // ---------- image attachments ----------

  const IMAGE_MIME = new Set(["image/png", "image/jpeg", "image/webp", "image/gif"]);
  const MAX_IMAGE_DIMENSION = 1600;
  let dragDepth = 0;

  function attachmentBytes(dataUrl) {
    const comma = String(dataUrl).indexOf(",");
    if (comma < 0) return 0;
    return Math.floor(((dataUrl.length - comma - 1) * 3) / 4);
  }

  function imageElement(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const img = new Image();
        img.onload = () => resolve(img);
        img.onerror = () =>
          reject(new Error(`${file.name || "That image"} could not be opened.`));
        img.src = String(reader.result || "");
      };
      reader.onerror = () =>
        reject(new Error(`${file.name || "That image"} could not be read.`));
      reader.readAsDataURL(file);
    });
  }

  function loadImageFile(file) {
    if (typeof createImageBitmap === "function") {
      return createImageBitmap(file).catch(() => imageElement(file));
    }
    return imageElement(file);
  }

  function scaledCanvas(source) {
    const width = source.width || source.naturalWidth || 0;
    const height = source.height || source.naturalHeight || 0;
    if (!width || !height) throw new Error("That image has no readable size.");
    const scale = Math.min(1, MAX_IMAGE_DIMENSION / Math.max(width, height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(width * scale));
    canvas.height = Math.max(1, Math.round(height * scale));
    const context = canvas.getContext("2d");
    if (!context) throw new Error("This browser could not prepare the image.");
    context.drawImage(source, 0, 0, canvas.width, canvas.height);
    return canvas;
  }

  async function prepareAttachment(file) {
    if (!IMAGE_MIME.has(file.type)) {
      throw new Error("Attach PNG, JPEG, WebP, or GIF images.");
    }
    const source = await loadImageFile(file);
    const canvas = scaledCanvas(source);
    const preferred =
      file.type === "image/png" || file.type === "image/gif" ? "image/png" : "image/jpeg";
    let dataUrl = canvas.toDataURL(preferred, 0.85);
    // A photo stays under the byte limit more reliably as JPEG; only fall back
    // for PNG/GIF input, where transparency may matter.
    if (attachmentBytes(dataUrl) > maxImageBytes() && preferred !== "image/jpeg") {
      dataUrl = canvas.toDataURL("image/jpeg", 0.82);
    }
    const size = attachmentBytes(dataUrl);
    if (!size) throw new Error("That image came out empty; try another file.");
    if (size > maxImageBytes()) {
      throw new Error(
        `Each image must be under ${state.chatConfig?.max_image_mb || 8} MB after scaling.`
      );
    }
    state.attachmentSeq += 1;
    const mime = dataUrl.slice(5, dataUrl.indexOf(";")) || preferred;
    return {
      id: `att-${state.attachmentSeq}`,
      name: file.name || `image-${state.attachmentSeq}`,
      mime,
      dataUrl,
      size,
      width: canvas.width,
      height: canvas.height,
    };
  }

  async function addAttachments(files) {
    const list = Array.from(files || []).filter(Boolean);
    if (!list.length) return;
    const room = maxImages() - state.attachments.length;
    if (room <= 0) {
      showToast(`Attach at most ${maxImages()} images per message.`, "bad");
      return;
    }
    if (list.length > room) {
      showToast(`Only ${room} more image${room === 1 ? "" : "s"} can be attached.`);
    }
    for (const file of list.slice(0, room)) {
      try {
        const attachment = await prepareAttachment(file);
        state.attachments.push(attachment);
        renderAttachments();
      } catch (error) {
        showToast(error.message, "bad");
      }
    }
    updateComposerState();
  }

  function removeAttachment(id) {
    state.attachments = state.attachments.filter((item) => item.id !== id);
    renderAttachments();
    updateComposerState();
  }

  function clearAttachments() {
    state.attachments = [];
    if (els.imageInput) els.imageInput.value = "";
    renderAttachments();
  }

  function renderAttachments() {
    if (!els.attachments) return;
    els.attachments.replaceChildren();
    els.attachments.classList.toggle("hidden", !state.attachments.length);
    state.attachments.forEach((attachment) => {
      const item = document.createElement("div");
      item.className = "attachment";
      item.dataset.id = attachment.id;

      const img = document.createElement("img");
      img.src = attachment.dataUrl;
      img.alt = attachment.name;
      img.title = `${attachment.name} · ${attachment.width}×${attachment.height} · ${formatBytes(attachment.size)}`;

      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "attachment-remove";
      remove.title = `Remove ${attachment.name}`;
      remove.setAttribute("aria-label", `Remove ${attachment.name}`);
      remove.textContent = "×";
      remove.addEventListener("click", () => removeAttachment(attachment.id));

      const meta = document.createElement("span");
      meta.className = "attachment-meta";
      meta.textContent = formatBytes(attachment.size);

      item.appendChild(img);
      item.appendChild(meta);
      item.appendChild(remove);
      els.attachments.appendChild(item);
    });
  }

  function updateComposerState() {
    if (!els.sendBtn) return;
    const hasContent = Boolean(els.messageInput?.value.trim()) || state.attachments.length > 0;
    els.sendBtn.disabled = state.running || !hasContent;
  }

  // Named distinctly from the file-tree helper above: a second `hasFiles`
  // declaration in this scope would hoist over it and blank the Files card.
  function dragHasFiles(event) {
    const types = event.dataTransfer?.types;
    return Boolean(types && Array.from(types).includes("Files"));
  }

  function showDrop(show) {
    els.dropOverlay?.classList.toggle("hidden", !show);
  }

  function wireAttachments() {
    els.attachBtn?.addEventListener("click", () => els.imageInput?.click());

    els.imageInput?.addEventListener("change", () => {
      if (els.imageInput.files?.length) addAttachments(els.imageInput.files);
      els.imageInput.value = "";
    });

    const panel = els.chatPanel;
    if (!panel) return;

    panel.addEventListener("dragenter", (event) => {
      if (!dragHasFiles(event)) return;
      event.preventDefault();
      dragDepth += 1;
      showDrop(true);
    });

    panel.addEventListener("dragover", (event) => {
      if (!dragHasFiles(event)) return;
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
      showDrop(true);
    });

    panel.addEventListener("dragleave", () => {
      if (!dragDepth) return;
      dragDepth = Math.max(0, dragDepth - 1);
      if (!dragDepth) showDrop(false);
    });

    panel.addEventListener("drop", (event) => {
      event.preventDefault();
      dragDepth = 0;
      showDrop(false);
      addAttachments(event.dataTransfer?.files);
    });

    panel.addEventListener("paste", (event) => {
      const files = Array.from(event.clipboardData?.files || []).filter((file) =>
        IMAGE_MIME.has(file.type)
      );
      if (!files.length) return;
      // Text that happens to come with a copied image should not be swallowed.
      if (event.clipboardData?.getData("text")) return;
      event.preventDefault();
      addAttachments(files);
    });
  }

  // ---------- chat actions ----------

  function autosize() {
    const input = els.messageInput;
    if (!input) return;
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
  }

  async function sendMessage(rawText) {
    const typed = String(rawText ?? "").trim();
    const attachments = state.attachments.slice();
    if (!typed && !attachments.length) return;
    // One submission, one generation request. `sending` closes the gap between
    // the click/Enter handler firing and the POST actually being in flight.
    if (state.running || state.sending) {
      showToast("The agent is already working. Stop it or wait for it to finish.");
      return;
    }
    if (typed && !attachments.length && isResetCommand(typed)) {
      resetSavedData();
      return;
    }
    if (typed.length > 8000) {
      showToast("That message is too long (maximum 8000 characters).", "bad");
      return;
    }

    // An image-only turn still needs visible text; the backend stores the same
    // sentence, so a reloaded history matches what was just shown.
    const text = typed || IMAGE_ONLY_TEXT;
    const previews = attachments.map((attachment) => ({
      url: attachment.dataUrl,
      name: attachment.name,
    }));

    state.sending = true;
    appendUser(text, previews);
    clearAttachments();
    if (els.messageInput) {
      els.messageInput.value = "";
      autosize();
    }
    if (!state.conversationStarted || state.conversationId === null) {
      setText(els.chatTitle, truncate(text, 70));
      setText(els.chatSubtitle, `Local conversation · ${modelName()}`);
    }
    state.conversationStarted = true;
    setRunning(true);
    updateComposerState();
    setText(
      els.agentActivity,
      attachments.length
        ? `Sending ${plural(attachments.length, "image")} to ${modelName()}…`
        : "Starting…"
    );
    showWaiting(`Thinking with ${modelName()}…`);

    try {
      const response = await api("/api/chat", {
        method: "POST",
        body: {
          message: typed,
          conversation_id: state.conversationId,
          images: attachments.map((attachment) => ({
            name: attachment.name,
            mime: attachment.mime,
            data: attachment.dataUrl.split(",")[1] || "",
          })),
        },
      });
      state.conversationId = response.conversation_id;
      refreshConversations();
    } catch (error) {
      hideWaiting();
      setRunning(false);
      updateComposerState();
      if (error.status === 409) {
        showToast(error.message);
      } else {
        appendError(error.message);
      }
      setText(els.agentActivity, "Ready");
    } finally {
      state.sending = false;
    }
  }

  function clearAfterLastUser() {
    if (!state.lastUserMessageEl) return;
    let node = state.lastUserMessageEl.nextSibling;
    while (node) {
      const next = node.nextSibling;
      node.remove();
      node = next;
    }
    state.lastAssistantEl = null;
    resetStream();
    state.pendingTools = [];
    hideWaiting();
    updateScrollButton();
  }

  async function regenerateLast() {
    if (state.running) {
      showToast("The agent is already working.");
      return;
    }
    const conversationId = state.conversationId;
    if (conversationId === null) {
      showToast("Ask a question first.");
      return;
    }
    if (!state.lastUserMessageEl) {
      showToast("There is no question to regenerate yet.");
      return;
    }

    clearAfterLastUser();
    setRunning(true);
    updateComposerState();
    setText(els.agentActivity, `Regenerating with ${modelName()}…`);
    showWaiting(`Regenerating with ${modelName()}…`);

    try {
      await api("/api/chat/regenerate", {
        method: "POST",
        body: { conversation_id: conversationId },
      });
      refreshConversations();
    } catch (error) {
      hideWaiting();
      setRunning(false);
      updateComposerState();
      appendError(error.message);
      setText(els.agentActivity, "Ready");
    }
  }

  async function copyMessage(msg) {
    const bubble = msg?.querySelector(".bubble");
    if (!bubble) return;
    const text = bubble.innerText || bubble.textContent || "";
    if (!text.trim()) return;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        throw new Error("clipboard unavailable");
      }
      showToast("Copied to clipboard.", "ok");
    } catch (_) {
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.top = "-1000px";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      try {
        document.execCommand("copy");
        showToast("Copied to clipboard.", "ok");
      } catch (__) {
        showToast("Copy failed — select the text and copy it manually.", "bad");
      } finally {
        area.remove();
      }
    }
  }

  async function stopAgent() {
    if (!state.running) return;
    els.stopBtn.disabled = true;
    try {
      await api("/api/agent/stop", { method: "POST", body: {} });
      setText(els.agentActivity, "Stopping after the current step…");
      showToast("Stop requested.");
    } catch (error) {
      showToast(error.message, "bad");
      els.stopBtn.disabled = false;
    }
  }

  function newChat() {
    if (state.running) {
      showToast("Stop the agent before starting a new chat.");
      return;
    }
    state.conversationId = null;
    state.conversationStarted = false;
    state.lastAssistantEl = null;
    state.lastUserMessageEl = null;
    state.lastUserText = "";
    resetStream();
    state.pendingTools = [];
    hideWaiting();
    clearAttachments();
    restoreWelcome();
    renderConversations(state.conversations);
    setText(els.chatTitle, defaultChatTitle);
    setText(els.chatSubtitle, defaultChatSubtitle);
    setText(els.agentActivity, "Ready");
    updateComposerState();
    els.messageInput?.focus();
  }

  // ---------- /reset ----------

  const RESET_COMMAND = "/reset";

  function isResetCommand(text) {
    return String(text ?? "").trim().toLowerCase() === RESET_COMMAND;
  }

  function resetSummary(summary) {
    if (!summary) return "Saved data cleared.";
    const parts = [
      plural(Number(summary.courses) || 0, "course"),
      plural(Number(summary.assignments) || 0, "assignment"),
      plural(Number(summary.conversations) || 0, "conversation"),
    ];
    const files = (Number(summary.workspace_files) || 0) + (Number(summary.generated_files) || 0);
    if (files) parts.push(plural(files, "file"));
    return `Cleared ${parts.join(", ")}.`;
  }

  function applyLocalReset() {
    state.conversationId = null;
    state.conversationStarted = false;
    state.lastAssistantEl = null;
    state.lastUserMessageEl = null;
    state.lastUserText = "";
    resetStream();
    state.pendingTools = [];
    state.approvals.clear();
    state.currentApprovalId = null;
    state.files = { workspace: [], generated: [] };

    hideWaiting();
    clearAttachments();
    hideApprovalModal();
    renderApprovalBar();
    renderCourses([]);
    renderAssignments([]);
    renderFiles();
    renderActivity([]);
    renderConversations([]);
    restoreWelcome();

    setText(els.chatTitle, defaultChatTitle);
    setText(els.chatSubtitle, defaultChatSubtitle);
    setText(els.agentActivity, "Ready");
    setRunning(false);
  }

  async function resetSavedData() {
    const confirmed = window.confirm(
      [
        "Clear all saved Moodle data?",
        "",
        "This removes courses, assignments, chats, activity and workspace files.",
        "Your Moodle login in Chrome is kept. This cannot be undone.",
      ].join("\n")
    );
    if (!confirmed) {
      showToast("Reset cancelled.");
      els.messageInput?.focus();
      return;
    }

    appendUser(RESET_COMMAND);
    if (els.messageInput) {
      els.messageInput.value = "";
      autosize();
    }
    setRunning(true);
    setText(els.agentActivity, "Clearing saved data…");
    resetEchoUntil = Date.now() + 3000;

    try {
      const summary = await api("/api/reset", { method: "POST", body: {} });
      applyLocalReset();
      refreshData();
      refreshActivity();
      showToast(resetSummary(summary), "ok");
    } catch (error) {
      setRunning(false);
      appendError(error.message);
      setText(els.agentActivity, "Ready");
    }
  }

  // ---------- SSE ----------

  function handleEvent(event) {
    if (!event || !event.type) return;
    const { type, message, data } = event;

    switch (type) {
      case "snapshot": {
        if (data) {
          updateBrowser(data.browser);
          renderSync(data.sync);
          if (data.ollama) updateOllama(data.ollama);
          if (data.chat) state.chatConfig = { ...state.chatConfig, ...data.chat };
          if (Array.isArray(data.conversations)) renderConversations(data.conversations);
          if (data.moodle_url) {
            state.moodleUrl = data.moodle_url;
            if (!els.moodleUrl.value) els.moodleUrl.value = data.moodle_url;
          }
          if (data.agent) {
            setAgentStatus(data.agent.status, data.agent.status === "idle" ? "Ready" : undefined);
            if (data.agent.running) setRunning(true);
          }
          (data.approvals || []).forEach((meta) => openApproval(meta));
        }
        break;
      }

      case "user_message": {
        const target = data?.conversation_id;
        if (target && state.conversationId !== null && target !== state.conversationId) {
          refreshConversations();
          break;
        }
        const messageId = data?.message_id || null;
        const shown = messageId ? messageById(messageId) : null;
        const alreadyShown =
          Boolean(shown) ||
          (state.lastUserText === message &&
            state.lastUserMessageEl &&
            els.messages?.contains(state.lastUserMessageEl));
        if (!alreadyShown) {
          appendUser(message, data?.images || [], undefined, messageId);
        } else if (messageId && state.lastUserMessageEl) {
          // The optimistic bubble was already drawn: adopt the server id so a
          // replay of this event can never add it a second time.
          state.lastUserMessageEl.dataset.messageId = messageId;
        }
        state.lastUserText = message;
        refreshConversations();
        break;
      }

      case "regenerate": {
        if (data?.conversation_id && state.conversationId !== null && data.conversation_id !== state.conversationId) {
          refreshConversations();
          break;
        }
        clearAfterLastUser();
        setRunning(true);
        setText(els.agentActivity, `Regenerating with ${modelName()}…`);
        showWaiting(`Regenerating with ${modelName()}…`);
        refreshConversations();
        break;
      }

      case "conversation_renamed": {
        const row = data?.conversation;
        if (row) {
          state.conversations = state.conversations.map((item) =>
            item.id === row.id ? { ...item, ...row } : item
          );
          renderConversations(state.conversations);
          if (state.conversationId === row.id) setText(els.chatTitle, row.title);
        }
        break;
      }

      case "conversation_deleted": {
        const removed = Number(data?.conversation_id);
        if (Number.isFinite(removed)) {
          state.conversations = state.conversations.filter((item) => item.id !== removed);
          renderConversations(state.conversations);
          if (state.conversationId === removed) newChat();
        }
        break;
      }

      case "assistant_delta": {
        appendAssistantDelta(message, data?.message_id || null);
        break;
      }

      case "assistant_message": {
        const messageId = data?.message_id || null;
        const stream = state.stream;
        if (stream && !stream.done && (!messageId || stream.id === messageId)) {
          // The final event for the round that is already on screen: finalise
          // that exact element. This is the end of the message, not a new one.
          finalizeStreaming(message, { messageId: stream.id });
          break;
        }
        if (stream) finalizeStreaming(undefined, { messageId: stream.id });
        // No live stream (or a replayed event): update by id, or create once.
        upsertAssistantMessage(messageId, message);
        break;
      }

      case "tool_start": {
        appendToolRow(data?.tool || "tool", data?.arguments);
        break;
      }

      case "tool_result": {
        finishToolRow(data, message);
        if (data && (data.ok === false || data.error)) {
          addActivity(
            {
              kind: "error",
              level: "error",
              message: message || data.error || "Tool failed",
              created_at: event.ts,
            },
            true
          );
        }
        scheduleDataRefresh();
        scheduleBrowserRefresh();
        break;
      }

      case "status": {
        const status = data?.status;
        if (status) setAgentStatus(status, status === "idle" ? "Ready" : message);
        else if (message) setText(els.agentActivity, message);
        break;
      }

      case "approval_required": {
        if (data) openApproval(data);
        addActivity(
          { kind: "approval_required", level: "info", message: message || "Approval required", created_at: event.ts },
          true
        );
        break;
      }

      case "approval_resolved": {
        if (data?.approval_id) {
          state.approvals.delete(data.approval_id);
          if (state.currentApprovalId === data.approval_id) {
            state.currentApprovalId = null;
            hideApprovalModal();
            showNextApproval();
          } else {
            renderApprovalBar();
          }
        }
        break;
      }

      case "approval_timeout": {
        if (data?.approval_id) {
          state.approvals.delete(data.approval_id);
          if (state.currentApprovalId === data.approval_id) {
            state.currentApprovalId = null;
            hideApprovalModal();
            showNextApproval();
          }
        }
        showToast("The approval timed out, so the action was denied.", "bad");
        break;
      }

      case "browser": {
        scheduleBrowserRefresh();
        addActivity({ kind: "browser", level: "info", message, created_at: event.ts }, true);
        break;
      }

      case "sync": {
        renderSync(data);
        addActivity({ kind: "sync", level: "info", message, created_at: event.ts }, true);
        break;
      }

      case "sync_done": {
        renderSync(data?.sync || data);
        addActivity(
          {
            kind: "sync",
            level: data?.ok === false ? "error" : "info",
            message,
            created_at: event.ts,
          },
          true
        );
        scheduleDataRefresh();
        showToast(message, data?.ok === false ? "bad" : "ok");
        break;
      }

      case "navigate": {
        if (els.pageUrl && data?.url) setText(els.pageUrl, data.url);
        scheduleBrowserRefresh();
        addActivity({ kind: "navigate", level: "info", message, created_at: event.ts }, true);
        break;
      }

      case "reading":
      case "download":
      case "file": {
        addActivity({ kind: type, level: "info", message, created_at: event.ts }, true);
        if (type === "file" || type === "download") scheduleDataRefresh();
        break;
      }

      case "warning": {
        addActivity({ kind: "warning", level: "info", message, created_at: event.ts }, true);
        break;
      }

      case "done": {
        finalizeStreaming(undefined, { messageId: data?.message_id || undefined });
        setRunning(false);
        if (data) {
          const parts = [];
          if (data.steps) parts.push(plural(data.steps, "model round"));
          if (data.tool_calls) parts.push(plural(data.tool_calls, "tool call"));
          const target = (data.message_id && messageById(data.message_id)) || state.lastAssistantEl;
          addMessageMeta(target, parts.join(" · "));
        }
        setText(els.agentActivity, "Ready");
        refreshConversations();
        scheduleDataRefresh();
        break;
      }

      case "stopped": {
        finalizeStreaming(undefined, { messageId: data?.message_id || undefined });
        appendNotice("Stopped. You can ask a smaller next step.");
        setRunning(false);
        setText(els.agentActivity, "Stopped");
        addActivity({ kind: "stopped", level: "info", message: message || "Agent stopped", created_at: event.ts }, true);
        refreshConversations();
        break;
      }

      case "draft": {
        addActivity({ kind: "draft", level: "info", message, created_at: event.ts }, true);
        if (data?.state === "done") {
          showToast(message || "Draft finished.", data.files ? "ok" : undefined);
          scheduleDataRefresh();
        }
        if (state.draftId !== null && data?.assignment_id === state.draftId) {
          loadDraft(state.draftId).catch(() => {});
        }
        break;
      }

      case "reset": {
        closeDraft();
        applyLocalReset();
        refreshData();
        refreshActivity();
        if (Date.now() >= resetEchoUntil) {
          showToast(message || "Saved data cleared.", "ok");
        }
        break;
      }

      case "error": {
        const wasRunning = state.running;
        finalizeStreaming(undefined, { messageId: data?.message_id || undefined });
        if (wasRunning) appendError(message || "Something went wrong.");
        else showToast(message || "Something went wrong.", "bad");
        setText(els.agentActivity, "Error — see the chat");
        addActivity({ kind: "error", level: "error", message, created_at: event.ts }, true);
        break;
      }

      default:
        break;
    }
  }

  function connectEvents() {
    if (!window.EventSource) {
      appendError("This browser does not support live updates. Use a current Chrome or Edge build.");
      return;
    }

    const source = new EventSource("/api/events");

    source.onopen = () => {
      if (state.sseFailed) {
        state.sseFailed = false;
        showToast("Reconnected to the local server.", "ok");
        // Anything may have changed while the stream was down.
        hydrate().catch(() => {});
      }
    };

    source.onmessage = (event) => {
      if (!event.data) return;
      let parsed;
      try {
        parsed = JSON.parse(event.data);
      } catch (_) {
        return;
      }
      handleEvent(parsed);
    };

    source.onerror = () => {
      if (!state.sseFailed) {
        state.sseFailed = true;
        showToast("Live connection lost — retrying…", "bad");
      }
    };
  }

  // ---------- initial state ----------

  async function hydrate() {
    try {
      const snapshot = await api("/api/state");
      state.chatConfig = { ...state.chatConfig, ...(snapshot.chat || {}) };
      updateOllama(snapshot.ollama);
      updateBrowser(snapshot.browser);
      renderSync(snapshot.sync);
      renderCourses(snapshot.courses);
      renderAssignments(snapshot.assignments);
      renderConversations(snapshot.conversations || []);
      state.files = snapshot.files || state.files;
      renderFiles();
      renderActivity(snapshot.activity);
      updateComposerState();

      state.tools = snapshot.tools || [];
      setText(
        els.toolCount,
        state.tools.length
          ? `${plural(state.tools.length, "tool")} · loopback 127.0.0.1`
          : "loopback 127.0.0.1"
      );

      if (snapshot.moodle_url) {
        state.moodleUrl = snapshot.moodle_url;
        if (els.moodleUrl) els.moodleUrl.value = snapshot.moodle_url;
      }

      if (snapshot.agent) {
        setAgentStatus(snapshot.agent.status);
        if (snapshot.agent.running) setRunning(true);
      } else {
        setAgentStatus("idle", "Ready");
      }

      (snapshot.approvals || []).forEach((meta) => openApproval(meta));

      const fileCount =
        (snapshot.files?.workspace?.length || 0) + (snapshot.files?.generated?.length || 0);
      if (fileCount && state.activeTab === "workspace" && !snapshot.files?.workspace?.length) {
        // no-op: keep workspace selected so the empty state stays predictable
      }
    } catch (error) {
      appendError(
        `${error.message}\n\nStart the dashboard with \`python main.py\`, then reload this page.`
      );
      setText(els.agentActivity, "Offline");
    }
  }

  // ---------- wiring ----------

  function bind() {
    els.themeBtn?.addEventListener("click", () => {
      const next = root.dataset.theme === "dark" ? "light" : "dark";
      applyTheme(next);
      try {
        localStorage.setItem(THEME_KEY, next);
      } catch (_) {
        /* ignore */
      }
    });

    media.addEventListener?.("change", (event) => {
      let saved = null;
      try {
        saved = localStorage.getItem(THEME_KEY);
      } catch (_) {
        saved = null;
      }
      if (!saved) applyTheme(event.matches ? "dark" : "light");
    });

    els.composer?.addEventListener("submit", (event) => {
      event.preventDefault();
      sendMessage(els.messageInput?.value);
    });

    els.messageInput?.addEventListener("input", () => {
      autosize();
      updateComposerState();
    });
    els.messageInput?.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendMessage(els.messageInput.value);
      }
    });

    els.messages?.addEventListener("click", (event) => {
      const action = event.target.closest(".msg-action");
      if (action) {
        const msg = action.closest(".msg");
        if (action.dataset.act === "copy") copyMessage(msg);
        else if (action.dataset.act === "regenerate") regenerateLast();
        return;
      }
      const suggestion = event.target.closest(".suggestion");
      if (!suggestion) return;
      const prompt = suggestion.dataset.prompt;
      if (prompt) sendMessage(prompt);
    });

    els.messages?.addEventListener("scroll", updateScrollButton, { passive: true });
    els.scrollBtn?.addEventListener("click", () => {
      if (els.messages) els.messages.scrollTop = els.messages.scrollHeight;
      updateScrollButton();
      els.messageInput?.focus();
    });

    // Both the sidebar and the chat header offer a New chat action.
    document.querySelectorAll("[data-new-chat]").forEach((button) => {
      button.addEventListener("click", newChat);
    });
    wireAttachments();

    els.stopBtn?.addEventListener("click", stopAgent);
    els.newChatBtn?.addEventListener("click", newChat);

    els.connectBtn?.addEventListener("click", () => {
      const raw = (els.moodleUrl?.value || "").trim();
      if (!raw) {
        showToast("Enter your Moodle URL first.", "bad");
        els.moodleUrl?.focus();
        return;
      }
      let candidate = raw;
      if (!/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(candidate)) candidate = `https://${candidate}`;
      let parsed;
      try {
        parsed = new URL(candidate);
        if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
          throw new Error("Only http and https addresses are allowed.");
        }
      } catch (error) {
        showToast(error.message || "That does not look like a valid URL.", "bad");
        return;
      }

      withLoading(els.connectBtn, async () => {
        try {
          // Optional HTTP Basic credentials for campuses that front Moodle with a
          // popup login. They go to Chrome via the backend and are never stored:
          // the password field is cleared as soon as the request returns.
          const httpUsername = (els.httpUser?.value || "").trim();
          const httpPassword = els.httpPass?.value || "";
          const status = await api("/api/browser/connect", {
            method: "POST",
            body: {
              url: parsed.href,
              http_username: httpUsername,
              http_password: httpPassword,
            },
          });
          if (els.httpPass) els.httpPass.value = "";
          updateBrowser(status);
          showToast("Chrome opened. Log in to Moodle manually.");
          scheduleBrowserRefresh();
        } catch (error) {
          showToast(error.message, "bad");
          if (els.loginHint) {
            els.loginHint.textContent = error.message;
            els.loginHint.classList.remove("ok", "warn");
            els.loginHint.classList.add("bad");
          }
        }
      });
    });

    els.moodleUrl?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        els.connectBtn?.click();
      }
    });

    els.checkLoginBtn?.addEventListener("click", () => {
      withLoading(els.checkLoginBtn, async () => {
        try {
          const status = await api("/api/browser/check-login", {
            method: "POST",
            body: {},
          });
          updateBrowser(status);
          if (status.logged_in === true) showToast("Moodle login confirmed.", "ok");
          else if (status.logged_in === false) {
            showToast("Still waiting for login in Chrome.", "bad");
          } else showToast("Login state is unclear — try opening a course.");
        } catch (error) {
          showToast(error.message, "bad");
        }
      });
    });

    els.syncBtn?.addEventListener("click", () => {
      withLoading(els.syncBtn, async () => {
        try {
          const result = await api("/api/sync", { method: "POST", body: {} });
          renderSync(result.sync);
          showToast(result.started ? "Reading Moodle…" : "A sync is already running.");
        } catch (error) {
          showToast(error.message, "bad");
        }
      });
    });

    els.disconnectBtn?.addEventListener("click", () => {
      withLoading(els.disconnectBtn, async () => {
        try {
          const status = await api("/api/browser/disconnect", {
            method: "POST",
            body: {},
          });
          updateBrowser(status);
          showToast("Browser disconnected.");
        } catch (error) {
          showToast(error.message, "bad");
        }
      });
    });

    els.refreshCourses?.addEventListener("click", () => {
      withLoading(els.refreshCourses, async () => {
        try {
          const data = await api("/api/courses");
          renderCourses(data.courses);
          showToast(`${plural((data.courses || []).length, "course")} loaded.`);
        } catch (error) {
          showToast(error.message, "bad");
        }
      });
    });

    els.refreshAssignments?.addEventListener("click", () => {
      withLoading(els.refreshAssignments, async () => {
        try {
          const data = await api("/api/assignments");
          renderAssignments(data.assignments);
          showToast(`${plural((data.assignments || []).length, "assignment")} loaded.`);
        } catch (error) {
          showToast(error.message, "bad");
        }
      });
    });

    els.refreshFiles?.addEventListener("click", () => {
      withLoading(els.refreshFiles, async () => {
        try {
          const data = await api("/api/workspace");
          state.files = data || state.files;
          renderFiles();
          showToast("File list refreshed.");
        } catch (error) {
          showToast(error.message, "bad");
        }
      });
    });

    document.querySelectorAll(".tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        state.activeTab = tab.dataset.tab === "generated" ? "generated" : "workspace";
        document.querySelectorAll(".tab").forEach((item) => {
          const active = item === tab;
          item.classList.toggle("active", active);
          item.setAttribute("aria-selected", active ? "true" : "false");
        });
        renderFiles();
      });
    });

    els.approveBtn?.addEventListener("click", () => {
      if (state.currentApprovalId) resolveApproval(state.currentApprovalId, true);
    });

    els.denyBtn?.addEventListener("click", () => {
      if (state.currentApprovalId) resolveApproval(state.currentApprovalId, false);
    });

    els.draftCloseBtn?.addEventListener("click", closeDraft);
    els.draftStartBtn?.addEventListener("click", startDraft);
    els.draftReviewBtn?.addEventListener("click", markReviewed);
    els.draftModal?.addEventListener("click", (event) => {
      if (event.target === els.draftModal) closeDraft();
    });
    document.addEventListener("keydown", (event) => {
      // Escape closes the draft dialog only; the approval dialog stays blocking.
      if (event.key === "Escape" && state.draftId !== null && !state.currentApprovalId) closeDraft();

      // Ctrl/Cmd+K focuses the composer from anywhere; Alt+N starts a new chat.
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        els.messageInput?.focus();
        return;
      }
      if (event.altKey && !event.ctrlKey && !event.metaKey && event.key.toLowerCase() === "n") {
        event.preventDefault();
        newChat();
      }
    });

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) {
        refreshBrowserStatus();
      }
    });

    window.setInterval(refreshBrowserStatus, 5000);
  }

  // ---------- start ----------

  initCards();
  renderSuggestions();
  bind();
  setRunning(false);
  setAgentStatus("idle", "Ready");
  autosize();
  hydrate().then(connectEvents);
})();
