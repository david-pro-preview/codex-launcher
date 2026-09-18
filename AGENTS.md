# Working on Codex Launcher

Read README.md before building or installing. This repository contains a SwiftUI/AppKit macOS menu-bar app and a Python standard-library helper.

- `Launcher.swift`: UI, helper process management, popover dismissal, usage rings.
- `account_manager.py`: private account registry, transactional auth switching, usage fetching.
- `build.sh`: Apple Silicon/macOS 14 build; writes only `.build/` and `dist/` under the repo.
- `tests/`: isolated Python tests with fake credentials and mocked side effects.

## Build and validate

On Apple Silicon macOS with Command Line Tools and Homebrew Python:

```sh
/opt/homebrew/bin/python3 -m unittest discover -s tests -p 'test_*.py'
PYTHON_BIN=/opt/homebrew/bin/python3 bash build.sh
"dist/Codex Launcher.app/Contents/MacOS/Codex Launcher" --self-test
```

Read `APP_PATH` before installation: the default is `/Applications/ChatGPT.app`; some installations use `/Applications/Codex.app`. Verify the selected bundle contains `Contents/Resources/codex`. Adapt the path to the user's actual installation without renaming another app.

The helper uses `~/.codex/auth.json` and saves private account profiles outside the repository. Never print token contents, include credentials in command arguments, commit account data, or attach real account screenshots. Tests and `--render-preview` use fake data. Do not invoke `switch` or `add` merely to smoke-test an installation: those actions terminate the desktop and app-server processes, including the current Codex session. Normal build/test operations need no account login.

Preserve the 370pt menu width, existing account transactions, and shared history behavior. Do not restore custom pointer tracking; users prefer standard cursors. Current-account cards have no hover state; other cards and Add Account darken their existing background color on hover. Outside clicks dismiss the popover.
