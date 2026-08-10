# Benchmark 성능 기록

이 문서는 Interview Assistant의 보존된 성능 측정값을 한곳에 요약합니다. 서로 다른 fixture, 문맥, 실행 방식의 결과는 직접적인 A/B 수치로 섞지 않습니다. 시간 단위는 별도 표기가 없으면 초입니다.

## 현재 Moonshine runtime

2026-08-10 실시간 검증 세션의 `question` 로그를 집계했습니다. `commit latency`는 commit trigger 이후 cursor barrier, `FORCE UPDATE`, transcript snapshot을 거쳐 질문이 준비될 때까지입니다. 자동 무음 수치는 이미 1,500ms 무음 조건이 충족된 뒤부터 측정하므로 발화 종료부터의 전체 지연이 아닙니다.

| Commit source | 표본 | 평균 | 중앙값 | 최소 | 최대 |
|---|---:|---:|---:|---:|---:|
| Silence | 17 | 5.2ms | 5.5ms | 1.2ms | 8.9ms |
| F8 new question | 3 | 358.4ms | 300.4ms | 229.9ms | 544.8ms |
| F9 continuation | 2 | 201.9ms | 201.9ms | 186.8ms | 216.9ms |

검증된 모든 표본에서 `cursor_complete=true`, `audio_drop_samples=0`이었습니다. 선택한 세션들의 최대 audio backlog는 545.0ms였습니다. F8/F9은 active line을 확정하는 Moonshine `force_update` 시간이 대부분을 차지하며, silence commit은 보통 이미 완료된 line을 확정합니다.

### 현재 persistent Codex 실사용 표본

동일한 persistent thread에서 `gpt-5.6-sol`, reasoning `low`, Fast `Off`로 완료된 유효 답변 10개를 집계했습니다. 질문과 누적 문맥이 서로 다르므로 현재 운용 분포이며 통제 모델 비교는 아닙니다.

| 구간 | 표본 | 평균 | 중앙값 | 최소 | 최대 |
|---|---:|---:|---:|---:|---:|
| Codex request → first visible | 10 | 2.583 | 2.707 | 1.317 | 3.313 |
| Codex request → completion | 10 | 3.688 | 3.704 | 1.868 | 6.088 |

First-visible 원시값: `3.313, 1.737, 2.646, 2.129, 3.238, 2.767, 2.506, 1.317, 2.935, 3.243`

Completion 원시값: `4.219, 2.712, 3.679, 3.203, 6.088, 4.237, 3.392, 1.868, 3.751, 3.728`

### F9 + Codex 실시간 검증

중간 무음으로 A가 commit된 뒤 B 끝에서 F9으로 A+B를 만든 단일 검증입니다.

| 항목 | 결과 |
|---|---:|
| Silence A commit | 8.8ms |
| F9 trigger → A+B question ready | 216.9ms |
| A+B question ready → request submission | 1ms |
| Request submission → Codex turn start | 161ms |
| A+B question ready → first visible | 4.458s |
| A+B Codex request → first visible | 4.295s |
| A+B Codex request → completion | 5.893s |
| F9 trigger → first visible | 4.675s |
| F9 trigger → completion | 6.273s |
| 최대 audio backlog | 197.6ms |
| Cursor / drop | complete / 0 samples |

이전 generation 1은 실행 중 interrupt됐고, 수정된 generation 2만 화면에 표시됐습니다. generation 1은 첫 출력 전에 종료되어 A의 first-visible/completion은 측정되지 않았습니다. 단일 표본이므로 일반적인 TTFT로 해석하지 않습니다. 이 검증의 목적은 이전 generation supersede와 수정된 A+B 답변의 최종 표시 확인이었습니다.

## Fast mode 짧은 통제 확인

2026-08-10, 동일한 짧은 질문과 developer instructions, `gpt-5.6-sol`, reasoning `low`, 새 App Server thread 조건에서 측정했습니다. STT와 오디오는 포함하지 않았습니다.

### 동일한 3회 대 3회 비교

| Fast | First-visible 원시값 | 평균 | 중앙값 | 최소 | 최대 |
|---|---|---:|---:|---:|---:|
| On | 3.673, 5.503, 3.561 | 4.246 | 3.673 | 3.561 | 5.503 |
| Off | 5.202, 4.780, 4.223 | 4.735 | 4.780 | 4.223 | 5.202 |

| Fast | Completion 원시값 | 평균 | 중앙값 |
|---|---|---:|---:|
| On | 3.910, 5.804, 3.854 | 4.523 | 3.910 |
| Off | 5.579, 5.237, 4.556 | 5.124 | 5.237 |

이 3:3 표본에서는 Fast On의 first-visible 중앙값이 1.108초(23.2%), completion 중앙값이 약 25.3% 짧았습니다.

앞선 UI/runtime smoke의 Fast On 한 표본(7.975초)과 Fast Off 한 표본(5.564초)까지 각각 포함하면 결과는 다음처럼 변합니다.

| Fast | 표본 | First-visible 평균 | 중앙값 | 최소 | 최대 |
|---|---:|---:|---:|---:|---:|
| On | 4 | 5.178 | 4.588 | 3.561 | 7.975 |
| Off | 4 | 4.942 | 4.991 | 4.223 | 5.564 |

