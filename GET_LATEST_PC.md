# Getting the Latest Code — Windows / PowerShell

Dummy-proof guide for syncing your local copy of RepoDoctor2 with GitHub.

---

## TL;DR (already cloned)

```powershell
cd ~\repodoctor2; .\start.ps1
```

`start.ps1` already runs `git pull origin main` for you, then launches the app. **This is the only command you need 99% of the time.**

If you get `Cannot find path 'C:\Users\<you>\repodoctor2'` → see **First Time on This Machine** below.

---

## Just Pull, Don't Launch

```powershell
cd ~\repodoctor2; git pull origin main
```

---

## First Time on This Machine

If `~\repodoctor2` doesn't exist yet, paste this **entire block** into PowerShell. It installs Python + Git (skipped if already present), clones the repo, and launches:

```powershell
Set-ExecutionPolicy RemoteSigned -Scope CurrentUser -Force
winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
winget install -e --id Git.Git --accept-source-agreements --accept-package-agreements
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
cd ~
git clone https://github.com/christreadaway/repodoctor2.git repodoctor2
cd ~\repodoctor2
.\start.ps1
```

Takes 2–5 minutes the first time. If GitHub asks for a password, use a Personal Access Token from https://github.com/settings/tokens (scope: `repo`).

After this, `cd ~\repodoctor2; .\start.ps1` is all you need.

---

## One-Paste Recovery (handles every case)

If you're not sure what state your machine is in — old `repodoctor22` folder lying around, partial clone, missing folder, anything — paste this. It's idempotent (safe to re-run) and lands you in a fresh, launched app:

```powershell
cd ~
if (Test-Path .\repodoctor22) { Remove-Item -Recurse -Force .\repodoctor22 }
if (-not (Test-Path .\repodoctor2)) { git clone https://github.com/christreadaway/repodoctor2.git repodoctor2 }
cd .\repodoctor2
.\start.ps1
```

If GitHub returns a 500 error during clone, that's a transient server hiccup — wait a minute and paste again.

---

## Common Problems

### "fatal: not a git repository"
You're in the wrong folder. Run:
```powershell
cd ~\repodoctor2
```
Then retry. If that folder doesn't exist, you haven't cloned yet — see `SETUP_PC.md`.

### "Your local changes would be overwritten by merge"
You edited a tracked file locally. Stash your changes, pull, then decide what to do:
```powershell
cd ~\repodoctor2
git stash
git pull origin main
git stash pop   # bring your changes back on top of the latest
```
If you want to **throw away** your local changes and take whatever is on GitHub:
```powershell
cd ~\repodoctor2
git reset --hard origin/main
git pull origin main
```
⚠️ `git reset --hard` is destructive — only run it if you're sure you don't want your local edits.

### GitHub asks for a username/password
Passwords don't work anymore. Use a Personal Access Token from
https://github.com/settings/tokens (scope: `repo`). Paste the token where it
asks for your password. Windows Credential Manager will remember it.

### "Updates were rejected because the tip of your current branch is behind"
You committed locally and someone else (or another machine) pushed since. Rebase your work on top of the latest:
```powershell
cd ~\repodoctor2
git pull --rebase origin main
```

### "Pulling without specifying how to reconcile divergent branches"
One-time config to default to fast-forward only (safest):
```powershell
git config --global pull.ff only
```

### "Could not resolve host: github.com"
Network issue. Check your internet, then retry. If you're on a VPN or behind a firewall, that can block git over HTTPS.

---

## Sanity Checks

**See what changed since you last pulled:**
```powershell
cd ~\repodoctor2; git log --oneline -10
```

**Confirm you're on `main` and clean:**
```powershell
cd ~\repodoctor2; git status
```
Expect: `On branch main` and `nothing to commit, working tree clean`.

**Confirm you're up to date with GitHub:**
```powershell
cd ~\repodoctor2; git fetch; git status
```
If you see `Your branch is up to date with 'origin/main'`, you're current.

---

## Notes

- `~\repodoctor2` resolves to `C:\Users\<you>\repodoctor2` in PowerShell.
- The local folder matches the GitHub repo name: `repodoctor2` (one 2).
- All work happens on the `main` branch. Don't create branches unless explicitly asked.
