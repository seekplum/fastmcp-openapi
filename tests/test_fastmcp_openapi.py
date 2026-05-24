import asyncio
import importlib.util
import inspect
import json
import sys
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypedDict, TypeVar, cast, overload

from fastmcp import Context, FastMCP
from fastmcp.tools.base import ToolResult
from fastmcp.utilities.tasks import TaskMeta
from fastmcp.utilities.versions import VersionSpec
from mcp.types import CreateTaskResult, TextContent
from pydantic import BaseModel, Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from fastmcp_openapi import FastMCPOpenAPI, _build_flattened_signature, _maybe_await, _mcp_custom_route
from fastmcp_openapi.config import FastMCPOpenAPIConfig
from fastmcp_openapi.extractor import (
    _build_parameters,
    _get_query_response_model,
    _get_return_schema,
    _model_schema_components,
    _normalize_schema_name,
    _parse_docstring_args,
    _resolve_type_str,
    build_tool_registry_entry,
)
from fastmcp_openapi.routes import RouteRegistrar, build_tool_route_path
from fastmcp_openapi.templates import get_docs_html, get_favicon_svg


class CreateItemInput(BaseModel):
    name: str = Field(description="条目名称")
    count: int = Field(default=1, description="条目数量")


class ItemResponse(BaseModel):
    name: str
    count: int


class QueryResponse(BaseModel):
    ok: bool


class NestedModel(BaseModel):
    value: str


class ModelWithFactory(BaseModel):
    required_name: str
    nested: NestedModel = Field(default_factory=lambda: NestedModel(value="factory"))


class FakeClient:
    def query(self, req: object, response_model: type[BaseModel] | str, flag: bool = False) -> None:
        return None


T = TypeVar("T")


class RegisteredRoute(TypedDict):
    path: str
    methods: list[str]
    name: str | None
    include_in_schema: bool
    func: Callable[[Request], Awaitable[Response]]


class ToolRegistryEntry(TypedDict):
    name: str
    title: str
    summary: str
    description: str
    tags: list[str]
    tag_descriptions: dict[str, str]
    parameters: list[dict[str, Any]]
    output_schema: dict[str, Any]
    input_model_name: str | None
    response_model_name: str | None
    component_schemas: dict[str, Any]


def run_async(awaitable: Awaitable[T]) -> T:
    return asyncio.run(cast(Coroutine[Any, Any, T], awaitable))


def test_tool_registration_builds_registry_for_plain_params() -> None:
    mcp = FastMCP("demo")
    openapi = FastMCPOpenAPI(mcp)

    @openapi.tool("sum_numbers")
    async def add(ctx: Context, a: int, b: int = 1) -> int:
        """整数加法。

        Args:
            a: 左操作数
            b: 右操作数
        """
        return a + b

    info = openapi._registry["sum_numbers"]
    parameters = {item["name"]: item for item in info["parameters"]}

    assert info["name"] == "sum_numbers"
    assert info["summary"] == "整数加法"
    assert parameters["a"]["required"] is True
    assert parameters["a"]["description"] == "左操作数"
    assert parameters["b"]["required"] is False
    assert parameters["b"]["default"] == 1
    assert parameters["b"]["description"] == "右操作数"


