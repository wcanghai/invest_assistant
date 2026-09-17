Option Explicit

Dim shell, fileSystem, scriptDirectory, guardScript, command, argument, result

Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")

scriptDirectory = fileSystem.GetParentFolderName(WScript.ScriptFullName)
guardScript = fileSystem.BuildPath(scriptDirectory, "check_and_launch_daily_sync.ps1")

command = "powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden" & _
          " -ExecutionPolicy Bypass -File " & QuoteArgument(guardScript)

For Each argument In WScript.Arguments
    command = command & " " & QuoteArgument(CStr(argument))
Next

' Window style 0 keeps both wscript.exe and the checker PowerShell invisible.
' Wait for the short checker and propagate launch failures to Task Scheduler.
' The checker launches its long-running Python worker in a hidden window.
result = shell.Run(command, 0, True)
WScript.Quit result

Function QuoteArgument(ByVal value)
    QuoteArgument = Chr(34) & Replace(value, Chr(34), Chr(34) & Chr(34)) & Chr(34)
End Function
