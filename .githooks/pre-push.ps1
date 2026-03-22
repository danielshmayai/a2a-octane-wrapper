Write-Host "Running test suite before push..."

$python = 'python'
if (-not (Get-Command $python -ErrorAction SilentlyContinue)) {
  if (Get-Command 'python3' -ErrorAction SilentlyContinue) { $python = 'python3' }
}

$proc = Start-Process -FilePath $python -ArgumentList '-m','pytest','-q' -NoNewWindow -Wait -PassThru
if ($proc.ExitCode -ne 0) {
  Write-Host 'Tests failed. Aborting push.' -ForegroundColor Red
  exit $proc.ExitCode
}

Write-Host 'All tests passed.'
