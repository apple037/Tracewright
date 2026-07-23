# Comment Legacy Environment Variables Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Disable every legacy `.env` assignment without deleting or changing its key/value text.

**Architecture:** Treat `.env` as an ignored, local compatibility record. Prefix each active assignment with `# ` and add one explanatory header; do not create replacement Agent Flow settings in this change.

**Tech Stack:** PowerShell, local `.env`

## Global Constraints

- Preserve all 30 original keys and values exactly.
- Do not print secret values.
- Do not commit `.env`.
- Leave zero active environment assignments.

---

### Task 1: Comment the legacy block

**Files:**
- Modify: `.env`

**Interfaces:**
- Consumes: the 30 currently active legacy assignments in `.env`
- Produces: a comment-only legacy configuration record

- [ ] **Step 1: Record structural RED evidence**

Count non-comment assignments without displaying their contents:

```powershell
$activeBefore = @(Get-Content .env | Where-Object {
  $_.Trim() -and -not $_.TrimStart().StartsWith('#') -and $_.Contains('=')
}).Count
if ($activeBefore -ne 30) { throw "Expected 30 active legacy assignments, found $activeBefore" }
```

Expected: the count is exactly `30`, showing the file is not yet comment-only.

- [ ] **Step 2: Apply the minimal edit**

Add this header:

```text
# Legacy runtime settings retained for reference.
# Agent Flow does not consume these variable names; all assignments are disabled.
```

For every active assignment, preserve its text and prefix it with `# `.

- [ ] **Step 3: Verify structure and preservation**

```powershell
$activeAfter = @(Get-Content .env | Where-Object {
  $_.Trim() -and -not $_.TrimStart().StartsWith('#') -and $_.Contains('=')
}).Count
$commentedAssignments = @(Get-Content .env | Where-Object {
  $_ -match '^\s*#\s+[A-Za-z_][A-Za-z0-9_]*='
}).Count
if ($activeAfter -ne 0) { throw "Active assignments remain: $activeAfter" }
if ($commentedAssignments -ne 30) { throw "Expected 30 retained assignments, found $commentedAssignments" }
```

Expected: zero active assignments and exactly 30 commented assignments.

- [ ] **Step 4: Verify repository safety**

```powershell
git check-ignore .env
git status --short
```

Expected: `.env` is ignored and does not appear as a tracked change.
