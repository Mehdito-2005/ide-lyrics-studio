' Silent zero-flash System Tray launcher for IDE Lyrics Studio.
' Double-click this file (or place a shortcut to it in shell:startup) to launch
' directly into the Windows System Tray with no console window or taskbar icon.
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
appPy = fso.BuildPath(scriptDir, "app.py")
shell.CurrentDirectory = scriptDir
shell.Run "pythonw """ & appPy & """", 0, False
