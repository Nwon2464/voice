# Codex latency records

v0에서 실행한 Codex 모델·Fast mode 응답시간 기록입니다. 원본 JSONL은 `raw/`에 보존합니다.

## 보존된 결과

| 모델 | Reasoning | Fast mode | 표본 | 평균 | 중앙값 | 비고 |
|---|---:|---:|---:|---:|---:|---|
| `gpt-5.6-luna` | low | on | 3 | 9.430s | 9.647s | 통제 벤치마크 |
| `gpt-5.6-sol` | low | on | 3 | 8.717s | 8.598s | 통제 벤치마크 |
| `gpt-5.6-terra` | low | on | 3 | 7.478s | 6.064s | 통제 벤치마크 |
| `gpt-5.4-mini` | low | on | 3 | 6.786s | 6.771s | 통제 벤치마크 |
| `gpt-5-nano` | low | on | 3 | 실패 | 실패 | 당시 ChatGPT 계정의 Codex에서 지원되지 않음 |
| `gpt-5.6-sol` | low | off | 7 | 8.734s | 6.757s | 실제 세션, 서로 다른 질문·문맥 |

Fast on의 네 모델은 같은 세 질문, 같은 프롬프트, 빈 문맥, 독립적인 `codex exec` 프로세스로 실행했습니다. 따라서 해당 네 모델끼리는 방향성 비교가 가능합니다.

Fast off 기록은 실제 면접 테스트에서 수집했습니다. 질문이 서로 다르고 문맥 크기가 1개에서 11개까지 증가했으며, 20.161초짜리 지연 표본도 포함합니다. 따라서 Fast on/off의 통제된 A/B 결과로 해석하면 안 됩니다. 정확한 비교는 v1에서 동일 질문·동일 문맥·반복 횟수로 다시 수행해야 합니다.

## 원본 파일

- `codex_benchmark_20260808_015941_luna_low_fast.jsonl`
- `codex_benchmark_20260808_020153_sol_low_fast.jsonl`
- `codex_benchmark_20260808_020506_other_models_low_fast.jsonl`
- `codex_live_20260808_sol_low_no_fast.jsonl`

오디오 WAV, 마이크 전사, 장치 이름 등 성능 비교에 필요하지 않은 실제 세션 데이터는 포함하지 않았습니다.

## v1 App Server smoke test

`codex_app_server_smoke_20260808_sol_low_no_fast.jsonl`은 v1 구현 직후 실행한 통합 확인 기록입니다. App Server 준비에는 0.704초가 걸렸고, 두 turn은 동일한 thread id를 사용했습니다. 두 번째 turn이 첫 번째 turn에서만 전달한 코드워드 `ORCHID`를 답해 세션 문맥 유지도 확인했습니다. 이 테스트는 질문과 실행 방식이 v0 벤치마크와 다르므로 속도 A/B 비교에는 사용하지 않습니다.

## v0/v1 동일 오디오 전체 경로 비교

`e2e_v0_v1_controlled_20260808_sol_low_no_fast.jsonl`은 동일한 두 WAV 파일을 v0와 v1에서 각각 시스템 오디오로 재생하고, F8부터 화면 답변 완료까지 측정한 기록입니다. 두 버전 모두 `Whisper small`, `gpt-5.6-sol`, reasoning `low`, Fast mode off를 사용했습니다.

| 버전 | 전송 방식 | 평균 STT | 평균 F8→Codex | 평균 Codex | 평균 F8→화면 |
|---|---|---:|---:|---:|---:|
| v0 | 새 `codex exec --ephemeral` | 1.617s | 2.800s | 8.845s | 11.645s |
| v1 | 상주 App Server의 동일 thread | 1.616s | 3.136s | 4.832s | 7.968s |

이 2회 비교에서 v1은 Codex 구간이 4.013초(45.370%), F8부터 화면 표시까지가 3.678초(31.580%) 짧았습니다. 질문 오디오와 주요 실행 조건은 같지만 생성된 답변 길이와 각 구조가 전달하는 문맥은 완전히 같지 않으므로, 수치는 현재 구현의 전체 경로 비교로 해석합니다.

## v1 스트리밍·Whisper 워밍업

`v1_stream_warmup_e2e_20260808_sol_low_no_fast.jsonl`은 위와 동일한 두 질문 WAV로 답변 스트리밍과 Whisper 사전 워밍업을 검증한 기록입니다. 노트북 스피커가 내부 마이크에 다시 들어오는 변수를 제거하기 위해 마이크를 음소거하여 이어폰 사용 조건을 근사했습니다.

| 항목 | 평균 |
|---|---:|
| Whisper 앱 시작 로드 | 1.617s |
| Whisper 사전 워밍업 | 1.602s |
| F8→Codex | 2.615s |
| Codex→첫 화면 표시 | 2.431s |
| F8→첫 화면 표시 | 5.046s |
| F8→전체 답변 완료 | 7.173s |

한 turn 안에서 스트리밍은 전체 완료보다 평균 2.127초 먼저 답변을 표시했습니다. 변경 전 v1 기록의 화면 표시 평균 7.968초와 비교하면 첫 표시가 2.922초 빨라졌지만, 변경 전에는 내부 마이크가 스피커 음성을 `ME`로 기록했으므로 교차 실행의 F8→Codex 차이에는 작업 대기와 환경 변동도 포함됩니다. Whisper 전사 자체는 1.616초에서 1.601초로 사실상 같았으며, 워밍업의 주효과는 첫 실제 전사 전에 초기 추론을 완료해 두는 것입니다.
