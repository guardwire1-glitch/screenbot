' ScreenBot Silent Launcher (Cross-Device & Friend-Proof)
On Error Resume Next

Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

currentDir = fso.GetAbsolutePathName(fso.GetParentFolderName(WScript.ScriptFullName))

' 1. Check if running inside an unextracted zip temporary folder
If InStr(LCase(currentDir), ".zip") > 0 Or InStr(LCase(currentDir), "\appdata\local\temp") > 0 Then
    MsgBox "You are running ScreenBot directly from inside a ZIP file!" & vbCrLf & vbCrLf & _
           "Please EXTRACT the zip first:" & vbCrLf & _
           "1. Right-click the zip file." & vbCrLf & _
           "2. Click 'Extract All...'" & vbCrLf & _
           "3. Open the extracted folder and run ScreenBot from there.", _
           vbExclamation, "ScreenBot - Please Extract Zip"
    WScript.Quit
End If

' 2. PREFERRED PAYLOAD: the frozen ScreenBot.exe.
'    This folder ships a self-contained build that needs NO Python install.
'    (The old launcher demanded bot.py here, which does not exist in a
'     frozen distribution, so every silent launch died with
'     "Cannot find bot.py".)
exePath = fso.GetAbsolutePathName(currentDir & "\ScreenBot.exe")
If fso.FileExists(exePath) Then
    WshShell.CurrentDirectory = currentDir
    WshShell.Run """" & exePath & """", 0, False
    If Err.Number <> 0 Then
        MsgBox "Error launching ScreenBot.exe:" & vbCrLf & Err.Description & vbCrLf & vbCrLf & _
               "Try running START_SCREENBOT.bat for a visible console.", _
               vbCritical, "ScreenBot Error"
    Else
        Err.Clear
    End If
    WScript.Quit
End If

' 3. FALLBACK: source mode (bot.py + Python) for developer installs only.
scriptPath = fso.GetAbsolutePathName(currentDir & "\bot.py")
If Not fso.FileExists(scriptPath) Then
    MsgBox "Neither ScreenBot.exe nor bot.py was found in:" & vbCrLf & currentDir & vbCrLf & vbCrLf & _
           "Please extract the archive fully, then run START_SCREENBOT.bat instead.", _
           vbCritical, "ScreenBot - Missing Files"
    WScript.Quit
End If

' 4. Locate Python / pythonw executable across local and system paths
pythonwExe = ""

' A: Local .venv in current directory
If fso.FileExists(currentDir & "\.venv\Scripts\pythonw.exe") Then
    pythonwExe = currentDir & "\.venv\Scripts\pythonw.exe"
' B: .venv in parent directory
ElseIf fso.FileExists(currentDir & "\..\.venv\Scripts\pythonw.exe") Then
    pythonwExe = fso.GetAbsolutePathName(currentDir & "\..\.venv\Scripts\pythonw.exe")
End If

' C: Check common Windows installation paths
If pythonwExe = "" Then
    localAppData = WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%")
    progFiles = WshShell.ExpandEnvironmentStrings("%ProgramFiles%")

    candidatePaths = Array( _
        localAppData & "\Programs\Python\Python314\pythonw.exe", _
        localAppData & "\Programs\Python\Python313\pythonw.exe", _
        localAppData & "\Programs\Python\Python312\pythonw.exe", _
        localAppData & "\Programs\Python\Python311\pythonw.exe", _
        localAppData & "\Programs\Python\Python310\pythonw.exe", _
        progFiles & "\Python314\pythonw.exe", _
        progFiles & "\Python313\pythonw.exe", _
        progFiles & "\Python312\pythonw.exe", _
        progFiles & "\Python311\pythonw.exe" _
    )

    For Each p In candidatePaths
        If fso.FileExists(p) Then
            pythonwExe = p
            Exit For
        End If
    Next
End If

' D: Try Windows Python Launcher pyw.exe
If pythonwExe = "" Then
    windir = WshShell.ExpandEnvironmentStrings("%WINDIR%")
    If fso.FileExists(windir & "\pyw.exe") Then
        pythonwExe = windir & "\pyw.exe"
    End If
End If

' E: Try pythonw on PATH
If pythonwExe = "" Then
    returnCode = WshShell.Run("pythonw.exe --version", 0, True)
    If Err.Number = 0 Then
        pythonwExe = "pythonw.exe"
    Else
        Err.Clear
    End If
End If

' If Python was not found, alert user clearly instead of crashing with error 80070002
If pythonwExe = "" Then
    MsgBox "Python was not found on this computer!" & vbCrLf & vbCrLf & _
           "This usually means ScreenBot.exe is missing from the folder." & vbCrLf & _
           "1. Re-extract the archive fully." & vbCrLf & _
           "2. Or install Python from https://www.python.org/ (check 'Add Python to PATH').", _
           vbCritical, "ScreenBot - Setup Required"
    WScript.Quit
End If

' 5. Launch silently
cmd = """" & pythonwExe & """ """ & scriptPath & """"
WshShell.CurrentDirectory = currentDir
WshShell.Run cmd, 0, False

If Err.Number <> 0 Then
    MsgBox "Error launching ScreenBot: " & Err.Description & vbCrLf & _
           "Please try running START_SCREENBOT.bat or run_debug.bat.", _
           vbCritical, "ScreenBot Error"
End If