def test_tool_registration_builds_registry_and_openapi_for_body_model() -> None:
    mcp = FastMCP("demo")
    openapi = FastMCPOpenAPI(mcp)

    @openapi.tool()
    async def create_item(ctx: Context, param: CreateItemInput) -> ItemResponse:
        return ItemResponse(name=param.name, count=param.count)

    info = openapi._registry["create_item"]
    parameter_names = {item["name"] for item in info["parameters"]}
    registrar = RouteRegistrar(mcp, openapi.config, openapi._registry)
    schema = registrar._build_openapi_schema()
    operation = schema["paths"]["/call/create_item"]["get"]

    assert info["input_model_name"] == "CreateItemInput"
    assert "CreateItemInput" in info["component_schemas"]
    assert {"name", "count"}.issubset(parameter_names)
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/CreateItemInput"
    }
    assert (
        operation["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["name"]["type"] == "string"
    )
    assert (
        operation["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["count"]["type"]
        == "integer"
    )


def test_setup_does_not_depend_on_list_tools() -> None:
    mcp = FastMCP("demo")

    async def fail_list_tools(*, run_middleware: bool = True) -> Sequence[Any]:
        raise AssertionError(f"setup() 不应再调用 list_tools: {run_middleware}")

    mcp.list_tools = fail_list_tools  # type: ignore[assignment]
    openapi = FastMCPOpenAPI(mcp)

    @openapi.tool()
    async def ping(ctx: Context) -> dict[str, bool]:
        return {"ok": True}

    run_async(openapi.setup())

    assert "ping" in openapi._registry


class StubMCP:
    def __init__(self) -> None:
        self.registered: RegisteredRoute | None = None

    def custom_route(
        self, path: str, methods: list[str], name: str | None = None, include_in_schema: bool = True
    ) -> Callable[
        [Callable[[Request], Coroutine[Any, Any, Response]]], Callable[[Request], Coroutine[Any, Any, Response]]
    ]:
        def decorator(
            func: Callable[[Request], Coroutine[Any, Any, Response]],
        ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
            self.registered = {
                "path": path,
                "methods": methods,
                "name": name,
                "include_in_schema": include_in_schema,
                "func": func,
            }
            return func

        return decorator


class RecordingMCP(FastMCP):
    def __init__(self) -> None:
        super().__init__("recording")
        self.routes: dict[str, RegisteredRoute] = {}

    def custom_route(
        self, path: str, methods: list[str], name: str | None = None, include_in_schema: bool = True
    ) -> Callable[[Callable[[Request], Awaitable[Response]]], Callable[[Request], Awaitable[Response]]]:
        def decorator(func: Callable[[Request], Awaitable[Response]]) -> Callable[[Request], Awaitable[Response]]:
            self.routes[path] = {
                "path": path,
                "methods": methods,
                "name": name,
                "include_in_schema": include_in_schema,
                "func": func,
            }
            return func

        return decorator


def test_custom_route_runs_before_and_after_handlers() -> None:
    events: list[str] = []
    stub = StubMCP()

    def before_sync(request: Request) -> None:
        events.append(f"before-sync:{request.url.path}")

    async def before_async(request: Request) -> None:
        events.append(f"before-async:{request.url.path}")

    def after_sync(_request: Request, response: Response) -> None:
        events.append(f"after-sync:{response.status_code}")

    async def after_async(_request: Request, _response: Response) -> Response:
        events.append("after-async:replace")
        return PlainTextResponse("replaced", status_code=202)

    decorator = _mcp_custom_route(
        stub,  # type: ignore[arg-type]
        "/demo",
        methods=["GET"],
        before_request_handlers=[before_sync, before_async],
        after_request_handlers=[after_sync, after_async],
    )

    @decorator
    async def demo(_request: Request) -> Response:
        events.append("handler")
        return PlainTextResponse("ok", status_code=200)

    request = Request({"type": "http", "method": "GET", "path": "/demo", "headers": []})
    registered = stub.registered
    assert registered is not None
    response = run_async(registered["func"](request))

    assert demo is registered["func"]
    assert response.status_code == 202
    assert response.body == b"replaced"
    assert events == [
        "before-sync:/demo",
        "before-async:/demo",
        "handler",
        "after-sync:200",
        "after-async:replace",
    ]


def test_build_flattened_signature_handles_default_factory_as_keyword_only() -> None:
    async def tool_with_model(ctx: Context, payload: ModelWithFactory, tail: str) -> dict[str, str]:
        return {"tail": tail, "value": payload.nested.value}

    signature, field_map = _build_flattened_signature(tool_with_model, {"payload": ModelWithFactory})
    params = list(signature.parameters.values())

    assert field_map == {"payload": ["required_name", "nested"]}
    assert params[1].name == "required_name"
    assert params[1].kind.name == "POSITIONAL_OR_KEYWORD"
    assert params[2].name == "nested"
    assert params[2].kind.name == "KEYWORD_ONLY"
    assert params[3].name == "tail"
    assert params[3].kind.name == "KEYWORD_ONLY"


def test_templates_render_expected_assets() -> None:
    config = FastMCPOpenAPIConfig(title="Demo Docs", openapi_route="/openapi.json", favicon_url="/custom.svg")
    html = get_docs_html(config)
    favicon = get_favicon_svg()

    assert "Demo Docs" in html
    assert 'href="/custom.svg"' in html
    assert 'url: "/openapi.json"' in html
    assert favicon.startswith('<svg xmlns="http://www.w3.org/2000/svg"')


def test_build_tool_route_path_validates_tool_name() -> None:
    assert build_tool_route_path("/call", "ping") == "/call/ping"
    assert build_tool_route_path("/call/", "ping") == "/call/ping"

    for bad_name in ("", "bad/name", "{dynamic}", "bad}"):
        try:
            build_tool_route_path("/call", bad_name)
        except ValueError:
            pass
        else:
            raise AssertionError(f"tool 名称 {bad_name!r} 应该校验失败")


def test_route_registrar_builds_plain_request_body_and_response_fallbacks() -> None:
    config = FastMCPOpenAPIConfig(
        title="Demo",
        version="1.0.0",
        description="desc",
        base_url="https://example.com",
        extra_servers=[{"url": "https://backup.example.com", "description": "backup"}],
    )
    registry = {
        "plain_tool": {
            "name": "plain_tool",
            "title": "Plain Tool",
            "summary": "",
            "description": "plain desc",
            "tags": [],
            "tag_descriptions": {"Custom": "自定义标签"},
            "parameters": [
                {"name": "color", "type": "string", "required": True, "enum": ["red", "blue"]},
                {"name": "limit", "type": "integer", "required": False, "default": 3},
            ],
            "output_schema": {},
            "input_model_name": None,
            "response_model_name": None,
            "component_schemas": {},
        }
    }
    registrar = RouteRegistrar(
        SimpleNamespace(name="demo", custom_route=lambda *args, **kwargs: None), config, registry
    )

    schema = registrar._build_openapi_schema()
    operation = schema["paths"]["/call/plain_tool"]["get"]
    request_body = operation["requestBody"]["content"]["application/json"]["schema"]

    assert schema["info"]["description"] == "desc"
    assert schema["servers"][0]["url"] == "https://example.com"
    assert schema["servers"][1]["url"] == "https://backup.example.com"
    assert {tag["name"] for tag in schema["tags"]} == {"Custom"}
    assert operation["summary"] == "Plain Tool"
    assert operation["tags"] == ["Tools"]
    assert request_body["required"] == ["color"]
    assert request_body["properties"]["color"]["enum"] == ["red", "blue"]
    assert request_body["properties"]["limit"]["default"] == 3
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {"type": "object"}
    assert registrar._build_request_body({"parameters": []}) is None
    assert registrar._build_response_schema({"response_model_name": "ItemResponse"}) == {
        "$ref": "#/components/schemas/ItemResponse"
    }
    assert registrar._build_response_schema({"output_schema": {"properties": {"ok": {"type": "boolean"}}}}) == {
        "properties": {"ok": {"type": "boolean"}}
    }


def test_route_registrar_registers_routes_and_handlers() -> None:
    route_mcp = RecordingMCP()
    config = FastMCPOpenAPIConfig(title="Demo", description="route-desc", base_url="https://example.com", verbose=True)
    registry: dict[str, ToolRegistryEntry] = {
        "tool": {
            "name": "tool",
            "title": "Tool",
            "summary": "Tool Summary",
            "description": "Tool Description",
            "tags": ["Alpha"],
            "tag_descriptions": {},
            "parameters": [],
            "output_schema": {},
            "input_model_name": None,
            "response_model_name": None,
            "component_schemas": {},
        }
    }
    registrar = RouteRegistrar(route_mcp, config, registry)
    registrar.register_all()

    options_request = Request({"type": "http", "method": "OPTIONS", "path": "/openapi.json", "headers": []})
    get_request = Request({"type": "http", "method": "GET", "path": "/openapi.json", "headers": []})
    docs_request = Request({"type": "http", "method": "GET", "path": "/docs", "headers": []})
    favicon_request = Request({"type": "http", "method": "GET", "path": "/favicon.svg", "headers": []})
    tools_request = Request({"type": "http", "method": "GET", "path": "/api/tools", "headers": []})

    options_response = run_async(route_mcp.routes["/openapi.json"]["func"](options_request))
    openapi_response = run_async(route_mcp.routes["/openapi.json"]["func"](get_request))
    docs_response = run_async(route_mcp.routes["/docs"]["func"](docs_request))
    favicon_response = run_async(route_mcp.routes["/favicon.svg"]["func"](favicon_request))
    tools_response = run_async(route_mcp.routes["/api/tools"]["func"](tools_request))

    assert route_mcp.routes["/openapi.json"]["methods"] == ["GET", "OPTIONS"]
    assert options_response.headers["Access-Control-Allow-Origin"] == "*"
    assert openapi_response.headers["Access-Control-Allow-Origin"] == "*"
    assert b"Tool Summary" in openapi_response.body
    assert b"swagger-ui" in docs_response.body
    assert favicon_response.media_type == "image/svg+xml"
    assert favicon_response.headers["Cache-Control"] == "public, max-age=86400"
    assert b'"server":"recording"' in tools_response.body


def test_tool_registration_registers_exact_proxy_routes() -> None:
    mcp = RecordingMCP()
    openapi = FastMCPOpenAPI(mcp)  # type: ignore[arg-type]

    @openapi.tool()
    async def alpha(ctx: Context) -> dict[str, bool]:
        return {"ok": True}

    @openapi.tool("beta")
    async def beta_tool(ctx: Context) -> dict[str, bool]:
        return {"ok": True}

    assert "/status" in mcp.routes
    assert "/call/alpha" in mcp.routes
    assert "/call/beta" in mcp.routes
    assert "/call/{tool_name}" not in mcp.routes
    assert mcp.routes["/call/alpha"]["methods"] == ["GET", "POST"]
    assert mcp.routes["/call/beta"]["methods"] == ["GET", "POST"]


def test_openapi_paths_match_registered_proxy_routes() -> None:
    mcp = RecordingMCP()
    openapi = FastMCPOpenAPI(mcp)  # type: ignore[arg-type]

    @openapi.tool()
    async def alpha(ctx: Context, param: CreateItemInput) -> ItemResponse:
        return ItemResponse(name=param.name, count=param.count)

    @openapi.tool("beta")
    async def beta_tool(ctx: Context) -> dict[str, bool]:
        return {"ok": True}

    schema = RouteRegistrar(mcp, openapi.config, openapi._registry)._build_openapi_schema()
    schema_paths = {path for path in schema["paths"] if path.startswith(openapi.config.api_base)}
    runtime_paths = {
        path
        for path in mcp.routes
        if path.startswith(openapi.config.api_base)
        and "{" not in path
        and path not in {openapi.config.openapi_route, openapi.config.api_tools_route}
    }

    assert schema_paths == runtime_paths
    assert f"{openapi.config.api_base}/{{tool_name}}" not in runtime_paths


def test_call_tool_proxy_uses_shared_logic_and_unknown_route_is_absent() -> None:
    class ProxyMCP(RecordingMCP):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[tuple[str, dict[str, Any] | None]] = []

        @overload
        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any] | None = None,
            *,
            version: VersionSpec | None = None,
            run_middleware: bool = True,
            task_meta: None = None,
        ) -> ToolResult:
            pass

        @overload
        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any] | None = None,
            *,
            version: VersionSpec | None = None,
            run_middleware: bool = True,
            task_meta: TaskMeta,
        ) -> CreateTaskResult:
            pass

        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any] | None = None,
            *,
            version: VersionSpec | None = None,
            run_middleware: bool = True,
            task_meta: TaskMeta | None = None,
        ) -> ToolResult | CreateTaskResult:
            del version, run_middleware, task_meta
            self.calls.append((name, arguments))
            return ToolResult(content=[TextContent(type="text", text='{"ok": true}')])

    mcp = ProxyMCP()
    openapi = FastMCPOpenAPI(mcp)  # type: ignore[arg-type]

    @openapi.tool()
    async def alpha(ctx: Context) -> dict[str, bool]:
        return {"ok": True}

    get_request = Request(
        {"type": "http", "method": "GET", "path": "/call/alpha", "query_string": b"x=1", "headers": []}
    )

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b'{"x": 2}', "more_body": False}

    post_request = Request(
        {"type": "http", "method": "POST", "path": "/call/alpha", "headers": [(b"content-type", b"application/json")]},
        receive=receive,
    )

    get_response = run_async(mcp.routes["/call/alpha"]["func"](get_request))
    post_response = run_async(mcp.routes["/call/alpha"]["func"](post_request))

    assert get_response.body == b'{"ok":true}'
    assert post_response.body == b'{"ok":true}'
    assert mcp.calls == [("alpha", {"x": "1"}), ("alpha", {"x": 2})]
    assert "/call/not_registered" not in mcp.routes


