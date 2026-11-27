"""Posts each pet that waits for adoption once, and edits that message when the pet has left.

The game server records no adoption: it deletes the pet's file. What the bot says about a pet that left therefore
follows from where the file was seen last:

- last seen in claimed/ (out on a visit): adopted. The server deletes a file there only when it accepts an adoption.
- last seen in available/: no longer at the Adoption Center, and no more than that. The offer was taken back, or a
  visit and its adoption both happened between two looks.

The time shown is the look that found the pet gone. When the bot was not watching just before (it had just started,
or the folder could not be read), the message says "some time before" that look.

State, pets.json in the state folder: for each stored pet its message ID, the folder it was seen in last and the
facts its message shows (the file is gone when the message has to be rewritten). A pet stays there until its file has
gone and its message was edited; then it is forgotten.
"""
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import string
import time

from .config import Config
from .discord import DiscordError, edit_message, post_message
from .pets import read_pet, scan

LOG = logging.getLogger('rehome-bot')
STATE_FILE = 'pets.json'
PICTURE_NAME = 'pet.png'
PET_ID = re.compile(r'[0-9a-f]{24}')
MESSAGE_ID = re.compile(r'[0-9]{1,25}')
FACTS = (('name', str), ('breed', str), ('animal', str), ('female', bool), ('offered', int))


class StateError(Exception):
    """The state file exists and is not one this bot wrote."""


def _valid(pet, record) -> bool:
    if not (isinstance(pet, str) and PET_ID.fullmatch(pet) and isinstance(record, dict)
            and record.get('where') in ('available', 'claimed')):
        return False
    if record.get('message') is None:
        return True  # Known and never announced: its facts were not read.
    ended = record.get('ended')
    return (isinstance(record['message'], str) and MESSAGE_ID.fullmatch(record['message']) is not None
            and all(isinstance(record.get(key), kind) for key, kind in FACTS)
            and (record.get('age_stage') is None or (
                isinstance(record['age_stage'], str) and isinstance(record.get('fixed'), bool)
                and isinstance(record.get('born'), bool) and isinstance(record.get('purebred'), bool)
                and (record.get('adopted') is None or type(record['adopted']) is int)))
            and (ended is None or isinstance(ended, dict) and ended.get('status') in ('adopted', 'left')
                 and isinstance(ended.get('at'), int) and isinstance(ended.get('exact'), bool)))


