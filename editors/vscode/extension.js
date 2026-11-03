// Starts the Ella language server (`ella-lsp`, see src/ella/lsp.py) for .ella files.
// Highlighting doesn't need it: that's the grammar in syntaxes/.

const vscode = require("vscode");
const { LanguageClient } = require("vscode-languageclient/node");

let client;

async function activate(context) {
  context.subscriptions.push(
    vscode.commands.registerCommand("ella.restartServer", async () => {
      await stop();
      await start();
    }),
  );
  await start();
}

async function start() {
  const [command, ...args] = vscode.workspace.getConfiguration("ella").get("server.command") ?? [];
  if (!command) {
    return;
  }
  const cwd = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  client = new LanguageClient(
    "ella",
    "Ella",
    { command, args, options: { cwd } },
    { documentSelector: [{ language: "ella" }] },
  );
  try {
    await client.start();
  } catch (error) {
    client = undefined;
    vscode.window.showWarningMessage(
      `The Ella language server didn't start (${[command, ...args].join(" ")}): ${error.message}. ` +
        "Set ella.server.command to change how it's started.",
    );
  }
}

async function stop() {
  if (client) {
    await client.stop();
    client = undefined;
  }
}

module.exports = { activate, deactivate: stop };
