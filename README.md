# fastmcp-openapi

`fastmcp-openapi` 为 FastMCP 服务提供 OpenAPI 文档生成、Swagger UI 页面、HTTP tool 代理路由，以及面向业务扩展的请求钩子封装。

[![LICENSE](https://img.shields.io/github/license/seekplum/fastmcp-openapi.svg)](https://github.com/seekplum/fastmcp-openapi/blob/master/LICENSE)[![coveralls](https://coveralls.io/repos/github/seekplum/fastmcp-openapi/badge.svg?branch=master)](https://coveralls.io/github/seekplum/fastmcp-openapi?branch=master) [![pypi version](https://img.shields.io/pypi/v/fastmcp-openapi.svg)](https://pypi.python.org/pypi/fastmcp-openapi) [![pyversions](https://img.shields.io/pypi/pyversions/fastmcp-openapi.svg)](https://pypi.python.org/pypi/fastmcp-openapi)

## 核心能力

- 在注册 `tool` 时提取参数、响应和描述信息，生成 OpenAPI 3.1 schema。
- 自动注册 `/openapi.json`、`/docs`、`/api/tools` 等文档路由。
- 通过 `FastMCPOpenAPI.tool()` 注册 MCP tool，并自动为每个 tool 注册对应 HTTP 代理路由。
- 支持 `before_request` / `after_request` 钩子，统一处理请求上下文与响应头。
- 支持把 Pydantic `BaseModel` 参数展开为 MCP tool 入参，并在代理调用时自动回填模型。
- 支持自定义 tool 代理异常处理与校验异常处理。

## 安装

```bash
uv add fastmcp-openapi
```

本仓库本地开发可直接同步依赖：

```bash
uv sync
```

## 快速开始

下面的示例来自仓库根目录的 `examples/demo_server.py`，展示了完整的工具注册、请求钩子、响应模型和本地启动方式。

```python
import asyncio
import typing as t
import uuid

from fastmcp import Context, FastMCP
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import Response

from fastmcp_openapi import FastMCPOpenAPI

DataT = t.TypeVar("DataT")

openapi = FastMCPOpenAPI(
    FastMCP("demo"),
    title="Demo MCP Tools API",
    description="Demo FastMCP OpenAPI docs",
    base_url="http://127.0.0.1:8333",
)


class ItemAddInput(BaseModel):
    aid: str = Field(..., description="活动ID")
    itemIds: list[str] = Field(..., description="待添加商品的 num_id 列表")


class ActCount(BaseModel):
    dealing: int = Field(..., description="待处理的商品数")
    rendered: int = Field(..., description="渲染中的商品数")
    applied: int = Field(default=0, description="已应用的商品数")
    fail: int = Field(default=0, description="应用失败的商品数")
    removed: int = Field(default=0, description="已移除的商品数")


class ActInfo(BaseModel):
    id: str = Field(..., description="活动ID")
    name: str = Field(..., description="活动显示名称")
    count: ActCount = Field(..., description="活动占用的商品数")
    thumbnail: str = Field(default="", description="活动缩略图")


class ItemActData(BaseModel):
    act: ActInfo = Field(..., description="活动信息")


class ItemAddData(BaseModel):
    data: ItemActData = Field(..., description="添加商品后的活动信息")


class BaseResponse(BaseModel, t.Generic[DataT]):
    message: str = Field(..., description="接口错误信息")
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="请求ID")
    code: int = Field(..., description="接口错误码")
    data: DataT | None = Field(default=None, description="接口返回数据")


@openapi.before_request
def load_request_id(request: Request) -> None:
    request.state.request_id = request.headers.get("X-Request-Id", "")


@openapi.after_request
def add_request_id_header(request: Request, response: Response) -> Response:
    response.headers["X-Request-Id"] = request.state.request_id
    return response


@openapi.tool()
async def list_items(ctx: Context, title: str = "") -> BaseResponse[list[dict]]:
    items = [
        {"num_id": "123456", "title": "测试商品1", "price": 9.9},
        {"num_id": "234567", "title": "正式商品2", "price": 19.9},
    ]
    items = [item for item in items if not title or title in item["title"]]
    return BaseResponse[list[dict]](code=0, message="success", data=items)


@openapi.tool()
async def item_add(ctx: Context, param: ItemAddInput) -> BaseResponse[ItemAddData]:
    return BaseResponse[ItemAddData](
        code=0,
        message="success",
        data=ItemAddData.model_validate(
            {
                "data": {
                    "act": {
                        "id": param.aid,
                        "name": "test",
                        "count": {"dealing": 1, "rendered": 2},
                        "thumbnail": "http://example.com/thumbnail.jpg",
                    }
                }
            }
        ),
    )


if __name__ == "__main__":
    asyncio.run(openapi.setup())
    openapi.mcp.run(transport="http", host="0.0.0.0", port=8333, path="/mcp", stateless_http=True)
```

启动后可访问：

- `http://127.0.0.1:8333/docs`
- `http://127.0.0.1:8333/openapi.json`
- `http://127.0.0.1:8333/api/tools`
- `http://127.0.0.1:8333/call/list_items`
- `http://127.0.0.1:8333/call/item_add`

## 自动注册的路由

调用 `await openapi.setup()` 后会注册文档路由；每次使用 `@openapi.tool()` 注册 tool 时，也会注册对应代理路由。

### 文档路由

- `GET /openapi.json`：返回 OpenAPI schema。
- `GET /docs`：返回 Swagger UI 页面。
- `GET /api/tools`：返回内部 registry 中提取出的 tool 信息。
- `GET /favicon.svg`：默认文档图标。

### tool 代理路由

- `GET /call/{tool_name}`：从 query 参数读取入参。
- `POST /call/{tool_name}`：从 JSON body 读取入参。
- `GET|POST /status`：默认健康检查路由，返回 `OK`。

说明：

- 只会为已注册的 tool 生成精确路径，不会开放任意 tool 名称的通配调用。
- `BaseModel` 类型入参会在注册阶段展开为字段级 schema，在运行时再组装回模型实例。
- tool 调用抛出 `ValidationError` 时默认返回 `422`；其他异常默认返回 `500`。

## 常见配置

```python
from fastmcp_openapi import FastMCPOpenAPI, FastMCPOpenAPIConfig

config = FastMCPOpenAPIConfig(
    title="Demo MCP Tools API",
    version="1.0.0",
    description="Demo FastMCP OpenAPI docs",
    base_url="http://127.0.0.1:8333",
    openapi_route="/openapi.json",
    docs_ui_route="/docs",
    api_tools_route="/api/tools",
    api_base="/call",
    status_route="/healthz",
    enable_status_route=True,
    enable_cors=True,
)

openapi = FastMCPOpenAPI(FastMCP("demo"), config=config)
```

如果需要关闭状态路由：

```python
config = FastMCPOpenAPIConfig(enable_status_route=False)
```

## Hook 机制

`before_request` 和 `after_request` 都支持同步函数与异步函数，并按注册顺序执行。

- `before_request` 返回值会被忽略。
- `after_request` 返回 `Response` 时会替换当前响应。
- `after_request` 返回 `None` 时沿用当前响应。

## 文档导航

- [docs/architecture.md](docs/architecture.md)：项目架构、模块职责、请求链路。
- [docs/example-from-test.md](docs/example-from-test.md)：基于 `examples/demo_server.py` 的完整示例与调用说明。
- [docs/technical-notes.md](docs/technical-notes.md)：OpenAPI 生成策略、代理路由行为与扩展点。
- [docs/publish-package.md](docs/publish-package.md)：基于当前仓库配置的手动构建与发包流程。

## 源码布局

```text
src/fastmcp_openapi/
├── __init__.py
├── config.py
├── decorators.py
├── extractor.py
├── routes.py
└── templates.py
```

## 本地开发

```bash
uv sync
uv run poe lint
uv run poe test
```
