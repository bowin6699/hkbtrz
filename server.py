import os
import json
import uuid
import hashlib
import secrets
import socket
import mimetypes
# Register Office MIME types for proper browser preview
mimetypes.add_type("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx")
mimetypes.add_type("application/msword", ".doc")
mimetypes.add_type("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx")
mimetypes.add_type("application/vnd.ms-excel", ".xls")
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
import uvicorn, time

app = FastAPI(title="投融资部信息智能化平台")

# ── Config ──────────────────────────────────────────────
CONFIG_FILE = Path(__file__).parent / "config.json"
USERS_FILE = Path(__file__).parent / "users.json"
PORT = 8000

def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}

CONFIG = load_config()
WIN_PASSWORD = CONFIG.get("win_password", "")
SESSION_EXPIRE_SECONDS = CONFIG.get("session_expire_hours", 12) * 3600
RATE_LIMIT = CONFIG.get("rate_limit_per_minute", 30)

# ── Rate limiter ────────────────────────────────────────
rate_store: dict[str, list] = {}  # ip -> [timestamps]

def get_client_ip(request: Request) -> str:
    # Use X-Forwarded-For from nginx proxy, fallback to direct IP
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[0].strip() if forwarded else request.client.host

def check_rate_limit(ip: str):
    now = time.time()
    rate_store.setdefault(ip, [])
    rate_store[ip] = [t for t in rate_store[ip] if now - t < 60]
    if len(rate_store[ip]) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    rate_store[ip].append(now)

# ── Security headers middleware ─────────────────────────
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        resp.headers["X-XSS-Protection"] = "1; mode=block"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        resp.headers["Cache-Control"] = "no-store"
        return resp

app.add_middleware(SecurityHeadersMiddleware)

# ── User storage ────────────────────────────────────────
def load_users() -> dict:
    if USERS_FILE.exists():
        with open(USERS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_users(users: dict):
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}${h}"

def verify_password(password: str, stored: str) -> bool:
    salt, h = stored.split("$", 1)
    return hashlib.sha256((salt + password).encode()).hexdigest() == h

# Init: seed admin account if not exists
def init_admin():
    users = load_users()
    config = load_config()
    admin_pwd = config.get("admin_password", "")
    if not admin_pwd:
        print("[错误] config.json 中未设置 admin_password，请配置后重新启动")
        print("示例: {\"admin_password\": \"你的密码\"}")
        return
    if "bowin6699" not in users:
        users["bowin6699"] = {
            "password": hash_password(admin_pwd),
            "role": "admin",
            "name": "管理员",
            "department": "管理部",
            "approved": True,
            "created_at": datetime.now().isoformat(),
        }
        save_users(users)

init_admin()

# ── Auth helpers ────────────────────────────────────────
# Session store with timestamps: token -> {"username": str, "created": float}
sessions: dict[str, dict] = {}

def get_current_user(request: Request) -> dict:
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        token = request.query_params.get("token", "")
    if not token:
        token = request.cookies.get("session_token", "")
    sess = sessions.get(token)
    if not sess:
        raise HTTPException(status_code=401, detail="未登录，请先登录")
    # Check session expiry
    if time.time() - sess["created"] > SESSION_EXPIRE_SECONDS:
        sessions.pop(token, None)
        raise HTTPException(status_code=401, detail="会话已过期，请重新登录")
    username = sess["username"]
    users = load_users()
    user = users.get(username)
    if not user or not user.get("approved"):
        raise HTTPException(status_code=403, detail="账号未通过审核")
    if user.get("locked_until"):
        lt = datetime.fromisoformat(user["locked_until"])
        if datetime.now() < lt:
            raise HTTPException(status_code=403, detail=f"账号已锁定，请{lt.strftime('%H:%M')}后重试")
    return {"username": username, **user}

def require_admin(request: Request):
    user = get_current_user(request)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user

# ── File search helpers ─────────────────────────────────
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".ico", ".heic", ".tiff", ".tif", ".raw", ".cr2"}
PDF_EXTS = {".pdf"}
WORD_EXTS = {".doc", ".docx"}
EXCEL_EXTS = {".xls", ".xlsx", ".csv"}
DOC_EXTS = PDF_EXTS | WORD_EXTS | EXCEL_EXTS
WORD_EXTS = {".doc", ".docx"}
EXCEL_EXTS = {".xls", ".xlsx", ".csv"}
DOC_EXTS = PDF_EXTS | WORD_EXTS | EXCEL_EXTS

def get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def format_size(size_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"

# ══════════════════════════════════════════════════════════
#  API: Auth
# ══════════════════════════════════════════════════════════

@app.post("/api/login")
async def login(request: Request):
    check_rate_limit(get_client_ip(request))
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")
    if not username or not password:
        raise HTTPException(status_code=400, detail="请输入账号和密码")
    users = load_users()
    user = users.get(username)
    if not user:
        raise HTTPException(status_code=400, detail="账号不存在")
    # Check lockout
    if user.get("locked_until"):
        lt = datetime.fromisoformat(user["locked_until"])
        if datetime.now() < lt:
            raise HTTPException(status_code=403, detail=f"账号已锁定，请{lt.strftime('%H:%M')}后重试")
    if not verify_password(password, user["password"]):
        # Increment failed count, lock after 5
        user["failed_count"] = user.get("failed_count", 0) + 1
        if user["failed_count"] >= 5:
            user["locked_until"] = (datetime.now() + timedelta(minutes=30)).isoformat()
            save_users(users)
            raise HTTPException(status_code=403, detail="密码错误5次，账号锁定30分钟")
        save_users(users)
        raise HTTPException(status_code=400, detail=f"密码错误（剩余{5 - user['failed_count']}次尝试）")
    if not user.get("approved"):
        raise HTTPException(status_code=403, detail="账号尚未通过审核，请联系管理员")
    # Reset failed count on success
    user["failed_count"] = 0
    user.pop("locked_until", None)
    save_users(users)
    token = secrets.token_urlsafe(32)
    sessions[token] = {"username": username, "created": time.time()}
    resp = JSONResponse({"token": token, "username": username, "role": user["role"], "name": user.get("name", "")})
    resp.set_cookie("session_token", token, httponly=True, samesite="lax", max_age=86400)
    return resp


@app.post("/api/register")
async def register(request: Request):
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")
    name = body.get("name", "").strip()
    department = body.get("department", "").strip()
    if not username or not password:
        raise HTTPException(status_code=400, detail="请输入账号和密码")
    if not name:
        raise HTTPException(status_code=400, detail="请输入真实姓名")
    if not department:
        raise HTTPException(status_code=400, detail="请填写部门信息")
    if len(username) < 3:
        raise HTTPException(status_code=400, detail="账号至少3个字符")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="密码至少6位")
    users = load_users()
    if username in users:
        raise HTTPException(status_code=400, detail="账号已存在")
    users[username] = {
        "password": hash_password(password),
        "role": "user",
        "name": name,
        "department": department,
        "approved": False,
        "created_at": datetime.now().isoformat(),
    }
    save_users(users)
    return {"message": "注册申请已提交，请等待管理员审核"}


@app.get("/api/me")
async def me(request: Request):
    user = get_current_user(request)
    return {"username": user["username"], "role": user["role"], "name": user.get("name", ""), "department": user.get("department", "")}


@app.post("/api/logout")
async def logout(request: Request):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    sessions.pop(token, None)
    return {"message": "已退出登录"}


# ══════════════════════════════════════════════════════════
#  API: Admin
# ══════════════════════════════════════════════════════════

@app.get("/api/admin/pending")
async def admin_pending(request: Request):
    require_admin(request)
    users = load_users()
    pending = []
    for uname, u in users.items():
        if not u.get("approved") and u.get("role") != "admin":
            pending.append({
                "username": uname,
                "name": u.get("name", ""),
                "department": u.get("department", ""),
                "created_at": u.get("created_at", ""),
            })
    return {"pending": pending}


@app.post("/api/admin/approve")
async def admin_approve(request: Request):
    require_admin(request)
    body = await request.json()
    username = body.get("username", "").strip()
    action = body.get("action", "")  # "approve" or "reject"
    if not username or action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="参数错误")
    users = load_users()
    if username not in users:
        raise HTTPException(status_code=404, detail="用户不存在")
    if action == "approve":
        users[username]["approved"] = True
    else:
        del users[username]
    save_users(users)
    return {"message": "操作成功"}


# ══════════════════════════════════════════════════════════
#  API: Search & Download (protected)
# ══════════════════════════════════════════════════════════

@app.get("/api/info")
async def server_info(request: Request):
    get_current_user(request)
    return {"ip": get_lan_ip(), "port": PORT}