따라서 Fast 설정이 실제 App Server에 전달되고 정상 응답하는 것은 확인했지만, 이 작은 표본만으로 항상 더 빠르다고 단정할 수는 없습니다. 네트워크와 서비스 변동을 줄인 더 큰 반복 측정이 필요합니다.

## 모델·설정 smoke 결과

같은 한 문장 답변 요청으로 설정 전달과 실제 응답 성공을 짧게 확인했습니다. 각 항목은 단일 표본이므로 모델 간 성능 순위가 아닙니다.

| 모델 | Effort | Fast | App Server 시작 | First visible | Completion | 결과 |
|---|---|---:|---:|---:|---:|---|
| `gpt-5.6-sol` | low | Off | 0.478 | 5.564 | 5.834 | 정상, reroute 없음 |
| `gpt-5.6-luna` | high | Off | 0.419 | 5.880 | 6.317 | 정상, reroute 없음 |
| `gpt-5.6-sol` | low | On | 0.440 | 7.975 | 8.303 | 정상, reroute 없음 |

## 과거 Codex 모델 벤치마크

2026-08-08의 독립 `codex exec` 기록입니다. Fast On 네 모델은 같은 세 질문, 같은 프롬프트, 빈 문맥으로 실행했으므로 이 표 안에서만 방향성 비교가 가능합니다.

| 모델 | Effort | Fast | 표본 | 평균 completion | 중앙값 | 최소 | 최대 |
|---|---|---:|---:|---:|---:|---:|---:|
| `gpt-5.6-luna` | low | On | 3 | 9.430 | 9.647 | 6.431 | 12.211 |
| `gpt-5.6-sol` | low | On | 3 | 8.717 | 8.598 | 8.415 | 9.139 |
| `gpt-5.6-terra` | low | On | 3 | 7.478 | 6.064 | 5.733 | 10.638 |
| `gpt-5.4-mini` | low | On | 3 | 6.786 | 6.771 | 6.763 | 6.825 |
| `gpt-5-nano` | low | On | 3 | 실패 | 실패 | - | - |

당시 `gpt-5-nano`는 현재 계정의 Codex에서 지원되지 않아 0/3회 성공했습니다.

별도의 실제 `gpt-5.6-sol`, low, Fast Off 세션 7회는 평균 8.734초, 중앙값 6.757초, 최소 6.161초, 최대 20.161초였습니다. 질문이 서로 다르고 문맥이 증가한 기록이므로 위 Fast On 표와의 A/B 비교에는 사용하지 않습니다.

## 과거 v0 / v1 전체 경로 비교

2026-08-08, 같은 WAV 2개, Whisper Small, `gpt-5.6-sol`, low, Fast Off 조건입니다.

| 버전 | Codex 전송 | 평균 STT | 평균 F8→Codex | 평균 Codex | 평균 F8→화면 완료 |
|---|---|---:|---:|---:|---:|
| v0 | 새 `codex exec --ephemeral` | 1.617 | 2.800 | 8.845 | 11.645 |
| v1 | persistent App Server thread | 1.616 | 3.136 | 4.832 | 7.968 |

이 2회 비교에서 v1의 Codex 구간은 4.013초(45.37%), F8부터 화면 완료까지는 3.678초(31.58%) 짧았습니다. 답변 길이와 각 방식이 전달한 문맥은 완전히 같지 않으므로 전체 구현 비교로만 해석합니다.

### v1 스트리밍 출력과 Whisper 워밍업

같은 WAV 2개로 답변 스트리밍과 Whisper 사전 워밍업을 검증한 후속 기록입니다.

| 항목 | 평균 |
|---|---:|
| Whisper 앱 시작 로드 | 1.617 |
| Whisper 사전 워밍업 | 1.602 |
| F8 → Codex request | 2.615 |
| Codex request → first visible | 2.431 |
| F8 → first visible | 5.046 |
| F8 → 전체 답변 완료 | 7.173 |

스트리밍은 전체 완료보다 평균 2.127초 먼저 답변을 표시했습니다. 변경 전 v1의 화면 완료 7.968초와 first-visible 5.046초를 단순 비교하면 2.922초 빠르지만, 후속 실행은 내부 마이크를 음소거해 환경도 달랐습니다.

## 과거 App Server same-thread smoke

App Server 준비 0.704초, 같은 thread의 두 turn은 다음과 같았습니다.

| Turn | First token | Completion |
|---|---:|---:|
| 1 | 5.390 | 5.648 |
| 2 | 4.622 | 4.910 |

두 번째 turn이 첫 turn에서만 전달한 코드워드 `ORCHID`를 답해 persistent thread 문맥 유지가 확인됐습니다. 질문이 달라 다른 표와 성능 비교하지 않습니다.

## 해석 시 주의사항

- 현재 최종 KPI는 F8/F9 질문 확정 이후 Codex first-visible이며, Preview 품질은 별도 지표입니다.
- Moonshine commit 지연과 Codex TTFT는 서로 다른 구간이므로 합산할 때 같은 세션의 타임스탬프만 사용해야 합니다.
- 자동 silence의 1,500ms는 안정성을 위한 정책 지연이며 위 5.2ms commit 평균에 포함되지 않습니다.
- 모델, Fast, persistent-thread 길이 비교는 동일 질문·동일 문맥·충분한 반복 수가 있어야 결론을 낼 수 있습니다.
- 사용자 음성, 네트워크, 서비스 부하와 생성 답변 길이가 결과에 영향을 줍니다.
