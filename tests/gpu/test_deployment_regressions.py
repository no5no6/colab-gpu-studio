"""Regressions observed on a Windows coordinator with account-scoped OAuth."""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from app import admission, review_routes
from app.common import APIError
from app.db import Store
from app.gpu import accounts as account_module
from app.gpu.colab import ColabAllocationError, ColabError, GoogleColabProvider
from app.gpu.compute import MockColabProvider, MockGPUExecutor
from app.gpu.repository import Repository
from app.gpu.scheduler import Scheduler


@pytest.mark.parametrize("operation,http_status,deleted,unknown", [
    ({"done": True, "error": {"code": 9}}, 404, True, False),
    ({"done": False}, 404, False, False),
    (None, 404, False, True),
    ({"done": True, "error": {"code": 9}}, 200, False, False),
])
def test_allocation_failure_cleanup_evidence(operation, http_status, deleted, unknown):
    provider = GoogleColabProvider.__new__(GoogleColabProvider)
    provider._owned = lambda handle: {"create_attempts": 1, "operation_done": False}
    provider._is_cli_tunnel = lambda lease: False
    provider._operation = lambda handle, lease: operation
    provider._request = lambda method, path: SimpleNamespace(status_code=http_status)
    status = provider.inspect({"runtime_name": "runtimes/owned"})
    assert status["deleted"] is deleted
    assert bool(status.get("allocation_unknown")) is unknown
    assert bool(status.get("allocation_failed")) is bool(operation and operation.get("error"))


def test_capacity_is_distinct_from_disappeared_runtime_and_raw_errors_stay_private():
    operation = {"done": True, "error": {"code": 9, "message": "PRIVATE_CREDENTIAL", "details": [{
        "@type": "type.googleapis.com/google.rpc.ErrorInfo", "domain": "colaboratory.googleapis.com", "reason": "NO_RUNTIMES",
    }]}}
    status = {"deleted": True, **GoogleColabProvider._operation_failure(operation)}
    scheduler = Scheduler.__new__(Scheduler)
    scheduler._lease = lambda job: {"handle": {}}
    scheduler._lease_handle = lambda lease: lease["handle"]
    scheduler._provider = lambda job: SimpleNamespace(inspect=lambda handle: status)
    scheduler._begin_failure = lambda job, code, message: {"code": code, "message": message}
    # _prepare requires a non-empty handle, independently of the provider response.
    scheduler._lease = lambda job: {
        "handle": {"runtime_name": "runtimes/owned"},
        "requested_spec": {"variant": "VARIANT_GPU", "accelerator": "L4", "shape": "SHAPE_STANDARD"},
    }
    result = scheduler._prepare({"id": "owned"})
    assert result["code"] == "gpu_capacity_unavailable"
    assert "上游资源紧缺" in result["message"] and "L4" in result["message"]
    assert "PRIVATE_CREDENTIAL" not in result["message"]
    with pytest.raises(ColabError, match="NO_RUNTIMES") as error:
        GoogleColabProvider._raise_operation_error(operation)
    assert "PRIVATE_CREDENTIAL" not in str(error.value)
    operation["error"]["details"][0]["reason"] = "OTHER https://private.example/PRIVATE_CREDENTIAL"
    with pytest.raises(ColabError) as error:
        GoogleColabProvider._raise_operation_error(operation)
    assert "PRIVATE_CREDENTIAL" not in str(error.value)


def test_registration_failure_releases_owned_runtime_without_starting_executor(monkeypatch):
    repo = Repository(Store("sqlite://"))
    job = repo.create("jobs", "owner", {"state": "loading", "provider": "mock", "max_runtime_minutes": 1})
    provider, executor = MockColabProvider(), MockGPUExecutor()
    handle = provider.create({}, "request", job["id"], "runtime")
    lease = repo.create("leases", "owner", {"state": "active", "owner_job_id": job["id"], "handle": handle})
    job = repo.update("jobs", job["id"], {"lease_id": lease["id"]}, "owner")
    def refuse(*args):
        raise ValueError("lease is not active")
    monkeypatch.setattr(repo, "prepare_execution", refuse)
    scheduler = Scheduler(repo, provider_factory=lambda job: provider, executor_factory=lambda job: executor)
    result = scheduler._load(job)
    assert result["state"] == "cleaning_up"
    assert result["error_code"] == "gpu_execution_prepare_failed"
    assert not executor.started
    result = scheduler._cleanup(result)
    assert result["state"] == "failed" and provider.deleted
    assert result["error_code"] == "gpu_execution_prepare_failed"


