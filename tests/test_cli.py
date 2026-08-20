from collections.abc import Sequence

import pytest

from alza_ai.cli import main

PROJECT = "example-project"


class FakeSenderPolicyStore:
    def __init__(self, entries: Sequence[str] = ()) -> None:
        self.entries = tuple(entries)
        self.writes: list[tuple[str, ...]] = []

    def allowed_senders(self) -> tuple[str, ...]:
        return self.entries

    def set_allowed_senders(self, entries: Sequence[str]) -> None:
        self.entries = tuple(entries)
        self.writes.append(tuple(entries))


def run(store: FakeSenderPolicyStore, *arguments: str) -> int:
    projects: list[str] = []

    def factory(project: str) -> FakeSenderPolicyStore:
        projects.append(project)
        return store

    code = main(list(arguments), sender_policy_store_factory=factory)
    assert projects == [PROJECT]
    return code


def test_ops_02_allowlist_list_prints_live_entries_without_writing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = FakeSenderPolicyStore(("person@example.test", "@alza.cz"))

    code = run(store, "allowlist", "list", "--project", PROJECT)

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == "person@example.test\n@alza.cz\n"
    assert captured.err == ""
    assert store.writes == []


def test_ops_02_allowlist_add_normalizes_and_deduplicates_entries(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = FakeSenderPolicyStore(("Person@Example.Test",))

    code = run(store, "allowlist", "add", "--project", PROJECT, "@ALZA.CZ")

    captured = capsys.readouterr()
    assert code == 0
    assert store.writes == [("person@example.test", "@alza.cz")]
    assert captured.out == "person@example.test\n@alza.cz\n"


def test_ops_02_allowlist_add_is_idempotent() -> None:
    store = FakeSenderPolicyStore(("@alza.cz",))

    code = run(store, "allowlist", "add", "--project", PROJECT, "@alza.cz")

    assert code == 0
    assert store.writes == [("@alza.cz",)]
    assert store.entries == ("@alza.cz",)


@pytest.mark.parametrize("entry", ("not-an-address", "@", "person@", "@ alza.cz"))
def test_ops_02_allowlist_add_rejects_a_malformed_entry(
    entry: str, capsys: pytest.CaptureFixture[str]
) -> None:
    store = FakeSenderPolicyStore(("@alza.cz",))

    code = run(store, "allowlist", "add", "--project", PROJECT, entry)

    captured = capsys.readouterr()
    assert code == 1
    assert store.writes == []
    assert captured.out == ""
    assert captured.err == f"Invalid allowlist entry: {entry}\n"


def test_ops_02_allowlist_remove_drops_one_normalized_entry(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = FakeSenderPolicyStore(("person@example.test", "@alza.cz"))

    code = run(store, "allowlist", "remove", "--project", PROJECT, "@Alza.CZ")

    captured = capsys.readouterr()
    assert code == 0
    assert store.writes == [("person@example.test",)]
    assert captured.out == "person@example.test\n"


def test_ops_02_allowlist_remove_reports_an_absent_entry(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = FakeSenderPolicyStore(("@alza.cz",))

    code = run(store, "allowlist", "remove", "--project", PROJECT, "other@a.test")

    captured = capsys.readouterr()
    assert code == 1
    assert store.writes == []
    assert captured.err == "Allowlist entry not present: other@a.test\n"


def test_ops_02_allowlist_requires_an_explicit_project() -> None:
    with pytest.raises(SystemExit) as failure:
        main(["allowlist", "list"])

    assert failure.value.code == 2