def test_call_tool_proxy_returns_422_for_validation_error() -> None:
    mcp = RecordingMCP()
    openapi = FastMCPOpenAPI(mcp)  # type: ignore[arg-type]

    @openapi.tool()
    async def alpha(ctx: Context, param: CreateItemInput) -> ItemResponse:
        return ItemResponse(name=param.name, count=param.count)

    request = Request({"type": "http", "method": "GET", "path": "/call/alpha", "query_string": b"", "headers": []})
    response = run_async(mcp.routes["/call/alpha"]["func"](request))
    payload = json.loads(bytes(response.body))

    assert response.status_code == 422
    assert payload[0]["type"] == "missing_argument"
    assert payload[0]["loc"] == ["name"]


def test_call_tool_proxy_returns_500_for_unexpected_error() -> None:
    class ExplodingMCP(RecordingMCP):
        @overload
        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any] | None = None,
            *,
            version: VersionSpec | None = None,
            run_middleware: bool = True,
            task_meta: None = None,
        ) -> ToolResult:
            pass

        @overload
        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any] | None = None,
            *,
            version: VersionSpec | None = None,
            run_middleware: bool = True,
            task_meta: TaskMeta,
        ) -> CreateTaskResult:
            pass

        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any] | None = None,
            *,
            version: VersionSpec | None = None,
            run_middleware: bool = True,
            task_meta: TaskMeta | None = None,
        ) -> ToolResult | CreateTaskResult:
            del version, run_middleware, task_meta
            raise RuntimeError(f"boom:{name}:{arguments}")

    mcp = ExplodingMCP()
    openapi = FastMCPOpenAPI(mcp)  # type: ignore[arg-type]

    @openapi.tool()
    async def alpha(ctx: Context) -> dict[str, bool]:
        return {"ok": True}

    request = Request({"type": "http", "method": "GET", "path": "/call/alpha", "query_string": b"", "headers": []})
    response = run_async(mcp.routes["/call/alpha"]["func"](request))
    payload = json.loads(bytes(response.body))

    assert response.status_code == 500
    assert payload == {"code": 500, "message": "tool 调用失败"}


