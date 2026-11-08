Option Explicit

Dim shell, fso, scriptDir, pyw, script

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
script = scriptDir & "\app\main.py"

If WScript.Arguments.Count = 0 Then
  If Not IsAdmin() Then
    CreateObject("Shell.Application").ShellExecute "wscript.exe", """" & WScript.ScriptFullName & """ elevated", scriptDir, "runas", 0
    WScript.Quit 0
  End If
ElseIf Not IsAdmin() Then
  MsgBox "Запуск от администратора отменён.", vbCritical, "Remote TG"
  WScript.Quit 1
End If

pyw = FindPyw()
If pyw = "" Then
  MsgBox "Не найден Python 3.11 или новее. Установите его вместе с py launcher.", vbCritical, "Remote TG"
  WScript.Quit 1
End If

shell.CurrentDirectory = scriptDir
If LCase(fso.GetFileName(pyw)) = "pyw.exe" Then
  shell.Run """" & pyw & """ -3 """ & script & """", 0, False
Else
  shell.Run """" & pyw & """ """ & script & """", 0, False
End If

Function IsAdmin()
  On Error Resume Next
  shell.RegRead "HKEY_USERS\S-1-5-19\Environment\TEMP"
  If Err.Number = 0 Then
    IsAdmin = True
  Else
    IsAdmin = False
  End If
  Err.Clear
End Function

Function FindPyw()
  Dim found, versions
  versions = Array("3.14", "3.13", "3.12", "3.11")
  found = RegPyw("HKCU", versions)
  If found <> "" Then
    FindPyw = found
    Exit Function
  End If
  found = RegPyw("HKLM", versions)
  If found <> "" Then
    FindPyw = found
    Exit Function
  End If
  found = FirstExisting(Array( _
    shell.ExpandEnvironmentStrings("%LocalAppData%") & "\Programs\Python\Launcher\pyw.exe", _
    shell.ExpandEnvironmentStrings("%WINDIR%") & "\pyw.exe", _
    shell.ExpandEnvironmentStrings("%LocalAppData%") & "\Programs\Python\Python314\pythonw.exe", _
    shell.ExpandEnvironmentStrings("%LocalAppData%") & "\Programs\Python\Python313\pythonw.exe", _
    shell.ExpandEnvironmentStrings("%LocalAppData%") & "\Programs\Python\Python312\pythonw.exe", _
    shell.ExpandEnvironmentStrings("%LocalAppData%") & "\Programs\Python\Python311\pythonw.exe" _
  ))
  If found <> "" Then
    FindPyw = found
    Exit Function
  End If
  FindPyw = OnPath("pyw.exe")
End Function

Function RegPyw(root, versions)
  Dim i, install
  RegPyw = ""
  For i = 0 To UBound(versions)
    On Error Resume Next
    Err.Clear
    install = shell.RegRead(root & "\Software\Python\PythonCore\" & versions(i) & "\InstallPath\")
    If Err.Number = 0 Then
      If Len(install) > 0 Then
        If Right(install, 1) <> "\" Then install = install & "\"
        If fso.FileExists(install & "pythonw.exe") Then
          RegPyw = install & "pythonw.exe"
          Exit Function
        End If
      End If
    End If
    Err.Clear
  Next
End Function

Function FirstExisting(paths)
  Dim i
  FirstExisting = ""
  For i = 0 To UBound(paths)
    If fso.FileExists(paths(i)) Then
      FirstExisting = paths(i)
      Exit Function
    End If
  Next
End Function

Function OnPath(name)
  Dim env, dirs, i, candidate
  OnPath = ""
  env = shell.ExpandEnvironmentStrings("%PATH%")
  dirs = Split(env, ";")
  For i = 0 To UBound(dirs)
    candidate = dirs(i)
    If candidate <> "" Then
      If Right(candidate, 1) <> "\" Then candidate = candidate & "\"
      If fso.FileExists(candidate & name) Then
        OnPath = candidate & name
        Exit Function
      End If
    End If
  Next
End Function
