# Daily Audio News Brief

A Python automation that runs every morning on GitHub Actions, pulls the latest articles from a fixed list of RSS feeds (English + Telugu), asks Claude to produce a single English audio-friendly summary, converts it to an MP3 with OpenAI TTS, uploads the file to Google Drive (replacing yesterday's), and sends the share link to WhatsApp (or email).

Runs in about 1–2 minutes per day. Delivers ~5 minutes of audio.

---

## What it does

1. Fetches articles from 5 RSS feeds across 4 categories: World, Canada, India/Telugu, Cricket
2. Deduplicates against `state/seen.json` (URLs from the last 7 days)
3. Sends everything new to Claude in a single API call — Telugu content is translated to English as part of the summary
4. Converts the summary to an MP3 via OpenAI TTS (chunking at 4096 chars if needed)
5. Uploads to a specific Google Drive folder, deletes yesterday's file, makes today's shareable
6. Sends the share link via WhatsApp (Twilio) or email (SMTP)

---

## Setup

### 1. API keys

| Service | What you need | Where |
|---|---|---|
| Google Gemini | API key (free tier) | https://aistudio.google.com/app/apikey |
| Google Cloud | Service account JSON with Drive API + Cloud Text-to-Speech API enabled | https://cloud.google.com/iam/docs/service-accounts-create |
| Twilio | Account SID + Auth Token + WhatsApp sandbox | https://www.twilio.com/docs/whatsapp/sandbox |
| Anthropic | *(optional)* API key — only if you switch `summary.provider` to `anthropic` | https://console.anthropic.com/settings/keys |
| OpenAI | *(optional)* API key — only if you switch `tts.provider` to `openai` | https://platform.openai.com/api-keys |

### 2. Google Cloud project (Drive + TTS)

The service account does double duty: Drive upload **and** Cloud Text-to-Speech. Set them up once:

1. Create a folder in your Drive — anywhere is fine. Open it; the URL ends in `/folders/<FOLDER_ID>` — that's your `GDRIVE_FOLDER_ID`
2. In Google Cloud Console, enable **both**:
   - [Google Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com)
   - [Cloud Text-to-Speech API](https://console.cloud.google.com/apis/library/texttospeech.googleapis.com)
3. Create a service account, download its JSON key, grab the `client_email` from the JSON
4. **Share the Drive folder with that `client_email`, granting Editor access** — without this step the upload will 404
5. The TTS API uses the same service account — no extra sharing needed, just ensure the API is enabled on the project

### 3. Twilio WhatsApp

The WhatsApp sandbox requires you to opt in first:
1. From your phone, send the sandbox join message (e.g. `join <two-word-code>`) to the Twilio sandbox number — see your Twilio console
2. Use your own number as `TWILIO_WHATSAPP_TO` in `whatsapp:+1...` format
3. Use the sandbox number as `TWILIO_WHATSAPP_FROM` (default for the sandbox is `whatsapp:+14150000000`)

---

## Run locally

```bash
# 1. Clone and create a venv
python3.11 -m venv .venv
source .venv/bin/activate

# 2. Install deps
pip install -r requirements.txt

# 3. Install ffmpeg (needed by pydub to concatenate MP3 chunks)
brew install ffmpeg          # macOS
# or: sudo apt-get install ffmpeg   (Linux)

# 4. Create your .env from the template
cp .env.example .env
# edit .env and fill in real values

# 5. Run
python main.py
```

On first run, everything fetched goes into `state/seen.json`. On subsequent runs, only new articles are picked up.

---

## Configure GitHub Actions

Add each env var from `.env.example` as a **repository secret** at Settings → Secrets and variables → Actions:

Required (default stack):
- `GEMINI_API_KEY`
- `GOOGLE_SERVICE_ACCOUNT_JSON` — paste the full contents of the service account JSON file
- `GDRIVE_FOLDER_ID`
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM`, `TWILIO_WHATSAPP_TO`

Optional:
- `ANTHROPIC_API_KEY` — only if `summary.provider: anthropic`
- `OPENAI_API_KEY` — only if `tts.provider: openai`
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO` — only if `delivery.notifier: email`

The workflow runs daily at **10:00 UTC** (6am Eastern in EDT, 5am in EST) and can also be triggered manually from the Actions tab via **Run workflow**.

After a successful run, the workflow commits the updated `state/seen.json` back to the repo so the next day's dedup works.

---

## Configuration (`config.yaml`)

- `feeds` — RSS feed list, grouped by category, with per-feed `max_articles` and `lang` (`en` or `te`)
- `summary.provider` — `gemini` (default) or `anthropic`; model id lives under the matching sub-block
- `summary.target_words` — target script length (default: 750 ≈ 5 minutes audio)
- `tts.provider` — `google` (default) or `openai`; voice/model under the matching sub-block
- `delivery.notifier` — `whatsapp` or `email`

The summarization prompt lives in `prompts/summary_prompt.txt` — iterate on it without touching code.

---

## Troubleshooting

**Andhra Jyothy returns 403.** The script already retries Telugu feeds with a browser User-Agent. If that still fails, the feed is logged and skipped rather than crashing the run. Check the workflow logs for the warning.

**Twilio WhatsApp messages never arrive.** You must send the sandbox `join <code>` message from your phone before Twilio will deliver anything. If your number hasn't opted in, the Twilio API returns `success` but no message reaches your phone. Re-opt-in after 72 hours of inactivity.

**Drive upload fails with `File not found`.** The service account email doesn't have access to the folder. Open the folder in the Drive UI → Share → add the service account's `client_email` (ends in `.iam.gserviceaccount.com`) → give it **Editor**.

---

## Cost estimate (daily run, default stack)

| Component | Monthly |
|---|---|
| Gemini 2.5 Flash (AI Studio free tier — well under the rate/quota limits for 1 call/day) | $0 |
| Google Cloud TTS (Neural2 voice, ~150k chars/mo — free tier covers 1M chars/mo standard, 4M chars/mo WaveNet) | $0 |
| Google Drive | $0 |
| Twilio WhatsApp (sandbox) | $0 |
| GitHub Actions (public repo, or ~1 min on private) | $0 |
| **Total** | **$0** |

One caveat on Gemini: the AI Studio free tier uses prompts/responses for product improvement. If that matters, switch to paid tier (~$0.50/mo at this volume) or Vertex AI.

If you swap providers in `config.yaml`:

| Swap | Added monthly cost |
|---|---|
| `summary.provider: anthropic` (Claude Sonnet 4.6) | ~$4.50 |
| `tts.provider: openai` (tts-1, alloy) | ~$2.40 |
