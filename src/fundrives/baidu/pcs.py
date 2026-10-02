from __future__ import annotations

import json
import os
import random
import re
import time
import urllib
from base64 import standard_b64encode
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import IO, Any, Literal
from urllib.error import HTTPError
from urllib.parse import quote_plus, urlparse

import requests  # type: ignore
from farcache import ttl_cache
from requests_toolbelt import MultipartEncoder, MultipartEncoderMonitor

from .common.crypto import calu_md5, calu_sha1
from .common.date import now_timestamp
from .common.io import MAX_CHUNK_SIZE, RangeRequestIO
from .errors import BaiduPCSError, assert_ok
from .phone import get_phone_model, sum_imei
from .utils import dump_json

PCS_BAIDU_COM = "https://pcs.baidu.com"
# PCS_BAIDU_COM = 'http://127.0.0.1:8888'
PAN_BAIDU_COM = "https://pan.baidu.com"
# PAN_BAIDU_COM = 'http://127.0.0.1:8888'

# PCS_UA = "netdisk;P2SP;2.2.90.43;WindowsBaiduYunGuanJia;netdisk;11.4.5;android-android;11.0;JSbridge4.4.0;LogStatistic"
# PCS_UA = "netdisk;P2SP;2.2.91.26;netdisk;11.6.3;GALAXY_S8;android-android;7.0;JSbridge4.4.0;jointBridge;1.1.0;"
# PCS_UA = "netdisk;P2SP;3.0.0.3;netdisk;11.5.3;PC;PC-Windows;android-android;11.0;JSbridge4.4.0"
# PCS_UA = "netdisk;P2SP;3.0.0.8;netdisk;11.12.3;GM1910;android-android;11.0;JSbridge4.4.0;jointBridge;1.1.0;"
PCS_UA = "softxm;netdisk"
PAN_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_14_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/77.0.3865.75 Safari/537.36"

PCS_HEADERS = {"User-Agent": PCS_UA}
PAN_HEADERS = {"User-Agent": PAN_UA}

PCS_APP_ID = "778750"
PAN_APP_ID = "250528"

M3u8Type = Literal["M3U8_AUTO_720", "M3U8_AUTO_480"]


def _from_to(f: str, t: str) -> dict[str, str]:
    return {"from": f, "to": t}


class Method(Enum):
    Head = "HEAD"
    Get = "GET"
    Post = "POST"


class PcsNode(Enum):
    """Pan Nodes which use pcs.baidu.com"""

    Quota = "rest/2.0/pcs/quota"
    File = "rest/2.0/pcs/file"

    def url(self) -> str:
        return f"{PCS_BAIDU_COM}/{self.value}"


class PanNode(Enum):
    """Pan Nodes which use pan.baidu.com"""

    TransferShared = "share/transfer"
    Share = "share/set"
    SharedPathList = "share/list"
    SharedRecord = "share/record"
    SharedCancel = "share/cancel"
    SharedPassword = "share/surlinfoinrecord"
    Getcaptcha = "api/getcaptcha"
    Cloud = "rest/2.0/services/cloud_dl"
    UserProducts = "rest/2.0/membership/user"

    def url(self) -> str:
        return f"{PAN_BAIDU_COM}/{self.value}"


