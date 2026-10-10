# 论文评阅书监控（get_thesis 版）—— 看门狗
#
# 由 Windows 计划任务 GetThesis_ReviewWatch 每 10 分钟经 run_hidden.vbs **隐藏**调用。
# 作用：监控进程若不在（崩溃、注销重登、机器重启后），自动把它拉起来；正常时静默退出。
#
# 三种情况不会拉起：
#   1. 存在 .watch_stop —— 用户主动停止；
#   2. 监控进程已在运行；
#   3. 今日重启次数已达上限（防脚本自身崩溃导致无限拉起）。
#
# 注意：所有子进程都用 -WindowStyle Hidden 启动，配合 run_hidden.vbs，
#       计划任务触发时不会闪出任何命令行窗口。

$ErrorActionPreference = 'SilentlyContinue'

$WorkDir  = 'C:\Users\gejiamin\Downloads\get_thesis'
$Py       = 'C:\Users\gejiamin\anaconda3\envs\get_thesis\python.exe'
$Script   = 'watch_thesis.py'
$Interval = 60                       # 5 分钟一轮
$LogFile  = Join-Path $WorkDir 'watchdog.log'

function Write-Log([string]$msg) {
    Add-Content -Path $LogFile -Encoding UTF8 `
        -Value ("[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg)
}

# --- 1. 用户主动停止？ ---------------------------------------------------
if (Test-Path (Join-Path $WorkDir '.watch_stop')) { exit 0 }

# --- 2. 监控进程是否已在跑？ ---------------------------------------------
$running = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
             Where-Object { $_.CommandLine -like "*$Script*" })
if ($running.Count -gt 0) { exit 0 }        # 一切正常，不写日志

# --- 3. 今日重启次数是否超限？ -------------------------------------------
$today = Get-Date -Format 'yyyy-MM-dd'
$restartsToday = 0
if (Test-Path $LogFile) {
    $restartsToday = @(Select-String -Path $LogFile -SimpleMatch '已拉起' |
                       Where-Object { $_.Line -like "*$today*" }).Count
}
if ($restartsToday -ge 10) {
    Write-Log "今日重启已达 10 次，暂停拉起，请人工检查 check_thesis.log"
    exit 0
}

# --- 4. 拉起监控（隐藏窗口） ---------------------------------------------
Start-Process -FilePath $Py `
    -ArgumentList @($Script, '--interval', "$Interval") `
    -WorkingDirectory $WorkDir `
    -WindowStyle Hidden
Write-Log "监控进程不在，已拉起（interval=${Interval}s）"
