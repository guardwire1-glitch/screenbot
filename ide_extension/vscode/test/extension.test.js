/*
 * Tests for the ScreenBot editor bridge extension.
 *
 * The vscode API is mocked through Module._load, and a tiny real HTTP server
 * stands in for the Python bridge. That keeps the tests dependency-free while
 * exercising the actual request/discovery code paths.
 */

const { test, beforeEach, afterEach, after } = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const crypto = require("node:crypto");
const Module = require("node:module");

const sha256 = (text) => crypto.createHash("sha256").update(Buffer.from(text, "utf8")).digest("hex");

const state = {
  documents: [],
  activeTextEditor: null,
  diagnostics: {},
  edits: [],
  openedProposals: [],
  diffCalls: [],
  messages: [],
  quickPick: null,
  executeCommandCalls: [],
};

function fileUri(fsPath) {
  return {
    fsPath,
    scheme: "file",
    toString: () => `file:///${String(fsPath).replace(/\\/g, "/")}`,
  };
}

function makeDocument(fsPath, text, { dirty = false } = {}) {
  let current = text;
  return {
    uri: fileUri(fsPath),
    languageId: "csharp",
    eol: 1,
    isDirty: dirty,
    isClosed: false,
    getText: () => current,
    positionAt: (offset) => ({ line: 0, character: offset }),
    save: async () => true,
    replaceText: (next) => {
      current = next;
    },
  };
}

class Range {
  constructor(start, end) {
    this.start = start;
    this.end = end;
  }
}

class WorkspaceEdit {
  constructor() {
    this.entries = [];
  }

  replace(uri, range, text) {
    this.entries.push({ uri, range, text });
  }
}

class ThemeColor {
  constructor(id) {
    this.id = id;
  }
}

const vscodeMock = {
  EndOfLine: { LF: 1, CRLF: 2 },
  StatusBarAlignment: { Right: 2 },
  Range,
  WorkspaceEdit,
  ThemeColor,
  Uri: {
    file: fileUri,
  },
  env: { appName: "Visual Studio Code" },
  languages: {
    getDiagnostics: (uri) => state.diagnostics[uri.fsPath] || [],
  },
  commands: {
    registerCommand: () => ({ dispose() {} }),
    executeCommand: async (...args) => {
      state.executeCommandCalls.push(args);
      if (args[0] === "vscode.diff") {
        state.diffCalls.push(args);
      }
      return undefined;
    },
  },
  window: {
    activeTextEditor: null,
    createStatusBarItem: () => ({
      show() {},
      hide() {},
      dispose() {},
    }),
    showInformationMessage: async (...args) => {
      state.messages.push(args);
      return args.includes("Apply") ? "Apply" : undefined;
    },
    showWarningMessage: async (...args) => {
      state.messages.push(args);
      return undefined;
    },
    showErrorMessage: async (...args) => {
      state.messages.push(args);
      return undefined;
    },
    showQuickPick: async (items) => {
      state.quickPick = items;
      return items[0] || undefined;
    },
  },
  workspace: {
    textDocuments: [],
    getConfiguration: () => ({
      get: (_key, fallback) => fallback,
    }),
    openTextDocument: async (arg) => {
      if (arg && arg.scheme === "file") {
        const found = state.documents.find(
          (doc) => path.resolve(doc.uri.fsPath).toLowerCase() === path.resolve(arg.fsPath).toLowerCase()
        );
        if (!found) {
          throw new Error(`cannot open ${arg.fsPath}`);
        }
        return found;
      }
      state.openedProposals.push(arg);
      return {
        uri: { fsPath: "untitled:ScreenBot", scheme: "untitled", toString: () => "untitled:ScreenBot" },
        languageId: (arg && arg.language) || "plaintext",
        getText: () => (arg && arg.content) || "",
      };
    },
    applyEdit: async (edit) => {
      state.edits.push(edit);
      for (const entry of edit.entries) {
        const doc = state.documents.find(
          (candidate) => candidate.uri.fsPath === entry.uri.fsPath
        );
        if (doc) {
          doc.replaceText(entry.text);
        }
      }
      return true;
    },
  },
};

// The extension requires "vscode" at module load time. Inject the mock before
// requiring it, exactly the way VS Code would supply the real module.
const originalLoad = Module._load;
Module._load = function load(request, parent, isMain) {
  if (request === "vscode") {
    return vscodeMock;
  }
  return originalLoad.call(this, request, parent, isMain);
};

