from __future__ import annotations

import os
import re
from collections import deque
from collections.abc import Callable
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import IO

from farlog import getLogger
from PIL import Image
from requests_toolbelt import MultipartEncoderMonitor
from rich.prompt import Prompt

from .common import constant
from .common.crypto import calu_md5
from .common.io import MAX_CHUNK_SIZE, RangeRequestIO
from .inner import (
    CloudTask,
    FromTo,
    PcsAuth,
    PcsFile,
    PcsMagnetFile,
    PcsQuota,
    PcsRapidUploadInfo,
    PcsSharedLink,
    PcsSharedPath,
    PcsUser,
    PcsUserProduct,
)
from .pcs import BaiduPCS, BaiduPCSError, M3u8Type

logger = getLogger("fundrive")

SHARED_URL_PREFIX = "https://pan.baidu.com/s/"


def _unify_shared_url(url: str) -> str:
    """统一分享链接格式。

    :param url: 原始分享链接（标准链接或旧版 surl 链接）
    :return: 统一为 ``https://pan.baidu.com/s/<id>`` 格式的分享链接
    :raises ValueError: 传入的链接不是合法的百度网盘分享链接
    """

    # For Standard url
    temp = r"pan\.baidu\.com/s/(.+?)(\?|$)"
    m = re.search(temp, url)
    if m:
        return SHARED_URL_PREFIX + m.group(1)

    # For surl url
    temp = r"baidu\.com.+?\?surl=(.+?)(\?|$)"
    m = re.search(temp, url)
    if m:
        return SHARED_URL_PREFIX + "1" + m.group(1)

    raise ValueError(f"The shared url is not a valid url. {url}")


