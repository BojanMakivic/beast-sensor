$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetDirectoryName($MyInvocation.MyCommand.Path)
$venvPath = [System.IO.Path]::Combine($projectRoot, ".venv")
$python = [System.IO.Path]::Combine($venvPath, "Scripts", "python.exe")
$requirements = [System.IO.Path]::Combine($projectRoot, "requirements.txt")

& py -3.13 --version
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.13 was not found. Install it from python.org with the Python Launcher."
}

& py -3.13 -m venv $venvPath
if ($LASTEXITCODE -ne 0) { throw "Could not create the virtual environment." }

& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Could not upgrade pip." }

& $python -m pip install -r $requirements
if ($LASTEXITCODE -ne 0) { throw "Could not install project dependencies." }

[System.Console]::WriteLine("")
[System.Console]::WriteLine("Setup complete. Start the tracker with:")
[System.Console]::WriteLine("  .\run.ps1")