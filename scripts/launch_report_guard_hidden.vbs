Option Explicit
Dim shell, fs, script, command, result
Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
script = fs.BuildPath(fs.GetParentFolderName(WScript.ScriptFullName), "run_report_guard.ps1")
command = "powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden" & _
          " -ExecutionPolicy Bypass -File " & Chr(34) & script & Chr(34)
result = shell.Run(command, 0, True)
WScript.Quit result
