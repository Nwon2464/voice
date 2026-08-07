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