def test_call_tool_proxy_uses_custom_validation_error_handler() -> None:
    handler_calls: list[tuple[str, str, ValidationError]] = []

    async def validation_error_handler(request: Request, tool_name: str, exc: ValidationError) -> Response:
        handler_calls.append((request.method, tool_name, exc))
        return JSONResponse({"handled": "validation", "tool": tool_name}, status_code=400)

    config = FastMCPOpenAPIConfig(tool_proxy_validation_error_handler=validation_error_handler)
    mcp = RecordingMCP()
    openapi = FastMCPOpenAPI(mcp, config=config)  # type: ignore[arg-type]

    @openapi.tool()
    async def alpha(ctx: Context, param: CreateItemInput) -> ItemResponse:
        return ItemResponse(name=param.name, count=param.count)

    request = Request({"type": "http", "method": "GET", "path": "/call/alpha", "query_string": b"", "headers": []})
    response = run_async(mcp.routes["/call/alpha"]["func"](request))
    payload = json.loads(bytes(response.body))

    assert response.status_code == 400
    assert payload == {"handled": "validation", "tool": "alpha"}
    assert len(handler_calls) == 1
    assert handler_calls[0][0] == "GET"
    assert handler_calls[0][1] == "alpha"
    assert isinstance(handler_calls[0][2], ValidationError)


