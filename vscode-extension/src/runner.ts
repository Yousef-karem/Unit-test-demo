import { spawn } from "child_process";
import * as vscode from "vscode";

export interface RunResult {
  exitCode: number | null;
  stdout: string;
  stderr: string;
}

export interface RunOptions {
  pythonPath: string;
  scriptPath: string;
  cwd: string;
  args: string[];
  outputChannel: vscode.OutputChannel;
  progress: vscode.Progress<{ message?: string; increment?: number }>;
  token: vscode.CancellationToken;
}

/**
 * Runs the existing Python tool via child_process, streaming stdout/stderr to
 * the output channel and surfacing the latest line as progress. The tool's
 * own logic is never reimplemented here - this only shells out to it.
 */
export function runGenerator(options: RunOptions): Promise<RunResult> {
  const { pythonPath, scriptPath, cwd, args, outputChannel, progress, token } = options;

  return new Promise((resolve, reject) => {
    const fullArgs = [scriptPath, ...args];
    outputChannel.appendLine(`$ ${pythonPath} ${fullArgs.map((a) => (a.includes(" ") ? `"${a}"` : a)).join(" ")}`);
    outputChannel.appendLine(`(cwd: ${cwd})`);

    const child = spawn(pythonPath, fullArgs, { cwd });

    let stdout = "";
    let stderr = "";

    const reportLine = (chunk: string) => {
      const lines = chunk.split(/\r?\n/).filter((l) => l.trim().length > 0);
      if (lines.length > 0) {
        progress.report({ message: lines[lines.length - 1].slice(0, 200) });
      }
    };

    child.stdout.on("data", (data: Buffer) => {
      const text = data.toString();
      stdout += text;
      outputChannel.append(text);
      reportLine(text);
    });

    child.stderr.on("data", (data: Buffer) => {
      const text = data.toString();
      stderr += text;
      outputChannel.append(text);
      reportLine(text);
    });

    const cancellationListener = token.onCancellationRequested(() => {
      child.kill();
    });

    child.on("error", (err) => {
      cancellationListener.dispose();
      reject(err);
    });

    child.on("close", (code) => {
      cancellationListener.dispose();
      resolve({ exitCode: code, stdout, stderr });
    });
  });
}
