# Runtime Deployment

## Deployment Mode

- Mode: foreground | scheduled | service-wrapper
- Command: python scripts/master_agent_tool.py supervisor-start --state-dir docs/master-agent --poll-seconds 60 --spawn
- Working directory:

## Windows Startup

- Task Scheduler name:
- Startup trigger:
- Restart policy:
- Log path:

## Process Identity

- PID:
- Supervisor identity:
- Lock file: state/supervisor.lock
- Started at:
- Liveness check: supervisor-status verifies recorded PID plus supervisor identity before reporting running.

## Crash Recovery

- Stale lock threshold:
- Crash marker:
- Recovery command: python scripts/master_agent_tool.py supervisor-recover --state-dir docs/master-agent
- Forced recovery: allowed only after independently confirming the recorded process must be overridden.
- Identity mismatch action: treat as not running and investigate before forced recovery.

## Stop And Status

- Status command:
- Stop command:
- Graceful stop file:

## Operator Override

- Manual stop:
- Manual recovery:
- Manual conflict handling:

## Production Limits

- Maximum cycles:
- Maximum repeated remediation:
- Maximum restart attempts:
