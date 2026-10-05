"""轻量冒烟测试（smoke tests）for fundrive-baidu.

本仓库此前没有 tests/ 目录。这里只做最基础的可用性验证：
- 顶层包 / 共享命名空间包能否正常 import
- 主要驱动类能否在不触发真实网络请求 / 不需要真实百度网盘凭据的情况下完成构造
- 明确无法在不使用真实凭据的情况下验证的路径，用 pytest.skip 标注并说明原因

不追求覆盖业务逻辑正确性，只保证包本身是"可安装、可导入、基础对象可构造"的。
"""

import socket

import pytest


class _NetworkGuard:
    """在 with 块内如果发生真实 TCP connect，则立即失败，用于证明构造过程没有偷偷发起网络请求。"""

    def __enter__(self):
        self._orig_connect = socket.socket.connect

        def _blocked(*args, **kwargs):
            raise AssertionError(
                "unexpected real network access during smoke test construction"
            )

        socket.socket.connect = _blocked
        return self

    def __exit__(self, exc_type, exc, tb):
        socket.socket.connect = self._orig_connect
        return False


def test_import_top_level_namespace():
    """共享导入命名空间 `fundrives`（NAMING.md 中记录的插件式布局）应可正常导入。"""
    import fundrives
    import fundrives.baidu  # noqa: F401


def test_import_public_api_symbols():
    """`fundrives.baidu` 对外导出的核心类型应可正常导入。"""
    from fundrives.baidu import (
        BaiduPCS,
        BaiduPCSApi,
        BaiduPCSError,
        PcsAuth,
        PcsFile,
        PcsQuota,
        PcsUser,
    )

    assert BaiduPCS is not None
    assert BaiduPCSApi is not None
    assert issubclass(BaiduPCSError, Exception)
    assert PcsFile is not None
    assert PcsQuota is not None
    assert PcsAuth is not None
    assert PcsUser is not None


def test_baidupcs_requires_credentials():
    """`BaiduPCS` 在既没有 bduss 也没有 cookies 时应当明确拒绝构造，而不是静默成功。"""
    from fundrives.baidu import BaiduPCS, BaiduPCSError

    with pytest.raises(BaiduPCSError, match="缺少 BDUSS"):
        BaiduPCS()


def test_baidupcs_construct_with_user_id_skips_network():
    """当显式传入 user_id 时，`BaiduPCS.__init__` 不应发起真实网络请求即可完成构造。

    源码路径：BaiduPCS.__init__ 中 `if not user_id: user_info = self.user_info()`，
    显式提供 user_id 时会跳过这次网络调用。
    """
    from fundrives.baidu import BaiduPCS

    with _NetworkGuard():
        pcs = BaiduPCS(bduss="dummy-bduss-for-smoke-test", user_id=123456)

    assert pcs.cookies.get("BDUSS") == "dummy-bduss-for-smoke-test"
    assert pcs._user_id == 123456


def test_baidupcs_reads_credentials_from_environment(monkeypatch):
    """未显式传参时从环境变量读取凭据。"""
    from fundrives.baidu import BaiduPCS

    monkeypatch.setenv("BDUSS", "env-bduss")
    with _NetworkGuard():
        pcs = BaiduPCS(user_id=123456)

    assert pcs._bduss == "env-bduss"


def test_baidupcsapi_construct_with_user_id_skips_network():
    """`BaiduPCSApi` 是 `BaiduPCS` 的包装类，同样验证可在不联网的情况下完成构造。"""
    from fundrives.baidu import BaiduPCSApi

    with _NetworkGuard():
        api = BaiduPCSApi(bduss="dummy-bduss-for-smoke-test", user_id=123456)

    assert api.user_id == 123456
    assert api.bduss == "dummy-bduss-for-smoke-test"


def test_baidupcs_construct_without_user_id_uses_mocked_login(mocker=None):
    """不提供 user_id 时，构造函数会调用 `user_info()`（内部用 `requests.post` 请求
    `tieba.baidu.com`）来反查 user_id。这里用 `unittest.mock` 打桩掉该请求，
    验证构造流程本身（数据组装、解析）是通的，而不需要真实凭据/网络。
    """
    from unittest.mock import MagicMock, patch

    from fundrives.baidu import BaiduPCS

    fake_response = MagicMock()
    fake_response.json.return_value = {"errno": 0, "user": {"id": 999}}

    with patch(
        "fundrives.baidu.pcs.requests.post", return_value=fake_response
    ) as mocked_post:
        pcs = BaiduPCS(bduss="dummy-bduss-for-smoke-test")

    mocked_post.assert_called_once()
    assert pcs._user_id == 999


