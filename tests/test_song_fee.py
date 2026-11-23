"""Offline regression tests: python -m unittest discover -s tests -v."""
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


SOURCE = Path(__file__).resolve().parents[1] / 'src' / 'cloud_music_mcp'
MISSING = object()
FEES = (0, 1, 4, 8, 99, None, MISSING)


def load_modules():
    # Isolate optional dependencies and import-time server/logging setup.
    package = types.ModuleType('cloud_music_mcp')
    package.__path__ = [str(SOURCE)]
    pyncm = types.ModuleType('pyncm')
    pyncm.apis = Mock()
    auth = types.ModuleType('cloud_music_mcp.auth')
    auth.load_session = Mock(return_value=(True, {}))
    auth.check_login_status = Mock()
    auth.login_via_qrcode = Mock()
    log = types.ModuleType('cloud_music_mcp.log')
    log.setup_logging = Mock(return_value=Mock())
    fastmcp = types.ModuleType('fastmcp')
    server = Mock()
    server.tool.side_effect = lambda **kwargs: lambda fn: fn
    fastmcp.FastMCP = Mock(return_value=server)
    modules = {'cloud_music_mcp': package, 'pyncm': pyncm,
               'auth': auth, 'cloud_music_mcp.auth': auth,
               'cloud_music_mcp.log': log, 'fastmcp': fastmcp}
    stderr, path, env = sys.stderr, sys.path[:], os.environ.copy()
    levels = {name: logging.getLogger(name).level for name in
              ('fastmcp', 'mcp', 'uvicorn', 'uvicorn.access',
               'pyncm', 'pyncm.api', 'pyncm.helper')}
    try:
        with patch.dict(sys.modules, modules):
            loaded = []
            for name in ('api', 'main'):
                spec = importlib.util.spec_from_file_location(
                    'cloud_music_mcp.' + name, SOURCE / (name + '.py'))
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
                loaded.append(module)
            return (*loaded, pyncm.apis, auth)
    finally:
        if sys.stderr is not stderr:
            sys.stderr.close()
        sys.stderr = stderr
        sys.path[:] = path
        os.environ.clear()
        os.environ.update(env)
        for name, level in levels.items():
            logging.getLogger(name).setLevel(level)


API, MAIN, APIS, AUTH = load_modules()


def song(fee=MISSING):
    value = {'id': 4238109, 'name': 'Time',
             'artist': 'Pink Floyd', 'ar': [{'name': 'Pink Floyd'}],
             'artists': [{'name': 'Pink Floyd'}],
             'al': {'name': 'The Dark Side of the Moon'},
             'album': {'name': 'The Dark Side of the Moon'}}
    if fee is not MISSING:
        value['fee'] = fee
    return value


