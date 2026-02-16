# Session End - Save Progress

Run this at the end of every session to save context for next time.

## Steps:

1. Update SESSION_NOTES.md with a new entry at the TOP of the file using this format:

```
## [Today's Date] [Current Time] - [Brief summary of session]

### What We Built
- [List features/changes with files modified]

### Current Status
- Working: [what's tested]
- Broken: [known issues]
- In Progress: [incomplete work]

### Branch Info
Branch: [current branch]
Ready to merge: [Yes/No]

### Next Steps
1. [Most important next action]
2. [Second priority]
3. [Third priority]

### Questions/Blockers
- [Anything unresolved]
```

2. Commit the session notes:
```bash
git add SESSION_NOTES.md
git commit -m "Update session notes: [brief description]"
```

3. Confirm to the user that session notes are saved.