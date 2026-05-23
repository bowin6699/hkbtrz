# 投融资部信息智能化平台

基于 Python FastAPI 的本地文件检索与企业用户管理 Web 系统，支持局域网及公网访问。部署于阿里云服务器，通过 Nginx 反向代理 + 宝塔面板管理。

---

## 功能总览

### 🔍 文件检索
- 多目录文件搜索（图片、PDF、Word、Excel、文件夹）
- 支持关键词模糊匹配与文件类型过滤
- 文件在线预览（图片/PDF/Office文档）
- 文件下载
- 目录入口：E2、E3、E4、H区、J3区、YLd、YLe、YLf

### 🔐 用户系统
- 用户注册与登录（密码加密存储）
- 注册审核（管理员审批）
- 密码错误 5 次锁定 30 分钟
- 会话过期机制（可配置有效期）
- 实时数据统计（文件数/目录数/用户数）

### 👥 用户管理（仅管理员）
- 注册审批（通过/拒绝）
- 已注册用户列表展示（账号、姓名、部门、角色、状态、注册时间）
- 编辑用户实名信息（姓名、部门）
- 管理员/普通用户角色标识

### 🖼️ 界面
- 汉口北背景图片
- 深蓝金配色风格
- 响应式设计（支持手机端）
- 登录/注册/检索/管理 多页面布局

### 🔗 远程文件服务器搜索
- 通过 SSH 隧道连接 Windows 文件服务器（YLd/YLe/YLf）
- 支持远程 `dir` 命令搜索

### 🔒 安全防护
- 请求频率限制（可配置每分钟上限）
- 安全响应头（X-Frame-Options、XSS 防护等）
- 会话过期自动退出
- 密码 SHA256 加盐加密

---

## 技术栈

| 组件 | 技术 |
|------|------|
| 后端框架 | Python FastAPI |
| Web 服务器 | Uvicorn |
| 反向代理 | Nginx（宝塔面板管理） |
| 数据存储 | JSON 文件（users.json / config.json） |
| 远程访问 | SSH 隧道（端口映射 / 反向代理） |
| 自动部署 | GitHub Actions |

---

## 项目结构

```
/data/file-finder/
├── server.py              # 主程序
├── preview.html           # 前端页面（注入到 server.py）
├── config.json            # 敏感配置（含密码，不上传 Git）
├── config.example.json    # 配置模板
├── users.json             # 用户数据（不上传 Git）
├── requirements.txt       # Python 依赖
├── start.sh               # Linux 启动脚本
├── office-tunnel-auto.bat # Windows 远程文件服务器 SSH 隧道
├── W020210618335720932703.jpg  # 登录页背景图
└── .github/workflows/
    └── deploy.yml         # GitHub Actions 自动部署
```

---

## 配置说明

### config.json

```json
{
    "win_password": "Windows远程服务器密码",
    "session_expire_hours": 12,
    "rate_limit_per_minute": 30,
    "allowed_download_paths": [
        "/data/汉口北工作/证照",
        "/mnt/office"
    ]
}
```

### users.json

每用户一条记录，示例：

```json
{
  "bowin6699": {
    "password": "sha256加盐密码",
    "role": "admin",
    "name": "管理员",
    "department": "管理部",
    "approved": true,
    "created_at": "2026-05-15T11:01:08"
  }
}
```

首次启动自动创建管理员账号 `bowin6699`。

---

## 部署指南

### 本地开发

```bash
pip install -r requirements.txt
cp config.example.json config.json  # 编辑填入实际配置
python server.py
```

访问 `http://localhost:8000`

### 服务器部署（阿里云）

#### systemd 服务

创建 `/etc/systemd/system/filefinder.service`：

```ini
[Unit]
Description=汉口北集团不动产信息检索
After=network.target

[Service]
Type=simple
WorkingDirectory=/data
ExecStart=/usr/bin/python3.11 /data/file-finder/server.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

#### Nginx 反向代理（宝塔面板）

在宝塔面板 - 网站 - 添加反向代理：

```
代理路径: /rz/
目标URL:  http://127.0.0.1:8000/
```

若代理前缀不为 `/`，前端静态资源路径需对应调整（如 `/bg.jpg` → `/rz/bg.jpg`）。

#### GitHub Actions 自动部署

推送 `main` 分支自动触发部署，需在 GitHub 仓库设置以下 Secrets：

| Secret | 说明 |
|--------|------|
| `SERVER_HOST` | 服务器 IP 地址 |
| `SERVER_USER` | SSH 用户名（如 root） |
| `SERVER_PASSWORD` | SSH 密码 |

---

## 启动命令

### 服务器

```bash
cd /data/file-finder && nohup python3.11 server.py > /tmp/hkbtrz.log 2>&1 &
```

### 远程 Windows 文件服务器隧道（在 Windows 机器上运行）

```
office-tunnel-auto.bat
```
建立 SSH 反向隧道，将 Windows 的 SSH 端口（22）映射到服务器的 2222 端口，供搜索模块远程调用 `dir` 命令。

---

## API 接口

| 方法 | 路径 | 说明 | 权限 |
|------|------|------|------|
| GET | `/` | 返回前端页面 HTML | 公开 |
| GET | `/bg.jpg` | 登录页背景图 | 公开 |
| POST | `/api/login` | 用户登录 | 公开 |
| POST | `/api/register` | 用户注册 | 公开 |
| POST | `/api/logout` | 用户退出 | 登录 |
| GET | `/api/info` | 服务器信息（IP/端口） | 登录 |
| GET | `/api/search` | 文件搜索 | 登录 |
| GET | `/api/preview` | 文件预览 | 登录 |
| GET | `/api/download` | 文件下载 | 登录 |
| GET | `/api/admin/pending` | 待审批用户列表 | 管理员 |
| POST | `/api/admin/approve` | 审批用户（通过/拒绝） | 管理员 |
| GET | `/api/admin/stats` | 实时统计数据（文件/目录/用户） | 管理员 |
| GET | `/api/admin/users` | 所有注册用户列表 | 管理员 |
| POST | `/api/admin/users/update` | 修改用户信息（姓名/部门） | 管理员 |

---

## 管理账号

管理员账号 `bowin6699`，密码在服务器的 `config.json` 中配置（`admin_password` 字段）。

管理员登录后右上角可见「用户管理」入口，支持：审批新用户、编辑已注册用户信息。

---

## 运行端口

- 内部服务端口: **8000**
- 公网访问: **80（Nginx 代理到 8000）**
- SSH 隧道端口: **2222**（Windows 远程文件服务器）

---

## 注意事项

- `config.json` 和 `users.json` 包含敏感信息，已加入 `.gitignore`
- 先通过 `start.sh` 建立 SSH 隧道后，文件搜索功能才能访问远程 Windows 目录
- 搜索目录若为网络挂载点，需确保 `sshpass` 和 SSH 隧道可用