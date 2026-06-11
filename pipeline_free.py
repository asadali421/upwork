"""
Upwork Job Alert + AI Proposal Pipeline — 100% FREE EDITION
============================================================
No paid monitoring service needed. Uses Upwork's own free email job
alerts as the feed source.

Flow:
    Upwork saved search -> instant email alert -> Gmail
    -> this script (IMAP polling) -> parse jobs
    -> LLM (free Gemini tier, or Claude API) -> score + draft proposal
    -> Telegram alert -> YOU review & submit on Upwork

Usage:
    1. Fill in config.json (see README_FREE.md)
    2. pip install -r requirements.txt
    3. python pipeline_free.py
"""

import email
import imaplib
import json
import logging
import re
import sys
import time
from email.header import decode_header
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("pipeline")

CONFIG_PATH = Path(__file__).parent / "config.json"
SEEN_PATH = Path(__file__).parent / "seen_jobs.json"

UPWORK_JOB_LINK_RE = re.compile(r"https?://www\.upwork\.com/jobs/[^\s\"'<>\)]+")
# Any Upwork-related link, including tracking/redirect domains (click.upwork.com,
# e.upwork.com, etc.) that the alert emails actually use for their buttons.
ANY_UPWORK_LINK_RE = re.compile(r"https?://[^\s\"'<>\)]*upwork\.com[^\s\"'<>\)]*")
# Job-alert emails have subjects like: "New job: Build a Hotel Price Tracker"
NEW_JOB_SUBJECT_RE = re.compile(r"^\s*new job:\s*(.+)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Config / state
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        log.error("config.json not found. Copy config.example.json and fill it in.")
        sys.exit(1)
    return json.loads(CONFIG_PATH.read_text())


def load_seen() -> set:
    if SEEN_PATH.exists():
        return set(json.loads(SEEN_PATH.read_text()))
    return set()


def save_seen(seen: set) -> None:
    SEEN_PATH.write_text(json.dumps(list(seen)[-2000:]))


# ---------------------------------------------------------------------------
# Gmail IMAP — read Upwork job alert emails
# ---------------------------------------------------------------------------

def connect_imap(cfg: dict) -> imaplib.IMAP4_SSL:
    mail = imaplib.IMAP4_SSL(cfg["email"].get("imap_server", "imap.gmail.com"))
    mail.login(cfg["email"]["address"], cfg["email"]["app_password"])
    return mail


def decode_subject(raw) -> str:
    parts = decode_header(raw or "")
    out = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            out += text.decode(enc or "utf-8", errors="replace")
        else:
            out += text
    return out


def get_email_body(msg) -> str:
    """Extract text body (prefers HTML since Upwork alerts are HTML)."""
    html_body, text_body = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            decoded = payload.decode(part.get_content_charset() or "utf-8",
                                     errors="replace")
            if ctype == "text/html":
                html_body += decoded
            elif ctype == "text/plain":
                text_body += decoded
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            decoded = payload.decode(msg.get_content_charset() or "utf-8",
                                     errors="replace")
            if msg.get_content_type() == "text/html":
                html_body = decoded
            else:
                text_body = decoded
    return html_body or text_body


def html_to_text(raw: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw,
                  flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|tr|li|h\d)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def find_job_link(body_html: str) -> str | None:
    """Best click-through link for a job alert. Prefer a direct /jobs/ posting
    link; fall back to any Upwork link (the alert buttons are usually tracking
    redirects, not clean job URLs)."""
    m = UPWORK_JOB_LINK_RE.search(body_html)
    if m:
        return m.group(0).split("?")[0]
    m = ANY_UPWORK_LINK_RE.search(body_html)
    return m.group(0) if m else None


def parse_jobs_from_email(subject: str, body_html: str) -> list[dict]:
    """
    Upwork "New job: ..." alert emails are one posting each, and the subject
    line carries the job title reliably. We use the subject as the title and
    the email body as the description. (Older digest emails with multiple
    /jobs/ links are handled by the fallback path.)
    """
    text = html_to_text(body_html)

    m = NEW_JOB_SUBJECT_RE.match(subject or "")
    if m:
        title = re.sub(r"\*+", "", m.group(1)).strip()
        link = find_job_link(body_html)
        return [{
            # Title is stable across re-reads, so it's a good dedup key even
            # when the click-through is a one-off tracking URL.
            "id": link if (link and "/jobs/" in link) else f"subject:{title.lower()}",
            "title": title[:120],
            "description": text[:4000],
            "link": link or "https://www.upwork.com/nx/find-work/",
        }]

    # Fallback: digest email with several direct /jobs/ links.
    links = UPWORK_JOB_LINK_RE.findall(body_html)
    clean_links, seen_local = [], set()
    for link in links:
        base = link.split("?")[0]
        if base not in seen_local:
            seen_local.add(base)
            clean_links.append(base)

    jobs = []
    blocks = [b.strip() for b in text.split("\n\n") if len(b.strip()) > 40]
    for i, link in enumerate(clean_links):
        desc = blocks[i] if i < len(blocks) else text[:1500]
        title = desc.split("\n")[0][:120]
        jobs.append({
            "id": link,
            "title": title,
            "description": desc[:4000],
            "link": link,
        })
    return jobs


def fetch_new_alert_emails(cfg: dict) -> list[dict]:
    """Return jobs parsed from unread Upwork alert emails."""
    jobs = []
    mail = connect_imap(cfg)
    try:
        mail.select("INBOX")
        # Unread emails from Upwork
        status, data = mail.search(None, '(UNSEEN FROM "upwork.com")')
        if status != "OK":
            return jobs
        # IMAP returns oldest-first; keep only the most recent N so a backlog
        # doesn't trigger a flood of LLM calls and alerts in one pass.
        max_emails = cfg.get("max_emails", 10)
        nums = data[0].split()[-max_emails:]
        for num in nums:
            status, msg_data = mail.fetch(num, "(RFC822)")
            if status != "OK":
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            subject = decode_subject(msg.get("Subject", ""))
            body = get_email_body(msg)
            found = parse_jobs_from_email(subject, body)
            # Mark as read regardless, so account/marketing emails (support
            # requests, milestones, message notifications, etc.) don't pile
            # up and get re-read on every poll.
            mail.store(num, "+FLAGS", "\\Seen")
            if not found:
                # Not a job-feed email (no /jobs/ posting links). Skip quietly.
                log.debug("No jobs in '%s' — skipping", subject[:60])
                continue
            log.info("Email '%s' -> %d job(s) parsed", subject[:60], len(found))
            jobs.extend(found)
    finally:
        try:
            mail.logout()
        except Exception:
            pass
    return jobs


# ---------------------------------------------------------------------------
# Keyword / budget filter (same as before)
# ---------------------------------------------------------------------------

def extract_budget(text: str) -> float | None:
    amounts = re.findall(r"\$\s?([\d,]+(?:\.\d{1,2})?)", text)
    if not amounts:
        return None
    return max(float(a.replace(",", "")) for a in amounts)


def job_passes_filter(job: dict, cfg: dict) -> tuple[bool, str]:
    text = (job["title"] + " " + job["description"]).lower()
    includes = [k.lower() for k in cfg["filters"]["include_keywords"]]
    if includes and not any(k in text for k in includes):
        return False, "no include keyword matched"
    excludes = [k.lower() for k in cfg["filters"]["exclude_keywords"]]
    hit = next((k for k in excludes if k in text), None)
    if hit:
        return False, f"exclude keyword: {hit}"
    min_budget = cfg["filters"].get("min_budget", 0)
    budget = extract_budget(job["description"])
    if budget is not None and budget < min_budget:
        return False, f"budget ${budget:.0f} below floor ${min_budget}"
    return True, "ok"


# ---------------------------------------------------------------------------
# LLM layer — supports Groq (free), Gemini (free tier), or Claude (paid)
# ---------------------------------------------------------------------------

PROPOSAL_SYSTEM = """You write short, high-converting Upwork proposals.

Rules:
- 120-180 words. Clients skim; brevity wins.
- Open with a sentence that proves you read THIS job (reference a specific
  detail from the description). Never open with "Hi, I'm excited..."
- One short paragraph connecting the freelancer's background to the job's
  actual problem.
- One concrete suggestion, question, or mini-plan that shows thinking.
- End with a low-friction call to action.
- No buzzwords, no flattery, no "I am the perfect fit".
- Plain text only.
"""


def _post_with_retry(url: str, *, headers: dict | None = None,
                     json_body: dict, max_retries: int = 4) -> requests.Response:
    """POST with exponential backoff on rate-limit / transient errors
    (429, 500, 502, 503). Honors a Retry-After header when present."""
    delay = 2.0
    for attempt in range(1, max_retries + 1):
        r = requests.post(url, headers=headers, json=json_body, timeout=60)
        if r.status_code in (429, 500, 502, 503) and attempt < max_retries:
            retry_after = r.headers.get("Retry-After")
            wait = float(retry_after) if (retry_after and retry_after.isdigit()) else delay
            log.warning("LLM %s — backing off %.0fs (attempt %d/%d): %s",
                        r.status_code, wait, attempt, max_retries, r.text[:300])
            time.sleep(wait)
            delay = min(delay * 2, 60)
            continue
        if not r.ok:
            log.error("LLM %s response: %s", r.status_code, r.text[:500])
        r.raise_for_status()
        return r
    r.raise_for_status()  # exhausted retries on a retryable status
    return r


def call_llm(cfg: dict, system: str, prompt: str, max_tokens: int = 600) -> str:
    provider = cfg["llm"]["provider"]

    if provider == "gemini":
        # Google Gemini — FREE tier available
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{cfg['llm'].get('model', 'gemini-2.0-flash')}:generateContent"
            f"?key={cfg['llm']['api_key']}"
        )
        r = _post_with_retry(
            url,
            json_body={
                "system_instruction": {"parts": [{"text": system}]},
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": max_tokens},
            },
        )
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

    elif provider == "claude":
        # Anthropic Claude — paid, ~$0.01-0.03 per job, best proposal quality
        r = _post_with_retry(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": cfg["llm"]["api_key"],
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json_body={
                "model": cfg["llm"].get("model", "claude-sonnet-4-20250514"),
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        return r.json()["content"][0]["text"].strip()

    elif provider == "groq":
        # Groq — FREE tier, fast, no billing/region lock. OpenAI-compatible API.
        r = _post_with_retry(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {cfg['llm']['api_key']}",
                "Content-Type": "application/json",
            },
            json_body={
                "model": cfg["llm"].get("model", "llama-3.3-70b-versatile"),
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            },
        )
        return r.json()["choices"][0]["message"]["content"].strip()

    else:
        raise ValueError(f"Unknown llm provider: {provider}")


def score_job(cfg: dict, job: dict) -> dict:
    prompt = f"""Freelancer background:
{cfg['freelancer_profile']}

Job posting:
Title: {job['title']}
{job['description'][:4000]}

Respond ONLY with JSON, no markdown fences:
{{"score": <1-10 fit score>, "reason": "<one sentence>", "red_flags": "<comma-separated or 'none'>"}}"""
    raw = call_llm(cfg, "You are a strict job-fit evaluator.", prompt, 200)
    raw = re.sub(r"```(json)?|```", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"score": 5, "reason": "could not parse score", "red_flags": "none"}


def generate_proposal(cfg: dict, job: dict) -> str:
    prompt = f"""Freelancer background:
{cfg['freelancer_profile']}

Job posting:
Title: {job['title']}

{job['description'][:4000]}

Write the proposal now."""
    return call_llm(cfg, PROPOSAL_SYSTEM, prompt, 600)


# ---------------------------------------------------------------------------
# Notification delivery — Telegram and/or Slack
# ---------------------------------------------------------------------------

def send_telegram(cfg: dict, text: str) -> None:
    url = f"https://api.telegram.org/bot{cfg['telegram']['bot_token']}/sendMessage"
    chunks = [text[i: i + 4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        r = requests.post(
            url,
            json={
                "chat_id": cfg["telegram"]["chat_id"],
                "text": chunk,
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if not r.ok:
            log.error("Telegram send failed: %s", r.text)


def send_slack(cfg: dict, text: str) -> None:
    """Post to a Slack channel via an Incoming Webhook URL."""
    url = cfg["slack"]["webhook_url"]
    # Slack renders best with a code block; chunk to stay under message limits.
    chunks = [text[i: i + 3500] for i in range(0, len(text), 3500)]
    for chunk in chunks:
        r = requests.post(url, json={"text": chunk}, timeout=15)
        if not r.ok:
            log.error("Slack send failed: %s", r.text)


def notify(cfg: dict, text: str) -> None:
    """Send an alert to every notification channel that is configured."""
    if cfg.get("telegram", {}).get("bot_token"):
        send_telegram(cfg, text)
    if cfg.get("slack", {}).get("webhook_url"):
        send_slack(cfg, text)


def format_alert(job: dict, score: dict, proposal: str) -> str:
    return (
        f"🔔 NEW JOB — fit {score['score']}/10\n"
        f"{'─' * 28}\n"
        f"📌 {job['title']}\n\n"
        f"🧠 Why: {score['reason']}\n"
        f"🚩 Red flags: {score['red_flags']}\n\n"
        f"🔗 {job['link']}\n"
        f"{'─' * 28}\n"
        f"✍️ DRAFT PROPOSAL (review before sending!):\n\n"
        f"{proposal}"
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_once(cfg: dict, seen: set, min_score: int) -> None:
    """Single pass: check inbox, process new jobs, exit. Used by --once mode
    (e.g. GitHub Actions scheduled runs)."""
    jobs = fetch_new_alert_emails(cfg)
    new_jobs = [j for j in jobs if j["id"] not in seen]
    if new_jobs:
        log.info("%d new job(s) from email alerts.", len(new_jobs))

    try:
        for job in new_jobs:
            seen.add(job["id"])
            passes, reason = job_passes_filter(job, cfg)
            if not passes:
                log.info("Skipped '%s' (%s)", job["title"][:50], reason)
                continue

            try:
                score = score_job(cfg, job)
                if score["score"] < min_score:
                    log.info("Low fit %s/10 — skipping '%s'",
                             score["score"], job["title"][:50])
                    continue

                proposal = generate_proposal(cfg, job)
                notify(cfg, format_alert(job, score, proposal))
                log.info("Alert sent: '%s' (fit %s/10)",
                         job["title"][:50], score["score"])
            except Exception:
                # One job failing (e.g. LLM error) shouldn't abort the pass.
                log.exception("Failed to process '%s' — continuing",
                              job["title"][:50])
    finally:
        # Persist progress even if something blows up mid-pass.
        save_seen(seen)


def config_from_env() -> dict | None:
    """Build config from environment variables (for GitHub Actions, where
    secrets are injected as env vars instead of a config.json on disk)."""
    import os
    if not os.environ.get("EMAIL_ADDRESS"):
        return None
    return {
        "poll_seconds": 60,
        "max_emails": int(os.environ.get("MAX_EMAILS", "10")),
        "email": {
            "address": os.environ["EMAIL_ADDRESS"],
            "app_password": os.environ["EMAIL_APP_PASSWORD"],
            "imap_server": os.environ.get("IMAP_SERVER", "imap.gmail.com"),
        },
        "llm": {
            "provider": os.environ.get("LLM_PROVIDER", "gemini"),
            "api_key": os.environ["LLM_API_KEY"],
            "model": os.environ.get("LLM_MODEL", "gemini-2.0-flash"),
        },
        "telegram": {
            "bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            "chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
        },
        "slack": {
            "webhook_url": os.environ.get("SLACK_WEBHOOK_URL", ""),
        },
        "filters": {
            "include_keywords": [k.strip() for k in
                                 os.environ.get("INCLUDE_KEYWORDS", "").split(",") if k.strip()],
            "exclude_keywords": [k.strip() for k in
                                 os.environ.get("EXCLUDE_KEYWORDS", "").split(",") if k.strip()],
            "min_budget": float(os.environ.get("MIN_BUDGET", "0")),
            "min_fit_score": int(os.environ.get("MIN_FIT_SCORE", "6")),
        },
        "freelancer_profile": os.environ["FREELANCER_PROFILE"],
    }


def main() -> None:
    once_mode = "--once" in sys.argv
    cfg = config_from_env() or load_config()
    seen = load_seen()
    poll_seconds = cfg.get("poll_seconds", 60)
    min_score = cfg["filters"].get("min_fit_score", 6)

    if once_mode:
        log.info("Running single pass (--once mode).")
        run_once(cfg, seen, min_score)
        return

    log.info("FREE pipeline started. Polling inbox every %ss.", poll_seconds)
    notify(cfg, "✅ Upwork pipeline (free edition) is live.")

    while True:
        try:
            jobs = fetch_new_alert_emails(cfg)
            new_jobs = [j for j in jobs if j["id"] not in seen]
            if new_jobs:
                log.info("%d new job(s) from email alerts.", len(new_jobs))

            for job in new_jobs:
                seen.add(job["id"])
                passes, reason = job_passes_filter(job, cfg)
                if not passes:
                    log.info("Skipped '%s' (%s)", job["title"][:50], reason)
                    continue

                score = score_job(cfg, job)
                if score["score"] < min_score:
                    log.info("Low fit %s/10 — skipping '%s'",
                             score["score"], job["title"][:50])
                    continue

                proposal = generate_proposal(cfg, job)
                notify(cfg, format_alert(job, score, proposal))
                log.info("Alert sent: '%s' (fit %s/10)",
                         job["title"][:50], score["score"])

            save_seen(seen)

        except KeyboardInterrupt:
            log.info("Stopping.")
            break
        except Exception:
            log.exception("Loop error — continuing after delay.")

        time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
