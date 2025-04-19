#!/usr/bin/env python3
"""
Generate a code knowledge graph for a Java codebase and output hierarchical JSON.
Nodes: package, file, class, method, field.
Edges: contains, import, extends, implements, calls.
Usage: python3 graph_generator.py /path/to/src [output.json]
"""
import os
import sys
import json
import javalang

def parse_java_file(filepath):
    try:
        content = open(filepath, 'r', encoding='utf-8').read()
        tree = javalang.parse.parse(content)
    except Exception as e:
        print(f"Skipping {filepath}: parse error: {e}", file=sys.stderr)
        return None, [], []
    pkg = tree.package.name if tree.package else ""
    imports = [imp.path for imp in tree.imports]
    types = [node for _, node in tree.filter(javalang.tree.TypeDeclaration)
             if isinstance(node, (javalang.tree.ClassDeclaration, javalang.tree.InterfaceDeclaration))]
    return pkg, imports, types

def main(src_dir, output_file):
    src_dir = os.path.abspath(src_dir)  # normalize for reliable relpath
    nodes = []
    edges = []
    seen = {'package': set(), 'file': set(), 'class': set(), 'method': set(), 'field': set()}

    # directories to ignore
    exclude_dirs = {'venv', '.venv', '.git', '.github', '__pycache__'}

    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        for fname in files:
            if not fname.endswith('.java'):
                continue
            path = os.path.join(root, fname)
            pkg, imports, types = parse_java_file(path)
            # file node
            if path not in seen['file']:
                nodes.append({"id": path, "name": fname, "type": "file", "path": path})
                seen['file'].add(path)
            # package node and package->file
            if pkg:
                if pkg not in seen['package']:
                    nodes.append({"id": pkg, "name": pkg, "type": "package"})
                    seen['package'].add(pkg)
                edges.append({"source": pkg, "target": path, "type": "contains"})
            # import edges
            for imp in imports:
                if imp not in seen['package']:
                    nodes.append({"id": imp, "name": imp, "type": "package"})
                    seen['package'].add(imp)
                edges.append({"source": path, "target": imp, "type": "import"})
            # classes
            for cls in types:
                cls_name = cls.name
                fqcn = pkg + "." + cls_name if pkg else cls_name
                if fqcn not in seen['class']:
                    entry = {"id": fqcn, "name": cls_name, "type": "class", "package": pkg, "file": path}
                    if cls.extends:
                        if isinstance(cls.extends, list):
                            entry["extends"] = [e.name for e in cls.extends]
                        else:
                            entry["extends"] = cls.extends.name
                    if getattr(cls, 'implements', None):
                        entry["implements"] = [i.name for i in cls.implements]
                    nodes.append(entry)
                    seen['class'].add(fqcn)
                edges.append({"source": path, "target": fqcn, "type": "contains"})
                # inheritance
                if cls.extends:
                    if isinstance(cls.extends, list):
                        for parent_type in cls.extends:
                            parent = parent_type.name
                            pid = pkg + "." + parent if pkg else parent
                            if pid not in seen['class']:
                                nodes.append({"id": pid, "name": parent, "type": "class"})
                                seen['class'].add(pid)
                            edges.append({"source": fqcn, "target": pid, "type": "extends"})
                    else:
                        parent = cls.extends.name
                        pid = pkg + "." + parent if pkg else parent
                        if pid not in seen['class']:
                            nodes.append({"id": pid, "name": parent, "type": "class"})
                            seen['class'].add(pid)
                        edges.append({"source": fqcn, "target": pid, "type": "extends"})
                if getattr(cls, 'implements', None):
                    for impl in cls.implements:
                        name = impl.name
                        cid = pkg + "." + name if pkg else name
                        if cid not in seen['class']:
                            nodes.append({"id": cid, "name": name, "type": "interface"})
                            seen['class'].add(cid)
                        edges.append({"source": fqcn, "target": cid, "type": "implements"})
                # members
                for m in cls.body:
                    if isinstance(m, javalang.tree.FieldDeclaration):
                        tname = getattr(m.type, 'name', str(m.type))
                        for d in m.declarators:
                            fid = fqcn + "." + d.name
                            if fid not in seen['field']:
                                nodes.append({"id": fid, "name": d.name, "type": "field", "datatype": tname, "class": fqcn})
                                seen['field'].add(fid)
                            edges.append({"source": fqcn, "target": fid, "type": "contains"})
                    elif isinstance(m, javalang.tree.MethodDeclaration):
                        params = [p.type.name for p in m.parameters]
                        sig = m.name + "(" + ",".join(params) + ")"
                        mid = fqcn + "." + sig
                        if mid not in seen['method']:
                            nodes.append({"id": mid, "name": m.name, "type": "method", "signature": sig, "class": fqcn, "return_type": getattr(m.return_type, 'name', 'void')})
                            seen['method'].add(mid)
                        edges.append({"source": fqcn, "target": mid, "type": "contains"})
                        if m.body:
                            for _, inv in m.filter(javalang.tree.MethodInvocation):
                                qual = inv.qualifier + "." if inv.qualifier else ""
                                target = qual + inv.member
                                edges.append({"source": mid, "target": target, "type": "calls"})
    # build hierarchy
    node_map = {n['id']: dict(n, children=[]) for n in nodes}
    parent = {}
    for e in edges:
        if e['type'] == "contains":
            src, tgt = e['source'], e['target']
            if src in node_map and tgt in node_map:
                node_map[src]['children'].append(node_map[tgt])
                parent[tgt] = src
    roots = [node_map[n] for n in node_map if n not in parent and node_map[n]['type'] == "package"]
    if not roots:
        roots = [node_map[n] for n in node_map if n not in parent]
    rels = [e for e in edges if e['type'] != "contains"]

    # if output_file is a directory (modular output), not a single .json
    if not output_file.endswith('.json'):
        out_dir = output_file.rstrip(os.sep)
        os.makedirs(out_dir, exist_ok=True)

        # ──────────────────────────────────────────────────────────────
        # 1) Determine top‑level modules based on package *or* directory
        # ──────────────────────────────────────────────────────────────
        def _module_id(node):
            """
            Decide which top‑level module a node belongs to.

            • package nodes → first dotted segment
            • file nodes    → first directory under src_dir
            • class/method/field → package if present, else owning file dir
            """
            if node['type'] == 'package':
                return node['id'].split('.')[0]

            if node['type'] == 'file':
                fpath = os.path.abspath(node['id'])
                rel = os.path.relpath(fpath, src_dir)
                if rel.startswith('..'):
                    return 'root'
                return rel.split(os.sep)[0]

            # class / interface / method / field
            pkg = node.get('package')
            if pkg:
                return pkg.split('.')[0]

            fpath = node.get('file') or node.get('path')
            if fpath:
                rel = os.path.relpath(fpath, src_dir)
                return rel.split(os.sep)[0]

            return 'root'

        # Build module → node‑id list map
        top_pkg_map = {}
        for n in nodes:
            mod = _module_id(n)
            top_pkg_map.setdefault(mod, []).append(n['id'])

        written_modules = []

        # ──────────────────────────────────────────────────────────────
        # 2) For each module write its own JSON file
        # ──────────────────────────────────────────────────────────────
        for top_pkg, pkg_ids in top_pkg_map.items():
            # ──────────────────────────────────────────────────────────────
            # Build a self‑contained hierarchy *inside* this module
            # ──────────────────────────────────────────────────────────────
            module_nodes = [n for n in nodes if _module_id(n) == top_pkg]
            module_node_ids = {n['id'] for n in module_nodes}

            module_edges = [
                e for e in edges
                if e['source'] in module_node_ids and e['target'] in module_node_ids
            ]

            # Rebuild parent/child links local to the module
            mod_node_map = {n['id']: dict(n, children=[]) for n in module_nodes}
            mod_parent = {}
            for e in module_edges:
                if e['type'] == "contains":
                    s, t = e['source'], e['target']
                    mod_node_map[s]['children'].append(mod_node_map[t])
                    mod_parent[t] = s

            sub_hierarchy = [mod_node_map[nid] for nid in module_node_ids if nid not in mod_parent]
            sub_edges = [e for e in module_edges if e['type'] != "contains"]

            # Safe filename: dots → underscores
            fname = f"{top_pkg.replace('.', '_')}.json"
            with open(os.path.join(out_dir, fname), 'w', encoding='utf-8') as f:
                json.dump({"hierarchy": sub_hierarchy, "edges": sub_edges}, f, indent=2)

            written_modules.append(top_pkg)

        # ──────────────────────────────────────────────────────────────
        # 3) Write module index
        # ──────────────────────────────────────────────────────────────
        with open(os.path.join(out_dir, 'index.json'), 'w', encoding='utf-8') as f:
            json.dump(sorted(written_modules), f, indent=2)

        print(f"Modular graphs saved under {out_dir}")
    else:
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2)
        print(f"Knowledge graph saved to {output_file}")

if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "."
    out = sys.argv[2] if len(sys.argv) > 2 else "knowledge_graph.json"
    main(src, out)
