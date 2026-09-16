Option Explicit

Dim shell, fileSystem, scriptDirectory, guardScript, command, argument

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
' The checker itself may still deliberately open a visible sync terminal when
' today's A-share or ETF data is incomplete.
shell.Run command, 0, False

Function QuoteArgument(ByVal value)
    QuoteArgument = Chr(34) & Replace(value, Chr(34), Chr(34) & Chr(34)) & Chr(34)
End Function