def test_baidupcs_login_with_real_credentials_is_skipped():
    """真正登录 / 访问百度网盘账号信息需要真实的 BDUSS/cookie，
    在 CI / 冒烟测试环境中不具备，也不应该在测试里硬编码真实凭据。
    这里显式跳过，避免伪造一个“通过”的假象。
    """
    pytest.skip(
        "需要真实凭据，跳过：BaiduPCS 对真实百度网盘账号的登录/鉴权无法在无凭据环境下验证"
    )


def test_no_cli_entry_point_declared():
    """`pyproject.toml` 未声明 `[project.scripts]`，说明本包目前不提供命令行入口，
    因此没有需要用 `--help` 冒烟验证的 CLI。此测试仅确认这一前提没有被悄悄改变——
    如果以后加了 CLI 入口，请补充对应的 `--help` 冒烟测试后再更新本断言。
    """
    import pathlib
    import sys

    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib

    pyproject_path = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    assert "scripts" not in data.get("project", {}), (
        "检测到新增了 [project.scripts] CLI 入口，"
        "请为其补充 --help 冒烟测试后再更新/删除本断言"
    )


def test_shared_namespace_has_no_init_py():
    """共享命名空间目录 `src/fundrives/` 不得出现 `__init__.py`。

    `fundrive-alipan` / `fundrive-baidu` / `fundrive-lanzou` / `fundrive-quark`
    四个发行包共用 `fundrives` 这个 PEP 420 隐式命名空间。一旦这里出现
    `__init__.py`，隐式命名空间会退化成常规包，先安装的那个 wheel 会屏蔽掉
    兄弟插件，且四个 wheel 会争抢同一个文件。该文件曾被 `487b08b`（quark）/
    `11ed372`（baidu）删除过，这里挡住回归。
    """
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    assert not (repo_root / "src" / "fundrives" / "__init__.py").exists()


def test_namespace_package_is_implicit_at_runtime():
    """运行期 `fundrives` 必须是隐式命名空间包（`__file__` 为 None）。"""
    import fundrives

    assert getattr(fundrives, "__file__", None) is None
    assert hasattr(fundrives.__path__, "_path") or len(list(fundrives.__path__)) >= 1


def test_py_typed_marker_lives_in_real_package_root():
    """PEP 561 标记必须在真实包根 `src/fundrives/baidu/`，不能放在共享命名空间目录。"""
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    assert (repo_root / "src" / "fundrives" / "baidu" / "py.typed").is_file()
    assert not (repo_root / "src" / "fundrives" / "py.typed").exists()


def test_version_is_exposed_on_subpackage():
    """版本号挂在子包 `fundrives.baidu` 上，不能挂到共享命名空间 `fundrives` 上。"""
    import fundrives
    from fundrives import baidu

    assert isinstance(baidu.__version__, str) and baidu.__version__
    assert not hasattr(fundrives, "__version__")


def test_human_size_handles_sizes_beyond_tb():
    """`human_size` 超过 1024TB 时不能退化成缺数字的 `" TB"`。"""
    from fundrives.baidu.utils import human_size

    assert human_size(0) == "0 B"
    assert human_size(1024) == "1 KB"
    assert human_size(1536) == "1.5 KB"
    # 2PB：单位最大只到 TB，应折算成 2048 TB 而不是返回 " TB" 或 2.0 TB
    assert human_size(2 * 1024**5) == "2048 TB"


def test_request_sets_default_timeout():
    """`BaiduPCS._request` 必须带默认超时，否则服务端不响应时整个进程挂死。"""
    from unittest.mock import MagicMock, patch

    from fundrives.baidu import BaiduPCS
    from fundrives.baidu.pcs import DEFAULT_TIMEOUT

    with _NetworkGuard():
        pcs = BaiduPCS(bduss="dummy-bduss", user_id=1)

    with patch.object(pcs._session, "request", return_value=MagicMock()) as mocked:
        pcs._request_get("https://pan.baidu.com/x", params={"a": "b"})

    assert mocked.call_args.kwargs["timeout"] == DEFAULT_TIMEOUT


