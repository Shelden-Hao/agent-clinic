"""把 16:9 的界面截图重排成 3:4 竖版封面（1080x1440），给小红书信息流用。

为什么需要这个：小红书信息流按 3:4 竖版排版，1.44 的横图会被压成很窄的一条，
标题在缩略图里完全读不出来。重排不是"换个尺寸"，而是把「一句能看懂的结论」
放到画面顶部，截图降级为证据。

用法：./.venv/Scripts/python.exe tools/make_xhs_covers.py
输出：submission/xhs/01.png ~ 03.png
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1080, 1440
PAD = 60

BG_TOP = (11, 13, 16)
BG_BOTTOM = (20, 23, 28)
ACCENT = (255, 92, 77)          # 与产品内的告警色一致
WHITE = (243, 245, 248)
GREY = (154, 163, 175)
FRAME = (38, 42, 48)

FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"
FONT_REG = "C:/Windows/Fonts/msyh.ttc"

COVERS = [
    {
        "src": "01-会诊报告.png",
        "tag": "Agent 会诊室 · 1024 造物挑战",
        "lines": [("失败率降了 12.5 个点", WHITE), ("真正修好的只有 1 个", ACCENT)],
        "sub": "另外 1 个被弄坏，4 个从「每次都失败」\n变成「一半概率失败」——失败率降了，不确定性涨了",
        "stats": [("48", "条轨迹"), ("27", "条失败"), ("4", "类病")],
    },
    {
        "src": "02-多轮会话.png",
        "tag": "Agent 会诊室 · 多轮会话诊断",
        "lines": [("32 轮真实会话", WHITE), ("12 轮造不出样本", ACCENT)],
        "sub": "用户说「就按你说的办」，输入里根本没有订单号。\n孤立的一条输入不是会做错，是无从下手",
        "stats": [("16", "场会话"), ("32", "个轮次"), ("12", "轮无样本")],
    },
    {
        "src": "03-Prompt版本墙.png",
        "tag": "Agent 会诊室 · 回归验收",
        "lines": [("改完 prompt", WHITE), ("是修好了，还是换了死法", ACCENT)],
        "sub": "11 类失败归因 → 最小复现 → 回归五态判定\n每条新增规则都挂着它的出处",
        "stats": [("11→4", "步最小复现"), ("5", "态回归判定"), ("100%", "规则带出处")],
    },
]


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def bg() -> Image.Image:
    """竖向渐变 + 左上角一团暖色光，避免纯黑在信息流里发死。"""
    img = Image.new("RGB", (W, H), BG_TOP)
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / (H - 1)
        d.line(
            [(0, y), (W, y)],
            fill=tuple(round(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)),
        )

    glow = Image.new("RGB", (W, H), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([-380, -420, 620, 380], fill=(70, 26, 20))
    glow = glow.filter(ImageFilter.GaussianBlur(150))
    return Image.blend(img, Image.blend(img, glow, 0.55), 0.9)


def rounded(img: Image.Image, radius: int) -> Image.Image:
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, img.size[0] - 1, img.size[1] - 1], radius, fill=255)
    out = img.convert("RGBA")
    out.putalpha(mask)
    return out


def build(spec: dict, out_path: Path) -> None:
    canvas = bg().convert("RGBA")
    d = ImageDraw.Draw(canvas)

    # 标签
    f_tag = font(FONT_REG, 26)
    d.ellipse([PAD, 74, PAD + 12, 86], fill=ACCENT)
    d.text((PAD + 24, 64), spec["tag"], font=f_tag, fill=GREY)

    # 标题：两行，第二行用强调色
    y = 148
    f_title = font(FONT_BOLD, 72)
    for text, color in spec["lines"]:
        d.text((PAD, y), text, font=f_title, fill=color)
        y += 100

    # 副标题
    y += 18
    f_sub = font(FONT_REG, 30)
    for line in spec["sub"].split("\n"):
        d.text((PAD, y), line, font=f_sub, fill=GREY)
        y += 48

    # 数据条：三组数字，填掉标题与截图之间的空档，同时给缩略图一点信息密度
    y += 34
    d.line([(PAD, y), (W - PAD, y)], fill=(44, 48, 55), width=2)
    y += 26
    f_num = font(FONT_BOLD, 50)
    f_lab = font(FONT_REG, 24)
    col = (W - PAD * 2) / 3
    for i, (num, label) in enumerate(spec["stats"]):
        x = PAD + col * i
        d.text((x, y), num, font=f_num, fill=WHITE if i else ACCENT)
        d.text((x, y + 66), label, font=f_lab, fill=GREY)
    y += 118

    # 截图：宽度撑满，底部留 60 给页脚
    shot_w = W - PAD * 2
    shot = Image.open(spec["src"]).convert("RGB")
    shot_h = round(shot_w * shot.size[1] / shot.size[0])
    shot = shot.resize((shot_w, shot_h), Image.LANCZOS)

    max_top = H - 70 - shot_h
    top = min(max(y + 56, 700), max_top)

    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [PAD - 8, top - 2, PAD + shot_w + 8, top + shot_h + 18], 27, fill=(0, 0, 0, 190)
    )
    canvas = Image.alpha_composite(canvas, shadow.filter(ImageFilter.GaussianBlur(26)))
    d = ImageDraw.Draw(canvas)

    framed = Image.new("RGB", (shot_w + 4, shot_h + 4), FRAME)
    framed.paste(shot, (2, 2))
    canvas.alpha_composite(rounded(framed, 20), (PAD - 2, top - 2))
    d = ImageDraw.Draw(canvas)

    # 页脚
    f_foot = font(FONT_REG, 24)
    d.text((PAD, H - 52), "秀秀不只会前端", font=f_foot, fill=(120, 128, 138))
    right = "agent-clinic.app.workbuddy.host"
    d.text((W - PAD - d.textlength(right, font=f_foot), H - 52), right, font=f_foot, fill=(120, 128, 138))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(out_path, quality=95)
    print(f"{out_path.name}  {canvas.size}")


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    sub = root / "submission"
    for i, spec in enumerate(COVERS, 1):
        spec = dict(spec, src=str(sub / spec["src"]))
        build(spec, sub / "xhs" / f"{i:02d}.png")


if __name__ == "__main__":
    main()
