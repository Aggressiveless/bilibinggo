param(
    [string]$Profile = (Join-Path $PSScriptRoot '..\..\work\binggo-control-profile.json')
)
$ErrorActionPreference = 'Stop'
$profilePath = (Resolve-Path -LiteralPath $Profile).Path
$settings = Get-Content -LiteralPath $profilePath -Raw -Encoding UTF8 | ConvertFrom-Json
$codexCommand = Get-Command codex -ErrorAction Stop
& $codexCommand.Source mcp add binggo `
    --env "PYTHONPATH=$($settings.source_root)\mcp" `
    --env "BINGGO_CONTROL_PROFILE=$profilePath" `
    --env 'PYTHONIOENCODING=utf-8' `
    -- $settings.mcp_python -m binggo_mcp
if ($LASTEXITCODE -ne 0) { throw 'Codex MCP registration failed. Existing Binggo account data was not changed.' }
Write-Output 'Binggo MCP registered. Reload MCP servers or restart ChatGPT/Codex to load the tools.'
