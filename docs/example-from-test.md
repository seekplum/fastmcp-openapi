# 基于 `examples/demo_server.py` 的示例说明

本文档基于 [examples/demo_server.py](../examples/demo_server.py) 说明如何把 `fastmcp-openapi` 接入一个 FastMCP 服务。

## 示例覆盖的能力

这个示例同时覆盖了四类能力：

1. 初始化 `FastMCPOpenAPI`
2. 定义 Pydantic 输入输出模型
3. 注册 `before_request` / `after_request` 钩子
4. 注册可被 OpenAPI 暴露的 tool

## 初始化

```python
openapi = FastMCPOpenAPI(
    FastMCP("demo"),
    title="Demo MCP Tools API",
    description="Demo FastMCP OpenAPI docs",
    base_url="http://127.0.0.1:8333",
)
```

这里完成了三件事：

- 创建底层 `FastMCP("demo")` 服务。
- 指定 OpenAPI 文档标题和描述。
- 指定文档中的 `servers.url`，让 Swagger UI 调用地址与本地服务一致。

## 响应模型

示例中定义了通用响应模型：

```python
class BaseResponse(BaseModel, t.Generic[DataT]):
    message: str = Field(..., description="接口错误信息")
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="请求ID")
    code: int = Field(..., description="接口错误码")
    data: DataT | None = Field(default=None, description="接口返回数据")
```

这个模式适合把业务返回统一包装成固定信封结构，Swagger UI 中也能看到字段说明。

## 请求钩子

### before_request

```python
@openapi.before_request
def load_request_id(request: Request) -> None:
    request.state.request_id = request.headers.get("X-Request-Id", "")
```

作用：

- 从请求头读取 `X-Request-Id`
- 写入 `request.state`
- 供后续 tool 或响应处理复用

### after_request

```python
@openapi.after_request
def add_request_id_header(request: Request, response: Response) -> Response:
    response.headers["X-Request-Id"] = request.state.request_id
    return response
```

作用：

- 把 `request_id` 回写到响应头
- 保证代理接口与文档接口返回一致的链路追踪信息

## tool 示例一：query 参数

```python
@openapi.tool()
async def list_items(ctx: Context, title: str = "") -> BaseResponse[list[dict]]:
    ...
```

这个 tool 的特点：

- 入参是基础类型 `title: str = ""`
- 代理 GET 请求时，对应 `/call/list_items?title=测试`
- OpenAPI requestBody 会根据参数自动生成 object schema

可直接调用：

```bash
curl "http://127.0.0.1:8333/call/list_items?title=测试"
```

## tool 示例二：Pydantic Body

```python
class ItemAddInput(BaseModel):
    aid: str = Field(..., description="活动ID")
    itemIds: list[str] = Field(..., description="待添加商品的 num_id 列表")


@openapi.tool()
async def item_add(ctx: Context, param: ItemAddInput) -> BaseResponse[ItemAddData]:
    ...
```

这个 tool 的特点：

- 业务函数接收 `param: ItemAddInput`
- 注册阶段会把模型字段展开给 FastMCP 使用
- 文档阶段会把 `ItemAddInput` 放入 `components/schemas`
- HTTP POST 代理调用时会把 JSON body 重新组装成 `ItemAddInput`

可直接调用：

```bash
curl -X POST "http://127.0.0.1:8333/call/item_add" \
  -H "Content-Type: application/json" \
  -H "X-Request-Id: demo-request-id" \
  -d '{
    "aid": "act-001",
    "itemIds": ["123456", "234567"]
  }'
```

## 启动方式

```python
if __name__ == "__main__":
    asyncio.run(openapi.setup())
    openapi.mcp.run(transport="http", host="0.0.0.0", port=8333, path="/mcp", stateless_http=True)
```

建议启动后优先检查以下地址：

- `http://127.0.0.1:8333/docs`
- `http://127.0.0.1:8333/openapi.json`
- `http://127.0.0.1:8333/api/tools`

## 适合作为模板的部分

如果你要在业务项目中复用这个示例，通常最值得直接沿用的是：

- `BaseResponse[T]` 这种统一响应模型
- `before_request` / `after_request` 的 trace 透传方式
- 一个 query tool + 一个 body tool 的最小组合

如果你只想要最简接入，可以先保留 `FastMCPOpenAPI` 初始化和 `@openapi.tool()`，后续再逐步增加 hook 和统一响应模型。
