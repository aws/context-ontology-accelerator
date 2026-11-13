# supply_chain — third-party NOTICE generation

Generates the root `NOTICE` that attributes every redistributed third-party
component with its version and license, satisfying the Apache-2.0 §4(d)
attribution obligation.

Stdlib-only: the inventory is read from the installed environment via
`importlib.metadata`, so it runs in any build without an extra scanner
dependency.

## Usage

```bash
# Regenerate the root NOTICE from the resolved environment
uv run python -m scripts.supply_chain.cli notice

# Verify the committed NOTICE is current (non-zero exit if stale/missing)
uv run python -m scripts.supply_chain.cli notice --check
```

`make build` runs the `notice` target, so the checked-in `NOTICE` is regenerated
(and re-committed through the normal dev flow) whenever the dependency set moves.
There is no separate CI freshness gate — freshness is a build side effect.

## Modules

- `licenses.py` — enumerate installed third-party distributions and normalize each
  declared license to a canonical SPDX token. First-party workspace packages are
  excluded; a short, cited `REVIEWED_OVERRIDES` map covers deps whose metadata
  omits/malforms the license.
- `notice.py` — render the NOTICE body from the inventory.
- `cli.py` — `notice` subcommand (write / `--check`).
