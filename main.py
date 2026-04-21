"""
Daily Audio News Brief — entry point.
Orchestrates the pipeline: fetch → summarize → tts → upload → notify.
"""

import logging
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s UTC [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
# Force UTC timestamps in logs
import time
logging.Formatter.converter = time.gmtime

log = logging.getLogger(__name__)


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def print_feed_summary(config: dict) -> None:
    log.info("Configured feeds:")
    for category, feeds in config["feeds"].items():
        for feed in feeds:
            lang_note = " [Telugu — will translate]" if feed["lang"] == "te" else ""
            log.info(
                "  [%s] %s — %s (max %d articles)%s",
                category,
                feed["name"],
                feed["url"],
                feed["max_articles"],
                lang_note,
            )


def main() -> None:
    log.info("=== Daily Audio News Brief starting ===")

    config = load_config()
    print_feed_summary(config)

    # Ensure output and state directories exist
    Path(config["output_dir"]).mkdir(exist_ok=True)
    Path(config["state_dir"]).mkdir(exist_ok=True)

    log.info("Output dir : %s", config["output_dir"])
    log.info("State dir  : %s", config["state_dir"])
    summary_provider = config["summary"]["provider"]
    tts_provider = config["tts"]["provider"]
    log.info("Summary  : %s (%s)", summary_provider,
             config["summary"][summary_provider]["model"])
    log.info("TTS      : %s (%s)", tts_provider,
             config["tts"][tts_provider].get("voice", config["tts"][tts_provider].get("model")))
    log.info("Notifier : %s", config["delivery"]["notifier"])

    # --- Slice 2: fetch ---
    from brief import fetch
    articles = fetch.run(config)
    if not articles:
        log.info("No new articles — exiting gracefully.")
        return

    # --- Slice 3: summarize ---
    from brief import summarize
    summary_text = summarize.run(articles, config)
    log.info("Summary: %d words", len(summary_text.split()))

    # --- Slice 4: tts ---
    from brief import tts
    mp3_path = tts.run(summary_text, config)

    # --- Slice 5: upload + notify ---
    from brief import upload, notify
    share_url = upload.run(mp3_path, config)
    notify.run(share_url, config)

    log.info("=== Daily brief complete: %s ===", share_url)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception("Pipeline failed: %s", e)
        sys.exit(1)