def test_immediate_allocation_failure_is_classified_and_absence_is_confirmed():
    repo = Repository(Store("sqlite://"))
    job = repo.create("jobs", "owner", {"state": "allocating", "provider": "mock", "max_runtime_minutes": 1})
    class Provider(MockColabProvider):
        def create(self, *args):
            raise ColabAllocationError(9, "NO_RUNTIMES")
        def inspect(self, handle):
            return {"deleted": True, "allocation_failed": True}
    scheduler = Scheduler(repo, provider_factory=lambda job: Provider())
    failed = scheduler._allocation(job)
    assert failed["state"] == "cleaning_up" and failed["error_code"] == "gpu_capacity_unavailable"
    assert scheduler._cleanup(failed)["state"] == "failed"
    assert repo.list("leases", "owner")[0]["state"] == "deleted"


@pytest.mark.parametrize("state", ["cleanup_pending", "releasing", "deleting", "quarantined", "deleted"])
def test_fenced_allocation_never_reuses_handle(state):
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.db_store = SimpleNamespace(dispatch_lock=lambda: nullcontext(True))
    scheduler._ensure_lease = lambda job: {"id": "lease", "state": state, "handle": {"runtime_name": "runtimes/owned"}}
    scheduler._begin_failure = lambda job, code, message: {"state": "cleaning_up", "error_code": code}
    result = scheduler._allocation({"id": "job", "owner_id": "owner", "lease_id": "lease"})
    assert result == {"state": "cleaning_up", "error_code": "gpu_allocation_aborted"}


@pytest.fixture
def scoped_accounts(monkeypatch):
    rows = [{"id": "owned", "account_id": "owned", "owner_id": "owner", "enabled": True, "allow_allocation": True}]
    connected = {"owned"}
    class Repo:
        def list(self, kind, owner):
            return [row for row in rows if row["owner_id"] == owner]
        def get(self, kind, key, owner):
            return next((row for row in rows if row["id"] == key and row["owner_id"] == owner), None)
    def status(repo, owner, account):
        assert account is not None
        return {"colab": {"connected": account in connected}}
    monkeypatch.setattr(admission, "_google_status", status)
    return Repo(), rows, connected


def test_auto_account_is_bound_before_scoped_oauth_and_foreign_accounts_are_excluded(scoped_accounts):
    repo, rows, connected = scoped_accounts
    policy = {"provider": "colab", "allocation": "auto", "account_id": None, "instance_id": None}
    rows.append({**rows[0], "id": "foreign", "account_id": "foreign", "owner_id": "other"})
    connected.add("foreign")
    assert admission._validate_allocation(repo, {"id": "owner"}, policy)["account_id"] == "owned"
    with pytest.raises(APIError):
        admission._validate_allocation(repo, {"id": "owner"}, {**policy, "account_id": "foreign"})
    rows.append({**rows[0], "id": "second", "account_id": "second"})
    connected.add("second")
    assert admission._validate_allocation(repo, {"id": "owner"}, policy)["account_id"] is None


@pytest.mark.parametrize("flag", ["enabled", "allow_allocation"])
def test_auto_account_requires_enabled_allocation(scoped_accounts, flag):
    repo, rows, _ = scoped_accounts
    rows[0][flag] = False
    with pytest.raises(APIError):
        admission._validate_allocation(repo, {"id": "owner"}, {"provider": "colab", "allocation": "auto", "account_id": None})


def test_prepare_preserves_explicit_compute_and_shared_drive_identity(monkeypatch):
    calls = []
    class Accounts:
        def __init__(self, repo, owner):
            pass
        def resolve_drive_account_id(self, account):
            return "drive"
    class Google:
        def __init__(self, repo, owner, *, account_id):
            calls.append(account_id)
            self.account = account_id
        def status(self):
            return {"colab": {"connected": self.account == "compute"}, "drive": {"connected": self.account == "drive"}}
    monkeypatch.setattr(account_module, "AccountService", Accounts)
    monkeypatch.setattr(review_routes, "GoogleService", Google)
    monkeypatch.setattr(review_routes, "_require_account", lambda repo, owner, account: None)
    assert review_routes._prepare_account_ids(None, "owner", "compute") == ("compute", "drive")
    assert calls == ["compute", "drive"]


