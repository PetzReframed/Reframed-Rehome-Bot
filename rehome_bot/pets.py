"""The game server's rehoming folder, read only.

The server (Petz5Reframed, server/petz/rehoming.py) keeps every pet a player offered to other players as one file:

- available/<id>.<hash>.<hash>.pet while the pet waits at the Adoption Center;
- claimed/<id>.<hash>.<hash>.<expiry>.<cap>.pet while it is out on a visit with a player who may adopt it.

The ID is 24 hex digits, made by the server for each offer; the same pet offered again gets a new one. A file appears
complete, by one rename from a .pending-<id> name, and moves between the two folders by one rename; a renewed visit
renames the file inside claimed/. When the server accepts an adoption it deletes the file from claimed/. An offer taken
back right after it was made is deleted from available/. Nothing else is recorded: no list, no history, no adopter.
The file's modification time is when the pet was offered (renames keep it; the game shows it as "Waiting since").

So a scan can miss a pet that is being renamed just then; the tracker looks twice before it takes a pet as gone.
Nothing here writes to the folder: the server stops rehoming when it finds an entry it does not know.

A stored file is a Petz 5 pet file the server read in full and cleaned of its previous owner's name and comments
(server/petz/pet_file.py). It is still a player's bytes: every read here is bounded and nothing is trusted to be
where it should be. What is read:

- the header, 0x330 bytes: the pet's name at 2, its breed at 0x102 and the breed file's path at 0x202 (each text
  ends with a zero byte, in Windows-1252), the species number at 0x310 (0 cat, 1 dog), the format version at 0x318
  and at 0x31c the 16-bit sum of the header's other bytes;
- the sex, one byte after the appearance block: found by stepping over the saved state, the nine saved traits, the
  three saved colour tables with their fixed counts, the two LnZ texts and the appearance records;
- the picture, the last block of the file: a marker, a 32-bit size, and a DIB of that size up to the file's end.

A file that does not read this way is not a pet as far as the bot is concerned.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import struct

from .picture import DIB_SIZE, dib_to_png

AVAILABLE = re.compile(r'([0-9a-f]{24})\.[0-9a-f]{32}\.[0-9a-f]{32}\.pet')
CLAIMED = re.compile(r'([0-9a-f]{24})\.[0-9a-f]{32}\.[0-9a-f]{32}\.[0-9]{1,12}(?:\.[0-9]{1,12})?\.pet')

HEADER = 0x330
VERSION = 0x0ACB0003
STATE = b'p.f.magicpetzIII\0'
PICTURE = b'PFMAGICPETZIII\0'
LNZ_END = b'\xaa\xcc\xbb\x03'
# The server's own bounds for one LnZ text, the appearance records and the picture block.
MAX_LNZ = 65536
MAX_RECORDS = 1024
MAX_RECORD_TEXT = 1024
MAX_PICTURE = 65536
# The folder of a species' breed files, as the breed path names it. The species number tells only which of the two
# engines runs the pet (a pig is run as a dog, a bunny as a cat), so the folder is asked first.
ANIMALS = {'Catz': 'cat', 'Dogz': 'dog', 'Pigz': 'pig', 'Bunnyz': 'bunny'}


class PetFileError(ValueError):
    """The bytes do not read as a stored pet."""


@dataclass(frozen=True)
class Pet:
    name: str
    breed: str
    animal: str
    female: bool
    offered: int   # Unix time
    picture: bytes  # PNG


def scan(directory: Path, max_pets: int) -> dict[str, tuple[str, Path]]:
    """Each stored pet's ID with the folder it was seen in ('available' or 'claimed') and its file.

    OSError when a folder cannot be read or holds more entries than max_pets: a pass that saw only part of the
    folder would take the pets it did not see as gone.
    """
    found = {}
    for where, pattern in (('available', AVAILABLE), ('claimed', CLAIMED)):
        count = 0
        with os.scandir(directory / where) as entries:
            for entry in entries:
                count += 1
                if count > max_pets:
                    raise OSError(f'{where}/ holds more than MAX_PETS ({max_pets}) entries')
                match = pattern.fullmatch(entry.name)
                if match and entry.is_file(follow_symlinks=False):
                    found[match[1]] = (where, Path(entry.path))
    return found


def _int(data: bytes, at: int) -> int:
    if at < 0 or at + 4 > len(data):
        raise PetFileError('The file ends early')
    return struct.unpack_from('<i', data, at)[0]


def _marker(data: bytes, at: int, expected: bytes) -> int:
    """The position after the marker that must be at this one."""
    if data[at:at + len(expected)] != expected:
        raise PetFileError('A marker is missing')
    return at + len(expected)


def _text(field: bytes) -> str:
    end = field.find(b'\0')
    if end <= 0 or any(c < 0x20 or c == 0x7f for c in field[:end]):
        raise PetFileError('A text of the header is empty, unterminated or has control characters')
    return field[:end].decode('cp1252', 'replace')


def _female(data: bytes) -> bool:
    at = _marker(data, HEADER, STATE)
    at += 1 + 4 * 8 + 1 + 4 * 8 + 0x960  # saved state
    at += 9 * 4 + 4 + 6                  # the nine saved traits and the fields after them
    for size, stride in ((512, 1), (15, 4), (512, 4)):  # saved colours, each after its count
        if _int(data, at) != size:
            raise PetFileError('A saved-colour count is not the one the game writes')
        at += 4 + size * stride
    at = _marker(data, at + 4, STATE)
    for _ in range(2):  # the adult and the juvenile LnZ text
        end = data.find(b'\0', at, at + MAX_LNZ + 1)
        if end < 0:
            raise PetFileError('An LnZ text is unterminated or too long')
        at = _marker(data, end + 1, LNZ_END)
    at += 4 + 0xdc8
    records = _int(data, at)
    if not 0 <= records <= MAX_RECORDS:
        raise PetFileError('The appearance record count is out of range')
    at += 4
    for _ in range(records):  # a 16-bit ID, a 32-bit length and that many bytes
        length = _int(data, at + 2)
        if not 0 <= length <= MAX_RECORD_TEXT:
            raise PetFileError('An appearance record is too long')
        at += 6 + length
    at = _marker(data, at, STATE)
    if at >= len(data) or data[at] > 1:
        raise PetFileError('The sex byte is missing or not one the server stores')
    return data[at] == 1


def _picture(data: bytes) -> bytes:
    # The block ends the file, so its size field must equal what follows it. The marker is searched for only where
    # the largest block could start.
    at = data.find(PICTURE, max(HEADER, len(data) - MAX_PICTURE - len(PICTURE) - 4))
    while at >= 0:
        dib = at + len(PICTURE) + 4
        if dib <= len(data) and DIB_SIZE <= _int(data, dib - 4) == len(data) - dib:
            try:
                return dib_to_png(data[dib:])
            except ValueError as error:
                raise PetFileError(str(error)) from None
        at = data.find(PICTURE, at + 1)
    raise PetFileError('The picture block is missing')


def parse_pet(data: bytes, offered: int) -> Pet:
    """What the bot shows of a stored pet file; PetFileError when the bytes do not read as one."""
    if len(data) < HEADER:
        raise PetFileError('The header is incomplete')
    version, stored = struct.unpack_from('<IH', data, 0x318)
    if version != VERSION:
        raise PetFileError('Not a Petz 5 pet file')
    if (sum(data[:HEADER]) - data[0x31c] - data[0x31d]) & 0xffff != stored:
        raise PetFileError('The header checksum does not match')
    name, breed, path = _text(data[2:0x102]), _text(data[0x102:0x202]), _text(data[0x202:0x306])
    species = _int(data, 0x310)
    if species not in (0, 1):
        raise PetFileError('The species is not one the game has')
    folder = path.split('\\')[2] if path.startswith('\\Resource\\') else ''
    return Pet(name, breed, ANIMALS.get(folder, 'dog' if species else 'cat'), _female(data), offered, _picture(data))


def read_pet(path: Path, max_bytes: int) -> Pet | None:
    """The stored pet, or None when its file has moved, is not a regular file, is too large or does not read as a
    pet. A file that moved is found again by the next scan."""
    try:
        with open(path, 'rb') as stream:
            status = os.fstat(stream.fileno())
            if not stat.S_ISREG(status.st_mode) or status.st_size > max_bytes:
                return None
            data = stream.read(max_bytes + 1)
        return parse_pet(data, int(status.st_mtime)) if len(data) <= max_bytes else None
    except (OSError, PetFileError):
        return None
