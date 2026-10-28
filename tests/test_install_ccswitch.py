"""CC Switch owns client writes; installer only prepares private imports."""
import argparse
import base64
import importlib.util
import json
import pathlib
import tempfile
import unittest
import urllib.parse
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('elk_installer', pathlib.Path(__file__).resolve().parents[1] / 'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class CCSwitchInstall(unittest.TestCase):
    def test_public_protocol_roundtrip(self):
        entry = {'type': 'http', 'url': 'http://127.0.0.1:8765/mcp',
                 'headers': {'Authorization': 'Bearer test+token&value'}}
        mcp, skill = installer.ccswitch_links([('elk', entry)], ['claude', 'codex'])
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(mcp).query)
        decoded = json.loads(base64.b64decode(query['config'][0]))
        self.assertEqual(decoded, {'mcpServers': {'elk': entry}})
        self.assertEqual(query['apps'], ['claude,codex'])
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(skill).query)['repo'], ['p7878389/elk-log-analysis'])

    def args(self, output):
        return argparse.Namespace(mode='remote', link=False, name='elk', gitnexus_name='gitnexus-remote',
                                  url='http://127.0.0.1:8765/mcp', token='test-only', token_store='config',
                                  no_gitnexus=False, ccswitch_output=str(output),
                                  skill_repo='p7878389/elk-log-analysis', skill_branch='main')

    def test_default_flow_never_writes_client_config_or_skills(self):
        with tempfile.TemporaryDirectory() as directory:
            out = pathlib.Path(directory) / 'import.html'
            with patch.object(installer, 'probe_gitnexus', return_value=True), \
                    patch.object(installer, 'private'), \
                    patch.object(installer, 'install_skill', side_effect=AssertionError('client skill write')), \
                    patch.object(installer, 'update_claude', side_effect=AssertionError('Claude config write')), \
                    patch.object(installer, 'update_codex', side_effect=AssertionError('Codex config write')):
                self.assertEqual(installer.prepare_ccswitch(self.args(out), ['claude', 'codex']), str(out))
            page = out.read_text(encoding='utf-8')
            self.assertIn('ccswitch://v1/import', page)
            self.assertIn('resource=mcp', page)
            self.assertIn('resource=skill', page)

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            out = pathlib.Path(directory) / 'absent' / 'import.html'
            with patch.object(installer, 'DRY', True), patch.object(installer, 'probe_gitnexus', return_value=False):
                installer.prepare_ccswitch(self.args(out), ['claude'])
            self.assertFalse(out.parent.exists())

    def test_main_defaults_to_ccswitch(self):
        with patch('sys.argv', ['install.py', '--mode', 'remote', '--url', 'http://localhost:8765/mcp',
                                '--token', 'test-only', '--clients', 'claude,codex', '--skip-check']), \
                patch.object(installer, 'prepare_ccswitch') as prepare, \
                patch.object(installer, 'install_skill', side_effect=AssertionError('direct installation')):
            installer.main()
            self.assertEqual(prepare.call_args.args[0].manager, 'cc-switch')


if __name__ == '__main__':
    unittest.main()
