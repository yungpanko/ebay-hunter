# eBay Hunter

A secondhand shopping companion for eBay. You tell it roughly what you want, it helps you turn that into a
researched search, then it keeps watch every couple of hours and emails you the listings worth your attention,
with the reasons. Reply to an email and it learns from what you say.

It never buys anything. It only reads public eBay listings through eBay's official API.

## Quick start: build and try it on your own computer

You need Python 3.10+, an Anthropic API key (starts with `sk-ant-api`), and your eBay production keys.

```
git clone https://github.com/yungpanko/ebay-hunter && cd ebay-hunter
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then open .env and paste your keys (git ignores this file)
```

Then, any time:

```
source .venv/bin/activate
python -m hunter new vintage jeans   # build a search by conversation
python -m hunter dev                 # hunt once (no email sent), then open the roundup page in your browser
```

`hunter dev` writes the roundup to `data/outbox/` and serves the roundup and per-find chat pages at
http://127.0.0.1:8000 until you press Ctrl+C. Useful flags: `--no-hunt` just reopens the pages, `--reset`
forgets which listings were seen so you can re-hunt what's live now.

When it feels right, save searches for the schedule with `git add data && git commit -m "searches" && git push`,
and see "Host it on Railway" below for the always-on version.

## How it works

1. **Build a search by talking.** `python -m hunter new vintage jeans` starts a conversation. It asks a few
   questions at a time and says why each matters (Made in USA? which eras? how do you like the rise to fit?),
   researches on the web when it helps, checks eBay's real categories and item specifics, previews what a query
   actually returns, then saves the search as a YAML file in `data/searches/`. Anything about you that applies
   beyond this search (measurements, taste, how you buy) goes into `data/profile.md`.
2. **Watch.** Every 2 hours a GitHub Actions job runs each active search through the eBay Browse API, newest
   first, and drops anything it has already seen.
3. **Judge.** New listings get a quick look at their title, price and main photo. Promising ones (and anything
   off-brief that looks like your taste) get a close look: every photo, the item specifics and the description,
   checked against your must-haves, measurement ranges, price guidance and past reactions. It looks for what
   sellers miss, like a Made in USA tag that only shows in photo 4.
4. **Deliver.** One roundup email per run, best first. Each find gets a tier (Strong buy, Worth a look, Long shot), a 1-10 score, a one-line why, the eBay link, what it spotted
   in the photos, fit, price take, concerns, and a ready-to-send seller question when something is missing.
