# Windows 작업 스케줄러에 배민 크롤링 등록
# 관리자 권한으로 실행 필요

$TASK_NAME  = "DW_BaeminCrawl"
$PS_SCRIPT  = "e:\git-copilot\dify-practice\run_baemin_crawl.ps1"
$TIMES      = @("09:00", "11:00", "14:00", "17:00")

# 기존 태스크 삭제 (재등록)
Unregister-ScheduledTask -TaskName $TASK_NAME -Confirm:$false -ErrorAction SilentlyContinue

# 액션: PowerShell 실행
$ACTION = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NonInteractive -ExecutionPolicy Bypass -File `"$PS_SCRIPT`"" `
    -WorkingDirectory "e:\git-copilot\dify-practice"

# 설정: 이미 실행 중이면 새 인스턴스 실행 안 함
$SETTINGS = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -StartWhenAvailable `
    -WakeToRun:$false

# 트리거 4개 생성 후 XML로 직접 등록 (멀티 트리거는 XML 방식이 안정적)
$XML = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <CalendarTrigger><StartBoundary>2026-08-21T09:00:00</StartBoundary><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>
    <CalendarTrigger><StartBoundary>2026-08-21T11:00:00</StartBoundary><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>
    <CalendarTrigger><StartBoundary>2026-08-21T14:00:00</StartBoundary><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>
    <CalendarTrigger><StartBoundary>2026-08-21T17:00:00</StartBoundary><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <ExecutionTimeLimit>PT3H</ExecutionTimeLimit>
    <StartWhenAvailable>true</StartWhenAvailable>
    <WakeToRun>false</WakeToRun>
    <Enabled>true</Enabled>
  </Settings>
  <Actions>
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>-NonInteractive -ExecutionPolicy Bypass -File "$PS_SCRIPT"</Arguments>
      <WorkingDirectory>e:\git-copilot\dify-practice</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

$XML_FILE = "$env:TEMP\DW_BaeminCrawl.xml"
[System.IO.File]::WriteAllText($XML_FILE, $XML, [System.Text.Encoding]::Unicode)
schtasks /Create /TN $TASK_NAME /XML $XML_FILE /F
Remove-Item $XML_FILE -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "완료: '$TASK_NAME' 등록됨"
Write-Host "  실행 시간: $($TIMES -join ', ')"
Write-Host "  스크립트: $PS_SCRIPT"
Write-Host ""
Write-Host "확인: schtasks /Query /TN $TASK_NAME /FO LIST"
Write-Host "수동 실행: schtasks /Run /TN $TASK_NAME"
