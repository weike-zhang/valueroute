from valueroute.application.service import overlaps
from valueroute.domain.models import ResourceRegion


def region(selector_type, selector_value):
    return ResourceRegion(resource_kind="file", resource_id="app.py", selector_type=selector_type, selector_value=selector_value, base_revision="r1")


def test_distinct_symbols_do_not_overlap():
    assert not overlaps(region("symbol", "a"), region("symbol", "b"))


def test_same_symbol_and_whole_file_overlap():
    assert overlaps(region("symbol", "a"), region("symbol", "a"))
    assert overlaps(region("whole_resource", ""), region("symbol", "a"))


def test_unknown_semantics_fail_closed():
    assert overlaps(region("ast_node", "a"), region("symbol", "b"))


def test_database_key_sets_and_partitions_can_run_in_parallel():
    left = ResourceRegion(resource_kind="database", resource_id="orders", selector_type="row_keys", selector_value=[1, 2], base_revision="r1")
    right = ResourceRegion(resource_kind="database", resource_id="orders", selector_type="row_keys", selector_value=[3, 4], base_revision="r1")
    assert not overlaps(left, right)
    partition_a = left.model_copy(update={"selector_type": "partition", "selector_value": "2026-01"})
    partition_b = left.model_copy(update={"selector_type": "partition", "selector_value": "2026-02"})
    assert not overlaps(partition_a, partition_b)


def test_ranges_pointers_and_provider_subresources_are_deterministic():
    base = ResourceRegion(resource_kind="database", resource_id="orders", selector_type="key_range", selector_value={"start": 0, "end": 10}, base_revision="r1")
    disjoint = base.model_copy(update={"selector_value": {"start": 11, "end": 20}})
    assert not overlaps(base, disjoint)
    pointer_a = ResourceRegion(resource_kind="external", resource_id="api", selector_type="json_pointer", selector_value="/items/a", base_revision="r1")
    pointer_b = pointer_a.model_copy(update={"selector_value": "/items/b"})
    assert not overlaps(pointer_a, pointer_b)
    sub_a = pointer_a.model_copy(update={"selector_type": "provider_subresource", "selector_value": "comments"})
    sub_b = sub_a.model_copy(update={"selector_value": "reactions"})
    assert not overlaps(sub_a, sub_b)
