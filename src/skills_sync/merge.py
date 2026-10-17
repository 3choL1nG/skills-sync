"""三方合并：文本按行 diff3 合并；二进制或重叠冲突保留双方候选。"""

from __future__ import annotations

import difflib

BINARY = "binary"
MAX_DIFF_LINES = 4000


def _decode_text(data: bytes):
    """返回 (文本, 是否带 BOM)；非文本返回 None。"""
    if b"\0" in data:
        return None
    bom = data.startswith(b"\xef\xbb\xbf")
    body = data[3:] if bom else data
    try:
        return body.decode("utf-8"), bom
    except UnicodeDecodeError:
        return None


def _changes(base, side):
    sm = difflib.SequenceMatcher(None, base, side, autojunk=False)
    return [(i1, i2, j1, j2) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]


def _overlaps(cluster_start, cluster_end, event_start, event_end):
    if event_start < cluster_end:
        return True
    if cluster_start == cluster_end and event_start == event_end and cluster_start == event_start:
        return True
    return False


def diff3(base: list[str], local: list[str], remote: list[str]) -> tuple[list[str], list[dict]]:
    """标准三路按行合并；返回 (结果行, 冲突区域列表)。"""
    events = []
    for i1, i2, j1, j2 in _changes(base, local):
        events.append({"side": "local", "bs": i1, "be": i2, "ss": j1, "se": j2, "ins": i1 == i2})
    for i1, i2, j1, j2 in _changes(base, remote):
        events.append({"side": "remote", "bs": i1, "be": i2, "ss": j1, "se": j2, "ins": i1 == i2})
    events.sort(key=lambda e: (e["bs"], 0 if e["ins"] else 1))

    clusters: list[list[dict]] = []
    for event in events:
        if clusters:
            cluster = clusters[-1]
            cs = min(e["bs"] for e in cluster)
            ce = max(e["be"] for e in cluster)
            if _overlaps(cs, ce, event["bs"], event["be"]):
                cluster.append(event)
                continue
        clusters.append([event])

    out: list[str] = []
    conflicts: list[dict] = []
    bcur = lcur = rcur = 0
    for cluster in clusters:
        bs = min(e["bs"] for e in cluster)
        be = max(e["be"] for e in cluster)
        lstart = lcur + (bs - bcur)
        rstart = rcur + (bs - bcur)
        ldelta = sum((e["se"] - e["ss"]) - (e["be"] - e["bs"]) for e in cluster if e["side"] == "local")
        rdelta = sum((e["se"] - e["ss"]) - (e["be"] - e["bs"]) for e in cluster if e["side"] == "remote")
        lend = lstart + (be - bs) + ldelta
        rend = rstart + (be - bs) + rdelta
        local_slice = local[lstart:lend]
        remote_slice = remote[rstart:rend]
        base_slice = base[bs:be]
        out.extend(local[lcur:lstart])
        if local_slice == remote_slice:
            out.extend(local_slice)
        elif local_slice == base_slice:
            out.extend(remote_slice)
        elif remote_slice == base_slice:
            out.extend(local_slice)
        else:
            conflicts.append({
                "base_start": bs,
                "base_end": be,
                "base": list(base_slice),
                "local": list(local_slice),
                "remote": list(remote_slice),
            })
            out.extend(local_slice)
        bcur, lcur, rcur = be, lend, rend
    out.extend(local[lcur:])
    return out, conflicts


def merge_text(base_bytes: bytes, local_bytes: bytes, remote_bytes: bytes) -> dict:
    """尝试文本合并；任一侧非 UTF-8 文本则返回 binary。"""
    if base_bytes == local_bytes or base_bytes == remote_bytes:
        # 调用方通常已处理，这里兜底
        return {"kind": "clean", "data": local_bytes if base_bytes == remote_bytes else remote_bytes}
    decoded = [_decode_text(x) for x in (base_bytes, local_bytes, remote_bytes)]
    if any(item is None for item in decoded):
        return {"kind": BINARY}
    base_text, local_text, remote_text = (item[0] for item in decoded)
    bom = decoded[1][1]
    base_lines = base_text.splitlines(keepends=True)
    local_lines = local_text.splitlines(keepends=True)
    remote_lines = remote_text.splitlines(keepends=True)
    merged, conflicts = diff3(base_lines, local_lines, remote_lines)
    text = "".join(merged)
    if conflicts:
        return {"kind": "conflict", "conflicts": conflicts, "local_text": local_text,
                "remote_text": remote_text, "base_text": base_text}
    data = text.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    return {"kind": "clean", "data": data, "merged_text": text}


