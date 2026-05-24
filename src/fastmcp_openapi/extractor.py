"""FastMCP Tool 信息构建工具。"""

import ast
import inspect
import re
import textwrap
from collections.abc import Callable
from typing import Any, Literal, get_type_hints

from pydantic import BaseModel

OPENAPI3_REF_TEMPLATE = "#/components/schemas/{model}"

_SKIP_PARAMS = frozenset({"ctx", "self", "cls"})
JsonSchemaMode = Literal["validation", "serialization"]


def _resolve_type_str(prop: object) -> str:
    """从单个 JSON Schema property dict 提取可读类型字符串。"""
    if not isinstance(prop, dict):
        return "any"
    if "type" in prop:
        return prop["type"]
    for key in ("anyOf", "oneOf"):
        if key in prop:
            non_null = [
                _resolve_type_str(sub) for sub in prop[key] if isinstance(sub, dict) and sub.get("type") != "null"
            ]
            return non_null[0] if non_null else "any"
    if "$ref" in prop:
        return prop["$ref"].split("/")[-1]
    return "any"


def _parse_docstring_args(docstring: str) -> dict[str, str]:
    """解析 Google-style 文档字符串中的 Args 段落。"""
    if not docstring:
        return {}

    params: dict[str, str] = {}
    lines = docstring.splitlines()
    in_args = False
    current_param: str | None = None
    current_lines: list[str] = []

    section_re = re.compile(r"^(\s*)(\w[\w\s]*):\s*$")
    param_re = re.compile(r"^    (\w+):\s*(.*)")

    for line in lines:
        stripped = line.strip()
        section_match = section_re.match(line)
        if section_match:
            if in_args:
                if current_param is not None:
                    params[current_param] = " ".join(current_lines).strip()
                break
            if stripped == "Args:":
                in_args = True
            continue

        if not in_args:
            continue

        param_match = param_re.match(line)
        if param_match:
            if current_param is not None:
                params[current_param] = " ".join(current_lines).strip()
            current_param = param_match.group(1)
            current_lines = [param_match.group(2).strip()] if param_match.group(2).strip() else []
        elif current_param is not None and line.startswith("      "):
            current_lines.append(stripped)

    if current_param is not None:
        params[current_param] = " ".join(current_lines).strip()

    return params


def _normalize_schema_name(name: str) -> str:
    """规范化 components/schemas 名称。"""
    return re.sub(r"[^\w.\-]", "_", name)


def _model_schema_components(
    model: type[BaseModel], *, mode: JsonSchemaMode = "validation"
) -> tuple[str, dict[str, Any]]:
    """生成模型 schema，并将 $defs 平铺为 components/schemas。"""
    schema = model.model_json_schema(ref_template=OPENAPI3_REF_TEMPLATE, mode=mode)
    definitions = schema.pop("$defs", {}) or {}
    model_name = _normalize_schema_name(str(schema.get("title") or model.__name__))
    components = {model_name: schema}
    for def_name, def_schema in definitions.items():
        components[_normalize_schema_name(def_name)] = def_schema
    return model_name, components


def _get_signature_defaults(fn: Callable[..., Any]) -> dict[str, dict[str, Any]]:
    """通过 inspect.signature 获取参数默认值。"""
    try:
        sig = inspect.signature(fn, follow_wrapped=True)
    except (ValueError, TypeError):
        return {}

    result: dict[str, dict[str, Any]] = {}
    for name, param in sig.parameters.items():
        if name in _SKIP_PARAMS:
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        has_default = param.default is not inspect.Parameter.empty
        result[name] = {
            "has_default": has_default,
            "default": param.default if has_default else None,
        }
    return result


def _get_query_response_model(fn: Callable[..., Any] | None) -> type[BaseModel] | None:
    """从 client.query(req, ResponseModel, ...) 调用中提取响应模型。"""
    if fn is None:
        return None
    try:
        source = textwrap.dedent(inspect.getsource(fn))
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError):
        return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "query":
            continue
        if len(node.args) < 2 or not isinstance(node.args[1], ast.Name):
            continue
        model = fn.__globals__.get(node.args[1].id)
        if inspect.isclass(model) and issubclass(model, BaseModel):
            return model
    return None


