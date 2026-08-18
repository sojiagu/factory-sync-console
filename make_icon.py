from PIL import Image, ImageDraw

im = Image.new("RGBA", (64, 64), (11, 110, 79, 255))
d = ImageDraw.Draw(im)
d.rectangle((16, 16, 48, 48), fill=(255, 255, 255, 255))
im.save("icon.ico")
print("wrote icon.ico")
