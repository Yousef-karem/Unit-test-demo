# LLM Unit Test + Coverage Demo (GPT Prompting → Ollama Codegen → JaCoCo)

This project is a **demo tool** that generates **JUnit unit tests** (JUnit 4 or 5, detected from the target project) for any Java Maven project, then runs **JaCoCo coverage** **using ONLY the generated tests** to measure their quality.

## High-level pipeline

1. **Clone (or copy) a target repository** into `demo_out/.../repo`
2. **Scan the repo source code** under `src/main/java`
3. For each target class/method:
   - **GPT (gpt-5.2)** writes a *strict prompt* for test generation
   - **Ollama model (e.g. qwen2.5-coder / llama)** generates Java test code from that prompt
4. Write generated tests into the target repo under `src/test/java` **(no git commit / no push)**
5. Run a two-stage quality gate:
   - **Compile stage**: compile *only generated tests* (`LLM_Generated*Test`)
   - **Runtime stage**: run *only generated tests* with **JaCoCo agent**
6. Always produce:
   - A coverage report folder (if any generated tests compiled & executed)
   - Logs showing what compiled / failed / was repaired / rejected
   - A final `summary.json` with the complete run results

---

## What this tool measures

✅ **Coverage from generated tests ONLY**  
This is intentional: we want to evaluate the quality of the generated tests independently, not mixed with existing tests.

You get JaCoCo metrics:
- **Line coverage**
- **Instruction coverage**
- **Branch coverage**

---

## Requirements

### System requirements
- Python 3.10+
- Git
- Java (JDK 17+ recommended) — optional when using `--docker-maven`
- Maven and/or Gradle (depending on target project) — Maven optional when using `--docker-maven`
- Ollama installed and running locally
- Docker (optional) — for pinned JDK/Maven via `--docker-maven`

### Python dependencies
Install and activate a venv:

```bash
python -m venv venv
source venv/bin/activate
pip install -U pip
pip install openai requests
```

### Ollama setup

Start Ollama and pull a model:

```bash
ollama serve
ollama pull qwen2.5-coder:7b
# or
ollama pull llama3
```

(Optional) change model storage location:

```bash
export OLLAMA_MODELS="/path/to/bigger_disk/ollama_models"
```

### OpenAI key

Set `OPENAI_API_KEY`:

```bash
export OPENAI_API_KEY="YOUR_KEY"
```

Or create a `.env` file in the same folder as `llm_coverage_demo.py`:

```env
OPENAI_API_KEY=YOUR_KEY
```

### Docker Maven (optional, hybrid mode)

Python and Ollama stay on the host. Only Maven compile/test/coverage runs inside Docker for a consistent JDK/Maven toolchain.

By default, the pipeline reads the target project's root `pom.xml` and uses that Java version for **LLM prompt generation, test codegen, repairs, and Docker Maven** (Docker coerces to supported image tags `8/11/17/21`). Docker pulls the matching official image on first use — no manual build required.

Run with Docker Maven:

```bash
python llm_coverage_demo.py \
  --repo "<YOUR_REPO_URL_OR_PATH>" \
  --mode method \
  --docker-maven
```

Useful flags:

| Flag | Default | Purpose |
|------|---------|---------|
| `--docker-maven` | off | Enable Docker for Maven stages |
| `--docker-maven-image` | auto | Override full image name |
| `--docker-maven-cache-volume` | `llm-coverage-maven-cache` | Persistent Maven `.m2` cache |

**Java version detection** scans the root `pom.xml` for (in order): `maven.compiler.release`, `java.version`, `maven.compiler.source`, then `maven-compiler-plugin` `release`/`source`. The detected version is injected into prompt writer, test codegen, and compile/runtime repair prompts. Properties inherited only from a parent POM are not resolved. Unsupported Docker JDK tags fall back to Java 17 for the container image only.

**Optional custom image:** build [`docker/maven/Dockerfile`](docker/maven/Dockerfile) and pass `--docker-maven-image llm-coverage-maven:17` for air-gapped or pinned environments.

**Performance note:** Docker improves reproducibility (JDK/Maven version, isolated `.m2`), not end-to-end speed. LLM generation still dominates runtime. First Docker run may be slower until the Maven cache volume warms up.

**Paths with spaces:** The cloned repo is mounted with its absolute path. Paths like `/media/user/New Volume/...` are supported.

**JUnit detection:** Reads `pom.xml` / Gradle files for `junit:junit` (JUnit 4) or `junit-jupiter` (JUnit 5). If unclear, scans existing `src/test/java` imports. Defaults to JUnit 5 when still unknown. Legacy dataset projects with JUnit 4 in the pom will generate JUnit 4 tests.

