# Interview Assistant

화상면접에서 상대방의 시스템 출력 음성을 실시간으로 자막화하고, 질문이 끝나면 Codex가 바로 말할 수 있는 답변 초안을 보여주는 Linux GNOME용 도구입니다.

- 영어/일본어 면접 음성을 지원하며, Preparation 화면에서 언어를 선택합니다.
- 영어는 Moonshine Small Streaming, 일본어는 Moonshine Base 모델로 실시간 자막화합니다.
- F7은 현재 transcript를 context checkpoint로 주입하고 새 ASR stream을 시작합니다.
- F8은 마지막 F7 이후 transcript만 새 질문으로 확정합니다.
- F9으로 직전 질문에 이어진 발화를 합쳐 질문을 교정합니다.
- Codex 답변은 별도의 Answer 창에 스트리밍합니다.
- 세션별로 언어, Codex Model, Reasoning, Fast 설정과 대화 이력을 유지합니다.

## 준비

시스템 패키지를 설치합니다.

```bash
sudo apt install ffmpeg pulseaudio-utils python3-venv python3-gi gir1.2-gtk-3.0
```

프로젝트용 Python 가상환경을 생성하고 필요한 패키지를 설치합니다.

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements.txt
```

Codex CLI 설치와 로그인이 완료되어 있어야 합니다.

## STT 모델

언어는 Preparation 화면에서 선택하며 세션별로 저장됩니다.

| 언어 | Moonshine 모델 |
|---|---|
| English | `small-streaming-en` / `SMALL_STREAMING` |
| Japanese | `base-ja` / `BASE` |

일본어는 비라틴 문자에서 필요한 token rate를 확보하기 위해 `max_tokens_per_second=13.0`을 사용합니다.

영어는 Moonshine 기본값을 사용합니다.

## 일반 실행

평소 사용할 때는 다음 스크립트로 실행합니다.

```bash
./start_interview_app.sh
```

앱은 백그라운드로 실행되며 터미널을 계속 사용할 수 있습니다.

실행 시 다음과 같이 PID와 runtime log 경로가 출력됩니다.

```text
Interview Assistant started (PID ...)
Runtime log: /run/user/.../interview-assistant.log
```

시작 오류가 발생한 경우 해당 runtime log를 확인합니다.

예:

```bash
tail -n 100 /run/user/1000/interview-assistant.log
```

## Desktop Launcher

GNOME Applications와 현재 사용자의 Desktop에 launcher를 설치합니다.

```bash
./install_desktop_launcher.sh
```

설치 후 Activities에서 `Interview Assistant`를 검색해 실행하면 세 가지 모드를 선택할 수 있습니다. Desktop 위치는 `xdg-user-dir DESKTOP`의 설정을 사용하며, Desktop이 비활성화된 환경에서는 Applications에만 등록됩니다.

| 모드 | 용도 | 대응 CLI command |
|---|---|---|
| Normal Interview | 실제 면접 기본 모드 | `./start_interview_app.sh` |
| Performance Test | `test_runs/`에 성능 JSONL 기록 | `INTERVIEW_TEST_LOG=1 INTERVIEW_TEST_LABEL=<label> .venv/bin/python interview_app.py` |
| STT / UI Debug | Codex 없이 STT·F7/F8/F9·UI 확인 | `INTERVIEW_DISABLE_CODEX=1 INTERVIEW_TEST_LOG=1 INTERVIEW_TEST_LABEL=<label> .venv/bin/python interview_app.py` |

Performance와 Debug의 test label은 `test_runs/`에서 실행 로그를 사람이 쉽게 구분하기 위한 이름입니다. 예: `a2z`, `latency-test`, `audio-debug`.

### 터미널 foreground 실행

문제 확인이나 개발 중에는 앱을 터미널에 직접 연결하여 실행할 수 있습니다.

```bash
.venv/bin/python interview_app.py
```

앱 자체의 기능은 `./start_interview_app.sh`로 실행한 경우와 동일합니다.

차이는 foreground 실행 중에는 해당 터미널을 계속 점유하며 로그와 오류가 터미널에 직접 표시된다는 점입니다.

`Ctrl+C`로 종료할 수 있습니다.

## 환경 변수

주요 환경 변수:

```text
INTERVIEW_CODEX_MODEL
INTERVIEW_CODEX_REASONING
INTERVIEW_DISABLE_CODEX
INTERVIEW_TEST_LOG
INTERVIEW_TEST_LABEL
```

기본값:

```text
INTERVIEW_CODEX_MODEL=gpt-5.6-sol
INTERVIEW_CODEX_REASONING=low
INTERVIEW_DISABLE_CODEX=0
INTERVIEW_TEST_LOG=0
```

`INTERVIEW_TEST_LABEL`은 테스트 로그를 구분하기 위한 선택 값입니다.

언어는 환경 변수가 아니라 Preparation 화면에서 선택합니다.

Codex Model, Reasoning, Fast 설정도 Preparation 화면에서 변경할 수 있으며 세션별로 저장됩니다.

## 디버그 실행

터미널 foreground에서 실행하고 JSONL 테스트 로그를 남깁니다.

```bash
INTERVIEW_TEST_LOG=1 \
INTERVIEW_TEST_LABEL=debug \
.venv/bin/python interview_app.py
```

Codex 요청 없이 Moonshine, F7/F8/F9와 UI만 확인하려면:

```bash
INTERVIEW_DISABLE_CODEX=1 \
INTERVIEW_TEST_LOG=1 \
INTERVIEW_TEST_LABEL=audio-debug \
.venv/bin/python interview_app.py
```

테스트 로그는 `test_runs/` 아래에 저장됩니다.

디버깅할 때 주로 확인할 값:

```text
asr_backend
language
commit_source
cursor_complete
audio_drop_samples
max_backlog_ms
force_update_ms
```

F7/F8/F9 처리 시간과 Codex 응답 시간도 JSONL 로그에서 확인할 수 있습니다.

오디오 처리 정상 여부를 볼 때는 특히 다음을 확인합니다.

```text
cursor_complete=true
audio_drop_samples=0
```

## 테스트

전체 unittest:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## 테스트 음성 생성

`tts.py`는 Kokoro를 이용해 영어/일본어 테스트 음성을 생성하는 보조 도구입니다.

앱 실행에는 필요하지 않으며 별도의 `.venv-kokoro` 가상환경을 사용합니다.

### Kokoro 테스트 환경 준비

처음 사용할 때 가상환경을 생성합니다.

```bash
python3 -m venv .venv-kokoro
source .venv-kokoro/bin/activate
```

필요한 패키지를 설치합니다.

```bash
python -m pip install \
  kokoro==0.9.4 \
  soundfile==0.14.0 \
  numpy==2.2.6 \
  "misaki[ja]==0.9.4" \
  pyopenjtalk==0.4.1 \
  fugashi==1.5.2 \
  unidic==1.1.0