def plan_tree_merge(*, base_dir, local_dir, remote_dir, base_files: dict, local_files: dict,
                    remote_files: dict) -> dict:
    """按文件对比两端与基线，生成可执行合并操作。

    返回 {status: "clean"|"conflict", ops: [...], files: {...}, conflicts: [...], diff: str}
    ops: {path, action: keep|copy_remote|delete|write, source, data}
    """
    ops: list[dict] = []
    conflicts: list[dict] = []
    result_files: dict[str, dict] = {}
    all_paths = sorted(set(base_files) | set(local_files) | set(remote_files))
    for rel in all_paths:
        b = base_files.get(rel)
        l = local_files.get(rel)
        r = remote_files.get(rel)
        bh = b["hash"] if b else None
        lh = l["hash"] if l else None
        rh = r["hash"] if r else None

        if lh == bh and rh == bh:
            if l is not None:
                result_files[rel] = dict(l)
            continue
        if lh == bh:
            if rh is None:
                ops.append({"path": rel, "action": "delete"})
            elif lh == rh:
                result_files[rel] = dict(l)
            else:
                ops.append({"path": rel, "action": "copy_remote"})
                result_files[rel] = dict(r)
            continue
        if rh == bh:
            if lh is None:
                ops.append({"path": rel, "action": "delete"})
            else:
                result_files[rel] = dict(l)
            continue
        if lh == rh:
            result_files[rel] = dict(l)
            continue

        if l is not None and r is not None and b is not None:
            base_bytes = (base_dir / rel).read_bytes()
            local_bytes = (local_dir / rel).read_bytes()
            remote_bytes = (remote_dir / rel).read_bytes()
            merged = merge_text(base_bytes, local_bytes, remote_bytes)
            if merged["kind"] == "clean":
                data = merged["data"]
                import hashlib

                ops.append({"path": rel, "action": "write", "data": data})
                result_files[rel] = {"hash": hashlib.sha256(data).hexdigest(), "size": len(data)}
                continue
            if merged["kind"] == "conflict":
                conflicts.append({
                    "path": rel,
                    "kind": "text-overlap",
                    "detail": f"{len(merged['conflicts'])} 处重叠改动无法确定合并",
                    "hunks": merged["conflicts"],
                })
                continue
            conflicts.append({"path": rel, "kind": BINARY, "detail": "二进制文件两端内容不同"})
            continue

        if b is None:
            conflicts.append({"path": rel, "kind": "add-add", "detail": "两端各自新增且内容不同（无共同基线）"})
        elif l is None or r is None:
            conflicts.append({"path": rel, "kind": "delete-modify", "detail": "一端删除、另一端修改"})
        else:  # pragma: no cover - 覆盖在文本/二进制判断中
            conflicts.append({"path": rel, "kind": "modify-modify", "detail": "两端修改同一文件"})

    status = "conflict" if conflicts else "clean"
    return {
        "status": status,
        "ops": ops,
        "files": result_files,
        "conflicts": conflicts,
        "diff": build_diff(local_dir, remote_dir, local_files, remote_files, conflicts),
    }


def build_diff(local_dir, remote_dir, local_files: dict, remote_files: dict, conflicts: list[dict]) -> str:
    """为冲突生成可读 diff（本地 vs 远端），供用户决策。"""
    lines: list[str] = []
    for item in conflicts:
        rel = item["path"]
        lines.append(f"=== 冲突文件：{rel}（{item['kind']}：{item['detail']}）")
        lf = local_dir / rel
        rf = remote_dir / rel
        if not lf.exists():
            lines.append("--- 本地：文件不存在")
        if not rf.exists():
            lines.append("--- 远端：文件不存在")
        if lf.exists() and rf.exists():
            local_text = _read_text_limited(lf)
            remote_text = _read_text_limited(rf)
            if local_text is None or remote_text is None:
                lines.append(f"（二进制文件，本地 {lf.stat().st_size} 字节 / 远端 {rf.stat().st_size} 字节）")
            else:
                diff = difflib.unified_diff(
                    local_text.splitlines(keepends=True),
                    remote_text.splitlines(keepends=True),
                    fromfile=f"local/{rel}",
                    tofile=f"remote/{rel}",
                    n=3,
                )
                for line in diff:
                    lines.append(line.rstrip("\n"))
                    if len(lines) > MAX_DIFF_LINES:
                        lines.append("...（diff 已截断）")
                        return "\n".join(lines) + "\n"
        lines.append("")
    return "\n".join(lines) + ("\n" if lines else "")


def _read_text_limited(path, limit: int = 512 * 1024):
    try:
        if path.stat().st_size > limit:
            return None
        return path.read_text("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def apply_merge_ops(local_dir, remote_dir, target_dir, ops: list[dict]) -> None:
    """把合并操作落到 target_dir（先复制本地，再套用 remote/write/delete）。"""
    import shutil

    util_copy_tree(local_dir, target_dir)
    for op in ops:
        rel = op["path"]
        target = target_dir / rel
        if op["action"] == "delete":
            if target.exists():
                target.unlink()
        elif op["action"] == "copy_remote":
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(remote_dir / rel, target)
        elif op["action"] == "write":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(op["data"])


def util_copy_tree(src, dst):
    from .util import copy_tree

    copy_tree(src, dst)
