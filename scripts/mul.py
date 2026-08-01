import os
import re

EXCLUDE_FILES = {"README.md", "直接目录.txt", "树形目录.txt", "mul.py"}


def natural_sort_key(value):
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r"(\d+)", value)]


def should_exclude(name):
    return name.startswith(".") or name in EXCLUDE_FILES


def build_tree_lines(current_dir, prefix=""):
    try:
        names = os.listdir(current_dir)
    except PermissionError:
        return []
    names = sorted((name for name in names if not should_exclude(name)), key=natural_sort_key)
    lines = []
    for index, name in enumerate(names):
        path = os.path.join(current_dir, name)
        is_last = index == len(names) - 1
        lines.append(f"{prefix}{'└── ' if is_last else '├── '}{name}")
        if os.path.isdir(path):
            lines.extend(build_tree_lines(path, prefix + ("    " if is_last else "│   ")))
    return lines


def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    root_name = os.path.basename(base_dir) or "Root"
    tree_lines = [root_name] + build_tree_lines(base_dir)
    flat_lines = []
    for root, dirs, files in os.walk(base_dir):
        dirs[:] = sorted((name for name in dirs if not should_exclude(name)), key=natural_sort_key)
        for name in sorted((name for name in files if not should_exclude(name)), key=natural_sort_key):
            relative = os.path.relpath(os.path.join(root, name), base_dir).replace("\\", "/")
            flat_lines.append(f"./{relative}")
    with open(os.path.join(base_dir, "树形目录.txt"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(tree_lines) + "\n")
    with open(os.path.join(base_dir, "直接目录.txt"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(flat_lines) + "\n")


if __name__ == "__main__":
    main()
