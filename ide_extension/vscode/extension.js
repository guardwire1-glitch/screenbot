/*
 * ScreenBot Editor Bridge
 *
 * Runs inside VS Code-compatible editors (VS Code, Antigravity, Cursor,
 * Windsurf, ...). It long-polls ScreenBot's localhost bridge for jobs and
 * executes them with the real editor API:
 *
 *   context -> active buffer, dirty flag, cursor, diagnostics
 *   apply   -> hash-checked full-buffer replace, save, fresh diagnostics
 *   undo    -> restore the snapshot taken immediately before an apply
 *
 * The extension never decides what code is correct. ScreenBot does that and
 * holds changes that fail its gates; this process only supplies ground truth
 * and performs the edit the user's editor would have performed.
 */

const vscode = require("vscode");
const http = require("http");
const fs = require("fs");
const os = require("os");
const path = require("path");
const crypto = require("crypto");

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const sha256 = (text) => crypto.createHash("sha256").update(Buffer.from(text, "utf8")).digest("hex");
const samePath = (a, b) => path.resolve(a || "").toLowerCase() === path.resolve(b || "").toLowerCase();

let running = true;
let statusBar = null;
let lastConnectionOk = false;
const undoSnapshots = new Map();

function discoveryPath() {
  const base = process.env.APPDATA || path.join(os.homedir(), ".config");
  return path.join(base, "ScreenBot", "bridge.json");
}

function loadDiscovery() {
  const configured = vscode.workspace.getConfiguration("screenbot").get("bridgePort", 0);
  try {
    const data = JSON.parse(fs.readFileSync(discoveryPath(), "utf8"));
    if (!data || !data.token || !data.port) {
      return null;
    }
    return {
      port: configured ? Number(configured) : Number(data.port),
      token: String(data.token),
    };
  } catch (err) {
    return null;
  }
}

function request(method, route, body, timeoutMs) {
  const discovery = loadDiscovery();
  if (!discovery) {
    return Promise.resolve({ status: 0, data: null, error: "bridge discovery file not found" });
  }
  return new Promise((resolve) => {
    const payload = body === undefined || body === null ? null : JSON.stringify(body);
    const req = http.request(
      {
        host: "127.0.0.1",
        port: discovery.port,
        path: route,
        method,
        headers: {
          "X-ScreenBot-Token": discovery.token,
          "Content-Type": "application/json",
          "Content-Length": payload ? Buffer.byteLength(payload) : 0,
        },
      },
      (res) => {
        const chunks = [];
        res.on("data", (chunk) => chunks.push(chunk));
        res.on("end", () => {
          const raw = Buffer.concat(chunks).toString("utf8");
          let data = null;
          if (raw) {
            try {
              data = JSON.parse(raw);
            } catch (err) {
              data = { raw };
            }
          }
          resolve({ status: res.statusCode || 0, data });
        });
      }
    );
    req.on("error", (err) => resolve({ status: 0, data: null, error: String(err.message || err) }));
    req.setTimeout(Math.max(1000, timeoutMs || 8000), () => {
      req.destroy(new Error("bridge request timed out"));
    });
    if (payload) {
      req.write(payload);
    }
    req.end();
  });
}

function ideName() {
  const name = String((vscode.env && vscode.env.appName) || "").toLowerCase();
  return name.includes("antigravity") ? "antigravity" : "vscode";
}

function diagnosticsFor(doc) {
  const out = [];
  try {
    const severityNames = ["error", "warning", "info", "hint"];
    for (const diag of vscode.languages.getDiagnostics(doc.uri)) {
      out.push({
        file: doc.uri.fsPath,
        line: diag.range.start.line + 1,
        column: diag.range.start.character + 1,
        severity: severityNames[diag.severity] || "info",
        message: String(diag.message || "").slice(0, 500),
        source: String(diag.source || ""),
      });
      if (out.length >= 200) {
        break;
      }
    }
  } catch (err) {
    /* diagnostics are optional */
  }
  return out;
}

function findDocument(filePath) {
  if (!filePath) {
    return null;
  }
  return (
    vscode.workspace.textDocuments.find((doc) => !doc.isClosed && samePath(doc.uri.fsPath, filePath)) ||
    null
  );
}

async function documentForApply(filePath) {
  const existing = findDocument(filePath);
  if (existing) {
    return existing;
  }
  return vscode.workspace.openTextDocument(vscode.Uri.file(filePath));
}

function normalizeNewlines(doc, text) {
  if (doc.eol === vscode.EndOfLine.CRLF) {
    return String(text).replace(/\r?\n/g, "\r\n");
  }
  return String(text).replace(/\r\n/g, "\n");
}

async function replaceWholeBuffer(doc, text) {
  const fullRange = new vscode.Range(doc.positionAt(0), doc.positionAt(doc.getText().length));
  const edit = new vscode.WorkspaceEdit();
  edit.replace(doc.uri, fullRange, text);
  return vscode.workspace.applyEdit(edit);
}