**Related issue:** JUnit alignment does not fix legacy projects whose pom has no Java version (Maven defaults to Java 5 on JDK 17). Use `--docker-maven` with a Java 8 image or add `maven.compiler.source/target` to the pom.

---

## Naming convention

All generated tests follow:

* Prefix: `LLM_Generated`
* Pattern: `LLM_Generated*Test`

Example:

* `LLM_GeneratedUserServiceTest`
* `LLM_GeneratedPaymentProcessorTest`

This allows the tool to compile/run coverage **only** for generated tests.

---

## How it works (in detail)

### 1) Target discovery

The tool scans:

* `src/main/java/**/*.java`

Then it extracts either:

* `--mode class`: one target per class
* `--mode method`: one target per public method (usually yields more coverage)

### 2) GPT prompt generation (gpt-5.2)

GPT writes a strict JSON response:

```json
{
  "test_class_name": "LLM_GeneratedSomethingTest",
  "prompt": "..."
}
```

The prompt enforces:

* JUnit 4 or JUnit 5 (auto-detected from pom/gradle and existing tests)
* Mockito only when the project already depends on it
* No Spring Boot test framework
* No inventing missing dependencies
* Prefer concrete assertions
* Avoid `javax.*` vs `jakarta.*` mismatch

### 3) Ollama code generation

Ollama generates the actual Java unit test from the prompt.

### 4) Quality gates

#### Compile stage

Runs:

```bash
mvn -q -Dtest=LLM_Generated*Test test-compile
```

If a generated test fails to compile:

* the tool tries to repair it using GPT
* if still failing: it is moved to `DemoTestCases/rejected/compile/`

#### Runtime stage

Runs tests with JaCoCo agent:

```bash
mvn -q org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent -Dtest=LLM_Generated*Test test
mvn -q org.jacoco:jacoco-maven-plugin:0.8.12:report
```

If runtime errors occur:

* the tool tries a runtime repair using GPT
* if still failing: it is moved to `DemoTestCases/rejected/runtime/`

### 5) Coverage report

If at least one generated test executes successfully, JaCoCo produces:

* `jacoco.xml`
* `coverage/report/index.html`

---

## Output structure

Every run creates:

```
demo_out/
  <REPO_NAME>/
    runs/
      <TIMESTAMP>/
        repo/                  # full cloned/copied repo for that run (isolated)
        DemoTestCases/
          config.json           # run args and settings
          targets.json          # extracted targets
          prompts/              # GPT prompt JSON per test
          generated/            # raw generated Java code (before compile/runtime fixes)
          written_paths.json    # list of test files written into repo
          compile/
            compile_log.txt
            compile_gate_log.json
          runtime/
            test_log.txt
            runtime_gate_log.json
          rejected/
            compile/            # tests that failed compilation (moved here)
            runtime/            # tests that failed at runtime (moved here)
          failures/             # captured failure artifacts (java + error txt)
          coverage/
            build_log.txt
            repair_log.json
            removed_tests.json
            jacoco.xml          # (if created)
            report/             # JaCoCo HTML report
          summary.json          # final result summary (what to present in demo)
```

---

## Running the demo

### Minimal run (fast)

```bash
python llm_coverage_demo.py \
  --repo "https://github.com/Yousef-karem/Sagely_Backend_Java.git" \
  --mode class \
  --build auto \
  --gpt-model "gpt-5.2" \
  --max-files 5 \
  --max-targets 10
```

### Increase coverage (recommended)

#### Option A: class-based (broad)

```bash
python llm_coverage_demo.py \
  --repo "<YOUR_REPO_URL_OR_PATH>" \
  --mode class \
  --build auto \
  --max-files 60 \
  --max-targets 120
```

#### Option B: method-based (deeper coverage)

```bash
python llm_coverage_demo.py \
  --repo "<YOUR_REPO_URL_OR_PATH>" \
  --mode method \
  --build auto \
  --max-files 40 \
  --max-targets 300
```

### Pick packages interactively

```bash
python llm_coverage_demo.py \
  --repo "<YOUR_REPO_URL_OR_PATH>" \
  --select-packages \
  --mode method
```

### Use specific packages

```bash
python llm_coverage_demo.py \
  --repo "<YOUR_REPO_URL_OR_PATH>" \
  --packages "com.myapp.service,com.myapp.util" \
  --mode method
```

### Change Ollama model

```bash
python llm_coverage_demo.py \
  --repo "<YOUR_REPO_URL_OR_PATH>" \
  --ollama-model "qwen2.5-coder:7b"
```

---

## How to interpret results

Open:

* `DemoTestCases/summary.json`
  and/or
* `DemoTestCases/coverage/report/index.html`

Key summary fields:

* `generated_total`: total tests generated
* `compile_survivors`: compiled tests after compile gate + repairs
* `runtime_survivors`: tests that executed after runtime gate + repairs
* `coverage`: JaCoCo percentages from generated tests only
* `coverage_report_index`: absolute path to HTML report

