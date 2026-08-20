"""
MCP Server: Session History Reader
讀取 Claude Desktop Cowork 和 Claude Code CLI 的對話紀錄，
讓 agent 之間知道彼此（或上一次）討論了什麼。

- Cowork session: Desktop 主管用，存在 %APPDATA%/Claude/local-agent-mode-sessions/
- CLI session: Claude Code 用，存在 ~/.claude/projects/<project>/
"""
import json
import os
import glob
from datetime import datetime
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("cowork-history")

# === 路徑設定 ===

# Claude Desktop 的 local agent mode session 目錄。
# 2026-08 開發機由 WSL 換成 macOS：舊預設是 WSL 掛載的 Windows 路徑
# （/mnt/c/Users/charl/AppData/Roaming/Claude/…），在 macOS 上不存在。
# 兩邊都列出來取第一個存在的；仍可用 COWORK_SESSIONS_DIR 覆寫。
_COWORK_CANDIDATES = [
    os.path.expanduser("~/Library/Application Support/Claude/local-agent-mode-sessions"),  # macOS
    "/mnt/c/Users/charl/AppData/Roaming/Claude/local-agent-mode-sessions",                  # WSL→Windows
]
COWORK_SESSIONS_BASE = os.environ.get("COWORK_SESSIONS_DIR") or next(
    (p for p in _COWORK_CANDIDATES if os.path.isdir(p)), _COWORK_CANDIDATES[0]
)

CLI_SESSIONS_BASE = os.environ.get(
    "CLI_SESSIONS_DIR",
    os.path.expanduser("~/.claude/projects")
)

# 專案目錄對照表（CLI session 目錄名 → 人看得懂的名稱）
PROJECT_MAP = {
    "-mnt-d-gitDir-lawyer": "lawyer",
    "-mnt-d-gitDir-lawyerSupport": "lawyerSupport",
    "-mnt-d-gitDir-lawyer-services-semantic-search": "semantic-search",
}


# === 共用工具 ===

def _format_timestamp(ts):
    """timestamp 轉可讀時間。支援毫秒 int 和 ISO string"""
    if not ts:
        return ""
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts / 1000).strftime("%m/%d %H:%M")
        elif isinstance(ts, str):
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.strftime("%m/%d %H:%M")
    except (ValueError, OSError):
        pass
    return ""


def _parse_messages(file_path, limit=9999, role_filter=None, source_type="cowork"):
    """解析 JSONL 對話紀錄，提取人看得懂的對話"""
    messages = []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg = data.get("message", {})
                if not isinstance(msg, dict):
                    continue

                role = msg.get("role", "")
                if not role:
                    continue
                if role_filter and role != role_filter:
                    continue

                content = msg.get("content", "")
                text_parts = []
                timestamp = data.get("timestamp", "")

                if isinstance(content, str) and content.strip():
                    text_parts.append(content.strip())
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text" and block.get("text", "").strip():
                            text_parts.append(block["text"].strip())
                        elif block.get("type") == "tool_use":
                            tool_name = block.get("name", "unknown")
                            tool_input = block.get("input", {})
                            summary = json.dumps(tool_input, ensure_ascii=False)
                            if len(summary) > 200:
                                summary = summary[:200] + "..."
                            text_parts.append(f"[工具呼叫: {tool_name}] {summary}")
                        elif block.get("type") == "tool_result":
                            result_content = block.get("content", "")
                            if isinstance(result_content, str) and result_content.strip():
                                short = result_content[:300] + ("..." if len(result_content) > 300 else "")
                                text_parts.append(f"[工具結果] {short}")

                if text_parts:
                    combined = "\n".join(text_parts)
                    messages.append({
                        "role": role,
                        "content": combined,
                        "timestamp": timestamp,
                    })
    except OSError:
        pass

    return messages[-limit:]


# === Cowork (Desktop) ===

