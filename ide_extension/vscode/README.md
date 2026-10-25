# ScreenBot Editor Bridge

Connects ScreenBot to VS Code, Antigravity, Cursor, and Windsurf. The extension
gives ScreenBot the exact active buffer (including unsaved edits), diagnostics,
hash-checked applies, and a review UI for held changes.

## Install

Build the package from this folder:

```bash
npm run package
```

Then install it from the generated `.vsix`:

```bash
code --install-extension screenbot-editor-bridge-1.0.0.vsix
```

Restart the editor. Start ScreenBot first; it writes
`%APPDATA%\ScreenBot\bridge.json` only while running. The status bar should show
`ScreenBot: bridge connected`.

## Commands

- `ScreenBot: Show Bridge Status`
- `ScreenBot: Review Pending Code Changes`

## Security

The bridge binds to `127.0.0.1` and authenticates with the `X-ScreenBot-Token`
header from the discovery file. Tokens in URLs are rejected.
