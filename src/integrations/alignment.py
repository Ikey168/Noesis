"""Optional local WhisperX alignment; original segment text remains authoritative."""

import tempfile
from pathlib import Path

from .common import IntegrationError, digest, version


class WhisperXAligner:
    def __init__(
        self, model_directory, *, language="de", device="cpu", max_seconds=600
    ):
        import whisperx

        if not Path(model_directory).is_dir():
            raise IntegrationError(
                "model_unavailable", "Provide a pinned local alignment model"
            )
        if language not in {"de", "en"} or not 1 <= max_seconds <= 3600:
            raise ValueError("unsupported alignment configuration")
        self.model, self.metadata = whisperx.load_align_model(
            language_code=language, device=device, model_name=str(model_directory)
        )
        self.device = device
        self.max_seconds = max_seconds
        self.producer = {
            "backend": "whisperx",
            "version": version("whisperx"),
            "language": language,
            "model_config_sha256": digest(
                (Path(model_directory) / "config.json").read_text()
            ),
        }

    def __call__(self, audio_bytes, segments):
        import whisperx

        from src.ingestion.connectors.media.transcriber import _to_wav

        if len(audio_bytes) > 100_000_000 or len(segments) > 1000:
            raise IntegrationError("input_limit", "Alignment input exceeds bounds")
        with tempfile.TemporaryDirectory(prefix="noesis-alignment-") as directory:
            path = Path(directory) / "audio.wav"
            path.write_bytes(_to_wav(audio_bytes))
            audio = whisperx.load_audio(str(path))
        if len(audio) / 16000 > self.max_seconds:
            raise IntegrationError(
                "duration_limit", "Audio exceeds alignment duration bound"
            )
        request = [
            {"start": s.start_s, "end": s.end_s, "text": s.text} for s in segments
        ]
        aligned = whisperx.align(
            request,
            self.model,
            self.metadata,
            audio,
            self.device,
            return_char_alignments=False,
        )
        output = aligned.get("segments", [])
        if len(output) != len(segments):
            raise IntegrationError(
                "alignment_mismatch", "Alignment changed segment count"
            )
        results = []
        for source, result in zip(segments, output):
            words = []
            for word in result.get("words", []):
                start, end = word.get("start"), word.get("end")
                valid = (
                    start is not None
                    and end is not None
                    and 0 <= start <= end <= len(audio) / 16000
                )
                words.append(
                    {
                        "text": word.get("word", ""),
                        "start_s": start if valid else None,
                        "end_s": end if valid else None,
                        "status": "aligned" if valid else "unaligned",
                    }
                )
            results.append(
                {
                    "words": words,
                    "producer": self.producer,
                    "source_text_sha256": digest(source.text),
                }
            )
        return results
