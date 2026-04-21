"""
Slice 4 — convert the summary text to an MP3.

Supports two providers, selected in config.yaml -> tts.provider:
  - "google"  (default) — Google Cloud Text-to-Speech (uses the same
                           GOOGLE_SERVICE_ACCOUNT_JSON as Drive; the Cloud
                           Text-to-Speech API must be enabled on the project)
  - "openai"  — OpenAI TTS (tts-1, alloy by default)

Both providers cap single-request input at ~4k–5k chars. We split the
summary on sentence boundaries (budget = 4000 chars) to fit both, synthesize
each chunk, and concatenate the MP3s with pydub when there's more than one.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

from pydub import AudioSegment

log = logging.getLogger(__name__)

# Budget per request — under Google TTS's 5000-byte cap and OpenAI's 4096-char cap.
CHUNK_CHAR_BUDGET = 4000


# --------------------------- chunking -------------------------------

def _split_into_chunks(text: str, budget: int = CHUNK_CHAR_BUDGET) -> list[str]:
    """Split on sentence boundaries into chunks <= budget chars."""
    text = text.strip()
    if len(text) <= budget:
        return [text]

    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    current = ""
    for s in sentences:
        if not s:
            continue
        if len(s) > budget:
            if current:
                chunks.append(current.strip())
                current = ""
            for i in range(0, len(s), budget):
                chunks.append(s[i : i + budget])
            continue

        candidate = f"{current} {s}".strip() if current else s
        if len(candidate) <= budget:
            current = candidate
        else:
            chunks.append(current.strip())
            current = s

    if current:
        chunks.append(current.strip())
    return chunks


# --------------------------- providers ------------------------------

class TtsProvider(ABC):
    @abstractmethod
    def synthesize(self, text: str, out_path: Path) -> None: ...


class GoogleTts(TtsProvider):
    def __init__(self, language_code: str, voice: str) -> None:
        raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        if not raw:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not set")
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {e}")

        from google.oauth2 import service_account
        from google.cloud import texttospeech

        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        self._tts = texttospeech
        self._client = texttospeech.TextToSpeechClient(credentials=creds)
        self._voice = texttospeech.VoiceSelectionParams(
            language_code=language_code, name=voice,
        )
        self._audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
        )

    def synthesize(self, text: str, out_path: Path) -> None:
        resp = self._client.synthesize_speech(
            input=self._tts.SynthesisInput(text=text),
            voice=self._voice,
            audio_config=self._audio_config,
        )
        out_path.write_bytes(resp.audio_content)


class OpenAiTts(TtsProvider):
    def __init__(self, model: str, voice: str) -> None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)
        self._model = model
        self._voice = voice

    def synthesize(self, text: str, out_path: Path) -> None:
        # with_streaming_response is the non-deprecated way to write to disk
        with self._client.audio.speech.with_streaming_response.create(
            model=self._model,
            voice=self._voice,
            input=text,
            response_format="mp3",
        ) as resp:
            resp.stream_to_file(out_path)


def _build_provider(config: dict) -> tuple[TtsProvider, str]:
    kind = config["tts"]["provider"].lower()
    if kind == "google":
        g = config["tts"]["google"]
        return GoogleTts(g["language_code"], g["voice"]), f"google:{g['voice']}"
    if kind == "openai":
        o = config["tts"]["openai"]
        return OpenAiTts(o["model"], o["voice"]), f"openai:{o['model']}/{o['voice']}"
    raise ValueError(f"Unknown tts.provider: {kind!r} (expected 'google' or 'openai')")


# --------------------------- main entry -----------------------------

def _synthesize_with_retry(provider: TtsProvider, text: str, out_path: Path) -> None:
    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            t0 = time.time()
            provider.synthesize(text, out_path)
            log.info("  TTS chunk -> %s (%d chars, %.1fs)",
                     out_path.name, len(text), time.time() - t0)
            return
        except Exception as e:
            last_err = e
            if attempt == 1:
                log.warning("TTS call failed (attempt 1): %s — retrying", e)
                time.sleep(2)
            else:
                log.error("TTS call failed (attempt 2): %s", e)
                raise
    raise RuntimeError(f"TTS failed: {last_err}")


def run(summary_text: str, config: dict) -> Path:
    if not summary_text.strip():
        raise ValueError("tts.run called with empty summary text")

    provider, label = _build_provider(config)

    out_dir = Path(config["output_dir"])
    out_dir.mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    final_path = out_dir / f"brief-{today}.mp3"

    chunks = _split_into_chunks(summary_text)
    log.info("TTS: %d chars -> %d chunk(s), provider=%s",
             len(summary_text), len(chunks), label)

    if len(chunks) == 1:
        _synthesize_with_retry(provider, chunks[0], final_path)
        log.info("Wrote MP3 to %s (%.1f KB)",
                 final_path, final_path.stat().st_size / 1024)
        return final_path

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        parts: list[Path] = []
        for i, chunk in enumerate(chunks, 1):
            part_path = tmp_dir / f"part-{i:02d}.mp3"
            _synthesize_with_retry(provider, chunk, part_path)
            parts.append(part_path)

        log.info("Concatenating %d parts with pydub…", len(parts))
        combined = AudioSegment.empty()
        for p in parts:
            combined += AudioSegment.from_mp3(p)
        combined.export(final_path, format="mp3")

    log.info("Wrote MP3 to %s (%.1f KB)",
             final_path, final_path.stat().st_size / 1024)
    return final_path


# Standalone runner: python -m brief.tts path/to/summary.txt
if __name__ == "__main__":
    import sys
    import yaml
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s UTC [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logging.Formatter.converter = time.gmtime

    if len(sys.argv) < 2:
        print("Usage: python -m brief.tts <summary_file.txt>")
        sys.exit(1)

    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)

    text = Path(sys.argv[1]).read_text()
    path = run(text, cfg)
    print(f"MP3 written to: {path}")
