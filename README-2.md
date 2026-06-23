# Resume From Saved Prompts Workflow

This is the updated workflow where GPT/prompt generation can stop after `targets.json`, then saved prompt JSON files are passed to Ollama later to generate tests.

## 1. Start A New Run And Export Targets

From the project root:

```powershell
cd F:\GP\Unit-test-demo
.\.venv\Scripts\python.exe .\llm_coverage_demo.py --repo "<REPO_URL_OR_LOCAL_PATH>" --mode method --build auto --max-files 10 --max-targets 50 --ollama-model qwen2.5-coder:7b
```

Because `demo/pipeline.py` currently returns after writing targets, this creates:

```text
demo_out\<repo_name>\runs\<timestamp>\DemoTestCases\targets.json
demo_out\<repo_name>\runs\<timestamp>\DemoTestCases\config.json
demo_out\<repo_name>\runs\<timestamp>\repo\
```

Use the printed `Targets file:` path to find the run folder.

## 2. Create Prompt JSON Files

Create one JSON file per target inside:

```text
demo_out\<repo_name>\runs\<timestamp>\DemoTestCases\prompts\
```

Each file must look like:

```json
{
  "test_class_name": "LLM_GeneratedSomeClassSomeMethod_xxxxxxxxTest",
  "prompt": "Your strict prompt for Ollama. It must tell the model to output only Java code."
}
```

The `test_class_name` should start with:

```text
LLM_Generated
```

and end with:

```text
Test
```

## 3. Generate Tests From Saved Prompts

Do not run the normal `--repo` command for this step. Resume from the existing `DemoTestCases` folder:

```powershell
.\.venv\Scripts\python.exe .\llm_coverage_demo.py --generate-from-prompts .\demo_out\<repo_name>\runs\<timestamp>\DemoTestCases --ollama-model qwen2.5-coder:7b
```

This reads:

```text
DemoTestCases\prompts\*.json
```

and writes generated tests to:

```text
DemoTestCases\generated\
repo\src\test\java\<package>\
DemoTestCases\written_paths.json
```

## 4. If Ollama Fails

Check installed models:

```powershell
ollama list
```

Use the exact model name shown by `ollama list`:

```powershell
.\.venv\Scripts\python.exe .\llm_coverage_demo.py --generate-from-prompts .\demo_out\<repo_name>\runs\<timestamp>\DemoTestCases --ollama-model qwen2.5-coder:7b
```

If you get GPU memory errors, stop Ollama and retry:

```powershell
Stop-Process -Name ollama -Force
```

Then rerun the generation command. If memory still fails, use a smaller installed model.

## 5. Compile Only Generated Tests

Go into the cloned repo for that run:

```powershell
cd .\demo_out\<repo_name>\runs\<timestamp>\repo
```

If the repo already has old/bad tests, disable non-generated test files first:

```powershell
Get-ChildItem .\src\test\java -Recurse -Filter *.java |
  Where-Object { $_.Name -notlike "LLM_Generated*Test.java" } |
  Rename-Item -NewName { $_.Name + ".disabled" }
```

Compile generated tests:

```powershell
mvn -q -Dtest=LLM_Generated*Test test-compile
```

Warnings are okay. Errors are not okay.

## 6. Run Generated Tests With JaCoCo

Run tests with the JaCoCo agent:

```powershell
mvn -q org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent -Dtest=LLM_Generated*Test test
```

Generate the coverage report:

```powershell
mvn -q org.jacoco:jacoco-maven-plugin:0.8.12:report
```

## 7. Open Coverage Report

Check that the HTML report exists:

```powershell
Test-Path .\target\site\jacoco\index.html
```

Open it:

```powershell
Start-Process .\target\site\jacoco\index.html
```

The XML report is:

```text
target\site\jacoco\jacoco.xml
```

## 8. Print Coverage Percentages In PowerShell

From the run repo folder:

```powershell
[xml]$x = Get-Content .\target\site\jacoco\jacoco.xml
$x.report.counter | ForEach-Object {
  $missed = [int]$_.missed
  $covered = [int]$_.covered
  $total = $missed + $covered
  if ($total -gt 0) {
    "{0}: {1:N2}%" -f $_.type, (($covered / $total) * 100)
  }
}
```

## 9. Example Using Current Max Run

Generate from saved prompts:

```powershell
cd F:\GP\Unit-test-demo
.\.venv\Scripts\python.exe .\llm_coverage_demo.py --generate-from-prompts .\demo_out\max\runs\20260610_152615\DemoTestCases --ollama-model qwen2.5-coder:7b
```

Compile and run coverage:

```powershell
cd .\demo_out\max\runs\20260610_152615\repo
mvn -q -Dtest=LLM_Generated*Test test-compile
mvn -q org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent -Dtest=LLM_Generated*Test test
mvn -q org.jacoco:jacoco-maven-plugin:0.8.12:report
Start-Process .\target\site\jacoco\index.html
```
