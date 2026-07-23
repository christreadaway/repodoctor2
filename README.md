# RepoDoctor2

A Flask web app that gives you a single-screen view of every GitHub repo you own — branch counts, required-file status, AI-generated project summaries, and a Claude conversation-to-repo mapping. Built for people running multiple projects through Claude Code.

## Run It Locally

Same command whether this is a brand-new computer or you've run it here a hundred times before.

### Windows

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

### Mac

Open **Terminal** (Cmd+Space → type `terminal` → Enter), paste this, press Enter:

```bash
xcode-select --install 2>/dev/null
cd ~
if [ ! -d "RepoDoctor2" ]; then git clone https://github.com/christreadaway/repodoctor2.git RepoDoctor2; fi
cd ~/RepoDoctor2
./start.command
```

Both open your browser to **http://127.0.0.1:5001** when ready. First run takes 2-5 minutes (installing Python/Git and dependencies); every run after that takes a few seconds. Press **Ctrl+C** in the terminal to stop the server.

**Something went wrong?** See [RUN_LOCAL.md](RUN_LOCAL.md) for troubleshooting, or the deeper setup guides: [SETUP_PC.md](SETUP_PC.md), [SETUP_MAC.md](SETUP_MAC.md), [GET_LATEST_PC.md](GET_LATEST_PC.md).

## Docs

- [PRODUCT_SPEC.md](PRODUCT_SPEC.md) — what this is and why
- [PROJECT_STATUS.md](PROJECT_STATUS.md) — current state, what's built
- [PROJECT_RULES.md](PROJECT_RULES.md) — engineering conventions
