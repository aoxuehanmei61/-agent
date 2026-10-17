# 小书童 · 多人阅读顾问

一个可在 VS Code 中直接运行的前后端阅读顾问工程，包含共享房间、Agent 接口和本地 RAG 检索。后端仅使用 Python 标准库，不依赖 JDK、Node.js 或第三方 Python 包。

## 启动

在 VS Code 终端（PowerShell）运行：

```powershell
python server.py
```

浏览器打开 `http://127.0.0.1:8000`。首次进入填写昵称和房间名；其他成员打开相同地址并填写相同房间名，就能看到共享对话。首次运行会在项目目录创建 `xiaoshutong.sqlite3`，用于保存房间消息、会话和知识资料。

同一局域网的其他设备访问时，在启动前设置监听地址：

```powershell
$env:HOST = "0.0.0.0"
python server.py
```

其他设备打开运行电脑的局域网 IP 地址，例如 `http://192.168.1.20:8000`。Windows 防火墙可能需要允许 Python 接受局域网连接。

## 功能

- **前端**：书巢聊天界面，支持手机与桌面布局、快捷问题、书目浏览、共享资料上传。
- **后端**：Python 标准库 HTTP 服务、SQLite 持久化、按房间隔离的会话/消息/知识库。
- **多人房间**：浏览器每约 2.2 秒同步新消息和在线成员；同一房间中的对话和上传资料共享。
- **Agent**：可选 OpenAI-compatible Chat Completions 服务；未配置模型时使用本地 RAG 回复。
- **RAG**：内置示例书库，并可上传 `.txt`、`.md`、`.csv` 文本；上传内容切块后按中文/英文词项做本地检索，回答会显示参考资料。

## 配置 DeepSeek

将 `.env.example` 复制为 `.env`，在本机 `.env` 中填写 `LLM_API_KEY`，保存后重启服务。密钥只由 Python 后端读取，不会发送给网页前端；`.env` 已加入 `.gitignore`。不要把 `.env` 提交到 Git 仓库。

当前示例使用 DeepSeek Chat Completions 的 `https://api.deepseek.com/v1` 地址和 `deepseek-chat` 模型。可在 `.env` 中调整 `LLM_BASE_URL`、`LLM_MODEL`。

## 通过 ngrok 分享

Windows 尚未安装 ngrok 时，可在 PowerShell 安装：

```powershell
winget install ngrok -s msstore --accept-source-agreements --accept-package-agreements
```

从 ngrok Dashboard 复制 authtoken，在终端配置一次：

```powershell
ngrok config add-authtoken <你的-ngrok-authtoken>
```

之后先在一个 VS Code 终端启动后端：

```powershell
python server.py
```

再开第二个终端，给本机的 8000 端口建立 HTTPS 隧道：

```powershell
ngrok http 8000
```

把 ngrok 输出的 `https://...ngrok-free.app` 地址发给参与者，他们填写同一个房间名即可加入。停止分享时，在运行 ngrok 的终端按 `Ctrl+C`。

ngrok 会把这个本地应用暴露到公网。当前房间没有密码验证；只把隧道地址发给可信的人，测试完就关闭隧道。不要把 DeepSeek key 放到聊天框或前端代码里。

## 文件

- `index.html`：聊天、加入房间与资料上传界面。
- `styles.css`：响应式页面样式。
- `app.js`：前端 API 调用、消息同步与 UI 渲染。
- `server.py`：HTTP API、SQLite、RAG 检索和 Agent 调用。
- `books.json`：示例书目。
- `.env.example`：不含密钥的模型与服务监听配置模板。
- `.env`：本机密钥配置文件，不要分享。

## 当前边界

这个版本适合本机或可信局域网试用。加入房间只需要昵称和房间名，没有密码、账号验证、房间权限管理或上传审批；房间名也不能作为隐私保护。部署到公网前应增加正式认证、速率限制、HTTPS、上传安全策略和数据备份。多人同步使用轮询实现；RAG 当前为轻量词项检索，尚未接入向量数据库或网页搜索。清单中的书名、价格和评分是示例数据。
