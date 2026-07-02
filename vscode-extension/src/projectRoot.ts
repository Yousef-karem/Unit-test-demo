import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";

const PROJECT_MARKERS = ["pom.xml", "build.gradle", "build.gradle.kts"];

function hasProjectMarker(dir: string): boolean {
  return PROJECT_MARKERS.some((marker) => fs.existsSync(path.join(dir, marker)));
}

/**
 * Walks upward from `startPath` looking for a Maven/Gradle project marker,
 * never climbing above `workspaceRoot`. Falls back to `workspaceRoot` if no
 * marker is found.
 */
export function findProjectRoot(startPath: string, workspaceRoot: string): string {
  let dir = fs.existsSync(startPath) && fs.statSync(startPath).isDirectory() ? startPath : path.dirname(startPath);

  const normalizedRoot = path.normalize(workspaceRoot);

  while (true) {
    if (hasProjectMarker(dir)) {
      return dir;
    }
    if (path.normalize(dir) === normalizedRoot) {
      break;
    }
    const parent = path.dirname(dir);
    if (parent === dir) {
      break;
    }
    dir = parent;
  }

  return workspaceRoot;
}

/**
 * Resolves the workspace folder that contains the given resource, falling
 * back to the first workspace folder if the resource isn't contained in any.
 */
export function resolveWorkspaceFolder(resourceUri: vscode.Uri | undefined): vscode.WorkspaceFolder | undefined {
  if (resourceUri) {
    const folder = vscode.workspace.getWorkspaceFolder(resourceUri);
    if (folder) {
      return folder;
    }
  }
  return vscode.workspace.workspaceFolders?.[0];
}
