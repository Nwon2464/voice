# Interview Assistant v0

Linux GNOME에서 웹 화상면접의 상대방 음성과 내 마이크 음성을 실시간으로 전사하고, F8을 누른 시점의 질문을 Codex에 보내 말하기 쉬운 답변 초안을 표시하는 로컬 데스크톱 도구입니다.

이 저장소의 `v0`는 현재 검증된 기준 버전입니다. Codex 호출 방식은 질문마다 독립적인 `codex exec` 프로세스를 실행합니다. 후속 `v1-app-server` 브랜치에서는 하나의 Codex App Server와 하나의 대화 스레드를 면접 내내 유지하는 방식을 실험합니다.

## 현재 동작

```text
브라우저 출력 음성 ──> PulseAudio monitor ──> Whisper small ──> INTERVIEWER 창
내 마이크 음성 ─────> 기본 입력 장치 ───────> Whisper small ──> ME 창
                                                    │
                              질문 종료 시 F8 ──────┘
                                                    ↓
                                  최근 질문 + 대화 문맥
                                                    ↓
                                  Codex CLI (Sol, low, Fast off)
                                                    ↓
                                            답변 초안 창
```

- 상대방 음성과 내 음성을 별도 창에 표시합니다.
- 세 창은 이동과 가로·세로 크기 조절이 가능하며 항상 위에 표시됩니다.
- Answer 창은 마우스 휠로 전체 내용을 위아래로 이동할 수 있습니다.
- 어느 창의 X 버튼을 눌러도 오디오 캡처와 작업 프로세스를 정리하고 종료합니다.
- F8 연속 오입력은 300ms 안에서는 한 번으로 처리합니다.
- 질문 경계는 F8 시각 주변의 문장부호·무음·반응시간 보정을 사용합니다.

## v0 기본 설정

| 항목 | 값 |
|---|---|
| 운영체제 | Ubuntu 22.04 계열, GNOME/X11 |
| Whisper | `small`, CPU `int8`, 영어 |
| Codex 모델 | `gpt-5.6-sol` |
| Reasoning effort | `low` |
| Fast mode | 끔 |
| Codex 세션 | F8마다 독립적인 ephemeral `codex exec` |
| 테스트 기록 | 기본값 끔 |

## 필요한 프로그램

- Python 3.10 이상
- `ffmpeg`
- `pactl`을 제공하는 `pulseaudio-utils`
- GTK 3 Python 바인딩인 `python3-gi`와 `gir1.2-gtk-3.0`
- 인증을 마친 Codex CLI

Ubuntu에서 시스템 패키지를 설치합니다.

```bash
sudo apt update
sudo apt install ffmpeg pulseaudio-utils python3-venv python3-gi gir1.2-gtk-3.0
```

