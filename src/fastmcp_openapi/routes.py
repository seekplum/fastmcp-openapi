"""FastMCP OpenAPI 路由注册"""

import re
from typing import Any

from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from .config import FastMCPOpenAPIConfig
from .templates import get_docs_html, get_favicon_svg

OPENAPI3_REF_TEMPLATE = "#/components/schemas/{model}"


def build_tool_route_path(api_base: str, tool_name: str) -> str:
    """构建单个 tool 的精确 HTTP 路径。"""
    normalized_base = api_base.rstrip("/") or "/"
    if not tool_name or any(part in tool_name for part in ("/", "{", "}")):
        raise ValueError(f"非法 tool 名称: {tool_name}")
    return f"{normalized_base}/{tool_name}"


class ValidationErrorModel(BaseModel):
    type: str = Field(..., title="Error Type", description="A computer-readable identifier of the error type.")
    loc: list[Any] = Field(..., title="Location", description="The error's location as a list.")
    msg: str = Field(..., title="Message", description="A human readable explanation of the error.")
    input: Any = Field(..., title="Input", description="The input provided for validation.")
    url: str | None = Field(default=None, title="URL", description="The URL to further information about the error.")
    ctx: dict[str, Any] | None = Field(
        default=None,
        title="Error context",
        description="An optional object which contains values required to render the error message.",
    )


