"""Local-only Whisper command for a bridge using a separate Hermes runtime.

Run with a Python environment that already has faster-whisper. Configure
HERMES_LOCAL_STT_COMMAND with {input_path} and {output_dir}; no credentials needed.
Models must already be cached. This never downloads or invokes a cloud fallback.
"""
import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    from faster_whisper import WhisperModel

    model = WhisperModel("base", device="cpu", compute_type="int8", cpu_threads=2,
                         num_workers=1, local_files_only=True)
    segments, _ = model.transcribe(args.input, beam_size=5, vad_filter=True,
                                   condition_on_previous_text=False, language=None)
    # Preserve Hermes' conservative silence-hallucination AND gate. Auto-detect
    # language rather than forcing English onto Dutch Kids profiles.
    text = " ".join(s.text.strip() for s in segments
                    if not (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0)).strip()
    Path(args.output_dir, "transcript.txt").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
