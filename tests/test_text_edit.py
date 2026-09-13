# -*- coding: utf-8 -*-
"""
文本编辑（GET/POST /api/fs/text）的回归测试。

重点不是「能不能存」，而是**存错会毁数据**的那几种情况：

    * 把截断的预览写回文件（100MB 日志被悄悄截成 2MB）
    * 覆盖掉别人在磁盘上的改动（乐观并发失效）
    * 写到一半失败，留下半截文件或残留临时文件
    * GBK / BOM / CRLF 的文件存一次就变了字节（乱码、结构被改）

只用标准库，服务端跑真实子进程（见 tests/_harness.py）。
"""

from __future__ import annotations

import codecs
import os
import shutil
import tempfile
import time
import unittest

from tests._harness import ServerProcess, Client
from fileweb.routers.content import _decode_text, _detect_bom, _detect_newline

# 可编辑上限。config.prepare() 会把 preview.text_max_kb 夹到 >=16，
# 所以这里就用允许的最小值，边界文件才不用造几十 MB。
TEXT_MAX_KB = 16
TEXT_MAX_BYTES = TEXT_MAX_KB * 1024

# 原子写入用的临时文件前缀，必须和 content.py 里保持一致
TMP_PREFIX = ".fileweb-text-"


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _write_bytes(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


def _temp_leftovers(directory: str) -> list:
    """列出目录里残留的写入临时文件（正常情况必须为空）。"""
    return [name for name in os.listdir(directory) if name.startswith(TMP_PREFIX)]


class TextEditBase(unittest.TestCase):
    """公共装置：一份真实服务器 + 三个临时根目录。"""

    @classmethod
    def setUpClass(cls):
        # 三个根目录，用途不同，便于把各条 403 路径单独钉死：
        #   main     —— 可写，绝大部分用例在这里
        #   ro       —— 只读根：测 _ensure_writable
        #   guarded  —— 可写但被 protected_paths 覆盖：单独测 _ensure_not_protected
        cls.root = tempfile.mkdtemp(prefix="fw-text-")
        cls.readonly_root = tempfile.mkdtemp(prefix="fw-text-ro-")
        cls.guarded_root = tempfile.mkdtemp(prefix="fw-text-guarded-")

        def _config(cfg):
            cfg["preview"]["text_max_kb"] = TEXT_MAX_KB
            cfg["protected_paths"] = [cls.guarded_root]

        cls.server = ServerProcess(
            [
                {"id": "main", "name": "main", "path": cls.root, "readonly": False},
                {"id": "ro", "name": "ro", "path": cls.readonly_root, "readonly": True},
                {"id": "guarded", "name": "guarded", "path": cls.guarded_root,
                 "readonly": False},
            ],
            extra_config=_config,
        )
        cls.server.start()
        cls.client = cls.server.login_client()

    @classmethod
    def tearDownClass(cls):
        cls.server.cleanup()
        for directory in (cls.root, cls.readonly_root, cls.guarded_root):
            shutil.rmtree(directory, ignore_errors=True)

    # -- 工具 ---------------------------------------------------------------

    def path(self, *parts) -> str:
        return os.path.join(self.root, *parts)

    def get_text(self, rel, root="main"):
        status, body = self.client.json(
            "GET", "/api/fs/text?root=%s&path=%s" % (root, rel))
        self.assertEqual(status, 200, body)
        return body

    def save(self, rel, text, meta, root="main", **overrides):
        """
        按 GET 回报的元信息回传一次保存请求。

        meta 就是 GET 的响应体；overrides 用来故意改错某一项（例如塞一个
        过期的 base_mtime）以测冲突分支。
        """
        payload = {
            "root": root,
            "path": rel,
            "text": text,
            "encoding": meta["encoding"],
            "bom": meta["bom"],
            "newline": meta["newline"],
            "base_mtime": meta["mtime"],
            "base_size": meta["size"],
        }
        payload.update(overrides)
        return self.client.json("POST", "/api/fs/text", payload)

    def roundtrip(self, rel, root="main", new_text=None):
        """
        GET -> POST -> 返回 (保存响应体, 磁盘上的新字节)。

        new_text 为 None 表示「原样保存」（不改一个字符）。
        """
        meta = self.get_text(rel, root=root)
        text = meta["content"] if new_text is None else new_text
        status, body = self.save(rel, text, meta, root=root)
        self.assertEqual(status, 200, body)
        return body, _read_bytes(self.path(rel))


# ---------------------------------------------------------------------------
# 字节形态往返
# ---------------------------------------------------------------------------

class TextRoundTripTests(TextEditBase):
    """编码 / BOM / 换行符的忠实往返：原样保存 == 字节不变。"""

    def test_utf8_chinese_roundtrip_is_byte_identical(self):
        rel = "utf8_cn.txt"
        original = "第一行：中文测试\n第二行：tab\t与空格\n第三行：end\n"
        _write_bytes(self.path(rel), original.encode("utf-8"))

        body, after = self.roundtrip(rel)

        self.assertEqual(after, original.encode("utf-8"))
        self.assertEqual(body["size"], len(original.encode("utf-8")))
        self.assertTrue(body["ok"])
        # 返回体里的 size_text / mtime 要和磁盘对得上
        self.assertIsInstance(body["mtime"], float)
        self.assertAlmostEqual(body["mtime"],
                               os.path.getmtime(self.path(rel)), delta=1.0)

    def test_gbk_roundtrip_stays_valid_gbk(self):
        """GBK 中文文件：读出来 → 原样写回 → 仍是合法 GBK，无乱码。"""
        rel = "gbk_cn.txt"
        original = "简体中文内容：测试一二三四五。\n第二行：这是GBK编码的文件。\n"
        raw = original.encode("gbk")
        _write_bytes(self.path(rel), raw)

        meta = self.get_text(rel)
        # 解码器应识别为 gb18030（GBK 的超集）
        self.assertEqual(meta["encoding"], "gb18030")
        self.assertFalse(meta["bom"])
        # 内容必须没有乱码：解出来就是原文
        self.assertEqual(meta["content"], original)

        status, body = self.save(rel, meta["content"], meta)
        self.assertEqual(status, 200, body)

        after = _read_bytes(self.path(rel))
        # 逐字节一致 —— 说明没被悄悄转成 UTF-8
        self.assertEqual(after, raw)
        # 仍然能被 GBK 解出同样的中文（没有 mojibake）
        self.assertEqual(after.decode("gbk"), original)
        self.assertNotIn("\ufffd", after.decode("gbk"))

    def test_gbk_with_crlf_roundtrips_byte_identical(self):
        """
        ★ GBK + CRLF 叠加时必须逐字节往返。

        这是最容易翻车的组合，因为它要求三件事同时成立：
        解码路径保留 \\r\\n（GET 必须用 newline="" 解除通用换行折叠）、
        换行探测认出 CRLF 而不是裸 CR、写回时再按 CRLF 还原。

        为什么之前的用例没拦住：GBK 那条用的是纯 LF，CRLF 那条用的是 UTF-8，
        两个特性各自的用例都是绿的，**叠加**起来才是坏的那个 ——
        一旦 CRLF 被读成裸 CR，_apply_newline 会把每个 \\n 换成 \\r，
        结果是所有 \\r\\n 都变成孤立的 \\r，整个文件的换行全毁。
        """
        rel = "gbk_crlf.txt"
        original = "中文内容测试\r\n第二行\r\n"
        raw = original.encode("gbk")
        _write_bytes(self.path(rel), raw)

        meta = self.get_text(rel)
        self.assertEqual(meta["encoding"], "gb18030")
        self.assertEqual(meta["newline"], "\r\n", "CRLF 必须识别成 CRLF，而不是裸 CR")
        # content 保留 \\r\\n，正好证明 GET 是按 newline="" 解码的
        self.assertEqual(meta["content"], original)
        self.assertIn("\r\n", meta["content"])

        status, body = self.save(rel, meta["content"], meta)
        self.assertEqual(status, 200, body)

        after = _read_bytes(self.path(rel))
        # ★ 逐字节一致：不是「解出来看起来对」，而是磁盘上一个字节都没变
        self.assertEqual(after, raw, "GBK+CRLF 文件保存后必须逐字节不变")
        self.assertEqual(after.decode("gbk"), original)
        self.assertEqual(after.count(b"\r\n"), 2)
        # 不存在孤立的 \r（裸 CR 个数必须正好等于 CRLF 个数）
        self.assertEqual(after.count(b"\r"), after.count(b"\r\n"))

    def test_batch_file_crlf_roundtrip_is_byte_identical(self):
        """
        ★ .bat 这类 CRLF 文本存一次之后必须还是 CRLF。

        这是本功能破坏力最大的回归：CRLF 被写成裸 CR 后，多数编辑器会把
        整个文件显示成一行，而 .bat 会直接不再执行 —— 用户只是「保存」了
        一次，文件就坏了，而且看起来像是编辑器弄坏的。
        所以这里除了字节相等，还直接钉「裸 CR 数 == CRLF 数」。
        """
        rel = "run.bat"
        original = "@echo off\r\necho hello\r\necho world\r\n"
        raw = original.encode("utf-8")
        _write_bytes(self.path(rel), raw)

        meta = self.get_text(rel)
        self.assertEqual(meta["newline"], "\r\n")

        status, body = self.save(rel, meta["content"], meta)
        self.assertEqual(status, 200, body)

        after = _read_bytes(self.path(rel))
        self.assertEqual(after, raw, ".bat 文件保存后字节必须完全不变")
        self.assertEqual(after.count(b"\r"), after.count(b"\r\n"),
                         "不能出现孤立 \\r（那样 .bat 会变成一行而无法执行）")

    def test_utf8_bom_preserved(self):
        rel = "bom.txt"
        original = "带 BOM 的 UTF-8 文件\n中文内容\n"
        raw = codecs.BOM_UTF8 + original.encode("utf-8")
        _write_bytes(self.path(rel), raw)

        meta = self.get_text(rel)
        self.assertTrue(meta["bom"], "GET 必须报告 BOM 存在")
        self.assertEqual(meta["encoding"], "utf-8-sig")
        self.assertEqual(meta["content"], original)

        _body, after = self.roundtrip(rel)

        self.assertTrue(after.startswith(codecs.BOM_UTF8), "BOM 不能丢")
        self.assertEqual(after, raw)
        # 而且不能出现两个 BOM（重复加 BOM 是经典翻车点）
        self.assertFalse(after[3:].startswith(codecs.BOM_UTF8))

    def test_crlf_preserved_and_lf_stays_lf(self):
        # CRLF 文件
        crlf_rel = "crlf.txt"
        crlf_original = "一行\r\n二行\r\n三行\r\n"
        _write_bytes(self.path(crlf_rel), crlf_original.encode("utf-8"))

        meta = self.get_text(crlf_rel)
        self.assertEqual(meta["newline"], "\r\n", "GET 必须报告 CRLF")
        # content 是文件的**原样**内容，所以这里保留 CRLF；
        # 换行风格另外由 newline 字段表达，保存时再按它还原。
        self.assertEqual(meta["content"], crlf_original)

        _body, after = self.roundtrip(crlf_rel)
        self.assertEqual(after, crlf_original.encode("utf-8"))
        self.assertEqual(after.count(b"\r\n"), 3)

        # LF-only 文件必须保持 LF
        lf_rel = "lf.txt"
        lf_original = "一行\n二行\n三行\n"
        _write_bytes(self.path(lf_rel), lf_original.encode("utf-8"))

        meta = self.get_text(lf_rel)
        self.assertEqual(meta["newline"], "\n")

        _body, after = self.roundtrip(lf_rel)
        self.assertEqual(after, lf_original.encode("utf-8"))
        self.assertNotIn(b"\r", after)

    def test_bom_still_reported_after_save(self):
        """
        保存一次之后，GET 仍须报 bom=true。

        这一条是钉住一个真实踩过的坑：GET 对带 BOM 的文件回报的 encoding 是
        "utf-8-sig"（解码器的名字），而 BOM 查表只认 "utf-8"，
        于是第一次读能报对、存完再读就报成 false。
        """
        rel = "bom_after_save.txt"
        _write_bytes(self.path(rel), codecs.BOM_UTF8 + "内容\n".encode("utf-8"))

        first = self.get_text(rel)
        self.assertTrue(first["bom"])
        self.assertEqual(first["encoding"], "utf-8-sig")

        status, body = self.save(rel, first["content"], first)
        self.assertEqual(status, 200, body)

        second = self.get_text(rel)
        self.assertTrue(second["bom"], "保存后 BOM 必须仍然被识别")
        self.assertEqual(second["encoding"], "utf-8-sig")
        self.assertEqual(_read_bytes(self.path(rel)),
                         codecs.BOM_UTF8 + "内容\n".encode("utf-8"))

    def test_crlf_plus_bom_together(self):
        """CRLF + BOM 同时存在时，三个新字段都要报对，且往返字节不变。"""
        rel = "bom_crlf.txt"
        original = "第一行\r\n第二行\r\n"
        raw = codecs.BOM_UTF8 + original.encode("utf-8")
        _write_bytes(self.path(rel), raw)

        meta = self.get_text(rel)
        self.assertEqual(meta["newline"], "\r\n")
        self.assertTrue(meta["bom"])
        self.assertAlmostEqual(meta["mtime"],
                               os.path.getmtime(self.path(rel)), delta=1.0)
        self.assertEqual(meta["size"], len(raw))

        _body, after = self.roundtrip(rel)
        self.assertEqual(after, raw)

    def test_cr_only_newline_is_detected_and_preserved(self):
        """老 Mac 风格的纯 CR 文件不能丢换行。"""
        rel = "cr.txt"
        original = "一行\r二行\r"
        _write_bytes(self.path(rel), original.encode("utf-8"))

        meta = self.get_text(rel)
        self.assertEqual(meta["newline"], "\r")

        _body, after = self.roundtrip(rel)
        self.assertEqual(after, original.encode("utf-8"))

    def test_file_without_newline_defaults_to_lf(self):
        rel = "oneline.txt"
        _write_bytes(self.path(rel), b"no newline at all")

        meta = self.get_text(rel)
        self.assertEqual(meta["newline"], "\n", "没有换行符时默认 LF")
        self.assertFalse(meta["bom"])

        _body, after = self.roundtrip(rel)
        self.assertEqual(after, b"no newline at all")

    def test_edited_text_replaces_content_fully(self):
        """不是「追加」也不是「部分写」：磁盘上必须正好是新内容。"""
        rel = "overwrite.txt"
        _write_bytes(self.path(rel), ("x" * 4000).encode("utf-8"))

        meta = self.get_text(rel)
        new_text = "新的内容，很短。\n"
        status, body = self.save(rel, new_text, meta)
        self.assertEqual(status, 200, body)

        self.assertEqual(_read_bytes(self.path(rel)), new_text.encode("utf-8"))
        self.assertEqual(body["size"], len(new_text.encode("utf-8")))
        self.assertEqual(os.path.getsize(self.path(rel)), body["size"])

    def test_repeated_save_does_not_accumulate_carriage_returns(self):
        """连续保存两次 CRLF 文件，不能变成 \\r\\r\\n。"""
        rel = "crlf_twice.txt"
        _write_bytes(self.path(rel), b"a\r\nb\r\n")

        self.assertEqual(self.get_text(rel)["newline"], "\r\n")

        for attempt in range(2):
            # 每轮都必须重新 GET：上一次保存已经改动了 mtime，
            # 沿用旧基线会被（正确工作的）乐观并发校验判成 409。
            meta = self.get_text(rel)
            status, body = self.save(rel, meta["content"], meta)
            self.assertEqual(status, 200, "第 %d 次保存失败：%s" % (attempt + 1, body))

        after = _read_bytes(self.path(rel))
        self.assertEqual(after, b"a\r\nb\r\n")
        self.assertNotIn(b"\r\r", after)

    def test_saving_empty_text_empties_the_file(self):
        """
        清空文件是合法编辑，必须真的写进去（0 字节），不能因为内容为空就跳过。
        """
        rel = "emptied.txt"
        _write_bytes(self.path(rel), b"some existing content\n")

        meta = self.get_text(rel)
        status, body = self.save(rel, "", meta)

        self.assertEqual(status, 200, body)
        self.assertEqual(_read_bytes(self.path(rel)), b"")
        self.assertEqual(body["size"], 0)
        self.assertEqual(os.path.getsize(self.path(rel)), 0)
        self.assertEqual(_temp_leftovers(self.root), [])

    def test_new_text_gets_newline_style_applied(self):
        """客户端提交 LF 文本 + CRLF 目标：写出来必须是 CRLF。"""
        rel = "apply_crlf.txt"
        _write_bytes(self.path(rel), b"old\r\n")

        meta = self.get_text(rel)
        status, body = self.save(rel, "新行一\n新行二\n", meta)
        self.assertEqual(status, 200, body)
        self.assertEqual(_read_bytes(self.path(rel)), "新行一\r\n新行二\r\n".encode("utf-8"))


# ---------------------------------------------------------------------------
# 安全护栏
# ---------------------------------------------------------------------------

class TextEditGuardTests(TextEditBase):
    """数据丢失 / 并发 / 原子性护栏。"""

    def test_oversized_file_is_refused_and_untouched(self):
        """
        ★ 截断护栏：文件真实大小超过可编辑上限时拒绝保存。

        GET 只会读前 text_max_kb，所以客户端手里是截断内容；
        此时放行就等于把 40KB 的文件截成 16KB —— 不可逆的数据丢失。
        """
        rel = "big.txt"
        big = ("line of text\n" * 4000).encode("utf-8")   # ~52KB > 16KB 上限
        self.assertGreater(len(big), TEXT_MAX_BYTES)
        _write_bytes(self.path(rel), big)

        meta = self.get_text(rel)
        self.assertTrue(meta["truncated"], "预览必须标记为截断")
        # 客户端「不知情」地提交它手里那份（截断的）内容
        status, body = self.save(rel, meta["content"], meta)

        self.assertEqual(status, 400, body)
        self.assertFalse(body["ok"])
        self.assertIn("下载", body["message"], "提示要引导用户改走下载→编辑→上传")
        # 文件必须一个字节都没变
        self.assertEqual(_read_bytes(self.path(rel)), big)
        self.assertEqual(_temp_leftovers(self.root), [])

    def test_submitted_text_over_cap_is_413(self):
        """提交的文本本身超上限 → 413（文件本身很小，所以不是 400）。"""
        rel = "small_but_huge_edit.txt"
        _write_bytes(self.path(rel), b"seed\n")

        meta = self.get_text(rel)
        assert meta["size"] < TEXT_MAX_BYTES

        huge = "a" * (TEXT_MAX_BYTES + 1024)
        status, body = self.save(rel, huge, meta)

        self.assertEqual(status, 413, body)
        self.assertFalse(body["ok"])
        # 文件保持原样
        self.assertEqual(_read_bytes(self.path(rel)), b"seed\n")

    def test_conflict_when_file_changed_on_disk(self):
        """
        ★ 乐观并发：GET 之后文件被别人改了，用旧 base_mtime 保存必须 409
        且一个字节都不写。
        """
        rel = "conflict.txt"
        _write_bytes(self.path(rel), b"original content\n")

        meta = self.get_text(rel)

        # 模拟「别人改了文件」：内容变了，并把 mtime 改成明显不同的过去时间，
        # 确保与基线差得远超容差（不去依赖两次写入是否落在同一 mtime 滴答里）。
        _write_bytes(self.path(rel), b"someone else edited this\n")
        os.utime(self.path(rel), (time.time() - 3600, time.time() - 3600))

        before = _read_bytes(self.path(rel))
        status, body = self.save(rel, "my new text\n", meta)

        self.assertEqual(status, 409, body)
        self.assertFalse(body["ok"])
        self.assertIn("修改", body["message"])
        # 关键：失败时绝不能碰文件
        self.assertEqual(_read_bytes(self.path(rel)), before)
        self.assertEqual(_temp_leftovers(self.root), [])

    def test_conflict_when_only_size_differs(self):
        """mtime 被故意设成基线值，但大小不同 —— 仍必须 409。"""
        rel = "conflict_size.txt"
        _write_bytes(self.path(rel), b"1234567890")

        meta = self.get_text(rel)

        _write_bytes(self.path(rel), b"1234567890extra")
        # mtime 重置回基线，专门考验 size 这一维度的比对
        os.utime(self.path(rel), (meta["mtime"], meta["mtime"]))

        before = _read_bytes(self.path(rel))
        status, body = self.save(rel, "replacement\n", meta)

        self.assertEqual(status, 409, body)
        self.assertEqual(_read_bytes(self.path(rel)), before)

    def test_no_temp_file_left_after_success(self):
        """★ 成功保存后目录里不能有残留临时文件。"""
        rel = "clean_success.txt"
        _write_bytes(self.path(rel), b"before\n")
        self.assertEqual(_temp_leftovers(self.root), [])

        body, after = self.roundtrip(rel, new_text="after\n")

        self.assertEqual(after, b"after\n")
        self.assertEqual(_temp_leftovers(self.root), [],
                         "成功保存后不该有临时文件残留")

    def test_no_temp_file_left_after_failed_save(self):
        """★ 被拒绝的保存（409）同样不能留下临时文件。"""
        rel = "clean_fail.txt"
        _write_bytes(self.path(rel), b"before\n")

        meta = self.get_text(rel)
        _write_bytes(self.path(rel), b"changed\n")
        os.utime(self.path(rel), (time.time() - 7200, time.time() - 7200))

        status, _body = self.save(rel, "ignored\n", meta)
        self.assertEqual(status, 409)

        self.assertEqual(_temp_leftovers(self.root), [],
                         "失败的保存不该有临时文件残留")

    def test_no_temp_file_left_after_other_rejections(self):
        """403 / 413 / 400 这几条拒绝路径也不该留临时文件。"""
        rel = "clean_more.txt"
        _write_bytes(self.path(rel), b"seed\n")
        meta = self.get_text(rel)

        # 413
        status, _ = self.save(rel, "b" * (TEXT_MAX_BYTES + 10), meta)
        self.assertEqual(status, 413)

        # 400：编码不在白名单
        status, _ = self.save(rel, "text", meta, encoding="rot13")
        self.assertEqual(status, 400)

        # 403：写只读根目录
        ro_rel = "in_ro.txt"
        _write_bytes(os.path.join(self.readonly_root, ro_rel), b"ro\n")
        ro_meta = self.get_text(ro_rel, root="ro")
        status, _ = self.save(ro_rel, "nope", ro_meta, root="ro")
        self.assertEqual(status, 403)

        self.assertEqual(_temp_leftovers(self.root), [])
        self.assertEqual(_temp_leftovers(self.readonly_root), [])

    def test_binary_content_is_refused(self):
        """提交的内容编码后含 NUL → 400 且不写入。"""
        rel = "nul.txt"
        _write_bytes(self.path(rel), b"seed\n")

        meta = self.get_text(rel)
        before = _read_bytes(self.path(rel))

        status, body = self.save(rel, "abc\x00def", meta)

        self.assertEqual(status, 400, body)
        self.assertFalse(body["ok"])
        self.assertEqual(_read_bytes(self.path(rel)), before,
                         "拒绝写入时文件必须保持原样")

    def test_bogus_bom_flag_is_400_not_500(self):
        """
        客户端谎报 bom=true 时必须 400，不能是 500。

        真实踩到的坑：GBK / Big5 这类编码根本没有 BOM 的概念，
        而内部 BOM 查表只认识 utf-8 / utf-16；直接拿编码名去查会抛 KeyError，
        于是一次畸形请求就把接口打成 500（服务器内部错误）。
        """
        rel = "bogus_bom.txt"
        _write_bytes(self.path(rel), "中文内容\n".encode("gbk"))
        before = _read_bytes(self.path(rel))

        meta = self.get_text(rel)
        self.assertEqual(meta["encoding"], "gb18030")
        self.assertFalse(meta["bom"])

        for encoding in ("gbk", "gb18030", "big5", "cp932", "latin-1"):
            with self.subTest(encoding=encoding):
                status, body = self.save(rel, "新内容\n", meta,
                                         encoding=encoding, bom=True)
                self.assertEqual(status, 400, body)
                self.assertFalse(body["ok"])

        self.assertEqual(_read_bytes(self.path(rel)), before)

    def test_utf16_without_bom_is_rejected(self):
        """UTF-16 不带 BOM 没有合法写法，应 400 而不是猜一个字节序。"""
        rel = "utf16.txt"
        _write_bytes(self.path(rel), "内容\n".encode("utf-8"))

        meta = self.get_text(rel)
        status, body = self.save(rel, "新内容\n", meta,
                                 encoding="utf-16", bom=False)

        self.assertEqual(status, 400, body)
        self.assertFalse(body["ok"])

    def test_utf16_with_bom_roundtrips_byte_identical(self):
        """
        UTF-16 + BOM 的文本可以原样存回，且字节完全一致。

        两条路径的行为不同，但都是对的：
          * GET 把 UTF-16 判成二进制（字节里天然有 NUL），前端不给编辑入口；
          * POST 若真收到 UTF-16 + BOM 的提交，仍然要能忠实写回 ——
            判断「是不是二进制」对 UTF-16 必须看**解码后的文本**，
            拿编码字节去量会把每个正常 UTF-16 文件都误判成二进制。
        """
        rel = "utf16bom.txt"
        original = "第一行\n第二行\n"
        raw = original.encode("utf-16")      # Python 会加上 LE BOM
        _write_bytes(self.path(rel), raw)
        self.assertTrue(raw.startswith(b"\xff\xfe"))

        status, body = self.client.json("POST", "/api/fs/text", {
            "root": "main", "path": rel, "text": original,
            "encoding": "utf-16", "bom": True, "newline": "\n",
            "base_mtime": os.path.getmtime(self.path(rel)),
            "base_size": os.path.getsize(self.path(rel)),
        })

        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(_read_bytes(self.path(rel)), raw)
        # 只能有一个 BOM
        self.assertFalse(_read_bytes(self.path(rel))[2:].startswith(b"\xff\xfe"))

    def test_utf16_without_bom_is_rejected_and_never_500(self):
        """UTF-16 不带 BOM 没有合法写法：必须 4xx，绝不能是 500。"""
        rel = "utf16_nobom.txt"
        _write_bytes(self.path(rel), "内容\n".encode("utf-16"))
        before = _read_bytes(self.path(rel))

        status, body = self.client.json("POST", "/api/fs/text", {
            "root": "main", "path": rel, "text": "新内容\n",
            "encoding": "utf-16", "bom": False, "newline": "\n",
            "base_mtime": os.path.getmtime(self.path(rel)),
            "base_size": os.path.getsize(self.path(rel)),
        })

        self.assertIn(status, (400, 404), body)
        self.assertFalse(body["ok"])
        self.assertEqual(_read_bytes(self.path(rel)), before)

    def test_unknown_encoding_is_rejected(self):
        """编码白名单：任意字符串不能进 str.encode。"""
        rel = "encoding.txt"
        _write_bytes(self.path(rel), b"abc\n")
        meta = self.get_text(rel)

        for bogus in ("utf-7", "rot13", "", "utf-8(replace)", "idna", "base64"):
            with self.subTest(encoding=bogus):
                status, body = self.save(rel, "abc", meta, encoding=bogus)
                self.assertEqual(status, 400, body)
                self.assertFalse(body["ok"])

        self.assertEqual(_read_bytes(self.path(rel)), b"abc\n")

    def test_missing_file_is_404(self):
        status, body = self.client.json("POST", "/api/fs/text", {
            "root": "main", "path": "does_not_exist.txt",
            "text": "hi", "encoding": "utf-8", "bom": False, "newline": "\n",
            "base_mtime": 0.0, "base_size": 0,
        })
        self.assertEqual(status, 404, body)
        self.assertFalse(body["ok"])

    def test_directory_target_is_400(self):
        os.makedirs(self.path("adir"), exist_ok=True)
        status, body = self.client.json("POST", "/api/fs/text", {
            "root": "main", "path": "adir",
            "text": "hi", "encoding": "utf-8", "bom": False, "newline": "\n",
            "base_mtime": 0.0, "base_size": 0,
        })
        self.assertEqual(status, 400, body)

    def test_readonly_root_is_403(self):
        rel = "ro_target.txt"
        target = os.path.join(self.readonly_root, rel)
        _write_bytes(target, b"read only\n")

        meta = self.get_text(rel, root="ro")
        status, body = self.save(rel, "changed\n", meta, root="ro")

        self.assertEqual(status, 403, body)
        self.assertFalse(body["ok"])
        self.assertEqual(_read_bytes(target), b"read only\n")

    def test_protected_path_is_403(self):
        """
        受保护路径禁止写入。

        guarded 根是可写的，所以这里唯一能拦下请求的就是 protected_paths ——
        403 只可能来自 _ensure_not_protected，不会和只读根的判断混淆。
        """
        rel = "protected.txt"
        target = os.path.join(self.guarded_root, rel)
        _write_bytes(target, b"protected\n")

        meta = self.get_text(rel, root="guarded")
        status, body = self.save(rel, "changed\n", meta, root="guarded")

        self.assertEqual(status, 403, body)
        self.assertFalse(body["ok"])
        self.assertEqual(_read_bytes(target), b"protected\n")

    def test_csrf_token_required(self):
        """没有 X-CSRF-Token 的 POST 必须被中间件挡下（403）且不写文件。"""
        rel = "csrf.txt"
        _write_bytes(self.path(rel), b"untouched\n")

        meta = self.get_text(rel)
        payload = {
            "root": "main", "path": rel, "text": "hacked\n",
            "encoding": meta["encoding"], "bom": meta["bom"],
            "newline": meta["newline"],
            "base_mtime": meta["mtime"], "base_size": meta["size"],
        }

        # 手动发一个不带 CSRF 头的请求（Client 默认会自动加上）
        raw_client = Client(self.server.port)
        raw_client.cookie = self.client.cookie
        raw_client.csrf = self.client.csrf      # 有令牌，但下面明确不带
        status, text = raw_client.request("POST", "/api/fs/text", payload,
                                          with_csrf=False)

        self.assertEqual(status, 403, text)
        self.assertIn("CSRF", text)
        self.assertEqual(_read_bytes(self.path(rel)), b"untouched\n")

    def test_binary_file_target_nul_is_refused(self):
        """二进制目标文件：提交内容含 NUL，拒绝写入。"""
        rel = "already_binary.bin"
        _write_bytes(self.path(rel), b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        before = _read_bytes(self.path(rel))

        status, body = self.client.json("POST", "/api/fs/text", {
            "root": "main", "path": rel,
            "text": "text\x00with nul", "encoding": "utf-8",
            "bom": False, "newline": "\n",
            "base_mtime": os.path.getmtime(self.path(rel)),
            "base_size": os.path.getsize(self.path(rel)),
        })

        self.assertEqual(status, 400, body)
        self.assertEqual(_read_bytes(self.path(rel)), before)


# ---------------------------------------------------------------------------
# GET 的新字段
# ---------------------------------------------------------------------------

class TextPreviewFieldTests(TextEditBase):
    """GET /api/fs/text 新增的三个字段必须存在且取值合理。"""

    def test_new_fields_present_on_plain_utf8(self):
        rel = "plain.txt"
        _write_bytes(self.path(rel), b"alpha\nbeta\n")

        body = self.get_text(rel)

        # 老字段一个都不能少
        for field in ("ok", "binary", "content", "encoding", "size",
                      "size_text", "truncated", "max_kb", "line_count", "name"):
            self.assertIn(field, body, "原有字段 %s 不能丢" % field)
        # 新增字段
        for field in ("mtime", "bom", "newline"):
            self.assertIn(field, body, "新增字段 %s 缺失" % field)

        self.assertEqual(body["name"], rel)
        self.assertEqual(body["size"], len(b"alpha\nbeta\n"))
        self.assertEqual(body["encoding"], "utf-8")
        self.assertFalse(body["bom"])
        self.assertEqual(body["newline"], "\n")
        self.assertAlmostEqual(body["mtime"], os.path.getmtime(self.path(rel)),
                               delta=1.0)
        self.assertEqual(body["max_kb"], TEXT_MAX_KB)

    def test_new_fields_correct_for_crlf_bom_fixture(self):
        """CRLF + BOM 的样本：bom=true、newline=\\r\\n、mtime 与磁盘一致。"""
        rel = "crlf_bom_fixture.txt"
        raw = codecs.BOM_UTF8 + "甲\r\n乙\r\n丙\r\n".encode("utf-8")
        _write_bytes(self.path(rel), raw)

        body = self.get_text(rel)

        self.assertTrue(body["bom"])
        self.assertEqual(body["newline"], "\r\n")
        self.assertAlmostEqual(body["mtime"], os.path.getmtime(self.path(rel)),
                               delta=1.0)
        self.assertEqual(body["size"], len(raw))
        self.assertEqual(os.path.getsize(self.path(rel)), body["size"])
        self.assertFalse(body["truncated"])

    def test_binary_branch_is_unchanged(self):
        """二进制分支结构不变，且不带那三个新字段。"""
        rel = "bin.bin"
        _write_bytes(self.path(rel), b"\x00\x01\x02\x03" * 100)

        body = self.get_text(rel)

        self.assertFalse(body["ok"])
        self.assertTrue(body["binary"])
        self.assertIn("message", body)
        self.assertIn("size", body)
        # 二进制分支保持原样，不追加编辑相关字段
        self.assertNotIn("content", body)
        self.assertNotIn("mtime", body)


class TextCodecUnitTests(unittest.TestCase):
    """
    不走 HTTP，直接钉住两个内部函数的契约。

    这两条都是真实踩过的坑，放在单元层最省事、失败信息也最直白：
      * _decode_text 曾经多了一个**假的** newline 参数 —— bytes.decode 的
        签名只有 (encoding, errors)，没有 newline。于是 newline="" 实际是
        errors=""（只在真的解码失败时才炸成 LookupError），
        而默认值 None 直接 TypeError。顺带一提：bytes.decode 本来就不做
        通用换行折叠，所以换行参数从始至终都是画蛇添足；
      * _detect_newline 曾经一看到 \r 就立刻返回裸 "\\r"，
        于是 CRLF 被降级成 CR，保存一次就把整份文件的换行写坏。
    """

    def test_decode_text_default_call_preserves_crlf(self):
        """默认调用不得抛异常，且必须原样保留 \\r\\n 与单独的 \\r。"""
        text, encoding = _decode_text("中文内容\r\n第二行\r\n".encode("gbk"))
        self.assertEqual(encoding, "gb18030")
        self.assertEqual(text, "中文内容\r\n第二行\r\n")

        text, encoding = _decode_text(b"a\r\nb\rc\n")
        self.assertEqual(encoding, "utf-8")
        self.assertEqual(text, "a\r\nb\rc\n", "bytes.decode 不折叠换行")

    def test_detect_newline_covers_all_three_styles(self):
        self.assertEqual(_detect_newline("一行\r\n二行\r\n"), "\r\n", "CRLF 必须优先")
        self.assertEqual(_detect_newline("一行\n二行\n"), "\n")
        self.assertEqual(_detect_newline("一行\r二行\r"), "\r", "裸 CR 仍要认成 CR")
        self.assertEqual(_detect_newline("没有换行符"), "\n", "无换行时默认 LF")
        self.assertEqual(_detect_newline("\r\n"), "\r\n")
        # 混用换行符时按「第一个出现的」风格（已与用户确认，优于「最多的」）
        self.assertEqual(_detect_newline("a\nb\r\n"), "\n")
        self.assertEqual(_detect_newline("a\r\nb\n"), "\r\n")

    def test_detect_bom_uses_raw_bytes(self):
        self.assertTrue(_detect_bom(codecs.BOM_UTF8 + b"x", "utf-8-sig"))
        self.assertTrue(_detect_bom(codecs.BOM_UTF8 + b"x", "utf-8"))
        self.assertFalse(_detect_bom(b"x", "utf-8"))
        self.assertFalse(_detect_bom("中文".encode("gbk"), "gb18030"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
