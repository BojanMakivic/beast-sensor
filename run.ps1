$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetDirectoryName($MyInvocation.MyCommand.Path)
$python = [System.IO.Path]::Combine($projectRoot, ".venv", "Scripts", "python.exe")
$tracker = [System.IO.Path]::Combine($projectRoot, "beast sensor.py")

if (-not [System.IO.File]::Exists($python)) {
    throw "The .venv environment is missing. Run .\setup.ps1 first."
}

& $python $tracker @args
