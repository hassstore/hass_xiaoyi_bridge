"""渲染 hass_xiaoyi_bridge 的品牌图标：语音气泡 + 白色桥拱（双节点）。

  * icon.png      256x256 透明背景
  * icon@2x.png   512x512 透明背景

纯标准库实现（zlib + struct，不依赖 Pillow）。图形全部用有符号距离场
（SDF）解析描述：语音气泡 = 圆角盒 ∪ 带圆头的尾巴（smin 圆滑并集）；
桥拱 = 圆环 ∩ 扇形，两端各并一个节点圆点。颜色是从左上青绿到右下深蓝的
线性渐变，左上加一层很淡的白色柔光。边缘抗锯齿用 SS×SS 超采样，纯数学、
无随机源，因此重复运行结果完全一致（确定性 / 幂等）。

用法：
    python3 tools/make_icon.py [输出目录]
"""

import math
import struct
import zlib

ICON_SIZE = 256
ICON_2X_SIZE = 512
SS = 4  # 每边超采样数（边缘抗锯齿）

# ---- 设计包围盒（形状坐标系，y 轴向上）----
BB_X0, BB_X1 = -0.80, 0.80
BB_Y0, BB_Y1 = -0.86, 0.70
FILL = 0.78  # 图形占画布比例 → 四周各留约 11% 透明边距

# ---- 语音气泡 ----
BUB_CX, BUB_CY = 0.0, 0.10  # 气泡中心
BUB_HX, BUB_HY = 0.80, 0.60  # 半宽 / 半高
BUB_R = 0.30  # 圆角半径
TAIL_K = 0.10  # 尾巴与气泡主体的圆滑并集半径（在根部倒圆角）
TAIL_LEFT, TAIL_RIGHT = (-0.48, -0.50), (-0.22, -0.50)  # 尾巴根部（贴在气泡下边缘）
TAIL_APEX = (-0.35, -0.74)  # 尾尖圆心
TIP_R = 0.12  # 尾尖圆头半径（最低点即 BB_Y0）

# ---- 桥拱 glyph ----
ARCH_CX, ARCH_CY = 0.0, -0.08  # 拱的圆心
ARCH_R_IN, ARCH_R_OUT = 0.305, 0.415  # 拱的内 / 外半径
ARCH_A0, ARCH_A1 = math.radians(18.0), math.radians(162.0)  # 拱的张角
NODE_R = 0.095  # 两端节点半径
GLYPH_K = 0.035  # 拱与节点的圆滑并集半径
WEDGE_FAR = 6.0  # 扇形远端点的距离（用来构造精确的三角形 SDF）

# ---- 配色 ----
TOP_COLOUR = (0x38, 0xCE, 0xDC)  # 左上：青绿
BOT_COLOUR = (0x15, 0x65, 0xC0)  # 右下：深蓝
WHITE = (0xFF, 0xFF, 0xFF)
GRAD_X, GRAD_Y = -0.30, 0.94  # 渐变方向（指向左上；y 轴向上）

# ---- 左上一层很淡的白色柔光 ----
SHEEN_CX, SHEEN_CY = -0.42, 0.44
SHEEN_A, SHEEN_B = 0.24, 0.11  # 半短轴 / 半长轴
SHEEN_ROT = math.radians(-20.0)
SHEEN_SOFT = 0.16  # 边缘羽化宽度
SHEEN_STRENGTH = 0.11  # 最大白度

# ---- 派生常量 ----
SPAN_X = BB_X1 - BB_X0
SPAN_Y = BB_Y1 - BB_Y0
SCALE = max(SPAN_X, SPAN_Y) / FILL  # 画布在形状坐标里的跨度
DESIGN_CY = 0.5 * (BB_Y0 + BB_Y1)  # 图形的竖直中心
HALF_U = 0.5 * SPAN_X / SCALE  # 图形在 u 方向的半宽（0..1 画布坐标）
HALF_V = 0.5 * SPAN_Y / SCALE  # 图形在 v 方向的半高

ARCH_MID = 0.5 * (ARCH_R_IN + ARCH_R_OUT)
ARCH_HALF_T = 0.5 * (ARCH_R_OUT - ARCH_R_IN)
NODES = (
    (ARCH_CX + ARCH_MID * math.cos(ARCH_A0), ARCH_CY + ARCH_MID * math.sin(ARCH_A0)),
    (ARCH_CX + ARCH_MID * math.cos(ARCH_A1), ARCH_CY + ARCH_MID * math.sin(ARCH_A1)),
)
WEDGE = (  # 以拱心为原点的扇形（无界楔形的有界近似）
    (0.0, 0.0),
    (WEDGE_FAR * math.cos(ARCH_A0), WEDGE_FAR * math.sin(ARCH_A0)),
    (WEDGE_FAR * math.cos(ARCH_A1), WEDGE_FAR * math.sin(ARCH_A1)),
)


