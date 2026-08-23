' AI Agent Lab one-click launcher: runs start.ps1 with no console window.
' The desktop shortcut points here. See launcher.log for what happened.
Dim shell, fso, here
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
' 0 = hidden window / False = do not wait for exit
shell.Run "powershell -NoProfile -ExecutionPolicy Bypass -File """ & here & "\start.ps1""", 0, False
