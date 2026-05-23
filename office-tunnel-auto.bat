@echo off
title Office Tunnel (auto-reconnect)
echo Office Tunnel - Auto-reconnect mode
echo Press Ctrl+C to stop
echo.
echo Edit this file to set SERVER_HOST to your server address before running.

:: ── 配置 ──
:: 请修改为实际的服务器地址后再运行
set SERVER_HOST=YOUR_SERVER_IP
set SERVER_USER=root

:loop
echo [%%date%% %%time%%] Connecting...
ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30 -N -R 2222:localhost:22 %SERVER_USER%@%SERVER_HOST%
echo Disconnected. Reconnecting in 10 seconds...
timeout /t 10 /nobreak >nul
goto loop