async function handleContext(payload) {
  let doc = null;
  if (payload && payload.file_path) {
    // An explicit path is a contract. Never substitute another open tab: that
    // would make ScreenBot plan against the wrong file.
    doc = findDocument(payload.file_path);
    if (!doc) {
      return { ok: false, error: "requested file is not open in this editor" };
    }
  }
  if (!doc && vscode.window.activeTextEditor) {
    doc = vscode.window.activeTextEditor.document;
  }
  if (!doc || doc.uri.scheme !== "file") {
    return { ok: false, error: "no active file editor" };
  }
  const text = doc.getText();
  const editor = vscode.window.activeTextEditor;
  return {
    ok: true,
    file_path: doc.uri.fsPath,
    text,
    hash: sha256(text),
    dirty: doc.isDirty,
    language: doc.languageId,
    cursor_line: editor && samePath(editor.document.uri.fsPath, doc.uri.fsPath)
      ? editor.selection.active.line + 1
      : 0,
    diagnostics: diagnosticsFor(doc),
    can_apply: !doc.isClosed,
    detail: vscode.env.appName,
  };
}

async function handleApply(payload) {
  const filePath = payload && payload.file_path;
  const baseHash = payload && payload.base_hash;
  const newText = payload && payload.text;
  if (!filePath || typeof newText !== "string" || !baseHash) {
    return { ok: false, error: "apply job is missing file_path, base_hash, or text" };
  }
  if (path.extname(filePath).toLowerCase() === ".lock") {
    return { ok: false, error: "refusing to edit lock files" };
  }

  let doc;
  try {
    doc = await documentForApply(filePath);
  } catch (err) {
    return { ok: false, error: `cannot open target document: ${err.message || err}` };
  }
  if (!doc || doc.uri.scheme !== "file") {
    return { ok: false, error: "target is not a file document" };
  }

  const before = doc.getText();
  if (sha256(before) !== baseHash) {
    return { ok: false, conflict: true, error: "buffer changed since ScreenBot read it" };
  }

  try {
    const success = await replaceWholeBuffer(doc, normalizeNewlines(doc, newText));
    if (!success) {
      return { ok: false, error: "workspace.applyEdit was rejected" };
    }
    // The snapshot records both the previous buffer and the buffer this apply
    // produced. Undo may only restore when the file still equals appliedHash.
    undoSnapshots.set(doc.uri.fsPath, {
      text: before,
      appliedHash: sha256(doc.getText()),
      at: Date.now(),
    });
    if (undoSnapshots.size > 20) {
      const oldest = [...undoSnapshots.entries()].sort((a, b) => a[1].at - b[1].at)[0];
      if (oldest) {
        undoSnapshots.delete(oldest[0]);
      }
    }
    if (payload.save !== false) {
      await doc.save();
    }
  } catch (err) {
    return { ok: false, error: `apply failed: ${err.message || err}` };
  }

  const active = vscode.window.activeTextEditor;
  const formatOnApply = vscode.workspace.getConfiguration("screenbot").get("formatOnApply", false);
  if ((payload.format || formatOnApply) && active && samePath(active.document.uri.fsPath, doc.uri.fsPath)) {
    try {
      await vscode.commands.executeCommand("editor.action.formatDocument");
      await doc.save();
    } catch (err) {
      /* no formatter installed: leaving the code as applied is correct */
    }
  }

  await sleep(900);
  const finalText = doc.getText();
  return {
    ok: true,
    applied: true,
    text: finalText,
    hash: sha256(finalText),
    diagnostics: diagnosticsFor(doc),
    dirty: doc.isDirty,
  };
}

async function handleUndo(payload) {
  const filePath = payload && payload.file_path;
  const snapshot = undoSnapshots.get(filePath);
  if (!snapshot) {
    return { ok: false, error: "no snapshot for this file" };
  }
  const doc = await documentForApply(filePath);
  if (!snapshot.appliedHash || sha256(doc.getText()) !== snapshot.appliedHash) {
    return { ok: false, conflict: true, error: "buffer changed; refusing to overwrite undo snapshot" };
  }
  const success = await replaceWholeBuffer(doc, normalizeNewlines(doc, snapshot.text));
  if (!success) {
    return { ok: false, error: "undo edit rejected" };
  }
  await doc.save();
  undoSnapshots.delete(filePath);
  return { ok: true, applied: true, text: doc.getText(), hash: sha256(doc.getText()), diagnostics: diagnosticsFor(doc) };
}

async function handleJob(job) {
  switch (job.type) {
    case "context":
      return handleContext(job.payload || {});
    case "apply":
      return handleApply(job.payload || {});
    case "undo":
      return handleUndo(job.payload || {});
    default:
      return { ok: false, error: `unsupported job type: ${job.type}` };
  }
}

function setStatus(connected) {
  if (!statusBar) {
    return;
  }
  statusBar.text = connected ? "ScreenBot: bridge connected" : "ScreenBot: waiting for app";
  statusBar.tooltip = "Click to review pending ScreenBot changes";
  statusBar.color = connected ? undefined : new vscode.ThemeColor("statusBarItem.warningForeground");
}