@app.get("/api/search")
async def search(
    request: Request,
    q: str = Query(default="", description="搜索关键词"),
    type: str = Query(default="all", description="文件类型: image, pdf, word, excel, folder, all"),
    dir: str = Query(..., description="搜索根目录"),
):
    check_rate_limit(get_client_ip(request))
    get_current_user(request)
    base_dir = os.path.expanduser(dir)
    if not base_dir.startswith("/mnt/") and not os.path.isdir(base_dir):
        raise HTTPException(status_code=400, detail=f"目录不存在: {base_dir}")
    base_dir = os.path.realpath(base_dir)
    keyword = q.strip().lower()
    browse_all = not keyword
    if browse_all:
        keyword = "*"  # Show all files
    results = []
    max_results = 2000

    # For network mounts, use SSH directly to Windows (avoids sshfs issues)
    if base_dir.startswith("/mnt/office/YL"):
        import subprocess
        # Map mount point to Windows drive
        drive_map = {"YLd": "D:", "YLe": "E:", "YLf": "F:"}
        # Extract drive label from path: /mnt/office/YLd/... → YLd
        parts = base_dir[len("/mnt/office/"):].split("/")
        label = parts[0] if parts else ""
        win_dir = drive_map.get(label, "")
        if win_dir:
            # Convert path from /mnt/office/YLx/subdir to F:\subdir
            rel = "/".join(parts[1:]) if len(parts) > 1 else ""
            rel_win = rel.replace("/", "\\")
            if rel_win:
                win_path = win_dir + "\\" + rel_win + "\\"
            else:
                win_path = win_dir + "\\"
            try:
                # Build dir pattern: * for all files, *keyword* for specific search
                if keyword == "*":
                    pattern = "*"
                else:
                    pattern = f"*{keyword}*"
                if rel:
                    dir_cmd = f"dir \"{win_path}{pattern}\" /s /b 2>nul"
                else:
                    dir_cmd = f"dir \"{win_path}{pattern}\" /b 2>nul"
                cmd = ["sshpass", "-p", WIN_PASSWORD, "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
                       "-p", "2222", "Administrator@localhost", dir_cmd]
                proc = subprocess.run(cmd, capture_output=True, timeout=45)
                # Windows SSH returns GBK, decode properly
                try:
                    output = proc.stdout.decode("gbk")
                except:
                    output = proc.stdout.decode("utf-8", errors="replace")
                for line in output.strip().split("\n"):
                    if len(results) >= max_results:
                        break
                    if not line or line.strip() == "":
                        continue
                    win_path = line.strip()
                    # Root-level dir /b returns just filename; prepend search path
                    if "\\" not in win_path and "/" not in win_path:
                        win_path = win_path.replace("/", "\\")
                    if ":" not in win_path:
                        win_path = win_dir + "\\" + win_path
                    name = os.path.basename(win_path)
                    ext = os.path.splitext(name)[1].lower()
                    # Replace Windows backslashes and map to /mnt/office/ path
                    path = win_path.replace("\\", "/")
                    for lbl, drv in drive_map.items():
                        if path.startswith(drv) or path.startswith(drv.lower()):
                            path = path.replace(drv, f"/mnt/office/{lbl}", 1)
                            break
                    # Check if directory (no extension)
                    if not ext:
                        if type in ("folder", "all"):
                            results.append({"name": name, "path": path, "type": "folder", "size": "—", "size_bytes": 0, "modified": ""})
                    else:
                        if type == "folder":
                            continue
                        if type == "image" and ext not in IMAGE_EXTS:
                            continue
                        if type == "pdf" and ext not in PDF_EXTS:
                            continue
                        if type == "word" and ext not in WORD_EXTS:
                            continue
                        if type == "excel" and ext not in EXCEL_EXTS:
                            continue
                        if type == "all" and ext not in IMAGE_EXTS and ext not in DOC_EXTS:
                            continue
                        if ext in IMAGE_EXTS:
                            file_type = "image"
                        elif ext in PDF_EXTS:
                            file_type = "pdf"
                        elif ext in WORD_EXTS:
                            file_type = "word"
                        elif ext in EXCEL_EXTS:
                            file_type = "excel"
                        else:
                            continue
                        results.append({"name": name, "path": path, "type": file_type, "size": "—", "size_bytes": 0, "modified": ""})
            except subprocess.TimeoutExpired:
                pass
    else:
        base_depth = base_dir.rstrip(os.sep).count(os.sep)
        for root, dirs, files in os.walk(base_dir):
            if len(results) >= max_results:
                break
            if root.count(os.sep) - base_depth > 4:
                dirs.clear()
                continue
            if type in ("folder", "all"):
                for d in dirs:
                    if len(results) >= max_results:
                        break
                    if keyword == "*" or keyword in d.lower():
                        full_path = os.path.join(root, d)
                        st = os.stat(full_path)
                        results.append({
                            "name": d, "path": full_path, "type": "folder",
                            "size": "—", "size_bytes": 0,
                            "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                        })
            if type != "folder":
                for f in files:
                    if len(results) >= max_results:
                        break
                    ext = os.path.splitext(f)[1].lower()
                    if type == "image" and ext not in IMAGE_EXTS:
                        continue
                    if type == "pdf" and ext not in PDF_EXTS:
                        continue
                    if type == "word" and ext not in WORD_EXTS:
                        continue
                    if type == "excel" and ext not in EXCEL_EXTS:
                        continue
                    if type == "all" and ext not in IMAGE_EXTS and ext not in DOC_EXTS:
                        continue
                    if keyword == "*" or keyword in f.lower():
                        full_path = os.path.join(root, f)
                        st = os.stat(full_path)
                        if ext in IMAGE_EXTS:
                            file_type = "image"
                        elif ext in PDF_EXTS:
                            file_type = "pdf"
                        elif ext in WORD_EXTS:
                            file_type = "word"
                        else:
                            file_type = "excel"
                        results.append({
                            "name": f, "path": full_path, "type": file_type,
                            "size": format_size(st.st_size), "size_bytes": st.st_size,
                            "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                        })
    return {"results": results[:max_results], "total": len(results), "keyword": q}


def serve_remote_file(file_path: str, inline: bool = False):
    """Serve a file from /mnt/office/ via Windows SSH."""
    drive_map = {"YLd": "D:", "YLe": "E:", "YLf": "F:"}
    parts = file_path[len("/mnt/office/"):].split("/")
    label = parts[0]
    drive = drive_map.get(label)
    if not drive:
        raise HTTPException(status_code=404, detail="无法解析远程路径")
    win_path = drive + "\\" + "\\".join(parts[1:])
    ext = os.path.splitext(win_path)[1].lower()

    import subprocess, tempfile
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    tmp.close()
    try:
        # Use PowerShell to base64-encode the binary file
        ps_cmd = f"powershell -Command \"[Convert]::ToBase64String([IO.File]::ReadAllBytes('{win_path}'))\""
        cmd = ["sshpass", "-p", WIN_PASSWORD, "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=15",
               "-p", "2222", "Administrator@localhost", ps_cmd]
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
        if proc.returncode != 0:
            raise HTTPException(status_code=404, detail="文件不存在")
        import base64
        with open(tmp.name, "wb") as f:
            f.write(base64.b64decode(proc.stdout.decode("ascii", errors="ignore").strip()))
        fname = os.path.basename(win_path)
        if inline:
            return FileResponse(
                tmp.name,
                media_type=mimetypes.guess_type(win_path)[0] or "application/octet-stream",
                headers={"Content-Disposition": "inline"},
            )
        else:
            return FileResponse(
                tmp.name,
                filename=fname,
                media_type=mimetypes.guess_type(win_path)[0] or "application/octet-stream",
            )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="远程文件读取超时")


@app.get("/api/download")
async def download(request: Request, path: str = Query(..., description="文件路径")):
    get_current_user(request)
    file_path = os.path.realpath(os.path.expanduser(path))
    # Path allowlist check
    ALLOWED = CONFIG.get("allowed_download_paths", ["/data/", "/mnt/office/"])
    if not any(file_path.startswith(p) for p in ALLOWED):
        raise HTTPException(status_code=403, detail="路径不允许")
    if file_path.startswith("/mnt/office/"):
        return serve_remote_file(file_path)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(
        file_path,
        filename=os.path.basename(file_path),
        media_type=mimetypes.guess_type(file_path)[0] or "application/octet-stream",
    )


@app.get("/api/preview")
async def preview(request: Request, path: str = Query(..., description="文件路径")):
    get_current_user(request)
    file_path = os.path.realpath(os.path.expanduser(path))
    if file_path.startswith("/mnt/office/"):
        return serve_remote_file(file_path, inline=True)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="文件不存在")
    ext = os.path.splitext(file_path)[1].lower()
    if ext in IMAGE_EXTS:
        return FileResponse(file_path, media_type=mimetypes.guess_type(file_path)[0])
    elif ext in PDF_EXTS:
        return FileResponse(file_path, media_type="application/pdf",
                            headers={"Content-Disposition": "inline"})
    elif ext in WORD_EXTS | EXCEL_EXTS:
        return FileResponse(file_path,
                            media_type=mimetypes.guess_type(file_path)[0] or "application/octet-stream",
                            headers={"Content-Disposition": "inline"})
    else:
        raise HTTPException(status_code=400, detail="不支持预览该文件类型")


# ══════════════════════════════════════════════════════════

@app.get("/bg.jpg")
async def bg_image():
    bg_path = Path(__file__).parent / "W020210618335720932703.jpg"
    return FileResponse(bg_path, media_type="image/jpeg")


#  Frontend SPA
# ══════════════════════════════════════════════════════════

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>投融资部信息智能化平台</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Noto+Serif+SC:wght@400;600;700;900&display=swap" rel="stylesheet">
<style>
:root {
  --gold: #c9a84c;
  --gold-light: #e8d08a;
  --gold-dark: #a8882e;
  --navy: #0b1629;
  --navy-80: rgba(11, 22, 41, 0.85);
  --navy-60: rgba(11, 22, 41, 0.6);
  --bg: #f0f2f6;
  --card: #ffffff;
  --text: #1a1a2e;
  --text-secondary: #6b7280;
  --text-muted: #9ca3af;
  --border: #e5e7eb;
  --success: #10b981;
  --error: #ef4444;
  --radius: 12px;
  --radius-sm: 8px;
  --shadow: 0 1px 3px rgba(0,0,0,0.06);
  --shadow-md: 0 4px 20px rgba(0,0,0,0.08);
  --shadow-lg: 0 10px 40px rgba(0,0,0,0.12);
  --transition: 0.3s cubic-bezier(0.4, 0, 0.2, 1);
}

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  -webkit-font-smoothing: antialiased;
}

/* ── BACKGROUND IMAGE ── */
.bg-image {
  position: fixed;
  inset: 0;
  z-index: -1;
  overflow: hidden;
}
.bg-image img {
  position: absolute;
  top: 50%;
  left: 50%;
  width: 100vh;
  height: 100vw;
  transform: translate(-50%, -50%) rotate(-90deg);
  object-fit: cover;
}
.bg-image::after {
  content: '';
  position: absolute;
  inset: 0;
  background: linear-gradient(135deg, rgba(11, 22, 41, 0.55) 0%, rgba(11, 22, 41, 0.3) 50%, rgba(11, 22, 41, 0.55) 100%);
}

/* ── Login / Register ── */
.auth-page {
  display: flex;
  justify-content: center;
  align-items: center;
  width: 100vw;
  height: 100vh;
  padding: 20px;
  position: fixed;
  top: 0;
  left: 0;
  overflow: hidden;
}

.auth-card {
  position: relative;
  background: rgba(255, 255, 255, 0.96);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  border-radius: 20px;
  padding: 44px 36px 36px;
  width: 100%;
  max-width: 420px;
  box-shadow: 0 25px 60px rgba(0,0,0,0.3);
  animation: cardSlideUp 0.6s ease-out;
  border: 1px solid rgba(201, 168, 76, 0.15);
}

@keyframes cardSlideUp {
  from { opacity: 0; transform: translateY(30px); }
  to { opacity: 1; transform: translateY(0); }
}

.auth-card::before {
  content: '';
  position: absolute;
  top: 0; left: 36px; right: 36px;
  height: 3px;
  background: linear-gradient(90deg, var(--gold-dark), var(--gold-light), var(--gold-dark));
  border-radius: 0 0 3px 3px;
}

.auth-card h2 {
  font-family: 'Noto Serif SC', serif;
  text-align: center;
  font-size: 1.5rem;
  font-weight: 700;
  color: var(--navy);
  letter-spacing: 0.5px;
  margin-bottom: 4px;
}

.auth-card .subtitle {
  text-align: center;
  font-size: 0.85rem;
  color: var(--text-muted);
  margin-bottom: 28px;
}

.auth-card label {
  display: block;
  font-size: 0.82rem;
  font-weight: 600;
  margin-bottom: 5px;
  color: var(--text-secondary);
  letter-spacing: 0.3px;
}

.auth-card input {
  width: 100%;
  padding: 11px 14px;
  border: 1.5px solid var(--border);
  border-radius: var(--radius-sm);
  font-size: 15px;
  font-family: 'Inter', sans-serif;
  margin-bottom: 16px;
  outline: none;
  transition: border-color var(--transition), box-shadow var(--transition);
}

.auth-card input:focus {
  border-color: var(--gold);
  box-shadow: 0 0 0 3px rgba(201, 168, 76, 0.15);
}

.auth-card .btn-primary {
  width: 100%;
  padding: 13px;
  background: linear-gradient(135deg, #0b1629, #1a2d53);
  color: #fff;
  border: none;
  border-radius: var(--radius-sm);
  font-size: 16px;
  font-weight: 600;
  font-family: 'Inter', sans-serif;
  cursor: pointer;
  transition: all var(--transition);
  letter-spacing: 2px;
  position: relative;
  overflow: hidden;
}

.auth-card .btn-primary:hover {
  transform: translateY(-1px);
  box-shadow: 0 6px 20px rgba(11, 22, 41, 0.3);
}

.auth-card .btn-primary::after {
  content: '';
  position: absolute;
  inset: 0;
  background: linear-gradient(135deg, transparent 40%, rgba(201, 168, 76, 0.15) 100%);
  pointer-events: none;
}

.auth-card .switch {
  text-align: center;
  margin-top: 18px;
  font-size: 0.85rem;
  color: var(--text-muted);
}

.auth-card .switch a {
  color: var(--gold-dark);
  cursor: pointer;
  text-decoration: none;
  font-weight: 600;
  transition: color var(--transition);
}

.auth-card .switch a:hover { color: var(--gold); }

.auth-card .error {
  color: var(--error);
  font-size: 0.82rem;
  text-align: center;
  margin-bottom: 12px;
  min-height: 22px;
  line-height: 1.4;
}

.auth-card .success {
  color: var(--success);
  font-size: 0.82rem;
  text-align: center;
  margin-bottom: 12px;
}

/* ── Header ── */
.header {
  background: rgba(11, 22, 41, 0.92);
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  color: #fff;
  padding: 0 24px;
  display: flex;
  align-items: center;
  height: 60px;
  position: sticky;
  top: 0;
  z-index: 100;
  border-bottom: 2px solid var(--gold);
}

.header h1 {
  font-family: 'Noto Serif SC', serif;
  font-size: 1.15rem;
  font-weight: 700;
  letter-spacing: 1px;
  position: absolute;
  left: 50%;
  transform: translateX(-50%);
  white-space: nowrap;
  color: #f0ede5;
}

.header .user-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 0.82rem;
  margin-left: auto;
}

.header .user-bar .user-name {
  color: rgba(255,255,255,0.8);
  font-weight: 500;
  padding: 0 8px;
}

.header .user-bar button {
  background: rgba(255,255,255,0.08);
  color: rgba(255,255,255,0.9);
  border: 1px solid rgba(255,255,255,0.12);
  padding: 6px 14px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 0.78rem;
  font-family: 'Inter', sans-serif;
  font-weight: 500;
  transition: all var(--transition);
}

.header .user-bar button:hover {
  background: rgba(255,255,255,0.15);
  border-color: rgba(255,255,255,0.25);
}

.header .user-bar .admin-badge {
  background: linear-gradient(135deg, var(--gold-dark), var(--gold-light));
  color: var(--navy);
  padding: 3px 10px;
  border-radius: 4px;
  font-weight: 700;
  font-size: 0.68rem;
  letter-spacing: 0.5px;
}

/* ── Main content ── */
.container {
  max-width: 860px;
  margin: 0 auto;
  padding: 24px 16px 40px;
  animation: fadeIn 0.4s ease-out;
}

@keyframes fadeIn {
  from { opacity: 0; transform: translateY(10px); }
  to { opacity: 1; transform: translateY(0); }
}

.page { display: none; }
.page.active { display: block; }
.auth-page.page.active { display: flex; }

/* ── PAGE CONTAINER (for background) ── */
.page-container {
  position: relative;
  min-height: calc(100vh - 60px);
}

.page-container::after {
  content: '';
  position: fixed;
  inset: 0;
  z-index: -1;
  background: linear-gradient(135deg, rgba(11, 22, 41, 0.7) 0%, rgba(11, 22, 41, 0.4) 50%, rgba(11, 22, 41, 0.7) 100%);
}

/* ── Welcome banner ── */
.welcome-banner {
  background: rgba(255, 255, 255, 0.12);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  border-radius: 16px;
  padding: 28px 32px;
  margin-bottom: 20px;
  border: 1px solid rgba(255, 255, 255, 0.1);
  color: #fff;
  display: flex;
  align-items: center;
  gap: 20px;
}

.welcome-banner .greeting {
  font-family: 'Noto Serif SC', serif;
  font-size: 1.3rem;
  font-weight: 700;
  letter-spacing: 0.5px;
}

.welcome-banner .greeting-sub {
  font-size: 0.85rem;
  opacity: 0.7;
  margin-top: 4px;
}

.welcome-banner .stats {
  margin-left: auto;
  display: flex;
  gap: 24px;
}

.welcome-banner .stat-item {
  text-align: center;
}

.welcome-banner .stat-item .num {
  font-size: 1.5rem;
  font-weight: 700;
  color: var(--gold-light);
}

.welcome-banner .stat-item .label {
  font-size: 0.75rem;
  opacity: 0.6;
  margin-top: 2px;
}

/* ── Search Box ── */
.search-box {
  background: rgba(255, 255, 255, 0.95);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border-radius: 16px;
  padding: 28px 28px 24px;
  box-shadow: var(--shadow-md);
  margin-bottom: 20px;
  border: 1px solid rgba(255, 255, 255, 0.2);
}

.dir-presets {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 20px;
  justify-content: center;
}

.dir-preset {
  font-size: 0.85rem;
  padding: 8px 22px;
  background: rgba(240, 242, 246, 0.8);
  color: var(--navy);
  border-radius: 24px;
  cursor: pointer;
  border: 1.5px solid transparent;
  font-weight: 600;
  font-family: 'Inter', sans-serif;
  transition: all var(--transition);
}

.dir-preset:hover {
  background: #e8edf5;
  border-color: var(--navy);
}

.dir-preset.active {
  background: var(--navy);
  color: #fff;
  border-color: var(--navy);
  box-shadow: 0 2px 12px rgba(11, 22, 41, 0.25);
}

.row {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}

input, select, button {
  font-size: 15px;
  padding: 10px 14px;
  border: 1.5px solid var(--border);
  border-radius: var(--radius-sm);
  outline: none;
  font-family: 'Inter', sans-serif;
}

input:focus, select:focus {
  border-color: var(--gold-dark);
  box-shadow: 0 0 0 3px rgba(201, 168, 76, 0.12);
}

input[type="text"] { flex: 1; min-width: 160px; }
select { background: #fff; cursor: pointer; color: var(--text); }
button { cursor: pointer; transition: all var(--transition); }
button:active { transform: scale(0.97); }

.btn-search {
  background: var(--navy);
  color: #fff;
  border: none;
  font-weight: 600;
  padding: 10px 28px;
  white-space: nowrap;
  letter-spacing: 0.5px;
}

.btn-search:hover {
  background: #152544;
  box-shadow: 0 4px 12px rgba(11, 22, 41, 0.25);
}

/* ── Results ── */
.status {
  text-align: center;
  padding: 40px 20px;
  color: rgba(255,255,255,0.5);
  font-size: 0.9rem;
}

.result-count {
  font-size: 0.85rem;
  color: rgba(255,255,255,0.6);
  margin-bottom: 12px;
  padding: 0 4px;
  font-weight: 500;
}

.results { list-style: none; }

.result-card {
  display: flex;
  align-items: center;
  gap: 14px;
  background: rgba(255, 255, 255, 0.95);
  backdrop-filter: blur(8px);
  border-radius: var(--radius);
  padding: 14px 18px;
  margin-bottom: 8px;
  box-shadow: var(--shadow);
  transition: all var(--transition);
  border: 1px solid transparent;
}

.result-card:hover {
  box-shadow: var(--shadow-md);
  border-color: var(--gold-light);
  transform: translateY(-1px);
}

.icon {
  width: 44px;
  height: 44px;
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 1.3rem;
  flex-shrink: 0;
}

.icon.folder { background: #fef3c7; color: #d97706; }
.icon.image { background: #d1fae5; color: #059669; }
.icon.pdf { background: #fee2e2; color: #dc2626; }
.icon.word { background: #dbeafe; color: #2563eb; }
.icon.excel { background: #d1fae5; color: #059669; }

.meta {
  flex: 1;
  min-width: 0;
}

.meta .name {
  font-weight: 600;
  font-size: 0.92rem;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--text);
}

.meta .info {
  font-size: 0.78rem;
  color: var(--text-muted);
  margin-top: 3px;
}

.download-btn {
  background: rgba(11, 22, 41, 0.06);
  color: var(--navy);
  padding: 8px 16px;
  border-radius: 6px;
  text-decoration: none;
  font-size: 0.82rem;
  font-weight: 600;
  border: none;
  cursor: pointer;
  flex-shrink: 0;
  transition: all var(--transition);
}

.download-btn:hover {
  background: rgba(11, 22, 41, 0.1);
}

.preview-btn {
  background: rgba(201, 168, 76, 0.12);
  color: var(--gold-dark);
  padding: 8px 16px;
  border-radius: 6px;
  text-decoration: none;
  font-size: 0.82rem;
  font-weight: 600;
  border: none;
  cursor: pointer;
  flex-shrink: 0;
  transition: all var(--transition);
}

.preview-btn:hover {
  background: rgba(201, 168, 76, 0.2);
}

/* ── Modal ── */
.modal-overlay {
  display: none;
  position: fixed;
  top: 0; left: 0;
  width: 100%; height: 100%;
  background: rgba(11, 22, 41, 0.7);
  backdrop-filter: blur(4px);
  z-index: 1000;
  justify-content: center;
  align-items: center;
}

.modal-overlay.active { display: flex; }

.modal-box {
  background: var(--card);
  border-radius: 16px;
  width: 92vw;
  height: 88vh;
  max-width: 1000px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  box-shadow: 0 25px 60px rgba(0,0,0,0.3);
  animation: modalIn 0.3s ease-out;
}

@keyframes modalIn {
  from { opacity: 0; transform: scale(0.95) translateY(10px); }
  to { opacity: 1; transform: scale(1) translateY(0); }
}

.modal-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 20px;
  border-bottom: 1px solid var(--border);
}

.modal-header .title {
  font-weight: 600;
  font-size: 0.95rem;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1;
  margin-right: 12px;
  color: var(--text);
}

.modal-close-x {
  background: none;
  border: none;
  font-size: 1.5rem;
  cursor: pointer;
  color: var(--text-muted);
  padding: 0 4px;
  line-height: 1;
}

.modal-close-x:hover { color: var(--text); }

.modal-body {
  flex: 1;
  overflow: auto;
  display: flex;
  justify-content: center;
  align-items: flex-start;
  padding: 12px;
  background: #f8f9fb;
}

.modal-body img { max-width: 100%; height: auto; border-radius: 8px; }
.modal-body iframe { width: 100%; height: 100%; border: none; min-height: 500px; border-radius: 8px; }

.modal-actions {
  display: flex;
  gap: 10px;
  padding: 12px 20px;
  border-top: 1px solid var(--border);
  justify-content: flex-end;
}

.modal-dl {
  background: var(--navy);
  color: #fff;
  padding: 9px 24px;
  border-radius: 6px;
  text-decoration: none;
  font-size: 0.88rem;
  font-weight: 600;
  font-family: 'Inter', sans-serif;
  transition: all var(--transition);
}

.modal-dl:hover { background: #152544; }

/* ── Admin panel ── */
.admin-panel {
  background: rgba(255, 255, 255, 0.95);
  backdrop-filter: blur(12px);
  border-radius: 16px;
  padding: 28px;
  box-shadow: var(--shadow-md);
  border: 1px solid rgba(255, 255, 255, 0.2);
}

.admin-panel h3 {
  font-family: 'Noto Serif SC', serif;
  font-size: 1.1rem;
  margin-bottom: 16px;
  color: var(--navy);
  padding-bottom: 12px;
  border-bottom: 2px solid var(--gold-light);
}

.admin-table {
  width: 100%;
  border-collapse: collapse;
  border-radius: 10px;
  overflow: hidden;
}

.admin-table th {
  background: rgba(240, 242, 246, 0.8);
  padding: 12px 14px;
  text-align: left;
  font-size: 0.8rem;
  color: var(--text-secondary);
  font-weight: 600;
  letter-spacing: 0.3px;
}

.admin-table td {
  padding: 12px 14px;
  border-top: 1px solid var(--border);
  font-size: 0.85rem;
}

.admin-approve {
  background: var(--success);
  color: #fff;
  border: none;
  padding: 6px 14px;
  border-radius: 6px;
  font-size: 0.8rem;
  cursor: pointer;
  font-family: 'Inter', sans-serif;
  font-weight: 600;
  margin-right: 6px;
  transition: all var(--transition);
}

.admin-approve:hover { filter: brightness(1.1); }

.admin-reject {
  background: var(--error);
  color: #fff;
  border: none;
  padding: 6px 14px;
  border-radius: 6px;
  font-size: 0.8rem;
  cursor: pointer;
  font-family: 'Inter', sans-serif;
  font-weight: 600;
  transition: all var(--transition);
}

.admin-reject:hover { filter: brightness(1.1); }

.btn-secondary {
  background: rgba(255,255,255,0.15);
  color: #fff;
  padding: 10px 24px;
  border-radius: 12px;
  border: 1px solid rgba(255,255,255,0.15);
  font-size: 0.88rem;
  font-weight: 600;
  cursor: pointer;
  font-family: 'Inter', sans-serif;
  transition: all var(--transition);
}
.btn-secondary:hover { background: rgba(255,255,255,0.25); }

.admin-empty { text-align: center; color: var(--text-muted); padding: 40px; }

.user-badge { display:inline-block;padding:2px 10px;border-radius:10px;font-size:0.75rem;font-weight:600; }
.user-badge-admin { background:#c9a84c20;color:#c9a84c; }
.user-badge-user { background:#3b82f620;color:#3b82f6; }
.edit-user-input { padding:4px 8px;border:1px solid var(--border);border-radius:6px;font-size:0.82rem;width:120px;font-family:inherit; }
.edit-user-btn { background:var(--navy);color:#fff;border:none;padding:4px 14px;border-radius:6px;font-size:0.8rem;cursor:pointer;font-weight:600;font-family:inherit; }
.edit-user-btn:hover { filter:brightness(1.2); }
.edit-user-cancel { background:#f3f4f6;color:#374151;border:none;padding:4px 14px;border-radius:6px;font-size:0.8rem;cursor:pointer;font-weight:500;font-family:inherit;margin-left:4px; }
.edit-user-cancel:hover { background:#e5e7eb; }

/* ── Footer ── */
.footer {
  text-align: center;
  padding: 20px;
  color: rgba(255,255,255,0.3);
  font-size: 0.78rem;
}

/* ── Responsive ── */
@media (max-width: 640px) {
  .auth-card { padding: 32px 24px; }
  .auth-card h2 { font-size: 1.25rem; }
  .header { height: auto; flex-wrap: wrap; padding: 10px 14px; gap: 6px; }
  .header h1 { font-size: 0.85rem; position: static; transform: none; flex: 1; text-align: center; }
  .header .user-bar { width: 100%; justify-content: center; gap: 4px; margin-left: 0; }
  .header .user-bar button { padding: 4px 10px; font-size: 0.7rem; }
  .container { padding: 16px 12px; }
  .search-box { padding: 20px 16px; }
  .dir-presets { gap: 6px; }
  .dir-preset { font-size: 0.78rem; padding: 6px 16px; }
  .result-card { padding: 12px; flex-wrap: wrap; }
  .modal-box { width: 98vw; height: 94vh; }
  .admin-panel { padding: 16px; overflow-x: auto; }
  .welcome-banner { flex-direction: column; text-align: center; padding: 20px; }
  .welcome-banner .stats { margin-left: 0; }
}

/* ── Loading ── */
.btn-search.loading { opacity: 0.7; pointer-events: none; }

/* ── Scrollbar ── */
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: #c4c4c4; border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: #a8a8a8; }
</style>
</head>
<body>

<div class="bg-image">
  <img src="/rz/bg.jpg" alt="">
</div>

<!-- ═══════════ LOGIN PAGE ═══════════ -->
<div id="loginPage" class="auth-page page active">
  <div class="auth-card">
    <h2>投融资部信息智能化平台</h2>
    <div class="subtitle">汉口北 · 金融赋能 · 智慧办公</div>
    <div class="error" id="loginError"></div>
    <label>账号</label>
    <input type="text" id="loginUsername" placeholder="请输入账号" autocomplete="username">
    <label>密码</label>
    <input type="password" id="loginPassword" placeholder="请输入密码" autocomplete="current-password">
    <button class="btn-primary" onclick="doLogin()">登 录</button>
    <div class="switch">还没有账号？<a onclick="showPage('registerPage')">申请注册</a></div>
  </div>
</div>

<!-- ═══════════ REGISTER PAGE ═══════════ -->
<div id="registerPage" class="auth-page page">
  <div class="auth-card">
    <h2>申请注册账号</h2>
    <div class="subtitle">提交后需等待管理员审核</div>
    <div class="error" id="regError"></div>
    <div class="success" id="regSuccess"></div>
    <label>账号</label>
    <input type="text" id="regUsername" placeholder="设置登录账号（至少3位）">
    <label>密码</label>
    <input type="password" id="regPassword" placeholder="设置密码（至少6位）">
    <label>真实姓名</label>
    <input type="text" id="regName" placeholder="请填写真实姓名">
    <label>部门</label>
    <input type="text" id="regDept" placeholder="请填写所在部门">
    <button class="btn-primary" onclick="doRegister()">提交申请</button>
    <div class="switch">已有账号？<a onclick="showPage('loginPage')">返回登录</a></div>
  </div>
</div>

<!-- ═══════════ MAIN APP ═══════════ -->
<div id="mainApp" class="page">
  <div class="header">
    <h1>投融资部信息智能化平台</h1>
    <div class="user-bar">
      <span class="user-name" id="userDisplay"></span>
      <span class="admin-badge" id="adminBadge" style="display:none">管理员</span>
      <button onclick="showAdmin()" id="adminBtn" style="display:none">用户管理</button>
      <button onclick="showSearchPage()">检索首页</button>
      <button onclick="doLogout()">退出</button>
    </div>
  </div>

  <div class="page-container">

    <!-- Welcome banner -->
    <div style="padding: 0 16px;max-width:860px;margin:0 auto">
      <div class="welcome-banner">
        <div>
          <div class="greeting" id="greetingText">欢迎回来</div>
          <div class="greeting-sub">汉口北投融资部 · 2026年5月</div>
        </div>
        <div class="stats">
          <div class="stat-item">
            <div class="num" id="statFiles">--</div>
            <div class="label">归档文件</div>
          </div>
          <div class="stat-item">
            <div class="num" id="statDirs">--</div>
            <div class="label">目录分区</div>
          </div>
          <div class="stat-item">
            <div class="num" id="statUsers">--</div>
            <div class="label">注册用户</div>
          </div>
        </div>
      </div>
    </div>

    <!-- Search view -->
    <div id="searchView" class="container" style="padding-top:0">
      <div class="search-box">
        <div class="dir-presets">
          <button class="dir-preset active" id="presetE2" onclick="setDir('/data/汉口北工作/证照/E2',this)">E2</button>
          <button class="dir-preset" id="presetE3" onclick="setDir('/data/汉口北工作/证照/E3',this)">E3</button>
          <button class="dir-preset" id="presetE4" onclick="setDir('/data/汉口北工作/证照/E4',this)">E4</button>
          <button class="dir-preset" id="presetH" onclick="setDir('/data/汉口北工作/证照/H区',this)">H区</button>
          <button class="dir-preset" id="presetJ3" onclick="setDir('/data/汉口北工作/证照/J3区不动产（1645）',this)">J3</button>
          <button class="dir-preset" id="presetYLd" onclick="setDir('/mnt/office/YLd',this)">YLd</button>
          <button class="dir-preset" id="presetYLe" onclick="setDir('/mnt/office/YLe',this)">YLe</button>
          <button class="dir-preset" id="presetYLf" onclick="setDir('/mnt/office/YLf',this)">YLf</button>
        </div>
        <div class="row">
          <input type="text" id="searchInput" placeholder="输入关键词搜索文件名，如「不动产」「规划图」..." autocomplete="off" autofocus>
          <select id="typeSelect">
            <option value="all">全部类型</option>
            <option value="image">图片</option>
            <option value="pdf">PDF</option>
            <option value="word">Word</option>
            <option value="excel">Excel</option>
            <option value="folder">文件夹</option>
          </select>
          <button class="btn-search" onclick="doSearch()">搜索</button>
        </div>
      </div>
      <div id="resultCount" class="result-count" style="display:none"></div>
      <ul id="results" class="results"></ul>
      <div id="status" class="status">🏢 请选择目录并输入关键词检索文件</div>
    </div>

    <!-- Admin view -->
    <div id="adminView" class="container" style="padding-top:0;display:none">
      <div style="display:flex;gap:8px;margin-bottom:16px">
        <button id="adminTabPending" class="btn-primary" style="flex:1" onclick="adminSwitchTab('pending')">📋 审批管理</button>
        <button id="adminTabUsers" class="btn-secondary" style="flex:1" onclick="adminSwitchTab('users')">👥 用户管理</button>
      </div>
      <div id="adminPendingPanel" class="admin-panel">
        <h3>注册审批管理</h3>
        <div id="adminContent"></div>
      </div>
      <div id="adminUsersPanel" class="admin-panel" style="display:none">
        <h3>已注册用户管理</h3>
        <div id="adminUsersContent"></div>
      </div>
    </div>

    <div class="footer">汉口北投融资部信息智能化平台 · 内部专用</div>
  </div>
</div>

<!-- Preview Modal -->
<div class="modal-overlay" id="modalOverlay" onclick="if(event.target===this)closePreview()">
  <div class="modal-box">
    <div class="modal-header">
      <span class="title" id="modalTitle"></span>
      <button class="modal-close-x" onclick="closePreview()">&times;</button>
    </div>
    <div class="modal-body" id="modalBody"></div>
    <div class="modal-actions">
      <button onclick="closePreview()" style="background:#f3f4f6;color:#374151;padding:9px 24px;border-radius:6px;border:none;font-size:0.88rem;cursor:pointer;font-family:Inter,sans-serif;font-weight:500">关闭</button>
      <a class="modal-dl" id="modalDownload" href="#" download>下载文件</a>
    </div>
  </div>
</div>

<script>
var token = localStorage.getItem('token') || '';
var currentUser = null;
var API = '';
var MOCK_MODE = false;

var mockFiles = [
  { name: "不动产登记证_E2_001.pdf", path: "/data/汉口北工作/证照/E2/不动产登记证_E2_001.pdf", type: "pdf", size: "2.3 MB", modified: "2026-05-20 14:30", icon: "📄" },
  { name: "土地使用权证_E2_002.pdf", path: "/data/汉口北工作/证照/E2/土地使用权证_E2_002.pdf", type: "pdf", size: "1.8 MB", modified: "2026-05-19 10:15", icon: "📄" },
  { name: "施工现场照片_01.jpg", path: "/data/汉口北工作/证照/E2/施工现场照片_01.jpg", type: "image", size: "4.2 MB", modified: "2026-05-18 16:45", icon: "🖼️" },
  { name: "项目规划图_2026.jpg", path: "/data/汉口北工作/证照/E2/项目规划图_2026.jpg", type: "image", size: "6.7 MB", modified: "2026-05-17 09:20", icon: "🖼️" },
  { name: "房屋测绘报告.docx", path: "/data/汉口北工作/证照/E2/房屋测绘报告.docx", type: "word", size: "1.2 MB", modified: "2026-05-16 11:00", icon: "📝" },
  { name: "建筑面积统计.xlsx", path: "/data/汉口北工作/证照/E2/建筑面积统计.xlsx", type: "excel", size: "856 KB", modified: "2026-05-15 15:30", icon: "📊" },
  { name: "竣工验收备案表.pdf", path: "/data/汉口北工作/证照/E2/竣工验收备案表.pdf", type: "pdf", size: "3.1 MB", modified: "2026-05-14 08:45", icon: "📄" },
  { name: "楼层平面图", path: "/data/汉口北工作/证照/E2/楼层平面图", type: "folder", size: "—", modified: "2026-05-13 10:00", icon: "📁" },
  { name: "消防验收文件", path: "/data/汉口北工作/证照/E2/消防验收文件", type: "folder", size: "—", modified: "2026-05-12 14:20", icon: "📁" },
  { name: "环评报告.pdf", path: "/data/汉口北工作/证照/E2/环评报告.pdf", type: "pdf", size: "5.5 MB", modified: "2026-05-11 09:30", icon: "📄" },
  { name: "招标文件_2026年第一期.docx", path: "/data/汉口北工作/证照/E2/招标文件_2026年第一期.docx", type: "word", size: "2.1 MB", modified: "2026-05-10 13:00", icon: "📝" },
  { name: "资金使用明细_2026Q1.xlsx", path: "/data/汉口北工作/证照/E2/资金使用明细_2026Q1.xlsx", type: "excel", size: "1.5 MB", modified: "2026-05-09 16:30", icon: "📊" },
];

(function() {
  if (MOCK_MODE) {
    setTimeout(function() {
      var errEl = document.getElementById('loginError');
      errEl.style.color = 'rgba(255,255,255,0.6)';
      errEl.innerHTML = '💡 预览模式 · 输入任意账号密码登录体验';
    }, 300);
  }
})();

function mockLogin(username, role) {
  currentUser = { username: username, role: role, name: '用户', department: '' };
  token = 'mock_' + Date.now();
}

function showPage(id) {
  document.querySelectorAll('.page').forEach(function(p) { p.classList.remove('active'); });
  var el = document.getElementById(id);
  if (el) el.classList.add('active');
}

function showMainApp() {
  showPage('mainApp');
  document.getElementById('userDisplay').textContent = currentUser.name;
  document.getElementById('greetingText').textContent = '欢迎回来，' + currentUser.name;
  if (currentUser.role === 'admin') {
    document.getElementById('adminBadge').style.display = 'inline';
    document.getElementById('adminBtn').style.display = 'inline';
  } else {
    document.getElementById('adminBadge').style.display = 'none';
    document.getElementById('adminBtn').style.display = 'none';
  }
  showSearchPage();
  loadStats();
}
function showSearchPage() {
  document.getElementById('searchView').style.display = 'block';
  document.getElementById('adminView').style.display = 'none';
}

function showAdmin() {
  if (!currentUser || currentUser.role !== 'admin') { showSearchPage(); return; }
  document.getElementById('searchView').style.display = 'none';
  document.getElementById('adminView').style.display = 'block';
  adminSwitchTab('pending');
}

function adminSwitchTab(tab) {
  if (tab === 'pending') {
    document.getElementById('adminPendingPanel').style.display = 'block';
    document.getElementById('adminUsersPanel').style.display = 'none';
    document.getElementById('adminTabPending').className = 'btn-primary';
    document.getElementById('adminTabUsers').className = 'btn-secondary';
    loadAdminPending();
  } else {
    document.getElementById('adminPendingPanel').style.display = 'none';
    document.getElementById('adminUsersPanel').style.display = 'block';
    document.getElementById('adminTabPending').className = 'btn-secondary';
    document.getElementById('adminTabUsers').className = 'btn-primary';
    loadUsers();
  }
}

function doLogin() {
  var u = document.getElementById('loginUsername').value.trim();
  var p = document.getElementById('loginPassword').value;
  var errEl = document.getElementById('loginError');
  errEl.textContent = '';
  if (MOCK_MODE) {
    if (!u || !p) { errEl.textContent = '请输入账号和密码'; return; }
    mockLogin('demo', 'admin');
    showMainApp();
    return;
  }
  if (!u || !p) { errEl.textContent = '请输入账号和密码'; return; }
  fetch(API + '/api/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: u, password: p })
  })
  .then(function(r) { return r.json().then(function(d) { return { status: r.status, body: d }; }); })
  .then(function(r) {
    if (r.status !== 200) { errEl.textContent = r.body.detail || '登录失败'; return; }
    token = r.body.token;
    currentUser = { username: r.body.username, role: r.body.role, name: r.body.name };
    localStorage.setItem('token', token);
    showMainApp();
  })
  .catch(function(e) { errEl.textContent = '网络错误'; });
}

function doRegister() {
  var u = document.getElementById('regUsername').value.trim();
  var p = document.getElementById('regPassword').value;
  var n = document.getElementById('regName').value.trim();
  var d = document.getElementById('regDept').value.trim();
  var errEl = document.getElementById('regError');
  var okEl = document.getElementById('regSuccess');
  errEl.textContent = ''; okEl.textContent = '';
  if (MOCK_MODE) {
    if (!u || !p || !n || !d) { errEl.textContent = '请填写所有字段'; return; }
    if (u.length < 3) { errEl.textContent = '账号至少3个字符'; return; }
    if (p.length < 6) { errEl.textContent = '密码至少6位'; return; }
    okEl.textContent = '✅ 注册申请已提交，请等待管理员审核';
    document.getElementById('regUsername').value = '';
    document.getElementById('regPassword').value = '';
    document.getElementById('regName').value = '';
    document.getElementById('regDept').value = '';
    return;
  }
  if (!u || !p || !n || !d) { errEl.textContent = '请填写所有字段'; return; }
  if (u.length < 3) { errEl.textContent = '账号至少3个字符'; return; }
  if (p.length < 6) { errEl.textContent = '密码至少6位'; return; }
  fetch(API + '/api/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: u, password: p, name: n, department: d })
  })
  .then(function(r) { return r.json().then(function(d) { return { status: r.status, body: d }; }); })
  .then(function(r) {
    if (r.status !== 200) { errEl.textContent = r.body.detail || '注册失败'; return; }
    okEl.textContent = r.body.message;
    document.getElementById('regUsername').value = '';
    document.getElementById('regPassword').value = '';
    document.getElementById('regName').value = '';
    document.getElementById('regDept').value = '';
  })
  .catch(function(e) { errEl.textContent = '网络错误'; });
}

function doLogout() {
  if (MOCK_MODE) {
    localStorage.removeItem('token'); token = ''; currentUser = null;
    showPage('loginPage');
    document.getElementById('loginError').textContent = '';
    return;
  }
  fetch(API + '/api/logout', { method: 'POST', headers: { 'Authorization': 'Bearer ' + token } })
  .finally(function() {
    localStorage.removeItem('token'); token = ''; currentUser = null;
    showPage('loginPage');
    document.getElementById('loginError').textContent = '';
  });
}

function loadStats() {
  if (MOCK_MODE) {
    document.getElementById('statFiles').textContent = '1,247';
    document.getElementById('statDirs').textContent = '8';
    document.getElementById('statUsers').textContent = '36';
    return;
  }
  fetch(API + '/api/admin/stats', { headers: { 'Authorization': 'Bearer ' + token } })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    document.getElementById('statFiles').textContent = d.file_count.toLocaleString();
    document.getElementById('statDirs').textContent = d.dir_count;
    document.getElementById('statUsers').textContent = d.user_count;
  })
  .catch(function() {});
}

function loadUsers() {
  var el = document.getElementById('adminUsersContent');
  if (MOCK_MODE) {
    el.innerHTML = '<table class="admin-table"><tr><th>账号</th><th>姓名</th><th>部门</th><th>角色</th><th>状态</th><th>注册时间</th></tr><tr><td>bowin6699</td><td>管理员</td><td>管理部</td><td><span class="user-badge user-badge-admin">管理员</span></td><td><span style="color:#10b981;font-weight:600">已通过</span></td><td>2026-05-15</td></tr><tr><td>bowin6699-1</td><td>侯博文</td><td>投融资部</td><td><span class="user-badge user-badge-user">用户</span></td><td><span style="color:#10b981;font-weight:600">已通过</span></td><td>2026-05-15</td></tr><tr><td>lamb</td><td>杨</td><td>融资</td><td><span class="user-badge user-badge-user">用户</span></td><td><span style="color:#10b981;font-weight:600">已通过</span></td><td>2026-05-18</td></tr></table>';
    return;
  }
  fetch(API + '/api/admin/users', { headers: { 'Authorization': 'Bearer ' + token } })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (!d.users || d.users.length === 0) { el.innerHTML = '<div class="admin-empty">暂无用户数据</div>'; return; }
    var html = '<table class="admin-table"><tr><th>账号</th><th>姓名</th><th>部门</th><th>角色</th><th>状态</th><th>注册时间</th><th>操作</th></tr>';
    d.users.forEach(function(u) {
      var roleBadge = u.role === 'admin' ? '<span class="user-badge user-badge-admin">管理员</span>' : '<span class="user-badge user-badge-user">用户</span>';
      var statusText = u.approved ? '<span style="color:#10b981;font-weight:600">已通过</span>' : '<span style="color:#f59e0b;font-weight:600">待审核</span>';
      html += '<tr>' +
        '<td>' + escHtml(u.username) + '</td>' +
        '<td><span id="uname_' + escJs(u.username) + '">' + escHtml(u.name) + '</span></td>' +
        '<td><span id="udept_' + escJs(u.username) + '">' + escHtml(u.department) + '</span></td>' +
        '<td>' + roleBadge + '</td>' +
        '<td>' + statusText + '</td>' +
        '<td style="font-size:0.8rem">' + escHtml(u.created_at.substring(0,10)) + '</td>' +
        '<td><button class="admin-approve" onclick="editUser(\'' + escJs(u.username) + '\')">编辑</button></td>' +
        '</tr>';
    });
    html += '</table>';
    el.innerHTML = html;
  });
}

var editingUser = null;

function editUser(username) {
  if (editingUser) return;
  editingUser = username;
  var nameEl = document.getElementById('uname_' + username);
  var deptEl = document.getElementById('udept_' + username);
  var name = nameEl ? nameEl.textContent : '';
  var dept = deptEl ? deptEl.textContent : '';
  if (nameEl) nameEl.innerHTML = '<input class="edit-user-input" id="editNameInput" value="' + escHtml(name) + '">';
  if (deptEl) deptEl.innerHTML = '<input class="edit-user-input" id="editDeptInput" value="' + escHtml(dept) + '">';
  var btn = document.querySelector('#adminUsersContent .admin-table tr:last-child td:last-child .admin-approve');
  if (btn) {
    btn.textContent = '保存';
    btn.onclick = function() { saveUser(username); };
    var cancelBtn = document.createElement('button');
    cancelBtn.className = 'edit-user-cancel';
    cancelBtn.textContent = '取消';
    cancelBtn.onclick = function() { cancelEdit(); };
    btn.parentNode.appendChild(cancelBtn);
  }
}

function saveUser(username) {
  var name = document.getElementById('editNameInput').value.trim();
  var dept = document.getElementById('editDeptInput').value.trim();
  if (MOCK_MODE) {
    cancelEdit();
    loadUsers();
    return;
  }
  fetch(API + '/api/admin/users/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token },
    body: JSON.stringify({ username: username, name: name, department: dept })
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.detail) { alert('更新失败: ' + d.detail); return; }
    editingUser = null;
    loadUsers();
    loadStats();
  })
  .catch(function() { alert('网络错误'); });
}

function cancelEdit() {
  editingUser = null;
  loadUsers();
}

function loadAdminPending() {
  var el = document.getElementById('adminContent');
  if (MOCK_MODE) {
    el.innerHTML =
      '<table class="admin-table">' +
      '<tr><th>账号</th><th>姓名</th><th>部门</th><th>申请时间</th><th>状态</th><th>操作</th></tr>' +
      '<tr><td>zhangsan</td><td>张三</td><td>市场部</td><td>2026-05-22 10:30</td><td><span style="color:#f59e0b;font-weight:600">待审核</span></td><td><button class="admin-approve" onclick="alert(\'模拟：已通过 zhangsan\')">通过</button><button class="admin-reject" onclick="alert(\'模拟：已拒绝 zhangsan\')">拒绝</button></td></tr>' +
      '<tr><td>lisi</td><td>李四</td><td>财务部</td><td>2026-05-21 15:20</td><td><span style="color:#f59e0b;font-weight:600">待审核</span></td><td><button class="admin-approve" onclick="alert(\'模拟：已通过 lisi\')">通过</button><button class="admin-reject" onclick="alert(\'模拟：已拒绝 lisi\')">拒绝</button></td></tr>' +
      '<tr><td>wangwu</td><td>王五</td><td>工程部</td><td>2026-05-20 09:15</td><td><span style="color:#f59e0b;font-weight:600">待审核</span></td><td><button class="admin-approve" onclick="alert(\'模拟：已通过 wangwu\')">通过</button><button class="admin-reject" onclick="alert(\'模拟：已拒绝 wangwu\')">拒绝</button></td></tr>' +
      '</table>';
    return;
  }
  fetch(API + '/api/admin/pending', { headers: { 'Authorization': 'Bearer ' + token } })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.pending.length === 0) { el.innerHTML = '<div class="admin-empty">没有待审批的注册申请</div>'; return; }
    var html = '<table class="admin-table"><tr><th>账号</th><th>姓名</th><th>部门</th><th>申请时间</th><th>操作</th></tr>';
    d.pending.forEach(function(u) {
      html += '<tr><td>' + escHtml(u.username) + '</td><td>' + escHtml(u.name) + '</td><td>' + escHtml(u.department) + '</td><td>' + escHtml(u.created_at) + '</td>' +
        '<td><button class="admin-approve" onclick="approveUser(\'' + escJs(u.username) + '\',\'approve\')">通过</button>' +
        '<button class="admin-reject" onclick="approveUser(\'' + escJs(u.username) + '\',\'reject\')">拒绝</button></td></tr>';
    });
    html += '</table>';
    el.innerHTML = html;
  });
}

