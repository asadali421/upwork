# Upwork Pipeline — 100% Free Edition

Zero subscriptions. The feed source is **Upwork's own free email job alerts**, read from your Gmail via IMAP.

```
Upwork saved search → instant email alert → Gmail (free)
   → pipeline_free.py polls inbox (free)
   → Gemini API scores + drafts proposal (free tier)
   → Telegram alert (free)
   → you review & submit
```

Total monthly cost: **$0** (runs on your own PC) or ~$4–5 if you want a VPS for 24/7 uptime. A Raspberry Pi or an old laptop left on also works for $0.

---

## Setup (~30 minutes)

### Step 1 — Turn on Upwork email alerts (the free feed)
1. On Upwork, run a job search with your keywords and filters.
2. Click **"Save Search"**.
3. In the saved search settings, set email alerts to the most frequent option available (instant/daily — choose instant if offered for your search).
4. Repeat for 2–3 keyword variations to widen coverage.

> Tip: Make a Gmail filter that labels these Upwork emails and keeps them unread in the inbox until the script processes them.

### Step 2 — Gmail App Password (so the script can read your inbox)
1. Google Account → Security → enable **2-Step Verification** (required).
2. Then Security → **App passwords** → create one for "Mail".
3. Put the 16-character password in `config.json` → `email.app_password`.

> The script only READS emails from upwork.com and marks them as seen. It never sends or deletes anything.

### Step 3 — Free LLM key (Google Gemini)
1. Go to **aistudio.google.com** → Get API key (free).
2. Free tier limits are generous enough for hundreds of job evaluations per day.
3. Paste the key into `config.json` → `llm.api_key`, keep `"provider": "gemini"`.

> Want better proposal quality later? Switch `"provider": "claude"` with an Anthropic API key (~$0.01–0.03 per job). The script supports both.

### Step 4 — Telegram bot (free, 2 minutes)
1. Message **@BotFather** on Telegram → `/newbot` → copy the token.
2. Send your new bot any message to open the chat.
3. Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser, find `"chat":{"id": ...}` and copy the number.
4. Put both values in `config.json`.

### Step 5 — Run it
```bash
cp config_free.example.json config.json   # then edit it
pip install requests
python pipeline_free.py
```
(Only dependency is `requests` — IMAP and email parsing use Python's standard library.)

---

## Honest trade-offs vs. the paid version

| | Free (email alerts) | Paid (Vollna etc.) |
|---|---|---|
| Cost | $0 | ~$15+/mo |
| Latency | minutes (email delivery) | <1 minute |
| Job detail in alert | snippet only | full description |
| Filtering depth | your keywords | 30+ parameters |

The email snippet is shorter than the full job post, so the fit score and proposal are based on partial info. **Workflow tip:** when an alert looks good, open the job link, copy the full description, and paste it to Claude/Gemini for a refined proposal before submitting. Still takes under 5 minutes total.

## Tuning
- Too noisy → raise `min_fit_score` or tighten `include_keywords`.
- Proposals generic → make `freelancer_profile` more specific (numbers, named projects, outcomes).
- Proposal voice → edit `PROPOSAL_SYSTEM` at the top of `pipeline_free.py`.

## What this deliberately does NOT do
No auto-submission. Auto-bidding violates Upwork's ToS and risks permanent suspension of your account. You review every proposal and click submit yourself — which also makes your proposals better than bot spam.
