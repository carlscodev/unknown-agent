# Reproducible benchmarks

Unknown Agent includes a durable benchmark harness for performance and reliability regression gates. It executes argument-vector subprocesses directly—never through a shell—under a bounded, minimal environment.

## Suite format

```yaml
name: cli-smoke
scenarios:
  - name: version
    command: [ua, version]
    warmups: 1
    repetitions: 5
    timeout_seconds: 30
    expected_exit_code: 0
    stdout_contains: [Unknown Agent]
```

Suite and scenario schemas reject unknown fields. A scenario supports at most 20 measured repetitions, five warmups, 64 command arguments, and a 300-second timeout. Standard output and error are capped at 1 MiB and secret-redacted before SQLite persistence.

## Run and inspect

```bash
ua benchmark run benchmarks/cli.yaml --revision "$(git rev-parse HEAD)"
ua benchmark show RUN_ID
```

Warmups are executed but never included in statistics. Measured samples persist duration, exit status, timeout status, assertions, and bounded output in `.unknown/benchmarks.db`. Summaries report pass rate, median duration, and nearest-rank p95 duration. A failed scenario produces a non-zero CLI exit code.

## Regression gate

```bash
ua benchmark compare BASELINE_RUN CANDIDATE_RUN --threshold 10
```

Comparison requires identical suite and scenario identities. The command fails when median or p95 latency increases beyond the threshold, or when pass rate drops beyond it. Reports are deterministic JSON in machine-output mode and include every metric decision rather than only failed values.