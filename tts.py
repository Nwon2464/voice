from kokoro import KPipeline
import soundfile as sf
import numpy as np
import argparse
import re
from pathlib import Path
from datetime import datetime

SAMPLE_RATE = 24000

parser = argparse.ArgumentParser()
parser.add_argument("text", nargs="+")
parser.add_argument("--out", default=None)
parser.add_argument("--lang", choices=["en", "ja"], default="en")

args = parser.parse_args()


text = " ".join(args.text).strip()

LANG_CONFIG = {
    "en": {"lang_code": "a", "voice": "af_heart"},
    "ja": {"lang_code": "j", "voice": "jf_alpha"},
}

config = LANG_CONFIG[args.lang]

pipeline = KPipeline(lang_code=config["lang_code"])



# 예:
# Hello. [pause=1.5] How are you?
PAUSE_PATTERN = re.compile(r"\[pause=(\d+(?:\.\d+)?)\]")

parts = PAUSE_PATTERN.split(text)

audio_parts = []

for i, part in enumerate(parts):

    # 홀수 index에는 pause 초가 들어옴
    if i % 2 == 1:
        pause_seconds = float(part)

        silence = np.zeros(
            int(SAMPLE_RATE * pause_seconds),
            dtype=np.float32,
        )

        audio_parts.append(silence)
        print(f"Inserted silence: {pause_seconds:.3f}s")
        continue

    sentence = part.strip()

    if not sentence:
        continue

    generator = pipeline(
        sentence,
        voice=config["voice"],
    )


    generated_parts = []

    for _, _, audio in generator:
        generated_parts.append(
            np.asarray(audio, dtype=np.float32)
        )

    if generated_parts:
        audio_parts.append(
            np.concatenate(generated_parts)
        )

if not audio_parts:
    print("No audio generated.")
    raise SystemExit(1)

audio = np.concatenate(audio_parts)

if args.out:
    output_path = Path(args.out)
else:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = Path("voice_tests") / f"tts_{timestamp}.wav"


output_path.parent.mkdir(parents=True, exist_ok=True)

sf.write(
    output_path,
    audio,
    SAMPLE_RATE,
)

print(f"Created: {output_path}")