def load_state(state_dir: Path) -> dict | None:
    """The pets known so far, by ID; None when nothing was ever recorded (a first start)."""
    try:
        text = (state_dir / STATE_FILE).read_text(encoding='utf-8')
    except FileNotFoundError:
        return None
    try:
        pets = json.loads(text)['pets']
        valid = isinstance(pets, dict) and all(_valid(pet, record) for pet, record in pets.items())
    except (ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        raise StateError(f'{STATE_FILE} in the state folder is damaged. Restore it, or delete it to start again '
                         '(pets waiting now are then taken as known, or posted again with POST_EXISTING=true)')
    return pets


def save_state(state_dir: Path, pets: dict) -> None:
    """Written beside the file and moved over it, so the file is always a whole one."""
    state_dir.mkdir(parents=True, exist_ok=True)
    pending = state_dir / (STATE_FILE + '.tmp')
    with open(pending, 'w', encoding='utf-8') as stream:
        json.dump({'version': 1, 'pets': pets}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending, state_dir / STATE_FILE)


def nonce_of(pet: str) -> str:
    """What tells Discord that two posts are the same pet's. The pet's ID is the server's, so it is not sent as it is."""
    return hashlib.sha256(('rehome-bot:' + pet).encode('ascii')).hexdigest()[:25]


def plain(text: str, limit=64) -> str:
    """A text of a pet file as Discord shows it letter for letter: a backslash before every punctuation mark keeps
    it from being read as formatting, a mention or a link."""
    text = ''.join(c if c.isprintable() else ' ' for c in text)
    if len(text) > limit:
        text = text[:limit - 1] + '…'
    return ''.join('\\' + c if c in string.punctuation else c for c in text)


def message_text(record: dict) -> str:
    """The message of a pet: who it is and since when it waits; once it has left, what became of it and when."""
    who = f"**{plain(record['name'])}** the {plain(record['breed'])}"
    about = f"{'Female' if record['female'] else 'Male'} {record['animal']}"
    offered = f"<t:{record['offered']}:f>"
    ended = record.get('ended')
    if record.get('age_stage') is not None:
        adopted = record.get('adopted')
        date = f'<t:{adopted}:D>' if adopted is not None else 'Unknown'
        title = 'is looking for a home.' if ended is None else (
            'has been adopted.' if ended['status'] == 'adopted' else 'is no longer at the Adoption Center.')
        lines = [
            f'{who} {title}',
            'Breed: ' + ('Mixed Breed' if record.get('purebred') is False else plain(record['breed'])),
            'Gender: ' + ('Female' if record['female'] else 'Male'),
            ('Born: ' if record.get('born') else 'Adopted: ') + date,
            'Age: ' + plain(record['age_stage']),
            ('Spayed: ' if record['female'] else 'Neutered: ') + ('Yes' if record['fixed'] else 'No'),
            f'Waiting since: {offered}',
        ]
        if ended:
            label = 'Rehomed' if ended['status'] == 'adopted' else 'Left'
            when = f"<t:{ended['at']}:f>"
            lines.append(f"{label}: {'' if ended['exact'] else 'some time before '}{when}")
        return '\n'.join(lines)
    if ended is None:
        return f'{who} is looking for a home.\n{about}, waiting at the Adoption Center since {offered}.'
    when = f"<t:{ended['at']}:f>" if ended['exact'] else f"some time before <t:{ended['at']}:f>"
    if ended['status'] == 'adopted':
        return f'{who} has been adopted.\n{about}, offered {offered}, adopted {when}.'
    return f'{who} is no longer at the Adoption Center.\n{about}, offered {offered}, left {when}.'


class Tracker:
    def __init__(self, config: Config, *, send=post_message, edit=edit_message, clock=time.time, sleep=time.sleep):
        self.config, self.send, self.edit, self.clock, self.sleep = config, send, edit, clock, sleep
        self.pets = load_state(config.state_dir)
        self.unsaved = False
        # Whether the pass before this one saw the folder: a pet missing now then left between the two.
        self.watched = False
        self.unreadable = set()

    def save(self) -> None:
        """Keeps going when the state cannot be written: what was posted is still known to this process, and the
        next pass writes it."""
        try:
            save_state(self.config.state_dir, self.pets)
            self.unsaved = False
        except OSError as error:
            self.unsaved = True
            LOG.warning('The state could not be saved: %s', error.strerror or type(error).__name__)

    def run_once(self) -> None:
        """One pass over the folder. OSError when the folder cannot be read; nothing is concluded then."""
        config, watched = self.config, self.watched
        self.watched = False
        present = scan(config.rehoming_dir, config.max_pets)
        if self.pets is None:
            # A first start: pets already waiting are not announced unless asked for.
            self.pets = {} if config.post_existing else {pet: {'message': None, 'where': where}
                                                         for pet, (where, _) in present.items()}
            if self.pets:
                LOG.info('First start: %d stored pets are taken as known and not posted', len(self.pets))
            self.unsaved = True
        if any('ended' not in record and pet not in present for pet, record in self.pets.items()):
            # A file renamed during a scan can be missed by it: a pet has left when a second scan misses it too.
            present = {**present, **scan(config.rehoming_dir, config.max_pets)}
        self.watched = True
        now = int(self.clock())
        for pet, record in list(self.pets.items()):
            seen = present.get(pet)
            if seen is None:
                if record['message'] is None:
                    del self.pets[pet]  # Never announced: there is no message to edit.
                elif 'ended' not in record:
                    record['ended'] = {'status': 'adopted' if record['where'] == 'claimed' else 'left',
                                       'at': now, 'exact': watched}
                else:
                    continue
            elif record['where'] != seen[0] or 'ended' in record:
                record['where'] = seen[0]
                record.pop('ended', None)  # Its file is back before its message was edited: it waits again.
            else:
                continue
            self.unsaved = True
        if self.unsaved:
            self.save()
        self.unreadable &= present.keys()
        try:
            self._post_new(present)
            self._edit_ended()
        except DiscordError as error:
            # The same work is tried again on the next pass; nothing is skipped.
            LOG.warning('Discord did not take a message: %s', error)
            if error.retry_after is not None:
                self.sleep(min(error.retry_after, 60))

    def _post_new(self, present: dict) -> None:
        config, new = self.config, []
        for pet, (_, path) in present.items():
            if pet not in self.pets:
                try:
                    new.append((os.stat(path, follow_symlinks=False).st_mtime_ns, pet))
                except OSError:
                    pass  # It moved since the scan: the next pass finds it.
        for _, pet in sorted(new):  # The pet offered first is posted first.
            where, path = present[pet]
            info = read_pet(path, config.max_pet_bytes)
            if info is None:
                if pet not in self.unreadable:
                    self.unreadable.add(pet)
                    LOG.warning('Pet %s cannot be read as a stored pet; it is looked at again on every pass', pet)
                continue
            self.unreadable.discard(pet)
            record = {'message': None, 'where': where, 'name': info.name, 'breed': info.breed, 'animal': info.animal,
                      'female': info.female, 'offered': info.offered, 'born': info.born, 'adopted': info.adopted,
                      'fixed': info.fixed, 'age_stage': info.age_stage, 'purebred': info.purebred}
            files = [(PICTURE_NAME, info.picture)] if config.attach_picture else []
            record['message'] = self.send(config.token, config.channel_id, message_text(record), files, nonce_of(pet))
            self.pets[pet] = record
            self.save()
            LOG.info('Posted pet %s', pet)

    def _edit_ended(self) -> None:
        config = self.config
        for pet, record in list(self.pets.items()):
            if 'ended' not in record:
                continue
            try:
                self.edit(config.token, config.channel_id, record['message'], message_text(record))
                LOG.info('Pet %s has gone (%s); its message was edited', pet, record['ended']['status'])
            except DiscordError as error:
                if not error.message_lost:
                    raise
                # Trying again cannot succeed: the pet is forgotten like one whose message was edited.
                LOG.info('Pet %s has gone; its message was deleted or is not the bot\'s, so nothing is edited', pet)
            del self.pets[pet]
            self.save()


def run(config: Config) -> None:
    LOG.info('Watching %s every %d seconds', config.rehoming_dir, config.poll_seconds)
    tracker = Tracker(config)
    warned = False
    while True:
        try:
            tracker.run_once()
            warned = False
        except OSError as error:
            if not warned:
                LOG.warning('The rehoming folder %s cannot be read: %s', config.rehoming_dir, error.strerror or error)
                warned = True
        time.sleep(config.poll_seconds)
