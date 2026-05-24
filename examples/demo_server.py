import asyncio
import typing as t
import uuid
from typing import TypedDict

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


class DemoItem(TypedDict):
    num_id: str
    title: str
    price: float


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
async def list_items(ctx: Context, title: str = "") -> BaseResponse[list[DemoItem]]:
    """获取活动中的商品列表。

    指定活动，获取活动中的商品列表。
    """
    items: list[DemoItem] = [
        {
            "num_id": "123456",
            "title": "测试商品1",
            "price": 9.9,
        },
        {
            "num_id": "234567",
            "title": "正式商品2",
            "price": 19.9,
        },
    ]
    items = [item for item in items if not title or title in item["title"]]
    return BaseResponse[list[DemoItem]](code=0, message="success", data=items)


@openapi.tool()
async def item_add(ctx: Context, param: ItemAddInput) -> BaseResponse[ItemAddData]:
    """往活动中添加商品。

    指定活动，添加需要的商品，可自定义文字和标签。
    """
    return BaseResponse[ItemAddData](
        code=0,
        message="success",
        data=ItemAddData.model_validate(
            {
                "data": {
                    "act": {
                        "id": param.aid,
                        "name": "test",
                        "count": {
                            "dealing": 1,
                            "rendered": 2,
                        },
                        "thumbnail": "http://example.com/thumbnail.jpg",
                    }
                }
            }
        ),
    )


if __name__ == "__main__":
    asyncio.run(openapi.setup())
    openapi.mcp.run(transport="http", host="0.0.0.0", port=8333, path="/mcp", stateless_http=True)