If coverage is low:

* increase `--max-files` / `--max-targets`
* switch to `--mode method`
* focus on core packages (services/utilities)
* include more logic-heavy classes

---

## Common issues & fixes

### 1) “Coverage report not found”

Usually means:

* no generated tests compiled, OR
* tests didn’t run successfully, OR
* JaCoCo didn’t produce `jacoco.exec`

Check:

* `DemoTestCases/coverage/build_log.txt`
* `DemoTestCases/compile/compile_log.txt`
* `DemoTestCases/runtime/test_log.txt`

### 2) Generated tests fail compilation

This can happen because:

* the code generator referenced a type that doesn’t exist
* wrong imports (javax vs jakarta)
* Mockito stubbing with incorrect generic types

The tool:

* attempts GPT repairs
* moves unrecoverable tests into `rejected/compile/`

### 3) Generated tests fail runtime

Often caused by:

* null fields not initialized
* missing enum values
* partial mocking or wrong assumptions

The tool:

* attempts runtime repair
* moves unrecoverable tests into `rejected/runtime/`

### 4) Docker Maven errors

| Symptom | Fix |
|---------|-----|
| `Docker was not found on PATH` | Install Docker or drop `--docker-maven` |
| `Unable to find image 'maven:3.9-eclipse-temurin-21'` | Run `docker pull maven:3.9-eclipse-temurin-21` (or the version shown in the log) |
| Wrong Java version in generated tests | Add `java.version` to child `pom.xml` if it is only defined in a parent POM |
| Properties only in parent POM | Duplicate `java.version` in child pom so detection and LLM prompts see it |
| Permission denied on `/var/run/docker.sock` | Add your user to the `docker` group |
| Slow first run | Normal — Maven downloads dependencies into the cache volume |

---

## Notes / Limitations

* This tool is designed for  **unit tests** , not integration tests.
* It generates tests with the **JUnit version detected from the target project** (4 or 5), plus Mockito only when already present
* It does **not commit or push** anything into the target repo.
* JaCoCo coverage here measures how much code was executed by the generated tests — it does not guarantee correctness.

---

## Quick demo script (presentation-friendly)

1. Run command on a public GitHub repo
2. Show that tests are created in `repo/src/test/java/...`
3. Open `summary.json`
4. Open HTML report:
   `DemoTestCases/coverage/report/index.html`
5. Explain:
   * compile survivors vs rejected
   * runtime survivors vs rejected
   * coverage numbers from generated tests only



## Run command options (explained flag-by-flag)

You typically run the tool like this:

```bash
python llm_coverage_demo.py \
  --repo "https://github.com/Yousef-karem/Sagely_Backend_Java.git" \
  --output-dir "<repo>/demo_out" \
  --mode class \
  --build auto \
  --gpt-model "gpt-5.2" \
  --ollama-model "qwen2.5-coder:7b" \
  --max-files 5 \
  --max-targets 10
````

Below is what **each option/flag does**, and how it affects generation + coverage.

---

### `--repo`

**What it does:** Selects the target Java project to analyze and generate tests for.
**Accepts:**

* GitHub repo URL (e.g., `https://github.com/user/repo.git`)
* GitHub folder URL (e.g., `https://github.com/user/repo/tree/main/subproject`) — clones the repo, checks out the branch, and uses that subfolder as the project root
* Local path (e.g., `/home/youssef/projects/MyApp`)

**What happens:**
The tool clones/copies the repo into:
`<output_dir>/<repo_name>/runs/<timestamp>/repo/`
(see `--output-dir` below for what `<output_dir>` resolves to)

---

### `--output-dir`

**What it does:** Sets the root directory where all generated artifacts are written — `runs/`, `coverage/`, `summary.json`, `logs/`, and everything else nested under a run.

**Optional:** yes.

**Default when omitted:** a `demo_out` directory created relative to the current working directory (the tool's original behavior, unchanged for existing scripts/CI that don't pass this flag).

**Example:**

```bash
--repo "C:\Projects\Calculator" \
--output-dir "C:\Projects\Calculator\demo_out"
```

This writes output next to the target project instead of next to the Python tool:

```
Calculator/
├── src/
├── pom.xml
├── demo_out/
│   ├── runs/
│   ├── coverage/
│   ├── summary.json
│   └── logs/
└── ...
```

---

### `--branch`

**What it does:** Checks out a specific branch after cloning.
**Example:**

```bash
--branch develop
```

**If omitted:** Uses the repo’s default branch.

---

### `--mode {class|method}`

Controls the *granularity* of test generation.

#### `--mode class`

**What it does:** Generates **one test class per source class**.
**Pros:** Faster, fewer generated files, easier to read.
**Cons:** Often lower coverage (tests tend to stay “high-level”).

