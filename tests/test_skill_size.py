import json
import tempfile
import unittest
from pathlib import Path

from app.skills import MAX_STEPS, Skills

STEP = {'kind': 'click', 'x': 1, 'y': 1, 'label': 'x'}


class SkillSizeTests(unittest.TestCase):
    """A skill is a short routine. One run saved 300 recorded steps of an afternoon's browsing as a "skill"."""
    def test_a_long_recording_is_not_saved(self):
        with tempfile.TemporaryDirectory() as d:
            s = Skills(d)
            self.assertIsNone(s.save('Scout photographers', [STEP] * (MAX_STEPS + 1)))
            self.assertIsNotNone(s.save('Open the notes app and save', [STEP] * 5))
            self.assertEqual(len(s.list()), 1)

    def test_long_recordings_from_an_earlier_version_are_dropped_and_never_replayed(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, 'skills.json').write_text(json.dumps([
                {'id': 'a', 'task': 'Scout photographers', 'key': 'scout photographers', 'steps': [STEP] * 300, 'created': 1, 'uses': 0},
                {'id': 'b', 'task': 'Save a note', 'key': 'save a note', 'steps': [STEP] * 4, 'created': 2, 'uses': 0}]))
            s = Skills(d)
            self.assertIsNone(s.match('Scout photographers')); self.assertIsNotNone(s.match('Save a note'))
            self.assertEqual([k['id'] for k in s.list()], ['b'])


if __name__ == '__main__': unittest.main()
