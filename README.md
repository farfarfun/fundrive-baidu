# fundrive-baidu

百度网盘（BaiduPCS）Python API 封装，提供文件列表/搜索、上传下载、移动复制、
离线下载、分享与转存等常用能力，供 [fundrive](https://github.com/farfarfun/fundrive)
及其他项目以统一接口接入百度网盘。

## 安装

```bash
pip install fundrive-baidu
# 或
uv add fundrive-baidu
```

## 快速开始

使用百度账号登录后获取的 `BDUSS`（以及分享相关接口需要的 `STOKEN`）创建客户端：

```python
from fundrives.baidu import BaiduPCSApi

api = BaiduPCSApi(bduss="your-bduss", stoken="your-stoken")

# 列出网盘根目录文件
for pcs_file in api.list("/"):
    print(pcs_file.path, pcs_file.is_dir, pcs_file.size)
```

## 主要能力

- 文件列表、元数据查询、搜索
- 上传（含分片上传、秒传）、下载链接与流式下载
- 移动、复制、重命名、删除、新建目录
- 离线下载任务（普通链接、磁力链接）
- 分享链接创建/查询/取消，以及分享内容转存

## 来源说明

本项目源码来自 [PeterDing/BaiduPCS-Py](https://github.com/PeterDing/BaiduPCS-Py)
（[MIT](https://github.com/PeterDing/BaiduPCS-Py/blob/master/LICENSE) 协议，
Copyright (c) 2021 Peter Ding），在此基础上去除了命令行界面代码与部分服务端代码，
减少相关依赖，只保留可编程调用的 API 部分。

---

## 关于 farfarfun

[farfarfun](https://github.com/farfarfun) 是一个专注于实用工具库的开源组织，
涵盖云存储、数据处理、AI、多媒体与开发工具链等方向。

- 🏠 组织主页：<https://github.com/farfarfun>
- 📦 PyPI：<https://pypi.org/user/niuliangtao/>
- 📧 联系：farfarfun@qq.com

本项目基于 [MIT](LICENSE) 协议开源。
