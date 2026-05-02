# RepoDoctor2 — Windows PowerShell Setup

Everything runs from PowerShell. No installers to click through, no boxes to remember to check.

Requires Windows 10 or 11 (which ship with `winget` built in).

---

## Step 1: Open PowerShell

Press the **Windows key**, type `powershell`, and click **Windows PowerShell**.

A blue window opens. All the commands below get pasted here.

---

## Step 2: Allow Scripts to Run (one-time)

Paste this into PowerShell and press **Enter**:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser -Force
```

(No output means it worked.)

---

## Step 3: One-Shot Install + Setup

Paste this **entire block** into PowerShell and press **Enter**. It installs Python, installs Git, clones the repo, creates the virtual environment, and installs all dependencies — in one go.

```powershell
# Install Python and Git via winget
winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
winget install -e --id Git.Git --accept-source-agreements --accept-package-agreements

# Refresh PATH so python and git work without restarting PowerShell
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

# Clone the repo into ~\repodoctor22
cd ~
git clone https://github.com/christreadaway/repodoctor2.git repodoctor22
cd ~\repodoctor22

# Create virtual environment and install dependencies
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt

Write-Host "`n=== Setup complete. Run .\start.ps1 to launch RepoDoctor. ===" -ForegroundColor Green
```

This takes 2–5 minutes. When you see the green "Setup complete" message, you're done with one-time setup.

If `winget` says Python or Git is already installed, that's fine — keep going.

---

## Step 4: Start the App

Every time you want to run RepoDoctor, paste this into PowerShell:

```powershell
cd ~\repodoctor22
.\start.ps1
```

That script (already in the repo) pulls the latest code, activates the virtual environment, opens your browser to http://127.0.0.1:5001, and starts the server.

To stop the app, click the PowerShell window and press **Ctrl + C**.

---

## What `start.ps1` Does

It runs these commands so you don't have to remember them:

1. `git pull origin main` — grabs the latest code
2. `.\venv\Scripts\Activate.ps1` — turns on the virtual environment
3. `pip install -r requirements.txt --quiet` — installs any new dependencies
4. Opens http://127.0.0.1:5001 in your default browser
5. `python app.py` — starts the Flask server

---

## Troubleshooting

**`winget` not recognized** — You're on an older Windows. Update Windows, or install from https://aka.ms/getwinget.

**`python` or `git` not recognized after Step 3** — Close PowerShell, reopen it, and try again. The `$env:Path` refresh works most of the time, but a full restart of PowerShell always works.

**`Activate.ps1 cannot be loaded because running scripts is disabled`** — Step 2 didn't take. Run it again, then close and reopen PowerShell.

**Port 5001 already in use** — Another instance is running. Kill it:
```powershell
Get-NetTCPConnection -LocalPort 5001 | Select-Object -ExpandProperty OwningProcess | ForEach-Object { Stop-Process -Id $_ -Force }
```

**GitHub asks for a password when cloning** — Use a Personal Access Token (not your real password). Generate one at https://github.com/settings/tokens — give it `repo` scope.

**Anything else** — Copy the red error text, paste it to Claude Code, and we'll fix it.
