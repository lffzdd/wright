import ast
import os
import sys

root = "src/wright"
graph = {}
for dirpath, dirs, files in os.walk(root):
    dirs[:] = [d for d in dirs if d not in ("__pycache__", "tests")]
    for f in files:
        if not f.endswith(".py"):
            continue
        mod = os.path.relpath(os.path.join(dirpath, f), root)[:-3].replace(os.sep, ".")
        if mod.endswith(".__init__"):
            mod = mod[:-9]
        pkg = mod.rsplit(".", 1)[0] if "." in mod else ""
        deps = set()
        tree = ast.parse(open(os.path.join(dirpath, f), encoding="utf-8").read())
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.level:
                base = pkg
                for _ in range(n.level - 1):
                    base = base.rsplit(".", 1)[0] if "." in base else ""
                deps.add((f"{base}.{n.module}" if n.module else base).lstrip("."))
        graph[mod] = deps
color, stack, cycles = {}, [], []


def dfs(u):
    color[u] = 1
    stack.append(u)
    for v in sorted(graph.get(u, ())):
        if v not in graph:
            continue
        if color.get(v) == 1:
            cycles.append(stack[stack.index(v) :] + [v])
        elif color.get(v, 0) == 0:
            dfs(v)
    stack.pop()
    color[u] = 2


for m in sorted(graph):
    if color.get(m, 0) == 0:
        dfs(m)
print("MODULE-LEVEL CYCLES:", len(cycles))
for c in cycles:
    print("  ", " -> ".join(c))
sys.exit(1 if cycles else 0)
