const $ = (selector) => document.querySelector(selector);
const joinScreen = $("#join-screen");
const joinForm = $("#join-form");
const chatForm = $("#chat-form");
const input = $("#chat-input");
const log = $("#chat-log");
const welcome = $("#welcome-block");
const trending = $("#trending-list");
const tokenKey = "xiaoshutong-session";
let token = localStorage.getItem(tokenKey) || "";
let currentUser = null;
let books = [];
let lastMessageId = 0;
let polling = false;
let busy = false;
let llmConfigured = false;

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `请求失败（${response.status}）`);
  return data;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}

function renderTrending() {
  const picks = books.slice().sort((a, b) => Number(b.rating) - Number(a.rating)).slice(0, 4);
  trending.innerHTML = picks.map((book) => `<button class="book-row" type="button" data-book="${escapeHtml(book.title)}">
    <span class="book-cover cover-${escapeHtml(book.tone)}">${escapeHtml(book.short).replace("\n", "<br>")}</span>
    <span class="book-meta"><strong>${escapeHtml(book.title)}</strong><small>${escapeHtml(book.author)} · ${escapeHtml(book.category)}</small></span>
    <span class="book-rating">★ ${escapeHtml(book.rating)}</span>
  </button>`).join("");
}

function appendMessage(message) {
  if (message.id <= lastMessageId) return;
  welcome.hidden = true;
  log.hidden = false;
  const row = document.createElement("div");
  const isAssistant = message.role === "assistant";
  const isMine = !isAssistant && message.display_name === currentUser?.display_name;
  row.className = `message ${isAssistant ? "assistant" : isMine ? "user" : "other-user"}`;
  if (isAssistant) {
    const avatar = document.createElement("div");
    avatar.className = "message-avatar";
    avatar.textContent = "📖";
    row.append(avatar);
  }
  const column = document.createElement("div");
  column.className = "message-column";
  if (!isAssistant) {
    const sender = document.createElement("span");
    sender.className = "message-sender";
    sender.textContent = isMine ? "我" : message.display_name;
    column.append(sender);
  }
  const bubble = document.createElement("div");
  bubble.className = "message-bubble";
  bubble.textContent = message.content;
  column.append(bubble);
  if (isAssistant && message.sources?.length) {
    const sources = document.createElement("div");
    sources.className = "source-list";
    const label = document.createElement("span");
    label.textContent = "参考资料";
    sources.append(label);
    for (const source of message.sources.slice(0, 4)) {
      const tag = document.createElement("span");
      tag.className = "source-tag";
      tag.textContent = source.title;
      sources.append(tag);
    }
    column.append(sources);
  }
  row.append(column);
  log.append(row);
  lastMessageId = message.id;
  log.scrollTop = log.scrollHeight;
}

function renderParticipants(names) {
  const unique = [...new Set(names)];
  $("#participant-count").textContent = `${unique.length} 位成员`;
  const stack = $("#participant-stack");
  const colors = ["#c48b7b", "#758e79", "#8580a4", "#7895a1", "#b7965f"];
  stack.innerHTML = unique.slice(0, 5).map((name, index) => `<span class="participant-avatar" title="${escapeHtml(name)}" style="--avatar-color:${colors[index % colors.length]}">${escapeHtml(name.slice(0, 1))}</span>`).join("") + (unique.length > 5 ? `<span class="participant-extra">+${unique.length - 5}</span>` : "");
}

async function syncRoom() {
  if (!token || polling) return;
  polling = true;
  try {
    const data = await api(`/api/messages?after=${lastMessageId}`);
    data.messages.forEach(appendMessage);
    renderParticipants(data.participants);
  } catch (error) {
    if (/加入阅读房间/.test(error.message)) logout();
    else console.warn("同步房间失败：", error.message);
  } finally {
    polling = false;
  }
}

function setJoined(user) {
  currentUser = user;
  $("#room-name").textContent = user.room;
  $("#mode-label").textContent = llmConfigured ? "Agent + RAG" : "本地 RAG";
  joinScreen.hidden = true;
  document.body.classList.add("joined");
  syncRoom();
}

function logout() {
  token = "";
  currentUser = null;
  lastMessageId = 0;
  localStorage.removeItem(tokenKey);
  log.replaceChildren();
  log.hidden = true;
  welcome.hidden = false;
  document.body.classList.remove("joined");
  joinScreen.hidden = false;
}

async function join(name, room) {
  const data = await api("/api/join", { method: "POST", body: JSON.stringify({ name, room }) });
  token = data.token;
  localStorage.setItem(tokenKey, token);
  setJoined(data.user);
}

async function sendMessage(text) {
  const value = text.trim();
  if (!value || busy) return;
  busy = true;
  input.disabled = true;
  try {
    await api("/api/messages", { method: "POST", body: JSON.stringify({ content: value }) });
    await syncRoom();
  } catch (error) {
    showToast(error.message);
  } finally {
    busy = false;
    input.disabled = false;
    input.focus();
  }
}

let toastTimer;
function showToast(text) {
  let toast = $("#toast");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "toast";
    toast.className = "toast";
    document.body.append(toast);
  }
  toast.textContent = text;
  toast.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("visible"), 3200);
}

async function uploadKnowledge(file) {
  if (!file) return;
  if (file.size > 500_000) return showToast("文件不能超过 500 KB");
  try {
    const content = await file.text();
    await api("/api/knowledge", { method: "POST", body: JSON.stringify({ title: file.name, content }) });
    showToast(`已添加「${file.name}」，房间成员都可以检索`);
  } catch (error) {
    showToast(error.message);
  }
}

joinForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = joinForm.querySelector("button[type=submit]");
  button.disabled = true;
  $("#join-error").textContent = "";
  try {
    await join($("#join-name").value, $("#join-room").value);
  } catch (error) {
    $("#join-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

chatForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = input.value;
  input.value = "";
  sendMessage(text);
});

document.addEventListener("click", (event) => {
  const prompt = event.target.closest("[data-prompt]");
  if (prompt) sendMessage(prompt.dataset.prompt);
  const book = event.target.closest("[data-book]");
  if (book) sendMessage(`介绍一下《${book.dataset.book}》`);
  if (event.target.closest("#show-library, #library-nav")) sendMessage("请从书库里推荐几本书，并简单介绍");
  if (event.target.closest("#upload-knowledge, #attach-knowledge")) $("#knowledge-file").click();
  if (event.target.closest("#leave-room")) logout();
});

$("#knowledge-file").addEventListener("change", (event) => {
  uploadKnowledge(event.target.files[0]);
  event.target.value = "";
});

async function boot() {
  try {
    const [health, bookData] = await Promise.all([api("/api/health"), api("/api/books")]);
    llmConfigured = health.llm_configured;
    $("#mode-label").textContent = llmConfigured ? "Agent + RAG" : "本地 RAG";
    books = bookData.books;
    renderTrending();
    if (token) {
      const data = await api("/api/me");
      if (data.user) setJoined(data.user);
    }
  } catch (error) {
    showToast("无法连接后端，请先运行 python server.py");
    console.error(error);
  }
}

setInterval(syncRoom, 2200);
boot();
