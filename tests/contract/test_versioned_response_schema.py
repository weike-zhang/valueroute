import json
from pathlib import Path

from valueroute.api.app import create_app


def test_response_manifest_matches_openapi_components_and_routes(tmp_path: Path):
    manifest = json.loads((Path(__file__).parents[2] / "schemas/v1/response-manifest.json").read_text(encoding="utf-8"))
    openapi = create_app(tmp_path).openapi()
    components = openapi["components"]["schemas"]
    assert manifest["schema_version"] == "v1"
    assert set(manifest["openapi_components"]) <= set(components)
    actual = {}
    for path, item in openapi["paths"].items():
        for method, operation in item.items():
            if method.upper() not in {"GET", "POST"} or "responses" not in operation:
                continue
            for status, response in operation["responses"].items():
                schema = response.get("content", {}).get("application/json", {}).get("schema")
                if schema and "$ref" in schema:
                    actual[f"{method.upper()} {path}"] = schema["$ref"].split("/")[-1]
                    break
    assert {key: actual[key] for key in manifest["routes"]} == manifest["routes"]
