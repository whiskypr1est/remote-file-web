# -*- coding: utf-8 -*-
"""
照片（时间轴相册）的测试
========================

覆盖四块最要紧的东西：

1. **可见性与安全**：索引里只存「根标识 + 相对路径」，每次读取都要重新过
   当前用户的路径解析器 —— 子用户看不到别人的照片；手工把索引里的 relpath
   改成 `../../` 也读不出根目录外的文件（这是「编辑一个 JSON = 任意文件读取」
   那个漏洞的防线）。
2. **就地索引的代价**：文件被移动/改名之后，按内容指纹能认回来，
   而且**用户改过的时间与地点跟着走** —— 这是这套设计能不能用的关键。
   文件暂时找不到时条目要保留，绝不因为「不在」就丢掉用户的编辑记录。
3. **两层分离**：索引是派生缓存、编辑是用户数据。索引坏掉/删掉之后重建，
   用户的编辑必须一个都不少。
4. **时间的三态**：用户覆盖 > EXIF > 文件时间。没有 EXIF 的图（微信图、截图）
   会落到文件时间，界面要能据此把它们标成「待整理」。

图片用**现场生成的真 JPEG**（带真实 EXIF 与 GPS），不是改名的空文件：
这样 EXIF 解析、缩略图、尺寸读的都是真实数据。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from PIL import Image
from PIL.TiffImagePlugin import IFDRational

from fileweb import photos
from fileweb import users as users_module
from fileweb.security import PathResolver
from tests._harness import ServerProcess

ADMIN_USER = "tester"
ADMIN_PASSWORD = "test-password-123"
STUDENT_PASSWORD = "student-pass-123"


# ---------------------------------------------------------------------------
# 造图工具
# ---------------------------------------------------------------------------

def make_jpeg(path: str, color=(200, 30, 30), taken: str = "", gps=None) -> str:
    """
    生成一张带真实 EXIF 的 JPEG。

    taken 用 EXIF 的原始写法（`2023:08:15 12:34:56`），gps 是
    {"lat_ref","lat","lon_ref","lon"}。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im = Image.new("RGB", (80, 60), color)
    exif = Image.Exif()
    if taken:
        exif[306] = taken            # DateTime
        exif[36867] = taken          # DateTimeOriginal
    exif[271] = "TestMake"
    exif[272] = "TestModel X1"
    exif[34855] = 200
    exif[33437] = IFDRational(18, 10)
    exif[33434] = IFDRational(1, 500)
    exif[42036] = "TestLens 24-70"
    if gps:
        block = exif.get_ifd(0x8825)
        block[1] = gps["lat_ref"]
        block[2] = gps["lat"]
        block[3] = gps["lon_ref"]
        block[4] = gps["lon"]
    im.save(path, "JPEG", exif=exif)
    return path