class SongFeeTests(unittest.TestCase):
    def setUp(self):
        APIS.reset_mock(return_value=True, side_effect=True)
        AUTH.load_session.reset_mock()
        AUTH.load_session.return_value = (True, {})

    def invoke_api(self, source, item):
        if source == 'search':
            APIS.cloudsearch.GetSearchResult.return_value = {
                'code': 200, 'result': {'songs': [item]}}
            result = API.search('Pink Floyd Time')
            APIS.cloudsearch.GetSearchResult.assert_called_once_with(
                'Pink Floyd Time', stype=1, limit=5)
            self.assertEqual(len(APIS.mock_calls), 1)
            return result['items']
        if source == 'daily':
            def decorate(fn):
                def request():
                    endpoint, _ = fn()
                    self.assertEqual(endpoint, '/weapi/v1/discovery/recommend/songs')
                    return {'code': 200, 'recommend': [item]}
                return request
            APIS.WeapiCryptoRequest.side_effect = decorate
            result = API.get_daily_recommendations()
            APIS.WeapiCryptoRequest.assert_called_once()
            self.assertEqual(len(APIS.mock_calls), 1)
        elif source == 'playlist':
            APIS.playlist.GetPlaylistInfo.return_value = {
                'playlist': {'name': 'Test', 'tracks': [item], 'trackCount': 1}}
            result = API.get_playlist_detail(123)
            APIS.playlist.GetPlaylistInfo.assert_called_once_with(123)
            self.assertEqual(len(APIS.mock_calls), 1)
        elif source == 'album':
            APIS.album.GetAlbumInfo.return_value = {'album': {}, 'songs': [item]}
            result = API.get_album_info(123)
            APIS.album.GetAlbumInfo.assert_called_once_with(123)
            self.assertEqual(len(APIS.mock_calls), 1)
        else:
            APIS.artist.GetArtistDetails.return_value = {'data': {'artist': {}}}
            APIS.artist.GetArtistTracks.return_value = {'songs': [item]}
            result = API.get_artist_info(123)
            APIS.artist.GetArtistDetails.assert_called_once_with(123)
            APIS.artist.GetArtistTracks.assert_called_once_with(123, limit=10)
            self.assertEqual(len(APIS.mock_calls), 2)
        self.assertTrue(result['success'])
        return result['songs']

    def test_raw_fee_survives_all_song_api_paths_without_extra_calls(self):
        for source in ('search', 'daily', 'playlist', 'album', 'artist'):
            for fee in FEES:
                with self.subTest(source=source, fee=fee):
                    self.setUp()
                    item = song(fee)
                    output = self.invoke_api(source, item)[0]
                    self.assertIn('fee', output)
                    self.assertEqual(output['fee'], None if fee is MISSING else fee)
                    self.assertEqual(output['id'], item['id'])
                    self.assertEqual(output['name'], item['name'])

    def test_mcp_search_preserves_fee(self):
        for fee in FEES:
            item = {'id': 1, 'name': 'Song', 'fee': None if fee is MISSING else fee}
            with self.subTest(fee=fee), patch.object(
                    MAIN, 'search', return_value={'success': True, 'items': [item]}) as search:
                self.assertEqual(MAIN.cloud_music_search('song'), [item])
                search.assert_called_once_with('song', category='song')

    def test_mcp_text_outputs_expose_fee_including_unknown(self):
        paths = (
            ('get_daily_recommendations', 'cloud_music_get_daily_recommend', ()),
            ('get_playlist_detail', 'cloud_music_playlist_detail', (123,)),
            ('get_album_info', 'cloud_music_album_info', (123,)),
            ('get_artist_info', 'cloud_music_artist_info', (123,)),
        )
        for api, tool, args in paths:
            for fee in FEES:
                value = {'success': True, 'name': 'List', 'count': 1, 'songs': [song(fee)],
                         'album': {'name': 'Album', 'artist': 'Artist', 'publish_date': '', 'size': 1},
                         'artist': {'name': 'Artist', 'id': 1, 'album_count': 1,
                                    'song_count': 1, 'description': ''}}
                with self.subTest(tool=tool, fee=fee), patch.object(MAIN, api, return_value=value):
                    output = getattr(MAIN, tool)(*args)
                    expected = json.dumps(None if fee is MISSING else fee)
                    self.assertIn('fee=' + expected, output)
                    self.assertIn('Time', output)
                    self.assertIn('4238109', output)

    def test_non_song_search_shapes_are_unchanged(self):
        cases = (('album', 'albums', {'id': 1, 'name': 'A', 'artist': {'name': 'B'}},
                  {'id': 1, 'name': 'A', 'artist': 'B'}, 10),
                 ('artist', 'artists', {'id': 2, 'name': 'B'}, {'id': 2, 'name': 'B'}, 100),
                 ('playlist', 'playlists', {'id': 3, 'name': 'C', 'trackCount': 4},
                  {'id': 3, 'name': 'C', 'count': 4}, 1000))
        for category, key, upstream, expected, stype in cases:
            with self.subTest(category=category):
                self.setUp()
                APIS.cloudsearch.GetSearchResult.return_value = {'code': 200, 'result': {key: [upstream]}}
                self.assertEqual(API.search('test', category)['items'], [expected])
                APIS.cloudsearch.GetSearchResult.assert_called_once_with('test', stype=stype, limit=5)

    def test_playlist_summaries_do_not_fetch_tracks_or_claim_song_fee(self):
        APIS.login.GetCurrentLoginStatus.return_value = {'profile': {'userId': 1}}
        APIS.user.GetUserPlaylists.return_value = {'code': 200, 'playlist': [
            {'id': 2, 'name': 'List', 'trackCount': 3, 'creator': {'nickname': 'N', 'userId': 1}}]}
        result = API.get_user_playlists()
        self.assertEqual(result['playlists'], [
            {'id': 2, 'name': 'List', 'count': 3, 'creator': 'N', 'is_mine': True}])
        self.assertEqual(len(APIS.mock_calls), 2)
        with patch.object(MAIN, 'get_user_playlists', return_value=result):
            self.assertNotIn('fee', MAIN.cloud_music_my_playlists())

    def test_login_failures_do_not_call_song_endpoints(self):
        AUTH.load_session.return_value = (False, {})
        for name in ('get_daily_recommendations', 'get_playlist_detail', 'get_album_info', 'get_artist_info'):
            args = () if name == 'get_daily_recommendations' else (1,)
            self.assertFalse(getattr(API, name)(*args)['success'])
        self.assertEqual(APIS.mock_calls, [])

    def test_search_errors_and_empty_results_are_preserved(self):
        for result in ({'code': 500}, {'code': 200, 'result': {}}):
            APIS.cloudsearch.GetSearchResult.return_value = result
            self.assertFalse(API.search('none')['success'])
        APIS.cloudsearch.GetSearchResult.side_effect = RuntimeError('offline')
        self.assertEqual(API.search('none'), {'success': False, 'error': 'offline'})

    def test_tool_failure_messages_are_preserved(self):
        for api, tool, args in (('search', 'cloud_music_search', ('x',)),
                               ('get_daily_recommendations', 'cloud_music_get_daily_recommend', ()),
                               ('get_playlist_detail', 'cloud_music_playlist_detail', (1,))):
            with patch.object(MAIN, api, return_value={'success': False, 'error': 'offline'}):
                self.assertIn('offline', getattr(MAIN, tool)(*args))

    def test_playback_messages_do_not_claim_verified_playback(self):
        with patch.object(MAIN.sys, 'platform', 'darwin'), patch.object(MAIN.subprocess, 'run') as run:
            run.return_value.returncode = 0
            self.assertIn('未确认实际播放', MAIN.cloud_music_play('1'))
            run.side_effect = [types.SimpleNamespace(returncode=1), types.SimpleNamespace(returncode=0)]
            output = MAIN.cloud_music_play('1')
            self.assertIn('未确认实际播放', output)
            self.assertNotIn('已在浏览器中播放', output)


if __name__ == '__main__':
    unittest.main()
