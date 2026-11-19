#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分片合片（§6.8.3 / L2 容错）。

硬约束（都是实测换来的，改代码前先读）：
- **K22**：合片前必须校验各分片编码参数一致，否则 ``-c copy`` 会产出
  **音画不同步的坏文件且不报错**。不一致时**默认不重编码**，分开放置并标记。
- **K23**：时长一律用 ffprobe 实读，**不能用「段数 × 3600」推算**（段长只对齐关键帧，K21）。
- **K8 / K24**：目录链必须 ASCII 英文（否则 av 库读 WAV 失败）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..common.paths import ws

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


@dataclass
class MergeResult:
    ok: bool
    output: Path | None = None
    totalDurationSec: float = 0.0
    mergedDurationSec: float = 0.0
    deltaSec: float = 0.0
    partCount: int = 0
    reason: str = ""
    signatures: list[str] = field(default_factory=list)
    # v3.57：这一组成片的**实际画面尺寸**（按分辨率分组后每组可能不同）
    width: int = 0
    height: int = 0
    # 这一组实际包含的分片名（给上层把"精确起止时间"对回去用）
    partNames: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "output": str(self.output) if self.output else None,
            "totalDurationSec": round(self.totalDurationSec, 3),
            "mergedDurationSec": round(self.mergedDurationSec, 3),
            "deltaSec": round(self.deltaSec, 3),
            "partCount": self.partCount,
            "reason": self.reason,
            "signatures": self.signatures,
            "width": self.width,
            "height": self.height,
            "partNames": self.partNames,
        }