def test_request_get_forwards_extra_kwargs():
    """`_request_get` 声明了 `**kwargs` 就必须透传，不能静默丢弃调用方给的 timeout/stream。"""
    from unittest.mock import MagicMock, patch

    from fundrives.baidu import BaiduPCS

    with _NetworkGuard():
        pcs = BaiduPCS(bduss="dummy-bduss", user_id=1)

    with patch.object(pcs._session, "request", return_value=MagicMock()) as mocked:
        pcs._request_get("https://pan.baidu.com/x", timeout=1, stream=True)

    assert mocked.call_args.kwargs["timeout"] == 1
    assert mocked.call_args.kwargs["stream"] is True


def test_download_link_retry_is_bounded():
    """接口持续返回非 200 时，`download_link` 必须在有限次重试后返回 None，而不是死循环。"""
    from unittest.mock import MagicMock, patch

    from fundrives.baidu import BaiduPCS
    from fundrives.baidu.pcs import DOWNLOAD_LINK_MAX_RETRIES

    with _NetworkGuard():
        pcs = BaiduPCS(bduss="dummy-bduss", user_id=1)

    blocked = MagicMock()
    blocked.status = 403

    with (
        patch("fundrives.baidu.pcs.urllib.request.urlopen", return_value=blocked) as op,
        patch("fundrives.baidu.pcs.time.sleep") as slept,
    ):
        # ttl_cache 会缓存结果，用未缓存过的路径
        link = BaiduPCS.download_link.__wrapped__(pcs, "/retry-bounded.bin")

    assert link is None
    assert op.call_count == DOWNLOAD_LINK_MAX_RETRIES
    assert slept.call_count == DOWNLOAD_LINK_MAX_RETRIES - 1


def test_download_link_urlopen_has_timeout():
    """`download_link` 用的是 urllib，必须显式传 timeout，否则会无限等待。"""
    from unittest.mock import MagicMock, patch

    from fundrives.baidu import BaiduPCS

    with _NetworkGuard():
        pcs = BaiduPCS(bduss="dummy-bduss", user_id=1)

    ok = MagicMock()
    ok.status = 200
    ok.read.return_value = b'{"urls": [{"url": "https://example.invalid/f"}]}'

    with patch("fundrives.baidu.pcs.urllib.request.urlopen", return_value=ok) as op:
        link = BaiduPCS.download_link.__wrapped__(pcs, "/timeout-check.bin")

    assert link == "https://example.invalid/f"
    assert op.call_args.kwargs.get("timeout")


def test_range_request_retry_is_bounded():
    """`AutoDecryptRequest._request` 遇到持续的 `user is not authorized` 也要有重试上限。"""
    from unittest.mock import MagicMock, patch

    from fundrives.baidu.common.io import REQUEST_MAX_RETRIES, AutoDecryptRequest

    req = AutoDecryptRequest("GET", "https://example.invalid/f")

    blocked = MagicMock()
    blocked.ok = False
    blocked.status_code = 403
    blocked.content = b"user is not authorized, hitcode:122"

    with (
        patch.object(req._session, "request", return_value=blocked) as mocked,
        patch("fundrives.baidu.common.io.time.sleep") as slept,
        pytest.raises(OSError, match="Response is not ok"),
    ):
        req._request((0, 10))

    assert mocked.call_count == REQUEST_MAX_RETRIES
    assert slept.call_count == REQUEST_MAX_RETRIES - 1


def test_range_request_sets_default_timeout():
    """Range 请求必须带默认超时。"""
    from fundrives.baidu.common.io import (
        DEFAULT_TIMEOUT,
        AutoDecryptRequest,
        RangeRequestIO,
    )

    assert (
        AutoDecryptRequest("GET", "https://example.invalid/f")._kwargs["timeout"]
        == DEFAULT_TIMEOUT
    )
    assert (
        RangeRequestIO("GET", "https://example.invalid/f")._kwargs["timeout"]
        == DEFAULT_TIMEOUT
    )
