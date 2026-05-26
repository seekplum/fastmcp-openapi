"""fastmcp_openapi — 为 FastMCP server 提供完整的 OpenAPI 文档服务"""

import functools
import inspect
import json
import logging
import typing as t

from fastmcp import Context, FastMCP
from fastmcp.tools.base import Tool
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticUndefined
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from .config import FastMCPOpenAPIConfig
from .extractor import build_tool_registry_entry
from .routes import RouteRegistrar, build_tool_route_path

__version__ = "1.0.0"
__all__ = [
    "AfterRequestHandler",
    "BeforeRequestHandler",
    "FastMCPOpenAPI",
    "FastMCPOpenAPIConfig",
]

T = t.TypeVar("T")
BeforeRequestHandler = t.Callable[[Request], t.Awaitable[None] | None]
AfterRequestHandler = t.Callable[[Request, Response], t.Awaitable[Response | None] | Response | None]

_STATUS_ROUTE_MARKER = "_fastmcp_openapi_status_route_registered"
_TOOL_PROXY_ROUTES_MARKER = "_fastmcp_openapi_tool_proxy_routes_registered"
logger = logging.getLogger(__name__)


@t.overload
async def _maybe_await(value: t.Awaitable[T]) -> T:
    pass


@t.overload
async def _maybe_await(value: T) -> T:
    pass


async def _maybe_await(value: t.Awaitable[T] | T) -> T:
    if inspect.isawaitable(value):
        return await t.cast(t.Awaitable[T], value)
    return value


def _build_flattened_signature(  # pylint: disable=too-many-locals
    func: t.Callable[..., t.Any], body_params: dict[str, type[BaseModel]]
) -> tuple[inspect.Signature, dict[str, list[str]]]:

    original_sig = inspect.signature(func)
    field_map: dict[str, list[str]] = {}
    new_params: list[inspect.Parameter] = []
    has_keyword_only_field = False

    for param_name, param in original_sig.parameters.items():
        if param_name not in body_params:
            if has_keyword_only_field and param.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD:
                param = param.replace(kind=inspect.Parameter.KEYWORD_ONLY)
            new_params.append(param)
            continue

        model_cls = body_params[param_name]
        field_names: list[str] = []

        try:
            field_hints = t.get_type_hints(model_cls)
        except Exception:
            field_hints = {}

        for field_name, field_info in model_cls.model_fields.items():
            field_names.append(field_name)
            base_type = field_hints.get(field_name, t.Any)
            annotation = t.Annotated[base_type, field_info]  # type: ignore
            default = field_info.default if field_info.default is not PydanticUndefined else inspect.Parameter.empty
            kind = inspect.Parameter.POSITIONAL_OR_KEYWORD
            if field_info.default_factory is not None:
                default = inspect.Parameter.empty
                kind = inspect.Parameter.KEYWORD_ONLY  # type: ignore
                has_keyword_only_field = True
            elif has_keyword_only_field:
                kind = inspect.Parameter.KEYWORD_ONLY  # type: ignore
            new_params.append(
                inspect.Parameter(
                    name=field_name,
                    kind=kind,
                    default=default,
                    annotation=annotation,
                )
            )

        field_map[param_name] = field_names

    return original_sig.replace(parameters=new_params), field_map


def _resolve_tool_name(
    func: t.Callable[..., t.Any], decorator_args: tuple[t.Any, ...], decorator_kwargs: dict[str, t.Any]
) -> str:
    explicit_name = decorator_kwargs.get("name")
    if isinstance(explicit_name, str) and explicit_name:
        return explicit_name
    if decorator_args and isinstance(decorator_args[0], str):
        return decorator_args[0]
    return func.__name__


def _build_registered_tool(
    wrapper: t.Callable[..., t.Any],
    tool_name: str,
    decorator_kwargs: dict[str, t.Any],
) -> Tool:
    annotations = decorator_kwargs.get("annotations")
    if isinstance(annotations, dict):
        annotations = ToolAnnotations(**annotations)

    return Tool.from_function(
        wrapper,
        name=tool_name,
        version=decorator_kwargs.get("version"),
        title=decorator_kwargs.get("title"),
        description=decorator_kwargs.get("description"),
        icons=decorator_kwargs.get("icons"),
        tags=decorator_kwargs.get("tags"),
        output_schema=decorator_kwargs.get("output_schema", ...),
        annotations=annotations,
        meta=decorator_kwargs.get("meta"),
        task=decorator_kwargs.get("task"),
        timeout=decorator_kwargs.get("timeout"),
        auth=decorator_kwargs.get("auth"),
        run_in_thread=decorator_kwargs.get("run_in_thread"),
    )