class BaiduPCS:
    """提供返回原始 JSON 数据的百度网盘 PCS 接口。"""

    def __init__(
        self,
        bduss: str | None = None,
        stoken: str | None = None,
        ptoken: str | None = None,
        cookies: dict[str, str | None] | None = None,
        user_id: int | None = None,
    ) -> None:
        cookies = cookies or {}
        bduss = bduss or os.getenv("BDUSS")
        stoken = stoken or os.getenv("STOKEN")
        ptoken = ptoken or os.getenv("PTOKEN")
        if not bduss and cookies and cookies.get("BDUSS", ""):
            bduss = cookies["BDUSS"]
        if not stoken and cookies and cookies.get("STOKEN", ""):
            stoken = cookies["STOKEN"]
        if not ptoken and cookies and cookies.get("PTOKEN", ""):
            ptoken = cookies["PTOKEN"]

        if not bduss:
            raise BaiduPCSError(
                "BaiduPCS.__init__: 缺少 BDUSS，请通过参数、cookies 或环境变量提供"
            )

        if not cookies:
            cookies = {"BDUSS": bduss, "STOKEN": stoken, "PTOKEN": ptoken}

        self._bduss = bduss
        self._stoken = stoken
        self._ptoken = ptoken
        self._bdstoken = ""
        self._logid = None
        self._baiduid = cookies.get("BAIDUID")
        if self._baiduid:
            self._logid = standard_b64encode(self._baiduid.encode("ascii")).decode(
                "utf-8"
            )

        self._cookies = cookies
        self._session = requests.Session()
        self._session.cookies.update(cookies)

        user_info = None
        if not user_id:
            user_info = self.user_info()
            user_id = user_info.get("user", {}).get("id")
            if not user_id:
                user_info = None
        self._user_id = user_id
        self._user_info = user_info

    @property
    def cookies(self) -> dict[str, str | None]:
        return self._session.cookies.get_dict()

    @staticmethod
    def _app_id(url: str) -> str:
        """根据 URL 选择应用 ID。"""

        if PCS_BAIDU_COM in url:
            return PCS_APP_ID
        else:
            return PAN_APP_ID

    @property
    def bdstoken(self) -> str:
        if not (self._stoken or self._cookies.get("STOKEN")):
            raise BaiduPCSError("BaiduPCS.bdstoken: cookies 中缺少 STOKEN")

        if self._bdstoken:
            return self._bdstoken

        url = "http://pan.baidu.com/disk/home"
        resp = self._request(Method.Get, url, params=None)
        cn = resp.text
        mod = re.search(r'bdstoken[\'":\s]+([0-9a-f]{32})', cn)
        if mod:
            s = mod.group(1)
            self._bdstoken = str(s)
            return s
        return ""

    @staticmethod
    def _headers(url: str) -> dict[str, str]:
        """根据 URL 选择请求头。"""

        if PCS_BAIDU_COM in url:
            return dict(PCS_HEADERS)
        else:
            return dict(PAN_HEADERS)

    def _cookies_update(self, cookies: dict[str, str]) -> None:
        self._session.cookies.update(cookies)

    def _request(
        self,
        method: Method,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        data: str | bytes | dict[str, str] | Any = None,
        files: dict[str, Any] | None = None,
        **kwargs,
    ) -> requests.Response:
        if params and isinstance(params, dict):
            app_id = self._app_id(url)
            params["app_id"] = app_id

        if not headers:
            headers = self._headers(url)

        if isinstance(data, (MultipartEncoder, MultipartEncoderMonitor)):
            assert headers
            headers["Content-Type"] = data.content_type

        try:
            resp = self._session.request(
                method.value,
                url,
                params=params,
                headers=headers,
                data=data,
                files=files,
                **kwargs,
            )
            return resp
        except Exception as err:
            raise BaiduPCSError("BaiduPCS._request", cause=err)

    def _request_get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        **kwargs,
    ) -> requests.Response:
        return self._request(Method.Get, url, params=params, headers=headers)

    @assert_ok
    def quota(self) -> dict[str, Any]:
        """返回网盘容量信息。"""

        url = PcsNode.Quota.url()
        params = {"method": "info"}
        resp = self._request(Method.Get, url, params=params)
        return resp.json()

    def meta(self, *remotepaths: str) -> dict[str, Any]:
        if not remotepaths or not all(p.startswith("/") for p in remotepaths):
            raise BaiduPCSError(
                f"BaiduPCS.meta: remotepaths 必须是非空的绝对路径列表: {remotepaths!r}"
            )

        param = [{"path": p} for p in remotepaths]
        return self.file_operate("meta", param)

    def exists(self, remotepath: str) -> bool:
        r = self.meta(remotepath)
        if r.get("error_code"):
            return False
        else:
            return True

    def is_file(self, remotepath: str) -> bool:
        r = self.meta(remotepath)
        if r.get("error_code"):
            return False
        if r["list"][0]["isdir"] == 0:
            return True
        else:
            return False

    def is_dir(self, remotepath: str) -> bool:
        r = self.meta(remotepath)
        if r.get("error_code"):
            return False
        if r["list"][0]["isdir"] == 1:
            return True
        else:
            return False

    @assert_ok
    def list(
        self,
        remotepath: str,
        desc: bool = False,
        name: bool = False,
        time: bool = False,
        size: bool = False,
    ) -> dict[str, Any]:
        url = PcsNode.File.url()
        orderby = None
        if name:
            orderby = "name"
        elif time:
            orderby = "time"  # 服务器最后修改时间
        elif size:
            orderby = "size"
        else:
            orderby = "name"

        params = {
            "method": "list",
            "by": orderby,
            "limit": "0-2147483647",
            "order": ["asc", "desc"][desc],
            "path": str(remotepath),
        }
        resp = self._request(Method.Get, url, params=params)
        return resp.json()

    @assert_ok
    def upload_file(
        self,
        io: IO,
        remotepath: str,
        ondup: str = "overwrite",
        callback: Callable[[MultipartEncoderMonitor], None] | None = None,
    ) -> dict[str, Any]:
        """上传 IO 内容到远程路径（不支持设置本地创建和修改时间）。"""

        if not remotepath.startswith("/"):
            raise BaiduPCSError(
                f"BaiduPCS.upload_file: remotepath 必须是绝对路径: {remotepath!r}"
            )
        remotePath = Path(remotepath)

        url = PcsNode.File.url()
        params = {
            "method": "upload",
            "ondup": ondup,
            "dir": remotePath.parent.as_posix(),
            "filename": remotePath.name,
            "BDUSS": self._bduss,
        }

        m = MultipartEncoder(fields={"file": ("file", io, "")})
        monitor = MultipartEncoderMonitor(m, callback=callback)

        resp = self._request(Method.Post, url, params=params, data=monitor)
        return resp.json()

    @assert_ok
    def rapid_upload_file(
        self,
        slice_md5: str,
        content_md5: str,
        content_crc32: int,  # not needed
        io_len: int,
        remotepath: str,
        local_ctime: int | None = None,
        local_mtime: int | None = None,
        ondup: str = "overwrite",
    ) -> dict[str, Any]:
        """通过文件摘要快速上传。

        slice_md5 (32 bytes): the md5 of pre 256KB of content.
        content_md5 (32 bytes): the md5 of total content.
        content_crc32 (int): the crc32 of total content (Not Needed),
            if content_crc32 is 0, the params of the api will be ignored.
        io_len (int): the length of total content.
        remotepath (str): the absolute remote path to save the content.
        """

        if not remotepath.startswith("/"):
            raise BaiduPCSError(
                f"BaiduPCS.rapid_upload_file: remotepath 必须是绝对路径: {remotepath!r}"
            )

        url = PcsNode.File.url()
        params = {
            "method": "rapidupload",
            "BDUSS": self._bduss,
        }

        ntp = now_timestamp()

        data = {
            "path": remotepath,
            "content-length": io_len,
            "content-md5": content_md5,
            "slice-md5": slice_md5,
            "content-crc32": content_crc32,
            "local_ctime": str(local_ctime or ntp),
            "local_mtime": str(local_mtime or ntp),
            "ondup": ondup,
        }

        # Not needed
        if content_crc32 == 0:
            del data["content-crc32"]

        resp = self._request(Method.Post, url, params=params, data=data)
        return resp.json()

    @assert_ok
    def upload_slice(
        self, io: IO, callback: Callable[[MultipartEncoderMonitor], None] | None = None
    ) -> dict[str, Any]:
        url = PcsNode.File.url()
        params = {
            "method": "upload",
            "type": "tmpfile",
            "BDUSS": self._bduss,
        }

        m = MultipartEncoder(fields={"file": ("file", io, "")})
        monitor = MultipartEncoderMonitor(m, callback=callback)

        resp = self._request(
            Method.Post,
            url,
            params=params,
            data=monitor,
            timeout=(3, 9),  # (connect timeout, read timeout)
        )
        return resp.json()

    @assert_ok
    def combine_slices(
        self,
        slice_md5s: list[str],
        remotepath: str,
        local_ctime: int | None = None,
        local_mtime: int | None = None,
        ondup: str = "overwrite",
    ) -> dict[str, Any]:
        url = PcsNode.File.url()
        params = {
            "method": "createsuperfile",
            "path": remotepath,
            "ondup": ondup,
            "BDUSS": self._bduss,
        }

        ntp = now_timestamp()

        data = {
            "param": dump_json({"block_list": slice_md5s}),
            "local_ctime": str(local_ctime or ntp),
            "local_mtime": str(local_mtime or ntp),
        }
        resp = self._request(Method.Post, url, params=params, data=data)
        return resp.json()

    @assert_ok
    def search(
        self, keyword: str, remotepath: str, recursive: bool = False
    ) -> dict[str, Any]:
        url = PcsNode.File.url()
        params = {
            "method": "search",
            "path": remotepath,
            "wd": keyword,
            "re": "1" if recursive else "0",
        }
        resp = self._request(Method.Get, url, params=params)
        return resp.json()

    @assert_ok
    def makedir(self, directory: str) -> dict[str, Any]:
        url = PcsNode.File.url()
        params = {
            "method": "mkdir",
            "path": directory,
        }
        resp = self._request(Method.Get, url, params=params)
        return resp.json()

    def file_operate(self, operate: str, param: list[dict[str, str]]) -> dict[str, Any]:
        url = PcsNode.File.url()
        params = {"method": operate}
        data = {"param": dump_json({"list": param})}
        resp = self._request(Method.Post, url, params=params, data=data)
        return resp.json()

    @assert_ok
    def move(self, *remotepaths: str) -> dict[str, Any]:
        """
        Move sources to destination

        sources, dest = remotepaths[:-1], remotepaths[-1]

        `dest` must be a directory
        """

        if len(remotepaths) <= 1 or not all(p.startswith("/") for p in remotepaths):
            raise BaiduPCSError(
                f"BaiduPCS.move: 源路径和目标路径必须是绝对路径: {remotepaths!r}"
            )

        sources, dest = remotepaths[:-1], remotepaths[-1]

        if self.is_file(dest):
            raise BaiduPCSError("The remote `dest` is a file. It must be a directory.")

        if not self.is_dir(dest):
            self.makedir(dest)

        _sources = (Path(s) for s in sources)
        _dest = Path(dest)

        param = [_from_to(s.as_posix(), (_dest / s.name).as_posix()) for s in _sources]
        return self.file_operate("move", param)

    @assert_ok
    def rename(self, source: str, dest: str) -> dict[str, Any]:
        """重命名远程路径。"""

        if not all(p.startswith("/") for p in (source, dest)):
            raise BaiduPCSError(
                f"BaiduPCS.rename: source 和 dest 必须是绝对路径: {source!r}, {dest!r}"
            )

        param = [_from_to(source, dest)]
        return self.file_operate("move", param)

    @assert_ok
    def copy(self, *remotepaths: str) -> dict[str, Any]:
        """
        Copy sources to destination

        sources, dest = remotepaths[:-1], remotepaths[-1]

        `dest` must be a directory
        """

        if len(remotepaths) <= 1 or not all(p.startswith("/") for p in remotepaths):
            raise BaiduPCSError(
                f"BaiduPCS.copy: 源路径和目标路径必须是绝对路径: {remotepaths!r}"
            )

        sources, dest = remotepaths[:-1], remotepaths[-1]

        if self.is_file(dest):
            raise BaiduPCSError("The remote `dest` is a file. It must be a directory.")

        if not self.is_dir(dest):
            self.makedir(dest)

        _sources = (Path(s) for s in sources)
        _dest = Path(dest)

        param = [_from_to(s.as_posix(), (_dest / s.name).as_posix()) for s in _sources]
        return self.file_operate("copy", param)

    @assert_ok
    def remove(self, *remotepaths: str) -> dict[str, Any]:
        if not remotepaths or not all(p.startswith("/") for p in remotepaths):
            raise BaiduPCSError(
                f"BaiduPCS.remove: remotepaths 必须是非空的绝对路径列表: {remotepaths!r}"
            )

        param = [{"path": p} for p in remotepaths]
        return self.file_operate("delete", param)

    @assert_ok
    def cloud_operate(
        self, params: dict[str, str], data: dict[str, str] | None = None
    ) -> dict[str, Any]:
        url = PanNode.Cloud.url()
        if data:
            resp = self._request(Method.Post, url, params=params, data=data)
        else:
            resp = self._request(Method.Get, url, params=params)
        return resp.json()

    def magnet_info(self, magnet: str) -> dict[str, Any]:
        params = {
            "method": "query_magnetinfo",
            "source_url": magnet,
            # 'save_path': '/',
            "type": "4",
        }
        return self.cloud_operate(params)

    def torrent_info(self, remote_torrent: str) -> dict[str, Any]:
        params = {
            "method": "query_sinfo",
            "source_path": remote_torrent,
            # 'save_path': '/',
            "type": "2",
            # 't': int(time.time() * 1000),
        }
        return self.cloud_operate(params)

    def add_task(self, task_url: str, remotedir: str) -> dict[str, Any]:
        """Add cloud task for http/s and ed2k url

        Warning: `STOKEN` must be in `cookies`
        """

        if not self._stoken:
            raise BaiduPCSError("BaiduPCS.add_task: cookies 中缺少 STOKEN")

        params = {
            "channel": "chunlei",
            "web": "1",
            "app_id": "250528",
            "bdstoken": self.bdstoken,  # Must be set
            # "logid": self._logid,
            "clienttype": "0",
        }
        data = {
            "method": "add_task",
            "app_id": "250528",
            "save_path": remotedir,
            "source_url": task_url,
            "timeout": "2147483647",
        }
        return self.cloud_operate(params, data=data)

    def add_magnet_task(
        self, task_url: str, remotedir: str, selected_idx: list[int]
    ) -> dict[str, Any]:
        """Add cloud task for magnet

        Args:
            selected_idx (list[int]): indexes of `BaiduPCS.magnet_info` list,
                starting from 1

        Warning: `STOKEN` must be in `cookies`
        """

        if not self._stoken:
            raise BaiduPCSError("BaiduPCS.add_magnet_task: cookies 中缺少 STOKEN")

        params = {
            "channel": "chunlei",
            "web": "1",
            "app_id": "250528",
            "bdstoken": self.bdstoken,  # Must be set
            # "logid": self._logid,
            "clienttype": "0",
        }
        data = {
            "method": "add_task",
            "app_id": "250528",
            "save_path": remotedir,
            "source_url": task_url,
            "timeout": "2147483647",
            "type": "4",
            "t": str(int(time.time() * 1000)),
            "file_sha1": "",
            "selected_idx": ",".join([str(i) for i in selected_idx or []]),
            "task_from": "1",
        }
        return self.cloud_operate(params, data=data)

    def tasks(self, *task_ids: str) -> dict[str, Any]:
        params = {
            "method": "query_task",
            "task_ids": ",".join(task_ids),
        }
        return self.cloud_operate(params)

    def list_tasks(self) -> dict[str, Any]:
        params = {
            "method": "list_task",
            "need_task_info": "1",
            "status": "255",
            "start": "0",
            "limit": "1000",
        }
        return self.cloud_operate(params)

    def clear_tasks(self) -> dict[str, Any]:
        params = {
            "method": "clear_task",
        }
        return self.cloud_operate(params)

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        params = {
            "method": "cancel_task",
            "task_id": task_id,
        }
        return self.cloud_operate(params)

    @assert_ok
    def share(
        self, *remotepaths: str, password: str, period: int = 0
    ) -> dict[str, Any]:
        """Share `remotepaths` to public

        period (int): The days for expiring. `0` means no expiring
        """

        if not self._stoken:
            raise BaiduPCSError("BaiduPCS.share: cookies 中缺少 STOKEN")
        if len(password) != 4:
            raise BaiduPCSError(
                f"BaiduPCS.share: password 必须为 4 个字符，实际长度为 {len(password)}"
            )

        meta = self.meta(*remotepaths)
        fs_ids = [i["fs_id"] for i in meta["list"]]

        url = PanNode.Share.url()
        params = {
            "channel": "chunlei",
            "clienttype": "0",
            "web": "1",
            "bdstoken": self.bdstoken,
        }
        data = {
            "fid_list": dump_json(fs_ids),
            "schannel": "0",
            "channel_list": "[]",
            "period": str(int(period)),
        }
        if password:
            data["pwd"] = password
            data["schannel"] = "4"

        resp = self._request(Method.Post, url, params=params, data=data)
        return resp.json()

    @assert_ok
    def list_shared(self, page: int = 1) -> dict[str, Any]:
        """
        list.0.channel:
            - 0, no password
            - 4, with password
        """

        url = PanNode.SharedRecord.url()
        params = {
            "page": str(page),
            "desc": "1",
            "order": "time",
        }
        resp = self._request(Method.Get, url, params=params)
        return resp.json()

    @assert_ok
    def shared_password(self, share_id: int) -> dict[str, Any]:
        """
        Only return password
        """

        url = PanNode.SharedPassword.url()
        params = {
            "shareid": str(share_id),
            "sign": calu_md5(f"{share_id}_sharesurlinfo!@#"),
        }
        resp = self._request(Method.Get, url, params=params)
        return resp.json()

    @assert_ok
    def cancel_shared(self, *share_ids: int) -> dict[str, Any]:
        url = PanNode.SharedCancel.url()
        data = {
            "shareid_list": dump_json(share_ids),
        }
        hdrs = dict(PCS_HEADERS)
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        resp = self._request(Method.Post, url, headers=hdrs, params=None, data=data)
        return resp.json()

    def shared_init_url(self, shared_url: str) -> str:
        """生成分享验证页地址。

        :param shared_url: 百度网盘分享链接
        :return: 对应的分享验证页地址
        """
        u = urlparse(shared_url)
        surl = u.path.split("/s/1")[-1]
        return f"https://pan.baidu.com/share/init?surl={surl}"

    @assert_ok
    def access_shared(
        self, shared_url: str, password: str, vcode_str: str, vcode: str
    ) -> dict[str, Any]:
        """Pass password to the session

        WARNING: this method is not threadsafe.
        """

        url = "https://pan.baidu.com/share/verify"
        init_url = self.shared_init_url(shared_url)
        params = {
            "surl": init_url.split("surl=")[-1],
            "t": str(now_timestamp() * 1000),
            "channel": "chunlei",
            "web": "1",
            "bdstoken": "null",
            "clienttype": "0",
        }
        data = {
            "pwd": password,
            "vcode": vcode,
            "vcode_str": vcode_str,
        }
        hdrs = dict(PAN_HEADERS)
        hdrs["Referer"] = init_url
        resp = self._request(Method.Post, url, headers=hdrs, params=params, data=data)

        # These cookies must be included through all sub-processes
        self._cookies_update(resp.cookies.get_dict())

        return resp.json()

    @assert_ok
    def getcaptcha(self, shared_url: str) -> dict[str, Any]:
        """获取分享访问验证码信息。

        :param shared_url: 百度网盘分享链接
        :return: 服务端返回的验证码信息
        """
        url = PanNode.Getcaptcha.url()
        params = {
            "prod": "shareverify",
            "channel": "chunlei",
            "web": "1",
            "bdstoken": "null",
            "clienttype": "0",
        }

        hdrs = dict(PAN_HEADERS)
        hdrs["Referer"] = self.shared_init_url(shared_url)
        hdrs["X-Requested-With"] = "XMLHttpRequest"
        hdrs["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        resp = self._request(Method.Get, url, headers=hdrs, params=params)
        return resp.json()

    def get_vcode_img(self, vcode_img_url: str, shared_url: str) -> bytes:
        """下载分享访问验证码图片。

        :param vcode_img_url: 验证码图片地址
        :param shared_url: 百度网盘分享链接，用于设置请求来源
        :return: 验证码图片字节
        """
        hdrs = dict(PAN_HEADERS)
        hdrs["Referer"] = self.shared_init_url(shared_url)
        resp = self._request_get(vcode_img_url, headers=hdrs)
        return resp.content

    @assert_ok
    def shared_paths(self, shared_url: str) -> dict[str, Any]:
        """Get shared paths

        Call `BaiduPCS.access_share` before calling the function

        WARNING: this method is not threadsafe.
        """

        if not self._stoken:
            raise BaiduPCSError("BaiduPCS.shared_paths: cookies 中缺少 STOKEN")

        resp = self._request(Method.Get, shared_url, params=None)
        html = resp.text

        # These cookies must be included through all sub-processes
        self._cookies_update(resp.cookies.get_dict())

        m = re.search(r"(?:yunData.setData|locals.mset)\((.+?)\);", html)
        if not m:
            raise BaiduPCSError(
                f"BaiduPCS.shared_paths: 无法从分享页面解析数据: {shared_url!r}"
            )

        shared_data = m.group(1)
        return json.loads(shared_data)

    @assert_ok
    def list_shared_paths(
        self, sharedpath: str, uk: int, share_id: int, page: int = 1, size: int = 100
    ) -> dict[str, Any]:
        if not self._stoken:
            raise BaiduPCSError("BaiduPCS.list_shared_paths: cookies 中缺少 STOKEN")

        url = PanNode.SharedPathList.url()
        params = {
            "channel": "chunlei",
            "clienttype": "0",
            "web": "1",
            "page": str(page),  # from 1
            "num": str(size),  # max is 100
            "dir": sharedpath,
            "t": str(random.random()),
            "uk": str(uk),
            "shareid": str(share_id),
            "desc": "1",  # reversely
            "order": "other",  # sort by name, or size, time
            "bdstoken": "null",
            "showempty": "0",
        }
        resp = self._request(Method.Get, url, params=params)
        return resp.json()

    @assert_ok
    def transfer_shared_paths(
        self,
        remotedir: str,
        fs_ids: list[int],
        uk: int,
        share_id: int,
        bdstoken: str,
        shared_url: str,
    ) -> dict[str, Any]:
        """`remotedir` must exist"""

        url = PanNode.TransferShared.url()
        params = {
            "shareid": str(share_id),
            "from": str(uk),
            "bdstoken": bdstoken,
            "channel": "chunlei",
            "clienttype": "0",
            "web": "1",
        }
        data = {
            "fsidlist": dump_json(fs_ids),
            "path": remotedir,
        }
        hdrs = dict(PAN_HEADERS)
        hdrs["X-Requested-With"] = "XMLHttpRequest"
        hdrs["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        hdrs["Origin"] = "https://pan.baidu.com"
        hdrs["Referer"] = shared_url  # WARNING: Referer must be set to shared_url

        resp = self._request(Method.Post, url, headers=hdrs, params=params, data=data)
        info = resp.json()
        if info.get("info") and info["info"][0]["errno"]:
            info["errno"] = info["info"][0]["errno"]
        return info

    @assert_ok
    def user_info(self) -> dict[str, Any]:
        bduss = self._bduss
        timestamp = str(now_timestamp())
        model = get_phone_model(bduss)
        phoneIMEIStr = sum_imei(bduss)

        data = {
            "bdusstoken": bduss + "|null",
            "channel_id": "",
            "channel_uid": "",
            "stErrorNums": "0",
            "subapp_type": "mini",
            "timestamp": timestamp + "922",
        }
        data["_client_type"] = "2"
        data["_client_version"] = "7.0.0.0"
        data["_phone_imei"] = phoneIMEIStr
        data["from"] = "mini_ad_wandoujia"
        data["model"] = model
        data["cuid"] = (
            calu_md5(
                bduss
                + "_"
                + data["_client_version"]
                + "_"
                + data["_phone_imei"]
                + "_"
                + data["from"]
            ).upper()
            + "|"
            + phoneIMEIStr[::-1]
        )
        data["sign"] = calu_md5(
            "".join([k + "=" + data[k] for k in sorted(data.keys())]) + "tiebaclient!!!"
        ).upper()

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": "ka=open",
            "net": "1",
            "User-Agent": "bdtb for Android 6.9.2.1",
            "client_logid": timestamp + "416",
            "Connection": "Keep-Alive",
        }

        resp = requests.post(
            "http://tieba.baidu.com/c/s/login", headers=headers, data=data
        )
        return resp.json()

    @assert_ok
    def tieba_user_info(self, user_id: int) -> dict[str, Any]:
        params = f"has_plist=0&need_post_count=1&rn=1&uid={user_id}"
        params += "&sign=" + calu_md5(params.replace("&", "") + "tiebaclient!!!")
        url = "http://c.tieba.baidu.com/c/u/user/profile?" + params
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": "ka=open",
            "net": "1",
            "User-Agent": "bdtb for Android 6.9.2.1",
            "client_logid": str(now_timestamp() * 1000),
            "Connection": "Keep-Alive",
        }
        resp = requests.get(url, headers=headers, params=None)
        return resp.json()

    @assert_ok
    def user_products(self) -> dict[str, Any]:
        url = PanNode.UserProducts.url()
        params = {
            "method": "query",
        }
        resp = self._request(Method.Get, url, params=params)
        return resp.json()

    @ttl_cache(ttl=1 * 60 * 60)
    def download_link(self, remotepath: str, pcs: bool = False) -> str | None:
        if pcs:
            return (
                "http://c.pcs.baidu.com/rest/2.0/pcs/file"
                f"?method=download&app_id={PCS_APP_ID}&path={quote_plus(remotepath)}"
                "&ver=2.0&clienttype=1"
            )

        bduss = self._bduss
        uid = str(self._user_id or "")
        devuid = calu_md5(bduss).upper() + "|0"
        enc = calu_sha1(bduss)

        while True:
            timestamp = str(now_timestamp())

            rand = calu_sha1(
                enc + uid + "ebrcUYiuxaZv2XGu7KIYKxUrqfnOfpDF" + timestamp + devuid
            )

            url = PcsNode.File.url()
            params = {
                "apn_id": "1_0",
                "app_id": PAN_APP_ID,
                "channel": "0",
                "check_blue": "1",
                "clienttype": "17",
                "es": "1",
                "esl": "1",
                "freeisp": "0",
                "method": "locatedownload",
                "path": quote_plus(remotepath),
                "queryfree": "0",
                "use": "0",
                "ver": "4.0",
                "time": timestamp,
                "rand": rand,
                "devuid": devuid,
                "cuid": devuid,
            }

            params_str = "&".join([f"{k}={v}" for k, v in params.items()])

            headers = dict(PCS_HEADERS)
            headers["Cookie"] = "; ".join([f"{k}={v}" for k, v in self.cookies.items()])
            req = urllib.request.Request(
                url + "?" + params_str, headers=headers, method="GET"
            )  # type: ignore
            try:
                resp = urllib.request.urlopen(req)  # type: ignore

                # Error: "user is not authorized"
                # This error occurs when the method is called by too many times
                if resp.status != 200:
                    time.sleep(2)
                    continue

                info = json.loads(resp.read())

                # This error is gotten when remote path is blocked
                if info.get("host") == "issuecdn.baidupcs.com":
                    return None

                if not info.get("urls"):
                    return None
                else:
                    # return info["urls"][0]["url"].replace("&htype=", "")
                    return info["urls"][0]["url"]
            except HTTPError:
                # 403 code could occor at unavailable downloading url, #97
                return None

    def file_stream(
        self,
        remotepath: str,
        max_chunk_size: int = MAX_CHUNK_SIZE,
        callback: Callable[..., None] | None = None,
        encrypt_password: bytes = b"",
        pcs: bool = False,
    ) -> RangeRequestIO | None:
        url = self.download_link(remotepath, pcs=pcs)
        if not url:
            return None

        headers = {
            "Cookie": f"BDUSS={self._bduss};",
            "User-Agent": PCS_UA,
            "Connection": "Keep-Alive",
        }
        return RangeRequestIO(
            Method.Get.value,
            url,
            headers=headers,
            max_chunk_size=max_chunk_size,
            callback=callback,
            encrypt_password=encrypt_password,
        )

    # Playing the m3u8 file is needed add `--stream-lavf-o-append="protocol_whitelist=file,http,https,tcp,tls,crypto,hls,applehttp"` for mpv
    # https://github.com/mpv-player/mpv/issues/6928#issuecomment-532198445
    @assert_ok
    def m3u8_stream(
        self, remotepath: str, type: M3u8Type = "M3U8_AUTO_720"
    ) -> dict[str, Any]:
        """Get content of the m3u8 stream file"""

        url = PcsNode.File.url()
        params = {
            "method": "streaming",
            "path": remotepath,
            "type": type,
        }

        resp = self._request(Method.Get, url, params=params)
        cn = resp.text
        if cn.startswith("{"):  # error
            return resp.json()
        else:
            return {"m3u8_content": cn}