var searchInput = document.getElementById('searchInput');
var typeSelect = document.getElementById('typeSelect');
var resultsEl = document.getElementById('results');
var statusEl = document.getElementById('status');
var resultCountEl = document.getElementById('resultCount');
var currentDir = '/data/汉口北工作/证照/E2';

function setDir(path, btn) {
  currentDir = path;
  document.querySelectorAll('.dir-preset').forEach(function(b) { b.classList.remove('active'); });
  if (btn) btn.classList.add('active');
  searchInput.focus();
}

function enterFolder(path) {
  currentDir = path;
  document.querySelectorAll('.dir-preset').forEach(function(b) { b.classList.remove('active'); });
  doSearch();
}

searchInput.addEventListener('keydown', function(e) { if (e.key === 'Enter') doSearch(); });

function doSearch() {
  var dir = currentDir;
  var keyword = searchInput.value.trim();
  var type = typeSelect.value;
  if (MOCK_MODE) {
    resultsEl.innerHTML = '';
    resultCountEl.style.display = 'none';
    statusEl.style.display = 'block';
    statusEl.textContent = '🔍 搜索中...';
    document.querySelector('.btn-search').classList.add('loading');
    setTimeout(function() {
      document.querySelector('.btn-search').classList.remove('loading');
      var filtered = mockFiles.filter(function(item) {
        if (type !== 'all' && item.type !== type) return false;
        if (keyword && item.name.toLowerCase().indexOf(keyword.toLowerCase()) === -1) return false;
        return true;
      });
      statusEl.style.display = 'none';
      if (filtered.length === 0) {
        statusEl.style.display = 'block';
        statusEl.textContent = keyword ? '📭 没有找到匹配的文件' : '📂 请输入关键词搜索';
        return;
      }
      resultCountEl.style.display = 'block';
      resultCountEl.textContent = '📋 找到 ' + filtered.length + ' 个结果';
      filtered.forEach(function(item) {
        var li = document.createElement('li');
        li.className = 'result-card';
        li.innerHTML =
          '<div class="icon ' + item.type + '">' + item.icon + '</div>' +
          '<div class="meta">' +
            '<div class="name" title="' + escHtml(item.name) + '">' + escHtml(item.name) + '</div>' +
            '<div class="info">' + escHtml(item.size) + ' · ' + escHtml(item.modified) + '</div>' +
          '</div>' +
          (item.type !== 'folder'
            ? '<button class="preview-btn" onclick="openPreview(\'' + escJs(item.path) + '\',\'' + escJs(item.name) + '\',\'' + item.type + '\')">预览</button>' +
              '<a class="download-btn" href="#">下载</a>'
            : '<button class="preview-btn" onclick="enterFolder(\'' + escJs(item.path) + '\')">进入</button>');
        resultsEl.appendChild(li);
      });
    }, 500);
    return;
  }
  resultsEl.innerHTML = '';
  resultCountEl.style.display = 'none';
  statusEl.style.display = 'block';
  statusEl.textContent = '搜索中...';
  var params = new URLSearchParams({ dir: dir, type: type });
  if (keyword) params.set('q', keyword);
  fetch(API + '/api/search?' + params.toString(), { headers: { 'Authorization': 'Bearer ' + token } })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      statusEl.style.display = 'none';
      if (data.results.length === 0) { resultCountEl.style.display = 'none'; statusEl.style.display = 'block'; statusEl.textContent = keyword ? '没有找到匹配的文件' : '请输入关键词搜索'; return; }
      resultCountEl.style.display = 'block';
      resultCountEl.textContent = '找到 ' + data.total + ' 个结果' + (data.total > 2000 ? '（仅显示前 2000 条）' : '');
      data.results.forEach(function(item) {
        var li = document.createElement('li');
        li.className = 'result-card';
        var iconEmoji = item.type === 'folder' ? '📁' : item.type === 'image' ? '🖼️' : item.type === 'pdf' ? '📄' : item.type === 'word' ? '📝' : '📊';
        li.innerHTML =
          '<div class="icon ' + item.type + '">' + iconEmoji + '</div>' +
          '<div class="meta">' +
            '<div class="name" title="' + escHtml(item.name) + '">' + escHtml(item.name) + '</div>' +
            '<div class="info">' + escHtml(item.size) + ' · ' + escHtml(item.modified) + '</div>' +
          '</div>' +
          (item.type !== 'folder'
            ? '<button class="preview-btn" onclick="openPreview(\'' + escJs(item.path) + '\',\'' + escJs(item.name) + '\',\'' + escJs(item.type) + '\')">预览</button>' +
              '<a class="download-btn" href="' + API + '/api/download?path=' + encodeURIComponent(item.path) + '" download>下载</a>'
            : '<button class="preview-btn" onclick="enterFolder(\'' + escJs(item.path) + '\')">进入</button>');
        resultsEl.appendChild(li);
      });
    })
    .catch(function() { statusEl.style.display = 'block'; statusEl.textContent = '搜索出错'; });
}

