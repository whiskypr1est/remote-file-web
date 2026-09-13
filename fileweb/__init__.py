# -*- coding: utf-8 -*-
"""
远程文件管理 Web 应用（Windows 桌面风格）
=========================================

包结构：
    fileweb.config     —— 配置文件读写、默认值、首次运行初始化
    fileweb.security   —— 口令哈希、会话令牌、路径穿越防护、文件名清洗
    fileweb.thumbs     —— 图片缩略图生成与磁盘缓存
    fileweb.office     —— Office 文档预览（LibreOffice 转 PDF / 纯 Python 降级解析）
    fileweb.fsops      —— 文件系统操作（列目录、新建、重命名、删除、上传、打包）
    fileweb.routers    —— FastAPI 路由模块
"""

__version__ = "1.0.0"
APP_NAME = "远程文件管理"