class RouteRegistrar:
    """向 FastMCP server 注册 OpenAPI 相关路由"""

    def __init__(self, mcp: Any, config: FastMCPOpenAPIConfig, registry: dict[str, Any]):
        self.mcp = mcp
        self.config = config
        self.registry = registry

    def register_all(self) -> None:
        self._register_openapi()
        self._register_docs_ui()
        self._register_api_tools()
        if self.config.favicon_url is None:
            self._register_favicon()
        if self.config.verbose:
            print(
                f"[fastmcp_openapi] 已注册路由："
                f" {self.config.openapi_route}"
                f" {self.config.docs_ui_route}"
                f" {self.config.api_tools_route}"
            )

    # ------------------------------------------------------------------
    # 各路由
    # ------------------------------------------------------------------

    def _register_openapi(self) -> None:
        @self.mcp.custom_route(self.config.openapi_route, methods=["GET", "OPTIONS"])
        async def openapi_json(request: Request) -> Response:
            if request.method == "OPTIONS":
                return JSONResponse({}, headers=self._cors_headers())

            schema = self._build_openapi_schema()
            headers = self._cors_headers() if self.config.enable_cors else {}
            return JSONResponse(schema, headers=headers)

    def _register_docs_ui(self) -> None:
        @self.mcp.custom_route(self.config.docs_ui_route, methods=["GET"])
        async def docs_ui(_request: Request) -> Response:
            html = get_docs_html(self.config)
            return HTMLResponse(html)

    def _register_api_tools(self) -> None:
        @self.mcp.custom_route(self.config.api_tools_route, methods=["GET"])
        async def api_tools(_request: Request) -> Response:
            return JSONResponse(
                {
                    "server": getattr(self.mcp, "name", "MCP Server"),
                    "total": len(self.registry),
                    "tools": list(self.registry.values()),
                }
            )

    def _register_favicon(self) -> None:
        @self.mcp.custom_route("/favicon.svg", methods=["GET"])
        async def favicon(_request: Request) -> Response:
            return Response(
                content=get_favicon_svg(),
                media_type="image/svg+xml",
                headers={"Cache-Control": "public, max-age=86400"},
            )

    # ------------------------------------------------------------------
    # OpenAPI Schema 构建
    # ------------------------------------------------------------------

    def _build_openapi_schema(self) -> dict[str, Any]:
        """构建符合 OpenAPI 3.x 规范的 JSON Schema。"""
        tag_descriptions: dict[str, str] = {}
        for tool_info in self.registry.values():
            for tag in tool_info.get("tags") or []:
                tag_descriptions.setdefault(tag, f"{tag} 相关 Tool")
            tag_descriptions.update(tool_info.get("tag_descriptions") or {})

        openapi_info: dict[str, Any] = {
            "title": self.config.title,
            "version": self.config.version,
        }
        if self.config.description:
            openapi_info["description"] = self.config.description

        schema: dict[str, Any] = {
            "openapi": self.config.openapi_version,
            "info": openapi_info,
            "paths": self._build_paths(),
            "components": {
                "schemas": self._collect_component_schemas(),
                "securitySchemes": None,
            },
        }

        servers = []
        if self.config.base_url:
            servers.append({"url": self.config.base_url, "description": "MCP Server"})
        servers.extend(self.config.extra_servers)
        if servers:
            schema["servers"] = servers
        if tag_descriptions:
            schema["tags"] = [{"name": tag, "description": tag_descriptions[tag]} for tag in sorted(tag_descriptions)]
        return schema

    def _build_paths(self) -> dict[str, Any]:
        """为每个 tool 生成一条 GET 路径。"""
        paths: dict[str, Any] = {}

        for tool_name, info in self.registry.items():
            tags = info.get("tags") or ["Tools"]
            path = build_tool_route_path(self.config.api_base, tool_name)
            request_body = self._build_request_body(info)
            response_schema = self._build_response_schema(info)

            operation: dict[str, Any] = {
                "description": info.get("description", ""),
                "operationId": self._get_operation_id(tool_name, path, "get"),
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": response_schema,
                            }
                        },
                        "description": "OK",
                    },
                    "422": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "items": {"$ref": "#/components/schemas/ValidationErrorModel"},
                                    "type": "array",
                                }
                            }
                        },
                        "description": "Unprocessable Content",
                    },
                },
                "summary": info.get("summary") or info.get("title", tool_name),
                "tags": tags,
            }
            if request_body is not None:
                operation["requestBody"] = request_body
            paths[path] = {"get": operation}

        return paths

    def _build_request_body(self, info: dict[str, Any]) -> dict[str, Any] | None:
        """构建 requestBody。"""
        input_model_name = info.get("input_model_name")
        if input_model_name:
            return {
                "content": {
                    "application/json": {
                        "schema": {"$ref": f"#/components/schemas/{input_model_name}"},
                    }
                },
                "required": True,
            }

        properties: dict[str, Any] = {}
        required_fields: list[str] = []
        for param in info.get("parameters") or []:
            prop: dict[str, Any] = {"type": param.get("type", "string")}
            if param.get("description"):
                prop["description"] = param["description"]
            if "default" in param:
                prop["default"] = param["default"]
            if param.get("enum"):
                prop["enum"] = param["enum"]
            properties[param["name"]] = prop
            if param.get("required"):
                required_fields.append(param["name"])

        if not properties:
            return None
        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required_fields:
            schema["required"] = required_fields
        return {
            "content": {"application/json": {"schema": schema}},
            "required": bool(required_fields),
        }

    def _build_response_schema(self, info: dict[str, Any]) -> dict[str, Any]:
        """构建 200 response schema。"""
        response_model_name = info.get("response_model_name")
        if response_model_name:
            return {"$ref": f"#/components/schemas/{response_model_name}"}

        output_schema = info.get("output_schema") or {}
        if output_schema and output_schema.get("properties"):
            return output_schema
        return {"type": "object"}

    def _collect_component_schemas(self) -> dict[str, Any]:
        """收集 components/schemas。"""
        schemas: dict[str, Any] = {}
        for info in self.registry.values():
            schemas.update(info.get("component_schemas") or {})

        validation_schema = ValidationErrorModel.model_json_schema(ref_template=OPENAPI3_REF_TEMPLATE)
        validation_schema.pop("$defs", None)
        schemas["ValidationErrorModel"] = validation_schema
        return schemas

    def _get_operation_id(self, name: str, path: str, method: str) -> str:
        """生成与 flask_openapi 一致的 operationId。"""
        return re.sub(r"\W", "_", name + path) + "_" + method.lower()

    # ------------------------------------------------------------------
    # CORS
    # ------------------------------------------------------------------

    def _cors_headers(self) -> dict[str, str]:
        return {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "*",
        }