def make_plain_png(path: str, color=(10, 10, 200)) -> str:
    """生成一张**没有 EXIF** 的图（模拟微信图片 / 截图）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGB", (50, 50), color).save(path, "PNG")
    return path


def jpeg_bytes(color=(180, 90, 40), taken: str = "", size=(80, 60)) -> bytes:
    """
    直接在内存里造一张 JPEG 的字节（用于上传接口的用例）。

    ★ 必须是**真的 JPEG**：上传接口会读 EXIF、算缩略图、比对指纹，
      拿一段随便的字节只能测到「文件不是图片」那一条。
    """
    import io

    im = Image.new("RGB", size, color)
    exif = Image.Exif()
    if taken:
        exif[306] = taken
        exif[36867] = taken
    buffer = io.BytesIO()
    im.save(buffer, "JPEG", exif=exif)
    return buffer.getvalue()


def noisy_jpeg_bytes(size=(1500, 1500)) -> bytes:
    """造一张**压不小**的 JPEG（噪声），用来测上传大小上限。"""
    import io
    import random

    random.seed(20240915)
    width, height = size
    data = bytes(random.getrandbits(8) for _ in range(width * height * 3))
    im = Image.frombytes("RGB", (width, height), data)
    buffer = io.BytesIO()
    im.save(buffer, "JPEG", quality=95)
    return buffer.getvalue()


def photo_cfg(work: str) -> dict:
    return {"photos": {
        "enabled": True,
        "state_path": os.path.join(work, "photos_state.json"),
        "index_path": os.path.join(work, "photo_index.json"),
        "upload_dir": os.path.join(work, "photos_uploads"),
        "upload_per_user": True,
        "thumb_size": 320,
    }}


ADMIN = {"username": "admin", "role": "admin"}


# ---------------------------------------------------------------------------
# 纯函数：标识校验与时间解析
# ---------------------------------------------------------------------------

class PhotoGuardTests(unittest.TestCase):
    """标识与时间解析（纯函数，不启服务、不碰磁盘）。"""

    def test_norm_id_accepts_a_hex_fingerprint(self):
        self.assertEqual(photos.norm_id("A" * 40), "a" * 40)
        self.assertEqual(photos.norm_id("0123456789abcdef"), "0123456789abcdef")

    def test_norm_id_rejects_traversal_and_junk(self):
        """
        ★ id 会作为字典键去索引里查表，形状不对必须当场拒绝，
          绝不能被拿去做任何路径拼接。
        """
        for bad in ("../../etc/passwd", "..\\..\\win.ini", "/abs/path",
                    "a/b", "", "   ", "zzzz", "a" * 10, "a" * 200,
                    "0123456789abcdefg", None):
            with self.assertRaises(photos.PhotoError, msg="应当拒绝：%r" % (bad,)):
                photos.norm_id(bad)

    def test_parse_user_time_accepts_the_common_shapes(self):
        self.assertEqual(photos.parse_user_time("2023-08-15 12:34:56"),
                         "2023-08-15T12:34:56")
        self.assertEqual(photos.parse_user_time("2023-08-15T12:34"),
                         "2023-08-15T12:34:00")
        # 只给日期 = 当天零点
        self.assertEqual(photos.parse_user_time("2023-08-15"),
                         "2023-08-15T00:00:00")
        # 空串 = 清空覆盖值（不是错误）
        self.assertEqual(photos.parse_user_time(""), "")
        self.assertEqual(photos.parse_user_time(None), "")

    def test_parse_user_time_rejects_junk(self):
        for bad in ("昨天", "2023/08/15", "2023-13-01", "2023-08-32",
                    "2023-08-15 25:00:00", "15-08-2023"):
            with self.assertRaises(photos.PhotoError, msg="应当拒绝：%r" % bad):
                photos.parse_user_time(bad)

    def test_exif_datetime_handles_the_zero_value(self):
        """
        ★ `0000:00:00 00:00:00` 是「没写」的意思，必须当成没有，
          否则时间轴上会冒出一堆排在最前面的幽灵照片。
        """
        self.assertEqual(photos._exif_datetime("2023:08:15 12:34:56"),
                         "2023-08-15T12:34:56")
        self.assertEqual(photos._exif_datetime("0000:00:00 00:00:00"), "")
        self.assertEqual(photos._exif_datetime(""), "")
        self.assertEqual(photos._exif_datetime(None), "")
        self.assertEqual(photos._exif_datetime("not a date"), "")
        # 相机写疯了的 24:00:00 夹到合法区间，而不是丢掉整条时间
        self.assertEqual(photos._exif_datetime("2023:08:15 24:00:00"),
                         "2023-08-15T23:00:00")

    def test_is_photo_follows_the_thumbnail_module(self):
        for name in ("a.jpg", "a.JPEG", "b.png", "c.webp", "d.tiff", "e.avif"):
            self.assertTrue(photos.is_photo(name), name)
        # .ico 是图标不是照片；音频/文本更不是
        for name in ("a.ico", "a.mp3", "a.txt", "a", "a.heic"):
            self.assertFalse(photos.is_photo(name), name)

    def test_format_exposure(self):
        self.assertEqual(photos._format_exposure(IFDRational(1, 500)), "1/500")
        self.assertEqual(photos._format_exposure(0.002), "1/500")
        self.assertEqual(photos._format_exposure(2.5), "2.5s")
        self.assertEqual(photos._format_exposure(None), "")
        self.assertEqual(photos._format_exposure(0), "")


# ---------------------------------------------------------------------------
# 纯函数：时间三态
# ---------------------------------------------------------------------------

class EffectiveTimeTests(unittest.TestCase):
    """`用户覆盖 ?? EXIF ?? 文件时间` 这条优先级，以及它带出来的来源标记。"""

    ENTRY = {"exif_taken": "2023-08-15T12:34:56", "mtime": 1700000000.0}

    def test_user_override_wins(self):
        taken, source = photos.effective_time(
            self.ENTRY, {"taken_at": "2020-01-01T08:00:00"})
        self.assertEqual((taken, source), ("2020-01-01T08:00:00", "user"))

    def test_exif_wins_over_file_time(self):
        taken, source = photos.effective_time(self.ENTRY, None)
        self.assertEqual((taken, source), ("2023-08-15T12:34:56", "exif"))

    def test_file_time_is_the_last_resort_and_is_marked_as_such(self):
        """
        ★ 没有 EXIF 的图（微信图片、截图）会落到这一档。来源必须是 'file'，
          界面才能把它们标出来、汇进「待整理」——
          否则「按时间轴归类」从一开始就建立在一批不可信的时间上。
        """
        taken, source = photos.effective_time({"exif_taken": "", "mtime": 1700000000.0}, None)
        self.assertEqual(source, "file")
        self.assertTrue(taken.startswith("2023-11-1"), taken)

    def test_user_clearing_the_override_falls_back_to_exif(self):
        # 空串的 taken_at 不该被当成一个有效的覆盖值
        taken, source = photos.effective_time(self.ENTRY, {"taken_at": ""})
        self.assertEqual((taken, source), ("2023-08-15T12:34:56", "exif"))


# ---------------------------------------------------------------------------
# EXIF 读取
# ---------------------------------------------------------------------------

class ExifTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="fw-photo-exif-")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, ignore_errors=True)

    def test_reads_datetime_gps_and_camera(self):
        path = make_jpeg(
            os.path.join(self.work, "a.jpg"), taken="2023:08:15 12:34:56",
            gps={"lat_ref": "N", "lat": (36.0, 3.0, 40.0),
                 "lon_ref": "E", "lon": (120.0, 19.0, 10.0)})
        meta = photos.read_image_meta(path)

        self.assertEqual(meta["exif_taken"], "2023-08-15T12:34:56")
        self.assertEqual((meta["w"], meta["h"]), (80, 60))
        self.assertEqual(meta["iso"], 200)
        self.assertEqual(meta["fnum"], 1.8)
        self.assertEqual(meta["exposure"], "1/500")
        self.assertEqual(meta["lens"], "TestLens 24-70")
        self.assertIn("TestModel", meta["camera"])
        # 佳能的 Make 与 Model 会重复，不该拼成 "Canon Canon EOS R6"
        self.assertNotIn("TestMake TestMake", meta["camera"])

        self.assertIsNotNone(meta["gps"])
        self.assertAlmostEqual(meta["gps"]["lat"], 36.061111, places=5)
        self.assertAlmostEqual(meta["gps"]["lon"], 120.319444, places=5)

    def test_south_and_west_are_negative(self):
        path = make_jpeg(
            os.path.join(self.work, "s.jpg"), taken="2023:01:01 00:00:00",
            gps={"lat_ref": "S", "lat": (33.0, 52.0, 0.0),
                 "lon_ref": "W", "lon": (70.0, 40.0, 0.0)})
        gps = photos.read_image_meta(path)["gps"]
        self.assertLess(gps["lat"], 0, "南纬必须是负数")
        self.assertLess(gps["lon"], 0, "西经必须是负数")

    def test_zero_zero_gps_is_treated_as_absent(self):
        """(0,0) 在几内亚湾，实际基本都是「相机没写坐标」，当成没有更诚实。"""
        path = make_jpeg(
            os.path.join(self.work, "zero.jpg"), taken="2023:01:01 00:00:00",
            gps={"lat_ref": "N", "lat": (0.0, 0.0, 0.0),
                 "lon_ref": "E", "lon": (0.0, 0.0, 0.0)})
        self.assertIsNone(photos.read_image_meta(path)["gps"])

    def test_image_without_exif_is_not_an_error(self):
        """★ 没有 EXIF 是常态，必须得到一条「尺寸可用、时间待定」的记录。"""
        path = make_plain_png(os.path.join(self.work, "shot.png"))
        meta = photos.read_image_meta(path)
        self.assertEqual((meta["w"], meta["h"]), (50, 50))
        self.assertEqual(meta["exif_taken"], "")
        self.assertIsNone(meta["gps"])

    def test_broken_file_never_raises(self):
        """坏图 / 半截文件不能让整个导入失败。"""
        path = os.path.join(self.work, "broken.jpg")
        with open(path, "wb") as fh:
            fh.write(b"\xff\xd8\xff\xe0 this is not a real jpeg")
        meta = photos.read_image_meta(path)
        self.assertEqual(meta["exif_taken"], "")
        self.assertIsNone(meta["gps"])

        # 根本不存在的文件同样不能抛
        meta = photos.read_image_meta(os.path.join(self.work, "nope.jpg"))
        self.assertEqual(meta["w"], 0)


# ---------------------------------------------------------------------------
# 就地索引的完整链路（纯库层，不启服务）
# ---------------------------------------------------------------------------

class PhotosLibraryTests(unittest.TestCase):
    """导入 → 时间轴 → 编辑 → 平移 → 撤销 → 重扫找回。"""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="fw-photo-lib-")
        self.root = os.path.join(self.work, "share")
        self.resolver = PathResolver([
            {"id": "share", "name": "share", "path": self.root}])
        self.cfg = photo_cfg(self.work)

        make_jpeg(os.path.join(self.root, "trip", "a.jpg"), (200, 30, 30),
                  "2023:08:15 12:34:56",
                  {"lat_ref": "N", "lat": (36.0, 3.0, 40.0),
                   "lon_ref": "E", "lon": (120.0, 19.0, 10.0)})
        make_jpeg(os.path.join(self.root, "trip", "b.jpg"), (30, 200, 30),
                  "2024:01:02 03:04:05")
        make_plain_png(os.path.join(self.root, "misc", "shot.png"))

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    # -- 工具 ---------------------------------------------------------------

    def _import_all(self):
        result = photos.import_paths(self.cfg, ADMIN, self.resolver,
                                     "share", ["trip", "misc"])
        return result

    def _library(self):
        return photos.build_library(self.cfg, ADMIN, self.resolver)

    def _by_name(self, name):
        for item in self._library()["photos"]:
            if item["name"] == name:
                return item
        self.fail("库里没有 %s" % name)

    # -- 导入与时间三态 -----------------------------------------------------

    def test_import_indexes_without_copying_any_file(self):
        """
        ★ 就地索引：导入**不复制**文件。源目录里的文件还在原地，
          而且库里没有任何多出来的副本。
        """
        before = sorted(os.listdir(os.path.join(self.root, "trip")))
        result = self._import_all()
        after = sorted(os.listdir(os.path.join(self.root, "trip")))

        self.assertEqual(result["imported"], 3, result)
        self.assertEqual(before, after, "导入不该改动源目录")
        self.assertEqual(len(self._library()["photos"]), 3)

    def test_time_source_is_labelled_three_ways(self):
        self._import_all()
        stats = self._library()["stats"]
        self.assertEqual(stats["exif"], 2, "两张带 EXIF 的应当是 exif 档")
        self.assertEqual(stats["file"], 1, "没有 EXIF 的截图应当落到 file 档")
        self.assertEqual(stats["user"], 0)
        self.assertEqual(stats["with_gps"], 1)

    def test_timeline_is_sorted_newest_first(self):
        self._import_all()
        names = [p["name"] for p in self._library()["photos"]]
        self.assertEqual(names[0], "shot.png", "截图用的是文件时间（今天），排最前")
        self.assertEqual(names[1], "b.jpg")
        self.assertEqual(names[2], "a.jpg")

    def test_gps_is_surfaced_as_a_placeless_place(self):
        """EXIF 有坐标但用户没命名时，界面要拿到坐标好提示「给这批起个名字」。"""
        self._import_all()
        place = self._by_name("a.jpg")["place"]
        self.assertIsNotNone(place)
        self.assertEqual(place["name"], "")
        self.assertTrue(place.get("from_gps"))

    # -- 编辑 ---------------------------------------------------------------

    def test_edit_sets_time_place_tags_and_rating(self):
        self._import_all()
        pid = self._by_name("a.jpg")["id"]

        photos.apply_edits(self.cfg, ADMIN, [pid], {
            "taken_at": "2020-01-01 08:00:00",
            "place": {"name": "青岛·栈桥"},
            "tags": "家人, 海边",
            "rating": 5,
        })

        item = self._by_name("a.jpg")
        self.assertEqual(item["taken_at"], "2020-01-01T08:00:00")
        self.assertEqual(item["source"], "user", "改过之后来源要变成 user")
        self.assertEqual(item["place"]["name"], "青岛·栈桥")
        self.assertEqual(item["tags"], ["家人", "海边"])
        self.assertEqual(item["rating"], 5)

    def test_editing_does_not_touch_the_original_file(self):
        """
        ★ 这是用户明确选定的方案：改动只留在相册里，**原图一个字节都不动**。
          所以这里要真的核对文件的字节与修改时间。
        """
        self._import_all()
        target = os.path.join(self.root, "trip", "a.jpg")
        with open(target, "rb") as fh:
            before_bytes = fh.read()
        before_stat = os.stat(target)

        pid = self._by_name("a.jpg")["id"]
        photos.apply_edits(self.cfg, ADMIN, [pid],
                           {"taken_at": "1999-09-09 09:09:09",
                            "place": {"name": "别处"}})

        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), before_bytes, "原图字节必须完全没变")
        after_stat = os.stat(target)
        self.assertEqual(after_stat.st_size, before_stat.st_size)
        self.assertEqual(after_stat.st_mtime, before_stat.st_mtime)

        # 而且磁盘上的 EXIF 也还是老值
        self.assertEqual(photos.read_image_meta(target)["exif_taken"],
                         "2023-08-15T12:34:56")

    def test_clearing_the_override_falls_back_to_exif(self):
        self._import_all()
        pid = self._by_name("a.jpg")["id"]
        photos.apply_edits(self.cfg, ADMIN, [pid], {"taken_at": "2020-01-01"})
        self.assertEqual(self._by_name("a.jpg")["source"], "user")

        photos.apply_edits(self.cfg, ADMIN, [pid], {"taken_at": ""})
        item = self._by_name("a.jpg")
        self.assertEqual(item["source"], "exif", "清空覆盖值应当退回 EXIF")
        self.assertEqual(item["taken_at"], "2023-08-15T12:34:56")

    def test_edit_rejects_unknown_ids(self):
        """
        ★ 只接受**索引里真实存在**的 id：否则接口可以被用来往 state 文件里
          灌任意垃圾，久而久之把文件撑爆。
        """
        self._import_all()
        with self.assertRaises(photos.PhotoError):
            photos.apply_edits(self.cfg, ADMIN, ["f" * 40], {"rating": 5})

    def test_edit_rejects_bad_values(self):
        self._import_all()
        pid = self._by_name("a.jpg")["id"]
        with self.assertRaises(photos.PhotoError):
            photos.apply_edits(self.cfg, ADMIN, [pid], {"taken_at": "昨天"})
        with self.assertRaises(photos.PhotoError):
            photos.apply_edits(self.cfg, ADMIN, [pid], {"rating": "很好"})
        with self.assertRaises(photos.PhotoError):
            photos.apply_edits(self.cfg, ADMIN, [pid],
                               {"place": {"name": "x", "lat": "北纬36度"}})

    # -- 批量平移与撤销 -----------------------------------------------------

    def test_batch_shift_preserves_relative_order(self):
        """相机时区设错的那种场景：整体 +8 小时，彼此的先后不能乱。"""
        self._import_all()
        library = self._library()
        ids = [p["id"] for p in library["photos"]]
        before = {p["name"]: p["taken_at"] for p in library["photos"]}

        result = photos.shift_time(self.cfg, ADMIN, ids, 8 * 3600)
        self.assertEqual(result["updated"], 3)

        after = {p["name"]: p["taken_at"] for p in self._library()["photos"]}
        self.assertEqual(after["b.jpg"], "2024-01-02T11:04:05")
        self.assertEqual(after["a.jpg"], "2023-08-15T20:34:56")
        # 相对顺序没变
        order_before = sorted(before, key=lambda n: before[n])
        order_after = sorted(after, key=lambda n: after[n])
        self.assertEqual(order_before, order_after)

    def test_shift_time_can_pull_the_shoebox_photo_into_place(self):
        """
        ★ 原本靠「文件时间」的截图，平移之后也可以有明确时间了 ——
          这正是「把待整理的照片归到正确的时间轴上」要用的动作。
        """
        self._import_all()
        pid = self._by_name("shot.png")["id"]
        self.assertEqual(self._by_name("shot.png")["source"], "file")

        photos.shift_time(self.cfg, ADMIN, [pid], 3600)
        item = self._by_name("shot.png")
        self.assertEqual(item["source"], "user")

    def test_shift_time_rejects_zero_and_absurd_deltas(self):
        self._import_all()
        pid = self._by_name("a.jpg")["id"]
        with self.assertRaises(photos.PhotoError):
            photos.shift_time(self.cfg, ADMIN, [pid], 0)
        with self.assertRaises(photos.PhotoError):
            photos.shift_time(self.cfg, ADMIN, [pid], 100 * 365 * 24 * 3600)

    def test_undo_restores_the_exact_previous_state(self):
        """批量平移改错了必须能一键回到原样（不可逆操作不该只靠确认框兜底）。"""
        self._import_all()
        library = self._library()
        ids = [p["id"] for p in library["photos"]]
        before = {p["name"]: (p["taken_at"], p["source"]) for p in library["photos"]}

        photos.shift_time(self.cfg, ADMIN, ids, 8 * 3600)
        self.assertNotEqual(
            {p["name"]: (p["taken_at"], p["source"]) for p in self._library()["photos"]},
            before)

        result = photos.undo_last(self.cfg, ADMIN)
        self.assertEqual(result["restored"], 3)
        after = {p["name"]: (p["taken_at"], p["source"]) for p in self._library()["photos"]}
        self.assertEqual(after, before, "撤销后必须逐字段回到原样")

    def test_undo_restores_a_deleted_override_including_previous_value(self):
        """
        ★ 撤销要能区分「原来没有覆盖值」与「原来有、这次被改了」两种情况：
          前者要真的删掉，后者要回到旧值。
        """
        self._import_all()
        pid = self._by_name("a.jpg")["id"]

        photos.apply_edits(self.cfg, ADMIN, [pid], {"taken_at": "2020-01-01 00:00:00"})
        photos.apply_edits(self.cfg, ADMIN, [pid], {"taken_at": "2021-02-02 00:00:00"})
        self.assertEqual(self._by_name("a.jpg")["taken_at"], "2021-02-02T00:00:00")

        photos.undo_last(self.cfg, ADMIN)
        self.assertEqual(self._by_name("a.jpg")["taken_at"], "2020-01-01T00:00:00",
                         "应当回到上一次的值，而不是回到 EXIF")

        photos.undo_last(self.cfg, ADMIN)
        self.assertEqual(self._by_name("a.jpg")["source"], "exif",
                         "再撤销一次应当把覆盖值整个删掉")

    def test_undo_without_history_is_a_readable_error(self):
        with self.assertRaises(photos.PhotoError):
            photos.undo_last(self.cfg, ADMIN)

    # -- 重扫：找回被移动的文件 ---------------------------------------------

    def test_importing_the_root_itself_records_a_source(self):
        """
        ★ 回归：`paths: [""]` 是「导入这个根目录」。早先的写法把空串当无效项
          跳过了，于是一条来源都没记下 —— 表现是「导入整个根之后挪动文件，
          重扫一律报暂时找不到」，而且完全没有报错，很难查。
        """
        photos.import_paths(self.cfg, ADMIN, self.resolver, "share", [""])
        sources = photos.load_state(self.cfg, ADMIN)["sources"]
        self.assertIn({"root": "share", "path": ""},
                      [{"root": s["root"], "path": s["path"]} for s in sources],
                      "导入根目录也必须记下来源，否则重扫找不到被移动的文件")

        # 而且重扫真的能把根目录下被移动的文件认回来
        pid = self._by_name("a.jpg")["id"]
        photos.apply_edits(self.cfg, ADMIN, [pid], {"place": {"name": "青岛"}})
        os.makedirs(os.path.join(self.root, "elsewhere"), exist_ok=True)
        shutil.move(os.path.join(self.root, "trip", "a.jpg"),
                    os.path.join(self.root, "elsewhere", "renamed.jpg"))

        result = photos.rescan(self.cfg, ADMIN, self.resolver)
        self.assertEqual(result["relinked"], 1, result)
        self.assertEqual(self._by_name("renamed.jpg")["place"]["name"], "青岛")

    def test_rescan_relinks_a_moved_file_and_keeps_the_edits(self):
        """
        ★★ 整套设计的核心断言：改好时间与地点之后把文件挪个位置/改个名，
          重扫要**按内容指纹认回来**，而且编辑记录跟着走。
          如果这条不成立，「就地索引」就不能用 —— 用户会不敢整理目录。
        """
        self._import_all()
        pid = self._by_name("a.jpg")["id"]
        photos.apply_edits(self.cfg, ADMIN, [pid],
                           {"taken_at": "2020-01-01 08:00:00",
                            "place": {"name": "青岛"}})

        os.makedirs(os.path.join(self.root, "trip", "sub"), exist_ok=True)
        shutil.move(os.path.join(self.root, "trip", "a.jpg"),
                    os.path.join(self.root, "trip", "sub", "renamed.jpg"))

        result = photos.rescan(self.cfg, ADMIN, self.resolver)
        self.assertEqual(result["relinked"], 1, result)
        self.assertEqual(result["missing"], 0,
                         "找回来了就不该报「找不到」（这是个会吓人的假警报）")

        item = self._by_name("renamed.jpg")
        self.assertEqual(item["id"], pid, "指纹不变，id 应当也不变")
        self.assertEqual(item["taken_at"], "2020-01-01T08:00:00")
        self.assertEqual(item["place"]["name"], "青岛")
        self.assertFalse(item["missing"])

    def test_rescan_keeps_entries_for_files_that_are_gone(self):
        """
        ★ 移动硬盘没插、目录临时改名都会走到这里。
          「文件暂时不在」从来不是丢掉用户编辑记录的理由 ——
          条目要留着并标成 missing，插上盘重扫就自动回来。
        """
        self._import_all()
        pid = self._by_name("a.jpg")["id"]
        photos.apply_edits(self.cfg, ADMIN, [pid], {"place": {"name": "青岛"}})

        # 把整个 trip 目录挪走（模拟移动硬盘拔了）
        shutil.move(os.path.join(self.root, "trip"),
                    os.path.join(self.work, "trip-detached"))

        result = photos.rescan(self.cfg, ADMIN, self.resolver)
        self.assertGreaterEqual(result["missing"], 2, result)

        library = self._library()
        names = [p["name"] for p in library["photos"]]
        self.assertIn("a.jpg", names, "★ 找不到的文件不能被悄悄删掉")
        gone = self._by_name("a.jpg")
        self.assertTrue(gone["missing"])
        # 编辑记录必须原样留着
        self.assertEqual(gone["place"]["name"], "青岛")
        self.assertEqual(
            photos.load_state(self.cfg, ADMIN)["edits"][pid]["place"]["name"], "青岛")

        # 插回去，重扫应当自动恢复
        shutil.move(os.path.join(self.work, "trip-detached"),
                    os.path.join(self.root, "trip"))
        photos.rescan(self.cfg, ADMIN, self.resolver)
        back = self._by_name("a.jpg")
        self.assertFalse(back["missing"], "设备插回来之后应当自动恢复")
        self.assertEqual(back["place"]["name"], "青岛")

    def test_rescan_discovers_newly_added_files(self):
        self._import_all()
        make_jpeg(os.path.join(self.root, "trip", "new.jpg"), (1, 2, 3),
                  "2022:02:02 02:02:02")
        result = photos.rescan(self.cfg, ADMIN, self.resolver)
        self.assertEqual(result["added"], 1, result)
        self.assertIn("new.jpg", [p["name"] for p in self._library()["photos"]])

    def test_rescan_migrates_edits_when_a_file_is_re_saved_in_place(self):
        """
        ★ 同一个路径上内容变了（重新导出/修过图）：当作**同一张照片**，
          编辑记录迁到新指纹上。否则「我重新导出了一遍，改好的时间全没了」。
        """
        self._import_all()
        old_pid = self._by_name("a.jpg")["id"]
        photos.apply_edits(self.cfg, ADMIN, [old_pid], {"place": {"name": "青岛"}})

        # 原地覆盖成另一张图（内容与大小都变了）
        make_jpeg(os.path.join(self.root, "trip", "a.jpg"), (7, 7, 7),
                  "2025:05:05 05:05:05")

        photos.rescan(self.cfg, ADMIN, self.resolver)
        item = self._by_name("a.jpg")
        self.assertNotEqual(item["id"], old_pid, "内容变了，指纹应当也变")
        self.assertEqual(item["place"]["name"], "青岛",
                         "★ 编辑记录应当迁移到新指纹上")

    def test_stale_metadata_is_flagged_not_silently_used(self):
        """文件被改过但还没重扫时，要如实标 stale，不能用着旧元数据装作没事。"""
        self._import_all()
        target = os.path.join(self.root, "trip", "b.jpg")
        make_jpeg(target, (9, 9, 9), "2024:01:02 03:04:05")   # 内容变了

        item = self._by_name("b.jpg")
        self.assertTrue(item["stale"], "大小或时间对不上就应当标 stale")

    # -- 两层分离：索引可重建，用户数据不能丢 -------------------------------

    def test_rebuilding_the_index_keeps_every_edit(self):
        """
        ★★ 这是把「覆盖层」与「索引层」分成两个文件的原因。
          合成一张表的话，一次扫描失败就可能顺手冲掉用户改了半天的时间戳。
        """
        self._import_all()
        pid = self._by_name("a.jpg")["id"]
        photos.apply_edits(self.cfg, ADMIN, [pid], {
            "taken_at": "2020-01-01 08:00:00",
            "place": {"name": "青岛"}, "tags": ["家人"], "rating": 4,
            "caption": "第一次看海",
        })

        # 索引文件删掉（模拟损坏后重建）
        os.unlink(photos.index_path(self.cfg, ADMIN))
        self.assertEqual(len(self._library()["photos"]), 0)

        # 重新导入 = 重建索引
        self._import_all()

        item = self._by_name("a.jpg")
        self.assertEqual(item["taken_at"], "2020-01-01T08:00:00")
        self.assertEqual(item["place"]["name"], "青岛")
        self.assertEqual(item["tags"], ["家人"])
        self.assertEqual(item["rating"], 4)
        self.assertEqual(item["caption"], "第一次看海")

    def test_a_corrupt_state_file_does_not_break_the_gallery(self):
        """坏掉的覆盖层退回空结构（但文件留在磁盘上，还有抢救机会）。"""
        self._import_all()
        with open(photos.state_path(self.cfg, ADMIN), "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")

        library = self._library()
        self.assertEqual(len(library["photos"]), 3, "相册必须还能打开")
        self.assertEqual(library["stats"]["user"], 0)

        # 而且写入时会把它修好，而不是把坏内容原样带着走
        pid = self._by_name("a.jpg")["id"]
        photos.apply_edits(self.cfg, ADMIN, [pid], {"rating": 3})
        self.assertEqual(self._by_name("a.jpg")["rating"], 3)

    def test_a_corrupt_index_file_does_not_break_the_gallery(self):
        self._import_all()
        with open(photos.index_path(self.cfg, ADMIN), "w", encoding="utf-8") as fh:
            fh.write("[[[ not json")
        self.assertEqual(len(self._library()["photos"]), 0)

    def test_junk_entries_in_the_state_are_dropped(self):
        """state 里的坏条目要被清洗掉，不能原样传出去。"""
        self._import_all()
        state = photos.load_state(self.cfg, ADMIN)
        state["edits"]["../../evil"] = {"taken_at": "2020-01-01T00:00:00"}
        state["edits"]["a" * 40] = {"taken_at": "不是时间", "rating": 99,
                                    "tags": ["x", "x", ""]}
        state["albums"] = [{"name": ""}, {"name": "好相册"}]
        photos._atomic_write(photos.state_path(self.cfg, ADMIN), state)

        loaded = photos.load_state(self.cfg, ADMIN)
        self.assertNotIn("../../evil", loaded["edits"])
        self.assertNotIn("taken_at", loaded["edits"]["a" * 40], "坏时间要被丢掉")
        self.assertEqual(loaded["edits"]["a" * 40]["rating"], 5, "星级要夹到 0~5")
        self.assertEqual(loaded["edits"]["a" * 40]["tags"], ["x"], "标签要去重去空")
        self.assertEqual([a["name"] for a in loaded["albums"]], ["好相册"])

    # -- 安全：索引里不存绝对路径 -------------------------------------------

    def test_a_tampered_index_cannot_read_outside_the_root(self):
        """
        ★★ 关键防线：把索引里的 relpath 手工改成 `../../`，读取必须被挡下来。
          索引里如果存的是绝对路径，编辑一个 JSON 就等于任意文件读取。
        """
        self._import_all()
        pid = self._by_name("a.jpg")["id"]

        index = photos.load_index(self.cfg, ADMIN)
        index["entries"][pid]["relpath"] = "../../../../Windows/win.ini"
        photos._atomic_write(photos.index_path(self.cfg, ADMIN), index)

        with self.assertRaises(photos.PhotoError):
            photos.resolve_photo(self.cfg, ADMIN, self.resolver, pid)

        # 列表里也要看不到它，而不是把根目录外的文件列出来
        library = self._library()
        self.assertEqual(len(library["photos"]), 2)
        self.assertEqual(library["inaccessible"], 1)

    def test_an_entry_pointing_at_an_unknown_root_is_skipped(self):
        """根标识不是当前用户能访问的那个时，条目要被跳过（不泄露、也读不到）。"""
        self._import_all()
        pid = self._by_name("a.jpg")["id"]
        index = photos.load_index(self.cfg, ADMIN)
        index["entries"][pid]["root"] = "someone-elses-root"
        photos._atomic_write(photos.index_path(self.cfg, ADMIN), index)

        with self.assertRaises(photos.PhotoError):
            photos.resolve_photo(self.cfg, ADMIN, self.resolver, pid)
        self.assertEqual(len(self._library()["photos"]), 2)

    def test_collect_images_skips_links_and_junctions(self):
        """递归索引时不跟进符号链接 / 目录联接（否则会把根目录外的树拖进来）。"""
        outside = os.path.join(self.work, "outside")
        make_jpeg(os.path.join(outside, "secret.jpg"), (1, 1, 1), "2020:01:01 00:00:00")

        link = os.path.join(self.root, "trip", "link")
        made_link = False
        try:
            os.symlink(outside, link, target_is_directory=True)
            made_link = True
        except (OSError, NotImplementedError, AttributeError):
            pass

        if not made_link:
            self.skipTest("当前环境不支持创建符号链接")

        found, _skipped, _ignored = photos.collect_images(self.resolver, "share", ["trip"])
        names = [os.path.basename(f[2]) for f in found]
        self.assertNotIn("secret.jpg", names, "★ 不能跟进符号链接索引到根目录外")

    def test_collect_images_reports_unreadable_paths(self):
        found, skipped, _ignored = photos.collect_images(
            self.resolver, "share", ["不存在的目录"])
        self.assertEqual(found, [])
        self.assertEqual(len(skipped), 1)

    def test_collect_images_rejects_paths_outside_the_root(self):
        found, skipped, _ignored = photos.collect_images(
            self.resolver, "share", ["../../etc"])
        self.assertEqual(found, [])
        self.assertEqual(len(skipped), 1, "越界路径要报「看不到」而不是静默忽略")

    def test_collect_images_counts_ignored_non_images_without_listing_them(self):
        """
        ★ 遍历目录时非图片**只计数、不逐条上报**：一个照片文件夹里混着几十个
          .txt/.docx 是常态，逐条塞进「跳过原因」会把真正要看的问题淹掉；
          但完全不吭声又会让用户想不通「选了 100 个文件、怎么只进来 80 张」。
        """
        with open(os.path.join(self.root, "trip", "note.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("不是照片")

        found, skipped, ignored = photos.collect_images(self.resolver, "share", ["trip"])
        self.assertEqual(len(found), 2, "只该收 2 张图")
        self.assertEqual(ignored, 1, "非图片要被计数")
        self.assertEqual(skipped, [], "但不必逐条列出来")

    def test_collect_images_reports_a_directly_selected_non_image(self):
        """用户**明确点选**一个非图片文件时，要如实说出原因。"""
        with open(os.path.join(self.root, "trip", "note.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("不是照片")
        found, skipped, _ignored = photos.collect_images(
            self.resolver, "share", ["trip/note.txt"])
        self.assertEqual(found, [])
        self.assertEqual(len(skipped), 1)
        self.assertIn("图片", skipped[0]["reason"])

    # -- 相册 ---------------------------------------------------------------

    def test_manual_album_add_and_remove(self):
        self._import_all()
        pid = self._by_name("a.jpg")["id"]
        album = photos.create_album(self.cfg, ADMIN, "精选", "manual", items=[pid])
        self.assertEqual(album["items"], [pid])

        photos.album_items(self.cfg, ADMIN, album["id"], [pid], "remove")
        albums = photos.load_state(self.cfg, ADMIN)["albums"]
        self.assertEqual(albums[0]["items"], [])

        # 移出相册不该影响照片本身
        self.assertEqual(len(self._library()["photos"]), 3)

    def test_smart_album_by_time_range(self):
        """「按时间段归类」—— 直接用时间轴上的区间建相册。"""
        album = photos.create_album(self.cfg, ADMIN, "2023 暑假", "smart",
                                    start="2023-07-01", end="2023-08-31")
        self.assertEqual(album["kind"], "smart")
        self.assertEqual(album["start"], "2023-07-01T00:00:00")
        self.assertEqual(album["end"], "2023-08-31T00:00:00")

    def test_smart_album_needs_at_least_one_bound(self):
        with self.assertRaises(photos.PhotoError):
            photos.create_album(self.cfg, ADMIN, "空区间", "smart")

    def test_smart_album_rejects_reversed_range(self):
        with self.assertRaises(photos.PhotoError):
            photos.create_album(self.cfg, ADMIN, "反了", "smart",
                                start="2024-01-01", end="2023-01-01")

    def test_smart_album_cannot_take_manual_items(self):
        album = photos.create_album(self.cfg, ADMIN, "按时间", "smart",
                                    start="2023-01-01")
        with self.assertRaises(photos.PhotoError):
            photos.album_items(self.cfg, ADMIN, album["id"], ["a" * 40], "add")

    def test_album_name_is_required_and_bounded(self):
        with self.assertRaises(photos.PhotoError):
            photos.create_album(self.cfg, ADMIN, "   ", "manual")
        album = photos.create_album(self.cfg, ADMIN, "长" * 300, "manual")
        self.assertLessEqual(len(album["name"]), photos.MAX_ALBUM_NAME)

    def test_delete_album_does_not_delete_photos(self):
        self._import_all()
        album = photos.create_album(self.cfg, ADMIN, "临时", "manual")
        self.assertTrue(photos.delete_album(self.cfg, ADMIN, album["id"]))
        self.assertEqual(len(self._library()["photos"]), 3)
        with self.assertRaises(photos.PhotoError):
            photos.delete_album(self.cfg, ADMIN, album["id"])

    def test_forget_source_removes_entries_but_never_deletes_files(self):
        """
        ★「移出相册」是最容易被误解的一步：它必须只动索引与编辑记录，
          绝不删用户的照片文件。所以这里真的检查文件还在。
        """
        self._import_all()
        result = photos.forget_source(self.cfg, ADMIN, "share", "misc")
        self.assertEqual(result["removed"], 1)

        self.assertTrue(os.path.isfile(os.path.join(self.root, "misc", "shot.png")),
                        "★ 移出相册绝不能删掉照片文件")

    # -- 偏好 ---------------------------------------------------------------

    def test_prefs_round_trip_and_are_sanitized(self):
        prefs = photos.save_prefs(self.cfg, ADMIN, {"level": "month", "sort": "name_asc"})
        self.assertEqual(prefs, {"level": "month", "sort": "name_asc"})
        self.assertEqual(self._library()["prefs"]["level"], "month")

        # 非法值退回默认，而不是原样存下去
        prefs = photos.save_prefs(self.cfg, ADMIN, {"level": "随便", "sort": "乱来"})
        self.assertEqual(prefs["level"], "day")
        self.assertEqual(prefs["sort"], "taken_desc")

    # -- 按用户分文件 -------------------------------------------------------

    def test_state_and_index_are_per_user_files(self):
        """
        ★ 索引必须按用户分开：它记的是「这个人能看到哪些文件」，
          两个子用户的可见目录完全不同，共用一份会让界面泄露别人的文件名。
        """
        student = {"username": "stu01", "role": "user"}
        self.assertNotEqual(photos.state_path(self.cfg, ADMIN),
                            photos.state_path(self.cfg, student))
        self.assertNotEqual(photos.index_path(self.cfg, ADMIN),
                            photos.index_path(self.cfg, student))
        self.assertIn("stu01", photos.state_path(self.cfg, student))
        # 管理员沿用不带后缀的那份
        self.assertTrue(photos.state_path(self.cfg, ADMIN).endswith("photos_state.json"))

    def test_a_student_with_his_own_root_cannot_see_the_admins_photos(self):
        self._import_all()
        pid = self._by_name("a.jpg")["id"]

        other = os.path.join(self.work, "other")
        make_jpeg(os.path.join(other, "mine.jpg"), (4, 4, 4), "2021:01:01 00:00:00")
        student = {"username": "stu01", "role": "user",
                   "roots": [{"id": "mine", "name": "mine", "path": other}]}
        student_resolver = PathResolver(
            [{"id": "mine", "name": "mine", "path": other}])

        photos.import_paths(self.cfg, student, student_resolver, "mine", [""])
        names = [p["name"] for p in photos.build_library(
            self.cfg, student, student_resolver)["photos"]]
        self.assertEqual(names, ["mine.jpg"], "学生只该看到自己那份索引")

        # 拿着管理员的 id 来读：读不到
        with self.assertRaises(photos.PhotoError):
            photos.resolve_photo(self.cfg, student, student_resolver, pid)
        # 拿着管理员的根标识来导入：被挡
        with self.assertRaises(photos.PhotoError):
            photos.import_paths(self.cfg, student, student_resolver, "share", ["trip"])


# ---------------------------------------------------------------------------
# 从浏览器所在的电脑上传
# ---------------------------------------------------------------------------

class PhotoUploadTests(unittest.TestCase):
    """上传：落点、命名、索引，以及「读不出上传目录之外」这条闸门。"""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="fw-photo-up-")
        self.root = os.path.join(self.work, "share")
        os.makedirs(self.root, exist_ok=True)
        self.resolver = PathResolver([
            {"id": "share", "name": "share", "path": self.root}])
        self.cfg = photo_cfg(self.work)

        # 造一张带 EXIF 的图放在「外面」，当作用户自己电脑上的文件
        self.source = make_jpeg(os.path.join(self.work, "outbox", "我的照片.jpg"),
                                (12, 34, 56), "2022:05:06 07:08:09")
        with open(self.source, "rb") as fh:
            self.blob = fh.read()

        # 另造一张**内容不同**、但**同名**的图：用来验证重名不覆盖
        other = make_jpeg(os.path.join(self.work, "outbox2", "我的照片.jpg"),
                          (200, 100, 50), "2023:01:02 03:04:05")
        with open(other, "rb") as fh:
            self.blob_other = fh.read()

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def _upload(self, filename="我的照片.jpg", blob=None, user=None):
        """走一遍「算落点 -> 写字节 -> 索引」，等价于路由层的上传。"""
        account = user or ADMIN
        name, target = photos.prepare_upload_target(self.cfg, account, filename)
        with open(target, "wb") as fh:
            fh.write(self.blob if blob is None else blob)
        item = photos.index_upload(self.cfg, account, self.resolver, target, name)
        # 与路由层保持一致：内容重复就把刚写下的那份删掉
        if item.get("duplicate"):
            os.unlink(target)
        return item, target

    def _library(self, user=None):
        account = user or ADMIN
        return photos.build_library(self.cfg, account,
                                    self.resolver if account is ADMIN
                                    else PathResolver([]))

    # -- 落点与命名 ---------------------------------------------------------

    def test_upload_lands_under_the_users_own_upload_dir(self):
        item, target = self._upload()
        expect_dir = os.path.join(self.cfg["photos"]["upload_dir"], "admin")
        self.assertTrue(target.startswith(expect_dir),
                        "上传应当落在 <upload_dir>/<用户名>/ 下：%s" % target)
        self.assertTrue(os.path.isfile(target))
        self.assertEqual(item["name"], "我的照片.jpg")

    def test_same_name_with_different_content_never_overwrites(self):
        """★ 上传是最容易踩到覆盖的一步，而覆盖掉别人的照片几乎无法挽回。"""
        first, target_a = self._upload()
        second, target_b = self._upload(blob=self.blob_other)

        self.assertNotEqual(target_a, target_b)
        self.assertTrue(os.path.isfile(target_a), "先上传的那张必须还在")
        self.assertEqual(os.path.basename(target_b), "我的照片 (1).jpg")
        self.assertFalse(first["duplicate"])
        self.assertFalse(second["duplicate"])

        library = self._library()
        self.assertEqual(len(library["photos"]), 2, "两张不同内容的照片都该在库里")
        # 两张都读得出各自的 EXIF 时间，说明确实是两份不同的数据
        self.assertEqual(sorted(p["taken_at"] for p in library["photos"]),
                         ["2022-05-06T07:08:09", "2023-01-02T03:04:05"])

    def test_auto_renamed_upload_records_the_name_it_actually_landed_on(self):
        """
        ★ 回归：重名自动改名之后，索引里记的 relpath 必须是**磁盘上真正的
          那个名字**（`我的照片 (1).jpg`），而不是用户原本请求的名字。

          早先这里直接用了 resolve_upload_target 回的那个「原请求名」，
          于是第二个条目指向了**第一个文件** —— 条目内容和实际文件对不上，
          界面上就会看到错误的缩略图 / 莫名其妙的「找不到」。
        """
        _first, _target_a = self._upload()
        second, target_b = self._upload(blob=self.blob_other)

        self.assertEqual(second["relpath"], os.path.basename(target_b))
        self.assertNotEqual(second["relpath"], "我的照片.jpg")

        # 条目指向的文件必须真的存在，而且就是刚上传的那一份
        upload_root = os.path.join(self.cfg["photos"]["upload_dir"], "admin")
        on_disk = os.path.join(upload_root, second["relpath"])
        self.assertTrue(os.path.isfile(on_disk), "索引指向的文件必须真的在磁盘上")
        with open(on_disk, "rb") as fh:
            self.assertEqual(fh.read(), self.blob_other)

        # 两条条目指向两个不同的文件，且都不 stale（大小/时间都对得上）
        library = self._library()
        paths = sorted(p["relpath"] for p in library["photos"])
        self.assertEqual(paths, ["我的照片 (1).jpg", "我的照片.jpg"])
        self.assertEqual([p["stale"] for p in library["photos"]], [False, False],
                         "条目与实际文件对不上时会被标成 stale —— 这里不该出现")

    def test_uploading_the_same_content_twice_is_reported_as_duplicate(self):
        """
        ★ 照片的身份就是**内容指纹**，所以同一张传两遍时索引里只能有一条。
          如果不拦，第二次会悄悄把第一条的位置顶掉，磁盘上留下一个永远
          不会被索引的副本（用户看不到、也不会去删）。这里要：
            1. 如实报 duplicate；
            2. 相册里只有一张；
            3. 第一条的位置没被动过；
            4. 刚写下的那份已经被删掉，不在磁盘上留孤儿。
        """
        first, target_a = self._upload()
        second, target_b = self._upload()          # 同一个 blob

        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"], "同一份内容应当被识别为重复")
        self.assertEqual(second["id"], first["id"], "内容相同就是同一个指纹")

        library = self._library()
        self.assertEqual(len(library["photos"]), 1, "相册里只该有一张")
        self.assertFalse(os.path.isfile(target_b), "★ 重复的那份不该留在磁盘上")

        # 第一次的位置必须原样保留（不能被第二次顶掉）
        self.assertTrue(os.path.isfile(target_a))
        self.assertTrue(library["photos"][0]["relpath"].startswith("我的照片"),
                        library["photos"][0]["relpath"])

    def test_re_uploading_restores_a_deleted_photo(self):
        """原来那张被删掉之后再传一次：位置应当补回来，而不是报「重复」。"""
        first, target_a = self._upload()
        os.unlink(target_a)
        photos.rescan(self.cfg, ADMIN, self.resolver)
        self.assertTrue(self._library()["photos"][0]["missing"])

        second, target_b = self._upload()
        self.assertFalse(second["duplicate"], "原位置已经不在了，这次是恢复而不是重复")
        photo = self._library()["photos"][0]
        self.assertFalse(photo["missing"])
        self.assertTrue(os.path.isfile(target_b))

    def test_non_image_is_rejected(self):
        for bad in ("病毒.exe", "说明.txt", "archive.zip", "没有扩展名"):
            with self.assertRaises(photos.PhotoError, msg="应当拒绝：%r" % bad):
                photos.prepare_upload_target(self.cfg, ADMIN, bad)

    def test_blocked_extension_is_rejected_even_with_an_image_suffix(self):
        # 图片扩展名 + 黑名单里的可执行后缀：两边都要挡得住
        with self.assertRaises(photos.PhotoError):
            photos.prepare_upload_target(self.cfg, ADMIN, "a.jpg.bat")

    def test_filename_is_sanitized(self):
        name, target = photos.prepare_upload_target(self.cfg, ADMIN, "../../evil.jpg")
        # 补齐目录后才能写，这里只关心「名字里的穿越成分被消掉了」
        self.assertNotIn("..", name)
        self.assertTrue(target.startswith(self.cfg["photos"]["upload_dir"]))

    # -- 安全闸门 -----------------------------------------------------------

    def test_safe_rel_rejects_traversal_and_absolute_paths(self):
        for bad in ("../x.jpg", "..\\x.jpg", "/etc/passwd", "C:\\Windows\\x.jpg",
                    "a/../../b.jpg", "", "   ", "."):
            with self.assertRaises(photos.PhotoError, msg="应当拒绝：%r" % bad):
                photos._safe_rel(bad)

    def test_upload_file_path_stays_inside_the_users_dir(self):
        """
        ★ 上传目录里的文件名会参与路径拼接，所以必须挡掉 `..` ——
          这个目录是**服务端自己写**的，但读取时仍要重新过闸门。
        """
        with self.assertRaises(photos.PhotoError):
            photos.upload_file_path(self.cfg, ADMIN, "../../../Windows/win.ini")

        ok = photos.upload_file_path(self.cfg, ADMIN, "我的照片.jpg")
        self.assertTrue(ok.startswith(os.path.join(
            self.cfg["photos"]["upload_dir"], "admin")))

    def test_a_tampered_upload_entry_cannot_escape(self):
        """把索引里的 relpath 改成 `../../`，读取必须被挡下来。"""
        item, _target = self._upload()
        index = photos.load_index(self.cfg, ADMIN)
        index["entries"][item["id"]]["relpath"] = "../../../../Windows/win.ini"
        photos._atomic_write(photos.index_path(self.cfg, ADMIN), index)

        with self.assertRaises(photos.PhotoError):
            photos.resolve_photo(self.cfg, ADMIN, self.resolver, item["id"])

        library = self._library()
        self.assertEqual(len(library["photos"]), 0, "坏条目要被跳过，而不是把文件交出去")
        self.assertEqual(library["inaccessible"], 1)

    def test_upload_root_cannot_be_reached_through_a_real_root_id(self):
        """上传条目的 root 是伪根，解析器里根本没有它 —— 不能借它绕过闸门。"""
        with self.assertRaises(photos.PhotoError):
            photos.resolve_entry(self.cfg, ADMIN, self.resolver,
                                 {"root": "__uploads__", "relpath": ""})

    # -- 进库与后续操作 -----------------------------------------------------

    def test_uploaded_photo_shows_up_with_its_exif_time(self):
        item, _target = self._upload()
        library = self._library()
        self.assertEqual(len(library["photos"]), 1)

        photo = library["photos"][0]
        self.assertEqual(photo["name"], "我的照片.jpg")
        self.assertEqual(photo["taken_at"], "2022-05-06T07:08:09",
                         "上传的照片同样要读 EXIF 时间")
        self.assertEqual(photo["source"], "exif")
        self.assertFalse(photo["missing"], "★ 上传完就该能正常显示，不该是「找不到」")

    def test_uploaded_photo_can_be_edited_like_any_other(self):
        """上传走的是同一条索引结构，所以编辑/相册/撤销这些能力一律适用。"""
        item, _target = self._upload()
        photos.apply_edits(self.cfg, ADMIN, [item["id"]],
                           {"taken_at": "2001-02-03 04:05:06",
                            "place": {"name": "上传测试地点"}})

        photo = self._library()["photos"][0]
        self.assertEqual(photo["taken_at"], "2001-02-03T04:05:06")
        self.assertEqual(photo["source"], "user")
        self.assertEqual(photo["place"]["name"], "上传测试地点")

    def test_rescan_verifies_uploaded_photos_instead_of_reporting_them_missing(self):
        """
        ★ 重扫里也要走同一条解析路径。漏掉的话，上传的照片会在重扫之后
          集体变成「找不到」—— 这是最容易漏、也最容易被当成数据丢了的一处。
        """
        self._upload()
        result = photos.rescan(self.cfg, ADMIN, self.resolver)
        self.assertEqual(result["verified"], 1, result)
        self.assertEqual(result["missing"], 0, result)
        self.assertFalse(self._library()["photos"][0]["missing"])

    def test_deleting_an_uploaded_file_shows_up_as_missing_not_as_a_crash(self):
        item, target = self._upload()
        os.unlink(target)
        photos.rescan(self.cfg, ADMIN, self.resolver)

        library = self._library()
        self.assertEqual(len(library["photos"]), 1, "条目要留着（编辑记录不能丢）")
        self.assertTrue(library["photos"][0]["missing"])

    # -- 按用户分目录 -------------------------------------------------------

    def test_each_user_uploads_into_his_own_directory(self):
        student = {"username": "stu01", "role": "user"}

        _item_a, target_a = self._upload()
        _item_b, target_b = self._upload(user=student)

        self.assertIn(os.path.join("photos_uploads", "admin"), target_a)
        self.assertIn(os.path.join("photos_uploads", "stu01"), target_b)
        self.assertNotEqual(os.path.dirname(target_a), os.path.dirname(target_b))

    def test_a_student_cannot_read_the_admins_upload(self):
        item, _target = self._upload()
        student = {"username": "stu01", "role": "user"}
        student_resolver = PathResolver([])

        # 学生的索引是另一个文件，根本看不到这条记录
        with self.assertRaises(photos.PhotoError):
            photos.resolve_photo(self.cfg, student, student_resolver, item["id"])

        # 就算把管理员的条目硬塞进学生的索引，也只能落在学生自己的上传目录里
        index = photos.load_index(self.cfg, student)
        index["entries"][item["id"]] = {
            "id": item["id"], "root": photos.UPLOAD_ROOT,
            "relpath": "我的照片.jpg", "size": 1, "mtime": 0.0,
            "w": 0, "h": 0, "exif_taken": "", "exif_tz": "",
            "gps": None, "camera": "", "lens": "", "iso": 0, "fnum": 0.0,
            "exposure": "", "added": 0.0,
        }
        photos._atomic_write(photos.index_path(self.cfg, student), index)

        resolved = photos.resolve_entry(self.cfg, student, student_resolver,
                                        {"root": photos.UPLOAD_ROOT,
                                         "relpath": "我的照片.jpg"})
        self.assertIn(os.path.join("photos_uploads", "stu01"), resolved,
                      "★ 学生拿到的必须是**他自己**目录下的同名文件，而不是管理员的")
        self.assertNotEqual(resolved, photos.upload_file_path(
            self.cfg, ADMIN, "我的照片.jpg"))

    def test_upload_dir_can_be_shared_with_upload_per_user_off(self):
        cfg = photo_cfg(self.work)
        cfg["photos"]["upload_per_user"] = False
        student = {"username": "stu01", "role": "user"}
        self.assertEqual(photos.upload_dir(cfg, ADMIN), photos.upload_dir(cfg, student))

    def test_missing_upload_dir_config_is_a_readable_error(self):
        cfg = {"photos": {"upload_dir": ""}}
        with self.assertRaises(photos.PhotoError):
            photos.prepare_upload_target(cfg, ADMIN, "a.jpg")


# ---------------------------------------------------------------------------
# 端到端：真的起一个服务
# ---------------------------------------------------------------------------

class PhotosApiTests(unittest.TestCase):
    """接口层：功能开关、认证、导入/编辑/看图、以及子用户的可见性。"""

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="fw-photo-api-")
        cls.root_a = os.path.join(cls.work, "root-a")
        cls.root_b = os.path.join(cls.work, "root-b")
        cls.trip = os.path.join(cls.root_a, "trip")
        os.makedirs(cls.root_b, exist_ok=True)

        make_jpeg(os.path.join(cls.trip, "a.jpg"), (200, 30, 30),
                  "2023:08:15 12:34:56",
                  {"lat_ref": "N", "lat": (36.0, 3.0, 40.0),
                   "lon_ref": "E", "lon": (120.0, 19.0, 10.0)})
        make_jpeg(os.path.join(cls.trip, "b.jpg"), (30, 200, 30),
                  "2024:01:02 03:04:05")
        make_plain_png(os.path.join(cls.trip, "shot.png"))
        with open(os.path.join(cls.trip, "notes.txt"), "w", encoding="utf-8") as fh:
            fh.write("这不是照片")
        # 学生自己的目录里放一张，用来验证按用户分索引
        make_jpeg(os.path.join(cls.root_b, "mine.jpg"), (4, 4, 4), "2021:01:01 00:00:00")

        def _extra(cfg):
            cfg["photos"]["enabled"] = True
            # 调小上限，方便测「超过大小就拒绝」（413）；其余用例传的都是几百字节的小图
            cfg["photos"]["max_upload_mb"] = 1

        cls.server = ServerProcess(
            [{"id": "main", "name": "main", "path": cls.root_a, "readonly": False}],
            username=ADMIN_USER, password=ADMIN_PASSWORD, extra_config=_extra,
        ).start()

        cls._saved_users_path = users_module.USERS_PATH
        users_module.set_path(os.path.join(
            os.path.dirname(cls.server.cfg_path), "users.json"))
        users_module.create("stu01", STUDENT_PASSWORD, display_name="甲同学", roots=[
            {"id": "private", "name": "我的空间", "path": cls.root_b, "readonly": False}])

    @classmethod
    def tearDownClass(cls):
        users_module.set_path(cls._saved_users_path)
        cls.server.stop()
        cls.server.cleanup()
        shutil.rmtree(cls.work, ignore_errors=True)

    def setUp(self):
        self.admin = self.server.login_client()
        self._reset_gallery()
        self._indexed = False

    # -- 工具 ---------------------------------------------------------------

    def _reset_gallery(self):
        """
        把相册数据清空（直接删掉两个状态文件）。

        ★ 为什么必须做：库是**按用户存在磁盘上**的，整个测试类共用一个服务，
          所以用例之间会互相影响 —— 「导入 3 张」在别的用例已经导过之后会
          变成「导入 0 张」，断言就会随执行顺序漂移。
          索引与编辑都是每次请求现读磁盘，所以删掉文件就等于重置
          （不需要重启服务，也没有内存缓存要清）。
        """
        work = os.path.dirname(self.server.cfg_path)
        for name in os.listdir(work):
            if name.startswith(("photos_state", "photo_index")):
                try:
                    os.unlink(os.path.join(work, name))
                except OSError:
                    pass

    def _library(self, client=None):
        status, data = (client or self.admin).json("GET", "/api/photos/library")
        self.assertEqual(status, 200, data)
        return data

    def _ensure_indexed(self):
        """索引是共享的（同一个服务、同一个用户），所以只导一次。"""
        if self._indexed:
            return
        status, data = self.admin.json(
            "POST", "/api/photos/import",
            {"root": "main", "paths": ["trip"], "background": False})
        self.assertEqual(status, 200, data)
        self._indexed = True

    def _by_name(self, name, client=None):
        for item in self._library(client)["photos"]:
            if item["name"] == name:
                return item
        self.fail("库里没有 %s" % name)

    def _student(self):
        client = self.server.client()
        status, data = client.login("stu01", STUDENT_PASSWORD)
        self.assertEqual(status, 200, "登录学生失败：%s" % data)
        return client

    # -- 功能开关 -----------------------------------------------------------

    def test_system_info_exposes_the_photos_feature_flag(self):
        """
        ★ 界面与接口必须用同一个判断。前端靠 features.photos 决定要不要显示入口，
          后端靠 photos.enabled 决定要不要处理请求 —— 两边跑偏就会出现
          「入口在、点了就 403」或者「功能关了、入口还挂着」。
        """
        status, data = self.admin.json("GET", "/api/system/info")
        self.assertEqual(status, 200, data)
        self.assertIs(data["features"].get("photos"), True)

    # -- 导入与时间轴 -------------------------------------------------------

    def test_import_then_timeline(self):
        status, data = self.admin.json(
            "POST", "/api/photos/import",
            {"root": "main", "paths": ["trip"], "background": False})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["imported"], 3, "只该索引 3 张图，跳过 .txt")
        self.assertEqual(data["ignored"], 1, "目录里的 notes.txt 应当被计为「忽略」")
        self.assertNotIn("notes.txt", json.dumps(data["skipped"], ensure_ascii=False),
                         "遍历目录时的非图片只计数、不逐条上报（否则真实问题会被淹掉）")

        # 用户**明确点选**一个非图片时，才逐条说明原因
        status, picked = self.admin.json(
            "POST", "/api/photos/import",
            {"root": "main", "paths": ["trip/notes.txt"], "background": False})
        self.assertEqual(status, 400, picked)
        self.assertIn("图片", json.dumps(picked, ensure_ascii=False))

        library = self._library()
        self.assertEqual(library["stats"]["total"], 3)
        self.assertEqual(library["stats"]["exif"], 2)
        self.assertEqual(library["stats"]["file"], 1)
        self.assertEqual([p["name"] for p in library["photos"]][-1], "a.jpg")

    def test_import_requires_paths(self):
        status, data = self.admin.json(
            "POST", "/api/photos/import", {"root": "main", "paths": []})
        self.assertEqual(status, 400, data)

    def test_rescan_endpoint(self):
        self._ensure_indexed()
        status, data = self.admin.json("POST", "/api/photos/rescan", {})
        self.assertEqual(status, 200, data)
        self.assertIn("message", data)

    def test_sources_endpoint(self):
        self._ensure_indexed()
        status, data = self.admin.json("GET", "/api/photos/sources")
        self.assertEqual(status, 200, data)
        self.assertTrue(any(s["path"] == "trip" for s in data["sources"]), data)
        self.assertTrue(all(s["exists"] for s in data["sources"]))

    # -- 看图 ---------------------------------------------------------------

    def test_thumb_returns_a_real_jpeg(self):
        self._ensure_indexed()
        pid = self._by_name("a.jpg")["id"]

        status, headers, body = self.admin.raw_get("/api/photos/thumb?id=" + pid)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "image/jpeg")
        self.assertTrue(body.startswith(b"\xff\xd8"), "应当是真的 JPEG 数据")
        self.assertGreater(len(body), 100)

    def test_thumb_for_a_unknown_id_is_404_not_500(self):
        status, _headers, _body = self.admin.raw_get("/api/photos/thumb?id=" + "f" * 40)
        self.assertEqual(status, 404)

    def test_thumb_supports_a_custom_box(self):
        """画廊缩略图比资源管理器的大；缓存键里带尺寸，两者不会打架。"""
        self._ensure_indexed()
        pid = self._by_name("a.jpg")["id"]
        small = self.admin.raw_get("/api/photos/thumb?id=%s&box=64" % pid)
        large = self.admin.raw_get("/api/photos/thumb?id=%s&box=640" % pid)
        self.assertEqual(small[0], 200)
        self.assertEqual(large[0], 200)
        self.assertGreater(len(large[2]), len(small[2]), "640 的缩略图应当更大")

    def test_raw_supports_range(self):
        """大图查看靠 Range：不支持的话浏览器没法分段取，也没法快速预览。"""
        self._ensure_indexed()
        pid = self._by_name("a.jpg")["id"]

        status, headers, body = self.admin.raw_get(
            "/api/photos/raw?id=" + pid, headers={"Range": "bytes=0-9"})
        self.assertEqual(status, 206, "带 Range 的请求应当返回 206")
        self.assertEqual(len(body), 10)
        self.assertTrue(headers.get("Content-Range", "").startswith("bytes 0-9/"))

    def test_item_detail_exposes_exif_and_effective_values(self):
        self._ensure_indexed()
        pid = self._by_name("a.jpg")["id"]

        status, data = self.admin.json("GET", "/api/photos/item?id=" + pid)
        self.assertEqual(status, 200, data)
        photo = data["photo"]
        self.assertEqual(photo["exif"]["taken_at"], "2023-08-15T12:34:56")
        self.assertEqual(photo["exif"]["iso"], 200)
        self.assertAlmostEqual(photo["exif"]["gps"]["lat"], 36.061111, places=5)
        self.assertEqual(photo["source"], "exif")
        self.assertEqual(photo["taken_at"], "2023-08-15T12:34:56")

    # -- 编辑 ---------------------------------------------------------------

    def test_edit_and_read_back(self):
        self._ensure_indexed()
        pid = self._by_name("b.jpg")["id"]

        status, data = self.admin.json("POST", "/api/photos/edit", {
            "ids": [pid],
            "patch": {"taken_at": "2019-05-05 05:05:05",
                      "place": {"name": "北京·故宫"},
                      "tags": ["旅行", "家人"], "rating": 5,
                      "caption": "晴天"},
        })
        self.assertEqual(status, 200, data)
        self.assertEqual(data["updated"], 1)

        item = self._by_name("b.jpg")
        self.assertEqual(item["taken_at"], "2019-05-05T05:05:05")
        self.assertEqual(item["source"], "user")
        self.assertEqual(item["place"]["name"], "北京·故宫")
        self.assertEqual(item["tags"], ["旅行", "家人"])
        self.assertEqual(item["rating"], 5)
        self.assertEqual(item["caption"], "晴天")

    def test_edit_accepts_flat_fields_too(self):
        self._ensure_indexed()
        pid = self._by_name("b.jpg")["id"]
        status, data = self.admin.json("POST", "/api/photos/edit",
                                       {"ids": [pid], "rating": 3})
        self.assertEqual(status, 200, data)
        self.assertEqual(self._by_name("b.jpg")["rating"], 3)

    def test_edit_without_content_is_400(self):
        self._ensure_indexed()
        pid = self._by_name("a.jpg")["id"]
        status, data = self.admin.json("POST", "/api/photos/edit", {"ids": [pid]})
        self.assertEqual(status, 400, data)

    def test_edit_with_a_bad_time_is_400_not_500(self):
        self._ensure_indexed()
        pid = self._by_name("a.jpg")["id"]
        status, data = self.admin.json("POST", "/api/photos/edit",
                                       {"ids": [pid], "taken_at": "昨天下午"})
        self.assertEqual(status, 400, data)

    def test_batch_time_shift_and_undo(self):
        self._ensure_indexed()
        before = {p["name"]: p["taken_at"] for p in self._library()["photos"]}
        ids = [p["id"] for p in self._library()["photos"]]

        status, data = self.admin.json("POST", "/api/photos/batch/time",
                                       {"ids": ids, "delta_hours": 8})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["mode"], "shift")

        after = {p["name"]: p["taken_at"] for p in self._library()["photos"]}
        self.assertEqual(after["a.jpg"], "2023-08-15T20:34:56")

        status, data = self.admin.json("POST", "/api/photos/undo", {})
        self.assertEqual(status, 200, data)
        self.assertEqual({p["name"]: p["taken_at"] for p in self._library()["photos"]},
                         before, "撤销之后应当逐字段回到原样")

    def test_batch_time_set_to_one_value(self):
        self._ensure_indexed()
        ids = [p["id"] for p in self._library()["photos"]]
        status, data = self.admin.json("POST", "/api/photos/batch/time",
                                       {"ids": ids, "set_to": "2000-01-01 00:00:00"})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["mode"], "set")
        for item in self._library()["photos"]:
            self.assertEqual(item["taken_at"], "2000-01-01T00:00:00")

        self.admin.json("POST", "/api/photos/undo", {})

    def test_batch_time_without_a_delta_is_400(self):
        self._ensure_indexed()
        status, data = self.admin.json("POST", "/api/photos/batch/time",
                                       {"ids": ["a" * 40]})
        self.assertEqual(status, 400, data)

    def test_undo_without_history_is_400(self):
        status, data = self.admin.json("POST", "/api/photos/undo", {})
        self.assertEqual(status, 400, data)

    # -- 相册 ---------------------------------------------------------------

    def test_album_lifecycle(self):
        self._ensure_indexed()
        pid = self._by_name("a.jpg")["id"]

        status, data = self.admin.json("POST", "/api/photos/albums",
                                       {"name": "精选", "kind": "manual",
                                        "items": [pid]})
        self.assertEqual(status, 200, data)
        album_id = data["album"]["id"]

        status, data = self.admin.json("POST", "/api/photos/albums",
                                       {"name": "2023 夏", "kind": "smart",
                                        "start": "2023-06-01", "end": "2023-09-01"})
        self.assertEqual(status, 200, data)

        library = self._library()
        self.assertEqual(len(library["albums"]), 2)

        status, data = self.admin.json("POST", "/api/photos/albums/rename",
                                       {"id": album_id, "name": "最好的"})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["album"]["name"], "最好的")

        status, data = self.admin.json("POST", "/api/photos/albums/items",
                                       {"id": album_id, "ids": [pid],
                                        "action": "remove"})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["count"], 0)

        status, data = self.admin.json("POST", "/api/photos/albums/delete",
                                       {"id": album_id})
        self.assertEqual(status, 200, data)
        self.assertEqual(len(self._library()["albums"]), 1)

        # 删相册不该删照片
        self.assertEqual(len(self._library()["photos"]), 3)

    def test_prefs_endpoint(self):
        status, data = self.admin.json("POST", "/api/photos/prefs", {"level": "year"})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["prefs"]["level"], "year")
        self.admin.json("POST", "/api/photos/prefs", {"level": "day"})

    # -- 多用户可见性 -------------------------------------------------------

    def test_a_student_cannot_see_the_admins_photos(self):
        """
        ★★ 最要紧的一条：相册不能成为绕过文件可见性的旁路。
        """
        self._ensure_indexed()
        admin_pid = self._by_name("a.jpg")["id"]

        student = self._student()
        library = self._library(student)
        self.assertEqual(library["photos"], [],
                         "★ 学生不该看到管理员索引的照片")

        status, data = student.json("GET", "/api/photos/item?id=" + admin_pid)
        self.assertEqual(status, 400, "拿别人的 id 取详情要被拒：%s" % data)

        status, _headers, _body = student.raw_get("/api/photos/thumb?id=" + admin_pid)
        self.assertEqual(status, 404)

        status, _headers, _body = student.raw_get("/api/photos/raw?id=" + admin_pid)
        self.assertEqual(status, 404)

        # 学生也不能用管理员的根标识导入
        status, data = student.json("POST", "/api/photos/import",
                                    {"root": "main", "paths": ["trip"]})
        self.assertEqual(status, 400, "越权导入要被拒：%s" % data)

    def test_a_student_indexes_his_own_directory(self):
        student = self._student()
        status, data = student.json("POST", "/api/photos/import",
                                    {"root": "private", "paths": [""]})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["imported"], 1, data)

        library = self._library(student)
        self.assertEqual([p["name"] for p in library["photos"]], ["mine.jpg"])

        # 管理员的库不受影响（索引按用户分文件）
        self.assertNotIn("mine.jpg", [p["name"] for p in self._library()["photos"]])

    def test_anonymous_requests_are_rejected(self):
        anon = self.server.client()
        status, _data = anon.json("GET", "/api/photos/library")
        self.assertEqual(status, 401)

    # -- 从浏览器所在的电脑上传 ---------------------------------------------

    def _upload(self, filename, blob, client=None):
        from urllib.parse import quote
        return (client or self.admin).raw_post(
            "/api/photos/upload?filename=" + quote(filename), blob)

    def test_upload_from_the_browser_appears_in_the_library(self):
        """★ 用户在自己电脑上选一张照片传上来，应当立刻出现在时间轴上。"""
        blob = jpeg_bytes((190, 80, 40), "2022:05:06 07:08:09")
        status, data = self._upload("我的照片.jpg", blob)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["photo"]["name"], "我的照片.jpg")
        self.assertFalse(data["duplicate"])

        library = self._library()
        self.assertEqual(len(library["photos"]), 1)
        photo = library["photos"][0]
        self.assertEqual(photo["taken_at"], "2022-05-06T07:08:09",
                         "上传的照片同样要读 EXIF 时间")
        self.assertFalse(photo["missing"], "上传完就该能正常显示")

        # 缩略图与原图都要能取
        status, headers, body = self.admin.raw_get(
            "/api/photos/thumb?id=" + photo["id"])
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"\xff\xd8"))
        status, _headers, raw = self.admin.raw_get("/api/photos/raw?id=" + photo["id"])
        self.assertEqual(status, 200)
        self.assertEqual(raw, blob, "原图应当就是刚上传的那份字节")

    def test_uploaded_photo_can_be_edited_through_the_api(self):
        blob = jpeg_bytes((30, 120, 200), "2022:05:06 07:08:09")
        status, data = self._upload("可编辑.jpg", blob)
        self.assertEqual(status, 200, data)
        pid = data["photo"]["id"]

        status, data = self.admin.json("POST", "/api/photos/edit", {
            "ids": [pid],
            "patch": {"taken_at": "2001-02-03 04:05:06", "place": {"name": "上传地点"}},
        })
        self.assertEqual(status, 200, data)

        status, detail = self.admin.json("GET", "/api/photos/item?id=" + pid)
        self.assertEqual(status, 200, detail)
        self.assertEqual(detail["photo"]["taken_at"], "2001-02-03T04:05:06")
        self.assertEqual(detail["photo"]["place"]["name"], "上传地点")

    def test_uploading_the_same_content_twice_reports_duplicate(self):
        """同一张传两遍：只留一份，而且明确告诉用户（不能悄悄少一张）。"""
        blob = jpeg_bytes((70, 70, 70), "2020:01:01 00:00:00")
        status, first = self._upload("重复.jpg", blob)
        self.assertEqual(status, 200, first)
        self.assertFalse(first["duplicate"])

        status, second = self._upload("重复.jpg", blob)
        self.assertEqual(status, 200, second)
        self.assertTrue(second["duplicate"], second)
        self.assertIn("完全相同", second["message"])

        self.assertEqual(len(self._library()["photos"]), 1,
                         "内容相同的照片在相册里只该出现一次")

    def test_upload_rejects_a_non_image(self):
        status, data = self._upload("说明.txt", b"this is not a photo at all")
        self.assertEqual(status, 400, data)
        self.assertIn("图片", json.dumps(data, ensure_ascii=False))

    def test_upload_rejects_an_executable_extension(self):
        status, data = self._upload("木马.exe", jpeg_bytes())
        self.assertEqual(status, 400, data)

    def test_upload_without_a_filename_is_rejected(self):
        status, data = self.admin.raw_post("/api/photos/upload", jpeg_bytes())
        self.assertEqual(status, 400, data)

    def test_upload_over_the_size_limit_is_rejected(self):
        """超过 photos.max_upload_mb 的要在写入前就被拒掉（413）。"""
        blob = noisy_jpeg_bytes()
        self.assertGreater(len(blob), 1024 * 1024,
                           "噪声图应当大于 1MB，否则这条用例测不到上限")
        status, data = self._upload("太大了.jpg", blob)
        self.assertEqual(status, 413, data)

    def test_upload_requires_login(self):
        anon = self.server.client()
        status, _data = self._upload("匿名.jpg", jpeg_bytes(), client=anon)
        self.assertEqual(status, 401)

    def test_student_upload_does_not_leak_into_the_admin_library(self):
        """上传目录与索引都按用户分开：学生传的照片不该出现在管理员的相册里。"""
        student = self._student()
        blob = jpeg_bytes((123, 45, 67), "2019:09:09 09:09:09")
        status, data = self._upload("学生的照片.jpg", blob, client=student)
        self.assertEqual(status, 200, data)

        self.assertEqual(len(self._library(student)["photos"]), 1)
        self.assertEqual(self._library()["photos"], [],
                         "★ 学生上传的照片不该出现在管理员的相册里")

    def test_state_files_are_written_into_the_temp_dir(self):
        """
        ★ 反向守卫：确认照片的两个状态文件都落在临时目录里，
          而不是项目根目录 —— 否则测试会写脏真实部署。
        """
        self._ensure_indexed()
        work = os.path.dirname(self.server.cfg_path)
        self.assertTrue(os.path.isfile(os.path.join(work, "photos_state.json")),
                        "photos_state.json 应当写在临时目录里")
        self.assertTrue(os.path.isfile(os.path.join(work, "photo_index.json")),
                        "photo_index.json 应当写在临时目录里")


class PhotosDisabledTests(unittest.TestCase):
    """功能被服务端关掉时：接口 403，features 标记为 false。"""

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="fw-photo-off-")
        cls.root = os.path.join(cls.work, "root")
        os.makedirs(cls.root, exist_ok=True)
        make_jpeg(os.path.join(cls.root, "a.jpg"), (1, 2, 3), "2023:01:01 00:00:00")

        def _extra(cfg):
            cfg["photos"]["enabled"] = False

        cls.server = ServerProcess(
            [{"id": "main", "name": "main", "path": cls.root, "readonly": False}],
            username=ADMIN_USER, password=ADMIN_PASSWORD, extra_config=_extra,
        ).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.server.cleanup()
        shutil.rmtree(cls.work, ignore_errors=True)

    def test_endpoints_return_403(self):
        client = self.server.login_client()
        for method, path, body in (
            ("GET", "/api/photos/library", None),
            ("GET", "/api/photos/sources", None),
            ("GET", "/api/photos/item?id=" + "a" * 40, None),
            ("POST", "/api/photos/import", {"root": "main", "paths": ["a.jpg"]}),
            ("POST", "/api/photos/rescan", {}),
            ("POST", "/api/photos/edit", {"ids": ["a" * 40], "rating": 1}),
            ("POST", "/api/photos/undo", {}),
            ("POST", "/api/photos/prefs", {"level": "day"}),
        ):
            status, data = client.json(method, path, body)
            self.assertEqual(status, 403, "%s %s 应当是 403：%s" % (method, path, data))

    def test_feature_flag_is_false(self):
        client = self.server.login_client()
        status, data = client.json("GET", "/api/system/info")
        self.assertEqual(status, 200, data)
        self.assertIs(data["features"].get("photos"), False,
                      "★ 关掉之后界面必须拿到 false，否则入口还挂着、点了就 403")
