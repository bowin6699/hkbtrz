# 投融资部信息智能化平台

基于 Python FastAPI 的本地文件搜索与下载 Web 工具，支持图片、PDF、Word、Excel 及文件夹搜索。

## 功能

- 文件搜索（图片、PDF、Word、Excel、文件夹）
- 用户注册/登录/审批系统
- 远程 Windows 文件服务器搜索
- 文件在线预览与下载
- 移动端响应式界面

## 快速开始

```bash
pip install -r requirements.txt
cp config.example.json config.json  # 编辑 config.json 填入实际配置
python server.py
```

## 部署

服务器通过 systemd 管理，定时 8:00 启动、22:00 停止。
