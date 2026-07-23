# Run RepoDoctor Locally — One Paste

Copy the whole block for your computer, paste it, press Enter. Works whether this is a brand-new computer or you've run it here a hundred times before — same command every time. Your browser opens to **http://127.0.0.1:5001**.

Press **Ctrl + C** in the terminal window to stop the server.

First run takes 2-5 minutes (installing Python/Git and dependencies). Every run after that takes a few seconds.

---

## Mac

Open **Terminal** (Cmd+Space → type `terminal` → Enter), paste this, press Enter:

```bash
xcode-select --install 2>/dev/null
cd ~
if [ ! -d "RepoDoctor2" ]; then git clone https://github.com/christreadaway/repodoctor2.git RepoDoctor2; fi
cd ~/RepoDoctor2
./start.command
```

If macOS pops up a dialog to install Xcode Command Line Tools, click **Install**, wait for it to finish, then paste the block again.

---

## Windows

Press the **Windows key**, type `powershell`, press Enter, paste this, press Enter:

```powershell
Set-ExecutionPolicy RemoteSigned -Scope CurrentUser -Force
winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
winget install -e --id Git.Git --accept-source-agreements --accept-package-agreements
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
cd ~
if (-not (Test-Path .\repodoctor2)) { git clone https://github.com/christreadaway/repodoctor2.git repodoctor2 }
cd .\repodoctor2
.\start.ps1
```

If `winget` reports Python or Git already installed, that's fine — keep going.

---

## If Something Goes Wrong

**GitHub asks for a password** → Passwords don't work. Use a Personal Access Token from https://github.com/settings/tokens (scope: `repo`), and paste the token where it asks for your password.

**"Your local changes would be overwritten by merge"** → You edited a tracked file locally. To throw away local edits and take exactly what's on GitHub:

- Mac:
  ```bash
  cd ~/RepoDoctor2 && git reset --hard origin/main && ./start.command
  ```
- Windows:
  ```powershell
  cd ~\repodoctor2; git reset --hard origin/main; .\start.ps1
  ```
  ⚠️ `git reset --hard` deletes your local edits. Only run it if you don't want them.

**Port 5001 already in use** → Both launch scripts free the port automatically. If one doesn't:

- Mac: `lsof -ti:5001 | xargs kill -9`
- Windows: `Get-NetTCPConnection -LocalPort 5001 | Select-Object -ExpandProperty OwningProcess | ForEach-Object { Stop-Process -Id $_ -Force }`

**`./start.command` permission denied (Mac)** → Run `chmod +x start.command` once, then retry.

**`winget` not recognized (Windows)** → Update Windows, or install from https://aka.ms/getwinget.

---

*Notes: The Mac folder is `~/RepoDoctor2`; the Windows folder is `~\repodoctor2`. Both point at the same GitHub repo (`repodoctor2`) and always run off the `main` branch. For deeper setup and sync help, see `SETUP_MAC.md`, `SETUP_PC.md`, and `GET_LATEST_PC.md`.*
