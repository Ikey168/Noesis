# Local-first CLI

Install the small local stack and initialize a private workspace:

```console
python -m pip install -e ".[minimal]"
noesis init --non-interactive
noesis doctor
```

Ingest and ask without Docker, cloud credentials, or an API key:

```console
noesis ingest examples/quickstart/moon-mission.md --domain local
noesis ask "What was the mission result?" --domain local
```

Machine consumers use `--format json` for answers and briefs, or `--json` for
lifecycle commands. JSON stdout is reserved for the documented contract;
diagnostic/progress text goes to stderr.

Exporting a private local answer is deliberately explicit:

```console
noesis export answer \
  --domain local \
  --question "What was the mission result?" \
  --include-private \
  --output answer.bundle.json
noesis verify answer.bundle.json
```

Claim Watch polling persists its opaque cursor by default:

```console
noesis watch create --domain local --type topic --value mission --json
noesis watch poll WATCH_ID --json
noesis watches --domain local --json
noesis watch pause WATCH_ID
noesis watch resume WATCH_ID
noesis watch delete WATCH_ID --yes
```

Use `--cursor-file PATH` to control cursor storage or `--no-save-cursor` for a
read without persistence. An explicit `--cursor` overrides the saved value.

The supported server launchers validate the local config and report the bind
address, auth posture, and enabled surface before starting:

```console
python -m pip install -e ".[server]"
noesis serve --surface api
noesis serve --surface kb-mcp --transport http --port 8100
```

`noesis serve --dry-run --json` validates and reports configuration without
binding a socket. See [the CLI contract](../../contracts/noesis-cli-v1.md) for
output shapes, exit codes, privacy defaults, and compatibility policy.
