"""Synthetic eight-Skill bundle, never a substitute for a user-supplied package."""
import zipfile


def skill_files(index, name=None):
    """Complete native Skill tree for one fixture Skill."""
    name = name or f"missing-skill-{index}"
    skill = (f"---\r\nname: {name}\r\ndescription: >\r\n  Fixture description {index}\r\n  continued\r\n---\r\n"
             f"\r\n# 正文 {index}\r\n\r\n  保留缩进、中文及尾部空行。\r\n```text\r\n  exact {index}\r\n```\r\n\r\n")
    return {"SKILL.md": skill.encode(),
            "references/a.md": f"# 参考资料 {index}\r\n\r\n- 逐字节保留\r\n".encode(),
            "references/nested/b.md": f"# 嵌套 {index}\r\n".encode(),
            "scripts/check.py": f"raise RuntimeError('NEVER_EXECUTE_{index}')\n".encode()}


def write_eight_skills(path):
    trees = {}
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index in range(8):
            name = f"missing-skill-{index}"
            trees[name] = skill_files(index, name)
            for relative, payload in trees[name].items():
                archive.writestr(f"Paveway-missing-skills/{name}/{relative}", payload)
    return trees
