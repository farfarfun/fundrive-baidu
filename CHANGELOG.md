# CHANGELOG

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)，变更记录按版本倒序排列。

## [未发布]

### 修复

- 函数缓存改用组织自有包 `farcache`（`ttl_cache`），移除自建的 `TimeoutCache`/`timeout_cache`
- 凭据、路径、分享密码等运行时校验由 `assert` 改为显式条件判断并抛出 `BaiduPCSError`，避免在 Python 优化模式下被跳过而失去校验
- 修复 `_request`/`remote_path_exists`/`RapidUploadInfo.list` 等处的可变默认参数（`{}`/`[]`）跨调用共享状态问题
- `phone.py` 的 `sum_IMEI` 重命名为 `sum_imei`（`snake_case`），旧名称保留为带 `DeprecationWarning` 的兼容入口
- `common/io.py` 的 `_request` 请求失败日志误用 stdlib logging 的 `%s` 占位符，farlog（loguru）不支持该语法，参数被静默丢弃；改为 `{}` 占位符

### 新增

- 补充 `BaiduPCS` 类与 `shared_init_url`/`getcaptcha`/`get_vcode_img` 等公开方法的中文 docstring
- 为 `upload_file`/`rapid_upload_file`/`upload_slice`/`download_link`/`file_stream`、离线任务管理（`add_task`/`list_tasks`/`clear_tasks`/`cancel_task`）、`transfer_shared_paths` 等此前缺失覆盖的公开 API 补充基于 mock 的正常路径与边界测试

## [1.0.13] - 2026-09-21

### 修复

- 凭据未显式传入时从 `BDUSS`、`STOKEN` 和 `PTOKEN` 环境变量读取
- 补齐核心 API 返回类型标注并移除未使用的运行时依赖

## [1.0.12] - 2026-09-03

### 修复

- 日志统一迁移到组织自有包 `farlog`，移除对 `funutil` 的直接依赖
- 删除自定义日志模块 `common/log.py`（直接使用标准 `logging` 并自建 handler/日志目录），`common/io.py` 中的裸 `logging.warning` 改为 `farlog` logger
- 全仓库类型标注统一改为 Python 3.10 原生泛型/联合类型写法（`str | None`、`list[str]`、`dict[...]`），移除 `typing.Optional/List/Dict/Union/Tuple`（`typing_extensions.Literal` 也随之改为标准库 `typing.Literal`，移除该依赖）
- 补充 `BaiduPCSApi` 公开类与方法的中文 docstring（含此前完全缺失 docstring 的 `makedir`/`rename`/`save_shared` 等方法），明确用途、参数与返回值

### 新增

- 补充 `CHANGELOG.md`
- 提交 `uv.lock` 保证可复现构建
- 为公开 API 增加更多基于 mock 的正常路径与边界测试

### 变更

- `pyproject.toml` 补充 `[project] license = "MIT"` 与 `license-files`，移除冗余的 `[tool.setuptools] license-files = []`
- README 补充项目简介、安装命令、最小可运行示例，并明确上游 BaiduPCS-Py 项目的 MIT 协议与版权信息，追加组织介绍区块
- `.gitignore` 补充 `*.db`、`*.rar`、`.run/`、`logs/`、`.idea/`、`.vscode/` 忽略规则

## [1.0.11] 及更早版本

早期版本未系统记录变更，详见 Git 提交历史。
