# rebuild_icon.ps1
# 아이콘을 교체해서 EXE 재빌드
# 사용: .\rebuild_icon.ps1 v2_dongwon_blue

param(
    [ValidateSet("servercheck","logo_A","logo_B","logo_C","logo_D")]
    [string]$IconVersion = "servercheck"
)

$SPEC = "e:\git-copilot\dify-practice\watchdog_gui.spec"
$ICON_PATH = "e:\git-copilot\dify-practice\icons\$IconVersion.ico"
$EXE_PATH  = "e:\git-copilot\dify-practice\dist\DWHF_ChatBot_Monitor.exe"
$PYINSTALLER = "e:\git-copilot\.conda\Scripts\pyinstaller.exe"

Write-Host "선택된 아이콘: $IconVersion"

# spec 파일에서 아이콘 경로 교체
$content = Get-Content $SPEC -Raw
$content = $content -replace 'ICON = r"[^"]+"', "ICON = r`"$ICON_PATH`""
Set-Content $SPEC $content -Encoding UTF8
Write-Host "spec 파일 아이콘 경로 업데이트 완료"

# 빌드
Write-Host "빌드 시작..."
& $PYINSTALLER $SPEC `
    --distpath "e:\git-copilot\dify-practice\dist" `
    --workpath "e:\git-copilot\dify-practice\build" `
    --noconfirm

if ($LASTEXITCODE -eq 0 -and (Test-Path $EXE_PATH)) {
    Write-Host "✅ 빌드 완료: $EXE_PATH"
    Write-Host "   크기: $([math]::Round((Get-Item $EXE_PATH).Length / 1MB, 1)) MB"
} else {
    Write-Host "❌ 빌드 실패"
}
