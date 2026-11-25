"""Generate Windows application icons and NSIS artwork from tracked source assets."""
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'packaging' / 'assets'
OUTPUT = ROOT / 'packaging' / 'generated'


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    icon = Image.open(ASSETS / 'cat.ico').convert('RGBA')
    icon.save(OUTPUT / 'icon.ico', format='ICO')
    icon.resize((512, 512), Image.Resampling.LANCZOS).save(OUTPUT / 'icon.png')
    logo = Image.open(ASSETS / 'logo.png').convert('RGBA')
    for name, size, bounds in (
        ('sidebar.bmp', (164, 314), (120, int(314 * 0.55))),
        ('header.bmp', (150, 57), (130, 48)),
    ):
        canvas = Image.new('RGBA', size, (248, 249, 250, 255))
        artwork = logo.copy()
        artwork.thumbnail(bounds, Image.Resampling.LANCZOS)
        canvas.paste(artwork, ((size[0] - artwork.width) // 2,
                             (size[1] - artwork.height) // 2), artwork)
        canvas.convert('RGB').save(OUTPUT / name, 'BMP')
    print('Installer artwork ready:', OUTPUT)


if __name__ == '__main__':
    main()
