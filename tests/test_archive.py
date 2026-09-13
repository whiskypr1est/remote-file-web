# -*- coding: utf-8 -*-
"""
压缩 / 解压接口的回归测试（只用标准库 + 内置测试工具）。

覆盖的重点是**安全边界**，不是「能不能压」：
路径穿越、解压炸弹、重名覆盖、盘根保护、外部工具缺失。
这些一旦回归就是能写文件的漏洞，所以每个都要有独立用例钉死。
"""

from __future__ import annotations

import gzip
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
import zipfile
import zlib

from tests._harness import ServerProcess
from fileweb import archive


def raw_zip(name_bytes: bytes, data: bytes, flags: int = 0) -> bytes:
    """
    手工拼一个 ZIP 文件。

    为什么不用 zipfile 写：它见到非 ASCII 名字就会**强制**置上 UTF-8 标志位
    （_encodeFilenameFlags 先试 ASCII，失败就 UTF-8 + 0x800），
    这样就造不出「Windows 资源管理器」那种「GBK 字节 + 不置位」的真实样本，
    也就测不到中文乱码修复。flags 参数则用来伪造「加密位」。
    """
    crc = zlib.crc32(data) & 0xffffffff
    n, m = len(name_bytes), len(data)
    lfh = struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, flags, 0, 0, 0,
                      crc, m, m, n, 0) + name_bytes
    cdh = struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, flags, 0, 0, 0,
                      crc, m, m, n, 0, 0, 0, 0, 0, 0) + name_bytes
    eocd = struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, 1, 1, len(cdh), len(lfh) + m, 0)
    return lfh + data + cdh + eocd