def test_call_tool_proxy_uses_custom_exception_handler() -> None:
    class ExplodingMCP(RecordingMCP):
        @overload
        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any] | None = None,
            *,
            version: VersionSpec | None = None,
            run_middleware: bool = True,
            task_meta: None = None,
        ) -> ToolResult:
            pass

        @overload
        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any] | None = None,
            *,
            version: VersionSpec | None = None,
            run_middleware: bool = True,
            task_meta: TaskMeta,
        ) -> CreateTaskResult:
            pass

        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any] | None = None,
            *,
            version: VersionSpec | None = None,
            run_middleware: bool = True,
            task_meta: TaskMeta | None = None,
        ) -> ToolResult | CreateTaskResult:
            del version, run_middleware, task_meta
            raise RuntimeError(f"boom:{name}:{arguments}")

    handler_calls: list[tuple[str, str, Exception]] = []

    def exception_handler(request: Request, tool_name: str, exc: Exception) -> Response:
        handler_calls.append((request.method, tool_name, exc))
        return JSONResponse({"handled": "exception", "message": str(exc)}, status_code=503)

    config = FastMCPOpenAPIConfig(tool_proxy_exception_handler=exception_handler)
    mcp = ExplodingMCP()
    openapi = FastMCPOpenAPI(mcp, config=config)  # type: ignore[arg-type]

    @openapi.tool()
    async def alpha(ctx: Context) -> dict[str, bool]:
        return {"ok": True}

    request = Request({"type": "http", "method": "GET", "path": "/call/alpha", "query_string": b"", "headers": []})
    response = run_async(mcp.routes["/call/alpha"]["func"](request))
    payload = json.loads(bytes(response.body))

    assert response.status_code == 503
    assert payload == {"handled": "exception", "message": "boom:alpha:{}"}
    assert len(handler_calls) == 1
    assert handler_calls[0][0] == "GET"
    assert handler_calls[0][1] == "alpha"
    assert isinstance(handler_calls[0][2], RuntimeError)