```

일본어 TTS를 사용할 경우 UniDic 사전도 다운로드합니다.

```bash
python -m unidic download
```

UniDic 다운로드에는 약 500MB 이상의 공간이 필요합니다.

이후 다시 사용할 때는 패키지를 재설치할 필요 없이 가상환경만 활성화합니다.

```bash
source .venv-kokoro/bin/activate
```

활성화된 환경 확인:

```bash
echo "$VIRTUAL_ENV"
which python
```

### 영어 음성 생성

```bash
python tts.py \
  --lang en \
  "Tell me about your greatest strength."
```

영어 기본 voice는 `af_heart`입니다.

### 일본어 음성 생성

```bash
python tts.py \
  --lang ja \
  "あなたの強みについて教えてください。"
```

일본어 기본 voice는 `jf_alpha`입니다.

`--out`을 생략하면 자동으로 다음 위치에 저장됩니다.

```text
voice_tests/tts_YYYYMMDD_HHMMSS.wav
```

출력 경로를 직접 지정할 수도 있습니다.

```bash
python tts.py \
  --lang ja \
  "あなたの強みについて教えてください。" \
  --out voice_tests/test_ja_24k.wav
```

### 문장 사이 pause 삽입

```bash
python tts.py \
  --lang en \
  "Hello. [pause=1.5] How are you?"
```

`[pause=X]`에서 `X`는 초 단위입니다.

예:

```text
[pause=0.5]
[pause=1.5]
[pause=2.0]
```

사용이 끝나면 가상환경에서 빠져나옵니다.

```bash
deactivate
```

## 테스트 WAV 변환 및 재생

Kokoro는 기본적으로 24kHz WAV를 생성합니다.

Moonshine 테스트용으로 사용할 경우 다음 형식으로 변환할 수 있습니다.

```text
16 kHz
mono
signed 16-bit PCM
```

24kHz → 16kHz 변환:

```bash
ffmpeg -y \
  -i voice_tests/test_ja_24k.wav \
  -ar 16000 \
  -ac 1 \
  -c:a pcm_s16le \
  voice_tests/test_ja.wav
```

파일 정보 확인:

```bash
ffprobe voice_tests/test_ja.wav
```

16kHz 변환본 재생:

```bash
ffplay -nodisp -autoexit voice_tests/test_ja.wav
```

24kHz Kokoro 원본 재생:

```bash
ffplay -nodisp -autoexit voice_tests/test_ja_24k.wav
```

생성된 WAV 파일은 테스트 산출물이므로 Git에는 포함하지 않습니다.

## Git worktree 사용 시

`.venv/`와 `.venv-kokoro/`는 Git에서 관리하지 않으므로 새로운 worktree를 만들면 자동으로 복사되지 않습니다.

새 worktree에서 앱 환경이 필요하면 해당 worktree 안에서 다시 생성합니다.

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements.txt
```

테스트 실행:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Kokoro 테스트 환경이 필요한 경우에도 해당 worktree 안에서 `.venv-kokoro`를 별도로 생성합니다.

## 조작

- `F7`: 현재 발화를 체크포인트로 보존하고 새 ASR stream에서 계속 전사
- `F8`: 마지막 F7 이후 현재 발화만 새 질문으로 확정
- `F9`: 현재 발화를 직전 질문에 이어 붙여 교정
- `Hide/Restore`: 면접 세션을 유지한 채 두 live 창만 숨김/복원
- `Back`: Preparation 화면으로 복귀
- `Close`: 세션 종료