Codex CLI 설치와 로그인은 [OpenAI 공식 Codex CLI 문서](https://developers.openai.com/codex/cli)를 따릅니다. 설치 후 아래 명령이 정상 동작해야 합니다.

```bash
codex --version
codex
```

## 설치

프로젝트 루트에서 가상환경을 만들고 Python 의존성을 설치합니다.

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

`--system-site-packages`는 Ubuntu가 제공하는 `python3-gi`를 가상환경에서도 사용하기 위해 필요합니다. Whisper 모델은 첫 실행 때 내려받으므로 시간이 걸릴 수 있습니다. 이후에는 로컬 캐시를 사용합니다.

## 실행

먼저 GNOME 설정의 **소리**에서 다음 장치를 선택합니다.

- 출력: 면접에서 실제로 사용할 이어폰 또는 헤드셋
- 입력: 내 목소리를 가장 선명하게 받는 마이크

앱은 실행 시점의 기본 출력 장치 monitor와 기본 입력 장치를 사용합니다. 장치를 바꿨다면 앱을 다시 실행하는 것이 안전합니다.

```bash
./start_interview_app.sh
```

터미널에는 PID와 런타임 로그 경로가 출력됩니다. Whisper가 준비되면 창의 상태가 바뀝니다. 브라우저에서 상대방 음성이 재생되는 것을 확인하고, 질문이 끝났다고 판단한 순간 F8을 한 번 누릅니다.

종료는 별도 스크립트 없이 아무 창의 X 버튼을 누릅니다.

## 창 사용법

### INTERVIEWER

브라우저·화상회의에서 나오는 상대방 음성을 표시합니다. F8 질문 추출의 기준이 되는 음성입니다.

### ME

기본 마이크로 들어오는 내 음성을 별도로 표시합니다. 대화 문맥에는 포함되지만 질문으로 Codex에 직접 전달되지는 않습니다.

### Answer

Codex의 답변 초안을 표시합니다. 상단의 빈 사각형은 카메라 근처의 시선 위치를 의식하기 위한 가이드입니다. 마우스 포인터를 Answer 창 위에 두고 휠을 사용하면 답변 전체가 이동합니다.

## 환경 변수

필요할 때만 실행 명령 앞에 지정합니다.

| 변수 | 기본값 | 용도 |
|---|---|---|
| `INTERVIEW_WHISPER_MODEL` | `small` | Whisper 모델 |
| `INTERVIEW_LANGUAGE` | `en` | 전사 언어 |
| `INTERVIEW_CODEX_MODEL` | `gpt-5.6-sol` | Codex 모델 |
| `INTERVIEW_CODEX_REASONING` | `low` | 추론 강도 |
| `INTERVIEW_VAD_RMS` | `250` | 마이크 음성 감지 민감도 |
| `INTERVIEW_TEST_LOG` | `0` | `1`이면 테스트 음성·JSONL 저장 |
| `INTERVIEW_TEST_LABEL` | 없음 | 테스트 세션 라벨 |

예를 들어 테스트 기록을 활성화하려면 다음과 같이 실행합니다.

```bash
INTERVIEW_TEST_LOG=1 INTERVIEW_TEST_LABEL=manual ./start_interview_app.sh
```

기록은 `test_runs/app_session_<시각>_<PID>/`에 저장됩니다. 여기에 전사 대상 WAV와 `session.jsonl`이 포함될 수 있으므로 실제 면접에서는 개인정보 보호를 위해 기본값인 기록 끔을 유지하십시오. `test_runs/`는 Git에서 제외됩니다.

## 문제 해결

### 상대방 음성이 표시되지 않음

```bash
pactl get-default-sink
pactl list short sources
```

기본 sink에 대응하는 `.monitor` source가 있는지 확인합니다. 브라우저 출력 장치와 시스템 기본 출력 장치가 다르면 캡처되지 않을 수 있습니다.

### 내 음성이 표시되지 않음

```bash
pactl get-default-source
```

GNOME 소리 설정에서 원하는 Internal Microphone 또는 Headset Microphone을 기본 입력으로 선택한 뒤 앱을 재실행합니다.

### F8이 작동하지 않음

앱은 GNOME 사용자 지정 단축키에 F8을 등록합니다. 다른 프로그램이나 기존 GNOME 단축키가 F8을 사용하면 충돌 상태가 로그에 기록됩니다. 현재 전역 단축키는 GNOME 환경에 의존하므로 다른 데스크톱에서는 별도 구현이 필요합니다.

### Codex 답변이 나오지 않음

```bash
codex --version
```

Codex CLI가 `PATH`에 있고 로그인이 유효한지 확인합니다. v0는 60초 안에 응답이 없으면 요청을 실패로 처리합니다. 네트워크와 OpenAI 서비스 상태도 응답시간에 영향을 줍니다.

### 런타임 로그

시작 스크립트가 출력한 경로를 확인합니다. 일반적으로 다음 위치입니다.

```text
$XDG_RUNTIME_DIR/interview-assistant.log
```

## 파일 구조

```text
interview_app.py        GTK UI, 오디오 스트림, Whisper/Codex 작업 관리
audio_utils.py          F8 질문 경계 계산과 JSONL 기록
start_interview_app.sh  백그라운드 실행 진입점
requirements.txt        직접 사용하는 Python 패키지
README.md               설치와 운용 문서
```

`.venv`, Whisper 모델 캐시, 테스트 음성, 세션 로그와 창 위치 설정은 저장소에 포함하지 않습니다. 창 위치는 `~/.config/interview-assistant/window_state.json`에 보관됩니다.

## 알려진 제약

- v0는 Linux GNOME/X11 전용입니다.
- Wayland 또는 다른 데스크톱의 전역 F8은 보장하지 않습니다.
- 브라우저별 음성이 아니라 시스템 기본 출력 전체를 캡처합니다.
- Whisper 전사는 로컬이지만 질문과 최근 대화 문맥은 Codex 응답 생성을 위해 OpenAI 서비스로 전달됩니다.
- F8 시점은 사람의 반응시간과 Whisper 단어 타임스탬프를 보정한 추정 경계입니다.
- v0는 F8마다 새 `codex exec` 프로세스를 실행하므로 대화 세션 자체는 유지하지 않습니다.

## 버전 계획

- `v0` 태그: 현재 검증된 `codex exec` 기준 버전
- `v1-app-server` 브랜치: App Server 상주 프로세스, 단일 thread, 답변 스트리밍과 장애 복구 실험
- 최종 Linux 버전 선정: 동일한 테스트 질문과 로그 지표로 v0/v1 비교
- Windows 포팅: Linux 최종 버전을 확정한 뒤 Windows 오디오 캡처·전역 단축키·UI에 맞게 별도 개발

v0와 v1 비교 시에는 질문 경계 정확도, F8부터 첫 글자까지의 시간, 전체 답변 완료시간, 대화 일관성, 장애 복구 여부를 동일한 조건에서 기록합니다.
