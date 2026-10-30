"""Public bio address evidence is explicit, transient and independent of message content."""
import asyncio
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from tg_triage import group_notices,telegram_io
from tg_triage.store import Store
from tests.helpers import message


class BioTests(unittest.TestCase):
    def test_only_explicit_self_description_sets_address(self):
        for bio,address in (('he/him','Anh'),('pronouns: she/her','Chị'),('I use he/him','Anh'),
                            ('Developer • she/her','Chị'),('gender: male','Anh'),('giới tính: nữ','Chị'),
                            ('preferred address: chị','Chị'),('Nam','Anh/Chị'),('My friend uses he/him','Anh/Chị'),
                            ('She is our team lead','Anh/Chị'),('he/him; she/her','Anh/Chị'),
                            ('gender: male\npronouns: she/her','Anh/Chị'),(None,'Anh/Chị')):
            with self.subTest(bio=bio): self.assertEqual(group_notices.address_from_public_bio(bio),address)

    def test_personalization_preserves_quoted_source_and_urls(self):
        question=group_notices.address_question('Bạn muốn sửa “bạn dùng Anh/Chị nhé” ở đâu?')
        text=group_notices.personalize_notice(question+' https://example.test/Anh/Chị','Chị')
        self.assertTrue(text.startswith('Chị muốn'))
        self.assertIn('“bạn dùng Anh/Chị nhé”',text)
        self.assertTrue(text.endswith('https://example.test/Anh/Chị'))


    def test_nested_or_unbalanced_quotes_never_rewrite_source_words(self):
        for quote in ('“A ” Anh/Chị B”','“A Anh/Chị B','"A " Anh/Chị B"'):
            text='Anh/Chị vui lòng xác nhận '+quote
            rendered=group_notices.personalize_notice(text,'Chị')
            self.assertTrue(rendered.startswith('Chị vui lòng'))
            self.assertTrue(rendered.endswith(quote))
        from tg_triage.followups import _describe
        source=message(text='A ” Anh/Chị B')
        described=_describe(source)
        self.assertIn('Anh/Chị',group_notices.personalize_notice(described,'Chị'))


    def test_generated_suffix_after_balanced_or_nested_source_is_personalized(self):
        for quote in ('“source Anh/Chị”','“A ” Anh/Chị B”','"A \" Anh/Chị B"'):
            question=group_notices.address_question('Bạn xác nhận '+quote+' — bạn muốn thế nào?')
            text=group_notices.personalize_notice(question,'Anh')
            self.assertTrue(text.startswith('Anh xác nhận'))
            self.assertIn(quote,text)
            self.assertTrue(text.endswith('— Anh muốn thế nào?'))
        from tg_triage.followups import _describe
        for source in ('A “ Anh/Chị B','A ” Anh/Chị B'):
            described=_describe(message(text=source))
            text=group_notices.personalize_notice(described+' — Anh/Chị xác nhận nhé','Anh')
            self.assertIn('Anh/Chị B',text)
            self.assertTrue(text.endswith('— Anh xác nhận nhé'))


class ProfileTests(unittest.IsolatedAsyncioTestCase):
    def modules(self):
        types=SimpleNamespace(Chat=type('Chat',(),{}))
        request=type('GetFullUserRequest',(),{'__init__':lambda self,user:setattr(self,'id',user)})
        return types,{'telethon':SimpleNamespace(),'telethon.tl':SimpleNamespace(types=types),
                      'telethon.tl.functions':SimpleNamespace(),
                      'telethon.tl.functions.users':SimpleNamespace(GetFullUserRequest=request)}

    def client(self,types,bio='pronouns: she/her'):
        entity=types.Chat()
        class Client:
            get_entity=AsyncMock(return_value=entity)
            get_input_entity=AsyncMock(side_effect=lambda sender:sender)
            send_message=AsyncMock(return_value=SimpleNamespace(id=901))
            edit_message=AsyncMock(return_value=SimpleNamespace(id=441))
            full=AsyncMock(return_value=SimpleNamespace(full_user=SimpleNamespace(about=bio),users=[]))
            async def __call__(self,request): return await self.full(request)
        return Client()

    async def test_normal_send_and_notice_edit_resolve_actual_stored_source_author(self):
        types,modules=self.modules()
        with patch.dict(sys.modules,modules):
            client=self.client(types); store=Store(':memory:'); self.addCleanup(store.close)
            store.enqueue(message(440,sender_id=77))
            store.reserve_group_notice(message().chat_id,440,'followup')
            store.finish_group_notice(message().chat_id,440,'sent',441)
            sender=telegram_io.make_group_notice_sender(client,store)
            editor=telegram_io.make_group_notice_editor(client,store)
            text='Em cần Anh/Chị xác nhận “bạn đã nói Anh/Chị” https://example.test/Anh/Chị'
            await sender(message().chat_id,440,text)
            await editor(message().chat_id,441,text)
            self.assertEqual([c.args[0] for c in client.get_input_entity.await_args_list],[77,77])
            sent=client.send_message.await_args.args[1]; edited=client.edit_message.await_args.args[2]
            self.assertTrue(sent.startswith('Em cần Chị')); self.assertEqual(sent,edited)
            self.assertIn('“bạn đã nói Anh/Chị”',edited)
            self.assertIn('https://example.test/Anh/Chị',edited)

    async def test_timeout_blocked_failure_and_unknown_profiles_fallback(self):
        types,modules=self.modules()
        with patch.dict(sys.modules,modules):
            for scenario in ('unknown','blocked','failure','timeout'):
                client=self.client(types,bio='Nam')
                if scenario=='blocked': client.full.return_value.full_user.blocked=True
                if scenario=='failure': client.full.side_effect=OSError('private profile')
                if scenario=='timeout':
                    async def slow(*args): await asyncio.sleep(1)
                    client.full.side_effect=slow
                resolver=telegram_io.make_public_profile_address_resolver(client,timeout=.01)
                self.assertEqual(await resolver(77),'Anh/Chị')
                self.assertEqual(await resolver(None),'Anh/Chị')

    async def test_profile_cache_is_bounded_ttl_and_has_no_bio_persistence(self):
        types,modules=self.modules()
        now=[100.0]
        # Control the cache clock only; real asyncio timers still govern wait_for.
        cache_asyncio=SimpleNamespace(
            get_running_loop=lambda: SimpleNamespace(time=lambda: now[0]),
            wait_for=asyncio.wait_for)
        with patch.dict(sys.modules,modules), patch.object(telegram_io,'asyncio',cache_asyncio):
            client=self.client(types,bio='he/him')
            resolver=telegram_io.make_public_profile_address_resolver(client,ttl=.01,max_entries=2)
            self.assertEqual(await resolver(1),'Anh'); self.assertEqual(await resolver(1),'Anh')
            self.assertEqual(client.full.await_count,1)
            await resolver(2); await resolver(3); await resolver(1)
            self.assertEqual(client.full.await_count,4)
            now[0]+=.02; await resolver(1)
            self.assertEqual(client.full.await_count,5)
