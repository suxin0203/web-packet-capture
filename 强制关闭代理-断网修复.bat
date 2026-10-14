@echo off
setlocal
title 网络一键诊断修复 - 抓包助手
cd /d %~dp0

echo ══════════════════════════════════════════
echo   网络一键诊断修复工具 (抓包助手)
echo   适用: 浏览器/网页打不开、断网、代理残留
echo ══════════════════════════════════════════
echo.

set FIXED=0

:: ---------- [1] 读取系统代理状态 ----------
set PROXY_ON=0
set PROXY_ALIVE=0
for /f "tokens=3" %%a in ('reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyEnable 2^>NUL ^| findstr ProxyEnable') do set PROXY_ON=%%a
set /a PROXY_ON=%PROXY_ON% 2>NUL
if "%PROXY_ON%"=="" set PROXY_ON=0
set PROXY_ADDR=
for /f "tokens=3" %%a in ('reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyServer 2^>NUL ^| findstr ProxyServer') do set PROXY_ADDR=%%a
echo [1] 系统代理: ProxyEnable=%PROXY_ON%  ProxyServer=%PROXY_ADDR%

:: ---------- [2] 代理指向的服务是否存活 ----------
if "%PROXY_ON%"=="1" if not "%PROXY_ADDR%"=="" (
  for /f "tokens=2 delims=:" %%p in ("%PROXY_ADDR%") do set PROXY_PORT=%%p
  powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort %PROXY_PORT% -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >NUL 2>&1
  if not errorlevel 1 set PROXY_ALIVE=1
  if "%PROXY_ALIVE%"=="1" (
    echo [2] 代理服务存活, 当前处于抓包模式
  ) else (
    echo [2] 代理指向的服务已停止 -- 这就是断网原因! ^(代理残留^)
  )
)

:: ---------- [3] 浏览器路径连通测试 ----------
echo [3] 测试网页连通性(走系统代理的真实路径)...
set NET_OK=0
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'https://www.baidu.com' -UseBasicParsing -TimeoutSec 8 | Out-Null; exit 0 } catch { exit 1 }" >NUL 2>&1
if not errorlevel 1 set NET_OK=1

:: ---------- [分支处理] ----------
if "%PROXY_ON%"=="1" if "%PROXY_ALIVE%"=="1" if "%NET_OK%"=="1" (
  echo.
  echo [诊断] 抓包正在运行, 网络经由代理 -- 这是正常状态
  echo        但如果抖音/个别网页打不开, 多为证书未被信任:
  echo        重启抓包助手后点顶部「证书」徽标安装, 或点「是」确认
  echo.
  choice /c YN /m "是否退出抓包模式恢复直连网络 [Y=恢复/N=保持抓包]"
  if not errorlevel 2 (
    taskkill /f /im "抓包工具网页版.exe" >NUL 2>&1
    for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":16823 .*LISTENING"') do taskkill /f /pid %%p >NUL 2>&1
    for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":16824 .*LISTENING"') do taskkill /f /pid %%p >NUL 2>&1
    reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyEnable /t REG_DWORD /d 0 /f >NUL
    set FIXED=1
    echo 已退出抓包模式并恢复直连
  )
  goto REPORT
)

if "%PROXY_ON%"=="1" (
  echo.
  echo [修复] 关闭残留代理 + 清理 PAC 自动配置...
  reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyEnable /t REG_DWORD /d 0 /f >NUL
  reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v AutoConfigURL /f >NUL 2>&1
  set FIXED=1
)

:: ---------- [4] 代理已处理, 复测网络 ----------
if "%FIXED%"=="1" (
  powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'https://www.baidu.com' -UseBasicParsing -TimeoutSec 8 | Out-Null; exit 0 } catch { exit 1 }" >NUL 2>&1
  if not errorlevel 1 (
    echo [4] 网络已恢复 √ -- 问题就是代理残留
    goto REPORT
  )
)

:: ---------- [5] 清 DNS + 重置 WinHTTP ----------
if "%NET_OK%"=="0" (
  echo [5] 深度修复: 刷新 DNS 缓存 + 重置 WinHTTP 代理...
  ipconfig /flushdns >NUL 2>&1
  powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath cmd.exe -ArgumentList '/c netsh winhttp reset proxy'" >NUL 2>&1
  timeout /t 3 /nobreak >NUL
  powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'https://www.baidu.com' -UseBasicParsing -TimeoutSec 8 | Out-Null; exit 0 } catch { exit 1 }" >NUL 2>&1
  if not errorlevel 1 (
    echo [5] 网络已恢复 √
    goto REPORT
  )
)

:: ---------- [6] 杀掉抓包服务残留进程 ----------
echo [6] 检查抓包服务残留进程...
set KILLED=0
taskkill /f /im "抓包工具网页版.exe" >NUL 2>&1 && set KILLED=1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":16823 .*LISTENING"') do taskkill /f /pid %%p >NUL 2>&1 && set KILLED=1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":16824 .*LISTENING"') do taskkill /f /pid %%p >NUL 2>&1 && set KILLED=1
if "%KILLED%"=="1" (
  echo [6] 已结束抓包服务残留进程
  timeout /t 2 /nobreak >NUL
)

:: ---------- [7] 终极选项 ----------
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'https://www.baidu.com' -UseBasicParsing -TimeoutSec 8 | Out-Null; exit 0 } catch { exit 1 }" >NUL 2>&1
if not errorlevel 1 (
  echo [7] 网络已恢复 √
  goto REPORT
)
echo.
echo [7] 常规手段未能恢复, 需要终极手段(需重启电脑生效):
echo     [1] 重置 Winsock(重启后生效)
echo     [2] 跳过(建议先重启电脑 / 检查路由器 / 关闭浏览器代理插件)
choice /c 12 /m "选择"
if not errorlevel 2 (
  powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath cmd.exe -ArgumentList '/c netsh winsock reset & echo 完成, 请重启电脑'" >NUL 2>&1
)

:REPORT
echo.
echo ══════════════════════════════════════════
echo   诊断报告
echo ══════════════════════════════════════════
if "%FIXED%"=="1" echo   已修复: 残留代理/抓包进程问题
echo   系统代理: 
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyEnable 2>NUL | findstr ProxyEnable
echo.
echo   如果浏览器仍打不开个别网站:
echo     1. 重启浏览器 (旧页面会缓存错误状态)
echo     2. Firefox 用户: 浏览器有独立代理设置, 检查 选项-网络设置
echo     3. 抖音等 App 网页: 检查是否开了浏览器代理插件
echo     4. 全部无效: 重启电脑
echo.
pause