async function pollLoop() {
  while (running) {
    const claim = await request(
      "POST",
      "/v1/jobs/claim",
      { ide: ideName(), wait: 3 },
      8000
    );

    if (!running) {
      break;
    }
    if (claim.status === 204) {
      if (!lastConnectionOk) {
        lastConnectionOk = true;
        setStatus(true);
      }
      await sleep(120);
      continue;
    }
    if (claim.status !== 200 || !claim.data) {
      if (lastConnectionOk) {
        lastConnectionOk = false;
        setStatus(false);
      }
      await sleep(1500);
      continue;
    }

    lastConnectionOk = true;
    setStatus(true);
    const job = claim.data;
    if (job.expires_at && Date.now() / 1000 > Number(job.expires_at)) {
      // The caller already gave up (and may have queued the change for review).
      // Executing it now would apply an edit ScreenBot no longer expects.
      await request(
        "POST",
        "/v1/jobs/result",
        { id: job.id, result: { ok: false, expired: true, error: "job expired before execution" }, error: "" },
        5000
      );
      continue;
    }
    let result;
    try {
      result = await handleJob(job);
    } catch (err) {
      result = { ok: false, error: String((err && err.message) || err) };
    }
    await request(
      "POST",
      "/v1/jobs/result",
      { id: job.id, result: result || { ok: false, error: "empty result" }, error: "" },
      5000
    );
  }
}

async function reviewPending() {
  const res = await request("GET", "/v1/pending", null, 4000);
  if (res.status === 0) {
    vscode.window.showWarningMessage(
      "ScreenBot bridge is not reachable. Start ScreenBot first."
    );
    return;
  }
  if (res.status !== 200 || !res.data || !res.data.items || !res.data.items.length) {
    vscode.window.showInformationMessage("ScreenBot has no pending changes.");
    return;
  }
  const items = res.data.items.map((item) => ({
    label: `${path.basename(item.file_path)} — ${item.reason}`,
    description: item.file_path,
    item,
  }));
  const picked = await vscode.window.showQuickPick(items, {
    placeHolder: "Pending ScreenBot changes — choose one to review",
  });
  if (!picked) {
    return;
  }
  const pending = picked.item;

  // The list is a summary on purpose; the proposed buffer is only sent when
  // the user opens a specific change.
  const detail = await request(
    "GET",
    `/v1/pending/${encodeURIComponent(pending.id)}`,
    null,
    4000
  );
  const proposal = detail.status === 200 && detail.data && detail.data.item;
  if (!proposal || typeof proposal.proposed_text !== "string") {
    vscode.window.showErrorMessage(
      "ScreenBot could not load the proposed change. It may already have been applied or dismissed."
    );
    return;
  }

  try {
    const current = await vscode.workspace.openTextDocument(vscode.Uri.file(pending.file_path));
    const proposed = await vscode.workspace.openTextDocument({
      content: proposal.proposed_text,
      language: current.languageId,
    });
    await vscode.commands.executeCommand(
      "vscode.diff",
      current.uri,
      proposed.uri,
      `ScreenBot: ${path.basename(pending.file_path)} (proposed)`
    );
  } catch (err) {
    vscode.window.showErrorMessage(`Could not open diff: ${(err && err.message) || err}`);
  }

  const choice = await vscode.window.showInformationMessage(
    `Apply this ScreenBot change to ${path.basename(pending.file_path)}?`,
    "Apply",
    "Dismiss"
  );
  if (!choice) {
    return;
  }
  const resolved = await request(
    "POST",
    "/v1/pending/resolve",
    { id: pending.id, accept: choice === "Apply" },
    10000
  );
  if (resolved.data && resolved.data.ok) {
    vscode.window.showInformationMessage(
      choice === "Apply" ? "ScreenBot change applied." : "ScreenBot change dismissed."
    );
  } else {
    vscode.window.showWarningMessage(
      `ScreenBot could not resolve the change: ${(resolved.data && resolved.data.error) || "unknown error"}`
    );
  }
}

async function showStatus() {
  const res = await request("GET", "/v1/health", null, 3000);
  if (res.status === 200 && res.data) {
    const hosts = (res.data.hosts || []).join(", ") || "none";
    vscode.window.showInformationMessage(
      `ScreenBot bridge is running (v${res.data.version}); connected hosts: ${hosts}. Pending changes: use the command palette.`
    );
  } else {
    vscode.window.showWarningMessage(
      "ScreenBot bridge is not reachable. Start ScreenBot first; this extension reads %APPDATA%/ScreenBot/bridge.json."
    );
  }
}

function activate(context) {
  running = true;
  statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 50);
  statusBar.command = "screenbot.reviewPending";
  setStatus(false);
  statusBar.show();

  context.subscriptions.push(
    statusBar,
    vscode.commands.registerCommand("screenbot.reviewPending", reviewPending),
    vscode.commands.registerCommand("screenbot.showStatus", showStatus)
  );

  pollLoop();
}

function deactivate() {
  running = false;
  if (statusBar) {
    statusBar.dispose();
    statusBar = null;
  }
}

module.exports = {
  activate,
  deactivate,
  handleJob,
  reviewPending,
  request,
};