5. **Learn.** Reply in your own words, by number ("1: love it", "3: rise looks short", "2: bought it", "not my
   era"). The next run reads replies, logs them, and rewrites the "Learned from feedback" part of your profile.

## Files

```
data/profile.md            who you are as a shopper (yours to edit too)
data/searches/*.yaml       one file per saved search (yours to edit too)
data/state/seen.json       listings already handled, per search
data/state/reviews.jsonl   every close review and its verdict
data/state/feedback.jsonl  your reactions
hunter/                    the code
```

## Commands

```
python -m hunter new <idea>          build a search by conversation (carries over what it knows about you)
python -m hunter new --fresh <idea>  same, but start from scratch
python -m hunter edit <slug> [change] refine a search by conversation
python -m hunter list                 show searches
python -m hunter pause|resume <slug>
python -m hunter run [--dry-run]      one hunt now (dry run writes emails to data/outbox/)
python -m hunter feedback <item_id> liked|passed|bought|note [words...]
python -m hunter cron                 what the schedule runs: read replies, learn, hunt
```

## Setup

### 1. eBay developer keys (do this first; approval can take a few days)

1. Sign up at https://developer.ebay.com and confirm your email.
2. Go to **Application Keys** and create a keyset in **Production**.
3. When asked about **Marketplace Account Deletion** notifications, choose to opt out: this app stores no eBay
   user data. (eBay can hold production keys until this is answered.)
4. Copy the **App ID (Client ID)** and **Cert ID (Client Secret)**. No user token or OAuth sign-in is needed.

The default Browse API limit is 5,000 calls a day; this uses roughly (queries per search + close reviews) per run,
well under that.

### 2. Anthropic API key

Create one at https://console.anthropic.com, add billing, and copy the key.

### 3. Email (Gmail app password)

1. On the Google account that should send the emails, turn on 2-Step Verification.
2. Create an app password at https://myaccount.google.com/apppasswords.
3. Make sure IMAP is on (Gmail settings, Forwarding and POP/IMAP) so replies can be read.

Any SMTP/IMAP provider works; set `SMTP_HOST`, `SMTP_PORT` and `IMAP_HOST` for non-Gmail accounts.

### 4. GitHub

In the repo, go to **Settings > Secrets and variables > Actions** and add these secrets:

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` | your Anthropic key |
| `EBAY_CLIENT_ID` | eBay App ID |
| `EBAY_CLIENT_SECRET` | eBay Cert ID |
| `SMTP_USER` | the Gmail address that sends |
| `SMTP_PASSWORD` | the app password |
| `HUNTER_EMAIL_TO` | where finds should go |

Optional variable `HUNTER_MODEL` picks the Claude model (default `claude-opus-5-5`).
Then **Actions > hunt > Run workflow** (tick dry run the first time) to try it.

### 5. Building searches

Locally (or in a GitHub Codespace):

```
pip install -r requirements.txt
export ANTHROPIC_API_KEY=... EBAY_CLIENT_ID=... EBAY_CLIENT_SECRET=...
python -m hunter new vintage jeans
git add data && git commit -m "new search" && git push
```

The watcher picks it up on its next run. On a search's first run only the newest few listings get a close
review, so you aren't flooded with the backlog.

## The web page (talk a find through)

Each find in a roundup links to a private page with the full breakdown (all photos, fit, price, concerns, a
seller question to copy) and a chat with the agent: will it fit, how it compares with the other finds, what it's
worth, and how to buy it well (an offer and walk-away number, or a max bid and when to place it). Reactions you
give there feed your profile like email replies do. It never buys or bids for you.

The page runs on a small always-on host, which also takes over the 2-hourly hunt from GitHub Actions. Data stays
in this repo: the server pulls before it reads and pushes finds, chats and learned taste back.

### Host it on Railway (about $5 a month)

1. Sign up at https://railway.com and pick the Hobby plan.
2. **New Project > Deploy from GitHub repo >** this repo. It finds `requirements.txt` and the `Procfile`.
3. In the service's **Settings**: under Networking choose **Generate Domain** (copy the URL); under Source set
   **Watch Paths** to `hunter/**` and `requirements.txt`, so the server's own data commits don't trigger redeploys.
4. Create a GitHub token at https://github.com/settings/personal-access-tokens: fine-grained, only this repo,
   **Contents: Read and write**.
5. In the service's **Variables**, add the same six secrets as GitHub Actions, plus:

| Variable | Value |
|---|---|
| `HUNTER_WEB_URL` | the Railway URL from step 3, e.g. `https://ebay-hunter-production.up.railway.app` |
| `HUNTER_WEB_SECRET` | any long random string (it signs the private links) |
| `HUNTER_GIT_REPO` | `owner/ebay-hunter` |
| `GITHUB_TOKEN` | the token from step 4 |

6. In GitHub, **Settings > Secrets and variables > Actions > Variables**, add `HUNTER_RUNNER` = `server` so
   the Actions schedule stands down and you don't get two roundups.

Check `https://<your-url>/healthz` says `ok`. The first hunt starts about 30 seconds after each deploy, then
every `HUNTER_HUNT_EVERY_HOURS` (default 2).

To try the page locally: `HUNTER_WEB_URL=http://127.0.0.1:8000 HUNTER_WEB_SECRET=dev python -m hunter serve`,
then open a link from a dry-run roundup in `data/outbox/`.

## Why these choices

- **Official eBay API over scraping:** stable, allowed, fast, and won't get blocked when it runs every two
  hours. The tradeoff: it has no sold prices, so price judgment leans on research saved in each search.
- **GitHub Actions as the server:** no machine to keep running or patch, free at this volume, and every change
  to your searches, profile and history is versioned in the repo. The tradeoff: scheduled runs can start a few
  minutes late, and the repo should stay private. If it outgrows this, the same `python -m hunter cron` runs on
  any small server.
- **Plain files:** you can read and hand-edit everything; nothing hidden in a database.

## Cost and tuning

Claude cost depends on how many new listings your searches turn up. A quick look at a batch of new listings
is a few cents; a close photo review is roughly 10 cents on the default model. Knobs (environment variables):

| Variable | Default | Effect |
|---|---|---|
| `HUNTER_MODEL` | `claude-opus-5-5` | `claude-sonnet-5-5` costs about half |
| `HUNTER_MAX_DEEP_REVIEWS` | 6 | close reviews per search per run |
| `HUNTER_MAX_PHOTOS` | 8 | photos read per close review |
| `HUNTER_RESULTS_PER_QUERY` | 50 | newest listings fetched per query |

Change the cron line in `.github/workflows/hunt.yml` to run more or less often.

## Tests

```
pip install pytest && python -m pytest
```

The tests run the full flow offline with stand-ins for eBay and Claude.
