Daily Audio News Brief — v1 Build Prompt
Project goal
Build a Python automation that runs daily on GitHub Actions, pulls the latest articles from a fixed list of RSS feeds, uses Claude to produce a single English-language audio-friendly summary (translating Telugu content to English as part of summarization), converts that summary to an MP3 using OpenAI TTS, uploads the MP3 to Google Drive, deletes yesterday's MP3 from Drive, and sends the share link to WhatsApp.
Do not build anything beyond v1. Keep it simple, boring, and debuggable. No web framework, no database, no async unless genuinely needed.
Target user
I am a software architect comfortable with code, but new to building AI apps. Prioritize readability and clear structure over cleverness. Explain non-obvious choices with brief comments.
Tech stack (locked in — do not substitute)

Python 3.11
feedparser for RSS parsing
httpx for HTTP (sync client is fine)
trafilatura for article text extraction where RSS gives only summaries
anthropic SDK for summarization (model: claude-sonnet-4-5 — use the latest Sonnet; if that model name is wrong, ask before substituting)
openai SDK for TTS (model: tts-1, voice: alloy, format: mp3)
google-api-python-client + google-auth for Google Drive upload
PyYAML for config
python-dotenv for local dev
GitHub Actions for scheduling

RSS feed list (hardcode in config.yaml)
Five feeds, grouped by category:
World

The Guardian World: https://www.theguardian.com/world/rss

Canada

CBC Top Stories: https://www.cbc.ca/cmlink/rss-topstories

India / Telugu states

BBC Telugu: https://feeds.bbci.co.uk/telugu/rss.xml (Telugu — translate to English during summarization)
Andhra Jyothy: https://andhrajyothy.com/rss/feed.xml (Telugu — translate to English during summarization; may need a browser-like User-Agent header or return 403)

Cricket

ESPNCricinfo Global: https://www.espncricinfo.com/rss/content/story/feeds/0.xml

Make the feed list, category grouping, and number-of-articles-per-feed configurable via config.yaml.
Pipeline (one script, sequential steps)
Build as separate modules in a brief/ package, called from a single main.py:

fetch.py — for each feed, pull latest N articles (default 5 per feed). For each article, try to get full text: prefer RSS content:encoded if present, else fall back to fetching the article URL and running it through trafilatura. Deduplicate against state/seen.json (store article URLs seen in the last 7 days; prune older entries). If nothing new across all feeds, exit gracefully with a log message and skip the rest of the pipeline.
summarize.py — send all new articles to Claude in a single API call, grouped by category, with this shaping:

Translate Telugu articles to English before summarizing
Output is a single continuous spoken-word script, not bullet points
Opens with a greeting and the date
Segments by category with natural verbal transitions ("Now, turning to cricket…")
Each article gets 2–4 sentences focused on what happened and why it matters
Target total length: 600–900 words (roughly 4–6 minutes of audio)
Ends with a sign-off
No markdown, no headers, no lists — this will be read aloud
Put the prompt in a separate prompts/summary_prompt.txt file loaded at runtime so I can iterate on it without touching code.


tts.py — send the summary text to OpenAI TTS (tts-1, alloy, mp3), save to out/brief-YYYY-MM-DD.mp3. If the text exceeds OpenAI's TTS character limit (4096 chars), split on sentence boundaries, generate multiple MP3s, concatenate with pydub (add to dependencies).
upload.py — upload the MP3 to a specific Google Drive folder (folder ID from env var GDRIVE_FOLDER_ID). Before uploading today's file, list files in that folder matching the pattern brief-*.mp3 and delete any that aren't today's date. Make the uploaded file shareable via link (anyone with link can view). Return the share URL. Use a service account for auth (credentials JSON from env var GOOGLE_SERVICE_ACCOUNT_JSON — the full JSON as a string, parsed at runtime).
notify.py — send the share link to WhatsApp via Twilio's WhatsApp API (env vars: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM, TWILIO_WHATSAPP_TO). Structure this as a Notifier base class with WhatsAppNotifier and EmailNotifier (SMTP) implementations, switchable via config.yaml. Default to whatsapp, but include the email fallback so I can test before WhatsApp is set up.
main.py — orchestrates the pipeline, top-to-bottom, with try/except around each step, clear log output at each stage, and a non-zero exit code on failure (so GitHub Actions shows a red X).

