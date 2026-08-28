"""
提示词构造

从 Prompt/ 和 Skills/ 目录读文本文件，组装 system prompt。
所有需要拼提示词的地方都从这里走，方便统一管理。
"""
import os
import re

# 项目根目录（本文件在 core/ 下，所以往上一级）
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_txt(rel_path: str) -> str:
    """读项目根目录下的文本文件（相对路径，如 'Prompt/system.md'）。"""
    with open(os.path.join(_BASE_DIR, rel_path), "r", encoding="utf-8") as f:
        return f.read()


def _skill_description(path: str) -> str:
    """从 skill 的 frontmatter description 或第一个 Markdown 标题提取简短说明。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            header = f.read(4000)
    except OSError:
        return ""

    match = re.search(r"^description:\s*(.+)$", header, re.MULTILINE)
    if match:
        return match.group(1).strip().strip('"')
    match = re.search(r"^#\s+(.+)$", header, re.MULTILINE)
    return match.group(1).strip() if match else ""


def build_skill_catalog() -> str:
    """
    扫描 Skills/ 目录，生成本次请求真实可读取的 skill 列表。

    这样新增、重命名或删除 .md 技能文件后，无需手动维护清单；
    下一次 AI 请求会自动看到最新路径。skills.md 本身是清单说明，不作为技能列出。
    """
    skills_dir = os.path.join(_BASE_DIR, "Skills")
    entries = []
    for root, dirs, files in os.walk(skills_dir):
        dirs.sort()
        for filename in sorted(files):
            if not filename.endswith(".md"):
                continue
            full_path = os.path.join(root, filename)
            rel_path = os.path.relpath(full_path, _BASE_DIR).replace(os.sep, "/")
            if rel_path == "Skills/skills.md":
                continue
            description = _skill_description(full_path)
            suffix = f"：{description}" if description else ""
            entries.append(f'- `read_file(path="{rel_path}")`{suffix}')
    return "\n".join(entries) if entries else "（当前没有可用的技能文件）"


def build_system_prompt() -> str:
    """
    拼接 system prompt。
    结构：system.md + skill 使用说明 + 动态扫描得到的可用技能路径。
    """
    return (
        read_txt("Prompt/system.md")
        + "\n\n# 可用技能\n"
        + read_txt("Skills/skills.md")
        + "\n\n## 当前可用技能文件\n"
        + build_skill_catalog()
    )
