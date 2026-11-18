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

## Live updates run on your Mac

The live updater runs on your Mac and publishes to the `data` branch; the website reads
it from there. It uses sources that serve automated requests: DraftKings' lobby and
salary files, ESPN box scores, and NBA.com's lineups, injury report and referee pages.
(DraftKings' player API and NBA.com's stats CDN refuse automated requests, so they
aren't used.)

One-time setup (Terminal):

```bash
git clone https://github.com/burgett-1/sigma-dfs.git ~/sigma-dfs
```

```bash
cd ~/sigma-dfs && bash mac/setup.sh
```

The script creates a private Python environment, checks out the data folder, checks
that your Mac can push to GitHub (sign in once with GitHub Desktop or `gh auth login`
if it can't), asks for your ntfy topic, and installs a background job. The job runs from
8am to 2am Eastern: it polls every 60 seconds and fully refreshes at least every 10
minutes. It keeps the Mac awake while running and restarts on its own after a reboot or
login.

- Watch it: `tail -f ~/Library/Logs/sigma-dfs.log`
- Stop it: `bash mac/uninstall.sh`
- The app's Live Projections card and `meta.json` → `sources` show whether each data
  source is responding.

## Running simulations, odds and markets

**Run simulations.** Projections update continuously (about every minute, and when lines
move). Lineups are built only when you press **▶ Run simulations** on the Lineups tab or
in the Simulation card. The site sends the request to your Mac over your private control
channel; the Mac builds 10,000 lineups and simulates 1,000,000 contests for that slate
with the projections at that moment, publishes them (about a minute), then upgrades them
to exactly solved lineups (2–3 more minutes). If projections change afterwards, the card
says so and you can run again.

**Sharp odds in the projections.** Add a key from The Odds API (the-odds-api.com) on the
Mac:

```bash
echo 'ODDS_API_KEY=your_key_here' >> ~/.sigma-dfs.env
```

Then restart the updater:

```bash
launchctl kickstart -k gui/$(id -u)/com.sigmadfs.updater
```

Game lines (every 2 minutes) and player props for points, rebounds, assists, threes,
blocks, steals and turnovers (every 10 minutes) come from Pinnacle first, then low-margin
books, then the major US books. Prices are de-vigged and turned into expected stats, and
each projection moves 70% of the way to the market for every stat the market prices
(`market_weight` in pipeline/config.py). Sharp team totals replace ESPN's line. Change the
refresh timers with `ODDS_LINES_MINUTES` / `ODDS_PROPS_MINUTES` in `~/.sigma-dfs.env`.
API usage grows with refresh frequency (props are billed per game), and the credits left
are shown on the Betting Markets tab.

**Betting Markets tab** (loads when you open it): each game's fair spread, total and win
chance, implied team totals, prices by book and today's line movement; every player prop
with the market's fair number next to Sigma's own and the edge between them; and each
team's record against the spread and over/under. The historical records come from ESPN's
archived closing lines on every box score, for this season and last.

**Exposures.** Every time you like lineups (one at a time, a page, or Like Top N), the
Lineups tab shows each player's share of your liked lineups, next to the suggested
exposure from the winning-lineup history. The Exposures tab lists every player and lets
you cap exposures.

## How it works

**Live loop.** GitHub starts a run every 15 minutes (8am to 1am Eastern). Each run polls every
60 seconds for 14 minutes, so news is picked up within about a minute:

| What | Source | Verification |
|---|---|---|
| Slates, salaries, positions, DK player IDs, lock times | DraftKings lobby + DraftKings salary file ("Export to CSV") | the same IDs DK uses for uploads |
| Injury status | Official NBA injury report (PDF) → ESPN → DraftKings | official report wins once a team has submitted |
| Starting lineups | NBA.com "Today's Lineups" feed (Confirmed / Expected) | Sigma projects the five until a team posts |
| Referee crews | official.nba.com referee assignments | |
| History (every player-game, refs, starters) and live in-game scoring | ESPN box scores | NBA.com's data servers refuse automated requests |
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
- **Referee crews are measured leaving out the game being predicted**, and opponent
  defense is a control in the fit, so neither can leak the result or pose as altitude/rest.

**Every column is checked against real results (daily backtest).** Once a day the Mac
replays ~40 past nights with only what was known before tip-off (earlier box scores, who
sat, who started, the closing spread/total) and compares every column with what happened:
projection, minutes, points per minute, std dev, floor, ceiling, boom and bust. From that
it fits a calibration the live projections use:
- projection level and projected minutes, by range (e.g. players projected ~34 that really
  averaged ~37 are moved up);
- the spread of outcomes by projection level (low projections vary far more, relatively);
- floor = 10th percentile, ceiling = 90th percentile, boom/bust probabilities, all from
  the same skewed distribution the contest simulator uses.

It is fitted on half the nights and scored on the other half; the report is published
as `audit.json`. On the current history (6,300 player-games), held-out nights:

| | before | after |
|---|---|---|
| Results above the ceiling (target 10%) | 17.7% | 11.5% |
| Results below the floor (target 10%; includes surprise DNPs) | 27.4% | 14.1% |
| Spread of errors vs. stated std dev (target 1.0) | 2.57× | 1.05× |
| DK points mean absolute error | 7.23 | 7.11 |

**Lineups.** For every slate the optimizer:
1. Builds **10,000 unique lineups**, each the exact best lineup for one simulated outcome of
   the slate (HiGHS integer programming; Hall's-theorem position model, verified against
   brute force).
2. Simulates **1,000,000 contests**: your 10,000 lineups plus 10,000 opponent lineups drafted
   from projected ownership. Player scores are skewed (gamma) and correlated: teammates and
   opponents share game/team effects measured from history, and Questionable players
   sometimes sit.
3. Ranks the lineups by **win probability** (share of the million contests won). A win
   tied with an identical field lineup is split with it (the card shows **dupes**).
4. Orders a **best portfolio**: each pick is the lineup that adds the most simulated
   contests in which at least one of your lineups beats the entire field. It still leans
   on the chalk where the chalk wins, but spreads into the low-owned outcomes that
   actually take down GPPs, exactly as far as that pays. **Like Top… → Portfolio 20 / 50 /
   150** likes them, and shows the chance that set wins.

**Why lineups were too concentrated before, and what the data says.** The backtest showed
outcome variance was underestimated by about 2.5×, so every simulated slate looked alike
and the same players kept being optimal. With the measured variance, on a real main slate
the best 150 lineups use 122 different players instead of 82, the most-used player drops
from 57% to 39%, average total ownership from 136% to 122%, and each lineup carries ~3.8
players under 10% owned. Forcing extra low-owned "leverage" lineups was also tested: the
contest sim scored them lower (top-150 win coverage 8.40% → 8.11% → 7.72%), so it is off
(`SIM["leverage_share"]` in `pipeline/config.py`).

When news hits, a fast pool is published in about a minute, then replaced by the exactly
solved pool about 2–3 minutes later. Likes carry over as long as the lineup is still in
the pool.

**Winning patterns.** For every verified night: the 20 highest-scoring legal DK lineups
(solved exactly), their structure (salary used, price tiers, team/game stacks,
bring-backs) and *why* each player hit (teammate out, surprise start, minutes spike,
overtime, blowout, high-scoring game, actual vs. projection). The salary floor and stacking
limits applied to your lineups come from this data. Your own contest results are
private: see **My contests (admin only)** below.

**Using it on game day.**
1. Open the site. The **Lineups** tab shows the pool ranked by win %, 50 per page.
2. Like lineups one at a time, a whole page, or **Like Top… → 20 / 50 / 150 / 500 / all**.
3. **⬇ Export** downloads `DK_upload_N_lineups.csv`: slot headers plus DraftKings player
   IDs, ready for DraftKings' lineup upload. To fill entries you already have, load your
   DK entries CSV first and export fills it in.
4. When news breaks, your phone buzzes, the pool rebuilds itself, and a banner shows what
   moved. Like, export, upload.


## Slates, tools and alerts (in the app)

**Pick your slate first.** The app opens on the DraftKings slates for today, pulled straight
from DraftKings: Main, Night/Turbo/Early and each Showdown, each with its own games and lock
times. Everything after that is for the slate you pick: projections, the 10,000-lineup pool and
its simulations, the spotlight, the patterns, late swap and the lineup simulator. Showdown is
fully supported: Captain at 1.5× points and salary, both teams required, and export uses the
captain's own DraftKings ID.

**Starters.** A green **S** marks a confirmed starter; a yellow **S** marks a projected or
expected starter. They update as teams post and change lineups. ⚡ marks a surprise starter,
"NOT STARTING" a benched regular, and 🔒 a player whose game has tipped.

**Filters.** On the lineup pool (and in Late Swap) you can search by player name and filter by
team chips, alongside the existing filters (ownership, projection, min-unique, team stacks,
must include / exclude, lineup type).

**Winning Patterns** (tailored to the selected slate's size: 1–3, 4–6 or 7+ games):
- *Tonight's spotlight*: connections between the history and tonight, strongest first and
  highlighted. Examples: "Without X, he averaged 46 DK pts in 7 games (vs 34 with) and hit 6×
  tonight's salary in 4", or a player whose profile historically filled a large share of
  winning lineups while the field is light on him. Same-situation cards only appear if the
  difference passes a statistical test (Welch's t ≥ 2.5), so a hot streak alone doesn't count.
- *Exposure guide*: for every player, the share of historical winning lineups that contained
  players with the same pre-game profile (slate size × value rank × teammate out). Compare it
  with your exposure in liked lineups (also shown on the Exposures tab).
- *What wins on slates like this*: salary used, price tiers, stacking, and why players hit.

**Late Swap** (its own tab). After lock, upload the entries file from DraftKings (My Contests →
Edit Entries → Download). Players whose games have started are locked 🔒 and never move. Every
other slot is re-optimised with the same simulation model, using official live scoring for
games in progress. The tab shows average ROI before and after, per-entry ROI, and the same
filters; you pick swaps and download a file ready to upload back to DraftKings. Projections and
live scores keep updating after lock, until the last game tips.

**Lineup Simulator** (its own tab). Build a lineup by hand: search the player list and press the
green **+**. The red **−** removes a player and 🔒 keeps him. **Clear** removes everyone who
isn't locked; a locked player has to be unlocked before he can be removed. **Complete lineup**
fills the open spots with the highest projection that fits the salary cap. **Run simulation**
plays the lineup through simulated contests and shows ROI, cash rate, top-1% rate, win % and
average score.

## My contests (admin only, private)

On DraftKings open a finished contest and press **Export Lineups to CSV** (the file is
`contest-standings-<id>.csv` or `.zip`). The Mac picks it up from **~/Downloads** within
about 10 minutes and keeps a copy in **~/SigmaDFS-Private/** — outside the repository and
never published, so users of the site never see it. (If macOS blocks the updater from
reading Downloads, drop the file into `~/SigmaDFS-Private/drop/` instead.)

Open your report on the Mac:

    open ~/SigmaDFS-Private/report.html

For each contest: entries, winning score, top-1% cutoff, the winning lineup with real
ownership, how many sub-10%-owned players the top 1% had versus the field, and where your
entries finished. Your username is detected as the one name present in every contest you
export; to set it yourself add `SIGMA_DK_USERNAME=yourname` to `~/.sigma-dfs.env`.

How it feeds lineup generation (a small factor, as it should be): before each lock the Mac
privately saves the model's projected ownership; your exports' real %Drafted is compared
with it and a correction is fitted, shrunk toward "no change" by sample size (weight =
players / (players + 600), never more than half). The simulated field your lineups play
against uses the corrected ownership, which is what shapes the game theory.

### Honest limits
- GitHub's scheduler can start a run late (usually minutes). Inside a run, polling is every 60s.
- An exact rebuild takes ~2–3 minutes of compute per slate; the fast pool covers that gap.
- Ownership is a model estimate (no free real-time ownership source exists).
- Late swap and the lineup simulator run in your browser. The defaults (20,000 simulations,
  2,000 sampled opponents scaled to your contest size) finish in seconds to a minute on a phone.
- Historical patterns cover classic slates; showdown uses the same projections and simulation.
- Public data has no complete archive of real contest winners. The pattern analysis uses
  each night's top possible lineups (what large-field GPP winners approximate); your own
  contest results are analysed privately (above).
- Historical DK salaries come from RotoGuru. If its pages can't be read, the Mac saves one
  raw page to `debug/rotoguru_sample.html` so the parser can be fixed, and unverified nights
  are retried after a parser update.

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
