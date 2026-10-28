"""定时把已有分支 worktree 推进到远程最新并增量更新索引：git、gitnexus 均为替身。"""
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import code_core as code
import elk_core as core
import repo_sync


class WorktreeSource(unittest.TestCase):
    def test_recorded_source_keeps_escaped_branch_name(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = {'cache': directory}
            name = 'sample@feature-x-1a2b3c'
            with open(code._source_file(cfg, name), 'w', encoding='utf-8') as f:
                json.dump({'repo_path': '/code/sample', 'branch': 'feature/x'}, f)
            self.assertEqual(code.worktree_source(cfg, name, '/unused'), ('/code/sample', 'feature/x'))

    def test_commit_worktree_is_never_refreshed(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(code.worktree_source({'cache': directory}, 'sample@0123456789', '/unused'))

    def test_unknown_worktree_without_git_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(code.worktree_source({'cache': directory}, 'sample@beta', directory))


class RefreshWorktrees(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cfg = {'cache': self.directory.name}
        base = pathlib.Path(code._cache_dir(self.cfg, 'worktrees'))
        for entry in ('fresh@beta', 'stale@beta', 'busy@beta', 'gone@beta', 'stale@beta.stale-1', 'nogit@beta'):
            (base / entry).mkdir()
            if entry != 'nogit@beta':
                (base / entry / '.git').write_text('gitdir: x')
        self.states = {'fresh@beta': 'fresh', 'stale@beta': 'stale', 'busy@beta': 'building'}

    def ensure(self, cfg, info, ctx, **kwargs):
        self.assertEqual(kwargs, {'want_index': False, 'touch': False})
        name = info['repo'] + '@beta'
        return '/wt/' + name, name, {'state': self.states[name]}, [], 'sha-' + name

    def commit(self, repo_path, rev):
        if repo_path.endswith('gone'):
            raise core.ElkError('远程分支 origin/beta 不存在')
        return {'sha': 'sha'}

    def test_only_outdated_branch_worktrees_are_reindexed(self):
        with patch.object(code, 'gitnexus', return_value={}), \
                patch.object(code, 'worktree_source', side_effect=lambda cfg, n, wt: ('/code/' + n.split('@')[0], 'beta')), \
                patch.object(code, 'fetch', return_value=''), \
                patch.object(code, 'commit_info', side_effect=self.commit), \
                patch.object(code, 'repo_name', side_effect=lambda cfg, p: p.rsplit('/', 1)[-1]), \
                patch.object(code, 'ensure_worktree', side_effect=self.ensure), \
                patch.object(repo_sync, '_run_index', return_value=None) as run_index:
            report = repo_sync.refresh_worktrees(self.cfg)
        run_index.assert_called_once_with(self.cfg, '/wt/stale@beta', 'stale@beta', 'sha-stale@beta', 3600)
        self.assertEqual(report['indexed'], ['stale@beta'])
        self.assertEqual(report['fresh'], 1)
        self.assertEqual(report['skipped'], ['busy@beta：索引构建中'])
        self.assertEqual([f['repo'] for f in report['failed']], ['gone@beta'])

    def test_missing_gitnexus_skips_everything(self):
        with patch.object(code, 'gitnexus', return_value={'error': '未找到 gitnexus'}), \
                patch.object(code, 'ensure_worktree', side_effect=AssertionError('must not touch worktrees')):
            self.assertEqual(repo_sync.refresh_worktrees(self.cfg)['skipped'], ['全部：未找到 gitnexus'])


if __name__ == '__main__':
    unittest.main()
