# RepoDoctor2

A Flask web app that gives you a single-screen view of every GitHub repo you can reach — personal, private, public, and the ones inside your organizations — with branch counts, required-file status, AI-generated project summaries, and a Claude conversation-to-repo mapping. Built for people running multiple projects through Claude Code.

Its **Portfolio** screen groups every project by theme tag (tags you can add, rename, and have AI suggest) with a one-line summary each, and maps the shared infrastructure areas (messaging, security, data viz, courseware, video, and more) to the repos that hold them.

It also includes a **Repo Selector** (tick repos, get three-sentence summaries, copy a clean sheet), and three screens for switching between projects: **In Flight** (every unmerged branch and open PR across all repos, with a ready-to-merge / finish / stale call on each, and one-click deletion of branches that are already merged), **Resume** (a one-page, one-paste context packet for any project, read live from GitHub with no AI cost), and **Components** (which repo holds which reusable infrastructure, so it can be extracted into shared components — see `COMPONENTIZATION_PLAN.md`).

Everything the app does is logged to `data/logs/app.log` (plus one JSON-lines event log per feature). Settings → Debug log shows the tail with a COPY FOR CLAUDE CODE button.

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
- [COMPONENTIZATION_PLAN.md](COMPONENTIZATION_PLAN.md) — how shared infrastructure gets extracted into reusable components
