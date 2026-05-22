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
    if "bowin6699" not in users:
        users["bowin6699"] = {
            "password": hash_password("ADMIN_PASSWORD"),
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
@app.get("/logo")
async def logo():
    logo_path = "/home/user/汉口北工作/2026金融赋能工作/2026金融赋能工作/宣讲会/LOGO/白色集团.png"
    return FileResponse(logo_path, media_type="image/png")


#  Frontend SPA
# ══════════════════════════════════════════════════════════

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>投融资部信息智能化平台</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f0f2f5; color: #333; min-height: 100vh; }
/* ── Login / Register ── */
.auth-page { display: flex; justify-content: center; align-items: center; width: 100vw; height: 100vh; padding: 20px; background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); position: fixed; top: 0; left: 0; }
.auth-card { background: #fff; border-radius: 16px; padding: 36px 28px; width: 100%; max-width: 400px; box-shadow: 0 8px 30px rgba(0,0,0,0.2); }
.auth-card h2 { text-align: center; margin-bottom: 6px; font-size: 1.4rem; color: #1a1a2e; }
.auth-card .subtitle { text-align: center; font-size: 0.8rem; color: #999; margin-bottom: 24px; }
.auth-card label { display: block; font-size: 0.85rem; font-weight: 600; margin-bottom: 4px; color: #555; }
.auth-card input { width: 100%; padding: 10px 12px; border: 1px solid #ddd; border-radius: 8px; font-size: 15px; margin-bottom: 14px; outline: none; }
.auth-card input:focus { border-color: #4a6cf7; }
.auth-card .btn-primary { width: 100%; padding: 12px; background: #1a1a2e; color: #fff; border: none; border-radius: 8px; font-size: 16px; font-weight: 600; cursor: pointer; }
.auth-card .btn-primary:hover { background: #16213e; }
.auth-card .switch { text-align: center; margin-top: 14px; font-size: 0.85rem; color: #888; }
.auth-card .switch a { color: #4a6cf7; cursor: pointer; text-decoration: none; font-weight: 600; }
.auth-card .error { color: #e74c3c; font-size: 0.82rem; text-align: center; margin-bottom: 10px; min-height: 20px; }
.auth-card .success { color: #27ae60; font-size: 0.82rem; text-align: center; margin-bottom: 10px; }
/* ── Header ── */
.header { background: #1a1a2e; color: #fff; padding: 10px 20px; display: flex; align-items: center; position: relative; min-height: 48px; }
.header-logo { height: 34px; width: auto; flex-shrink: 0; }
.header h1 { font-size: 1.2rem; letter-spacing: 1px; position: absolute; left: 50%; transform: translateX(-50%); white-space: nowrap; }
.header .user-bar { display: flex; align-items: center; gap: 10px; font-size: 0.85rem; margin-left: auto; }
.header .user-bar button { background: rgba(255,255,255,0.15); color: #fff; border: 1px solid rgba(255,255,255,0.3); padding: 5px 12px; border-radius: 6px; cursor: pointer; font-size: 0.8rem; }
.header .user-bar button:hover { background: rgba(255,255,255,0.25); }
.header .user-bar .admin-badge { background: #f39c12; color: #000; padding: 3px 8px; border-radius: 4px; font-weight: 700; font-size: 0.7rem; }

@media (max-width: 640px) {
  .header { flex-wrap: wrap; gap: 6px; padding: 8px 10px; }
  .header-logo { height: 24px; }
  .header h1 { font-size: 0.85rem; position: static; transform: none; flex: 1; text-align: center; }
  .header .user-bar { width: 100%; justify-content: center; gap: 4px; margin-left: 0; }
  .header .user-bar button { padding: 3px 7px; font-size: 0.68rem; }
}
/* ── Main content ── */
.container { max-width: 800px; margin: 0 auto; padding: 60px 16px 16px 16px; }
.page { display: none; }
.page.active { display: block; }
.auth-page.page.active { display: flex; }
.search-box { background: #fff; border-radius: 16px; padding: 28px 24px; box-shadow: 0 4px 16px rgba(0,0,0,0.1); margin-bottom: 20px; max-width: 600px; margin-left: auto; margin-right: auto; }
.row { display: flex; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
.row:last-child { margin-bottom: 0; }
input, select, button { font-size: 15px; padding: 10px 14px; border: 1px solid #ddd; border-radius: 8px; outline: none; }
input:focus, select:focus { border-color: #4a6cf7; }
input[type="text"] { flex: 1; min-width: 140px; }
select { background: #fff; cursor: pointer; }
button { cursor: pointer; transition: background 0.2s, transform 0.1s; }
button:active { transform: scale(0.97); }
.status { text-align: center; padding: 20px; color: #999; font-size: 0.9rem; }
.result-count { font-size: 0.85rem; color: #666; margin-bottom: 10px; }
.results { list-style: none; }
.result-card { display: flex; align-items: center; gap: 12px; background: #fff; border-radius: 10px; padding: 12px 14px; margin-bottom: 8px; box-shadow: 0 1px 4px rgba(0,0,0,0.05); transition: box-shadow 0.15s; }
.result-card:hover { box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
.icon { width: 40px; height: 40px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 1.3rem; flex-shrink: 0; }
.icon.folder { background: #fff3cd; }
.icon.image { background: #d4edda; }
.icon.pdf { background: #f8d7da; }
.icon.word { background: #d4e6f1; }
.icon.excel { background: #d5f5e3; }
.meta { flex: 1; min-width: 0; }
.meta .name { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.meta .info { font-size: 0.78rem; color: #888; margin-top: 2px; }
.btn-search { background: #4a6cf7; color: #fff; border: none; font-weight: 600; white-space: nowrap; }
.btn-search:hover { background: #3b5de7; }
.download-btn { background: #e8ecf4; color: #4a6cf7; padding: 8px 14px; border-radius: 6px; text-decoration: none; font-size: 0.85rem; font-weight: 600; border: none; cursor: pointer; flex-shrink: 0; }
.download-btn:hover { background: #dce1f0; }
.preview-btn { background: #e8f5e9; color: #2e7d32; padding: 8px 14px; border-radius: 6px; text-decoration: none; font-size: 0.85rem; font-weight: 600; border: none; cursor: pointer; flex-shrink: 0; }
.preview-btn:hover { background: #c8e6c9; }
.dir-presets { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; justify-content: center; }
.dir-preset { font-size: 0.9rem; padding: 8px 20px; background: #e8ecf4; color: #4a6cf7; border-radius: 20px; cursor: pointer; border: none; font-weight: 600; transition: all 0.2s; }
.dir-preset:hover { background: #dce1f0; }
.dir-preset.active { background: #4a6cf7; color: #fff; box-shadow: 0 2px 8px rgba(74,108,247,0.3); }
/* ── Modal ── */
.modal-overlay { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.7); z-index: 1000; justify-content: center; align-items: center; }
.modal-overlay.active { display: flex; }
.modal-box { background: #fff; border-radius: 12px; width: 92vw; height: 88vh; max-width: 1000px; display: flex; flex-direction: column; overflow: hidden; }
.modal-header { display: flex; justify-content: space-between; align-items: center; padding: 12px 18px; border-bottom: 1px solid #eee; }
.modal-header .title { font-weight: 600; font-size: 0.95rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; margin-right: 12px; }
.modal-close-x { background: none; border: none; font-size: 1.5rem; cursor: pointer; color: #999; padding: 0 4px; line-height: 1; }
.modal-body { flex: 1; overflow: auto; display: flex; justify-content: center; align-items: flex-start; padding: 8px; }
.modal-body img { max-width: 100%; height: auto; }
.modal-body iframe { width: 100%; height: 100%; border: none; min-height: 500px; }
.modal-actions { display: flex; gap: 8px; padding: 10px 18px; border-top: 1px solid #eee; justify-content: flex-end; }
.modal-dl { background: #4a6cf7; color: #fff; padding: 8px 20px; border-radius: 6px; text-decoration: none; font-size: 0.9rem; font-weight: 600; }
/* ── Admin panel ── */
.admin-panel h3 { margin-bottom: 12px; }
.admin-table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 10px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,0.05); }
.admin-table th { background: #f7f7f7; padding: 10px 12px; text-align: left; font-size: 0.8rem; color: #666; }
.admin-table td { padding: 10px 12px; border-top: 1px solid #f0f0f0; font-size: 0.85rem; }
.admin-approve { background: #27ae60; color: #fff; border: none; padding: 5px 12px; border-radius: 4px; font-size: 0.8rem; cursor: pointer; margin-right: 4px; }
.admin-reject { background: #e74c3c; color: #fff; border: none; padding: 5px 12px; border-radius: 4px; font-size: 0.8rem; cursor: pointer; }
.admin-empty { text-align: center; color: #999; padding: 30px; }
</style>
</head>
<body>

<!-- ═══════════ LOGIN PAGE ═══════════ -->
<div id="loginPage" class="auth-page page active">
  <div class="auth-card">
    <h2>投融资部信息智能化平台</h2>
    <div class="subtitle">请登录您的账号</div>
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

<!-- ═══════════ MAIN APP (hidden until login) ═══════════ -->
<div id="mainApp" class="page">
  <div class="header">
    <img class="header-logo" src="/logo" alt="Logo">
    <h1>投融资部信息智能化平台</h1>
    <div class="user-bar">
      <span id="userDisplay"></span>
      <span class="admin-badge" id="adminBadge" style="display:none">管理员</span>
      <button onclick="showAdmin()" id="adminBtn" style="display:none">审批管理</button>
      <button onclick="showSearchPage()">检索首页</button>
      <button onclick="doLogout()">退出</button>
    </div>
  </div>
  <!-- Search view -->
  <div id="searchView" class="container">
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
        <input type="text" id="searchInput" placeholder="输入关键词搜索文件名..." autocomplete="off" autofocus>
        <select id="typeSelect">
          <option value="all">全部</option>
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
    <div id="status" class="status">输入目录和关键词开始搜索</div>
  </div>

  <!-- Admin view -->
  <div id="adminView" class="container" style="display:none">
    <div class="admin-panel">
      <h3>注册审批管理</h3>
      <div id="adminContent"></div>
    </div>
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
      <button onclick="closePreview()" style="background:#eee;color:#333;padding:8px 20px;border-radius:6px;border:none;font-size:0.9rem;cursor:pointer">关闭</button>
      <a class="modal-dl" id="modalDownload" href="#" download>下载文件</a>
    </div>
  </div>
</div>

<script>
// ── Global state ──
var token = localStorage.getItem('token') || '';
var currentUser = null;
var API = '';

// ── Init ──
(function() {
  if (token) {
    fetch(API + '/api/me', { headers: { 'Authorization': 'Bearer ' + token } })
      .then(function(r) {
        if (r.ok) return r.json().then(function(u) {
          currentUser = u;
          showMainApp();
        });
        else { localStorage.removeItem('token'); token = ''; }
      })
      .catch(function() {});
  }
})();

// ── Page switching ──
function showPage(id) {
  document.querySelectorAll('.page').forEach(function(p) { p.classList.remove('active'); });
  var el = document.getElementById(id);
  if (el) el.classList.add('active');
}

function showMainApp() {
  showPage('mainApp');
  document.getElementById('userDisplay').textContent = currentUser.name + ' (' + currentUser.username + ')';
  if (currentUser.role === 'admin') {
    document.getElementById('adminBadge').style.display = 'inline';
    document.getElementById('adminBtn').style.display = 'inline';
  } else {
    document.getElementById('adminBadge').style.display = 'none';
    document.getElementById('adminBtn').style.display = 'none';
  }
  showSearchPage();
}

function showSearchPage() {
  document.getElementById('searchView').style.display = 'block';
  document.getElementById('adminView').style.display = 'none';
}

function showAdmin() {
  if (!currentUser || currentUser.role !== 'admin') {
    showSearchPage();
    return;
  }
  document.getElementById('searchView').style.display = 'none';
  document.getElementById('adminView').style.display = 'block';
  loadAdminPending();
}

// ── Auth actions ──
function doLogin() {
  var u = document.getElementById('loginUsername').value.trim();
  var p = document.getElementById('loginPassword').value;
  var errEl = document.getElementById('loginError');
  errEl.textContent = '';
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
  errEl.textContent = '';
  okEl.textContent = '';
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
  fetch(API + '/api/logout', {
    method: 'POST',
    headers: { 'Authorization': 'Bearer ' + token }
  }).finally(function() {
    localStorage.removeItem('token');
    token = '';
    currentUser = null;
    showPage('loginPage');
    document.getElementById('loginError').textContent = '';
  });
}

// ── Admin actions ──
function loadAdminPending() {
  var el = document.getElementById('adminContent');
  fetch(API + '/api/admin/pending', { headers: { 'Authorization': 'Bearer ' + token } })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.pending.length === 0) {
        el.innerHTML = '<div class="admin-empty">没有待审批的注册申请</div>';
        return;
      }
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

function approveUser(username, action) {
  if (action === 'reject' && !confirm('确定拒绝 ' + username + ' 的注册申请吗？该账号将被删除。')) return;
  fetch(API + '/api/admin/approve', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token },
    body: JSON.stringify({ username: username, action: action })
  })
  .then(function(r) { return r.json(); })
  .then(function() { loadAdminPending(); });
}

// ── Search ──
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
      if (data.results.length === 0) {
        resultCountEl.style.display = 'none';
        statusEl.style.display = 'block';
        statusEl.textContent = keyword ? '没有找到匹配的文件' : '请输入关键词搜索';
        return;
      }
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
            '<div class="info">' + escHtml(item.size) + ' &middot; ' + escHtml(item.modified) + '</div>' +
          '</div>' +
          (item.type !== 'folder'
            ? '<button class="preview-btn" onclick="openPreview(\'' + escJs(item.path) + '\',\'' + escJs(item.name) + '\',\'' + escJs(item.type) + '\')">预览</button>' +
              '<a class="download-btn" href="' + API + '/api/download?path=' + encodeURIComponent(item.path) + '" download>下载</a>'
            : '<button class="preview-btn" style="background:#fff3cd;color:#856404" onclick="enterFolder(\'' + escJs(item.path) + '\')">进入</button>');
        resultsEl.appendChild(li);
      });
    })
    .catch(function(err) {
      statusEl.style.display = 'block';
      statusEl.textContent = '搜索出错';
    });
}

// ── Preview ──
function openPreview(path, name, type) {
  document.getElementById('modalTitle').textContent = name;
  var body = document.getElementById('modalBody');
  body.innerHTML = '';
  // Pass token as query param for image/iframe src
  var previewUrl = API + '/api/preview?path=' + encodeURIComponent(path);
  if (type === 'image') {
    var img = document.createElement('img');
    img.src = previewUrl;
    img.alt = name;
    body.appendChild(img);
  } else if (type === 'word' || type === 'excel') {
    // Try iframe preview (Chrome/Edge may render Office docs natively)
    var iframe = document.createElement('iframe');
    iframe.src = previewUrl;
    body.appendChild(iframe);
  } else {
    var iframe = document.createElement('iframe');
    iframe.src = previewUrl;
    body.appendChild(iframe);
  }
  document.getElementById('modalDownload').href = API + '/api/download?path=' + encodeURIComponent(path);
  document.getElementById('modalOverlay').classList.add('active');
}
function closePreview() {
  document.getElementById('modalOverlay').classList.remove('active');
  document.getElementById('modalBody').innerHTML = '';
}

// ── Utils ──
function escHtml(s) {
  var d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
function escJs(s) { return s.replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/"/g,'\\"'); }

// Enter key on login/register fields
document.getElementById('loginPassword').addEventListener('keydown', function(e) { if (e.key === 'Enter') doLogin(); });
document.getElementById('regDept').addEventListener('keydown', function(e) { if (e.key === 'Enter') doRegister(); });
</script>
</body>
</html>"""


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
