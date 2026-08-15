import pytest

from valueroute.domain.errors import DomainError
from valueroute.domain.models import (
    Acceptance,
    ControllerSession,
    OrchestrationMode,
    ParentTask,
    TaskStatus,
    Workspace,
)
from valueroute.routing.automatic import AutomaticControllerService
from valueroute.routing.manifest import ModelProfile
from valueroute.routing.rank import ControllerRanker
from valueroute.storage.journal import LocalJournal
from valueroute.storage.store import Store


def certified(**overrides):
    base = {
        "provider_id": "openai",
        "model_id": "gpt-5-6-mini",
        "measured_at": "2026-08-15T00:00:00Z",
        "protocol_status": "compatible",
        "worker_status": "candidate",
        "controller_status": "certified",
        "supported_modalities": ["text"],
        "supported_tools": [],
        "effort_mapping": {},
        "region": "test",
        "evidence_refs": [],
    }
    base.update(overrides)
    return ModelProfile.model_validate(base)


def make_session(tmp_path, *, mode=OrchestrationMode.automatic):
    journal = LocalJournal(tmp_path)
    store = Store(journal)
    session = ControllerSession(id="cs_1", tenant_id="tenant", host_session_id="host_1", orchestration_mode=mode)
    store.sessions[session.id] = session
    store.commit({"type": "session.created", "data": session.model_dump(mode="json")})
    return store, journal, session


def test_ensure_selects_certified_controller_on_first_use_and_is_sticky(tmp_path):
    store, journal, session = make_session(tmp_path)
    service = AutomaticControllerService(store, profiles=[certified(model_id="m1"), certified(model_id="m2")])
    first = service.ensure_controller(session.id, expected_version=1)
    assert first.model_id in {"m1", "m2"}
    assert store.sessions[session.id].active_controller_epoch_id == first.id

    second = service.ensure_controller(session.id, expected_version=2)
    assert second.id == first.id
    journal.close()


def test_ensure_requires_automatic_mode(tmp_path):
    store, journal, session = make_session(tmp_path, mode=OrchestrationMode.worker_only)
    service = AutomaticControllerService(store, profiles=[certified()])
    with pytest.raises(DomainError, match="automatic orchestration"):
        service.ensure_controller(session.id, expected_version=1)
    journal.close()


def test_ensure_fails_closed_without_certified_controller(tmp_path):
    store, journal, session = make_session(tmp_path)
    service = AutomaticControllerService(store, profiles=[])
    with pytest.raises(DomainError, match="no certified controller"):
        service.ensure_controller(session.id, expected_version=1)
    assert store.sessions[session.id].active_controller_epoch_id is None
    journal.close()


def test_ensure_requires_current_expected_version(tmp_path):
    store, journal, session = make_session(tmp_path)
    service = AutomaticControllerService(store, profiles=[certified()])
    with pytest.raises(DomainError, match="version conflict"):
        service.ensure_controller(session.id, expected_version=99)
    journal.close()


def test_switch_releases_previous_epoch_and_activates_new(tmp_path):
    store, journal, session = make_session(tmp_path)
    service = AutomaticControllerService(
        store,
        profiles=[certified(model_id="m1"), certified(model_id="m2")],
        ranker=ControllerRanker(),
    )
    first = service.ensure_controller(session.id, expected_version=1)
    switched = service.switch_controller(session.id, expected_version=2)

    assert switched.id != first.id
    assert store.epochs[first.id].status == "released"
    assert store.epochs[switched.id].status == "active"
    assert store.sessions[session.id].active_controller_epoch_id == switched.id
    journal.close()


def test_switch_blocked_while_tasks_are_running(tmp_path):
    store, journal, session = make_session(tmp_path)
    service = AutomaticControllerService(store, profiles=[certified(), certified()])
    service.ensure_controller(session.id, expected_version=1)

    running = ParentTask(
        id="pt_1",
        controller_session_id="cs_1",
        request_type="new_task",
        goal="busy",
        acceptance_contract=[Acceptance(id="a1", description="done")],
        data_classification="internal",
        workspace=Workspace(canonical_uri="workspace://x", base_revision="r1"),
        status=TaskStatus.running,
    )
    store.tasks[running.id] = running
    store.commit({"type": "task.created", "data": running.model_dump(mode="json")})

    with pytest.raises(DomainError, match="cannot switch controller while tasks are running"):
        service.switch_controller(session.id, expected_version=2)
    assert store.sessions[session.id].active_controller_epoch_id is not None
    journal.close()


def test_switch_allowed_when_no_running_tasks(tmp_path):
    store, journal, session = make_session(tmp_path)
    service = AutomaticControllerService(store, profiles=[certified(model_id="m1"), certified(model_id="m2")])
    service.ensure_controller(session.id, expected_version=1)

    completed = ParentTask(
        id="pt_1",
        controller_session_id="cs_1",
        request_type="new_task",
        goal="done",
        acceptance_contract=[Acceptance(id="a1", description="done")],
        data_classification="internal",
        workspace=Workspace(canonical_uri="workspace://x", base_revision="r1"),
        status=TaskStatus.completed,
    )
    store.tasks[completed.id] = completed
    store.commit({"type": "task.created", "data": completed.model_dump(mode="json")})

    switched = service.switch_controller(session.id, expected_version=2)
    assert switched.id is not None
    assert switched.version == 2
    journal.close()


def test_epochs_survive_journal_replay(tmp_path):
    store, journal, session = make_session(tmp_path)
    service = AutomaticControllerService(store, profiles=[certified(model_id="m1")])
    selected = service.ensure_controller(session.id, expected_version=1)
    journal.close()

    replayed_journal = LocalJournal(tmp_path)
    replayed = Store(replayed_journal)
    assert replayed.sessions[session.id].active_controller_epoch_id == selected.id
    assert replayed.epochs[selected.id].model_id == "m1"
    assert replayed.epochs[selected.id].status == "active"
    replayed_journal.close()
