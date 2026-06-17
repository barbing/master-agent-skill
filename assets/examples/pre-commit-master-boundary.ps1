param(
    [string]$ProjectRoot = ".",
    [string]$StateDir = "docs/master-agent"
)

$ErrorActionPreference = "Stop"

python scripts/master_agent_tool.py enforce-master-boundary `
    --project-root $ProjectRoot `
    --state-dir $StateDir

