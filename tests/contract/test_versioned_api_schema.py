import json
from pathlib import Path
from typing import Any

from valueroute.api.app import create_app
from valueroute.api.schemas import (
    AdvisoryRequest,
    ControlTaskRequest,
    CreateSessionRequest,
    CreateTaskRequest,
    DecideApprovalRequest,
    OwnerReviewRequest,
    RecordEvidenceRequest,
    RegisterEpochRequest,
    RequestApprovalRequest,
    SubmitPlanRequest,
    VerifyReviewRequest,
    VerifyTaskRequest,
)

SCHEMA_DIR = Path(__file__).parents[2] / "schemas" / "v1"
REQUEST_MODELS = {
    "CreateSessionRequest": CreateSessionRequest,
    "RegisterEpochRequest": RegisterEpochRequest,
    "CreateTaskRequest": CreateTaskRequest,
    "VerifyTaskRequest": VerifyTaskRequest,
    "RecordEvidenceRequest": RecordEvidenceRequest,
    "RequestApprovalRequest": RequestApprovalRequest,
    "DecideApprovalRequest": DecideApprovalRequest,
    "ControlTaskRequest": ControlTaskRequest,
    "SubmitPlanRequest": SubmitPlanRequest,
    "OwnerReviewRequest": OwnerReviewRequest,
    "VerifyReviewRequest": VerifyReviewRequest,
    "AdvisoryRequest": AdvisoryRequest,
}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def expand_schema(node: Any, local_defs: dict[str, Any], components: dict[str, Any], seen: frozenset[str] = frozenset()) -> Any:
    if isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if ref.startswith("#/$defs/"):
            name = ref.removeprefix("#/$defs/")
            assert name not in seen
            return expand_schema(local_defs[name], local_defs, components, seen | {name})
        if ref.startswith("#/components/schemas/"):
            name = ref.removeprefix("#/components/schemas/")
            assert name not in seen
            return expand_schema(components[name], local_defs, components, seen | {name})
        raise AssertionError(f"unexpected schema reference: {ref}")
    if isinstance(node, dict):
        return {
            key: expand_schema(value, local_defs, components, seen)
            for key, value in node.items()
            if key != "$defs"
        }
    if isinstance(node, list):
        return [expand_schema(value, local_defs, components, seen) for value in node]
    return node


def comparable_schema(node: Any) -> Any:
    """Normalize the harmless OpenAPI omission of a nullable default: null."""
    if isinstance(node, dict):
        return {
            key: comparable_schema(value)
            for key, value in node.items()
            if not (key == "default" and value is None)
        }
    if isinstance(node, list):
        return [comparable_schema(value) for value in node]
    return node


def test_v1_manifest_and_artifacts_are_complete_and_model_generated():
    manifest = load_json(SCHEMA_DIR / "manifest.json")

    assert manifest["schema_version"] == "v1"
    assert manifest["media_type"] == "application/schema+json"
    assert set(manifest["schemas"]) == set(REQUEST_MODELS)
    assert set(manifest["schemas"].values()) == {
        "create-session-request.json",
        "register-epoch-request.json",
        "create-task-request.json",
        "verify-task-request.json",
        "record-evidence-request.json",
        "request-approval-request.json",
        "decide-approval-request.json",
        "control-task-request.json",
        "submit-plan-request.json",
        "owner-review-request.json",
        "verify-review-request.json",
        "advisory-request.json",
    }

    for model_name, model in REQUEST_MODELS.items():
        artifact_path = SCHEMA_DIR / manifest["schemas"][model_name]
        assert artifact_path.is_file()
        assert load_json(artifact_path) == model.model_json_schema()


def test_openapi_request_components_and_routes_match_the_v1_contract(tmp_path: Path):
    manifest = load_json(SCHEMA_DIR / "manifest.json")
    openapi = create_app(tmp_path).openapi()
    components = openapi["components"]["schemas"]

    assert openapi["info"]["version"] == "0.0.1"
    for model_name, filename in manifest["schemas"].items():
        artifact = load_json(SCHEMA_DIR / filename)
        assert model_name in components
        assert comparable_schema(expand_schema(artifact, artifact.get("$defs", {}), components)) == comparable_schema(
            expand_schema(components[model_name], artifact.get("$defs", {}), components)
        )

    actual_routes = {}
    for path, path_item in openapi["paths"].items():
        for method, operation in path_item.items():
            if method.upper() != "POST" or "requestBody" not in operation:
                continue
            schema = operation["requestBody"]["content"]["application/json"]["schema"]
            actual_routes[f"POST {path}"] = schema["$ref"].removeprefix("#/components/schemas/")

    assert actual_routes == manifest["routes"]


def test_v1_artifacts_keep_strict_unknown_field_boundaries():
    for model in REQUEST_MODELS.values():
        assert model.model_config["extra"] == "forbid"
    for filename in load_json(SCHEMA_DIR / "manifest.json")["schemas"].values():
        assert load_json(SCHEMA_DIR / filename)["additionalProperties"] is False
