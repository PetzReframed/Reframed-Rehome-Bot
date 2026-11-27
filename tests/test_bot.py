"""The bot against a rehoming folder made here, as the game server keeps one, and a stand-in for Discord.

The pet files are made here as well, from these facts of the stored format (Reframed, server/petz/pet_file.py):
a 0x330-byte header with the name at 2, the breed at 0x102, the breed path at 0x202, the species number at 0x310, the
version 0x0acb0003 at 0x318, the 16-bit sum of the header's other bytes at 0x31c and of the payload at 0x31e; a
payload that starts with the marker "p.f.magicpetzIII", 0x9a2 bytes of saved state, nine 32-bit traits, ten bytes,
three counted colour tables (512 bytes, 15 and 512 32-bit values), four bytes, the marker again, two LnZ texts each
ended by a zero byte and AA CC BB 03, 0xdcc bytes, the counted appearance records, the marker a third time and then
the sex byte; and, as the last block of the file, the marker "PFMAGICPETZIII", a 32-bit size and a 108x80 24-bit DIB.
The genome, attitude tables and family tree follow the saved flags in the server's field order.
"""
import io
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock
import urllib.error
import zlib

from rehome_bot.config import load_config
from rehome_bot.discord import DiscordError, edit_message, multipart, post_message
from rehome_bot.pets import PetFileError, parse_pet, read_pet, scan
from rehome_bot.picture import dib_to_png
from rehome_bot.tracker import StateError, Tracker, load_state, message_text, nonce_of, plain

ENV = {'DISCORD_BOT_TOKEN': 'token.for-tests', 'DISCORD_CHANNEL_ID': '123456789012345678'}
FIRST, SECOND, THIRD = '0123456789abcdef01234567', '89abcdef0123456789abcdef', 'fedcba9876543210fedcba98'
WITHDRAW, NO_CLAIM, CLAIM = 'a' * 32, '0' * 32, 'c' * 32
OFFERED, NOW = 1795428960, 1795430000
STATE, PICTURE, LNZ_END = b'p.f.magicpetzIII\0', b'PFMAGICPETZIII\0', b'\xaa\xcc\xbb\x03'


def dib(pixel=lambda x, y: (x, y, 255 - x), *, width=108, height=80, bits=24):
    """A DIB whose pixel x, y (from the top left) has the colour pixel(x, y) as red, green, blue."""
    rows = b''.join(bytes(c for x in range(108) for c in reversed(pixel(x, y))) for y in range(79, -1, -1))
    return struct.pack('<IiiHHIIiiII', 40, width, height, 1, bits, 0, len(rows), 0, 0, 0, 0) + rows


def pet_file(name=b'Biscuit', breed=b'Calico', folder=b'Catz', extension=b'cat', species=0, female=True,
             records=((1, b'texture'),), picture=None, age=30, fixed=False, dates=(0, 0), born=False, purebred=True,
             pregnant=False):
    """A stored pet file as far as the bot reads one."""
    header = bytearray(0x330)
    path = b'\\Resource\\' + folder + b'\\' + breed + b'.' + extension
    for start, text in ((2, name), (0x102, breed), (0x202, path)):
        header[start:start + len(text)] = text
    struct.pack_into('<i', header, 0x310, species)
    struct.pack_into('<I', header, 0x318, 0x0ACB0003)
    def family(first, second, flags, parent=b''):
        text = lambda value: struct.pack('<i', len(value)) + value
        return (bytes(16) + text(name) + text(breed) + text(b'') + struct.pack('<iiiB', first, second, 0, flags)
                + bytes([bool(parent)]) + parent + b'\0')
    genome = b''.join(struct.pack('<i', n) + bytes(n * 15) for n in (41, 22, 25) * 2)
    payload = (STATE + bytes(1 + 4 * 8 + 1 + 4 * 8 + 0x960) + struct.pack('<9i', *[50] * 8, age) + bytes(4 + 6)
               + b''.join(struct.pack('<i', size) + bytes(size * stride) for size, stride in ((512, 1), (15, 4), (512, 4)))
               + bytes(4) + STATE + b'[Adult]\r\n\0' + LNZ_END + b'[Juvenile]\r\n\0' + LNZ_END + bytes(4 + 0xdc8)
               + struct.pack('<i', len(records))
               + b''.join(struct.pack('<Hi', number, len(text)) + text for number, text in records)
               + STATE + bytes((female, fixed, 0, pregnant)) + bytes(8) + genome + bytes(4))
    if pregnant:
        payload += genome + family(123, 456, 1) + bytes(5)
    payload += (STATE + struct.pack('<i', 41) + bytes(41 * 23) + struct.pack('<i', 22) + bytes(22 * 23)
                + bytes(20) + b'PfMaGiCpEtZIII\0' + bytes(4)
                + family(*dates, purebred, family(111, 222, 1) if born else b'')
                + bytes(16 + 1 + 77 + 12))
    picture = dib() if picture is None else picture
    struct.pack_into('<H', header, 0x31e, sum(payload) & 0xffff)
    struct.pack_into('<H', header, 0x31c, sum(header) & 0xffff)
    return bytes(header) + payload + PICTURE + struct.pack('<i', len(picture)) + picture


def png_pixels(png):
    """The size of a PNG written by the bot and its rows of (red, green, blue, alpha), after checking every chunk's CRC."""
    assert png[:8] == b'\x89PNG\r\n\x1a\n'
    chunks, at = [], 8
    while at < len(png):
        length, kind = struct.unpack_from('>I4s', png, at)
        data = png[at + 8:at + 8 + length]
        assert struct.unpack_from('>I', png, at + 8 + length)[0] == zlib.crc32(kind + data)
        chunks.append((kind, data))
        at += 12 + length
    assert [kind for kind, _ in chunks] == [b'IHDR', b'IDAT', b'IEND']
    width, height, depth, colour, compression, filtering, interlace = struct.unpack('>IIBBBBB', chunks[0][1])
    assert (depth, colour, compression, filtering, interlace) == (8, 6, 0, 0, 0)
    raw = zlib.decompress(chunks[1][1])
    assert len(raw) == height * (1 + width * 4)
    rows = []
    for y in range(height):
        row = raw[y * (1 + width * 4):(y + 1) * (1 + width * 4)]
        assert row[0] == 0
        rows.append([tuple(row[1 + x * 4:5 + x * 4]) for x in range(width)])
    return width, height, rows


