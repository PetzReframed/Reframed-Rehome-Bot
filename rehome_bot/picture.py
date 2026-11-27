"""A pet file's saved picture as a PNG.

The picture is a Windows DIB: a 40-byte BITMAPINFOHEADER, then 80 rows of 108 pixels, three bytes each in the order
blue, green, red, the bottom row first. A row is 324 bytes, a multiple of four, so rows have no padding. The game
server stores only pets whose picture has exactly this form (server/petz/pet_file.py in Reframed).

Discord shows a PNG and not a DIB. The PNG is written here with zlib alone: one IHDR, one IDAT and one IEND chunk,
8-bit RGBA, every row with filter 0. Each pixel becomes a square of SCALE pixels, since 108x80 is very small in a chat.
"""
import struct
import zlib

WIDTH, HEIGHT = 108, 80
ROW = WIDTH * 3
DIB_HEADER = 40
DIB_SIZE = DIB_HEADER + ROW * HEIGHT
SCALE = 3


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))


def _portrait_alpha(dib: bytes) -> bytearray:
    """Use exact native palette entry four as the transparency key for an unmasked headshot."""
    navy_bgr = bytes((128, 0, 0))
    return bytearray(0 if dib.startswith(navy_bgr, offset) else 255
                     for offset in range(DIB_HEADER, DIB_SIZE, 3))


def dib_to_png(dib: bytes, coverage: bytes | None = None) -> bytes:
    """The PNG of a 108x80 24-bit uncompressed DIB; ValueError for anything else."""
    if len(dib) < DIB_SIZE:
        raise ValueError('The picture is too short')
    size, width, height, planes, bits, compression = struct.unpack_from('<IiiHHI', dib, 0)
    if (size, width, height, planes, bits, compression) != (DIB_HEADER, WIDTH, HEIGHT, 1, 24, 0):
        raise ValueError('The picture is not a 108x80 24-bit picture')
    if coverage is None:
        alpha = _portrait_alpha(dib)
    else:
        if len(coverage) != WIDTH * HEIGHT // 8:
            raise ValueError('The portrait coverage is the wrong size')
        alpha = bytearray(WIDTH * HEIGHT)
        for i in range(WIDTH * HEIGHT):
            if coverage[i // 8] & (1 << (i % 8)):
                y, x = divmod(i, WIDTH)
                alpha[(HEIGHT - 1 - y) * WIDTH + x] = 255
    rows = bytearray()
    for y in range(HEIGHT - 1, -1, -1):  # The DIB's last row is the top of the picture.
        start = DIB_HEADER + y * ROW
        # Repeat every RGBA pixel and scanline for nearest-neighbour scaling.
        wide = b''.join(bytes((dib[x + 2], dib[x + 1], dib[x], alpha[(x - DIB_HEADER) // 3])) * SCALE for x in range(start, start + ROW, 3))
        rows += (b'\0' + wide) * SCALE
    return (b'\x89PNG\r\n\x1a\n'
            + _chunk(b'IHDR', struct.pack('>IIBBBBB', WIDTH * SCALE, HEIGHT * SCALE, 8, 6, 0, 0, 0))
            + _chunk(b'IDAT', zlib.compress(bytes(rows), 9))
            + _chunk(b'IEND', b''))