def _ccw(p0, p1, p2):
    """保证三角形顶点逆时针排列（sd_triangle 的符号约定要求）。"""
    if (p1[0] - p0[0]) * (p2[1] - p0[1]) - (p1[1] - p0[1]) * (p2[0] - p0[0]) < 0.0:
        return (p0, p2, p1)
    return (p0, p1, p2)


TAIL_TRI = _ccw(TAIL_APEX, TAIL_LEFT, TAIL_RIGHT)
WEDGE_TRI = _ccw(*WEDGE)


def _grad(x, y):
    return GRAD_X * x + GRAD_Y * y


GRAD_LO = min(_grad(x, y) for x in (BB_X0, BB_X1) for y in (BB_Y0, BB_Y1))  # 青绿端
GRAD_HI = max(_grad(x, y) for x in (BB_X0, BB_X1) for y in (BB_Y0, BB_Y1))  # 深蓝端


# --------------------------------------------------------------------------
# 有符号距离场（负值 = 内部）
# --------------------------------------------------------------------------
def sd_round_box(x, y, cx, cy, hx, hy, r):
    qx = abs(x - cx) - (hx - r)
    qy = abs(y - cy) - (hy - r)
    return min(max(qx, qy), 0.0) + math.hypot(max(qx, 0.0), max(qy, 0.0)) - r


def sd_circle(x, y, cx, cy, r):
    return math.hypot(x - cx, y - cy) - r


def sd_triangle(x, y, p0, p1, p2):
    """Inigo Quilez 的精确三角形 SDF（顶点需逆时针）。"""
    e0 = (p1[0] - p0[0], p1[1] - p0[1])
    e1 = (p2[0] - p1[0], p2[1] - p1[1])
    e2 = (p0[0] - p2[0], p0[1] - p2[1])
    v0 = (x - p0[0], y - p0[1])
    v1 = (x - p1[0], y - p1[1])
    v2 = (x - p2[0], y - p2[1])

    def proj(v, e):
        t = (v[0] * e[0] + v[1] * e[1]) / (e[0] * e[0] + e[1] * e[1])
        t = 0.0 if t < 0.0 else (min(t, 1.0))
        return (v[0] - e[0] * t, v[1] - e[1] * t)

    pq0, pq1, pq2 = proj(v0, e0), proj(v1, e1), proj(v2, e2)
    s = 1.0 if e0[0] * e2[1] - e0[1] * e2[0] >= 0.0 else -1.0
    dx = min(pq0[0] * pq0[0] + pq0[1] * pq0[1],
             pq1[0] * pq1[0] + pq1[1] * pq1[1],
             pq2[0] * pq2[0] + pq2[1] * pq2[1])
    dy = min(s * (v0[0] * e0[1] - v0[1] * e0[0]),
             s * (v1[0] * e1[1] - v1[1] * e1[0]),
             s * (v2[0] * e2[1] - v2[1] * e2[0]))
    return -math.sqrt(dx) * (1.0 if dy >= 0.0 else -1.0)


def smin(a, b, k):
    """多项式圆滑并集：只在两个距离接近处倒圆角，远处退化为 min。"""
    if k <= 0.0:
        return min(b, a)
    h = 0.5 + 0.5 * (b - a) / k
    h = 0.0 if h < 0.0 else (min(h, 1.0))
    return b + (a - b) * h - k * h * (1.0 - h)


def bubble_sdf(x, y):
    """语音气泡：圆角盒 ∪（三角形尾巴 ∪ 尾尖圆头），根部用 smin 倒圆角。"""
    d_box = sd_round_box(x, y, BUB_CX, BUB_CY, BUB_HX, BUB_HY, BUB_R)
    d_tri = sd_triangle(x, y, *TAIL_TRI)
    d_tip = sd_circle(x, y, TAIL_APEX[0], TAIL_APEX[1], TIP_R)
    d_tail = min(d_tip, d_tri)
    return smin(d_box, d_tail, TAIL_K)


def glyph_sdf(x, y):
    """桥拱：圆环 ∩ 扇形，两端各并一个节点圆点。"""
    d_ring = abs(math.hypot(x - ARCH_CX, y - ARCH_CY) - ARCH_MID) - ARCH_HALF_T
    d_wedge = sd_triangle(x - ARCH_CX, y - ARCH_CY, *WEDGE_TRI)
    d = max(d_wedge, d_ring)  # 交集
    for nx, ny in NODES:
        d = smin(d, sd_circle(x, y, nx, ny, NODE_R), GLYPH_K)
    return d


