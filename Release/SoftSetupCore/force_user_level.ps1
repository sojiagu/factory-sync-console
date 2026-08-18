# SoftwarePack writes RequestExecutionLevel admin; switch back to user before makensis.
$ErrorActionPreference = "Stop"
$nsi = Join-Path $PSScriptRoot "SetupScripts\runtime\setup.nsi"
if (-not (Test-Path -LiteralPath $nsi)) {
    Write-Error ("setup.nsi not found: " + $nsi)
    exit 1
}
$enc = New-Object System.Text.UnicodeEncoding $false, $true
$t = [IO.File]::ReadAllText($nsi, $enc)
if ($t -notmatch "RequestExecutionLevel") {
    Write-Error "setup.nsi has no RequestExecutionLevel"
    exit 1
}
$t2 = $t.Replace("RequestExecutionLevel admin", "RequestExecutionLevel user")
if ($t2 -notmatch "RequestExecutionLevel user") {
    Write-Error "failed to set RequestExecutionLevel user"
    exit 1
}
[IO.File]::WriteAllText($nsi, $t2, $enc)
Write-Host "RequestExecutionLevel -> user"
exit 0
