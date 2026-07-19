$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetDirectoryName($MyInvocation.MyCommand.Path)
$venvPath = [System.IO.Path]::Combine($projectRoot, ".venv")
$python = [System.IO.Path]::Combine($venvPath, "Scripts", "python.exe")
$requirements = [System.IO.Path]::Combine($projectRoot, "requirements.txt")
$dashboardComponent = [System.IO.Path]::Combine(
    $projectRoot,
    "components",
    "agile-vbt-live-display"
)
$pythonVersionFile = [System.IO.Path]::Combine($projectRoot, ".python-version")

if ([System.IO.File]::Exists($pythonVersionFile)) {
    $requiredPythonVersion = ([System.IO.File]::ReadAllText($pythonVersionFile)).Trim()
} else {
    $requiredPythonVersion = "3.13"
}

function Add-PythonCandidate {
    param (
        [System.Collections.Generic.List[object]] $Candidates,
        [string] $Command,
        [string[]] $Args,
        [string] $Display
    )

    $Candidates.Add([PSCustomObject]@{
        Command = $Command
        Args = $Args
        Display = $Display
    })
}

function Get-PythonVersion {
    param (
        [string] $Command,
        [string[]] $Args
    )

    $version = & $Command @Args -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    if ($LASTEXITCODE -ne 0) {
        return $null
    }

    return $version
}

function Get-RequiredPython {
    param (
        [string] $RequiredVersion
    )

    $candidates = [System.Collections.Generic.List[object]]::new()
    $versionNoDot = $RequiredVersion.Replace(".", "")

    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $launcher) {
        Add-PythonCandidate $candidates $launcher.Source @("-$RequiredVersion") "py -$RequiredVersion"
    }

    foreach ($commandName in @("python$RequiredVersion", "python$versionNoDot", "python3", "python")) {
        foreach ($pathPython in (Get-Command $commandName -All -ErrorAction SilentlyContinue)) {
            Add-PythonCandidate $candidates $pathPython.Source @() $pathPython.Source
        }
    }

    $commonInstallPaths = [System.Collections.Generic.List[string]]::new()
    $commonInstallPaths.Add("C:\Python$versionNoDot\python.exe")

    if (-not [string]::IsNullOrWhiteSpace($env:LocalAppData)) {
        $commonInstallPaths.Add([System.IO.Path]::Combine($env:LocalAppData, "Programs", "Python", "Python$versionNoDot", "python.exe"))
    }
    if (-not [string]::IsNullOrWhiteSpace($env:ProgramFiles)) {
        $commonInstallPaths.Add([System.IO.Path]::Combine($env:ProgramFiles, "Python$versionNoDot", "python.exe"))
    }

    $programFilesX86 = [Environment]::GetEnvironmentVariable("ProgramFiles(x86)")
    if (-not [string]::IsNullOrWhiteSpace($programFilesX86)) {
        $commonInstallPaths.Add([System.IO.Path]::Combine($programFilesX86, "Python$versionNoDot", "python.exe"))
    }

    foreach ($installPath in $commonInstallPaths) {
        if ([System.IO.File]::Exists($installPath)) {
            Add-PythonCandidate $candidates $installPath @() $installPath
        }
    }

    foreach ($candidate in $candidates) {
        try {
            $candidateCommand = $candidate.Command
            $candidateArgs = $candidate.Args
            $version = Get-PythonVersion $candidateCommand $candidateArgs
            if ($version -eq $RequiredVersion) {
                return $candidate
            }
        } catch {
            continue
        }
    }

    throw "Python $RequiredVersion was not found. Install Python $RequiredVersion from python.org or add it to PATH."
}

$basePython = Get-RequiredPython $requiredPythonVersion
[System.Console]::WriteLine("Using Python $requiredPythonVersion from: $($basePython.Display)")

if (-not [System.IO.File]::Exists($python)) {
    $basePythonCommand = $basePython.Command
    $basePythonArgs = $basePython.Args
    & $basePythonCommand @basePythonArgs -m venv $venvPath
    if ($LASTEXITCODE -ne 0) { throw "Could not create the virtual environment." }
} else {
    $venvPythonVersion = Get-PythonVersion $python @()
    if ($venvPythonVersion -ne $requiredPythonVersion) {
        throw "The existing .venv uses Python $venvPythonVersion, but this project requires Python $requiredPythonVersion. Remove .venv and run .\setup.ps1 again."
    }

    [System.Console]::WriteLine("Using existing virtual environment: $venvPath")
}

& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Could not upgrade pip." }

& $python -m pip install -r $requirements
if ($LASTEXITCODE -ne 0) { throw "Could not install project dependencies." }

& $python -m pip install --editable $dashboardComponent
if ($LASTEXITCODE -ne 0) {
    throw "Could not install the Agile VBT dashboard component."
}

[System.Console]::WriteLine("")
[System.Console]::WriteLine("Setup complete. Start the tracker with:")
[System.Console]::WriteLine("  .\run.ps1")
