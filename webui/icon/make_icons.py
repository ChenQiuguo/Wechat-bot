# -*- coding: utf-8 -*-
"""从 avatar-src.jpg 生成整套图标（favicon.ico 多尺寸 + icon-*.png + avatar.png）。

用法：python webui/icon/make_icons.py
换图：替换 avatar-src.jpg 后重跑本脚本即可；改完记得把 index.html 里 favicon 的
`?v=` 版本号加一，否则 Edge 会用缓存的旧图标。

小尺寸策略（16–48px）：裁到脸部 + 单步 LANCZOS 缩放 + UnsharpMask 锐化，
否则 24/32px 会糊成一团粉（任务栏那个位置就是它）。
"""
import os

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "avatar-src.jpg")
SIZES_SMALL = (16, 20, 24, 32, 40, 48)
SIZES_BIG = (64, 96, 128, 256, 512, 1024)
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)


def rounded(im: Image.Image, radius_ratio: float = 0.225) -> Image.Image:
    """圆角遮罩（Windows/macOS 应用图标那种方中带圆）。"""
    w, h = im.size
    ss = 4
    r = int(min(w, h) * radius_ratio)
    mask = Image.new("L", (w * ss, h * ss), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w * ss - 1, h * ss - 1], radius=r * ss, fill=255)
    mask = mask.resize((w, h), Image.LANCZOS)
    out = im.convert("RGBA")
    out.putalpha(mask)
    return out


def sharpen(im: Image.Image, size: int) -> Image.Image:
    """小尺寸锐化：人像缩到 32px 后五官会糊掉，锐化 + 轻提对比才立得住。"""
    if size > 64:
        return im
    out = im.filter(ImageFilter.UnsharpMask(radius=0.8, percent=140, threshold=2))
    out = ImageEnhance.Contrast(out).enhance(1.06)
    out = ImageEnhance.Color(out).enhance(1.05)
    return out


def main() -> None:
    src = Image.open(SRC).convert("RGB")
    w, h = src.size

    full = ImageEnhance.Color(src).enhance(1.08)
    full = ImageEnhance.Brightness(full).enhance(1.03)

    # 小尺寸用「脸部特写」：整张图缩到 24px 时五官太小，看起来就是一团粉。
    # ⚠️ 所有尺寸都用特写（Chromium 做窗口图标时往往直接拿 ICO 里最大的那张缩放，
    # 只有小档位做特写的话任务栏看到的还是整张图缩下来的糊版）。
    side = int(w * 0.58)
    cx, cy = int(w * 0.50), int(h * 0.42)
    box = (cx - side // 2, cy - side // 2, cx + side // 2, cy + side // 2)
    tight = full.crop(box)
    tight = ImageEnhance.Sharpness(tight).enhance(1.25)

    # ⚠️ 关键：小尺寸**不要**先放大到 1024 再缩回去（两次重采样 = 糊）。
    # 直接从原图裁出来的 ~545px 那份单步 LANCZOS 缩到目标尺寸，任务栏才立得住。
    master_full = rounded(tight.resize((1024, 1024), Image.LANCZOS))

    def render(size: int) -> Image.Image:
        base = tight if size <= 96 else master_full
        im = base.resize((size, size), Image.LANCZOS)
        im = rounded(im) if size <= 96 else im
        return sharpen(im, size)

    made = []
    for size in SIZES_BIG + SIZES_SMALL:
        im = render(size)
        im.save(os.path.join(HERE, f"icon-{size}.png"))
        made.append(size)

    # ICO：一次塞满 Windows 会用到的高 DPI 档（100%/125%/150%/200%）
    ico_imgs = [render(size) for size in ICO_SIZES]
    ico_imgs[-1].save(
        os.path.join(HERE, "favicon.ico"),
        format="ICO",
        sizes=[(s, s) for s in ICO_SIZES],
        append_images=ico_imgs[:-1],
    )

    # 面板头像（圆形由 CSS 裁，这里给方图）
    src.resize((512, 512), Image.LANCZOS).save(os.path.join(HERE, "avatar.png"))

    print("生成完成：", sorted(made), "+ favicon.ico", ICO_SIZES, "+ avatar.png")


if __name__ == "__main__":
    main()
