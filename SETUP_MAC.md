# RepoDoctor2 — Mac Setup

One paste. Works whether this is a brand-new computer or you've run it here before — clones if needed, and launches.

---

## Setup + Launch

1. Open **Terminal** (Cmd+Space → type `terminal` → Enter).
2. Paste this **entire block** and press **Enter**:

```bash
xcode-select --install 2>/dev/null
cd ~
if [ ! -d "RepoDoctor2" ]; then git clone https://github.com/christreadaway/repodoctor2.git RepoDoctor2; fi
cd ~/RepoDoctor2
./start.command
```

If macOS pops up a dialog asking to install Xcode Command Line Tools, click **Install** and wait for it to finish, then paste the block again.

`start.command` creates the Python virtual environment, installs dependencies, and opens http://127.0.0.1:5001 in your browser. First run takes a few minutes; every run after that takes a few seconds.

---

## Every Future Session

Either:

- **Double-click** `start.command` in Finder (inside `~/RepoDoctor2`), **or**
- Paste the exact same block from **Setup + Launch** above — it's safe to run again any time.

Press **Ctrl + C** in Terminal to stop the server.

---

## Troubleshooting

**`git` not recognized** → Run `xcode-select --install` and finish the installer.

**`python3 not found`** → Install via Homebrew:
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install python@3.12
```

**Port 5001 already in use** → `start.command` handles this automatically. If it doesn't:
```bash
lsof -ti:5001 | xargs kill -9
```

**`./start.command` permission denied** → Run `chmod +x start.command` once, then retry.

**GitHub asks for a password** → Use a Personal Access Token from https://github.com/settings/tokens (scope: `repo`).
