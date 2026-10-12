@echo off
echo ========================================
echo 网络修复工具
echo ========================================
echo.

echo [1/3] 检测当前网络状态...
powershell -Command "$ErrorActionPreference = 'SilentlyContinue'; $testUrls = @('www.baidu.com', '114.114.114.114', 'www.qq.com'); $success = $false; foreach ($url in $testUrls) { $result = Test-Connection -ComputerName $url -Count 1 -Quiet; if ($result) { $success = $true; break; } }; if ($success) { Write-Host '网络正常，无需修复' -ForegroundColor Green; exit 0 } else { Write-Host '网络异常，开始修复...' -ForegroundColor Yellow; exit 1 }"

if %errorlevel% equ 0 (
    echo.
    pause
    exit /b 0
)

echo.
echo [2/3] 关闭系统代理...
powershell -Command "$regPath = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'; try { Set-ItemProperty -Path $regPath -Name ProxyEnable -Value 0 -ErrorAction Stop; Write-Host '系统代理已禁用' -ForegroundColor Green; $signature = '[DllImport(\"wininet.dll\")] public static extern bool InternetSetOption(IntPtr hInternet, int dwOption, IntPtr lpBuffer, int dwBufferLength);'; $type = Add-Type -MemberDefinition $signature -Name WinINet -Namespace InternetSettings -PassThru; [void]$type::InternetSetOption([IntPtr]::Zero, 39, [IntPtr]::Zero, 0); [void]$type::InternetSetOption([IntPtr]::Zero, 37, [IntPtr]::Zero, 0); } catch { Write-Host '关闭代理时出错' -ForegroundColor Red; exit 1 }"

echo.
echo [3/3] 验证网络连接...
timeout /t 2 /nobreak >nul

powershell -Command "$ErrorActionPreference = 'SilentlyContinue'; $testUrls = @('www.baidu.com', '114.114.114.114', 'www.qq.com'); $success = $false; foreach ($url in $testUrls) { $result = Test-Connection -ComputerName $url -Count 1 -Quiet; if ($result) { $success = $true; break; } }; if ($success) { Write-Host '网络已恢复正常！' -ForegroundColor Green; exit 0 } else { Write-Host '网络仍然异常，请尝试以下方法：' -ForegroundColor Red; Write-Host '1. 重启浏览器' -ForegroundColor Yellow; Write-Host '2. 手动关闭代理：设置 -> 网络和Internet -> 代理' -ForegroundColor Yellow; Write-Host '3. 检查防火墙设置' -ForegroundColor Yellow; Write-Host '4. 重启电脑' -ForegroundColor Yellow; exit 1 }"

echo.
pause
