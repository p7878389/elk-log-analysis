#!/usr/bin/env python3
"""生成团队版 skill 压缩包：成员解压到 skills 目录（或在 claude.ai 上传）即可使用，不需要 clone 整个仓库。

团队版只面向远程（团队共享）elk MCP：不含本机脚本、cc-switch/钥匙串配置，SKILL.md 里写好了接入两个 MCP 的步骤。
源文件命名为 SKILL.team.md，避免本仓库作为 skill 目录时被递归扫描成第二个同名 skill；打包时改名为 SKILL.md。
本仓库公开，源文件中服务地址为占位符，打包时用 --url 填入，生成的 zip 只在团队内部分发。

用法：python3 team-skill/build.py --url http://<服务地址>:8765/mcp [-o elk-log-analysis-team.zip]
"""
import argparse
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from install import gitnexus_url  # noqa: E402  与安装脚本保持同一套地址推导

SKILL = "elk-log-analysis"
FILES = {"SKILL.team.md": "SKILL.md", "references/query-cookbook.md": "references/query-cookbook.md"}


def build(url, out):
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for src, dst in FILES.items():
            with open(os.path.join(HERE, src), encoding="utf-8") as f:
                text = f.read().replace("{{ELK_MCP_URL}}", url).replace("{{GITNEXUS_MCP_URL}}", gitnexus_url(url))
            if "{{" in text:
                raise SystemExit("%s 中仍有未替换的占位符" % src)
            z.writestr("%s/%s" % (SKILL, dst), text)
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--url", required=True, help="elk MCP 地址，如 http://elk-mcp.example.com:8765/mcp")
    p.add_argument("-o", "--out", default="elk-log-analysis-team.zip", help="输出 zip 路径")
    a = p.parse_args()
    print("已生成 %s（elk: %s，gitnexus-remote: %s）" % (build(a.url, a.out), a.url, gitnexus_url(a.url)))


if __name__ == "__main__":
    main()
