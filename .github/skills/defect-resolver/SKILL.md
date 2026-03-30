---
name: defect-resolver
description: "Analyze developer-reported defects and find the fix. Use when: debugging bugs, understanding error reports, analyzing screenshots of issues, diagnosing root cause, resolving defects, fixing reported problems, triaging bug reports."
argument-hint: "Describe the defect, paste the bug report, or attach screenshots"
---

# Defect Resolver

Analyze defects reported by developers — from descriptions, error messages, and screenshots — then locate the root cause in the codebase and implement the fix.

## When to Use

- A developer describes a bug or unexpected behavior
- Screenshots of errors or broken UI are provided
- Error logs or stack traces need diagnosis
- You need to find and fix the root cause, not just the symptom

## Procedure

### Phase 1: Understand the Defect

1. **Parse the report** — Extract from the developer's description:
   - What was expected vs what actually happened
   - Steps to reproduce (if provided)
   - Error messages, status codes, or stack traces
   - Affected feature or area

2. **Analyze screenshots** — If images are attached:
   - Use the image viewing tool to examine each screenshot
   - Identify error messages, broken layouts, unexpected states
   - Note HTTP status codes, console errors, or UI anomalies visible in the image
   - Correlate visual issues with likely code paths

3. **Classify the defect** — Determine the category:
   - **Runtime error**: Exception, crash, unhandled rejection
   - **Logic bug**: Wrong result, missing data, incorrect state
   - **Integration failure**: API/MCP/auth/connection issue
   - **Configuration**: Missing env vars, wrong settings
   - **Regression**: Something that previously worked

### Phase 2: Check Previous Knowledge

1. **Search memory** — Check `/memories/` and `/memories/repo/` for:
   - Similar past issues and how they were resolved
   - Known gotchas or patterns for this area
   - Project-specific conventions that affect the fix

2. **Review project rules** — Check for relevant instruction files (`.github/instructions/`, `.claude/rules/`) that may contain domain knowledge about the affected area.

### Phase 3: Locate the Code

1. **Identify entry points** — From the defect signals, determine which module/file is involved. Use the search subagent for broad codebase exploration when the affected area isn't obvious.

2. **Trace the execution path** — Read the relevant code and follow the flow:
   - Entry point (endpoint, handler, event)
   - Business logic (transformation, validation, routing)
   - External calls (API, database, MCP, file I/O)
   - Response/output construction

3. **Narrow to the fault** — Find where actual behavior diverges from expected behavior. Look for:
   - Missing null/undefined checks
   - Wrong variable or parameter names
   - Race conditions or async ordering issues
   - Incorrect conditional logic
   - Mismatched types or schemas

### Phase 4: Root Cause Analysis

1. **Identify the root cause** — Not just the line that fails, but WHY it fails:
   - Is this a data issue, logic issue, or integration issue?
   - Was the original code wrong, or did a dependency change?
   - Are there related places with the same bug pattern?

2. **Assess blast radius** — Before fixing:
   - What else calls or depends on the affected code?
   - Could the fix break other functionality?
   - Are there tests covering this area?

### Phase 5: Implement the Fix

1. **Make the minimal correct change** — Fix the root cause, not the symptom. Avoid refactoring unrelated code.

2. **Handle edge cases** — If the root cause is a missing check, consider what other inputs could trigger the same class of bug.

3. **Run existing tests** — Execute the test suite to verify:
   - The fix doesn't break existing tests
   - If tests exist for the affected area, they now pass
   - If no tests cover the bug, mention this to the developer

### Phase 6: Report

Summarize concisely:
- **Root cause**: One sentence explaining WHY the bug occurred
- **Fix**: What was changed and why
- **Risk**: Any related areas to watch or test manually
- **Prevention**: If the defect reveals a recurring pattern, save it to memory for future reference