Config file (config.yaml)
yamlfeeds:
  world:
    - name: Guardian World
      url: https://www.theguardian.com/world/rss
      lang: en
      max_articles: 4
  canada:
    - name: CBC Top Stories
      url: https://www.cbc.ca/cmlink/rss-topstories
      lang: en
      max_articles: 4
  india_telugu:
    - name: BBC Telugu
      url: https://feeds.bbci.co.uk/telugu/rss.xml
      lang: te
      max_articles: 4
    - name: Andhra Jyothy
      url: https://andhrajyothy.com/rss/feed.xml
      lang: te
      max_articles: 4
  cricket:
    - name: ESPNCricinfo
      url: https://www.espncricinfo.com/rss/content/story/feeds/0.xml
      lang: en
      max_articles: 5

summary:
  target_words: 750
  model: claude-sonnet-4-5

tts:
  provider: openai
  model: tts-1
  voice: alloy

delivery:
  notifier: whatsapp   # or "email"

state_dir: state
output_dir: out
Secrets (env vars — documented in README)

ANTHROPIC_API_KEY
OPENAI_API_KEY
GOOGLE_SERVICE_ACCOUNT_JSON (full JSON content)
GDRIVE_FOLDER_ID
TWILIO_ACCOUNT_SID
TWILIO_AUTH_TOKEN
TWILIO_WHATSAPP_FROM (e.g., whatsapp:+14150000000 for Twilio sandbox)
TWILIO_WHATSAPP_TO (my WhatsApp number in whatsapp:+… format)
(Optional, for email fallback): SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_TO

Load from .env locally via python-dotenv; in GitHub Actions, they come from repository secrets.
Project structure
.
├── .github/workflows/daily-brief.yml
├── brief/
│   ├── __init__.py
│   ├── fetch.py
│   ├── summarize.py
│   ├── tts.py
│   ├── upload.py
│   └── notify.py
├── prompts/
│   └── summary_prompt.txt
├── state/
│   └── .gitkeep
├── out/
│   └── .gitkeep
├── config.yaml
├── main.py
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
GitHub Actions workflow
File: .github/workflows/daily-brief.yml. Cron at 0 11 * * * UTC (7am Eastern — my timezone is America/Toronto, adjust if DST matters but don't overcomplicate). Also workflow_dispatch so I can trigger manually from the GitHub UI. Python 3.11 setup, pip install from requirements.txt, run python main.py. On failure, leave the logs visible (default behavior is fine). Commit the updated state/seen.json back to the repo after a successful run using a minimal commit step (only if the file changed).
README
Include:

What this does (one paragraph)
Setup: how to create Anthropic, OpenAI, Twilio, and Google Cloud service account credentials — link to the relevant docs, don't write a tutorial
How to create the Drive folder and get its ID, and how to share it with the service account email
How to run locally (python main.py with .env populated)
How to configure GitHub Actions secrets
Troubleshooting section with the three most likely failure modes: 403 from Andhra Jyothy (solution: add User-Agent header), Twilio WhatsApp sandbox requiring you to send a join message first, Drive service account not having access to the folder
Cost estimate: ballpark monthly cost assuming daily runs

Build order I want you to follow
Do not build everything at once. Build in these thin slices, pausing after each for me to review:

Slice 1 — Project scaffolding: directory structure, requirements.txt, .gitignore, config.yaml, .env.example, skeleton main.py that loads config and prints feed URLs. No API calls yet.
Slice 2 — fetch.py end-to-end: fetch all five feeds, extract article text, dedupe against seen.json, print a summary of what was found. Verify it works before moving on.
Slice 3 — summarize.py: send fetched articles to Claude, write the English summary to out/summary-YYYY-MM-DD.txt. Verify output reads well.
Slice 4 — tts.py: convert text to MP3, save locally. Handle the 4096-char chunking case.
Slice 5 — upload.py + notify.py: Drive upload with yesterday-deletion, then WhatsApp (or email fallback) link delivery.
Slice 6 — GitHub Actions workflow + README + final polish.

At the end of each slice, stop and print a short summary of what was built, what I need to test manually, and what's next. Do not proceed to the next slice until I say "go."
Constraints and preferences

Prefer standard library where reasonable
Every function that calls an external API needs try/except and a clear log line on both success and failure
Timestamps in logs, in UTC
Do not commit API keys to .env.example — use placeholder values
Do not add tests in v1 — I'll add them in v2 once the shape is stable
Do not add type-checking tooling, linters, or pre-commit hooks in v1
Do not add retry/backoff logic beyond one simple retry on network calls
Do not invent feed URLs — use exactly what's listed above
If a Telugu feed returns 403, add a realistic browser User-Agent header and retry once; if it still fails, log and skip that feed (don't crash the run)

First action
Before writing any code, read this prompt back to me in your own words as a 5-bullet summary so I can confirm you've got it right. Then wait for my "go" before starting Slice 1.