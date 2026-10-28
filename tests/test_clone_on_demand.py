"""code_* 指定的仓库不在代码目录时在后台按需 clone：不访问真实 GitLab，git、API 与线程均为替身。"""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import elk_core as core
import mcp_server
import repo_sync


def project(pid, path_with_namespace):
    return {'id': pid, 'path': path_with_namespace.rsplit('/', 1)[-1], 'path_with_namespace': path_with_namespace,
            'http_url': 'https://gitlab.invalid/%s.git' % path_with_namespace, 'ssh_url': '', 'default_branch': ''}


def with_url(projects):
    for p in projects:
        p['url'] = p['http_url']
    return projects


class ImmediateThread:
    """start() 时同步执行，测试里就能看到后台 clone 的结果。"""
    def __init__(self, target, args=(), **kwargs):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


class PendingThread(ImmediateThread):
    """start() 时什么都不做，模拟 clone 仍在进行。"""
    def start(self):
        pass


class CloneOnDemand(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.directory.name)
        self.scfg = {'dir': str(self.root), 'code': {'cache': str(self.root / 'cache')}, 'auth': {}}
        for target, value, kwargs in ((repo_sync.gitlab_auth, 'problem', {'return_value': None}),
                                      (repo_sync, 'check_login', {'return_value': None}),
                                      (repo_sync, 'choose_transport', {'side_effect': lambda scfg, ps: with_url(ps)}),
                                      (repo_sync, '_ON_DEMAND', {'new': {}})):
            patcher = patch.object(target, value, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.directory.cleanup)

    def clone(self, cwd, *args, **kwargs):
        if 'clone' in args:
            (pathlib.Path(args[-1]) / '.git').mkdir()
            return subprocess.CompletedProcess(args, 0, '', '')
        raise AssertionError(args)

    def test_unique_full_path_match_is_cloned_in_background(self):
        found = [project(7, 'ehomejava/check/sample'), project(8, 'ehomejava/check/sample-web')]
        with patch.object(repo_sync, 'list_projects', return_value=found) as listing, \
                patch.object(repo_sync.code, 'git', side_effect=self.clone), \
                patch.object(repo_sync.threading, 'Thread', ImmediateThread):
            message = repo_sync.clone_on_demand(self.scfg, 'ehomejava/check/sample')
        self.assertIn('已开始后台 clone', message)
        listing.assert_called_once_with(self.scfg, search='sample')
        self.assertTrue((self.root / 'sample' / '.git').is_dir())
        manifest = json.loads((self.root / repo_sync.MANIFEST).read_text(encoding='utf-8'))
        self.assertEqual(manifest['projects']['7']['path'], 'ehomejava/check/sample')
        self.assertEqual(repo_sync.on_demand_jobs(), {})

    def test_running_clone_is_not_started_twice(self):
        with patch.object(repo_sync, 'list_projects', return_value=[project(7, 'group/sample')]), \
                patch.object(repo_sync.threading, 'Thread', PendingThread):
            repo_sync.clone_on_demand(self.scfg, 'sample')
            with patch.object(repo_sync.threading, 'Thread', side_effect=AssertionError('started twice')):
                self.assertIn('正在后台 clone', repo_sync.clone_on_demand(self.scfg, 'group/sample'))
        self.assertTrue(repo_sync.on_demand_jobs()['group/sample']['running'])

    def test_failure_is_reported_once_then_retried(self):
        def fail(cwd, *args, **kwargs):
            return subprocess.CompletedProcess(args, 128, '', 'fatal: unable to access repository')

        with patch.object(repo_sync, 'list_projects', return_value=[project(7, 'group/sample')]), \
                patch.object(repo_sync.threading, 'Thread', ImmediateThread):
            with patch.object(repo_sync.code, 'git', side_effect=fail):
                repo_sync.clone_on_demand(self.scfg, 'sample')
            with self.assertRaisesRegex(core.ElkError, '上次后台 clone 失败：fatal: unable to access'):
                repo_sync.clone_on_demand(self.scfg, 'sample')
            with patch.object(repo_sync.code, 'git', side_effect=self.clone):
                self.assertIn('已开始后台 clone', repo_sync.clone_on_demand(self.scfg, 'sample'))
        self.assertTrue((self.root / 'sample' / '.git').is_dir())
        self.assertEqual(list(self.root.glob('.sample.cloning-*')), [])

    def test_same_name_in_two_groups_requires_full_path(self):
        found = [project(1, 'group-a/sample'), project(2, 'group-b/sample')]
        with patch.object(repo_sync, 'list_projects', return_value=found), \
                patch.object(repo_sync.threading, 'Thread', side_effect=AssertionError('must not clone')):
            with self.assertRaisesRegex(core.ElkError, 'group-a/sample, group-b/sample'):
                repo_sync.clone_on_demand(self.scfg, 'sample')

    def test_project_outside_sync_scope_is_not_cloned(self):
        with patch.object(repo_sync, 'list_projects', return_value=[project(3, 'group/sample-web')]), \
                patch.object(repo_sync.threading, 'Thread', side_effect=AssertionError('must not clone')):
            with self.assertRaisesRegex(core.ElkError, '同步范围内都没有仓库 sample'):
                repo_sync.clone_on_demand(self.scfg, 'sample')


class MissingRepoHook(unittest.TestCase):
    def test_existing_repo_skips_gitlab(self):
        with patch.object(mcp_server.code, 'repo_map', return_value={'sample': '/code/sample'}), \
                patch.object(mcp_server.repo_sync, 'load_sync_config', side_effect=AssertionError('no lookup')):
            a = {'repo': 'sample'}
            self.assertIs(mcp_server._clone_missing_repo({}, a), a)

    def test_missing_repo_without_sync_config_keeps_original_error_path(self):
        with patch.object(mcp_server.code, 'repo_map', return_value={}), \
                patch.object(mcp_server.repo_sync, 'load_sync_config', return_value=None):
            self.assertEqual(mcp_server._clone_missing_repo({}, {'repo': 'sample'}), {'repo': 'sample'})

    def test_missing_repo_returns_retry_later_without_waiting(self):
        with patch.object(mcp_server.code, 'repo_map', return_value={}), \
                patch.object(mcp_server.repo_sync, 'load_sync_config', return_value={'dir': '/code'}), \
                patch.object(mcp_server.repo_sync, 'clone_on_demand', return_value='已开始后台 clone') as clone:
            with self.assertRaisesRegex(core.ElkError, '已开始后台 clone'):
                mcp_server._clone_missing_repo({}, {'repo': 'group/sample', 'env': 'test'})
        clone.assert_called_once_with({'dir': '/code'}, 'group/sample')


if __name__ == '__main__':
    unittest.main()
