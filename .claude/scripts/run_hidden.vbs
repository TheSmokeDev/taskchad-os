' run_hidden.vbs -- run a console script with NO visible window.
'
' Windows Task Scheduler tasks in the interactive session pop a cmd window
' for every .bat action (the watchdog alone = 288 windows/day). Wrapping the
' action as  wscript.exe run_hidden.vbs <script> [logfile]  runs it with
' window style 0: same user session (toasts + visible-Chrome CDP still
' reachable), no console. Stdout/stderr append to [logfile] when given so
' hidden runs keep receipts; the script's exit code is propagated so
' Task Scheduler LastTaskResult stays truthful.
Option Explicit

If WScript.Arguments.Count < 1 Then WScript.Quit 87

Dim sh, target, logf, cmd
Set sh = CreateObject("WScript.Shell")
target = WScript.Arguments(0)

If WScript.Arguments.Count >= 2 Then
    logf = WScript.Arguments(1)
    cmd = "cmd.exe /c " & Chr(34) & Chr(34) & target & Chr(34) & _
          " >> " & Chr(34) & logf & Chr(34) & " 2>&1" & Chr(34)
Else
    cmd = "cmd.exe /c " & Chr(34) & target & Chr(34)
End If

WScript.Quit sh.Run(cmd, 0, True)
