"""Synthetic eight-Skill bundle, never a substitute for a user-supplied package."""
import zipfile


def write_eight_skills(path):
    bodies = {}
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index in range(8):
            name = f"missing-skill-{index}"
            body = f"\r\n# 正文 {index}\r\n\r\n  保留缩进、中文及尾部空行。\r\n```text\r\n  exact {index}\r\n```\r\n\r\n"
            text = f"---\r\nname: {name}\r\ndescription: >\r\n  Fixture description {index}\r\n  continued\r\n---\r\n" + body
            archive.writestr(f"Paveway-missing-skills/{name}/SKILL.md", text.encode())
            bodies[name] = body
    return bodies