#### `--mode method`

**What it does:** Generates **tests per public method** (many more targets).
**Pros:** Usually higher coverage (more targeted prompts).
**Cons:** More tests, more time, more compile/runtime failures to repair.

**Rule of thumb:**

* Start with `class` for a quick demo
* Use `method` when you want better coverage

---

### `--build {auto|maven|gradle}`

Controls how the tool decides the build system.

#### `--build auto`

**What it does:** Detects build tool:

* Maven if `pom.xml` exists
* Gradle if `build.gradle` / `build.gradle.kts` exists

#### `--build maven`

Forces Maven flow even if Gradle files exist.

#### `--build gradle`

Forces Gradle flow (requires `./gradlew` wrapper in the repo).

---

### `--gpt-model`

**What it does:** Selects which OpenAI model is used to:

1. write the strict prompts
2. repair generated tests (compile/runtime repair loops)

**Example:**

```bash
--gpt-model "gpt-5.2"
```

---

### `--ollama-model`

**What it does:** Selects which Ollama model generates the Java test code.
**Example:**

```bash
--ollama-model "qwen2.5-coder:7b"
```

**Impact:**

* Different models produce different compile success rate + test quality.
* Code-focused models usually compile more often.

---

### `--max-files`

**What it does:** Limits how many source `.java` files are scanned from:
`src/main/java`

**Example:**

```bash
--max-files 50
```

**Impact:**

* Higher value → more classes analyzed → more tests → usually higher coverage
* Lower value → faster demo

---

### `--max-targets`

**What it does:** Limits how many targets are generated from the scanned files.

A “target” means:

* in `--mode class`: each class is 1 target
* in `--mode method`: each public method is 1 target

**Example:**

```bash
--max-targets 300
```

**Impact:**

* More targets → more generated tests → higher chance of increasing coverage
* Also increases runtime/repairs

---

### `--max-refinement-iterations`

**What it does:** Sets how many times the feedback loop may repair a generated test after compile or runtime failure.

**Example:**

```bash
--max-refinement-iterations 5
```

**Impact:**

* Higher value → more CubeTester-style refinement attempts
* Lower value → faster runs with more rejected tests

---

### `--packages`

**What it does:** Restricts generation to specific packages only.
**Example:**

```bash
--packages "com.myapp.service,com.myapp.util"
```

**Impact:**

* Great when you want to focus on “logic-heavy” code
* Helps avoid framework/config classes that don’t unit-test cleanly

If omitted or `"ALL"` → scans all packages.

---

### `--select-packages`

**What it does:** Interactive package selection (multi-select).
**Usage:**

```bash
--select-packages
```

The tool prints discovered packages and you choose by numbers.

---

### `--skip-framework-classes` / `--no-skip-framework-classes`

**What it does:** Filters out “framework wiring” classes by name keywords:

* application, config, filter, security, interceptor

Default is **enabled**.

**Disable it if you want full coverage exploration:**

```bash
--no-skip-framework-classes
```

**Impact:**

* Keeping it enabled usually reduces compile/runtime failures
* Disabling it may generate tests for filters/configs that need servlet/security setup

---

## What the tool actually runs (build commands)

### Maven (compile stage)

Compiles only generated tests:

```bash
mvn -q -Dtest=LLM_Generated*Test test-compile
```

If compilation fails:

* GPT tries to repair the failing file up to `--max-refinement-iterations`
* unrepaired tests are moved to `DemoTestCases/rejected/compile/`

### Maven (runtime + coverage stage)

Runs only generated tests with JaCoCo agent:

```bash
mvn -q org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent -Dtest=LLM_Generated*Test test
mvn -q org.jacoco:jacoco-maven-plugin:0.8.12:report
```

If runtime errors occur:

* GPT tries runtime repairs up to `--max-refinement-iterations`
* unrepaired tests are moved to `DemoTestCases/rejected/runtime/`

### Gradle (coverage stage)

Runs only generated tests and produces JaCoCo report via init script:

```bash
./gradlew -q -I <init.gradle> test --tests "*LLM_Generated*Test" jacocoTestReport
```

---

## Recommended run profiles

### Quick demo (fast)

```bash
python llm_coverage_demo.py --repo "<repo>" --mode class --max-files 5 --max-targets 10
```

### Better coverage (balanced)

```bash
python llm_coverage_demo.py --repo "<repo>" --mode method --max-files 40 --max-targets 300
```

### Focus coverage on core logic packages

```bash
python llm_coverage_demo.py --repo "<repo>" --mode method --packages "com.myapp.service,com.myapp.util"
```

### Reproducible Maven (Docker, auto Java version)

```bash
python llm_coverage_demo.py --repo "<repo>" --mode method --docker-maven --max-files 40 --max-targets 300
```
