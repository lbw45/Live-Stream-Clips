"""外网总闸（**唯一出处**）：`SLICE_DISABLE_AUTO` 到底怎么判。

为什么抽出来（v3.59，）
--------------------------------
这个开关原先在两处各写了一份判定逻辑：`recorder_manager._net_enabled()`
与 `auto_pipeline.DISABLE_ENV`；别处还要用第三次。**同一件事写三份必然漂移**
（K44 的老教训），而这道闸一旦漂移，后果是「冒烟里真去敲外部站点」
——正是 v3.50 那次事故的形态（K91）。

闸的含义：**用户动作之外自发发起的外部请求**，都要过这里。
用户明确点一下的动作（点「开始下载」）不在此列，但仍必须带**频率闸门**
（见各模块的退避/冷却实现）。
"""
from __future__ import annotations

import os

#: 环境变量名。设成非空 = 关掉一切自动外呼（冒烟、离线演示用）。
DISABLE_ENV = "SLICE_DISABLE_AUTO"


def net_enabled() -> bool:
    """是否允许真的去外网。"""
    return not os.environ.get(DISABLE_ENV)


def disabled_reason() -> str:
    """给用户看的一句中文说明（K69：拦下来了就要说清为什么）。"""
    return (f"离线模式已开启（环境变量 {DISABLE_ENV}），本次不会发起任何外部请求。"
            "关掉它再试，或改用本机已有素材。")
