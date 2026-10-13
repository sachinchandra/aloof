"""Generate OpenGraph PNGs (1200x630) into paas/static/og. Run locally; outputs are committed (Pillow isn't a server dependency)."""
import sys, os; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from paas.www_blog import posts
F = "/usr/share/fonts/truetype/sand-box/google/Inter/Inter-VariableFont_opsz,wght.ttf"; M = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
def font(sz, w=700):
    f = ImageFont.truetype(F, sz)
    try: f.set_variation_by_axes([32, w])
    except Exception: pass
    return f
def card(name, eyebrow, title, sub):
    W, H = 1200, 630; im = Image.new("RGB", (W, H), (10, 8, 7))
    glow = Image.new("RGB", (W, H), (0, 0, 0)); g = ImageDraw.Draw(glow)
    pass  # no decorative blobs
    d = ImageDraw.Draw(im)
    for x in range(0, W, 48): d.line((x, 0, x, H), fill=(26, 21, 17))
    for y in range(0, H, 48): d.line((0, y, W, y), fill=(26, 21, 17))
    for x, y in ((4, 5), (7, 13.2), (10, 21.4)): d.rounded_rectangle((72 + x, 64 + y, 72 + x + 18, 64 + y + 5.6), 2.8, fill=(238, 240, 246))
    d.text((118, 60), "aloof", font=font(32, 680), fill=(238, 240, 246))
    d.text((72, 170), eyebrow.upper(), font=ImageFont.truetype(M, 22), fill=(255, 122, 26))
    words, lines, cur = title.split(), [], ""
    tf = font(64 if len(title) < 48 else 54, 780)
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(t, font=tf) > 1040: lines.append(cur); cur = w
        else: cur = t
    lines.append(cur); y = 214
    for l in lines[:3]: d.text((72, y), l, font=tf, fill=(238, 240, 246)); y += tf.size + 14
    d.text((72, 540), sub, font=font(26, 450), fill=(169, 176, 195)); im.save(f"paas/static/og/{name}.png", optimize=True)
card("default", "Backend hosting", "Cheap backends, billed per second", "Deploy images, git repos or a directory · scale to zero · MCP for AI agents")
for n, e, t in [("blog", "Blog", "Engineering notes"), ("guides", "Guides", "Framework quickstarts"), ("changelog", "Changelog", "What's shipped"),
                ("customers", "Example architectures", "What you can build on Aloof"), ("regions", "Regions", "Nuremberg live. More coming soon."),
                ("about", "About", "Hosting for backends, without the bill shock"), ("security", "Security", "Security, stated plainly"), ("status", "Status", "System status")]:
    card(n, e, t, "www.aloof.si/" + n)
for p in posts(): card("blog-" + p["slug"], "Blog · " + p["date"], p["title"], "www.aloof.si/blog")
