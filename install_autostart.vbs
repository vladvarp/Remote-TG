Option Explicit

Dim shell, fso, scriptDir, startup, linkPath, link, quiet

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
quiet = False
If WScript.Arguments.Count > 0 Then
  If LCase(WScript.Arguments(0)) = "quiet" Then quiet = True
End If

startup = shell.SpecialFolders("Startup")
linkPath = startup & "\Remote TG.lnk"
Set link = shell.CreateShortcut(linkPath)
link.TargetPath = "wscript.exe"
link.Arguments = """" & scriptDir & "\start.vbs"""
link.WorkingDirectory = scriptDir
link.WindowStyle = 7
link.IconLocation = scriptDir & "\assets\icon.ico,0"
link.Description = "Remote TG"
On Error Resume Next
link.Save
If Err.Number <> 0 Then
  If Not quiet Then MsgBox "Не удалось создать ярлык: " & Err.Description, vbCritical, "Remote TG"
  WScript.Quit 1
End If
On Error GoTo 0
If Not quiet Then MsgBox "Автозагрузка включена:" & vbCrLf & linkPath, vbInformation, "Remote TG"
