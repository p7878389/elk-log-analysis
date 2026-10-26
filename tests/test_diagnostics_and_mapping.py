import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import diag
import elk_core as core
import repo_sync


class DiagnosticsAndMapping(unittest.TestCase):
    def test_worktree_registration_read_without_git_process(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = pathlib.Path(directory)
            admin = repo / '.git' / 'worktrees' / 'cached'
            admin.mkdir(parents=True)
            target = repo / 'missing worktree' / '.git'
            (admin / 'gitdir').write_text(str(target) + '\n', encoding='utf-8')
            with patch.object(diag.code, 'git', side_effect=AssertionError('must not spawn git')):
                self.assertEqual(diag._registered_worktrees(str(repo)), [str(target.parent)])

    def test_pod_service_matches_exact_or_delimited_prefix(self):
        query = core.build_query({'_fields': dict(core.DEFAULT_FIELDS, service='fields.pod_name.keyword')},
                                 {'service': 'mall-manage'}, core.parse_tz('+08:00'))
        clause = query['bool']['filter'][1]['bool']['should'][0]
        self.assertEqual(clause['bool']['should'], [
            {'term': {'fields.pod_name.keyword': 'mall-manage'}},
            {'prefix': {'fields.pod_name.keyword': 'mall-manage-'}}])

    def test_application_field_keeps_phrase_semantics(self):
        query = core.build_query({'_fields': core.DEFAULT_FIELDS}, {'service': 'mall-manage'}, core.parse_tz('+08:00'))
        self.assertEqual(query['bool']['filter'][1]['bool']['should'],
                         [{'match_phrase': {'app_name': 'mall-manage'}}])

    def test_abandoned_staging_directory_does_not_block_clone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            old = root / '.sample.cloning'
            old.mkdir()
            (old / 'residue').write_text('keep')
            cfg = {'dir': str(root), 'code': {'cache': str(root / 'cache')}}
            project = {'dir': 'sample', 'id': 1, 'url': 'https://gitlab.invalid/group/sample.git',
                       'default_branch': '', 'path_with_namespace': 'group/sample'}

            def git(cwd, *args, **kwargs):
                if 'clone' in args:
                    stage = pathlib.Path(args[-1])
                    self.assertNotEqual(stage, old)
                    self.assertEqual(list(stage.iterdir()), [])
                    (stage / '.git').mkdir()
                    return subprocess.CompletedProcess(args, 0, '', '')
                raise AssertionError(args)

            with patch.object(repo_sync.code, 'git', side_effect=git):
                self.assertEqual(repo_sync._sync_one(cfg, project, {'projects': {}}), ('cloned', None))
            self.assertTrue((root / 'sample' / '.git').is_dir())
            self.assertEqual((old / 'residue').read_text(), 'keep')


if __name__ == '__main__':
    unittest.main()
