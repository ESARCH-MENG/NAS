# Repository Instructions

- When changing `nas_tool.py` or application dependencies, inspect `packaging/create_dmg.py` and explicitly assess whether the PyInstaller inputs, bundled dependencies/resources, `Info.plist` privacy declarations, or signing steps must change. Keep the DMG build aligned with the runnable application; if no packaging change is needed, state why.
- When changing macOS bundle metadata after PyInstaller runs, re-sign and verify the final `.app` before staging it into the DMG.
- For packaging behavior changes, add or update a focused test for the generated app metadata or build step, then run the focused test and the project test suite when available.