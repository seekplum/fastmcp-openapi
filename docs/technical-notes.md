# 技术说明

## OpenAPI 生成策略

项目的 OpenAPI 生成不是在运行时扫描所有业务代码，而是在 `@openapi.tool()` 注册阶段同步收集元数据，再由 [routes.py](../src/fastmcp_openapi/routes.py) 输出最终 schema。

这样做有两个好处：

- 避免在文档生成阶段重复反射业务函数。
- 保证 `/api/tools` 与 `/openapi.json` 使用同一份数据源。

## tool 元数据提取来源

tool 信息主要来自以下几部分：

1. 函数签名
2. Pydantic 模型字段定义
3. docstring 首行与正文
4. FastMCP `Tool` 对象上的描述、标签和输出信息

对应实现集中在 [extractor.py](../src/fastmcp_openapi/extractor.py)。

## `BaseModel` 入参的处理方式

当业务 tool 使用如下签名时：

```python
async def item_add(ctx: Context, param: ItemAddInput) -> BaseResponse[ItemAddData]:
    ...
```

内部会经历两个阶段：

### 注册阶段

- 从类型注解识别 `param` 是 `BaseModel` 子类。
- 把 `aid`、`itemIds` 等字段展开到扁平签名。
- 让 FastMCP 能像处理普通参数一样处理这些字段。

### 调用阶段

- 从 query 参数或 JSON body 取出字段值。
- 重新构造 `ItemAddInput.model_validate(...)`。
- 再把模型实例传给原始业务函数。

这部分逻辑位于 [__init__.py:54-101](../src/fastmcp_openapi/__init__.py#L54-L101) 和 [__init__.py:147-209](../src/fastmcp_openapi/__init__.py#L147-L209)。

## 响应 schema 的来源

响应 schema 有两种主要来源：

1. 若能识别到响应模型，则优先以组件引用方式输出。
2. 若没有明确模型，则退化为返回注解推导出的 schema 或通用 object。

这意味着：

- 使用明确的 Pydantic 响应模型时，文档展示最完整。
- 仅返回 `dict` 或宽泛类型时，生成的响应 schema 会更保守。

## 代理路由行为

每个 tool 会对应一个精确路径：

```text
{api_base}/{tool_name}
```

默认即：

```text
/call/list_items
/call/item_add
```

对应行为：

- `GET`：读取 query 参数
- `POST`：读取 JSON body
- 未注册的 tool 不会生成路径
- 非法 tool 名称会在构建路径时抛出异常

路径构建逻辑位于 [routes.py:16-21](../src/fastmcp_openapi/routes.py#L16-L21)。

## 错误处理

tool 代理错误分为两类：

### 校验错误

当 Pydantic 校验失败时：

- 默认返回 `422`
- 返回体为 `ValidationError.errors(include_url=False)`
- 也可以通过 `tool_proxy_validation_error_handler` 自定义响应

### 执行错误

当 tool 执行过程中抛出异常时：

- 默认返回 `500`
- 返回 `{"code": 500, "message": "tool 调用失败"}`
- 也可以通过 `tool_proxy_exception_handler` 自定义响应

## Hook 执行语义

`before_request` / `after_request` 的设计目标是让业务侧在不改 FastMCP 内部实现的情况下，统一注入横切逻辑。

支持场景包括：

- trace id 透传
- 用户身份注入
- 响应头补充
- 统一审计日志
- 自定义错误包装

注意点：

- `before_request` 返回值不会改变主流程。
- `after_request` 只有返回 `Response` 时才会替换当前响应。
- 同步和异步 handler 都支持。

## 示例与文档的对应关系

[examples/demo_server.py](../examples/demo_server.py) 可以作为文档里的端到端样例：

- [README.md](../README.md) 给出快速接入版。
- [example-from-test.md](./example-from-test.md) 解释每个示例片段的用途。
- [architecture.md](./architecture.md) 说明内部模块如何支撑该示例。

如果后续新增能力，建议优先同步更新 `examples/demo_server.py` 示例，再回填 README 和 docs，以保持示例与实现一致。
