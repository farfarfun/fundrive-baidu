"""针对 `BaiduPCSApi` 公开方法的正常路径与边界测试。

区别于 test_smoke.py（只验证 import / 构造不联网），本文件通过 mock 掉
`BaiduPCS._request`，覆盖公开 API 在正常响应、错误码响应下的行为。
"""

from unittest.mock import MagicMock, patch

import pytest

from fundrives.baidu import BaiduPCSApi
from fundrives.baidu.errors import BaiduPCSError


def _api() -> BaiduPCSApi:
    """构造一个跳过网络请求的 `BaiduPCSApi` 实例。"""
    with patch(
        "fundrives.baidu.pcs.requests.post",
        return_value=MagicMock(json=lambda: {"errno": 0, "user": {"id": 1}}),
    ):
        return BaiduPCSApi(bduss="dummy-bduss", user_id=1)


def _mock_json(api: BaiduPCSApi, payload: dict) -> MagicMock:
    """将 `api._baidupcs._request` 打桩为返回给定 JSON 的响应。"""
    resp = MagicMock()
    resp.json.return_value = payload
    api._baidupcs._request = MagicMock(return_value=resp)
    return resp


def test_quota_returns_pcsquota():
    api = _api()
    _mock_json(api, {"errno": 0, "quota": 100, "used": 50})

    quota = api.quota()

    assert quota.quota == 100
    assert quota.used == 50


def test_meta_returns_pcsfile_list():
    api = _api()
    _mock_json(
        api,
        {
            "errno": 0,
            "list": [{"path": "/a.txt", "isdir": 0, "fs_id": 1, "size": 10}],
        },
    )

    files = api.meta("/a.txt")

    assert len(files) == 1
    assert files[0].path == "/a.txt"
    assert files[0].is_file is True


def test_exists_is_file_is_dir():
    api = _api()
    _mock_json(api, {"errno": 0, "list": [{"path": "/a.txt", "isdir": 0, "fs_id": 1}]})

    assert api.exists("/a.txt") is True
    assert api.is_file("/a.txt") is True
    assert api.is_dir("/a.txt") is False


def test_exists_returns_false_on_error_code():
    api = _api()
    _mock_json(api, {"error_code": -9})

    assert api.exists("/missing.txt") is False


def test_list_recursive_collects_subdirectory_files():
    api = _api()
    top_level = {
        "errno": 0,
        "list": [
            {"path": "/dir", "isdir": 1, "fs_id": 1},
            {"path": "/file.txt", "isdir": 0, "fs_id": 2},
        ],
    }
    sub_level = {
        "errno": 0,
        "list": [{"path": "/dir/nested.txt", "isdir": 0, "fs_id": 3}],
    }
    responses = iter([top_level, sub_level])

    def _fake_request(*args, **kwargs):
        resp = MagicMock()
        resp.json.return_value = next(responses)
        return resp

    api._baidupcs._request = MagicMock(side_effect=_fake_request)

    files = api.list("/", recursive=True)

    paths = {f.path for f in files}
    assert paths == {"/dir", "/file.txt", "/dir/nested.txt"}


def test_makedir_returns_pcsfile():
    api = _api()
    _mock_json(api, {"errno": 0, "path": "/newdir", "isdir": 1})

    pcs_file = api.makedir("/newdir")

    assert pcs_file.path == "/newdir"
    assert pcs_file.is_dir is True


def test_rename_returns_fromto():
    api = _api()
    _mock_json(
        api,
        {"errno": 0, "extra": {"list": [{"from": "/a.txt", "to": "/b.txt"}]}},
    )

    result = api.rename("/a.txt", "/b.txt")

    assert result.from_ == "/a.txt"
    assert result.to_ == "/b.txt"


def test_rename_raises_when_no_list_in_response():
    api = _api()
    _mock_json(api, {"errno": 0, "extra": {}})

    with pytest.raises(BaiduPCSError):
        api.rename("/a.txt", "/b.txt")


def test_move_and_copy_return_fromto_list():
    api = _api()
    # `move()` 先后调用 is_file(dest)、is_dir(dest)（各触发一次 meta 请求），
    # 最终才发起真正的移动请求，因此需要按调用顺序打桩多次响应。
    dest_meta = {"errno": 0, "list": [{"path": "/dir", "isdir": 1, "fs_id": 9}]}
    move_result = {
        "errno": 0,
        "extra": {
            "list": [
                {"from": "/a.txt", "to": "/dir/a.txt"},
                {"from": "/b.txt", "to": "/dir/b.txt"},
            ]
        },
    }
    responses = iter([dest_meta, dest_meta, move_result])

    def _fake_request(*args, **kwargs):
        resp = MagicMock()
        resp.json.return_value = next(responses)
        return resp

    api._baidupcs._request = MagicMock(side_effect=_fake_request)

    results = api.move("/a.txt", "/b.txt", "/dir")

    assert [r.from_ for r in results] == ["/a.txt", "/b.txt"]
    assert [r.to_ for r in results] == ["/dir/a.txt", "/dir/b.txt"]


def test_remove_calls_underlying_pcs():
    api = _api()
    _mock_json(api, {"errno": 0})

    api.remove("/a.txt", "/b.txt")

    api._baidupcs._request.assert_called_once()


def test_share_sets_password_and_paths():
    api = _api()
    api._baidupcs.meta = MagicMock(return_value={"list": [{"fs_id": 1}]})
    _mock_json(api, {"errno": 0, "shareid": 1, "link": "https://pan.baidu.com/s/1abc"})
    api._baidupcs._stoken = "stoken"
    # 预置缓存的 bdstoken，避免 `share()` 内部访问 `self.bdstoken` 时
    # 触发一次额外的、返回 HTML 页面的真实请求路径。
    api._baidupcs._bdstoken = "cached-bdstoken"

    link = api.share("/a.txt", password="1234")

    assert link.password == "1234"
    assert link.paths == ["/a.txt"]


def test_shared_password_returns_none_when_expired():
    api = _api()
    _mock_json(api, {"errno": 0, "pwd": "0"})

    assert api.shared_password(1) is None


def test_shared_password_returns_password():
    api = _api()
    _mock_json(api, {"errno": 0, "pwd": "abcd"})

    assert api.shared_password(1) == "abcd"


def test_assert_ok_raises_baidupcserror_on_nonzero_errno():
    """`assert_ok` 装饰器应在响应 errno 非 0 时转换为 `BaiduPCSError`，而不是静默返回原始数据。"""
    api = _api()
    _mock_json(api, {"errno": -9})

    with pytest.raises(BaiduPCSError):
        api.quota()


def test_unify_shared_url_rejects_invalid_url():
    from fundrives.baidu.api import _unify_shared_url

    with pytest.raises(ValueError):
        _unify_shared_url("https://example.com/not-a-shared-link")


def test_unify_shared_url_normalizes_standard_link():
    from fundrives.baidu.api import _unify_shared_url

    url = _unify_shared_url("https://pan.baidu.com/s/1AbCdEfG?pwd=1234")

    assert url == "https://pan.baidu.com/s/1AbCdEfG"
