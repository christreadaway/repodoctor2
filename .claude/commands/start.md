# Session Start - Branch & File Recovery

Run this at the beginning of every session to ensure continuity.

## Steps:

1. Fetch latest remote state:
```bash
git fetch origin
```

2. Check current branch:
```bash
git branch --show-current
```

3. Check for critical tracking files:
```bash
ls -la PROJECT_STATUS.md SESSION_NOTES.md 2>/dev/null || echo "WARNING: One or more tracking files missing"
```

4. If files are missing, recover from main:
```bash
git checkout origin/main -- PROJECT_STATUS.md 2>/dev/null; echo "Recovered PROJECT_STATUS.md" || echo "PROJECT_STATUS.md not on main"
git checkout origin/main -- SESSION_NOTES.md 2>/dev/null; echo "Recovered SESSION_NOTES.md" || echo "SESSION_NOTES.md not on main"
```

5. Show the last session entry from SESSION_NOTES.md so we know where we left off:
```bash
head -50 SESSION_NOTES.md 2>/dev/null || echo "No session notes yet"
```

6. Confirm ready state:
```bash
git status
```

Report what you found and what was recovered. Then ask what we're working on today.