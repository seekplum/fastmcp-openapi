"""FastMCP OpenAPI 配置"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import Response

ToolProxyValidationErrorHandler = Callable[[Request, str, ValidationError], Response | Awaitable[Response]]
ToolProxyExceptionHandler = Callable[[Request, str, Exception], Response | Awaitable[Response]]


@dataclass
class FastMCPOpenAPIConfig:  # pylint: disable=too-many-instance-attributes
    """FastMCP OpenAPI 文档服务配置"""

    # 文档基础信息
    title: str = "MCP Tools API"
    version: str = "1.0.0"
    description: str = ""
    base_url: str = ""

    # 路由路径
    openapi_route: str = "/openapi.json"
    docs_ui_route: str = "/docs"
    api_tools_route: str = "/api/tools"
    api_base: str = "/call"
    enable_status_route: bool = True
    status_route: str = "/status"

    # OpenAPI 版本
    openapi_version: str = "3.1.0"

    # CORS
    enable_cors: bool = True

    # 调试输出
    verbose: bool = False

    # 自定义 favicon（None 则使用内置 SVG）
    favicon_url: str | None = None

    # 额外的 OpenAPI servers 配置
    extra_servers: list = field(default_factory=list)

    # tool 代理异常处理
    tool_proxy_validation_error_handler: ToolProxyValidationErrorHandler | None = None
    tool_proxy_exception_handler: ToolProxyExceptionHandler | None = None