function openPreview(path, name, type) {
  document.getElementById('modalTitle').textContent = name;
  var body = document.getElementById('modalBody');
  body.innerHTML = '';
  if (MOCK_MODE) {
    if (type === 'image') {
      body.innerHTML = '<div style="text-align:center;padding:60px 20px;color:#6b7280"><div style="font-size:72px;margin-bottom:20px">🖼️</div><div style="font-size:1.1rem;font-weight:600">' + escHtml(name) + '</div><div style="font-size:0.85rem;margin-top:10px;color:#9ca3af">实际部署后将展示文件内容</div></div>';
    } else {
      body.innerHTML = '<div style="text-align:center;padding:60px 20px;color:#6b7280"><div style="font-size:72px;margin-bottom:20px">📄</div><div style="font-size:1.1rem;font-weight:600">' + escHtml(name) + '</div><div style="font-size:0.85rem;margin-top:10px;color:#9ca3af">实际部署后将展示文件内容</div></div>';
    }
    document.getElementById('modalDownload').href = '#';
    document.getElementById('modalOverlay').classList.add('active');
    return;
  }
  var previewUrl = API + '/api/preview?path=' + encodeURIComponent(path);
  if (type === 'image') { var img = document.createElement('img'); img.src = previewUrl; img.alt = name; body.appendChild(img); }
  else { var iframe = document.createElement('iframe'); iframe.src = previewUrl; body.appendChild(iframe); }
  document.getElementById('modalDownload').href = API + '/api/download?path=' + encodeURIComponent(path);
  document.getElementById('modalOverlay').classList.add('active');
}

