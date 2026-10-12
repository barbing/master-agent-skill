# Pilot artifacts

These files come from two completed runtime pilots and are kept byte-for-byte as evidence. They are not part of the regression suite in `tests/`; new checks belong there. Keep this folder one level below the repository root, because `owned_cli_test.py` points its subprocess `PYTHONPATH` at the folder above its own.

| File | Pilot | Role |
| --- | --- | --- |
| `acceptance/oracle.py` | Store/CLI workflow | Frozen acceptance oracle. Its sha256, `fbadcbe7107d859e2041803c3b8ca315ccef560822bdaec82979c46d01063b4b`, is the one the pilot's freeze record binds. |
| `owned_store_test.py`, `owned_cli_test.py` | Store/CLI workflow | Tests written by the pilot's module owners. They exercise the current `Store` and runtime CLI. |
| `runtime_live_oracle.py` | Admission/health | Frozen oracle for the live module-delivery pilot. A standalone script, not a unittest module. |

Rerun the owner tests from the repository root:

```bash
python -B -m unittest discover -s pilots -p 'owned_*_test.py' -v
```

This reports 20 executions: the owners' 12 tests, plus the oracle's 4 Store checks twice, because `owned_store_test.py` both imports and subclasses `StoreReadOnlyAcceptance`.

Both oracles also run standalone and make no model calls. The Store/CLI oracle overlays CANDIDATE on a private copy of CONTEXT. The admission/health oracle creates its temporary folders in the current directory, so run it from a scratch directory.

```bash
python -B pilots/acceptance/oracle.py CONTEXT CANDIDATE store|cli|integration
python -B pilots/runtime_live_oracle.py CANDIDATE CONTEXT admission|health|integration
```