def test_workflow_sync_uses_workflow_identity_and_deduplicates_shared_drive(monkeypatch):
    from app import main
    from app.gpu import drive, google

    row = {"id": "workflow-id", "workflow_key": "example", "workflow_version": 1}
    seen = []
    class Accounts:
        def __init__(self, repo, owner):
            pass
        def list(self):
            return [{"account_id": name, "authorization": {"drive": {"connected": True}}} for name in ("compute-a", "compute-b")]
        def resolve_drive_account_id(self, account):
            return "shared-drive"
    class Google:
        def __init__(self, *args):
            pass
        def status(self):
            return {"drive": {"connected": False}}
    class Storage:
        def __init__(self, job, repo):
            seen.append(job)
        def put(self, name, content, mime):
            return {"name": name}
        def verify(self, record, content):
            return True
    repo = SimpleNamespace(update=lambda kind, rid, patch, owner: {**row, **patch})
    monkeypatch.setenv("GPU_STORAGE", "drive")
    monkeypatch.setattr(main, "Repository", lambda store: repo)
    monkeypatch.setattr(account_module, "AccountService", Accounts)
    monkeypatch.setattr(google, "GoogleService", Google)
    monkeypatch.setattr(drive, "DriveStorage", Storage)
    result = main.cloud_sync_workflow(None, "owner", row)
    assert len(result["drive_files"]) == 4
    assert seen == [{"id": "workflow-id", "owner_id": "owner", "account_id": "shared-drive", "drive_account_id": "shared-drive"}]


def test_auto_retry_on_capacity_retries_after_cooldown_and_succeeds():
    from app.gpu.compute import GpuConfig
    from app.job_schemas import Policy, normalize_policy
    from app.presenters import public_job

    assert normalize_policy(Policy(auto_retry_on_capacity=True))["auto_retry_on_capacity"] is True
    repo = Repository(Store("sqlite://"))
    now = [1000.0]
    job = repo.create(
        "jobs",
        "owner",
        {
            "state": "queued",
            "provider": "mock",
            "max_runtime_minutes": 10,
            "execution_policy": {"provider": "mock", "lifecycle": "save", "auto_retry_on_capacity": True},
        },
    )

    class FlakyCapacityProvider(MockColabProvider):
        def __init__(self):
            super().__init__()
            self.created_runtime_ids: list[str] = []

        def create(self, spec, request_id, job_id, runtime_id):
            self.created_runtime_ids.append(runtime_id)
            return super().create(spec, request_id, job_id, runtime_id)

        def inspect(self, handle):
            if handle["runtime_id"] == self.created_runtime_ids[0]:
                return {
                    "ready": False,
                    "deleted": True,
                    "allocation_failed": True,
                    "allocation_error_code": 9,
                    "allocation_failure_reason": "NO_RUNTIMES",
                }
            return super().inspect(handle)

    provider = FlakyCapacityProvider()
    executor = MockGPUExecutor()
    scheduler = Scheduler(
        repo,
        provider_factory=lambda j: provider,
        executor_factory=lambda j: executor,
        config=GpuConfig(capacity_retry_seconds=20.0),
        clock=lambda: now[0],
    )
    # queued -> validating -> waiting_for_capacity -> allocating -> preparing -> waiting_for_capacity (retry #1)
    for _ in range(5):
        scheduler.tick()
    retrying = repo.get("jobs", job["id"], "owner")
    assert retrying["state"] == "waiting_for_capacity"
    assert retrying["capacity_retry_count"] == 1
    assert retrying["capacity_retry_at"] == 1020.0
    assert retrying["lease_id"] is None
    presented = public_job(retrying)
    assert presented["auto_retry_on_capacity"] is True
    assert presented["capacity_retry_count"] == 1
    assert "自动重试" in presented["capacity_retry_message"]
    assert len(repo.list("instances", "owner")) == 1

    # 冷却时间内不会被重复认领
    now[0] = 1010.0
    assert scheduler.tick()["advanced"] is False

    # 冷却结束后使用新租约重试并最终完成
    now[0] = 1021.0
    for _ in range(15):
        scheduler.tick()
        if repo.get("jobs", job["id"], "owner")["state"] == "completed":
            break
    done = repo.get("jobs", job["id"], "owner")
    assert done["state"] == "completed"
    assert len(provider.created_runtime_ids) == 2
    assert provider.created_runtime_ids[0] != provider.created_runtime_ids[1]
    assert len(repo.list("instances", "owner")) == 1
    assert repo.list("instances", "owner")[0]["state"] == "deleted"