function closePreview() {
  document.getElementById('modalOverlay').classList.remove('active');
  document.getElementById('modalBody').innerHTML = '';
}

function escHtml(s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
function escJs(s) { return s.replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/"/g,'\\"'); }

document.getElementById('loginPassword').addEventListener('keydown', function(e) { if (e.key === 'Enter') doLogin(); });
document.getElementById('regDept').addEventListener('keydown', function(e) { if (e.key === 'Enter') doRegister(); });
</script>
</body>
</html>"""



# ══════════════════════════════════════════════════════════
#  API: Admin – Real stats & user management
# ══════════════════════════════════════════════════════════

@app.get("/api/admin/stats")
async def admin_stats(request: Request):
    require_admin(request)
    import subprocess
    users_data = load_users()
    user_count = len(users_data)
    dir_preset_count = 8  # E2, E3, E4, H区, J3区, YLd, YLe, YLf
    file_count = 0
    try:
        result = subprocess.run(
            ["find", "/data/汉口北工作/证照/", "-type", "f"],
            capture_output=True, text=True, timeout=30
        )
        lines = result.stdout.strip()
        file_count = len(lines.split("\n")) if lines else 0
    except Exception:
        file_count = 0
    return {"file_count": file_count, "dir_count": dir_preset_count, "user_count": user_count}


@app.get("/api/admin/users")
async def admin_users(request: Request):
    require_admin(request)
    users_data = load_users()
    result = []
    for uname, u in users_data.items():
        result.append({
            "username": uname,
            "name": u.get("name", ""),
            "department": u.get("department", ""),
            "role": u.get("role", ""),
            "approved": u.get("approved", False),
            "created_at": u.get("created_at", ""),
        })
    return {"users": result}


@app.post("/api/admin/users/update")
async def admin_users_update(request: Request):
    require_admin(request)
    body = await request.json()
    username = body.get("username", "").strip()
    users_data = load_users()
    if username not in users_data:
        raise HTTPException(status_code=404, detail="用户不存在")
    if "name" in body and body["name"].strip():
        users_data[username]["name"] = body["name"].strip()
    if "department" in body and body["department"].strip():
        users_data[username]["department"] = body["department"].strip()
    if "role" in body and body["role"].strip():
        users_data[username]["role"] = body["role"].strip()
    save_users(users_data)
    return {"message": "更新成功"}

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


if __name__ == "__main__":
    print(f"\n  投融资部信息智能化平台 已启动")
    print(f"  本机访问: http://localhost:{PORT}")
    print(f"  手机访问: http://{get_lan_ip()}:{PORT}")
    print(f"  管理员账号: bowin6699")
    print(f"  按 Ctrl+C 停止\n")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
