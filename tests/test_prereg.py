"""The pre-registration hash, its log and the re-registration budget, on a fixture config."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from roi import estimator, prereg
from roi.gen import books, config, manifest, panel
from roi.workdir import WorkDir

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
SCRIPT = ROOT / "scripts" / "reregister-seed.sh"
DAY = "2026-10-13"
EST = estimator.ESTIMATOR_VERSION


@pytest.fixture
def repo(tmp_path):
    """A throwaway tree with config/gen.yaml and config/calibration.yaml from the fixtures, registered once."""
    (tmp_path / "config").mkdir()
    shutil.copy(FIXTURES / "gen_fixture.yaml", tmp_path / "config" / "gen.yaml")
    shutil.copy(FIXTURES / "calibration_fixture.yaml", tmp_path / "config" / "calibration.yaml")
    return tmp_path


@pytest.fixture
def paths(repo):
    p = prereg.Paths.under(repo)
    prereg.register_initial(p, estimator=EST, day=DAY)
    return p


def edit_gen(paths, change):
    raw = yaml.safe_load(paths.gen.read_text(encoding="utf-8"))
    change(raw)
    paths.gen.write_text(yaml.safe_dump(raw), encoding="utf-8")


def edit_calibration(paths, change):
    raw = yaml.safe_load(paths.calib.read_text(encoding="utf-8"))
    change(raw)
    paths.calib.write_text(yaml.safe_dump(raw), encoding="utf-8")


def team(raw, team_id):
    return next(t for t in raw["teams"] if t["team_id"] == team_id)


def spend(paths, n):
    """Re-register n times with successive seeds; returns the seeds."""
    seeds = []
    for i in range(n):
        seeds.append(100 + i)
        prereg.reregister(paths, seeds[-1], f"miss {i}", estimator=EST, day=DAY)
    return seeds


# ---- the hash --------------------------------------------------------------------------------------

def test_the_hash_is_stable(repo):
    p = prereg.Paths.under(repo)
    first = prereg.compute_hash(p.gen, p.calib)
    assert first == prereg.compute_hash(p.gen, p.calib)
    assert len(first) == 64 and int(first, 16) >= 0


def test_the_hash_ignores_comments_whitespace_and_key_order(repo):
    p = prereg.Paths.under(repo)
    before = prereg.compute_hash(p.gen, p.calib)
    raw = yaml.safe_load(p.gen.read_text(encoding="utf-8"))
    reordered = {k: raw[k] for k in reversed(list(raw))}
    p.gen.write_text("# a new comment\n\n" + yaml.safe_dump(reordered, sort_keys=False, width=200), encoding="utf-8")
    assert prereg.compute_hash(p.gen, p.calib) == before
    p.calib.write_text("# another comment\n" + p.calib.read_text(encoding="utf-8"), encoding="utf-8")
    assert prereg.compute_hash(p.gen, p.calib) == before


@pytest.mark.parametrize(
    "name,change",
    [
        ("seed", lambda raw: raw.update(seed=raw["seed"] + 1)),
        ("generator version", lambda raw: raw.update(generator_version="gen-other")),
        ("month window", lambda raw: raw["months"].update(first="2025-05")),
        ("month shock", lambda raw: raw["dgp"].update(month_shock_sd=0.04)),
        ("ramp", lambda raw: raw["dgp"].update(ramp_months=4)),
        ("share of payroll", lambda raw: raw["dgp"].update(target_ai_share_of_payroll=0.02)),
        ("a new dgp key", lambda raw: raw["dgp"].update(new_key=1)),
        ("price", lambda raw: raw["prices"][0].update(input_cents_per_mtok=301.0)),
        ("planted effect", lambda raw: team(raw, "support").update(effect_at_target=0.141)),
        ("noise sd", lambda raw: team(raw, "people_operations").update(noise_sd=0.031)),
        ("headcount", lambda raw: team(raw, "marketing").update(headcount=5)),
        ("target spend", lambda raw: team(raw, "marketing").update(target_spend_per_head_usd=46)),
        ("rollout month", lambda raw: team(raw, "support").update(rollout_month="2025-08")),
        ("baseline output", lambda raw: team(raw, "support").update(baseline_output=51.0)),
        ("a team removed", lambda raw: raw["teams"].pop()),
    ],
)
def test_any_change_to_a_registered_gen_input_changes_the_hash(repo, name, change):
    p = prereg.Paths.under(repo)
    before = prereg.compute_hash(p.gen, p.calib)
    edit_gen(p, change)
    assert prereg.compute_hash(p.gen, p.calib) != before, name


@pytest.mark.parametrize(
    "change",
    [
        lambda raw: raw["rows"][0].update(effect_low=0.04),
        lambda raw: raw["rows"][3].update(effect_high=0.26),
        lambda raw: raw["rows"].pop(),
    ],
)
def test_a_change_to_a_calibration_range_changes_the_hash(repo, change):
    p = prereg.Paths.under(repo)
    before = prereg.compute_hash(p.gen, p.calib)
    edit_calibration(p, change)
    assert prereg.compute_hash(p.gen, p.calib) != before


def test_the_source_text_of_a_calibration_row_is_not_a_registered_input(repo):
    p = prereg.Paths.under(repo)
    before = prereg.compute_hash(p.gen, p.calib)
    edit_calibration(p, lambda raw: raw["rows"][0].update(verified_on="2000-02-02", source_ref="Fixture A, 2001, Elsewhere"))
    assert prereg.compute_hash(p.gen, p.calib) == before


@pytest.mark.parametrize("name,value", [("LEVEL", 0.9), ("PRETREND_ALPHA", 0.05)])
def test_a_change_to_the_interval_level_or_the_pretrend_level_changes_the_hash(repo, monkeypatch, name, value):
    p = prereg.Paths.under(repo)
    before = prereg.compute_hash(p.gen, p.calib)
    monkeypatch.setattr(estimator, name, value)
    assert prereg.compute_hash(p.gen, p.calib) != before


def test_the_real_thresholds_are_the_registered_ones():
    assert prereg.thresholds() == {"level": 0.95, "pretrend_alpha": 0.10}


# ---- the log and the hash file ---------------------------------------------------------------------

def test_the_first_registration_writes_the_hash_file_and_one_log_line(paths):
    entries = prereg.read_log(paths.log)
    assert len(entries) == 1 and entries[0].old_seed is None and entries[0].seed == 20261013
    assert paths.digest.read_text(encoding="utf-8") == entries[0].sha256 + "\n"
    assert entries[0].sha256 == prereg.compute_hash(paths.gen, paths.calib)
    assert prereg.problems(paths) == []


def test_a_second_first_registration_is_refused(paths):
    with pytest.raises(prereg.Refused, match="already has entries"):
        prereg.register_initial(paths, estimator=EST, day=DAY)


def test_an_entry_survives_a_round_trip_with_quotes_and_newlines():
    e = prereg.Entry(DAY, EST, 7, "a" * 64, 6, 'support "x"\nmissed: 0.3 vs [0.1, 0.2] \\ seed=5 sha256=' + "b" * 64)
    back = prereg.parse_entry(prereg.format_entry(e))
    assert back == prereg.Entry(DAY, EST, 7, "a" * 64, 6, " ".join(e.why.split()))
    assert "\n" not in prereg.format_entry(e)


@pytest.mark.parametrize("line", ["", "not a line", f"{DAY} seed=1", f"{DAY} estimator=e seed=x sha256={'a' * 64}", f"{DAY} estimator=e seed=1 sha256=short"])
def test_a_malformed_log_line_is_rejected(line):
    with pytest.raises(ValueError):
        prereg.parse_entry(line)


def test_a_reregistration_changes_the_seed_the_hash_and_the_log_and_nothing_else(paths):
    before_log = paths.log.read_text(encoding="utf-8")
    before_gen = yaml.safe_load(paths.gen.read_text(encoding="utf-8"))
    old_hash = paths.digest.read_text(encoding="utf-8")
    e = prereg.reregister(paths, 4242, "support missed", estimator=EST, day="2026-11-02")
    assert e.old_seed == 20261013 and e.seed == 4242
    after_gen = yaml.safe_load(paths.gen.read_text(encoding="utf-8"))
    assert after_gen == {**before_gen, "seed": 4242}
    assert paths.digest.read_text(encoding="utf-8") != old_hash
    assert prereg.problems(paths) == []
    new_log = paths.log.read_text(encoding="utf-8")
    assert prereg.append_only(before_log, new_log) and len(new_log.splitlines()) == 2
    last = prereg.read_log(paths.log)[-1]
    assert (last.date, last.old_seed, last.seed, last.why) == ("2026-11-02", 20261013, 4242, "support missed")
    assert not list(paths.gen.parent.glob("*.tmp")) and not list(paths.gen.parent.glob("*.next"))


def test_a_reregistration_keeps_the_comments_of_gen_yaml(paths):
    paths.gen.write_text("# keep me\n" + paths.gen.read_text(encoding="utf-8"), encoding="utf-8")
    # the comment is not a registered input, so the registration is still intact
    prereg.reregister(paths, 5, "why", estimator=EST, day=DAY)
    text = paths.gen.read_text(encoding="utf-8")
    assert text.startswith("# keep me\n") and "\nseed: 5\n" in text


def test_the_budget_is_the_first_registration_and_three_more(paths):
    spend(paths, 3)
    assert len(prereg.read_log(paths.log)) == 4 and prereg.problems(paths) == []
    snapshot = {p: p.read_bytes() for p in (paths.gen, paths.digest, paths.log)}
    with pytest.raises(prereg.Refused, match="budget is spent"):
        prereg.reregister(paths, 999, "a fourth miss", estimator=EST, day=DAY)
    assert {p: p.read_bytes() for p in snapshot} == snapshot  # a refusal changes nothing


def test_a_new_estimator_version_starts_a_new_budget_but_inherits_the_registered_seed_as_its_first_draw(paths):
    spend(paths, 3)
    for i in range(3):
        prereg.reregister(paths, 200 + i, f"miss {i}", estimator="cs-next", day=DAY)
    assert prereg.problems(paths) == []
    with pytest.raises(prereg.Refused, match="budget is spent"):
        prereg.reregister(paths, 300, "again", estimator="cs-next", day=DAY)
    assert len(prereg.read_log(paths.log)) == 7


@pytest.mark.parametrize("seed,why,match", [(0, "x", "positive"), (-3, "x", "positive"), (20261013, "x", "already"), (7, "  ", "reason")])
def test_a_bad_seed_or_a_missing_reason_is_refused(paths, seed, why, match):
    with pytest.raises(prereg.Refused, match=match):
        prereg.reregister(paths, seed, why, estimator=EST, day=DAY)


def test_a_registered_input_edited_by_hand_blocks_reregistration(paths):
    edit_gen(paths, lambda raw: team(raw, "support").update(effect_at_target=0.15))
    assert any("registered input has changed" in p for p in prereg.problems(paths))
    with pytest.raises(prereg.Refused, match="not intact"):
        prereg.reregister(paths, 5, "sneaking in a change", estimator=EST, day=DAY)


def test_a_seed_edited_by_hand_is_caught(paths):
    paths.gen.write_text(paths.gen.read_text(encoding="utf-8").replace("seed: 20261013", "seed: 31"), encoding="utf-8")
    found = prereg.problems(paths)
    assert any("registered input has changed" in p for p in found) and any("seed in gen.yaml (31)" in p for p in found)


def test_a_hash_file_that_is_not_the_last_log_entry_is_caught(paths):
    spend(paths, 1)
    paths.digest.write_text("f" * 64 + "\n", encoding="utf-8")
    assert any("not the hash of the last log entry" in p for p in prereg.problems(paths))


def test_a_log_with_more_than_four_entries_for_a_version_is_caught(paths):
    spend(paths, 3)
    extra = prereg.Entry(DAY, EST, 55, prereg.compute_hash(paths.gen, paths.calib), 100, "forged")
    paths.log.write_text(paths.log.read_text(encoding="utf-8") + prereg.format_entry(extra) + "\n", encoding="utf-8")
    assert any("more than 4 registrations" in p for p in prereg.problems(paths))


def test_reregistration_needs_a_first_registration(repo):
    with pytest.raises(prereg.Refused, match="no first registration"):
        prereg.reregister(prereg.Paths.under(repo), 5, "x", estimator=EST, day=DAY)


def test_append_only():
    assert prereg.append_only("a\nb\n", "a\nb\nc\n")
    assert prereg.append_only("", "a\n")
    assert not prereg.append_only("a\nb\n", "a\nB\nc\n")
    assert not prereg.append_only("a\nb\n", "a\n")
    assert not prereg.append_only("a\nb", "a\nbc\n")


def test_the_doctor_accepts_a_registration_this_module_wrote(paths):
    from roi import doctor

    assert doctor.prereg(paths.gen.parent.parent).status == doctor.OK
    prereg.reregister(paths, 6, "x", estimator=EST, day=DAY)
    assert doctor.prereg(paths.gen.parent.parent).status == doctor.WARN
    assert doctor.prereg(paths.gen.parent.parent).status != doctor.FAIL


# ---- the manifests ---------------------------------------------------------------------------------

def generate(gen, work):
    """What `roi gen` writes for this config into `work`."""
    cfg = config.load(gen)
    books.write(cfg, panel.simulate(cfg), WorkDir(work))
    return WorkDir(work).data


def manifest_files(data):
    return {n: (data / n).read_text(encoding="utf-8") for n in (manifest.BOOKS_NAME, manifest.INCOMING_NAME)}


@pytest.fixture
def registered(paths, tmp_path_factory):
    """The fixture repository with `data/MANIFEST*.sha256` as generated from its registered config."""
    root = paths.gen.parent.parent
    for name, text in manifest_files(generate(paths.gen, tmp_path_factory.mktemp("shipped"))).items():
        paths.data.mkdir(exist_ok=True)
        (paths.data / name).write_text(text, encoding="utf-8")
    return paths


def test_a_reregistration_rewrites_both_manifests_to_the_books_of_the_new_seed(registered, tmp_path):
    before = manifest_files(registered.data)
    prereg.reregister(registered, 4242, "support missed", estimator=EST, day=DAY)
    after = manifest_files(registered.data)
    assert all(after[n] != before[n] for n in before)
    fresh = manifest_files(generate(registered.gen, tmp_path / "fresh"))
    assert after == fresh
    assert manifest.parse(after[manifest.BOOKS_NAME]) and manifest.parse(after[manifest.INCOMING_NAME])
    assert sorted(p.name for p in registered.data.iterdir()) == sorted(before)  # no stray files


def test_a_refused_reregistration_leaves_the_manifests_alone(registered):
    spend(registered, 3)
    snapshot = manifest_files(registered.data)
    with pytest.raises(prereg.Refused, match="budget is spent"):
        prereg.reregister(registered, 999, "a fourth miss", estimator=EST, day=DAY)
    assert manifest_files(registered.data) == snapshot


# ---- a change to the data-generating keys ----------------------------------------------------------

def test_every_entry_logs_the_hash_of_the_data_generating_keys(registered):
    prereg.reregister(registered, 7, "x", estimator=EST, day=DAY)
    dgp = prereg.dgp_hash(registered.gen)
    assert [e.dgp for e in prereg.read_log(registered.log)] == [dgp, dgp]


def test_the_dgp_hash_ignores_the_seed_and_sees_the_keys(registered):
    before = prereg.dgp_hash(registered.gen)
    edit_gen(registered, lambda raw: raw.update(seed=3))
    assert prereg.dgp_hash(registered.gen) == before
    edit_gen(registered, lambda raw: raw["dgp"].update(ramp_months=4))
    assert prereg.dgp_hash(registered.gen) != before


def test_a_dgp_change_logs_old_and_new_key_hashes_keeps_the_seed_and_spends_budget(registered, tmp_path):
    old_dgp = prereg.dgp_hash(registered.gen)
    edit_gen(registered, lambda raw: raw["dgp"].update(ramp_months=4))
    assert any("registered input has changed" in p for p in prereg.problems(registered))
    before = manifest_files(registered.data)
    e = prereg.dgp_change(registered, "ramp 3 to 4 months", estimator=EST, day="2026-11-02")
    assert (e.seed, e.old_seed) == (20261013, 20261013)
    assert (e.old_dgp, e.dgp) == (old_dgp, prereg.dgp_hash(registered.gen)) and e.old_dgp != e.dgp
    assert prereg.problems(registered) == []
    assert yaml.safe_load(registered.gen.read_text(encoding="utf-8"))["seed"] == 20261013
    log = prereg.read_log(registered.log)
    assert len(log) == 2 and log[-1] == e and prereg.count_for(log, EST) == 2
    assert f"old_dgp={old_dgp}" in registered.log.read_text(encoding="utf-8")
    after = manifest_files(registered.data)
    assert after != before and after == manifest_files(generate(registered.gen, tmp_path / "fresh"))


def test_a_dgp_change_is_followed_by_a_dgp_change_or_a_new_seed(registered):
    edit_gen(registered, lambda raw: team(raw, "support").update(noise_sd=0.05))
    prereg.dgp_change(registered, "noise", estimator=EST, day=DAY)
    edit_gen(registered, lambda raw: raw["dgp"].update(ramp_months=2))
    second = prereg.dgp_change(registered, "ramp", estimator=EST, day=DAY)
    assert second.old_dgp == prereg.read_log(registered.log)[1].dgp
    prereg.reregister(registered, 8, "miss", estimator=EST, day=DAY)
    assert prereg.problems(registered) == [] and len(prereg.read_log(registered.log)) == 4


def test_a_dgp_change_with_no_change_is_refused(registered):
    snapshot = {p: p.read_bytes() for p in (registered.gen, registered.digest, registered.log)}
    manifests = manifest_files(registered.data)
    with pytest.raises(prereg.Refused, match="nothing changed"):
        prereg.dgp_change(registered, "no edit", estimator=EST, day=DAY)
    assert {p: p.read_bytes() for p in snapshot} == snapshot and manifest_files(registered.data) == manifests


def test_a_dgp_change_needs_a_reason_a_kept_seed_and_a_change_in_the_keys(registered):
    edit_gen(registered, lambda raw: raw["dgp"].update(ramp_months=4))
    with pytest.raises(prereg.Refused, match="reason"):
        prereg.dgp_change(registered, " ", estimator=EST, day=DAY)
    edit_gen(registered, lambda raw: raw.update(seed=9))
    with pytest.raises(prereg.Refused, match="not intact"):
        prereg.dgp_change(registered, "seed edited too", estimator=EST, day=DAY)
    edit_gen(registered, lambda raw: raw.update(seed=20261013))
    edit_gen(registered, lambda raw: raw["dgp"].update(ramp_months=3))
    edit_calibration(registered, lambda raw: raw["rows"][0].update(effect_low=0.04))
    with pytest.raises(prereg.Refused, match="no data-generating key changed"):
        prereg.dgp_change(registered, "only a range", estimator=EST, day=DAY)


def test_both_forms_are_refused_once_the_budget_is_spent(registered):
    spend(registered, 3)
    edit_gen(registered, lambda raw: raw["dgp"].update(ramp_months=4))
    snapshot = {p: p.read_bytes() for p in (registered.gen, registered.digest, registered.log)}
    manifests = manifest_files(registered.data)
    with pytest.raises(prereg.Refused, match="budget is spent"):
        prereg.dgp_change(registered, "a fourth draw", estimator=EST, day=DAY)
    assert {p: p.read_bytes() for p in snapshot} == snapshot and manifest_files(registered.data) == manifests


def test_an_entry_with_key_hashes_survives_a_round_trip():
    e = prereg.Entry(DAY, EST, 7, "a" * 64, 7, "ramp", "b" * 64, "c" * 64)
    assert prereg.parse_entry(prereg.format_entry(e)) == e
    with pytest.raises(ValueError):
        prereg.parse_entry(prereg.format_entry(e).replace("b" * 64, "b" * 5))


# ---- the script ------------------------------------------------------------------------------------

def script(root, *args):
    env = {**os.environ, "ROI_PREREG_ROOT": str(root), "ROI_PREREG_PYTHON": __import__("sys").executable}
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, check=False)


def test_the_script_checks_its_arguments(paths):
    root = paths.gen.parent.parent
    assert script(root).returncode == 2
    assert script(root, "5").returncode == 2
    assert script(root, "5", "a", "b").returncode == 2
    assert script(root, "five", "why").returncode == 2
    assert script(root, "-5", "why").returncode == 2
    assert script(root, "5", "   ").returncode == 2
    assert len(prereg.read_log(paths.log)) == 1


def test_the_script_reregisters_creates_no_tag_and_enforces_the_budget(paths, repo):
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=True).stdout  # noqa: E731
    git("init", "-q", "-b", "main")
    for i in range(3):
        r = script(repo, str(500 + i), f"miss {i}")
        assert r.returncode == 0, r.stderr
        assert "no tag was created" in r.stdout
    assert git("tag", "--list") == ""
    assert prereg.problems(paths) == []
    assert [e.seed for e in prereg.read_log(paths.log)] == [20261013, 500, 501, 502]
    r = script(repo, "600", "a fourth miss")
    assert r.returncode == 3 and "budget is spent" in r.stderr
    assert len(prereg.read_log(paths.log)) == 4


def test_the_script_takes_a_dgp_change_as_its_second_form(registered, repo):
    assert script(repo, "--dgp-change").returncode == 2
    assert script(repo, "--dgp-change", "  ").returncode == 2
    r = script(repo, "--dgp-change", "no edit yet")
    assert r.returncode == 3 and "nothing changed" in r.stderr
    edit_gen(registered, lambda raw: raw["dgp"].update(ramp_months=4))
    r = script(repo, "--dgp-change", "ramp 3 to 4")
    assert r.returncode == 0, r.stderr
    assert "no tag was created" in r.stdout
    assert [e.seed for e in prereg.read_log(registered.log)] == [20261013, 20261013]
    assert prereg.problems(registered) == []
    r = script(repo, "6", "miss")
    assert r.returncode == 0, r.stderr
    assert manifest_files(registered.data) == manifest_files(generate(registered.gen, repo / "fresh"))


def test_the_cli_wants_exactly_one_form(registered):
    from click.testing import CliRunner

    root = str(registered.gen.parent.parent)
    run = lambda *a: CliRunner().invoke(prereg.main, ["--root", root, "reregister", *a])  # noqa: E731
    assert run("--why", "x").exit_code == 2
    assert run("--new-seed", "5", "--dgp-change", "--why", "x").exit_code == 2
    assert len(prereg.read_log(registered.log)) == 1


def test_the_script_refuses_before_a_first_registration(repo):
    r = script(repo, "5", "why")
    assert r.returncode == 3 and "no first registration" in r.stderr


# ---- the real tree ---------------------------------------------------------------------------------

def test_a_registration_in_the_real_tree_is_intact_and_append_only():
    real = prereg.Paths.under(ROOT)
    if not real.digest.exists():
        pytest.skip("nothing is registered yet: the author registers the seed by hand")
    assert prereg.problems(real) == []
    if (ROOT / ".git").exists():
        old = subprocess.run(["git", "-C", str(ROOT), "show", "HEAD:config/prereg.log"], capture_output=True, text=True, check=False)
        if old.returncode == 0:
            assert prereg.append_only(old.stdout, real.log.read_text(encoding="utf-8"))
        tags = subprocess.run(["git", "-C", str(ROOT), "tag", "--list", "prereg-v*"], capture_output=True, text=True, check=False).stdout.split()
        if tags:
            latest = max(tags, key=lambda t: int(t.removeprefix("prereg-v")))
            pinned = subprocess.run(["git", "-C", str(ROOT), "show", f"{latest}:config/prereg.sha256"], capture_output=True, text=True, check=True).stdout
            assert real.digest.read_text(encoding="utf-8") == pinned