def _mcp_tool(  # noqa: C901
    mcp: FastMCP,
    registry: dict[str, t.Any],
    *a: t.Any,
    on_registered: t.Callable[[str], None] | None = None,
    **kw: t.Any,
) -> t.Callable[..., t.Any]:
    def decorator(func: t.Callable[..., t.Awaitable[T]]) -> t.Any:
        body_params: dict[str, type[BaseModel]] = {}
        try:
            hints = t.get_type_hints(func)
            for pname, hint in hints.items():
                if pname in ("ctx", "self", "cls", "return"):
                    continue
                if inspect.isclass(hint) and issubclass(hint, BaseModel):
                    body_params[pname] = hint
        except Exception:
            pass

        field_map: dict[str, list[str]] = {}
        flat_sig = None
        if body_params:
            try:
                flat_sig, field_map = _build_flattened_signature(func, body_params)
            except Exception:
                pass

        @functools.wraps(func)
        async def wrapper(ctx: Context, *args: t.Any, **kwargs: t.Any) -> T:
            for model_param, field_names in field_map.items():
                model_cls = body_params[model_param]
                model_kwargs = {
                    field_name: kwargs.pop(field_name) for field_name in field_names if field_name in kwargs
                }
                kwargs[model_param] = model_cls.model_validate(obj=model_kwargs)
            for pname, model_cls in body_params.items():
                if pname in kwargs and isinstance(kwargs[pname], dict):
                    kwargs[pname] = model_cls.model_validate(obj=kwargs[pname])
            return await func(ctx, *args, **kwargs)

        if flat_sig is not None:
            wrapper.__signature__ = flat_sig  # type: ignore[attr-defined]
            new_annotations: dict[str, t.Any] = {
                parameter.name: parameter.annotation
                for parameter in flat_sig.parameters.values()
                if parameter.annotation is not inspect.Parameter.empty
            }
            if flat_sig.return_annotation is not inspect.Signature.empty:
                new_annotations["return"] = flat_sig.return_annotation
            wrapper.__annotations__ = new_annotations
            try:
                del wrapper.__wrapped__  # type: ignore[attr-defined]
            except AttributeError:
                pass

        decorated = mcp.tool(*a, **kw)(wrapper)
        tool_name = _resolve_tool_name(func, a, kw)
        logger.info("Registering tool: %s", tool_name)
        registered_tool = _build_registered_tool(wrapper, tool_name, kw)
        registry[tool_name] = build_tool_registry_entry(tool_name, registered_tool, func, wrapper, body_params)
        if on_registered is not None:
            on_registered(tool_name)
        return decorated

    return decorator


def _mcp_custom_route(
    mcp: FastMCP,
    path: str,
    methods: list[str],
    name: str | None = None,
    include_in_schema: bool = True,
    *,
    before_request_handlers: list[BeforeRequestHandler] | None = None,
    after_request_handlers: list[AfterRequestHandler] | None = None,
) -> t.Callable[..., t.Callable[[Request], t.Awaitable[Response]]]:
    def decorator(func: t.Callable[[Request], t.Awaitable[Response]]) -> t.Callable[[Request], t.Awaitable[Response]]:
        @functools.wraps(func)
        async def wrapper(request: Request) -> Response:
            for before_handler in before_request_handlers or []:
                await _maybe_await(before_handler(request))

            response = await func(request)
            for after_handler in after_request_handlers or []:
                next_result = after_handler(request, response)
                next_response: Response | None
                if inspect.isawaitable(next_result):
                    next_response = await next_result
                else:
                    next_response = next_result
                if next_response is not None:
                    response = next_response
            return response

        return mcp.custom_route(path, methods=methods, name=name, include_in_schema=include_in_schema)(wrapper)

    return decorator