def probe(path: Path) -> dict:
    """读分片的时长与编码参数。读不出来返回 ``{"error": ...}``。"""
    r = subprocess.run(
        [str(ws.ffprobe), "-v", "error", "-show_entries",
         "format=duration:stream=codec_name,codec_type,width,height,r_frame_rate",
         "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=CREATE_NO_WINDOW)
    try:
        data = json.loads(r.stdout or "{}")
    except ValueError:
        return {"error": (r.stderr or "ffprobe 失败").strip()[:300]}
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    return {
        "duration": round(float(fmt.get("duration") or 0), 3),
        "sizeBytes": int(fmt.get("size") or 0),
        "video": f"{video.get('codec_name')} {video.get('width')}x{video.get('height')}",
        "fps": str(video.get("r_frame_rate")),
        "audio": str(audio.get("codec_name")),
        # v3.25：画幅识别要读原始宽高（不要从 "video" 字符串里再切一遍）
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
    }


def probe_duration(path: Path) -> float:
    """只要时长（K23：一切时长以实读为准）。"""
    return float(probe(path).get("duration") or 0.0)


def probe_stream_size(url: str, headers: str = "",
                      timeout: float = 20.0) -> tuple[int, int]:
    """探测**远端流**的视频宽高（录制开始前先知道画幅就靠它）。

    探不到返回 ``(0, 0)``，调用方按 unknown 处理（不因此中断录制）。

    ⚠️ 必须限制 ``analyzeduration/probesize``：默认会先吞掉一大段数据才出结论，
    每 30s 探一次的话等于额外拉一路流（K57）。1MB / 1s 足够拿到分辨率。
    """
    cmd = [str(ws.ffprobe), "-v", "error",
           "-analyzeduration", "1000000", "-probesize", "1000000",
           "-select_streams", "v:0"]
    if headers:
        cmd += ["-headers", headers]
    cmd += ["-show_entries", "stream=width,height", "-of", "json", str(url)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return (0, 0)
    try:
        streams = (json.loads(r.stdout or "{}").get("streams") or [])
    except ValueError:
        return (0, 0)
    for s in streams:
        try:
            w, h = int(s.get("width") or 0), int(s.get("height") or 0)
        except (TypeError, ValueError):
            continue
        if w > 0 and h > 0:
            return (w, h)
    return (0, 0)


#: v4.46：尺寸**等价容差**（像素 + 宽高比）。
#: 抖音常见 1088x1920 与 1080x1920 交替——1088 只是宏块对齐的补边，画面完全一样；
#: 以前按"精确相等"判 mixed ⇒ 整段 5 分钟被踢进 unmerged 变成 00000.ts（真机证据 2026-11-06）。
RES_SIZE_TOLERANCE_PX = 16
RES_ASPECT_TOLERANCE = 0.02


def sizes_compatible(a, b) -> bool:
    """两个尺寸是否**等价**（同画幅、边长差 ≤16px 视为同一规格）。"""
    if not a or not b:
        return True                        # 读不到 ⇒ 无法证明不同，按兼容处理（不拆组）
    aw, ah = int(a[0] or 0), int(a[1] or 0)
    bw, bh = int(b[0] or 0), int(b[1] or 0)
    if not (aw and ah and bw and bh):
        return False
    if abs(aw - bw) > RES_SIZE_TOLERANCE_PX or abs(ah - bh) > RES_SIZE_TOLERANCE_PX:
        return False
    ar_a, ar_b = aw / ah, bw / bh
    return abs(ar_a - ar_b) / max(ar_a, ar_b, 1e-9) <= RES_ASPECT_TOLERANCE


def spec_key(info: dict) -> str:
    """编码规格（**不含尺寸**）：编解码 / 帧率 / 音频 —— 分组时配 :func:`sizes_compatible`。"""
    return f"{str(info.get('video', '')).split(' ')[0]}|{info.get('fps')}|{info.get('audio')}"


def signature(info: dict, size: tuple[int, int] | None = None) -> str:
    """编码参数指纹，用于 K22 一致性校验。

    v3.57：``size`` 允许用**分片内部实测**的尺寸覆盖分片头里的尺寸 ——
    由于 TS 的节目信息只在起播时写一次，一个"中途变过分辨率"的分片，
    它的头可能还写着**旧尺寸**（见 :func:`sample_sizes` 的取证），
    拿头去做 K22 校验会漏判（K100）。
    """
    if size and size[0] and size[1]:
        video = f"{info.get('video', '').split(' ')[0]} {size[0]}x{size[1]}"
    else:
        video = info.get("video")
    return f"{video}|{info.get('fps')}|{info.get('audio')}"


# ---------------------------------------------------------------- 闸 3：分片内部体检

RES_SAMPLE_POINTS = (0.02, 0.50, 0.90)
RES_SAMPLE_BACKOFF = (0.4, 1.5, 3.0)
RES_SAMPLE_TIMEOUT = 60.0
_SHOWINFO_SIZE_RE = re.compile(r"\bs:(\d{2,5})x(\d{2,5})\b")


def frame_size_at(path: Path, seconds: float,
                  timeout: float = RES_SAMPLE_TIMEOUT) -> tuple[int, int] | None:
    """读单个分片在 ``seconds`` 处的**帧**尺寸；读不到返回 ``None``（不猜）。

    为什么不能直接用 ffprobe 的 ``-read_intervals``：本机 ffmpeg（N-126308）上
    ``-read_intervals "T%+#1"`` 对本眼角感的 TS **只有第一个点能读出**，
    中/尾一律返回 0 帧（实测证据见 §6.8.2c）；而 ffprobe 又不支持 ``-ss``。
    所以改用 ffmpeg 自己的 seek + ``showinfo`` —— **唯一出处**在这里，别各处各写一套。
    """
    if not path or not Path(path).is_file():
        return None
    cmd = [str(ws.ffmpeg), "-hide_banner", "-nostdin", "-v", "info",
           "-ss", f"{max(0.0, float(seconds)):.3f}", "-i", str(path),
           "-an", "-sn", "-dn", "-frames:v", "1", "-vf", "showinfo", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    m = _SHOWINFO_SIZE_RE.search(r.stderr or "")
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    return (w, h) if w > 0 and h > 0 else None


def sample_sizes(path: Path, duration: float,
                 points: tuple[float, ...] = RES_SAMPLE_POINTS,
                 timeout: float = RES_SAMPLE_TIMEOUT) -> list[tuple[int, int] | None]:
    """在分片内部按**头/中/尾**三点采样（K100 闸 3 的输入）。

    读不到的点保留 ``None``：**读不到 ≠ 发生了变化**，绝不把它当成某种尺寸。
    靠末尾时 ffmpeg 常常一帧都出不来，这时往前挪一点再试（见 :data:`RES_SAMPLE_BACKOFF`）。
    """
    out: list[tuple[int, int] | None] = []
    span = max(0.0, float(duration or 0))
    for frac in points:
        target = span * frac
        got = frame_size_at(path, target, timeout=timeout)
        for back in RES_SAMPLE_BACKOFF:
            if got is not None:
                break
            retry = target - back
            if retry <= 0.05:
                break
            got = frame_size_at(path, retry, timeout=timeout)
        out.append(got)
    return out


def part_profile(info: dict, sizes: list[tuple[int, int] | None]) -> dict:
    """单个分片的体检结论：``{sizes, readable, mixed}``。

    ``mixed=True`` 表示**这个分片自己内部**就有两种以上分辨率 ——
    K22 那种"按分片比对签名"的校验只看分片头，查不出这种情况。
    """
    raw = [tuple(s) for s in sizes if s]
    # v4.46：把"等价尺寸"（1088x1920 / 1080x1920 这种宏块补边差）合并成一种规格
    clusters: list[list] = []          # [代表尺寸, 出现次数]
    for s in raw:
        for c in clusters:
            if sizes_compatible(c[0], s):
                c[0] = (max(c[0][0], s[0]), max(c[0][1], s[1]))
                c[1] += 1
                break
        else:
            clusters.append([s, 1])
    uniq = sorted({s for s in raw})
    return {
        "sizes": [list(s) for s in uniq],
        "readable": len(raw),
        "mixed": len(clusters) > 1,
        # 内部尺寸一致时以实测为准（比分片头可信）；不一致/读不到则回落分片头
        "size": (max(clusters, key=lambda c: c[1])[0] if len(clusters) == 1
                 else (int(info.get("width") or 0), int(info.get("height") or 0))),
    }


def plan_groups(parts: list[Path], infos: list[dict],
                profiles: list[dict]) -> tuple[list[list[int]], list[int]]:
    """按「相邻 + 规格一致」把分片切成若干组，并挑出**内部混了分辨率**的分片。

    返回 ``(groups, rejected)``，两者都是 ``parts`` 的下标。
    ``rejected`` 里的分片自己内部就混了分辨率，合进任何文件都会压扁 → 不入任何组。
    """
    rejected = [i for i, pr in enumerate(profiles) if pr.get("mixed")]
    rejected_set = set(rejected)
    groups: list[list[int]] = []
    cur: list[int] = []
    cur_spec = ""
    cur_size: tuple[int, int] | None = None
    for i, info in enumerate(infos):
        if i in rejected_set:
            if cur:
                groups.append(cur)
                cur, cur_spec, cur_size = [], "", None
            continue
        spec = spec_key(info)
        size = tuple(profiles[i].get("size") or ()) or None
        # v4.47b：分组要**精确尺寸**——concat copy 跨尺寸会产出截断文件（实测 32s→16.7s）。
        # 容差只用于「单个分片内部体检」（part_profile），不参与分组。
        if cur_spec and (spec != cur_spec or cur_size != size):
            groups.append(cur)
            cur, cur_spec, cur_size = [], "", None
        cur.append(i)
        cur_spec = spec
        cur_size = size if size else cur_size
    if cur:
        groups.append(cur)
    return groups, rejected


def _group_path(output: Path, index: int) -> Path:
    """第 index 组的输出路径。第 0 组**不加任何标记**（老命名不变）。

    多出来的组把 ``_g2/_g3`` 插在**画幅后缀之前**，这样
    ``recorder_manager._ASPECT_SUFFIX_RE``（``_(169|916|34|43)$``）依然能反查到画幅。
    """
    if index <= 0:
        return output
    stem, tag = output.stem, ""
    m = re.search(r"_(169|916|34|43)$", stem)
    if m:
        tag = m.group(0)
        stem = stem[: m.start()]
    return output.with_name(f"{stem}_g{index + 1}{tag}{output.suffix}")


def merge_parts(parts: list[Path], output: Path) -> MergeResult:
    """把若干分片按序合并为一个文件。零重编码（``-c copy``）。

    ``parts`` 必须已按时间顺序排好。合片成功且时长校验通过后才算 ok。
    单分片时直接移动，不做无谓的 remux。
    """
    result = MergeResult(ok=False, partCount=len(parts),
                         partNames=[Path(p).name for p in parts])
    if not parts:
        result.reason = "没有分片"
        return result

    infos = [probe(p) for p in parts]
    broken = [(p.name, i.get("error")) for p, i in zip(parts, infos) if "error" in i]
    if broken:
        result.reason = f"分片不可读（可能录制被强杀，moov 未写出）：{broken}"
        return result
    result.width = int(infos[0].get("width") or 0)
    result.height = int(infos[0].get("height") or 0)

    sigs = [signature(i) for i in infos]
    result.signatures = sorted(set(sigs))
    result.totalDurationSec = sum(float(i.get("duration") or 0) for i in infos)

    # K22（v4.47b）：多分片合片要**编码规格 + 尺寸都完全一致**——
    # concat copy 跨尺寸会截断（实测 32s→16.7s）。单分片走 remux，天然不受此限。
    first = infos[0]
    first_spec = spec_key(first)
    first_size = (result.width, result.height)
    for info in infos[1:]:
        if spec_key(info) != first_spec or first_size != (
                int(info.get("width") or 0), int(info.get("height") or 0)):
            result.reason = ("分片编码参数不一致（平台中途改了画质/分辨率），"
                             "已放弃合片以免产出音画不同步的坏文件："
                             + " / ".join(result.signatures))
            return result

    output.parent.mkdir(parents=True, exist_ok=True)
    if len(parts) == 1:
        # 单分片：TS → MP4 remux（K25：录制用 TS 保证崩溃安全，出片必须是 MP4）
        r = subprocess.run(
            [str(ws.ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(parts[0]), "-c", "copy", "-movflags", "+faststart",
             str(output)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW, timeout=3600)
        if r.returncode != 0 or not output.is_file():
            result.reason = (f"TS→MP4 remux 失败 rc={r.returncode} "
                             f"{(r.stderr or '').strip()[-300:]}")
            return result
        result.ok = True
        result.output = output
        result.mergedDurationSec = probe_duration(output)
        result.deltaSec = round(result.mergedDurationSec - result.totalDurationSec, 3)
        for p in parts:
            try:
                p.unlink()
            except OSError:
                pass
        return result

    list_path = output.parent / f".{output.stem}.concat.txt"
    list_path.write_text(
        "\n".join(f"file '{p.as_posix()}'" for p in parts) + "\n", encoding="utf-8")
    try:
        # 注意：concat 的输入是本地列表文件，不需要（也不能）带 -headers
        cmd = [str(ws.ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
               "-f", "concat", "-safe", "0", "-i", str(list_path),
               "-c", "copy", "-movflags", "+faststart", str(output)]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", creationflags=CREATE_NO_WINDOW, timeout=3600)
    finally:
        list_path.unlink(missing_ok=True)

    if r.returncode != 0 or not output.is_file():
        result.reason = f"ffmpeg 合片失败 rc={r.returncode} {(r.stderr or '').strip()[-300:]}"
        return result

    result.mergedDurationSec = probe_duration(output)
    result.deltaSec = round(result.mergedDurationSec - result.totalDurationSec, 3)
    # K23：合片后再次实读校验。允许 1.5s 误差（关键帧对齐 + 容器开销）
    if abs(result.deltaSec) > 1.5:
        result.reason = (f"合片后时长异常：合并后 {result.mergedDurationSec}s vs "
                         f"分片累计 {result.totalDurationSec}s（差 {result.deltaSec}s）")
        return result

    result.ok = True
    result.output = output
    # 合片成功且校验通过 → 才删分片（§6.8.3）
    for p in parts:
        try:
            p.unlink()
        except OSError:
            pass
    return result


# ---------------------------------------------------------------- 分组合片（v3.57 闸 3）

def inspect_parts(parts: list[Path], sample: bool = True) -> dict:
    """体检一批分片：读参数 → 内部三点采样 → 按相邻同规格分组。

    返回 ``{infos, profiles, groups, rejected}``。
    ``sample=False`` 时跳过内部采样（快，用于不关心压扁的场合 / 单元测试替身）。
    """
    infos = [probe(p) for p in parts]
    profiles: list[dict] = []
    for p, info in zip(parts, infos):
        sizes = sample_sizes(p, float(info.get("duration") or 0)) if sample else []
        profiles.append(part_profile(info, sizes))
    groups, rejected = plan_groups(parts, infos, profiles)
    return {"infos": infos, "profiles": profiles,
            "groups": groups, "rejected": rejected}


def merge_parts_grouped(parts: list[Path], output: Path,
                        sample: bool = True,
                        rename=None) -> tuple[list[MergeResult], list[Path], str]:
    """合一个槽位；**允许**产出多个文件（按分辨率分组），返回被剔除的分片。

    为什么不像以前那样"参数不一致就整槽不合"：
        一个小时槽位里中途变了分辨率时，以前的做法会把**整段素材**丢进 unmerged；
        现在按"相邻同规格"切成几组各自成品这样一个都不能少，
        而且每个成品内部只有一种分辨率 —— **素材库里不会出现压扁的文件**（K100）。

    ``rename(index, width, height, output)`` 可以让调用方按每组**实际尺寸**决定文件名
    （画幅后缀要跟着组走，不然 `录制库` 反查出来的画幅是错的）。
    不传就用 :func:`_group_path`（第 0 组保持原名，其余加 ``_g2/_g3``）。

    ``rejected`` 里的分片是「它自己内部就混了分辨率」的那种 —— 合进任何文件都会压扁，
    只能原样留给人工处理（**不删**，K55）。
    """
    if not parts:
        bad = MergeResult(ok=False, partCount=0, reason="没有分片")
        return ([bad], [], "没有分片")
    report = inspect_parts(parts, sample=sample)
    groups, rejected = report["groups"], report["rejected"]
    rejected_paths = [parts[i] for i in rejected]

    results: list[MergeResult] = []
    for k, idxs in enumerate(groups):
        group = [parts[i] for i in idxs]
        profile = report["profiles"][idxs[0]]
        w, h = profile.get("size") or (0, 0)
        w, h = int(w or 0), int(h or 0)
        path = rename(k, w, h, output) if rename else _group_path(output, k)
        res = merge_parts(group, path)
        # 组尺寸以**实测**为准（分片头在变过分辨率的分片上不可信）
        res.width, res.height = w, h
        results.append(res)

    notes = ""
    if rejected:
        detail = "; ".join(
            f"{parts[i].name}：内部存在多种分辨率 "
            f"{' → '.join(f'{a}x{b}' for a, b in report['profiles'][i]['sizes'])}"
            for i in rejected)
        notes = ("有分片内部变了分辨率（合进去会压扁），未参与合片："
                 + detail)
    return (results, rejected_paths, notes)
