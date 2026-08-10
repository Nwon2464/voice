# Moonshine Small 최소 Runtime

## 구현 범위

이번 단계는 기존 GNOME UI, Codex App Server, 세션과 답변 표시 흐름을 유지하고 ASR runtime만 다음 구조로 연결한다.

```text
FFmpeg / PulseAudio monitor
  → 16kHz mono s16le raw PCM + absolute sample cursor
  → MoonshineStreamingWorker queue
  → persistent Moonshine Small model / streaming transcript
  → transcript.lines mirror
  → 기존 INTERVIEWER 창

F8
  → capture lock 안에서 absolute target cursor 기록
  → 같은 lock 안에서 snapshot request enqueue
  → worker가 target cursor까지 PCM을 순서대로 소비
  → FORCE UPDATE
  → transcript snapshot
  → question text
  → 기존 Codex/session/Answer 흐름
```

## 고정 사항

- capture thread와 GTK thread에서는 Moonshine inference를 실행하지 않는다.
- PCM은 capture에서 worker로 한 번만 전달한다.
- cursor 단위는 16kHz absolute sample index다.
- F8 target은 그 순간의 cursor이며 post-context를 추가하지 않는다.
- `captured == queued == consumed == target`일 때만 snapshot을 확정한다.
- Preview는 500ms update interval의 `transcript.lines` mirror를 줄바꿈으로 표시한다.
- 질문 text는 같은 snapshot의 non-empty line text를 공백으로 합친 값이다.
- F8 확정 후 모델은 유지하고 stream만 새로 생성해 다음 질문 transcript와 분리한다.
- word timestamp, boundary resolver, Whisper fallback은 이번 runtime에 연결하지 않는다.

## 계측

각 `question` JSONL event에는 다음을 기록한다.

- `captured_sample_cursor`
- `queued_sample_cursor`
- `consumed_sample_cursor`
- `target_sample_cursor`
- `cursor_complete`
- `audio_drop_samples`
- `max_backlog_ms`
- `barrier_wait_ms`
- `force_update_ms`
- `f8_to_question_ms`
- 확정된 `transcript_lines`

## 변경 파일

- `moonshine_streaming_worker.py`: persistent model, stream, PCM queue, cursor barrier, FORCE UPDATE
- `interview_app.py`: 기존 capture/F8/INTERVIEWER 창/Codex 흐름과 worker 연결
- `tests/test_moonshine_streaming_worker.py`: worker 순서, barrier, snapshot과 stream reset 검증
- `tests/test_interview_app.py`: raw PCM 단일 전달, atomic F8 cursor, 질문/Codex 단일 commit 검증
- `requirements.txt`: `moonshine-voice==0.0.69`

## 현재 의도적으로 제외한 것

- word timestamp와 PCM boundary alignment
- completed/active line overlap de-dup
- F8 후 600ms post-context
- Whisper Preview/Final fallback
- UI 재설계

종료된 Whisper/VAD/replay 구현과 PoC 코드는 제거했다. 이 문서와 현재 runtime 코드가 초기 개발 기준선이다.
