"""小书童 backend: stdlib HTTP server, SQLite persistence, retrieval and optional LLM."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "xiaoshutong.sqlite3"
BOOKS = json.loads((ROOT / "books.json").read_text(encoding="utf-8"))
DB_LOCK = threading.RLock()
MAX_BODY = 600_000


def load_dotenv():
    """Load local .env values without overriding existing shell environment."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


load_dotenv()


def connect():
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def initialize():
    with DB_LOCK, connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY, display_name TEXT NOT NULL, room TEXT NOT NULL,
          last_seen REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
          created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT NOT NULL,
          user_id INTEGER NOT NULL REFERENCES users(id), role TEXT NOT NULL,
          content TEXT NOT NULL, sources TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS knowledge (
          id INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT NOT NULL, title TEXT NOT NULL,
          content TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS messages_room_id ON messages(room, id);
        CREATE INDEX IF NOT EXISTS knowledge_room ON knowledge(room);
        """)


def now_iso(epoch=None):
    return datetime.fromtimestamp(epoch or time.time(), timezone.utc).isoformat(timespec="seconds")


def clean_text(value, limit=4000):
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value or "")).strip()[:limit]


def tokenize(text):
    text = str(text).lower()
    tokens = re.findall(r"[a-z0-9_]{2,}|[\u3400-\u9fff]+", text)
    grams = {token for token in tokens if not re.fullmatch(r"[\u3400-\u9fff]+", token)}
    for token in tokens:
        if re.fullmatch(r"[\u3400-\u9fff]+", token):
            grams.update(token)
            grams.update(token[i:i + 2] for i in range(len(token) - 1))
    return grams


def retrieve(query, room, limit=4):
    documents = []
    for book in BOOKS:
        body = f"{book['title']}。作者：{book['author']}。类别：{book['category']}。简介：{book['blurb']}。价格：{book['price']}。评分：{book['rating']}。"
        documents.append({"title": book["title"], "content": body, "type": "book"})
    with DB_LOCK, connect() as db:
        rows = db.execute("SELECT title, content FROM knowledge WHERE room IN ('*', ?) ORDER BY id DESC", (room,)).fetchall()
    for row in rows:
        content = row["content"]
        step, width = 650, 750
        for index, start in enumerate(range(0, max(1, len(content)), step), 1):
            chunk = content[start:start + width]
            documents.append({"title": row["title"], "content": chunk, "type": "knowledge", "chunk": index})
    query_terms = tokenize(query)
    if not query_terms:
        return []
    scored = []
    for doc in documents:
        terms = tokenize(doc["title"] + " " + doc["content"])
        overlap = query_terms & terms
        if overlap:
            score = len(overlap) / max(1, len(query_terms)) + (0.35 if any(q in doc["title"].lower() for q in query_terms if len(q) > 1) else 0)
            scored.append((score, doc))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [doc for score, doc in scored[:limit] if score > 0.06]


def local_agent_reply(message, docs):
    text = message.lower()
    if re.search(r"退|退款|售后|客服|支付|付款|设备", text):
        return "这个问题需要结合店铺的实际政策来确认，我这里还没有接入售后或订单数据。请联系店铺客服获取准确答复。"
    if re.search(r"优惠|活动|便宜|贵|价格|折", text):
        return "我目前没有实时促销信息，清单中的优惠只是演示数据。正式使用前请核实有效期和使用条件；也可以告诉我预算，我先按书目帮你筛选。"
    books = [d for d in docs if d["type"] == "book"]
    if books:
        lines = []
        for doc in books[:3]:
            book = next((b for b in BOOKS if b["title"] == doc["title"]), None)
            if book:
                lines.append(f"《{book['title']}》——{book['blurb']}（{book['category']}，示例价格 {book['price']}）")
        if lines:
            return "我从书库里找到这些可能合适的选择：\n" + "\n".join(lines) + "\n这些是示例书目信息。你更想先了解哪一本？"
    if docs:
        return f"我在房间知识库的「{docs[0]['title']}」中找到一些相关内容：\n{docs[0]['content'][:650]}"
    return "我还没有找到足够相关的书目信息。你可以说说偏好的主题、阅读场景或预算，我再帮你缩小范围。"


def agent_reply(message, room, docs, message_id):
    api_key = (os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY", "")).strip()
    if not api_key:
        return local_agent_reply(message, docs)
    base = (os.getenv("DEEPSEEK_BASE_URL") or os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com")).rstrip("/")
    model = os.getenv("DEEPSEEK_MODEL") or os.getenv("OPENAI_MODEL", "deepseek-flash")
    context = "\n\n".join(f"[{d['title']}]\n{d['content']}" for d in docs) or "无匹配知识。"
    with DB_LOCK, connect() as db:
        previous = db.execute("SELECT role, content FROM messages WHERE room=? AND id<? AND role IN ('user', 'assistant') ORDER BY id DESC LIMIT 12", (room, message_id)).fetchall()
    history = [{"role": row["role"], "content": row["content"]} for row in reversed(previous)]
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是书巢的阅读顾问小书童。回答简短友善，最多推荐3本书。只能根据提供的检索资料描述书籍和价格；没有资料时明确说明不知道。优惠、售后政策不得编造。回答用中文。\n检索资料：\n" + context},
            *history,
            {"role": "user", "content": message}],
        "temperature": 0.5,
    }
    req = urllib.request.Request(base + "/chat/completions", data=json.dumps(payload).encode(), headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=35) as response:
            result = json.loads(response.read().decode("utf-8"))
        return clean_text(result["choices"][0]["message"]["content"], 4000)
    except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError, TimeoutError) as error:
        print(f"LLM request failed; using local agent: {error}", flush=True)
        return local_agent_reply(message, docs)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}", flush=True)

    def json_response(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_BODY:
            raise ValueError("请求内容太大")
        return json.loads(self.rfile.read(length) or b"{}")

    def current_user(self):
        token = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not token:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with DB_LOCK, connect() as db:
            row = db.execute("SELECT u.id, u.display_name, u.room FROM users u JOIN sessions s ON s.user_id=u.id WHERE s.token_hash=?", (digest,)).fetchone()
            if row:
                db.execute("UPDATE users SET last_seen=? WHERE id=?", (time.time(), row["id"]))
        return dict(row) if row else None

    def require_user(self):
        user = self.current_user()
        if not user:
            self.json_response(401, {"error": "请先加入阅读房间"})
        return user

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            return self.json_response(200, {"ok": True, "llm_configured": bool(os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY"))})
        if parsed.path == "/api/me":
            user = self.current_user()
            return self.json_response(200, {"user": user})
        if parsed.path == "/api/books":
            return self.json_response(200, {"books": BOOKS})
        if parsed.path == "/api/messages":
            user = self.require_user()
            if not user: return
            params = parse_qs(parsed.query)
            try: after = max(0, int(params.get("after", ["0"])[0]))
            except ValueError: after = 0
            with DB_LOCK, connect() as db:
                if after == 0:
                    rows = db.execute("SELECT * FROM (SELECT m.id, m.role, m.content, m.sources, m.created_at, u.display_name FROM messages m JOIN users u ON u.id=m.user_id WHERE m.room=? ORDER BY m.id DESC LIMIT 100) ORDER BY id", (user["room"],)).fetchall()
                else:
                    rows = db.execute("SELECT m.id, m.role, m.content, m.sources, m.created_at, u.display_name FROM messages m JOIN users u ON u.id=m.user_id WHERE m.room=? AND m.id>? ORDER BY m.id LIMIT 100", (user["room"], after)).fetchall()
                people = db.execute("SELECT display_name FROM users WHERE room=? AND last_seen>? ORDER BY display_name LIMIT 30", (user["room"], time.time() - 90)).fetchall()
            messages = [{"id": r["id"], "role": r["role"], "content": r["content"], "sources": json.loads(r["sources"]), "created_at": now_iso(r["created_at"]), "display_name": r["display_name"]} for r in rows]
            return self.json_response(200, {"messages": messages, "participants": [p["display_name"] for p in people]})
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            data = self.read_json()
        except (ValueError, json.JSONDecodeError) as error:
            return self.json_response(400, {"error": str(error) or "JSON 格式无效"})
        if parsed.path == "/api/join":
            name = clean_text(data.get("name"), 32)
            room = clean_text(data.get("room"), 48)
            if not name or not room:
                return self.json_response(400, {"error": "请填写昵称和房间名"})
            token = secrets.token_urlsafe(32)
            with DB_LOCK, connect() as db:
                cursor = db.execute("INSERT INTO users(display_name, room, last_seen) VALUES (?, ?, ?)", (name, room, time.time()))
                db.execute("INSERT INTO sessions(token_hash, user_id, created_at) VALUES (?, ?, ?)", (hashlib.sha256(token.encode()).hexdigest(), cursor.lastrowid, time.time()))
            return self.json_response(200, {"token": token, "user": {"display_name": name, "room": room}})
        user = self.require_user()
        if not user: return
        if parsed.path == "/api/knowledge":
            title = clean_text(data.get("title"), 120) or "共享阅读笔记"
            content = clean_text(data.get("content"), 500_000)
            if len(content) < 20:
                return self.json_response(400, {"error": "请提供至少 20 个字的文本"})
            with DB_LOCK, connect() as db:
                db.execute("INSERT INTO knowledge(room, title, content, created_at) VALUES (?, ?, ?, ?)", (user["room"], title, content, time.time()))
            return self.json_response(201, {"ok": True, "title": title})
        if parsed.path == "/api/messages":
            message = clean_text(data.get("content"), 3000)
            if not message:
                return self.json_response(400, {"error": "请输入消息"})
            with DB_LOCK, connect() as db:
                cursor = db.execute("INSERT INTO messages(room, user_id, role, content, created_at) VALUES (?, ?, 'user', ?, ?)", (user["room"], user["id"], message, time.time()))
                user_message_id = cursor.lastrowid
            docs = retrieve(message, user["room"])
            answer = agent_reply(message, user["room"], docs, user_message_id)
            sources = [{"title": doc["title"], "type": doc["type"]} for doc in docs]
            with DB_LOCK, connect() as db:
                cursor = db.execute("INSERT INTO messages(room, user_id, role, content, sources, created_at) VALUES (?, ?, 'assistant', ?, ?, ?)", (user["room"], user["id"], answer, json.dumps(sources, ensure_ascii=False), time.time()))
                answer_id = cursor.lastrowid
            return self.json_response(201, {"user_message_id": user_message_id, "answer_id": answer_id})
        self.json_response(404, {"error": "未找到 API"})


if __name__ == "__main__":
    initialize()
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    print(f"小书童运行中：http://{host}:{port}", flush=True)
    print("共享局域网访问时可设置 HOST=0.0.0.0；配置模型参见 .env.example。", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
