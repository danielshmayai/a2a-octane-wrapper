Run a full pre-deploy check before pushing.

## Steps (run all of these):

1. **Unit tests** — All must pass
   ```
   pytest --tb=short -v
   ```

2. **Syntax check** — Verify all Python files parse correctly
   ```
   python -c "import py_compile; import glob; [py_compile.compile(f, doraise=True) for f in glob.glob('**/*.py', recursive=True)]"
   ```

3. **Import check** — Verify core modules import without errors
   ```
   python -c "import main, config, gemini_agent, mcp_client, tool_router, a2a_models"
   ```

4. **Git status** — Show what's changed
   ```
   git status
   git diff --stat
   ```

## Report format

Summarize results as a checklist:
- [ ] or [x] Tests — X passed, Y failed
- [ ] or [x] Syntax — all files OK / errors found
- [ ] or [x] Imports — all modules OK / errors found

End with a clear **GO** or **NO-GO** verdict.
