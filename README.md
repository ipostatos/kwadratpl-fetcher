# kwadratpl-fetcher

Public listings fetcher for **[Kwadrat PL](https://t.me/KwadratPLBot)** — a Telegram
Mini App for finding rental housing in Poland (flats, rooms & flat-shares, short stays).

Every ~5 minutes a GitHub Actions cron runs `fetch_olx.py`, which collects fresh
rental listings from the public OLX.pl API (6 cities × 3 categories), normalises
them and POSTs the result to the Kwadrat PL backend (`/api/listings`,
authenticated with a secret header). The backend diffs the data and delivers
instant Telegram notifications to subscribed users.

This repo is public so the workflow runs on GitHub's free unlimited minutes for
public repositories. It contains no secrets — the ingest token lives in Actions
secrets, the bot token never leaves the backend server.
