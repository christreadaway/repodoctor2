# RepoDoctor2 — Windows PC Setup

Step-by-step instructions to pull down the latest code and run RepoDoctor2 on a Windows PC.

---

## One-Time Setup (only do this once per PC)

### 1. Install Python 3.10+
- Download from https://www.python.org/downloads/windows/
- During install, **check the box "Add Python to PATH"** (important!)
- Verify in PowerShell:
  ```powershell
  python --version
  ```

### 2. Install Git
- Download from https://git-scm.com/download/win
- Use default install options
- Verify in PowerShell:
  ```powershell
  git --version
  ```

### 3. Clone the Repo
Open **PowerShell** and run:
```powershell
cd ~
git clone https://github.com/christreadaway/repodoctor2.git repodoctor22
cd ~\repodoctor22
```

> Note: We name the local folder `repodoctor22` to match the Mac convention used in CLAUDE.md.

### 4. Create Virtual Environment + Install Dependencies
```powershell
cd ~\repodoctor22
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

You should see `(venv)` at the start of your PowerShell prompt when the venv is active.

---

## Pulling Down the Latest Code (every session)

Open **PowerShell** and run:
```powershell
cd ~\repodoctor22
git checkout main
git pull origin main
```

If dependencies changed (e.g., `requirements.txt` was updated):
```powershell
venv\Scripts\activate
pip install -r requirements.txt
```

---

## Running the App

```powershell
cd ~\repodoctor22
venv\Scripts\activate
python app.py
```

Then open your browser to:
```
http://127.0.0.1:5001
```

Press `Ctrl+C` in PowerShell to stop the server.

---

## Quick Launcher (Optional)

Create a file called `start.bat` in the project folder with this content so you can double-click to launch:

```bat
@echo off
cd /d "%~dp0"
if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
    call venv\Scripts\activate.bat
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate.bat
)
echo Starting RepoDoctor on http://127.0.0.1:5001
start "" http://127.0.0.1:5001
python app.py
pause
```

Then just double-click `start.bat` to launch.

---

## Troubleshooting

**`python` not recognized** — Reinstall Python and check "Add Python to PATH". Or try `py` instead of `python`.

**`git` not recognized** — Reinstall Git for Windows and restart PowerShell.

**Port 5001 already in use** — Find and kill the process:
```powershell
netstat -ano | findstr :5001
taskkill /PID <PID_FROM_ABOVE> /F
```

**PowerShell blocks `venv\Scripts\activate`** — Run once as admin:
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

**Credentials don't carry over from Mac** — Encrypted credentials are local only. On first run on the PC, re-enter your GitHub Personal Access Token and Anthropic API key in the app.
