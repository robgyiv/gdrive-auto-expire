"""Folder path resolution: create, reuse a cached id, recover from a stale one."""

from __future__ import annotations

from gdrive_auto_expire import config
from gdrive_auto_expire.drive import resolve_folder


def test_empty_path_means_my_drive_root(drive):
    cfg = config.load_config()
    assert resolve_folder(drive, "", cfg) is None
    assert resolve_folder(drive, None, cfg) is None


def test_creates_the_default_folder_once(drive, service):
    cfg = config.load_config()
    first = resolve_folder(drive, "gdrive-auto-expire", cfg)
    assert first is not None
    assert cfg["folder_ids"]["gdrive-auto-expire"] == first
    creates = [c for c in service.calls if c[0] == "files.create"]
    assert len(creates) == 1


def test_cached_id_avoids_a_second_lookup(drive, service):
    cfg = config.load_config()
    folder_id = resolve_folder(drive, "gdrive-auto-expire", cfg)
    service.calls.clear()
    assert resolve_folder(drive, "gdrive-auto-expire", cfg) == folder_id
    # One existence check, no list and no create.
    assert [c[0] for c in service.calls] == ["files.get"]


def test_existing_folder_is_reused_without_creating(drive, service):
    """A folder we made on an earlier run but whose id was never cached — the
    files().list lookup has to find it."""
    folder_id = drive.create_folder("gdrive-auto-expire", "root")
    cfg = config.load_config()
    service.calls.clear()
    assert resolve_folder(drive, "gdrive-auto-expire", cfg) == folder_id
    assert not [c for c in service.calls if c[0] == "files.create"]


def test_stale_cached_id_self_heals(drive, service):
    """Deleting the folder in the Drive UI must not leave the tool erroring on a
    cached id that 404s."""
    cfg = config.load_config()
    original = resolve_folder(drive, "gdrive-auto-expire", cfg)
    service.drop_folder(original)

    recreated = resolve_folder(drive, "gdrive-auto-expire", cfg)
    assert recreated != original
    assert cfg["folder_ids"]["gdrive-auto-expire"] == recreated


def test_trashed_folder_is_treated_as_stale(drive, service):
    cfg = config.load_config()
    original = resolve_folder(drive, "gdrive-auto-expire", cfg)
    service.folders[original]["trashed"] = True
    assert resolve_folder(drive, "gdrive-auto-expire", cfg) != original


def test_nested_path_creates_each_segment(drive, service):
    cfg = config.load_config()
    leaf = resolve_folder(drive, "gdrive-auto-expire/recruiters/acme", cfg)
    assert set(cfg["folder_ids"]) == {
        "gdrive-auto-expire",
        "gdrive-auto-expire/recruiters",
        "gdrive-auto-expire/recruiters/acme",
    }
    assert cfg["folder_ids"]["gdrive-auto-expire/recruiters/acme"] == leaf
    parent = cfg["folder_ids"]["gdrive-auto-expire/recruiters"]
    assert service.folders[leaf]["parents"] == [parent]


def test_nested_path_shares_its_prefix_with_a_sibling(drive, service):
    cfg = config.load_config()
    resolve_folder(drive, "gdrive-auto-expire/recruiters", cfg)
    before = dict(cfg["folder_ids"])
    resolve_folder(drive, "gdrive-auto-expire/archive", cfg)
    assert cfg["folder_ids"]["gdrive-auto-expire"] == before["gdrive-auto-expire"]
    assert cfg["folder_ids"]["gdrive-auto-expire/recruiters"] == before[
        "gdrive-auto-expire/recruiters"
    ]


def test_leading_and_trailing_slashes_are_ignored(drive):
    cfg = config.load_config()
    a = resolve_folder(drive, "gdrive-auto-expire", cfg)
    assert resolve_folder(drive, "/gdrive-auto-expire/", cfg) == a


def test_upload_lands_in_the_resolved_folder(drive, service, tmp_path):
    cfg = config.load_config()
    folder_id = resolve_folder(drive, "gdrive-auto-expire", cfg)
    path = tmp_path / "cv.pdf"
    path.write_text("x")
    uploaded = drive.upload(path, parent_id=folder_id)
    assert service.files_store[uploaded["id"]]["parents"] == [folder_id]
    assert uploaded["webViewLink"].endswith("/view")


def test_upload_without_a_parent_omits_parents(drive, service, tmp_path):
    path = tmp_path / "cv.pdf"
    path.write_text("x")
    drive.upload(path, parent_id=None)
    create = next(c for c in service.calls if c[0] == "files.create")
    assert "parents" not in create[1]


def test_make_public_is_link_sharing_not_discoverable(drive, service, tmp_path):
    path = tmp_path / "cv.pdf"
    path.write_text("x")
    uploaded = drive.upload(path)
    permission = drive.make_public(uploaded["id"])
    assert permission["id"] in service.permissions_store[uploaded["id"]]


def test_exists_reports_missing_files(drive, service, tmp_path):
    path = tmp_path / "cv.pdf"
    path.write_text("x")
    uploaded = drive.upload(path)
    assert drive.exists(uploaded["id"])
    service.drop_file(uploaded["id"])
    assert not drive.exists(uploaded["id"])


def test_folder_name_with_a_quote_is_escaped(drive, service):
    """An apostrophe in a folder name must not break the query."""
    folder_id = drive.create_folder("robbie's cvs", "root")
    service.calls.clear()
    assert drive.find_folder("robbie's cvs", "root") == folder_id
    query = next(c[1] for c in service.calls if c[0] == "files.list")
    assert "\\'" in query
