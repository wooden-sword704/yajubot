$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
try {
    firebase deploy --only hosting
}
finally {
    Pop-Location
}