def test_auto_retry_on_capacity_cancels_immediately_and_closes_instance():
    from app.gpu.compute import GpuConfig

    repo = Repository(Store("sqlite://"))
    now = [2000.0]
    job = repo.create(
        "jobs",
        "owner",
        {
            "state": "queued",
            "provider": "mock",
            "max_runtime_minutes": 10,
            "execution_policy": {"provider": "mock", "lifecycle": "save", "auto_retry_on_capacity": True},
        },
    )

    class AlwaysNoRuntimeProvider(MockColabProvider):
        def inspect(self, handle):
            return {
                "ready": False,
                "deleted": True,
                "allocation_failed": True,
                "allocation_error_code": 9,
                "allocation_failure_reason": "NO_RUNTIMES",
            }

    scheduler = Scheduler(
        repo,
        provider_factory=lambda j: AlwaysNoRuntimeProvider(),
        executor_factory=lambda j: MockGPUExecutor(),
        config=GpuConfig(capacity_retry_seconds=20.0),
        clock=lambda: now[0],
    )
    for _ in range(5):
        scheduler.tick()
    assert repo.get("jobs", job["id"], "owner")["state"] == "waiting_for_capacity"
    cancelled = scheduler.cancel("owner", job["id"])
    assert cancelled["state"] == "cancelled"
    assert cancelled["capacity_retry_at"] is None
    assert repo.list("instances", "owner")[0]["state"] == "deleted"
    now[0] = 2100.0
    assert scheduler.tick()["advanced"] is False
    assert repo.get("jobs", job["id"], "owner")["state"] == "cancelled"


def test_auto_retry_on_capacity_cleans_up_before_retrying_immediate_error():
    from app.gpu.compute import GpuConfig

    repo = Repository(Store("sqlite://"))
    job = repo.create(
        "jobs",
        "owner",
        {
            "state": "allocating",
            "provider": "mock",
            "max_runtime_minutes": 1,
            "execution_policy": {"provider": "mock", "lifecycle": "save", "auto_retry_on_capacity": True},
        },
    )

    class ImmediateNoRuntimeProvider(MockColabProvider):
        def create(self, *args):
            raise ColabAllocationError(9, "NO_RUNTIMES")

        def inspect(self, handle):
            return {"deleted": True, "allocation_failed": True}

    scheduler = Scheduler(
        repo,
        provider_factory=lambda j: ImmediateNoRuntimeProvider(),
        config=GpuConfig(capacity_retry_seconds=15.0),
    )
    cleaning = scheduler._allocation(job)
    assert cleaning["state"] == "cleaning_up"
    assert cleaning["capacity_retry_pending"] is True
    retrying = scheduler._cleanup(cleaning)
    assert retrying["state"] == "waiting_for_capacity"
    assert retrying["capacity_retry_count"] == 1
    assert repo.list("leases", "owner")[0]["state"] == "deleted"


def test_instance_retention_syncs_state_and_deadline_to_running_job():
    from app.gpu.instances import InstanceService
    from app.presenters import public_job

    repo = Repository(Store("sqlite://"))
    job = repo.create(
        "jobs",
        "owner",
        {
            "state": "running",
            "provider": "mock",
            "max_runtime_minutes": 10,
            "execution_policy": {"provider": "mock", "lifecycle": "save"},
        },
    )
    service = InstanceService(repo, clock=lambda: 5000.0)
    instance = service.allocate(job)
    service.mark_ready({**job, "instance_id": instance["id"]})
    service.job_busy({**job, "instance_id": instance["id"]})

    updated = service.retention("owner", instance["id"], "retain", 15, "retain-key-1")
    assert updated["state"] == "retained_idle"
    assert updated["retained_until"] == 5900.0

    synced_job = public_job(repo.get("jobs", job["id"], "owner"))
    assert synced_job["instance_state"] == "retained_idle"
    assert synced_job["retained_until"] == 5900.0
