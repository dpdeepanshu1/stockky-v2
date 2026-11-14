# Round 532 (also removes legacy.css, folded into base.css). Round 531: robocopy /E keeps files that were deleted in the release, so remove the 10 folded CSS layers by hand.
# Run from the repo root (D:\stockky-v2). Safe to run twice.
$src = Join-Path $PSScriptRoot "..\frontend\src"
$old = "index.css","design-v3.css","design-v4.css","design-v5.css","design-v6.css","design-v7.css","design-v8.css","auth-login.css","light-contrast.css","alignment.css"
foreach ($f in $old) { $p = Join-Path $src $f; if (Test-Path $p) { Remove-Item $p; "removed $f" } else { "already gone $f" } }

$leg = Join-Path $src "styles\legacy.css"
if (Test-Path $leg) { Remove-Item $leg; "removed styles\legacy.css" } else { "already gone styles\legacy.css" }
