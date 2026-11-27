"""NBA golden replay: proves the multi-sport changes leave NBA output unchanged.

Runs the NBA pipeline against a given code root (the working tree, or `main`
extracted with `git archive main | tar -x -C <dir>`) and writes every output to
a folder; `diff` then compares two such folders. Only APIs that exist on `main`
are used, so the same file drives both sides.

    python -I tests/nba_replay.py run  --code-root <root> --out <dir>                  # synthetic mode
    python -I tests/nba_replay.py run  --code-root <root> --out <dir> \
        --slate-csv data/slates/156302.csv --salaries data/state/salaries/157220.csv   # real-slate mode
    python tests/nba_replay.py diff A B [--self A2]

Synthetic mode reuses the tests/test_run.py fake network (synthetic league, fixed
clock, SIM sizes 300 / 20k / 300) and runs a projection pass, a `fast` and an
`exact` sim request, then a post-lock pass. Real-slate mode runs the shared
engine directly (build_pool, seeded sample_scores, generate_lineups,
build_slate_lineups fast and exact, estimate_ownership, optimal_rates) on one
classic and one showdown slate and saves arrays (.npy) and lineup JSON.

Diff rules: CSV and other files byte-for-byte; JSON after canonicalising with the
timestamp keys below ignored; .npy with np.array_equal. Every file in A must be
in B; B may add only the scheduler's files. `--self A2` (main replayed twice)
reports keys that differ main-vs-main and excludes them.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
from pathlib import Path

IGNORE_KEYS = {"updated_at", "generated_at", "at", "published_at", "last_run", "feed_ok_at",
               "contest_seconds"}   # contest_seconds: a timing, like generate_seconds
ALLOWED_NEW = {"sports.json", "state/dg_index.json", "state/scheduler.json"}


def _ignored(key: str) -> bool:
    return key in IGNORE_KEYS or key.endswith("_fetched")


# ─────────────────────────────────────────────────────────────────────────────
# run
# ─────────────────────────────────────────────────────────────────────────────
def _prepare(code_root: Path, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    env = {"SIGMA_DATA_DIR": out / "data", "SIGMA_PRIVATE_DIR": out / "_private",
           "SIGMA_CONTEST_IMPORT": out / "_import", "SIGMA_CACHE_DIR": out / "_cache"}
    for k, v in env.items():
        v.mkdir(parents=True, exist_ok=True)
        os.environ[k] = str(v)
    for k in ("CONTROL_TOPIC", "NTFY_TOPIC", "NTFY_TOKEN", "NTFY_SERVER", "X_BEARER_TOKEN", "ODDS_API_KEY",
              "SIGMA_DK_USERNAME", "SITE_URL"):
        os.environ.pop(k, None)
    sys.path.insert(0, str(code_root.resolve()))
    return env


def run_synthetic(code_root: Path, out: Path) -> None:
    _prepare(code_root, out)
    from datetime import date

    from pipeline import history, notify, patterns, run as runmod
    from pipeline.config import SIM
    from pipeline.sources import draftkings, espn, espn_box, injury_report, lineups, referees
    from tests import synthetic as syn

    today, tip = date(2026, 11, 15), syn.utc(2026, 11, 16, 0, 30)
    runmod._MEM.clear()
    SIM.update({"lineups": 300, "sims": 20000, "field_lineups": 300})
    lineups.fetch = lambda day: {}
    referees.fetch = lambda: {"BOS": ["Ref A"], "LAL": ["Ref A"]}
    history.save(syn.season_logs(today))
    draftkings.get_json = lambda url: (syn.lobby(tip) if "lobby" in url else
                                       syn.showdown_draftables(tip) if "/556/" in url else syn.draftables(tip))
    draftkings.get_text = syn.fake_dk_text
    espn_box.games_on = lambda d: []
    history.backfill_season = lambda logs, y: logs
    espn.odds_for_date = lambda d: {
        "BOS": {"implied": 116.0, "spread": -4.0}, "LAL": {"implied": 112.0, "spread": 4.0},
        "GSW": {"implied": 114.0, "spread": 1.0}, "NYK": {"implied": 115.0, "spread": -1.0}}
    espn.injuries = lambda: []
    star = syn.name("BOS", 0)
    report = {"rows": [{"name": star, "team": "BOS", "status": "QUESTIONABLE", "reason": "Knee",
                        "game_date": "11/15/2026"}]}
    injury_report.find_latest = lambda now, last=None: ("u", b"%PDF")
    injury_report.parse_pdf = lambda body: report["rows"]
    sent = []
    notify.send = lambda title, msg, *a, **k: sent.append((title, msg)) or True
    patterns.OUT = Path(os.environ["SIGMA_DATA_DIR"]) / "patterns.json"

    runmod.run(syn.utc(2026, 11, 15, 18))
    runmod.run(syn.utc(2026, 11, 15, 18, 1), sim_request={"slate_id": 555, "phase": "fast", "req": "r1"})
    runmod.run(syn.utc(2026, 11, 15, 18, 5), sim_request={"slate_id": 555, "phase": "exact", "req": "r1"})
    runmod.run(syn.utc(2026, 11, 15, 18, 6), sim_request={"slate_id": 556, "phase": "fast", "req": "r2"})
    report["rows"][0]["status"] = "OUT"
    runmod.run(syn.utc(2026, 11, 16, 0, 45))
    (out / "notifications.json").write_text(json.dumps(sent, indent=1))


def _rows_from_projection_csv(path: Path) -> list[dict]:
    """A published slates/{dg}.csv back into sim rows (what run.py passes to sim)."""
    from datetime import datetime
    status = {"Questionable": "QUESTIONABLE", "Probable": "PROBABLE", "Doubtful": "DOUBTFUL", "Out": "OUT"}
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                proj = float(r["Proj"] or 0)
            except ValueError:
                continue
            q = [float(x) for x in r["Quantiles"].split(";")] if r.get("Quantiles") else None
            st = r.get("GameStartUTC") or ""
            rows.append({
                "name": r["Name"], "salary": int(float(r["Salary"] or 0)), "position": r["Position"],
                "team_dk": r["Team"], "opp_dk": r["Opp"], "proj": proj,
                "mean": float(r["Mean"]) if r.get("Mean") else proj,
                "std_dev": float(r["StdDev"] or 0), "q": q, "own": float(r["Own"] or 0),
                "value": proj / (int(float(r["Salary"] or 1)) / 1000.0), "bump": 0.0,
                "draftable_id": int(r["DKID"]) if r["DKID"].isdigit() else 0,
                "cpt_id": int(r["CptID"]) if r.get("CptID", "").isdigit() else None,
                "cpt_salary": int(float(r["CptSalary"])) if r.get("CptSalary") else None,
                "status": status.get(r.get("Injury") or "", ""),
                "competition_id": r.get("CorrGame") or None,
                "start_utc": datetime.fromisoformat(st) if st else None,
            })
    return rows


def _rows_from_salary_csv(path: Path) -> tuple[list[dict], str]:
    from pipeline.sources import draftkings
    s = draftkings.Slate(draft_group_id=int(path.stem) if path.stem.isdigit() else 1, name="x",
                         start_utc=draftkings.parse_dt("2026-11-26T00:00:00Z"), game_count=0, featured=False)
    draftkings.load_salary_csv(s, path.read_text(encoding="utf-8-sig"))
    avg = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                avg[r["Name"]] = float(r.get("AvgPointsPerGame") or 0)
            except ValueError:
                pass
    rows = []
    for p in s.players:
        proj = avg.get(p["name"], 0.0)
        rows.append({**p, "proj": proj, "mean": proj, "std_dev": 0.35 * proj,
                     "value": proj / (p["salary"] / 1000.0) if p["salary"] else 0.0, "bump": 0.0,
                     "status": "", "own": 0.0})
    return rows, s.kind


def run_real(code_root: Path, out: Path, slate_csv: Path, salaries: Path | None) -> None:
    _prepare(code_root, out)
    import numpy as np

    from pipeline import ownership, sim
    from pipeline.config import SIM
    SIM.update({"lineups": 300, "sims": 20000, "field_lineups": 300, "portfolio_sims": 5000})
    corr = dict(SIM["default_corr"])
    cases = [("classic", _rows_from_projection_csv(slate_csv))]
    if salaries:
        rows, kind = _rows_from_salary_csv(salaries)
        cases.append((kind, rows))
    for kind, rows in cases:
        d = out / kind
        d.mkdir(parents=True, exist_ok=True)
        rules = {"max_per_team": 4, "min_salary": 48000} if kind == "classic" else {}
        own_rows = [dict(r) for r in rows]
        ownership.estimate_ownership(own_rows, 6 if kind == "showdown" else 8, own_cal={})
        np.save(d / "own.npy", np.array([r["own"] for r in own_rows], dtype=np.float64))
        for r, o in zip(rows, own_rows):
            r["own"] = o["own"]
        pool = sim.build_pool(rows, kind)
        np.save(d / "pool_ids.npy", pool.ids)
        np.save(d / "pool_code.npy", pool.code)
        np.save(d / "scores.npy", sim.sample_scores(pool, 2000, np.random.default_rng(42), corr))
        fast = sim.generate_lineups(pool, 200, rules, corr, seed=3, workers=2, exact=False)
        (d / "generate_fast.json").write_text(json.dumps([list(map(int, lu)) for lu in fast]))
        np.save(d / "field.npy", np.array(sim.sample_field(pool, 300, np.random.default_rng(5), rules), dtype=np.int64))
        try:
            import highspy  # noqa: F401
            have_highs = True
        except ImportError:
            have_highs = False
        phases = ("fast", "exact") if have_highs else ("fast",)
        if kind == "showdown" and not have_highs:
            phases = ()
        for phase in phases:
            res = sim.build_slate_lineups([dict(r) for r in rows], rules, corr, n_lineups=150, sims=20000,
                                          field_size=300, seed=1, exact=(phase == "exact"), kind=kind)
            res.pop("generate_seconds", None)
            res.pop("contest_seconds", None)
            (d / f"lineups_{phase}.json").write_text(json.dumps(res, default=float, sort_keys=True))
        if kind == "classic" or have_highs:
            opt_rows = [dict(r) for r in rows]
            ownership.optimal_rates(opt_rows, kind, rules, corr, sims=200, seed=7)
            np.save(d / "opt.npy", np.array([[r["opt_pct"], r["lev"]] for r in opt_rows], dtype=np.float64))
        (d / "highspy.txt").write_text("yes" if have_highs else "no (exact phases skipped)")


# ─────────────────────────────────────────────────────────────────────────────
# diff
# ─────────────────────────────────────────────────────────────────────────────
def _canon(obj, path=""):
    if isinstance(obj, dict):
        return {k: _canon(v, f"{path}.{k}") for k, v in sorted(obj.items()) if not _ignored(k)}
    if isinstance(obj, list):
        return [_canon(v, f"{path}[]") for v in obj]
    return obj


def _json_diffs(a, b, path="", out=None, limit=40):
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append(f"{path}.{k}: only in {'B' if k not in a else 'A'}")
            else:
                _json_diffs(a[k], b[k], f"{path}.{k}", out, limit)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: length {len(a)} != {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            _json_diffs(x, y, f"{path}[{i}]", out, limit)
    elif a != b:
        out.append(f"{path}: {str(a)[:80]!r} != {str(b)[:80]!r}")
    return out


def _files(root: Path) -> dict[str, Path]:
    return {str(p.relative_to(root)): p for p in sorted(root.rglob("*")) if p.is_file()}


def _strip_path(path: str) -> str:
    import re
    return re.sub(r"\[\d+\]", "[]", path)


def compare(a_dir: Path, b_dir: Path, exclude: set[str] | None = None) -> list[str]:
    import gzip

    import numpy as np
    exclude = exclude or set()
    A, B = _files(a_dir), _files(b_dir)
    problems = []
    for rel in A:
        if rel not in B:
            problems.append(f"missing in B: {rel}")
    for rel in B:
        if rel not in A and not any(rel.endswith(x) for x in ALLOWED_NEW):
            problems.append(f"new in B (not allowed): {rel}")
    for rel, pa in A.items():
        pb = B.get(rel)
        if pb is None:
            continue
        if rel.endswith(".npy"):
            if not np.array_equal(np.load(pa, allow_pickle=False), np.load(pb, allow_pickle=False)):
                problems.append(f"{rel}: arrays differ")
        elif rel.endswith(".json"):
            try:
                ja, jb = json.loads(pa.read_text()), json.loads(pb.read_text())
            except ValueError:
                if pa.read_bytes() != pb.read_bytes():
                    problems.append(f"{rel}: bytes differ (not JSON)")
                continue
            ds = [d for d in _json_diffs(_canon(ja), _canon(jb), limit=10_000)
                  if f"{rel}:{_strip_path(d.split(':')[0])}" not in exclude
                  and f"key:{_last_key(d.split(':')[0])}" not in exclude]
            problems += [f"{rel}: {d}" for d in ds[:40]]
        elif rel.endswith(".gz"):
            if gzip.decompress(pa.read_bytes()) != gzip.decompress(pb.read_bytes()):
                problems.append(f"{rel}: decompressed bytes differ")
        elif pa.read_bytes() != pb.read_bytes():
            problems.append(f"{rel}: bytes differ")
    return problems


def _last_key(path: str) -> str:
    return _strip_path(path).replace("[]", "").rsplit(".", 1)[-1]


def self_noise(a_dir: Path, a2_dir: Path) -> set[str]:
    """JSON keys that differ between two replays of the same code (timing etc.).
    A key that is noisy in one file is excluded in every file (e.g. the
    generate_seconds timing of each lineup pool)."""
    noise = set()
    for p in compare(a_dir, a2_dir):
        rel, _, rest = p.partition(": ")
        if rel.endswith(".json") and rest.startswith("."):
            noise.add(f"{rel}:{_strip_path(rest.split(':')[0])}")
            noise.add(f"key:{_last_key(rest.split(':')[0])}")
        else:
            noise.add(p)
    return noise


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--code-root", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--slate-csv")
    r.add_argument("--salaries")
    d = sub.add_parser("diff")
    d.add_argument("a")
    d.add_argument("b")
    d.add_argument("--self", dest="a2", help="a second replay of A's code, to exclude run-to-run noise")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        out = Path(a.out)
        if out.exists():
            shutil.rmtree(out)
        if a.slate_csv:
            run_real(Path(a.code_root), out, Path(a.slate_csv).resolve(),
                     Path(a.salaries).resolve() if a.salaries else None)
        else:
            run_synthetic(Path(a.code_root), out)
        print(f"replay written to {out}")
        return 0
    noise = self_noise(Path(a.a), Path(a.a2)) if a.a2 else set()
    if noise:
        print(f"excluded (differs main-vs-main): {len(noise)}")
        for n in sorted(noise):
            print("  ~", n)
    probs = compare(Path(a.a), Path(a.b), noise)
    for p in probs:
        print("  DIFF", p)
    print("PASS: zero diffs" if not probs else f"FAIL: {len(probs)} diffs")
    return 0 if not probs else 1


if __name__ == "__main__":
    raise SystemExit(main())