class FastMCPOpenAPI:
    """FastMCP OpenAPI 文档挂载器"""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        mcp: FastMCP,
        *,
        title: str = "MCP Tools API",
        version: str = "1.0.0",
        description: str = "",
        base_url: str | None = None,
        config: FastMCPOpenAPIConfig | None = None,
        verbose: bool = False,
    ) -> None:
        self.mcp = mcp
        self.config = config or FastMCPOpenAPIConfig(
            title=title,
            version=version,
            description=description,
            base_url=base_url or "",
            verbose=verbose,
        )
        self._registry: dict[str, t.Any] = {}
        self._before_request_handlers: list[BeforeRequestHandler] = []
        self._after_request_handlers: list[AfterRequestHandler] = []

    def before_request(self, func: BeforeRequestHandler) -> BeforeRequestHandler:
        self._before_request_handlers.append(func)
        return func

    def after_request(self, func: AfterRequestHandler) -> AfterRequestHandler:
        self._after_request_handlers.append(func)
        return func

    def tool(self, *args: t.Any, **kwargs: t.Any) -> t.Callable[..., t.Any]:
        self._ensure_status_route_registered()
        return _mcp_tool(
            self.mcp,
            self._registry,
            *args,
            on_registered=self._ensure_tool_proxy_route_registered,
            **kwargs,
        )

    def custom_route(
        self,
        path: str,
        methods: list[str],
        name: str | None = None,
        include_in_schema: bool = True,
    ) -> t.Callable[..., t.Callable[[Request], t.Awaitable[Response]]]:
        return _mcp_custom_route(
            self.mcp,
            path,
            methods,
            name=name,
            include_in_schema=include_in_schema,
            before_request_handlers=self._before_request_handlers,
            after_request_handlers=self._after_request_handlers,
        )

    def _ensure_status_route_registered(self) -> None:
        if not self.config.enable_status_route:
            return
        if getattr(self.mcp, _STATUS_ROUTE_MARKER, False):
            return
        setattr(self.mcp, _STATUS_ROUTE_MARKER, True)

        @self.custom_route(self.config.status_route, methods=["GET", "POST"])
        async def status(_request: Request) -> PlainTextResponse:
            return PlainTextResponse("OK")

    def _get_registered_tool_proxy_routes(self) -> set[str]:
        routes = getattr(self.mcp, _TOOL_PROXY_ROUTES_MARKER, None)
        if routes is None:
            routes = set()
            setattr(self.mcp, _TOOL_PROXY_ROUTES_MARKER, routes)
        return routes

    async def _call_tool_proxy(self, request: Request, tool_name: str) -> Response:
        if request.method == "POST":
            args = await request.json()
        else:
            args = dict(request.query_params)
        try:
            result = await self.mcp.call_tool(tool_name, args)
            contents = [content.text for content in result.content if content.type == "text"]
            data = json.loads("".join(contents))
            return JSONResponse(data)
        except ValidationError as exc:
            logger.warning("FastMCP tool proxy validation failed: %s", tool_name, exc_info=exc)
            if self.config.tool_proxy_validation_error_handler is not None:
                response = await _maybe_await(self.config.tool_proxy_validation_error_handler(request, tool_name, exc))
                return t.cast(Response, response)
            return JSONResponse(exc.errors(include_url=False), status_code=422)
        except Exception as exc:
            logger.exception("FastMCP tool proxy failed: %s", tool_name)
            if self.config.tool_proxy_exception_handler is not None:
                response = await _maybe_await(self.config.tool_proxy_exception_handler(request, tool_name, exc))
                return t.cast(Response, response)
            return JSONResponse({"code": 500, "message": "tool 调用失败"}, status_code=500)

    def _ensure_tool_proxy_route_registered(self, tool_name: str) -> None:
        path = build_tool_route_path(self.config.api_base, tool_name)
        registered_routes = self._get_registered_tool_proxy_routes()
        if path in registered_routes:
            logger.warning("Tool proxy route already registered for path: %s", path)
            return
        registered_routes.add(path)
        logger.info("Registering tool proxy route: %s -> %s", path, tool_name)

        @self.custom_route(path, methods=["GET", "POST"])
        async def call_tool_proxy(request: Request) -> Response:
            return await self._call_tool_proxy(request, tool_name)

    async def setup(self) -> None:
        if self.config.verbose:
            logger.info("[fastmcp_openapi] 开始注册 OpenAPI 路由…")

        registrar = RouteRegistrar(self.mcp, self.config, self._registry)
        registrar.register_all()

        if self.config.verbose:
            logger.info(
                f"[fastmcp_openapi] 完成，共 {len(self._registry)} 个 tool。"
                f" 文档：{self.config.base_url}{self.config.docs_ui_route}"
            )
