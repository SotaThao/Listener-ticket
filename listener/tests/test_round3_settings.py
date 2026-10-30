"""Ignored prefixes and model-lock error boundaries using real transport/store/pipeline interfaces."""
import asyncio
import copy
import dataclasses
import io
import json
import os
import pickle
import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import tests
from tg_triage import classifier, model_lock, rules, telegram_io
from tg_triage.config import ConfigError, load_config
from tg_triage.pipeline import Pipeline
from tg_triage.store import Store
from tests.helpers import EXAMPLE, FAKE_HASH, FakeClassifier, FakeGitHub, FakeHTTP, config
from tests.test_telegram_io import event


class PrefixConfigTests(unittest.TestCase):
    def source(self):
        return re.sub(r'^ignored_command_prefixes.*\r?\n','',EXAMPLE.read_text(encoding='utf-8'),flags=re.M)

    def test_default_empty_and_valid_example_and_immutable_roundtrips(self):
        env={'TG_API_ID':'123','TG_API_HASH':FAKE_HASH}
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'cfg.toml';p.write_text(self.source(),encoding='utf-8')
            self.assertEqual(load_config(p,env).telegram.ignored_command_prefixes,())
            p.write_text(self.source().replace('[telegram]','[telegram]\nignored_command_prefixes=["/Aux","/Else"]'),encoding='utf-8')
            value=load_config(p,env)
            self.assertEqual(value.telegram.ignored_command_prefixes,('/Aux','/Else'))
            self.assertEqual(copy.deepcopy(value),value);self.assertEqual(pickle.loads(pickle.dumps(value)),value)
            self.assertEqual(dataclasses.asdict(value)['telegram']['ignored_command_prefixes'],('/Aux','/Else'))
        self.assertEqual(load_config(EXAMPLE,env).telegram.ignored_command_prefixes,('/nailhub',))

    def test_invalid_prefix_setting_fails_without_echoing_input(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'cfg.toml'
            for raw in ('"/Aux"','[1]','[true]','[""]','["PRIVATE"]','[" /Aux"]','["/Aux\\nPRIVATE"]'):
                p.write_text(self.source().replace('[telegram]','[telegram]\nignored_command_prefixes='+raw),encoding='utf-8')
                with self.subTest(raw=raw),self.assertRaises(ConfigError) as caught:
                    load_config(p,{'TG_API_ID':'123','TG_API_HASH':FAKE_HASH})
                self.assertIn('ignored_command_prefixes',str(caught.exception));self.assertNotIn('PRIVATE',str(caught.exception))


    def test_rejects_bare_overlap_invalid_names_and_duplicate_without_value(self):
        for prefixes in (('/',),('/b',),('/bug',),('/tas',),('/BUG',),('/Aux','/aUX'),('/Aux','/Aux'),('/aux-private',),('/aux@Bot',)):
            with self.subTest(prefixes=prefixes),tempfile.TemporaryDirectory() as directory:
                p=Path(directory)/'cfg.toml'
                p.write_text(self.source().replace('[telegram]','[telegram]\nignored_command_prefixes='+json.dumps(prefixes)),encoding='utf-8')
                with self.assertRaises(ConfigError) as caught:
                    load_config(p,{'TG_API_ID':'123','TG_API_HASH':FAKE_HASH})
                for prefix in prefixes:self.assertNotIn(prefix,str(caught.exception))

    def test_all_owned_commands_and_their_prefixes_are_rejected_from_canonical_rule(self):
        commands=rules.COMMAND.pattern.split('(',1)[1].split(')',1)[0].split('|')
        for command in commands:
            for size in range(1,len(command)+1):
                with self.subTest(command=command,size=size),tempfile.TemporaryDirectory() as directory:
                    p=Path(directory)/'cfg.toml'
                    p.write_text(self.source().replace('[telegram]','[telegram]\nignored_command_prefixes='+json.dumps(['/'+command[:size]])),encoding='utf-8')
                    with self.assertRaises(ConfigError):load_config(p,{'TG_API_ID':'123','TG_API_HASH':FAKE_HASH})

class PrefixTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_prefix_skips_before_db_reaction_metadata_and_model(self):
        store=Store(':memory:');self.addCleanup(store.close);queue=asyncio.Queue();react=AsyncMock()
        model=FakeClassifier();pipeline=Pipeline(config(),store,model,FakeGitHub())
        for text in ('/Aux scan PRIVATE',' /nailhub scan PRIVATE','\n/nailhub scan PRIVATE','/nailhub@Bot scan','/aUX@BotName scan','/aux'):
            incoming=event(raw_text=text,is_group=True)
            with patch.object(store,'enqueue',wraps=store.enqueue) as enqueue,self.assertLogs('tg_triage.telegram_io',level='INFO') as logs:
                await telegram_io.enqueue_event(incoming,queue,config().telegram.chat_ids,store,react=react,ignored_command_prefixes=('/aux','/nailhub'))
            self.assertEqual([r.getMessage() for r in logs.records],[f'decision=ignored_prefix chat_id={incoming.chat_id} message_id={incoming.id}'])
            enqueue.assert_not_called();incoming.get_sender.assert_not_called();incoming.get_chat.assert_not_called()
        self.assertTrue(queue.empty());self.assertEqual(list(store.queued_messages()),[]);react.assert_not_awaited()
        stop=asyncio.Event();task=asyncio.create_task(telegram_io.worker(queue,pipeline,stop,react=react))
        await asyncio.sleep(0);stop.set();await task
        self.assertEqual(model.calls,[])

    async def test_default_and_nonmatching_prefix_preserve_ingress(self):
        for prefixes,text in (((),'/aux scan'),(('/else',),'/aux scan'),(('/aux',),'/auxiliary PRIVATE'),(('/nailhub',),'/nailhubx'),(('/aux',),'/aux@Bot! scan'),(('/aux',),'/bug fix this')):
            store=Store(':memory:');self.addCleanup(store.close);queue=asyncio.Queue()
            await telegram_io.enqueue_event(event(raw_text=text),queue,config().telegram.chat_ids,store,ignored_command_prefixes=prefixes)
            self.assertEqual(queue.qsize(),1);self.assertEqual(len(list(store.queued_messages())),1)

    async def test_registered_listener_handler_passes_configured_prefix(self):
        store=Store(':memory:');self.addCleanup(store.close);cfg=config()
        cfg=replace(cfg,telegram=replace(cfg.telegram,ignored_command_prefixes=('/nailhub',)))
        handlers=[];registered=asyncio.Event();disconnected=asyncio.Event()
        def add(h,b):handlers.append(h);registered.set()
        client=SimpleNamespace(add_event_handler=add,remove_event_handler=Mock(),connect=AsyncMock(),
            get_me=AsyncMock(return_value=SimpleNamespace(id=42,bot=False)),disconnect=AsyncMock(),
            run_until_disconnected=AsyncMock(side_effect=disconnected.wait))
        model=FakeClassifier();pipeline=Pipeline(cfg,store,model,FakeGitHub())
        with patch.dict('sys.modules',{'telethon':SimpleNamespace(events=SimpleNamespace(NewMessage=lambda **k:k))}),patch('tg_triage.telegram_io.make_client',return_value=client),patch('tg_triage.reactions.react',new=AsyncMock(return_value=False)) as reaction:
            task=asyncio.create_task(telegram_io.run(cfg,pipeline))
            try:
                await asyncio.wait_for(registered.wait(),1)
                for i,text in enumerate(('/NaIlHuB@Bot scan',' /nailhub scan','\n/nailhub scan'),1):
                    with self.assertLogs('tg_triage.telegram_io',level='INFO') as logs:
                        await handlers[0](event(id=i,raw_text=text))
                    self.assertEqual(logs.records[0].getMessage(),f'decision=ignored_prefix chat_id={cfg.telegram.chat_ids[0]} message_id={i}')
                self.assertEqual(list(store.queued_messages()),[]);self.assertEqual(model.calls,[])
                # These messages must reach the real ingress store through the registered handler.
                with patch.object(store,'enqueue',wraps=store.enqueue) as enqueue:
                    await handlers[0](event(id=10,raw_text='/nailhubx'))
                    await handlers[0](event(id=11,raw_text='/bug fix this'))
                    self.assertEqual(enqueue.call_count,2)
                    self.assertEqual([call.args[0].text for call in enqueue.call_args_list],['/nailhubx','/bug fix this'])
                    self.assertEqual(reaction.await_count,1)
            finally:
                disconnected.set()
                with self.assertRaises(RuntimeError):await task


class LockBoundaryTests(unittest.TestCase):
    def test_os_lock_errors_log_error_distinct_from_http(self):
        for error in (PermissionError('PRIVATE'),OSError('PRIVATE')):
            with self.subTest(error=type(error).__name__),patch('tg_triage.model_lock._open_lock',side_effect=error),self.assertLogs('tg_triage.classifier',level='ERROR') as logs:
                opener=Mock()
                self.assertIsNone(classifier.Classifier(config().ollama,opener=opener).classify('ordinary'))
                opener.assert_not_called()
            self.assertTrue(all('model_lock' in r.getMessage() for r in logs.records))
            self.assertNotIn('PRIVATE',' '.join(logs.output));self.assertNotIn('Ollama request failed',' '.join(logs.output))

    def test_http_oserror_retains_http_failure_log(self):
        with self.assertLogs('tg_triage.classifier',level='WARNING') as logs:
            self.assertIsNone(classifier.Classifier(config().ollama,opener=FakeHTTP(OSError('private'))).classify('ordinary'))
        self.assertTrue(any('Ollama request failed' in r.getMessage() for r in logs.records))
        self.assertFalse(any('model_lock' in r.getMessage() for r in logs.records))

    def test_package_setup_owns_private_temp_model_directory(self):
        configured=Path(os.environ[model_lock.LOCK_DIR_ENV]).resolve()
        self.assertEqual(configured,Path(tests.TEST_MODEL_LOCK_DIR).resolve())
        self.assertTrue(configured.is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.assertNotEqual(str(configured),model_lock.DEFAULT_LOCK_DIR)
