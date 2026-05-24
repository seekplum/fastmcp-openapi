# 项目架构

## 项目目标

`fastmcp-openapi` 的目标是在 FastMCP 原有 tool 注册能力之上，补齐面向 HTTP 使用场景的 OpenAPI 文档、Swagger UI 页面、tool 代理路由和请求生命周期扩展点。

## 模块划分

### `src/fastmcp_openapi/__init__.py`

核心编排模块，同时也是主要导出入口，负责：

- 封装 `FastMCPOpenAPI` 主类。
- 定义 `BeforeRequestHandler`、`AfterRequestHandler` 类型。
- 包装 `mcp.tool()`，在注册阶段提取工具元数据并写入内部 registry。
- 识别 `BaseModel` 入参并展开为扁平签名，兼容 FastMCP 的 tool 注册方式。
- 在 HTTP 代理调用时把扁平参数重新组装为 Pydantic 模型。
- 管理 `before_request` / `after_request` 钩子。
- 为每个 tool 注册精确代理路由。

使用方通常只需要从这个入口导入。

### `src/fastmcp_openapi/decorators.py`

兼容导出层，负责：

- 重新导出 `_mcp_tool`、`_mcp_custom_route`。
- 为旧的导入路径保留兼容入口。

### `src/fastmcp_openapi/extractor.py`

元数据提取模块，负责：

- 从函数签名、Pydantic schema、docstring 中提取参数描述。
- 归集输入模型、响应模型和 components/schemas。
- 生成内部 registry 条目，供 `/api/tools` 和 `/openapi.json` 复用。

### `src/fastmcp_openapi/routes.py`

文档路由注册模块，负责：

- 注册 `/openapi.json`、`/docs`、`/api/tools`、`/favicon.svg`。
- 生成 OpenAPI `paths`、`components`、`servers`、`tags`。
- 为校验失败响应统一生成 `ValidationErrorModel` schema。

### `src/fastmcp_openapi/config.py`

配置模型模块，负责定义：

- 文档标题、版本、描述、基础地址。
- 文档路由和 tool 代理路由前缀。
- 状态路由开关。
- CORS 开关。
- tool 代理异常处理回调。

### `src/fastmcp_openapi/templates.py`

负责生成 Swagger UI HTML 和默认 favicon 内容。

## 运行时结构

```text
业务代码
  │
  ├─ @openapi.before_request
  ├─ @openapi.tool()
  ├─ @openapi.after_request
  │
  ▼
FastMCPOpenAPI
  ├─ registry（tool 元数据）
  ├─ hook handlers
  ├─ tool proxy route registration
  └─ RouteRegistrar
       ├─ /openapi.json
       ├─ /docs
       ├─ /api/tools
       └─ /favicon.svg
```

## 请求链路

### 1. tool 注册阶段

当业务函数通过 `@openapi.tool()` 注册时：

1. 检查是否需要注册 `/status`。
2. 识别函数签名中的 `BaseModel` 入参。
3. 生成扁平签名，供 FastMCP 侧识别字段级参数。
4. 构造 `Tool` 对象并提取 registry 信息。
5. 为当前 tool 自动注册 `/call/{tool_name}` 代理路由。

### 2. 文档初始化阶段

当调用 `await openapi.setup()` 时：

1. 使用 `RouteRegistrar` 注册文档相关路由。
2. 基于 registry 生成 OpenAPI schema。
3. 提供 Swagger UI 页面和内部工具清单接口。

### 3. HTTP 代理调用阶段

当请求 `/call/{tool_name}` 时：

1. 先执行 `before_request` handler。
2. 根据请求方法解析 query 参数或 JSON body。
3. 调用 `mcp.call_tool(tool_name, args)`。
4. 若入参校验失败，返回 `422` 或自定义校验异常响应。
5. 若执行异常，返回 `500` 或自定义异常响应。
6. 执行 `after_request` handler，并允许替换响应对象。

## 设计特点

### 精确路由注册

项目不会暴露通配型 `/call/{tool_name:any}` 路由，而是在 tool 注册时为每个 tool 单独注册精确路径。这样可以让 OpenAPI `paths` 与实际可访问接口保持一致，也能避免未注册 tool 被误调用。

### registry 作为单一事实源

tool 的参数、说明、响应 schema 在注册阶段统一进入 registry。后续 `/api/tools` 与 `/openapi.json` 都从 registry 读取，避免文档信息重复推导。

### 基于 Pydantic 的 schema 复用

输入模型与响应模型会被转换为 OpenAPI components/schemas，从而支持：

- 嵌套模型展示
- 字段描述透传
- 默认值透传
- 复用统一的 schema 引用

## 基于 `examples/demo_server.py` 的典型接入方式

[examples/demo_server.py](../examples/demo_server.py) 展示了完整接入方式：

- 使用 `BaseResponse[T]` 统一响应结构。
- 使用 `before_request` 注入 `request_id`。
- 使用 `after_request` 回写 `X-Request-Id` 响应头。
- 同时演示了简单查询参数 tool 和 `BaseModel` body tool。

建议把它作为集成当前库的最小参考示例。
