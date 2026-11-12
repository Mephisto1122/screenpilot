"""A phone number for sign-ups, and codes from the user's phone: asked for, typed, never shown to the model."""
import unittest

from app.localexec import parse_step
from test_system import await_done
from test_text_mode import TextModeBase


class PhoneTests(TextModeBase):
    def test_phone_number_is_checked_and_told_to_the_model(self):
        self.app.room.update({'phone': '+972 50 123 4567'})
        with self.assertRaisesRegex(ValueError, 'Phone number'): self.app.room.update({'phone': 'call me'})
        context = self.app.room.prompt_context()
        self.assertIn('+972 50 123 4567', context); self.assertIn('ask_code', context)

    def test_code_from_the_phone_is_typed_but_not_shown_to_the_model(self):
        self.app.room.update({'phone': '+972 50 123 4567'})
        self.claude([{'actions': [{'kind': 'ask_code', 'reason': 'Send me the code WhatsApp texted you'}]}, {'goal_done': True, 'actions': [], 'note': 'ok'}])
        self.app.start_agent('Sign up for WhatsApp Business', 'anthropic_text')
        await_done(lambda: self.app.agent['status'] == 'waiting', 10)
        self.assertEqual(self.app.agent['waiting']['type'], 'code')
        with self.assertRaisesRegex(ValueError, 'Send the code'): self.app.runner.reply('a code; rm -rf')
        self.app.runner.reply('482 913')
        await_done(lambda: self.app.agent['status'] not in ('running', 'waiting') and not self.app.runner.thread.is_alive(), 10)
        self.assertEqual(self.app.agent['status'], 'completed', self.app.agent)
        typed = [a for a in self.app.desktop.lab_actions if a['kind'] == 'type']
        self.assertEqual((typed[0]['text'], typed[0].get('secret')), ('482913', True))
        self.assertNotIn('482', self.prompt(1)); self.assertIn('The user gave the code', self.prompt(1))
        self.assertIsNone(self.app.skills.match('Sign up for WhatsApp Business'))  # depends on a person: not saved as a skill

    def test_no_code_came(self):
        self.claude([{'actions': [{'kind': 'ask_code', 'reason': 'Send me the code'}]}, {'goal_done': True, 'actions': [], 'note': 'ok'}])
        self.app.start_agent('Sign up', 'anthropic_text')
        await_done(lambda: self.app.agent['status'] == 'waiting', 10)
        self.app.runner.reply('deny')
        await_done(lambda: self.app.agent['status'] not in ('running', 'waiting') and not self.app.runner.thread.is_alive(), 10)
        self.assertIn('The user did not give a code', self.prompt(1))
        self.assertEqual([a for a in self.app.desktop.lab_actions if a['kind'] == 'type'], [])

    def test_code_sent_to_the_telegram_bot_goes_to_the_waiting_worker(self):
        self.claude([{'actions': [{'kind': 'ask_code', 'reason': 'Send me the code'}]}, {'goal_done': True, 'actions': [], 'note': 'ok'}])
        tg = self.app.telegram; sent = []; tg.send = lambda text, buttons=None: sent.append(text)
        self.app.room.value['telegram_chat'] = 42
        self.app.start_agent('Sign up', 'anthropic_text')
        await_done(lambda: self.app.agent['status'] == 'waiting', 10)
        tg.handle({'update_id': 1, 'message': {'chat': {'id': 42}, 'text': '73 1904'}})
        await_done(lambda: self.app.agent['status'] not in ('running', 'waiting') and not self.app.runner.thread.is_alive(), 10)
        self.assertEqual([a['text'] for a in self.app.desktop.lab_actions if a['kind'] == 'type'], ['731904'])
        self.assertIn('Got it', sent[0]); self.assertEqual(self.app.tasks, [])  # not a new task

    def test_this_mac_sentences(self):
        self.assertEqual(parse_step('Ask the user for the code.'), [('ask_code', 'Ask the user for the code.')])
        self.assertEqual(parse_step('Type the verification code into the field'), [('ask_code', 'Send me the code you received')])
        self.assertEqual(parse_step('Ask the user to scan the QR code with WhatsApp.'), [('ask_user', 'scan the QR code with WhatsApp')])


if __name__ == '__main__': unittest.main()


class NoActionTests(TextModeBase):
    def test_a_reply_without_actions_is_asked_again(self):
        self.claude([{'actions': [], 'note': 'checking the archive'}, {'actions': [{'kind': 'scroll', 'amount': 2}]}, {'goal_done': True, 'actions': [], 'note': 'ok'}])
        self.assertEqual(self.run_task('Check the archive')['status'], 'completed')
        self.assertIn('Your reply had no actions', self.prompt(1))
