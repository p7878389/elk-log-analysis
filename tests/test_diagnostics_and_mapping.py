import pathlib
import stat
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

    def test_failed_clone_removes_read_only_staging_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            cfg = {'dir': str(root), 'code': {'cache': str(root / 'cache')}}
            project = {'dir': 'sample', 'id': 1, 'url': 'https://gitlab.invalid/group/sample.git',
                       'default_branch': '', 'path_with_namespace': 'group/sample'}

            def git(cwd, *args, **kwargs):
                if 'clone' in args:
                    pack = pathlib.Path(args[-1]) / '.git' / 'objects' / 'pack'
                    pack.mkdir(parents=True)
                    (pack / 'pack-1.pack').write_text('x')
                    # Windows 上 git 的 pack 文件只读；这里同时把目录设为只读，让非 Windows 平台上的普通删除也失败
                    (pack / 'pack-1.pack').chmod(stat.S_IREAD)
                    pack.chmod(stat.S_IREAD | stat.S_IEXEC)
                    return subprocess.CompletedProcess(args, 128, '', 'fatal: unable to checkout working tree')
                raise AssertionError(args)

            try:
                with patch.object(repo_sync.code, 'git', side_effect=git):
                    action, err = repo_sync._sync_one(cfg, project, {'projects': {}})
                self.assertIsNone(action)
                self.assertIn('unable to checkout working tree', err)
                self.assertEqual([p.name for p in root.iterdir() if '.cloning' in p.name], [])
            finally:
                for pack in root.glob('.sample.cloning-*/.git/objects/pack'):
                    pack.chmod(stat.S_IRWXU)  # 修复前的残留也要能被 TemporaryDirectory 清理


if __name__ == '__main__':
    unittest.main()
