Write-Host "Configuring repository to use .githooks as git hooks path..."
git config core.hooksPath .githooks
Write-Host "Done. To remove: git config --unset core.hooksPath"
