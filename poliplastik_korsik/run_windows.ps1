param(
  [Parameter(Mandatory=$true)][string]$InputPath,
  [string]$OutputPath = "data\ingested",
  [string]$TesseractPath = "C:\Program Files\Tesseract-OCR\tesseract.exe",
  [string]$Reviews = ""
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$PythonPath = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (!(Test-Path $PythonPath)) {
  throw "Create the environment first: py -3.12 -m venv .venv; then install requirements-tested.txt"
}
$RunArgs = @("ingest.py", "--input", $InputPath, "--output", $OutputPath, "--tesseract-path", $TesseractPath)
if ($Reviews) { $RunArgs += @("--reviews", $Reviews) }
& $PythonPath @RunArgs
$ResultCode = $LASTEXITCODE
if ($ResultCode -eq 2) { Write-Host "Extraction contains blocked segments. See report.json in your output folder; --audit is available for diagnostics." }
exit $ResultCode
