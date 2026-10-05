# CHANGELOG

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)，变更记录按版本倒序排列。

## [未发布]

### 修复

- 函数缓存改用组织自有包 `farcache`（`ttl_cache`），移除自建的 `TimeoutCache`/`timeout_cache`
- 凭据、路径、分享密码等运行时校验由 `assert` 改为显式条件判断并抛出 `BaiduPCSError`，避免在 Python 优化模式下被跳过而失去校验
- 修复 `_request`/`remote_path_exists`/`RapidUploadInfo.list` 等处的可变默认参数（`{}`/`[]`）跨调用共享状态问题
- `phone.py` 的 `sum_IMEI` 重命名为 `sum_imei`（`snake_case`），旧名称保留为带 `DeprecationWarning` 的兼容入口
- `common/io.py` 的 `_request` 请求失败日志误用 stdlib logging 的 `%s` 占位符，farlog（loguru）不支持该语法，参数被静默丢弃；改为 `{}` 占位符
- `utils.human_size`：大小超过 1024TB 时内部循环不会 break，返回 `" TB"` 这种缺数字的结果；补兜底并乘回最后一轮多除的 1024
- 所有 HTTP 请求补默认超时 `(10, 60)`（`BaiduPCS._request`、贴吧登录/资料接口、`AutoDecryptRequest`/`RangeRequestIO` 的 Range 请求）。此前除分片上传外一律无超时，服务端不响应时整个进程挂死；`user_info()` 会在 `BaiduPCS.__init__` 中被调用，构造函数本身就可能永久阻塞
- `BaiduPCS._request_get` 声明了 `**kwargs` 却没有转发给 `_request`，调用方传的 `timeout`/`stream` 等参数被静默丢弃
- `BaiduPCS.download_link` 的 `while True` 在接口持续返回非 200（调用过频被限流）时是无上限热循环，且 `urllib.request.urlopen` 未设超时；改为最多重试 5 次、每次间隔 2 秒，耗尽后记日志并返回 `None`
- `common/io.py` 的 `AutoDecryptRequest._request` 同样存在无上限 `while True`：服务端持续返回 `user is not authorized` 时永不退出；改为最多重试 5 次，耗尽后按既有逻辑抛 `OSError`

### 变更

- `uv.lock` 按组织规范不再纳入版本管理，已从仓库移除并保留在 `.gitignore` 中（`da521c0`）。`[1.0.12]` 里「提交 `uv.lock`」只反映当时状态，不代表现行做法。
- PEP 561 标记 `py.typed` 从共享命名空间目录 `src/fundrives/` 移到真实包根 `src/fundrives/baidu/`：`fundrives` 是四个 `fundrive-*` 发行包共用的 PEP 420 隐式命名空间，标记放在共用目录会越权声明兄弟包也带类型，且多个 wheel 会争抢同一个文件

### 新增

- `fundrives.baidu` 暴露 `__version__`（取自 `importlib.metadata`，版本号唯一来源仍是 `pyproject.toml`）
- 补充 `BaiduPCS` 类与 `shared_init_url`/`getcaptcha`/`get_vcode_img` 等公开方法的中文 docstring
- 补齐 `pcs.py` / `inner.py` / `errors.py` / `utils.py` 全部公开类、方法、函数的中文 docstring，原有英文 docstring 一并译为中文
- `tests/test_smoke.py` 新增 11 条回归测试（全部基于 mock，无任何真实百度网盘调用）：命名空间契约（`src/fundrives/__init__.py` 不得存在、运行期 `fundrives.__file__` 必须为 `None`、`py.typed` 位置、`__version__` 挂载点）、`human_size` 超大值边界、默认超时、`_request_get` 的 kwargs 透传、两处重试上限
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
