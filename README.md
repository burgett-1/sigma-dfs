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
   last update and next lock. Upload your DraftKings entries file as before ("Player ID CSV")
   so exported lineups carry player IDs.

You can still upload your own projections CSV at any time; it overrides the live numbers
until you press **Use live**.

## What happens every 15 minutes

| Step | Source |
|---|---|
| New box scores added to the history | `cdn.nba.com` (box scores + schedule) |
| Today's classic slates, salaries, positions, lock times | DraftKings public lobby / draftables API |
| Injury statuses | Official NBA injury report PDF (authoritative once a team submits) → ESPN → DraftKings |
| Spreads and totals | ESPN scoreboard |
| Projections, ownership, opt% | `pipeline/model.py`, `pipeline/ownership.py` |
| Notifications | `ntfy.sh` |

- Runs from 8am to about 1am Eastern. **Players freeze at their game's tip-off**; later games
  keep updating. When every game has locked, the job stops doing work for the day.
- You get **one push per team per run**, listing every player whose status changed
  (for example `Questionable → OUT (Left ankle sprain)`) and the teammates whose projections
  moved the most. Newly ruled-out players come through as urgent pushes.
- The site checks for new data every 2 minutes. If numbers change after you've built lineups,
  a banner lists what moved so you can re-run.

## The projection model

`projection = minutes × DK points per minute × usage bump × game environment × opponent-position factor × home edge`

- **Minutes:** exponentially weighted recent games (half-life 4 games), blended with last
  season early in the year, and with salary for players with little history. Each team is
  then **re-balanced to 241 minutes** without the OUT/Doubtful players, so vacated minutes
  flow to teammates, mostly to the same position. Players returning from a 5+ game absence
  get a minutes cap. Back-to-backs and blowout risk (big spreads) trim starters' minutes.
- **Points per minute:** weighted recent rate, shrunk toward last season and the league
  average for the position (Bayesian, about 250 minutes of evidence for half weight).
- **Usage bump:** when rotation players are out, the remaining players' per-minute rate goes up
  in proportion to the production that left, with stars absorbing more. Long-term absences
  already reflected in recent games are not double-counted.
- **Game environment:** Vegas implied team total vs league average (falls back to the
  opponent's DK points allowed if no line is posted), plus a shrunk defense-vs-position factor.
- **Distribution:** each player's own game-to-game volatility (shrunk to a league prior)
  gives StdDev, Ceiling/Floor (90th/10th percentile), Boom% (≥ 6× salary) and Bust% (< 4×).
- **Ownership** is *estimated* (there's no free ownership feed) from value, projection and
  injury-driven bumps, scaled to 800% per slate. **Opt%** comes from simulating optimal
  lineups; **Leverage = Opt% − Own%**.

Every constant is in `pipeline/config.py`.

## Changes to the app (`web/src/App.jsx`)

- Loads projections, slates, game times and injury news automatically, with a slate picker.
- Re-checks every 2 minutes and shows a banner listing players whose projections or status changed.
- Injury badges (OUT / D / Q / P), a Minutes column, and 🔒 on locked games.
- **Multi-position fix:** the original kept only a player's first position, so a `PG/SG` could
  never fill SG. Players now fill every slot they're eligible for, which gives the simulator
  many more valid lineups.
- Game start times feed the existing late-swap slot ordering (later games go into G/F/UTIL).

`web/src/App.original.jsx` is your original file, kept for reference.

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