def _get_return_schema(fn: Callable[..., Any] | None) -> dict[str, Any]:
    """尝试从函数返回注解生成 JSON Schema。"""
    if fn is None:
        return {}

    try:
        hints = get_type_hints(fn)
    except Exception:
        hints = {}

    return_type = hints.get("return")
    if return_type is None:
        return {}

    try:
        origin = getattr(return_type, "__origin__", None)
        if origin is not None and inspect.isclass(origin) and issubclass(origin, BaseModel):
            return origin.model_json_schema()
        if inspect.isclass(return_type) and issubclass(return_type, BaseModel):
            return return_type.model_json_schema()
    except Exception:
        pass

    type_name = getattr(return_type, "__name__", str(return_type))
    return {"description": type_name}


def _build_parameters(  # pylint: disable=too-many-locals
    input_schema: dict[str, Any],
    sig_defaults: dict[str, dict[str, Any]],
    doc_params: dict[str, str],
) -> list[dict[str, Any]]:
    """融合 schema、签名和 docstring，构建完整参数列表。"""
    if not isinstance(input_schema, dict):
        return []

    properties: dict[str, Any] = input_schema.get("properties") or {}
    required_set: set[str] = set(input_schema.get("required") or [])

    params: list[dict[str, Any]] = []
    for param_name, prop in properties.items():
        if not isinstance(prop, dict):
            continue

        type_str = _resolve_type_str(prop)
        description = prop.get("description", "") or doc_params.get(param_name, "")

        schema_has_default = "default" in prop
        sig_info = sig_defaults.get(param_name, {})
        if schema_has_default:
            has_default = True
            default_value = prop["default"]
        elif sig_info.get("has_default"):
            has_default = True
            default_value = sig_info["default"]
        else:
            has_default = False
            default_value = None

        is_required = param_name in required_set if required_set else not has_default
        enum_values: list[Any] | None = prop.get("enum")
        example = prop.get("example")

        entry: dict[str, Any] = {
            "name": param_name,
            "type": type_str,
            "description": description,
            "required": is_required,
        }
        if has_default:
            entry["default"] = default_value
        if enum_values is not None:
            entry["enum"] = enum_values
        if example is not None:
            entry["example"] = example
        params.append(entry)

    return params


def build_tool_registry_entry(  # pylint: disable=too-many-locals
    tool_name: str,
    tool: Any,
    original_fn: Callable[..., Any],
    wrapper_fn: Callable[..., Any],
    body_params: dict[str, type[BaseModel]],
) -> dict[str, Any]:
    """基于注册阶段可获得的信息构建 registry 条目。"""
    description: str = getattr(tool, "description", "") or ""
    tags: list[str] = list(getattr(tool, "tags", None) or [])
    title = tool_name
    if getattr(tool, "annotations", None) is not None:
        ann_title = getattr(tool.annotations, "title", None)
        if ann_title:
            title = ann_title

    input_schema: dict[str, Any] = getattr(tool, "parameters", None) or getattr(tool, "input_schema", None) or {}
    sig_defaults = _get_signature_defaults(wrapper_fn)
    doc = inspect.getdoc(original_fn) or ""
    doc_params = _parse_docstring_args(doc)
    parameters = _build_parameters(input_schema, sig_defaults, doc_params)

    component_schemas: dict[str, Any] = {}
    input_model_name = None
    input_model = next(iter(body_params.values()), None)
    if input_model is not None:
        input_model_name, input_components = _model_schema_components(input_model, mode="validation")
        component_schemas.update(input_components)

    response_model_name = None
    response_model = _get_query_response_model(original_fn)
    if response_model is not None:
        response_model_name, response_components = _model_schema_components(response_model, mode="serialization")
        component_schemas.update(response_components)

    output_schema = _get_return_schema(original_fn) or _get_return_schema(wrapper_fn)

    doc_lines = [line.strip() for line in doc.splitlines() if line.strip()]
    summary = doc_lines[0].rstrip("。.") if doc_lines else title
    openapi_description = "<br/>".join(doc_lines) if doc_lines else description or ""
    tag_descriptions: dict[str, str] = {}
    if tool_name.startswith("shuiyin_"):
        tags = ["水印相关接口"]
        tag_descriptions["水印相关接口"] = "水印对应业务逻辑实现的相关接口"

    return {
        "name": tool_name,
        "title": title,
        "summary": summary,
        "description": openapi_description or "（无描述）",
        "tags": tags,
        "tag_descriptions": tag_descriptions,
        "parameters": parameters,
        "output_schema": output_schema,
        "input_model_name": input_model_name,
        "response_model_name": response_model_name,
        "component_schemas": component_schemas,
    }
