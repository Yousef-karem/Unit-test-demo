import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";
import { findProjectRoot, resolveWorkspaceFolder } from "./projectRoot";
import { runGenerator } from "./runner";

let outputChannel: vscode.OutputChannel;

export function activate(context: vscode.ExtensionContext): void {
  outputChannel = vscode.window.createOutputChannel("Unit Test Generator");
  context.subscriptions.push(outputChannel);

  const disposable = vscode.commands.registerCommand(
    "unitTestGenerator.generateTests",
    (resourceUri?: vscode.Uri) => generateTests(resourceUri)
  );
  context.subscriptions.push(disposable);
}

export function deactivate(): void {
  // no-op
}

function resolveSelectedUri(resourceUri?: vscode.Uri): vscode.Uri | undefined {
  if (resourceUri) {
    return resourceUri;
  }
  return vscode.window.activeTextEditor?.document.uri;
}

async function generateTests(resourceUri?: vscode.Uri): Promise<void> {
  const selectedUri = resolveSelectedUri(resourceUri);
  if (!selectedUri || selectedUri.scheme !== "file") {
    vscode.window.showErrorMessage(
      "Unit Test Generator: select a Java file, package, or project folder first."
    );
    return;
  }

  const workspaceFolder = resolveWorkspaceFolder(selectedUri);
  if (!workspaceFolder) {
    vscode.window.showErrorMessage("Unit Test Generator: open a workspace folder first.");
    return;
  }

  const repoPath = findProjectRoot(selectedUri.fsPath, workspaceFolder.uri.fsPath);

  const config = vscode.workspace.getConfiguration("unitTestGenerator");
  const pythonPath = config.get<string>("pythonPath", "python");
  const rawScriptPath = config.get<string>("scriptPath", "llm_coverage_demo.py");
  const toolDirectory = config.get<string>("toolDirectory", "");
  const mode = config.get<string>("mode", "method");
  const analysisMode = config.get<string>("analysisMode", "ast");
  const classPromptSlices = config.get<number>("classPromptSlices", 3);
  const maxTargets = config.get<number>("maxTargets", 150);
  const maxRefinementIterations = config.get<number>("maxRefinementIterations", 5);
  const ollamaModel = config.get<string>("ollamaModel", "");
  const openaiModel = config.get<string>("openaiModel", "");

  // The Python tool lives outside the Java project being scanned, so its
  // script path must never be resolved against the target workspace folder.
  let scriptPath: string;
  if (path.isAbsolute(rawScriptPath)) {
    scriptPath = rawScriptPath;
  } else if (toolDirectory.trim().length > 0) {
    scriptPath = path.resolve(toolDirectory.trim(), rawScriptPath);
  } else {
    vscode.window.showErrorMessage(
      'Unit Test Generator: "Script Path" is relative and no "Tool Directory" is configured. ' +
        'Set "unitTestGenerator.scriptPath" to an absolute path, or set "unitTestGenerator.toolDirectory" ' +
        "to the folder containing llm_coverage_demo.py."
    );
    return;
  }

  if (!fs.existsSync(scriptPath)) {
    vscode.window.showErrorMessage(
      `Unit Test Generator: could not find the Python script at "${scriptPath}". ` +
        `Check the "Unit Test Generator: Script Path" setting.`
    );
    return;
  }

  const cwd = path.dirname(scriptPath);

  // Written into the Java project itself (not next to the tool), so multiple
  // projects don't share/overwrite one another's demo_out.
  const outputDir = path.join(repoPath, "demo_out");

  const args = [
    "--repo",
    repoPath,
    "--output-dir",
    outputDir,
    "--mode",
    mode,
    "--analysis-mode",
    analysisMode,
  ];

  if (mode === "class" && classPromptSlices > 1) {
    args.push("--class-prompt-slices", String(classPromptSlices));
  }
  args.push("--max-targets", String(maxTargets));
  args.push("--max-refinement-iterations", String(maxRefinementIterations));
  if (ollamaModel.trim().length > 0) {
    args.push("--ollama-model", ollamaModel.trim());
  }
  if (openaiModel.trim().length > 0) {
    args.push("--gpt-model", openaiModel.trim());
  }

  outputChannel.clear();
  outputChannel.show(true);

  try {
    const result = await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: "Unit Test Generator: generating tests",
        cancellable: true,
      },
      (progress, token) =>
        runGenerator({
          pythonPath,
          scriptPath,
          cwd,
          args,
          outputChannel,
          progress,
          token,
        })
    );

    if (result.exitCode === 0) {
      const action = await vscode.window.showInformationMessage(
        `Unit Test Generator: tests generated successfully for "${path.basename(repoPath)}".`,
        "Open demo_out",
        "Show Output"
      );
      if (action === "Show Output") {
        outputChannel.show(true);
      } else if (action === "Open demo_out") {
        const uri = vscode.Uri.file(outputDir);
        vscode.commands.executeCommand("revealFileInOS", uri);
      }
    } else {
      const tail = result.stderr.trim().split(/\r?\n/).slice(-5).join("\n") || result.stdout.trim().slice(-500);
      const action = await vscode.window.showErrorMessage(
        `Unit Test Generator: generation failed (exit code ${result.exitCode}). ${tail}`,
        "Show Output"
      );
      if (action === "Show Output") {
        outputChannel.show(true);
      }
    }
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    vscode.window.showErrorMessage(`Unit Test Generator: failed to run the tool: ${message}`);
  }
}
