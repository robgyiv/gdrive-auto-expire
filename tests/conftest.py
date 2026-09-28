"""Shared fixtures: isolated config/state dirs and a fake Drive service."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Point every path in config.py at tmp_path so no test can touch the real
    config, token or state."""
    monkeypatch.setenv("GDRIVE_AUTO_EXPIRE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("GDRIVE_AUTO_EXPIRE_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


class HttpErrorStub(Exception):
    """Stands in for googleapiclient.errors.HttpError.

    drive.py reads the status off `.resp.status`, which is the only part of the
    real class it depends on.
    """

    def __init__(self, status: int, message: str = "stub"):
        super().__init__(f"{status}: {message}")
        self.resp = type("Resp", (), {"status": status})()


@pytest.fixture(autouse=True)
def patch_http_error(monkeypatch):
    """Make `except HttpError` in drive.py catch our stub.

    drive.py imports the error class inside each method, so patching the
    attribute on the module is enough.
    """
    import googleapiclient.errors as errors

    monkeypatch.setattr(errors, "HttpError", HttpErrorStub)
    return HttpErrorStub


def _escape(name: str) -> str:
    """Mirror drive.py's query escaping so the fake matches what it builds."""
    return name.replace("\\", "\\\\").replace("'", "\\'")


class FakeRequest:
    def __init__(self, result, error=None):
        self._result = result
        self._error = error

    def execute(self):
        if self._error is not None:
            raise self._error
        return self._result


class FakeFiles:
    def __init__(self, service):
        self.service = service

    def create(self, body=None, media_body=None, fields=None):
        body = body or {}
        self.service.calls.append(("files.create", body))
        if body.get("mimeType") == "application/vnd.google-apps.folder":
            file_id = f"folder-{self.service.next_id()}"
            self.service.folders[file_id] = {
                "id": file_id,
                "name": body["name"],
                "mimeType": body["mimeType"],
                "parents": body.get("parents", ["root"]),
                "trashed": False,
            }
            return FakeRequest({"id": file_id})
        file_id = f"file-{self.service.next_id()}"
        record = {
            "id": file_id,
            "name": body.get("name", "upload"),
            "webViewLink": f"https://drive.google.com/file/d/{file_id}/view",
            "trashed": False,
            "parents": body.get("parents", []),
        }
        self.service.files_store[file_id] = record
        return FakeRequest(dict(record))

    def list(self, q=None, fields=None, pageSize=None):
        self.service.calls.append(("files.list", q))
        hits = [
            {"id": f["id"], "name": f["name"]}
            for f in self.service.folders.values()
            if f"name='{_escape(f['name'])}'" in q
            and f"'{f['parents'][0]}' in parents" in q
            and not f["trashed"]
        ]
        return FakeRequest({"files": hits})

    def get(self, fileId=None, fields=None):
        self.service.calls.append(("files.get", fileId))
        record = self.service.folders.get(fileId) or self.service.files_store.get(fileId)
        if record is None:
            return FakeRequest(None, error=HttpErrorStub(404, "not found"))
        return FakeRequest(dict(record))

    def update(self, fileId=None, body=None):
        self.service.calls.append(("files.update", fileId))
        record = self.service.files_store.get(fileId)
        if record is None:
            return FakeRequest(None, error=HttpErrorStub(404, "not found"))
        record.update(body or {})
        return FakeRequest(dict(record))


class FakePermissions:
    def __init__(self, service):
        self.service = service

    def create(self, fileId=None, body=None, fields=None):
        self.service.calls.append(("permissions.create", fileId))
        if fileId not in self.service.files_store:
            return FakeRequest(None, error=HttpErrorStub(404, "no such file"))
        permission_id = "anyoneWithLink"
        self.service.permissions_store.setdefault(fileId, set()).add(permission_id)
        return FakeRequest({"id": permission_id})

    def delete(self, fileId=None, permissionId=None):
        self.service.calls.append(("permissions.delete", (fileId, permissionId)))
        held = self.service.permissions_store.get(fileId, set())
        if permissionId not in held:
            return FakeRequest(None, error=HttpErrorStub(404, "no such permission"))
        held.discard(permissionId)
        return FakeRequest({})


class FakeService:
    """In-memory stand-in for the Drive v3 service object."""

    def __init__(self):
        self.files_store: dict[str, dict] = {}
        self.folders: dict[str, dict] = {}
        self.permissions_store: dict[str, set[str]] = {}
        self.calls: list[tuple] = []
        self._counter = 0

    def next_id(self) -> int:
        """Monotonic, so a deleted id is never handed out again — Drive does not
        recycle ids and a test must not accidentally rely on it."""
        self._counter += 1
        return self._counter

    def files(self):
        return FakeFiles(self)

    def permissions(self):
        return FakePermissions(self)

    # test helpers

    def drop_file(self, file_id):
        """Simulate the file being deleted out from under us."""
        self.files_store.pop(file_id, None)
        self.permissions_store.pop(file_id, None)

    def drop_folder(self, folder_id):
        self.folders.pop(folder_id, None)


@pytest.fixture
def service():
    return FakeService()


@pytest.fixture
def drive(service):
    from gdrive_auto_expire.drive import Drive

    return Drive(service)
