# v1 App Server scope

v1의 첫 비교 단계에서는 v0의 오디오 캡처, Whisper, F8 경계, UI와 요청 대기열을 유지하고 Codex 전송 계층을 다음과 같이 교체했다.

```text
v0: F8 -> 새 codex exec 프로세스
v1: F8 -> 상주 codex app-server의 동일 thread에서 새 turn
```

## 현재 범위

- 앱 시작 시 App Server 프로세스 한 번 실행
- stdio JSONL 전송
- 앱 수명 동안 하나의 ephemeral thread 유지
- Sol, low, Fast mode off
- F8 요청을 기존과 동일하게 순차 처리
- 마지막 요청 이후 새로 전사된 대화만 다음 turn에 전달
- X 버튼 종료 시 App Server 프로세스 종료
- App Server 준비시간, 첫 토큰과 전체 응답시간 기록
- `final_answer` delta를 Answer 창에 도착 즉시 표시
- 앱 시작 시 Whisper 1초 무음 추론을 실행해 모델 워밍업
- Whisper 로드·워밍업·준비시간과 첫 화면 표시시간 기록

## 후속 개선으로 보류

- Codex 답변 생성 중 새 F8이 들어왔을 때 기존 turn 중단
- 연속 질문을 하나의 복합 질문으로 자동 병합
- 앱 장애 후 저장된 thread resume
- 면접 전 이력서·말투·답변 스타일 입력 화면

현재는 새 F8이 들어와도 기존 답변을 중단하지 않는다. 질문 음성은 즉시 캡처하고 기존 대기열에 저장한 뒤, 앞선 Codex turn이 완료되면 같은 thread에 순서대로 전달한다.

## 구현 검증 기록

- App Server 준비: 0.704초
- 첫 turn: 첫 토큰 5.390초, 완료 5.648초, 응답 `READY`
- 두 번째 turn: 첫 토큰 4.622초, 완료 4.910초, 응답 `ORCHID`
- 두 turn의 thread id 일치
- 첫 turn에서만 알려준 코드워드를 두 번째 turn이 기억하여 문맥 유지 확인
- 실제 GTK 앱 종료 후 App Server 자식 프로세스가 남지 않음

이 수치는 동일 질문을 사용한 v0/v1 속도 비교가 아니라 연결과 세션 유지 여부를 확인한 smoke test다.
