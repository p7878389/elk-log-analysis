"""envs.json 中的凭据：允许明文，也继续支持 env: / keychain: 引用。"""
import base64
import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import elk_core as core


def basic(user, pwd):
    return {"Authorization": "Basic " + base64.b64encode(("%s:%s" % (user, pwd)).encode()).decode()}


class EnvsJsonCredentials(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "envs.json")
        # 只走 envs.json：环境变量与 cc-switch 都不提供配置
        clean = {k: v for k, v in os.environ.items() if not k.startswith("ELK_")}
        for p in (patch.dict(os.environ, clean, clear=True), patch.object(core, "CONFIG_PATH", self.path),
                  patch.object(core, "config_from_ccswitch", return_value=None)):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.dir.cleanup()

    def write(self, data, mode=0o600):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.chmod(self.path, mode)

    def load(self, name="test"):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cfg = core.load_config()
        return core.get_env(cfg, name), cfg, err.getvalue()

    def test_array_plaintext_password(self):
        self.write({"environments": [{"env": "test", "url": "http://kibana:5601", "mode": "kibana",
                                      "username": "reader", "password": "p@ss:word", "default": True}]})
        env, cfg, _ = self.load()
        self.assertEqual(core.auth_header(env, interactive=False), basic("reader", "p@ss:word"))
        self.assertIn("auth=basic(已配置)", core.describe_envs(cfg))
        self.assertNotIn("p@ss:word", core.describe_envs(cfg, verbose=True))

    def test_array_plaintext_api_key_in_defaults(self):
        self.write({"defaults": {"api_key": "k-123"},
                    "environments": [{"env": "test", "url": "http://es:9200"}]})
        env, _, _ = self.load()
        self.assertEqual(core.auth_header(env, interactive=False), {"Authorization": "ApiKey k-123"})

    def test_array_env_reference_still_works(self):
        self.write({"environments": [{"env": "test", "url": "http://kibana:5601", "username": "reader",
                                      "password": "env:ELK_T_PWD"}]})
        env, _, _ = self.load()
        with patch.dict(os.environ, {"ELK_T_PWD": "from-env"}):
            self.assertEqual(core.auth_header(env, interactive=False), basic("reader", "from-env"))

    def test_legacy_dict_plaintext_and_inline(self):
        self.write({"default_env": "test", "environments": {
            "test": {"es_url": "http://es:9200", "auth": {"type": "basic", "username": "u", "password": "plain"}},
            "stag": {"es_url": "http://es:9200",
                     "auth": {"type": "basic", "username": "u", "password": {"type": "inline", "value": "iv"}}},
        }})
        env, cfg, _ = self.load("test")
        self.assertEqual(core.auth_header(env, interactive=False), basic("u", "plain"))
        self.assertEqual(core.auth_header(core.get_env(cfg, "stag"), interactive=False), basic("u", "iv"))

    @unittest.skipIf(os.name == "nt", "Windows 不检查文件权限位")
    def test_loose_permission_still_warns(self):
        self.write({"environments": [{"env": "test", "url": "http://kibana:5601", "password": "x"}]}, mode=0o644)
        _, _, err = self.load()
        self.assertIn("建议 chmod 600", err)


if __name__ == "__main__":
    unittest.main()
