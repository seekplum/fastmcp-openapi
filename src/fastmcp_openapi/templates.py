"""FastMCP OpenAPI HTML 模板（Swagger UI CDN 渲染）"""


def get_favicon_svg() -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<circle cx="50" cy="50" r="48" fill="#49cc90" stroke="#3ba876" stroke-width="2"/>'
        '<text x="50" y="50" text-anchor="middle" dominant-baseline="central"'
        ' font-family="Arial,sans-serif" font-size="60" font-weight="bold" fill="white">M</text>'
        "</svg>"
    )


def get_docs_html(config) -> str:
    """生成内嵌 Swagger UI CDN 的 HTML 页面。"""
    favicon_href = config.favicon_url or "/favicon.svg"
    openapi_url = config.openapi_route

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{config.title}</title>
  <link rel="icon" type="image/svg+xml" href="{favicon_href}" />
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css" />
  <style>
    body {{ margin: 0; }}
    .topbar {{ display: none; }}
  </style>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    window.onload = function () {{
      SwaggerUIBundle({{
        url: "{openapi_url}",
        dom_id: "#swagger-ui",
        presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
        layout: "BaseLayout",
        deepLinking: true,
        displayRequestDuration: true,
        defaultModelsExpandDepth: 2,
        defaultModelExpandDepth: 2,
      }});
    }};
  </script>
</body>
</html>"""
