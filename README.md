# Islands Build Automation

Python 3 CLI to build and run the Islands workspace.

## Requirements

- Python 3.8+
- Apache Maven (`mvn` on PATH)
- Java JDK (`java` on PATH)
- *(optional)* `rich` for coloured output: `pip install rich`

## Project structure

```
Build/
  build.py      ← CLI entry point
  config.py     ← all paths & artifact names
  logger.py     ← coloured logging helpers
  maven.py      ← Maven subprocess wrapper
  fs.py         ← file-system helpers (copy, mkdir, write JSON)
  runner.py     ← high-level run configurations
  output/       ← generated at runtime
    config.json         (CoffeeLoader runtime config)
    CoffeeLoader-*.jar  (runner)
    modules/
      islands-*.jar     (Islands module)
```

## Commands

### `build-all`
Build every project in dependency order (`ModularKit → CoffeeLoader → Islands`).

```bash
python build.py build-all                # tests skipped
python build.py build-all --with-tests   # run unit tests
python build.py build-all --verbose      # stream Maven output
```

### `run-islands`
Full pipeline: build all projects, assemble the `output/` directory,
write the CoffeeLoader `config.json`, then **launch CoffeeLoader** (blocks until Ctrl+C).

```bash
python build.py run-islands
python build.py run-islands --with-tests       # run tests before launching
python build.py run-islands --no-clean         # keep existing output dir
python build.py run-islands --verbose          # stream Maven output
python build.py run-islands --java-opts "-Xmx512m"
```

What this does step by step:
1. `mvn clean install` → ModularKit
2. `mvn clean install` → CoffeeLoader
3. `mvn clean install` → Islands
4. Create `output/` and `output/modules/`
5. Copy `CoffeeLoader-*-jar-with-dependencies.jar` → `output/`
6. Copy `islands-*.jar` → `output/modules/`
7. Write `output/config.json` with `sources` pointing to `output/modules/`
8. `java -jar output/CoffeeLoader-*.jar` (runs until Ctrl+C)

### `assemble`
Assemble the output directory from **already-built** artifacts (no Maven build).

```bash
python build.py assemble
python build.py assemble --no-clean
```

### `clean`
Delete the `output/` directory.

```bash
python build.py clean
```

### `status`
Show whether each project's Maven artifact exists on disk.

```bash
python build.py status
```

### `info`
Print all resolved workspace and output paths.

```bash
python build.py info
```
