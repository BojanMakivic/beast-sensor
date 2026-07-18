$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetDirectoryName($MyInvocation.MyCommand.Path)
$venvPath = [System.IO.Path]::Combine($projectRoot, ".venv")
$python = [System.IO.Path]::Combine($venvPath, "Scripts", "python.exe")
$requirements = [System.IO.Path]::Combine($projectRoot, "requirements.txt")

function Get-Python313 {
    $candidates = [System.Collections.Generic.List[object]]::new()

    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $launcher) {
        $candidates.Add([PSCustomObject]@{
            Command = $launcher.Source
            Args = @("-3.13")
            Display = "py -3.13"
        })
    }

    $defaultPython313 = "C:\Python313\python.exe"
    if ([System.IO.File]::Exists($defaultPython313)) {
        $candidates.Add([PSCustomObject]@{
            Command = $defaultPython313
            Args = @()
            Display = $defaultPython313
        })
    }

    foreach ($pathPython in (Get-Command python -All -ErrorAction SilentlyContinue)) {
        $candidates.Add([PSCustomObject]@{
            Command = $pathPython.Source
            Args = @()
            Display = $pathPython.Source
        })
    }

    foreach ($candidate in $candidates) {
        try {
            $candidateCommand = $candidate.Command
            $candidateArgs = $candidate.Args
            $version = & $candidateCommand @candidateArgs -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
            if ($LASTEXITCODE -eq 0 -and $version -eq "3.13") {
                return $candidate
            }
        } catch {
            continue
        }
    }

    throw "Python 3.13 was not found. Install Python 3.13 from python.org or add it to PATH."
}

$basePython = Get-Python313
[System.Console]::WriteLine("Using Python 3.13 from: $($basePython.Display)")

if (-not [System.IO.File]::Exists($python)) {
    $basePythonCommand = $basePython.Command
    $basePythonArgs = $basePython.Args
    & $basePythonCommand @basePythonArgs -m venv $venvPath
    if ($LASTEXITCODE -ne 0) { throw "Could not create the virtual environment." }
} else {
    [System.Console]::WriteLine("Using existing virtual environment: $venvPath")
}

& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Could not upgrade pip." }

& $python -m pip install -r $requirements
if ($LASTEXITCODE -ne 0) { throw "Could not install project dependencies." }

[System.Console]::WriteLine("")
[System.Console]::WriteLine("Setup complete. Start the tracker with:")
[System.Console]::WriteLine("  .\run.ps1")
