"""
Slice 3 — summarize all fetched articles into a single spoken-word script.

Supports two providers, selected in config.yaml -> summary.provider:
  - "gemini"     (default) — Google Gemini via the google-genai SDK
  - "anthropic"  — Claude via the anthropic SDK

Whichever is selected makes a single API call with the full articles prompt
and returns plain text. The text is written to out/summary-YYYY-MM-DD.txt.
The prompt template lives in prompts/summary_prompt.txt.
"""

from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

from brief.fetch import Article

log = logging.getLogger(__name__)

PROMPT_PATH = Path("prompts/summary_prompt.txt")
MAX_ARTICLE_CHARS = 4000  # truncate each article body so the prompt stays sane

CATEGORY_LABELS = {
    "world": "World",
    "canada": "Canada",
    "india_telugu": "India / Telugu states",
    "cricket": "Cricket",
}


# --------------------------- prompt building ------------------------

def _format_articles_block(articles: list[Article]) -> str:
    by_cat: dict[str, list[Article]] = {}
    for a in articles:
        by_cat.setdefault(a.category, []).append(a)

    sections: list[str] = []
    for cat in CATEGORY_LABELS:
        items = by_cat.get(cat, [])
        if not items:
            continue
        label = CATEGORY_LABELS[cat]
        lines = [f"### Category: {label}"]
        for i, a in enumerate(items, 1):
            body = a.text.strip().replace("\n\n", "\n")
            if len(body) > MAX_ARTICLE_CHARS:
                body = body[:MAX_ARTICLE_CHARS] + "…"
            lang_note = f" (original language: {a.lang})" if a.lang != "en" else ""
            lines.append(
                f"\n[{i}] Source: {a.source}{lang_note}\n"
                f"Title: {a.title}\n"
                f"URL: {a.url}\n"
                f"Body:\n{body}\n"
            )
        sections.append("\n".join(lines))

    return "\n\n".join(sections)


def _build_prompt(articles: list[Article], target_words: int) -> str:
    template = PROMPT_PATH.read_text()
    articles_block = _format_articles_block(articles)
    today = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    return (
        template
        .replace("{target_words}", str(target_words))
        .replace("{articles}", articles_block)
        + f"\n\nToday's date for the greeting: {today}.\n"
    )


# --------------------------- providers ------------------------------

class Summarizer(ABC):
    @abstractmethod
    def generate(self, prompt: str) -> str: ...


class GeminiSummarizer(Summarizer):
    def __init__(self, model: str) -> None:
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) is not set")
        # Lazy import so the other provider's SDK isn't required at import time
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model = model

    def generate(self, prompt: str) -> str:
        resp = self._client.models.generate_content(
            model=self._model, contents=prompt,
        )
        text = (resp.text or "").strip()
        if not text:
            raise RuntimeError(f"Gemini returned empty response (model={self._model})")
        return text


class AnthropicSummarizer(Summarizer):
    def __init__(self, model: str) -> None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        from anthropic import Anthropic

        self._client = Anthropic(api_key=api_key)
        self._model = model

    def generate(self, prompt: str) -> str:
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not text:
            raise RuntimeError(f"Claude returned empty response (model={self._model})")
        return text


def _build_summarizer(config: dict) -> tuple[Summarizer, str]:
    provider = config["summary"]["provider"].lower()
    if provider == "gemini":
        model = config["summary"]["gemini"]["model"]
        return GeminiSummarizer(model), model
    if provider == "anthropic":
        model = config["summary"]["anthropic"]["model"]
        return AnthropicSummarizer(model), model
    raise ValueError(f"Unknown summary.provider: {provider!r} (expected 'gemini' or 'anthropic')")


# --------------------------- main entry -----------------------------

def run(articles: list[Article], config: dict) -> str:
    if not articles:
        raise ValueError("summarize.run called with no articles")

    target_words = config["summary"]["target_words"]
    summarizer, model = _build_summarizer(config)
    prompt = _build_prompt(articles, target_words)

    log.info("Summarize: provider=%s model=%s articles=%d prompt=%d chars",
             config["summary"]["provider"], model, len(articles), len(prompt))

    last_err: Exception | None = None
    text: str | None = None
    for attempt in (1, 2):
        try:
            t0 = time.time()
            text = summarizer.generate(prompt)
            log.info("Summarizer responded in %.1fs — %d chars",
                     time.time() - t0, len(text))
            break
        except Exception as e:
            last_err = e
            if attempt == 1:
                log.warning("Summary call failed (attempt 1): %s — retrying", e)
                time.sleep(2)
            else:
                log.error("Summary call failed (attempt 2): %s", e)
                raise

    assert text is not None, last_err  # loop guarantees this

    out_dir = Path(config["output_dir"])
    out_dir.mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = out_dir / f"summary-{today}.txt"
    out_path.write_text(text)
    log.info("Wrote summary to %s", out_path)
    return text


# Standalone runner: python -m brief.summarize
if __name__ == "__main__":
    import yaml
    from dotenv import load_dotenv
    from brief import fetch

    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s UTC [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logging.Formatter.converter = time.gmtime

    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)

    arts = fetch.run(cfg)
    if not arts:
        log.info("No new articles — nothing to summarize.")
    else:
        summary = run(arts, cfg)
        print("\n==== SUMMARY ====\n")
        print(summary)
