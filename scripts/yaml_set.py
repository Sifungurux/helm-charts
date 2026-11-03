#!/usr/bin/env python3
"""Set one scalar in a YAML file in place, touching only that line.

    yaml_set.py FILE .dotted.path VALUE

Keeps comments, blank lines and formatting (yq -i rewrites the whole file).
Handles plain block mappings, which is all values.yaml and Chart.yaml use for
the pinned keys. Fails if the path is missing, ambiguous, or not a scalar line.
"""
import json
import re
import sys

KEY = re.compile(r"^( *)([^\s#:'\"-][^:#]*?):(?:\s|$)")


def main():
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    path, dotted, value = sys.argv[1:]
    want = dotted.lstrip(".").split(".")
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()

    stack, hits = [], []
    for i, line in enumerate(lines):
        m = KEY.match(line)
        if not m:
            continue
        indent, key = len(m.group(1)), m.group(2).strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, key))
        if [k for _, k in stack] == want:
            hits.append(i)
    if len(hits) != 1:
        sys.exit(f"{path}: {dotted} found {len(hits)} times")

    i = hits[0]
    m = re.match(r"^(\s*[^:#]+:[ \t]*)(\"[^\"]*\"|'[^']*'|[^\s#]*)(.*?)(\r?\n?)$", lines[i])
    if not m or m.group(2) in ("|", ">", "|-", ">-"):
        sys.exit(f"{path}:{i + 1}: {dotted} is not a one-line scalar")
    lines[i] = m.group(1) + json.dumps(value) + m.group(3) + m.group(4)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


if __name__ == "__main__":
    main()
