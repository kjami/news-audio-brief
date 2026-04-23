"""
Slice 5 — send the share link (and optionally the full text) to the user.

Two notifiers implementing a common interface:
  - WhatsAppNotifier (Twilio)
  - EmailNotifier (SMTP)

Which one runs is controlled by config.yaml -> delivery.notifier
("whatsapp" or "email"). The email path is intended as a fallback while
the Twilio WhatsApp sandbox is being set up.

Both notifiers receive the audio share URL AND the summary text. If
`delivery.send_text: true` (default), the text is sent alongside the
audio — for WhatsApp this becomes one or more follow-up text messages
because Twilio enforces a 1600-char body cap.
"""

from __future__ import annotations

import logging
import os
import smtplib
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from email.message import EmailMessage

log = logging.getLogger(__name__)

# Twilio WhatsApp body cap is 1600 chars. Stay comfortably under so we
# don't get truncated mid-sentence.
WHATSAPP_CHUNK_CHARS = 1400


# --------------------------- base class -----------------------------

class Notifier(ABC):
    @abstractmethod
    def send(self, share_url: str, summary_text: str, title: str,
             send_text: bool) -> None:
        ...


# --------------------------- WhatsApp -------------------------------

class WhatsAppNotifier(Notifier):
    def __init__(self) -> None:
        self.account_sid = _require_env("TWILIO_ACCOUNT_SID")
        self.auth_token = _require_env("TWILIO_AUTH_TOKEN")
        self.from_ = _require_env("TWILIO_WHATSAPP_FROM")
        self.to = _require_env("TWILIO_WHATSAPP_TO")

    def send(self, share_url: str, summary_text: str, title: str,
             send_text: bool) -> None:
        # Import here so unused providers don't require the dependency
        from twilio.rest import Client

        client = Client(self.account_sid, self.auth_token)
        today = datetime.now(timezone.utc).strftime("%A, %b %d")
        caption = f"{title} — {today}\n{share_url}"

        # 1. Audio message (media attachment + short caption/link)
        self._send_with_retry(
            client,
            body=caption,
            media_url=[share_url],
            label="audio",
        )

        if not send_text or not summary_text.strip():
            return

        # 2. Follow-up text message(s) with the full summary, chunked
        # to stay under the Twilio body cap.
        chunks = _chunk_text(summary_text, WHATSAPP_CHUNK_CHARS)
        total = len(chunks)
        for i, chunk in enumerate(chunks, 1):
            prefix = f"📝 Text ({i}/{total}):\n\n" if total > 1 else "📝 Text:\n\n"
            self._send_with_retry(
                client,
                body=prefix + chunk,
                media_url=None,
                label=f"text {i}/{total}",
            )

    def _send_with_retry(self, client, *, body: str,
                         media_url: list[str] | None, label: str) -> None:
        last_err: Exception | None = None
        for attempt in (1, 2):
            try:
                kwargs: dict = {"from_": self.from_, "to": self.to, "body": body}
                if media_url:
                    kwargs["media_url"] = media_url
                msg = client.messages.create(**kwargs)
                log.info("WhatsApp %s sent, sid=%s", label, msg.sid)
                return
            except Exception as e:
                last_err = e
                if attempt == 1:
                    log.warning("Twilio %s send failed (attempt 1): %s — retrying",
                                label, e)
                    time.sleep(2)
                else:
                    log.error("Twilio %s send failed (attempt 2): %s", label, e)
                    raise
        raise RuntimeError(f"WhatsApp {label} send failed: {last_err}")


# --------------------------- Email ----------------------------------

class EmailNotifier(Notifier):
    def __init__(self) -> None:
        self.host = _require_env("SMTP_HOST")
        self.port = int(_require_env("SMTP_PORT"))
        self.user = _require_env("SMTP_USER")
        self.password = _require_env("SMTP_PASSWORD")
        self.to = _require_env("EMAIL_TO")

    def send(self, share_url: str, summary_text: str, title: str,
             send_text: bool) -> None:
        today = datetime.now(timezone.utc).strftime("%A, %b %d %Y")
        msg = EmailMessage()
        msg["From"] = self.user
        msg["To"] = self.to
        msg["Subject"] = f"{title} — {today}"

        body = f"{title} for {today}:\n\n{share_url}\n"
        if send_text and summary_text.strip():
            body += f"\n---\n\n{summary_text}\n"
        msg.set_content(body)

        try:
            with smtplib.SMTP(self.host, self.port, timeout=30) as server:
                server.starttls()
                server.login(self.user, self.password)
                server.send_message(msg)
            log.info("Email sent to %s", self.to)
        except Exception as e:
            log.error("Email send failed: %s", e)
            raise


# --------------------------- helpers --------------------------------

def _require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"{key} is not set")
    return val


def _chunk_text(text: str, budget: int) -> list[str]:
    """Split text on paragraph / sentence boundaries into chunks <= budget."""
    text = text.strip()
    if len(text) <= budget:
        return [text]

    # Try paragraph split first for natural breaks.
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for p in paragraphs:
        candidate = p if not current else current + "\n\n" + p
        if len(candidate) <= budget:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        # Paragraph alone is too big — fall back to hard slicing.
        if len(p) <= budget:
            current = p
        else:
            for i in range(0, len(p), budget):
                piece = p[i:i + budget]
                if len(piece) == budget:
                    chunks.append(piece)
                else:
                    current = piece
    if current:
        chunks.append(current)
    return chunks


# --------------------------- dispatcher -----------------------------

def run(share_url: str, summary_text: str, config: dict) -> None:
    """Construct and invoke the configured notifier."""
    delivery = config.get("delivery", {})
    kind = delivery.get("notifier", "whatsapp").lower()
    title = delivery.get("title", "Daily audio brief")
    send_text = bool(delivery.get("send_text", True))

    log.info("Notifier: %s (send_text=%s)", kind, send_text)

    if kind == "whatsapp":
        notifier: Notifier = WhatsAppNotifier()
    elif kind == "email":
        notifier = EmailNotifier()
    else:
        raise ValueError(f"Unknown notifier: {kind} (expected 'whatsapp' or 'email')")

    notifier.send(share_url, summary_text, title, send_text)