class ArchiveApiTests(unittest.TestCase):
    """正常流程 + 重名 + 格式识别。"""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="fw-arch-")
        cls.server = ServerProcess(
            [{"id": "main", "name": "main", "path": cls.root, "readonly": False}],
        )
        cls.server.start()
        cls.client = cls.server.login_client()

        os.makedirs(os.path.join(cls.root, "docs", "sub"))
        with open(os.path.join(cls.root, "docs", "a.txt"), "w", encoding="utf-8") as fh:
            fh.write("hello A")
        with open(os.path.join(cls.root, "docs", "中文名.txt"), "w", encoding="utf-8") as fh:
            fh.write("中文内容")
        with open(os.path.join(cls.root, "docs", "sub", "b.bin"), "wb") as fh:
            fh.write(b"\x00\x01" * 500)
        os.makedirs(os.path.join(cls.root, "out"))
        os.makedirs(os.path.join(cls.root, "restore"))

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.server.cleanup()
        shutil.rmtree(cls.root, ignore_errors=True)

    def path(self, *parts) -> str:
        return os.path.join(self.root, *parts)

    # -- 压缩 ---------------------------------------------------------------

    def test_compress_zip_then_extract_roundtrip(self):
        status, res = self.client.json("POST", "/api/fs/compress", {
            "root": "main", "paths": ["docs"],
            "target_root": "main", "target_path": "out", "name": "打包.zip",
        })
        self.assertEqual(status, 200, res)
        body = res
        self.assertTrue(body["ok"])
        self.assertEqual(body["format"], "zip")
        self.assertEqual(body["file_count"], 3)
        self.assertFalse(body["renamed"])
        self.assertTrue(os.path.isfile(self.path("out", "打包.zip")))

        status, res = self.client.json("POST", "/api/fs/extract", {
            "root": "main", "path": "out/打包.zip",
            "target_root": "main", "target_path": "restore",
        })
        self.assertEqual(status, 200, res)
        self.assertEqual(res["extracted"], 3)

        with open(self.path("restore", "docs", "a.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "hello A")
        # 中文名在包内包外都要正确（顺带验证没有触发乱码修复的误判）
        with open(self.path("restore", "docs", "中文名.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "中文内容")
        self.assertEqual(os.path.getsize(self.path("restore", "docs", "sub", "b.bin")), 1000)

    def test_compress_auto_renames_instead_of_overwriting(self):
        status, first = self.client.json("POST", "/api/fs/compress", {
            "root": "main", "paths": ["docs"],
            "target_root": "main", "target_path": "out", "name": "重复.zip",
        })
        self.assertEqual(status, 200, first)
        self.assertEqual(first["name"], "重复.zip")

        status, second = self.client.json("POST", "/api/fs/compress", {
            "root": "main", "paths": ["docs"],
            "target_root": "main", "target_path": "out", "name": "重复.zip",
        })
        self.assertEqual(status, 200, second)
        self.assertTrue(second["renamed"])
        self.assertEqual(second["name"], "重复 (2).zip")
        # 两个文件都要在，第一个不能被覆盖掉
        self.assertTrue(os.path.isfile(self.path("out", "重复.zip")))
        self.assertTrue(os.path.isfile(self.path("out", "重复 (2).zip")))

    def test_compress_tar_gz_and_7z(self):
        for name, fmt in (("归档.tar.gz", "tar"), ("归档.7z", "7z")):
            with self.subTest(name=name):
                status, res = self.client.json("POST", "/api/fs/compress", {
                    "root": "main", "paths": ["docs", ],
                    "target_root": "main", "target_path": "out",
                    "name": name, "format": fmt,
                })
                self.assertEqual(status, 200, res)
                self.assertTrue(os.path.getsize(self.path("out", name)) > 0)

                # 解压目标目录得自己先建好（接口不负责创建不存在的目标）
                dest = "restore_" + fmt
                os.makedirs(self.path(dest), exist_ok=True)
                status, res = self.client.json("POST", "/api/fs/extract", {
                    "root": "main", "path": "out/" + name,
                    "target_root": "main", "target_path": dest,
                })
                self.assertEqual(status, 200, res)
                self.assertEqual(res["extracted"], 3)
                with open(self.path(dest, "docs", "中文名.txt"), encoding="utf-8") as fh:
                    self.assertEqual(fh.read(), "中文内容")

    def test_compress_does_not_follow_directory_junctions(self):
        """
        打包不能跟进目录联接（junction）。

        这条防线很容易被漏掉：Windows 上 os.path.islink() 对**目录联接**返回
        False，所以「跳过符号链接」那种写法在这里完全不起作用，
        而 os.walk 会把联接当成普通目录走进去 —— 结果就是把联接指向的
        外部内容一起装进包里。这个用例就是钉死这一点。
        """
        outside = os.path.join(self.root, "outside_secret")
        os.makedirs(outside, exist_ok=True)
        with open(os.path.join(outside, "SECRET.txt"), "w", encoding="utf-8") as fh:
            fh.write("SECRET")

        linked = os.path.join(self.root, "docs", "linked")
        # 先确保没有同名项，否则 mklink 会失败
        if os.path.isdir(linked):
            os.rmdir(linked)

        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", linked, outside],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        if created.returncode != 0 or not os.path.isdir(linked):
            self.skipTest("无法创建目录联接（需要 Windows / mklink 权限）")

        # 清理必须用 os.rmdir：它只删联接本身，不会递归删掉目标目录里的内容
        def _unlink():
            try:
                if os.path.isdir(linked) and not os.path.islink(linked):
                    os.rmdir(linked)
            except OSError:
                pass
        self.addCleanup(_unlink)

        status, res = self.client.json("POST", "/api/fs/compress", {
            "root": "main", "paths": ["docs"],
            "target_root": "main", "target_path": "out", "name": "联接.zip",
        })
        self.assertEqual(status, 200, res)

        with zipfile.ZipFile(self.path("out", "联接.zip")) as zf:
            names = zf.namelist()
        # 外部机密不能进包，正常文件要照常在包里
        self.assertFalse([n for n in names if "SECRET" in n], names)
        self.assertTrue([n for n in names if n.endswith("a.txt")], names)
        # 跳过了什么必须如实告诉用户
        self.assertTrue(any("联接" in item for item in res["skipped"]), res["skipped"])

        # RAR 走的是外部程序，它有自己的遍历逻辑：光靠我们剪枝是不够的，
        # 还得给 Rar.exe 加 -ol 才会「存链接而不是展开链接」。
        # 上面 zip 那套断言挡不住这个回归，所以单独再验一次。
        rar_tool, unrar_tool = archive.find_rar_tools()
        if not (rar_tool and unrar_tool):
            return
        status, res = self.client.json("POST", "/api/fs/compress", {
            "root": "main", "paths": ["docs"],
            "target_root": "main", "target_path": "out", "name": "联接.rar",
        })
        self.assertEqual(status, 200, res)
        listing = subprocess.run(
            [unrar_tool, "lb", self.path("out", "联接.rar")],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        ).stdout.decode("utf-8", "replace")
        self.assertNotIn("SECRET", listing, listing)
        self.assertIn("a.txt", listing, listing)

    def test_created_archive_uses_real_compression_for_every_suffix(self):
        """
        后缀必须决定**真正的**压缩方式，这里看的是文件头魔数，不是文件名。

        这条路径上出现过两个真实缺陷，都是「名字说是压缩包、内容却是裸 tar」：
          1. 「备份.gz」落到 mode="w"，产出没压缩的 tar 却叫 .gz，任何工具都打不开；
          2. 修第 1 条时把 .tgz/.tbz2/.txz 漏了 —— 它们**并不以** .gz/.bz2/.xz 结尾
             （结尾是 tgz/tbz2/txz），于是同样退化成没压缩的 tar。
        所以这里把 10 种后缀全测一遍：只看后缀的写法挡不住第 2 类问题。
        """
        variants = [
            ("打包.gz", "gzip"), ("打包.tar.gz", "gzip"), ("打包.tgz", "gzip"),
            ("打包.bz2", "bzip2"), ("打包.tar.bz2", "bzip2"), ("打包.tbz2", "bzip2"),
            ("打包.xz", "xz"), ("打包.tar.xz", "xz"), ("打包.txz", "xz"),
            ("打包.tar", "plain"),
        ]
        magics = ((b"\x1f\x8b", "gzip"), (b"BZh", "bzip2"), (b"\xfd7zXZ\x00", "xz"))

        def magic_of(path):
            with open(path, "rb") as fh:
                head = fh.read(6)
            for magic, label in magics:
                if head.startswith(magic):
                    return label
            return "plain"

        for index, (name, want) in enumerate(variants):
            with self.subTest(name=name):
                status, res = self.client.json("POST", "/api/fs/compress", {
                    "root": "main", "paths": ["docs"],
                    "target_root": "main", "target_path": "out", "name": name,
                })
                self.assertEqual(status, 200, res)
                self.assertEqual(res["format"], "tar")
                # 名字说是压缩包，内容就必须真的是那种压缩流
                self.assertEqual(magic_of(self.path("out", name)), want,
                                 "%s 的内容不是 %s" % (name, want))

                # 而且要能正常解回来
                dest = "suffix_out_%d" % index
                os.makedirs(self.path(dest), exist_ok=True)
                status, res = self.client.json("POST", "/api/fs/extract", {
                    "root": "main", "path": "out/" + name,
                    "target_root": "main", "target_path": dest,
                })
                self.assertEqual(status, 200, res)
                self.assertTrue(os.path.isfile(self.path(dest, "docs", "a.txt")))

    def test_compress_default_name_when_blank(self):
        status, res = self.client.json("POST", "/api/fs/compress", {
            "root": "main", "paths": ["docs"],
            "target_root": "main", "target_path": "out", "name": "",
            "format": "zip",
        })
        self.assertEqual(status, 200, res)
        name = res["name"]
        self.assertTrue(name.endswith(".zip"), name)

    def test_compress_single_file_keeps_it_at_top_level(self):
        """只选一个文件时，包内应该是这个名字本身，而不是被套一层目录。"""
        with open(self.path("solo.txt"), "w", encoding="utf-8") as fh:
            fh.write("solo")
        status, res = self.client.json("POST", "/api/fs/compress", {
            "root": "main", "paths": ["solo.txt"],
            "target_root": "main", "target_path": "out", "name": "单文件.zip",
        })
        self.assertEqual(status, 200, res)
        self.assertEqual(res["file_count"], 1)

        with zipfile.ZipFile(self.path("out", "单文件.zip")) as zf:
            self.assertEqual(zf.namelist(), ["solo.txt"])

    def test_compress_and_extract_rar_when_available(self):
        """
        RAR 依赖 WinRAR 的命令行工具，没装就跳过（而不是失败）。

        Rar.exe 的参数很容易写错（-r / -ep1 组合、cwd 与相对名的关系），
        所以这里连「包内只有选中的那个文件」一起断言，防止把整个目录打进去。
        """
        rar_tool, unrar_tool = archive.find_rar_tools()
        if not (rar_tool and unrar_tool):
            self.skipTest("本机没有 WinRAR 的 Rar.exe / UnRAR.exe")

        status, res = self.client.json("POST", "/api/fs/compress", {
            "root": "main", "paths": ["docs"],
            "target_root": "main", "target_path": "out", "name": "归档.rar",
        })
        self.assertEqual(status, 200, res)
        self.assertEqual(res["format"], "rar")

        os.makedirs(self.path("restore_rar"), exist_ok=True)
        status, res = self.client.json("POST", "/api/fs/extract", {
            "root": "main", "path": "out/归档.rar",
            "target_root": "main", "target_path": "restore_rar",
        })
        self.assertEqual(status, 200, res)
        self.assertEqual(res["extracted"], 3)
        with open(self.path("restore_rar", "docs", "中文名.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "中文内容")
        # 目标目录里只应该有 docs 这一项，不能把 out 里的其他压缩包一起带出来
        self.assertEqual(sorted(os.listdir(self.path("restore_rar"))), ["docs"])

    # -- 重名 / 解压目标 -----------------------------------------------------

    def test_extract_refuses_when_target_has_same_name(self):
        """重名必须整体拒绝，不能覆盖也不能半途而废。"""
        self.client.json("POST", "/api/fs/compress", {
            "root": "main", "paths": ["docs"],
            "target_root": "main", "target_path": "out", "name": "冲突.zip",
        })
        # restore/docs 已经存在（上一个用例解压出来的），这里不清理，直接用
        status, res = self.client.json("POST", "/api/fs/extract", {
            "root": "main", "path": "out/冲突.zip",
            "target_root": "main", "target_path": "restore",
        })
        self.assertEqual(status, 409, res)
        self.assertIn("同名", res["message"])
        self.assertIn("docs", res["message"])

    def test_extract_into_dedicated_dir_succeeds(self):
        """换个空目录就应该能解，证明上面的 409 是「撞名」而不是「坏了」。"""
        self.client.json("POST", "/api/fs/compress", {
            "root": "main", "paths": ["docs"],
            "target_root": "main", "target_path": "out", "name": "冲突.zip",
        })
        os.makedirs(self.path("restore2"))
        status, res = self.client.json("POST", "/api/fs/extract", {
            "root": "main", "path": "out/冲突.zip",
            "target_root": "main", "target_path": "restore2",
        })
        self.assertEqual(status, 200, res)

    # -- 安全 ---------------------------------------------------------------

    def test_extract_rejects_zip_slip(self):
        """四种穿越写法都要被拒，且磁盘上不能出现任何越界文件。"""
        marker = os.path.join(os.path.dirname(self.root), "pwned-by-zipslip.txt")
        if os.path.exists(marker):
            os.unlink(marker)

        evil_names = [
            b"/absolute.txt",
            b"C:/drive.txt",
            b"../../../pwned-by-zipslip.txt",
            b"..\\..\\..\\pwned-by-zipslip.txt",
        ]
        for index, raw_name in enumerate(evil_names):
            with self.subTest(name=raw_name):
                target = os.path.join(self.root, "evil%d.zip" % index)
                with open(target, "wb") as fh:
                    fh.write(raw_zip(raw_name, b"pwned"))
                os.makedirs(self.path("evilout%d" % index), exist_ok=True)

                status, res = self.client.json("POST", "/api/fs/extract", {
                    "root": "main", "path": "evil%d.zip" % index,
                    "target_root": "main", "target_path": "evilout%d" % index,
                })
                self.assertEqual(status, 400, res)
                self.assertFalse(os.path.exists(marker))

        # 目标目录里也不该留下任何东西
        for index in range(len(evil_names)):
            self.assertEqual(os.listdir(self.path("evilout%d" % index)), [])

    def test_extract_rejects_7z_path_traversal(self):
        """
        7z 走的是「先整包解到暂存目录、再按校验过的名字搬运」，
        所以穿越防护只能靠解压**之前**逐个校验条目名。这条用例钉住那个前置校验。

        （绝对路径不用测：py7zr 在写入时就会把 /abs.txt 归一成 abs.txt，
        包里存的本来就是安全的相对名。）
        """
        try:
            import py7zr
        except ImportError:                      # pragma: no cover
            self.skipTest("没有安装 py7zr")

        payload = os.path.join(self.root, "payload.txt")
        with open(payload, "w", encoding="utf-8") as fh:
            fh.write("pwned")
        target = os.path.join(self.root, "evil7z.7z")
        try:
            with py7zr.SevenZipFile(target, "w") as zf:
                zf.write(payload, arcname="../../evil7z.txt")
        except Exception:                        # noqa: BLE001 - 版本差异，造不出来就跳过
            self.skipTest("当前 py7zr 版本不允许写入带 .. 的条目名")

        os.makedirs(self.path("evil7z_out"), exist_ok=True)
        status, res = self.client.json("POST", "/api/fs/extract", {
            "root": "main", "path": "evil7z.7z",
            "target_root": "main", "target_path": "evil7z_out",
        })
        self.assertEqual(status, 400, res)
        self.assertEqual(os.listdir(self.path("evil7z_out")), [])
        # 任何可能被穿越写到的地方都不该出现这个文件
        self.assertFalse(os.path.exists(os.path.join(self.root, "evil7z.txt")))

    def test_extract_single_file_gzip_stream(self):
        """
        裸 .gz 是「一个文件的压缩流」，不是 tar —— 日志轮转出来的 app.log.gz 就是这种。

        它以前会让 tarfile 抛 ReadError，而那个异常不是 ArchiveError，
        一路冒到路由层就成了 500，用户看到的只有「服务器内部错误」。
        现在要解成 app.log。
        """
        payload = b"2024-01-01 boot ok\n" * 100
        with gzip.open(self.path("app.log.gz"), "wb") as fh:
            fh.write(payload)
        os.makedirs(self.path("gz_out"), exist_ok=True)

        status, res = self.client.json("POST", "/api/fs/extract", {
            "root": "main", "path": "app.log.gz",
            "target_root": "main", "target_path": "gz_out",
        })
        self.assertEqual(status, 200, res)
        self.assertEqual(res["format"], "gzip")
        self.assertEqual(res["extracted"], 1)
        with open(self.path("gz_out", "app.log"), "rb") as fh:
            self.assertEqual(fh.read(), payload)

    def test_corrupt_archive_is_400_not_500(self):
        """后缀说是 tar 但内容不是：必须给明确的 400，不能是 500。"""
        with open(self.path("broken.tar.gz"), "wb") as fh:
            fh.write(b"definitely not a gzip stream")
        os.makedirs(self.path("broken_out"), exist_ok=True)

        status, res = self.client.json("POST", "/api/fs/extract", {
            "root": "main", "path": "broken.tar.gz",
            "target_root": "main", "target_path": "broken_out",
        })
        self.assertEqual(status, 400, res)
        self.assertIn("TAR", res["message"])
        self.assertEqual(os.listdir(self.path("broken_out")), [])

    def test_extract_repairs_gbk_zip_filenames(self):
        """Windows 资源管理器压出来的中文名 zip：解压后名字要正确。"""
        target = os.path.join(self.root, "gbk.zip")
        with open(target, "wb") as fh:
            fh.write(raw_zip("中文文件.txt".encode("gbk"), "内容".encode("utf-8")))
        os.makedirs(self.path("gbkout"))
        status, res = self.client.json("POST", "/api/fs/extract", {
            "root": "main", "path": "gbk.zip",
            "target_root": "main", "target_path": "gbkout",
        })
        self.assertEqual(status, 200, res)
        self.assertTrue(os.path.isfile(self.path("gbkout", "中文文件.txt")))
        with open(self.path("gbkout", "中文文件.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "内容")

    def test_extract_skips_symlink_entries(self):
        """符号链接条目必须跳过：否则等于给了个写任意链接的跳板。"""
        target = os.path.join(self.root, "link.zip")
        with zipfile.ZipFile(target, "w") as zf:
            info = zipfile.ZipInfo("link")
            info.external_attr = 0o120777 << 16        # S_IFLNK | 0777
            zf.writestr(info, "/etc/passwd")
            zf.writestr("real.txt", "ok")
        os.makedirs(self.path("linkout"))
        status, res = self.client.json("POST", "/api/fs/extract", {
            "root": "main", "path": "link.zip",
            "target_root": "main", "target_path": "linkout",
        })
        self.assertEqual(status, 200, res)
        body = res
        self.assertEqual(body["extracted"], 1)
        self.assertEqual(len(body["skipped"]), 1)
        self.assertFalse(os.path.lexists(self.path("linkout", "link")))

    def test_extract_rejects_encrypted_zip_with_clear_message(self):
        """
        带密码的压缩包要给明确提示，不能变成 500。

        真实场景：用户手里有加密 zip，点解压却只看到「服务器内部错误」，
        完全不知道是密码的问题。这里伪造加密位（flag 0x1）来钉住这条分支。
        """
        target = os.path.join(self.root, "enc.zip")
        with open(target, "wb") as fh:
            fh.write(raw_zip(b"secret.txt", b"data", flags=0x1))
        os.makedirs(self.path("enc_out"), exist_ok=True)

        status, res = self.client.json("POST", "/api/fs/extract", {
            "root": "main", "path": "enc.zip",
            "target_root": "main", "target_path": "enc_out",
        })
        self.assertEqual(status, 400, res)
        self.assertIn("加密", res["message"])
        # 被拒之后不该留下任何文件
        self.assertEqual(os.listdir(self.path("enc_out")), [])

    def test_compress_multiple_selected_items(self):
        """多选压缩：每个选中项各自作为包内的顶层名字（不套一层公共父目录）。"""
        for name in ("one.txt", "two.txt"):
            with open(self.path(name), "w", encoding="utf-8") as fh:
                fh.write(name)

        status, res = self.client.json("POST", "/api/fs/compress", {
            "root": "main", "paths": ["one.txt", "two.txt"],
            "target_root": "main", "target_path": "out", "name": "多选.zip",
        })
        self.assertEqual(status, 200, res)
        self.assertEqual(res["file_count"], 2)

        with zipfile.ZipFile(self.path("out", "多选.zip")) as zf:
            self.assertEqual(sorted(zf.namelist()), ["one.txt", "two.txt"])

    def test_extract_overwrite_true_is_opt_in(self):
        """
        默认拒绝重名，但 overwrite=true 时允许覆盖。

        这条同时是在确认那个「逃生舱」真的接通了 —— 一个写着却失效的参数
        比没有这个参数更糟：调用方以为覆盖成功了，其实什么都没发生。
        """
        os.makedirs(self.path("ow"), exist_ok=True)
        status, res = self.client.json("POST", "/api/fs/compress", {
            "root": "main", "paths": ["docs"],
            "target_root": "main", "target_path": "out", "name": "覆盖.zip",
        })
        self.assertEqual(status, 200, res)

        # 第一次：目录是空的，正常解压
        status, res = self.client.json("POST", "/api/fs/extract", {
            "root": "main", "path": "out/覆盖.zip",
            "target_root": "main", "target_path": "ow",
        })
        self.assertEqual(status, 200, res)

        # 第二次：默认策略是整体拒绝
        status, res = self.client.json("POST", "/api/fs/extract", {
            "root": "main", "path": "out/覆盖.zip",
            "target_root": "main", "target_path": "ow",
        })
        self.assertEqual(status, 409, res)

        # 显式要求覆盖时才放行，且内容确实被写进去了
        status, res = self.client.json("POST", "/api/fs/extract", {
            "root": "main", "path": "out/覆盖.zip",
            "target_root": "main", "target_path": "ow", "overwrite": True,
        })
        self.assertEqual(status, 200, res)
        with open(self.path("ow", "docs", "a.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "hello A")

    def test_extract_unknown_format_is_400(self):
        with open(self.path("plain.txt"), "w", encoding="utf-8") as fh:
            fh.write("not an archive")
        status, res = self.client.json("POST", "/api/fs/extract", {
            "root": "main", "path": "plain.txt",
            "target_root": "main", "target_path": "restore",
        })
        self.assertEqual(status, 400, res)
        self.assertIn("无法识别", res["message"])

    def test_extract_missing_archive_is_404(self):
        status, res = self.client.json("POST", "/api/fs/extract", {
            "root": "main", "path": "没有这个.zip",
            "target_root": "main", "target_path": "restore",
        })
        self.assertEqual(status, 404, res)

    def test_compress_cannot_pack_the_root_itself(self):
        """paths=[""] 会解析成盘根；打包整个盘是误操作，必须拦住。"""
        status, res = self.client.json("POST", "/api/fs/compress", {
            "root": "main", "paths": [""],
            "target_root": "main", "target_path": "out", "name": "整盘.zip",
        })
        # _ensure_not_root 抛的是 403（「不允许对这个位置做此操作」）
        self.assertEqual(status, 403, res)
        self.assertFalse(os.path.isfile(self.path("out", "整盘.zip")))

    def test_compress_rejects_readonly_root(self):
        """只读根目录不能写出压缩包。"""
        server = ServerProcess([
            {"id": "main", "name": "main", "path": self.root, "readonly": False},
            {"id": "ro", "name": "ro", "path": self.root, "readonly": True},
        ])
        server.start()
        try:
            client = server.login_client()
            status, res = client.json("POST", "/api/fs/compress", {
                "root": "main", "paths": ["docs"],
                "target_root": "ro", "target_path": "", "name": "只读.zip",
            })
            self.assertEqual(status, 403, res)
            self.assertFalse(os.path.isfile(self.path("只读.zip")))
        finally:
            server.stop()
            server.cleanup()

    def test_archive_endpoints_require_csrf(self):
        for path, body in (
            ("/api/fs/compress", {"root": "main", "paths": ["docs"], "name": "x.zip"}),
            ("/api/fs/extract", {"root": "main", "path": "x.zip"}),
        ):
            with self.subTest(path=path):
                status, text = self.client.request("POST", path, body, with_csrf=False)
                self.assertEqual(status, 403, text[:200])


class ArchiveProtectedPathTests(unittest.TestCase):
    """
    受保护路径（protected_paths）在压缩/解压里的正确边界。

    语义是「**允许浏览、禁止修改**」，所以关键是分清「读」和「写」：
      * 压缩**源**在受保护目录里 —— 只是读，应当允许（与打包下载一致）；
      * 把压缩产物**写进**受保护目录 —— 属于修改，必须拒绝；
      * 解压**目标**是受保护目录 —— 同样必须拒绝。

    这里曾经把源也一起拦了：那样 C:\\Program Files 下的东西根本压不了，
    而同一个目录用「打包下载」却能下载 —— 两个入口行为不一致。
    """

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="fw-arch-prot-")
        cls.guarded = os.path.join(cls.root, "guarded")

        def protect(cfg):
            # 受保护路径是「绝对路径字符串」的列表
            cfg["protected_paths"] = [cls.guarded]

        cls.server = ServerProcess(
            [{"id": "main", "name": "main", "path": cls.root, "readonly": False}],
            extra_config=protect,
        )
        cls.server.start()
        cls.client = cls.server.login_client()

        os.makedirs(os.path.join(cls.guarded, "inner"))
        with open(os.path.join(cls.guarded, "inner", "data.txt"),
                  "w", encoding="utf-8") as fh:
            fh.write("guarded")
        os.makedirs(os.path.join(cls.root, "plain"))
        with open(os.path.join(cls.root, "plain", "a.txt"),
                  "w", encoding="utf-8") as fh:
            fh.write("plain")
        os.makedirs(os.path.join(cls.root, "out"))

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.server.cleanup()
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_compress_from_protected_source_is_allowed(self):
        """压缩源在受保护目录里：只读，应当允许。"""
        res = self.client.json("POST", "/api/fs/compress", {
            "root": "main", "paths": ["guarded"],
            "target_root": "main", "target_path": "out", "name": "受保护源.zip",
        })
        self.assertEqual(res[0], 200, res[1])
        self.assertTrue(os.path.isfile(os.path.join(self.root, "out", "受保护源.zip")))

    def test_compress_into_protected_dir_is_rejected(self):
        """产物写进受保护目录：属于修改，必须拒绝，且不能留下文件。"""
        res = self.client.json("POST", "/api/fs/compress", {
            "root": "main", "paths": ["plain"],
            "target_root": "main", "target_path": "guarded", "name": "写进去.zip",
        })
        self.assertEqual(res[0], 403, res[1])
        self.assertFalse(os.path.exists(os.path.join(self.guarded, "写进去.zip")))

    def test_extract_into_protected_dir_is_rejected(self):
        """解压到受保护目录：同样必须拒绝，且目录内容不变。"""
        self.client.json("POST", "/api/fs/compress", {
            "root": "main", "paths": ["plain"],
            "target_root": "main", "target_path": "out", "name": "待解压.zip",
        })
        before = sorted(os.listdir(self.guarded))
        res = self.client.json("POST", "/api/fs/extract", {
            "root": "main", "path": "out/待解压.zip",
            "target_root": "main", "target_path": "guarded",
        })
        self.assertEqual(res[0], 403, res[1])
        self.assertEqual(sorted(os.listdir(self.guarded)), before)


class ArchiveToolPathTests(unittest.TestCase):
    """
    archive.rar_path / archive.unrar_path 必须真的生效。

    这条曾经是**死配置**：创建 RAR 时读了 rar_path，解压时却只读环境变量
    `FW_UNRAR_PATH`，于是 config.json 里写了 unrar_path 也不管用，
    README 却明说「装在别处就填 archive.unrar_path」。
    所以这里断言的是「配了一个不存在的路径就必须明确失败」——
    如果配置被忽略、悄悄回退到自动探测，测试就会因为拿到 200 而失败。
    """

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="fw-arch-tool-")

        def bogus(cfg):
            cfg["archive"]["rar_path"] = r"C:\definitely-not-here\Rar.exe"
            cfg["archive"]["unrar_path"] = r"C:\definitely-not-here\UnRAR.exe"

        cls.server = ServerProcess(
            [{"id": "main", "name": "main", "path": cls.root, "readonly": False}],
            extra_config=bogus,
        )
        cls.server.start()
        cls.client = cls.server.login_client()

        os.makedirs(os.path.join(cls.root, "src"))
        with open(os.path.join(cls.root, "src", "a.txt"), "w", encoding="utf-8") as fh:
            fh.write("hi")
        os.makedirs(os.path.join(cls.root, "out"))

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.server.cleanup()
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_configured_bogus_rar_path_is_honoured_for_create(self):
        """配了不存在的 Rar.exe：要明确报「找不到工具」，而不是偷偷用自动探测。"""
        res = self.client.json("POST", "/api/fs/compress", {
            "root": "main", "paths": ["src"],
            "target_root": "main", "target_path": "out", "name": "x.rar",
        })
        self.assertEqual(res[0], 501, res[1])
        self.assertIn("Rar.exe", res[1]["message"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "out", "x.rar")))

    def test_configured_bogus_unrar_path_is_honoured_for_extract(self):
        """
        解压侧同样要尊重配置。

        造一个真 RAR 需要 WinRAR；没有就跳过。但即使跳过，上面的创建用例
        也已经证明了「配置能到达 archive 模块」这条链路是通的。
        """
        rar_tool, unrar_tool = archive.find_rar_tools()
        if not (rar_tool and unrar_tool):
            self.skipTest("本机没有 WinRAR，无法造出 RAR 样本")

        # 先用真实的工具造一个 rar，再让服务器（配置里指向不存在的路径）去解它
        real = os.path.join(self.root, "real.rar")
        archive.create([os.path.join(self.root, "src")], real, fmt="rar",
                       rar_path=rar_tool)
        self.assertTrue(os.path.isfile(real))

        os.makedirs(os.path.join(self.root, "unpack"), exist_ok=True)
        res = self.client.json("POST", "/api/fs/extract", {
            "root": "main", "path": "real.rar",
            "target_root": "main", "target_path": "unpack",
        })
        # 配置里的 unrar 路径不存在 -> 必须明确报错，而不是悄悄换成自动探测
        self.assertEqual(res[0], 501, res[1])
        self.assertIn("UnRAR.exe", res[1]["message"])
        self.assertEqual(os.listdir(os.path.join(self.root, "unpack")), [])


class ArchiveLimitTests(unittest.TestCase):
    """解压炸弹防线：条数与体积上限要用配置生效。"""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="fw-arch-limit-")

        def tighten(cfg):
            cfg["archive"]["max_entries"] = 5
            cfg["archive"]["max_single_mb"] = 1
            cfg["archive"]["max_total_mb"] = 2

        cls.server = ServerProcess(
            [{"id": "main", "name": "main", "path": cls.root, "readonly": False}],
            extra_config=tighten,
        )
        cls.server.start()
        cls.client = cls.server.login_client()
        os.makedirs(os.path.join(cls.root, "dest"))

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.server.cleanup()
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_too_many_entries_is_rejected(self):
        target = os.path.join(self.root, "many.zip")
        with zipfile.ZipFile(target, "w") as zf:
            for index in range(20):
                zf.writestr("f%02d.txt" % index, "x")
        status, res = self.client.json("POST", "/api/fs/extract", {
            "root": "main", "path": "many.zip",
            "target_root": "main", "target_path": "dest",
        })
        self.assertEqual(status, 400, res)
        self.assertIn("上限", res["message"])

    def test_oversized_single_entry_is_rejected(self):
        target = os.path.join(self.root, "bomb.zip")
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("big.bin", b"\x00" * (3 * 1024 * 1024))
        status, res = self.client.json("POST", "/api/fs/extract", {
            "root": "main", "path": "bomb.zip",
            "target_root": "main", "target_path": "dest",
        })
        self.assertEqual(status, 400, res)
        self.assertIn("单个文件", res["message"])
        # 被拒之后磁盘上不能留下半个文件
        self.assertEqual(os.listdir(os.path.join(self.root, "dest")), [])


class DiskSpaceGuardTests(unittest.TestCase):
    """
    确认压缩/解压的落盘前检查确实复用了 fsops.check_disk_space。

    这条容易被改坏：check_disk_space 抛的是 OSError，如果忘了翻译成
    HTTPException，用户看到的就是 500 而不是「空间不足」的 507。
    另外它自带 64MB 余量，自己重写一份很容易把这个细节漏掉。
    """

    def test_low_space_raises_507_not_500(self):
        from fastapi import HTTPException
        from fileweb.routers import fs as fs_router

        with tempfile.TemporaryDirectory() as tmp:
            # 要一个天文数字，必定判定为空间不足
            with self.assertRaises(HTTPException) as ctx:
                fs_router._ensure_enough_space(tmp, 10 ** 15, "创建压缩包")
        self.assertEqual(ctx.exception.status_code, 507)
        self.assertIn("空间不足", ctx.exception.detail)
        self.assertIn("创建压缩包", ctx.exception.detail)

    def test_small_need_is_allowed(self):
        from fastapi import HTTPException
        from fileweb.routers import fs as fs_router

        with tempfile.TemporaryDirectory() as tmp:
            try:
                fs_router._ensure_enough_space(tmp, 1024, "解压")
            except HTTPException as exc:            # pragma: no cover - 只会在磁盘真满时触发
                self.fail("1KB 的写入不该被空间检查拦下：%s" % exc.detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