def test_call_tool_proxy_custom_handler_can_return_plain_text_response() -> None:
    class ExplodingMCP(RecordingMCP):
        @overload
        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any] | None = None,
            *,
            version: VersionSpec | None = None,
            run_middleware: bool = True,
            task_meta: None = None,
        ) -> ToolResult:
            pass

        @overload
        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any] | None = None,
            *,
            version: VersionSpec | None = None,
            run_middleware: bool = True,
            task_meta: TaskMeta,
        ) -> CreateTaskResult:
            pass

        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any] | None = None,
            *,
            version: VersionSpec | None = None,
            run_middleware: bool = True,
            task_meta: TaskMeta | None = None,
        ) -> ToolResult | CreateTaskResult:
            del version, run_middleware, task_meta
            raise RuntimeError(f"boom:{name}:{arguments}")

    def exception_handler(request: Request, tool_name: str, exc: Exception) -> Response:
        del request, exc
        return PlainTextResponse(f"handled:{tool_name}", status_code=418)

    config = FastMCPOpenAPIConfig(tool_proxy_exception_handler=exception_handler)
    mcp = ExplodingMCP()
    openapi = FastMCPOpenAPI(mcp, config=config)  # type: ignore[arg-type]

    @openapi.tool()
    async def alpha(ctx: Context) -> dict[str, bool]:
        return {"ok": True}

    request = Request({"type": "http", "method": "GET", "path": "/call/alpha", "query_string": b"", "headers": []})
    response = run_async(mcp.routes["/call/alpha"]["func"](request))

    assert response.status_code == 418
    assert bytes(response.body) == b"handled:alpha"
    assert response.media_type == "text/plain"


def test_registry_entry_builder_handles_query_model_and_special_tags() -> None:
    client = FakeClient()

    def shuiyin_query(req: object) -> QueryResponse:
        """查询水印。

        Args:
            req: 请求体
        """
        client.query(req, QueryResponse, False)
        return QueryResponse(ok=True)

    tool = SimpleNamespace(
        description="tool-desc",
        tags={"raw"},
        annotations=SimpleNamespace(title="水印查询"),
        parameters={
            "properties": {
                "req": {"$ref": "#/components/schemas/Req"},
                "level": {"type": "string", "example": "high"},
            },
            "required": ["req"],
        },
    )

    info = build_tool_registry_entry("shuiyin_query", tool, shuiyin_query, shuiyin_query, {})

    assert info["title"] == "水印查询"
    assert info["tags"] == ["水印相关接口"]
    assert info["tag_descriptions"]["水印相关接口"]
    assert info["response_model_name"] == "QueryResponse"
    assert info["parameters"][0]["type"] == "Req"
    assert info["parameters"][1]["example"] == "high"


