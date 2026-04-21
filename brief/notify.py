"""
Slice 5 — send the share link to the user.

Two notifiers implementing a common interface:
  - WhatsAppNotifier (Twilio)
  - EmailNotifier (SMTP)

Which one runs is controlled by config.yaml -> delivery.notifier
("whatsapp" or "email"). The email path is intended as a fallback while
the Twilio WhatsApp sandbox is being set up.
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


# --------------------------- base class -----------------------------

class Notifier(ABC):
    @abstractmethod
    def send(self, share_url: str) -> None:
        ...


# --------------------------- WhatsApp -------------------------------

class WhatsAppNotifier(Notifier):
    def __init__(self) -> None:
        self.account_sid = _require_env("TWILIO_ACCOUNT_SID")
        self.auth_token = _require_env("TWILIO_AUTH_TOKEN")
        self.from_ = _require_env("TWILIO_WHATSAPP_FROM")
        self.to = _require_env("TWILIO_WHATSAPP_TO")

    def send(self, share_url: str) -> None:
        # Import here so unused providers don't require the dependency
        from twilio.rest import Client

        client = Client(self.account_sid, self.auth_token)
        today = datetime.now(timezone.utc).strftime("%A, %b %d")
        body = f"Your daily audio brief for {today}:\n{share_url}"

        last_err: Exception | None = None
        for attempt in (1, 2):
            try:
                msg = client.messages.create(
                    from_=self.from_, to=self.to, body=body,
                )
                log.info("WhatsApp sent, sid=%s", msg.sid)
                return
            except Exception as e:
                last_err = e
                if attempt == 1:
                    log.warning("Twilio send failed (attempt 1): %s — retrying", e)
                    time.sleep(2)
                else:
                    log.error("Twilio send failed (attempt 2): %s", e)
                    raise
        raise RuntimeError(f"WhatsApp send failed: {last_err}")


# --------------------------- Email ----------------------------------

class EmailNotifier(Notifier):
    def __init__(self) -> None:
        self.host = _require_env("SMTP_HOST")
        self.port = int(_require_env("SMTP_PORT"))
        self.user = _require_env("SMTP_USER")
        self.password = _require_env("SMTP_PASSWORD")
        self.to = _require_env("EMAIL_TO")

    def send(self, share_url: str) -> None:
        today = datetime.now(timezone.utc).strftime("%A, %b %d %Y")
        msg = EmailMessage()
        msg["From"] = self.user
        msg["To"] = self.to
        msg["Subject"] = f"Daily audio brief — {today}"
        msg.set_content(f"Your daily audio brief for {today}:\n\n{share_url}\n")

        try:
            with smtplib.SMTP(self.host, self.port, timeout=30) as server:
                server.starttls()
                server.login(self.user, self.password)
                server.send_message(msg)
            log.info("Email sent to %s", self.to)
        except Exception as e:
            log.error("Email send failed: %s", e)
            raise


# --------------------------- dispatcher -----------------------------

def _require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"{key} is not set")
    return val


def run(share_url: str, config: dict) -> None:
    """Construct and invoke the configured notifier."""
    kind = config["delivery"]["notifier"].lower()
    log.info("Notifier: %s", kind)

    if kind == "whatsapp":
        notifier: Notifier = WhatsAppNotifier()
    elif kind == "email":
        notifier = EmailNotifier()
    else:
        raise ValueError(f"Unknown notifier: {kind} (expected 'whatsapp' or 'email')")

    notifier.send(share_url)
