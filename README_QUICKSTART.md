# Interview Assistant

화상면접에서 상대방의 시스템 출력 음성을 실시간으로 자막화하고, 질문이 끝나면 Codex가 바로 말할 수 있는 답변 초안을 보여주는 Linux GNOME용 도구입니다.

- Moonshine Streaming으로 면접관 음성을 INTERVIEWER 창에 표시합니다.
- 1.5초 무음 또는 F8로 새 질문을 확정합니다.
- F9으로 직전 질문에 이어진 발화를 합쳐 질문을 교정합니다.
- Codex 답변은 별도의 Answer 창에 스트리밍합니다.
- 세션별로 Codex Model, Reasoning, Fast 설정과 대화 이력을 유지합니다.

## 준비

```bash
sudo apt install ffmpeg pulseaudio-utils python3-venv python3-gi gir1.2-gtk-3.0
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements.txt
```

Codex CLI 설치와 로그인이 완료되어 있어야 합니다.

## 일반 실행

```bash
./start_interview_app.sh
```

앱은 백그라운드로 실행됩니다. 출력된 runtime log 경로에서 시작 오류를 확인할 수 있습니다.

## 디버그 실행

터미널 foreground에서 실행하고 JSONL 테스트 로그를 남깁니다.

```bash
INTERVIEW_TEST_LOG=1 \
INTERVIEW_TEST_LABEL=debug \
.venv/bin/python interview_app.py
```

Codex 요청 없이 Moonshine, 무음 commit, F8/F9와 UI만 확인합니다.

```bash
INTERVIEW_DISABLE_CODEX=1 \
INTERVIEW_TEST_LOG=1 \
INTERVIEW_TEST_LABEL=audio-debug \
.venv/bin/python interview_app.py
```

## 테스트

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## 조작

- `F8`: 현재 발화를 새 질문으로 확정
- `F9`: 현재 발화를 직전 질문에 이어 붙여 교정
- `Hide/Restore`: 면접 세션을 유지한 채 두 live 창만 숨김/복원
- `Back`: 준비 채팅으로 복귀
- `Close`: 세션 종료
