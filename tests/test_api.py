"""针对 `BaiduPCSApi` 公开方法的正常路径与边界测试。

区别于 test_smoke.py（只验证 import / 构造不联网），本文件通过 mock 掉
`BaiduPCS._request`，覆盖公开 API 在正常响应、错误码响应下的行为。
"""

from unittest.mock import MagicMock, patch

import pytest

from fundrives.baidu import BaiduPCS, BaiduPCSApi
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


def test_invalid_remote_path_raises_domain_error():
    pcs = BaiduPCS(bduss="dummy-bduss", user_id=1)

    with pytest.raises(BaiduPCSError, match="BaiduPCS.meta"):
        pcs.meta("relative/path")


def test_share_rejects_invalid_password_before_request():
    pcs = BaiduPCS(bduss="dummy-bduss", stoken="dummy-stoken", user_id=1)

    with pytest.raises(BaiduPCSError, match="password 必须为 4 个字符"):
        pcs.share("/a.txt", password="123")


def test_remote_path_cache_is_isolated_per_instance():
    first = _api()
    second = _api()
    first.list = MagicMock(return_value=[])
    second.list = MagicMock(return_value=[])

    first.remote_path_exists("a.txt", "/")
    first.remote_path_exists("b.txt", "/")
    second.remote_path_exists("a.txt", "/")

    first.list.assert_called_once_with("/")
    second.list.assert_called_once_with("/")


def test_sum_imei_keeps_legacy_alias_with_warning():
    from fundrives.baidu.phone import sum_IMEI, sum_imei

    expected = sum_imei("key")
    with pytest.warns(DeprecationWarning, match="sum_IMEI 已弃用"):
        actual = sum_IMEI("key")

    assert actual == expected


def test_upload_file_returns_pcsfile():
    api = _api()
    api._baidupcs.upload_file = MagicMock(
        return_value={"path": "/a.txt", "isdir": 0, "fs_id": 1, "size": 10}
    )

    pcs_file = api.upload_file(io=MagicMock(), remotepath="/a.txt")

    assert pcs_file.path == "/a.txt"
    api._baidupcs.upload_file.assert_called_once()


def test_upload_file_rejects_relative_path():
    """底层 `BaiduPCS.upload_file` 对非绝对路径应抛出领域异常而非静默失败。"""
    pcs = BaiduPCS(bduss="dummy-bduss", user_id=1)

    with pytest.raises(BaiduPCSError, match="BaiduPCS.upload_file"):
        pcs.upload_file(io=MagicMock(), remotepath="relative/a.txt")


def test_rapid_upload_file_returns_pcsfile():
    api = _api()
    api._baidupcs.rapid_upload_file = MagicMock(
        return_value={"path": "/a.txt", "isdir": 0, "fs_id": 1, "size": 10}
    )

    pcs_file = api.rapid_upload_file(
        slice_md5="a" * 32,
        content_md5="b" * 32,
        content_crc32=0,
        io_len=10,
        remotepath="/a.txt",
    )

    assert pcs_file.path == "/a.txt"


def test_rapid_upload_file_rejects_relative_path():
    pcs = BaiduPCS(bduss="dummy-bduss", user_id=1)

    with pytest.raises(BaiduPCSError, match="BaiduPCS.rapid_upload_file"):
        pcs.rapid_upload_file(
            slice_md5="a" * 32,
            content_md5="b" * 32,
            content_crc32=0,
            io_len=10,
            remotepath="relative/a.txt",
        )


def test_upload_slice_returns_md5():
    api = _api()
    api._baidupcs.upload_slice = MagicMock(return_value={"md5": "deadbeef"})

    assert api.upload_slice(io=MagicMock()) == "deadbeef"


def test_download_link_returns_link_on_success():
    api = _api()
    api._baidupcs.download_link = MagicMock(return_value="https://example.com/a.txt")

    assert api.download_link("/a.txt") == "https://example.com/a.txt"


def test_download_link_returns_none_when_missing():
    api = _api()
    api._baidupcs.download_link = MagicMock(return_value=None)

    assert api.download_link("/missing.txt") is None


def test_file_stream_returns_none_when_missing():
    api = _api()
    api._baidupcs.file_stream = MagicMock(return_value=None)

    assert api.file_stream("/missing.txt") is None


def test_file_stream_returns_io_on_success():
    api = _api()
    fake_io = MagicMock()
    api._baidupcs.file_stream = MagicMock(return_value=fake_io)

    assert api.file_stream("/a.txt") is fake_io


def test_add_task_returns_task_id():
    api = _api()
    api._baidupcs.add_task = MagicMock(return_value={"task_id": 123})

    assert api.add_task("https://example.com/a.torrent", "/dir") == "123"


def test_list_tasks_returns_cloud_tasks():
    api = _api()
    api._baidupcs.list_tasks = MagicMock(
        return_value={
            "task_info": [
                {
                    "task_id": "1",
                    "source_url": "https://example.com/a",
                    "task_name": "a",
                    "save_path": "/dir",
                    "status": 2,
                    "size": "100",
                    "finished_size": "100",
                    "ctime": 1,
                    "stime": 1,
                    "ftime": 2,
                }
            ]
        }
    )

    tasks = api.list_tasks()

    assert len(tasks) == 1
    assert tasks[0].task_id == "1"
    assert tasks[0].status == 2


def test_clear_tasks_returns_total():
    api = _api()
    api._baidupcs.clear_tasks = MagicMock(return_value={"total": 3})

    assert api.clear_tasks() == 3


def test_cancel_task_delegates_to_baidupcs():
    api = _api()
    api._baidupcs.cancel_task = MagicMock()

    api.cancel_task("task-1")

    api._baidupcs.cancel_task.assert_called_once_with("task-1")


def test_transfer_shared_paths_delegates_with_all_params():
    api = _api()
    api._baidupcs.transfer_shared_paths = MagicMock()

    api.transfer_shared_paths(
        remotedir="/dir",
        fs_ids=[1, 2],
        uk=1,
        share_id=2,
        bdstoken="token",
        shared_url="https://pan.baidu.com/s/1abc",
    )

    api._baidupcs.transfer_shared_paths.assert_called_once_with(
        "/dir", [1, 2], 1, 2, "token", "https://pan.baidu.com/s/1abc"
    )
