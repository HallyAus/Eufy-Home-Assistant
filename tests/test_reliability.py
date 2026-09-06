"""Hardware-independent regression tests for the reliability overhaul."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = load('reliability_api', 'custom_components/eufy_nvr/go2rtc_api.py')
snap = load('reliability_snapshot', 'custom_components/eufy_nvr/snapshot.py')
gen = load('reliability_generator', 'bridge/gen_go2rtc.py')
runtime = load('reliability_runtime', 'bridge/runtime.py')


class EndpointTests(unittest.TestCase):
    def test_rejects_invalid_hosts(self):
        for value in (None, 42, '', 'bad host', 'bad\nhost', 'host\\name', '-host',
                      'host-.local', 'a..b', 'foo:1985', 'https://host', 'host/path',
                      'http://bridge\n.local', 'http://user:password@host', 'http://host:123', 'http://host/?a=b'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                api.normalize_host(value)

    def test_canonical_identity(self):
        for value, expected in [(' HA.Local. ', 'ha.local'), ('http://HA.local/', 'ha.local'),
                                ('[2001:0db8:0:0::1]', '2001:db8::1'), ('192.168.1.10', '192.168.1.10')]:
            self.assertEqual(api.normalize_host(value), expected)

    def test_named_streams_does_not_hide_eufy_stream(self):
        self.assertEqual(set(api.extract_streams({'streams': {}, 'eufy_front': {}})), {'eufy_front'})

    def test_invalid_ports(self):
        for value in (True, None, -1, 0, 65536, '1985', 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                api.validate_port(value)


class ApiClientTests(unittest.IsolatedAsyncioTestCase):
    def client(self, *, payload=None, status=200, error=None, json_error=None):
        self.response = MagicMock(status=status)
        self.response.json = AsyncMock(return_value=payload, side_effect=json_error)
        if error:
            self.response.raise_for_status.side_effect = error
        self.context = MagicMock()
        self.context.__aenter__ = AsyncMock(return_value=self.response)
        self.context.__aexit__ = AsyncMock(return_value=False)
        self.session = MagicMock()
        self.session.get.return_value = self.context
        return api.Go2RtcClient(self.session, 'bridge.local', 1985, 10)

    async def test_filters_without_opening_media(self):
        client = self.client(payload={'eufy_front': {}, 'other': {}})
        self.assertEqual(await client.async_get_streams(), {'eufy_front': {}})
        self.assertEqual(client.total_stream_count, 2)
        self.session.get.assert_called_once_with(client.url, timeout=10, allow_redirects=False)

    async def test_http_auth_is_distinct_and_private(self):
        for status in (401, 403):
            error = aiohttp.ClientResponseError(MagicMock(), (), status=status, message='private-url-secret')
            client = self.client(status=status, error=error)
            with self.assertRaises(api.Go2RtcAuthenticationError) as raised:
                await client.async_get_streams()
            self.assertNotIn('private-url-secret', str(raised.exception))

    async def test_http_failure_is_private(self):
        client = self.client(error=aiohttp.ClientResponseError(MagicMock(), (), status=500, message='secret'))
        with self.assertRaises(api.Go2RtcConnectionError) as raised:
            await client.async_get_streams()
        self.assertNotIn('secret', str(raised.exception))

    async def test_redirect_is_not_followed(self):
        client = self.client(status=302)
        with self.assertRaises(api.Go2RtcPayloadError):
            await client.async_get_streams()
        self.response.json.assert_not_awaited()

    async def test_invalid_json_and_shape(self):
        for payload, error in [(None, ValueError('private-json')), ([], None), ({'streams': []}, None)]:
            client = self.client(payload=payload, json_error=error)
            with self.assertRaises(api.Go2RtcPayloadError) as raised:
                await client.async_get_streams()
            self.assertNotIn('private-json', str(raised.exception))

    async def test_timeout(self):
        client = self.client()
        self.context.__aenter__.side_effect = TimeoutError()
        with self.assertRaises(api.Go2RtcConnectionError):
            await client.async_get_streams()

    async def test_cancellation_propagates(self):
        client = self.client(json_error=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await client.async_get_streams()


class SnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 0
        self.cache = snap.SnapshotCache(ttl=30, cooldown=10, clock=lambda: self.now)
        self.fetch = AsyncMock(return_value=b'fresh-frame')
        self.size = (640, 360)

    async def test_repeated_viewers_use_one_extraction(self):
        results = await asyncio.gather(*(self.cache.async_get(self.size, self.fetch) for _ in range(25)))
        self.assertEqual(results, [b'fresh-frame'] * 25)
        self.fetch.assert_awaited_once()

    async def test_expires_and_caches_only_one_size(self):
        await self.cache.async_get(self.size, self.fetch)
        self.now = 29
        await self.cache.async_get(self.size, self.fetch)
        self.assertEqual(self.fetch.await_count, 1)
        self.now = 30
        await self.cache.async_get(self.size, self.fetch)
        await self.cache.async_get((320, 180), self.fetch)
        await self.cache.async_get(self.size, self.fetch)
        self.assertEqual(self.fetch.await_count, 4)

    async def test_failure_does_not_return_stale_frame_and_backs_off(self):
        await self.cache.async_get(self.size, self.fetch)
        self.now = 31
        self.fetch.side_effect = RuntimeError('private-rtsp-url')
        self.assertIsNone(await self.cache.async_get(self.size, self.fetch))
        self.assertIsNone(await self.cache.async_get(self.size, self.fetch))
        self.assertEqual(self.fetch.await_count, 2)
        self.now = 41
        self.fetch.side_effect = None
        self.assertEqual(await self.cache.async_get(self.size, self.fetch), b'fresh-frame')
        self.assertEqual(self.fetch.await_count, 3)

    async def test_none_result_has_cooldown(self):
        self.fetch.return_value = None
        await self.cache.async_get(self.size, self.fetch)
        await self.cache.async_get(self.size, self.fetch)
        self.fetch.assert_awaited_once()

    async def test_timeout_is_bounded(self):
        self.cache.timeout = 0.01
        self.assertIsNone(await self.cache.async_get(self.size, lambda: asyncio.sleep(10)))
        self.assertGreater(self.cache._retry_after, self.now)

    async def test_cancel_is_not_swallowed_and_lock_is_released(self):
        self.fetch.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.cache.async_get(self.size, self.fetch)
        self.fetch.side_effect = None
        self.assertEqual(await self.cache.async_get(self.size, self.fetch), b'fresh-frame')

    async def test_clear_discards_private_image(self):
        await self.cache.async_get(self.size, self.fetch)
        self.cache.clear()
        self.assertIsNone(self.cache._image)
        await self.cache.async_get(self.size, self.fetch)
        self.assertEqual(self.fetch.await_count, 2)


class GeneratorTests(unittest.TestCase):
    def manifest(self, cameras):
        return {'nvr_sn': 'test-nvr', 'cameras': cameras}

    def test_duplicate_names_are_unique_and_deterministic(self):
        cameras = [{'channel': 2, 'name': 'Front Door'}, {'channel': 0, 'name': 'Front Door'},
                   {'channel': 1, 'name': 'Front-Door'}, {'channel': 3, 'name': '日本語'}]
        streams, _ = gen.assign_names(self.manifest(cameras))
        self.assertEqual([name for name, _ in streams], ['eufy_front_door', 'eufy_front_door_ch1',
                                                       'eufy_front_door_ch2', 'eufy_ch3'])
        self.assertEqual(gen.assign_names(self.manifest(list(reversed(cameras))))[0], streams)

    def test_rename_and_offline_do_not_change_identity(self):
        streams, registry = gen.assign_names(self.manifest([{'channel': 0, 'name': 'Garage', 'status': 0}]))
        streams2, registry2 = gen.assign_names(self.manifest([{'channel': 0, 'name': 'New Name', 'status': 1}]), registry)
        self.assertEqual(streams[0][0], streams2[0][0])
        self.assertEqual(registry, registry2)

    def test_removed_channel_name_remains_reserved(self):
        _, registry = gen.assign_names(self.manifest([{'channel': 0, 'name': 'Garage'}]))
        streams, _ = gen.assign_names(self.manifest([{'channel': 1, 'name': 'Garage'}]), registry)
        self.assertEqual(streams[0][0], 'eufy_garage_ch1')

    def test_another_nvr_gets_own_registry(self):
        _, registry = gen.assign_names(self.manifest([{'channel': 0, 'name': 'Garage'}]))
        streams, _ = gen.assign_names({'nvr_sn': 'new-nvr', 'cameras': [{'channel': 0, 'name': 'New'}]}, registry)
        self.assertEqual(streams[0][0], 'eufy_new')

    def test_invalid_manifests(self):
        for manifest in [None, [], {}, {'cameras': None}, self.manifest([None]),
                         *[self.manifest([{'channel': ch}]) for ch in [None, True, -1, 256, '0', 'bad-data']],
                         self.manifest([{'channel': 0}, {'channel': 0}]),
                         self.manifest([{'channel': 0, 'name': []}]),
                         self.manifest([{'channel': 0, 'sn': 'a'}, {'channel': 1, 'sn': 'a'}])]:
            with self.subTest(manifest=manifest), self.assertRaises(ValueError):
                gen.assign_names(manifest)

    def test_corrupt_registry_does_not_silently_rename(self):
        for registry in [[], {}, {'version': 2, 'names': {}},
                         {'version': 1, 'nvr_sn': 'test-nvr', 'names': {'0': 'invalid name'}},
                         {'version': 1, 'nvr_sn': 'test-nvr', 'names': {'0': 'eufy_a', '1': 'eufy_a'}}]:
            with self.subTest(registry=registry), self.assertRaises(ValueError):
                gen.assign_names(self.manifest([{'channel': 0}]), registry)

    def test_offline_mapping_is_valid_yaml(self):
        for status in [0, '0', False]:
            streams, _ = gen.assign_names(self.manifest([{'channel': 0, 'status': status}]))
            data = yaml.safe_load(gen.render_config(streams, 1985, 8556, 8557))
            self.assertEqual(data['streams'], {})
        streams, _ = gen.assign_names(self.manifest([{'channel': 0, 'name': 'Front " door', 'status': 1}]))
        data = yaml.safe_load(gen.render_config(streams, 1985, 8556, 8557))
        self.assertEqual(len(data['streams']), 1)
        self.assertIn('eufy_stream.py 0 --rtsp {output}', next(iter(data['streams'].values())))

    def test_atomic_file_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.yaml'
            gen.atomic_write(path, 'old')
            gen.atomic_write(path, 'new')
            self.assertEqual(path.read_text(), 'new')
            self.assertEqual(len(list(Path(directory).iterdir())), 1)
            if os.name != 'nt':
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_cli_failure_keeps_previous_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, config = root / 'cameras.json', root / 'go2rtc.yaml'
            config.write_text('known-good')
            manifest.write_text('{broken private-json')
            env = {**os.environ, 'EUFY_CAMERAS': str(manifest), 'EUFY_GO2RTC_CONFIG': str(config)}
            result = subprocess.run([sys.executable, str(ROOT / 'bridge/gen_go2rtc.py')], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(config.read_text(), 'known-good')
            self.assertNotIn('private-json', result.stderr)
            self.assertNotIn('Traceback', result.stderr)

    def test_cli_invalid_ports_keep_previous_config(self):
        for port in ['0', '65536', 'bad', '8554']:
            with self.subTest(port=port), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                manifest, config = root / 'cameras.json', root / 'go2rtc.yaml'
                manifest.write_text(json.dumps(self.manifest([{'channel': 0}])))
                config.write_text('known-good')
                env = {**os.environ, 'EUFY_CAMERAS': str(manifest), 'EUFY_GO2RTC_CONFIG': str(config), 'GO2RTC_API_PORT': port, 'GO2RTC_RTSP_PORT': '8554', 'GO2RTC_WEBRTC_PORT': '8555'}
                result = subprocess.run([sys.executable, str(ROOT / 'bridge/gen_go2rtc.py')], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(config.read_text(), 'known-good')


class WatchdogTests(unittest.TestCase):
    def test_first_frame_and_stall_boundaries(self):
        now = [0]
        watchdog = runtime.MediaWatchdog(clock=lambda: now[0])
        now[0] = 44
        self.assertIsNone(watchdog.failure())
        now[0] = 45
        self.assertEqual(watchdog.failure(), 'first_frame_timeout')
        watchdog.received_frame()
        now[0] = 74
        self.assertIsNone(watchdog.failure())
        now[0] = 75
        self.assertEqual(watchdog.failure(), 'video_stalled')
        watchdog.received_frame()
        self.assertIsNone(watchdog.failure())

    def test_owner_account_error_requires_exact_acknowledgement(self):
        frame = bytearray(148)
        frame[:4] = b'XZYH'
        struct.pack_into('<H', frame, 4, 1350)
        struct.pack_into('<I', frame, 6, 132)
        frame[14] = 1
        struct.pack_into('<i', frame, 16, -104)
        self.assertEqual(runtime.command_error_status(frame), -104)
        for offset in (0, 4, 6, 14, 100):
            changed = bytearray(frame)
            changed[offset] ^= 1
            self.assertIsNone(runtime.command_error_status(changed))
        for data in (b'', b'XZYH', frame[:-1], frame + b'0'):
            self.assertIsNone(runtime.command_error_status(data))


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_exit_cancels_tasks_and_reaps_processes(self):
        with tempfile.TemporaryFile() as handle:
            async with runtime.SessionResources() as resources:
                process = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time;time.sleep(120)', stdin=asyncio.subprocess.PIPE)
                resources.processes.append(process)
                task = resources.create_task(asyncio.sleep(120))
                resources.files.append(handle)
                peer = MagicMock()
                peer.close = AsyncMock()
                resources.peer = peer
            self.assertTrue(task.cancelled())
            self.assertIsNotNone(process.returncode)
            self.assertTrue(handle.closed)
            peer.close.assert_awaited_once()

    async def test_exception_still_cleans_up(self):
        with self.assertRaisesRegex(RuntimeError, 'original error'):
            async with runtime.SessionResources() as resources:
                task = resources.create_task(asyncio.sleep(120))
                raise RuntimeError('original error')
        self.assertTrue(task.cancelled())

    async def test_cancellation_still_cleans_up(self):
        with self.assertRaises(asyncio.CancelledError):
            async with runtime.SessionResources() as resources:
                task = resources.create_task(asyncio.sleep(120))
                raise asyncio.CancelledError()
        self.assertTrue(task.cancelled())
