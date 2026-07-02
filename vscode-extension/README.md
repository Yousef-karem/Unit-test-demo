# Unit Test Generator

Invokes the existing `llm_coverage_demo.py` Java unit test generation tool from within VS Code. This extension does not reimplement any of the tool's logic - it only detects the target project and shells out to the Python script.

## Usage

- Right-click a Java file, package, or project folder in the Explorer and choose **Unit Test Generator: Generate Tests**.
- Or run **Unit Test Generator: Generate Tests** from the Command Palette while a Java file is open.

The extension walks upward from the selected resource to find a `pom.xml`, `build.gradle`, or `build.gradle.kts`, and passes that directory as `--repo` to the tool.

The Python tool typically lives **outside** the Java project it scans, so its script path is never resolved against the target workspace. Configure either:
- `unitTestGenerator.scriptPath` as an **absolute path**, or
- `unitTestGenerator.scriptPath` as a relative filename plus `unitTestGenerator.toolDirectory` pointing at the folder containing `llm_coverage_demo.py`.

The tool process is launched with the tool's own directory as its working directory (needed for the script's own imports), but the extension always passes `--output-dir "<repo>/demo_out"` explicitly, so generated artifacts land inside the target Java project rather than next to the tool. This keeps output from different projects from mixing together.

## Settings

| Setting | Description | Default |
| --- | --- | --- |
| `unitTestGenerator.pythonPath` | Python executable | `python` |
| `unitTestGenerator.scriptPath` | Path to `llm_coverage_demo.py` (absolute, or relative to `toolDirectory`) | `llm_coverage_demo.py` |
| `unitTestGenerator.toolDirectory` | Directory containing the tool; also used as its working directory | *(empty)* |
| `unitTestGenerator.mode` | `class` or `method` | `method` |
| `unitTestGenerator.analysisMode` | `ast` or `source` | `ast` |
| `unitTestGenerator.classPromptSlices` | Prompt slices per class (class mode only) | `3` |
| `unitTestGenerator.maxTargets` | Max generation targets | `150` |
| `unitTestGenerator.maxRefinementIterations` | Max LLM repair attempts per test | `5` |
| `unitTestGenerator.ollamaModel` | Ollama model override | *(tool default)* |
| `unitTestGenerator.openaiModel` | OpenAI/GPT model override | *(tool default)* |

## Development

```bash
cd vscode-extension
npm install
npm run compile
```

Press F5 in VS Code to launch an Extension Development Host.