class FakeDiscord:
    """Stands in for Discord: keeps the messages, and answers a nonce it has seen with the message it made for it."""
    def __init__(self):
        self.messages, self.nonces, self.calls, self.failures = {}, {}, [], []

    def post(self, token, channel, content, files, nonce):
        self.calls.append('post')
        if self.failures:
            raise self.failures.pop(0)
        if nonce not in self.nonces:
            self.nonces[nonce] = str(1000 + len(self.messages))
            self.messages[self.nonces[nonce]] = {'channel': channel, 'content': content, 'files': files}
        return self.nonces[nonce]

    def edit(self, token, channel, message, content):
        self.calls.append('edit')
        if self.failures:
            raise self.failures.pop(0)
        if message not in self.messages:
            raise DiscordError(404, 'Discord answered 404: Unknown Message', code=10008)
        self.messages[message]['content'] = content

    def contents(self):
        return [message['content'] for message in self.messages.values()]


class Folder(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.rehoming, self.state = Path(temp.name) / 'rehoming', Path(temp.name) / 'state'
        for name in ('available', 'claimed'):
            (self.rehoming / name).mkdir(parents=True)
        self.discord, self.now, self.slept = FakeDiscord(), NOW, []

    def config(self, **extra):
        return load_config({**ENV, 'REHOMING_DIR': str(self.rehoming), 'STATE_DIR': str(self.state), **extra})

    def tracker(self, **extra):
        """The bot as it starts: it reads its state file."""
        return Tracker(self.config(**extra), send=self.discord.post, edit=self.discord.edit,
                       clock=lambda: self.now, sleep=self.slept.append)

    def file_of(self, pet):
        return next(self.rehoming.glob(f'*/{pet}.*'))

    def offer(self, pet, data=None, offered=OFFERED):
        """As the server stores an offer: the whole file under a pending name, then one rename."""
        pending = self.rehoming / 'available' / ('.pending-' + pet)
        pending.write_bytes(pet_file() if data is None else data)
        os.utime(pending, (offered, offered))
        pending.rename(self.rehoming / 'available' / f'{pet}.{WITHDRAW}.{NO_CLAIM}.pet')

    def claim(self, pet):
        """A player meets the pet: it is out on a visit."""
        self.file_of(pet).rename(self.rehoming / 'claimed' / f'{pet}.{WITHDRAW}.{CLAIM}.{NOW + 120}.{NOW + 1860}.pet')

    def release(self, pet):
        """The visit ended without an adoption: the pet waits again."""
        self.file_of(pet).rename(self.rehoming / 'available' / f'{pet}.{WITHDRAW}.{CLAIM}.pet')

    def delete(self, pet):
        """From claimed/: the server accepted an adoption. From available/: the offer was taken back."""
        self.file_of(pet).unlink()

    def stored_state(self):
        return json.loads((self.state / 'pets.json').read_text(encoding='utf-8'))['pets']


class ConfigTests(unittest.TestCase):
    def test_the_token_and_the_channel_are_required_and_the_rest_has_defaults(self):
        config = load_config(ENV)
        self.assertEqual((config.rehoming_dir, config.state_dir, config.poll_seconds, config.post_existing,
                          config.attach_picture, config.max_pet_bytes, config.max_pets),
                         (Path('/rehoming'), Path('/state'), 5, False, True, 1048576, 10000))
        for changed, word in (({'DISCORD_BOT_TOKEN': ''}, 'DISCORD_BOT_TOKEN'), ({'DISCORD_CHANNEL_ID': 'general'}, 'DISCORD_CHANNEL_ID'),
                              ({'POLL_SECONDS': '0'}, 'POLL_SECONDS'), ({'POST_EXISTING': 'yes'}, 'POST_EXISTING'),
                              ({'ATTACH_PICTURE': '1'}, 'ATTACH_PICTURE'), ({'MAX_PET_BYTES': '100'}, 'MAX_PET_BYTES'),
                              ({'MAX_PETS': '20000'}, 'MAX_PETS')):
            with self.subTest(changed=changed), self.assertRaises(ValueError) as raised:
                load_config({**ENV, **changed})
            self.assertIn(word, str(raised.exception))
            self.assertNotIn(ENV['DISCORD_BOT_TOKEN'], str(raised.exception))


class PictureTests(unittest.TestCase):
    def test_a_dib_becomes_a_png_three_times_as_large_with_the_top_row_first_and_red_first(self):
        width, height, rows = png_pixels(dib_to_png(dib()))
        self.assertEqual((width, height), (324, 240))
        for x, y in ((0, 0), (107, 0), (0, 79), (107, 79), (53, 40)):
            for dx, dy in ((0, 0), (2, 2)):
                with self.subTest(x=x, y=y, dx=dx):
                    self.assertEqual(rows[y * 3 + dy][x * 3 + dx], (x, y, 255 - x, 255))
        # Bytes after the 80 rows are not part of the picture.
        self.assertEqual(dib_to_png(dib() + b'more'), dib_to_png(dib()))

    def test_unmasked_portrait_clears_all_exact_navy_and_preserves_near_navy(self):
        backdrop = (0, 0, 128)
        def pixel(x, y):
            if x < 10 or (x, y) in ((10, 12), (11, 13), (50, 50)):
                return backdrop
            return (0, 0, 129)
        _, _, rows = png_pixels(dib_to_png(dib(pixel=pixel)))
        for x, y, expected in ((0, 79, (*backdrop, 0)), (10, 12, (*backdrop, 0)),
                               (11, 13, (*backdrop, 0)), (50, 50, (*backdrop, 0)),
                               (107, 0, (0, 0, 129, 255))):
            for dy in range(3):
                for dx in range(3):
                    self.assertEqual(rows[y * 3 + dy][x * 3 + dx], expected)

    def test_captured_mask_can_keep_exact_navy_and_clear_near_navy(self):
        mask = bytearray(1080)
        mask[0] = 1
        source = dib(pixel=lambda x, y: (0, 0, 129) if x == 1 else (0, 0, 128))
        _, _, rows = png_pixels(dib_to_png(source, mask))
        self.assertEqual(rows[0][0], (0, 0, 128, 255))
        self.assertEqual(rows[0][3], (0, 0, 129, 0))
        self.assertEqual(rows[0][6], (0, 0, 128, 0))

    def test_anything_but_a_108_by_80_24_bit_picture_is_refused(self):
        for label, data in (('short', dib()[:-1]), ('empty', b''), ('wide', dib(width=109)), ('upside down', dib(height=-80)),
                            ('palette', dib(bits=8)), ('header size', b'\x0c' + dib()[1:])):
            with self.subTest(label), self.assertRaises(ValueError):
                dib_to_png(data)


class PetFileTests(Folder):
    def test_listing_sidecar_sets_top_down_alpha_independently_of_portrait_colour(self):
        self.offer(FIRST)
        pet_path = self.file_of(FIRST)
        masks = self.rehoming / 'portraits'
        masks.mkdir()
        bitmap = bytearray(1080)
        bitmap[13] = 16  # first pixel of the second top-down row
        (masks / (FIRST + '.mask')).write_bytes(bitmap)
        info = read_pet(pet_path, 1048576)
        _, _, rows = png_pixels(info.picture)
        self.assertEqual([rows[0][0][3], rows[3][0][3], rows[3][3][3]], [0, 255, 0])

    def test_wrong_sized_listing_masks_fall_back_to_the_portrait_colours(self):
        portrait = dib(pixel=lambda x, y: (0, 0, 128) if x == 20 and y == 30 else (0, 0, 129))
        self.offer(FIRST, pet_file(picture=portrait))
        path = self.file_of(FIRST)
        masks = self.rehoming / 'portraits'
        masks.mkdir()
        for size in (1079, 1081):
            with self.subTest(size=size):
                (masks / (FIRST + '.mask')).write_bytes(bytes(size))
                pet = read_pet(path, 1048576)
                self.assertIsNotNone(pet)
                _, _, rows = png_pixels(pet.picture)
                self.assertEqual(rows[90][60], (0, 0, 128, 0))
                self.assertEqual(rows[90][63], (0, 0, 129, 255))

    def test_the_name_the_breed_the_animal_the_sex_and_the_picture_are_read(self):
        pet = parse_pet(pet_file(), OFFERED)
        self.assertEqual((pet.name, pet.breed, pet.animal, pet.female, pet.offered), ('Biscuit', 'Calico', 'cat', True, OFFERED))
        self.assertEqual(pet.picture, dib_to_png(dib()))
        for label, data, expected in (
                ('a male dog', pet_file(b'Rex', b'Labrador', b'Dogz', b'dog', 1, False), ('Rex', 'Labrador', 'dog', False)),
                # A pig is run by the dog engine and a bunny by the cat engine: the breed's folder says what it is.
                ('a pig', pet_file(b'Truffle', b'Pig', b'Pigz', b'pig', 1), ('Truffle', 'Pig', 'pig', True)),
                ('a bunny', pet_file(b'Clover', b'Bunny', b'Bunnyz', b'bunny', 0, False), ('Clover', 'Bunny', 'bunny', False)),
                ('a folder the bot does not know', pet_file(folder=b'Birdz', species=1), ('Biscuit', 'Calico', 'dog', True)),
                ('a Windows-1252 name', pet_file(b'Zo\xeb \x80'), ('Zoë €', 'Calico', 'cat', True)),
                ('no appearance records', pet_file(records=()), ('Biscuit', 'Calico', 'cat', True))):
            with self.subTest(label):
                pet = parse_pet(data, OFFERED)
                self.assertEqual((pet.name, pet.breed, pet.animal, pet.female), expected)

    def test_profile_date_and_flags_come_from_the_pet_own_family_record(self):
        pet = parse_pet(pet_file(female=True, fixed=True, dates=(1000, 2000), born=True,
                                 purebred=False, pregnant=True), OFFERED)
        self.assertEqual((pet.fixed, pet.adopted, pet.born, pet.purebred), (True, 2000, True, False))
        pet = parse_pet(pet_file(dates=(1000, 0)), OFFERED)
        self.assertEqual((pet.fixed, pet.adopted, pet.born, pet.purebred), (False, 1000, False, True))
        self.assertIsNone(parse_pet(pet_file(), OFFERED).adopted)

    def test_shared_profile_age_labels_use_the_species_and_saved_growth_trait(self):
        for age, expected in [(0, 'Newborn'), (5, 'Newborn'), (6, 'Infant'), (11, 'Infant'),
                              (12, 'Kitten'), (29, 'Kitten'), (30, 'Adult'), (100, 'Adult')]:
            with self.subTest(age=age):
                self.assertEqual(parse_pet(pet_file(age=age), OFFERED).age_stage, expected)
        self.assertEqual(parse_pet(pet_file(age=12, folder=b'Bunnyz', breed=b'Bunny'), OFFERED).age_stage, 'Kitten')
        self.assertEqual(parse_pet(pet_file(age=29, species=1, folder=b'Pigz', breed=b'Pig'), OFFERED).age_stage, 'Puppy')

    def test_a_file_cut_short_at_any_place_is_refused_and_never_read_past_its_end(self):
        whole = pet_file()
        for cut in sorted({*range(0, len(whole), 997), 0x32f, 0x330, 0x340, len(whole) - 25961, len(whole) - 1}):
            with self.subTest(cut=cut), self.assertRaises(PetFileError):
                parse_pet(whole[:cut], OFFERED)

    def test_damaged_files_are_refused(self):
        def changed(data, at, value):
            return data[:at] + value + data[at + len(value):]
        whole = pet_file()
        sex = whole.index(STATE + bytes((1, 0, 0, 0)) + bytes(8)) + len(STATE)
        records = whole.index(struct.pack('<i', 1) + struct.pack('<Hi', 1, 7) + b'texture')
        for label, data in (
                ('not a pet file', b'GIF89a' + bytes(4096)),
                ('another version', changed(whole, 0x318, struct.pack('<I', 0x0ACB0002))),
                ('a changed header', changed(whole, 2, b'X')),
                ('an unterminated name', pet_file(b'N' * 0x100)),
                ('a control character in the name', pet_file(b'Bis\ncuit')),
                ('an unknown species', pet_file(species=2)),
                ('a sex byte the server does not store', changed(whole, sex, b'\x02')),
                ('a record count without end', changed(whole, records, struct.pack('<i', 0x7fffffff))),
                ('a negative record length', changed(whole, records + 6, struct.pack('<i', -8))),
                ('an LnZ text without end', pet_file().replace(b'[Adult]\r\n\0', b'[Adult]\r\n ' + b' ' * 70000)),
                ('no picture', whole[:whole.rindex(PICTURE)]),
                ('a picture size that is not the rest of the file', whole + b'\0'),
                ('a picture of another form', pet_file(picture=dib(bits=8)))):
            with self.subTest(label), self.assertRaises(PetFileError):
                parse_pet(data, OFFERED)

    def test_only_a_regular_file_within_the_size_limit_is_read(self):
        self.offer(FIRST)
        path = self.file_of(FIRST)
        self.assertEqual(read_pet(path, 1048576).offered, OFFERED)
        self.assertIsNone(read_pet(path, len(pet_file()) - 1))
        self.assertIsNone(read_pet(path.with_name('moved.pet'), 1048576))
        self.assertIsNone(read_pet(path.parent, 1048576))
        path.write_bytes(b'')
        self.assertIsNone(read_pet(path, 1048576))

    def test_a_scan_finds_stored_pets_and_ignores_everything_else(self):
        self.offer(FIRST)
        self.offer(SECOND)
        self.claim(SECOND)
        (self.rehoming / '.rehoming.lock').write_bytes(b'\0')
        (self.rehoming / 'available' / ('.pending-' + THIRD)).write_bytes(pet_file()[:5000])
        (self.rehoming / 'available' / 'notes.txt').write_text('not a pet')
        (self.rehoming / 'available' / f'{THIRD}.{WITHDRAW}.{NO_CLAIM}.pet').mkdir()
        (self.rehoming / 'claimed' / f'{THIRD}.{WITHDRAW}.pet').write_bytes(pet_file())
        found = scan(self.rehoming, 100)
        self.assertEqual({pet: where for pet, (where, _) in found.items()}, {FIRST: 'available', SECOND: 'claimed'})
        self.assertEqual(found[SECOND][1], self.file_of(SECOND))
        # A folder that is missing or holds more than the bound cannot be scanned in part.
        with self.assertRaises(OSError) as raised:
            scan(self.rehoming, 3)
        self.assertIn('MAX_PETS', str(raised.exception))
        (self.rehoming / 'claimed').rename(self.rehoming / 'elsewhere')
        with self.assertRaises(OSError):
            scan(self.rehoming, 100)


class TrackerTests(Folder):
    CALICO_FACTS = ('Breed: Calico\nGender: Female\nAdopted: Unknown\nAge: Adult\nSpayed: No\n'
                    f'Waiting since: <t:{OFFERED}:f>')
    WAITING = '**Biscuit** the Calico is looking for a home.\n' + CALICO_FACTS

    def test_a_new_pet_is_posted_once_with_its_picture(self):
        tracker = self.tracker()
        tracker.run_once()
        self.assertEqual((self.discord.calls, self.stored_state()), ([], {}))
        self.offer(FIRST)
        tracker.run_once()
        (message, posted), = self.discord.messages.items()
        self.assertEqual((posted['channel'], posted['content']), (ENV['DISCORD_CHANNEL_ID'], self.WAITING))
        self.assertEqual(posted['files'], [('pet.png', dib_to_png(dib()))])
        self.assertEqual(self.stored_state(), {FIRST: {'message': message, 'where': 'available', 'name': 'Biscuit',
                                                       'breed': 'Calico', 'animal': 'cat', 'female': True, 'offered': OFFERED,
                                                       'age_stage': 'Adult', 'fixed': False, 'born': False,
                                                       'adopted': None, 'purebred': True}})
        # Nothing is posted twice, also after a restart, and nothing is edited while the pet waits.
        tracker.run_once()
        self.tracker().run_once()
        self.assertEqual(self.discord.calls, ['post'])
        # The bot leaves the rehoming folder as it found it.
        self.assertEqual(sorted(p.name for p in self.rehoming.rglob('*')),
                         [f'{FIRST}.{WITHDRAW}.{NO_CLAIM}.pet', 'available', 'claimed'])

    def test_a_mixed_kitten_keeps_its_birth_date_and_spay_status_after_restart(self):
        born_at = 1600000000
        tracker = self.tracker()
        tracker.run_once()
        self.offer(FIRST, pet_file(age=12, fixed=True, born=True, purebred=False, dates=(born_at, born_at)))
        tracker.run_once()
        facts = ('Breed: Mixed Breed\nGender: Female\nBorn: <t:1600000000:D>\nAge: Kitten\n'
                 f'Spayed: Yes\nWaiting since: <t:{OFFERED}:f>')
        self.assertEqual(self.discord.contents(), ['**Biscuit** the Calico is looking for a home.\n' + facts])
        self.claim(FIRST)
        tracker.run_once()
        self.delete(FIRST)
        self.tracker().run_once()
        self.assertEqual(self.discord.contents(), ['**Biscuit** the Calico has been adopted.\n'
                                                  + facts + f'\nRehomed: some time before <t:{NOW}:f>'])

    def test_dated_adoption_and_legacy_announcements_keep_their_distinct_formats(self):
        self.offer(FIRST, pet_file(female=False, fixed=True, dates=(1600000000, 1600000000)))
        self.tracker(POST_EXISTING='true').run_once()
        self.assertEqual(self.discord.contents()[0].splitlines()[2:6],
                         ['Gender: Male', 'Adopted: <t:1600000000:D>', 'Age: Adult', 'Neutered: Yes'])
        record = self.stored_state()[FIRST]
        for key in ('age_stage', 'born', 'adopted', 'fixed', 'purebred'):
            del record[key]
        (self.state / 'pets.json').write_text(json.dumps({'version': 1, 'pets': {FIRST: record}}), encoding='utf-8')
        self.delete(FIRST)
        self.tracker().run_once()
        self.assertEqual(self.discord.contents(), ['**Biscuit** the Calico is no longer at the Adoption Center.\n'
                         f'Male cat, offered <t:{OFFERED}:f>, left some time before <t:{NOW}:f>.'])

    def test_first_seen_on_a_visit_posts_a_portrait_using_the_listing_mask(self):
        tracker = self.tracker()
        tracker.run_once()
        self.offer(FIRST, pet_file(picture=dib(pixel=lambda x, y: (0, 0, 128))))
        self.claim(FIRST)
        masks = self.rehoming / 'portraits'
        masks.mkdir()
        mask = bytearray(1080)
        mask[-1] = 0x80  # Bottom right pixel in the top-down coverage.
        (masks / (FIRST + '.mask')).write_bytes(mask)
        tracker.run_once()
        self.assertEqual(self.stored_state()[FIRST]['where'], 'claimed')
        self.assertEqual(self.discord.calls, ['post'])
        [(name, attachment)] = self.discord.messages['1000']['files']
        self.assertEqual(name, 'pet.png')
        _, _, rows = png_pixels(attachment)
        self.assertEqual(rows[-1][-1], (0, 0, 128, 255))
        self.assertEqual(rows[-1][-4], (0, 0, 128, 0))

    def test_the_picture_is_left_out_when_asked_for(self):
        self.offer(FIRST)
        self.tracker(POST_EXISTING='true', ATTACH_PICTURE='false').run_once()
        self.assertEqual([message['files'] for message in self.discord.messages.values()], [[]])

    def test_an_adoption_edits_the_pets_own_message(self):
        tracker = self.tracker()
        tracker.run_once()
        self.offer(FIRST)
        tracker.run_once()
        self.claim(FIRST)
        tracker.run_once()
        self.assertEqual((self.discord.calls, self.stored_state()[FIRST]['where']), (['post'], 'claimed'))
        self.now += 300
        self.delete(FIRST)
        tracker.run_once()
        self.assertEqual(self.discord.calls, ['post', 'edit'])
        self.assertEqual(self.discord.contents(), [
            '**Biscuit** the Calico has been adopted.\n'
            + self.CALICO_FACTS + f'\nRehomed: <t:{NOW + 300}:f>'])
        # The picture stays, and the pet is forgotten: later passes and a restart do nothing more.
        self.assertEqual([name for name, _ in self.discord.messages['1000']['files']], ['pet.png'])
        self.assertEqual(self.stored_state(), {})
        tracker.run_once()
        self.tracker().run_once()
        self.assertEqual(self.discord.calls, ['post', 'edit'])

    def test_a_visit_that_ends_without_an_adoption_changes_nothing(self):
        tracker = self.tracker()
        tracker.run_once()
        self.offer(FIRST)
        for step in (lambda: None, lambda: self.claim(FIRST), lambda: self.release(FIRST)):
            step()
            tracker.run_once()
        self.assertEqual((self.discord.calls, self.discord.contents()), (['post'], [self.WAITING]))
        self.assertEqual(self.stored_state()[FIRST]['where'], 'available')

    def test_a_pet_that_leaves_without_being_seen_on_a_visit_is_not_called_adopted(self):
        tracker = self.tracker()
        tracker.run_once()
        self.offer(FIRST)
        tracker.run_once()
        self.now += 60
        self.delete(FIRST)
        tracker.run_once()
        self.assertEqual(self.discord.contents(), [
            '**Biscuit** the Calico is no longer at the Adoption Center.\n'
            + self.CALICO_FACTS + f'\nLeft: <t:{NOW + 60}:f>'])

    def test_a_restart_keeps_each_pets_message_and_says_when_the_time_is_not_known(self):
        first = self.tracker()
        first.run_once()
        self.offer(FIRST)
        self.offer(SECOND, pet_file(b'Rex', b'Labrador', b'Dogz', b'dog', 1, False), OFFERED + 60)
        first.run_once()
        self.claim(SECOND)
        first.run_once()
        # The bot is stopped; the visiting pet is adopted meanwhile.
        self.delete(SECOND)
        self.now += 7200
        second = self.tracker()
        second.run_once()
        self.assertEqual(self.discord.calls, ['post', 'post', 'edit'])
        self.assertEqual(self.discord.contents(), [
            self.WAITING,
            '**Rex** the Labrador has been adopted.\n'
            'Breed: Labrador\nGender: Male\nAdopted: Unknown\nAge: Adult\nNeutered: No\n'
            f'Waiting since: <t:{OFFERED + 60}:f>\nRehomed: some time before <t:{NOW + 7200}:f>'])
        # From here the bot is watching again: the next departure has its time.
        self.claim(FIRST)
        second.run_once()
        self.now += 30
        self.delete(FIRST)
        second.run_once()
        self.assertEqual(self.discord.contents()[0].splitlines()[-1], f'Rehomed: <t:{NOW + 7230}:f>')

    def test_pets_waiting_at_a_first_start_are_taken_as_known_and_never_posted(self):
        self.offer(FIRST)
        self.offer(SECOND)
        self.claim(SECOND)
        with self.assertLogs('rehome-bot', 'INFO') as logs:
            tracker = self.tracker()
            tracker.run_once()
        self.assertIn('2 stored pets are taken as known', logs.output[0])
        self.assertEqual(self.stored_state(), {FIRST: {'message': None, 'where': 'available'},
                                               SECOND: {'message': None, 'where': 'claimed'}})
        # Their adoption has no message to edit; they are forgotten. A pet offered later is posted.
        self.delete(SECOND)
        self.offer(THIRD, offered=OFFERED + 5)
        tracker.run_once()
        self.assertEqual((self.discord.calls, sorted(self.stored_state())), (['post'], sorted([FIRST, THIRD])))
        self.delete(FIRST)
        self.tracker().run_once()
        self.assertEqual((self.discord.calls, list(self.stored_state())), (['post'], [THIRD]))

    def test_pets_waiting_at_a_first_start_are_posted_oldest_first_when_asked_for(self):
        self.offer(FIRST, pet_file(b'Rex', b'Labrador', b'Dogz', b'dog', 1, False), OFFERED + 60)
        self.offer(SECOND, offered=OFFERED)
        self.claim(SECOND)
        tracker = self.tracker(POST_EXISTING='true')
        tracker.run_once()
        self.assertEqual([content.split('**')[1] for content in self.discord.contents()], ['Biscuit', 'Rex'])
        self.delete(SECOND)
        tracker.run_once()
        self.assertEqual(self.discord.calls, ['post', 'post', 'edit'])
        self.assertIn('has been adopted', self.discord.contents()[0])

    def test_a_pet_adopted_before_the_first_start_left_nothing_to_post(self):
        # The server deletes an adopted pet's file and keeps no record of it: both settings see an empty folder.
        self.offer(FIRST)
        self.claim(FIRST)
        self.delete(FIRST)
        for setting in ('false', 'true'):
            with self.subTest(POST_EXISTING=setting):
                self.tracker(POST_EXISTING=setting).run_once()
                self.assertEqual((self.discord.calls, self.stored_state()), ([], {}))
                (self.state / 'pets.json').unlink()

    def test_a_file_that_does_not_read_as_a_pet_is_skipped_and_looked_at_again(self):
        tracker = self.tracker()
        tracker.run_once()
        self.offer(FIRST, pet_file()[:20000])
        self.offer(SECOND, offered=OFFERED + 1)
        # An offer the server is still writing has a pending name and is not a pet yet.
        (self.rehoming / 'available' / ('.pending-' + THIRD)).write_bytes(pet_file()[:100])
        with self.assertLogs('rehome-bot', 'WARNING') as logs:
            tracker.run_once()
        self.assertEqual(len(logs.output), 1)
        self.assertIn(FIRST, logs.output[0])
        self.assertEqual((self.discord.calls, list(self.stored_state())), (['post'], [SECOND]))
        with self.assertNoLogs('rehome-bot', 'WARNING'):
            tracker.run_once()
        self.assertEqual(self.discord.calls, ['post'])
        # The same file, whole: it is posted now.
        path = self.file_of(FIRST)
        path.write_bytes(pet_file(b'Rex', b'Labrador', b'Dogz', b'dog', 1, False))
        tracker.run_once()
        self.assertEqual(self.discord.calls, ['post', 'post'])
        self.assertIn('**Rex** the Labrador', self.discord.contents()[1])
        # A file larger than the bound is not read at all.
        self.offer(THIRD, pet_file() + bytes(70000))
        with self.assertLogs('rehome-bot', 'WARNING'):
            self.tracker(MAX_PET_BYTES='65536').run_once()
        self.assertEqual(self.discord.calls, ['post', 'post'])

    def test_a_rate_limit_is_waited_out_and_the_post_is_made_on_the_next_pass(self):
        tracker = self.tracker()
        tracker.run_once()
        self.offer(FIRST)
        self.offer(SECOND, offered=OFFERED + 1)
        self.discord.failures = [DiscordError(429, 'Discord answered 429', 2.5), DiscordError(429, 'Discord answered 429', 900.0)]
        for expected in ([2.5], [2.5, 60]):
            with self.assertLogs('rehome-bot', 'WARNING'):
                tracker.run_once()
            # The pass stops at the refusal: the second pet waits behind the first, and nothing is recorded.
            self.assertEqual((self.slept, self.discord.messages, self.stored_state()), (expected, {}, {}))
        self.assertEqual(self.discord.calls, ['post', 'post'])
        tracker.run_once()
        self.assertEqual((len(self.discord.messages), self.slept), (2, [2.5, 60]))

    def test_an_edit_discord_did_not_take_is_made_later_with_the_time_first_seen(self):
        tracker = self.tracker()
        tracker.run_once()
        self.offer(FIRST)
        tracker.run_once()
        self.claim(FIRST)
        tracker.run_once()
        self.delete(FIRST)
        self.discord.failures = [DiscordError(0, 'Discord could not be reached'), DiscordError(429, 'Discord answered 429', 1.0)]
        for _ in range(2):
            with self.assertLogs('rehome-bot', 'WARNING'):
                tracker.run_once()
            self.now += 500
        self.assertEqual((self.discord.contents(), self.slept), ([self.WAITING], [1.0]))
        self.assertEqual(self.stored_state()[FIRST]['ended'], {'status': 'adopted', 'at': NOW, 'exact': True})
        # Also after a restart: the edit that is still owed is in the state file.
        self.tracker().run_once()
        self.assertEqual(self.discord.contents()[0].splitlines()[-1], f'Rehomed: <t:{NOW}:f>')
        self.assertEqual(self.stored_state(), {})

    def test_a_deleted_message_is_not_edited_again_and_again(self):
        tracker = self.tracker()
        tracker.run_once()
        self.offer(FIRST)
        self.offer(SECOND, offered=OFFERED + 1)
        tracker.run_once()
        del self.discord.messages['1000']  # A moderator deleted the first pet's message.
        self.delete(FIRST)
        self.delete(SECOND)
        with self.assertNoLogs('rehome-bot', 'WARNING'):
            tracker.run_once()
        # One attempt for the deleted message, then the other pet's edit in the same pass; both are forgotten.
        self.assertEqual(self.discord.calls, ['post', 'post', 'edit', 'edit'])
        self.assertIn('no longer at the Adoption Center', self.discord.messages['1001']['content'])
        self.assertEqual(self.stored_state(), {})
        tracker.run_once()
        self.tracker().run_once()
        self.assertEqual(len(self.discord.calls), 4)

    def test_a_post_whose_state_could_not_be_saved_is_not_made_twice(self):
        tracker = self.tracker()
        tracker.run_once()
        self.offer(FIRST)
        with mock.patch('rehome_bot.tracker.save_state', side_effect=OSError(28, 'No space left on device')):
            with self.assertLogs('rehome-bot', 'WARNING') as logs:
                tracker.run_once()
                # The process still knows the message: it posts nothing more and keeps trying to save.
                tracker.run_once()
        self.assertIn('No space left on device', logs.output[0])
        self.assertEqual((self.discord.calls, self.stored_state()), (['post'], {}))
        # Stopped before the state could be written: the next start posts with the same nonce, which Discord
        # answers with the message it already has (for a few minutes), so there is still one message.
        restarted = self.tracker()
        restarted.run_once()
        self.assertEqual((self.discord.calls, list(self.discord.messages)), (['post', 'post'], ['1000']))
        self.assertEqual(self.stored_state()[FIRST]['message'], '1000')
        # Left running instead, the first process saves on its next pass.
        (self.state / 'pets.json').write_text('{"version": 1, "pets": {}}', encoding='utf-8')
        tracker.run_once()
        self.assertEqual((len(self.discord.calls), self.stored_state()[FIRST]['message']), (2, '1000'))

    def test_a_pet_offered_again_after_its_adoption_gets_a_new_message(self):
        tracker = self.tracker()
        tracker.run_once()
        self.offer(FIRST)
        tracker.run_once()
        self.claim(FIRST)
        tracker.run_once()
        self.delete(FIRST)
        tracker.run_once()
        # The server gives every offer a new ID, also of a pet it has stored before.
        self.offer(SECOND, offered=OFFERED + 9000)
        tracker.run_once()
        self.assertEqual(self.discord.calls, ['post', 'edit', 'post'])
        self.assertEqual([content.split('\n')[0] for content in self.discord.contents()],
                         ['**Biscuit** the Calico has been adopted.', '**Biscuit** the Calico is looking for a home.'])
        self.assertNotEqual(nonce_of(FIRST), nonce_of(SECOND))

    def test_a_pet_whose_file_is_back_before_its_message_was_edited_waits_again(self):
        tracker = self.tracker()
        tracker.run_once()
        self.offer(FIRST)
        tracker.run_once()
        data = self.file_of(FIRST).read_bytes()
        self.delete(FIRST)
        self.discord.failures = [DiscordError(0, 'Discord could not be reached')]
        with self.assertLogs('rehome-bot', 'WARNING'):
            tracker.run_once()
        self.assertIn('ended', self.stored_state()[FIRST])
        self.offer(FIRST, data)
        tracker.run_once()
        self.assertEqual((self.discord.calls, self.discord.contents()), (['post', 'edit'], [self.WAITING]))
        self.assertNotIn('ended', self.stored_state()[FIRST])

    def test_a_folder_that_cannot_be_read_says_nothing_about_its_pets(self):
        tracker = self.tracker()
        tracker.run_once()
        self.offer(FIRST)
        tracker.run_once()
        self.claim(FIRST)
        tracker.run_once()
        (self.rehoming / 'claimed').rename(self.rehoming / 'elsewhere')
        with self.assertRaises(OSError):
            tracker.run_once()
        self.assertNotIn('ended', self.stored_state()[FIRST])
        # Readable again, with the pet adopted in the meantime: the bot was not watching, so no time is claimed.
        (self.rehoming / 'elsewhere').rename(self.rehoming / 'claimed')
        self.delete(FIRST)
        self.now += 900
        tracker.run_once()
        self.assertEqual(self.discord.contents()[0].splitlines()[-1], f'Rehomed: some time before <t:{NOW + 900}:f>')

    def test_a_first_start_waits_for_a_folder_it_can_read(self):
        (self.rehoming / 'available').rmdir()
        tracker = self.tracker()
        with self.assertRaises(OSError):
            tracker.run_once()
        self.assertFalse((self.state / 'pets.json').exists())
        (self.rehoming / 'available').mkdir()
        self.offer(FIRST)
        tracker.run_once()
        self.assertEqual((self.discord.calls, self.stored_state()), ([], {FIRST: {'message': None, 'where': 'available'}}))

    def test_a_pets_text_cannot_format_mention_or_link_and_the_message_fits(self):
        record = {'name': '**@everyone** <@1234> [x](https://example.com) `', 'breed': '_Tabby_ ' + 'y' * 300, 'animal': 'cat',
                  'female': False, 'offered': OFFERED}
        text = message_text(record)
        self.assertTrue(text.startswith('**\\*\\*\\@everyone\\*\\* \\<\\@1234\\> \\[x\\]\\(https\\:\\/\\/example\\.com\\) \\`** the \\_Tabby\\_ yyy'), text)
        self.assertLess(len(text), 2000)
        self.assertEqual(plain('Mr. O\'Malley-Smith II'), 'Mr\\. O\\\'Malley\\-Smith II')
        self.assertEqual(plain('x' * 65), 'x' * 63 + '…')
        # Only letters, digits and spaces are left as they are.
        self.assertEqual(plain('Biscuit 2 Zoë'), 'Biscuit 2 Zoë')

    def test_a_state_file_that_is_not_the_bots_stops_the_start(self):
        self.assertIsNone(load_state(self.state))
        self.state.mkdir()
        for text in ('', '{"pets": [', '{"posted": []}', '{"pets": {"not-an-id": {"message": null, "where": "available"}}}',
                     json.dumps({'pets': {FIRST: {'message': '../../webhooks/1', 'where': 'available'}}}),
                     json.dumps({'pets': {FIRST: {'message': '1000', 'where': 'available', 'name': 'Biscuit'}}})):
            with self.subTest(text=text):
                (self.state / 'pets.json').write_text(text, encoding='utf-8')
                with self.assertRaises(StateError):
                    self.tracker()
        (self.state / 'pets.json').unlink()
        self.offer(FIRST)
        self.tracker(POST_EXISTING='true').run_once()
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ['pets.json'])
        self.assertEqual(list(load_state(self.state)), [FIRST])


