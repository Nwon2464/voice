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
- 면접 통합 제어창의 X 버튼 종료 시 App Server 프로세스 종료
- App Server 준비시간, 첫 토큰과 전체 응답시간 기록
- `final_answer` delta를 Answer 창에 도착 즉시 표시
- 앱 시작 시 Whisper 1초 무음 추론을 실행해 모델 워밍업
- Whisper 로드·워밍업·준비시간과 첫 화면 표시시간 기록
- 앱이 만든 영속 thread를 시작 화면에서 생성·선택
- ↑/↓와 Enter로 세션 선택, `세션 삭제`로 Codex 보관함 이동
- 선택한 thread를 `thread/resume`으로 재개
- 선택 후 기존 turn 이력을 표시하는 준비 채팅 제공
- 준비 채팅의 입력과 Codex 스트리밍 답변을 같은 thread에 저장
- 준비 채팅에서 정한 답변 형식을 F8 turn이 그대로 이어서 사용
- 면접 중 마이크와 ME 전사를 사용하지 않고 직전 Codex 답변을 지원자가 말한 것으로 간주
- INTERVIEWER 전사만 Whisper 작업 대기열을 사용해 ME 전사로 인한 질문 지연 제거
- `small` 미리보기를 별도 프로세스로 실행하고 F8 순간 종료해 최종 `small` 전사가 CPU를 단독 사용
- 미리보기 프로세스 통신에 Pipe를 사용해 반복 종료 시 semaphore 누적 방지
- 질문 확정 후 미리보기 프로세스를 자동 재시작하고 준비·취소시간을 세션 로그에 기록
- 종료 신호를 최종 전사 큐 뒤에 배치하고 작업 스레드에서 즉시 발화 로그를 기록해 마지막 WAV와 이벤트 수 일치
- 비어 있지 않은 최신 질문 STT가 확정되면 실행 중인 이전 Codex turn을 중단하고 대기 요청을 최신 하나로 압축
- superseded generation의 늦은 stream·완료 callback을 무시하고 해당 답변을 `NOT SPOKEN`으로 기록
- timeout·프로세스 종료·EOF·broken pipe 발생 시 App Server를 정리하고 같은 persistent thread를 resume한 뒤 최신 질문을 1회 재시도
- recovery 재시도도 실패하면 Codex만 unavailable로 표시하고 오디오·Whisper·F8 처리는 계속 유지
- 이동 가능한 면접 통합 제어창의 뒤로가기로 오디오를 정리하고 준비 채팅 복귀

## 후속 개선으로 보류

- 연속 질문을 하나의 복합 질문으로 자동 병합
- GTK가 초기화된 부모에서 미리보기 spawn을 반복하면 프로세스당 semaphore 하나가 종료 시점까지 추적되는 `resource_tracker` 경고가 발생한다. 일반적인 개인 면접에서는 기능 장애가 없고 앱 완전 종료 시 정리되므로 현재 구조를 유지하되, 배포 전에는 `multiprocessing` 대신 stdin/stdout 기반 일반 subprocess로 교체할지 검토한다.

질문 음성과 interviewer transcript는 모두 보존한다. 다만 비어 있지 않은 최신 질문의 최종 STT가 확정되면 이전 Codex 답변을 `superseded / NOT SPOKEN`으로 표시하고 실행 중인 turn을 중단한다. 대기 중인 질문이 여러 개면 가장 최신 generation 하나만 같은 thread에서 이어서 실행한다.

Codex stdio transport가 끊기거나 turn이 timeout되면 현재 App Server 프로세스를 종료하고 선택된 persistent `thread_id`로 새 App Server를 resume한다. 실패한 최신 질문은 부분 출력이 실제 발화되지 않았다는 recovery 지시와 함께 최대 한 번만 다시 보낸다. 두 번째 시도도 실패하면 `Codex unavailable`을 표시하지만 앱과 음성 처리는 종료하지 않으며, 다음 valid 질문에서 새 복구를 다시 시도할 수 있다.

## 구현 검증 기록

- App Server 준비: 0.704초
- 첫 turn: 첫 토큰 5.390초, 완료 5.648초, 응답 `READY`
- 두 번째 turn: 첫 토큰 4.622초, 완료 4.910초, 응답 `ORCHID`
- 두 turn의 thread id 일치
- 첫 turn에서만 알려준 코드워드를 두 번째 turn이 기억하여 문맥 유지 확인
- 실제 GTK 앱 종료 후 App Server 자식 프로세스가 남지 않음

이 수치는 동일 질문을 사용한 v0/v1 속도 비교가 아니라 연결과 세션 유지 여부를 확인한 smoke test다.