const extension = require("../extension.js");

const originalAppData = process.env.APPDATA;
let tempRoot = null;
let activeServer = null;

function resetState() {
  state.documents = [];
  state.activeTextEditor = null;
  state.diagnostics = {};
  state.edits = [];
  state.openedProposals = [];
  state.diffCalls = [];
  state.messages = [];
  state.quickPick = null;
  state.executeCommandCalls = [];
  vscodeMock.workspace.textDocuments = state.documents;
  vscodeMock.window.activeTextEditor = state.activeTextEditor;
}

function writeDiscovery(port, token = "test-token") {
  const dir = path.join(tempRoot, "ScreenBot");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(
    path.join(dir, "bridge.json"),
    JSON.stringify({ version: 1, port, token, pid: process.pid }),
    "utf8"
  );
}

function startBridge(routes) {
  return new Promise((resolve) => {
    const requests = [];
    const server = http.createServer((req, res) => {
      const chunks = [];
      req.on("data", (chunk) => chunks.push(chunk));
      req.on("end", () => {
        const url = new URL(req.url, "http://127.0.0.1");
        const raw = Buffer.concat(chunks).toString("utf8");
        const entry = {
          method: req.method,
          path: url.pathname,
          headers: req.headers,
          body: raw ? JSON.parse(raw) : null,
        };
        requests.push(entry);
        const route = routes[`${req.method} ${url.pathname}`];
        const response = route ? route(entry) : { status: 404, body: { error: "not found" } };
        res.writeHead(response.status || 200, { "Content-Type": "application/json" });
        res.end(response.body === undefined ? "" : JSON.stringify(response.body));
      });
    });
    server.listen(0, "127.0.0.1", () => {
      activeServer = server;
      resolve({ server, port: server.address().port, requests });
    });
  });
}

beforeEach(() => {
  resetState();
  tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "screenbot-extension-"));
  process.env.APPDATA = tempRoot;
});

afterEach(async () => {
  if (activeServer) {
    await new Promise((resolve) => activeServer.close(resolve));
    activeServer = null;
  }
});

after(() => {
  Module._load = originalLoad;
  if (originalAppData === undefined) {
    delete process.env.APPDATA;
  } else {
    process.env.APPDATA = originalAppData;
  }
});

test("context never substitutes a different open tab for an explicit path", async () => {
  const other = makeDocument(path.join(tempRoot, "Other.cs"), "class Other {}\n");
  state.documents.push(other);
  vscodeMock.window.activeTextEditor = {
    document: other,
    selection: { active: { line: 3 } },
  };

  const missing = await extension.handleJob({
    type: "context",
    payload: { file_path: path.join(tempRoot, "Demo.cs") },
  });
  assert.equal(missing.ok, false);
  assert.match(missing.error, /not open/);

  const demo = makeDocument(path.join(tempRoot, "Demo.cs"), "class Demo {}\n");
  state.documents.push(demo);
  vscodeMock.window.activeTextEditor = {
    document: demo,
    selection: { active: { line: 1 } },
  };

  const context = await extension.handleJob({
    type: "context",
    payload: { file_path: demo.uri.fsPath },
  });
  assert.equal(context.ok, true);
  assert.equal(context.hash, sha256("class Demo {}\n"));
  assert.equal(context.cursor_line, 2);
  assert.equal(context.can_apply, true);
});

test("apply refuses a buffer that changed since ScreenBot read it", async () => {
  const demo = makeDocument(path.join(tempRoot, "Demo.cs"), "class Demo { int y = 2; }\n");
  state.documents.push(demo);

  const result = await extension.handleJob({
    type: "apply",
    payload: {
      file_path: demo.uri.fsPath,
      base_hash: sha256("class Demo {}\n"),
      text: "class Demo { int x = 1; }\n",
    },
  });

  assert.equal(result.ok, false);
  assert.equal(result.conflict, true);
  assert.equal(state.edits.length, 0);
  assert.equal(demo.getText(), "class Demo { int y = 2; }\n");
});

