# PowerShell wrapper for yandere-wallpaper
# Usage: .\yandere.ps1 [options]

param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)

# Get script directory
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Load configuration if it exists
$ConfigFile = Join-Path $ScriptDir "configuration.conf"
if (Test-Path $ConfigFile) {
    Get-Content $ConfigFile | Where-Object { $_ -match '^[A-Z_]*=' } | ForEach-Object {
        $line = $_
        $name, $value = $line -split '=', 2
        [System.Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

# Run Python script
$PythonScript = Join-Path $ScriptDir "main.py"
& python $PythonScript @Arguments

exit $LASTEXITCODE
