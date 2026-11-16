# Sigma DFS (NBA / DraftKings)

Your Sigma DFS optimizer, now with **its own projections**. You no longer have to upload a
projections CSV: a GitHub Actions job builds projections from NBA box scores, refreshes them
**every 15 minutes** as the injury report changes, freezes each game when it locks, and
sends a **phone notification** whenever a player's injury status changes.

```
GitHub Actions (every 15 min)          GitHub Pages
┌──────────────────────────────┐       ┌──────────────────────────────┐
│ DraftKings salaries/slates   │       │ Sigma DFS app                │
│ NBA.com box scores (history) │──────▶│  • loads projections itself  │
│ Official NBA injury report   │ data  │  • re-checks every 2 minutes │
│ ESPN injuries + Vegas lines  │       │  • banner when numbers move  │
│ → model → projections.csv    │       └──────────────────────────────┘
│ → ntfy push on status change │──────▶ 📱 your phone
└──────────────────────────────┘
```

## One-time setup (about 10 minutes)

1. **Create a public GitHub repository** and push this folder to it.
   Public repos get unlimited free Actions minutes; at 15-minute intervals a private repo
   would use up the free 2,000 minutes/month in under two weeks.
2. **Turn on Pages:** repo **Settings → Pages → Build and deployment → Source: GitHub Actions**.
3. **Phone notifications:**
   - Install the free **ntfy** app ([iOS](https://apps.apple.com/app/ntfy/id1625396347) /
     [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)).
   - Make up a long, random topic name (anyone who knows it can read it), for example
     `sigma-nba-7f3k9q2xw8`, and **subscribe** to it in the app.
   - In the repo: **Settings → Secrets and variables → Actions → New repository secret**,
     name `NTFY_TOPIC`, value = your topic.
   - Optional: under the **Variables** tab add `SITE_URL` = your Pages URL
     (`https://<you>.github.io/<repo>/`) so tapping a notification opens the optimizer.
4. **First run:** **Actions → Update projections → Run workflow**. The first run downloads
   last season's box scores as well (about 5–10 minutes). After that it runs on its own.
5. Open `https://<you>.github.io/<repo>/`. The **Live Projections** card shows the slate,
   last update and next lock.

You can still upload your own projections CSV at any time; it overrides the live numbers
until you press **Use live**.

## How it works

**Live loop.** GitHub starts a run every 15 minutes (8am to 1am Eastern). Each run polls every
60 seconds for 14 minutes, so news is picked up within about a minute:

| What | Source | Verification |
|---|---|---|
| Slates, salaries, positions, DK player IDs, lock times | DraftKings lobby + draftables API | the same IDs DK uses for uploads |
| Injury status | Official NBA injury report (PDF) → ESPN → DraftKings | official report wins once a team has submitted |
| Starting lineups | NBA.com "Today's Lineups" feed (Confirmed / Expected) | Sigma projects the five until a team posts |
| Referee crews | official.nba.com referee assignments | |
| History (every player-game, refs, arenas) | NBA.com box scores (cdn.nba.com) | official league data |
| Historical DK salaries | RotoGuru DK archive | a night is used only if its DK points match NBA.com box scores for ≥95% of players |
| Spreads / totals | ESPN | |

Results are pushed to the `data` branch. The app reads them straight from GitHub, pinned
to the newest commit, so there's no redeploy delay and no stale cache.

**Alerts (phone + computer).** Through ntfy you get a push for every injury status change
and for every starting-lineup event: expected lineup posted, lineup confirmed, and any
change. Surprise starters and benched regulars are flagged, along with the teammates whose
projections moved. On a computer, open the site and press **🔔 Desktop alerts**, or
subscribe to the same topic at ntfy.sh/app.

**Projections.** minutes × DK points/minute × usage × game environment × opponent
position × situational factors:
- **Role-aware minutes.** Starter and bench minutes are estimated separately, so a
  confirmed surprise start (or benching) moves the projection right away. Each team is
  re-balanced to 241 minutes around who is actually available.
- **Teammate with/without.** How each player actually produced in past games without the
  teammates who are out today, blended with a general usage model.
- **Fitted situational factors.** Back-to-backs, 3+ days of rest, travel distance,
  east/west time-zone changes, altitude (DEN/UTA), home court and referee-crew scoring
  tendency. Their sizes are fitted from every past player-game, and they're only used if
  they reduce error on the most recent 20% of games (out-of-sample backtest; results are
  in `meta.json`).
- **Minutes restrictions.** "Reconditioning" or "minutes restriction" in the injury report caps minutes.
- Vegas implied totals, blowout risk, defense vs. position, returning-from-injury limits.

**Lineups.** For every slate the optimizer:
1. Builds **10,000 unique lineups**, each the exact best lineup for one simulated outcome of
   the slate (HiGHS integer programming; Hall's-theorem position model, verified against
   brute force).
2. Simulates **1,000,000 contests**: your 10,000 lineups plus 10,000 opponent lineups drafted
   from projected ownership. Player scores are skewed (gamma) and correlated: teammates and
   opponents share game/team effects measured from history, and Questionable players
   sometimes sit.
3. Ranks the lineups by **win probability** (share of the million contests won).

When news hits, a fast pool is published in about a minute, then replaced by the exactly
solved pool about 2–3 minutes later. Likes carry over as long as the lineup is still in
the pool.

**Winning patterns.** For every verified night: the 20 highest-scoring legal DK lineups
(solved exactly), their structure (salary used, price tiers, team/game stacks,
bring-backs) and *why* each player hit (teammate out, surprise start, minutes spike,
overtime, blowout, high-scoring game, actual vs. projection). The salary floor and stacking
limits applied to your lineups come from this data. To add real winners with real
ownership, drop DraftKings "contest standings" CSVs into `contest_results/`.

**Using it on game day.**
1. Open the site. The **Lineups** tab shows the pool ranked by win %, 50 per page.
2. Like lineups one at a time, a whole page, or **Like Top… → 20 / 50 / 150 / 500 / all**.
3. **⬇ Export** downloads `DK_upload_N_lineups.csv`: slot headers plus DraftKings player
   IDs, ready for DraftKings' lineup upload. To fill entries you already have, load your
   DK entries CSV first and export fills it in.
4. When news breaks, your phone buzzes, the pool rebuilds itself, and a banner shows what
   moved. Like, export, upload.

### Honest limits
- GitHub's scheduler can start a run late (usually minutes). Inside a run, polling is every 60s.
- An exact rebuild takes ~2–3 minutes of compute per slate; the fast pool covers that gap.
- Ownership is a model estimate (no free real-time ownership source exists).
- Public data has no complete archive of real contest winners. The pattern analysis uses
  each night's top possible lineups (what large-field GPP winners approximate) and,
  optionally, your own contest results.

## Running locally

```bash
pip install -r requirements.txt
```

```bash
python -m pipeline.run --no-notify
```

```bash
cd web && npm install && npm run dev
```

`npm run dev` serves `../data` to the app. Tests (offline, no network): `pip install pytest && python -m pytest -q`.

## Troubleshooting

- **A player's name doesn't match between sites:** add `"variant name": "canonical name"`
  to `name_aliases.json`.
- **GitHub's scheduler is sometimes late** (5–15 minutes at busy times). Use
  **Run workflow** for an immediate update.
- **GitHub pauses scheduled workflows after 60 days with no repository activity.** If you get
  that email, click "Enable workflow" in the Actions tab.
- Generated data (history, state, outputs) lives on the `data` branch, kept as one rolling commit.
