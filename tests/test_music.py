# -*- coding: utf-8 -*-
"""
音乐播放器的后端测试
====================

覆盖三块最要紧的东西：

1. **可见性与安全闸门**：导入走的是当前用户的路径解析器 —— 子用户导不进
   别人的目录；歌曲标识只认裸文件名 + 音频扩展名，`../` 这类写法既删不掉
   东西、也写不到库外面。
2. **曲库按用户分开**（默认）：我导入的歌不出现在同学的播放器里。
3. **能真的放**：`/api/music/stream` 支持 Range（进度条能拖），
   否则「拖动进度条」会退化成从头重放。

外加歌词识别/上传、歌单、播放偏好这些功能面的断言。

音频用**现场生成的合法 WAV**（不是改名的空文件）：这样 Range、字节数、
大小校验测的都是真实数据，而不是「假装是音频的字节流」。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import struct
import tempfile
import unittest
from urllib.parse import urlencode

from fileweb import music
from fileweb import users as users_module
from tests._harness import ServerProcess

ADMIN_USER = "tester"
ADMIN_PASSWORD = "test-password-123"
STUDENT_PASSWORD = "student-pass-123"


def make_wav(seconds: float = 0.1, rate: int = 8000) -> bytes:
    """
    生成一段合法的 PCM WAV（默认 0.1 秒静音，约 1.6KB）。

    为什么不用「改名的空文件」：那样 Range、Content-Length、大小校验测到的
    都是假数据 —— 而这几条正是播放能不能用的关键。
    """
    frames = max(1, int(rate * seconds))
    data = b"\x00\x00" * frames          # 16bit 单声道静音
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(data))
    return header + data


LRC_TEXT = "[00:01.00]第一句歌词\n[00:05.50]第二句歌词\n"


class SongIdGuardTests(unittest.TestCase):
    """歌曲标识的校验（纯函数，不启服务）。"""

    def test_accepts_a_plain_audio_filename(self):
        self.assertEqual(music._safe_song_id("晴天.mp3"), "晴天.mp3")
        self.assertEqual(music._safe_song_id("a b.flac"), "a b.flac")

    def test_rejects_traversal_and_separators(self):
        for bad in ("../evil.mp3", "..\\evil.mp3", "a/b.mp3", "a\\b.mp3",
                    "/etc/passwd.mp3", "C:\\Windows\\x.mp3", "..", "", "   "):
            with self.assertRaises(music.MusicError, msg="应当拒绝：%r" % bad):
                music._safe_song_id(bad)

    def test_rejects_non_audio_extensions(self):
        for bad in ("song.txt", "song.exe", "song.mp3.exe", "song"):
            with self.assertRaises(music.MusicError, msg="应当拒绝：%r" % bad):
                music._safe_song_id(bad)

    def test_parse_title_uses_the_artist_dash_title_convention(self):
        self.assertEqual(music.parse_title("周杰伦 - 晴天.mp3"), ("晴天", "周杰伦"))
        self.assertEqual(music.parse_title("song.mp3"), ("song", ""))
        # 不能拿单独的连字符当分隔符：My-Song 会被切得莫名其妙
        self.assertEqual(music.parse_title("My-Song.flac"), ("My-Song", ""))


class MusicApiTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="fw-music-")
        cls.root_a = os.path.join(cls.work, "root-a")
        cls.root_b = os.path.join(cls.work, "root-b")
        cls.songs_src = os.path.join(cls.root_a, "songs")
        for directory in (cls.songs_src, cls.root_b):
            os.makedirs(directory, exist_ok=True)

        # 一个可以导入的音频 + 同名歌词；一个不能导入的文本
        with open(os.path.join(cls.songs_src, "晴天.wav"), "wb") as fh:
            fh.write(make_wav(0.1))
        with open(os.path.join(cls.songs_src, "晴天.lrc"), "w", encoding="utf-8") as fh:
            fh.write(LRC_TEXT)
        with open(os.path.join(cls.songs_src, "readme.txt"), "w", encoding="utf-8") as fh:
            fh.write("这不是音频")

        def _extra(cfg):
            cfg["music"]["enabled"] = True
            cfg["music"]["per_user"] = True
            cfg["music"]["max_upload_mb"] = 1          # 方便测 413

        cls.server = ServerProcess(
            [{"id": "main", "name": "main", "path": cls.root_a, "readonly": False}],
            username=ADMIN_USER, password=ADMIN_PASSWORD, extra_config=_extra,
        ).start()

        cls._saved_users_path = users_module.USERS_PATH
        users_module.set_path(os.path.join(
            os.path.dirname(cls.server.cfg_path), "users.json"))
        users_module.create("stu01", STUDENT_PASSWORD, display_name="甲同学", roots=[
            {"id": "private", "name": "我的空间", "path": cls.root_b, "readonly": False}])
        users_module.create("stu02", STUDENT_PASSWORD, display_name="乙同学", roots=[
            {"id": "private", "name": "我的空间", "path": cls.root_b, "readonly": False}])

    @classmethod
    def tearDownClass(cls):
        users_module.set_path(cls._saved_users_path)
        cls.server.stop()
        cls.server.cleanup()
        shutil.rmtree(cls.work, ignore_errors=True)

    # -- 工具 ---------------------------------------------------------------

    def setUp(self):
        self.admin = self.server.login_client()

    def _library(self, client=None):
        status, data = (client or self.admin).json("GET", "/api/music/library")
        self.assertEqual(status, 200, data)
        return data

    def _ids(self, client=None):
        return [song["id"] for song in self._library(client)["songs"]]

    def _import(self, client, paths, root="main"):
        return client.json("POST", "/api/music/import", {"root": root, "paths": paths})

    def _student(self, username="stu01"):
        client = self.server.client()
        status, data = client.login(username, STUDENT_PASSWORD)
        self.assertEqual(status, 200, "登录 %s 失败：%s" % (username, data))
        return client

    # -- 库与导入 -----------------------------------------------------------

    def test_import_copies_the_song_and_its_lyrics(self):
        # 用**独有**的文件名：本类的曲库是共享的，别的用例可能已经导入过同名文件，
        # 那样会被自动改名成 `xxx (1).wav`，断言具体名字就会随执行顺序漂移。
        source = os.path.join(self.songs_src, "带歌词的歌.wav")
        with open(source, "wb") as fh:
            fh.write(make_wav(0.05))
        with open(os.path.join(self.songs_src, "带歌词的歌.lrc"), "w",
                  encoding="utf-8") as fh:
            fh.write(LRC_TEXT)

        status, data = self._import(self.admin, ["songs/带歌词的歌.wav"])
        self.assertEqual(status, 200, data)
        self.assertEqual(len(data["imported"]), 1, data)

        song = data["imported"][0]
        self.assertEqual(song["id"], "带歌词的歌.wav")
        self.assertEqual(song["title"], "带歌词的歌")
        self.assertTrue(song["lyrics"], "★ 同名 .lrc 应当被一起带进来")

        listed = [s for s in self._library()["songs"] if s["id"] == "带歌词的歌.wav"]
        self.assertEqual(len(listed), 1)
        self.assertTrue(listed[0]["has_lyrics"], "列表里应当标出「有歌词」")

    def test_import_never_overwrites_an_existing_song(self):
        source = os.path.join(self.songs_src, "不许覆盖.wav")
        with open(source, "wb") as fh:
            fh.write(make_wav(0.05))

        first = self._import(self.admin, ["songs/不许覆盖.wav"])[1]["imported"][0]["id"]
        second = self._import(self.admin, ["songs/不许覆盖.wav"])[1]["imported"][0]

        self.assertNotEqual(second["id"], first, "重名必须自动改名，绝不覆盖")
        self.assertTrue(second["renamed"])
        self.assertIn(first, self._ids(), "原来那首不能被动过")
        self.assertIn(second["id"], self._ids())

    def test_import_rejects_unsupported_files(self):
        status, data = self._import(self.admin, ["songs/readme.txt"])
        self.assertEqual(status, 400, data)
        self.assertIn("不支持", json.dumps(data, ensure_ascii=False))

    def test_import_reports_skips_when_only_some_succeed(self):
        status, data = self._import(
            self.admin, ["songs/晴天.wav", "songs/readme.txt"])
        self.assertEqual(status, 200, data)
        self.assertEqual(len(data["imported"]), 1)
        self.assertEqual(len(data["skipped"]), 1)
        self.assertIn("readme.txt", data["skipped"][0]["name"])

    def test_admin_can_import_from_the_configured_root(self):
        """基线：管理员的可见范围里有这个目录，所以导得进来。"""
        status, data = self._import(self.admin, ["songs/晴天.wav"])
        self.assertEqual(status, 200, data)

    # -- ★ 按用户分开 -------------------------------------------------------

    def test_each_user_gets_his_own_library(self):
        """★ 我导入的歌不该出现在同学的播放器里。"""
        student_a = self._student("stu01")
        student_b = self._student("stu02")

        before = self._ids(student_b)
        status, data = self._import(student_a, ["songs/晴天.wav"], root="private")
        self.assertEqual(status, 400,
                         "前提：stu01 的根里没有这首歌（源的可见性要生效）：%s" % (data,))

        # 换一个真在 stu01 根里的文件
        source = os.path.join(self.root_b, "甲的歌.wav")
        with open(source, "wb") as fh:
            fh.write(make_wav(0.05))

        status, data = self._import(student_a, ["甲的歌.wav"], root="private")
        self.assertEqual(status, 200, data)

        self.assertIn("甲的歌.wav", self._ids(student_a))
        self.assertNotIn("甲的歌.wav", self._ids(student_b),
                         "★ 同学的曲库不该出现我的歌")
        self.assertEqual(self._ids(student_b), before)

    def test_student_cannot_import_from_a_folder_he_cannot_see(self):
        """
        ★ 导入走当前用户的解析器：看不见的路径导不进来。

        这条和文件管理器是同一套口径 —— 播放器不能成为「绕过可见性」的旁路。
        """
        student = self._student("stu01")
        before = self._ids(student)          # 别的用例可能已经往他的库里导过东西

        # `main` 是管理员那个根（root-a），stu01 没被分配，用 id 也访问不到
        status, data = self._import(student, ["songs/晴天.wav"], root="main")
        self.assertEqual(status, 400, data)
        self.assertEqual(self._ids(student), before, "★ 不该有任何东西被导进来")

    def test_student_library_dir_is_a_subfolder(self):
        """库目录落在自己名下（库里能看到路径，便于排查）。"""
        student = self._student("stu01")
        library = self._library(student)["library"]
        self.assertTrue(library["per_user"])
        self.assertTrue(library["dir"].rstrip("\\/").endswith("stu01"),
                        "每人的库目录应当是自己的名字：%s" % library["dir"])

    # -- 上传 ---------------------------------------------------------------

    def test_upload_song(self):
        payload = make_wav(0.05)
        status, data = self.admin.raw_post(
            "/api/music/upload?" + urlencode({"filename": "上传的歌.wav"}), payload)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["song"]["size"], len(payload))
        self.assertIn("上传的歌.wav", self._ids())

    def test_upload_rejects_unsupported_extension(self):
        status, data = self.admin.raw_post(
            "/api/music/upload?" + urlencode({"filename": "bad.txt"}), b"hello")
        self.assertEqual(status, 400, data)

    def test_upload_rejects_oversized_file(self):
        """
        超过 music.max_upload_mb 直接 413（本类配的是 1MB）。

        ★ 这里可能拿不到响应而是连接被重置 —— 服务端一看 Content-Length 就拒绝了，
        并不会把 2MB 读完。这是**有意**的（省带宽、省磁盘），与壁纸上传同款做法；
        代价是客户端可能只看到网络错误，所以前端会**先按 File.size 自查一次**，
        正常操作下用户看到的是干净的「文件太大」提示。

        因此本用例真正的硬断言是「没有被收下」，响应码是能拿到才核对。
        """
        big = b"\x00" * (2 * 1024 * 1024)
        try:
            status, data = self.admin.raw_post(
                "/api/music/upload?" + urlencode({"filename": "big.wav"}), big)
        except Exception:  # noqa: BLE001 - 服务端提前拒绝导致连接重置
            status, data = 0, {"_reset": True}

        self.assertIn(status, (0, 413), "太大时应当拒绝：%s %s" % (status, data))
        self.assertNotIn("big.wav", self._ids(), "被拒的上传不该留下文件")

    # -- ★ 播放（Range） ----------------------------------------------------

    def test_stream_supports_range_so_the_progress_bar_can_seek(self):
        """
        ★ 没有 Range 支持，进度条就只能从头播到尾、拖一下重来。

        这里同时核对字节内容：分段拿到的必须是**那一段**，而不是整个文件。
        """
        payload = make_wav(0.2)
        self.admin.raw_post(
            "/api/music/upload?" + urlencode({"filename": "range.wav"}), payload)

        # 整段
        status, headers, body = self.admin.raw_get(
            "/api/music/stream?" + urlencode({"id": "range.wav"}))
        self.assertEqual(status, 200)
        self.assertEqual(len(body), len(payload))
        self.assertEqual(headers.get("Accept-Ranges"), "bytes",
                         "必须声明支持 Range，否则浏览器不会去拖")
        self.assertEqual(body, payload, "输出的应当就是文件本身")

        # 前 100 字节
        status, headers, body = self.admin.raw_get(
            "/api/music/stream?" + urlencode({"id": "range.wav"}),
            headers={"Range": "bytes=0-99"})
        self.assertEqual(status, 206, "带 Range 的请求应当回 206 Partial Content")
        self.assertEqual(len(body), 100)
        self.assertEqual(body, payload[:100])
        self.assertIn("bytes 0-99/", headers.get("Content-Range", ""))

        # 中段：确认偏移是对的
        status, headers, body = self.admin.raw_get(
            "/api/music/stream?" + urlencode({"id": "range.wav"}),
            headers={"Range": "bytes=200-299"})
        self.assertEqual(status, 206)
        self.assertEqual(body, payload[200:300])

    def test_stream_rejects_a_traversal_id(self):
        status, _headers, _body = self.admin.raw_get(
            "/api/music/stream?" + urlencode({"id": "../config.json"}))
        self.assertEqual(status, 400)

    def test_stream_missing_song_is_404(self):
        status, _headers, _body = self.admin.raw_get(
            "/api/music/stream?" + urlencode({"id": "nope.mp3"}))
        self.assertEqual(status, 404)

    # -- 删除 ---------------------------------------------------------------

    def test_delete_removes_the_song_and_its_lyrics(self):
        self.admin.raw_post(
            "/api/music/upload?" + urlencode({"filename": "del.wav"}), make_wav(0.05))
        self.admin.json("POST", "/api/music/lyrics",
                        {"id": "del.wav", "text": LRC_TEXT})

        library = self._library()
        self.assertIn("del.wav", [s["id"] for s in library["songs"]])
        self.assertTrue([s for s in library["songs"]
                         if s["id"] == "del.wav"][0]["has_lyrics"])

        status, data = self.admin.json("POST", "/api/music/delete", {"id": "del.wav"})
        self.assertEqual(status, 200, data)
        self.assertNotIn("del.wav", self._ids())

        # 歌词是附属物，一起删掉（否则下次导入同名歌会莫名带上旧歌词）
        status, data = self.admin.json("GET", "/api/music/lyrics?" + urlencode({"id": "del.wav"}))
        self.assertFalse(data.get("found"), "歌词应当随歌曲一起删掉")

        status, data = self.admin.json("POST", "/api/music/delete", {"id": "del.wav"})
        self.assertEqual(status, 404, data)

    def test_delete_rejects_a_traversal_id(self):
        """★ 删不掉库外面的东西。"""
        outside = os.path.join(self.work, "outside.mp3")
        with open(outside, "wb") as fh:
            fh.write(b"x")

        status, data = self.admin.json(
            "POST", "/api/music/delete", {"id": "../outside.mp3"})
        self.assertEqual(status, 400, data)
        self.assertTrue(os.path.isfile(outside), "★ 库外的文件必须原封不动")

    # -- 歌词 ---------------------------------------------------------------

    def test_lyrics_roundtrip_and_timestamp_detection(self):
        self.admin.raw_post(
            "/api/music/upload?" + urlencode({"filename": "lrc.wav"}), make_wav(0.05))

        status, data = self.admin.json("GET", "/api/music/lyrics?" + urlencode({"id": "lrc.wav"}))
        self.assertEqual(status, 200, data)
        self.assertFalse(data["found"], "还没上传时应当如实说「没有」")

        status, data = self.admin.json("POST", "/api/music/lyrics",
                                       {"id": "lrc.wav", "text": LRC_TEXT})
        self.assertEqual(status, 200, data)

        status, data = self.admin.json("GET", "/api/music/lyrics?" + urlencode({"id": "lrc.wav"}))
        self.assertTrue(data["found"])
        self.assertTrue(data["has_timestamps"], "带时间标签的歌词要能被识别出来")
        self.assertIn("第一句歌词", data["text"])

    def test_plain_text_lyrics_are_accepted_without_timestamps(self):
        self.admin.raw_post(
            "/api/music/upload?" + urlencode({"filename": "plain.wav"}), make_wav(0.05))
        self.admin.json("POST", "/api/music/lyrics",
                        {"id": "plain.wav", "text": "只有一行没有时间标签的歌词"})

        status, data = self.admin.json("GET", "/api/music/lyrics?" + urlencode({"id": "plain.wav"}))
        self.assertTrue(data["found"])
        self.assertFalse(data["has_timestamps"])

    def test_lyrics_for_a_missing_song_is_rejected(self):
        status, data = self.admin.json("POST", "/api/music/lyrics",
                                       {"id": "ghost.mp3", "text": LRC_TEXT})
        self.assertEqual(status, 400, data)

    def test_empty_lyrics_are_rejected(self):
        self.admin.raw_post(
            "/api/music/upload?" + urlencode({"filename": "empty.wav"}), make_wav(0.05))
        status, data = self.admin.json("POST", "/api/music/lyrics",
                                       {"id": "empty.wav", "text": "   \n  "})
        self.assertEqual(status, 400, data)

    # -- 歌单 ---------------------------------------------------------------

    def test_playlist_crud_and_membership(self):
        self.admin.raw_post(
            "/api/music/upload?" + urlencode({"filename": "pl.wav"}), make_wav(0.05))

        status, data = self.admin.json("POST", "/api/music/playlists", {"name": "我喜欢的"})
        self.assertEqual(status, 200, data)
        playlist = data["playlist"]
        self.assertEqual(playlist["songs"], [])

        # 同名歌单要拒绝（否则界面上会出现两个一模一样的，分不清）
        status, data = self.admin.json("POST", "/api/music/playlists", {"name": "我喜欢的"})
        self.assertEqual(status, 400, data)

        status, data = self.admin.json("POST", "/api/music/playlists/songs",
                                       {"id": playlist["id"], "song": "pl.wav"})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["playlist"]["songs"], ["pl.wav"])

        # 重复加同一首不该出现两条
        status, data = self.admin.json("POST", "/api/music/playlists/songs",
                                       {"id": playlist["id"], "song": "pl.wav"})
        self.assertEqual(data["playlist"]["songs"], ["pl.wav"])

        status, data = self.admin.json("POST", "/api/music/playlists/rename",
                                       {"id": playlist["id"], "name": "改过名的"})
        self.assertEqual(data["playlist"]["name"], "改过名的")

        status, data = self.admin.json("POST", "/api/music/playlists/songs",
                                       {"id": playlist["id"], "song": "pl.wav",
                                        "action": "remove"})
        self.assertEqual(data["playlist"]["songs"], [])

        status, data = self.admin.json("POST", "/api/music/playlists/delete",
                                       {"id": playlist["id"]})
        self.assertEqual(status, 200, data)
        self.assertEqual(self._library()["playlists"], [])

    def test_playlists_are_per_user(self):
        """★ 歌单是「我的」东西：同学看不到，也改不了。"""
        student_a = self._student("stu01")
        student_b = self._student("stu02")

        status, data = student_a.json("POST", "/api/music/playlists", {"name": "甲的歌单"})
        self.assertEqual(status, 200, data)

        self.assertEqual([p["name"] for p in self._library(student_a)["playlists"]],
                         ["甲的歌单"])
        self.assertEqual(self._library(student_b)["playlists"], [],
                         "★ 同学的歌单不该出现在我这里")
        self.assertEqual(self._library(self.admin)["playlists"], [],
                         "管理员也不该看到（歌单不是审计对象）")

    def test_playlist_delete_of_an_unknown_id_is_404(self):
        status, data = self.admin.json("POST", "/api/music/playlists/delete",
                                       {"id": "pl_nope"})
        self.assertEqual(status, 404, data)

    # -- 播放偏好 -----------------------------------------------------------

    def test_prefs_roundtrip(self):
        status, data = self.admin.json("POST", "/api/music/prefs",
                                       {"volume": 0.35, "mode": "single", "last": "x.wav"})
        self.assertEqual(status, 200, data)

        prefs = self._library()["prefs"]
        self.assertAlmostEqual(prefs["volume"], 0.35)
        self.assertEqual(prefs["mode"], "single")
        self.assertEqual(prefs["last"], "x.wav")

    def test_unknown_play_mode_falls_back_to_list(self):
        """非法值不该被存进去 —— 否则界面会拿到一个自己不认识的模式。"""
        self.admin.json("POST", "/api/music/prefs", {"mode": "乱写的"})
        self.assertEqual(self._library()["prefs"]["mode"], "list")

    def test_prefs_are_per_user(self):
        student_a = self._student("stu01")
        student_b = self._student("stu02")

        student_a.json("POST", "/api/music/prefs", {"volume": 0.1})
        self.assertAlmostEqual(self._library(student_a)["prefs"]["volume"], 0.1)
        self.assertAlmostEqual(self._library(student_b)["prefs"]["volume"], 0.8,
                               msg="★ 我把音量调到 10% 不该影响同学的耳朵")


class MusicDisabledTests(unittest.TestCase):
    """music.enabled = false 时整个功能关闭（与 terminal/sysmon 同一套口径）。"""

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="fw-music-off-")

        def _extra(cfg):
            cfg["music"]["enabled"] = False

        cls.server = ServerProcess(
            [{"id": "main", "name": "main", "path": cls.work, "readonly": False}],
            username=ADMIN_USER, password=ADMIN_PASSWORD, extra_config=_extra,
        ).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.server.cleanup()
        shutil.rmtree(cls.work, ignore_errors=True)

    def test_all_endpoints_are_refused(self):
        client = self.server.login_client()
        for method, path in (
            ("GET", "/api/music/library"),
            ("GET", "/api/music/lyrics?id=x.mp3"),
        ):
            status, data = client.json(method, path)
            self.assertEqual(status, 403, "%s %s：%s" % (method, path, data))

        status, data = client.json("POST", "/api/music/playlists", {"name": "x"})
        self.assertEqual(status, 403, data)

    def test_system_info_reports_music_as_off(self):
        client = self.server.login_client()
        status, data = client.json("GET", "/api/system/info")
        self.assertEqual(status, 200, data)
        self.assertFalse(data["features"]["music"],
                         "关掉之后前端不该显示音乐播放器入口")


if __name__ == "__main__":
    unittest.main(verbosity=2)
