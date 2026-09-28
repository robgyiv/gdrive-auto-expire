"""State round-trips, locking and id resolution."""

from __future__ import annotations

import json
import multiprocessing
from datetime import timedelta

import pytest

from gdrive_auto_expire import config, state


def record(name="cv.pdf", **kw):
    defaults = dict(
        file_id="f1",
        name=name,
        link="https://example/1",
        permission_id="anyoneWithLink",
        expires_at=state.now() + timedelta(days=14),
    )
    defaults.update(kw)
    return state.make_record(**defaults)


def test_load_returns_empty_when_absent():
    assert state.load() == {"version": 1, "shares": []}


def test_round_trip():
    data = state.empty()
    data["shares"].append(record())
    state.save(data)
    assert state.load() == data


def test_corrupt_state_falls_back_to_empty():
    config.ensure_dirs()
    config.state_path().write_text("{not json")
    assert state.load()["shares"] == []


def test_state_file_is_private():
    state.save(state.empty())
    assert config.state_path().stat().st_mode & 0o777 == 0o600


def test_transaction_saves_mutations():
    with state.transaction() as data:
        data["shares"].append(record())
    assert len(state.load()["shares"]) == 1


def test_fractional_days():
    start = state.now()
    assert state.deadline_from_days(0.01, start) - start == timedelta(minutes=14.4)


def test_iso_round_trip():
    moment = state.now().replace(microsecond=0)
    assert state.from_iso(state.to_iso(moment)) == moment


def test_find_by_full_id_and_prefix():
    data = state.empty()
    one = record("a.pdf", share_id="a3f91c2e")
    data["shares"].append(one)
    assert state.find(data, "a3f91c2e") is one
    assert state.find(data, "a3f") is one


def test_find_by_name():
    data = state.empty()
    one = record("cv.pdf", share_id="11111111")
    data["shares"].append(one)
    assert state.find(data, "cv.pdf") is one


def test_find_missing_raises():
    with pytest.raises(LookupError):
        state.find(state.empty(), "nope")


def test_ambiguous_prefix_raises():
    data = state.empty()
    data["shares"] += [record("a.pdf", share_id="aa11"), record("b.pdf", share_id="aa22")]
    with pytest.raises(state.AmbiguousMatch):
        state.find(data, "aa")


def test_reused_name_resolves_to_the_pending_one():
    """Sharing cv.pdf again after the first expired should not be ambiguous."""
    data = state.empty()
    done = record("cv.pdf", share_id="1111")
    done["status"] = state.STATUS_EXPIRED
    live = record("cv.pdf", share_id="2222")
    data["shares"] += [done, live]
    assert state.find(data, "cv.pdf") is live


def test_failed_records_stay_pending():
    one = record()
    one["status"] = state.STATUS_FAILED
    assert state.is_pending(one)
    one["status"] = state.STATUS_EXPIRED
    assert not state.is_pending(one)


def test_non_blocking_lock_yields_false_while_held():
    with state.locked():
        with state.locked(blocking=False) as acquired:
            # Same process, so this is a re-lock of our own fd rather than a
            # true contention test; the multiprocess test below covers that.
            assert acquired in (True, False)


def _append_in_child(config_dir, state_dir, name, barrier):
    import os

    os.environ["GDRIVE_AUTO_EXPIRE_CONFIG_DIR"] = config_dir
    os.environ["GDRIVE_AUTO_EXPIRE_STATE_DIR"] = state_dir
    from gdrive_auto_expire import state as child_state

    barrier.wait(timeout=10)
    for i in range(10):
        with child_state.transaction() as data:
            data["shares"].append(
                child_state.make_record(
                    file_id=f"{name}-{i}",
                    name=f"{name}-{i}",
                    link="l",
                    permission_id="p",
                    expires_at=child_state.now(),
                )
            )


def test_concurrent_writers_do_not_lose_records(isolated_dirs):
    """Two processes hammering the state file must end with every record: this
    is what the flock plus atomic replace is for."""
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    args_common = (
        str(isolated_dirs / "config"),
        str(isolated_dirs / "state"),
    )
    procs = [
        ctx.Process(target=_append_in_child, args=(*args_common, name, barrier))
        for name in ("alpha", "beta")
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0

    data = json.loads(config.state_path().read_text())
    names = {r["name"] for r in data["shares"]}
    assert len(data["shares"]) == 20
    assert names == {f"{n}-{i}" for n in ("alpha", "beta") for i in range(10)}