def sheen(x, y):
    """柔光强度 0..SHEEN_STRENGTH：旋转椭圆内的羽化区域。"""
    dx, dy = x - SHEEN_CX, y - SHEEN_CY
    c, s = math.cos(SHEEN_ROT), math.sin(SHEEN_ROT)
    lx = dx * c + dy * s
    ly = -dx * s + dy * c
    d = (math.hypot(lx / SHEEN_A, ly / SHEEN_B) - 1.0) * min(SHEEN_A, SHEEN_B)
    return SHEEN_STRENGTH * max(0.0, min(1.0, -d / SHEEN_SOFT))


# --------------------------------------------------------------------------
# 采样与渲染
# --------------------------------------------------------------------------
def sample(u, v):
    """画布坐标（u、v 均在 0..1，v 向下）→ RGB，或 None（透明）。"""
    x = (u - 0.5) * SCALE
    y = (0.5 - v) * SCALE + DESIGN_CY
    if bubble_sdf(x, y) > 0.0:
        return None
    if glyph_sdf(x, y) <= 0.0:
        return WHITE
    # 线性渐变：左上青绿 → 右下深蓝
    t = (_grad(x, y) - GRAD_LO) / (GRAD_HI - GRAD_LO)
    t = 0.0 if t < 0.0 else (min(t, 1.0))
    r = BOT_COLOUR[0] + (TOP_COLOUR[0] - BOT_COLOUR[0]) * t
    g = BOT_COLOUR[1] + (TOP_COLOUR[1] - BOT_COLOUR[1]) * t
    b = BOT_COLOUR[2] + (TOP_COLOUR[2] - BOT_COLOUR[2]) * t
    h = sheen(x, y)
    if h > 0.0:
        r += (WHITE[0] - r) * h
        g += (WHITE[1] - g) * h
        b += (WHITE[2] - b) * h
    return (r, g, b)


TRANSPARENT_PIXEL = b"\x00\x00\x00\x00"


def render(size: int):
    """返回 size 行 RGBA 字节串（每行 4*size 字节）。"""
    step = 1.0 / size
    rows = []
    for py in range(size):
        v_mid = (py + 0.5) * step
        if abs(v_mid - 0.5) > HALF_V + step:
            rows.append(TRANSPARENT_PIXEL * size)
            continue
        row = bytearray()
        for px in range(size):
            u_mid = (px + 0.5) * step
            if abs(u_mid - 0.5) > HALF_U + step:
                row += TRANSPARENT_PIXEL
                continue
            rr = gg = bb = hit = 0
            for sy in range(SS):
                v = (py + (sy + 0.5) / SS) * step
                for sx in range(SS):
                    u = (px + (sx + 0.5) / SS) * step
                    colour = sample(u, v)
                    if colour is None:
                        continue
                    hit += 1
                    rr += colour[0]
                    gg += colour[1]
                    bb += colour[2]
            if not hit:
                row += TRANSPARENT_PIXEL
                continue
            row += bytes((round(rr / hit), round(gg / hit), round(bb / hit),
                          round(255 * hit / (SS * SS))))
        rows.append(bytes(row))
    return rows


def png(path, rows, size: int):
    """写出 8 位 / 通道的 RGBA PNG（zlib + struct，无第三方依赖）。"""
    raw = b"".join(b"\x00" + row for row in rows)

    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)))
        fh.write(chunk(b"IDAT", zlib.compress(raw, 9)))
        fh.write(chunk(b"IEND", b""))


def verify(path):
    """回读 IHDR，确认像素尺寸确实是 size x size。"""
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit(f"{path}: 不是合法的 PNG 文件")
    if data[12:16] != b"IHDR":
        raise SystemExit(f"{path}: 缺少 IHDR 块")
    width, height = struct.unpack(">II", data[16:24])
    if (width, height) != (ICON_SIZE, ICON_SIZE) and (width, height) != (ICON_2X_SIZE, ICON_2X_SIZE):
        raise SystemExit(f"{path}: IHDR 尺寸 {width}x{height} 不符合预期")
    if data[24] != 8 or data[25] != 6:
        raise SystemExit(f"{path}: 不是 8 位 RGBA 色彩类型")
    return width, height


if __name__ == "__main__":
    import sys
    from pathlib import Path

    out_dir = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path(__file__).resolve().parent.parent
        / "custom_components"
        / "hass_xiaoyi_bridge"
        / "brand"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, size in (("icon.png", ICON_SIZE), ("icon@2x.png", ICON_2X_SIZE)):
        path = out_dir / name
        png(path, render(size), size)
        width, height = verify(path)
        print(f"{path}  {width}x{height}  {path.stat().st_size} bytes")
