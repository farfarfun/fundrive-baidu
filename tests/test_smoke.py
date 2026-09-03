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
    from fundrives.baidu import BaiduPCS

    with pytest.raises(AssertionError):
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
