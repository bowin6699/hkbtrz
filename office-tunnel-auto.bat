@echo off
title Office Tunnel (auto-reconnect)
echo Office Tunnel - Auto-reconnect mode
echo Press Ctrl+C to stop
echo.
:loop
echo [%%date%% %%time%%] Connecting...
ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30 -N -R 2222:localhost:22 root@YOUR_SERVER_IP
echo Disconnected. Reconnecting in 10 seconds...
timeout /t 10 /nobreak >nul
goto loop
