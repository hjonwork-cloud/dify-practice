# 동원홈푸드 SAP Bridge Agent

## 설치 방법 (영업사원 PC)

1. `SAPBridgeAgent.exe` 와 `setup.bat` 를 같은 폴더에 저장
2. `setup.bat` 더블클릭 → 설치 자동 완료
3. 이후 PC 켤 때 에이전트 자동 실행됨

## 수동 실행

- `SAPBridgeAgent.exe` 더블클릭

## 동작 설명

- 포털(https://...)에서 "판가 적용 후 DM 발송" 클릭 시 이 에이전트가 SAP GUI 를 자동 조작합니다.
- 에이전트가 실행 중이 아니면 "SAP Bridge Agent 미실행" 오류가 표시됩니다.
- 에이전트는 https://localhost:7788 에서 대기합니다.

## 처음 실행 시

인증서를 자동으로 생성하고 Windows 신뢰 저장소에 등록합니다.  
별도 작업 없이 포털에서 바로 사용 가능합니다.

## 문의

IT 담당자에게 문의하세요.
