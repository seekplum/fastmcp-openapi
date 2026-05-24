# 包发布说明

## 当前仓库的发布方式

当前项目的发包元数据定义在 [pyproject.toml](../pyproject.toml)：

- 构建后端：`hatchling`
- 包名：`fastmcp-openapi`
- 包源码目录：`src/fastmcp_openapi`
- 当前对外版本号来源：`[project].version`

仓库里目前没有看到现成的 CI 自动发布流程，因此更稳妥的方式是按下面步骤手动构建并发布。

## 发布前检查

发包前建议先完成这几项检查：

1. 更新 [pyproject.toml](../pyproject.toml) 中的 `[project].version`
2. 确认 `README.md`、`docs/`、`examples/` 中对外描述与版本一致
3. 运行测试与静态检查，避免把不可用版本发布到包仓库

## 构建分发包

在项目根目录执行：

```bash
uv build
```

执行后会在 `dist/` 目录下生成：

- `*.tar.gz`：源码分发包（sdist）
- `*.whl`：wheel 包

如果只想明确生成这两类产物，也可以执行：

```bash
uv build --sdist --wheel
```

构建完成后建议检查产物是否齐全：

```bash
ls -lh dist
```

## 发布到包仓库

推荐使用 `uv publish`。

先把仓库令牌放到环境变量，不要写进代码仓库：

```bash
export UV_PUBLISH_TOKEN="<your-token>"
```

先做一次 dry-run：

```bash
uv publish --dry-run --token "$UV_PUBLISH_TOKEN"
```

确认无误后正式发布：

```bash
uv publish --token "$UV_PUBLISH_TOKEN"

# 从 ~/.pypirc 获取 token
uv publish --token "$(
python - <<'PY'
import configparser
from pathlib import Path

cfg = configparser.RawConfigParser()
cfg.read(Path('~/.pypirc').expanduser())
print(cfg['pypi']['password'])
PY
)"
```

说明：

- `uv publish` 默认会上传 `dist/*`
- 如果你的目标不是默认包仓库，需要额外传入对应仓库的发布参数，例如 `--publish-url` 或 `--check-url`
- 由于当前仓库没有内置发布仓库配置，发布目标应以你实际使用的包仓库配置为准

## 推荐发布顺序

建议按下面顺序操作：

1. 修改版本号
2. 执行测试与静态检查
3. 执行 `uv build`
4. 执行 `uv publish --dry-run`
5. 执行正式发布
6. 在全新环境验证安装结果

## 发布后验证

发布完成后，建议在一个全新虚拟环境里验证安装：

```bash
python -m venv /tmp/fastmcp-openapi-release-verify
source /tmp/fastmcp-openapi-release-verify/bin/activate
pip install fastmcp-openapi==<version>
python -c "import fastmcp_openapi; print(fastmcp_openapi.__file__)"
```

如果还希望校验 README 示例能否正常导入，可以继续执行：

```bash
python -c "from fastmcp_openapi import FastMCPOpenAPI; print(FastMCPOpenAPI)"
```

## 当前仓库的注意点

- 版本号以 [pyproject.toml](../pyproject.toml) 中的 `[project].version` 为准
- 项目使用 `src/` 布局，发包时实际包含的是 `src/fastmcp_openapi`
- 依赖解析默认配置了阿里云镜像用于安装依赖，但这不等同于发布目标仓库
- 如果后续补充 GitHub Actions 或其他发布流水线，建议同步更新本文件，避免文档与实际流程不一致