class BaiduPCSApi:
    """百度网盘 PCS 接口封装。

    是 `BaiduPCS` 的上层包装，将原始 PCS 请求的响应内容解析为更易用的
    内部数据结构（`PcsFile`、`PcsSharedLink` 等）。
    """

    def __init__(
        self,
        bduss: str | None = None,
        stoken: str | None = None,
        ptoken: str | None = None,
        cookies: dict[str, str | None] = {},
        user_id: int | None = None,
    ):
        """初始化百度网盘 API 客户端。

        :param bduss: 百度账号登录凭据 BDUSS
        :param stoken: 分享相关接口所需的 STOKEN
        :param ptoken: 部分接口所需的 PTOKEN
        :param cookies: 附加 cookies
        :param user_id: 百度用户 id
        """
        self._baidupcs = BaiduPCS(
            bduss, stoken=stoken, ptoken=ptoken, cookies=cookies, user_id=user_id
        )

    @property
    def bduss(self) -> str:
        """当前使用的 BDUSS。"""
        return self._baidupcs._bduss

    @property
    def bdstoken(self) -> str:
        """当前使用的 BDSTOKEN。"""
        return self._baidupcs.bdstoken

    @property
    def stoken(self) -> str | None:
        """当前使用的 STOKEN。"""
        return self._baidupcs._stoken

    @property
    def ptoken(self) -> str | None:
        """当前使用的 PTOKEN。"""
        return self._baidupcs._ptoken

    @property
    def baiduid(self) -> str | None:
        """当前使用的 BAIDUID。"""
        return self._baidupcs._baiduid

    @property
    def logid(self) -> str | None:
        """当前使用的 LOGID。"""
        return self._baidupcs._logid

    @property
    def user_id(self) -> int | None:
        """当前用户 id。"""
        return self._baidupcs._user_id

    @property
    def cookies(self) -> dict[str, str | None]:
        """当前会话使用的完整 cookies。"""
        return self._baidupcs.cookies

    def quota(self) -> PcsQuota:
        """获取网盘空间配额信息。

        :return: 包含总空间、已用空间的 `PcsQuota`
        """

        info = self._baidupcs.quota()
        return PcsQuota(quota=info["quota"], used=info["used"])

    def meta(self, *remotepaths: str) -> list[PcsFile]:
        """获取 `remotepaths` 的元数据。

        :param remotepaths: 一个或多个网盘绝对路径
        :return: 对应的 `PcsFile` 列表
        """

        info = self._baidupcs.meta(*remotepaths)
        return [PcsFile.from_(v) for v in info.get("list", [])]

    def exists(self, remotepath: str) -> bool:
        """检查 `remotepath` 是否存在。

        :param remotepath: 网盘绝对路径
        :return: 是否存在
        """

        return self._baidupcs.exists(remotepath)

    def is_file(self, remotepath: str) -> bool:
        """检查 `remotepath` 是否为文件。

        :param remotepath: 网盘绝对路径
        :return: 是否为文件
        """

        return self._baidupcs.is_file(remotepath)

    def is_dir(self, remotepath: str) -> bool:
        """检查 `remotepath` 是否为目录。

        :param remotepath: 网盘绝对路径
        :return: 是否为目录
        """

        return self._baidupcs.is_dir(remotepath)

    def list(
        self,
        remotepath: str,
        desc: bool = False,
        name: bool = False,
        time: bool = False,
        size: bool = False,
        recursive: bool = False,
    ) -> list[PcsFile]:
        """列出目录内容。

        :param remotepath: 网盘目录绝对路径
        :param desc: 是否倒序排列
        :param name: 是否按名称排序
        :param time: 是否按时间排序
        :param size: 是否按大小排序
        :param recursive: 是否递归列出子目录内容
        :return: `PcsFile` 列表
        """

        info = self._baidupcs.list(
            remotepath, desc=desc, name=name, time=time, size=size
        )
        pcs_files = [PcsFile.from_(v) for v in info.get("list", [])]
        if recursive:
            for pcs_file in pcs_files:
                if pcs_file.is_dir:
                    sub_pcs_files = self.list(
                        pcs_file.path, desc=desc, name=name, time=time, size=size
                    )
                    pcs_files.extend(sub_pcs_files)
        return pcs_files

    def upload_file(
        self,
        io: IO,
        remotepath: str,
        ondup="overwrite",
        callback: Callable[[MultipartEncoderMonitor], None] = None,
    ) -> PcsFile:
        """上传一个 IO 对象到 `remotepath`。

        :param io: 待上传内容的可读 IO 对象
        :param remotepath: 网盘绝对路径
        :param ondup: 同名处理策略，``overwrite`` 覆盖或 ``newcopy`` 新建副本
        :param callback: 上传进度回调
        :return: 上传成功后的 `PcsFile`

        注意：该接口无法设置 local_ctime 和 local_mtime。
        """

        info = self._baidupcs.upload_file(
            io, remotepath, ondup=ondup, callback=callback
        )
        return PcsFile.from_(info)

    def rapid_upload_file(
        self,
        slice_md5: str,
        content_md5: str,
        content_crc32: int,  # not needed
        io_len: int,
        remotepath: str,
        local_ctime: int | None = None,
        local_mtime: int | None = None,
        ondup="overwrite",
    ) -> PcsFile:
        """秒传文件。

        :param slice_md5: 内容前 256KB 的 md5（32 字节）
        :param content_md5: 完整内容的 md5（32 字节）
        :param content_crc32: 完整内容的 crc32（非必需，若为 0 则该参数会被忽略）
        :param io_len: 完整内容的长度
        :param remotepath: 保存内容的网盘绝对路径
        :param local_ctime: 本地创建时间戳（可选）
        :param local_mtime: 本地修改时间戳（可选）
        :param ondup: 同名处理策略，``overwrite`` 覆盖或 ``newcopy`` 新建副本
        :return: 上传成功后的 `PcsFile`
        """

        info = self._baidupcs.rapid_upload_file(
            slice_md5,
            content_md5,
            content_crc32,
            io_len,
            remotepath,
            local_ctime=local_ctime,
            local_mtime=local_mtime,
            ondup=ondup,
        )
        return PcsFile.from_(info)

    def upload_slice(
        self, io: IO, callback: Callable[[MultipartEncoderMonitor], None] = None
    ) -> str:
        """上传一个 IO 对象作为分片。

        :param io: 待上传分片内容
        :param callback: 上传进度回调
        :return: 分片的 md5
        """

        info = self._baidupcs.upload_slice(io, callback=callback)
        return info["md5"]

    def combine_slices(
        self,
        slice_md5s: list[str],
        remotepath: str,
        local_ctime: int | None = None,
        local_mtime: int | None = None,
        ondup="overwrite",
    ) -> PcsFile:
        """合并已上传的分片到 `remotepath`。

        :param slice_md5s: 各分片的 md5 列表
        :param remotepath: 保存内容的网盘绝对路径
        :param local_ctime: 本地创建时间戳（可选）
        :param local_mtime: 本地修改时间戳（可选）
        :param ondup: 同名处理策略，``overwrite`` 覆盖或 ``newcopy`` 新建副本
        :return: 合并成功后的 `PcsFile`
        """

        info = self._baidupcs.combine_slices(
            slice_md5s,
            remotepath,
            local_ctime=local_ctime,
            local_mtime=local_mtime,
            ondup=ondup,
        )
        return PcsFile.from_(info)

    def search(
        self, keyword: str, remotepath: str, recursive: bool = False
    ) -> list[PcsFile]:
        """在 `remotepath` 下按 `keyword` 搜索文件。

        :param keyword: 搜索关键字
        :param remotepath: 搜索起始目录
        :param recursive: 是否递归搜索子目录
        :return: 匹配的 `PcsFile` 列表
        """

        info = self._baidupcs.search(keyword, remotepath, recursive=recursive)
        pcs_files = []
        for file_info in info["list"]:
            pcs_files.append(PcsFile.from_(file_info))
        return pcs_files

    def makedir(self, directory: str) -> PcsFile:
        """创建目录。

        :param directory: 待创建的网盘绝对路径
        :return: 创建成功后的 `PcsFile`
        """
        info = self._baidupcs.makedir(directory)
        return PcsFile.from_(info)

    def move(self, *remotepaths: str) -> list[FromTo]:
        """将 `remotepaths[:-1]` 移动到 `remotepaths[-1]`。

        :param remotepaths: 一组网盘绝对路径，最后一个为目标目录
        :return: 每个源路径到目标路径的映射列表
        :raises BaiduPCSError: 移动操作失败
        """

        info = self._baidupcs.move(*remotepaths)
        r = info["extra"].get("list")
        if not r:
            raise BaiduPCSError("File operator [move] fails")
        return [FromTo(from_=v["from"], to_=v["to"]) for v in r]

    def rename(self, source: str, dest: str) -> FromTo:
        """重命名 `source` 为 `dest`。

        :param source: 原始网盘绝对路径
        :param dest: 新的网盘绝对路径
        :return: 源路径到新路径的映射
        :raises BaiduPCSError: 重命名操作失败
        """
        info = self._baidupcs.rename(source, dest)
        r = info["extra"].get("list")
        if not r:
            raise BaiduPCSError("File operator [rename] fails")
        v = r[0]
        return FromTo(from_=v["from"], to_=v["to"])

    def copy(self, *remotepaths: str) -> list[FromTo]:
        """将 `remotepaths[:-1]` 复制到 `remotepaths[-1]`。

        :param remotepaths: 一组网盘绝对路径，最后一个为目标目录
        :return: 每个源路径到目标路径的映射列表
        :raises BaiduPCSError: 复制操作失败
        """

        info = self._baidupcs.copy(*remotepaths)
        r = info["extra"].get("list")
        if not r:
            raise BaiduPCSError("File operator [copy] fails")
        return [FromTo(from_=v["from"], to_=v["to"]) for v in r]

    def remove(self, *remotepaths: str) -> None:
        """删除所有 `remotepaths`。

        :param remotepaths: 待删除的网盘绝对路径
        """

        self._baidupcs.remove(*remotepaths)

    def magnet_info(self, magnet: str) -> list[PcsMagnetFile]:
        """获取磁力链接的信息。

        :param magnet: 磁力链接
        :return: 磁力链接内包含的文件列表
        """

        info = self._baidupcs.magnet_info(magnet)
        return [PcsMagnetFile.from_(v) for v in info["magnet_info"]]

    def torrent_info(self, remote_torrent: str) -> None:
        """获取 `remote_torrent` 种子文件的信息。

        :param remote_torrent: 网盘内种子文件路径
        """

        self._baidupcs.torrent_info(remote_torrent)

    def add_task(self, task_url: str, remotedir: str) -> str:
        """添加一个离线下载任务，保存到 `remotedir`。

        :param task_url: 待下载资源的 http 链接
        :param remotedir: 保存目录
        :return: 任务 id
        """

        info = self._baidupcs.add_task(task_url, remotedir)
        return str(info["task_id"])

    def add_magnet_task(
        self, task_url: str, remotedir: str, selected_idx: list[int]
    ) -> str:
        """添加一个磁力链接离线下载任务，保存到 `remotedir`。

        :param task_url: 磁力链接
        :param remotedir: 保存目录
        :param selected_idx: 需要下载的文件索引
        :return: 任务 id
        """

        info = self._baidupcs.add_magnet_task(task_url, remotedir, selected_idx)
        return str(info["task_id"])

    def tasks(self, *task_ids: str) -> list[CloudTask]:
        """按 `task_ids` 列出离线下载任务。

        :param task_ids: 任务 id 列表
        :return: 对应的 `CloudTask` 列表
        """

        info = self._baidupcs.tasks(*task_ids)
        tasks = []
        for task_id, v in info["task_info"].items():
            v["task_id"] = task_id
            tasks.append(CloudTask.from_(v))
        return tasks

    def list_tasks(self) -> list[CloudTask]:
        """列出所有离线下载任务。

        :return: `CloudTask` 列表
        """

        info = self._baidupcs.list_tasks()
        return [CloudTask.from_(v) for v in info["task_info"]]

    def clear_tasks(self) -> int:
        """清空所有已完成和失败的离线下载任务。

        :return: 被清除的任务数量
        """

        info = self._baidupcs.clear_tasks()
        return info["total"]

    def cancel_task(self, task_id: str) -> None:
        """取消指定 `task_id` 的离线下载任务。

        :param task_id: 任务 id
        """

        self._baidupcs.cancel_task(task_id)

    def share(self, *remotepaths: str, password: str, period: int = 0) -> PcsSharedLink:
        """创建 `remotepaths` 的公开分享链接（可设置提取码）。

        调用该接口 cookies 中必须包含 `STOKEN`。

        :param remotepaths: 待分享的网盘绝对路径
        :param password: 分享提取码
        :param period: 过期天数，``0`` 表示永不过期
        :return: 分享链接信息
        """

        info = self._baidupcs.share(*remotepaths, password=password, period=period)
        link = PcsSharedLink.from_(info)._replace(
            paths=list(remotepaths), password=password
        )
        return link

    def list_shared(self, page: int = 1) -> list[PcsSharedLink]:
        """分页列出当前用户的分享链接。

        调用该接口 cookies 中必须包含 `STOKEN`。

        :param page: 页码，从 1 开始
        :return: 分享链接列表
        """

        info = self._baidupcs.list_shared(page)
        return [PcsSharedLink.from_(v) for v in info["list"]]

    def shared_password(self, share_id: int) -> str | None:
        """查看分享链接的提取码。

        调用该接口 cookies 中必须包含 `STOKEN`。

        :param share_id: 分享 id
        :return: 提取码，若分享未设置提取码或已过期则返回 ``None``
        """

        info = self._baidupcs.shared_password(share_id)
        p = info.get("pwd", "0")  # If "pwd" is not in info, error is 分享已过期
        if p == "0":
            return None
        return p

    def cancel_shared(self, *share_ids: int) -> None:
        """按 `share_ids` 取消分享链接。

        调用该接口 cookies 中必须包含 `STOKEN`。

        :param share_ids: 分享 id 列表
        """

        self._baidupcs.cancel_shared(*share_ids)

    def access_shared(
        self,
        shared_url: str,
        password: str,
        vcode_str: str = "",
        vcode: str = "",
        show_vcode: bool = True,
    ) -> None:
        """校验需要提取码的分享链接 `shared_url`。

        调用 `self.shared_paths` 之前必须先调用本方法。

        :param shared_url: 分享链接
        :param password: 分享提取码
        :param vcode_str: 验证码校验字符串，触发验证码时使用
        :param vcode: 验证码内容，触发验证码时使用
        :param show_vcode: 为 ``True`` 时在需要验证码时打开图形界面窗口显示；
            为 ``False`` 时需要调用方自行处理验证码
        :raises BaiduPCSError: 校验失败且非验证码相关错误
        """

        while True:
            try:
                self._baidupcs.access_shared(shared_url, password, vcode_str, vcode)
                return
            except BaiduPCSError as err:
                if err.error_code not in (-9, -62):
                    raise err
                if show_vcode:
                    if err.error_code == -62:  # -62: '可能需要输入验证码'
                        logger.warning("Need vcode!")
                    if err.error_code == -9:
                        logger.warning("vcode is incorrect!")
                    vcode_str, vcode_img_url = self.getcaptcha(shared_url)
                    img_cn = self.get_vcode_img(vcode_img_url, shared_url)
                    img_buf = BytesIO(img_cn)
                    img_buf.seek(0, 0)
                    img = Image.open(img_buf)
                    img.show()
                    vcode = Prompt.ask("input vcode")
                else:
                    raise err

    def getcaptcha(self, shared_url: str) -> tuple[str, str]:
        """获取一个验证码信息。

        :param shared_url: 分享链接
        :return: ``(vcode_str, vcode_img_url)``
        """

        info = self._baidupcs.getcaptcha(shared_url)
        return info["vcode_str"], info["vcode_img"]

    def get_vcode_img(self, vcode_img_url: str, shared_url: str) -> bytes:
        """获取验证码图片内容。

        :param vcode_img_url: 验证码图片地址
        :param shared_url: 分享链接
        :return: 图片二进制内容
        """

        return self._baidupcs.get_vcode_img(vcode_img_url, shared_url)

    def shared_paths(self, shared_url: str) -> list[PcsSharedPath]:
        """获取 `shared_url` 分享的路径列表。

        :param shared_url: 分享链接
        :return: `PcsSharedPath` 列表
        :raises ValueError: 分享信息解析失败
        """

        info = self._baidupcs.shared_paths(shared_url)
        uk = info.get("share_uk") or info.get("uk")
        uk = int(uk)

        assert uk, "`BaiduPCSApi.shared_paths`: Don't get `uk`"

        share_id = info["shareid"]
        bdstoken = info["bdstoken"]

        if not info.get("file_list"):
            return []

        if isinstance(info["file_list"], list):
            file_list = info["file_list"]
        elif isinstance(info["file_list"].get("list"), list):
            file_list = info["file_list"]["list"]
        else:
            raise ValueError("`shared_paths`: Parsing shared info fails")

        return [
            PcsSharedPath.from_(v)._replace(uk=uk, share_id=share_id, bdstoken=bdstoken)
            for v in file_list
        ]

    def list_shared_paths(
        self,
        sharedpath: str,
        uk: int,
        share_id: int,
        bdstoken: str,
        page: int = 1,
        size: int = 100,
    ) -> list[PcsSharedPath]:
        """列出分享目录 `sharedpath` 下的子路径。

        :param sharedpath: 分享目录路径
        :param uk: 分享者 uk
        :param share_id: 分享 id
        :param bdstoken: bdstoken
        :param page: 页码，从 1 开始
        :param size: 单页数量
        :return: `PcsSharedPath` 列表
        """

        info = self._baidupcs.list_shared_paths(
            sharedpath, uk, share_id, page=page, size=size
        )
        return [
            PcsSharedPath.from_(v)._replace(uk=uk, share_id=share_id, bdstoken=bdstoken)
            for v in info["list"]
        ]

    def transfer_shared_paths(
        self,
        remotedir: str,
        fs_ids: list[int],
        uk: int,
        share_id: int,
        bdstoken: str,
        shared_url: str,
    ) -> None:
        """将分享路径的 `fs_ids` 转存到 `remotedir`。

        :param remotedir: 目标保存目录
        :param fs_ids: 待转存文件的 fs_id 列表
        :param uk: 分享者 uk
        :param share_id: 分享 id
        :param bdstoken: bdstoken
        :param shared_url: 分享链接
        """

        self._baidupcs.transfer_shared_paths(
            remotedir, fs_ids, uk, share_id, bdstoken, shared_url
        )

    def user_info(self) -> PcsUser:
        """获取当前用户信息。

        :return: 包含账号、鉴权、配额、会员等信息的 `PcsUser`
        """

        info = self._baidupcs.user_info()
        user_id = int(info["user"]["id"])
        user_name = info["user"]["name"]

        info = self._baidupcs.tieba_user_info(user_id)
        age = float(info["user"]["tb_age"])
        sex = info["user"]["sex"]
        if sex == 1:
            sex = "♂"
        elif sex == 2:
            sex = "♀"
        else:
            sex = "unknown"

        auth = PcsAuth(
            bduss=self._baidupcs._bduss,
            cookies=self.cookies,
            stoken=self._baidupcs._stoken,
            ptoken=self._baidupcs._ptoken,
        )

        quota = self.quota()

        products, level = self.user_products()

        return PcsUser(
            user_id=user_id,
            user_name=user_name,
            auth=auth,
            age=age,
            sex=sex,
            quota=quota,
            products=products,
            level=level,
        )

    def user_products(self) -> tuple[list[PcsUserProduct], int]:
        """获取当前用户的会员产品信息。

        :return: ``(会员产品列表, 当前等级)``
        """

        info = self._baidupcs.user_products()
        pds = []
        for p in info["product_infos"]:
            # `product_name` of some entries are None (issue #30)
            if not p.get("product_name"):
                continue

            pds.append(
                PcsUserProduct(
                    name=p["product_name"],
                    start_time=p["start_time"],
                    end_time=p["end_time"],
                )
            )

        level = info["level_info"]["current_level"]
        return pds, level

    def download_link(self, remotepath: str, pcs: bool = False) -> str | None:
        """获取 `remotepath` 的下载链接。

        :param remotepath: 网盘绝对路径
        :param pcs: 为 ``True`` 时返回 PCS 下载链接（限速，即使是超级会员也有下行阈值限制）；
            为 ``False`` 时返回 android api 请求的下载链接（超级会员无限速）
        :return: 下载链接，获取失败时为 ``None``
        """

        return self._baidupcs.download_link(remotepath, pcs=pcs)

    def file_stream(
        self,
        remotepath: str,
        max_chunk_size: int = MAX_CHUNK_SIZE,
        callback: Callable[..., None] = None,
        encrypt_password: bytes = b"",
        pcs: bool = False,
    ) -> RangeRequestIO | None:
        """将 `remotepath` 作为普通 IO 流打开。

        :param remotepath: 网盘绝对路径
        :param max_chunk_size: 单次请求的最大分片大小
        :param callback: 读取进度回调
        :param encrypt_password: 解密密码（内容为加密文件时使用）
        :param pcs: 是否使用 PCS 下载链接
        :return: 可读的 `RangeRequestIO`，失败时为 ``None``
        """

        return self._baidupcs.file_stream(
            remotepath,
            max_chunk_size=max_chunk_size,
            callback=callback,
            encrypt_password=encrypt_password,
            pcs=pcs,
        )

    def m3u8_stream(self, remotepath: str, type: M3u8Type = "M3U8_AUTO_720") -> str:
        """获取媒体文件的 m3u8 内容。

        :param remotepath: 网盘绝对路径
        :param type: 清晰度类型
        :return: m3u8 文本内容，获取失败时为空字符串
        """

        info = self._baidupcs.m3u8_stream(remotepath, type)
        if info.get("m3u8_content"):
            return info["m3u8_content"]
        else:
            # Here should be a error
            return ""

    def rapid_upload_info(
        self, remotepath: str, check: bool = True
    ) -> PcsRapidUploadInfo | None:
        """获取秒传信息。

        :param remotepath: 网盘绝对路径
        :param check: 为 ``True`` 时会调用 `self.rapid_upload_file` 校验秒传信息
            是否有效（会改变 `remotepath` 的 server_ctime、server_mtime 等元信息）
        :return: 秒传信息，不满足秒传条件时为 ``None``
        """

        pcs_file = self.meta(remotepath)[0]
        content_length = pcs_file.size or 0

        if content_length < 256 * constant.OneK:
            return None

        fs = self.file_stream(remotepath, pcs=False)
        if not fs:
            return None

        data = fs.read(256 * constant.OneK)
        assert data and len(data) == 256 * constant.OneK

        slice_md5 = calu_md5(data)

        assert (
            content_length and content_length == fs._auto_decrypt_request.content_length
        )

        content_md5 = fs._auto_decrypt_request.content_md5
        content_crc32 = fs._auto_decrypt_request.content_crc32 or 0

        if not content_md5:
            return None

        block_list = pcs_file.block_list
        if block_list and len(block_list) == 1 and block_list[0] == pcs_file.md5:
            return PcsRapidUploadInfo(
                slice_md5=slice_md5,
                content_md5=content_md5,
                content_crc32=content_crc32,
                content_length=content_length,
                remotepath=pcs_file.path,
            )

        if check:
            try:
                # Try rapid_upload_file
                self.rapid_upload_file(
                    slice_md5,
                    content_md5,
                    content_crc32,
                    content_length,
                    pcs_file.path,
                    local_ctime=pcs_file.local_ctime,
                    local_mtime=pcs_file.local_mtime,
                    ondup="overwrite",
                )
            except BaiduPCSError as err:
                # 31079: "未找到文件MD5"
                if err.error_code != 31079:
                    raise err
                return None

        return PcsRapidUploadInfo(
            slice_md5=slice_md5,
            content_md5=content_md5,
            content_crc32=content_crc32,
            content_length=content_length,
            remotepath=pcs_file.path,
        )

    def save_shared(
        self, shared_url: str, remote_dir: str, password: str | None = None
    ) -> None:
        """将分享链接 `shared_url` 中的内容转存到 `remote_dir`。

        :param shared_url: 分享链接
        :param remote_dir: 转存到的网盘目录
        :param password: 分享提取码，链接需要提取码时必须传入
        """
        shared_url = _unify_shared_url(shared_url)

        if password:
            self.access_shared(
                shared_url,
                password,
            )

        shared_paths = deque(self.shared_paths(shared_url))
        _remote_dirs: dict[PcsSharedPath, str] = dict(
            [(sp, remote_dir) for sp in shared_paths]
        )
        _dir_exists: set[str] = set()

        while shared_paths:
            shared_path = shared_paths.popleft()
            rd = _remote_dirs[shared_path]

            # Make sure remote dir exists
            if rd not in _dir_exists:
                if not self.exists(rd):
                    self.makedir(rd)
                _dir_exists.add(rd)

            if shared_path.is_file and self.remote_path_exists(
                PurePosixPath(shared_path.path).name, rd
            ):
                logger.warning(f"{shared_path.path} has be in {rd}")
                continue

            uk, share_id, bdstoken = (
                shared_path.uk,
                shared_path.share_id,
                shared_path.bdstoken,
            )

            try:
                self.transfer_shared_paths(
                    rd, [shared_path.fs_id], uk, share_id, bdstoken, shared_url
                )
                logger.info(f"save: {shared_path.path} to {rd}")
                continue
            except BaiduPCSError as err:
                if err.error_code == 12:
                    logger.warning(
                        f"error_code: {err.error_code},文件已经存在, {shared_path.path} has be in {rd}"
                    )
                elif err.error_code == -32:
                    logger.error(f"error_code:{err.error_code} 剩余空间不足，无法转存")
                elif err.error_code == -33:
                    logger.error(
                        f"error_code:{err.error_code} 一次支持操作999个，减点试试吧"
                    )
                elif err.error_code == 4:
                    logger.error(
                        f"error_code:{err.error_code} share transfer pcs error"
                    )
                elif err.error_code == 130 or err.error_code == 120:
                    logger.error(f"error_code:{err.error_code} 转存文件数超限")
                else:
                    logger.error(f"error_code:{err.error_code}:{err}")
                    raise err

            if shared_path.is_dir:
                sub_paths = self.list_all_sub_paths(
                    shared_path.path, uk, share_id, bdstoken
                )
                rd = (Path(rd) / os.path.basename(shared_path.path)).as_posix()
                for sp in sub_paths:
                    _remote_dirs[sp] = rd
                shared_paths.extendleft(sub_paths[::-1])

    def remote_path_exists(
        self, name: str, rd: str, _cache: dict[str, set[str]] = {}
    ) -> bool:
        """检查名为 `name` 的路径是否已存在于目录 `rd` 下（带内部缓存）。

        :param name: 文件/目录名
        :param rd: 网盘目录路径
        :param _cache: 内部缓存，调用方一般无需传入
        :return: 是否已存在
        """
        names = _cache.get(rd)
        if not names:
            names = set([PurePosixPath(sp.path).name for sp in self.list(rd)])
            _cache[rd] = names
        return name in names

    def list_all_sub_paths(
        self, shared_path: str, uk: int, share_id: int, bdstoken: str, size=100
    ) -> list[PcsSharedPath]:
        """分页列出分享目录 `shared_path` 下的全部子路径。

        :param shared_path: 分享目录路径
        :param uk: 分享者 uk
        :param share_id: 分享 id
        :param bdstoken: bdstoken
        :param size: 单页数量
        :return: 全部子路径的 `PcsSharedPath` 列表
        """
        sub_paths = []
        for page in range(1, 1000):
            sps = self.list_shared_paths(
                shared_path, uk, share_id, bdstoken, page=page, size=size
            )
            sub_paths += sps
            if len(sps) < 100:
                break
        return sub_paths