class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class DiscordTests(unittest.TestCase):
    def test_a_post_is_one_message_with_its_file_a_nonce_and_no_mentions(self):
        seen = {}
        def opener(request, timeout):
            seen.update(url=request.full_url, headers=dict(request.header_items()), body=request.data, method=request.get_method())
            return Answer(b'{"id": "42"}')
        self.assertEqual(post_message('secret-token', '123456789012345678', '@everyone hello', [('pet.png', b'\x89PNG')], 'abc123', opener=opener), '42')
        self.assertEqual((seen['method'], seen['url']), ('POST', 'https://discord.com/api/v10/channels/123456789012345678/messages'))
        self.assertEqual(seen['headers']['Authorization'], 'Bot secret-token')
        boundary = seen['headers']['Content-type'].split('boundary=')[1].encode()
        parts = seen['body'].split(b'--' + boundary)
        self.assertEqual(len(parts), 4)
        payload = json.loads(parts[1].split(b'\r\n\r\n', 1)[1])
        self.assertEqual(payload, {'content': '@everyone hello', 'allowed_mentions': {'parse': []}, 'nonce': 'abc123',
                                   'enforce_nonce': True, 'attachments': [{'id': 0, 'filename': 'pet.png'}]})
        self.assertIn(b'name="files[0]"; filename="pet.png"', parts[2])
        self.assertTrue(parts[2].endswith(b'\r\n\r\n\x89PNG\r\n'))

    def test_an_edit_replaces_the_text_of_one_message_and_names_no_files(self):
        seen = {}
        def opener(request, timeout):
            seen.update(url=request.full_url, headers=dict(request.header_items()), body=request.data, method=request.get_method())
            return Answer(b'{"id": "42", "content": "new text"}')
        self.assertIsNone(edit_message('secret-token', '123456789012345678', '42', '@here new text', opener=opener))
        self.assertEqual((seen['method'], seen['url']), ('PATCH', 'https://discord.com/api/v10/channels/123456789012345678/messages/42'))
        self.assertEqual((seen['headers']['Authorization'], seen['headers']['Content-type']), ('Bot secret-token', 'application/json'))
        # Without an "attachments" list Discord keeps the files the message has.
        self.assertEqual(json.loads(seen['body']), {'content': '@here new text', 'allowed_mentions': {'parse': []}})

    def test_a_refusal_carries_its_status_its_wait_and_its_code_and_never_the_token(self):
        def refuse(status, body):
            def opener(request, timeout):
                raise urllib.error.HTTPError(request.full_url, status, 'refused', {}, io.BytesIO(body))
            return opener
        for status, body, check in (
                (429, b'{"message": "You are being rate limited.", "retry_after": 1.5, "global": false}', lambda e: e.retry_after == 1.5 and not e.message_lost),
                (429, b'<html>rate limited</html>', lambda e: e.retry_after == 5.0),
                (429, b'{"retry_after": "soon"}', lambda e: e.retry_after == 5.0),
                (429, b'{"retry_after": -3}', lambda e: e.retry_after == 5.0),
                (404, b'{"message": "Unknown Message", "code": 10008}', lambda e: e.message_lost and e.retry_after is None),
                (403, b'{"message": "Cannot edit a message authored by another user", "code": 50005}', lambda e: e.message_lost),
                (404, b'{"message": "Unknown Channel", "code": 10003}', lambda e: not e.message_lost),
                (403, b'{"message": "Missing Permissions", "code": 50013}', lambda e: not e.message_lost),
                (401, b'not json', lambda e: not e.message_lost and e.code == 0)):
            for call in (lambda opener: post_message('secret-token', '1', 'x', [], 'n', opener=opener),
                         lambda opener: edit_message('secret-token', '1', '2', 'x', opener=opener)):
                with self.subTest(status=status, body=body), self.assertRaises(DiscordError) as raised:
                    call(refuse(status, body))
                self.assertEqual(raised.exception.status, status)
                self.assertTrue(check(raised.exception))
                self.assertNotIn('secret-token', str(raised.exception))
        def unreachable(request, timeout):
            raise urllib.error.URLError('no route')
        with self.assertRaises(DiscordError) as raised:
            edit_message('secret-token', '1', '2', 'x', opener=unreachable)
        self.assertEqual(raised.exception.status, 0)

    def test_an_answer_without_a_message_id_is_not_taken_as_a_post(self):
        for body in (b'{}', b'[]', b'{"id": "../1"}', b'not json'):
            with self.subTest(body=body), self.assertRaises(DiscordError) as raised:
                post_message('secret-token', '1', 'x', [], 'n', opener=lambda request, timeout: Answer(body))
            self.assertEqual(raised.exception.status, 0)

    def test_the_body_has_one_part_for_the_payload_and_one_for_each_file(self):
        body, content_type = multipart({'content': 'x'}, [])
        self.assertTrue(content_type.startswith('multipart/form-data; boundary=rehome-bot-'))
        self.assertEqual(body.count(b'Content-Disposition'), 1)

    def test_a_nonce_fits_discords_limit_and_does_not_show_the_pets_id(self):
        nonce = nonce_of(FIRST)
        self.assertEqual((len(nonce), nonce, FIRST in nonce), (25, nonce_of(FIRST), False))


if __name__ == '__main__':
    unittest.main()
