# -*- coding: utf-8 -*-
"""
图片查看器的旋转与缩放（渲染契约守卫）
=====================================

★ 为什么这里是「静态契约检查」而不是普通的功能测试：

  要守的那条规矩**只有在真实浏览器里才有意义** ——
  `transform: rotate()` 只改变「画出来的样子」，**不改变元素的布局盒子**。
  unittest 里没有浏览器，跑不出这件事。所以退而求其次，钉住两样东西：

    1. 契约本身：谁负责交换宽高、谁必须保持原比例；
    2. 那个真实出过的 bug 不会被重新写回来（负向断言）。

背景（用户报的真实 bug）
------------------------
  400×200 的图片点「旋转 90°」之后，**看起来还是 400×200，而且严重畸变**。

  旧代码把「旋转后的尺寸」（200×400）直接写成了 `<img>` 的 width/height，
  同时又给它加 `transform: rotate(90deg)` —— 同一件事被做了两遍：

    * `<img>` 被强行撑成 200×400，而图片内容的原始比例是 2:1，
      填进 1:2 的框里就是横向压扁、纵向拉长（用户看到的「畸变严重」）；
    * 这个已经变形的盒子再被 transform 转 90°，屏幕上又回到 400×200。

  正确做法是把两件事分开：
    * `<img>` 永远保持**原始**宽高 × scale（内容绝不被拉伸），旋转只由 transform 负责；
    * 旋转后的**占位尺寸**交给外层 `.image-rotor` —— 它决定滚动区域与居中，
      所以大图旋转之后仍然能滚到每一个角。

  修复后已用真实 Chrome 渲染做过对照验证（源图 400×200，含一个正圆）：
    未旋转          → img 视觉外框 400×200，占位 400×200
    修复后旋转 90°  → img 视觉外框 200×400，占位 200×400，**正圆仍是正圆**
    旧写法旋转 90°  → img 被撑成 200×400，视觉外框又回到 400×200（即上述 bug）
"""

from __future__ import annotations

import io
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(ROOT, "static", "js")
CSS_DIR = os.path.join(ROOT, "static", "css")


def _read(directory: str, name: str) -> str:
    with io.open(os.path.join(directory, name), encoding="utf-8") as fh:
        return fh.read()


def _css_block(src: str, selector: str) -> str:
    """取出某个选择器的声明块（只匹配「选择器 紧跟 {」的那种）。"""
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", src)
    return match.group(1) if match else ""


class ImageRotationContractTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.js = _read(JS_DIR, "preview.js")
        cls.css = _read(CSS_DIR, "preview.css")

    # -- ① 图片元素必须保持原图比例 -----------------------------------------

    def test_img_keeps_its_natural_dimensions(self):
        """
        ★ 核心：`<img>` 的宽高只能来自**原始**尺寸 × scale。

        写成交换后的尺寸就会把内容拉伸 —— 这正是那个畸变 bug。

        正则刻意不绑定具体写法（先算成中间变量再赋值、还是直接写在一行，都行）：
        真正要紧的是「naturalW / naturalH 参与了 img 宽高的计算」，
        以及下面那条负向断言。
        """
        self.assertRegex(self.js, r"Math\.round\(naturalW\s*\*\s*scale\)",
                         "img 的宽度必须由 naturalW 算出（保持原图比例）")
        self.assertRegex(self.js, r"Math\.round\(naturalH\s*\*\s*scale\)",
                         "img 的高度必须由 naturalH 算出（保持原图比例）")
        self.assertRegex(self.js, r"img\.style\.width\s*=",
                         "应当真的把算出来的宽度写到 img 上")
        self.assertRegex(self.js, r"img\.style\.height\s*=",
                         "应当真的把算出来的高度写到 img 上")

    def test_img_size_is_never_taken_from_the_swapped_size(self):
        """
        ★ 负向断言：绝不允许把「旋转后的尺寸」写给 img。

        这一条就是那个 bug 的指纹 —— 只要有人再写成 `img.style.width = ... size.w`，
        这里立刻变红。
        """
        for axis in ("width", "height"):
            self.assertIsNone(
                re.search(r"img\.style\.%s\s*=[^;]*size\.%s" % (axis, "w" if axis == "width" else "h"),
                          self.js),
                "★ img.style.%s 不能取自 displaySize()（那会把内容拉伸）" % axis)

    # -- ② 旋转后的占位交给外层 ---------------------------------------------

    def test_rotor_takes_the_swapped_footprint(self):
        """外层 .image-rotor 负责占位，尺寸取 displaySize()（旋转 90° 时宽高互换）。"""
        self.assertIn("image-rotor", self.js,
                      "应当存在负责旋转占位的外层容器")
        self.assertRegex(
            self.js,
            r"rotor\.style\.width\s*=\s*Math\.max\(1,\s*Math\.round\(size\.w\s*\*\s*scale\)\)",
            "占位层的宽度应当取显示尺寸（旋转 90° 时是原图的高）")
        self.assertRegex(
            self.js,
            r"rotor\.style\.height\s*=\s*Math\.max\(1,\s*Math\.round\(size\.h\s*\*\s*scale\)\)",
            "占位层的高度应当取显示尺寸")

    def test_rotation_happens_around_the_image_centre(self):
        """旋转必须绕图片自身中心：transform 里要有 translate(-50%,-50%)。"""
        self.assertRegex(
            self.js,
            r"translate\(-50%,\s*-50%\)\s*rotate\(",
            "transform 应当先居中再旋转，否则旋转后的图片会偏出可视区域")

    def test_fit_uses_the_displayed_size(self):
        """
        适应窗口要按**旋转后**的尺寸算，否则竖过来的大图会被裁掉两头。
        """
        self.assertRegex(
            self.js,
            r"scale\s*=\s*Math\.min\(boxW\s*/\s*size\.w,\s*boxH\s*/\s*size\.h\)",
            "fit() 应当用 displaySize() 的结果算缩放")

    # -- ③ 尺寸标签要能看出旋转生效 -----------------------------------------

    def test_size_label_reports_the_displayed_orientation(self):
        """
        旋转之后标签要写出「显示 200 × 400」。

        只写文件本身的尺寸（400 × 200）会让人以为旋转没生效 ——
        这大概率是用户报这个问题时的一部分困惑来源。
        """
        self.assertIn("显示", self.js)
        self.assertRegex(self.js, r"naturalW\s*\+\s*' × '\s*\+\s*naturalH",
                         "标签里应当同时给出原始尺寸")

    # -- ④ CSS 侧的配套契约 -------------------------------------------------

    def test_rotor_is_the_positioning_context(self):
        rotor = _css_block(self.css, ".image-rotor")
        self.assertTrue(rotor, "CSS 里应当有 .image-rotor 规则")
        self.assertRegex(rotor, r"position\s*:\s*relative")

    def test_img_is_absolutely_positioned_inside_the_rotor(self):
        img = _css_block(self.css, ".image-stage img")
        self.assertTrue(img, "CSS 里应当有 .image-stage img 规则")
        self.assertRegex(img, r"position\s*:\s*absolute",
                         "img 必须脱离文档流，占位由 .image-rotor 负责")
        self.assertRegex(img, r"left\s*:\s*50%")
        self.assertRegex(img, r"top\s*:\s*50%")

    def test_stage_centres_with_auto_margins_instead_of_flex_centering(self):
        """
        ★ 居中方式不能改回 `justify-content: center`。

        用 flex 居中时，内容一旦比容器大，左右（上下）会**同时**溢出，
        而滚动容器滚不到负方向 —— 结果是大图或放大后左上角永远看不到。
        `margin: auto` 在空间不足时会塌成 0，溢出只发生在右下，全部可滚到。
        """
        stage = _css_block(self.css, ".image-stage")
        self.assertTrue(stage, "CSS 里应当有 .image-stage 规则")
        self.assertNotRegex(
            stage, r"justify-content\s*:\s*center",
            "★ 不要用 flex 居中：内容超出时左上角会滚不到")
        self.assertNotRegex(
            stage, r"align-items\s*:\s*center",
            "★ 同上")
        self.assertRegex(_css_block(self.css, ".image-rotor"), r"margin\s*:\s*auto",
                         "居中应当由 .image-rotor 的 margin: auto 负责")


if __name__ == "__main__":
    unittest.main(verbosity=2)
