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

DEFAULT_PROMPT_PATH = Path("prompts/summary_prompt.txt")
MAX_ARTICLE_CHARS = 4000  # truncate each article body so the prompt stays sane


def _humanise(key: str) -> str:
    return key.replace("_", " ").title()


# --------------------------- prompt building ------------------------

def _format_articles_block(articles: list[Article], labels: dict[str, str]) -> str:
    by_cat: dict[str, list[Article]] = {}
    for a in articles:
        by_cat.setdefault(a.category, []).append(a)

    # Preserve the label order from the config where possible, then append
    # any categories that appeared in articles but weren't listed.
    ordered_cats = list(labels.keys()) + [c for c in by_cat if c not in labels]

    sections: list[str] = []
    for cat in ordered_cats:
        items = by_cat.get(cat, [])
        if not items:
            continue
        label = labels.get(cat, _humanise(cat))
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


def _build_prompt(articles: list[Article], target_words: int,
                  prompt_path: Path, labels: dict[str, str]) -> str:
    template = prompt_path.read_text()
    articles_block = _format_articles_block(articles, labels)
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
    prompt_path = Path(config["summary"].get("prompt_file") or DEFAULT_PROMPT_PATH)
    labels = config.get("category_labels") or {}
    prompt = _build_prompt(articles, target_words, prompt_path, labels)

    log.info("Summarize: provider=%s model=%s articles=%d prompt=%d chars",
             config["summary"]["provider"], model, len(articles), len(prompt))

    # Summary retry: up to 3 attempts with a 30s wait between them.
    # The free Gemini tier returns 503 "high demand" during spikes that
    # typically clear in under a minute, so a short 2s retry is not enough.
    last_err: Exception | None = None
    text: str | None = None
    max_attempts = 3
    retry_wait_s = 30
    for attempt in range(1, max_attempts + 1):
        try:
            t0 = time.time()
            text = summarizer.generate(prompt)
            log.info("Summarizer responded in %.1fs — %d chars",
                     time.time() - t0, len(text))
            break
        except Exception as e:
            last_err = e
            if attempt < max_attempts:
                log.warning("Summary call failed (attempt %d/%d): %s — "
                            "retrying in %ds", attempt, max_attempts, e, retry_wait_s)
                time.sleep(retry_wait_s)
            else:
                log.error("Summary call failed (attempt %d/%d): %s",
                          attempt, max_attempts, e)
                raise

    assert text is not None, last_err  # loop guarantees this

    out_dir = Path(config["output_dir"])
    out_dir.mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prefix = config.get("output", {}).get("file_prefix", "brief-")
    # Use the same prefix as the MP3 so news/market summaries don't clobber.
    out_path = out_dir / f"{prefix}summary-{today}.txt"
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