test("apply then undo restores the exact previous buffer", async () => {
  const original = "class Demo {}\n";
  const applied = "class Demo\n{\n    int x = 1;\n}\n";
  const demo = makeDocument(path.join(tempRoot, "Demo.cs"), original);
  state.documents.push(demo);

  const applyResult = await extension.handleJob({
    type: "apply",
    payload: {
      file_path: demo.uri.fsPath,
      base_hash: sha256(original),
      text: applied,
    },
  });
  assert.equal(applyResult.ok, true);
  assert.equal(applyResult.applied, true);
  assert.equal(demo.getText(), applied);
  assert.equal(applyResult.hash, sha256(applied));

  const undoResult = await extension.handleJob({
    type: "undo",
    payload: { file_path: demo.uri.fsPath },
  });
  assert.equal(undoResult.ok, true);
  assert.equal(demo.getText(), original);
  assert.equal(undoResult.hash, sha256(original));

  const secondUndo = await extension.handleJob({
    type: "undo",
    payload: { file_path: demo.uri.fsPath },
  });
  assert.equal(secondUndo.ok, false);
  assert.match(secondUndo.error, /no snapshot/);
});

test("undo refuses to overwrite edits made after the apply", async () => {
  const original = "class Demo {}\n";
  const applied = "class Demo\n{\n    int x = 1;\n}\n";
  const demo = makeDocument(path.join(tempRoot, "Demo.cs"), original);
  state.documents.push(demo);

  const applyResult = await extension.handleJob({
    type: "apply",
    payload: {
      file_path: demo.uri.fsPath,
      base_hash: sha256(original),
      text: applied,
    },
  });
  assert.equal(applyResult.ok, true);

  const userEdited = applied + "// my own note\n";
  demo.replaceText(userEdited);

  const undoResult = await extension.handleJob({
    type: "undo",
    payload: { file_path: demo.uri.fsPath },
  });

  assert.equal(undoResult.ok, false);
  assert.equal(undoResult.conflict, true);
  assert.equal(demo.getText(), userEdited);
});

test("review loads the full proposal from the detail endpoint and resolves it", async () => {
  const filePath = path.join(tempRoot, "Demo.cs");
  const currentText = "class Demo\n{\n    int x = 1;\n}\n";
  const proposedText = "class Demo\n{\n    int x = 2;\n}\n";
  const demo = makeDocument(filePath, currentText);
  state.documents.push(demo);

  const bridge = await startBridge({
    "GET /v1/pending": () => ({
      body: {
        items: [
          {
            id: "pc_1",
            file_path: filePath,
            reason: "lecturer 1 edited block(s)",
            source: "disk",
            added_lines: 4,
            diagnostics: [],
          },
        ],
      },
    }),
    "GET /v1/pending/pc_1": () => ({
      body: {
        item: {
          id: "pc_1",
          file_path: filePath,
          base_hash: sha256(currentText),
          proposed_text: proposedText,
          reason: "lecturer 1 edited block(s)",
          source: "disk",
          diagnostics: [],
        },
      },
    }),
    "POST /v1/pending/resolve": () => ({ body: { ok: true, applied: true } }),
  });
  writeDiscovery(bridge.port);

  await extension.reviewPending();

  const detailRequest = bridge.requests.find(
    (entry) => entry.method === "GET" && entry.path === "/v1/pending/pc_1"
  );
  assert.ok(detailRequest, "the extension must fetch the pending detail");
  assert.equal(detailRequest.headers["x-screenbot-token"], "test-token");

  assert.equal(state.openedProposals.length, 1);
  assert.equal(state.openedProposals[0].content, proposedText);
  assert.equal(state.diffCalls.length, 1);
  assert.match(state.diffCalls[0][3], /Demo\.cs/);

  const resolve = bridge.requests.find(
    (entry) => entry.method === "POST" && entry.path === "/v1/pending/resolve"
  );
  assert.deepEqual(resolve.body, { id: "pc_1", accept: true });

  assert.ok(
    state.messages.some((args) => args[0] === "ScreenBot change applied."),
    "success must be shown to the user"
  );
});

test("review distinguishes an unreachable bridge from no pending changes", async () => {
  await extension.reviewPending();

  assert.ok(
    state.messages.some((args) => /not reachable/.test(String(args[0]))),
    "an unreachable bridge must warn instead of claiming there is nothing to review"
  );
  assert.ok(
    !state.messages.some((args) => /no pending changes/.test(String(args[0]))),
    "an unreachable bridge is not the same as an empty review list"
  );
});