def test_extractor_helpers_cover_edge_cases() -> None:
    client = FakeClient()

    assert _resolve_type_str("bad") == "any"
    assert _resolve_type_str({"oneOf": [{"type": "null"}, {"type": "integer"}]}) == "integer"
    assert _resolve_type_str({"$ref": "#/components/schemas/Model"}) == "Model"
    assert _parse_docstring_args("") == {}
    assert _normalize_schema_name("demo name/with space") == "demo_name_with_space"

    class GenericResponse(BaseModel):
        value: str

    class LocalModel(BaseModel):
        name: str

    def returns_model() -> LocalModel:
        return LocalModel(name="x")

    def returns_plain() -> int:
        return 1

    def no_return_annotation(value):
        return value

    def broken_signature(*args, **kwargs):
        return args, kwargs

    doc = """摘要。\n\nArgs:\n    first: 第一行\n      第二行\n\nReturns:\n    done\n"""
    parsed = _parse_docstring_args(doc)
    assert parsed["first"] == "第一行 第二行"

    defaults = _get_return_schema(returns_model)
    assert defaults["properties"]["name"]["type"] == "string"
    assert _get_return_schema(returns_plain) == {"description": "int"}
    assert _get_return_schema(no_return_annotation) == {}
    assert _get_return_schema(None) == {}

    components_name, components = _model_schema_components(ModelWithFactory)
    assert components_name == "ModelWithFactory"
    assert "NestedModel" in components

    params = _build_parameters(
        {
            "properties": {
                "status": {"oneOf": [{"type": "null"}, {"type": "string"}], "enum": ["ok"], "example": "ok"},
                "limit": {"type": "integer"},
            }
        },
        {"limit": {"has_default": True, "default": 5}},
        {"limit": "最大条数"},
    )
    assert params[0]["type"] == "string"
    assert params[0]["enum"] == ["ok"]
    assert params[0]["example"] == "ok"
    assert params[1]["default"] == 5
    assert params[1]["description"] == "最大条数"

    assert _get_query_response_model(None) is None

    def no_query() -> QueryResponse:
        return QueryResponse(ok=True)

    assert _get_query_response_model(no_query) is None

    def query_with_literal(req: object) -> QueryResponse:
        client.query(req, "QueryResponse", False)
        return QueryResponse(ok=True)

    assert _get_query_response_model(query_with_literal) is None

    def query_fn(req: object) -> QueryResponse:
        client.query(req, QueryResponse, False)
        return QueryResponse(ok=True)

    assert _get_query_response_model(query_fn) is QueryResponse

    import fastmcp_openapi.extractor as extractor_module

    original_signature = extractor_module.inspect.signature

    def raising_signature(
        _fn: Callable[..., Any],
        *,
        follow_wrapped: bool = True,
        globals: Mapping[str, Any] | None = None,
        locals: Mapping[str, Any] | None = None,
        eval_str: bool = False,
    ) -> inspect.Signature:
        del follow_wrapped, globals, locals, eval_str
        raise ValueError("boom")

    extractor_module.inspect.signature = cast(Any, raising_signature)
    try:
        assert extractor_module._get_signature_defaults(broken_signature) == {}
    finally:
        extractor_module.inspect.signature = original_signature


def _build_demo_openapi_schema() -> dict[str, Any]:
    project_root = Path(__file__).resolve().parents[1]
    module_path = project_root / "examples" / "demo_server.py"
    module_name = "demo_server_module"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise AssertionError("无法加载 examples/demo_server.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    run_async(module.openapi.setup())
    return RouteRegistrar(module.openapi.mcp, module.openapi.config, module.openapi._registry)._build_openapi_schema()


def test_demo_server_generates_expected_openapi_schema() -> None:
    schema = _build_demo_openapi_schema()

    assert schema["info"]["title"] == "Demo MCP Tools API"
    assert schema["info"]["description"] == "Demo FastMCP OpenAPI docs"
    assert schema["servers"][0]["url"] == "http://127.0.0.1:8333"

    operation = schema["paths"]["/call/item_add"]["get"]
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ItemAddInput"
    }

    response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert response_schema["title"] == "BaseResponse[ItemAddData]"
    assert response_schema["properties"]["message"]["type"] == "string"
    assert response_schema["properties"]["code"]["type"] == "integer"
    assert response_schema["properties"]["data"]["anyOf"][0]["$ref"] == "#/$defs/ItemAddData"


def test_openapi_json_matches_demo_schema_snapshot() -> None:
    schema = _build_demo_openapi_schema()
    snapshot_path = Path(__file__).resolve().parents[1] / "examples" / "openapi.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))

    assert snapshot == schema


def test_maybe_await_supports_sync_and_async_values() -> None:
    async def async_value() -> str:
        return "async"

    assert run_async(_maybe_await(async_value())) == "async"
    assert run_async(_maybe_await("sync")) == "sync"
