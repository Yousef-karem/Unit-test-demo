# LLM Unit Test + Coverage Demo — Complete Code Documentation

This document explains **every part of the codebase** so you can understand how the tool works from the command line down to individual functions. Share this with anyone who needs to read, run, or extend the project.

---

## Table of Contents

1. [What This Project Does](#1-what-this-project-does)
2. [Architecture Overview](#2-architecture-overview)
3. [Project File Structure](#3-project-file-structure)
4. [Prerequisites & Setup](#4-prerequisites--setup)
5. [How to Run](#5-how-to-run)
6. [End-to-End Pipeline Flow](#6-end-to-end-pipeline-flow)
7. [Output Folder Structure](#7-output-folder-structure)
8. [Entry Point: `llm_coverage_demo.py`](#8-entry-point-llm_coverage_demopy)
9. [Configuration: `demo/config.py`](#9-configuration-democonfigpy)
10. [Repository Handling: `demo/repo.py`](#10-repository-handling-demorepopy)
11. [Package Discovery: `demo/packages.py`](#11-package-discovery-demopackagespy)
12. [Target Extraction: `demo/targets.py`](#12-target-extraction-demotargetspy)
13. [Utilities: `demo/utils.py`](#13-utilities-demoutilspy)
14. [Ollama Client: `demo/llm/ollama.py`](#14-ollama-client-demollmollamapy)
15. [Prompt Writer & Repair: `demo/llm/prompt_writer.py`](#15-prompt-writer--repair-demollmprompt_writerpy)
16. [Maven Coverage: `demo/coverage/maven.py`](#16-maven-coverage-democoveragemavenpy)
17. [Coverage Parsing: `demo/coverage/parse.py`](#17-coverage-parsing-democoverageparsepy)
18. [Gradle Coverage: `demo/coverage/gradle.py`](#18-gradle-coverage-democoveragegradlepy)
19. [Main Pipeline: `demo/pipeline.py`](#19-main-pipeline-demopipelinepy)
20. [Legacy / Experimental Files](#20-legacy--experimental-files)
21. [Environment & Secrets](#21-environment--secrets)
22. [Common Problems & Where to Look](#22-common-problems--where-to-look)
23. [Glossary](#23-glossary)

---

## 1. What This Project Does

This is an **automated Java unit-test generator and coverage evaluator**. Given any Java Maven project (GitHub URL or local path), it:

1. Scans production code under `src/main/java`
2. Uses a **local Ollama LLM** in two roles:
   - **Prompt writer** — builds strict instructions for test generation
   - **Code generator** — writes JUnit 4 or JUnit 5 test classes (version detected from target project)
3. Writes tests into the target repo as `LLM_Generated*Test.java`
4. Compiles and runs **only those generated tests**
5. Measures **JaCoCo code coverage** to see how much production code the generated tests actually execute
6. Saves a full audit trail under `demo_out/`

**Important:** Generated tests are written locally only — nothing is committed or pushed to the target repository.

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  llm_coverage_demo.py  (CLI entry point)                        │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  demo/pipeline.py  (orchestrator)                               │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────────────────┐ │
│  │ repo.py     │  │ packages.py  │  │ targets.py              │ │
│  │ clone/copy  │  │ find pkgs    │  │ extract class/methods   │ │
│  └─────────────┘  └──────────────┘  └─────────────────────────┘ │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │ prompt_writer.py → ollama.py  (prompt + codegen + repair)   ││
│  └─────────────────────────────────────────────────────────────┘│
│  ┌─────────────────────────────────────────────────────────────┐│
│  │ utils.py  (sanitize, validate, mock→concrete rewrites)      ││
│  └─────────────────────────────────────────────────────────────┘│
│  ┌─────────────────────────────────────────────────────────────┐│
│  │ maven.py + parse.py  (compile, test, JaCoCo, parse reports) ││
│  └─────────────────────────────────────────────────────────────┘│
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
                    demo_out/<repo>/runs/<timestamp>/
```

**Two-stage LLM design:**

| Stage | Model role | Purpose |
|-------|------------|---------|
| Prompt writing | `--gpt-model` (Ollama) | Produces JSON with `test_class_name` + detailed generation prompt |
| Code generation | `--ollama-model` (Ollama) | Produces actual Java test source code |
| Compile repair | `--gpt-model` | Fixes compilation errors |
| Runtime repair | `--gpt-model` | Fixes test failures (exceptions, assertions) |

Both "gpt-model" and "ollama-model" use **Ollama locally** — the name `gpt-model` is historical.

---

## 3. Project File Structure

```
llm-unit-test-demo/
├── llm_coverage_demo.py      ← Main CLI (run this)
├── DOCUMENTATION.md          ← This file
├── README_DEMO.md            ← Quick start guide
├── .env.example              ← Environment variable template
├── .gitignore
│
├── demo/                     ← Core application package
│   ├── config.py             ← Constants (Ollama URL, prefixes, paths)
│   ├── pipeline.py           ← Main orchestration logic (~938 lines)
│   ├── repo.py               ← Clone/copy repos, detect Maven/Gradle
│   ├── packages.py           ← Java package discovery & filtering
│   ├── targets.py            ← Extract test targets from source files
│   ├── utils.py              ← Java output cleaning, validation, rewrites
│   │
│   ├── llm/
│   │   ├── ollama.py         ← Simple Ollama HTTP client for codegen
│   │   └── prompt_writer.py  ← Prompt generation + compile/runtime repair
│   │
│   └── coverage/
│       ├── maven.py          ← Maven test-compile, test, JaCoCo report
│       ├── runner.py         ← Host vs Docker Maven execution
│       ├── java_version.py   ← Detect Java version from pom.xml
│       ├── test_libraries.py ← Detect JUnit 4/5 from pom/gradle/tests
│       ├── parse.py          ← Parse JaCoCo XML + Surefire reports
│       └── gradle.py         ← Gradle JaCoCo helper (not wired in pipeline yet)
│
├── docker/
│   └── maven/
│       └── Dockerfile        ← Pinned JDK 17 + Maven image for --docker-maven
│
├── demo.py                   ← Legacy experiment (CodeBERT + Ollama)
├── demo_pipeline.py          ← Legacy monolithic pipeline (superseded)
│
└── demo_out/                 ← Generated at runtime (gitignored)
    └── <repo_name>/runs/<timestamp>/
```

---

## 4. Prerequisites & Setup

| Requirement | Why |
|-------------|-----|
| Python 3.10+ | Runs the pipeline |
| Java JDK 17+ | Compiles target projects (not required when using `--docker-maven`) |
| Maven | Builds and tests target projects (not required when using `--docker-maven`) |
| Git | Clones GitHub repos |
| Ollama | Local LLM for prompts, codegen, repairs |
| Docker (optional) | Pinned JDK/Maven for compile/coverage when using `--docker-maven` |

```bash
python -m venv venv
source venv/bin/activate
pip install requests

ollama serve
ollama pull qwen2.5-coder:7b
```

**Optional — Docker Maven (hybrid mode):** Python and Ollama stay on the host; only Maven runs in a container. Java version is auto-detected from the target `pom.xml` for LLM prompts and Docker Maven, then mapped to an official image such as `maven:3.9-eclipse-temurin-21`. Docker pulls the image on first use.

Then pass `--docker-maven` when running the demo (see [Section 8](#8-entry-point-llm_coverage_demopy)).

Optional `.env` file (see [Section 21](#21-environment--secrets)).

---

## 5. How to Run

```bash
python llm_coverage_demo.py \
  --repo "https://github.com/Yousef-karem/max" \
  --mode method \
  --build auto \
  --max-files 40 \
  --max-targets 300
```

**With Docker Maven** (consistent JDK/Maven, no host Java toolchain needed):

```bash
python llm_coverage_demo.py \
  --repo "https://github.com/Yousef-karem/max" \
  --mode method \
  --docker-maven \
  --max-files 40 \
  --max-targets 300
```

Java version is auto-detected from the cloned repo's root `pom.xml` and used for LLM generation/repairs and Docker Maven.

See [Section 8](#8-entry-point-llm_coverage_demopy) for all CLI flags.

---

## 6. End-to-End Pipeline Flow

The function `run_pipeline()` in `demo/pipeline.py` executes these steps:

| Step | What happens |
|------|--------------|
| **1. Setup** | Create `demo_out/<repo>/runs/<timestamp>/`, clone/copy repo |
| **2. Build detect** | Confirm Maven (`pom.xml`) — Gradle not supported in pipeline yet |
| **3. Package filter** | Optionally restrict to specific Java packages |
| **4. Target scan** | Walk `src/main/java`, extract classes/methods to test |
| **5. Generation** | For each target: write prompt → generate Java → validate → write to repo |
| **6. Isolation** | Move pre-existing `*Test.java` files out of the way |
| **7. Compile stage** | `mvn test-compile` on `LLM_Generated*Test` only; repair failures |
| **8. Runtime stage** | `mvn test` with JaCoCo; repair runtime failures |
| **9. Coverage report** | `mvn jacoco:report`, copy HTML/XML to `DemoTestCases/coverage/` |
| **10. Summary** | Write `summary.json`, restore isolated tests, print results |

**Quality gates:**

- **Generation gate** — rejects invalid LLM output before writing to repo
- **Compile gate** — up to `--max-refinement-iterations` LLM repairs per file, then moves to `rejected/compile/`
- **Runtime gate** — up to `--max-refinement-iterations` LLM repairs per failing method/file, then moves to `rejected/runtime/`

If **zero tests compile**, the runtime stage is skipped and coverage is 0%.

---

## 7. Output Folder Structure

Each run creates:

```
demo_out/<repo_name>/runs/<YYYYMMDD_HHMMSS>/
├── repo/                              # Cloned/copied target project
└── DemoTestCases/
    ├── config.json                    # CLI args and model names
    ├── targets.json                   # All extracted targets
    ├── summary.json                   # Final results (START HERE)
    ├── written_paths.json             # Tests written into repo
    ├── generation_quality_log.json    # Tests rejected at generation
    │
    ├── prompts/                       # LLM prompt JSON per test class
    ├── generated/                     # Raw generated Java (before repairs)
    │
    ├── compile/
    │   ├── compile_log.txt            # Full Maven compile output
    │   ├── repair_log.json            # Compile repair attempts
    │   └── compile_gate_log.json      # Tests moved to rejected/compile
    │
    ├── runtime/
    │   ├── test_log.txt               # Maven test output
    │   ├── runtime_gate_log.json
    │   └── runtime_repair_log.json
    │
    ├── rejected/
    │   ├── compile/                   # Tests that failed compilation
    │   └── runtime/                   # Tests that failed at runtime
    │
    ├── failures/                      # Snapshots: __compile_before.java, etc.
    ├── isolation/                     # Pre-existing tests moved aside
    │
    └── coverage/
        ├── build_log.txt
        ├── jacoco.xml
        ├── repair_log.json
        ├── quality_gate.txt           # If tests pass but cover 0 lines
        ├── no_report_reason.txt       # If report missing
        └── report/
            ├── index.html             # ← Main HTML coverage report
            ├── jacoco.csv
            └── ds/Max.html            # Per-class drill-down
```

---

## 8. Entry Point: `llm_coverage_demo.py`

This is the **only file you need to run**.

### Imports

```python
from demo.config import DEFAULT_GPT_MODEL, DEFAULT_OLLAMA_MODEL
```

Loads default model names from config/environment.

### `main()`

Creates an `argparse` parser and passes parsed args to `run_pipeline()`.

| CLI Flag | Type | Default | Meaning |
|----------|------|---------|---------|
| `--repo` | string | **required** | GitHub URL or local project path |
| `--branch` | string | `None` | Git branch to checkout after clone |
| `--mode` | `method` \| `class` | `method` | One test per method vs one per class |
| `--build` | `auto` \| `maven` \| `gradle` | `auto` | Build system detection |
| `--packages` | string | `None` | Comma-separated package filter, or `"ALL"` |
| `--select-packages` | flag | off | Interactive package picker |
| `--ollama-model` | string | `qwen2.5-coder:7b` | Model for **Java code generation** |
| `--gpt-model` | string | `qwen2.5-coder:7b` | Model for **prompts and repairs** |
| `--max-files` | int | `10` | Max source `.java` files to scan |
| `--max-targets` | int | `50` | Max targets (classes or methods) to test |
| `--max-refinement-iterations` | int | `5` | Max compile/runtime repair attempts per generated test |
| `--skip-framework-classes` | bool | `True` | Skip classes named *Application*, *Config*, *Filter*, etc. |
| `--no-skip-framework-classes` | flag | — | Disable framework class filtering |
| `--docker-maven` | flag | off | Run Maven compile/test/coverage inside Docker |
| `--docker-maven-image` | string | `None` | Override Docker image (default: `maven:3.9-eclipse-temurin-<version>`) |
| `--docker-maven-cache-volume` | string | `llm-coverage-maven-cache` | Named Docker volume for Maven `.m2` cache |

---

## 9. Configuration: `demo/config.py`

Central constants used across the project.

| Name | Value | Purpose |
|------|-------|---------|
| `OLLAMA_URL` | `http://127.0.0.1:11434/api/generate` | Ollama HTTP API endpoint |
| `DEFAULT_OLLAMA_MODEL` | env `OLLAMA_MODEL` or `qwen2.5-coder:7b` | Default codegen model |
| `DEFAULT_GPT_MODEL` | env `GPT_MODEL` or `qwen2.5-coder:7b` | Default prompt/repair model |
| `GENERATED_PREFIX` | `"LLM_Generated"` | Prefix for all generated test class names |
| `GENERATED_PATTERN` | `"LLM_Generated*Test"` | Maven `-Dtest=` filter pattern |
| `DEMO_OUT` | `Path("demo_out")` | Root folder for all run artifacts |
| `FALLBACK_JAVA_VERSION` | `"17"` | Used when pom.xml has no Java version property |
| `DEFAULT_DOCKER_MAVEN_IMAGE` | env or `None` | Optional override for Docker image name |
| `DEFAULT_DOCKER_MAVEN_CACHE_VOLUME` | env or `llm-coverage-maven-cache` | Named volume for Maven `.m2` cache |

---

## 10. Repository Handling: `demo/repo.py`

### `_run(cmd, cwd, check)`

Internal helper — runs a shell command via `subprocess.run`, captures stdout/stderr as text.

### `is_github_url(s)`

Returns `True` if `s` starts with `https://github.com/` or `git@github.com:`.

### `parse_github_web_url(repo) -> (clone_url, branch, subpath)`

Parses GitHub HTTPS URLs, including browser links with `/tree/branch/subpath` or `/blob/branch/path`, into a clone URL, optional branch, and optional subdirectory.

### `github_repo_label(repo) -> str`

Derives a folder name for `demo_out/` (uses subfolder name when a `/tree/.../subpath` URL is given).

### `clone_or_update(repo, dest_repo, branch)`

**If local path:** copies the directory to `dest_repo` (replacing any existing copy).

**If GitHub URL:**
- Normalizes browser URLs (`/tree/main/foo`) to `git clone` + checkout + subfolder
- If `dest_repo` already has `.git`: `git fetch`, optionally checkout `branch`, then `git pull --rebase`
- Otherwise: `git clone` into `dest_repo`, optionally checkout `branch`
- Returns the project root (`dest_repo` or `dest_repo/subpath`)

### `detect_build_system(project_root)`

- Returns `"maven"` if `pom.xml` exists
- Returns `"gradle"` if `build.gradle` or `build.gradle.kts` exists
- Raises `RuntimeError` if neither found

---

## 10b. Test Library Detection: `demo/test_libraries.py`

### `detect_junit_version(project_root) -> "4" | "5"`

Detects which JUnit major version the target project uses:

| Priority | Source |
|----------|--------|
| 1 | `pom.xml` / Gradle: `junit-jupiter` → 5; `junit:junit` → 4 |
| 2 | Existing `src/test/java` imports |
| 3 | Fallback | JUnit 5 |

Saved as `resolved_junit_version` and `test_libraries.junit` in `DemoTestCases/config.json`. Passed to prompt writer, codegen, and compile/runtime repairs.

---

## 11. Package Discovery: `demo/packages.py`

### `PACKAGE_RE`

Regex: `^\s*package\s+([a-zA-Z0-9_.]+)\s*;` — extracts Java package declarations.

### `list_java_files(project_root)`

Returns all `*.java` files under `src/main/java/` (empty list if folder missing).

### `discover_packages(project_root)`

Scans all main-source Java files, counts files per package, returns sorted dict `{package_name: file_count}`.

### `choose_packages_interactive(pkgs)`

Prints numbered package list, reads user input:
- Empty input → `["*"]` (all packages)
- Comma-separated numbers → list of selected package names

### `file_in_selected_packages(java_file, project_root, selected)`

Returns `True` if file's package is in `selected`, or if `selected == ["*"]`.

---

## 12. Target Extraction: `demo/targets.py`

A **target** is one thing to generate a test for — either a whole class or a single method.

### Regexes

| Name | Matches |
|------|---------|
| `CLASS_RE` | `class ClassName` |
| `METHOD_RE` | `public [static] ReturnType methodName(params) {` |

### `extract_targets(java_path, mode)`

Reads a Java source file and returns a list of target dicts:

```python
{
    "package": "ds",
    "class_name": "Max",
    "method_name": "max",           # None in class mode
    "signature": "public Item max(Item[] items, int n)",
    "snippet": "...",               # Up to 2400 chars of relevant source
    "source_file": "/path/to/Max.java",
    "package_line": "package ds;"
}
```

**`mode == "class"`:** one target per file (first 2400 chars as snippet).

**`mode == "method"`:** one target per public method found by `METHOD_RE`.

### `_extract_imports_context_from_text(text)`

Extracts `package` and `import` lines from source text.

### `extract_imports_context(target)`

Reads the target's source file and returns package + import lines as a string (fed to the prompt writer).

---

## 13. Utilities: `demo/utils.py`

These functions clean and validate LLM-generated Java before compile.

### Shell & environment helpers

| Function | Purpose |
|----------|---------|
| `run(cmd, cwd, check)` | Generic subprocess wrapper |
| `load_env_file(path)` | Loads `KEY=VALUE` lines into `os.environ` (does not override existing vars) |
| `safe_repo_dirname(url)` | Converts GitHub URL to safe folder name |
| `safe_name(s)` | Replaces non-alphanumeric chars with `_` |
| `repo_name_from_arg(repo)` | Derives folder name from URL or local path |

### Java output sanitization

| Function | Purpose |
|----------|---------|
| `_best_fenced_java_block(text)` | Extracts Java from markdown ` ```java ... ``` ` blocks |
| `_truncate_after_balanced_class(text)` | Cuts text after the closing `}` of the first class |
| `sanitize_java_output(text)` | Full cleanup pipeline: strip fences → drop prose → truncate class |

### API analysis

| Function | Purpose |
|----------|---------|
| `declared_api_names(source_bundle)` | Finds method names and public field names in Java source text |
| `find_concrete_impls(related_sources, type_name)` | Finds classes implementing an interface (e.g. `MyItem implements Item`) |
| `_has_int_constructor(related_sources, class_name)` | Checks if class has `public ClassName(int ...)` constructor |

### Test quality fixes (deterministic, no LLM)

| Function | Purpose |
|----------|---------|
| `_keys_for_test_method(method_name, var_count)` | Picks integer keys based on test name (`LessThan` → 20, `Equal` → 10, etc.) |
| `rewrite_interface_mocks_to_concrete(code, related_sources)` | Replaces `@Mock Item` with `MyItem item = new MyItem(5)` so real code runs |
| `remove_invented_api_stubs(code, source_bundle)` | Removes `when(x.getKey())` lines when `getKey` doesn't exist in source |
| `enforce_test_class_name(code, expected)` | Renames `class WrongName` → `class LLM_GeneratedFoo_abc123Test` |

### Validation

| Function | Returns `None` if OK, else error string |
|----------|------------------------------------------|
| `validate_java_test_output(code, expected_class_name)` | Checks: non-empty, no markdown, has class, correct name, has `@Test` |
| `validate_test_coverage_quality(code, target, related_sources)` | Rejects tests that mock interfaces when concrete impls exist |

### Naming

| Function | Purpose |
|----------|---------|
| `ensure_unique_run_class_name(base, used, index)` | Adds `_M{n}` suffix if class name already used in this run |

---

## 14. Ollama Client: `demo/llm/ollama.py`

Minimal HTTP client for **code generation** (not prompt writing).

### `ollama_generate(model, prompt) -> str`

POSTs to `OLLAMA_URL` with:
- `model`, `prompt`
- `stream: false`
- `temperature: 0.2`, `num_predict: 2200`

Returns the `response` field from JSON. Used when generating actual Java test files from prompts.

---

## 15. Prompt Writer & Repair: `demo/llm/prompt_writer.py`

Handles all LLM calls that need structured output or repair context.

### Constants

- `OLLAMA_TIMEOUT = 600` — 10-minute timeout for large repair prompts

### Internal helpers

| Function | Purpose |
|----------|---------|
| `_check_ollama_response(r, model)` | Raises clear error on 404 (model not pulled) |
| `_extract_first_json_object(text)` | Parses first balanced `{...}` JSON from LLM text |
| `ollama_generate_json(model, prompt, system)` | Ollama call with `format: "json"` |
| `ollama_generate_text(model, prompt, system)` | Plain text Ollama call |

### `ollama_write_prompt(model, target, project_types_text, java_version) -> Dict`

**Step 1 of generation.** Sends the target method/class info to Ollama and expects JSON:

```json
{
  "test_class_name": "LLM_GeneratedMax_abc123Test",
  "prompt": "package ds;\n\nimport ..."
}
```

The system prompt enforces strict rules based on `target.test_libraries.junit`:
- JUnit 4: `org.junit.Test`, `org.junit.Assert`, `@Before`/`@After`
- JUnit 5: `org.junit.jupiter.api.*`, `@BeforeEach`/`@AfterEach`
- Mockito only when `test_libraries.mockito` is true
- Real objects over mocks
- Exact class name matching
- No invented APIs (e.g. no `getKey()` if only `key` field exists)
- At least 3 `@Test` methods

Includes: package/imports, source snippet, and project type allowlist.

### `ollama_repair_test(...) -> str`

**Compile repair.** Sends compiler errors + failing test file + production source + related types to Ollama. Returns sanitized fixed Java.

Parameters:
- `compiler_errors` — Maven `[ERROR]` output
- `file_content` — current test source
- `source_text` — class under test source
- `package_imports`, `constructor_info`, `repository_types`, `related_type_sources` — context

### `ollama_runtime_repair_test(model, stack_trace, file_content, failing_method="") -> str`

**Runtime repair.** Sends Surefire stack trace + failing method name + test file. Returns fixed Java.

---

## 16. Maven Coverage: `demo/coverage/maven.py` + `demo/coverage/runner.py` + `demo/coverage/java_version.py`

All Maven subprocess calls go through `demo/coverage/runner.py`. By default Maven runs on the **host**; with `--docker-maven`, the same goals run inside a Docker container with a JDK matching the target project.

### Java version detection (`java_version.py`)

When the pipeline starts, it reads the **root** `pom.xml` and resolves Java version for the whole run:

| Priority | Source |
|----------|--------|
| 1 | `<maven.compiler.release>` |
| 2 | `<java.version>` |
| 3 | `<maven.compiler.source>` |
| 4 | `<release>` in `maven-compiler-plugin` |
| 5 | `<source>` in `maven-compiler-plugin` |

**Consumers:**
- **LLM prompts** (`ollama_write_prompt`, codegen wrapper, compile/runtime repairs) receive `project_java_version` and `java_version_guidance()`
- **Docker Maven** uses the same detected value, coerced to supported image tags `8`, `11`, `17`, `21`

Supported Docker JDK tags: `8`, `11`, `17`, `21`. Unknown or unsupported values fall back to `17` for Docker only (LLM still uses the detected value).

**Limitation:** Properties defined only in a parent POM (e.g. `spring-boot-starter-parent`) are not resolved unless duplicated in the child pom.

Functions:
- `detect_java_version(project_root) -> str | None`
- `resolve_project_java_version(project_root) -> str`
- `resolve_docker_java_version(project_root) -> str`
- `java_version_guidance(version) -> str`

Run artifacts store `resolved_java_version` (LLM/compile target) and `docker_java_version` (container image tag) in `DemoTestCases/config.json`.

### `run_maven(maven_args, project_root) -> (log, return_code)` (`runner.py`)

Central entry point. Host mode runs `mvn` locally; Docker mode runs:

```bash
docker run --rm \
  -v "<abs-path-to-cloned-repo>:/workspace" \
  -v llm-coverage-maven-cache:/root/.m2 \
  -w /workspace \
  maven:3.9-eclipse-temurin-<version> \
  <maven-args...>
```

Use **absolute paths** for volume mounts. Paths containing spaces (for example `/media/user/New Volume/...`) are supported when passed as list arguments (no shell interpolation).

### `default_docker_image_for_java(version)`, `configure_maven_runner(...)`, `ensure_docker_available()` (`runner.py`)

Called at pipeline startup from `run_pipeline()` when `--docker-maven` is set. Validates that `docker` is on `PATH` and selects `maven:3.9-eclipse-temurin-{version}` unless `--docker-maven-image` overrides it. Optional custom image: build [`docker/maven/Dockerfile`](docker/maven/Dockerfile) and pass `--docker-maven-image llm-coverage-maven:17`.

### `project_has_jacoco(project_root)`

Checks if `pom.xml` contains `jacoco-maven-plugin`.

### `run_maven_test_compile(project_root, test_filter=None) -> (log, return_code)`

```bash
mvn -Drat.skip=true -Dcheckstyle.skip=true -Denforcer.skip=true \
    -q -Dtest=LLM_Generated*Test test-compile
```

Used by compile repair and compile gate loops in `pipeline.py`.

### `run_maven_tests(project_root, test_filter=None) -> (log, return_code)`

Runs generated tests with JaCoCo:

**If JaCoCo already in pom:**
```bash
mvn -Drat.skip=true -Dcheckstyle.skip=true -Denforcer.skip=true \
    -q -Dtest=LLM_Generated*Test test
```

**Otherwise:** prepends `jacoco:prepare-agent` goal inline.

Skips RAT, Checkstyle, Enforcer plugins so unrelated project checks don't block coverage.

### `run_maven_report(project_root) -> (log, return_code)`

```bash
mvn ... org.jacoco:jacoco-maven-plugin:report
```

Generates HTML + XML under `target/site/jacoco/`.

### `strip_ansi(s)`

Removes terminal color codes from Maven output.

### `extract_first_failing_test_path(log) -> Path | None`

Parses Maven compile error lines like:
```
[ERROR] .../LLM_GeneratedFooTest.java:[28,22] cannot find symbol
```
Returns path to first failing generated test file.

### `extract_failing_test_paths(log) -> List[Path]`

Same as above but returns **all** failing generated test paths.

### `write_failure_artifacts(failing_path, errors, failures_dir, suffix)`

Saves debug copies:
- `LLM_GeneratedFooTest__compile_before.java`
- `LLM_GeneratedFooTest__compile_before.txt` (last 80 lines of errors)

Suffixes used: `compile_before`, `compile_final`, `runtime_before`, `runtime_final`.

---

## 17. Coverage Parsing: `demo/coverage/parse.py`

### `parse_jacoco_xml(xml_path) -> Dict[str, float]`

Reads JaCoCo XML report root counters and returns:

```python
{
    "line_coverage": 0.923,
    "instruction_coverage": 0.941,
    "branch_coverage": 1.0,
}
```

Percentages are 0.0–1.0 (pipeline multiplies by 100 when printing).

### `parse_surefire_summary(log) -> Dict | None`

Regex-parses Maven summary line:
`Tests run: 6, Failures: 1, Errors: 0, Skipped: 0`

### `parse_surefire_reports(reports_dir) -> Dict | None`

Aggregates all `TEST-*.xml` files in Surefire reports directory.

### `extract_runtime_failures(reports_dir) -> List[Dict]`

Finds failed/error test cases for `LLM_Generated*Test` classes. Returns:

```python
[{"class_name": "ds.LLM_GeneratedMaxTest", "stack_trace": "..."}]
```

---

## 18. Gradle Coverage: `demo/coverage/gradle.py`

**Note:** Gradle is detected in `repo.py` but `pipeline.py` currently raises an error for non-Maven projects. This module exists for future Gradle support.

### `gradle_init_script_text()`

Returns Groovy init script that:
- Applies JaCoCo plugin
- Filters tests to `LLM_Generated*Test*`
- Enables XML + HTML reports

### `run_gradle_jacoco(project_root, demo_root) -> (log, return_code)`

Writes init script to `demo_root/gradle_init.gradle` and runs:
```bash
./gradlew -q -I <init.gradle> test --tests "*LLM_Generated*Test" jacocoTestReport
```

---

## 19. Main Pipeline: `demo/pipeline.py`

The heart of the application. Below is every function and major code block.

### Naming helpers

#### `stable_suffix_for_target(t) -> str`

Creates an 8-character SHA-1 hash from `source_file|class_name|method_name|signature`. Ensures unique test class names in method mode.

#### `ensure_unique_test_class_name(base, t, mode) -> str`

- If LLM name doesn't start with `LLM_Generated` / end with `Test`, builds name from class + method
- In `method` mode, appends `_<hash8>` before `Test` for uniqueness

### Project type discovery

#### `collect_related_type_sources(project_root, target) -> str`

Finds Java types referenced in a target's signature/snippet (e.g. `Item` in `max(Item[] items, int n)`), loads their source files, and **also loads concrete implementations** of any interfaces found (e.g. `MyItem` for `Item`).

This is critical for coverage — without it, the LLM doesn't know `MyItem` exists.

#### `list_project_types(project_root) -> List[str]`

Simple set of all class/interface/enum names in the project.

#### `_extract_type_api_summary(source_text, type_name) -> str`

For interfaces: lists `public ...;` method declarations.
For classes: lists constructors and public fields/methods.

#### `list_project_type_context(project_root) -> List[str]`

Rich descriptions like:
```
class MyItem implements Item api: public int compareItem(Item it); constructors: int key
```

Fed to the prompt writer as an allowlist.

#### `list_repository_types(project_root) -> List[str]`

Finds types ending in `Repository` (for Spring-style projects).

#### `extract_constructor_info(source_text, class_name) -> str`

Extracts constructor parameter lists for a class.

#### `is_interface_target(target) -> bool`

Returns `True` if target's source file declares an `interface` (skipped — we test concrete classes instead).

### File I/O helpers

#### `write_test_file(project_root, target_pkg, test_class_name, code) -> Path`

Writes Java to `src/test/java/<package>/LLM_Generated...Test.java`.

#### `isolate_non_generated_test_files(project_root, generated_paths, backup_root) -> List[Dict]`

**Critical for coverage runs.** Moves all `*Test.java` files that are NOT `LLM_Generated*` to a backup folder. Prevents pre-existing broken tests from blocking compile.

#### `restore_isolated_test_files(moved)`

Moves isolated tests back after the run completes.

### `run_pipeline(args) -> None`

The main function. Detailed step-by-step:

#### Step 1 — Setup (lines ~306–323)

```python
run_root = DEMO_OUT / repo_name / "runs" / timestamp
project_root = clone_or_update(args.repo, run_root / "repo", args.branch)
demo_root = run_root / "DemoTestCases"
```

Creates artifact subfolders: `prompts`, `generated`, `coverage`, `compile`, `runtime`, `rejected`, `failures`.

#### Step 2 — Build detection (lines ~325–330)

Auto-detects Maven. Raises error if Gradle (not supported in this pipeline version).

#### Step 3 — Package selection (lines ~332–338)

Uses `--select-packages`, `--packages`, or defaults to all (`["*"]`).

#### Step 4 — Target collection (lines ~340–365)

Loops Java files (up to `--max-files`), extracts targets, skips:
- Framework classes (if `--skip-framework-classes`)
- Interface targets
- Stops at `--max-targets`

Saves `config.json` and `targets.json`.

#### Step 5 — Test generation loop (lines ~376–464)

For each target:

1. **`ollama_write_prompt`** — get JSON prompt
2. **`ensure_unique_test_class_name`** + **`ensure_unique_run_class_name`** — unique naming
3. Save prompt to `prompts/<TestClass>.json`
4. **`collect_related_type_sources`** — load related Java files
5. Build generation prompt with concrete-type hints
6. **Up to 3 attempts:**
   - `ollama_generate` → `sanitize_java_output` → `enforce_test_class_name`
   - `remove_invented_api_stubs` → `rewrite_interface_mocks_to_concrete`
   - `validate_java_test_output` + `validate_test_coverage_quality`
   - Retry with error message if invalid
7. Save to `generated/`; if still invalid → `rejected/compile/invalid_generation/`
8. Else → **`write_test_file`** into repo

#### Step 5b — Isolation (lines ~472–481)

Moves pre-existing tests aside. Logs count.

#### Step 6 — Compile repair Phase A

Loop until all generated tests compile, all failing tests are rejected, or each failing test reaches `--max-refinement-iterations`:
1. Run `mvn test-compile -Dtest=LLM_Generated*Test`
2. If success → break
3. Find first failing test path from log
4. If retries ≥ 2 → move to `rejected/compile/`
5. Try **deterministic fix** (`remove_invented_api_stubs` + `rewrite_interface_mocks_to_concrete`)
6. Else **`ollama_repair_test`** with full context
7. Validate fix, write back, increment retry count

Saves `compile/repair_log.json`.

#### Step 6b — Compile gate Phase B (lines ~651–686)

Strict gate: any remaining compile failures → move to `rejected/compile/` without more repairs.

Computes `compile_survivors` and `compile_rejected`.

#### Step 7 — Runtime stage

Skipped if `compile_survivors == 0`.

Loop until generated tests pass, failing tests are rejected, or each failing test reaches `--max-refinement-iterations`:
1. **`run_maven_tests`** with JaCoCo
2. **`extract_runtime_failures`** from Surefire XML
3. For each failure: up to `--max-refinement-iterations` **`ollama_runtime_repair_test`** attempts
4. Unrecoverable → `rejected/runtime/`

Then **`run_maven_report`**.

#### Step 8–9 — Coverage collection (lines ~818–846)

- Copies JaCoCo HTML to `DemoTestCases/coverage/report/`
- Copies `jacoco.xml`
- Parses coverage percentages

#### Step 10 — Summary (lines ~848–937)

Builds `summary.json` with all metrics, restores isolated tests, prints coverage to console.

**Key summary fields:**

| Field | Meaning |
|-------|---------|
| `generated_total` | Prompts attempted |
| `generated_written` | Tests written to repo |
| `generation_rejected` | Failed validation before compile |
| `compile_survivors` | Tests that compiled |
| `compile_rejected` | Tests rejected at compile stage |
| `runtime_survivors` | Tests still in repo after runtime gate |
| `runtime_rejected` | Tests rejected at runtime |
| `coverage.line_coverage` | JaCoCo line % (0.0–1.0) |
| `coverage_report_index` | Path to `index.html` |

---

## 20. Legacy / Experimental Files

These are **not used** by `llm_coverage_demo.py`:

| File | Description |
|------|-------------|
| `demo.py` | Early prototype using Ollama + CodeBERT embeddings for validation |
| `demo_pipeline.py` | Older monolithic pipeline before modular `demo/` package |
| `llm_coverage_demo.pyx` | Cython variant (unused) |
| `pom.xml` (root) | Not the target project pom — likely leftover |

Use **`llm_coverage_demo.py`** + **`demo/pipeline.py`** as the source of truth.

---

## 21. Environment & Secrets

Copy `.env.example` to `.env`:

```env
OLLAMA_MODEL=qwen2.5-coder:7b
GPT_MODEL=qwen2.5-coder:7b
```

Loaded by `load_env_file()` before generation starts. `.env` is gitignored.

---

## 22. Common Problems & Where to Look

| Symptom | Likely cause | File to check |
|---------|--------------|---------------|
| Coverage 0% | No tests compiled | `compile/compile_log.txt`, `summary.json` → `compile_survivors` |
| Coverage low but tests pass | Tests mock interfaces instead of calling real code | `generated/*.java`, look for `@Mock Item` |
| `getKey()` compile error | LLM invented a getter | `failures/*__compile_before.txt` |
| "Coverage report not found" | Runtime stage skipped | `coverage/no_report_reason.txt` |
| Ollama 404 | Model not pulled | Run `ollama pull qwen2.5-coder:7b` |
| Pre-existing tests block compile | Isolation failed | `isolation/moved_tests.json` |
| Docker image not found | Image not pulled yet | Run `docker pull maven:3.9-eclipse-temurin-<version>` |
| Wrong Java in Docker | Parent POM only defines version | Add `java.version` to child pom so detection succeeds |
| Docker permission denied | User not in `docker` group | Add user to `docker` group or use `sudo` |
| Slow first Docker run | Cold Maven cache | Re-runs reuse `llm-coverage-maven-cache` volume |

---

## 23. Glossary

| Term | Definition |
|------|------------|
| **Target** | One class or method selected for test generation |
| **Survivor** | A generated test that passed a quality gate (compile or runtime) |
| **Rejected** | A test moved out of the repo after failing a gate |
| **Isolation** | Temporarily moving pre-existing tests so only LLM tests compile |
| **JaCoCo** | Java code coverage library; produces line/instruction/branch metrics |
| **Surefire** | Maven's test runner; produces `TEST-*.xml` reports |
| **Deterministic fix** | Code rewrite without LLM (stub removal, mock→concrete) |
| **Related sources** | Java files for types referenced by the method under test |
| **GENERATED_PATTERN** | `LLM_Generated*Test` — Maven test filter |

---

## Quick Reference: Which File Does What?

| I want to… | Look at |
|------------|---------|
| Change CLI flags | `llm_coverage_demo.py` |
| Change default models | `demo/config.py` or `.env` |
| Change prompt rules | `demo/llm/prompt_writer.py` |
| Change compile/test commands | `demo/coverage/maven.py` |
| Change host vs Docker Maven execution | `demo/coverage/runner.py`, `docker/maven/Dockerfile` |
| Change coverage parsing | `demo/coverage/parse.py` |
| Change Java cleanup/validation | `demo/utils.py` |
| Change what gets scanned | `demo/targets.py`, `demo/packages.py` |
| Change overall flow | `demo/pipeline.py` |
| Read run results | `demo_out/.../DemoTestCases/summary.json` |
| View HTML coverage | `demo_out/.../DemoTestCases/coverage/report/index.html` |

---

*Generated for the LLM Unit Test Demo project. For a shorter quick-start, see `README_DEMO.md`.*
