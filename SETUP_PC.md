# RepoDoctor2 — Windows Setup

One paste. Everything installs, clones, and launches.

Requires Windows 10 or 11 (which ship with `winget`).

---

## First-Time Setup + Launch

1. Press **Windows key**, type `powershell`, press **Enter**.
2. Paste this **entire block** and press **Enter**:

```powershell
Set-ExecutionPolicy RemoteSigned -Scope CurrentUser -Force
winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
winget install -e --id Git.Git --accept-source-agreements --accept-package-agreements
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
cd ~
git clone https://github.com/christreadaway/repodoctor2.git repodoctor22
cd ~\repodoctor22
.\start.ps1
```

Takes 2–5 minutes. Your browser opens to http://127.0.0.1:5001 when it's ready.

If `winget` reports Python or Git already installed, that's fine — keep going.

---

## Every Future Session

```powershell
cd ~\repodoctor22; .\start.ps1
```

`start.ps1` pulls latest code, activates the virtual environment, refreshes dependencies, and starts the app.

Press **Ctrl + C** in PowerShell to stop the server.

---

## Desktop Shortcut (one-click launch)

Run this **once** in PowerShell. It creates a "RepoDoctor" icon on your desktop. Double-click it any time to launch the app:

```powershell
$DesktopPath = [Environment]::GetFolderPath("Desktop")
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("$DesktopPath\RepoDoctor.lnk")
$Shortcut.TargetPath = "powershell.exe"
$Shortcut.Arguments = "-NoExit -ExecutionPolicy Bypass -File `"$env:USERPROFILE\repodoctor22\start.ps1`""
$Shortcut.WorkingDirectory = "$env:USERPROFILE\repodoctor22"
$Shortcut.IconLocation = "powershell.exe,0"
$Shortcut.Save()
Write-Host "Shortcut created at: $DesktopPath\RepoDoctor.lnk" -ForegroundColor Green
```

> Uses `[Environment]::GetFolderPath("Desktop")` so it works whether your Desktop is the standard location or redirected to OneDrive.

When you double-click **RepoDoctor** on your desktop, a PowerShell window opens, the app starts, and your browser opens to http://127.0.0.1:5001. Closing the PowerShell window stops the server.

---

## Troubleshooting

**`winget` not recognized** → Update Windows, or install from https://aka.ms/getwinget.

**`python` or `git` not recognized after the paste** → Close PowerShell, reopen it, then run `cd ~\repodoctor22; .\start.ps1`.

**`Activate.ps1 cannot be loaded`** → Run `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser -Force`, then retry.

**Port 5001 already in use** → `start.ps1` handles this automatically. If it doesn't, run:
```powershell
Get-NetTCPConnection -LocalPort 5001 | Select-Object -ExpandProperty OwningProcess | ForEach-Object { Stop-Process -Id $_ -Force }
```

**GitHub asks for a password** → Use a Personal Access Token from https://github.com/settings/tokens (scope: `repo`).