def _find_cowork_sessions():
    """找到所有 Cowork session"""
    sessions = []
    for meta_json in glob.glob(os.path.join(COWORK_SESSIONS_BASE, "*", "*", "local_*.json")):
        session_dir = meta_json.replace(".json", "")
        audit_file = os.path.join(session_dir, "audit.jsonl")
        if not os.path.exists(audit_file):
            continue
        try:
            with open(meta_json, "r", encoding="utf-8") as f:
                meta = json.load(f)
            sessions.append({
                "session_id": meta.get("sessionId", ""),
                "title": meta.get("title", "(untitled)"),
                "model": meta.get("model", ""),
                "created_at": meta.get("createdAt", 0),
                "last_activity": meta.get("lastActivityAt", 0),
                "initial_message": meta.get("initialMessage", ""),
                "folders": meta.get("userSelectedFolders", []),
                "file": audit_file,
                "source": "cowork",
            })
        except (json.JSONDecodeError, OSError):
            continue
    sessions.sort(key=lambda s: s["last_activity"], reverse=True)
    return sessions


# === CLI (Claude Code) ===

def _find_cli_sessions(project: str = ""):
    """找到所有 CLI session，可選過濾專案"""
    sessions = []
    project_dirs = []

    if project:
        # 找對應的目錄名
        target_dir = None
        for dir_name, friendly_name in PROJECT_MAP.items():
            if project.lower() == friendly_name.lower():
                target_dir = dir_name
                break
        if not target_dir:
            # 嘗試直接用原始目錄名
            target_dir = project
        full_path = os.path.join(CLI_SESSIONS_BASE, target_dir)
        if os.path.isdir(full_path):
            project_dirs.append((target_dir, full_path))
    else:
        # 列出所有專案
        if os.path.isdir(CLI_SESSIONS_BASE):
            for d in os.listdir(CLI_SESSIONS_BASE):
                full_path = os.path.join(CLI_SESSIONS_BASE, d)
                if os.path.isdir(full_path) and not d.startswith("."):
                    project_dirs.append((d, full_path))

    for dir_name, dir_path in project_dirs:
        friendly_name = PROJECT_MAP.get(dir_name, dir_name)
        for jsonl_file in glob.glob(os.path.join(dir_path, "*.jsonl")):
            try:
                stat = os.stat(jsonl_file)
                # 讀第一條 user message 作為標題
                first_user_msg = ""
                first_timestamp = ""
                session_id = os.path.basename(jsonl_file).replace(".jsonl", "")
                with open(jsonl_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if data.get("type") == "user":
                            msg = data.get("message", {})
                            content = msg.get("content", "")
                            if isinstance(content, str) and content.strip():
                                first_user_msg = content.strip()[:100]
                                first_timestamp = data.get("timestamp", "")
                                break

                sessions.append({
                    "session_id": session_id,
                    "title": first_user_msg or "(untitled)",
                    "model": "",
                    "created_at": first_timestamp,
                    "last_activity": stat.st_mtime * 1000,  # 轉毫秒
                    "initial_message": first_user_msg,
                    "project": friendly_name,
                    "file": jsonl_file,
                    "source": "cli",
                })
            except OSError:
                continue

    sessions.sort(key=lambda s: s["last_activity"], reverse=True)
    return sessions


# === Cowork 工具 ===

@mcp.tool()
def list_cowork_sessions(limit: int = 10) -> str:
    """列出最近的 Desktop Cowork 對話 session。

    Args:
        limit: 最多顯示幾個 session（預設 10）
    """
    sessions = _find_cowork_sessions()[:limit]
    if not sessions:
        return "找不到任何 Cowork session。"

    lines = [f"找到 {len(sessions)} 個 Cowork session：\n"]
    for i, s in enumerate(sessions):
        created = _format_timestamp(s["created_at"])
        last = _format_timestamp(s["last_activity"])
        folders = ", ".join(s["folders"]) if s.get("folders") else "(無)"
        lines.append(
            f"{i+1}. **{s['title']}**\n"
            f"   ID: `{s['session_id']}`\n"
            f"   時間: {created} ~ {last} | 模型: {s['model']}\n"
            f"   資料夾: {folders}\n"
            f"   首句: {s['initial_message'][:100]}\n"
        )
    return "\n".join(lines)


@mcp.tool()
def read_cowork_session(
    session_id: str = "",
    session_index: int = 0,
    limit: int = 9999,
    summary: bool = False,
) -> str:
    """讀取指定 Desktop Cowork session 的完整對話紀錄。

    Args:
        session_id: Session ID（從 list_cowork_sessions 取得）。留空則用 session_index。
        session_index: 用序號選擇（0=最新，1=次新...）。session_id 優先。
        limit: 最多回傳幾條訊息（預設 9999 = 全部）
        summary: True 則只回傳 user 和 assistant 的文字訊息（省略工具呼叫）
    """
    sessions = _find_cowork_sessions()
    if not sessions:
        return "找不到任何 Cowork session。"

    target = None
    if session_id:
        for s in sessions:
            if s["session_id"] == session_id:
                target = s
                break
        if not target:
            return f"找不到 session: {session_id}"
    else:
        if session_index < 0 or session_index >= len(sessions):
            return f"session_index {session_index} 超出範圍（共 {len(sessions)} 個 session）"
        target = sessions[session_index]

    messages = _parse_messages(target["file"], limit=limit)

    if summary:
        messages = [
            m for m in messages
            if m["role"] in ("user", "assistant")
            and not m["content"].startswith("[工具")
        ]

    if not messages:
        return f"Session「{target['title']}」沒有對話紀錄。"

    created = _format_timestamp(target["created_at"])
    last = _format_timestamp(target["last_activity"])
    header = (
        f"## Cowork Session: {target['title']}\n"
        f"時間: {created} ~ {last} | 模型: {target['model']}\n"
        f"資料夾: {', '.join(target.get('folders', [])) or '(無)'}\n"
        f"訊息數: {len(messages)}\n\n---\n\n"
    )

    lines = []
    for m in messages:
        role_label = "👤 使用者" if m["role"] == "user" else "🤖 助手"
        content = m["content"]
        if len(content) > 2000:
            content = content[:2000] + "\n...(截斷)"
        lines.append(f"**{role_label}**:\n{content}\n")

    return header + "\n---\n\n".join(lines)


@mcp.tool()
def search_cowork_sessions(keyword: str, limit: int = 20) -> str:
    """在所有 Desktop Cowork session 中搜尋關鍵字。

    Args:
        keyword: 搜尋的關鍵字
        limit: 最多回傳幾條符合的訊息（預設 20）
    """
    if not keyword.strip():
        return "請提供搜尋關鍵字。"

    sessions = _find_cowork_sessions()
    results = []

    for s in sessions:
        messages = _parse_messages(s["file"], limit=9999)
        for m in messages:
            if keyword.lower() in m["content"].lower():
                snippet = m["content"]
                if len(snippet) > 300:
                    idx = snippet.lower().find(keyword.lower())
                    start = max(0, idx - 100)
                    end = min(len(snippet), idx + len(keyword) + 200)
                    snippet = ("..." if start > 0 else "") + snippet[start:end] + ("..." if end < len(snippet) else "")
                results.append({
                    "session_title": s["title"],
                    "session_id": s["session_id"],
                    "role": m["role"],
                    "content": snippet,
                    "last_activity": _format_timestamp(s["last_activity"]),
                })
                if len(results) >= limit:
                    break
        if len(results) >= limit:
            break

    if not results:
        return f"在所有 Cowork session 中找不到「{keyword}」。"

    lines = [f"搜尋「{keyword}」找到 {len(results)} 筆結果：\n"]
    for r in results:
        role_label = "使用者" if r["role"] == "user" else "助手"
        lines.append(
            f"**{r['session_title']}** ({r['last_activity']})\n"
            f"[{role_label}] {r['content']}\n"
        )
    return "\n---\n\n".join(lines)


# === CLI 工具 ===

@mcp.tool()
def list_cli_sessions(project: str = "", limit: int = 10) -> str:
    """列出最近的 Claude Code CLI 對話 session。

    Args:
        project: 專案名稱過濾（如 "lawyer"、"lawyerSupport"）。留空列出所有專案。
        limit: 最多顯示幾個 session（預設 10）
    """
    sessions = _find_cli_sessions(project)[:limit]
    if not sessions:
        proj_hint = f"（專案: {project}）" if project else ""
        return f"找不到任何 CLI session{proj_hint}。"

    lines = [f"找到 {len(sessions)} 個 CLI session：\n"]
    for i, s in enumerate(sessions):
        last = _format_timestamp(s["last_activity"])
        lines.append(
            f"{i+1}. **[{s['project']}]** {s['title']}\n"
            f"   ID: `{s['session_id']}`\n"
            f"   最後活動: {last}\n"
        )
    return "\n".join(lines)


@mcp.tool()
def read_cli_session(
    session_id: str = "",
    session_index: int = 0,
    project: str = "",
    limit: int = 9999,
    summary: bool = False,
) -> str:
    """讀取指定 Claude Code CLI session 的完整對話紀錄。

    Args:
        session_id: Session ID（從 list_cli_sessions 取得）。留空則用 session_index。
        session_index: 用序號選擇（0=最新，1=次新...）。session_id 優先。
        project: 專案名稱過濾（如 "lawyer"）。搭配 session_index 使用。
        limit: 最多回傳幾條訊息（預設 9999 = 全部）
        summary: True 則只回傳 user 和 assistant 的文字（省略工具呼叫）
    """
    sessions = _find_cli_sessions(project)
    if not sessions:
        return "找不到任何 CLI session。"

    target = None
    if session_id:
        for s in sessions:
            if s["session_id"] == session_id:
                target = s
                break
        if not target:
            return f"找不到 session: {session_id}"
    else:
        if session_index < 0 or session_index >= len(sessions):
            return f"session_index {session_index} 超出範圍（共 {len(sessions)} 個 session）"
        target = sessions[session_index]

    messages = _parse_messages(target["file"], limit=limit)

    if summary:
        messages = [
            m for m in messages
            if m["role"] in ("user", "assistant")
            and not m["content"].startswith("[工具")
        ]

    if not messages:
        return f"Session「{target['title']}」沒有對話紀錄。"

    last = _format_timestamp(target["last_activity"])
    header = (
        f"## CLI Session [{target['project']}]: {target['title']}\n"
        f"最後活動: {last}\n"
        f"訊息數: {len(messages)}\n\n---\n\n"
    )

    lines = []
    for m in messages:
        role_label = "👤 使用者" if m["role"] == "user" else "🤖 助手"
        content = m["content"]
        if len(content) > 2000:
            content = content[:2000] + "\n...(截斷)"
        lines.append(f"**{role_label}**:\n{content}\n")

    return header + "\n---\n\n".join(lines)


@mcp.tool()
def search_cli_sessions(keyword: str, project: str = "", limit: int = 20) -> str:
    """在 Claude Code CLI session 中搜尋關鍵字。

    Args:
        keyword: 搜尋的關鍵字
        project: 專案名稱過濾（如 "lawyer"）。留空搜尋所有專案。
        limit: 最多回傳幾條符合的訊息（預設 20）
    """
    if not keyword.strip():
        return "請提供搜尋關鍵字。"

    sessions = _find_cli_sessions(project)
    results = []

    for s in sessions:
        messages = _parse_messages(s["file"], limit=9999)
        for m in messages:
            if keyword.lower() in m["content"].lower():
                snippet = m["content"]
                if len(snippet) > 300:
                    idx = snippet.lower().find(keyword.lower())
                    start = max(0, idx - 100)
                    end = min(len(snippet), idx + len(keyword) + 200)
                    snippet = ("..." if start > 0 else "") + snippet[start:end] + ("..." if end < len(snippet) else "")
                results.append({
                    "project": s["project"],
                    "session_title": s["title"],
                    "session_id": s["session_id"],
                    "role": m["role"],
                    "content": snippet,
                    "last_activity": _format_timestamp(s["last_activity"]),
                })
                if len(results) >= limit:
                    break
        if len(results) >= limit:
            break

    if not results:
        proj_hint = f"（專案: {project}）" if project else ""
        return f"在 CLI session 中找不到「{keyword}」{proj_hint}。"

    lines = [f"搜尋「{keyword}」找到 {len(results)} 筆結果：\n"]
    for r in results:
        role_label = "使用者" if r["role"] == "user" else "助手"
        lines.append(
            f"**[{r['project']}] {r['session_title']}** ({r['last_activity']})\n"
            f"[{role_label}] {r['content']}\n"
        )
    return "\n---\n\n".join(lines)


if __name__ == "__main__":
    mcp.run()
