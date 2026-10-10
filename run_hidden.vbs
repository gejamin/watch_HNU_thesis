' Silently run the thesis-review watcher watchdog script.
'
' Why: if a Windows scheduled task launches powershell.exe / python.exe directly and the
' task logon mode is "Interactive only", a console window flashes on every trigger.
' This launcher is started by wscript and runs powershell with a hidden window:
'   * sh.Run arg 2 = 0     -> hidden window
'   * sh.Run arg 3 = False -> do not wait (the watchdog exits quickly)
'
' NOTE: keep this file ASCII-only -- wscript reads .vbs using the ANSI code page,
' so non-ASCII comments can corrupt parsing.

Option Explicit

Dim sh, script, cmd
Set sh = CreateObject("WScript.Shell")

script = "C:\Users\gejiamin\Downloads\get_thesis\ensure_watcher.ps1"
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & script & """"

sh.Run cmd, 0, False
